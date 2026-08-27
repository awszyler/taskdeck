from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from taskdeck_runner.workspace import Workspace

if TYPE_CHECKING:
    from pathlib import Path


async def _run(cmd: list[str], cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, f"{cmd} failed: {err.decode()}"


@pytest.fixture
async def local_origin(tmp_path: Path) -> str:
    """Create a bare 'origin' repo with one initial commit on main."""
    origin = tmp_path / "origin.git"
    await _run(["git", "init", "--bare", "-b", "main", str(origin)], cwd=tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    await _run(["git", "init", "-b", "main"], cwd=seed)
    await _run(["git", "config", "user.email", "t@t"], cwd=seed)
    await _run(["git", "config", "user.name", "t"], cwd=seed)
    (seed / "README.md").write_text("hello\n")
    await _run(["git", "add", "."], cwd=seed)
    await _run(["git", "commit", "-m", "init"], cwd=seed)
    await _run(["git", "remote", "add", "origin", str(origin)], cwd=seed)
    await _run(["git", "push", "origin", "main"], cwd=seed)

    return str(origin)


@pytest.mark.asyncio
async def test_workspace_clean_exit_retains_worktree(local_origin: str, tmp_path: Path):
    """P6.3.3: workspaces are now retained on clean exit so the
    sandbox feature can mount them later. LRU GC in sandbox-host
    handles long-term cleanup (30 days)."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-1", repo=local_origin, base_branch="main"
    ) as ws:
        assert ws.cwd.exists()
        assert (ws.cwd / "README.md").read_text() == "hello\n"
    # NEW behavior: worktree retained for sandbox/audit access.
    assert ws.cwd.exists(), "worktree should be retained for sandbox use"


@pytest.mark.asyncio
async def test_workspace_captures_diff_artifact(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    workspace = Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-2", repo=local_origin, base_branch="main"
    )
    async with workspace as ws:
        (ws.cwd / "README.md").write_text("hello\nworld\n")
    arts = workspace.artifacts()
    kinds = [a.kind for a in arts]
    assert "git-diff" in kinds
    diff_art = next(a for a in arts if a.kind == "git-diff")
    assert b"world" in diff_art.data


@pytest.mark.asyncio
async def test_workspace_no_repo_retains_dir(tmp_path: Path):
    """P6.3.3: same retention semantics for no-repo tasks."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-3", repo=None, base_branch="main"
    ) as ws:
        assert ws.cwd.exists()
        assert ws.cwd.parent.name == "tasks"
        assert ws.cwd.parent.parent.name == "test"  # the slug
    # Retained — sandbox-host's LRU GC removes it when stale.
    assert ws.cwd.exists()


@pytest.mark.asyncio
async def test_workspace_keeps_on_exception(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    workspace = Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-4", repo=local_origin, base_branch="main"
    )
    with pytest.raises(RuntimeError):
        async with workspace as ws:
            assert ws.cwd.exists()
            raise RuntimeError("boom")
    assert workspace.cwd.exists(), "worktree should be kept for postmortem on exception"


@pytest.mark.asyncio
async def test_workspace_writes_fallback_manifest_when_agent_didnt(tmp_path: Path):
    """P6.4.S8: when the agent didn't write .taskdeck/output.yml,
    the runner should generate one on exit so sandbox-host always
    sees a manifest. Uses no-repo path for simplicity."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-manifest",
        repo=None, base_branch="main",
    ) as ws:
        # Simulate agent output: a single .html demo.
        (ws.cwd / "counter.html").write_text("<h1>x</h1>")

    manifest = ws.cwd / ".taskdeck" / "output.yml"
    assert manifest.exists()
    text = manifest.read_text()
    assert "counter.html" in text
    assert "interactive" in text


@pytest.mark.asyncio
async def test_workspace_does_not_overwrite_agent_manifest(tmp_path: Path):
    """If the agent already wrote a manifest, runner must NOT overwrite
    it (or even modify it). Round-trip equality check."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-explicit",
        repo=None, base_branch="main",
    ) as ws:
        (ws.cwd / "demo.html").write_text("<h1>x</h1>")
        (ws.cwd / ".taskdeck").mkdir(parents=True, exist_ok=True)
        agent_yml = (
            "outputs:\n"
            "  - kind: interactive\n"
            "    entry: demo.html\n"
            "    label: 我的演示\n"
        )
        (ws.cwd / ".taskdeck" / "output.yml").write_text(agent_yml)

    # Untouched.
    assert (ws.cwd / ".taskdeck" / "output.yml").read_text() == agent_yml


@pytest.mark.asyncio
async def test_workspace_skips_manifest_when_no_outputs(tmp_path: Path):
    """Empty workspace → no manifest is written (nothing to declare)."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="t-empty",
        repo=None, base_branch="main",
    ) as ws:
        pass

    assert not (ws.cwd / ".taskdeck" / "output.yml").exists()


@pytest.mark.asyncio
async def test_collect_project_context_taskdeck_first(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="ctx-1", repo=local_origin, base_branch="main",
    ) as ws:
        (ws.cwd / ".taskdeck").mkdir(parents=True, exist_ok=True)
        (ws.cwd / ".taskdeck" / "CONTEXT.md").write_text("ccpt context\n", encoding="utf-8")
        (ws.cwd / "AGENTS.md").write_text("agents content\n", encoding="utf-8")
        (ws.cwd / "CLAUDE.md").write_text("claude content\n", encoding="utf-8")

        got = ws.collect_project_context()
        assert got is not None
        path, text = got
        assert path == ".taskdeck/CONTEXT.md"
        assert text == "ccpt context\n"


@pytest.mark.asyncio
async def test_collect_project_context_agents_md_when_ccpt_absent(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="ctx-2", repo=local_origin, base_branch="main",
    ) as ws:
        (ws.cwd / "AGENTS.md").write_text("agents content\n", encoding="utf-8")
        (ws.cwd / "CLAUDE.md").write_text("claude content\n", encoding="utf-8")

        got = ws.collect_project_context()
        assert got is not None
        path, text = got
        assert path == "AGENTS.md"
        assert text == "agents content\n"


@pytest.mark.asyncio
async def test_collect_project_context_claude_md_last_fallback(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="ctx-3", repo=local_origin, base_branch="main",
    ) as ws:
        (ws.cwd / "CLAUDE.md").write_text("claude content\n", encoding="utf-8")

        got = ws.collect_project_context()
        assert got is not None
        path, text = got
        assert path == "CLAUDE.md"
        assert text == "claude content\n"


@pytest.mark.asyncio
async def test_collect_project_context_absent_returns_none(local_origin: str, tmp_path: Path):
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="ctx-4", repo=local_origin, base_branch="main",
    ) as ws:
        assert ws.collect_project_context() is None


@pytest.mark.asyncio
async def test_collect_project_context_no_repo(tmp_path: Path):
    """Tasks without a repo should also return None (no files in tmp cwd)."""
    work_dir = tmp_path / "td-work"
    async with Workspace(
        work_dir=work_dir, workspace_slug="test", task_id="ctx-5", repo=None, base_branch="main",
    ) as ws:
        assert ws.collect_project_context() is None


def test_workspace_path_includes_slug(tmp_path: Path):
    """Workspace cwd is work_dir/<slug>/tasks/<task_id>."""
    ws = Workspace(
        work_dir=tmp_path,
        workspace_slug="alpha",
        task_id="t1",
        repo=None,
    )
    assert ws.cwd == tmp_path / "alpha" / "tasks" / "t1"
    assert ws.repos_dir == tmp_path / "alpha" / "repos"


def test_workspace_invalid_slug_raises(tmp_path: Path):
    """Slugs that fail the regex are rejected at constructor time."""
    for bad in ["..", "foo/bar", "WithCaps", "-dash", "with space", ""]:
        with pytest.raises(ValueError, match="invalid workspace_slug"):
            Workspace(
                work_dir=tmp_path,
                workspace_slug=bad,
                task_id="t1",
                repo=None,
            )


def test_workspace_two_slugs_isolated(tmp_path: Path):
    """Two workspaces never share filesystem state under work_dir."""
    ws_a = Workspace(work_dir=tmp_path, workspace_slug="alpha", task_id="t1", repo=None)
    ws_b = Workspace(work_dir=tmp_path, workspace_slug="beta", task_id="t1", repo=None)
    # Same task_id under different slugs → DIFFERENT cwds.
    assert ws_a.cwd != ws_b.cwd
    assert ws_a.cwd == tmp_path / "alpha" / "tasks" / "t1"
    assert ws_b.cwd == tmp_path / "beta" / "tasks" / "t1"
