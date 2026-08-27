from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from taskdeck_core.auth.middleware import current_principal
from taskdeck_core.db.models import IntentParseLog
from taskdeck_core.intent.parser import IntentParser, OpenAIClient, _MissingLLMClient
from taskdeck_core.intent.schema import IntentInput, ParsedIntent

router = APIRouter(prefix="/api/v1/intent", tags=["intent"])


def _get_parser(request: Request) -> IntentParser:
    parser = getattr(request.app.state, "intent_parser", None)
    if parser is None:
        settings = request.app.state.settings
        if not settings.litellm_base_url or not settings.litellm_api_key:
            client = _MissingLLMClient()
        else:
            client = OpenAIClient(
                base_url=settings.litellm_base_url,
                api_key=settings.litellm_api_key,
            )
        parser = IntentParser(
            client=client,
            model=settings.intent_parser_model,
            timeout=settings.intent_parser_timeout_seconds,
        )
        request.app.state.intent_parser = parser
    return parser


@router.post("/parse", response_model=ParsedIntent)
async def parse_intent(body: IntentInput, request: Request) -> ParsedIntent:
    await current_principal(request)  # enforces auth when auth_mode != disabled

    parser = _get_parser(request)
    hub = getattr(request.app.state, "runner_hub", None)
    caps = hub.available_capabilities() if hub is not None else []
    t0 = time.monotonic()
    parsed = await parser.parse(body, available_capabilities=caps)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # model name: prefer the parser's own model attr; fall back to settings if present.
    model_name: str = getattr(parser, "_model", None) or getattr(
        getattr(request.app.state, "settings", None), "intent_parser_model", "unknown"
    )

    last_usage = getattr(parser, "last_usage", None)
    if last_usage is not None:
        bus = getattr(request.app.state, "event_bus", None)
        if bus is not None:
            await bus.publish(
                {
                    "type": "cost.event",
                    "provider": "litellm",
                    "operation": "intent_parser",
                    "model": getattr(parser, "last_model", model_name),
                    "tokens_in": last_usage.get("prompt_tokens"),
                    "tokens_out": last_usage.get("completion_tokens"),
                }
            )

    sm = request.app.state.db_sessionmaker
    async with sm() as sess:
        sess.add(
            IntentParseLog(
                raw_input=body.raw_input,
                parsed_output=parsed.model_dump(mode="json"),
                model=model_name,
                latency_ms=latency_ms,
                success=parsed.confidence > 0,
                created_at=datetime.now(UTC),
            )
        )
        await sess.commit()

    return parsed
