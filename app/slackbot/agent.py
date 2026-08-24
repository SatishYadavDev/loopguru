"""Agentic Slack handler: an LLM tool-calling loop with the whole thread as memory.

The bot reads the thread transcript, then the model decides which tool to call —
answer a question (RAG), summarize, create a ticket, or update the thread's ticket —
and can iterate (follow-ups, "change the assignee", "set the title", etc.).
"""

from __future__ import annotations

import json
import re

from app.core.config import settings
from app.core.logging import get_logger
from app.core.openai_client import get_openai

log = get_logger(__name__)

_SYSTEM = (
    "You are LoopGuru, the team's internal assistant living inside a Slack thread. The full thread so "
    "far is given as context — treat it as memory and resolve follow-ups relative to "
    "earlier turns (e.g. 'explain in one line', 'change the assignee', 'set the title'). "
    "Choose and call the right tool:\n"
    "- answer_question: any factual / how-to question about internal knowledge.\n"
    "- find_discussions: when the user asks WHERE something was discussed, to 'share that "
    "thread', or whether an example / sample / link / doc exists (e.g. 'was this discussed "
    "before? share it', 'koi payable funds ka example/link hai kya?') — returns matching "
    "Slack thread & file links (no answer text, just the links). Prefer this over "
    "answer_question when the user wants the SOURCE/thread itself, not an explanation.\n"
    "- summarize_thread / summarize_channel: summaries.\n"
    "- create_reminder: when the user asks to be reminded (\"kal 5 baje yaad dilana\", "
    "\"remind me in 2 hours\"). Convert their words to an absolute local time and pass it as "
    "when_local in 'YYYY-MM-DD HH:MM' 24-hour form, using CURRENT TIME below as the anchor. "
    "Put WHAT to remind about in text. If they ask to remind SOMEONE ELSE (\"@Darshan ko kal "
    "yaad dila dena\"), pass that person's name in target_name.\n"
    "- list_reminders / cancel_reminder: show or cancel the user's pending reminders.\n"
    "- create_ticket: file an Azure Boards ticket (omit title/description to draft them "
    "from the thread).\n"
    "- get_ticket: read a ticket's CURRENT fields (title, description, state, assignee).\n"
    "- delete_ticket: delete a ticket (recycle bin) when the user asks to remove/delete it.\n"
    "- update_ticket: modify a ticket (title/description/assignee/state). If the user gives "
    "a ticket link or number (e.g. #14761 or a dev.azure.com/.../edit/14761 URL), extract "
    "the numeric id and pass it as work_item_id; otherwise this thread's own ticket is used.\n"
    "IMPORTANT: for a PARTIAL edit (e.g. 'remove the references', 'append X', 'change only "
    "the title'), FIRST call get_ticket, modify the returned content yourself, then call "
    "update_ticket with the full new value. Never claim a change you didn't make via a tool.\n"
    "DISAMBIGUATION: before you update or delete, identify the SPECIFIC ticket. Ticket "
    "numbers from earlier in the thread are visible to you. If the user doesn't say which "
    "ticket and MORE THAN ONE ticket appears in this thread, do NOT guess — ask them which "
    "one, listing the ticket numbers you see. Only omit work_item_id when exactly one ticket "
    "exists in the thread.\n"
    "Keep replies concise for Slack. When a tool returns sources or links, include them "
    "and copy each link EXACTLY as the tool wrote it — Slack link format <url|label>. "
    "Never rewrite links as markdown [label](url); Slack does not render that."
)

_TOOLS = [
    {"type": "function", "function": {
        "name": "answer_question",
        "description": "Answer a question using the org's internal knowledge (Slack docs/messages).",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                       "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "find_discussions",
        "description": ("Find Slack threads/files where a topic was discussed or an example/link "
                        "exists. Returns links only (no generated answer)."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "create_reminder",
        "description": ("Schedule a reminder. when_local is an absolute local time "
                        "'YYYY-MM-DD HH:MM' (24h). target_name only when reminding someone else."),
        "parameters": {"type": "object", "properties": {
            "when_local": {"type": "string"}, "text": {"type": "string"},
            "target_name": {"type": "string"}},
            "required": ["when_local", "text"]}}},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": ("List the user's reminders. Default = upcoming/pending only; pass "
                        "include_done=true when they ask about past/previous/old reminders."),
        "parameters": {"type": "object",
                       "properties": {"include_done": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "cancel_reminder", "description": "Cancel one of the user's pending reminders.",
        "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer"}},
                       "required": ["reminder_id"]}}},
    {"type": "function", "function": {
        "name": "summarize_thread", "description": "Summarize the current thread.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "summarize_channel", "description": "Digest of recent channel activity.",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "create_ticket",
        "description": "Create an Azure Boards ticket. Omit title/description to draft from the thread.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "assignee": {"type": "string"}, "work_item_type": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "delete_ticket",
        "description": "Delete a ticket (moves it to the Azure recycle bin). Pass work_item_id or use the thread's ticket.",
        "parameters": {"type": "object", "properties": {"work_item_id": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_ticket",
        "description": "Read a ticket's current fields. Pass work_item_id, else the thread's ticket.",
        "parameters": {"type": "object", "properties": {"work_item_id": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "update_ticket",
        "description": ("Update a ticket's fields. Pass work_item_id when the user references "
                        "a ticket by link/number; else this thread's ticket is used."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "assignee": {"type": "string"}, "state": {"type": "string"},
            "work_item_id": {"type": "integer"}}}}},
]


def _clean_assignee(v: str | None) -> str | None:
    if not v:
        return v
    m = re.match(r"<mailto:([^|>]+)", v) or re.match(r"<[^|>]*\|([^>]+)>", v)
    return (m.group(1) if m else v).strip("<>").strip()


def _make_tools(channel: str, thread_ts: str | None, transcript: str,
                user_id: str = "", is_dm: bool = False) -> dict:
    # In a channel, knowledge questions stay scoped to that channel; in a DM there is no
    # channel context, so search everything indexed.
    scope = None if is_dm else channel

    def answer_question(question: str = "") -> str:
        from app.rag.service import answer_query
        from app.schemas.query import QueryFilters, QueryRequest
        from app.slackbot.formatting import format_answer

        r = answer_query(QueryRequest(question=question, filters=QueryFilters(scope_id=scope)))
        return format_answer(r)

    def find_discussions(query: str = "", **_) -> str:
        from app.rag.service import find_sources

        hits = find_sources(query, scope_id=scope, limit=3)
        if not hits:
            return "I couldn't find any prior thread or file about that."
        links = "\n".join(
            f"- <{h.permalink}|{h.title or 'discussion'}>"
            + (f" ({h.age_days}d ago)" if h.age_days else "")
            for h in hits
        )
        return f"Here's where this shows up:\n{links}"

    def create_reminder(when_local: str = "", text: str = "", target_name: str | None = None) -> str:
        from app.slackbot.reminders import schedule_reminder

        return schedule_reminder(
            requester_user_id=user_id, channel_id=channel, thread_ts=thread_ts,
            when_local=when_local, text=text, target_name=target_name,
        )

    def list_reminders(include_done: bool = False, **_) -> str:
        from app.slackbot.reminders import render_pending

        return render_pending(user_id, include_done=bool(include_done))

    def cancel_reminder(reminder_id: int = 0, **_) -> str:
        from app.slackbot.reminders import cancel

        return cancel(user_id, int(reminder_id))

    def summarize_thread(**_) -> str:
        from app.rag.summarizer import summarize_thread as st

        return st(channel, thread_ts).summary if thread_ts else "Not inside a thread."

    def summarize_channel(days: int = 7) -> str:
        from app.rag.summarizer import summarize_channel as sc

        return sc(channel, min(int(days or 7), 36500)).summary

    def create_ticket(title=None, description=None, assignee=None, work_item_type=None) -> str:
        from app.connectors.azure.boards import AzureBoardsClient, AzureNotConfigured
        from app.rag.ticket_drafter import draft_ticket
        from app.storage.db import session_scope
        from app.storage.db.repositories import set_thread_ticket

        seed = transcript or f"{title or ''} {description or ''}".strip()
        d = draft_ticket(seed, work_item_type)
        if title:
            d["title"] = title
        if description:
            d["description_html"] = f"<p>{description}</p>"
        if work_item_type:
            d["work_item_type"] = work_item_type
        try:
            wi = AzureBoardsClient().create_work_item(
                title=d["title"], description_html=d["description_html"],
                work_item_type=d.get("work_item_type"), tags=d.get("tags", []),
                assigned_to=_clean_assignee(assignee),
            )
        except AzureNotConfigured:
            return "Azure isn't configured."
        if thread_ts:
            with session_scope() as s:
                set_thread_ticket(s, channel, thread_ts, wi.id)
        return f"Created #{wi.id}: {wi.title} → {wi.url}"

    def _resolve_wid(work_item_id):
        if work_item_id:
            return int(work_item_id)
        if thread_ts:
            from app.storage.db import session_scope
            from app.storage.db.repositories import get_thread_ticket

            with session_scope() as s:
                return get_thread_ticket(s, channel, thread_ts)
        return None

    def delete_ticket(work_item_id=None) -> str:
        from app.connectors.azure.boards import AzureBoardsClient
        from app.storage.db import session_scope
        from app.storage.db.models import ThreadTicket

        wid = _resolve_wid(work_item_id)
        if not wid:
            return "No ticket to delete — give me the ticket link or number."
        AzureBoardsClient().delete_work_item(wid)
        if thread_ts:  # forget the thread↔ticket link
            try:
                with session_scope() as s:
                    row = s.get(ThreadTicket, {"channel_id": channel, "thread_ts": thread_ts})
                    if row and row.work_item_id == wid:
                        s.delete(row)
            except Exception:  # noqa: BLE001
                pass
        return f"Deleted work item #{wid} (moved to the Azure recycle bin)."

    def get_ticket(work_item_id=None) -> str:
        from app.connectors.azure.boards import AzureBoardsClient

        wid = _resolve_wid(work_item_id)
        if not wid:
            return "No ticket — give me the ticket link or number."
        f = AzureBoardsClient().get_work_item(wid)
        a = f.get("System.AssignedTo") or {}
        return json.dumps({
            "id": wid,
            "title": f.get("System.Title"),
            "description_html": f.get("System.Description", ""),
            "state": f.get("System.State"),
            "assigned_to": (a.get("uniqueName") or a.get("displayName")) if isinstance(a, dict) else a,
            "tags": f.get("System.Tags", ""),
        })

    def update_ticket(title=None, description=None, assignee=None, state=None, work_item_id=None) -> str:
        from app.connectors.azure.boards import AzureBoardsClient
        from app.storage.db import session_scope
        from app.storage.db.repositories import set_thread_ticket

        wid = _resolve_wid(work_item_id)
        if not wid:
            return "No ticket to update — create one, or give me the ticket link/number."
        if thread_ts:  # remember the link for future follow-ups in this thread
            with session_scope() as s:
                set_thread_ticket(s, channel, thread_ts, wid)
        fields: dict[str, str] = {}
        if title:
            fields["System.Title"] = title
        if description is not None:
            fields["System.Description"] = (
                description if ("<" in description and ">" in description)
                else "<p>" + description.replace("\n", "<br>") + "</p>"
            )
        if assignee:
            fields["System.AssignedTo"] = _clean_assignee(assignee)
        if state:
            fields["System.State"] = state
        if not fields:
            return "Nothing to update."
        wi = AzureBoardsClient().update_work_item(wid, fields)
        return f"Updated #{wi.id}: {wi.title} → {wi.url}"

    return {
        "answer_question": answer_question,
        "find_discussions": find_discussions,
        "create_reminder": create_reminder,
        "list_reminders": list_reminders,
        "cancel_reminder": cancel_reminder,
        "summarize_thread": summarize_thread,
        "summarize_channel": summarize_channel,
        "create_ticket": create_ticket,
        "get_ticket": get_ticket,
        "update_ticket": update_ticket,
        "delete_ticket": delete_ticket,
    }


def _now_context() -> str:
    """Current local time — the anchor the model uses to resolve 'kal 5 baje' etc."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(settings.reminder_timezone)
    except Exception:  # noqa: BLE001 - bad tz name shouldn't break the bot
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return (f"\nCURRENT TIME: {now:%Y-%m-%d %H:%M} ({now:%A}) "
            f"in timezone {settings.reminder_timezone}.")


_DM_NOTE = (
    "\nYou are in a one-on-one DIRECT MESSAGE with this user, not a channel thread. Every "
    "message here is addressed to you, so always respond to their latest message. For a "
    "greeting or small talk, reply warmly in one line and briefly offer what you can do "
    "(answer questions from internal Slack knowledge, summarize, set reminders, file or edit "
    "Azure tickets). Never say a message wasn't addressed to you."
)


def run_agent(channel: str, thread_ts: str | None, transcript: str, user_id: str = "",
              is_dm: bool = False) -> str:
    tools = _make_tools(channel, thread_ts, transcript, user_id, is_dm)
    system = _SYSTEM + (_DM_NOTE if is_dm else "") + _now_context()
    label = "CONVERSATION" if is_dm else "THREAD"
    ask = ("Respond to the user's latest message." if is_dm
           else "Respond to the most recent request addressed to you.")
    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{label} so far (oldest → newest):\n{transcript}\n\n{ask}"},
    ]
    for _ in range(5):
        resp = get_openai().chat.completions.create(
            model=settings.chat_model, messages=messages, tools=_TOOLS, temperature=0,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "(no reply)"
        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = tools[tc.function.name](**args)
            except Exception as e:  # noqa: BLE001
                result = f"tool error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return "Sorry, I couldn't complete that."
