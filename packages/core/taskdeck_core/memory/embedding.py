from __future__ import annotations

import logging
from typing import Literal, Protocol

from taskdeck_core.metrics.registry import LLM_CALL_DURATION_SECONDS

log = logging.getLogger(__name__)

InputType = Literal["search_document", "search_query"]


class EmbeddingClient(Protocol):
    async def embed_batch(
        self, texts: list[str], *, input_type: InputType = "search_document"
    ) -> list[list[float]]: ...


class OpenAIEmbeddingClient:
    """Embedding client backed by the OpenAI embeddings API (or LiteLLM proxy)."""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model

    async def embed_batch(
        self, texts: list[str], *, input_type: InputType = "search_document"
    ) -> list[list[float]]:
        if not texts:
            return []
        with LLM_CALL_DURATION_SECONDS.labels(kind="embed", provider="litellm").time():
            resp = await self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]


class BedrockEmbeddingClient:
    """Direct Bedrock invoke_model client for embedding.

    Cohere embed v3 distinguishes ingestion (`search_document`) from retrieval
    (`search_query`). The vectors are NOT symmetric — querying with
    `search_document` yields nonsense ranking. Callers must pass the correct
    `input_type`.
    """

    def __init__(self, *, model_id: str, region: str) -> None:
        self._model_id = model_id
        self._region = region
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    async def embed_batch(
        self, texts: list[str], *, input_type: InputType = "search_document"
    ) -> list[list[float]]:
        if not texts:
            return []
        import asyncio
        import json as _json

        client = self._ensure_client()
        loop = asyncio.get_running_loop()

        def _call_one(text: str) -> list[float]:
            if self._model_id.startswith("cohere.embed"):
                body = _json.dumps({"texts": [text], "input_type": input_type})
                resp = client.invoke_model(modelId=self._model_id, body=body)
                payload = _json.loads(resp["body"].read())
                return payload["embeddings"][0]
            elif self._model_id.startswith("amazon.titan-embed"):
                body = _json.dumps({"inputText": text})
                resp = client.invoke_model(modelId=self._model_id, body=body)
                payload = _json.loads(resp["body"].read())
                return payload["embedding"]
            else:
                raise RuntimeError(f"unknown embedding model id: {self._model_id}")

        results: list[list[float]] = []
        with LLM_CALL_DURATION_SECONDS.labels(kind="embed", provider="bedrock").time():
            for t in texts:
                vec = await loop.run_in_executor(None, _call_one, t)
                results.append(vec)
        return results


class _MissingEmbeddingClient:
    """Fallback used when LiteLLM is not configured or memory is disabled."""

    async def embed_batch(
        self, texts: list[str], *, input_type: InputType = "search_document"
    ) -> list[list[float]]:
        raise RuntimeError("LiteLLM endpoint not configured for embedding")
