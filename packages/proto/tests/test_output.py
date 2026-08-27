"""Tests for taskdeck_proto.output (P6.4 manifest module).

The detect_outputs() side is exercised by sandbox_host's
test_detection.py — we only test write_manifest + roundtrip here
(the bits unique to proto).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from taskdeck_proto.output import Output, detect_outputs, write_manifest


def test_write_manifest_creates_taskdeck_dir(tmp_path):
    outputs = [
        Output(kind="interactive", entry="counter.html", label="counter",
               source="auto:html", runtime="static",
               start_cmd="nginx -g 'daemon off;'", port=8080),
        Output(kind="document", entry="README.md", label="readme",
               source="auto:md"),
    ]
    written = write_manifest(tmp_path, outputs)
    assert written == tmp_path / ".taskdeck" / "output.yml"
    assert written.exists()


def test_write_manifest_yaml_is_loadable(tmp_path):
    outputs = [
        Output(kind="interactive", entry="demo.html", label="demo",
               source="auto:html", runtime="static",
               start_cmd="nginx -g 'daemon off;'", port=8080),
    ]
    write_manifest(tmp_path, outputs)
    data = yaml.safe_load((tmp_path / ".taskdeck" / "output.yml").read_text())
    assert "outputs" in data
    assert data["outputs"][0]["entry"] == "demo.html"
    assert data["outputs"][0]["kind"] == "interactive"
    # Static runtime: we omit start_cmd in the written file because
    # the static image's nginx CMD handles it.
    assert "start" not in data["outputs"][0]


def test_write_manifest_keeps_python_start_cmd(tmp_path):
    outputs = [
        Output(kind="interactive", entry="main.py", label="API",
               source="auto:python", runtime="python",
               install_cmd="pip install -r requirements.txt",
               start_cmd="uvicorn main:app --host 0.0.0.0 --port 8000",
               port=8000),
    ]
    write_manifest(tmp_path, outputs)
    data = yaml.safe_load((tmp_path / ".taskdeck" / "output.yml").read_text())
    o = data["outputs"][0]
    assert o["runtime"] == "python"
    assert o["install"].startswith("pip install")
    assert "uvicorn" in o["start"]
    assert o["port"] == 8000


def test_roundtrip_detect_then_write_then_detect(tmp_path):
    """If we scan a workspace, write the manifest, and re-scan,
    detect_outputs should now read from the manifest (source=manifest)
    instead of running the fallback heuristic again."""
    (tmp_path / "counter.html").write_text("<h1>x</h1>")
    (tmp_path / "README.md").write_text("# x")

    first_pass = detect_outputs(tmp_path)
    assert all(o.source.startswith("auto:") for o in first_pass)

    write_manifest(tmp_path, first_pass)
    second_pass = detect_outputs(tmp_path)
    # Sources flipped to "manifest" because we now read from yml.
    assert all(o.source == "manifest" for o in second_pass)
    # Same entries (potentially different order — manifest preserves
    # what we wrote).
    assert {o.entry for o in second_pass} == {o.entry for o in first_pass}


def test_write_manifest_empty_list(tmp_path):
    """Empty outputs list still writes a valid (empty-outputs)
    manifest. detect_outputs reads it and returns []."""
    write_manifest(tmp_path, [])
    out = detect_outputs(tmp_path)
    assert out == []
