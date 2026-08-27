"""task.description + drop DRAFT status (parsing UX rework)

See docs/parsing-ux-rework.md.

Schema:
- add tasks.description VARCHAR(280) NULL — card tooltip / drawer subtitle.
- shrink tasks.title VARCHAR(256) -> VARCHAR(120). Real titles are ≤80;
  this defends against future "long prompt pasted as title" regressions.
  Existing rows all fit (verified: max title length in prod ~80).

Data:
- DRAFT status is removed from the app. Existing draft-state rows are
  almost always abandoned mid-parse experiments — cancel them so the
  (now-removed) Draft column empties cleanly instead of surprise-
  dispatching old rows. Records a task_event for audit.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("description", sa.String(length=280), nullable=True),
    )
    # Defensively clamp any existing over-120 title into description before
    # shrinking the column, so the ALTER TYPE never truncates real content.
    op.execute(
        """
        UPDATE tasks
        SET description = COALESCE(description, LEFT(title, 280))
        WHERE description IS NULL AND length(title) > 120
        """
    )
    op.execute("UPDATE tasks SET title = LEFT(title, 120) WHERE length(title) > 120")
    op.alter_column(
        "tasks",
        "title",
        existing_type=sa.String(length=256),
        type_=sa.String(length=120),
        existing_nullable=False,
    )

    # Retire DRAFT: cancel any rows still parked there, with an audit trail.
    op.execute(
        """
        INSERT INTO task_events (id, task_id, from_status, to_status, actor, reason, created_at)
        SELECT gen_random_uuid(), id, 'draft', 'cancelled', 'system',
               'draft_status_dropped (parsing UX rework)', CURRENT_TIMESTAMP
        FROM tasks WHERE status = 'draft'
        """
    )
    op.execute(
        """
        UPDATE tasks
        SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
        WHERE status = 'draft'
        """
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "title",
        existing_type=sa.String(length=120),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    op.drop_column("tasks", "description")
    # Cancelled-from-draft rows are NOT un-cancelled — that data step is
    # one-way (we can't reliably tell which cancelled rows were ex-drafts
    # apart from the task_events trail, and resurrecting them is unsafe).
