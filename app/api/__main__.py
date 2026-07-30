"""Run the API: `python -m app.api` (dev) or use uvicorn directly."""

from __future__ import annotations

import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,  # we configure JSON logging ourselves
        reload=bool(settings.log_level.upper() == "DEBUG"),
    )
