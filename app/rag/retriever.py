"""Retrieval: over-fetch → dedup → recency reorder → neighbor expansion →
token-budgeted context assembly (PRD §14.3–§14.8).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.tokens import count_tokens, truncate_tokens
from app.schemas.query import QueryRequest
from app.storage.qdrant.store import QdrantStore, RetrievedChunk


@dataclass
class Passage:
    label: int
    doc_id: str
    source: str
    scope_id: str | None
    title: str | None
    permalink: str | None
    text: str
    snippet: str
    created_epoch: int
    score: float


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _recency_factor(created_epoch: int, now: float) -> float:
    if created_epoch <= 0:
        return 1.0
    age_days = max(0.0, (now - created_epoch) / 86400.0)
    return math.exp(-age_days / max(1.0, settings.rag_recency_halflife_days))


def _dedup(hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Drop exact repeats (content_hash) then near-twins (mutual cosine ≥ threshold)."""
    kept: list[RetrievedChunk] = []
    seen_hashes: set[str] = set()
    for h in sorted(hits, key=lambda x: x.score, reverse=True):
        ch = h.content_hash
        if ch and ch in seen_hashes:
            continue
        if any(_cosine(h.vector, k.vector) >= settings.rag_dedup_sim for k in kept):
            continue
        kept.append(h)
        if ch:
            seen_hashes.add(ch)
    return kept


def _unique_docs(hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Keep the best-scoring chunk per doc_id (one citation per document)."""
    best: dict[str, RetrievedChunk] = {}
    for h in hits:
        cur = best.get(h.doc_id)
        if cur is None or h.score > cur.score:
            best[h.doc_id] = h
    return list(best.values())


def _expand_text(store: QdrantStore, hit: RetrievedChunk) -> str:
    """Small-to-big: whole doc if small, else the ±1 neighbor window (PRD §14.6)."""
    chunks = store.fetch_doc_chunks(hit.doc_id)
    if not chunks:
        return hit.text
    if len(chunks) <= settings.rag_expand_max_chunks:
        return "\n\n".join(c.text for c in chunks)
    idx = hit.chunk_idx
    window = [c for c in chunks if abs(c.chunk_idx - idx) <= 1]
    return "\n\n".join(c.text for c in sorted(window, key=lambda c: c.chunk_idx))


@dataclass
class RetrievalResult:
    passages: list[Passage]
    best_score: float
    had_hits: bool


def retrieve(store: QdrantStore, query_vector: list[float], req: QueryRequest) -> RetrievalResult:
    top_k = req.top_k or settings.rag_top_k
    overfetch = max(settings.rag_overfetch_k, top_k)
    f = req.filters
    hits = store.search(
        query_vector,
        overfetch,
        source=f.source if f else None,
        scope_id=f.scope_id if f else None,
        since_epoch=f.since_epoch if f else None,
    )
    if not hits:
        return RetrievalResult([], 0.0, False)

    best_score = max(h.score for h in hits)

    hits = _dedup(hits)
    hits = _unique_docs(hits)

    now = time.time()
    hits.sort(key=lambda h: h.score * _recency_factor(h.created_epoch, now), reverse=True)
    hits = hits[:top_k]

    # assemble with token budget
    budget = settings.rag_context_token_budget
    per_passage_cap = max(256, budget // max(1, top_k))
    passages: list[Passage] = []
    used = 0
    for h in hits:
        text = truncate_tokens(_expand_text(store, h), per_passage_cap)
        cost = count_tokens(text)
        if passages and used + cost > budget:
            break
        used += cost
        passages.append(
            Passage(
                label=len(passages) + 1,
                doc_id=h.doc_id,
                source=h.payload.get("source", ""),
                scope_id=h.payload.get("scope_id") or (
                    (h.payload.get("scope_ids") or [None])[0]
                ),
                title=h.payload.get("title"),
                permalink=h.payload.get("permalink"),
                text=text,
                snippet=truncate_tokens(h.text, 80).strip(),
                created_epoch=h.created_epoch,
                score=h.score,
            )
        )
    return RetrievalResult(passages, best_score, True)
