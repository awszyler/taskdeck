"""Detection of the <ccpt:ask>...</ccpt:ask> protocol tag in agent stdout.

Agents that opt in (claude-code, agentcore-*) are instructed via prompt
to emit <ccpt:ask>question</ccpt:ask> when they need user input. The
runner buffers full stdout, calls extract_ask after the agent exits,
and if a non-empty question is found, the task transitions to
awaiting_input instead of done.
"""
from __future__ import annotations

import re

_ASK_RE = re.compile(r"<ccpt:ask>(.*?)</ccpt:ask>", re.DOTALL)


def extract_ask(stdout: str) -> str | None:
    """Return the last non-empty <ccpt:ask> capture in stdout, else None.

    If multiple tags appear, the LAST one wins — earlier ones are treated
    as agent thinking out loud. Empty captures (whitespace-only) are
    ignored.
    """
    last_question: str | None = None
    for m in _ASK_RE.finditer(stdout):
        captured = m.group(1).strip()
        if captured:
            last_question = captured
    return last_question
