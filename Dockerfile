# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv
RUN pip install --no-cache-dir uv

# Copy workspace root and all package manifests first (layer cache)
COPY pyproject.toml uv.lock ./
COPY packages/proto/pyproject.toml  packages/proto/pyproject.toml
COPY packages/core/pyproject.toml   packages/core/pyproject.toml
COPY packages/runner/pyproject.toml packages/runner/pyproject.toml

# Copy source
COPY packages/proto/  packages/proto/
COPY packages/core/   packages/core/
COPY packages/runner/ packages/runner/

# Install into an explicit venv so we can COPY just the venv to the final stage.
# --no-editable ensures hatchling bakes the packages into site-packages rather
# than writing a .pth pointing at /build/... (which won't exist in stage 2).
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python \
        --no-cache \
        --no-editable \
        packages/proto packages/core

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

# Runtime deps only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source (needed for alembic migration discovery)
COPY packages/proto/  /app/packages/proto/
COPY packages/core/   /app/packages/core/
COPY scripts/         /app/scripts/

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app/packages/core

ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
