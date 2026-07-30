"""Qdrant vector store wrapper (PRD §13).

- One versioned concrete collection reached via a stable read/write alias.
- Payload indexes on source / scope_id / scope_ids / created_epoch.
- delete-by-doc_id then upsert (no orphaned chunks on re-chunk; PRD §8, §10.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from app.core.config import settings
from app.core.ids import point_id
from app.core.logging import get_logger
from app.schemas.chunk import Chunk
from app.schemas.document import Document

log = get_logger(__name__)


@dataclass
class RetrievedChunk:
    doc_id: str
    chunk_idx: int
    score: float
    payload: dict
    vector: list[float] | None = None

    @property
    def text(self) -> str:
        return self.payload.get("chunk_text", "")

    @property
    def created_epoch(self) -> int:
        return int(self.payload.get("created_epoch", 0) or 0)

    @property
    def content_hash(self) -> str:
        return self.payload.get("content_hash", "")


class QdrantStore:
    def __init__(self, client: QdrantClient | None = None) -> None:
        self.client = client or QdrantClient(url=settings.qdrant_url, timeout=30)
        self.concrete = settings.concrete_collection
        self.alias = settings.collection_alias

    # ---- schema management -------------------------------------------------

    def ensure_collection(self) -> None:
        """Create the concrete collection + payload indexes + alias if missing."""
        existing = {c.name for c in self.client.get_collections().collections}
        if self.concrete not in existing:
            log.info("creating qdrant collection", extra={"collection": self.concrete})
            self.client.create_collection(
                collection_name=self.concrete,
                vectors_config=qm.VectorParams(
                    size=settings.embedding_dim, distance=qm.Distance.COSINE
                ),
            )
            for field, schema in (
                ("source", qm.PayloadSchemaType.KEYWORD),
                ("scope_id", qm.PayloadSchemaType.KEYWORD),
                ("scope_ids", qm.PayloadSchemaType.KEYWORD),
                ("doc_id", qm.PayloadSchemaType.KEYWORD),
                ("created_epoch", qm.PayloadSchemaType.INTEGER),
            ):
                self.client.create_payload_index(
                    collection_name=self.concrete,
                    field_name=field,
                    field_schema=schema,
                )
        self._ensure_alias()

    def _ensure_alias(self) -> None:
        aliases = {a.alias_name: a.collection_name for a in self.client.get_aliases().aliases}
        if aliases.get(self.alias) != self.concrete:
            log.info("pointing alias", extra={"alias": self.alias, "to": self.concrete})
            self.client.update_collection_aliases(
                change_aliases_operations=[
                    qm.CreateAliasOperation(
                        create_alias=qm.CreateAlias(
                            collection_name=self.concrete, alias_name=self.alias
                        )
                    )
                ]
            )

    # ---- writes ------------------------------------------------------------

    def _payload_for(self, doc: Document, chunk: Chunk) -> dict:
        payload = {
            "doc_id": doc.doc_id,
            "source": doc.source,
            "kind": doc.kind,
            "title": doc.title,
            "author": doc.author,
            "created_ts": doc.created_ts,
            "created_epoch": doc.created_epoch,
            "permalink": doc.permalink,
            "thread_id": doc.thread_id,
            "chunk_idx": chunk.chunk_idx,
            "chunk_text": chunk.text,
            "prepend_text": chunk.prepend_text,
            "content_hash": doc.metadata.get("content_hash", ""),
            "metadata": doc.metadata,
        }
        if doc.kind == "file":
            payload["scope_ids"] = doc.all_scope_ids()
        else:
            payload["scope_id"] = doc.scope_id
        return payload

    def upsert_document_chunks(
        self, doc: Document, chunks: list[Chunk], vectors: list[list[float]]
    ) -> int:
        """Delete any existing points for this doc_id, then upsert fresh chunks.

        Returns number of points upserted. Caller records Postgres state AFTER
        this returns (ordering invariant, PRD §10.5).
        """
        # 1) remove stale chunks (prevents orphans when chunk count shrinks)
        self.delete_by_doc_id(doc.doc_id)
        # 2) upsert
        points = [
            qm.PointStruct(
                id=point_id(doc.doc_id, c.chunk_idx),
                vector=vec,
                payload=self._payload_for(doc, c),
            )
            for c, vec in zip(chunks, vectors)
        ]
        if points:
            self.client.upsert(collection_name=self.alias, points=points, wait=True)
        return len(points)

    def delete_by_doc_id(self, doc_id: str) -> None:
        self.client.delete(
            collection_name=self.alias,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]
                )
            ),
            wait=True,
        )

    def update_file_scope_ids(self, doc_id: str, scope_ids: list[str]) -> None:
        """Rewrite a file doc's scope_ids payload after a channel link is removed."""
        self.client.set_payload(
            collection_name=self.alias,
            payload={"scope_ids": scope_ids},
            points=qm.Filter(
                must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]
            ),
            wait=True,
        )

    # ---- reads -------------------------------------------------------------

    def search(
        self,
        vector: list[float],
        limit: int,
        source: str | None = None,
        scope_id: str | None = None,
        since_epoch: int | None = None,
    ) -> list[RetrievedChunk]:
        must: list = []
        if source:
            must.append(qm.FieldCondition(key="source", match=qm.MatchValue(value=source)))
        if since_epoch:
            must.append(
                qm.FieldCondition(key="created_epoch", range=qm.Range(gte=since_epoch))
            )
        # scope filter must match message docs (scope_id) OR file docs (scope_ids).
        # A nested Filter with only `should` matches when >=1 sub-condition matches.
        if scope_id:
            must.append(
                qm.Filter(
                    should=[
                        qm.FieldCondition(key="scope_id", match=qm.MatchValue(value=scope_id)),
                        qm.FieldCondition(key="scope_ids", match=qm.MatchValue(value=scope_id)),
                    ]
                )
            )
        query_filter = qm.Filter(must=must) if must else None

        hits = self.client.search(
            collection_name=self.alias,
            query_vector=vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=True,
        )
        return [
            RetrievedChunk(
                doc_id=h.payload.get("doc_id", ""),
                chunk_idx=int(h.payload.get("chunk_idx", 0)),
                score=float(h.score),
                payload=h.payload,
                vector=list(h.vector) if isinstance(h.vector, (list, tuple)) else None,
            )
            for h in hits
        ]

    def fetch_doc_chunks(self, doc_id: str, limit: int = 64) -> list[RetrievedChunk]:
        """Fetch all stored chunks for a doc (context expansion, PRD §14.6)."""
        points, _ = self.client.scroll(
            collection_name=self.alias,
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]
            ),
            with_payload=True,
            limit=limit,
        )
        chunks = [
            RetrievedChunk(
                doc_id=p.payload.get("doc_id", ""),
                chunk_idx=int(p.payload.get("chunk_idx", 0)),
                score=0.0,
                payload=p.payload,
            )
            for p in points
        ]
        return sorted(chunks, key=lambda c: c.chunk_idx)


@lru_cache
def get_store() -> QdrantStore:
    return QdrantStore()
