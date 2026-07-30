"""Manual permalink construction — no per-message chat.getPermalink call (PRD §9.7)."""

from __future__ import annotations


def build_permalink(subdomain: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    """https://<sub>.slack.com/archives/<channel>/p<ts_without_dot>[?thread_ts=..&cid=..]"""
    p = "p" + ts.replace(".", "")
    url = f"https://{subdomain}.slack.com/archives/{channel_id}/{p}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url
