"""Repository helpers over the ORM models (PRD §12). All functions take a Session.

Business logic (sync, ingestion, rag) goes through these — never raw SQL inline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.storage.db.models import (
    DocumentRow,
    FailedItem,
    FileMessage,
    FileRow,
    IdentityCache,
    IngestionRun,
    QueryAudit,
    Reminder,
    SyncState,
    ThreadRow,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# sync_state
# --------------------------------------------------------------------------- #

def get_or_create_sync_state(session, source: str, scope_id: str) -> SyncState:
    row = session.get(SyncState, {"source": source, "scope_id": scope_id})
    if row is None:
        row = SyncState(source=source, scope_id=scope_id, backfill_status="pending")
        session.add(row)
        session.flush()
    return row


def list_sync_states(session, source: str) -> list[SyncState]:
    return list(session.scalars(select(SyncState).where(SyncState.source == source)))


def set_backfill_status(
    session, source: str, scope_id: str, status: str, completed: bool = False
) -> None:
    row = get_or_create_sync_state(session, source, scope_id)
    row.backfill_status = status
    if completed:
        row.backfill_completed_at = _now()


def set_cursor(session, source: str, scope_id: str, cursor: str | None) -> None:
    row = get_or_create_sync_state(session, source, scope_id)
    row.next_cursor = cursor


def advance_checkpoint(session, source: str, scope_id: str, ts: str | None) -> None:
    """Move `last_checkpoint` forward only (monotonic; PRD §11.2)."""
    if not ts:
        return
    row = get_or_create_sync_state(session, source, scope_id)
    current = row.last_checkpoint
    if current is None or float(ts) > float(current):
        row.last_checkpoint = ts


def acquire_lock(
    session, source: str, scope_id: str, worker: str, stale_after_s: int = 3600
) -> bool:
    """Atomically claim a scope lock; reclaim if the previous lock is stale.

    Returns True if acquired. Relies on Postgres row-level atomicity of UPDATE.
    """
    get_or_create_sync_state(session, source, scope_id)
    cutoff = _now() - timedelta(seconds=stale_after_s)
    stmt = (
        update(SyncState)
        .where(
            SyncState.source == source,
            SyncState.scope_id == scope_id,
            (SyncState.locked_at.is_(None)) | (SyncState.locked_at < cutoff),
        )
        .values(locked_by=worker, locked_at=_now())
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def release_lock(session, source: str, scope_id: str, worker: str) -> None:
    stmt = (
        update(SyncState)
        .where(
            SyncState.source == source,
            SyncState.scope_id == scope_id,
            SyncState.locked_by == worker,
        )
        .values(locked_by=None, locked_at=None)
    )
    session.execute(stmt)
    session.commit()


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

def get_document(session, doc_id: str) -> DocumentRow | None:
    return session.get(DocumentRow, doc_id)


def content_hash_matches(session, doc_id: str, content_hash: str) -> bool:
    row = session.get(DocumentRow, doc_id)
    return row is not None and row.content_hash == content_hash


def upsert_document(
    session,
    *,
    doc_id: str,
    source: str,
    scope_id: str | None,
    kind: str,
    title: str | None,
    permalink: str | None,
    created_epoch: int,
    content_hash: str,
    chunk_count: int,
) -> None:
    """Record the document AFTER Qdrant confirms (PRD §10.5 ordering invariant)."""
    values = dict(
        doc_id=doc_id,
        source=source,
        scope_id=scope_id,
        kind=kind,
        title=title,
        permalink=permalink,
        created_epoch=created_epoch,
        content_hash=content_hash,
        chunk_count=chunk_count,
        indexed_at=_now(),
    )
    stmt = pg_insert(DocumentRow).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DocumentRow.doc_id],
        set_={
            k: values[k]
            for k in ("scope_id", "title", "permalink", "created_epoch",
                      "content_hash", "chunk_count", "indexed_at")
        },
    )
    session.execute(stmt)


def delete_document(session, doc_id: str) -> None:
    session.execute(delete(DocumentRow).where(DocumentRow.doc_id == doc_id))


def list_message_doc_ids_for_scope(session, source: str, scope_id: str) -> set[str]:
    rows = session.scalars(
        select(DocumentRow.doc_id).where(
            DocumentRow.source == source,
            DocumentRow.scope_id == scope_id,
            DocumentRow.kind == "message",
        )
    )
    return set(rows)


# --------------------------------------------------------------------------- #
# files + file_messages
# --------------------------------------------------------------------------- #

def upsert_file(
    session, *, file_id: str, source: str, name: str | None, mime: str | None,
    bytes_: int, extracted_ok: bool, doc_id: str,
) -> None:
    values = dict(
        file_id=file_id, source=source, name=name, mime=mime,
        bytes=bytes_, extracted_ok=extracted_ok, doc_id=doc_id,
    )
    stmt = pg_insert(FileRow).values(**values).on_conflict_do_update(
        index_elements=[FileRow.file_id],
        set_={k: values[k] for k in ("name", "mime", "bytes", "extracted_ok", "doc_id")},
    )
    session.execute(stmt)


def link_file_message(session, file_id: str, message_doc_id: str, scope_id: str) -> None:
    stmt = pg_insert(FileMessage).values(
        file_id=file_id, message_doc_id=message_doc_id, scope_id=scope_id
    ).on_conflict_do_nothing(index_elements=[FileMessage.file_id, FileMessage.message_doc_id])
    session.execute(stmt)


def file_scope_ids(session, file_id: str) -> list[str]:
    rows = session.scalars(
        select(FileMessage.scope_id).where(FileMessage.file_id == file_id).distinct()
    )
    return sorted(set(rows))


def unlink_file_messages_for_scope(session, file_id: str, scope_id: str) -> None:
    session.execute(
        delete(FileMessage).where(
            FileMessage.file_id == file_id, FileMessage.scope_id == scope_id
        )
    )


def file_has_links(session, file_id: str) -> bool:
    return session.scalar(
        select(FileMessage.file_id).where(FileMessage.file_id == file_id).limit(1)
    ) is not None


# --------------------------------------------------------------------------- #
# threads
# --------------------------------------------------------------------------- #

def upsert_thread(
    session, *, source: str, scope_id: str, thread_ts: str,
    reply_count: int, latest_reply: str | None,
) -> None:
    values = dict(
        source=source, scope_id=scope_id, thread_ts=thread_ts,
        reply_count=reply_count, latest_reply=latest_reply,
    )
    stmt = pg_insert(ThreadRow).values(**values).on_conflict_do_update(
        index_elements=[ThreadRow.source, ThreadRow.thread_ts],
        set_={"scope_id": scope_id, "reply_count": reply_count, "latest_reply": latest_reply},
    )
    session.execute(stmt)


def get_thread(session, source: str, thread_ts: str) -> ThreadRow | None:
    return session.get(ThreadRow, {"source": source, "thread_ts": thread_ts})


def threads_for_rotation(session, source: str, scope_id: str, limit: int) -> list[ThreadRow]:
    """Most recently active threads first, oldest-polled prioritized (PRD §11.3)."""
    return list(
        session.scalars(
            select(ThreadRow)
            .where(ThreadRow.source == source, ThreadRow.scope_id == scope_id)
            .order_by(
                ThreadRow.last_polled_at.asc().nulls_first(),
                ThreadRow.latest_reply.desc(),
            )
            .limit(limit)
        )
    )


def mark_thread_polled(session, source: str, thread_ts: str) -> None:
    row = get_thread(session, source, thread_ts)
    if row is not None:
        row.last_polled_at = _now()


# --------------------------------------------------------------------------- #
# identity cache
# --------------------------------------------------------------------------- #

def bulk_upsert_identities(
    session, source: str, entries: dict[str, str], kind: str = "user"
) -> None:
    for entity_id, name in entries.items():
        stmt = pg_insert(IdentityCache).values(
            source=source, entity_id=entity_id, kind=kind,
            display_name=name, updated_at=_now(),
        ).on_conflict_do_update(
            index_elements=[IdentityCache.source, IdentityCache.entity_id],
            set_={"display_name": name, "kind": kind, "updated_at": _now()},
        )
        session.execute(stmt)


def identity_map(session, source: str) -> dict[str, str]:
    rows = session.execute(
        select(IdentityCache.entity_id, IdentityCache.display_name).where(
            IdentityCache.source == source
        )
    )
    return {entity_id: name for entity_id, name in rows}


# --------------------------------------------------------------------------- #
# failed items
# --------------------------------------------------------------------------- #

def record_failure(
    session, *, source: str, scope_id: str, ref: str, reason: str,
    retryable: bool, error: str,
) -> None:
    session.add(
        FailedItem(
            source=source, scope_id=scope_id, ref=ref, reason=reason,
            retryable=retryable, last_error=error[:4000], attempts=1,
        )
    )


# --------------------------------------------------------------------------- #
# ingestion runs
# --------------------------------------------------------------------------- #

def start_run(session, source: str, scope_id: str, mode: str) -> IngestionRun:
    run = IngestionRun(source=source, scope_id=scope_id, mode=mode, status="running")
    session.add(run)
    session.flush()
    return run


def finish_run(
    session, run: IngestionRun, *, status: str, items_seen: int,
    chunks_upserted: int, chunks_deleted: int, embed_tokens: int,
    errors: list[str] | None = None,
) -> None:
    run.finished_at = _now()
    run.status = status
    run.items_seen = items_seen
    run.chunks_upserted = chunks_upserted
    run.chunks_deleted = chunks_deleted
    run.embed_tokens = embed_tokens
    run.errors_json = json.dumps(errors) if errors else None


def recent_runs(session, source: str, limit: int = 20) -> list[IngestionRun]:
    return list(
        session.scalars(
            select(IngestionRun)
            .where(IngestionRun.source == source)
            .order_by(IngestionRun.started_at.desc())
            .limit(limit)
        )
    )


def last_success_epoch(session, source: str, scope_id: str) -> float | None:
    row = session.scalars(
        select(IngestionRun)
        .where(
            IngestionRun.source == source,
            IngestionRun.scope_id == scope_id,
            IngestionRun.status == "ok",
        )
        .order_by(IngestionRun.finished_at.desc())
        .limit(1)
    ).first()
    if row and row.finished_at:
        return row.finished_at.timestamp()
    return None


# --------------------------------------------------------------------------- #
# query audit
# --------------------------------------------------------------------------- #

def get_thread_ticket(session, channel_id: str, thread_ts: str) -> int | None:
    from app.storage.db.models import ThreadTicket

    row = session.get(ThreadTicket, {"channel_id": channel_id, "thread_ts": thread_ts})
    return row.work_item_id if row else None


def set_thread_ticket(session, channel_id: str, thread_ts: str, work_item_id: int) -> None:
    from app.storage.db.models import ThreadTicket

    stmt = pg_insert(ThreadTicket).values(
        channel_id=channel_id, thread_ts=thread_ts, work_item_id=work_item_id, updated_at=_now()
    ).on_conflict_do_update(
        index_elements=[ThreadTicket.channel_id, ThreadTicket.thread_ts],
        set_={"work_item_id": work_item_id, "updated_at": _now()},
    )
    session.execute(stmt)


def record_query_audit(
    session, *, question: str, top_doc_ids: list[str], used_doc_ids: list[str],
    latency_ms: int,
) -> None:
    session.add(
        QueryAudit(
            question=question[:2000],
            top_doc_ids=json.dumps(top_doc_ids),
            used_doc_ids=json.dumps(used_doc_ids),
            latency_ms=latency_ms,
        )
    )


# --- reminders ---------------------------------------------------------------


def add_reminder(
    session, *, requester_user_id: str, target_user_id: str | None, channel_id: str,
    thread_ts: str | None, text: str, remind_at: datetime,
) -> Reminder:
    row = Reminder(
        requester_user_id=requester_user_id,
        target_user_id=target_user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=text[:2000],
        remind_at=remind_at,
    )
    session.add(row)
    session.flush()  # populate row.id for the confirmation message
    return row


def due_reminders(session, now: datetime | None = None, limit: int = 50) -> list[Reminder]:
    """Pending reminders whose time has come (oldest first)."""
    moment = now or datetime.now(timezone.utc)
    stmt = (
        select(Reminder)
        .where(Reminder.status == "pending", Reminder.remind_at <= moment)
        .order_by(Reminder.remind_at)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def mark_reminder_sent(session, reminder_id: int) -> None:
    session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(status="sent", delivered_at=datetime.now(timezone.utc))
    )


def pending_reminders_for(session, user_id: str, limit: int = 20) -> list[Reminder]:
    """Reminders this user asked for that haven't fired yet."""
    stmt = (
        select(Reminder)
        .where(Reminder.requester_user_id == user_id, Reminder.status == "pending")
        .order_by(Reminder.remind_at)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def recent_reminders_for(session, user_id: str, limit: int = 10) -> list[Reminder]:
    """The user's latest reminders whatever their status — for "what were my past ones?"."""
    stmt = (
        select(Reminder)
        .where(Reminder.requester_user_id == user_id)
        .order_by(Reminder.remind_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def cancel_reminder(session, reminder_id: int, user_id: str) -> bool:
    """Cancel one of the user's own pending reminders. Returns False if not theirs."""
    result = session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.requester_user_id == user_id,
            Reminder.status == "pending",
        )
        .values(status="cancelled")
    )
    return bool(result.rowcount)


def user_id_by_name(session, source: str, name: str) -> str | None:
    """Reverse identity lookup: a display name (as written in Slack) → user id."""
    target = (name or "").lstrip("@").strip().lower()
    if not target:
        return None
    rows = session.execute(
        select(IdentityCache.entity_id, IdentityCache.display_name).where(
            IdentityCache.source == source, IdentityCache.kind == "user"
        )
    ).all()
    for entity_id, display in rows:  # exact match first, then first-name / prefix
        if (display or "").strip().lower() == target:
            return entity_id
    for entity_id, display in rows:
        d = (display or "").strip().lower()
        if d.startswith(target) or target.startswith(d.split(" ")[0]):
            return entity_id
    return None
