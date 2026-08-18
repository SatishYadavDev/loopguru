"""ORM models mirroring PRD §12. Keep columns explicit so the Alembic initial
migration stays a faithful 1:1 mirror.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.db.base import Base


class SyncState(Base):
    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backfill_status: Mapped[str] = mapped_column(String(16), default="pending")
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_cursor: Mapped[str | None] = mapped_column(Text)
    last_checkpoint: Mapped[str | None] = mapped_column(String(32))  # opaque Slack ts
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="message")
    title: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    created_epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FileRow(Base):
    __tablename__ = "files"

    file_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(128))
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    extracted_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    doc_id: Mapped[str] = mapped_column(String(256), index=True)


class FileMessage(Base):
    __tablename__ = "file_messages"

    file_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_doc_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)


class ThreadRow(Base):
    __tablename__ = "threads"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_ts: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_reply: Mapped[str | None] = mapped_column(String(32))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityCache(Base):
    __tablename__ = "identity_cache"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="user")  # user | channel
    display_name: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FailedItem(Base):
    __tablename__ = "failed_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    ref: Mapped[str] = mapped_column(String(256))  # doc_id / file_id / message ts
    reason: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))  # backfill | incremental | sweep
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    chunks_upserted: Mapped[int] = mapped_column(Integer, default=0)
    chunks_deleted: Mapped[int] = mapped_column(Integer, default=0)
    embed_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text)


class ThreadTicket(Base):
    """Remembers which Azure work item a Slack thread created (for follow-up edits)."""

    __tablename__ = "thread_tickets"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_ts: Mapped[str] = mapped_column(String(32), primary_key=True)
    work_item_id: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QueryAudit(Base):
    __tablename__ = "query_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    question: Mapped[str] = mapped_column(Text)
    top_doc_ids: Mapped[str | None] = mapped_column(Text)  # JSON array
    used_doc_ids: Mapped[str | None] = mapped_column(Text)  # JSON array
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
