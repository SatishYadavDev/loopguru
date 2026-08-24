"""Celery tasks (PRD §16): per-scope sync/backfill/sweep/purge + scheduled sweeps."""

from __future__ import annotations

import time

from app.core.config import settings
from app.core.logging import get_logger, new_correlation_id
from app.schemas.connector import ScopeStatus
from app.workers.alerts import send_alert
from app.workers.celery_app import celery_app
from app.workers.sync import SOURCE, SyncEngine, prioritized_channels, resolve_scope

log = get_logger(__name__)


@celery_app.task(name="app.workers.tasks.full_backfill", bind=True, max_retries=3)
def full_backfill(self, channel_id: str) -> None:
    new_correlation_id()
    engine = SyncEngine()
    engine.prepare()
    scope = resolve_scope(engine, channel_id)
    if scope.status != ScopeStatus.OK:
        log.warning("skip backfill: scope not ok",
                    extra={"scope_id": channel_id, "status": scope.status.value})
        return
    engine.backfill(scope, sweep=False)


@celery_app.task(name="app.workers.tasks.sync_scope", bind=True, max_retries=3)
def sync_scope(self, channel_id: str) -> None:
    new_correlation_id()
    engine = SyncEngine()
    engine.prepare()
    scope = resolve_scope(engine, channel_id)
    if scope.status != ScopeStatus.OK:
        log.warning("skip incremental: scope not ok",
                    extra={"scope_id": channel_id, "status": scope.status.value})
        return
    engine.incremental(scope)


@celery_app.task(name="app.workers.tasks.sweep_scope", bind=True, max_retries=2)
def sweep_scope(self, channel_id: str) -> None:
    new_correlation_id()
    engine = SyncEngine()
    engine.prepare()
    scope = resolve_scope(engine, channel_id)
    if scope.status != ScopeStatus.OK:
        log.warning("skip sweep: scope not ok", extra={"scope_id": channel_id})
        return
    engine.backfill(scope, sweep=True)


@celery_app.task(name="app.workers.tasks.purge")
def purge(doc_id: str | None = None, channel_id: str | None = None) -> int:
    new_correlation_id()
    engine = SyncEngine()
    engine.prepare()
    if doc_id:
        engine.purge_doc(doc_id)
        return 1
    if channel_id:
        return engine.purge_scope(channel_id)
    return 0


@celery_app.task(name="app.workers.tasks.daily_sweep")
def daily_sweep() -> None:
    """Bootstrap trigger (PRD §11.1): backfill pending scopes, incremental for completed."""
    new_correlation_id()
    from app.storage.db import session_scope
    from app.storage.db.repositories import get_or_create_sync_state

    for channel_id in prioritized_channels():
        with session_scope() as session:
            state = get_or_create_sync_state(session, SOURCE, channel_id)
            status = state.backfill_status
        if status in ("pending", "in_progress"):
            full_backfill.delay(channel_id)
        else:
            sync_scope.delay(channel_id)
    check_stale_scopes.delay()


@celery_app.task(name="app.workers.tasks.weekly_reconcile")
def weekly_reconcile() -> None:
    """Weekly full re-backfill/sweep for completed scopes (deletions + edit drift)."""
    new_correlation_id()
    from app.storage.db import session_scope
    from app.storage.db.repositories import get_or_create_sync_state

    for channel_id in prioritized_channels():
        with session_scope() as session:
            state = get_or_create_sync_state(session, SOURCE, channel_id)
            status = state.backfill_status
        if status == "completed":
            sweep_scope.delay(channel_id)


@celery_app.task(name="app.workers.tasks.handle_mention", bind=True, max_retries=1)
def handle_mention(self, event: dict) -> None:
    """Answer an @mention with the agentic loop (thread memory + tools); reply in-thread."""
    import re

    from app.connectors.slack.client import SlackClient
    from app.connectors.slack.connector import SlackConnector
    from app.connectors.slack.normalizer import message_text
    from app.slackbot.agent import run_agent

    new_correlation_id()
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")  # reply stays in a thread
    try:
        conn = SlackConnector()
        conn.prepare(refresh_identities=False)
        lines = []
        if event.get("thread_ts"):  # established thread → full transcript = memory
            for m in conn.client.paginate(
                "conversations_replies", "messages", channel=channel, ts=event["thread_ts"]
            ):
                t = re.sub(r"<@[^>]+>", "", message_text(m, conn.names, conn.names)).strip()
                if t:
                    who = "assistant" if m.get("bot_id") else conn.names.get(m.get("user", ""), "user")
                    lines.append(f"{who}: {t}")
        else:  # top-level mention → just this message
            t = re.sub(r"<@[^>]+>", "", message_text(event, conn.names, conn.names)).strip()
            lines.append(f"user: {t}")
        reply = run_agent(channel, thread_ts, "\n".join(lines), event.get("user", ""))
    except Exception as e:  # noqa: BLE001
        log.warning("mention handling failed", extra={"error": str(e)})
        reply = "Sorry, I hit an error handling that. Please try again."
    try:
        SlackClient().call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=reply,
                           unfurl_links=False, unfurl_media=False)
        log.info("mention: replied", extra={"scope_id": channel, "thread_ts": thread_ts})
    except Exception as e:  # noqa: BLE001
        log.error("failed to post Slack reply", extra={"error": str(e)})


@celery_app.task(name="app.workers.tasks.handle_passive_message", bind=True, max_retries=0)
def handle_passive_message(self, event: dict) -> None:
    """Un-mentioned message → answer in-thread ONLY if confident, else stay silent."""
    from app.connectors.slack.client import SlackClient
    from app.rag.answerer import INSUFFICIENT
    from app.rag.service import answer_query
    from app.schemas.query import QueryFilters, QueryRequest
    from app.slackbot.formatting import format_answer
    from app.slackbot.passive import should_consider, strip_mentions

    new_correlation_id()
    if not should_consider(event):  # re-check: state/config may have changed since enqueue
        return
    channel = event.get("channel", "")
    ts = event.get("ts")
    question = strip_mentions(event.get("text") or "")
    if not question:
        return
    try:
        resp = answer_query(QueryRequest(
            question=question,
            filters=QueryFilters(scope_id=channel),
            min_score=settings.passive_confidence_floor,
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("passive answer failed", extra={"error": str(e)})
        return

    # Not confident enough (below the raised floor, or nothing relevant) → silent.
    if resp.answer == INSUFFICIENT or not resp.citations:
        log.info("passive: staying silent", extra={"scope_id": channel, "score": resp.best_score})
        return

    reply = format_answer(resp)
    try:
        SlackClient().call("chat_postMessage", channel=channel, thread_ts=ts, text=reply,
                           unfurl_links=False, unfurl_media=False)
        log.info("passive: replied", extra={"scope_id": channel, "thread_ts": ts,
                                            "score": resp.best_score,
                                            "sources": len(resp.citations)})
    except Exception as e:  # noqa: BLE001
        log.error("failed to post passive reply", extra={"error": str(e)})


@celery_app.task(name="app.workers.tasks.handle_dm", bind=True, max_retries=1)
def handle_dm(self, event: dict) -> None:
    """A direct message to the bot — every DM is for us, so no @mention is needed."""
    from app.connectors.slack.client import SlackClient
    from app.connectors.slack.connector import SlackConnector
    from app.connectors.slack.normalizer import message_text
    from app.slackbot.agent import run_agent

    new_correlation_id()
    channel = event.get("channel", "")
    user = event.get("user", "")
    thread_ts = event.get("thread_ts")  # DMs are usually flat; keep threads if used
    try:
        conn = SlackConnector()
        conn.prepare(refresh_identities=False)
        lines = []
        if thread_ts:  # threaded DM → full transcript as memory
            for m in conn.client.paginate(
                "conversations_replies", "messages", channel=channel, ts=thread_ts
            ):
                t = message_text(m, conn.names, conn.names).strip()
                if t:
                    who = "assistant" if m.get("bot_id") else conn.names.get(m.get("user", ""), "user")
                    lines.append(f"{who}: {t}")
        else:  # flat DM → recent history gives the conversation memory
            history = conn.client.call(
                "conversations_history", channel=channel, limit=12
            ).get("messages", [])
            for m in reversed(history):
                t = message_text(m, conn.names, conn.names).strip()
                if t:
                    who = "assistant" if m.get("bot_id") else conn.names.get(m.get("user", ""), "user")
                    lines.append(f"{who}: {t}")
        reply = run_agent(channel, thread_ts, "\n".join(lines), user, is_dm=True)
    except Exception as e:  # noqa: BLE001
        log.warning("dm handling failed", extra={"error": str(e)})
        reply = "Sorry, I hit an error handling that. Please try again."
    try:
        SlackClient().call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=reply,
                           unfurl_links=False, unfurl_media=False)
        log.info("dm: replied", extra={"scope_id": channel})
    except Exception as e:  # noqa: BLE001
        log.error("failed to post DM reply", extra={"error": str(e)})


@celery_app.task(name="app.workers.tasks.deliver_due_reminders")
def deliver_due_reminders() -> None:
    """Fire every reminder whose time has come (run once a minute by beat)."""
    from app.connectors.slack.client import SlackClient
    from app.storage.db import session_scope
    from app.storage.db.repositories import due_reminders, mark_reminder_sent

    new_correlation_id()
    if not settings.enable_reminders:
        return
    with session_scope() as session:
        rows = [
            {"id": r.id, "requester": r.requester_user_id, "target": r.target_user_id,
             "channel": r.channel_id, "thread_ts": r.thread_ts, "text": r.text}
            for r in due_reminders(session)
        ]
    if not rows:
        return
    client = SlackClient()
    for r in rows:
        try:
            if r["target"] and r["target"] != r["requester"]:
                # someone else's reminder → deliver privately, naming who asked
                dm = client.call("conversations_open", users=r["target"])
                channel = (dm.get("channel") or {}).get("id") or r["channel"]
                text = (f"⏰ Reminder from <@{r['requester']}>: {r['text']}")
                thread_ts = None
            else:
                channel, thread_ts = r["channel"], r["thread_ts"]
                text = f"⏰ <@{r['requester']}> reminder: {r['text']}"
            client.call("chat_postMessage", channel=channel, thread_ts=thread_ts, text=text,
                        unfurl_links=False, unfurl_media=False)
        except Exception as e:  # noqa: BLE001 - one bad reminder must not block the rest
            log.error("reminder delivery failed", extra={"reminder_id": r["id"], "error": str(e)})
            continue
        with session_scope() as session:
            mark_reminder_sent(session, r["id"])
        log.info("reminder: delivered", extra={"reminder_id": r["id"]})


@celery_app.task(name="app.workers.tasks.channel_digest")
def channel_digest(days: int = 1) -> None:
    """Generate a digest per channel and deliver via the alert webhook (Slack posting is
    pending `chat:write`). On-demand digests are also available at POST /summarize/channel.
    """
    new_correlation_id()
    from app.rag.summarizer import summarize_channel

    label = "Daily" if days == 1 else f"{days}-day"
    for channel_id in settings.slack_channels:
        try:
            s = summarize_channel(channel_id, days)
        except Exception as e:  # noqa: BLE001
            log.warning("digest failed", extra={"scope_id": channel_id, "error": str(e)})
            continue
        send_alert(f"*{label} digest — {channel_id}* ({s.item_count} items)\n{s.summary}")


@celery_app.task(name="app.workers.tasks.check_stale_scopes")
def check_stale_scopes() -> None:
    """Alert when a scope has had no successful run in > STALE_SCOPE_ALERT_DAYS (PRD §17)."""
    new_correlation_id()
    from app.storage.db import session_scope
    from app.storage.db.repositories import last_success_epoch

    now = time.time()
    stale_seconds = settings.stale_scope_alert_days * 86400
    stale: list[str] = []
    for channel_id in settings.slack_channels:
        with session_scope() as session:
            last = last_success_epoch(session, SOURCE, channel_id)
        if last is None or (now - last) > stale_seconds:
            stale.append(channel_id)
    if stale:
        send_alert(
            f"[loopguru] {len(stale)} Slack scope(s) stale "
            f"(no successful sync in > {settings.stale_scope_alert_days}d): {', '.join(stale)}"
        )
