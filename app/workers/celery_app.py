"""Celery application + Beat schedule (PRD §16). RabbitMQ broker, durable queues."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery(
    "loopguru",
    broker=settings.celery_broker_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,               # redeliver if a worker dies mid-task
    worker_prefetch_multiplier=1,      # backpressure: one heavy task at a time
    task_reject_on_worker_lost=True,
    task_default_queue="knowledge",
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    result_backend=None,
    timezone="UTC",
    beat_schedule={
        # Daily sweep: bootstrap pending backfills + run incremental for completed scopes.
        # Reminders: check every minute so a scheduled ping fires on time.
        "reminders-tick": {
            "task": "app.workers.tasks.deliver_due_reminders",
            "schedule": 60.0,
        },
        "daily-sync": {
            "task": "app.workers.tasks.daily_sweep",
            "schedule": crontab(hour=2, minute=0),
        },
        # Weekly full re-backfill/reconcile (deletions + edit drift).
        "weekly-reconcile": {
            "task": "app.workers.tasks.weekly_reconcile",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),  # Sundays
        },
        # Stale-scope alarm: catches a dead beat/worker (zero rows, not a failed row).
        "stale-check": {
            "task": "app.workers.tasks.check_stale_scopes",
            "schedule": crontab(hour="*/6", minute=15),
        },
        # Channel digests: daily (last 1 day) + weekly (last 7 days).
        "daily-digest": {
            "task": "app.workers.tasks.channel_digest",
            "schedule": crontab(hour=6, minute=0),
            "args": (1,),
        },
        "weekly-digest": {
            "task": "app.workers.tasks.channel_digest",
            "schedule": crontab(hour=6, minute=30, day_of_week=1),  # Mondays
            "args": (7,),
        },
    },
)


@setup_logging.connect
def _configure(**_kwargs) -> None:
    configure_logging(settings.log_level)
