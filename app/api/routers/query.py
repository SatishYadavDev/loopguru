"""Query endpoint (PRD §14, §15)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import require_api_key
from app.api.ratelimit import rate_limit
from app.core.config import settings
from app.rag.service import answer_query
from app.schemas.query import QueryRequest, QueryResponse

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
def query(
    req: QueryRequest,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit),
) -> QueryResponse:
    question = req.question.strip()
    if not question:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Question must not be empty")
    if len(question) > settings.max_question_chars:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Question exceeds {settings.max_question_chars} characters",
        )
    # answer_query is sync (embeddings/LLM/qdrant) → FastAPI runs this def in a threadpool.
    return answer_query(req)
