"""Slack text normalization (PRD §9.5): make raw Slack text human-readable, and
fall back to walking `blocks` (rich_text) when the top-level `text` is empty.
"""

from __future__ import annotations

import html
import re

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
_CHANNEL = re.compile(r"<#(C[A-Z0-9]+)\|([^>]*)>")
_LINK_LABELED = re.compile(r"<(https?://[^>|]+)\|([^>]+)>")
_LINK_BARE = re.compile(r"<(https?://[^>]+)>")
_SPECIAL = re.compile(r"<!(\w+)(?:\|[^>]*)?>")
_BOLD = re.compile(r"\*(?=\S)(.+?)(?<=\S)\*")
_ITALIC = re.compile(r"(?<![\w])_(?=\S)(.+?)(?<=\S)_(?![\w])")
_STRIKE = re.compile(r"~(?=\S)(.+?)(?<=\S)~")


def normalize_text(text: str, users: dict[str, str], channels: dict[str, str]) -> str:
    if not text:
        return ""
    text = _MENTION.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = _CHANNEL.sub(lambda m: "#" + (m.group(2) or channels.get(m.group(1), m.group(1))), text)
    text = _LINK_LABELED.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _LINK_BARE.sub(lambda m: m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    # light mrkdwn: keep code/backticks, drop emphasis markers
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = html.unescape(text)
    return text.strip()


def _walk_rich_text(elements: list) -> str:
    """Collect plain text out of a rich_text block's element tree."""
    parts: list[str] = []
    for el in elements or []:
        etype = el.get("type")
        if etype == "text":
            parts.append(el.get("text", ""))
        elif etype in ("link",):
            parts.append(el.get("text") or el.get("url", ""))
        elif etype == "user":
            parts.append("@" + el.get("user_id", ""))
        elif etype == "channel":
            parts.append("#" + el.get("channel_id", ""))
        elif etype == "emoji":
            parts.append(f":{el.get('name', '')}:")
        elif "elements" in el:
            parts.append(_walk_rich_text(el["elements"]))
    return "".join(parts)


def text_from_blocks(blocks: list) -> str:
    """Fallback extractor when message `text` is empty (PRD §9.5)."""
    out: list[str] = []
    for block in blocks or []:
        if block.get("type") == "rich_text":
            out.append(_walk_rich_text(block.get("elements", [])))
        elif block.get("type") == "section":
            txt = (block.get("text") or {}).get("text", "")
            if txt:
                out.append(txt)
    return "\n".join(p for p in out if p).strip()


def message_text(msg: dict, users: dict[str, str], channels: dict[str, str]) -> str:
    """Best-effort readable text for a Slack message: text, else blocks fallback."""
    raw = msg.get("text") or ""
    if not raw.strip():
        raw = text_from_blocks(msg.get("blocks", []))
    return normalize_text(raw, users, channels)
