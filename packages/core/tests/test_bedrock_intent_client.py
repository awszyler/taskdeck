"""Test BedrockIntentClient with a mocked boto3 bedrock-runtime client."""
from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

import pytest
from taskdeck_core.intent.bedrock_client import BedrockIntentClient


@pytest.mark.asyncio
async def test_translates_openai_tool_format_to_anthropic():
    client = BedrockIntentClient(model_id="us.anthropic.claude-opus-4-7", region="us-east-1")
    fake_boto = MagicMock()
    fake_boto.invoke_model.return_value = {
        "body": MagicMock(read=lambda: _json.dumps({
            "model": "claude-opus-4-7",
            "content": [
                {
                    "type": "tool_use",
                    "name": "record_intent",
                    "input": {"title": "x", "agent": "claude-code", "prompt": "p", "confidence": 0.9},
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }).encode())
    }
    client._client = fake_boto

    out = await client.create_completion(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": "you parse intents"},
            {"role": "user", "content": "do thing"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "record_intent",
                "description": "Record an intent.",
                "parameters": {"type": "object", "properties": {"agent": {"type": "string"}}},
            },
        }],
        tool_choice={"type": "function", "function": {"name": "record_intent"}},
    )

    args = _json.loads(out["arguments"])
    assert args["agent"] == "claude-code"

    sent_body = _json.loads(fake_boto.invoke_model.call_args.kwargs["body"])
    # System split out from messages
    assert sent_body["system"] == "you parse intents"
    assert len(sent_body["messages"]) == 1
    assert sent_body["messages"][0]["role"] == "user"
    # Tool schema flattened: no nested 'function' key
    assert sent_body["tools"][0]["name"] == "record_intent"
    assert "input_schema" in sent_body["tools"][0]
    # tool_choice translated
    assert sent_body["tool_choice"] == {"type": "tool", "name": "record_intent"}


@pytest.mark.asyncio
async def test_raises_when_no_tool_use_block():
    client = BedrockIntentClient(model_id="us.anthropic.claude-opus-4-7", region="us-east-1")
    fake_boto = MagicMock()
    fake_boto.invoke_model.return_value = {
        "body": MagicMock(read=lambda: _json.dumps({
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": "I refuse"}],
        }).encode())
    }
    client._client = fake_boto

    with pytest.raises(RuntimeError, match="no tool_use"):
        await client.create_completion(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"function": {"name": "record_intent", "parameters": {"type": "object"}}}],
            tool_choice={"type": "function", "function": {"name": "record_intent"}},
        )
