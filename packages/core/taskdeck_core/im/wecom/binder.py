from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


@dataclass
class BindCodeEntry:
    user_id: UUID | None   # single-user mode: None is fine
    workspace_id: UUID
    expires_at: float


class BindCodeCache:
    """Tiny in-memory bind-code store. 5-minute TTL, single-use."""

    TTL_SECONDS = 5 * 60

    def __init__(self):
        self._codes: dict[str, BindCodeEntry] = {}

    def issue(self, *, workspace_id: UUID, user_id: UUID | None = None) -> tuple[str, float]:
        code = self._generate_code()
        expires_at = time.time() + self.TTL_SECONDS
        self._codes[code] = BindCodeEntry(
            user_id=user_id, workspace_id=workspace_id, expires_at=expires_at
        )
        return code, expires_at

    def consume(self, code: str) -> BindCodeEntry | None:
        self._gc()
        entry = self._codes.pop(code, None)
        if entry is None or entry.expires_at <= time.time():
            return None
        return entry

    def _generate_code(self) -> str:
        # 6 chars, uppercase letters + digits. Unambiguous (no O/0, I/1).
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(6))

    def _gc(self) -> None:
        now = time.time()
        stale = [k for k, e in self._codes.items() if e.expires_at <= now]
        for k in stale:
            self._codes.pop(k, None)
