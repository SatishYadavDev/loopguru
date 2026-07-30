"""Structure-aware chunking + context prepend (PRD §10.3).

- Files: split on headings/paragraphs, keep list runs intact, pack to ~target tokens
  with overlap. Prepend `title` + sharing-message text so queries match even when the
  file body lacks the query's words.
- Messages: usually one chunk; long messages are token-split.
"""

from __future__ import annotations

import re

from app.core.config import settings
from app.core.tokens import count_tokens, split_tokens, tail_tokens
from app.schemas.chunk import Chunk
from app.schemas.document import Document

_HEADING = re.compile(r"^#{1,6}\s")
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def _blocks(text: str) -> list[str]:
    """Split text into logical blocks: headings start blocks; blank lines separate
    paragraphs; consecutive list items stay together.
    """
    blocks: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            joined = "\n".join(cur).strip()
            if joined:
                blocks.append(joined)
            cur.clear()

    for line in text.split("\n"):
        stripped = line.strip()
        if _HEADING.match(stripped):
            flush()
            cur.append(line)
            continue
        if stripped == "" and not (cur and _LIST.match(cur[-1].strip())):
            # blank line ends a paragraph, but not mid-list
            flush()
            continue
        cur.append(line)
    flush()
    return blocks


def _pack(blocks: list[str], target: int, overlap: int, hard_max: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    cur_tokens = 0

    def flush_cur() -> None:
        nonlocal cur, cur_tokens
        if cur:
            chunks.append("\n\n".join(cur).strip())
            cur, cur_tokens = [], 0

    for block in blocks:
        bt = count_tokens(block)
        if bt > hard_max:
            flush_cur()
            chunks.extend(split_tokens(block, target))
            continue
        if cur and cur_tokens + bt > target:
            prev = "\n\n".join(cur)
            flush_cur()
            carry = tail_tokens(prev, overlap)
            if carry:
                cur.append(carry)
                cur_tokens += count_tokens(carry)
        cur.append(block)
        cur_tokens += bt
    flush_cur()

    # final hard cap
    final: list[str] = []
    for c in chunks:
        if count_tokens(c) > hard_max:
            final.extend(split_tokens(c, hard_max))
        else:
            final.append(c)
    return [c.strip() for c in final if c.strip()]


def chunk_document(doc: Document) -> list[Chunk]:
    target = settings.chunk_target_tokens
    overlap = int(target * settings.chunk_overlap_ratio)
    hard_max = settings.chunk_max_tokens

    if doc.kind == "file":
        prepend = " — ".join(
            x for x in [doc.title or "", doc.metadata.get("sharing_text", "")] if x
        ).strip()
        pieces = _pack(_blocks(doc.text), target, overlap, hard_max)
    else:
        prepend = ""
        pieces = (
            [doc.text]
            if count_tokens(doc.text) <= target
            else _pack(_blocks(doc.text), target, overlap, hard_max)
        )

    return [
        Chunk(doc_id=doc.doc_id, chunk_idx=i, text=piece, prepend_text=prepend)
        for i, piece in enumerate(pieces)
    ]
