from __future__ import annotations

import base64
import secrets

from fastapi.testclient import TestClient
from taskdeck_core.im.wecom.crypto import compute_signature, encrypt
from taskdeck_core.main import create_app


def _make_key() -> str:
    raw = secrets.token_bytes(32)
    return base64.b64encode(raw).decode()[:43]


def test_callback_verify_returns_503_when_disabled(monkeypatch):
    monkeypatch.setenv("TD_WECOM_ENABLED", "false")
    app = create_app()
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"},
        )
    assert r.status_code == 503


def test_callback_verify_valid(monkeypatch):
    key = _make_key()
    corp = "wxTestCorp"
    token = "TOK"

    monkeypatch.setenv("TD_WECOM_ENABLED", "true")
    monkeypatch.setenv("TD_WECOM_TOKEN", token)
    monkeypatch.setenv("TD_WECOM_AES_KEY", key)
    monkeypatch.setenv("TD_WECOM_CORP_ID", corp)
    monkeypatch.setenv("TD_WECOM_AGENT_ID", "1000")
    monkeypatch.setenv("TD_WECOM_SECRET", "s")

    plain = "hello-echo"
    echostr = encrypt(key, plain, corp)
    sig = compute_signature(token, "123", "abc", echostr)

    app = create_app()
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": sig, "timestamp": "123", "nonce": "abc", "echostr": echostr},
        )
    assert r.status_code == 200
    assert r.text == plain


def test_callback_verify_bad_signature(monkeypatch):
    key = _make_key()
    corp = "wxTestCorp"
    monkeypatch.setenv("TD_WECOM_ENABLED", "true")
    monkeypatch.setenv("TD_WECOM_TOKEN", "TOK")
    monkeypatch.setenv("TD_WECOM_AES_KEY", key)
    monkeypatch.setenv("TD_WECOM_CORP_ID", corp)
    monkeypatch.setenv("TD_WECOM_AGENT_ID", "1000")
    monkeypatch.setenv("TD_WECOM_SECRET", "s")

    echostr = encrypt(key, "x", corp)

    app = create_app()
    with TestClient(app) as client:
        r = client.get(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": "00", "timestamp": "123", "nonce": "abc", "echostr": echostr},
        )
    assert r.status_code == 400
