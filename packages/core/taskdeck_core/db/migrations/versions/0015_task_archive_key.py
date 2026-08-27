"""task.archive_key + archived_at — S3 cold archive (P6.3.7)

When workspace_gc tar.gz's a 30-day-old workspace to S3 instead of just
rm -rf'ing it, sandbox-host POSTs the resulting key back to core via
/api/v1/tasks/{id}/archive. This lets the next "Open sandbox" click
pull the archive back instead of erroring "workspace pruned".

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("archive_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "archived_at")
    op.drop_column("tasks", "archive_key")
