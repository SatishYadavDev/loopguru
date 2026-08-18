# Installation & Setup — LoopGuru

> **Living document.** Whenever a new dependency, external download, or run command is
> introduced, add it here so setup stays complete and reproducible (PRD §20).

## 0. Prerequisites

- **Docker + Docker Compose** (recommended path). Install Docker Desktop (macOS/Windows)
  or Docker Engine + `docker compose` plugin (Linux).
- **Python 3.12+** (only needed for the no-Docker local dev path).
- An **OpenAI API key** (or an Azure OpenAI endpoint — see §4).
- Admin access to your Slack workspace to install the bot app.

## 1. Get the code & configure env

```bash
cd loopguru
cp .env.example .env
```

Edit `.env` and set at minimum:

- `OPENAI_API_KEY` — your key.
- `API_KEY` — a long random secret; callers send it as the `X-API-Key` header.
- `SLACK_BOT_TOKEN` — from step 2 (starts with `xoxb-`).
- `SLACK_CHANNELS` — JSON list of **channel IDs** to index (step 3), e.g. `["C0123ABCD"]`.

> Never commit `.env` — it is git-ignored. Only `.env.example` (placeholders) is tracked.

## 2. Create the Slack app (from the shipped manifest)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick your workspace, then paste the contents of [`slack_app_manifest.yaml`](slack_app_manifest.yaml).
3. Create the app, then **Install to Workspace** and authorize.
4. Copy the **Bot User OAuth Token** (`xoxb-...`) into `.env` as `SLACK_BOT_TOKEN`.

**Scopes** the manifest requests (all read-only):
`channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`,
`files:read`. (`channels:join` is optional — only if `SLACK_AUTO_JOIN=true`.)

> If you change scopes later you MUST **reinstall the app** for them to take effect.
> DM scopes are intentionally excluded.

### Invite the bot to each channel

The bot can only read channels it is a **member** of:

- Public channel: `/invite @loopguru` in the channel (or enable
  `SLACK_AUTO_JOIN=true` for auto-join of public channels).
- Private channel: you must invite it manually (`/invite @loopguru`).

Channels the bot isn't in show as `not_accessible` in `GET /admin/sync-status`.

## 3. Find channel IDs

- In the Slack desktop/web app, open the channel → click the channel name → the **channel
  ID** (`C…`) is shown at the bottom of the details popover; or copy the channel link —
  the ID is the `/archives/C0123ABCD` segment.
- Put the IDs (not names — names change) in `SLACK_CHANNELS`. Optionally set
  `SLACK_CHANNEL_PRIORITY` to control backfill order.

## 4. OpenAI vs Azure OpenAI

- **OpenAI (default):** leave `OPENAI_BASE_URL` empty. Set `EMBEDDING_MODEL`
  (`text-embedding-3-small`) and `CHAT_MODEL` (e.g. `gpt-4o-mini`).
- **Azure OpenAI:** set `OPENAI_BASE_URL` to your Azure endpoint and use your deployment
  names for the models. Everything else is identical.

## 5. Run with Docker (recommended)

```bash
docker compose up --build
```

Startup order is handled automatically: `postgres`/`qdrant`/`rabbitmq` become healthy →
`migrate` runs `alembic upgrade head` once → `api`, `worker`, `beat` start.

- API + Swagger UI: <http://localhost:8899/docs>
- Health: <http://localhost:8899/health>, readiness: <http://localhost:8899/health/ready>

> Host ports (mapped in `docker-compose.yml`, chosen to avoid clashes):
> API `8899`→8000, Qdrant `6399`→6333, Postgres `5544`→5432. Change them there if needed.

Data persists in named volumes `pgdata`, `qdrantdata`, `rabbitmqdata` across restarts.

## 6. First backfill & first query

Trigger a backfill immediately (otherwise the daily sweep at 02:00 UTC bootstraps it):

```bash
curl -X POST http://localhost:8899/admin/backfill \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{}'
```

Watch progress: `GET /admin/sync-status` (with the `X-API-Key` header). Then:

```bash
curl -X POST http://localhost:8899/query \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"question": "How do I set up X?"}'
```

## 6b. Use the CLI (optional, nicer than curl)

Stateless CLI over the same API (`app/cli`). From a host venv (`pip install -r
requirements.txt`) or inside the `api` container:

```bash
export LOOPGURU_API_URL=http://localhost:8899        # inside container: http://localhost:8000
export LOOPGURU_API_KEY="$API_KEY"

python -m app.cli query "how do I set up the UAT?"
python -m app.cli ask                              # interactive (each question independent)
python -m app.cli ingest --file steps.md --title "UAT setup"   # index a local doc, no Slack
python -m app.cli status
```

Test the how-to capability without Slack: `ingest` a local steps doc, then `query` it.

## 6c. Inspecting the data (verify where things went)

- **Qdrant (embeddings)** — browser dashboard: <http://localhost:6399/dashboard>
  (collection `knowledge_text-embedding-3-small_v1`). Or:
  `curl -s http://localhost:6399/collections/knowledge_text-embedding-3-small_v1`.
- **Postgres (registry/audit)** — connect a GUI to `localhost:5544` (user `loopguru`, pass
  `loopguru`, db `loopguru`), or:
  `docker compose exec postgres psql -U loopguru -d loopguru`, then `\dt` and
  `SELECT * FROM documents;`, `... files;`, `... ingestion_runs;`, `... query_audit;`.

## 7. Local dev without Docker (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up postgres qdrant rabbitmq        # infra only
set -a; source .env; set +a                        # load env (adjust hosts to localhost)
alembic upgrade head
python -m app.api
celery -A app.workers.celery_app:celery_app worker -Q knowledge --loglevel=INFO
celery -A app.workers.celery_app:celery_app beat --loglevel=INFO
```

> When running the app on the host, point `DATABASE_URL`, `QDRANT_URL`, and
> `CELERY_BROKER_URL` at `localhost` instead of the compose service names.

## 8. Backup / restore

- **Postgres:** `docker compose exec postgres pg_dump -U loopguru loopguru > backup.sql`
  (restore with `psql`). Holds sync checkpoints, document registry, audit — needed for
  dedup/purge tracking.
- **Qdrant:** use the snapshot API (`POST /collections/<name>/snapshots`) and copy the
  snapshot out of the `qdrantdata` volume. Losing Qdrant forces a full (paid) re-embed.
- Restore Postgres and Qdrant to consistent points together.

## 9. Secret rotation

Rotate `SLACK_BOT_TOKEN` (Slack app → OAuth → reinstall/rotate), `OPENAI_API_KEY`, and
`API_KEY` periodically and after any exposure. Update `.env` and restart the app services.

---

## Dependency & download log

Append here as things are added.

- **Python deps:** see `requirements.txt` (fastapi, uvicorn, pydantic, sqlalchemy,
  psycopg2-binary, alembic, celery, qdrant-client, openai, tiktoken, slack_sdk, httpx,
  pypdf, python-docx, **PyMuPDF** (scanned-PDF rasterization for vision), tenacity).
- **Container images:** `postgres:16`, `qdrant/qdrant:v1.12.4`, `rabbitmq:3.13-management`,
  `python:3.12-slim` (app image).
- **External downloads:** the Slack app manifest (`slack_app_manifest.yaml`) is used to
  create the Slack app; no other downloads.

## Known limitations to keep in mind

- **Images & scanned/image PDFs ARE now OCR'd** via the vision model (`ENABLE_VISION=true`,
  `VISION_MODEL`). Set `ENABLE_VISION=false` to skip images (saves vision-API cost).
- **External/hosted files** (Google Drive/Box links that aren't uploaded to Slack) are
  skipped-and-logged.
- **Deleted messages** are purged on the weekly reconcile (not instantly).
