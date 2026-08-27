from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from taskdeck_core.api.ws import EventBus, _authorise_ws


def _make_ws(*, settings_mode: str, cookies: dict | None = None, sess_row=None):
    settings = MagicMock()
    settings.auth_mode = settings_mode
    settings.session_cookie_name = "ccpt_session"

    async def _mock_get(_model_cls, _pk):
        return sess_row

    mock_session = AsyncMock()
    mock_session.get = _mock_get
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    sm = MagicMock(return_value=mock_ctx)

    app = MagicMock()
    app.state.settings = settings
    app.state.db_sessionmaker = sm

    ws = MagicMock()
    ws.app = app
    ws.cookies = dict(cookies or {})
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_disabled_mode_passes_through() -> None:
    ws = _make_ws(settings_mode="disabled")
    assert await _authorise_ws(ws) is True
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_no_cookie_closes() -> None:
    ws = _make_ws(settings_mode="cognito")
    assert await _authorise_ws(ws) is False
    ws.close.assert_called_once_with(code=1008)


@pytest.mark.asyncio
async def test_invalid_uuid_closes() -> None:
    ws = _make_ws(settings_mode="cognito", cookies={"ccpt_session": "not-a-uuid"})
    assert await _authorise_ws(ws) is False
    ws.close.assert_called_once_with(code=1008)


@pytest.mark.asyncio
async def test_session_not_found_closes() -> None:
    ws = _make_ws(
        settings_mode="cognito",
        cookies={"ccpt_session": str(uuid4())},
        sess_row=None,
    )
    assert await _authorise_ws(ws) is False
    ws.close.assert_called_once_with(code=1008)


@pytest.mark.asyncio
async def test_expired_refresh_closes() -> None:
    sess = MagicMock(
        refresh_token_expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    ws = _make_ws(
        settings_mode="cognito",
        cookies={"ccpt_session": str(uuid4())},
        sess_row=sess,
    )
    assert await _authorise_ws(ws) is False
    ws.close.assert_called_once_with(code=1008)


@pytest.mark.asyncio
async def test_valid_session_passes() -> None:
    sess = MagicMock(
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=29)
    )
    ws = _make_ws(
        settings_mode="cognito",
        cookies={"ccpt_session": str(uuid4())},
        sess_row=sess,
    )
    assert await _authorise_ws(ws) is True
    ws.close.assert_not_called()


def test_event_bus_smoke() -> None:
    """Sanity import test — bus still constructs cleanly after ws.py edits."""
    bus = EventBus()
    q = bus.subscribe()
    assert q is not None
    bus.unsubscribe(q)
