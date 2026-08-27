import pytest
from taskdeck_core.settings import Settings


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("TD_RUNNER_BEARER_TOKEN", "abc")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://foo")
    s = Settings()
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/d"
    assert s.runner_bearer_token == "abc"
    assert s.cors_origins == ["http://localhost:5173", "http://foo"]


def test_settings_cors_origins_default_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("TD_RUNNER_BEARER_TOKEN", "abc")
    # Set to empty string — pydantic-settings reads from .env file as fallback,
    # so delenv alone can't clear a value present in .env.  An explicit empty
    # string takes priority over the file and exercises the "no origins" path.
    monkeypatch.setenv("CORS_ORIGINS", "")
    s = Settings()
    assert s.cors_origins == []
