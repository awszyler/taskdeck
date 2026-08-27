"""Intent parse loop with per-error retry strategy and heuristic fallback.

Replaces the simple "call LLM, fall back on any exception" pattern in
`IntentParser.parse()`. The loop attempts up to two LLM calls and then a
heuristic, with the retry prompt **adapted to the specific failure** rather
than blind retry.

Design notes:

- Low confidence is NOT a retry trigger. A low-confidence parse is a
  truthful signal from the model and should be surfaced to the user
  (via DRAFT state) rather than masked by a retried "more confident"
  hallucination.
- Heuristic fallback is the terminal layer, never raises. It guarantees
  the parsing task always finalizes — at worst into DRAFT for review.
- `available_capabilities` empty → skip LLM entirely and surface
  "no runners connected" via the heuristic path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from taskdeck_core.metrics.registry import (
    INTENT_PARSE_ATTEMPTS_TOTAL,
    LLM_CALL_DURATION_SECONDS,
)

from .parser import _build_intent_tool, _build_system_prompt, _coerce_args
from .schema import IntentContext, IntentInput, ParsedIntent

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)


HIGH_CONFIDENCE_THRESHOLD = 0.7


# --- Error taxonomy --------------------------------------------------------


class ParseError(Exception):
    """Base class for loop-internal parse errors. Each subclass maps to one
    retry strategy."""


class LLMTimeoutError(ParseError):
    """LLM call exceeded the per-attempt timeout. Retry without prompt change."""


class LLMTransportError(ParseError):
    """LLM client raised a non-timeout error (network, 5xx, etc.). Retry without
    prompt change."""


class JSONParseError(ParseError):
    """LLM returned a tool_use with arguments that aren't valid JSON. Retry with
    a 'you MUST call the tool with valid JSON' nudge."""


class InvalidAgentError(ParseError):
    """LLM picked an agent name outside the enum. Retry with a 'previous
    answer X is not in the enum' nudge to discourage hallucination."""

    def __init__(self, invented_agent: str):
        super().__init__(f"agent '{invented_agent}' not in enum")
        self.invented_agent = invented_agent


class SchemaValidationError(ParseError):
    """Pydantic schema validation rejected the model's output (missing required
    field, wrong type, etc.). Retry with the original prompt — the model often
    self-corrects on the second pass."""


# --- Outcome --------------------------------------------------------------


@dataclass
class ParseOutcome:
    parsed: ParsedIntent
    result: Literal["high_conf", "low_conf", "heuristic"]
    attempts: int
    last_usage: dict | None
    last_model: str | None

    @property
    def should_auto_submit(self) -> bool:
        return self.result == "high_conf"


# --- LLM client protocol --------------------------------------------------


class _LLMClient(Protocol):
    async def create_completion(
        self, *, model: str, messages: list[dict], tools: list[dict], tool_choice: dict
    ) -> dict: ...


# --- The loop -------------------------------------------------------------


class IntentParseLoop:
    """Two-attempt LLM tier + heuristic terminal layer.

    Threading model: each `run()` is independent. Safe to call concurrently
    from many request handlers; the underlying `_LLMClient` must be re-entrant
    (boto3 sync client wrapped in run_in_executor IS re-entrant).
    """

    def __init__(
        self,
        *,
        llm_client: _LLMClient,
        model: str,
        timeout_attempt_1: float = 20.0,
        timeout_attempt_2: float = 25.0,
        clock: Callable[[], float] | None = None,
    ):
        self._client = llm_client
        self._model = model
        self._t1 = timeout_attempt_1
        self._t2 = timeout_attempt_2
        # Hook for tests that need to deterministically observe attempt count;
        # production passes None and we use real time.
        self._clock = clock

    async def run(
        self,
        input: IntentInput,
        *,
        capabilities: list[dict[str, str]],
    ) -> ParseOutcome:
        if not capabilities:
            return self._heuristic(
                input,
                capabilities=[],
                reason="no runners connected",
            )

        ctx = input.context if input.context is not None else IntentContext()
        base_system = _build_system_prompt(ctx, capabilities)

        # --- Attempt 1: full prompt, t1 timeout ---
        nudge: str | None
        try:
            parsed, usage, resp_model = await self._call_llm(
                input, capabilities,
                system_prompt=base_system,
                timeout=self._t1,
            )
            outcome = self._classify(parsed)
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome=outcome).inc()
            return ParseOutcome(
                parsed=parsed, result=outcome, attempts=1,
                last_usage=usage, last_model=resp_model,
            )
        except InvalidAgentError as e:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="invalid_agent").inc()
            nudge = (
                f"Your previous attempt selected agent='{e.invented_agent}', which is "
                f"NOT in the enum above. Pick strictly from the enum."
            )
        except JSONParseError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="json_error").inc()
            nudge = (
                "You MUST call the record_intent tool with VALID JSON arguments. "
                "Do not return any text response — only the tool call."
            )
        except SchemaValidationError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="schema_error").inc()
            nudge = (
                "Your previous attempt was missing required fields or had wrong types. "
                "Required: title (str), agent (one from enum), prompt (str), confidence (0.0-1.0)."
            )
        except LLMTimeoutError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="timeout").inc()
            nudge = None  # pure retry; prompt was fine
        except LLMTransportError as e:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="transport").inc()
            log.warning("intent attempt 1 transport error: %s", e)
            nudge = None  # pure retry
        except Exception as e:  # noqa: BLE001
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="1", outcome="unknown").inc()
            log.warning("intent attempt 1 unexpected error: %s", e)
            nudge = None

        # --- Attempt 2: same model, optional nudge, longer timeout ---
        sys_for_attempt_2 = base_system
        if nudge:
            sys_for_attempt_2 = base_system + "\n\nIMPORTANT FOR THIS RETRY: " + nudge
        try:
            parsed, usage, resp_model = await self._call_llm(
                input, capabilities,
                system_prompt=sys_for_attempt_2,
                timeout=self._t2,
            )
            outcome = self._classify(parsed)
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome=outcome).inc()
            return ParseOutcome(
                parsed=parsed, result=outcome, attempts=2,
                last_usage=usage, last_model=resp_model,
            )
        except InvalidAgentError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="invalid_agent").inc()
        except JSONParseError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="json_error").inc()
        except SchemaValidationError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="schema_error").inc()
        except LLMTimeoutError:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="timeout").inc()
        except LLMTransportError as e:
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="transport").inc()
            log.warning("intent attempt 2 transport error: %s", e)
        except Exception as e:  # noqa: BLE001
            INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="2", outcome="unknown").inc()
            log.warning("intent attempt 2 unexpected error: %s", e)

        # --- Attempt 3: heuristic, never raises ---
        return self._heuristic(
            input,
            capabilities=capabilities,
            reason="LLM failed twice; heuristic fallback",
        )

    @staticmethod
    def _classify(parsed: ParsedIntent) -> Literal["high_conf", "low_conf"]:
        return "high_conf" if parsed.confidence >= HIGH_CONFIDENCE_THRESHOLD else "low_conf"

    async def _call_llm(
        self,
        input: IntentInput,
        capabilities: list[dict[str, str]],
        *,
        system_prompt: str,
        timeout: float,
    ) -> tuple[ParsedIntent, dict | None, str | None]:
        """Single LLM round-trip + parse + validate. Raises ParseError subclass
        on each failure mode so the caller can route by error type."""
        tool = _build_intent_tool(capabilities)
        # P7: when the user attached files, wrap raw_input in an XML
        # envelope that explicitly names the attachments. Plain-text
        # appended hints get ignored ("no attachments provided" still
        # comes back); XML inside the user turn is treated as ground
        # truth and the LLM rewrites the prompt to cite each path.
        if input.attachments:
            file_lines = "\n".join(
                f"  <file path=\".taskdeck/inputs/{name}\"/>"
                for name in input.attachments
            )
            user_content = (
                "<attachments>\n"
                f"{file_lines}\n"
                "</attachments>\n"
                f"<request>{input.raw_input}</request>"
            )
        else:
            user_content = input.raw_input
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            with LLM_CALL_DURATION_SECONDS.labels(kind="intent", provider="bedrock").time():
                raw = await asyncio.wait_for(
                    self._client.create_completion(
                        model=self._model,
                        messages=messages,
                        tools=[tool],
                        tool_choice={"type": "function", "function": {"name": "record_intent"}},
                    ),
                    timeout=timeout,
                )
        except TimeoutError as e:
            raise LLMTimeoutError(f"LLM call exceeded {timeout}s") from e
        except Exception as e:
            raise LLMTransportError(str(e)) from e

        usage = raw.get("usage")
        resp_model = raw.get("model")
        usage_dict: dict | None = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }

        args_str = raw.get("arguments", "")
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError) as e:
            raise JSONParseError(f"tool args not valid JSON: {e}") from e
        _coerce_args(args)
        try:
            parsed = ParsedIntent.model_validate(args)
        except Exception as e:  # pydantic ValidationError, etc.
            raise SchemaValidationError(str(e)) from e

        valid_agents = {c["capability"] for c in capabilities}
        if parsed.agent not in valid_agents:
            raise InvalidAgentError(parsed.agent)

        return parsed, usage_dict, resp_model

    @staticmethod
    def _heuristic(
        input: IntentInput,
        *,
        capabilities: list[dict[str, str]],
        reason: str,
    ) -> ParseOutcome:
        """Terminal layer — guarantees an answer without invoking the LLM.

        Routing rules mirror the system prompt; this is the offline equivalent.
        """
        raw = (input.raw_input or "").strip()
        known = {c["capability"] for c in capabilities}
        lowered = raw.lower()

        # Explicit Kiro/AWS Q signal first — overrides shell heuristic for
        # natural-language input. We don't want "use kiro to echo hi" to land
        # in shell.
        explicit_kiro = (
            "kiro-cli" in known
            and any(needle in lowered for needle in (
                "用 kiro", "用kiro", "kiro-cli", "aws q", "走 aws", "走aws", "use kiro",
            ))
        )
        # "Looks like a shell command": single short ASCII line, no question
        # mark, no CJK characters.
        looks_like_shell = (
            len(raw) <= 200
            and "\n" not in raw
            and not any(ord(c) > 127 for c in raw)
            and not raw.endswith("?")
        )

        if explicit_kiro:
            agent = "kiro-cli"
        elif looks_like_shell and "shell" in known:
            agent = "shell"
        elif "claude-code" in known:
            agent = "claude-code"
        elif "shell" in known:
            agent = "shell"
        else:
            # Truly nothing connected; return shell as a syntactic placeholder.
            # The caller will reject the task before dispatch (P4.3 gate).
            agent = "shell"

        INTENT_PARSE_ATTEMPTS_TOTAL.labels(attempt="3", outcome="heuristic").inc()
        return ParseOutcome(
            parsed=ParsedIntent(
                title=(raw[:80] or "untitled"),
                agent=agent,
                repo=None,
                base_branch=None,
                priority="normal",
                prompt=raw,
                confidence=0.3,
                confidence_reasons=[f"heuristic fallback: {reason}"],
            ),
            result="heuristic",
            attempts=3,
            last_usage=None,
            last_model=None,
        )
