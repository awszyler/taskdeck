from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from taskdeck_core.db.engine import get_session, get_sessionmaker_for_tests
from taskdeck_core.intent.schema import IntentInput, ParsedIntent
from taskdeck_core.main import create_app
from taskdeck_core.settings import Settings


class _StaticParser:
    def __init__(self, result: ParsedIntent):
        self._result = result

    async def parse(self, _input: IntentInput, **_kwargs) -> ParsedIntent:
        return self._result


async def test_parse_endpoint_happy():
    sm = await get_sessionmaker_for_tests()
    app = create_app()
    app.state.db_sessionmaker = sm

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    # ASGITransport does not run ASGI lifespan; pre-seed state manually.
    app.state.intent_parser = _StaticParser(
        ParsedIntent(
            title="Test title",
            agent="shell",
            prompt="echo hi",
            confidence=0.8,
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/intent/parse",
            json={"raw_input": "please echo hi"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Test title"
    assert body["agent"] == "shell"
    assert body["confidence"] == 0.8


async def test_parse_endpoint_falls_back_when_no_llm_config():
    sm = await get_sessionmaker_for_tests()
    app = create_app()
    app.state.db_sessionmaker = sm
    # ASGITransport does not run ASGI lifespan; pre-seed settings manually.
    # Force the "no LLM configured" code path regardless of .env contents by
    # constructing Settings with explicit empty LiteLLM fields.
    real = Settings()  # type: ignore[call-arg]
    app.state.settings = real.model_copy(
        update={"litellm_base_url": None, "litellm_api_key": None}
    )

    async def override_get_session():
        async with sm() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/intent/parse",
            json={"raw_input": "do a thing"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["confidence"] == 0.0
    # No runner is connected in this test (no app.state.runner_hub) so the
    # parser short-circuits with the "no runners" reason instead of calling LLM.
    reason = body["confidence_reasons"][0].lower()
    assert "no runners" in reason or "unavailable" in reason
