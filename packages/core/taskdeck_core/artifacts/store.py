from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes) -> int: ...  # returns bytes written
    async def read(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalFSArtifactStore:
    """Writes artifacts as files under a base directory.

    key is a relative path like "<task_id>/<kind>". Subdirectories are created
    on put(). For M2 we write the whole payload in one shot — streaming is
    Phase 3 when artifacts get large.
    """

    def __init__(self, base_dir: Path):
        self._base = base_dir

    def _full(self, key: str) -> Path:
        p = (self._base / key).resolve()
        # Safety: ensure the resolved path is still under the base dir.
        if not str(p).startswith(str(self._base.resolve())):
            raise ValueError(f"unsafe artifact key: {key}")
        return p

    async def put(self, key: str, data: bytes) -> int:
        path = self._full(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return len(data)

    async def read(self, key: str) -> bytes:
        path = self._full(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._full(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)
