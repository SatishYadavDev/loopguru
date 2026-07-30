"""Shared OpenAI client factory (embeddings + chat). Azure-swappable via base_url."""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from app.core.config import settings


@lru_cache
def get_openai() -> OpenAI:
    return OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=settings.openai_timeout_s,
        max_retries=0,  # we do our own bounded retries where needed
    )
