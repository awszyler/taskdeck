from __future__ import annotations

from datetime import datetime  # noqa: TCH003
from uuid import UUID  # noqa: TCH003

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPkMixin


class UserSession(Base, UUIDPkMixin):
    """A logged-in browser session.

    The cookie ``ccpt_session`` carries this row's UUID. The browser never
    sees a Cognito token; the encrypted Cognito refresh + access tokens
    live here so the backend can call Cognito on the user's behalf.
    """

    __tablename__ = "user_sessions"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    cognito_sub: Mapped[str] = mapped_column(Text)

    # Fernet-encrypted ciphertext (bytes). Encryption key from
    # TD_SESSION_ENCRYPTION_KEY. Encrypting both protects against a DB
    # dump granting the remaining-lifetime of the access token.
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_access_token: Mapped[bytes] = mapped_column(LargeBinary)

    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
