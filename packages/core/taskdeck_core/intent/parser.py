from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Protocol

from taskdeck_core.metrics.registry import LLM_CALL_DURATION_SECONDS

from .schema import IntentContext, IntentInput, ParsedIntent

log = logging.getLogger(__name__)


def _build_intent_tool(capabilities: list[dict[str, str]]) -> dict:
    """Build the record_intent tool schema with a dynamic agent enum.

    capabilities is a list of {"capability": str, "description": str}.
    The enum is exactly the set of currently connected runtimes — never
    hard-coded. This way the parser cannot recommend an offline runtime.
    """
    enum_values = [c["capability"] for c in capabilities]
    return {
        "type": "function",
        "function": {
            "name": "record_intent",
            "description": (
                "Record the structured task intent extracted from the user's "
                "request. MUST be called exactly once per parse."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "A SHORT headline for the kanban card, ≤ 60 chars. "
                            "Summarize the request like a news headline — do NOT "
                            "paste the user's full text. E.g. for a long trip-"
                            "planning message, title='优化北海道行程安排'."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One or two sentences (≤ 280 chars) elaborating on "
                            "the task — the context a card tooltip would show. "
                            "Distinct from title (headline) and prompt (the "
                            "runtime instruction). Optional; omit only when the "
                            "title already says everything."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "enum": enum_values,
                        "description": (
                            "Pick exactly one runtime from the enum. The enum "
                            "lists every runtime currently connected. Inventing "
                            "values is a hard error."
                        ),
                    },
                    "repo": {
                        "type": ["string", "null"],
                        "description": "Repository path or URL. null when no repo is needed.",
                    },
                    "base_branch": {
                        "type": ["string", "null"],
                        "description": "Base branch for the worktree. null = use default.",
                    },
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "prompt": {
                        "type": "string",
                        "description": "The instruction that will be sent to the runtime.",
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "confidence_reasons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short notes on what drove the confidence score.",
                    },
                },
                "required": ["title", "agent", "prompt", "confidence"],
            },
        },
    }


def _build_system_prompt(
    ctx: IntentContext,
    capabilities: list[dict[str, str]],
) -> str:
    """Build the system prompt with the runtime catalogue inlined."""
    lines = [
        "You convert user requests into structured task specs for a developer",
        "task runner. Call the record_intent tool EXACTLY ONCE. If the request",
        "contains multiple sub-tasks, capture them in a single record_intent",
        "with a combined prompt — never split into multiple tool calls.",
        "",
        "Available runtimes (pick exactly one):",
        "",
    ]
    for cap in capabilities:
        desc = cap.get("description") or "(no description provided)"
        lines.append(f'  - "{cap["capability"]}": {desc}')
    lines.extend([
        "",
        "Routing rules (apply in order, stop at the first that fits):",
        "",
        '  1. If the request is a single shell command line (echo / ls / curl /',
        '     git fetch / docker run / awk etc.) AND can be expressed verbatim',
        '     as bash, pick "shell".',
        "",
        "  2. Otherwise, examine each agentcore-* runtime above. If the request",
        "     closely matches that runtime's description (deployment task vs an",
        "     AWS-deploy agent, customer-support question vs a CS agent, etc.),",
        "     pick that agentcore-X and quote the matching phrase from its",
        "     description in confidence_reasons.",
        "",
        '  3. claude-code is the default for everything else: coding, debugging,',
        "     research, document/PPT generation, translation, knowledge questions,",
        '     file edits, etc. Even if a description for openclaw / hermes / kiro-cli',
        '     / codex looks plausible, prefer claude-code unless rule 4 fires.',
        "",
        '  4. Pick a non-default agent (openclaw / hermes / kiro-cli / codex / etc.)',
        "     ONLY when the user explicitly names that agent or its provider",
        '     ("用 openclaw 给我...", "via Hermes", "with Kiro", "use AWS Q",',
        '     "with Codex", "用 OpenAI / GPT").',
        "     A topic match alone is NOT enough — claude-code can also query IMs,",
        "     read docs, and chat. Without an explicit user request, stay with",
        '     claude-code.',
        "",
        "  5. The agent value MUST come from the enum above. Inventing values",
        '     (e.g. "frontend_developer", "devops_engineer") is a hard error.',
        "     If no rule clearly fits, pick claude-code and set confidence below 0.5.",
        "",
        "Confidence:",
        "  - 0.85+ when rule 1, 2, or 4 fires on an unambiguous explicit signal.",
        "  - 0.7-0.85 when rule 3 fires (default claude-code) on a clear request.",
        "  - Below 0.5 when the input is vague.",
        "",
        "  Note: when the user message is wrapped as",
        "  <attachments><file path=\"…\"/>…</attachments><request>…</request>,",
        "  the listed files ARE on disk in the task's working directory. The",
        "  agent will be able to Read them. Treat the file paths as the",
        "  referent for vague phrases like 'the file' or '附件' inside",
        "  <request>; the file question is no longer ambiguous. Rewrite",
        "  the prompt to cite each path explicitly (e.g. \"读取并总结",
        "  `.taskdeck/inputs/notes.pdf`\"). Confidence still reflects the",
        "  rest of the request: if the action is unclear (e.g. user just",
        "  sends 'this file'), confidence stays low and the user reviews.",
        "",
        f"Recent repos in this workspace: {', '.join(ctx.recent_repos) or '(none)'}",
        f"Default base branch: {ctx.default_base_branch}",
        "",
        "Other guidelines:",
        "  - If the user names a repo, match it to one in the recent list when possible.",
        "  - prompt is the direct instruction for the runtime; it can expand on the raw input.",
        '  - For agent=shell, prompt MUST be a valid shell command (no leading "run ",',
        '    "execute ", etc.). E.g. "run echo hi" → prompt="echo hi".',
        "  - For coding agents (claude-code / kiro-cli), prompt should preserve the user's",
        "    full intent, including any deployment, testing, or follow-up steps.",
        "",
        "Title vs description vs prompt — keep these distinct:",
        "  - title: ≤ 60 char headline. NEVER the raw user text. Summarize.",
        "  - description: 1-2 sentences of context (≤ 280 chars). Optional.",
        "  - prompt: the full instruction the runtime executes.",
        "  Example — user sends a 400-char message reworking a Hokkaido",
        "  itinerary with train times and lavender farms:",
        '    title="优化北海道富良野行程"',
        '    description="按用户给的 JR 时刻表重排 7/18 当天，纳入薰衣草专列，'
        '避免行程过赶。"',
        "    prompt=<the full reworked-itinerary instruction, verbatim intent>",
    ])
    return "\n".join(lines)


def _parse_xml_tool_call(content: str) -> dict | None:
    """Parse the Qwen3 XML-style tool call format from response content.

    Handles: <function=name><parameter=key>value</parameter>...</function>
    Returns a dict of parameter name -> parsed value, or None if format not recognized.
    """
    m = re.search(r"<function=\w+>(.*?)</function>", content, re.DOTALL)
    if not m:
        return None
    body = m.group(1)
    params: dict = {}
    for pm in re.finditer(r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", body, re.DOTALL):
        key, raw = pm.group(1), pm.group(2).strip()
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            # Try as float/int before falling back to string
            try:
                params[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                params[key] = raw
    return params if params else None


TITLE_MAX = 60
DESCRIPTION_MAX = 280


def _clip(text: str, limit: int) -> str:
    """Clip text to AT MOST `limit` chars INCLUDING the ellipsis. The DB
    columns are exactly TITLE_MAX / DESCRIPTION_MAX wide, so the ellipsis
    must fit inside the budget — not be appended past it."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _derive_title_description(raw: str) -> tuple[str, str | None]:
    """Heuristic title+description from raw text, for the fallback paths
    where the LLM didn't produce a structured parse.

    title: first line (or first sentence) clipped to TITLE_MAX.
    description: a longer clip of the remaining text, or None when the
    raw input already fits in the title.
    """
    raw = (raw or "").strip()
    if not raw:
        return "untitled", None
    # Prefer the first line as the headline.
    title = _clip(raw.splitlines()[0], TITLE_MAX)
    # Description only adds value when raw has more than the title shows.
    description: str | None = None
    if len(raw) > len(title):
        description = _clip(raw, DESCRIPTION_MAX)
    return title, description


def _coerce_args(args: dict) -> None:
    """Coerce fields that Pydantic expects as list but the model may return as a string,
    and defend against the model pasting a long prompt into title."""
    reasons = args.get("confidence_reasons")
    if isinstance(reasons, str):
        args["confidence_reasons"] = [r.strip() for r in reasons.split(",") if r.strip()]
    # Defensive clamp: if the model ignored the ≤60 instruction and put a
    # wall of text in title, move the overflow into description rather than
    # letting it hit the DB column limit or render as a giant card title.
    title = args.get("title")
    if isinstance(title, str) and len(title) > TITLE_MAX:
        overflow = title.strip()
        args["title"] = _clip(overflow, TITLE_MAX)
        if not args.get("description"):
            args["description"] = _clip(overflow, DESCRIPTION_MAX)
    # Even when title is fine, a model can overshoot the description budget.
    desc = args.get("description")
    if isinstance(desc, str) and len(desc) > DESCRIPTION_MAX:
        args["description"] = _clip(desc, DESCRIPTION_MAX)


class LLMClient(Protocol):
    """Minimal surface needed by the parser. Lets tests inject a fake."""

    async def create_completion(
        self, *, model: str, messages: list[dict], tools: list[dict], tool_choice: dict
    ) -> dict: ...


class _MissingLLMClient:
    async def create_completion(self, **kwargs) -> dict:
        raise RuntimeError("LITELLM endpoint not configured")


class OpenAIClient:
    """Thin wrapper around AsyncOpenAI chat.completions.create, so the parser
    code is independent of the SDK's concrete types (easier to fake in tests)."""

    def __init__(self, *, base_url: str, api_key: str):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def create_completion(
        self, *, model: str, messages: list[dict], tools: list[dict], tool_choice: dict
    ) -> dict:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice=tool_choice,  # type: ignore[arg-type]
        )
        msg = resp.choices[0].message
        usage = resp.usage
        tool_calls = msg.tool_calls or []
        if tool_calls:
            tc = tool_calls[0]
            return {
                "arguments": tc.function.arguments,  # type: ignore[union-attr]
                "usage": usage,
                "model": resp.model,
            }
        # Some models (e.g. Qwen3) return tool calls in content as:
        # <function=name><parameter=key>value</parameter>...</function>
        if msg.content:
            args = _parse_xml_tool_call(msg.content)
            if args is not None:
                return {"arguments": json.dumps(args), "usage": usage, "model": resp.model}
        raise RuntimeError("LLM returned no tool_calls")


class IntentParser:
    def __init__(self, *, client: LLMClient, model: str, timeout: float):
        self._client = client
        self._model = model
        self._timeout = timeout
        # Best-effort usage capture for cost accounting (may be None if not supported).
        self.last_usage: dict | None = None
        self.last_model: str | None = None

    async def parse(
        self,
        input: IntentInput,
        *,
        available_capabilities: list[dict[str, str]] | None = None,
    ) -> ParsedIntent:
        caps = available_capabilities or []
        if not caps:
            log.warning("intent parse called with no available_capabilities; LLM not invoked")
            self.last_usage = None
            self.last_model = None
            _t, _d = _derive_title_description(input.raw_input)
            return ParsedIntent(
                title=_t,
                description=_d,
                agent="shell",
                repo=None,
                base_branch=None,
                priority="normal",
                prompt=input.raw_input,
                confidence=0.0,
                confidence_reasons=["no runners connected; cannot route"],
            )

        tool = _build_intent_tool(caps)
        ctx = input.context if input.context is not None else IntentContext()
        # P7: when the user attached files, structure the user message
        # with an XML <attachments> tag wrapping the raw_input. We tried
        # appended-text and system-prompt-tail variants first; Bedrock
        # Claude reliably ignored both and reported "no attachments".
        # Wrapping the raw_input INSIDE an XML envelope that explicitly
        # names the attachments makes them part of the user turn the
        # model can't elide.
        if input.attachments:
            file_lines = "\n".join(
                f"  <file path=\".taskdeck/inputs/{name}\"/>"
                for name in input.attachments
            )
            user_msg = (
                "<attachments>\n"
                f"{file_lines}\n"
                "</attachments>\n"
                f"<request>{input.raw_input}</request>"
            )
        else:
            user_msg = input.raw_input
        messages = [
            {"role": "system", "content": _build_system_prompt(ctx, caps)},
            {"role": "user", "content": user_msg},
        ]
        try:
            with LLM_CALL_DURATION_SECONDS.labels(kind="intent", provider="litellm").time():
                raw = await asyncio.wait_for(
                    self._client.create_completion(
                        model=self._model,
                        messages=messages,
                        tools=[tool],
                        tool_choice={"type": "function", "function": {"name": "record_intent"}},
                    ),
                    timeout=self._timeout,
                )
            usage = raw.get("usage")
            resp_model = raw.get("model")
            if usage is not None:
                self.last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                }
                self.last_model = resp_model or self._model
            else:
                self.last_usage = None
                self.last_model = None
            args_str = raw.get("arguments", "")
            args = json.loads(args_str)
            _coerce_args(args)
            parsed = ParsedIntent.model_validate(args)
            # Defensive: if the model invents an agent value not in the enum,
            # downgrade confidence and surface the violation in reasons rather
            # than letting an undeliverable agent leak through.
            valid_agents = {c["capability"] for c in caps}
            if parsed.agent not in valid_agents:
                parsed = parsed.model_copy(update={
                    "agent": "shell",
                    "confidence": 0.0,
                    "confidence_reasons": (parsed.confidence_reasons or []) + [
                        f"LLM picked unknown agent '{parsed.agent}'; downgraded to shell",
                    ],
                })
            return parsed
        except Exception as e:  # noqa: BLE001
            log.warning("intent parse failed: %s (falling back)", e)
            self.last_usage = None
            self.last_model = None
            return self._fallback(input, reason=str(e), capabilities=caps)

    @staticmethod
    def _fallback(
        input: IntentInput,
        *,
        reason: str,
        capabilities: list[dict[str, str]] | None = None,
    ) -> ParsedIntent:
        # When the LLM call fails (timeout, 5xx, etc.) we have to guess the
        # agent ourselves. "shell" is the wrong default for anything but a
        # bare one-liner — it would mis-execute natural-language input as a
        # shell command and blow up. So:
        #   - If the input looks like a shell command (single short line, no
        #     CJK, no question marks), keep shell.
        #   - Otherwise prefer claude-code when it's a known capability.
        #     This way users get reasonable routing on Bedrock hiccups.
        caps = capabilities or []
        known = {c["capability"] for c in caps}
        raw = (input.raw_input or "").strip()
        looks_like_shell = (
            len(raw) <= 200
            and "\n" not in raw
            and not any(ord(c) > 127 for c in raw)
            and not raw.endswith("?")
        )
        agent = "shell" if looks_like_shell or "claude-code" not in known else "claude-code"
        _t, _d = _derive_title_description(raw)
        return ParsedIntent(
            title=_t,
            description=_d,
            agent=agent,
            repo=None,
            base_branch=None,
            priority="normal",
            prompt=raw,
            confidence=0.0,
            confidence_reasons=[f"parser unavailable: {reason or 'timeout'}"],
        )
