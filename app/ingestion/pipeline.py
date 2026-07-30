"""Ingestion pipeline (PRD §10): normalize is done by the connector; here we do
change-detection → chunk → embed → delete-then-upsert (Qdrant) → record Postgres
(AFTER Qdrant confirms) → poison isolation per item.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.ids import content_hash
from app.core.logging import get_logger
from app.ingestion.chunker import chunk_document
from app.ingestion.embedder import embed_texts
from app.schemas.document import Document
from app.storage.db.repositories import (
    content_hash_matches,
    file_scope_ids,
    link_file_message,
    record_failure,
    upsert_document,
    upsert_file,
)
from app.storage.qdrant.store import QdrantStore

log = get_logger(__name__)


@dataclass
class IngestStats:
    docs_indexed: int = 0
    chunks_upserted: int = 0
    embed_tokens: int = 0

    def add(self, other: "IngestStats") -> None:
        self.docs_indexed += other.docs_indexed
        self.chunks_upserted += other.chunks_upserted
        self.embed_tokens += other.embed_tokens


def _doc_content_hash(doc: Document) -> str:
    return content_hash(doc.text, doc.metadata.get("edited_ts"))


def ingest_one(session, store: QdrantStore, doc: Document) -> IngestStats:
    """Index a single Document. Idempotent: unchanged content skips embedding."""
    ch = _doc_content_hash(doc)

    # File docs: fold the current channel into the union of all sharing channels.
    if doc.kind == "file":
        file_id = doc.metadata.get("file_id", "")
        existing = set(file_scope_ids(session, file_id))
        new_scope = doc.scope_ids[0] if doc.scope_ids else None
        union = sorted(existing | ({new_scope} if new_scope else set()))
        doc.scope_ids = union

        if content_hash_matches(session, doc.doc_id, ch):
            # already indexed — just make sure payload reflects a newly-added channel
            if new_scope and new_scope not in existing:
                store.update_file_scope_ids(doc.doc_id, union)
            return IngestStats()
    else:
        if content_hash_matches(session, doc.doc_id, ch):
            return IngestStats()

    doc.metadata["content_hash"] = ch
    chunks = chunk_document(doc)
    if not chunks:
        return IngestStats()

    embeds = embed_texts([c.embed_input() for c in chunks])
    upserted = store.upsert_document_chunks(doc, chunks, embeds.vectors)  # delete-then-upsert

    # --- only AFTER Qdrant confirms, write Postgres state (ordering invariant) ---
    upsert_document(
        session,
        doc_id=doc.doc_id,
        source=doc.source,
        scope_id=doc.scope_id,  # None for file docs
        kind=doc.kind,
        title=doc.title,
        permalink=doc.permalink,
        created_epoch=doc.created_epoch,
        content_hash=ch,
        chunk_count=upserted,
    )
    if doc.kind == "file":
        upsert_file(
            session,
            file_id=doc.metadata.get("file_id", ""),
            source=doc.source,
            name=doc.title,
            mime=doc.metadata.get("mime"),
            bytes_=int(doc.metadata.get("bytes", 0) or 0),
            extracted_ok=True,
            doc_id=doc.doc_id,
        )

    return IngestStats(docs_indexed=1, chunks_upserted=upserted, embed_tokens=embeds.total_tokens)


def ingest_documents(
    session, store: QdrantStore, docs: list[Document], source: str, scope_id: str
) -> IngestStats:
    """Ingest all documents produced from one raw item, with poison isolation.

    Handles file→message linkage and records file extraction errors as failed_items.
    """
    stats = IngestStats()
    for doc in docs:
        try:
            stats.add(ingest_one(session, store, doc))
        except Exception as e:  # noqa: BLE001 - one poison doc must not kill the scope
            log.warning("doc ingest failed", extra={"doc_id": doc.doc_id, "error": str(e)})
            record_failure(
                session, source=source, scope_id=scope_id, ref=doc.doc_id,
                reason="ingest_error", retryable=True, error=str(e),
            )
            continue

        # link a file to the message/channel that shared it
        if doc.kind == "file":
            message_doc_id = doc.metadata.get("message_doc_id", "")
            if message_doc_id:
                link_file_message(session, doc.metadata.get("file_id", ""),
                                  message_doc_id, scope_id)

        # surface file extraction errors carried on the doc
        for ferr in doc.metadata.get("file_errors", []) or []:
            record_failure(
                session, source=source, scope_id=scope_id,
                ref=ferr.get("file_id", ""), reason=ferr.get("reason", "file_error"),
                retryable=bool(ferr.get("retryable", False)),
                error=ferr.get("reason", ""),
            )
    return stats
