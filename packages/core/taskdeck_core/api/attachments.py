"""Task attachments API (P7 — multimodal task input).

Two endpoints:

  POST /api/v1/attachments
       Multipart upload. Hashes the file, dedups against existing rows
       in the same workspace by (workspace_id, sha256), uploads to S3
       at attachments/<workspace_id>/<sha256>, returns the attachment
       row id. task_id stays NULL until /tasks links it.

  GET  /api/v1/attachments/{id}/file
       Streams the file back from S3. Used by the drawer's "download"
       affordance and by the runner to fetch attachments at task
       dispatch time.

Storage layout: content-addressable. Two users uploading the same PDF
get the same S3 object; the dedup happens within a workspace because
sha256 collisions across workspaces should still be separate access
boundaries.

The 24h-orphan GC for unlinked rows is a separate background job;
see deploy/taskdeck-attachment-gc.* (TODO).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID  # noqa: TCH003 — Pydantic resolves at runtime

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskdeck_core.auth.memberships import get_visible_workspace_ids
from taskdeck_core.auth.middleware import ServicePrincipal, current_principal, require_user
from taskdeck_core.db.engine import get_session
from taskdeck_core.db.models import TaskAttachment, User, WorkspaceMember

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[User | ServicePrincipal, Depends(current_principal)]

# Read in 1 MiB chunks. Keeps memory steady for 50 MB uploads while
# letting hashing keep up with S3 streaming.
_CHUNK = 1024 * 1024


class AttachmentOut(BaseModel):
    id: UUID
    workspace_id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


def _s3_client(settings):
    return boto3.client("s3", region_name=settings.attachment_region)


def _storage_key(workspace_id: UUID, sha: str) -> str:
    return f"attachments/{workspace_id}/{sha}"


@router.post("", response_model=AttachmentOut)
async def upload_attachment(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    file: UploadFile = File(...),
    workspace_id: UUID = Form(...),
) -> AttachmentOut:
    """Stream-hash + upload a single file. Idempotent on (workspace, sha)."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(500, "settings not initialized")

    user = require_user(principal)

    # Workspace ACL: only members can upload into a workspace.
    member = await session.get(WorkspaceMember, (workspace_id, user.id))
    if member is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "not a member of this workspace",
        )

    # Spool the body to a temp file on disk while hashing. Holding a
    # 1 GB body in memory was fine at 50 MB but is wasteful at the new
    # ceiling — disk-spool is constant memory + cheap on EBS. The temp
    # file also lets us reuse boto3's multipart upload (`upload_file`),
    # which handles multipart automatically for large files.
    h = hashlib.sha256()
    cap = settings.attachment_max_bytes
    size = 0
    # NamedTemporaryFile in async context: we close it ourselves so
    # the path stays around for boto3.
    tmp = tempfile.NamedTemporaryFile(
        prefix="td-att-", suffix=".bin", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        try:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"attachment too large (>{cap} bytes)",
                    )
                h.update(chunk)
                tmp.write(chunk)
        finally:
            tmp.close()
        if size == 0:
            raise HTTPException(400, "empty file")

        sha = h.hexdigest()
        content_type = file.content_type or "application/octet-stream"
        filename = file.filename or "untitled"

        # Dedup: if a previous upload with the same sha exists in this
        # workspace, reuse its storage_key and create a new TaskAttachment
        # row pointing at it. Two parallel uploads of the same file create
        # two rows with the same key — both can be linked to different
        # tasks; that's fine.
        existing = await session.scalar(
            select(TaskAttachment)
            .where(TaskAttachment.workspace_id == workspace_id)
            .where(TaskAttachment.sha256 == sha)
            .limit(1)
        )
        if existing is not None:
            storage_key = existing.storage_key
            log.info(
                "attachment dedup: reusing %s for sha %s in workspace %s",
                storage_key, sha[:12], workspace_id,
            )
        else:
            storage_key = _storage_key(workspace_id, sha)

            def _upload() -> None:
                client = _s3_client(settings)
                # boto3's upload_file uses multipart automatically for
                # files above (default) 8 MB. Constant memory regardless
                # of file size.
                client.upload_file(
                    Filename=str(tmp_path),
                    Bucket=settings.attachment_bucket,
                    Key=storage_key,
                    ExtraArgs={
                        "ContentType": content_type,
                        "Metadata": {
                            "original-filename": filename.encode(
                                "ascii", "replace",
                            ).decode(),
                            "uploaded-by": str(user.id),
                        },
                    },
                )

            try:
                await asyncio.to_thread(_upload)
            except (BotoCoreError, ClientError) as e:
                log.warning("attachment upload to s3 failed: %s", e)
                raise HTTPException(502, f"upload failed: {e}") from e
    finally:
        # Always remove the spool file. tmp.close() above released
        # the fd; this just unlinks the inode.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("failed to unlink upload spool %s: %s", tmp_path, e)

    att = TaskAttachment(
        task_id=None,
        workspace_id=workspace_id,
        original_filename=filename,
        content_type=content_type,
        size_bytes=size,
        storage_key=storage_key,
        sha256=sha,
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    session.add(att)
    await session.commit()
    await session.refresh(att)

    return AttachmentOut(
        id=att.id,
        workspace_id=att.workspace_id,
        original_filename=att.original_filename,
        content_type=att.content_type,
        size_bytes=att.size_bytes,
        sha256=att.sha256,
        created_at=att.created_at,
    )


@router.get("/{attachment_id}/file")
async def download_attachment(
    attachment_id: UUID,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
):
    """Stream the attachment back from S3. ACL: caller must see the
    workspace this attachment belongs to."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(500, "settings not initialized")

    att = await session.get(TaskAttachment, attachment_id)
    if att is None:
        raise HTTPException(404, "attachment not found")

    visible = await get_visible_workspace_ids(session, principal)
    if visible is not None and att.workspace_id not in visible:
        # Don't leak existence cross-workspace.
        raise HTTPException(404, "attachment not found")

    def _open_stream():
        client = _s3_client(settings)
        resp = client.get_object(
            Bucket=settings.attachment_bucket,
            Key=att.storage_key,
        )
        return resp["Body"]

    try:
        body = await asyncio.to_thread(_open_stream)
    except (BotoCoreError, ClientError) as e:
        log.warning("attachment download from s3 failed: %s", e)
        raise HTTPException(502, f"download failed: {e}") from e

    # boto3's StreamingBody supports iter_chunks; wrap as an async
    # generator so StreamingResponse can pump it.
    async def aiter():
        try:
            for chunk in body.iter_chunks(chunk_size=_CHUNK):
                yield chunk
        finally:
            try:
                body.close()
            except Exception:  # noqa: BLE001
                pass

    # Use Content-Disposition with a sensible filename so browsers
    # save with the original name. RFC 5987 encoding keeps non-ASCII
    # filenames intact (Chinese / Japanese / etc.).
    from urllib.parse import quote
    safe = att.original_filename.encode("ascii", "replace").decode().replace('"', "")
    encoded = quote(att.original_filename, safe="")
    return StreamingResponse(
        aiter(),
        media_type=att.content_type,
        headers={
            "content-disposition": (
                f'attachment; filename="{safe}"; '
                f"filename*=UTF-8''{encoded}"
            ),
        },
    )
