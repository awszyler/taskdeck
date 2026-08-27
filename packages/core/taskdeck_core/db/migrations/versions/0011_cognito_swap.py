"""cognito swap (P5.1)

Revision ID: 0011
Revises: 0010

Replaces the GitHub-OAuth-era auth schema with a Cognito-backed BFF
session model.

- Drops ``users.github_id`` (production has zero rows because
  ``TD_AUTH_MODE`` has always been ``disabled``; dev DBs may have
  test rows but those tests are deleted in this milestone).
- Adds ``users.cognito_sub`` with a partial unique index.
- Drops the old ``sessions`` table and creates a new ``user_sessions``
  table that stores Fernet-encrypted Cognito refresh + access tokens
  along with their expiries. The browser still gets an opaque cookie
  carrying this row's UUID — the design changes the *contents*, not
  the cookie shape.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────
    # Drop GitHub-era unique index + column. login + avatar_url stay on
    # the model for one release for backward compat on read paths.
    op.execute("DROP INDEX IF EXISTS uq_users_github_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_id")

    op.add_column("users", sa.Column("cognito_sub", sa.Text(), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX users_cognito_sub_uniq "
        "ON users (cognito_sub) WHERE cognito_sub IS NOT NULL"
    )

    # ── sessions → user_sessions ──────────────────────────────────────
    # Drop the old table outright. There is no data worth migrating: the
    # old schema only held a uuid + user_id + timestamps, and any live
    # session would be from a deployment that never actually shipped
    # GitHub OAuth.
    op.execute("DROP INDEX IF EXISTS idx_sessions_expires")
    op.execute("DROP INDEX IF EXISTS idx_sessions_user_id")
    op.execute("DROP TABLE IF EXISTS sessions")

    op.create_table(
        "user_sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cognito_sub", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "access_token_expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "refresh_token_expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
    )
    op.create_index(
        "user_sessions_user_id_idx", "user_sessions", ["user_id"], unique=False
    )
    op.create_index(
        "user_sessions_refresh_exp_idx",
        "user_sessions",
        ["refresh_token_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("user_sessions_refresh_exp_idx", table_name="user_sessions")
    op.drop_index("user_sessions_user_id_idx", table_name="user_sessions")
    op.drop_table("user_sessions")

    # Recreate the old sessions table shape so a pre-P5.1 deployment can
    # roll back. The old code path inserts new rows on login; existing
    # rows would be lost (acceptable — rollback target had no real users).
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_sessions_user_id", "sessions", ["user_id"], unique=False
    )
    op.create_index(
        "idx_sessions_expires", "sessions", ["expires_at"], unique=False
    )

    op.execute("DROP INDEX IF EXISTS users_cognito_sub_uniq")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS cognito_sub")

    op.add_column("users", sa.Column("github_id", sa.BigInteger(), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX uq_users_github_id ON users (github_id) "
        "WHERE github_id IS NOT NULL"
    )
