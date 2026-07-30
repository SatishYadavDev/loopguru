"""Token counting/splitting helpers (tiktoken, cl100k_base — matches OpenAI embed/chat)."""

from __future__ import annotations

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text or ""))


def truncate_tokens(text: str, max_tokens: int) -> str:
    toks = _enc.encode(text or "")
    if len(toks) <= max_tokens:
        return text
    return _enc.decode(toks[:max_tokens])


def split_tokens(text: str, size: int) -> list[str]:
    toks = _enc.encode(text or "")
    return [_enc.decode(toks[i : i + size]) for i in range(0, len(toks), size)] or [""]


def tail_tokens(text: str, n: int) -> str:
    if n <= 0:
        return ""
    toks = _enc.encode(text or "")
    return _enc.decode(toks[-n:]) if toks else ""
