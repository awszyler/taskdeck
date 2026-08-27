from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so runner works regardless of cwd.
_ENV_FILE = Path(__file__).parent.parent.parent.parent / ".env"


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    core_ws_url: str = Field(alias="TD_CORE_WS_URL")
    core_http_url: str = Field(default="http://localhost:8000", alias="TD_CORE_HTTP_URL")
    token: str = Field(alias="TD_RUNNER_TOKEN")
    runner_name: str = Field(default="local-runner", alias="TD_RUNNER_NAME")
    max_parallel: int = Field(default=2, alias="TD_MAX_PARALLEL")
    work_dir: Path = Field(default=Path("/tmp/taskdeck-work"), alias="TD_WORK_DIR")
    claude_code_bin: str | None = Field(default=None, alias="TD_CLAUDE_CODE_BIN")
    kiro_cli_bin: str | None = Field(default=None, alias="TD_KIRO_CLI_BIN")
    openclaw_bin: str | None = Field(default=None, alias="TD_OPENCLAW_BIN")
    openclaw_agent_name: str = Field(default="main", alias="TD_OPENCLAW_AGENT_NAME")
    hermes_bin: str | None = Field(default=None, alias="TD_HERMES_BIN")
    codex_bin: str | None = Field(default=None, alias="TD_CODEX_BIN")

    metrics_enabled: bool = Field(default=True, alias="TD_RUNNER_METRICS_ENABLED")
    metrics_port: int = Field(default=9100, alias="TD_RUNNER_METRICS_PORT")

    # GitHub Personal Access Token. When set, runner injects it into
    # every AI-agent task as GH_TOKEN/GITHUB_TOKEN, lets agents create
    # repos, push, manage deploy keys, open PRs, etc. via the gh CLI
    # or git over HTTPS. Leave unset to keep agents read-only on
    # GitHub. The token has the runner's full permissions inside the
    # agent process — pair with the future sandbox work, not before
    # multi-tenant.
    github_token: str | None = Field(default=None, alias="TD_GITHUB_TOKEN")

    agentcore_enabled: bool = Field(default=False, alias="TD_AGENTCORE_ENABLED")
    agentcore_region: str = Field(default="ap-northeast-1", alias="TD_AGENTCORE_REGION")
    agentcore_agent_ids: list[str] = Field(default_factory=list, alias="TD_AGENTCORE_AGENT_IDS")
    agentcore_agent_alias_id: str = Field(default="TSTALIASID", alias="TD_AGENTCORE_AGENT_ALIAS_ID")
    # JSON object mapping AgentCore agent_id -> human description.
    # Surfaced to the intent parser so it can route by task fit.
    # Example: '{"AB12CD34": "AWS deployment expert (S3 + CloudFront)"}'
    agentcore_agent_descriptions: str | None = Field(
        default=None, alias="TD_AGENTCORE_AGENT_DESCRIPTIONS"
    )

    @field_validator("agentcore_agent_ids", mode="before")
    @classmethod
    def _split_agent_ids(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v  # type: ignore[return-value]
