from __future__ import annotations

from fastapi.testclient import TestClient
from taskdeck_core.main import create_app


class _FakeSTT:
    def __init__(self, *, transcript: str | None = None, raise_exc: Exception | None = None):
        self._transcript = transcript
        self._raise = raise_exc

    async def transcribe(self, data: bytes, **_) -> str:
        if self._raise:
            raise self._raise
        assert self._transcript is not None
        return self._transcript


def test_stt_empty_body():
    app = create_app()
    with TestClient(app) as client:
        app.state.stt_client = _FakeSTT(transcript="ok")
        r = client.post("/api/v1/stt", content=b"")
    assert r.status_code == 400


def test_stt_too_large():
    # BodySizeMiddleware (25 MB global cap) intercepts first and returns 413.
    # The route-level 400 is shadowed; 413 is the correct contract.
    app = create_app()
    with TestClient(app) as client:
        app.state.stt_client = _FakeSTT(transcript="ok")
        big = b"x" * (26 * 1024 * 1024)
        r = client.post("/api/v1/stt", content=big)
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_stt_happy_path():
    app = create_app()
    with TestClient(app) as client:
        app.state.stt_client = _FakeSTT(transcript="hello world")
        r = client.post(
            "/api/v1/stt",
            content=b"fakebytes",
            headers={"content-type": "audio/webm"},
        )
    assert r.status_code == 200
    assert r.json() == {"transcript": "hello world"}


def test_stt_backend_error():
    app = create_app()
    with TestClient(app) as client:
        app.state.stt_client = _FakeSTT(raise_exc=RuntimeError("provider said no"))
        r = client.post("/api/v1/stt", content=b"fakebytes")
    assert r.status_code == 502
    assert "provider said no" in r.json()["detail"]
