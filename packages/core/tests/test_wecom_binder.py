from __future__ import annotations

import time
from uuid import uuid4

from taskdeck_core.im.wecom.binder import BindCodeCache


def test_issue_and_consume():
    cache = BindCodeCache()
    ws_id = uuid4()
    code, exp = cache.issue(workspace_id=ws_id)
    assert len(code) == 6
    assert exp > time.time()
    entry = cache.consume(code)
    assert entry is not None
    assert entry.workspace_id == ws_id
    # Single-use: second consume returns None.
    assert cache.consume(code) is None


def test_consume_unknown_code():
    cache = BindCodeCache()
    assert cache.consume("NOPE99") is None


def test_consume_expired(monkeypatch):
    cache = BindCodeCache()
    ws_id = uuid4()
    code, _ = cache.issue(workspace_id=ws_id)
    # Force expiry
    cache._codes[code].expires_at = time.time() - 1
    assert cache.consume(code) is None
