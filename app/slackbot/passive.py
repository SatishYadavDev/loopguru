"""Ambient (passive) reply gate.

Decides whether a plain `message` event — one the user did NOT @mention the bot in —
should be considered for an unsolicited, confidence-gated reply. The actual answering
(and the confidence check) happens in the `handle_passive_message` Celery task; this
module only does the cheap up-front filtering so junk never reaches a query.
"""

from __future__ import annotations

import re

from app.core.config import settings

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def should_consider(event: dict) -> bool:
    """Cheap, side-effect-free filter for a Slack `message` event.

    Returns True only for a fresh, top-level, human message in an allowlisted channel
    that does not already @mention the bot (mentions go through the agentic path).
    """
    if not settings.enable_passive_reply:
        return False
    if event.get("type") != "message":
        return False
    # Skip edits, joins, channel_topic, bot messages, thread_broadcasts, etc.
    if event.get("subtype"):
        return False
    if event.get("bot_id"):
        return False
    # Top-level only: a threaded reply carries thread_ts != ts.
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        return False
    channel = event.get("channel", "")
    if settings.slack_channels and channel not in settings.slack_channels:
        return False
    text = (event.get("text") or "").strip()
    if len(text) < settings.passive_min_chars:
        return False
    # If the bot is mentioned, the app_mention path already handles it.
    mentioned = set(_MENTION_RE.findall(text))
    if settings.slack_bot_user_id and settings.slack_bot_user_id in mentioned:
        return False
    return True


def strip_mentions(text: str) -> str:
    """Remove `<@U…>` mention tokens, leaving the plain question text."""
    return _MENTION_RE.sub("", text or "").strip()
