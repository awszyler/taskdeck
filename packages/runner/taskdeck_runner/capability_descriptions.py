"""Build the capability_descriptions dict the runner sends in Hello.

Descriptions are short (≤ 200 chars) so the intent parser's system prompt
stays compact. Phrasing is chosen so an LLM matching tasks against
descriptions can route by content fit (especially for agentcore agents).
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import RunnerSettings

log = logging.getLogger(__name__)

SHELL = (
    "Single shell command executor. Runs the prompt verbatim as bash and "
    "streams stdout/stderr. No LLM, no git worktree, no reasoning."
)

CLAUDE_CODE = (
    "General-purpose AI coding agent (Anthropic-backed). Operates in a "
    "per-task git worktree, can read and write any file in the repo, "
    "produces a git diff. Default for any non-trivial coding work, "
    "debugging, or refactoring."
)

KIRO_CLI = (
    "General-purpose AI coding agent on AWS Q. Same capabilities as "
    "claude-code (worktree, file edits, git diff). Pick this ONLY when "
    "the user explicitly mentions Kiro, AWS Q, or wants AWS-flavored tooling."
)

OPENCLAW = (
    "Multi-channel personal AI agent (openclaw.ai). Pre-configured with "
    "Feishu/WeCom integrations, skills, and identity. Best for chat-style "
    "tasks: querying IMs, sending messages, reading docs, or routing through "
    "the user's pre-set agent persona. Not a coding-first agent."
)

HERMES = (
    "Hermes Agent (Nous Research) — open-source AI assistant with tool "
    "calling, MCP, skills, and multi-provider model support. General-purpose: "
    "coding, research, ad-hoc tasks. Pick when user mentions Hermes, Nous, "
    "or wants a non-Anthropic / non-AWS coding agent."
)

CODEX = (
    "Codex CLI (OpenAI) — coding agent with bash, file edit, and patch tools. "
    "Same shape as claude-code (worktree, file edits, narrative summary). "
    "Pick this ONLY when the user explicitly mentions Codex, OpenAI, or GPT."
)


def build_descriptions(settings: RunnerSettings) -> dict[str, str]:
    out: dict[str, str] = {"shell": SHELL}
    if settings.claude_code_bin:
        out["claude-code"] = CLAUDE_CODE
    if settings.kiro_cli_bin:
        out["kiro-cli"] = KIRO_CLI
    if settings.openclaw_bin:
        out["openclaw"] = OPENCLAW
    if settings.hermes_bin:
        out["hermes"] = HERMES
    if settings.codex_bin:
        out["codex"] = CODEX
    if settings.agentcore_enabled:
        agent_descs: dict[str, str] = {}
        try:
            agent_descs = json.loads(settings.agentcore_agent_descriptions or "{}")
        except json.JSONDecodeError:
            log.warning(
                "TD_AGENTCORE_AGENT_DESCRIPTIONS is not valid JSON; falling back to defaults"
            )
        for agent_id in settings.agentcore_agent_ids:
            cap = f"agentcore-{agent_id}"
            out[cap] = agent_descs.get(
                agent_id,
                f"AWS Bedrock AgentCore agent {agent_id} (no description configured).",
            )
    return out
