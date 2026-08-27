#!/usr/bin/env bash
set -euo pipefail

BUCKET="<backup-bucket>"
REGION="ap-northeast-1"
COMPOSE_DIR="/opt/taskdeck"
COMPOSE_OPTS="--env-file .env.production -f docker-compose.production.yml -f docker-compose.production.override.yml"
DATE=$(date -u +%Y%m%d-%H%M%S)
TMP="/tmp/td-pg-$DATE.dump"
KEY="postgres/daily/$DATE.dump"

cd "$COMPOSE_DIR"

# Take a logical dump (custom format -> smaller, parallel-restore-ready)
docker compose $COMPOSE_OPTS exec -T postgres \
  pg_dump -U taskdeck -F c taskdeck > "$TMP"

# Upload
aws s3 cp "$TMP" "s3://$BUCKET/$KEY" --region "$REGION"

# Clean local
rm -f "$TMP"

# Log result for journalctl
echo "backup ok: s3://$BUCKET/$KEY"
