# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir uv

# Workspace root + all package manifests first (layer cache).
COPY pyproject.toml uv.lock ./
COPY packages/proto/pyproject.toml         packages/proto/pyproject.toml
COPY packages/core/pyproject.toml          packages/core/pyproject.toml
COPY packages/runner/pyproject.toml        packages/runner/pyproject.toml
COPY apps/sandbox-host/pyproject.toml      apps/sandbox-host/pyproject.toml

# Sandbox-host depends on taskdeck-proto (workspace member). Both
# pyproject AND source must be present for uv to resolve + install.
COPY packages/proto/                  packages/proto/
COPY apps/sandbox-host/sandbox_host/  apps/sandbox-host/sandbox_host/

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python \
        --no-cache --no-editable \
        packages/proto apps/sandbox-host

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

# docker SDK reaches the host docker daemon via the mounted socket.
# No daemon binary needed inside the container.
COPY --from=builder /opt/venv /opt/venv

# Application source (uvicorn + module path).
COPY apps/sandbox-host/sandbox_host/ /app/sandbox_host/

WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 9101

CMD ["python", "-m", "sandbox_host.main"]
