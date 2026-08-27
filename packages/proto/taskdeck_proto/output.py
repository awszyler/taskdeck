"""Workspace Output model + detection (P6.4).

Lives in proto so runner and sandbox-host can both use it. The
runner uses detect_outputs() at task-done time to write a fallback
.taskdeck/output.yml when the agent didn't write one. The
sandbox-host uses the same function as a defensive fallback if
neither agent nor runner wrote one (e.g. workspaces produced by
older runner versions).

Pure module: no I/O outside the workspace path; YAML is the only
external dependency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

OutputKind = Literal["interactive", "document", "code", "data", "image", "archive"]
Runtime = Literal["static", "node", "python"]


@dataclass(frozen=True)
class Output:
    """One viewable artifact in a task's workspace."""
    kind: OutputKind
    entry: str
    label: str = ""
    source: str = ""

    runtime: Runtime | None = None
    install_cmd: str | None = None
    start_cmd: str | None = None
    port: int | None = None


_MANIFEST_PATH = Path(".taskdeck/output.yml")
_VALID_KINDS: set[OutputKind] = {
    "interactive", "document", "code", "data", "image", "archive",
}
_VALID_RUNTIMES: set[Runtime] = {"static", "node", "python"}

_FALLBACK_IGNORE = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt",
    ".taskdeck", ".git", ".gitignore", ".env", ".env.example",
    "node_modules", "__pycache__", ".venv", "venv",
    "AGENTS.md", "CLAUDE.md",
})


def detect_outputs(workspace: Path) -> list[Output]:
    """Return outputs for a workspace. Never raises — empty list
    means 'nothing to view'."""
    if not workspace.is_dir():
        return []

    yml = _load_manifest(workspace)
    if yml is not None:
        return _parse_manifest(yml, workspace)

    return _fallback_scan(workspace)


def _load_manifest(workspace: Path) -> dict | None:
    candidate = workspace / _MANIFEST_PATH
    if not candidate.is_file():
        return None
    try:
        with candidate.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
    except (yaml.YAMLError, OSError):
        pass
    return None


def _parse_manifest(data: dict, workspace: Path) -> list[Output]:
    raw_outputs = data.get("outputs")
    if not isinstance(raw_outputs, list):
        return []

    out: list[Output] = []
    for item in raw_outputs:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in _VALID_KINDS:
            continue
        entry = item.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            continue
        full = (workspace / entry).resolve()
        try:
            full.relative_to(workspace.resolve())
        except ValueError:
            continue
        if not full.exists():
            continue

        label = item.get("label") if isinstance(item.get("label"), str) else ""

        runtime = None
        install_cmd = None
        start_cmd = None
        port = None
        if kind == "interactive":
            r = item.get("runtime")
            if r in _VALID_RUNTIMES:
                runtime = r
            i = item.get("install")
            if isinstance(i, str):
                install_cmd = i
            s = item.get("start")
            if isinstance(s, str):
                start_cmd = s
            p = item.get("port")
            if isinstance(p, int) and 1 <= p <= 65535:
                port = p
            if runtime is None and start_cmd is None:
                inferred = _infer_interactive_for_entry(workspace, entry)
                if inferred is None:
                    continue
                runtime = inferred[0]
                install_cmd = install_cmd or inferred[1]
                start_cmd = start_cmd or inferred[2]
                port = port or inferred[3]

        out.append(Output(
            kind=kind,  # type: ignore[arg-type]
            entry=entry,
            label=label,
            source="manifest",
            runtime=runtime,
            install_cmd=install_cmd,
            start_cmd=start_cmd,
            port=port,
        ))
    return out


def _fallback_scan(workspace: Path) -> list[Output]:
    outputs: list[Output] = []

    interactive = _scan_runtime_interactive(workspace)
    if interactive is not None:
        outputs.append(interactive)

    if interactive is None or interactive.runtime == "static":
        for html in sorted(workspace.glob("*.html")):
            if any(o.entry == html.name for o in outputs):
                continue
            outputs.append(Output(
                kind="interactive",
                entry=html.name,
                label=html.stem,
                source="auto:html",
                runtime="static",
                install_cmd=None,
                start_cmd="nginx -g 'daemon off;'",
                port=8080,
            ))

    for ext in ("md", "markdown", "txt", "pdf"):
        for f in sorted(workspace.glob(f"*.{ext}")):
            if f.name in _FALLBACK_IGNORE:
                continue
            outputs.append(Output(
                kind="document", entry=f.name, label=f.stem,
                source=f"auto:{ext}",
            ))
    for ext in ("png", "jpg", "jpeg", "svg", "gif", "webp"):
        for f in sorted(workspace.glob(f"*.{ext}")):
            outputs.append(Output(
                kind="image", entry=f.name, label=f.stem,
                source=f"auto:{ext}",
            ))
    for ext in ("csv", "tsv", "json", "jsonl", "ndjson"):
        for f in sorted(workspace.glob(f"*.{ext}")):
            if f.name in _FALLBACK_IGNORE:
                continue
            outputs.append(Output(
                kind="data", entry=f.name, label=f.stem,
                source=f"auto:{ext}",
            ))
    # `archive` = download-only kinds. Includes office formats (.pptx,
    # .docx, .xlsx) since they're binary blobs the SPA can't render
    # inline; the user just needs a Download button. The detector
    # picks them up here so OpenClaw / claude-code outputs both work
    # uniformly through the existing archive download path.
    for ext in ("zip", "tar", "tar.gz", "tgz", "pptx", "docx", "xlsx"):
        for f in sorted(workspace.glob(f"*.{ext}")):
            outputs.append(Output(
                kind="archive", entry=f.name, label=f.stem,
                source=f"auto:{ext}",
            ))
    return outputs


def _scan_runtime_interactive(workspace: Path) -> Output | None:
    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") or {}
        for script_name in ("dev", "start", "serve"):
            if script_name in scripts:
                install = (
                    "npm ci" if (workspace / "package-lock.json").is_file()
                    else "npm install"
                )
                port = (
                    5173 if "vite" in (scripts.get(script_name, "")).lower()
                    else 3000
                )
                return Output(
                    kind="interactive", entry="package.json",
                    label="dev server", source="auto:node",
                    runtime="node", install_cmd=install,
                    start_cmd=f"npm run {script_name}", port=port,
                )
        if "build" in scripts:
            install = (
                "npm ci" if (workspace / "package-lock.json").is_file()
                else "npm install"
            )
            return Output(
                kind="interactive", entry="package.json",
                label="static build", source="auto:node-build",
                runtime="node",
                install_cmd=f"{install} && npm run build",
                start_cmd="npx --yes serve -s dist -l 3000", port=3000,
            )

    has_reqs = (workspace / "requirements.txt").is_file()
    has_pyproject = (workspace / "pyproject.toml").is_file()
    if has_reqs or has_pyproject:
        install = (
            "pip install -r requirements.txt" if has_reqs
            else "pip install -e ."
        )
        if (workspace / "main.py").is_file():
            return Output(
                kind="interactive", entry="main.py", label="API",
                source="auto:python", runtime="python",
                install_cmd=install,
                start_cmd="uvicorn main:app --host 0.0.0.0 --port 8000",
                port=8000,
            )
        if (workspace / "app.py").is_file():
            return Output(
                kind="interactive", entry="app.py", label="API",
                source="auto:python", runtime="python",
                install_cmd=install,
                start_cmd="flask --app app run --host 0.0.0.0 --port 8000",
                port=8000,
            )

    return None


def _infer_interactive_for_entry(
    workspace: Path, entry: str,
) -> tuple[Runtime, str | None, str, int] | None:
    name = entry.lower()
    if name.endswith(".html") or name.endswith(".htm"):
        return ("static", None, "nginx -g 'daemon off;'", 8080)
    if name == "package.json":
        rt = _scan_runtime_interactive(workspace)
        if rt is not None and rt.runtime == "node":
            return ("node", rt.install_cmd, rt.start_cmd or "npm run dev",
                    rt.port or 3000)
    if name in ("main.py", "app.py", "requirements.txt", "pyproject.toml"):
        rt = _scan_runtime_interactive(workspace)
        if rt is not None and rt.runtime == "python":
            return ("python", rt.install_cmd,
                    rt.start_cmd or "uvicorn main:app --host 0.0.0.0 --port 8000",
                    rt.port or 8000)
    return None


def write_manifest(workspace: Path, outputs: list[Output]) -> Path:
    """Serialize outputs as YAML to .taskdeck/output.yml.

    Used by the runner to write a system-generated manifest when the
    agent didn't write one. Idempotent — overwrites existing files.
    Returns the written path.
    """
    target = workspace / _MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for o in outputs:
        row: dict = {"kind": o.kind, "entry": o.entry}
        if o.label:
            row["label"] = o.label
        if o.kind == "interactive":
            if o.runtime:
                row["runtime"] = o.runtime
            if o.install_cmd:
                row["install"] = o.install_cmd
            if o.start_cmd and o.runtime != "static":
                # static doesn't need an explicit start (image's
                # default nginx CMD handles it).
                row["start"] = o.start_cmd
            if o.port:
                row["port"] = o.port
        rows.append(row)

    payload = {
        "_generated_by": "taskdeck-runner",
        "outputs": rows,
    }
    with target.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
    return target
