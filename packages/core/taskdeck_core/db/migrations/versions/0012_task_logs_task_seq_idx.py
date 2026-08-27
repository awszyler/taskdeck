"""task_logs(task_id, seq DESC) index for fast tail reads

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-21

Adds a composite index on task_logs(task_id, seq DESC) to accelerate
tail-read queries like `ORDER BY seq DESC LIMIT N WHERE task_id=?`.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "task_logs_task_seq_idx",
        "task_logs",
        ["task_id", sa.text("seq DESC")],
    )


def downgrade() -> None:
    op.drop_index("task_logs_task_seq_idx", table_name="task_logs")
