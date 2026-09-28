"""LoopGuru CLI — stateless client over the REST API.

Two ways to use it:
  • Interactive shell (default): run with no command → a slash-command REPL.
      loopguru                     # opens the shell
      you › what is the UAT setup?  # plain text = a question
      you › /summarize channel C0123 7
      you › /help
  • One-shot (scriptable):
      loopguru query "how to setup the uat?"
      loopguru summarize channel C0123 --days 7
      loopguru backfill --channel C0123
      loopguru status

Config (flags override env): --url / LOOPGURU_API_URL (default http://localhost:8899),
--key / LOOPGURU_API_KEY (falls back to API_KEY, which is present inside the container).
"""

from __future__ import annotations

import argparse
import html as _html
import os
import re
import shlex
import sys

import httpx

DEFAULT_URL = os.environ.get("LOOPGURU_API_URL", "http://localhost:8899")

# ---- pretty output (ANSI, auto-disabled when not a TTY) --------------------
_TTY = sys.stdout.isatty()
def _c(code: str) -> str:
    return code if _TTY else ""
BOLD, DIM, RESET = _c("\033[1m"), _c("\033[2m"), _c("\033[0m")
CYAN, GREEN, YELLOW, RED = _c("\033[36m"), _c("\033[32m"), _c("\033[33m"), _c("\033[31m")
RULE = "─" * 72


# ---- transport -------------------------------------------------------------

def _api_key(args) -> str:
    key = args.key or os.environ.get("LOOPGURU_API_KEY") or os.environ.get("API_KEY")
    if not key:
        sys.exit("No API key. Pass --key or set LOOPGURU_API_KEY (or API_KEY).")
    return key


def _client(args) -> httpx.Client:
    return httpx.Client(
        base_url=args.url,
        headers={"X-API-Key": _api_key(args), "Content-Type": "application/json"},
        timeout=180,
    )


def _call(client: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    resp = client.request(method, path, json=body)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text
        print(f"{RED}error {resp.status_code}:{RESET} {detail}")
        return {}
    return resp.json()


# ---- printers --------------------------------------------------------------

def print_answer(data: dict) -> None:
    if not data:
        return
    print(f"\n{CYAN}{RULE}{RESET}")
    print((data.get("answer") or "(no answer)").strip())
    citations = data.get("citations") or []
    if citations:
        print(f"\n{DIM}sources{RESET}")
        for i, c in enumerate(citations, 1):
            title = c.get("title") or c.get("scope_id") or c.get("source") or "source"
            print(f"  {BOLD}[{i}]{RESET} {title}")
            if c.get("permalink"):
                print(f"      {DIM}{c['permalink']}{RESET}")
            if c.get("snippet"):
                print(f"      {DIM}“{c['snippet']}”{RESET}")
    print(f"{CYAN}{RULE}{RESET}\n")


def print_summary(data: dict) -> None:
    if not data:
        return
    print(f"\n{CYAN}{RULE}{RESET}")
    print((data.get("summary") or "(no summary)").strip())
    meta = []
    if data.get("permalink"):
        meta.append(data["permalink"])
    if data.get("days") is not None:
        meta.append(f"last {data['days']}d")
    if data.get("message_count") is not None:
        meta.append(f"{data['message_count']} msgs")
    if data.get("doc_count") is not None:
        meta.append(f"{data['doc_count']} items")
    if meta:
        print(f"\n{DIM}{' · '.join(str(m) for m in meta)}{RESET}")
    print(f"{CYAN}{RULE}{RESET}\n")


def print_status(data: dict) -> None:
    if not data:
        return
    print(f"\n{BOLD}Sync status{RESET}")
    for s in data.get("scopes", []):
        age = s.get("days_since_last_success")
        age_s = f"{age}d ago" if age is not None else "never"
        print(f"  {s['scope_id']:<14} {s.get('backfill_status', '?'):<12} last ok: {age_s}")
    runs = (data.get("recent_runs") or [])[:5]
    if runs:
        print(f"{DIM}  recent runs:{RESET}")
        for r in runs:
            print(f"    {r['mode']:<11} {r['status']:<7} "
                  f"chunks={r.get('chunks_upserted', 0)} tokens={r.get('embed_tokens', 0)}")
    print()


# ---- API wrappers (shared by shell + one-shot) -----------------------------

def api_query(client, question, top_k=None, source=None, scope=None) -> dict:
    body: dict = {"question": question}
    if top_k:
        body["top_k"] = top_k
    filt = {k: v for k, v in (("source", source), ("scope_id", scope)) if v}
    if filt:
        body["filters"] = filt
    return _call(client, "POST", "/query", body)


def api_summarize_channel(client, scope, days) -> dict:
    return _call(client, "POST", "/summarize/channel", {"scope_id": scope, "days": days})


def api_summarize_thread(client, channel, ts) -> dict:
    return _call(client, "POST", "/summarize/thread", {"channel_id": channel, "thread_ts": ts})


def api_ingest(client, text, title=None) -> dict:
    body = {"text": text}
    if title:
        body["title"] = title
    return _call(client, "POST", "/admin/ingest", body)


def api_backfill(client, channel=None) -> dict:
    return _call(client, "POST", "/admin/backfill", {"scope_id": channel} if channel else {})


def api_status(client) -> dict:
    return _call(client, "GET", "/admin/sync-status")


def api_purge(client, doc_id=None, channel=None) -> dict:
    return _call(client, "POST", "/admin/purge", {"doc_id": doc_id, "channel_id": channel})


def api_ticket_draft(client, problem, wtype=None) -> dict:
    body = {"problem": problem}
    if wtype:
        body["work_item_type"] = wtype
    return _call(client, "POST", "/ticket/draft", body)


def api_ticket_create(client, draft, assignee=None) -> dict:
    body = {
        "title": draft["title"],
        "description_html": draft.get("description_html", ""),
        "work_item_type": draft.get("work_item_type"),
        "tags": draft.get("tags", []),
    }
    if assignee:
        body["assigned_to"] = assignee
    return _call(client, "POST", "/ticket/create", body)


def _strip_html(s: str) -> str:
    s = re.sub(r"</li>", "", s)
    s = re.sub(r"<li>", "\n  • ", s)
    s = re.sub(r"</?(p|b|ul|div|i)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(s)).strip()


def print_ticket_draft(d: dict) -> None:
    print(f"\n{CYAN}{RULE}{RESET}")
    print(f"{BOLD}{d.get('title', '')}{RESET}")
    print(f"{DIM}type: {d.get('work_item_type')} · tags: {', '.join(d.get('tags', []))}{RESET}\n")
    print(_strip_html(d.get("description_html", "")))
    print(f"{CYAN}{RULE}{RESET}")


class _CLIArgError(Exception):
    pass


class _ArgParser(argparse.ArgumentParser):
    def error(self, message):  # don't sys.exit inside the shell
        raise _CLIArgError(message)


def _ticket_parser() -> _ArgParser:
    p = _ArgParser(prog="ticket", add_help=False)
    p.add_argument("problem", nargs="*")
    p.add_argument("--title")
    p.add_argument("--description", "--desc", dest="description")
    p.add_argument("--type", dest="wtype")
    p.add_argument("--assignee", "--assign", dest="assignee")
    p.add_argument("--tags")  # comma-separated
    p.add_argument("--yes", "-y", action="store_true")
    return p


_TICKET_USAGE = (
    "usage: /ticket <problem statement> [--title ..] [--description ..] [--type ..] "
    "[--assignee ..] [--tags a,b] [--yes]\n"
    "  The AI always drafts the ticket; any flag you pass overrides just that field "
    "(flags in any order). Unset fields use the AI value or the configured default."
)


def _plain_to_html(text: str) -> str:
    body = _html.escape(text or "").replace("\n", "<br>")
    return f"<p>{body}</p><p><i>Filed via loopguru.</i></p>"


def run_ticket(client, *, problem="", title=None, description=None, wtype=None,
               assignee=None, tags=None, auto_yes=False) -> None:
    problem = (problem or "").strip()
    # The LLM ALWAYS drafts. Any field you pass via a flag overrides the drafted value;
    # fields you don't pass keep the AI-drafted value (or the configured default).
    seed = problem or ". ".join(x for x in [title, description] if x).strip()
    if not seed:
        print(f"{YELLOW}{_TICKET_USAGE}{RESET}")
        return
    draft = api_ticket_draft(client, seed, wtype)
    if not draft:
        return
    if title:
        draft["title"] = title
    if description:
        draft["description_html"] = _plain_to_html(description)
    if wtype:
        draft["work_item_type"] = wtype
    if tags:
        draft["tags"] = tags

    print_ticket_draft(draft)
    print(f"{DIM}assignee: {assignee or '(default from config)'}{RESET}")
    if not auto_yes:
        try:
            ans = input(f"{YELLOW}Create this ticket on Azure? [y/N]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print(f"{DIM}cancelled — nothing created.{RESET}")
            return
    res = api_ticket_create(client, draft, assignee=assignee)
    if res:
        print(f"{GREEN}✓ created work item #{res.get('id')}{RESET} → {res.get('url')}")


def _split_tags(raw: str | None) -> list[str] | None:
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else None


def _normalize_ts(s: str | None) -> str | None:
    """Accept a raw ts (1785934372.104539), a p-number (p1785934372104539), or a full
    Slack permalink, and return the canonical thread_ts."""
    if not s:
        return None
    s = s.strip().strip('"').strip("'")
    m = re.search(r"[?&]thread_ts=([0-9.]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/p(\d{10,})", s)  # .../archives/C.../p1785934372104539
    if m:
        s = m.group(1)
    if s.startswith("p") and s[1:].isdigit():
        s = s[1:]
    if s.isdigit() and len(s) > 6:  # 1785934372104539 -> 1785934372.104539
        return f"{s[:-6]}.{s[-6:]}"
    return s


def _channel_from_link(s: str) -> str | None:
    m = re.search(r"/archives/(C[A-Z0-9]+)", s or "")
    return m.group(1) if m else None


# ---- interactive shell -----------------------------------------------------

SLASH_HELP = f"""
{BOLD}LoopGuru — slash commands{RESET}  {DIM}(or just type a question){RESET}
  {GREEN}<question>{RESET}                              ask the agent (grounded + cited)
  {GREEN}/ask{RESET} <question>                         same as above
  {GREEN}/summarize channel{RESET} <scope_id> [days]    channel digest (default 7 days)
  {GREEN}/summarize thread{RESET} <channel_id> <ts>     summarize one thread
  {GREEN}/backfill{RESET} [channel_id]                  pull + index Slack data (alias /fill)
  {GREEN}/ingest{RESET} <text...>                       index raw text (quick manual add)
  {GREEN}/ticket{RESET} <problem...>                     AI drafts the whole ticket
  {GREEN}/ticket{RESET} <problem> [--title ..] [--description ..] [--type ..] [--assignee ..] [--tags a,b]
                                          AI drafts; each flag overrides just that field
  {GREEN}/status{RESET}                                 sync status
  {GREEN}/purge{RESET} <doc_id>                         delete an indexed doc
  {GREEN}/help{RESET}                                   show this help
  {GREEN}/exit{RESET}                                   quit
"""


def run_shell(args) -> None:
    client = _client(args)
    print(f"{BOLD}LoopGuru{RESET} {DIM}(interactive · stateless){RESET}")
    print(f"{DIM}Type a question, or /help for commands, /exit to quit.{RESET}")
    while True:
        try:
            line = input(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if not line.startswith("/"):
                print_answer(api_query(client, line))
                continue
            parts = line[1:].split()
            cmd, rest = parts[0].lower(), parts[1:]
            if cmd in ("exit", "quit", "q"):
                break
            elif cmd == "help":
                print(SLASH_HELP)
            elif cmd in ("ask", "query"):
                q = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                if q:
                    print_answer(api_query(client, q))
                else:
                    print(f"{YELLOW}usage: /ask <question>{RESET}")
            elif cmd in ("summarize", "summary"):
                _shell_summarize(client, rest)
            elif cmd in ("backfill", "fill"):
                data = api_backfill(client, rest[0] if rest else None)
                print(f"{GREEN}enqueued backfill:{RESET} {data.get('enqueued')}")
            elif cmd == "ingest":
                text = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                if not text:
                    print(f"{YELLOW}usage: /ingest <text...>{RESET}")
                else:
                    d = api_ingest(client, text)
                    print(f"{GREEN}ingested{RESET} doc={d.get('doc_id')} chunks={d.get('chunks_upserted')}")
            elif cmd == "ticket":
                rest_str = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                try:
                    ns = _ticket_parser().parse_args(shlex.split(rest_str))
                except (_CLIArgError, ValueError):
                    print(f"{YELLOW}{_TICKET_USAGE}{RESET}")
                else:
                    run_ticket(client, problem=" ".join(ns.problem), title=ns.title,
                               description=ns.description, wtype=ns.wtype,
                               assignee=ns.assignee, tags=_split_tags(ns.tags),
                               auto_yes=ns.yes)
            elif cmd == "status":
                print_status(api_status(client))
            elif cmd == "purge":
                if not rest:
                    print(f"{YELLOW}usage: /purge <doc_id>{RESET}")
                else:
                    print(api_purge(client, doc_id=rest[0]))
            else:
                print(f"{YELLOW}unknown command '/{cmd}'. Try /help{RESET}")
        except Exception as e:  # noqa: BLE001 - never crash the shell
            print(f"{RED}error:{RESET} {e}")


_SUMMARIZE_USAGE = (
    "usage: /summarize channel <scope_id> [days | --days N]  |  "
    "/summarize thread <channel_id> <thread_ts>"
)


def _shell_summarize(client, rest: list[str]) -> None:
    if not rest:
        print(f"{YELLOW}{_SUMMARIZE_USAGE}{RESET}")
        return
    kind = rest[0].lower()
    args = rest[1:]
    # accept both "--days N" and a bare positional number for days
    days = 7
    positional: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("--days", "-d") and i + 1 < len(args):
            days = int(args[i + 1]) if args[i + 1].isdigit() else days
            i += 2
        elif tok.isdigit() and kind == "channel":
            days = int(tok)
            i += 1
        else:
            positional.append(tok)
            i += 1

    if kind == "channel" and positional:
        print_summary(api_summarize_channel(client, positional[0], days))
    elif kind == "thread" and len(positional) == 1 and "/archives/" in positional[0]:
        # just a permalink → parse channel + ts from it
        channel, ts = _channel_from_link(positional[0]), _normalize_ts(positional[0])
        if channel and ts:
            print_summary(api_summarize_thread(client, channel, ts))
        else:
            print(f"{YELLOW}{_SUMMARIZE_USAGE}{RESET}")
    elif kind == "thread" and len(positional) >= 2:
        print_summary(api_summarize_thread(client, positional[0], _normalize_ts(positional[1])))
    else:
        print(f"{YELLOW}{_SUMMARIZE_USAGE}{RESET}")


# ---- one-shot commands -----------------------------------------------------

def cmd_query(args) -> None:
    with _client(args) as c:
        print_answer(api_query(c, args.question, args.top_k, args.source, args.scope))


def cmd_summarize(args) -> None:
    with _client(args) as c:
        if args.target == "channel":
            print_summary(api_summarize_channel(c, args.id, args.days))
        else:
            channel = args.id
            ts = _normalize_ts(args.ts)
            if "/archives/" in (args.id or ""):  # a permalink passed as the id
                channel = _channel_from_link(args.id) or channel
                ts = ts or _normalize_ts(args.id)
            if not ts:
                sys.exit("thread summary needs a thread_ts (or paste the message permalink)")
            print_summary(api_summarize_thread(c, channel, ts))


def cmd_ingest(args) -> None:
    text = args.text if args.text else _read_file(args.file)
    title = args.title or (os.path.basename(args.file) if args.file else None)
    with _client(args) as c:
        d = api_ingest(c, text, title)
    if d:
        print(f"ingested doc={d.get('doc_id')} chunks={d.get('chunks_upserted')} tokens={d.get('embed_tokens')}")


def cmd_backfill(args) -> None:
    with _client(args) as c:
        print("enqueued:", api_backfill(c, args.channel).get("enqueued"))


def cmd_status(args) -> None:
    with _client(args) as c:
        print_status(api_status(c))


def cmd_purge(args) -> None:
    if not (args.doc_id or args.channel):
        sys.exit("Provide --doc-id or --channel")
    with _client(args) as c:
        print(api_purge(c, args.doc_id, args.channel))


def cmd_ticket(args) -> None:
    with _client(args) as c:
        run_ticket(c, problem=args.problem or "", title=args.title,
                   description=args.description, wtype=args.type, assignee=args.assignee,
                   tags=_split_tags(args.tags), auto_yes=args.yes)


def cmd_shell(args) -> None:
    run_shell(args)


def _read_file(path: str) -> str:
    low = (path or "").lower()
    if low.endswith((".md", ".txt", ".markdown")):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    if low.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n\n".join((p.extract_text() or "") for p in PdfReader(path).pages).strip()
    if low.endswith(".docx"):
        import docx
        return "\n".join(p.text for p in docx.Document(path).paragraphs if p.text).strip()
    sys.exit(f"Unsupported file type: {path} (use .md/.txt/.pdf/.docx or --text)")


# ---- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loopguru", description="LoopGuru CLI")
    p.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default {DEFAULT_URL})")
    p.add_argument("--key", default=None, help="API key (else LOOPGURU_API_KEY / API_KEY env)")
    sub = p.add_subparsers(dest="command")  # optional: no command → interactive shell

    sh = sub.add_parser("shell", help="interactive slash shell (default)")
    sh.set_defaults(func=cmd_shell)

    q = sub.add_parser("query", help="ask one question")
    q.add_argument("question")
    q.add_argument("--top-k", type=int, default=None)
    q.add_argument("--source", default=None)
    q.add_argument("--scope", default=None, help="channel id filter")
    q.set_defaults(func=cmd_query)

    s = sub.add_parser("summarize", help="thread or channel summary")
    s.add_argument("target", choices=["channel", "thread"])
    s.add_argument("id", help="scope_id (channel) or channel_id (thread)")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--ts", default=None, help="thread_ts (for thread)")
    s.set_defaults(func=cmd_summarize)

    ing = sub.add_parser("ingest", help="manually index a doc")
    ing.add_argument("--file", default=None)
    ing.add_argument("--text", default=None)
    ing.add_argument("--title", default=None)
    ing.set_defaults(func=cmd_ingest)

    b = sub.add_parser("backfill", help="trigger a Slack backfill")
    b.add_argument("--channel", default=None)
    b.set_defaults(func=cmd_backfill)

    st = sub.add_parser("status", help="show sync status")
    st.set_defaults(func=cmd_status)

    t = sub.add_parser("ticket", help="draft + create an Azure Boards ticket")
    t.add_argument("problem", nargs="?", default="", help="problem statement (agent fills the rest)")
    t.add_argument("--title", default=None, help="explicit title (skips AI draft)")
    t.add_argument("--description", "--desc", dest="description", default=None)
    t.add_argument("--type", default=None, help="work item type (default from env, e.g. Task)")
    t.add_argument("--assignee", "--assign", dest="assignee", default=None)
    t.add_argument("--tags", default=None, help="comma-separated")
    t.add_argument("--yes", "-y", action="store_true", help="skip the confirm prompt")
    t.set_defaults(func=cmd_ticket)

    pr = sub.add_parser("purge", help="delete indexed content")
    pr.add_argument("--doc-id", default=None)
    pr.add_argument("--channel", default=None)
    pr.set_defaults(func=cmd_purge)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        run_shell(args)  # no subcommand → interactive shell
        return
    args.func(args)


if __name__ == "__main__":
    main()
