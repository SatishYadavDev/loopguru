"""Render a QueryResponse into Slack text — answer + sources + prior discussions + a
confidence badge. Shared by the @mention agent tool and the passive-reply task so both
look identical.
"""

from __future__ import annotations

from app.core.config import settings
from app.schemas.query import QueryResponse

_HIGH = 0.6
_MEDIUM = 0.45


def _badge(resp: QueryResponse) -> str:
    s = resp.best_score
    level = "🟢 High confidence" if s >= _HIGH else (
        "🟡 Medium confidence" if s >= _MEDIUM else "🟠 Low confidence"
    )
    n = len(resp.citations)
    return f"{level} · {n} source{'' if n == 1 else 's'}"


def format_answer(resp: QueryResponse) -> str:
    """Full Slack-ready reply. Empty citations → just the answer text."""
    parts = [resp.answer]

    seen: set[str] = set()
    src_lines = []
    for c in resp.citations:
        if not c.permalink or c.permalink in seen:  # one line per unique link
            continue
        seen.add(c.permalink)
        src_lines.append(f"- <{c.permalink}|{c.title or 'source'}>")
    if src_lines:
        parts.append("Sources:\n" + "\n".join(src_lines))

    if resp.related:
        rel = "\n".join(
            f"- <{r.permalink}|{r.title or 'earlier discussion'}> ({r.age_days}d ago)"
            for r in resp.related
        )
        parts.append(f"📌 Ye pehle bhi discuss hua tha:\n{rel}")

    if settings.enable_confidence_badge and resp.citations:
        parts.append(f"_{_badge(resp)}_")

    return "\n\n".join(parts)
