"""tasks.idempotency_key + parsing status

Revision ID: 0010
Revises: 0009

Adds tasks.idempotency_key UUID + a partial unique index on
(workspace_id, idempotency_key) for client-driven submit dedup.

The new TaskStatus.PARSING value is purely application-level — the status
column is already TEXT with no DB-level constraint, so no schema change is
needed for the enum extension.
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN idempotency_key UUID")
    # Partial unique index: NULL keys (legacy + structured-form tasks) don't
    # participate, so they can coexist freely. Only keyed pairs are dedup'd.
    # CONCURRENTLY is omitted because alembic wraps in a transaction by default;
    # the table is small in our deployment so a brief lock is acceptable.
    # Use a regular index for now — switch to CONCURRENTLY in a follow-up if
    # the tasks table grows large enough to matter.
    op.execute(
        "CREATE UNIQUE INDEX tasks_workspace_idempotency_uniq "
        "ON tasks (workspace_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tasks_workspace_idempotency_uniq")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS idempotency_key")
