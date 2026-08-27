"""Tests for /manifest /file /tree (P6.4.S2 viewer endpoints)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sandbox_host.main import create_app
from sandbox_host.settings import SandboxHostSettings


@pytest.fixture
def settings(tmp_path):
    return SandboxHostSettings(  # type: ignore[call-arg]
        TD_SBH_WORK_DIR=str(tmp_path / "work"),
        TD_SBH_CONTAINER_RUNTIME="runc",
        TD_SBH_SELF_CONTAINER_NAME="",
    )


def _seed(work_dir: Path, slug: str, task_id: str) -> Path:
    p = Path(work_dir) / slug / "tasks" / task_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_manifest_returns_outputs(settings):
    work = Path(settings.work_dir)
    ws = _seed(work, "demo", "t1")
    (ws / "counter.html").write_text("<h1>x</h1>")
    (ws / "README.md").write_text("# x")

    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/manifest/demo/t1")

    assert r.status_code == 200
    body = r.json()
    kinds = sorted(o["kind"] for o in body["outputs"])
    assert kinds == ["document", "interactive"]


def test_manifest_missing_workspace_returns_404(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/manifest/no-such-slug/no-such-task")
    assert r.status_code == 404
    assert "workspace" in r.json()["detail"].lower()


def test_manifest_empty_workspace_returns_empty_outputs(settings):
    work = Path(settings.work_dir)
    _seed(work, "demo", "t-empty")
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/manifest/demo/t-empty")
    assert r.status_code == 200
    assert r.json()["outputs"] == []


def test_file_returns_markdown_with_correct_mime(settings):
    work = Path(settings.work_dir)
    ws = _seed(work, "demo", "t-md")
    (ws / "README.md").write_text("# hello\n", encoding="utf-8")

    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/file/demo/t-md/README.md")

    assert r.status_code == 200
    assert "markdown" in r.headers["content-type"]
    assert r.text == "# hello\n"


def test_file_blocks_path_traversal(settings):
    work = Path(settings.work_dir)
    _seed(work, "demo", "t-trav")
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/file/demo/t-trav/../../escape.txt")
    assert r.status_code in (400, 404)


def test_file_404_on_missing(settings):
    work = Path(settings.work_dir)
    _seed(work, "demo", "t-miss")
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/file/demo/t-miss/no-such-file.md")
    assert r.status_code == 404


def test_tree_returns_directory_listing(settings):
    work = Path(settings.work_dir)
    ws = _seed(work, "demo", "t-tree")
    (ws / "a.html").write_text("x")
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_text("y")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "skipme.js").write_text("z")

    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/tree/demo/t-tree")

    assert r.status_code == 200
    paths = {e["path"] for e in r.json()["entries"]}
    assert "a.html" in paths
    assert "src" in paths
    assert "src/main.py" in paths
    # Skipped.
    assert "node_modules" not in paths
    assert "node_modules/skipme.js" not in paths
