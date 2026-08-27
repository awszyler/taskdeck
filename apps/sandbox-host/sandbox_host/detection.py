"""Workspace output detection (P6.4 manifest model).

Pass-through to taskdeck_proto.output. We re-export here so
existing import sites don't break, and to provide the legacy
detect()/SandboxSpec compat shim sandbox-host's main.py still
uses internally.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Re-export the canonical model from proto so callers can import
# from sandbox_host.detection or from taskdeck_proto.output —
# they get the same types either way.
from taskdeck_proto.output import (
    Output,
    OutputKind,
    Runtime,
    detect_outputs,
)

__all__ = [
    "DetectionError",
    "Output",
    "OutputKind",
    "Runtime",
    "SandboxSpec",
    "detect",
    "detect_outputs",
]


# ---------- legacy single-result API ----------------------------------


class DetectionError(Exception):
    """Kept for backward compatibility with the old detect()."""


@dataclass(frozen=True)
class SandboxSpec:
    runtime: Runtime
    image_key: str
    install_cmd: str | None
    start_cmd: str
    port: int
    source: str


def detect(workspace: Path) -> SandboxSpec:
    """Old API — returns a single SandboxSpec or raises. Resolves to
    the first interactive output. Will be removed once main.py
    migrates to the manifest model."""
    if not workspace.is_dir():
        raise DetectionError(f"workspace path is not a directory: {workspace}")

    outputs = detect_outputs(workspace)
    interactive = next(
        (o for o in outputs if o.kind == "interactive"), None,
    )
    if interactive is None:
        raise DetectionError(
            f"no interactive output for {workspace}. "
            f"Found {len(outputs)} non-interactive output(s); "
            "use the new /manifest endpoint to view them."
        )

    return SandboxSpec(
        runtime=interactive.runtime or "static",  # type: ignore[arg-type]
        image_key=interactive.runtime or "static",
        install_cmd=interactive.install_cmd,
        start_cmd=interactive.start_cmd or "nginx -g 'daemon off;'",
        port=interactive.port or 8080,
        source=interactive.source,
    )
