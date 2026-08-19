"""Slack Socket Mode listener — the @mention bot without a public URL.

Connects to Slack over a WebSocket using the app-level token (xapp-…) and hands events to
Celery: `app_mention` → `handle_mention` (agentic, thread memory); and, when passive replies
are enabled, plain `message` events → `handle_passive_message` (confidence-gated). Same tasks
the HTTP Events endpoint uses. Run as its own process: `python -m app.slackbot`.
"""

from __future__ import annotations

from threading import Event

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from app.core.config import settings
from app.core.logging import configure_logging, get_logger, new_correlation_id

log = get_logger(__name__)


def _on_request(client: SocketModeClient, req: SocketModeRequest) -> None:
    # Always ACK first so Slack doesn't retry.
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type != "events_api":
        return
    event = (req.payload or {}).get("event", {}) or {}
    if event.get("type") == "app_mention" and not event.get("bot_id"):
        new_correlation_id()
        from app.workers.tasks import handle_mention

        handle_mention.delay(event)  # the worker does the work + posts the reply
        return
    # Ambient path: un-mentioned messages, replied to only when the bot is confident.
    from app.slackbot.passive import should_consider

    if should_consider(event):
        new_correlation_id()
        from app.workers.tasks import handle_passive_message

        handle_passive_message.delay(event)


def main() -> None:
    configure_logging(settings.log_level)
    if not settings.slack_app_token:
        raise SystemExit("SLACK_APP_TOKEN (xapp-…) is required for Socket Mode")
    client = SocketModeClient(
        app_token=settings.slack_app_token,
        web_client=WebClient(token=settings.slack_bot_token),
    )
    client.socket_mode_request_listeners.append(_on_request)
    client.connect()
    log.info("slackbot connected (socket mode)")
    Event().wait()  # block forever


if __name__ == "__main__":
    main()
