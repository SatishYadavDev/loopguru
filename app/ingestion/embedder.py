"""OpenAI embeddings: batched, per-input token-capped, with bounded retries (PRD §10.4)."""

from __future__ import annotations

from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.openai_client import get_openai
from app.core.tokens import truncate_tokens

_MAX_INPUT_TOKENS = 8191  # text-embedding-3-* hard limit


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    total_tokens: int


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=30))
def _embed_batch(inputs: list[str]) -> tuple[list[list[float]], int]:
    resp = get_openai().embeddings.create(model=settings.embedding_model, input=inputs)
    vectors = [d.embedding for d in resp.data]
    tokens = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
    return vectors, tokens


def embed_texts(texts: list[str]) -> EmbedResult:
    """Embed many strings. Each input is truncated to the model's token ceiling."""
    if not texts:
        return EmbedResult([], 0)
    prepared = [truncate_tokens(t, _MAX_INPUT_TOKENS) for t in texts]
    vectors: list[list[float]] = []
    total = 0
    batch = max(1, settings.embed_batch_size)
    for i in range(0, len(prepared), batch):
        vecs, tokens = _embed_batch(prepared[i : i + batch])
        vectors.extend(vecs)
        total += tokens
    return EmbedResult(vectors, total)


def embed_query(text: str) -> list[float]:
    """Embed a single query string RAW (no prepend; PRD §14.2)."""
    return embed_texts([text]).vectors[0]
