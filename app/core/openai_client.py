"""Shared OpenAI client factory (embeddings + chat). Azure-swappable via base_url."""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from app.core.config import settings

# Public OpenAI default. We pass an explicit base_url so an EMPTY `OPENAI_BASE_URL`
# env var (which the SDK would otherwise pick up as "") can't produce a URL with no
# protocol. Azure/self-hosted users set OPENAI_BASE_URL to a real endpoint.
_DEFAULT_BASE_URL = "https://api.openai.com/v1"


@lru_cache
def get_openai() -> OpenAI:
    base_url = settings.openai_base_url.strip() or _DEFAULT_BASE_URL
    return OpenAI(
        api_key=settings.openai_api_key,
        base_url=base_url,
        timeout=settings.openai_timeout_s,
        max_retries=0,  # we do our own bounded retries where needed
    )
