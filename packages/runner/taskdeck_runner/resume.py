"""Resumed-prompt construction for tasks that pause via <ccpt:ask>.

When a task transitions awaiting_input -> pending and is re-dispatched,
the runner uses build_resumed_prompt to create a single string that
contains the original task, the conversation so far, and a directive
to continue (or ask again).

We don't trust the executor to maintain its own session — claude --print
is one-shot — so every resume is a fresh process with the full
context inlined into the prompt.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class _TurnLike(Protocol):
    role: str
    content: str


def build_resumed_prompt(original: str, turns: Sequence[_TurnLike]) -> str:
    """Build the prompt string for a resumed task.

    If turns is empty, returns the original prompt unchanged (the
    dispatcher already prepended the ccpt:ask protocol header upstream).
    Otherwise, returns a fresh prompt that includes:
    - original task
    - conversation history (agent-question / user-answer alternating)
    - a directive to continue or ask again

    The original prompt is INCLUDED inside this resume prompt; the
    dispatcher's protocol header is not — the dispatcher doesn't know
    a task has prior_turns at the time it wraps the prompt, so the
    runner has to take ownership of the full prompt shape on resume.
    """
    if not turns:
        return original

    parts = [
        "You are continuing a previous task. Context follows.",
        "",
        f"Original task: {original}",
        "",
        "Conversation history:",
    ]
    for turn in turns:
        if turn.role == "agent":
            parts.append(f"You previously asked: {turn.content}")
        elif turn.role == "user":
            parts.append(f"User answered: {turn.content}")
        else:
            # Defensive — proto validates role, but just in case.
            parts.append(f"({turn.role}): {turn.content}")
    parts.append("")
    parts.append(
        "Continue the task. If you still need clarifying information from "
        "the user, output <ccpt:ask>question</ccpt:ask> and exit. Otherwise, "
        "complete the task as instructed."
    )
    return "\n".join(parts)
