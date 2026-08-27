# pyright: reportCallIssue=false
"""Login state-machine integration tests with a stubbed Cognito client."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from taskdeck_core.auth.cognito_client import CodeMismatch, InvalidCredentials
from taskdeck_core.auth.flow_store import InMemoryFlowStore
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://x:y@localhost:5432/x",
        TD_RUNNER_BEARER_TOKEN="dev",
        TD_AUTH_MODE="cognito",
        TD_COGNITO_USER_POOL_ID="ap-northeast-1_xxx",
        TD_COGNITO_CLIENT_ID="abc",
        TD_COGNITO_REGION="ap-northeast-1",
        TD_SESSION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )


def _build():
    s = _settings()
    app = create_app(s)
    app.state.settings = s
    app.state.fernet = Fernet(s.session_encryption_key.encode())
    app.state.flow_store = InMemoryFlowStore()
    app.state.cognito_client = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_login_init_invalid_credentials_returns_401() -> None:
    app = _build()
    app.state.cognito_client.initiate_auth_srp = AsyncMock(
        side_effect=InvalidCredentials("NotAuthorizedException", "no")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/init",
            json={"email": "a@b.c", "srp_a": "deadbeef" * 16},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_init_returns_srp_b_and_flow_id() -> None:
    app = _build()
    app.state.cognito_client.initiate_auth_srp = AsyncMock(
        return_value={
            "ChallengeName": "PASSWORD_VERIFIER",
            "Session": "cognito-session-blob",
            "ChallengeParameters": {
                "USERNAME": "internal-username",
                "SRP_B": "srp-b-value",
                "SALT": "salt-value",
                "SECRET_BLOCK": "secret-block-value",
            },
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/init",
            json={"email": "a@b.c", "srp_a": "deadbeef" * 16},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["srp_b"] == "srp-b-value"
    assert body["salt"] == "salt-value"
    assert body["secret_block"] == "secret-block-value"
    assert "flow_id" in body
    # Flow state stored with cognito session
    state = await app.state.flow_store.get(body["flow_id"])
    assert state is not None
    assert state["session"] == "cognito-session-blob"
    assert state["username_internal"] == "internal-username"


@pytest.mark.asyncio
async def test_login_respond_unknown_flow_returns_400() -> None:
    app = _build()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/respond",
            json={
                "flow_id": "nonexistent",
                "password_proof": "p",
                "timestamp": "t",
                "secret_block": "s",
            },
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_login_totp_wrong_code_returns_401() -> None:
    app = _build()
    fid = InMemoryFlowStore.new_flow_id()
    await app.state.flow_store.put(
        fid,
        {
            "step": "password_verifier",
            "email": "a@b.c",
            "session": "sess",
            "username_internal": "internal",
        },
    )
    app.state.cognito_client.respond_software_token_mfa = AsyncMock(
        side_effect=CodeMismatch("CodeMismatchException", "no")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/totp",
            json={"flow_id": fid, "code": "000000"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_respond_yields_totp_required_branch() -> None:
    app = _build()
    fid = InMemoryFlowStore.new_flow_id()
    await app.state.flow_store.put(
        fid,
        {
            "step": "password_verifier",
            "email": "a@b.c",
            "session": "old-session",
            "username_internal": "internal",
        },
    )
    app.state.cognito_client.respond_password_verifier = AsyncMock(
        return_value={
            "ChallengeName": "SOFTWARE_TOKEN_MFA",
            "Session": "new-session",
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/respond",
            json={
                "flow_id": fid,
                "password_proof": "p",
                "timestamp": "t",
                "secret_block": "s",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "totp_required", "flow_id": fid}
    state = await app.state.flow_store.get(fid)
    assert state is not None
    assert state["session"] == "new-session"


@pytest.mark.asyncio
async def test_login_respond_yields_mfa_setup_branch_with_otpauth_uri() -> None:
    app = _build()
    fid = InMemoryFlowStore.new_flow_id()
    await app.state.flow_store.put(
        fid,
        {
            "step": "password_verifier",
            "email": "alice@example.com",
            "session": "old-session",
            "username_internal": "internal",
        },
    )
    app.state.cognito_client.respond_password_verifier = AsyncMock(
        return_value={
            "ChallengeName": "MFA_SETUP",
            "Session": "challenge-session",
        }
    )
    app.state.cognito_client.associate_software_token = AsyncMock(
        return_value={"SecretCode": "JBSWY3DPEHPK3PXP", "Session": "assoc-session"}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/auth/login/respond",
            json={
                "flow_id": fid,
                "password_proof": "p",
                "timestamp": "t",
                "secret_block": "s",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "mfa_setup"
    assert body["secret"] == "JBSWY3DPEHPK3PXP"
    assert "otpauth://totp/" in body["otpauth_uri"]
    assert "secret=JBSWY3DPEHPK3PXP" in body["otpauth_uri"]
    assert "issuer=Taskdeck" in body["otpauth_uri"]
