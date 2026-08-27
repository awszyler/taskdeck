"""task_attachments — multimodal task inputs (P7)

User-uploaded files (PPT, images, CSV, etc.) attached to a task.
Stored in S3 as content-addressable objects keyed by sha256 so the
same file uploaded twice doesn't double-charge storage.

task_id is nullable: the upload endpoint creates the row before the
task itself is committed, then /tasks links them. Orphans (no task_id
within 24h) are GC'd by a background sweep.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "task_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        # storage_key is the S3 key. Format: attachments/<workspace_id>/<sha256>
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    # Lookup by task: drawer + dispatcher both need attachments per task.
    op.create_index(
        "task_attachments_task_id_idx",
        "task_attachments",
        ["task_id"],
    )
    # Dedup probe + content-addressable lookup.
    op.create_index(
        "task_attachments_workspace_sha_idx",
        "task_attachments",
        ["workspace_id", "sha256"],
    )


def downgrade() -> None:
    op.drop_index(
        "task_attachments_workspace_sha_idx", table_name="task_attachments",
    )
    op.drop_index("task_attachments_task_id_idx", table_name="task_attachments")
    op.drop_table("task_attachments")
