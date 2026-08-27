"""Download task attachments from core into the task workspace (P7).

Called from crp_client after the workspace is set up. For each
attachment in the assign payload, GET /api/v1/attachments/<id>/file
from core (using the same bearer token the runner uses for CRP) and
write the body to <cwd>/.taskdeck/inputs/<filename>.

Failure to fetch one attachment does not fail the task — we log and
continue. The agent will see whichever subset arrived in
<task-inputs> and can ask the user about missing ones.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from taskdeck_proto.crp import TaskAttachment

log = logging.getLogger(__name__)


class AttachmentError(Exception):
    """One or more attachments failed to download (P-H Phase 6).

    The runner used to log a warning and silently skip — the agent
    then ran without the file the user explicitly attached, and the
    failure was easy to miss in stdout. Now we abort the task with
    this error, the user sees a clear "attachment X could not be
    downloaded" summary on the kanban, and a retry is one click.

    Carries `failures` so callers can include filename/reason in the
    surfaced error message.
    """
    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"{len(self.failures)} attachment(s) failed:"]
        for name, reason in self.failures:
            lines.append(f"  - {name}: {reason}")
        return "\n".join(lines)


# Sanitise a filename so it can't traverse out of cwd. We accept
# almost anything but normalise path separators and strip leading
# dots / slashes — agents may legitimately want filenames like
# "report (final).pdf" so we don't apply a strict whitelist.
_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f/\\]")


def _safe_filename(name: str) -> str:
    cleaned = _BAD_PATH_CHARS.sub("_", name).strip()
    cleaned = cleaned.lstrip(".")
    return cleaned or "untitled"


async def download_attachments(
    *,
    cwd: Path,
    attachments: list[TaskAttachment],
    core_http_url: str,
    bearer_token: str,
) -> list[Path]:
    """Fetch every attachment into cwd/.taskdeck/inputs/.

    Returns the list of paths actually written.

    Phase 6 fail-loud: if ANY attachment fails to download, raise
    AttachmentError with the failure list. We still attempt every
    other attachment first so the user gets a complete failure
    report rather than the first error only — easier to fix in one
    pass than discovering a second missing file on retry.
    """
    if not attachments:
        return []
    inputs_dir = cwd / ".taskdeck" / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    failures: list[tuple[str, str]] = []
    headers = {"Authorization": f"Bearer {bearer_token}"}
    # No explicit read timeout: a 1 GB attachment on a slow link
    # could legitimately take many minutes. We rely on the per-chunk
    # read returning eventually + the connect timeout to fail fast
    # when core is actually down. Body size cap is enforced by core.
    timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for att in attachments:
            url = f"{core_http_url}/api/v1/attachments/{att.id}/file"
            try:
                async with client.stream("GET", url) as resp:
                    if not resp.is_success:
                        snippet = (await resp.aread())[:200].decode(
                            "utf-8", errors="replace",
                        )
                        failures.append((
                            att.filename,
                            f"HTTP {resp.status_code}: {snippet}",
                        ))
                        continue
                    dest = inputs_dir / _safe_filename(att.filename)
                    with dest.open("wb") as f:
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            f.write(chunk)
                    written.append(dest.relative_to(cwd))
            except httpx.HTTPError as e:
                failures.append((att.filename, f"network error: {e}"))
                continue
            except OSError as e:
                # Filesystem error (out of disk, permission, etc.)
                failures.append((att.filename, f"local write error: {e}"))
                continue

    if failures:
        # Drop any partial files we did write — the run is going to
        # abort anyway, no need to leave half-state.
        for p in written:
            try:
                (cwd / p).unlink(missing_ok=True)
            except OSError:
                pass
        raise AttachmentError(failures)
    return written


def render_attachments_block(
    *,
    cwd_relative_paths: list[Path],
    attachments: list[TaskAttachment],
) -> str:
    """Build the <task-inputs>...</task-inputs> XML block prepended to
    the agent's prompt. Empty string when nothing was successfully
    downloaded. Sizes are kept compact so the envelope stays small.
    """
    if not cwd_relative_paths or not attachments:
        return ""
    # Index attachments by filename for type/size lookup. Multiple
    # attachments with identical filenames (rare but legal — same name
    # uploaded twice) share the same on-disk file because filenames
    # collide when written; treat the first match as authoritative.
    by_name: dict[str, TaskAttachment] = {}
    for att in attachments:
        by_name.setdefault(_safe_filename(att.filename), att)

    lines = ["<task-inputs>"]
    for rel in cwd_relative_paths:
        att = by_name.get(rel.name)
        size_hint = ""
        type_hint = ""
        if att is not None:
            type_hint = f' type="{att.content_type}"'
            size_hint = f' size="{_humanise(att.size_bytes)}"'
        lines.append(f'  <file path="{rel.as_posix()}"{type_hint}{size_hint}/>')
    lines.append("</task-inputs>\n")
    return "\n".join(lines)


def _humanise(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n // 1024}KB"
    return f"{n // (1024 * 1024)}MB"
