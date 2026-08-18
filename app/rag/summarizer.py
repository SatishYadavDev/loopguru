"""Summarization: thread summaries + channel digests (FEATURES: Summarization & digests).

Reuses the OpenAI chat model. Thread summaries pull the live thread from Slack; channel
digests read already-indexed chunks from Qdrant (no extra Slack calls).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential

from app.connectors.slack.connector import SOURCE, SlackConnector
from app.connectors.slack.normalizer import message_text
from app.connectors.slack.permalink import build_permalink
from app.core.config import settings
from app.core.logging import get_logger
from app.core.openai_client import get_openai
from app.core.tokens import truncate_tokens
from app.storage.qdrant.store import get_store

log = get_logger(__name__)

_THREAD_INSTRUCTION = (
    "Summarize this Slack thread for a teammate catching up. Cover: the main topic, key "
    "points/decisions, any action items (with who, if stated), and open questions. "
    "IMPORTANT: preserve concrete facts verbatim — specific times, dates, numbers, names, "
    "and any ANSWERS given (including self-answers, e.g. someone asking a question and then "
    "answering it). Do not turn an answered question back into an open question. Use short "
    "markdown bullets. Answer in the thread's language."
)
_DIGEST_INSTRUCTION = (
    "You are writing a digest of recent activity in a Slack channel. From the messages "
    "below, produce: main themes/topics, decisions made, action items, and any notable "
    "files/links shared. Be concise, use markdown headings + bullets, and do not invent "
    "anything not present."
)


@dataclass
class Summary:
    summary: str
    permalink: str | None = None
    item_count: int = 0
    period_days: int | None = None


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _summarize(instruction: str, transcript: str) -> str:
    resp = get_openai().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": transcript},
        ],
        temperature=0,  # deterministic — don't lose concrete facts to sampling variance
    )
    return (resp.choices[0].message.content or "").strip()


def summarize_thread(channel_id: str, thread_ts: str) -> Summary:
    conn = SlackConnector()
    conn.prepare()
    lines: list[str] = []
    for m in conn.client.paginate(
        "conversations_replies", "messages", channel=channel_id, ts=thread_ts
    ):
        txt = message_text(m, conn.names, conn.names)
        if not txt:
            continue
        author = conn.names.get(m.get("user", ""), m.get("user", ""))
        lines.append(f"{author}: {txt}")
    if not lines:
        return Summary("No readable messages found in that thread.", None, 0)
    transcript = truncate_tokens("\n".join(lines), settings.rag_context_token_budget)
    summary = _summarize(_THREAD_INSTRUCTION, transcript)
    permalink = build_permalink(conn.subdomain, channel_id, thread_ts)
    return Summary(summary=summary, permalink=permalink, item_count=len(lines))


def summarize_channel(scope_id: str, days: int = 7) -> Summary:
    since = int(time.time()) - days * 86400
    payloads = get_store().fetch_recent(scope_id, since)
    if not payloads:
        return Summary(f"No indexed activity in the last {days} day(s).", None, 0, days)
    # one line per doc (dedup chunks), oldest→newest
    by_doc: dict[str, dict] = {}
    for p in payloads:
        by_doc.setdefault(p.get("doc_id", ""), p)
    docs = sorted(by_doc.values(), key=lambda p: int(p.get("created_epoch", 0) or 0))
    lines = []
    for p in docs:
        who = p.get("author") or ""
        text = " ".join((p.get("chunk_text") or "").split())
        if text:
            lines.append(f"- {who}: {text}")
    transcript = truncate_tokens("\n".join(lines), settings.rag_context_token_budget)
    summary = _summarize(_DIGEST_INSTRUCTION, transcript)
    return Summary(summary=summary, permalink=None, item_count=len(docs), period_days=days)
