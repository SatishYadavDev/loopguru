"""Simple in-process fixed-window rate limiter (PRD §15).

Note: per-process. With multiple uvicorn workers the effective limit multiplies;
for stricter global limiting move this to Redis. Adequate for internal v1.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request, status

from app.core.config import settings

_lock = threading.Lock()
_buckets: dict[str, tuple[float, int]] = {}


def _client_key(request: Request) -> str:
    api_key = request.headers.get("x-api-key", "")
    ip = request.client.host if request.client else "unknown"
    return f"{api_key[:8]}:{ip}"


def rate_limit(request: Request) -> None:
    limit, window = settings.rate_limit_parts()
    key = _client_key(request)
    now = time.time()
    with _lock:
        start, count = _buckets.get(key, (now, 0))
        if now - start >= window:
            start, count = now, 0
        count += 1
        _buckets[key] = (start, count)
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
