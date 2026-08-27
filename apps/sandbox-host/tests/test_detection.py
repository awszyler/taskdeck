"""Tests for sandbox_host.detection (P6.4 manifest model).

Strategy: pure unit tests against tmp_path workspaces. Two surfaces:

  detect_outputs(workspace)  -> list[Output]   (new, primary)
  detect(workspace)          -> SandboxSpec     (legacy single-output)

Tests cover:
  - manifest path: explicit kinds + invalid entries skipped
  - fallback: html / md / image / data / archive
  - runtime detection inside fallback (node, python)
  - empty workspace returns []
  - missing dir handled gracefully
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sandbox_host.detection import (
    DetectionError,
    Output,
    SandboxSpec,
    detect,
    detect_outputs,
)


def _write(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------- detect_outputs: manifest path -----------------------------


def test_manifest_with_interactive_html(tmp_path):
    _write(tmp_path / "counter.html", "<h1>x</h1>")
    _write(
        tmp_path / ".taskdeck" / "output.yml",
        "outputs:\n"
        "  - kind: interactive\n"
        "    entry: counter.html\n"
        "    label: 计数器\n",
    )
    outputs = detect_outputs(tmp_path)
    assert len(outputs) == 1
    o = outputs[0]
    assert o.kind == "interactive"
    assert o.entry == "counter.html"
    assert o.label == "计数器"
    assert o.source == "manifest"
    # Static runtime inferred from .html.
    assert o.runtime == "static"
    assert o.port == 8080


def test_manifest_with_explicit_runtime(tmp_path):
    _write(tmp_path / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
    _write(
        tmp_path / ".taskdeck" / "output.yml",
        "outputs:\n"
        "  - kind: interactive\n"
        "    entry: main.py\n"
        "    runtime: python\n"
        "    install: pip install fastapi uvicorn\n"
        "    start: uvicorn main:app --host 0.0.0.0 --port 7000\n"
        "    port: 7000\n",
    )
    outputs = detect_outputs(tmp_path)
    assert len(outputs) == 1
    assert outputs[0].port == 7000
    assert outputs[0].install_cmd == "pip install fastapi uvicorn"


def test_manifest_with_multiple_kinds(tmp_path):
    _write(tmp_path / "demo.html", "<h1>x</h1>")
    _write(tmp_path / "README.md", "# notes")
    _write(tmp_path / "out.csv", "a,b\n1,2\n")
    _write(
        tmp_path / ".taskdeck" / "output.yml",
        "outputs:\n"
        "  - kind: interactive\n"
        "    entry: demo.html\n"
        "  - kind: document\n"
        "    entry: README.md\n"
        "  - kind: data\n"
        "    entry: out.csv\n",
    )
    outputs = detect_outputs(tmp_path)
    kinds = [o.kind for o in outputs]
    assert kinds == ["interactive", "document", "data"]


def test_manifest_skips_invalid_entries(tmp_path):
    _write(tmp_path / "good.html", "<h1>x</h1>")
    _write(
        tmp_path / ".taskdeck" / "output.yml",
        "outputs:\n"
        "  - kind: nonsense\n"
        "    entry: good.html\n"
        "  - kind: interactive\n"
        "    entry: missing-file.html\n"
        "  - kind: interactive\n"
        "    entry: good.html\n",
    )
    outputs = detect_outputs(tmp_path)
    # Only the good one remains.
    assert len(outputs) == 1
    assert outputs[0].entry == "good.html"


def test_manifest_path_traversal_blocked(tmp_path):
    _write(tmp_path / ".taskdeck" / "output.yml",
           "outputs:\n  - kind: document\n    entry: ../../../etc/passwd\n")
    outputs = detect_outputs(tmp_path)
    assert outputs == []


def test_manifest_malformed_yaml_falls_back(tmp_path):
    _write(tmp_path / "demo.html", "<h1>x</h1>")
    _write(tmp_path / ".taskdeck" / "output.yml", "::: not yaml :::\n")
    # Falls back to scanning. demo.html should still be picked up.
    outputs = detect_outputs(tmp_path)
    assert any(o.entry == "demo.html" for o in outputs)


# ---------- detect_outputs: fallback heuristics -----------------------


def test_fallback_finds_html(tmp_path):
    _write(tmp_path / "counter.html", "<h1>x</h1>")
    outputs = detect_outputs(tmp_path)
    assert len(outputs) == 1
    o = outputs[0]
    assert o.kind == "interactive"
    assert o.entry == "counter.html"
    assert o.runtime == "static"


def test_fallback_finds_multiple_html(tmp_path):
    _write(tmp_path / "counter.html", "<h1>x</h1>")
    _write(tmp_path / "clock.html", "<h1>y</h1>")
    outputs = detect_outputs(tmp_path)
    assert {o.entry for o in outputs} == {"counter.html", "clock.html"}
    assert all(o.kind == "interactive" for o in outputs)


def test_fallback_node_runtime(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    outputs = detect_outputs(tmp_path)
    interactive = [o for o in outputs if o.kind == "interactive"]
    assert len(interactive) == 1
    assert interactive[0].runtime == "node"
    assert interactive[0].port == 5173


def test_fallback_node_uses_npm_ci_with_lockfile(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    _write(tmp_path / "package-lock.json", "{}")
    outputs = detect_outputs(tmp_path)
    interactive = next(o for o in outputs if o.kind == "interactive")
    assert interactive.install_cmd == "npm ci"


def test_fallback_python_fastapi(tmp_path):
    _write(tmp_path / "requirements.txt", "fastapi\n")
    _write(tmp_path / "main.py", "x = 1")
    outputs = detect_outputs(tmp_path)
    interactive = next(o for o in outputs if o.kind == "interactive")
    assert interactive.runtime == "python"
    assert "uvicorn" in interactive.start_cmd


def test_fallback_finds_markdown(tmp_path):
    _write(tmp_path / "README.md", "# docs")
    _write(tmp_path / "notes.md", "more")
    outputs = detect_outputs(tmp_path)
    docs = [o for o in outputs if o.kind == "document"]
    assert {o.entry for o in docs} == {"README.md", "notes.md"}


def test_fallback_finds_images(tmp_path):
    _write(tmp_path / "diagram.png", "fakepng")
    _write(tmp_path / "screenshot.jpg", "fakejpg")
    outputs = detect_outputs(tmp_path)
    images = [o for o in outputs if o.kind == "image"]
    assert {o.entry for o in images} == {"diagram.png", "screenshot.jpg"}


def test_fallback_finds_data(tmp_path):
    _write(tmp_path / "results.csv", "a,b\n1,2\n")
    _write(tmp_path / "log.json", "[1,2,3]")
    outputs = detect_outputs(tmp_path)
    data = [o for o in outputs if o.kind == "data"]
    assert {o.entry for o in data} == {"results.csv", "log.json"}


def test_fallback_finds_archives(tmp_path):
    _write(tmp_path / "package.zip", "PK\x03\x04...")
    outputs = detect_outputs(tmp_path)
    archives = [o for o in outputs if o.kind == "archive"]
    assert len(archives) == 1
    assert archives[0].entry == "package.zip"


def test_fallback_skips_dependency_lock_files(tmp_path):
    """package.json + lock files shouldn't appear as data outputs."""
    _write(tmp_path / "package.json", "{}")
    _write(tmp_path / "package-lock.json", "{}")
    _write(tmp_path / "uv.lock", "x")
    outputs = detect_outputs(tmp_path)
    # No outputs of kind=data should reference these.
    data_entries = {o.entry for o in outputs if o.kind == "data"}
    assert "package.json" not in data_entries
    assert "package-lock.json" not in data_entries


def test_fallback_mixed_html_and_md(tmp_path):
    """Common pattern: agent writes a demo + a README explaining it."""
    _write(tmp_path / "demo.html", "<h1>x</h1>")
    _write(tmp_path / "README.md", "# how it works")
    outputs = detect_outputs(tmp_path)
    kinds = sorted(o.kind for o in outputs)
    # demo.html → interactive, README.md → document.
    assert kinds == ["document", "interactive"]


# ---------- empty / missing -------------------------------------------


def test_empty_workspace_returns_empty_list(tmp_path):
    outputs = detect_outputs(tmp_path)
    assert outputs == []


def test_missing_workspace_returns_empty_list(tmp_path):
    bogus = tmp_path / "no-such-dir"
    outputs = detect_outputs(bogus)
    assert outputs == []


# ---------- legacy detect() backcompat --------------------------------


def test_legacy_detect_returns_first_interactive(tmp_path):
    _write(tmp_path / "demo.html", "<h1>x</h1>")
    _write(tmp_path / "README.md", "# notes")
    spec = detect(tmp_path)
    assert isinstance(spec, SandboxSpec)
    assert spec.runtime == "static"
    assert spec.port == 8080


def test_legacy_detect_raises_when_no_interactive(tmp_path):
    _write(tmp_path / "README.md", "only docs")
    with pytest.raises(DetectionError):
        detect(tmp_path)


def test_legacy_detect_raises_on_empty(tmp_path):
    with pytest.raises(DetectionError):
        detect(tmp_path)
