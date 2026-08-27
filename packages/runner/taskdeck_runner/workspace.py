from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

CONTEXT_FILE_CANDIDATES = (".taskdeck/CONTEXT.md", "AGENTS.md", "CLAUDE.md")

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class ArtifactPayload:
    """An artifact produced during task execution, ready to be uploaded."""

    kind: str  # "git-diff" | "git-branch"
    data: bytes  # raw content
    meta: dict[str, str]  # e.g. {"branch": "taskdeck/<task_id>"}


def _slug_for_repo(repo: str) -> str:
    """Stable filesystem-safe slug. 'github.com/org/name' → 'github_com_org_name' plus short hash."""
    cleaned = repo.replace("/", "_").replace(":", "_").replace(".", "_")
    digest = hashlib.sha256(repo.encode()).hexdigest()[:8]
    return f"{cleaned}-{digest}"


async def _git(cwd: Path, *args: str, check: bool = True) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr}")
    return proc.returncode or 0, stdout, stderr


class Workspace:
    """Per-task filesystem workspace.

    Use as an async context manager:
        async with Workspace(...) as ws:
            # ws.cwd is the directory to run the agent in
            await executor.run(..., cwd=ws.cwd)
        # on successful __aexit__, artifacts are collected and worktree removed.
        # on exception, worktree is kept for postmortem.

    artifacts() returns the collected artifacts AFTER __aexit__ has run.
    """

    def __init__(
        self,
        *,
        work_dir: Path,
        workspace_slug: str,
        task_id: str,
        repo: str | None,
        base_branch: str = "main",
    ):
        if not _SLUG_RE.match(workspace_slug):
            raise ValueError(
                f"invalid workspace_slug {workspace_slug!r}: "
                "must match ^[a-z0-9][a-z0-9_-]{0,63}$"
            )
        self._work_dir = work_dir
        self._workspace_slug = workspace_slug
        self._task_id = task_id
        self._repo = repo
        self._base_branch = base_branch
        self._artifacts: list[ArtifactPayload] = []
        self._keep_on_exit = False

    @property
    def slug_dir(self) -> Path:
        """Per-workspace root: work_dir/<workspace_slug>/."""
        return self._work_dir / self._workspace_slug

    @property
    def repos_dir(self) -> Path:
        return self.slug_dir / "repos"

    @property
    def workspaces_dir(self) -> Path:
        return self.slug_dir / "tasks"

    @property
    def bare_path(self) -> Path | None:
        if not self._repo:
            return None
        return self.repos_dir / _slug_for_repo(self._repo)

    @property
    def cwd(self) -> Path:
        return self.workspaces_dir / self._task_id

    @property
    def branch(self) -> str:
        return f"taskdeck/{self._task_id}"

    async def __aenter__(self) -> Workspace:
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir.mkdir(parents=True, exist_ok=True)

        if self._repo is None:
            self.cwd.mkdir(parents=True, exist_ok=True)
            return self

        await self._ensure_bare_clone()
        await self._create_worktree()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        # P6.3.3: workspaces retained for sandbox use; LRU GC in
        # sandbox-host handles 30-day cleanup.
        if self._repo is not None:
            await self._collect_artifacts()

        # P6.4.S8: write a fallback manifest if the agent didn't.
        # Runs on clean exit AND on exception — even a partly-failed
        # task may have produced viewable artifacts the user wants
        # to inspect via Open. We swallow errors here because the
        # task's primary outcome should never be blocked by manifest
        # write trouble.
        try:
            self._ensure_manifest()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "task %s: failed to write fallback manifest: %s",
                self._task_id, e,
            )

        # Dependency injection: capture the manifest as an artifact so
        # child tasks (those that depend on this one) get the parent's
        # output list in their dispatch envelope. Without this, deps
        # injection only conveys git-diff/branch/decision — agents
        # producing PPT / markdown / data have no way to tell a child
        # what they made or where to find it.
        try:
            self._collect_manifest_artifact()
        except Exception as e:  # noqa: BLE001
            log.warning(
                "task %s: failed to capture manifest artifact: %s",
                self._task_id, e,
            )

        if exc_type is not None:
            log.info(
                "task %s exited with %s; worktree retained at %s",
                self._task_id, exc_type.__name__, self.cwd,
            )

    def _collect_manifest_artifact(self) -> None:
        """Append .taskdeck/output.yml as an `output-manifest` artifact
        so the dependency injector can hand it to child tasks. Runner's
        crp_client uploads any artifacts in `self._artifacts` after
        task completion, the same path git-diff/branch use."""
        manifest_path = self.cwd / ".taskdeck" / "output.yml"
        if not manifest_path.is_file():
            return
        try:
            data = manifest_path.read_bytes()
        except OSError:
            return
        if not data:
            return
        self._artifacts.append(
            ArtifactPayload(
                kind="output-manifest",
                data=data,
                # Path on the runner host — child tasks can compute
                # `../<parent_task_id>/` themselves; meta is for the
                # injector to pass any structured hints in future.
                meta={"task_id": self._task_id},
            )
        )

    def _ensure_manifest(self) -> None:
        """Write .taskdeck/output.yml if it doesn't exist already.

        The agent is taught (via dispatcher's prompt header) to write
        the manifest itself. This is the safety net: if it didn't,
        we scan the workspace and write a system-generated one so
        sandbox-host always reads from the same source of truth.
        """
        from taskdeck_proto.output import (
            detect_outputs,
            write_manifest,
        )

        manifest_path = self.cwd / ".taskdeck" / "output.yml"
        if manifest_path.exists():
            log.info(
                "task %s: agent-supplied manifest at %s",
                self._task_id, manifest_path,
            )
            return

        if not self.cwd.is_dir():
            return

        outputs = detect_outputs(self.cwd)
        if not outputs:
            log.info(
                "task %s: no outputs detected; skipping manifest",
                self._task_id,
            )
            return

        written = write_manifest(self.cwd, outputs)
        log.info(
            "task %s: wrote fallback manifest with %d output(s) at %s",
            self._task_id, len(outputs), written,
        )

    def keep(self) -> None:
        """Caller can mark the workspace as 'keep even on clean exit' (e.g. on TaskFailed)."""
        self._keep_on_exit = True

    def collect_project_context(self) -> tuple[str, str] | None:
        """Return (file_path_relative_to_cwd, text_content) if a known context file exists, else None."""
        for candidate in CONTEXT_FILE_CANDIDATES:
            path = self.cwd / candidate
            if path.is_file():
                try:
                    return candidate, path.read_text(encoding="utf-8")
                except OSError:
                    continue
        return None

    def artifacts(self) -> list[ArtifactPayload]:
        return list(self._artifacts)

    def add_artifact(self, artifact: ArtifactPayload) -> None:
        """Allow callers to append artifacts produced outside the workspace's
        built-in git collection (e.g. AgentCore `decision` payloads)."""
        self._artifacts.append(artifact)

    # --- private ---

    async def _ensure_bare_clone(self) -> None:
        assert self.bare_path is not None
        if self.bare_path.exists():
            # Fetch latest for the base branch; ignore errors for offline/local repos.
            try:
                await _git(
                    self.bare_path,
                    "fetch",
                    "--force",
                    "origin",
                    f"{self._base_branch}:{self._base_branch}",
                )
            except RuntimeError as e:
                log.warning("bare clone fetch failed: %s", e)
            return
        # First-time clone.
        url = self._normalize_repo_url(self._repo or "")
        await _git(self.repos_dir, "clone", "--bare", url, str(self.bare_path))

    @staticmethod
    def _normalize_repo_url(repo: str) -> str:
        """Normalize 'github.com/org/name' to a clonable URL.

        Absolute filesystem paths and already-prefixed URLs are passed through unchanged.
        """
        if (
            repo.startswith("http://")
            or repo.startswith("https://")
            or repo.startswith("git@")
            or repo.startswith("/")
        ):
            return repo
        return f"https://{repo}.git" if not repo.endswith(".git") else f"https://{repo}"

    async def _create_worktree(self) -> None:
        assert self.bare_path is not None
        if self.cwd.exists():
            # Stale — remove first.
            shutil.rmtree(self.cwd, ignore_errors=True)
        # In a bare clone, branches are local heads (refs/heads/<branch>), not
        # remote-tracking refs (origin/<branch>).  Use the local branch name.
        await _git(
            self.bare_path,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.cwd),
            self._base_branch,
        )

    async def _remove_worktree(self) -> None:
        assert self.bare_path is not None
        # `git worktree remove --force` cleans metadata AND the directory.
        try:
            await _git(self.bare_path, "worktree", "remove", "--force", str(self.cwd))
        except RuntimeError as e:
            log.warning("worktree remove failed, falling back to rmtree: %s", e)
            shutil.rmtree(self.cwd, ignore_errors=True)
        # Also delete the branch ref.
        with contextlib.suppress(RuntimeError):
            await _git(self.bare_path, "branch", "-D", self.branch)

    async def _collect_artifacts(self) -> None:
        # git-diff
        rc, out, _ = await _git(self.cwd, "diff", "HEAD", check=False)
        if rc == 0 and out.strip():
            self._artifacts.append(
                ArtifactPayload(
                    kind="git-diff",
                    data=out.encode(),
                    meta={"branch": self.branch},
                )
            )
        # git-branch: only record if the branch moved forward from the base branch.
        # In a bare clone, branches are local heads (not remote-tracking refs).
        rc, head_sha, _ = await _git(self.cwd, "rev-parse", "HEAD", check=False)
        rc2, base_sha, _ = await _git(
            self.cwd, "rev-parse", self._base_branch, check=False
        )
        if rc == 0 and rc2 == 0 and head_sha.strip() != base_sha.strip():
            self._artifacts.append(
                ArtifactPayload(
                    kind="git-branch",
                    data=self.branch.encode(),
                    meta={"head": head_sha.strip(), "base": base_sha.strip()},
                )
            )
