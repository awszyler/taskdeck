"""P6.3.7 cold archive — tar.gz workspaces to S3 before GC removes them.

The pre-P6.3.7 GC just rm -rf'd 30-day-old workspaces, which broke
sandbox-reactivation for any older task. This module bridges that:

  archive_workspace(): tar.gz a workspace, upload to S3, POST the key
  back to core via /api/v1/tasks/{id}/archive. Idempotent — uploading
  an already-uploaded key just overwrites.

  restore_workspace(): the inverse. Download the tar.gz from S3 and
  extract under work_dir. Caller is core's start_sandbox flow.

Failure mode: if archive_bucket is empty (config off), or boto3 raises,
or the core callback fails, the GC falls back to plain rm -rf and logs.
The user sees "workspace pruned" — same as the pre-P6.3.7 baseline.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError

from .settings import SandboxHostSettings

log = logging.getLogger(__name__)


# Hard cap on archive tarball size (in bytes). Protects against a
# runaway agent leaving a 50 GiB workspace that would balloon S3
# storage cost or fork-bomb the host on extract.
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _s3_client(settings: SandboxHostSettings):
    return boto3.client("s3", region_name=settings.archive_region)


def _archive_key(workspace_slug: str, task_id: str) -> str:
    # Single canonical layout: archive/<slug>/<task_id>.tar.gz. The
    # slug+id pair is unique by construction (slug scopes membership).
    return f"archive/{workspace_slug}/{task_id}.tar.gz"


def _make_tarball(src: Path, dst: Path) -> int:
    """Tar.gz src into dst. Returns archive size in bytes."""
    with tarfile.open(dst, "w:gz") as tar:
        tar.add(src, arcname=src.name)
    return dst.stat().st_size


async def archive_workspace(
    *,
    settings: SandboxHostSettings,
    workspace_slug: str,
    task_id: str,
    workspace_dir: Path,
) -> str | None:
    """Archive a workspace to S3 and notify core.

    Returns the S3 key on success, None on any failure (caller should
    fall back to rm -rf so the workspace doesn't accumulate).
    """
    if not settings.archive_bucket:
        log.info("archive: bucket not configured; skipping for %s", task_id)
        return None

    key = _archive_key(workspace_slug, task_id)
    tarball = Path(f"/tmp/td-archive-{task_id}.tar.gz")

    def _upload() -> tuple[str, int] | None:
        # Run sync boto3 + tarfile in a thread so the GC loop stays async.
        try:
            size = _make_tarball(workspace_dir, tarball)
            if size > _MAX_ARCHIVE_BYTES:
                log.warning(
                    "archive: %s tarball too large (%d > %d); skipping",
                    task_id, size, _MAX_ARCHIVE_BYTES,
                )
                return None
            client = _s3_client(settings)
            client.upload_file(
                str(tarball),
                settings.archive_bucket,
                key,
            )
            return key, size
        finally:
            tarball.unlink(missing_ok=True)

    try:
        result = await asyncio.to_thread(_upload)
    except (BotoCoreError, ClientError, OSError) as e:
        log.warning("archive: upload failed for %s: %s", task_id, e)
        return None
    if result is None:
        return None
    uploaded_key, size = result

    # Notify core. If this fails, S3 has the tarball but DB doesn't
    # know about it — orphaned key, harmless but invisible to users.
    # We log loudly so an operator can clean up later if needed.
    if not settings.core_service_token:
        log.warning(
            "archive: core_service_token not set, archived %s to s3 but "
            "DB won't know — set TD_SBH_CORE_SERVICE_TOKEN to fix",
            task_id,
        )
        return uploaded_key
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.core_http_url}/api/v1/tasks/{task_id}/archive",
                json={
                    "archive_key": uploaded_key,
                    "archived_at": datetime.now(UTC).isoformat(),
                    "size_bytes": size,
                },
                headers={"Authorization": f"Bearer {settings.core_service_token}"},
            )
        if not resp.is_success:
            log.warning(
                "archive: core callback non-2xx for %s: %d %s",
                task_id, resp.status_code, resp.text[:200],
            )
            return uploaded_key  # S3 has it; DB doesn't. Operator can fix.
    except httpx.HTTPError as e:
        log.warning("archive: core callback failed for %s: %s", task_id, e)
        return uploaded_key

    log.info(
        "archive: %s → s3://%s/%s (%d bytes)",
        task_id, settings.archive_bucket, uploaded_key, size,
    )
    return uploaded_key


async def restore_workspace(
    *,
    settings: SandboxHostSettings,
    workspace_slug: str,
    task_id: str,
    archive_key: str,
) -> Path:
    """Pull archive_key from S3, extract under work_dir. Returns the
    restored workspace path. Raises ArchiveError on failure."""
    if not settings.archive_bucket:
        raise ArchiveError("archive bucket not configured")

    workspace_dir = settings.work_dir / workspace_slug / "tasks" / task_id
    if workspace_dir.exists():
        log.info(
            "restore: %s already on disk; treating as no-op", workspace_dir,
        )
        return workspace_dir

    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    tarball = Path(f"/tmp/td-restore-{task_id}.tar.gz")

    def _download_and_extract() -> Path:
        try:
            client = _s3_client(settings)
            client.download_file(
                settings.archive_bucket, archive_key, str(tarball),
            )
            size = tarball.stat().st_size
            if size > _MAX_ARCHIVE_BYTES:
                # Should never trigger since archive enforces same cap,
                # but defense in depth — refuse to extract a huge tarball.
                raise ArchiveError(
                    f"archive {archive_key} too large to restore: {size}",
                )
            # Extract to a tmp dir then rename, so a partial extract
            # never leaves a half-restored workspace_dir behind.
            tmp_extract = workspace_dir.parent / f".restore-{task_id}"
            if tmp_extract.exists():
                shutil.rmtree(tmp_extract)
            tmp_extract.mkdir()
            with tarfile.open(tarball, "r:gz") as tar:
                # Resist tarball path traversal — drop entries whose
                # resolved path escapes the extract dir.
                safe_members = []
                for m in tar.getmembers():
                    target = (tmp_extract / m.name).resolve()
                    if not str(target).startswith(str(tmp_extract.resolve())):
                        log.warning(
                            "restore: dropping suspicious tar entry %s", m.name,
                        )
                        continue
                    safe_members.append(m)
                tar.extractall(tmp_extract, members=safe_members)
            # tar archived as `<task_id>/...`, so the extracted dir is
            # tmp_extract/<task_id>/. Move it into place.
            inner = tmp_extract / task_id
            if not inner.is_dir():
                # Older format or unexpected layout — move whole thing.
                inner = tmp_extract
            inner.rename(workspace_dir)
            if tmp_extract.exists():
                shutil.rmtree(tmp_extract, ignore_errors=True)
            return workspace_dir
        finally:
            tarball.unlink(missing_ok=True)

    try:
        return await asyncio.to_thread(_download_and_extract)
    except (BotoCoreError, ClientError, OSError, tarfile.TarError) as e:
        raise ArchiveError(f"restore failed: {e}") from e


class ArchiveError(Exception):
    """Raised when archive upload or restore fails."""
