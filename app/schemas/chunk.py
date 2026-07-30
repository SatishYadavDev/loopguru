"""Chunk schema — the embeddable unit produced from a Document (PRD §10.3)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    doc_id: str
    chunk_idx: int
    text: str  # raw chunk text (stored in payload as `chunk_text`)
    prepend_text: str = ""  # context prepended before embedding (title / sharing msg)

    def embed_input(self) -> str:
        """The exact string embedded: prepend + raw. Query is embedded WITHOUT prepend."""
        if self.prepend_text:
            return f"{self.prepend_text}\n\n{self.text}"
        return self.text
