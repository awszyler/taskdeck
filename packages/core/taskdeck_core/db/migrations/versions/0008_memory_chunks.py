"""memory_chunks — pgvector-backed RAG index

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), nullable=True),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"], ["task_artifacts.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Add the pgvector column via raw SQL — avoids a compile-time pgvector dep in the migration.
    op.execute("ALTER TABLE memory_chunks ADD COLUMN embedding vector(384)")
    op.create_index("idx_memory_workspace", "memory_chunks", ["workspace_id"])
    # HNSW index — pgvector 0.5+ (pgvector/pgvector:pg17 ships 0.7+).
    op.execute(
        "CREATE INDEX idx_memory_embedding ON memory_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_embedding")
    op.drop_index("idx_memory_workspace", table_name="memory_chunks")
    op.drop_table("memory_chunks")
    # Do NOT drop the vector extension — other objects may depend on it in future.
