"""audit_events

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_workspace_time", "audit_events", ["workspace_id", "created_at"])
    op.create_index("idx_audit_user_time", "audit_events", ["user_id", "created_at"])
    op.create_index("idx_audit_kind", "audit_events", ["kind"])


def downgrade() -> None:
    op.drop_index("idx_audit_kind", table_name="audit_events")
    op.drop_index("idx_audit_user_time", table_name="audit_events")
    op.drop_index("idx_audit_workspace_time", table_name="audit_events")
    op.drop_table("audit_events")
