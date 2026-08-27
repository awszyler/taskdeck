from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


def _settings_metrics_on() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev-runner-token",
        TD_METRICS_ENABLED=True,
    )


def _settings_metrics_off() -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://taskdeck:taskdeck@localhost:5432/taskdeck",
        TD_RUNNER_BEARER_TOKEN="dev-runner-token",
        TD_METRICS_ENABLED=False,
    )


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200_with_prometheus_format():
    """/metrics returns 200 and Prometheus plaintext format when metrics_enabled=True."""
    app = create_app(_settings_metrics_on())
    app.state.settings = _settings_metrics_on()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/metrics")
    assert r.status_code == 200
    # Prometheus text format uses '# HELP' and '# TYPE' comment lines.
    content = r.text
    assert "# HELP" in content or "# TYPE" in content


@pytest.mark.asyncio
async def test_metrics_endpoint_absent_when_disabled():
    """/metrics returns 404 when metrics_enabled=False."""
    app = create_app(_settings_metrics_off())
    app.state.settings = _settings_metrics_off()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/metrics")
    assert r.status_code == 404
