from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass
class _Token:
    value: str
    expires_at: float  # unix seconds


class WecomClient:
    """Thin wrapper around the WeCom Server API for sending messages."""

    def __init__(
        self,
        *,
        corp_id: str,
        secret: str,
        agent_id: str,
        base_url: str = "https://qyapi.weixin.qq.com",
        http: httpx.AsyncClient | None = None,
    ):
        self._corp_id = corp_id
        self._secret = secret
        self._agent_id = agent_id
        self._base = base_url
        self._http = http
        self._owns_http = http is None
        self._token: _Token | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and self._token.expires_at > now + 30:
            return self._token.value
        client = await self._client()
        r = await client.get(
            f"{self._base}/cgi-bin/gettoken",
            params={"corpid": self._corp_id, "corpsecret": self._secret},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"wecom gettoken failed: {data}")
        self._token = _Token(
            value=data["access_token"],
            expires_at=now + data.get("expires_in", 7200),
        )
        return self._token.value

    async def send_text(self, *, to_user: str, content: str) -> None:
        token = await self._get_token()
        client = await self._client()
        r = await client.post(
            f"{self._base}/cgi-bin/message/send",
            params={"access_token": token},
            json={
                "touser": to_user,
                "msgtype": "text",
                "agentid": int(self._agent_id) if self._agent_id.isdigit() else self._agent_id,
                "text": {"content": content},
                "safe": 0,
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"wecom send failed: {data}")


class _NoopWecomClient:
    """Safe fallback when WeCom is disabled — never sends, but doesn't crash the app."""

    async def send_text(self, *, to_user: str, content: str) -> None:  # noqa: ARG002
        log.info("[wecom noop] would send to %s: %s", to_user, content[:80])

    async def aclose(self) -> None:
        pass
