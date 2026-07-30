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
