"""sandboxes table — runtime info for per-task sandboxes (P6.3)

PK is task_id: a task has at most one running sandbox at a time. We
UPDATE the same row on subsequent provisions; history lives in
task_events / task_logs as usual, not here.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "sandboxes",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        # provisioning | running | stopping | stopped | error
        sa.Column("status", sa.String(length=16), nullable=False),
        # docker container id (full hex). Null while provisioning.
        sa.Column("container_id", sa.String(length=64), nullable=True),
        # The host-side TCP port the container is bound to. Null until
        # provisioning succeeds.
        sa.Column("host_port", sa.Integer(), nullable=True),
        # static | node | python (image_key from detection).
        sa.Column("runtime", sa.String(length=16), nullable=True),
        sa.Column("base_path", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "stopped_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "sandboxes_status_idx",
        "sandboxes",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("sandboxes_status_idx", table_name="sandboxes")
    op.drop_table("sandboxes")
