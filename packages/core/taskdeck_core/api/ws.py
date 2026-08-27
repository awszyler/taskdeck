from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from taskdeck_core.db.models import UserSession

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


WS_POLICY_VIOLATION = 1008


class EventBus:
    """Very small in-process publish/subscribe for UI WebSocket clients.

    M1 scope: single-process. Broadcast to all subscribers; no per-task filtering yet.
    M4.4: added subscribe_callback for fire-and-forget async callbacks (e.g. WeCom notifier).
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._callbacks: list[Callable[[dict], Awaitable[None]]] = []
        # Strong refs to in-flight callback tasks. Without this the
        # event loop's weak-ref task tracker can GC them mid-flight.
        self._inflight_callbacks: set[asyncio.Task] = set()

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(q)

    def subscribe_callback(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        """Register an async callback invoked on every published event (fire-and-forget)."""
        self._callbacks.append(cb)

    async def _safe_invoke(self, cb: Callable[[dict], Awaitable[None]], event: dict) -> None:
        try:
            await cb(event)
        except Exception as e:  # noqa: BLE001
            log.warning("event callback failed: %s", e)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer; drop and log — the UI can resync via REST on reconnect.
                log.warning("dropping event for slow subscriber")
        for cb in self._callbacks:
            t = asyncio.create_task(self._safe_invoke(cb, event))
            self._inflight_callbacks.add(t)
            t.add_done_callback(self._inflight_callbacks.discard)


def ws_router(bus: EventBus) -> APIRouter:
    r = APIRouter()

    @r.websocket("/api/v1/ws")
    async def endpoint(ws: WebSocket) -> None:
        if not await _authorise_ws(ws):
            return
        await ws.accept()
        q = bus.subscribe()

        # Industry-standard application-level heartbeat (Slack RTM,
        # Discord Gateway, GitHub Live Logs all do this). The protocol
        # ping/pong from uvicorn isn't always honored end-to-end —
        # CloudFront / corporate proxies can half-close the TCP
        # without delivering a FIN, leaving the browser thinking the
        # WS is alive. Echoing an app-level "ping" lets the browser
        # detect a missing pong and reconnect.
        async def reader() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json(
                        {"type": "pong", "id": msg.get("id")},
                    )
                # Other inbound messages: ignored.

        async def writer() -> None:
            while True:
                event = await q.get()
                await ws.send_json(event)

        try:
            done, pending = await asyncio.wait(
                [asyncio.create_task(reader()), asyncio.create_task(writer())],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for t in pending:
                t.cancel()
            # Surface the first exception (commonly WebSocketDisconnect).
            for t in done:
                t.result()
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(q)

    return r


async def _authorise_ws(ws: WebSocket) -> bool:
    """Validate the cookie session before ``accept()``.

    Returns True if the connection should proceed. On rejection the
    socket is closed with policy-violation (1008) and we return False.
    """
    settings = getattr(ws.app.state, "settings", None)
    if settings is None or settings.auth_mode == "disabled":
        return True

    cookie_name = settings.session_cookie_name
    sid_str = ws.cookies.get(cookie_name)
    if not sid_str:
        await ws.close(code=WS_POLICY_VIOLATION)
        return False
    try:
        session_id = UUID(sid_str)
    except ValueError:
        await ws.close(code=WS_POLICY_VIOLATION)
        return False

    sm = ws.app.state.db_sessionmaker
    async with sm() as db:
        sess_row = await db.get(UserSession, session_id)
        if sess_row is None:
            await ws.close(code=WS_POLICY_VIOLATION)
            return False
        now = datetime.now(UTC)
        exp = sess_row.refresh_token_expires_at
        exp = exp if exp.tzinfo is not None else exp.replace(tzinfo=UTC)
        if exp <= now:
            await ws.close(code=WS_POLICY_VIOLATION)
            return False
    return True
