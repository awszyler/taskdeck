from __future__ import annotations

import base64
import secrets

from fastapi.testclient import TestClient
from taskdeck_core.im.wecom.crypto import compute_signature, encrypt
from taskdeck_core.main import create_app


def _make_key() -> str:
    raw = secrets.token_bytes(32)
    return base64.b64encode(raw).decode()[:43]


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, *, to_user: str, content: str) -> None:
        self.sent.append((to_user, content))

    async def aclose(self) -> None:
        pass


def _seed_app(monkeypatch, key, corp, token):
    monkeypatch.setenv("TD_WECOM_ENABLED", "true")
    monkeypatch.setenv("TD_WECOM_TOKEN", token)
    monkeypatch.setenv("TD_WECOM_AES_KEY", key)
    monkeypatch.setenv("TD_WECOM_CORP_ID", corp)
    monkeypatch.setenv("TD_WECOM_AGENT_ID", "1000")
    monkeypatch.setenv("TD_WECOM_SECRET", "s")
    app = create_app()
    app.state.wecom_client = _FakeClient()
    return app


def test_post_callback_free_text_unbound_user(monkeypatch):
    """Free-text from an unbound user returns a 'not bound' prompt."""
    key = _make_key()
    corp = "wxC"
    token = "T"
    app = _seed_app(monkeypatch, key, corp, token)

    inner = """<xml>
<ToUserName>wxC</ToUserName>
<FromUserName>UserA</FromUserName>
<CreateTime>1</CreateTime>
<MsgType>text</MsgType>
<Content>ping</Content>
<MsgId>1</MsgId>
<AgentID>1000</AgentID>
</xml>"""
    encrypted = encrypt(key, inner, corp)
    body = f"<xml><ToUserName>{corp}</ToUserName><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    sig = compute_signature(token, "t", "n", encrypted)

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": sig, "timestamp": "t", "nonce": "n"},
            content=body,
            headers={"content-type": "application/xml"},
        )

    assert r.status_code == 200, r.text
    assert r.text == "success"
    sent = app.state.wecom_client.sent
    assert len(sent) == 1
    assert sent[0][0] == "UserA"
    # Unbound user should get a prompt to bind first, not an echo of the raw input.
    assert "bind" in sent[0][1].lower()


def test_post_callback_rejects_bad_signature(monkeypatch):
    key = _make_key()
    corp = "wxC"
    app = _seed_app(monkeypatch, key, corp, "T")

    encrypted = encrypt(key, "<xml></xml>", corp)
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": "00", "timestamp": "t", "nonce": "n"},
            content=body,
            headers={"content-type": "application/xml"},
        )

    assert r.status_code == 400


def test_post_callback_503_when_disabled(monkeypatch):
    monkeypatch.setenv("TD_WECOM_ENABLED", "false")
    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/im/wecom/callback",
            params={"msg_signature": "x", "timestamp": "1", "nonce": "n"},
            content="<xml/>",
        )
    assert r.status_code == 503
