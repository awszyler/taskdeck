from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentContext(BaseModel):
    recent_repos: list[str] = Field(default_factory=list)
    known_agents: list[str] = Field(default_factory=lambda: ["shell", "claude-code"])
    default_base_branch: str = "main"
    user_timezone: str = "Asia/Shanghai"


class IntentInput(BaseModel):
    raw_input: str = Field(min_length=1)
    hint: Literal["voice", "text", "im"] = "text"
    context: IntentContext | None = None
    # P7: filenames the user uploaded with the task. The parser uses
    # these to disambiguate vague prompts like "总结文件" (which would
    # otherwise look unsure → draft) and to bias toward agents that
    # can actually read files (claude-code).
    attachments: list[str] = Field(default_factory=list)


class ParsedIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    description: str | None = None
    agent: str  # "shell" | "claude-code" | future values
    repo: str | None = None
    base_branch: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    prompt: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reasons: list[str] = Field(default_factory=list)
