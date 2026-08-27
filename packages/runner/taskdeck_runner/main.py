from __future__ import annotations

import asyncio
import logging
import os
import signal

from .crp_client import CRPClient
from .settings import RunnerSettings

log = logging.getLogger(__name__)


def _export_github_token(s: RunnerSettings) -> None:
    """Export TD_GITHUB_TOKEN as GH_TOKEN/GITHUB_TOKEN so every
    spawned agent (claude-code, codex, hermes, kiro, openclaw, shell)
    inherits it, then run `gh auth setup-git` so `git push` over
    https://github.com/... transparently uses the token via the gh
    credential helper.

    Setting on os.environ rather than per-spawn keeps the change
    contained — every executor uses default env inheritance via
    create_subprocess_exec, and we don't have to plumb env through
    five executors and the workspace context manager.

    Without this, agents can only use the read-only deploy SSH key,
    which blocks repo creation, pushes to new repos, deploy-key
    rotation, PR comments, and any GitHub API surface.
    """
    if not s.github_token:
        return
    # Preserve any pre-existing values (e.g. operator override in
    # systemd unit) — runner-level config is the default, not a
    # forced overwrite.
    os.environ.setdefault("GH_TOKEN", s.github_token)
    os.environ.setdefault("GITHUB_TOKEN", s.github_token)

    # Wire `gh` as git's credential helper for github.com once at
    # startup. Idempotent: re-running just rewrites the same git
    # config entry. Best-effort: if `gh` is not installed or this
    # fails, agents can still hit the GitHub API directly via
    # gh-binary-less paths or write the credential URL inline.
    import subprocess
    try:
        r = subprocess.run(
            ["gh", "auth", "setup-git"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0:
            log.info("gh auth setup-git: git+https now uses GH_TOKEN")
        else:
            log.warning(
                "gh auth setup-git failed (rc=%s): %s",
                r.returncode, r.stderr.strip()[:200],
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("gh auth setup-git skipped: %s", e)

    log.info("GitHub token wired (TD_GITHUB_TOKEN → GH_TOKEN/GITHUB_TOKEN)")


async def _amain() -> None:
    s = RunnerSettings()  # type: ignore[call-arg]
    _export_github_token(s)
    client = CRPClient(s)
    if s.metrics_enabled:
        from .metrics import start_metrics_server
        start_metrics_server(s.metrics_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runner_task = asyncio.create_task(client.run_forever(stop))
    await stop.wait()
    log.info("shutdown signal received; stopping accept of new tasks")
    client.request_stop()
    try:
        await asyncio.wait_for(runner_task, timeout=60)
    except TimeoutError:
        log.warning("runner did not stop in 60s; cancelling in-flight")
        runner_task.cancel()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_amain())


if __name__ == "__main__":
    run()
