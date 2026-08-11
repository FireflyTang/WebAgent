from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .models import SessionState, utc_now
from .service import SessionBusyError, SessionService, SessionServiceError

logger = logging.getLogger(__name__)


class LifecycleReaper:
    def __init__(
        self,
        service: SessionService,
        *,
        pause_after_seconds: int,
        delete_after_seconds: int,
        interval_seconds: int,
    ) -> None:
        if not 0 < pause_after_seconds < delete_after_seconds:
            raise ValueError("delete timeout must be greater than pause timeout")
        self.service = service
        self.pause_after = timedelta(seconds=pause_after_seconds)
        self.delete_after = timedelta(seconds=delete_after_seconds)
        self.interval = interval_seconds
        self._stopping = asyncio.Event()

    async def tick(self) -> None:
        now = utc_now()
        list_sessions = getattr(self.service.repository, "list_sessions", None)
        all_sessions = await list_sessions() if list_sessions is not None else []

        # Finish DB-first resumes before considering ACTIVE records idle.  A
        # resume attempt is excluded from pausing for this tick whether it
        # succeeds or fails, so transient failures remain pending for retry.
        resume_attempted = {
            record.session_id
            for record in all_sessions
            if record.state is SessionState.ACTIVE and record.metadata.get("resume_pending") is True
        }
        for session_id in resume_attempted:
            try:
                await self.service.resume(session_id)
            except (SessionBusyError, SessionServiceError):
                continue
            except Exception:
                logger.exception(
                    "Failed to finish resuming session %s; continuing reaper tick", session_id
                )

        pause_due = await self.service.repository.list_due(
            now - self.pause_after, states=[SessionState.ACTIVE]
        )
        for record in pause_due:
            if record.session_id in resume_attempted:
                continue
            try:
                await self.service.pause(record.session_id)
            except (SessionBusyError, SessionServiceError):
                continue
            except Exception:
                logger.exception(
                    "Failed to pause idle session %s; continuing reaper tick", record.session_id
                )

        # A DB-first pause can survive a transient runtime/sandbox failure.
        # Retry its idempotent side effects on the next lifecycle tick.
        pending_pause = (
            [
                record
                for record in await list_sessions()
                if record.state is SessionState.PAUSED
                and record.metadata.get("pause_pending") is True
            ]
            if list_sessions is not None
            else []
        )
        for record in pending_pause:
            try:
                await self.service.pause(record.session_id)
            except (SessionBusyError, SessionServiceError):
                continue
            except Exception:
                logger.exception(
                    "Failed to finish pausing session %s; continuing reaper tick", record.session_id
                )

        delete_due = await self.service.repository.list_due(
            now - self.delete_after, states=[SessionState.PAUSED, SessionState.DELETED]
        )
        pending_cleanup = await self.service.repository.list_pending_cleanup()
        seen: set[str] = set()
        for record in [*delete_due, *pending_cleanup]:
            if record.session_id in seen:
                continue
            seen.add(record.session_id)
            if (
                record.state is SessionState.DELETED
                and record.metadata.get("cleanup_pending") is False
            ):
                continue
            try:
                await self.service.delete(record.session_id)
            except (SessionBusyError, SessionServiceError):
                continue
            except Exception:
                # A partially deleted tombstone retains cleanup_pending=True;
                # later ticks retry it without blocking unrelated sessions.
                logger.exception(
                    "Failed to delete idle session %s; continuing reaper tick", record.session_id
                )

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
            except TimeoutError:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Lifecycle reaper tick failed; it will retry")

    def stop(self) -> None:
        self._stopping.set()
