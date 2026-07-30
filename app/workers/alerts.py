"""Alerting hook (PRD §17): post a short message to a webhook (e.g. Slack incoming webhook)."""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def send_alert(text: str) -> None:
    if not settings.alert_webhook_url:
        log.warning("alert (no webhook configured)", extra={"alert": text})
        return
    try:
        with httpx.Client(timeout=10) as client:
            client.post(settings.alert_webhook_url, json={"text": text})
    except Exception as e:  # noqa: BLE001 - alerting must never raise into a task
        log.warning("alert send failed", extra={"error": str(e)})
