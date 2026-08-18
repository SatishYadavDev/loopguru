"""Central configuration (pydantic-settings). All env vars from PRD §19 live here.

Import the singleton `settings` everywhere; never read os.environ directly.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _json_list(value: object) -> list[str]:
    """Parse a JSON list from an env string; tolerate a bare/empty value."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
            return [str(parsed)]
        except json.JSONDecodeError:
            # allow comma-separated fallback
            return [v.strip() for v in value.split(",") if v.strip()]
    return []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM / vector ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1536, alias="EMBEDDING_DIM")
    chat_model: str = Field(default="gpt-4o-mini", alias="CHAT_MODEL")
    vision_model: str = Field(default="gpt-4o-mini", alias="VISION_MODEL")
    enable_vision: bool = Field(default=True, alias="ENABLE_VISION")
    max_image_pages: int = Field(default=8, alias="MAX_IMAGE_PAGES")
    openai_timeout_s: float = Field(default=60, alias="OPENAI_TIMEOUT_S")
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_collection: str = Field(default="knowledge", alias="QDRANT_COLLECTION")
    qdrant_collection_version: int = Field(default=1, alias="QDRANT_COLLECTION_VERSION")

    # --- infra ---
    database_url: str = Field(
        default="postgresql+psycopg2://loopguru:loopguru@localhost:5432/loopguru",
        alias="DATABASE_URL",
    )
    celery_broker_url: str = Field(
        default="amqp://guest:guest@localhost:5672//", alias="CELERY_BROKER_URL"
    )

    # --- slack ---
    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")  # xapp-… for Socket Mode
    slack_bot_user_id: str = Field(default="", alias="SLACK_BOT_USER_ID")
    slack_workspace_subdomain: str = Field(default="", alias="SLACK_WORKSPACE_SUBDOMAIN")
    slack_channels: list[str] = Field(default_factory=list, alias="SLACK_CHANNELS")
    slack_channel_priority: list[str] = Field(
        default_factory=list, alias="SLACK_CHANNEL_PRIORITY"
    )
    useful_bot_ids: list[str] = Field(default_factory=list, alias="USEFUL_BOT_IDS")
    slack_auto_join: bool = Field(default=False, alias="SLACK_AUTO_JOIN")

    # --- rag ---
    rag_top_k: int = Field(default=8, alias="RAG_TOP_K")
    rag_overfetch_k: int = Field(default=20, alias="RAG_OVERFETCH_K")
    rag_relevance_floor: float = Field(default=0.35, alias="RAG_RELEVANCE_FLOOR")
    rag_dedup_sim: float = Field(default=0.97, alias="RAG_DEDUP_SIM")
    rag_recency_halflife_days: float = Field(default=180, alias="RAG_RECENCY_HALFLIFE_DAYS")
    rag_expand_max_chunks: int = Field(default=24, alias="RAG_EXPAND_MAX_CHUNKS")
    rag_context_token_budget: int = Field(default=12000, alias="RAG_CONTEXT_TOKEN_BUDGET")
    rag_hybrid: bool = Field(default=True, alias="RAG_HYBRID")  # BM25 + vector fusion
    rag_rrf_k: int = Field(default=60, alias="RAG_RRF_K")  # reciprocal-rank-fusion constant

    # --- ingestion ---
    sync_overlap_days: float = Field(default=2, alias="SYNC_OVERLAP_DAYS")
    reply_poll_batch: int = Field(default=50, alias="REPLY_POLL_BATCH")
    embed_batch_size: int = Field(default=128, alias="EMBED_BATCH_SIZE")
    max_file_bytes: int = Field(default=26_214_400, alias="MAX_FILE_BYTES")  # 25 MiB
    max_question_chars: int = Field(default=4000, alias="MAX_QUESTION_CHARS")
    chunk_target_tokens: int = Field(default=650, alias="CHUNK_TARGET_TOKENS")
    chunk_overlap_ratio: float = Field(default=0.12, alias="CHUNK_OVERLAP_RATIO")
    chunk_max_tokens: int = Field(default=8000, alias="CHUNK_MAX_TOKENS")

    # --- azure devops (boards) ---
    azure_devops_org: str = Field(default="", alias="AZURE_DEVOPS_ORG")
    azure_devops_project: str = Field(default="", alias="AZURE_DEVOPS_PROJECT")
    azure_devops_pat: str = Field(default="", alias="AZURE_DEVOPS_PAT")
    azure_devops_workitem_type: str = Field(default="Task", alias="AZURE_DEVOPS_WORKITEM_TYPE")
    # Some projects require System.AssignedTo — set to a valid user email/display name.
    azure_devops_assigned_to: str = Field(default="", alias="AZURE_DEVOPS_ASSIGNED_TO")

    # --- api / ops ---
    api_key: str = Field(default="", alias="API_KEY")
    api_rate_limit: str = Field(default="60/minute", alias="API_RATE_LIMIT")
    max_request_bytes: int = Field(default=65536, alias="MAX_REQUEST_BYTES")
    alert_webhook_url: str = Field(default="", alias="ALERT_WEBHOOK_URL")
    stale_scope_alert_days: int = Field(default=3, alias="STALE_SCOPE_ALERT_DAYS")

    # --- misc ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # -- validators: coerce JSON-list env strings --
    @field_validator(
        "slack_channels", "slack_channel_priority", "useful_bot_ids", mode="before"
    )
    @classmethod
    def _parse_lists(cls, v: object) -> list[str]:
        return _json_list(v)

    # -- derived helpers --
    @property
    def concrete_collection(self) -> str:
        """Concrete Qdrant collection name (alias points here). See PRD §13."""
        model = self.embedding_model.replace("/", "-")
        return f"{self.qdrant_collection}_{model}_v{self.qdrant_collection_version}"

    @property
    def collection_alias(self) -> str:
        return self.qdrant_collection

    def rate_limit_parts(self) -> tuple[int, int]:
        """Parse `API_RATE_LIMIT` like '60/minute' -> (count, window_seconds)."""
        try:
            count_str, unit = self.api_rate_limit.split("/")
            count = int(count_str)
        except ValueError:
            return 60, 60
        unit = unit.strip().lower()
        window = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(unit, 60)
        return count, window


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
