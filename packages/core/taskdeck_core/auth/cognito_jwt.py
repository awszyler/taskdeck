"""Cognito JWT verification (JWKS).

Used as a guardrail when reading the access token out of a session row
before passing it on to Cognito (e.g. on the WS path we want to early-
reject sessions whose stored access token is structurally invalid).
The primary trust boundary is still: a valid session-row UUID in the
cookie.

Pure-stdlib RS256 verification using ``cryptography``. Keeps us off the
PyJWT dep gate.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers


class JwtVerificationError(Exception):
    pass


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _build_public_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return RSAPublicNumbers(e, n).public_key()


class CognitoJwtVerifier:
    """JWKS-cached verifier for Cognito-issued tokens.

    The ``token_use`` field distinguishes ``access`` from ``id`` tokens.
    We only ever accept ``access`` tokens in headers / WS handshakes
    because they have ``client_id``; ID tokens have no scope and shouldn't
    be authorising API calls.
    """

    JWKS_TTL_SECONDS = 3600
    LEEWAY_SECONDS = 30

    def __init__(self, *, region: str, user_pool_id: str, client_id: str) -> None:
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._client_id = client_id
        self._jwks: dict[str, dict[str, Any]] | None = None
        self._jwks_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _refresh_jwks(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(self._jwks_url)
            r.raise_for_status()
            keys = r.json().get("keys", [])
            self._jwks = {k["kid"]: k for k in keys}
            self._jwks_fetched_at = time.monotonic()

    async def _get_jwk(self, kid: str) -> dict[str, Any]:
        async with self._lock:
            stale = (
                self._jwks is None
                or time.monotonic() - self._jwks_fetched_at > self.JWKS_TTL_SECONDS
                or kid not in self._jwks
            )
            if stale:
                await self._refresh_jwks()
            if self._jwks is None or kid not in self._jwks:
                raise JwtVerificationError(f"unknown kid {kid}")
            return self._jwks[kid]

    async def verify_access_token(self, token: str) -> dict[str, Any]:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
        except ValueError as e:
            raise JwtVerificationError("malformed token") from e

        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)

        if header.get("alg") != "RS256":
            raise JwtVerificationError(f"unexpected alg {header.get('alg')}")

        kid = header.get("kid")
        if not kid:
            raise JwtVerificationError("missing kid")
        jwk = await self._get_jwk(kid)
        pub = _build_public_key(jwk)
        signed_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            pub.verify(signature, signed_input, padding.PKCS1v15(), hashes.SHA256())
        except Exception as e:
            raise JwtVerificationError("signature mismatch") from e

        if payload.get("iss") != self._issuer:
            raise JwtVerificationError("issuer mismatch")
        if payload.get("token_use") != "access":
            raise JwtVerificationError("not an access token")
        if payload.get("client_id") != self._client_id:
            raise JwtVerificationError("client_id mismatch")
        now = time.time()
        exp = payload.get("exp", 0)
        if exp + self.LEEWAY_SECONDS < now:
            raise JwtVerificationError("token expired")
        return payload
