"""In-process, per-session serialisation for the single-worker demo."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class SessionLockRegistry:
    """Maps a stable session id to one asyncio lock.

    Locks are intentionally process-local; SQLite version checks provide the
    persistent safety net. Multi-worker coordination is outside this demo's
    contract.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        if not session_id:
            raise ValueError("session_id must not be empty")
        # This method contains no await, hence task switching cannot create two
        # locks for the same id on one event loop.
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    get = lock_for

    @asynccontextmanager
    async def hold(self, session_id: str) -> AsyncIterator[None]:
        lock = self.lock_for(session_id)
        async with lock:
            yield

    def discard_if_idle(self, session_id: str) -> bool:
        """Forget an unused lock, primarily useful after a tombstone is reaped."""

        lock = self._locks.get(session_id)
        if lock is None or lock.locked():
            return False
        del self._locks[session_id]
        return True


# Short name retained for consumers which prefer a plural service object.
SessionLocks = SessionLockRegistry
