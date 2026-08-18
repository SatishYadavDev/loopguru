# LoopGuru — Design Spec / PRD

**Date:** 2026-07-23
**Status:** Hardened design (pre-implementation) — v2, after adversarial review
**Owner:** Satish Yadav

> This document is intended to be **self-sufficient**: an engineer or agent should be
> able to build the entire v1 from it without further clarification, except for the
> items explicitly listed in §4 (Open Decisions), which need an owner sign-off.

---

## 1. Summary

An **internal-only** agent that ingests data from organization sources, keeps it fresh on
a schedule, and answers natural-language questions grounded strictly in that internal
data — returning an answer plus the sources it used.

Primary use case is **procedural / how-to Q&A** (e.g. "How do I set up X?"). If a teammate
posted a PDF/Markdown/text file describing the steps on a channel, the agent finds it and
answers by following those steps, citing the source.

**v1 ingests Slack only.** The system is built around a **source-agnostic connector
interface** so later versions add **GitHub** (commits/history) and **Azure** (tickets/bugs)
without changing the retrieval or answering core. First run = full backfill; thereafter a
**daily** incremental sync (Celery Beat + RabbitMQ).

---

## 2. Goals

- Answer internal how-to / knowledge questions grounded in org data, with citations.
- Ingest Slack (allowlisted channels) including threads and file attachments
  (PDF/Markdown/text/docx), full backfill first, then daily incremental.
- Keep answers **correct and current** — never serve deleted content, and prefer the most
  recent guidance when sources conflict.
- Extensible: adding a source = implementing one connector interface, no core changes.
- Simple to run and reason about; each module has one clear responsibility.

---

## 3. Non-Goals (v1)

- No LangGraph orchestration — a lightweight RAG pipeline is sufficient.
- No real-time Slack ingestion (no Events API / webhooks). Daily sync only.
- No perfect reconciliation of *edited* messages older than the overlap window; a weekly
  full re-backfill bounds this drift (§11). **Deleted messages ARE purged** (§11.4) —
  this is no longer "ignored".
- No multi-tenancy — single organization, assumed single Slack workspace (§4).
- No per-user / per-channel authorization on answers in v1 (single static API key). The
  channel allowlist MUST therefore contain only content acceptable for all key holders
  (§4, §18).
- No search-time reranker model (mitigated in §14); no ingestion of Slack Canvases /
  Google-Drive-linked files (§4, §9.9). **(Update 2026-08-05: image & scanned-PDF OCR via
  a vision model is now implemented — see §9.9 and §27.)**
- No automated unit/integration test suite — deferred, revisit later. A small **manual
  calibration/eval gold set** (§23) is still recommended because retrieval tuning is
  otherwise blind.
- GitHub / Azure connectors are designed-for but not implemented (§26).

---

## 4. Decisions — owner-confirmed (2026-07-23)

All five previously-open decisions are now **confirmed by the owner**; the design proceeds
on these:

1. **LLM data governance — CONFIRMED: OpenAI is acceptable** for both embeddings and chat.
   No special data-residency constraint; `OPENAI_BASE_URL` stays configurable so Azure
   OpenAI *can* be swapped later, but is not required.
2. **Access model — CONFIRMED: single static API key** stays; holder can query everything
   indexed. Per-key/per-channel scoping remains out of v1. The channel allowlist should
   still contain only broadly-shareable channels.
3. **Slack rate-limit tier — CONFIRMED: accept slow ingestion.** If the strict 2025 tier
   applies, backfill just runs slowly/incrementally over time — acceptable. The
   resumable-backfill design (§11.1) already handles this. `SLACK_CHANNEL_PRIORITY` exists
   so the owner can specify channel ordering **at implementation time** (e.g. "this whole
   channel first, then that one").
4. **File content location — CONFIRMED: Slack-hosted uploads only.** Real content is files
   **uploaded directly to Slack** — primarily Markdown, sometimes PDF. The bot **does**
   download these via `url_private_download` + bot token (MD read directly, PDF via pypdf).
   External **hosted links** (Google Drive/Box that are only linked, not uploaded) are not
   needed and are skipped-and-logged.
5. **Enterprise Grid — CONFIRMED: single workspace** (one Slack, one login/URL; not Grid,
   no Slack Connect shared channels).

---

## 5. Key Decisions

| Area | Decision |
|---|---|
| Project | New standalone project `loopguru`; org `agentic-ai` used only as a reference for patterns |
| Language / framework | Python 3.12+, FastAPI, lightweight RAG pipeline (no LangGraph) |
| Env & deps | Plain `python -m venv` + `pip install -r requirements.txt` (no `uv`); services via Docker |
| Setup docs | Living `INSTALLATION.md`, updated whenever a dependency/download is introduced |
| Vector store | Qdrant, one versioned collection |
| Embeddings + LLM | OpenAI `text-embedding-3-small` (1536-d) + a GPT chat model; endpoint configurable (Azure-swappable) |
| v1 source | Slack — allowlisted channels (by **channel ID**) + threads + Slack-hosted file text |
| Retrieval | RAG: ingest → embed → vector search (over-fetch → dedup → recency) → cited answer |
| Sync | Resumable full backfill + daily incremental + weekly full re-backfill; Celery Beat |
| Interface | REST API (`POST /query`) |
| API auth | Static API key, timing-safe compare |
| Answers | Answer + source links (manually constructed Slack permalinks) |
| Background jobs | Celery + RabbitMQ (Celery Beat for cron) |

---

## 6. Architecture

Three logical planes over shared Postgres + Qdrant.

```
                 ┌─────────────── Query plane ───────────────┐
   user ─POST /query (X-API-Key)─▶ FastAPI
             → validate + rate-limit → embed question (OpenAI, raw)
             → Qdrant search over-fetch (+ filters) → dedup → recency re-order
             → neighbor/context expansion → build context (token-budgeted)
             → OpenAI chat (grounded, cite sources, answer in question's language)
             → faithfulness check → { answer, citations[] } ─▶ user  (+ audit log)

                 ┌────────────── Ingestion plane ─────────────┐
   Celery Beat: daily incremental sweep + weekly full re-backfill
   (manual full-backfill / purge via admin endpoints)
        Celery worker (per-scope, locked):
          connector.fetch(scope, since) → normalize (text-clean, blocks fallback)
          → chunk (structure-aware) → [skip if content_hash unchanged]
          → embed (OpenAI, batched, token-capped)
          → delete-by-doc_id then upsert to Qdrant (UUIDv5 point ids)
          → write registry/state to Postgres (AFTER Qdrant confirms)

                 ┌──────────────── Storage plane ─────────────┐
   Postgres: sync_state, documents, files, file_messages, threads,
             failed_items, identity_cache, ingestion_runs, query_audit
   Qdrant:   collection `knowledge_<model>_v<n>`; payload carries all metadata + indexes
```

**Infra services (docker-compose):** `migrate` (one-shot), `api`, `worker` (celery),
`beat` (celery-beat), `rabbitmq`, `qdrant`, `postgres`.

---

## 7. Source-agnostic connector interface (extensibility core)

Every source implements the same interface; everything downstream (chunk → embed → store →
retrieve → answer) is source-independent.

```python
class SourceConnector(Protocol):
    source_name: str  # "slack", later "github", "azure"

    def list_scopes(self) -> list[Scope]:
        """Units to sync (Slack channels, later GitHub repos / Azure boards).
        MUST validate access and mark unreachable scopes with a clear status."""

    def fetch(self, scope: Scope, cursor: SyncCursor | None) -> Iterable[RawItem]:
        """Yield raw items newer than the cursor, handling API pagination internally.
        The cursor is opaque + persistable so an interrupted run can resume."""

    def to_documents(self, item: RawItem) -> list[Document]:
        """Normalize a raw item into common Documents."""
```

v1 ships `SlackConnector`. GitHub/Azure later implement the same Protocol — no changes to
`ingestion`, `rag`, or `api`.

---

## 8. Common Document schema & ID scheme

```
Document {
  doc_id: str            # STABLE across re-syncs, independent of thread growth
  source: str            # "slack"
  scope_id: str          # channel ID (stable, never channel name)
  title: str | None      # e.g. file name, or None for a message
  text: str              # normalized, human-readable content
  author: str | None     # resolved display name
  created_ts: str        # opaque Slack ts string (never parsed to float)
  created_epoch: int     # integer seconds, for Qdrant range filters + recency
  permalink: str | None  # manually constructed link back to the source item
  thread_id: str | None  # thread_ts of parent, if part of a thread
  attachments: [FileRef] # each extracted file becomes its own Document
  metadata: dict         # source-specific extras (subtype, edited_ts, file_id, …)
}
```

**doc_id construction (Slack):**
- Message → `slack:{channel_id}:{ts}`
- File → `slack:file:{file_id}` (embedded **once**, deduped across messages/channels;
  each linking message recorded in `file_messages`). A file's payload carries
  `scope_ids: [..]` = the set of channels it was shared in (not a single `scope_id`), so
  scope-filtered retrieval and the deletion sweep both work for multi-channel files
  (§11.4, §14.1).

**Granularity:** documents are **per-message** (and per-file), keyed by `ts`/`file_id` —
NOT per-thread. Appending a thread reply adds a new document; it never reshuffles or
re-embeds existing messages. Thread linkage lives in `thread_id`.

**Qdrant point id:** `UUIDv5(namespace, f"{doc_id}:{chunk_idx}")` — Qdrant requires
uint64/UUID, so the deterministic string key is hashed to a stable UUIDv5. Re-ingest of a
`doc_id` overwrites its chunks; **stale chunks are removed by delete-by-filter on `doc_id`
before upsert** (prevents orphans when chunk count shrinks).

---

## 9. Slack connector (v1)

### 9.1 App, auth, scopes
- **Bot token** (`SLACK_BOT_TOKEN`). Sufficient for read-only history/replies/file
  download. (User-token-only features like `search.messages` are an explicit non-goal.)
- **Minimum scopes** (pinned up front to avoid reinstall cycles):
  `channels:history, channels:read, groups:history, groups:read, users:read, files:read`
  (+ `channels:join` if auto-join of public channels is enabled). **DM scopes are
  intentionally excluded.**
- Ship a **Slack app manifest (YAML)** in the repo declaring these scopes so install is
  one click and reproducible. Tracked in `INSTALLATION.md`.
- Workspace subdomain is **auto-derived once** via `auth.test`/`team.info` and cached for
  permalink construction; `SLACK_WORKSPACE_SUBDOMAIN` in `.env` is an optional override
  (auto-derived value is authoritative when the var is unset).

### 9.2 Scopes = channels, by ID
- `SLACK_CHANNELS` is a JSON list of **channel IDs** (`C0123ABCD`); IDs are stable, names
  are not. `scope_id = channel_id`. `INSTALLATION.md` documents how to find a channel ID.
- `list_scopes()` validates each: bot membership + readability. Public channels may be
  auto-joined (`conversations.join`) if `channels:join` is granted; private channels must
  be **manually invited** — otherwise the scope is marked `not_accessible` and surfaced in
  `/admin/sync-status` (never a silent empty success).
- `not_in_channel` / `channel_not_found` / `is_archived` → distinct **non-retryable** scope
  status, surfaced, not treated as a generic transient error.

### 9.3 Pagination
- All Slack list/history/replies calls are **cursor-paginated** via
  `response_metadata.next_cursor` with a configurable `limit`. The connector loops cursors
  internally and yields items; during backfill the cursor is **persisted** per scope for
  resume.

### 9.4 Rate limits
- Honor HTTP 429 `Retry-After` + bounded exponential backoff. Assume the strict 2025
  non-Marketplace tier (§4.3): ~1 req/min, ~15 objects/page. Consequences baked into
  design: backfill is long + resumable; permalinks are **constructed, not fetched**
  (§9.7); replies fetched **only for threads with changes** (§11.3); identity resolved via
  **bulk `users.list`**, not per-message `users.info` (§9.6).

### 9.5 Text normalization (required stage)
Raw Slack text is not plain text. Normalizer must:
- Resolve `<@U123>` → display name, `<#C456|name>` → `#name`, `<https://url|label>` →
  `label (url)`, `<!here>`/`<!channel>` → `@here`/`@channel`.
- Strip mrkdwn (`*bold*`, `_italic_`, backticks kept for code), unescape entities, map
  `:emoji:` to text or drop.
- **Fallback to walking `blocks` (rich_text)** when top-level `text` is empty (modern
  messages often carry content only in blocks) — otherwise these messages index as empty
  and silently vanish from retrieval.

### 9.6 Identity resolution
- Bulk `users.list` once per run → cached `ID → display_name` map persisted in
  `identity_cache` (refreshed daily). Fallback `users.info` for unknowns. Deactivated/
  deleted users still resolve to their stored `real_name`. Same pattern for channel names.

### 9.7 Permalinks (constructed)
- Construct: `https://<workspace>.slack.com/archives/<channel_id>/p<ts_without_dot>`;
  thread replies append `?thread_ts=<parent_ts>&cid=<channel_id>`. No per-message
  `chat.getPermalink` call (rate-limit cost).

### 9.8 Message filtering
- Drop no-content system subtypes: `channel_join`, `channel_leave`, `channel_topic`,
  `channel_purpose`, `channel_name`, `channel_archive`, `tombstone`, etc.
- `bot_message` kept only if from an allowlisted useful bot (config), else dropped.
- Skip the thread parent duplicate: when reading `conversations.replies`, ignore the item
  where `ts == thread_ts` (already captured via history), or rely on identical `doc_id`.
- `ts` is always treated as an **opaque string**; parsed to datetime only for
  `created_ts`/`created_epoch` display/sort, never round-tripped through float.

### 9.9 Files / attachments
- Detect files on a message; download via **`url_private_download`** with
  `Authorization: Bearer <bot-token>` (NOT the HTML `permalink`/`url_private`). Guard
  against HTML error responses masquerading as file bytes.
- Supported MIME allowlist: `application/pdf` (pypdf), `text/markdown`, `text/plain`,
  `.docx` (python-docx), and **images** `image/png|jpeg|gif|webp` (vision, §27).
  Everything else skipped-and-logged.
- **`is_external` / hosted files (Google Drive, Box)** cannot be byte-downloaded →
  skipped-and-logged (§4.4). **Slack Canvases are now ingested** (§27, `url_private` HTML →
  text via existing `files:read`); legacy `.doc` remains a gap.
- `MAX_FILE_BYTES` cap + download timeout; oversize skipped-and-logged. Images and
  text-less/scanned PDFs are routed to the **vision model** (§27); only if that also yields
  nothing is the file counted as a failure (not `extracted_ok`).
- Each file → one Document (`slack:file:{file_id}`), deduped across messages; each linking
  message recorded in `file_messages`; the file's `scope_ids` payload lists all channels
  it was shared in.

---

## 10. Ingestion pipeline

1. **Normalize** raw item → `Document`(s) via the connector (§9.5 text-clean + blocks
   fallback; identity resolved; permalink constructed).
2. **Change detection:** compute `content_hash` (over normalized text + `edited_ts` for
   messages, file bytes hash for files). If a `documents` row exists with the same hash →
   **skip embedding** (idempotent no-op on the daily overlap re-fetch).
3. **Chunk (structure-aware):**
   - Files: split on Markdown/PDF headings; **keep numbered/bulleted lists intact** (never
     cut mid-procedure); ~500–800 tokens/chunk, **~10–15% overlap**.
   - Messages: per-message; very short messages may be grouped only **within the same
     thread or a <5–10 min same-author gap** (never fuse unrelated conversations).
   - **Prepend context** to each chunk before embedding: `title` + (for file docs) the
     sharing message text, so a query's phrasing matches even when the file body doesn't
     contain it. Payload stores the **raw chunk text** and the **prepended-context text**
     as separate fields (both retained for display/debugging; only the concatenation is
     embedded).
4. **Embed:** OpenAI `text-embedding-3-small`, **batched** (≤ configurable batch size,
   e.g. 128 inputs / per-request token budget). Enforce a hard **8191-token per-input cap**
   (split/truncate oversized chunks). Count embedding tokens into `ingestion_runs`.
5. **Store (ordering is an invariant):** `delete-by-filter(doc_id)` in Qdrant → **upsert**
   new chunks (UUIDv5 ids) → **only after Qdrant confirms**, write `content_hash` +
   `indexed_at` to Postgres. (Never record the hash before the vector exists, or a future
   run will skip a doc that isn't actually indexed.)
6. **Poison isolation:** every item wrapped in try/catch. A failing item is recorded in
   `failed_items` with `attempts`; permanently-unsupported → quarantined (skip+log);
   transiently-failed → retried next run **without blocking the scope checkpoint**.

---

## 11. Sync strategy & correctness

### 11.1 Backfill (first run + weekly)
- Resumable: persists Slack `next_cursor` per scope in `sync_state`. A crash mid-backfill
  resumes from the cursor (no full re-embed; deterministic ids + content_hash skip already
  ingested).
- Explicit lifecycle per scope: `backfill_status ∈ {pending, in_progress, completed}` +
  `backfill_completed_at`. Incremental sync for a scope starts **only after** its backfill
  is `completed`.
- **Bootstrap trigger:** the daily Beat sweep, for each allowlisted scope, enqueues a
  `full_backfill` task if `backfill_status = pending` (or `in_progress` with a stale lock),
  and a `sync_scope` (incremental) task if `completed`. So a newly-added channel begins
  backfilling on the next daily tick without waiting for the weekly run or a manual call
  (manual `/admin/backfill` still available to trigger immediately).
- On clean completion: set `last_checkpoint` = max `ts` seen (monotonic forward only).
- **Backfill ordering:** scopes are backfilled in `SLACK_CHANNEL_PRIORITY` order (then the
  rest), so the most important channels become answerable first under the slow rate budget.
- A **weekly full re-backfill** (Beat) bounds edit-drift and reconciles deletions (§11.4).

### 11.2 Checkpoint semantics
- `last_checkpoint` = the raw Slack **`ts` string** (opaque, UTC) of the newest top-level
  message fully processed for that scope. Never a Postgres server clock. Writes are
  **monotonic** — a run may only advance it, never move it backward (guards races, §11.5).

### 11.3 Incremental (daily) — top-level + thread replies
Two passes per scope, because `conversations.history` returns only **top-level** messages:
- **(a) New messages:** `conversations.history` with `oldest = last_checkpoint −
  SYNC_OVERLAP` (overlap default **2 days**) to catch just-posted content and near-window
  edits.
- **(b) Late thread replies.** The `threads` table stores each known thread's `thread_ts`,
  `reply_count`, `latest_reply`. Detection has two tiers, because a reply to an *old* parent
  does NOT re-surface that parent in the recent history window (§9.4 rate budget forbids
  re-polling every thread daily):
  - **In-window threads:** any thread whose parent appears in pass (a)'s window — if its
    `reply_count`/`latest_reply` differs from stored state, call `conversations.replies`
    and ingest new replies.
  - **Old-thread rotation:** each daily run re-polls `conversations.replies` for a
    **bounded batch** of known threads — the `REPLY_POLL_BATCH` threads with the most recent
    `latest_reply` (most-active first) that haven't been polled most recently — staying
    within a per-run API-call budget. Threads beyond the batch are reconciled at the weekly
    full re-backfill.
  This is an explicit, bounded trade-off: fresh/active old threads get near-daily late-reply
  pickup; cold old threads reconcile weekly. (A reply to a genuinely dormant weeks-old thread
  may lag up to a week — documented limitation, acceptable per §3.)

### 11.4 Deletions & edits
- **Deletions ARE handled:** the weekly full re-backfill runs in **sweep mode** — collect
  all `doc_id`s seen for a scope, then **delete from Qdrant + Postgres any message `doc_id`
  not seen** (message deleted in Slack → removed from the index; no more citing dead
  content). **File docs are swept by linkage, not scope:** a file is deleted only when it
  has **no remaining rows in `file_messages`** across any channel (drop only the deleted
  channel's link); its payload `scope_ids` is updated to the surviving channels. This
  prevents purging a file still shared in another channel.
- **Manual purge:** `POST /admin/purge` removes by `doc_id` / channel (for secret/PII
  incidents, §18) from both stores immediately.
- **Edits within overlap** (or re-fetched threads) are caught by `content_hash` → re-index.
  Edits to old messages outside the window are corrected at the weekly re-backfill
  (documented limitation, §3).

### 11.5 Locking & concurrency
- Per-`(source, scope_id)` **Postgres advisory lock** (or `locked_by/locked_at` columns).
  A scope already locked is skipped (prevents daily-sync vs manual-backfill vs Celery-retry
  from clobbering the same scope). Combined with monotonic checkpoints, concurrent runs
  cannot corrupt state.

---

## 12. Data model (Postgres)

- `sync_state(source, scope_id, backfill_status, backfill_completed_at, next_cursor,
  last_checkpoint, locked_by, locked_at, updated_at)` — PK `(source, scope_id)`.
- `documents(doc_id PK, source, scope_id, title, permalink, created_epoch, content_hash,
  chunk_count, indexed_at)` — dedup, change detection, orphan cleanup.
- `files(file_id PK, source, name, mime, bytes, extracted_ok, doc_id)` — file dedup.
- `file_messages(file_id, message_doc_id, scope_id, PK(file_id, message_doc_id))` — links a
  file to every message/channel that shared it; drives multi-channel retrieval + sweep
  (§11.4, §14.3).
- `threads(source, scope_id, thread_ts, reply_count, latest_reply, last_polled_at,
  PK(source, thread_ts))` — drives late-reply detection + old-thread rotation (§11.3);
  `thread_ts` is globally unique so `scope_id` is informational.
- `identity_cache(source, entity_id, kind, display_name, updated_at,
  PK(source, entity_id))` — user/channel display names.
- `failed_items(id, source, scope_id, ref, reason, retryable, attempts, last_error,
  created_at)` — poison quarantine + retry.
- `ingestion_runs(id, source, scope_id, mode, started_at, finished_at, status, items_seen,
  chunks_upserted, chunks_deleted, embed_tokens, errors_json)` — observability + cost.
- `query_audit(id, ts, question, top_doc_ids, used_doc_ids, latency_ms)` — audit (§18);
  avoids storing full answer text verbatim.

Alembic migrations; run by a **one-shot `migrate` service/step** before app containers
start (app/worker/beat never auto-migrate — avoids replica races).

---

## 13. Qdrant schema

- **Collection naming + alias (authoritative):** concrete collections are named
  `knowledge_{EMBEDDING_MODEL}_v{QDRANT_COLLECTION_VERSION}` (e.g.
  `knowledge_text-embedding-3-small_v1`), vector size **1536**, distance **cosine**. A
  stable **read/write alias `knowledge`** points at the current concrete collection; **all
  ingestion upserts and all query searches go through the alias**, never the concrete name.
  Changing `EMBEDDING_MODEL` or bumping `QDRANT_COLLECTION_VERSION` → build the new concrete
  collection, backfill into it, then **atomically move the `knowledge` alias** to it (never
  mix models in one collection). `QDRANT_COLLECTION` env, if set, overrides the alias name.
- **Payload:** `doc_id`, `source`, `scope_id` (message docs) or `scope_ids: [..]` (file
  docs), `title`, `author`, `created_ts`, `created_epoch`, `permalink`, `thread_id`,
  `chunk_idx`, `chunk_text` (raw), `prepend_text`, `content_hash`, `metadata`.
- **Payload indexes:** keyword index on `source`, `scope_id`, `scope_ids`; **integer index
  on `created_epoch`** (so `since`/date range filters and recency actually work — a datetime
  string cannot be range-filtered).

---

## 14. RAG / query flow

1. `POST /query` `{ question, filters?: {source?, scope_id?, since_epoch?}, top_k? }` →
   validate (max length, `top_k` bounds).
2. Embed the question **raw** (no context prepend — asymmetry with doc chunks is
   intentional).
3. Qdrant search **over-fetch** (`RAG_OVERFETCH_K`, e.g. 20) through the `knowledge` alias
   with optional payload filters. A `scope_id` filter matches message docs on `scope_id`
   **and** file docs whose `scope_ids` contains it.
4. **Dedup** the retrieved set: drop exact repeats by `content_hash`/`doc_id`, then drop
   near-twins whose mutual cosine similarity ≥ `RAG_DEDUP_SIM` (default **0.97**), keeping
   the higher-scored/newer one.
5. **Recency & conflict handling:** apply a time-decay factor to each score before selecting
   the final **top_k (default 8)**: `adjusted = cosine × exp(−age_days / RAG_RECENCY_HALFLIFE_DAYS)`
   with `RAG_RECENCY_HALFLIFE_DAYS` default **180** (mild — recency breaks near-ties, doesn't
   dominate relevance). The prompt is also instructed to **prefer the most recent source on
   conflict and note superseded guidance** (the top how-to hazard).
6. **Context expansion (small-to-big):** for each selected chunk, expand to neighbors within
   the same `doc_id` (`chunk_idx ±1`); if the doc is small (≤ `RAG_EXPAND_MAX_CHUNKS`, default
   6) include the whole doc; for a message that is part of a thread, include the thread parent
   for context. Never exceed the doc/thread boundary. This keeps multi-step procedures whole.
7. **Token-budgeted context assembly:** enforce a context token budget; selection order =
   highest score, then newest; truncate rather than blindly concatenating.
8. **Relevance floor:** if the best score is below `RAG_RELEVANCE_FLOOR` → return an
   explicit "no relevant internal data found" (never hallucinate). Two-band policy: hard
   floor → refuse; soft band → answer but hedge. `text-embedding-3-small` relevant matches
   typically score ~0.30–0.55; **initial floor = 0.35**, to be **calibrated on the gold
   set** (§23), value + reasoning recorded in config.
9. **Answer:** OpenAI chat, grounded system prompt: answer only from context; present
   procedures as ordered steps; **answer in the question's language** (English/Hindi/
   Hinglish); cite sources; say so if context is insufficient.
10. **Citation contract (concrete):**
    - Each context passage is labeled in the prompt with a small integer tag `[i]` (1-indexed
      over the assembled context), and the builder keeps an in-memory map `i → {doc_id,
      chunk_idx, permalink, title, scope_id, chunk_text}`.
    - The chat call uses **structured/JSON output** returning
      `{ answer: str, cited: int[] }`, where `cited` lists the `[i]` tags the answer actually
      used. (`i` is used, not raw ids, so the model never has to echo long identifiers.)
    - The program resolves each `i` back through the map to build
      `citations: [{ source, title, permalink, scope_id, snippet }]`. **`snippet` is a
      program-selected verbatim substring** of that passage's `chunk_text` (never
      model-generated). `used_chunks = len(cited)`.
11. **Faithfulness check:** discard any `cited` tag not present in the context map; if the
    answer is non-empty but `cited` is empty, downgrade to the "insufficient context"
    response. This guarantees every returned citation maps to a real retrieved passage.
12. **Multilingual note:** English-query → romanized-Hinglish-doc matching is weaker; this
    is a known limitation, measured via the gold set (§23).
13. No reranker in v1; mitigated by over-fetch + dedup + recency + neighbor expansion.
    Trigger for adding one (v2): many near-duplicate chunks or long multi-step docs
    degrading precision.

---

## 15. API surface

- `POST /query` — main Q&A (API-key required).
- `POST /admin/backfill` — trigger full backfill (optional source/scope).
- `POST /admin/purge` — delete indexed content by `doc_id` / channel (secret/PII incidents).
- `GET /admin/sync-status` — per-scope `backfill_status`, `last_checkpoint`, last runs,
  `not_accessible` scopes, days-since-last-successful-run, days-since-last-full-backfill.
- `GET /health` — liveness/readiness (no key required); readiness checks downstream deps.

**Cross-cutting API rules:**
- **Auth:** `X-API-Key` header, compared with `hmac.compare_digest` (timing-safe); missing/
  invalid → 401; key never logged.
- **Limits:** max question length + request body size cap; simple per-key/IP **rate limit**;
  `top_k` bounded.
- **Timeouts:** OpenAI connect/read timeouts + bounded retries; upstream request timeout so
  a hung LLM call can't pin a worker; documented uvicorn worker/concurrency sizing.
- **CORS:** disabled by default (server-to-server). **Error envelope:** single JSON shape
  `{error, detail}`; never leak stack traces or the key.
- **Audit:** every query → `query_audit` (question, returned/used `doc_id`s, latency).

---

## 16. Scheduling & workers

- Celery app, RabbitMQ broker, **durable queues** (survive restart).
- **Beat:** daily sweep enqueues, per scope, either `full_backfill` (if `pending`) or
  `sync_scope` incremental (if `completed`) — see §11.1 bootstrap; plus a weekly
  `full_backfill` for all `completed` scopes (sweep/reconcile deletions).
- **Backpressure/limits:** bounded Celery concurrency + prefetch; `MAX_FILE_BYTES` guards
  worker memory against a giant PDF; docker memory/CPU limits per service.
- `full_backfill` / `sync_scope` tasks are idempotent and respect per-scope locks (§11.5).

---

## 17. Error handling & reliability

- Slack 429 → `Retry-After` + backoff; OpenAI/Qdrant errors → bounded retries + backoff.
- Per-scope isolation: one failing channel doesn't stop others; failure recorded, checkpoint
  not advanced for that scope; poison items quarantined so one bad item can't freeze a scope
  (§10.6).
- **Failure visibility / alerting:** alert (e.g. Slack webhook) on (a) any
  `ingestion_runs.status = failed`, and (b) **no successful run for a scope in > N days** —
  the latter catches a dead Beat/worker/RabbitMQ, which produces *zero* rows, not a failed
  row. Stale-but-confident answers are the worst failure mode for a knowledge tool.
- Query path: empty/low-relevance retrieval → explicit "not found", never fabrication.

---

## 18. Security & data privacy

- **LLM boundary (§4.1):** confirm zero-retention/no-train OpenAI DPA or Azure OpenAI;
  `OPENAI_BASE_URL` configurable.
- **Access (§4.2):** single key reads everything indexed — allowlist restricted to
  broadly-shareable channels; risk accepted in writing or per-key scoping added.
- **Secret/PII amplification:** Slack channels leak secrets/PII; these get embedded +
  stored as plaintext and could be resurfaced. Mitigations: deletions purged via weekly
  sweep (§11.4); **`POST /admin/purge`** for immediate removal; **recommended** optional
  secret-scanning/redaction pass at ingest.
- **Secrets management:** `.gitignore` MUST cover `.env`; repo ships only `.env.example`
  with placeholders; document rotation for each secret; prod secrets via docker secrets /
  host env / secrets manager — never baked into an image.
- **Logging:** structured JSON with a per-request/per-task **correlation id**; secrets and
  (ideally) full message text excluded from logs.
- **Cost controls:** OpenAI org-level monthly hard budget + alert; per-run/per-query token
  logging; a **dry-run backfill** that reports estimated token volume before committing.

---

## 19. Configuration (`.env`)

**LLM/vector:** `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `EMBEDDING_MODEL`, `CHAT_MODEL`,
`VISION_MODEL` (gpt-4o-mini), `ENABLE_VISION` (true), `MAX_IMAGE_PAGES` (8),
`OPENAI_TIMEOUT_S`, `QDRANT_URL`, `QDRANT_COLLECTION` (alias name; default `knowledge`),
`QDRANT_COLLECTION_VERSION` (int, default 1).
**Infra:** `DATABASE_URL`, `CELERY_BROKER_URL` (RabbitMQ).
**Slack:** `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` (@mention bot), `SLACK_BOT_USER_ID`
(optional), `SLACK_WORKSPACE_SUBDOMAIN` (optional override; auto-derived),
`SLACK_CHANNELS` (JSON list of channel IDs), `SLACK_CHANNEL_PRIORITY` (JSON, optional —
ordered channel IDs for backfill ordering; unlisted channels follow), `USEFUL_BOT_IDS`
(JSON, optional).
**RAG:** `RAG_TOP_K` (8), `RAG_OVERFETCH_K` (20), `RAG_RELEVANCE_FLOOR` (0.35),
`RAG_DEDUP_SIM` (0.97), `RAG_RECENCY_HALFLIFE_DAYS` (180), `RAG_EXPAND_MAX_CHUNKS` (24),
`RAG_CONTEXT_TOKEN_BUDGET` (12000).
**Ingestion:** `SYNC_OVERLAP_DAYS` (2), `REPLY_POLL_BATCH`, `EMBED_BATCH_SIZE`,
`MAX_FILE_BYTES`, `MAX_QUESTION_CHARS`.
**Azure (optional, ticket action):** `AZURE_DEVOPS_ORG`, `AZURE_DEVOPS_PROJECT`,
`AZURE_DEVOPS_PAT`, `AZURE_DEVOPS_WORKITEM_TYPE` (default Task), `AZURE_DEVOPS_ASSIGNED_TO`
(required by some project rules).
**API/ops:** `API_KEY`, `API_RATE_LIMIT` (e.g. `60/minute`), `MAX_REQUEST_BYTES`,
`ALERT_WEBHOOK_URL`, `STALE_SCOPE_ALERT_DAYS`.

---

## 20. Setup & installation (no uv)

- **Env:** `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- **Deps:** pinned in `requirements.txt`. No `uv`, no pyproject workspace.
- **`INSTALLATION.md` is a living document**, pre-populated and updated as anything new is
  added. It MUST include: Docker/Compose install; creating the Slack app **from the shipped
  manifest**; the exact **scope list**; how to **invite the bot** to each allowlisted
  channel; how to **find channel IDs**; OpenAI/Azure key acquisition + `OPENAI_BASE_URL`;
  the note that **images & scanned PDFs are OCR'd via the vision model** (`ENABLE_VISION`)
  and external/Drive-linked files are skipped; the **one-shot migrate** command;
  backup/restore steps (§21).

---

## 21. Deployment (docker-compose)

- Services: `migrate` (one-shot, runs `alembic upgrade head`, others `depends_on` it),
  `api`, `worker`, `beat`, `rabbitmq`, `qdrant`, `postgres`.
- **Named volumes** for `postgres`, `qdrant` storage, and `rabbitmq` (else restart =
  data loss + costly re-embed). Durable RabbitMQ queues.
- **Healthchecks** for postgres/rabbitmq/qdrant/api; `depends_on: condition:
  service_healthy`; `restart: unless-stopped`; per-service memory/CPU limits.
- **Backup/restore:** periodic `pg_dump` + Qdrant snapshot API; documented, tested restore
  path; Postgres registry and Qdrant restored to consistent points.

---

## 22. Observability

- Structured JSON logs + correlation ids (§18).
- `GET /admin/sync-status` exposes backfill state, checkpoints, recent runs, inaccessible
  scopes, staleness ages.
- `ingestion_runs` records counts, deletions, embed tokens (cost visibility).
- Alerting on failed runs and stale scopes (§17).

---

## 23. Calibration & eval (lightweight, recommended)

Not a test suite (tests deferred, §3) — a **small manual gold set** of 20–40 real
procedural questions with expected source permalinks + acceptable answers, including a few
**Hinglish** questions. A one-shot script reports retrieval **hit@k** and lets a human grade
answers. This is what makes `RAG_RELEVANCE_FLOOR`, `top_k`, and chunking **tunable instead
of guessed**, and detects regressions. ~1 day of effort; unblocks §14.8 and §10.3.

---

## 24. Project structure

```
loopguru/
  app/
    api/            # FastAPI app, routers, deps, API-key auth, rate limit, error envelope
    core/           # config (pydantic-settings), structured logging, correlation ids
    connectors/
      base.py       # SourceConnector Protocol, Scope, RawItem, SyncCursor
      slack/        # SlackConnector: client, pagination, rate limit, normalizer,
                    #   identity cache, file download+extract, permalink builder, manifest
    ingestion/      # normalize, chunk (structure-aware), embed (batched), pipeline,
                    #   change-detection, poison quarantine
    rag/            # retriever (over-fetch/dedup/recency/expansion), prompt builder,
                    #   answerer, citation + faithfulness check
    storage/
      qdrant/       # versioned collection, payload indexes, delete-then-upsert
      db/           # SQLAlchemy models, repositories, alembic migrations
    workers/        # celery app, tasks (sync_scope, full_backfill, purge), beat schedule
    schemas/        # pydantic models (Document, query req/resp, config)
  slack_app_manifest.yaml
  docker-compose.yml
  Dockerfile
  requirements.txt
  INSTALLATION.md
  .env.example
  .gitignore        # includes .env
  README.md
```

---

## 25. Milestones

1. Scaffold: config, structured logging, `requirements.txt`, `INSTALLATION.md`,
   `.gitignore`, Dockerfile, docker-compose (with volumes/healthchecks/migrate), `/health`.
2. Storage: Postgres models + Alembic migrations (all tables §12); Qdrant versioned
   collection + payload indexes; delete-then-upsert wrapper.
3. Connector interface + SlackConnector: manifest, scopes/membership validation, cursor
   pagination, rate-limit handling, text normalizer + blocks fallback, identity cache,
   permalink builder, file download + extraction, subtype filtering.
4. Ingestion pipeline: change-detection, structure-aware chunking + context prepend,
   batched/token-capped embedding, delete-then-upsert with correct write ordering, poison
   quarantine.
5. Sync: resumable backfill (cursor + lifecycle), daily incremental (history + thread-reply
   detection), weekly reconcile/purge sweep, per-scope locking, monotonic checkpoints.
6. Celery worker + Beat (daily + weekly) + `sync_scope`/`full_backfill`/`purge` tasks;
   concurrency/backpressure limits.
7. RAG query service: over-fetch → dedup → recency → expansion → token-budgeted context →
   grounded answer + verbatim citations + faithfulness check; API-key auth, rate limit,
   timeouts, error envelope, query audit.
8. Admin endpoints (backfill, purge, sync-status), alerting hooks, dry-run backfill
   estimate, README + finalize INSTALLATION.md.
9. (Recommended) Calibration gold set + one-shot eval script; calibrate relevance floor.

---

## 26. Future sources (designed-for, not in v1)

- **GitHub:** `GithubConnector` — repos as scopes; commits/PR/issue history as RawItems;
  `to_documents` maps commit messages/diff summaries and issue threads into Documents.
- **Azure:** `AzureConnector` — boards as scopes; tickets/bugs as RawItems.
- Neither requires changes to ingestion, RAG, storage, or the API — only a new connector +
  its config.

---

## 27. Changelog (post-v1 changes)

Newest first. Keep this in sync with `FEATURES.md`'s Progress log.

### 2026-08-18 — Agentic Slack bot (thread memory + tools) ✅ done
- `app/slackbot/agent.py`: an OpenAI tool-calling loop replaces first-word routing. The
  bot passes the full thread transcript as memory and exposes tools `answer_question`,
  `summarize_thread`, `summarize_channel`, `create_ticket`, `update_ticket`. Enables
  follow-ups and iterative ticket edits in a thread. New table `thread_tickets`
  (migration `0002`) maps a thread → its Azure work item; `AzureBoardsClient.update_work_item`
  (PATCH) added. Handler (`handle_mention`) builds the transcript and calls `run_agent`.

### 2026-08-18 — Slack @mention bot ✅ (code) done
- `POST /slack/events` (`app/api/routers/slack_events.py`): HMAC signature verification
  with `SLACK_SIGNING_SECRET`, url_verification handshake, fast-ACK; `app_mention` events
  are handed to Celery `handle_mention`, which routes to query/summarize/ticket and replies
  in-thread via `chat.postMessage`. Questions are scoped to the mention's channel. New
  config: `SLACK_SIGNING_SECRET`, `SLACK_BOT_USER_ID`; new scopes `chat:write`,
  `app_mentions:read`. This supersedes the "REST API only" interface note (§5).
- Pending (owner): set the Event Subscription Request URL to `<public-url>/slack/events`
  and subscribe `app_mention`. Real-time message ingestion via Events API is still future.

### 2026-08-10 — Slack Canvas ingestion ✅ done
- Canvas files (`application/vnd.slack-docs` / `quip`) carry content as HTML at
  `url_private`; `files.py` detects them (`FileRef.is_canvas`), downloads with the existing
  `files:read` scope (no reinstall), and strips HTML → text (`_canvas_text`). Indexed like
  any file. Note: unscoped retrieval can be diluted by other channels' volume — scope the
  query or purge stale channels.

### 2026-08-10 — Azure Boards ticket (action) ✅ done
- `app/connectors/azure/boards.py` (`AzureBoardsClient.create_work_item`, REST
  `POST .../_apis/wit/workitems/${type}`, Basic auth with PAT). `app/rag/ticket_drafter.py`
  drafts `{title, description_html, acceptance criteria, tags}` from a problem statement,
  enriched with retrieved Slack context. Endpoints `POST /ticket/draft` + `/ticket/create`;
  CLI `/ticket` (shell) / `loopguru ticket` with a confirm step. Config:
  `AZURE_DEVOPS_ORG/PROJECT/PAT`, `AZURE_DEVOPS_WORKITEM_TYPE` (default Task).
- Draft verified end-to-end; the actual create is run by the user (outward write to a
  shared board). This realizes the Azure connector anticipated in §26.
- CLI: the AI **always drafts** the ticket; each flag
  (`--title/--description/--type/--assignee/--tags`, any order) **overrides just that
  field**, unset fields keep the AI value / configured default. `System.AssignedTo` set
  from `AZURE_DEVOPS_ASSIGNED_TO` or `--assignee` (some project processes require it —
  `TF401320` otherwise).

### 2026-08-07 — CLI overhaul ✅ done
- `./loopguru` launcher (runs the CLI inside the api container) opens an interactive slash
  shell: plain text = a question; `/help`, `/ask`, `/summarize channel|thread`,
  `/backfill` (`/fill`), `/ingest`, `/status`, `/purge`, `/exit`. Clean ANSI formatting,
  auto-disabled when not a TTY. One-shot subcommands retained for scripting.

### 2026-08-07 — Summaries & digests ✅ done
- `app/rag/summarizer.py`: `summarize_thread()` (live `conversations.replies`) and
  `summarize_channel()` (reads Qdrant chunks via `store.fetch_recent()`, no extra Slack
  calls). Endpoints `POST /summarize/thread` and `POST /summarize/channel`.
- Celery Beat `channel_digest(days)` scheduled daily (1) + weekly (7); delivered via the
  alert webhook. Posting digests back into Slack is pending `chat:write`.

### 2026-08-07 — Hybrid retrieval (BM25 + vector) ✅ done
- `store.keyword_search()` (Qdrant full-text `MatchText` on a `chunk_text` TEXT index) +
  vector search are unioned and fused via **RRF** using an in-process **BM25** rank
  (`retriever._bm25_scores`, `_rrf_fuse`). Cosine is still used for the relevance-floor
  gate; RRF only reorders. New config: `RAG_HYBRID`, `RAG_RRF_K`.
- This supersedes the "no reranker in v1" note for keyword/acronym matching.

### 2026-08-05 — Image & diagram OCR (vision) ✅ done
- New `app/core/vision.py`: `describe_image()` transcribes/describes an image via the
  OpenAI **vision** model; `pdf_to_text_via_vision()` rasterizes scanned/text-less PDFs
  with **PyMuPDF** and runs vision per page (capped by `MAX_IMAGE_PAGES`).
- `connectors/slack/files.py` now routes `image/*` files to vision, and falls back to
  vision when a PDF has no text layer. New config: `ENABLE_VISION`, `VISION_MODEL`,
  `MAX_IMAGE_PAGES`. New dep: `PyMuPDF`.
- Verified **end-to-end**: a "Yield to Maturity" image posted in Slack was OCR'd, indexed,
  and answered with a citation.
- Bug fix (same day): shared/forwarded messages expose files under `attachments[].files`,
  not `msg.files`; the connector now collects both (deduped) so forwarded files index too.

### 2026-08-05 — Retrieval tuning ✅ done
- `RAG_EXPAND_MAX_CHUNKS` 6 → **24**, `RAG_CONTEXT_TOKEN_BUDGET` 6000 → **12000**, and the
  per-passage cap no longer divides the budget by top_k — so long procedural docs return
  complete step-by-step answers.

### Pending (need user-provided inputs)
- **Azure Boards ticket action** — CLI drafts + files a work item on Azure DevOps
  (needs `AZURE_DEVOPS_ORG` / `PROJECT` / `PAT`).
- **Slack `@mention` bot + real-time sync** — needs Slack Events API app config + a public
  webhook URL (and `chat:write` scope re-added).

### Planned (no external dependency)
- Thread summaries + scheduled daily/weekly channel digests.
- Hybrid retrieval: combine **BM25 keyword** scoring with vector cosine (better exact-term
  and acronym matches, e.g. "YTM").
