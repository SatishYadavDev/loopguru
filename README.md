# LoopGuru

Internal-only RAG agent that ingests organization data (Slack in v1), keeps it fresh on a
schedule, and answers natural-language **how-to / procedural** questions grounded strictly
in that data — returning an answer plus the sources it used.

Full design: [`docs/superpowers/specs/2026-07-23-loopguru-design.md`](docs/superpowers/specs/2026-07-23-loopguru-design.md).
Setup steps: [`INSTALLATION.md`](INSTALLATION.md).

## What it does

- **Ingests Slack** (allowlisted channels, by channel ID) — messages, threads, and
  Slack-hosted file attachments (PDF / Markdown / text / docx).
- **First run = full backfill**, then a **daily incremental** sync + **weekly reconcile**
  (removes deleted content). Runs on Celery + RabbitMQ.
- **Answers via REST** (`POST /query`): semantic search over Qdrant + OpenAI, with
  citations back to the Slack permalinks used.
- **Source-agnostic**: adding GitHub / Azure later = one new connector, no core changes.

## Architecture

```
Query:   POST /query → embed → Qdrant (over-fetch → dedup → recency → expand)
                     → OpenAI chat (grounded, cited) → {answer, citations[]}
Ingest:  Celery Beat → per-scope worker: Slack fetch → normalize → chunk → embed
                     → delete-then-upsert (Qdrant) → state (Postgres)
Storage: Postgres (state/registry/audit) + Qdrant (vectors, versioned collection + alias)
```

Services: `migrate` (one-shot), `api`, `worker`, `beat`, `postgres`, `qdrant`, `rabbitmq`.

## Quick start (Docker)

```bash
cp .env.example .env          # then fill in OPENAI_API_KEY, SLACK_BOT_TOKEN, API_KEY, SLACK_CHANNELS
docker compose up --build     # migrate runs first, then api/worker/beat
# API docs at http://localhost:8000/docs
```

Trigger a first backfill (or wait for the daily sweep):

```bash
curl -X POST http://localhost:8000/admin/backfill \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{}'
```

Ask a question:

```bash
curl -X POST http://localhost:8000/query \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"question": "How do I set up the payable fund system?"}'
```

## Local dev (no Docker for the app)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# bring up infra only:
docker compose up postgres qdrant rabbitmq
export $(grep -v '^#' .env | xargs)   # load env
alembic upgrade head                  # migrate
python -m app.api                     # API
celery -A app.workers.celery_app:celery_app worker -Q knowledge --loglevel=INFO   # worker
celery -A app.workers.celery_app:celery_app beat --loglevel=INFO                  # scheduler
```

## Project layout

```
app/
  api/         FastAPI app, routers, auth, rate limit, error envelope
  core/        config, logging, ids, tokens, openai client
  connectors/  base Protocol + slack/ (client, normalizer, identity, permalink, files)
  ingestion/   chunker, embedder, pipeline
  rag/         retriever, prompt, answerer, service
  storage/     db/ (models, repositories, alembic) + qdrant/ (store)
  workers/     celery app, sync engine, tasks, alerts
```

## Notes / limitations (v1)

- Deleted Slack messages are purged on the weekly reconcile; edits to old messages
  (outside the overlap window) reconcile weekly too.
- No OCR (scanned PDFs are skipped-and-logged); external/Drive-linked files are skipped.
- Single static API key = read access to everything indexed — keep the allowlist to
  broadly-shareable channels.
- No automated test suite yet (deferred); a small calibration gold set is recommended to
  tune `RAG_RELEVANCE_FLOOR`.
