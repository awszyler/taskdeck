from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from .commands import handle_bind, handle_cancel, handle_free_text, handle_status
from .crypto import WecomCryptoError, decrypt, verify_signature
from .parse import parse_inner_message, parse_outer_encrypt

if TYPE_CHECKING:
    from .binder import BindCodeCache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/im/wecom", tags=["wecom"])


@router.get("/callback", response_class=PlainTextResponse)
async def callback_verify(
    request: Request,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    """WeCom URL-verification handshake.

    Returns the decrypted echostr as plain text on success.
    Returns 400 on signature mismatch or decryption failure.
    503 if WeCom is not enabled — keeps routes visible but misconfigured installs
    surface clearly.
    """
    settings = request.app.state.settings
    if not settings.wecom_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "wecom disabled")
    if not (settings.wecom_token and settings.wecom_aes_key and settings.wecom_corp_id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "wecom config incomplete")

    if not verify_signature(
        settings.wecom_token, timestamp, nonce, echostr, msg_signature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signature mismatch")
    try:
        plain = decrypt(settings.wecom_aes_key, echostr, settings.wecom_corp_id)
    except WecomCryptoError as e:
        log.warning("echostr decrypt failed: %s", e)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"decrypt failed: {e}") from e

    return plain


@router.post("/callback", response_class=PlainTextResponse)
async def callback_message(
    request: Request,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> str:
    """Receive an encrypted WeCom message, decrypt, and dispatch commands."""
    settings = request.app.state.settings
    if not settings.wecom_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "wecom disabled")
    if not (settings.wecom_token and settings.wecom_aes_key and settings.wecom_corp_id):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "wecom config incomplete")

    raw_body = (await request.body()).decode("utf-8")
    try:
        encrypt_b64 = parse_outer_encrypt(raw_body)
    except (WecomCryptoError, ET.ParseError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad xml: {e}") from e

    if not verify_signature(settings.wecom_token, timestamp, nonce, encrypt_b64, msg_signature):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "signature mismatch")

    try:
        inner_xml = decrypt(settings.wecom_aes_key, encrypt_b64, settings.wecom_corp_id)
    except WecomCryptoError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"decrypt failed: {e}") from e

    try:
        msg = parse_inner_message(inner_xml)
    except ET.ParseError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad inner xml: {e}") from e

    from_user = msg.get("FromUserName", "")
    msg_type = msg.get("MsgType", "")
    content = msg.get("Content", "")

    client = request.app.state.wecom_client
    if msg_type == "text" and from_user and content:
        sm = request.app.state.db_sessionmaker
        cache: BindCodeCache = request.app.state.wecom_bind_codes
        parser = getattr(request.app.state, "intent_parser", None)
        public_base_url = request.app.state.settings.public_base_url
        dispatcher = getattr(request.app.state, "dispatcher", None)
        hub = getattr(request.app.state, "runner_hub", None)
        caps = hub.available_capabilities() if hub is not None else []
        reply = await _dispatch(
            content, from_user, sm, cache,
            parser=parser,
            public_base_url=public_base_url,
            dispatcher=dispatcher,
            available_capabilities=caps,
        )
        try:
            await client.send_text(to_user=from_user, content=reply)
        except Exception as e:  # noqa: BLE001
            log.warning("wecom send failed for %s: %s", from_user, e)

    # WeCom expects the response body to be the literal string "success".
    return "success"


async def _dispatch(
    content: str,
    from_user: str,
    sm,
    cache: BindCodeCache,
    *,
    parser=None,
    public_base_url: str = "",
    dispatcher=None,
    available_capabilities: list[dict[str, str]] | None = None,
) -> str:
    text = content.strip()
    async with sm() as session:
        if text.startswith("/bind "):
            code = text.removeprefix("/bind ").strip()
            return await handle_bind(code=code, external_id=from_user, session=session, cache=cache)
        if text == "/status":
            return await handle_status(external_id=from_user, session=session)
        if text.startswith("/cancel "):
            target = text.removeprefix("/cancel ").strip()
            return await handle_cancel(target=target, external_id=from_user, session=session)
        if text.startswith("/"):
            return "Unknown command. Try: /bind <code>, /status, /cancel <id>"
        if parser is None or dispatcher is None:
            return f"✓ received: {content[:200]}"
        return await handle_free_text(
            content=content,
            external_id=from_user,
            session=session,
            parser=parser,
            public_base_url=public_base_url,
            sessionmaker=sm,
            dispatcher=dispatcher,
            available_capabilities=available_capabilities,
        )
