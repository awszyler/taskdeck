from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so alembic (run from packages/core/) and
# uvicorn (run from repo root) both find it regardless of cwd.
_ENV_FILE = Path(__file__).parent.parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    runner_bearer_token: str = Field(validation_alias="TD_RUNNER_BEARER_TOKEN")
    cors_origins_raw: str = Field(default="", validation_alias="CORS_ORIGINS", exclude=True)
    artifact_dir: Path = Field(
        default=Path("/tmp/taskdeck-artifacts"), validation_alias="TD_ARTIFACT_DIR"
    )

    litellm_base_url: str | None = Field(default=None, alias="TD_LITELLM_BASE_URL")
    litellm_api_key: str | None = Field(default=None, alias="TD_LITELLM_API_KEY")
    intent_parser_model: str = Field(
        default="anthropic/claude-sonnet-4-6", alias="TD_INTENT_PARSER_MODEL"
    )
    intent_parser_timeout_seconds: float = Field(
        default=60.0, alias="TD_INTENT_PARSER_TIMEOUT_SECONDS"
    )
    bedrock_intent_enabled: bool = Field(default=False, alias="TD_BEDROCK_INTENT_ENABLED")
    bedrock_intent_model: str = Field(
        default="us.anthropic.claude-opus-4-7", alias="TD_BEDROCK_INTENT_MODEL"
    )
    bedrock_intent_region: str = Field(
        default="us-east-1", alias="TD_BEDROCK_INTENT_REGION"
    )
    stt_model: str = Field(default="whisper-1", alias="TD_STT_MODEL")
    stt_timeout_seconds: float = Field(default=90.0, alias="TD_STT_TIMEOUT_SECONDS")
    # "litellm" (default for back-compat) | "aws-transcribe"
    stt_backend: Literal["litellm", "aws-transcribe"] = Field(
        default="litellm", alias="TD_STT_BACKEND",
    )
    # AWS Transcribe needs an S3 bucket for the audio job source.
    # Reuses the attachment bucket by default — same region, same
    # IAM permissions already granted.
    stt_s3_bucket: str = Field(default="", alias="TD_STT_S3_BUCKET")
    stt_region: str = Field(default="ap-northeast-1", alias="TD_STT_REGION")
    # Comma-separated AWS Transcribe LanguageOptions candidates for
    # IdentifyMultipleLanguages. Without this AWS picks defaults that
    # don't reliably include zh-CN — so users speaking Mandarin get
    # garbage English transcripts.
    stt_language_options: str = Field(
        default="zh-CN,en-US,ja-JP", alias="TD_STT_LANGUAGE_OPTIONS",
    )

    wecom_enabled: bool = Field(default=False, alias="TD_WECOM_ENABLED")
    wecom_corp_id: str = Field(default="", alias="TD_WECOM_CORP_ID")
    wecom_secret: str = Field(default="", alias="TD_WECOM_SECRET")
    wecom_agent_id: str = Field(default="", alias="TD_WECOM_AGENT_ID")
    wecom_token: str = Field(default="", alias="TD_WECOM_TOKEN")
    wecom_aes_key: str = Field(default="", alias="TD_WECOM_AES_KEY")
    wecom_default_workspace_slug: str = Field(default="default", alias="TD_WECOM_DEFAULT_WORKSPACE_SLUG")
    public_base_url: str = Field(default="http://localhost:8000", alias="TD_PUBLIC_BASE_URL")

    cost_tracking_enabled: bool = Field(default=True, alias="TD_COST_TRACKING_ENABLED")
    pricing_file: str | None = Field(default=None, alias="TD_PRICING_FILE")

    @property
    def pricing_file_path(self) -> Path | None:
        return Path(self.pricing_file) if self.pricing_file else None

    auth_mode: Literal["disabled", "cognito"] = Field(default="disabled", alias="TD_AUTH_MODE")
    auth_allow_signup: bool = Field(default=False, alias="TD_AUTH_ALLOW_SIGNUP")
    cognito_user_pool_id: str = Field(default="", alias="TD_COGNITO_USER_POOL_ID")
    cognito_client_id: str = Field(default="", alias="TD_COGNITO_CLIENT_ID")
    cognito_region: str = Field(default="ap-northeast-1", alias="TD_COGNITO_REGION")
    session_encryption_key: str = Field(default="", alias="TD_SESSION_ENCRYPTION_KEY")
    session_cookie_name: str = Field(default="ccpt_session", alias="TD_SESSION_COOKIE_NAME")
    session_cookie_domain: str = Field(default="", alias="TD_SESSION_COOKIE_DOMAIN")

    memory_enabled: bool = Field(default=False, alias="TD_MEMORY_ENABLED")
    memory_embedding_model: str = Field(
        default="text-embedding-3-small", alias="TD_MEMORY_EMBEDDING_MODEL"
    )
    memory_top_k: int = Field(default=4, alias="TD_MEMORY_TOP_K")
    memory_total_cap_bytes: int = Field(default=4096, alias="TD_MEMORY_TOTAL_CAP_BYTES")
    memory_per_chunk_cap_bytes: int = Field(default=1024, alias="TD_MEMORY_PER_CHUNK_CAP_BYTES")
    bedrock_embedding_enabled: bool = Field(default=False, alias="TD_BEDROCK_EMBEDDING_ENABLED")
    bedrock_embedding_model: str = Field(
        default="cohere.embed-multilingual-v3", alias="TD_BEDROCK_EMBEDDING_MODEL"
    )
    bedrock_region: str = Field(default="ap-northeast-1", alias="TD_BEDROCK_REGION")

    metrics_enabled: bool = Field(default=True, alias="TD_METRICS_ENABLED")

    # Sandbox host service (P6.3). Internal docker-compose URL.
    sandbox_host_url: str = Field(
        default="http://sandbox-host:9101", alias="TD_SANDBOX_HOST_URL",
    )
    sandbox_provision_timeout: float = Field(
        default=45.0, alias="TD_SANDBOX_PROVISION_TIMEOUT",
    )
    # Reject task submissions whose `agent` value has no connected runner.
    # Default off so users can queue tasks ahead of bringing a runner online;
    # turn on (true) for a stricter UX that fails fast.
    reject_offline_agents: bool = Field(default=False, alias="TD_REJECT_OFFLINE_AGENTS")
    rate_limit_auth_per_minute: int = Field(default=30, alias="TD_RATE_LIMIT_AUTH_PER_MINUTE")
    # Default 25 MB — symmetry with artifact upload hard cap in the handler.
    request_max_body_bytes: int = Field(
        default=25 * 1024 * 1024, alias="TD_REQUEST_MAX_BODY_BYTES"
    )

    # P7 attachments. S3 bucket/region default to the same as P6.3.7 archive.
    # The attachment endpoint has its own size ceiling (separate from
    # request_max_body_bytes) — see attachments.py.
    attachment_bucket: str = Field(
        default="<data-bucket>",
        alias="TD_ATTACHMENT_BUCKET",
    )
    attachment_region: str = Field(
        default="ap-northeast-1", alias="TD_ATTACHMENT_REGION",
    )
    # Per-file cap on uploads via /api/v1/attachments. Tasks can still
    # pull larger objects via direct S3 if a future use case demands.
    attachment_max_bytes: int = Field(
        default=300 * 1024 * 1024, alias="TD_ATTACHMENT_MAX_BYTES",
    )

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        if self.cors_origins_raw:
            return [part.strip() for part in self.cors_origins_raw.split(",") if part.strip()]
        return []
