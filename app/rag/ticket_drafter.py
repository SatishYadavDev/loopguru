"""Draft an Azure Boards work item from a problem statement, enriched with related
internal Slack context (retrieved via the RAG pipeline).
"""

from __future__ import annotations

import html
import json

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.openai_client import get_openai
from app.ingestion.embedder import embed_query
from app.rag.retriever import retrieve
from app.schemas.query import QueryRequest
from app.storage.qdrant.store import get_store

_INSTRUCTION = (
    "You draft an Azure DevOps work item from a short problem statement. Use the optional "
    "internal context only if relevant; never invent specifics. Respond ONLY as JSON: "
    '{"title": "<concise imperative title>", "summary": "<1-3 sentence problem/goal>", '
    '"acceptance_criteria": ["<done condition>", ...], "tags": ["<short-label>", ...]}'
)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _chat_json(problem: str, context: str) -> dict:
    user = f"PROBLEM STATEMENT:\n{problem}\n\nINTERNAL CONTEXT (optional):\n{context or '(none)'}"
    resp = get_openai().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": _INSTRUCTION},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _build_html(summary: str, criteria: list[str], sources: list[dict]) -> str:
    parts = [f"<p>{html.escape(summary)}</p>"]
    if criteria:
        items = "".join(f"<li>{html.escape(c)}</li>" for c in criteria)
        parts.append(f"<b>Acceptance criteria</b><ul>{items}</ul>")
    if sources:
        links = "".join(
            f'<li><a href="{html.escape(s["permalink"])}">{html.escape(s["title"])}</a></li>'
            for s in sources if s.get("permalink")
        )
        if links:
            parts.append(f"<b>References (from Slack)</b><ul>{links}</ul>")
    parts.append("<p><i>Drafted by loopguru.</i></p>")
    return "".join(parts)


def draft_ticket(problem: str, work_item_type: str | None = None) -> dict:
    """Return a draft: {title, description_html, tags, work_item_type, sources}."""
    # pull related internal context (best-effort)
    sources: list[dict] = []
    context = ""
    try:
        # channel-id → readable name (dynamic, from the cached Slack identities)
        from app.storage.db import session_scope
        from app.storage.db.repositories import identity_map

        try:
            with session_scope() as session:
                names = identity_map(session, "slack")
        except Exception:  # noqa: BLE001
            names = {}

        res = retrieve(get_store(), embed_query(problem), QueryRequest(question=problem))
        lines = []
        seen: set[str] = set()
        for i, p in enumerate(res.passages[:4], 1):
            lines.append(f"[{i}] {p.text}")
            if p.permalink and p.permalink not in seen:
                seen.add(p.permalink)
                if p.title:
                    label = p.title
                elif p.scope_id and names.get(p.scope_id):
                    label = f"#{names[p.scope_id]}"
                else:
                    label = "Slack message"
                sources.append({"title": label, "permalink": p.permalink})
        context = "\n\n".join(lines)
    except Exception:  # noqa: BLE001 - drafting must work even if retrieval fails
        pass

    data = _chat_json(problem, context)
    title = (data.get("title") or problem[:120]).strip()
    summary = (data.get("summary") or problem).strip()
    criteria = [str(c) for c in (data.get("acceptance_criteria") or [])]
    tags = ["loopguru"] + [str(t) for t in (data.get("tags") or [])]

    return {
        "title": title,
        "description_html": _build_html(summary, criteria, sources),
        "tags": tags,
        "work_item_type": work_item_type or settings.azure_devops_workitem_type or "Task",
        "sources": [s["permalink"] for s in sources],
    }
