from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

log = logging.getLogger(__name__)


class AgentCoreExecutor:
    """Invokes an Amazon Bedrock AgentCore agent.

    One instance per agent_id. Capability string is `agentcore-<agent_id>`.
    Strategy: request-response. Output chunks are sliced into ~2 KB stdout
    lines so the UI sees streaming-ish progress.

    The full text is also accumulated and made available via
    `last_full_output` for artifact collection by the runner.
    """

    CHUNK_SIZE = 2048

    def __init__(
        self,
        *,
        agent_id: str,
        region: str,
        agent_alias_id: str = "TSTALIASID",
        client: Any = None,
    ):
        self._agent_id = agent_id
        self._region = region
        self._agent_alias_id = agent_alias_id
        self._client = client
        self.last_full_output: str = ""

    def _ensure_client(self) -> Any:
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-agent-runtime", region_name=self._region)
        return self._client

    async def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        # AgentCore ignores cwd — sessions are fresh sandboxes.
        client = self._ensure_client()
        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.invoke_agent(
                    agentId=self._agent_id,
                    agentAliasId=self._agent_alias_id,
                    sessionId=task_id,
                    inputText=prompt,
                ),
            )
        except Exception as e:  # noqa: BLE001
            self.last_full_output = ""
            yield "stderr", f"agentcore invoke failed: {type(e).__name__}: {e}"
            yield "finish", "1"
            return

        # invoke_agent returns an EventStream-like iterable.
        # boto3's synchronous client buffers the entire stream on read; we drain it
        # in a thread so the event loop is not blocked.
        def _drain() -> list[str]:
            chunks: list[str] = []
            completion = resp.get("completion", [])
            for event in completion:
                chunk = event.get("chunk", {})
                data = chunk.get("bytes", b"")
                if data:
                    chunks.append(data.decode("utf-8", errors="replace"))
            return chunks

        try:
            all_chunks = await loop.run_in_executor(None, _drain)
        except Exception as e:  # noqa: BLE001
            self.last_full_output = ""
            yield "stderr", f"agentcore read failed: {e}"
            yield "finish", "1"
            return

        full: list[str] = []
        for raw in all_chunks:
            full.append(raw)
            for i in range(0, len(raw), self.CHUNK_SIZE):
                piece = raw[i : i + self.CHUNK_SIZE]
                yield "stdout", piece

        self.last_full_output = "".join(full)
        yield "finish", "0"

    def summary(self) -> str | None:
        """Return the Bedrock agent's final answer for use as Task.summary.

        The whole completion is stored in last_full_output. We trim whitespace
        and return the tail (in case the agent leads with prefatory text);
        500 chars matches the runner's stdout-tail fallback.
        """
        if not self.last_full_output:
            return None
        text = self.last_full_output.strip()
        if not text:
            return None
        return text[-500:] if len(text) > 500 else text
