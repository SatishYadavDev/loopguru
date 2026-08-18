"""Retrieval: hybrid (vector + BM25/keyword, fused by RRF) → dedup → recency reorder →
neighbor expansion → token-budgeted context assembly (PRD §14.3–§14.8).
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass

from app.core.config import settings
from app.core.tokens import count_tokens, truncate_tokens
from app.schemas.query import QueryRequest
from app.storage.qdrant.store import QdrantStore, RetrievedChunk

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _bm25_scores(query: str, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Classic BM25 over a small candidate set (the retrieved pool)."""
    n = len(docs)
    if n == 0:
        return []
    avgdl = (sum(len(d) for d in docs) / n) or 1.0
    df: Counter = Counter()
    for d in docs:
        for term in set(d):
            df[term] += 1
    q_terms = set(_tokenize(query))
    scores: list[float] = []
    for d in docs:
        tf = Counter(d)
        dl = len(d) or 1
        s = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def _rrf_fuse(candidates: list[RetrievedChunk], query: str, k: int) -> None:
    """Reciprocal Rank Fusion of vector rank (current .score = cosine) and BM25 rank.
    Overwrites each candidate's .score with the fused score (used for ordering only).
    """
    n = len(candidates)
    if n == 0:
        return
    vec_order = sorted(range(n), key=lambda i: candidates[i].score, reverse=True)
    vrank = {i: r for r, i in enumerate(vec_order)}
    bm = _bm25_scores(query, [_tokenize(c.text) for c in candidates])
    bm_order = sorted(range(n), key=lambda i: bm[i], reverse=True)
    brank = {i: r for r, i in enumerate(bm_order)}
    for i, c in enumerate(candidates):
        c.score = 1.0 / (k + vrank[i]) + 1.0 / (k + brank[i])


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


def _clean_snippet(text: str, max_chars: int = 200) -> str:
    """A short, human-readable preview: collapse whitespace, cut at a word boundary, add …."""
    flat = " ".join((text or "").split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rsplit(" ", 1)[0].rstrip() + " …"


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
    src = f.source if f else None
    scope = f.scope_id if f else None
    since = f.since_epoch if f else None

    vhits = store.search(query_vector, overfetch, src, scope, since)
    khits: list[RetrievedChunk] = []
    if settings.rag_hybrid:
        try:
            khits = store.keyword_search(req.question, overfetch, src, scope, since)
        except Exception:  # noqa: BLE001 - keyword index may be absent; vector-only still works
            khits = []

    # union candidates by (doc_id, chunk_idx), preferring the vector hit (has cosine score)
    cand: dict[tuple[str, int], RetrievedChunk] = {}
    for h in vhits:
        cand[(h.doc_id, h.chunk_idx)] = h
    for h in khits:
        cand.setdefault((h.doc_id, h.chunk_idx), h)
    hits = list(cand.values())
    if not hits:
        return RetrievalResult([], 0.0, False)

    # keyword-only candidates arrive unscored — compute their cosine vs the query vector
    for h in hits:
        if h.score == 0.0 and h.vector:
            h.score = _cosine(query_vector, h.vector)

    # relevance floor is calibrated on cosine, so capture it BEFORE fusion overwrites score
    best_score = max((h.score for h in hits), default=0.0)

    if settings.rag_hybrid and khits:
        _rrf_fuse(hits, req.question, settings.rag_rrf_k)

    hits = _dedup(hits)
    hits = _unique_docs(hits)

    now = time.time()
    hits.sort(key=lambda h: h.score * _recency_factor(h.created_epoch, now), reverse=True)
    hits = hits[:top_k]

    # Assemble with an overall token budget. We deliberately do NOT split the budget
    # evenly across top_k: a long procedural doc (e.g. a multi-section setup guide) must
    # be allowed to occupy most/all of the budget so its steps arrive complete. Passages
    # are added in score order and we stop once the budget is exhausted.
    budget = settings.rag_context_token_budget
    per_passage_cap = budget
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
                snippet=_clean_snippet(h.text),
                created_epoch=h.created_epoch,
                score=h.score,
            )
        )
    return RetrievalResult(passages, best_score, True)
