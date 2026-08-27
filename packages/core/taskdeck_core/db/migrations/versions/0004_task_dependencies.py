"""task_dependencies

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14 15:00:00.000000+00:00

"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'task_dependencies',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('parent_task_id', sa.Uuid(), nullable=False),
        sa.Column('child_task_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('parent_task_id <> child_task_id', name='no_self_dep'),
        sa.ForeignKeyConstraint(['child_task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('parent_task_id', 'child_task_id', name='uq_task_dep_parent_child'),
    )
    op.create_index(op.f('ix_task_dependencies_child_task_id'), 'task_dependencies', ['child_task_id'], unique=False)
    op.create_index(op.f('ix_task_dependencies_parent_task_id'), 'task_dependencies', ['parent_task_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_task_dependencies_parent_task_id'), table_name='task_dependencies')
    op.drop_index(op.f('ix_task_dependencies_child_task_id'), table_name='task_dependencies')
    op.drop_table('task_dependencies')
