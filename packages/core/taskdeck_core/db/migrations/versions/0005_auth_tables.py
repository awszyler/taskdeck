"""auth_tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15 10:00:00.000000+00:00

"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expand users: add GitHub OAuth columns
    op.add_column('users', sa.Column('github_id', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('avatar_url', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('login', sa.String(length=64), nullable=True))
    # Partial unique index on github_id (only when NOT NULL)
    op.execute(
        "CREATE UNIQUE INDEX uq_users_github_id ON users (github_id) WHERE github_id IS NOT NULL"
    )

    # Make workspace_id nullable — users now exist before joining a workspace
    op.alter_column('users', 'workspace_id', existing_type=sa.Uuid(), nullable=True)

    # Sessions table
    op.create_table(
        'sessions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_sessions_user_id', 'sessions', ['user_id'], unique=False)
    op.create_index('idx_sessions_expires', 'sessions', ['expires_at'], unique=False)

    # Workspace members table
    op.create_table(
        'workspace_members',
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('workspace_id', 'user_id'),
    )

    # Workspace invites table
    op.create_table(
        'workspace_invites',
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('workspace_id', sa.Uuid(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_by', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['consumed_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('code'),
    )


def downgrade() -> None:
    op.drop_table('workspace_invites')
    op.drop_table('workspace_members')
    op.drop_index('idx_sessions_expires', table_name='sessions')
    op.drop_index('idx_sessions_user_id', table_name='sessions')
    op.drop_table('sessions')

    # Restore workspace_id to non-nullable
    op.alter_column('users', 'workspace_id', existing_type=sa.Uuid(), nullable=False)

    op.execute("DROP INDEX IF EXISTS uq_users_github_id")
    op.drop_column('users', 'login')
    op.drop_column('users', 'avatar_url')
    op.drop_column('users', 'github_id')
