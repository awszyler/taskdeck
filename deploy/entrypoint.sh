#!/usr/bin/env bash
set -euo pipefail

# Run database migrations before starting the server.
# alembic.ini lives in packages/core/ and uses Settings() to pick up DATABASE_URL.
echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn..."
exec uvicorn taskdeck_core.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1
