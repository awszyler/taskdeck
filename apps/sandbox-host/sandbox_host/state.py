"""Sandbox lifecycle state — SQLite-backed (P-H Phase 1).

The previous in-memory implementation forgot every record on
sandbox-host restart, which forced an "wipe + start over" startup
flow that interacted badly with self-attached networks (see runbook
§17 / git history). With state on disk we can do principled
reconciliation between recorded intent and docker reality.

Design constraints:

- The async interface (`add` / `get` / `list_all` / `remove` /
  `touch`) is unchanged so existing callers don't move.
- Every write is immediate fsync (`PRAGMA synchronous=FULL`); the
  write rate is at most a few per second per task and we'd rather
  pay 1ms latency than lose state on power loss.
- Reads run under WAL mode for cheap concurrent visibility; writers
  acquire a process-wide asyncio.Lock so SQLite's BEGIN IMMEDIATE
  doesn't deadlock if Phase 5's reconciler interleaves.
- `generation` column is reserved here for Phase 4 — defaults to 1
  for now so the schema doesn't need to migrate when generation
  semantics turn on.

The DB lives at <work_dir>/.sandbox-host/state.db. We choose
work_dir over a separate dir so a single bind-mount in compose
covers both worktrees and our state.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sandboxes (
    task_id          TEXT PRIMARY KEY,
    generation       INTEGER NOT NULL DEFAULT 1,
    container_id     TEXT NOT NULL,
    container_name   TEXT NOT NULL,
    network_name     TEXT NOT NULL,
    host_port        INTEGER NOT NULL,
    internal_port    INTEGER NOT NULL,
    runtime          TEXT NOT NULL,
    image            TEXT NOT NULL,
    base_path        TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    last_request_at  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS sandboxes_status_idx ON sandboxes(status);
"""


@dataclass
class SandboxRecord:
    """Live sandbox metadata. Read from / written to SQLite by SandboxRegistry."""
    task_id: str
    container_id: str
    container_name: str
    network_name: str
    host_port: int
    internal_port: int
    runtime: str
    image: str
    base_path: str
    started_at: datetime
    last_request_at: datetime
    status: str = "running"           # running | stopping | stopped | error | provisioning
    error_message: str | None = None
    # Reserved for Phase 4. Persisted but ignored by today's call sites.
    generation: int = 1


def _row_to_record(row: sqlite3.Row | tuple) -> SandboxRecord:
    # Accept either Row (column access) or tuple (positional).
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        # Positional matches our SELECT order below.
        d = dict(zip(_COLUMNS, row))
    return SandboxRecord(
        task_id=d["task_id"],
        container_id=d["container_id"],
        container_name=d["container_name"],
        network_name=d["network_name"],
        host_port=int(d["host_port"]),
        internal_port=int(d["internal_port"]),
        runtime=d["runtime"],
        image=d["image"],
        base_path=d["base_path"],
        started_at=datetime.fromisoformat(d["started_at"]),
        last_request_at=datetime.fromisoformat(d["last_request_at"]),
        status=d["status"] or "running",
        error_message=d["error_message"],
        generation=int(d["generation"] or 1),
    )


_COLUMNS = (
    "task_id", "generation", "container_id", "container_name",
    "network_name", "host_port", "internal_port", "runtime",
    "image", "base_path", "started_at", "last_request_at",
    "status", "error_message",
)


class SandboxRegistry:
    """SQLite-backed registry. Same async interface as the old
    in-memory version so callers don't change.

    Threading model: SQLite calls run in `asyncio.to_thread` so a slow
    fsync on a beefy reconciler pass doesn't block the event loop. A
    process-wide asyncio.Lock serialises *writes* — sqlite3's
    `BEGIN IMMEDIATE` acquires the file-level write lock and would
    raise OperationalError under contention; the asyncio lock turns
    that into clean queueing.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        # Initialise the schema synchronously at construction. Cheap
        # (one-shot CREATE IF NOT EXISTS) and lets callers fail fast
        # if the DB path is unwritable.
        with self._connect() as conn:
            conn.executescript(_DDL)
            row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                # Forward migration is a Phase 1 non-goal. We don't
                # have a v0 → v1 path because v1 is the first version.
                # If a future schema bump is needed, do it here.
                raise RuntimeError(
                    f"sandbox state schema mismatch: db={row[0]} expected={SCHEMA_VERSION}"
                )
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,  # autocommit; we manage transactions explicitly
            timeout=5.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            # WAL = readers don't block writers; durability = FULL.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    # ----- Read API (no lock; WAL gives consistent snapshots) -----

    async def get(self, task_id: str) -> SandboxRecord | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> SandboxRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sandboxes WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_record(row)

    async def list_all(self) -> list[SandboxRecord]:
        return await asyncio.to_thread(self._list_all_sync)

    def _list_all_sync(self) -> list[SandboxRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sandboxes "
                f"ORDER BY started_at ASC"
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def __len__(self) -> int:
        # Sync; called rarely (status endpoint logging). No lock needed
        # — WAL makes COUNT(*) consistent.
        with self._connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0]
            )

    # ----- Write API (lock-serialised) -----

    async def add(self, record: SandboxRecord) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._add_sync, record)

    def _add_sync(self, record: SandboxRecord) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO sandboxes (
                        task_id, generation, container_id, container_name,
                        network_name, host_port, internal_port, runtime,
                        image, base_path, started_at, last_request_at,
                        status, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        generation       = excluded.generation,
                        container_id     = excluded.container_id,
                        container_name   = excluded.container_name,
                        network_name     = excluded.network_name,
                        host_port        = excluded.host_port,
                        internal_port    = excluded.internal_port,
                        runtime          = excluded.runtime,
                        image            = excluded.image,
                        base_path        = excluded.base_path,
                        started_at       = excluded.started_at,
                        last_request_at  = excluded.last_request_at,
                        status           = excluded.status,
                        error_message    = excluded.error_message
                    """,
                    (
                        record.task_id,
                        record.generation,
                        record.container_id,
                        record.container_name,
                        record.network_name,
                        record.host_port,
                        record.internal_port,
                        record.runtime,
                        record.image,
                        record.base_path,
                        record.started_at.isoformat(),
                        record.last_request_at.isoformat(),
                        record.status,
                        record.error_message,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def remove(self, task_id: str) -> SandboxRecord | None:
        async with self._write_lock:
            return await asyncio.to_thread(self._remove_sync, task_id)

    def _remove_sync(self, task_id: str) -> SandboxRecord | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT {','.join(_COLUMNS)} FROM sandboxes WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute("DELETE FROM sandboxes WHERE task_id=?", (task_id,))
                conn.execute("COMMIT")
                return _row_to_record(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def touch(self, task_id: str) -> None:
        """Bump last_request_at. Hot path: called on every proxied request."""
        async with self._write_lock:
            await asyncio.to_thread(self._touch_sync, task_id)

    def _touch_sync(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sandboxes SET last_request_at=? WHERE task_id=?",
                (datetime.now(UTC).isoformat(), task_id),
            )
