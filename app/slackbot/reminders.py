"""Reminder scheduling for the Slack agent.

The model turns natural language ("kal 5 baje", "in 2 hours") into an absolute local
time; this module validates it, resolves an optional target person, stores the row, and
renders the user-facing confirmations. Delivery happens in the `deliver_due_reminders`
Celery task.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logging import get_logger
from app.storage.db import session_scope
from app.storage.db.repositories import (
    add_reminder,
    cancel_reminder,
    pending_reminders_for,
    recent_reminders_for,
    user_id_by_name,
)

log = get_logger(__name__)

SOURCE = "slack"
_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.reminder_timezone)
    except Exception:  # noqa: BLE001 - a bad tz name must not break the bot
        return ZoneInfo("UTC")


def _parse_local(when_local: str) -> datetime | None:
    """'YYYY-MM-DD HH:MM' in the configured timezone → an aware UTC datetime."""
    raw = (when_local or "").strip().replace("Z", "")
    for fmt in _FORMATS:
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=_tz()).astimezone(timezone.utc)
    return None


def _local(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%d %b %Y, %H:%M")


def schedule_reminder(
    *, requester_user_id: str, channel_id: str, thread_ts: str | None,
    when_local: str, text: str, target_name: str | None = None,
) -> str:
    if not settings.enable_reminders:
        return "Reminders are turned off."
    if not requester_user_id:
        return "I couldn't tell who's asking, so I can't set that reminder."
    when_utc = _parse_local(when_local)
    if not when_utc:
        return (f"I couldn't read the time '{when_local}'. Tell me a clear time, "
                "e.g. 'kal 5 baje' or 'tomorrow 17:00'.")
    if when_utc <= datetime.now(timezone.utc):
        return f"That time ({_local(when_utc)}) is already past — give me a future time."
    if not (text or "").strip():
        return "What should I remind about?"

    target_id = None
    if target_name:
        with session_scope() as session:
            target_id = user_id_by_name(session, SOURCE, target_name)
        if not target_id:
            return (f"I couldn't find anyone called '{target_name}' in this workspace. "
                    "Use their exact Slack display name.")

    with session_scope() as session:
        row = add_reminder(
            session, requester_user_id=requester_user_id, target_user_id=target_id,
            channel_id=channel_id, thread_ts=thread_ts, text=text.strip(),
            remind_at=when_utc,
        )
        rid = row.id
    who = f" to <@{target_id}>" if target_id else ""
    return f"Reminder #{rid} set for {_local(when_utc)}{who}: {text.strip()}"


def render_pending(user_id: str, include_done: bool = False) -> str:
    """Upcoming reminders, or (include_done) the latest ones whatever their status."""
    if not user_id:
        return "I couldn't tell who's asking."
    with session_scope() as session:
        source = recent_reminders_for if include_done else pending_reminders_for
        rows = [
            (r.id, r.remind_at, r.text, r.target_user_id, r.status)
            for r in source(session, user_id)
        ]
    if not rows:
        return ("You have no reminders yet." if include_done
                else "You have no pending reminders.")
    lines = [
        f"- #{rid} · {_local(when)}"
        + (f" · for <@{target}>" if target else "")
        + (f" · {status}" if include_done else "")
        + f" — {text}"
        for rid, when, text, target, status in rows
    ]
    header = "Your recent reminders:" if include_done else "Your pending reminders:"
    return header + "\n" + "\n".join(lines)


def cancel(user_id: str, reminder_id: int) -> str:
    with session_scope() as session:
        ok = cancel_reminder(session, reminder_id, user_id)
    return (f"Cancelled reminder #{reminder_id}." if ok
            else f"I couldn't cancel #{reminder_id} — it isn't yours or already fired.")
