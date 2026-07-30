"""Source-agnostic connector interface (PRD §7).

Everything downstream (chunk → embed → store → retrieve → answer) depends only on
`Document`; adding a source means implementing this Protocol and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from app.schemas.connector import RawItem, Scope, SyncCursor
from app.schemas.document import Document


@runtime_checkable
class SourceConnector(Protocol):
    source_name: str

    def list_scopes(self) -> list[Scope]:
        """Units to sync. MUST validate access + mark unreachable scopes."""
        ...

    def fetch(self, scope: Scope, cursor: SyncCursor | None) -> Iterable[RawItem]:
        """Yield raw items newer than the cursor, handling pagination internally."""
        ...

    def to_documents(self, item: RawItem) -> list[Document]:
        """Normalize a raw item into common Documents."""
        ...
