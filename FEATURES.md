# LoopGuru — Features & Architecture

An internal, **query-based AI agent** that indexes the organization's Slack knowledge and
answers questions in plain language — grounded **strictly in internal data**, with source
citations. Current focus: **Slack** (built to extend to GitHub / Azure later).

## Architecture

```mermaid
flowchart LR
    U["User / curl"] -->|question| API
    CLI["CLI"] --> API
    CLI --> ADMIN
    Slack["Slack Web API"] --> FETCH

    BEAT["Celery Beat<br/>daily / weekly schedule"] --> MQ["RabbitMQ<br/>task queue"]
    ADMIN["/admin — backfill, ingest"] --> MQ
    MQ --> FETCH

    subgraph Ingest["Ingestion — Celery worker"]
      direction TB
      FETCH["Fetch messages, threads, files"] --> EXTRACT["Extract text: PDF / MD / TXT / DOCX"]
      EXTRACT --> HASH["Skip if unchanged (content_hash)"]
      HASH --> CHUNK["Chunk + prepend context"]
      CHUNK --> EMBED["Embed"]
    end

    subgraph Query["Query — FastAPI (stateless)"]
      direction TB
      API["POST /query<br/>(API-key + rate-limit)"] --> RETRIEVE["Search → dedup → recency → expand"]
      RETRIEVE --> ANSWER["LLM answer + citations<br/>(grounded, or refuse)"]
    end

    OAI["OpenAI API<br/>embeddings + chat"]

    subgraph Store["Storage (Docker volumes)"]
      QDRANT[("Qdrant — vector embeddings")]
      PG[("Postgres — state, registry, audit")]
    end

    EMBED --> OAI
    EMBED --> QDRANT
    FETCH --> PG
    RETRIEVE --> OAI
    RETRIEVE --> QDRANT
    ANSWER --> OAI
    ANSWER --> PG
    ANSWER -->|answer + sources| U
```

## ✅ Built (Slack v1)

| Feature | What it does |
|---|---|
| **Slack ingestion** | Allowlisted channels — messages, thread replies, and file attachments |
| **File text extraction** | PDF, Markdown, TXT, DOCX, and **Slack Canvases** → clean text for indexing |
| **Image & diagram OCR (vision)** | Screenshots, diagrams, and scanned/text-less PDFs are transcribed via a vision model so their content becomes searchable |
| **Grounded RAG Q&A** | Semantic search + LLM answer using **only internal data** — refuses instead of hallucinating |
| **Source citations** | Every answer links back to the exact Slack message / file (permalinks) |
| **Hybrid retrieval (BM25 + vector)** | Keyword (BM25) fused with vector search via RRF — exact terms/acronyms (e.g. "YTM", "UAT") rank well, not only semantic matches |
| **Procedural / how-to** | "How do I set up X?" → full step-by-step guide reproduced from the doc |
| **Summaries & digests** | On-demand **thread summaries** + **channel digests**; scheduled daily/weekly digests (delivered via alert webhook; Slack-posting pending `chat:write`) |
| **Azure Boards ticket (action)** | Describe a problem → agent drafts a work item (title, acceptance criteria, **enriched with Slack references**) and files it on **Azure DevOps** with a confirm step |
| **Slack @mention bot** | `@loopguru <question>` in a channel → grounded + cited reply in-thread; also `@… summarize` (thread/channel) and `@… ticket <problem>` |
| **Incremental sync** | Resumable first-time backfill + daily auto-sync; deletions reconciled weekly |
| **Idempotent + dedup** | Re-runs never duplicate; unchanged content is skipped (saves time & cost) |
| **REST API + Auth** | `POST /query` + admin endpoints, API-key auth, rate limiting |
| **CLI (`./loopguru`)** | Short launcher → interactive **slash shell** (`/help`, `/ask`, `/summarize`, `/backfill`, `/ingest`, `/status`, `/purge`) + one-shot commands; clean formatted output |
| **Multilingual** | Answers in the question's language (English / Hindi / Hinglish) |
| **Observability** | Sync-status endpoint, per-query audit log, structured JSON logs |
| **Dockerized stack** | One command brings up API, workers, scheduler, Postgres, Qdrant, RabbitMQ |

## 🚧 Building next

- **Real-time sync** — index new messages instantly via the Slack Events API.
- **Evaluation gold-set + optional cross-encoder reranker** — measure and further improve
  answer quality (hybrid BM25+vector is already done).

## Try each feature (commands)

Start the stack: `docker compose up -d`. Then use the **`./loopguru`** launcher (opens the
interactive shell) — or the one-shot forms below.

```bash
./loopguru                                        # interactive slash shell (/help inside)

# Ask / RAG Q&A (grounded + cited; hybrid BM25+vector)
./loopguru query "how do I set up the UAT?"
#   in shell:  what is yield to maturity?     (plain text = a question)

# Summaries & digests
./loopguru summarize channel C0BKH1Z7PNH --days 7     #  shell: /summarize channel C0.. 7
./loopguru summarize thread  C0BKH1Z7PNH --ts 1785...  #  shell: /summarize thread C0.. 1785...

# Ingest / index data (Slack backfill, or a manual doc)
./loopguru backfill --channel C0BKH1Z7PNH             #  shell: /backfill C0..   (alias /fill)
./loopguru ingest --file ./steps.md --title "UAT setup"

# Azure Boards ticket — AI always drafts; each flag overrides just that field
./loopguru ticket "users get a 404 navigating Agent → Fund; fix the route"      # AI fills everything
./loopguru ticket "OTP email not arriving on UAT" --title "Fix UAT OTP" --type Bug --assignee me@example.com
#  shell:  /ticket <problem> [--title ..] [--description ..] [--type ..] [--assignee ..] [--tags a,b]

# Ops
./loopguru status                                     #  shell: /status
./loopguru purge --doc-id slack:file:F0...            #  shell: /purge <doc_id>
```

Image/PDF OCR needs no command — post an image/PDF in an indexed channel, run `backfill`,
then ask about it. Admin HTTP endpoints (same actions) are at `http://localhost:8899/docs`.

**In Slack** (once the @mention bot is wired): `@loopguru how do I set up the UAT?`
· `@loopguru summarize` (in a thread → that thread; else the channel) ·
`@loopguru ticket <problem>`.

## Tech stack

Python · FastAPI · Celery + RabbitMQ · Qdrant (vectors) · Postgres · OpenAI (embeddings +
chat + vision) · PyMuPDF · Docker Compose

## Progress log

- **2026-08-18 — Agentic Slack bot (thread memory + tools):** ✅ done. The bot's first-word
  routing is replaced by an **LLM tool-calling loop** (`app/slackbot/agent.py`) that gets
  the **whole thread as memory** and calls tools: `answer_question` (RAG), `summarize_thread`
  / `summarize_channel`, `create_ticket`, `update_ticket`. So a thread supports natural
  follow-ups ("explain in one line") and iterative ticket edits ("change the assignee",
  "set the title"). `update_ticket` also accepts a ticket **link/number** (the model
  extracts the id → `work_item_id`), so any ticket can be updated, not only this thread's.
  A `get_ticket` tool + `AzureBoardsClient.get_work_item` let the agent **read a ticket
  before editing**, so partial edits ("remove the references", "append X") actually apply
  instead of the model claiming a change it didn't make. `delete_ticket` removes a ticket
  (Azure recycle bin, recoverable) by link/number or the thread's ticket. If a thread has
  more than one ticket and the user doesn't say which, the bot **asks** (lists the numbers)
  instead of guessing.
  The thread↔ticket link is stored in a new `thread_tickets` table
  (migration `0002`); Azure gained `update_work_item` (PATCH). CLI + all endpoints
  unchanged. Verified: a follow-up "explain in one line" resolved against prior context.
- **2026-08-18 — Slack @mention bot:** ✅ done & connected. Two transports share one
  handler (`handle_mention` Celery task → routes to query/summarize/ticket → replies
  in-thread, questions scoped to the channel): **(a) Socket Mode** (`app/slackbot`, a
  `slackbot` compose service using `SLACK_APP_TOKEN` — no public URL; live-connected) and
  **(b) HTTP Events** (`POST /slack/events`, HMAC-verified with `SLACK_SIGNING_SECRET`, for
  when a public URL is preferred). Owner just subscribes the `app_mention` bot event.
  - `@… ticket` **inside a thread** reads the whole thread and drafts the ticket
    (title/description) from it; `assign:<email>` / `type:<Task|Bug>` inline overrides.
    `@… summarize N` sets the digest window (N days).
- **2026-08-10 — Slack Canvas ingestion:** ✅ done. Canvases (`application/vnd.slack-docs`
  / `quip`) are fetched from `url_private` (HTML) with the existing `files:read` scope —
  **no reinstall needed** — stripped to text and indexed. Verified: the "UAT company ids"
  canvas's `treasury_cli.py` command is now answerable (scoped).
- **2026-08-10 — Azure Boards ticket (action):** ✅ done. `/ticket <problem>` (shell) or
  `./loopguru ticket "..."`: LLM drafts a work item enriched with related Slack context, shows
  it, and on confirm files it on Azure DevOps (`AZURE_DEVOPS_ORG/PROJECT/PAT`,
  `POST /ticket/draft` + `/ticket/create`). Draft verified end-to-end; the actual create
  is user-run (outward write to a shared board).
  - _fix (2026-08-10): missing `re` import in the CLI broke the draft preview render — fixed._
  - _fix (2026-08-10): projects requiring `System.AssignedTo` — now set via `AZURE_DEVOPS_ASSIGNED_TO` / `--assignee`._
  - _fix (2026-08-18): ticket "References (from Slack)" now show readable `#channel` names
    (resolved dynamically from the identity cache) instead of raw channel IDs, and are
    de-duplicated by permalink._
  - _2026-08-10: the AI **always drafts** the ticket; each flag
    (`--title/--description/--type/--assignee/--tags`, any order) **overrides just that
    field**; unset fields keep the AI value or the configured default._
- **2026-08-07 — CLI overhaul:** ✅ done. New `./loopguru` short launcher opens an interactive
  **slash shell** (plain text = a question; `/help`, `/ask`, `/summarize`, `/backfill`
  (`/fill`), `/ingest`, `/status`, `/purge`, `/exit`) with clean ANSI-formatted output;
  one-shot subcommands retained. `/summarize` added.
- **2026-08-10 — Summaries fixes:** shell `/summarize` now honours `--days N` (previously
  only a bare positional number worked, so `--days` silently fell back to 7); channel
  digest day cap raised 90 → 36500 so a large value summarizes the **whole channel**.
  `/summarize thread` now accepts a pasted **message permalink** or `p…` number (channel +
  thread_ts auto-parsed) — no manual dot-insertion. Summaries are now **deterministic**
  (temperature 0) and the prompt preserves concrete facts/answers (times, dates, numbers) —
  e.g. reliably captures "college starts at 10 AM" instead of dropping it to variance.
- **2026-08-07 — Summaries & digests:** ✅ done. `POST /summarize/thread` and
  `POST /summarize/channel` (reads indexed chunks, no extra Slack calls); Celery Beat
  `channel_digest` runs daily (1d) + weekly (7d), delivered via the alert webhook (Slack
  posting pending `chat:write`). Verified: #testing channel digest.
- **2026-08-07 — Hybrid retrieval (BM25 + vector):** ✅ done. Vector search + full-text
  keyword recall (Qdrant text index) fused with **Reciprocal Rank Fusion** and an
  in-process **BM25**; relevance floor still gated on cosine. Config: `RAG_HYBRID`,
  `RAG_RRF_K`. Verified: bare acronym query "YTM" now retrieves + answers correctly.
- **2026-08-05 — Image & diagram OCR (vision):** ✅ done & **verified end-to-end**. Images
  (PNG/JPG/GIF/WebP) and text-less/scanned PDFs are transcribed via the OpenAI vision model
  (`ENABLE_VISION`, `VISION_MODEL`); scanned PDFs rasterized with PyMuPDF. Proven live: a
  "Yield to Maturity" **image** posted in Slack was OCR'd and answered with citation.
- **2026-08-05 — Bug fix:** shared/forwarded messages carry files under
  `attachments[].files` (not `msg.files`) — the connector now collects both (deduped), so
  forwarded images/files get indexed too.
- **2026-08-05 — Retrieval tuning:** context budget + whole-doc expansion raised so long
  procedural docs (e.g. multi-section setup guides) return complete answers.
- **In progress / next:** thread + channel summaries · hybrid BM25 + vector retrieval ·
  Azure Boards ticket action _(needs Azure PAT)_ · Slack `@mention` bot _(needs Events API
  config)_.
