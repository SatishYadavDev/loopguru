# LoopGuru — Architecture

Detailed diagrams of how the system actually works: components, the Celery ingestion
pipeline, and the query flow. (For the feature list see [`FEATURES.md`](FEATURES.md).)

---

## 1. System components

How the pieces fit together and who talks to whom.

```mermaid
flowchart TB
    subgraph Clients
      U["User / curl"]
      CLI["CLI — python -m app.cli"]
    end

    subgraph APIsvc["FastAPI service (stateless)"]
      Q["POST /query"]
      ADM["/admin — backfill · purge · ingest · sync-status"]
      H["/health · /health/ready"]
    end

    subgraph Celery["Background — Celery"]
      BEAT["Beat (scheduler)"]
      WORK["Worker — SyncEngine"]
    end

    MQ["RabbitMQ — task queue"]

    subgraph Data["Storage (Docker volumes)"]
      QD[("Qdrant — vector embeddings")]
      PG[("Postgres — state · registry · audit")]
    end

    OAI["OpenAI API — embeddings + chat"]
    SLACK["Slack Web API"]

    U --> Q
    CLI --> Q
    CLI --> ADM
    Q --> OAI
    Q --> QD
    Q --> PG
    ADM -->|enqueue| MQ
    BEAT -->|enqueue| MQ
    MQ --> WORK
    WORK --> SLACK
    WORK --> OAI
    WORK --> QD
    WORK --> PG
```

---

## 2. Ingestion pipeline (Celery)

What happens when a channel is (re)synced — triggered by the scheduler or a manual
`/admin/backfill`. Idempotent and resumable.

```mermaid
flowchart TB
    BEAT["Celery Beat<br/>daily sync · weekly reconcile · stale-check"] -->|enqueue per channel| MQ["RabbitMQ"]
    ADMIN["POST /admin/backfill"] -->|enqueue| MQ
    MQ --> TASK["Worker task<br/>full_backfill / sync_scope / sweep_scope"]

    TASK --> LOCK{"Per-channel lock free?"}
    LOCK -->|no| SKIP["skip — another run is active"]
    LOCK -->|yes| FETCH["SlackConnector<br/>fetch history pages + thread replies<br/>(cursor-paginated, rate-limit aware)"]
    FETCH --> NORM["Normalize text (resolve @mentions, links)<br/>Extract file text: PDF · MD · TXT · DOCX"]
    NORM --> HASH{"content_hash changed?"}
    HASH -->|no| SKIP2["skip embed — already indexed"]
    HASH -->|yes| CHUNK["Chunk + prepend title/context"]
    CHUNK --> EMBED["OpenAI embeddings (batched, token-capped)"]
    EMBED --> UPSERT["Qdrant: delete-by-doc_id → upsert<br/>(no orphan/duplicate chunks)"]
    UPSERT --> STATE["Postgres: documents · files · threads<br/>ingestion_runs · advance checkpoint"]
    STATE --> DONE["Checkpoint moves forward only (monotonic)"]
```

**Key guarantees:** per-channel lock prevents overlapping runs · `content_hash` skips
unchanged content (saves cost) · delete-then-upsert avoids duplicates · the weekly
reconcile removes messages deleted in Slack.

---

## 3. Query flow (stateless)

Every `/query` is independent — no conversation memory. The agent answers **only** from
retrieved internal context, or refuses.

```mermaid
sequenceDiagram
    participant U as User / CLI
    participant API as FastAPI /query
    participant OAI as OpenAI
    participant QD as Qdrant
    participant PG as Postgres

    U->>API: question + X-API-Key
    API->>API: auth (timing-safe) + rate-limit + validate
    API->>OAI: embed question
    OAI-->>API: query vector
    API->>QD: vector search (over-fetch + optional filters)
    QD-->>API: candidate chunks (+ vectors)
    API->>API: dedup → recency re-rank → top-k → expand neighbors → token-budget
    alt best score < relevance floor
        API-->>U: "no relevant internal information" (no hallucination)
    else enough context
        API->>OAI: grounded prompt (numbered context + question)
        OAI-->>API: JSON {answer, cited:[ids]}
        API->>API: faithfulness check → resolve citations (permalinks)
        API->>PG: audit (question, used docs, latency)
        API-->>U: answer + citations
    end
```
