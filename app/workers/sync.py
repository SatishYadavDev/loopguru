"""Sync engine (PRD §11): resumable backfill, daily incremental (history + thread
replies + old-thread rotation), weekly reconcile/purge sweep, per-scope locking,
monotonic checkpoints.

Called by Celery tasks (app/workers/tasks.py). Uses SlackConnector + ingestion
pipeline + repositories + Qdrant store.
"""

from __future__ import annotations

import os
import socket

from app.connectors.slack.client import SlackApiPermanentError
from app.connectors.slack.connector import SOURCE, SlackConnector
from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion.pipeline import IngestStats, ingest_documents
from app.schemas.connector import RawItem, Scope, ScopeStatus
from app.storage.db import session_scope
from app.storage.db.repositories import (
    acquire_lock,
    advance_checkpoint,
    delete_document,
    file_has_links,
    file_scope_ids,
    finish_run,
    get_thread,
    list_message_doc_ids_for_scope,
    mark_thread_polled,
    release_lock,
    set_backfill_status,
    set_cursor,
    start_run,
    threads_for_rotation,
    unlink_file_messages_for_scope,
    upsert_thread,
)
from app.storage.qdrant.store import get_store

log = get_logger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class SyncEngine:
    def __init__(self) -> None:
        self.connector = SlackConnector()
        self.store = get_store()

    def prepare(self) -> None:
        self.store.ensure_collection()
        self.connector.prepare()

    # ---- shared helpers ----------------------------------------------------

    def _ingest_msg(
        self, session, scope_id: str, msg: dict, seen_msgs: set, seen_files: set
    ) -> tuple[IngestStats, str | None]:
        docs = self.connector.to_documents(RawItem(SOURCE, scope_id, "message", msg))
        stats = (
            ingest_documents(session, self.store, docs, SOURCE, scope_id)
            if docs
            else IngestStats()
        )
        for d in docs:
            if d.kind == "message":
                seen_msgs.add(d.doc_id)
            elif d.kind == "file":
                seen_files.add(d.metadata.get("file_id"))
        return stats, msg.get("ts")

    def _handle_thread_parent(
        self, session, scope_id: str, msg: dict, seen_msgs: set, seen_files: set,
        stats: IngestStats,
    ) -> None:
        ts = msg.get("ts")
        if msg.get("thread_ts") == ts and msg.get("reply_count"):
            for reply in self.connector.get_thread_replies(scope_id, ts):
                st, _ = self._ingest_msg(session, scope_id, reply, seen_msgs, seen_files)
                stats.add(st)
            upsert_thread(
                session, source=SOURCE, scope_id=scope_id, thread_ts=ts,
                reply_count=int(msg.get("reply_count", 0)),
                latest_reply=msg.get("latest_reply"),
            )

    # ---- backfill / sweep --------------------------------------------------

    def backfill(self, scope: Scope, sweep: bool = False) -> None:
        """Full history (re-)index. `sweep=True` also reconciles deletions (PRD §11.4)."""
        mode = "sweep" if sweep else "backfill"
        if not self._lock(scope):
            log.info("scope locked, skipping", extra={"scope_id": scope.scope_id, "mode": mode})
            return
        seen_msgs: set[str] = set()
        seen_files: set[str] = set()
        stats = IngestStats()
        max_ts: str | None = None
        try:
            with session_scope() as session:
                run = start_run(session, SOURCE, scope.scope_id, mode)
                set_backfill_status(session, SOURCE, scope.scope_id, "in_progress")

            start_cursor = None if sweep else self._resume_cursor(scope.scope_id)
            for messages, next_cursor in self.connector.iter_history_pages(
                scope.scope_id, oldest=None, start_cursor=start_cursor
            ):
                with session_scope() as session:
                    for msg in messages:
                        st, ts = self._ingest_msg(session, scope.scope_id, msg,
                                                  seen_msgs, seen_files)
                        stats.add(st)
                        self._handle_thread_parent(session, scope.scope_id, msg,
                                                   seen_msgs, seen_files, stats)
                        if ts and (max_ts is None or float(ts) > float(max_ts)):
                            max_ts = ts
                    if not sweep:
                        set_cursor(session, SOURCE, scope.scope_id, next_cursor)

            deleted = 0
            if sweep:
                deleted = self._reconcile(scope.scope_id, seen_msgs, seen_files)

            with session_scope() as session:
                set_cursor(session, SOURCE, scope.scope_id, None)
                set_backfill_status(session, SOURCE, scope.scope_id, "completed", completed=True)
                advance_checkpoint(session, SOURCE, scope.scope_id, max_ts)
                self._finish(session, run.id, "ok", stats, deleted)
        except SlackApiPermanentError as e:
            self._fail(scope, mode, f"slack permanent: {e.error}")
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(scope, mode, str(e))
            raise
        finally:
            self._unlock(scope)
        log.info(
            "backfill done",
            extra={"scope_id": scope.scope_id, "mode": mode,
                   "chunks": stats.chunks_upserted, "tokens": stats.embed_tokens},
        )

    # ---- incremental -------------------------------------------------------

    def incremental(self, scope: Scope) -> None:
        if not self._lock(scope):
            log.info("scope locked, skipping", extra={"scope_id": scope.scope_id})
            return
        stats = IngestStats()
        max_ts: str | None = None
        seen_msgs: set[str] = set()
        seen_files: set[str] = set()
        try:
            with session_scope() as session:
                run = start_run(session, SOURCE, scope.scope_id, "incremental")
            oldest = self._incremental_oldest(scope.scope_id)

            # Pass A: new top-level messages within the overlap window
            for messages, _ in self.connector.iter_history_pages(scope.scope_id, oldest=oldest):
                with session_scope() as session:
                    for msg in messages:
                        st, ts = self._ingest_msg(session, scope.scope_id, msg,
                                                  seen_msgs, seen_files)
                        stats.add(st)
                        self._handle_thread_parent(session, scope.scope_id, msg,
                                                   seen_msgs, seen_files, stats)
                        if ts and (max_ts is None or float(ts) > float(max_ts)):
                            max_ts = ts

            # Pass B: old-thread rotation (bounded) — catch late replies on old threads
            self._poll_old_threads(scope.scope_id, stats, seen_msgs, seen_files)

            with session_scope() as session:
                advance_checkpoint(session, SOURCE, scope.scope_id, max_ts)
                self._finish(session, run.id, "ok", stats, 0)
        except Exception as e:  # noqa: BLE001
            self._fail(scope, "incremental", str(e))
            raise
        finally:
            self._unlock(scope)
        log.info("incremental done", extra={"scope_id": scope.scope_id,
                                            "chunks": stats.chunks_upserted})

    def _poll_old_threads(self, scope_id: str, stats: IngestStats, seen_msgs, seen_files) -> None:
        with session_scope() as session:
            threads = threads_for_rotation(session, SOURCE, scope_id, settings.reply_poll_batch)
            thread_ids = [t.thread_ts for t in threads]
        for thread_ts in thread_ids:
            replies = self.connector.get_thread_replies(scope_id, thread_ts)
            with session_scope() as session:
                for reply in replies:
                    st, _ = self._ingest_msg(session, scope_id, reply, seen_msgs, seen_files)
                    stats.add(st)
                if replies:
                    upsert_thread(
                        session, source=SOURCE, scope_id=scope_id, thread_ts=thread_ts,
                        reply_count=len(replies), latest_reply=replies[-1].get("ts"),
                    )
                mark_thread_polled(session, SOURCE, thread_ts)

    # ---- reconciliation (deletions) ----------------------------------------

    def _reconcile(self, scope_id: str, seen_msgs: set, seen_files: set) -> int:
        """Delete indexed docs no longer present in Slack (PRD §11.4)."""
        deleted = 0
        with session_scope() as session:
            db_msg_ids = list_message_doc_ids_for_scope(session, SOURCE, scope_id)
        # message deletions
        for doc_id in db_msg_ids - seen_msgs:
            self.store.delete_by_doc_id(doc_id)
            with session_scope() as session:
                delete_document(session, doc_id)
            deleted += 1
        # file link reconciliation for files no longer shared in this scope
        with session_scope() as session:
            # files currently linked to this scope
            from app.storage.db.models import FileMessage  # local import to avoid cycle
            from sqlalchemy import select

            linked = set(
                session.scalars(
                    select(FileMessage.file_id).where(FileMessage.scope_id == scope_id).distinct()
                )
            )
        for file_id in linked - {f for f in seen_files if f}:
            with session_scope() as session:
                unlink_file_messages_for_scope(session, file_id, scope_id)
                remaining = file_scope_ids(session, file_id)
                still_linked = file_has_links(session, file_id)
            file_doc_id = f"slack:file:{file_id}"
            if not still_linked:
                self.store.delete_by_doc_id(file_doc_id)
                with session_scope() as session:
                    delete_document(session, file_doc_id)
                deleted += 1
            else:
                self.store.update_file_scope_ids(file_doc_id, remaining)
        return deleted

    # ---- purge -------------------------------------------------------------

    def purge_doc(self, doc_id: str) -> None:
        self.store.delete_by_doc_id(doc_id)
        with session_scope() as session:
            delete_document(session, doc_id)

    def purge_scope(self, scope_id: str) -> int:
        with session_scope() as session:
            doc_ids = list_message_doc_ids_for_scope(session, SOURCE, scope_id)
        for doc_id in doc_ids:
            self.purge_doc(doc_id)
        return len(doc_ids)

    # ---- lock / cursor / run helpers --------------------------------------

    def _lock(self, scope: Scope) -> bool:
        with session_scope() as session:
            return acquire_lock(session, SOURCE, scope.scope_id, WORKER_ID)

    def _unlock(self, scope: Scope) -> None:
        with session_scope() as session:
            release_lock(session, SOURCE, scope.scope_id, WORKER_ID)

    def _resume_cursor(self, scope_id: str) -> str | None:
        from app.storage.db.repositories import get_or_create_sync_state

        with session_scope() as session:
            return get_or_create_sync_state(session, SOURCE, scope_id).next_cursor

    def _incremental_oldest(self, scope_id: str) -> str | None:
        from app.storage.db.repositories import get_or_create_sync_state

        with session_scope() as session:
            cp = get_or_create_sync_state(session, SOURCE, scope_id).last_checkpoint
        if not cp:
            return None
        oldest = float(cp) - settings.sync_overlap_days * 86400.0
        return f"{max(0.0, oldest):.6f}"

    def _finish(self, session, run_id: int, status: str, stats: IngestStats, deleted: int) -> None:
        from app.storage.db.models import IngestionRun

        run = session.get(IngestionRun, run_id)
        if run:
            finish_run(
                session, run, status=status, items_seen=stats.docs_indexed,
                chunks_upserted=stats.chunks_upserted, chunks_deleted=deleted,
                embed_tokens=stats.embed_tokens,
            )

    def _fail(self, scope: Scope, mode: str, error: str) -> None:
        log.error("sync failed", extra={"scope_id": scope.scope_id, "mode": mode, "error": error})
        with session_scope() as session:
            run = start_run(session, SOURCE, scope.scope_id, mode)
            finish_run(
                session, run, status="failed", items_seen=0, chunks_upserted=0,
                chunks_deleted=0, embed_tokens=0, errors=[error[:2000]],
            )


# --------------------------------------------------------------------------- #
# scope resolution + dispatch (used by tasks + admin)
# --------------------------------------------------------------------------- #

def resolve_scope(engine: SyncEngine, channel_id: str) -> Scope:
    for s in engine.connector.list_scopes():
        if s.scope_id == channel_id:
            return s
    return Scope(SOURCE, channel_id, status=ScopeStatus.NOT_FOUND)


def prioritized_channels() -> list[str]:
    """Channel IDs in backfill-priority order, then the rest (PRD §11.1)."""
    prio = [c for c in settings.slack_channel_priority if c in settings.slack_channels]
    rest = [c for c in settings.slack_channels if c not in prio]
    return prio + rest
