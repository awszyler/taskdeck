from __future__ import annotations

import logging
from uuid import UUID  # noqa: TC003 — used in type annotations at runtime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from taskdeck_core.db.models import Task, TaskArtifact, TaskDependency

log = logging.getLogger(__name__)


PER_ARTIFACT_CAP = 10 * 1024       # 10 KB
TOTAL_CAP = 50 * 1024              # 50 KB per task-assign envelope
# Kinds whose blobs we inline into the child's <dependency-outputs>
# envelope. `output-manifest` is parsed separately into structured
# parent_outputs metadata, NOT inlined as a content blob.
ARTIFACT_KINDS_INJECTED = frozenset(("git-diff", "git-branch", "decision"))
ARTIFACT_KIND_MANIFEST = "output-manifest"


async def collect_dependency_outputs(
    session: AsyncSession,
    *,
    child_task_id: UUID,
    artifact_store,  # ArtifactStore protocol
) -> list[dict]:
    """Return a list of DependencyOutput-shaped dicts (not Pydantic instances — the
    CRP router will serialise them). Size-capped and oldest-truncated-first."""
    parents = await _parents_of(session, child_task_id)
    if not parents:
        return []

    remaining = TOTAL_CAP
    outputs: list[dict] = []
    # Most-recently-finished parents first (more relevant to child).
    parents.sort(key=lambda t: t.finished_at or t.updated_at, reverse=True)

    for parent in parents:
        if parent.status not in {"done", "failed", "cancelled"}:
            # Defensive: resolver should prevent this, but don't explode.
            continue
        artifact_rows = await _artifact_rows(session, parent.id)
        artifacts_payload: list[dict] = []
        manifest_outputs: list[dict] = []
        for row in artifact_rows:
            # output-manifest is a structured field, not an inlined blob.
            # Parse it separately and stop processing this row as a blob.
            if row.kind == ARTIFACT_KIND_MANIFEST:
                manifest_outputs = await _parse_manifest_artifact(
                    artifact_store, row,
                )
                continue
            if row.kind not in ARTIFACT_KINDS_INJECTED:
                continue
            if remaining <= 0:
                break
            try:
                blob = await artifact_store.read(row.ref)
            except Exception as e:  # noqa: BLE001
                log.warning("could not read artifact %s: %s", row.ref, e)
                continue
            content = blob.decode("utf-8", errors="replace")
            truncated = False
            # Per-artifact cap
            if len(content) > PER_ARTIFACT_CAP:
                content = content[:PER_ARTIFACT_CAP] + "\n[...truncated]"
                truncated = True
                log.warning(
                    "injector: artifact %s/%s truncated to %d KB",
                    parent.id, row.kind, PER_ARTIFACT_CAP // 1024,
                )
            # Total cap
            if len(content) > remaining:
                content = content[:max(0, remaining - 20)] + "\n[...truncated]"
                truncated = True
                log.warning(
                    "injector: artifact %s/%s truncated to fit section cap (%d KB)",
                    parent.id, row.kind, TOTAL_CAP // 1024,
                )
            remaining -= len(content)
            artifacts_payload.append({
                "kind": row.kind,
                "content": content,
                "meta": {k: str(v) for k, v in (row.meta or {}).items()},
                "truncated": truncated,
            })
        # `parent_workspace_path` is computed relative to the child's
        # cwd at runtime. Both child and parent live under
        # <work_dir>/<workspace_slug>/tasks/<id>/, so the relative
        # path is just `../<parent_task_id>/`. Empty when parent has
        # nothing the child can read (e.g. archived, or cross-slug —
        # but cross-slug deps shouldn't be allowed anyway).
        outputs.append({
            "parent_task_id": str(parent.id),
            "parent_title": parent.title,
            "parent_status": parent.status,
            "artifacts": artifacts_payload,
            "parent_workspace_path": f"../{parent.id}/",
            "parent_outputs": manifest_outputs,
        })

    return outputs


async def _parse_manifest_artifact(
    artifact_store, row: TaskArtifact,
) -> list[dict]:
    """Read an output-manifest artifact and return DependencyOutputItem-shaped
    dicts. Errors are logged and treated as 'no manifest' — the child
    just won't see the parent's output list, which is no worse than the
    pre-fix behavior."""
    try:
        blob = await artifact_store.read(row.ref)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read manifest artifact %s: %s", row.ref, e)
        return []
    try:
        import yaml  # local import: yaml is a heavy import for cold paths
        data = yaml.safe_load(blob.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("manifest artifact %s parse failed: %s", row.ref, e)
        return []
    if not isinstance(data, dict):
        return []
    raw_outputs = data.get("outputs") or []
    if not isinstance(raw_outputs, list):
        return []
    parsed: list[dict] = []
    for item in raw_outputs:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        entry = str(item.get("entry", "")).strip()
        if not kind or not entry:
            continue
        parsed.append({
            "kind": kind,
            "entry": entry,
            "label": str(item.get("label", "")).strip(),
        })
    return parsed


async def _parents_of(session: AsyncSession, child_id: UUID) -> list[Task]:
    stmt = (
        select(Task)
        .join(TaskDependency, TaskDependency.parent_task_id == Task.id)
        .where(TaskDependency.child_task_id == child_id)
    )
    return list((await session.scalars(stmt)).all())


async def _artifact_rows(session: AsyncSession, task_id: UUID) -> list[TaskArtifact]:
    stmt = (
        select(TaskArtifact)
        .where(TaskArtifact.task_id == task_id)
        .order_by(TaskArtifact.created_at.asc())
    )
    return list((await session.scalars(stmt)).all())
