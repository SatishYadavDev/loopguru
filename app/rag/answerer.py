"""LLM answer generation + citation resolution + faithfulness check (PRD §14.9–§14.11)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger
from app.core.openai_client import get_openai
from app.rag.prompt import SYSTEM_PROMPT, build_user_prompt
from app.rag.retriever import Passage
from app.schemas.query import Citation

log = get_logger(__name__)

INSUFFICIENT = "I don't have relevant internal information to answer that."

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)>\]]+")


def _strip_fabricated_urls(text: str, passages: list[Passage]) -> str:
    """Remove any URL the model wrote that is NOT present verbatim in the context.

    The only trustworthy links are the citations the system attaches; the model must never
    invent a URL (e.g. https://example.com/…). Markdown links keep their visible label.
    """
    corpus = "\n".join(p.text for p in passages)
    text = _MD_LINK.sub(lambda m: m.group(0) if m.group(2) in corpus else m.group(1), text)
    text = _BARE_URL.sub(lambda m: m.group(0) if m.group(0) in corpus else "", text)
    # tidy leftovers like "linked here: ()" / "see: ." after a URL was removed
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


@dataclass
class Answer:
    text: str
    citations: list[Citation]
    used_doc_ids: list[str]


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _chat(question: str, passages: list[Passage]) -> dict:
    resp = get_openai().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(question, passages)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


def _parse(content: dict, passages: list[Passage]) -> tuple[str, list[int]]:
    answer = str(content.get("answer", "")).strip()
    cited_raw = content.get("cited", []) or []
    cited: list[int] = []
    for c in cited_raw:
        try:
            cited.append(int(c))
        except (TypeError, ValueError):
            continue
    return answer, cited


def answer_question(question: str, passages: list[Passage]) -> Answer:
    """Call the LLM, resolve [i] citations back to passages, enforce faithfulness."""
    if not passages:
        return Answer(INSUFFICIENT, [], [])

    by_label = {p.label: p for p in passages}
    try:
        content = _chat(question, passages)
        text, cited_labels = _parse(content, passages)
    except Exception as e:  # noqa: BLE001
        log.warning("chat/parse failed", extra={"error": str(e)})
        return Answer(INSUFFICIENT, [], [])

    # faithfulness: keep only labels that exist; drop fabricated ones (PRD §14.11)
    valid = [lbl for lbl in cited_labels if lbl in by_label]

    if not text:
        return Answer(INSUFFICIENT, [], [])
    if not valid:
        # answer with no real citation → downgrade
        return Answer(INSUFFICIENT, [], [])

    text = _strip_fabricated_urls(text, passages)  # kill any hallucinated link

    citations = [
        Citation(
            source=by_label[lbl].source,
            title=by_label[lbl].title,
            permalink=by_label[lbl].permalink,
            scope_id=by_label[lbl].scope_id,
            snippet=by_label[lbl].snippet,
        )
        for lbl in valid
    ]
    used_doc_ids = [by_label[lbl].doc_id for lbl in valid]
    return Answer(text, citations, used_doc_ids)
