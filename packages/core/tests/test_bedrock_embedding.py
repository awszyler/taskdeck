"""Test BedrockEmbeddingClient with a mocked boto3 client."""
from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

import pytest
from taskdeck_core.memory.embedding import BedrockEmbeddingClient


@pytest.mark.asyncio
async def test_cohere_embedding_round_trip():
    client = BedrockEmbeddingClient(model_id="cohere.embed-multilingual-v3", region="ap-northeast-1")
    fake_boto = MagicMock()
    fake_response = {
        "body": MagicMock(read=lambda: _json.dumps({"embeddings": [[0.1] * 1024]}).encode())
    }
    fake_boto.invoke_model.return_value = fake_response
    client._client = fake_boto

    out = await client.embed_batch(["hello"])
    assert len(out) == 1
    assert len(out[0]) == 1024
    assert out[0][0] == 0.1
    fake_boto.invoke_model.assert_called_once()
    call_args = fake_boto.invoke_model.call_args
    body = _json.loads(call_args.kwargs["body"])
    assert body["texts"] == ["hello"]
    assert body["input_type"] == "search_document"


@pytest.mark.asyncio
async def test_cohere_search_query_input_type():
    """Retrieval calls must pass input_type=search_query — Cohere v3 vectors are not symmetric."""
    client = BedrockEmbeddingClient(model_id="cohere.embed-multilingual-v3", region="ap-northeast-1")
    fake_boto = MagicMock()
    fake_boto.invoke_model.return_value = {
        "body": MagicMock(read=lambda: _json.dumps({"embeddings": [[0.2] * 1024]}).encode())
    }
    client._client = fake_boto

    await client.embed_batch(["pnpm version"], input_type="search_query")
    body = _json.loads(fake_boto.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_query"


@pytest.mark.asyncio
async def test_titan_embedding_shape():
    client = BedrockEmbeddingClient(model_id="amazon.titan-embed-text-v2:0", region="ap-northeast-1")
    fake_boto = MagicMock()
    fake_boto.invoke_model.return_value = {
        "body": MagicMock(read=lambda: _json.dumps({"embedding": [0.5] * 1024}).encode())
    }
    client._client = fake_boto
    out = await client.embed_batch(["hi"])
    assert len(out) == 1
    body = _json.loads(fake_boto.invoke_model.call_args.kwargs["body"])
    assert "inputText" in body


@pytest.mark.asyncio
async def test_unknown_model_id_raises():
    client = BedrockEmbeddingClient(model_id="nonsense", region="ap-northeast-1")
    fake_boto = MagicMock()
    client._client = fake_boto
    with pytest.raises(RuntimeError, match="unknown"):
        await client.embed_batch(["x"])


@pytest.mark.asyncio
async def test_empty_batch_returns_empty():
    client = BedrockEmbeddingClient(model_id="cohere.embed-multilingual-v3", region="ap-northeast-1")
    fake_boto = MagicMock()
    client._client = fake_boto
    out = await client.embed_batch([])
    assert out == []
    fake_boto.invoke_model.assert_not_called()
