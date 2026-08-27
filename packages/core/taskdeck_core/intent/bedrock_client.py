"""Anthropic-on-Bedrock LLM client for the Intent Parser.

Bedrock's native Anthropic Messages API uses a different schema than OpenAI
chat.completions: tools live at the top level (not nested under `function`),
and a tool call appears as `content[].type == "tool_use"` rather than
`message.tool_calls[]`. This client adapts both sides so `IntentParser` keeps
its existing OpenAI-style `LLMClient` protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging

from taskdeck_core.metrics.registry import LLM_CALL_DURATION_SECONDS

log = logging.getLogger(__name__)


class BedrockIntentClient:
    """Calls Anthropic models via Bedrock's invoke_model.

    The model_id should be an inference profile id like
    `us.anthropic.claude-opus-4-7` (cross-region) or a direct model id.
    """

    def __init__(self, *, model_id: str, region: str):
        self._model_id = model_id
        self._region = region
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    async def create_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: dict,
    ) -> dict:
        # Translate OpenAI-style tool definitions to Anthropic schema.
        # OpenAI: {"type":"function","function":{"name":..,"description":..,"parameters":{...}}}
        # Anthropic: {"name":..,"description":..,"input_schema":{...}}
        anth_tools = []
        for t in tools:
            fn = t.get("function") or t
            anth_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn["parameters"],
            })

        # Anthropic Messages API splits system out from messages.
        system_text = ""
        anth_messages: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_text = m.get("content", "")
            else:
                anth_messages.append({
                    "role": role,
                    "content": m.get("content", ""),
                })

        # tool_choice: OpenAI sends {"type":"function","function":{"name":N}}.
        # Anthropic accepts {"type":"tool","name":N} for forced single-tool.
        anth_tool_choice = {"type": "tool", "name": tool_choice["function"]["name"]} \
            if isinstance(tool_choice, dict) and tool_choice.get("type") == "function" \
            else {"type": "auto"}

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": system_text,
            "messages": anth_messages,
            "tools": anth_tools,
            "tool_choice": anth_tool_choice,
        }

        client = self._ensure_client()
        loop = asyncio.get_running_loop()

        def _call() -> dict:
            resp = client.invoke_model(
                modelId=self._model_id,
                body=json.dumps(body),
            )
            return json.loads(resp["body"].read())

        with LLM_CALL_DURATION_SECONDS.labels(kind="intent", provider="bedrock").time():
            payload = await loop.run_in_executor(None, _call)

        # Find the first tool_use block.
        for block in payload.get("content", []):
            if block.get("type") == "tool_use":
                return {
                    "arguments": json.dumps(block["input"]),
                    "usage": payload.get("usage"),
                    "model": payload.get("model", self._model_id),
                }
        raise RuntimeError("Bedrock Anthropic response had no tool_use block")
