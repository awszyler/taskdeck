"""intent_parse_log

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14 12:00:00.000000+00:00

"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'intent_parse_log',
        sa.Column('task_id', sa.Uuid(), nullable=True),
        sa.Column('raw_input', sa.Text(), nullable=False),
        sa.Column('parsed_output', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('intent_parse_log')
