from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, status

from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.stt.client import (
    AwsTranscribeSTTClient,
    OpenAISTTClient,
    STTClient,
    _MissingSTTClient,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["stt"])


MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Whisper hard cap


def _get_client(request: Request) -> STTClient:
    client = getattr(request.app.state, "stt_client", None)
    if client is None:
        settings = request.app.state.settings
        # Choose backend by setting. Empty bucket falls back to
        # _MissingSTTClient with a clear error so misconfiguration
        # surfaces fast.
        if settings.stt_backend == "aws-transcribe":
            bucket = settings.stt_s3_bucket or settings.attachment_bucket
            if not bucket:
                client = _MissingSTTClient()
            else:
                client = AwsTranscribeSTTClient(
                    bucket=bucket,
                    region=settings.stt_region,
                    language_options=[
                        c.strip()
                        for c in settings.stt_language_options.split(",")
                        if c.strip()
                    ],
                )
        elif settings.litellm_base_url and settings.litellm_api_key:
            client = OpenAISTTClient(
                base_url=settings.litellm_base_url,
                api_key=settings.litellm_api_key,
            )
        else:
            client = _MissingSTTClient()
        request.app.state.stt_client = client
    return client


@router.post("/stt")
async def transcribe(request: Request) -> dict[str, str]:
    await current_principal(request)  # enforces auth when auth_mode != disabled

    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty body")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"audio too large (>{MAX_AUDIO_BYTES} bytes)")

    client = _get_client(request)
    settings = request.app.state.settings
    content_type = request.headers.get("content-type", "audio/webm")
    try:
        transcript = await asyncio.wait_for(
            client.transcribe(data, model=settings.stt_model, content_type=content_type),
            timeout=settings.stt_timeout_seconds,
        )
    except TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "stt timed out") from None
    except Exception as e:  # noqa: BLE001
        log.warning("stt failed: %s", e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"stt backend error: {e}") from e

    return {"transcript": transcript}
