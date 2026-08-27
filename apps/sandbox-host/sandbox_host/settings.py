from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to repo root, mirroring runner/core convention.
_ENV_FILE = Path(__file__).parent.parent.parent.parent / ".env.sandbox-host"


class SandboxHostSettings(BaseSettings):
    """Sandbox-host configuration. Read from env (TD_SBH_* prefix)
    or from `.env.sandbox-host` at repo root."""
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # HTTP listen
    host: str = Field(default="0.0.0.0", alias="TD_SBH_HOST")
    port: int = Field(default=9101, alias="TD_SBH_PORT")

    # Where task worktrees live on the host filesystem (mounted into
    # sandbox containers as /workspace). Same root as the runner's
    # TD_WORK_DIR by convention.
    work_dir: Path = Field(
        default=Path("/var/taskdeck/work"), alias="TD_SBH_WORK_DIR",
    )

    # Docker runtime to use for sandbox containers. "runsc" = gVisor.
    # Override to "runc" only for local dev where gVisor isn't installed.
    container_runtime: str = Field(
        default="runsc", alias="TD_SBH_CONTAINER_RUNTIME",
    )

    # Per-sandbox resource limits.
    cpu_limit: float = Field(default=1.0, alias="TD_SBH_CPU_LIMIT")
    memory_limit_mb: int = Field(default=1024, alias="TD_SBH_MEMORY_LIMIT_MB")
    pids_limit: int = Field(default=200, alias="TD_SBH_PIDS_LIMIT")
    tmpfs_size_mb: int = Field(default=1024, alias="TD_SBH_TMPFS_SIZE_MB")

    # Idle reclaim: stop container after this many seconds without a
    # proxied HTTP request.
    idle_seconds: int = Field(default=300, alias="TD_SBH_IDLE_SECONDS")

    # GC tick.
    gc_interval_seconds: int = Field(default=60, alias="TD_SBH_GC_INTERVAL")

    # P-H Phase 5: reconciler loop interval. Heals drift between the
    # persisted SQLite registry and actual docker state (orphan
    # containers, vanished containers, zombie networks). 60s by default
    # — slow enough not to spam docker API, fast enough to clean up
    # within a couple minutes of a runtime hiccup.
    reconciler_interval_seconds: int = Field(
        default=60, alias="TD_SBH_RECONCILER_INTERVAL",
    )

    # Maximum concurrent sandboxes. Soft cap — provision call returns 429.
    max_concurrent: int = Field(default=4, alias="TD_SBH_MAX_CONCURRENT")

    # Sandbox image tags. We pre-build these and tag them locally.
    image_static: str = Field(
        default="td-sandbox-static:latest", alias="TD_SBH_IMAGE_STATIC",
    )
    image_node: str = Field(
        default="td-sandbox-node:latest", alias="TD_SBH_IMAGE_NODE",
    )
    image_python: str = Field(
        default="td-sandbox-python:latest", alias="TD_SBH_IMAGE_PYTHON",
    )

    # Network-prefix for per-sandbox docker bridges. Each sandbox gets
    # its own bridge network named td-sandbox-net-<task_id> so
    # sandboxes can't talk to each other or to taskdeck_default.
    network_prefix: str = Field(
        default="td-sandbox-net", alias="TD_SBH_NETWORK_PREFIX",
    )

    # Sandbox-host's own docker container name (so it can attach
    # itself to per-sandbox networks for the reverse proxy). When
    # unset, sandbox-host assumes it's running on the host as a
    # process (uses 127.0.0.1 + published ports, the dev path).
    self_container_name: str = Field(
        default="taskdeck-sandbox-host-1",
        alias="TD_SBH_SELF_CONTAINER_NAME",
    )

    # Container readiness timeout. Containers that don't bind to their
    # advertised port within this many seconds are killed.
    startup_timeout_seconds: int = Field(
        default=30, alias="TD_SBH_STARTUP_TIMEOUT",
    )

    # Workspace retention (P6.3.3). Worktrees older than this are
    # pruned by the LRU GC. Default 30 days.
    workspace_retention_days: int = Field(
        default=30, alias="TD_SBH_WORKSPACE_RETENTION_DAYS",
    )
    # How often the workspace LRU GC runs. Slower than container GC
    # because filesystem mtime changes infrequently.
    workspace_gc_interval_seconds: int = Field(
        default=3600, alias="TD_SBH_WORKSPACE_GC_INTERVAL",
    )

    # P6.3.7 cold archive. When set, workspace_gc tar.gz's expiring
    # workspaces into this S3 bucket and POSTs the resulting key back
    # to core (via core_http_url + core_service_token) so a future
    # "Open sandbox" can restore. Empty bucket = archive disabled,
    # GC reverts to plain rm -rf (the pre-P6.3.7 behavior).
    archive_bucket: str = Field(
        default="", alias="TD_SBH_ARCHIVE_BUCKET",
    )
    archive_region: str = Field(
        default="ap-northeast-1", alias="TD_SBH_ARCHIVE_REGION",
    )
    # Core URL + bearer token used for the archive callback (and only
    # that — sandbox-host doesn't otherwise depend on core).
    core_http_url: str = Field(
        default="http://core:8000", alias="TD_SBH_CORE_HTTP_URL",
    )
    core_service_token: str = Field(
        default="", alias="TD_SBH_CORE_SERVICE_TOKEN",
    )
