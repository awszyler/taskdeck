from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class AgentRuntime(Protocol):
    """The interface an agent implementation must satisfy.

    Emits ("stdout" | "stderr", str) lines during execution, then one final
    ("finish", str(exit_code)).
    """

    def run(
        self, *, task_id: str, prompt: str, cwd: Path | None = None
    ) -> AsyncIterator[tuple[str, str]]: ...
