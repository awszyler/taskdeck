from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev-runner-token",
        TD_REQUEST_MAX_BODY_BYTES=10 * 1024 * 1024,  # 10 MB for these tests
    )


@pytest.mark.asyncio
async def test_body_under_limit_not_rejected():
    """Content-Length within limit must not be rejected by BodySizeMiddleware.
    Uses /health (no DB required) to keep the test self-contained."""
    app = create_app(_settings())
    app.state.settings = _settings()
    body = b"x" * (1 * 1024 * 1024)  # 1 MB — within 10 MB limit
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/health",
            content=body,
            headers={"content-length": str(len(body)), "content-type": "application/octet-stream"},
        )
    # Middleware must not have rejected with 413; route returns 405 (method not allowed) which is fine.
    assert r.status_code != 413


@pytest.mark.asyncio
async def test_body_over_limit_returns_413():
    """Content-Length exceeding TD_REQUEST_MAX_BODY_BYTES must return 413.
    Middleware intercepts before any route or DB access."""
    app = create_app(_settings())
    app.state.settings = _settings()
    # 30 MB > 10 MB limit; we only set the header — body never needs to be streamed
    body_size = 30 * 1024 * 1024
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/health",
            content=b"",
            headers={
                "content-length": str(body_size),
                "content-type": "application/octet-stream",
            },
        )
    assert r.status_code == 413
    assert "body too large" in r.json()["detail"]
