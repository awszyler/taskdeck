"""Server-side session lifecycle for Cognito-backed auth.

The browser only ever sees the opaque session UUID via an HttpOnly +
Secure + SameSite=Strict cookie. The encrypted Cognito refresh + access
tokens live in this row and are only ever decrypted in the backend
process for the seconds of an outbound call.

Encryption is Fernet (cryptography library) using
``TD_SESSION_ENCRYPTION_KEY``. Loss of that key means every active
session must re-login (acceptable). Compromise of DB + key together
gives an attacker every active refresh token (30-day windows) until
GlobalSignOut is called per user — that's the same threat model that
defeats every server-stored token system, called out in spec §10.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from taskdeck_core.auth.cognito_client import CognitoClient, CognitoError
from taskdeck_core.db.models import User, UserSession

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


class SessionEncryptionError(Exception):
    """Encryption/decryption failed — usually a wrong/missing key."""


class CognitoRefreshFailed(Exception):
    """Refresh-token roundtrip to Cognito failed; row should be deleted."""


# Cognito's Refresh token TTL is configured per app client; 30 days is
# the default we recommend in the runbook. We track expiry server-side
# rather than parsing the token; if Cognito rejects on refresh we delete
# the row regardless.
DEFAULT_REFRESH_TTL_DAYS = 30


def make_fernet(key: str) -> Fernet:
    if not key:
        raise SessionEncryptionError("TD_SESSION_ENCRYPTION_KEY not set")
    try:
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:  # malformed key
        raise SessionEncryptionError(str(e)) from e


def encrypt(fernet: Fernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt(fernet: Fernet, ciphertext: bytes) -> str:
    try:
        return fernet.decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as e:
        raise SessionEncryptionError("invalid ciphertext or wrong key") from e


async def create_session(
    db: AsyncSession,
    *,
    fernet: Fernet,
    user_id: UUID,
    cognito_sub: str,
    access_token: str,
    access_token_ttl_seconds: int,
    refresh_token: str,
    refresh_token_ttl_days: int = DEFAULT_REFRESH_TTL_DAYS,
    user_agent: str | None = None,
    ip: str | None = None,
) -> UserSession:
    now = datetime.now(UTC)
    row = UserSession(
        user_id=user_id,
        cognito_sub=cognito_sub,
        encrypted_refresh_token=encrypt(fernet, refresh_token),
        encrypted_access_token=encrypt(fernet, access_token),
        access_token_expires_at=now + timedelta(seconds=access_token_ttl_seconds),
        refresh_token_expires_at=now + timedelta(days=refresh_token_ttl_days),
        created_at=now,
        last_seen_at=now,
        user_agent=user_agent,
        ip=ip,
    )
    db.add(row)
    await db.flush()
    return row


async def refresh_session(
    db: AsyncSession,
    *,
    sess_row: UserSession,
    fernet: Fernet,
    cognito: CognitoClient,
) -> None:
    """Mint a new access token and update the row in place.

    Raises ``CognitoRefreshFailed`` when Cognito rejects the refresh
    token (revoked, expired, user disabled). Caller deletes the row.
    """
    refresh_token = decrypt(fernet, sess_row.encrypted_refresh_token)
    try:
        out = await cognito.refresh_tokens(refresh_token=refresh_token)
    except CognitoError as e:
        raise CognitoRefreshFailed(str(e)) from e

    auth = out.get("AuthenticationResult") or {}
    access = auth.get("AccessToken")
    expires_in = int(auth.get("ExpiresIn") or 0)
    if not access or expires_in <= 0:
        raise CognitoRefreshFailed(f"unexpected refresh response: {out}")

    now = datetime.now(UTC)
    sess_row.encrypted_access_token = encrypt(fernet, access)
    sess_row.access_token_expires_at = now + timedelta(seconds=expires_in)
    # Refresh token rotation is opt-in on Cognito; if a new one is
    # returned, persist it. Otherwise keep the existing one.
    new_refresh = auth.get("RefreshToken")
    if new_refresh:
        sess_row.encrypted_refresh_token = encrypt(fernet, new_refresh)


async def revoke_session(
    db: AsyncSession,
    *,
    sess_row: UserSession,
    fernet: Fernet,
    cognito: CognitoClient | None = None,
) -> None:
    """Best-effort GlobalSignOut + delete the row.

    We delete the row even if GlobalSignOut fails — leaving a
    server-side row when the user has clicked logout would be the
    bigger UX bug. The Cognito refresh token may still be live
    elsewhere, but the row is gone so the cookie is dead from our
    side.
    """
    if cognito is not None:
        try:
            access = decrypt(fernet, sess_row.encrypted_access_token)
            await cognito.global_sign_out(access_token=access)
        except (CognitoError, SessionEncryptionError):
            pass
    await db.delete(sess_row)


async def lookup_user_by_sub(
    db: AsyncSession, *, cognito_sub: str
) -> User | None:
    return (
        await db.scalars(select(User).where(User.cognito_sub == cognito_sub))
    ).first()


async def upsert_user_from_claims(
    db: AsyncSession,
    *,
    cognito_sub: str,
    email: str,
    name: str | None = None,
) -> User:
    user = await lookup_user_by_sub(db, cognito_sub=cognito_sub)
    if user is not None:
        if email and user.email != email:
            user.email = email
        if name and user.name != name:
            user.name = name
        return user
    user = User(
        workspace_id=None,
        email=email,
        name=name or email,
        role="member",
        cognito_sub=cognito_sub,
    )
    db.add(user)
    await db.flush()
    return user
