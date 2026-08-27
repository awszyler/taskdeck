"""Cognito JWT verifier — generates a real RSA-signed token in test."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from taskdeck_core.auth.cognito_jwt import (
    CognitoJwtVerifier,
    JwtVerificationError,
)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _make_token(*, kid: str, claims: dict[str, Any], private_key) -> str:
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    h = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    signed_input = f"{h}.{p}".encode()
    sig = private_key.sign(signed_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{_b64u(sig)}"


def _build_jwk_from_public(*, kid: str, public_key) -> dict[str, Any]:
    pub_numbers = public_key.public_numbers()
    n = _b64u(
        pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, "big")
    )
    e = _b64u(
        pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, "big")
    )
    return {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig", "n": n, "e": e}


@pytest.fixture
def rsa_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    return priv, pub


def _wire_verifier_with_keys(verifier: CognitoJwtVerifier, *, kid: str, pub):
    """Bypass network fetch by seeding the verifier's JWKS cache."""
    verifier._jwks = {kid: _build_jwk_from_public(kid=kid, public_key=pub)}
    verifier._jwks_fetched_at = time.monotonic()


@pytest.mark.asyncio
async def test_verify_valid_access_token(rsa_keypair) -> None:
    priv, pub = rsa_keypair
    issuer = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_xxx"
    verifier = CognitoJwtVerifier(
        region="ap-northeast-1", user_pool_id="ap-northeast-1_xxx", client_id="abc"
    )
    _wire_verifier_with_keys(verifier, kid="k1", pub=pub)

    token = _make_token(
        kid="k1",
        claims={
            "iss": issuer,
            "client_id": "abc",
            "token_use": "access",
            "exp": int(time.time()) + 600,
            "sub": "user-sub",
        },
        private_key=priv,
    )
    payload = await verifier.verify_access_token(token)
    assert payload["sub"] == "user-sub"


@pytest.mark.asyncio
async def test_verify_rejects_id_token(rsa_keypair) -> None:
    priv, pub = rsa_keypair
    issuer = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_xxx"
    verifier = CognitoJwtVerifier(
        region="ap-northeast-1", user_pool_id="ap-northeast-1_xxx", client_id="abc"
    )
    _wire_verifier_with_keys(verifier, kid="k1", pub=pub)

    token = _make_token(
        kid="k1",
        claims={
            "iss": issuer,
            "aud": "abc",
            "token_use": "id",  # not 'access'
            "exp": int(time.time()) + 600,
        },
        private_key=priv,
    )
    with pytest.raises(JwtVerificationError):
        await verifier.verify_access_token(token)


@pytest.mark.asyncio
async def test_verify_rejects_expired(rsa_keypair) -> None:
    priv, pub = rsa_keypair
    issuer = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_xxx"
    verifier = CognitoJwtVerifier(
        region="ap-northeast-1", user_pool_id="ap-northeast-1_xxx", client_id="abc"
    )
    _wire_verifier_with_keys(verifier, kid="k1", pub=pub)

    token = _make_token(
        kid="k1",
        claims={
            "iss": issuer,
            "client_id": "abc",
            "token_use": "access",
            "exp": int(time.time()) - 600,  # already expired
        },
        private_key=priv,
    )
    with pytest.raises(JwtVerificationError):
        await verifier.verify_access_token(token)


@pytest.mark.asyncio
async def test_verify_rejects_wrong_client_id(rsa_keypair) -> None:
    priv, pub = rsa_keypair
    issuer = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_xxx"
    verifier = CognitoJwtVerifier(
        region="ap-northeast-1", user_pool_id="ap-northeast-1_xxx", client_id="abc"
    )
    _wire_verifier_with_keys(verifier, kid="k1", pub=pub)

    token = _make_token(
        kid="k1",
        claims={
            "iss": issuer,
            "client_id": "WRONG",
            "token_use": "access",
            "exp": int(time.time()) + 600,
        },
        private_key=priv,
    )
    with pytest.raises(JwtVerificationError):
        await verifier.verify_access_token(token)


@pytest.mark.asyncio
async def test_verify_rejects_unknown_kid(rsa_keypair) -> None:
    priv, pub = rsa_keypair
    verifier = CognitoJwtVerifier(
        region="ap-northeast-1", user_pool_id="ap-northeast-1_xxx", client_id="abc"
    )
    _wire_verifier_with_keys(verifier, kid="known", pub=pub)
    token = _make_token(
        kid="unknown-kid",
        claims={
            "iss": "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_xxx",
            "client_id": "abc",
            "token_use": "access",
            "exp": int(time.time()) + 600,
        },
        private_key=priv,
    )
    # Force the "refresh" path to a no-op so unknown kid stays unknown.
    async def _noop_refresh() -> None:
        return

    verifier._refresh_jwks = _noop_refresh  # type: ignore[method-assign]
    with pytest.raises(JwtVerificationError):
        await verifier.verify_access_token(token)


# Suppress: unused module-level imports referenced for typing.
_ = serialization
