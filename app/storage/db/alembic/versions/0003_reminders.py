"""reminders table (natural-language reminders delivered by the bot).

Uses metadata.create_all (idempotent) so it only adds the new table.

Revision ID: 0003_reminders
Revises: 0002_thread_tickets
Create Date: 2026-08-21
"""
from __future__ import annotations

from alembic import op

from app.storage.db import models  # noqa: F401 - register tables
from app.storage.db.base import Base

revision = "0003_reminders"
down_revision = "0002_thread_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())  # creates only missing tables


def downgrade() -> None:
    from app.storage.db.models import Reminder

    Reminder.__table__.drop(bind=op.get_bind())
