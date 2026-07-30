"""Thin Slack Web API wrapper: rate-limit handling + cursor pagination (PRD §9.3, §9.4)."""

from __future__ import annotations

import time
from collections.abc import Iterator

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Slack API errors that mean "this scope is permanently unreadable" (PRD §9.2).
NON_RETRYABLE_CHANNEL_ERRORS = {
    "not_in_channel",
    "channel_not_found",
    "is_archived",
    "missing_scope",
    "not_allowed_token_type",
}


class SlackApiPermanentError(Exception):
    """Raised for a non-retryable Slack error tied to a scope."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class SlackClient:
    def __init__(self, token: str | None = None, max_retries: int = 6) -> None:
        self.web = WebClient(token=token or settings.slack_bot_token)
        self.max_retries = max_retries

    def call(self, method: str, **kwargs) -> dict:
        """Call a Web API method with 429 backoff + bounded retries."""
        attempt = 0
        while True:
            try:
                return getattr(self.web, method)(**kwargs).data
            except SlackApiError as e:
                code = e.response.status_code
                err = e.response.data.get("error", "") if e.response.data else ""
                if err in NON_RETRYABLE_CHANNEL_ERRORS:
                    raise SlackApiPermanentError(err) from e
                if code == 429:
                    retry_after = int(e.response.headers.get("Retry-After", "5"))
                    log.warning(
                        "slack rate limited",
                        extra={"method": method, "retry_after": retry_after},
                    )
                    time.sleep(retry_after)
                    continue
                attempt += 1
                if attempt > self.max_retries:
                    raise
                backoff = min(2 ** attempt, 30)
                log.warning(
                    "slack error, backing off",
                    extra={"method": method, "error": err or str(e), "backoff": backoff},
                )
                time.sleep(backoff)

    def paginate(
        self, method: str, item_key: str, page_limit: int = 200, **kwargs
    ) -> Iterator[dict]:
        """Yield items across cursor-paginated pages."""
        cursor: str | None = None
        while True:
            resp = self.call(method, cursor=cursor, limit=page_limit, **kwargs)
            for item in resp.get(item_key, []) or []:
                yield item
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return

    def auth_test(self) -> dict:
        return self.call("auth_test")
