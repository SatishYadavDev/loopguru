"""Slack Events API endpoint for the @mention bot.

Verifies Slack's request signature, answers the url_verification handshake, and hands
`app_mention` events to a Celery task (fast-ACK so Slack doesn't retry).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["slack"])


def _verify(body: bytes, timestamp: str, signature: str) -> bool:
    if not settings.slack_signing_secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:  # replay guard (5 min)
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    mac = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(mac, signature)


@router.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    if not _verify(
        body,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

    data = json.loads(body or b"{}")
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    # Slack retries on slow/failed delivery — ignore retries to avoid double replies.
    if request.headers.get("X-Slack-Retry-Num"):
        return {"ok": True}

    event = data.get("event", {})
    if event.get("type") == "app_mention" and not event.get("bot_id"):
        from app.workers.tasks import handle_mention

        handle_mention.delay(event)
        return {"ok": True}

    # Direct messages: every DM is addressed to the bot, no @mention needed.
    if (event.get("type") == "message" and event.get("channel_type") == "im"
            and not event.get("bot_id") and not event.get("subtype")):
        from app.workers.tasks import handle_dm

        handle_dm.delay(event)
        return {"ok": True}

    # Ambient path: un-mentioned messages, replied to only when the bot is confident.
    from app.slackbot.passive import should_consider

    if should_consider(event):
        from app.workers.tasks import handle_passive_message

        handle_passive_message.delay(event)
    return {"ok": True}
