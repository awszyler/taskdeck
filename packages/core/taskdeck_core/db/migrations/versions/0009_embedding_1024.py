"""embedding 1024 dim

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # We're switching embedding model providers (LiteLLM 384-dim -> Bedrock Cohere v3 1024-dim).
    # Existing chunks with the old vectors are unrecoverable in the new space.
    # Drop and recreate the column. The HNSW index drops with the column.
    op.execute("DROP INDEX IF EXISTS idx_memory_embedding")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE memory_chunks ADD COLUMN embedding vector(1024)")
    op.execute(
        "CREATE INDEX idx_memory_embedding ON memory_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_embedding")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE memory_chunks ADD COLUMN embedding vector(384)")
    op.execute(
        "CREATE INDEX idx_memory_embedding ON memory_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
