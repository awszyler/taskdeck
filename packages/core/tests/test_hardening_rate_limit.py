# pyright: reportCallIssue=false
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from taskdeck_core.hardening.rate_limit import limiter
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev-runner-token",
        TD_AUTH_MODE="cognito",
        TD_AUTH_ALLOW_SIGNUP="false",
        TD_COGNITO_USER_POOL_ID="ap-northeast-1_xxx",
        TD_COGNITO_CLIENT_ID="abc",
        TD_COGNITO_REGION="ap-northeast-1",
        TD_SESSION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter._limiter.storage.reset()
    yield
    limiter._limiter.storage.reset()


@pytest.mark.asyncio
async def test_signup_rate_limit_hits_after_3_per_minute():
    """``/auth/signup`` is limited to 3/min/IP. 4th call must be 429.

    Signup is gated by ``allow_signup=false`` so the call returns 403,
    but slowapi's middleware applies regardless of business outcome.
    """
    app = create_app(_settings())
    app.state.settings = _settings()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        statuses = []
        for _ in range(4):
            r = await ac.post(
                "/api/v1/auth/signup",
                json={"email": "a@b.c", "password": "Pa55word!"},
            )
            statuses.append(r.status_code)
    assert all(s != 429 for s in statuses[:3]), statuses[:3]
    assert statuses[3] == 429, statuses
