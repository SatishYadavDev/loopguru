"""Prompt construction for grounded, cited, procedural answers (PRD §14.9–§14.10)."""

from __future__ import annotations

from app.rag.retriever import Passage

SYSTEM_PROMPT = """You are LoopGuru, the team's internal knowledge assistant. Answer ONLY using the \
numbered CONTEXT passages provided by the user. Rules:
- If the context does not contain the answer, say you don't have relevant internal \
information. Never invent facts or steps.
- When the context describes a procedure, present it as clear ordered steps.
- When passages conflict, prefer the MOST RECENT one and note that older guidance was \
superseded.
- Answer in the SAME language as the question (English, Hindi, or Hinglish).
- Cite the passages you actually used by their [number].

Respond ONLY with a JSON object of the form:
{"answer": "<your answer as markdown text>", "cited": [<numbers of passages you used>]}
Do not wrap it in code fences. `cited` must be a subset of the provided passage numbers."""


def build_user_prompt(question: str, passages: list[Passage]) -> str:
    lines = ["CONTEXT:"]
    for p in passages:
        meta = []
        if p.title:
            meta.append(f"title={p.title}")
        if p.created_epoch:
            meta.append(f"posted_epoch={p.created_epoch}")
        header = f"[{p.label}]" + (f" ({', '.join(meta)})" if meta else "")
        lines.append(f"{header}\n{p.text}")
    lines.append("")
    lines.append(f"QUESTION: {question}")
    return "\n\n".join(lines)
