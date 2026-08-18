"""thread_tickets table (Slack thread → Azure work item, for follow-up edits).

Uses metadata.create_all (idempotent) so it only adds the new table.

Revision ID: 0002_thread_tickets
Revises: 0001_initial
Create Date: 2026-08-18
"""
from __future__ import annotations

from alembic import op

from app.storage.db import models  # noqa: F401 - register tables
from app.storage.db.base import Base

revision = "0002_thread_tickets"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())  # creates only missing tables


def downgrade() -> None:
    from app.storage.db.models import ThreadTicket

    ThreadTicket.__table__.drop(bind=op.get_bind())
