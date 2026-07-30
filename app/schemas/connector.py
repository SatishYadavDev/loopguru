"""Connector-facing schemas: Scope, RawItem, SyncCursor (PRD §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ScopeStatus(str, Enum):
    OK = "ok"
    NOT_ACCESSIBLE = "not_accessible"  # bot not a member / no read access
    ARCHIVED = "archived"
    NOT_FOUND = "not_found"


@dataclass
class Scope:
    """A unit to sync (Slack channel, later GitHub repo / Azure board)."""

    source: str
    scope_id: str
    name: str = ""
    is_private: bool = False
    status: ScopeStatus = ScopeStatus.OK
    detail: str = ""


@dataclass
class RawItem:
    """A raw source item yielded by a connector's fetch()."""

    source: str
    scope_id: str
    kind: str  # "message"
    payload: dict = field(default_factory=dict)  # e.g. a Slack message dict
    is_thread_reply: bool = False


@dataclass
class SyncCursor:
    """Opaque, persistable resume point for an interrupted backfill (PRD §7, §11.1)."""

    next_cursor: str | None = None  # Slack pagination cursor
    oldest: str | None = None  # incremental lower bound (Slack ts)

    def to_str(self) -> str | None:
        return self.next_cursor
