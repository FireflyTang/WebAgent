from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import errno
import hashlib
import logging
import mimetypes
import os
import sqlite3
import stat
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import NoReturn, TypeVar

from app.runtime import (
    AgentRuntime,
    Completed,
    Failed,
    InteractionRequest,
    ProviderConfig,
    RuntimeContext,
    TextDelta,
    Usage,
)
from app.runtime.events import Diagnostic, Effort, Progress, validate_effort
from app.sandbox import SandboxManager

from .html_log import SessionHtmlLogger
from .locks import SessionLockRegistry
from .models import SessionRecord, SessionState, SessionTurnRequest, utc_now
from .repository import SessionLogEntry, SessionRepository, SessionVersionConflictError
from .runtime_debug import append_runtime_debug
from .state_machine import touch, transition

T = TypeVar("T")
logger = logging.getLogger(__name__)
_UNSET = object()
_RENAME_EXCHANGE = 0x2


def _rename_exchange(directory_fd: int, first: str, second: str) -> None:
    """Atomically exchange two names in one directory on Linux.

    ``rename(2)`` has no compare-and-swap mode: a final path ``stat`` before
    ``os.replace`` still leaves a scheduling window.  Exchange keeps a
    concurrently installed target inode recoverable under ``first`` so it can
    be restored when its identity differs from the checked revision inode.
    """
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:  # pragma: no cover - Linux is a deployment requirement
        raise SessionServiceError("Atomic editor replacement requires renameat2") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            directory_fd,
            os.fsencode(first),
            directory_fd,
            os.fsencode(second),
            _RENAME_EXCHANGE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


class _SessionEventLease(AsyncIterator[T]):
    """Own a turn iterator and its already-acquired session lock.

    An async-generator ``finally`` block does not run when ``aclose()`` is
    called before its first iteration.  This concrete iterator therefore owns
    the lock independently of whether the wrapped generator ever starts.
    """

    def __init__(self, inner: AsyncIterator[T], lock: asyncio.Lock) -> None:
        self._inner = inner
        self._lock = lock
        self._closed = False
        self._operation_lock = asyncio.Lock()

    def __aiter__(self) -> _SessionEventLease[T]:
        return self

    async def __anext__(self) -> T:
        async with self._operation_lock:
            if self._closed:
                raise StopAsyncIteration
            try:
                return await anext(self._inner)
            except BaseException:
                await self._close_unlocked()
                raise

    async def _close_unlocked(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._inner.aclose()
        finally:
            if self._lock.locked():
                self._lock.release()

    async def aclose(self) -> None:
        async with self._operation_lock:
            await self._close_unlocked()


@dataclass(frozen=True, slots=True)
class SessionTurnCompleted:
    """Terminal structured event for WebSocket consumers of a turn."""

    completed: bool
    stop_reason: str
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float


class SessionServiceError(RuntimeError):
    code = "session_error"


class SessionDeletedError(SessionServiceError):
    code = "session_deleted"


class SessionBusyError(SessionServiceError):
    code = "session_busy"


class SessionNotFoundError(SessionServiceError):
    code = "session_not_found"


class SessionBackendMismatchError(SessionServiceError):
    code = "session_backend_mismatch"


class SandboxUnavailableError(SessionServiceError):
    code = "sandbox_unavailable"


class InvalidWorkspacePathError(SessionServiceError):
    code = "invalid_workspace_path"


class FileChangedError(SessionServiceError):
    code = "file_changed"


class FileTooLargeError(SessionServiceError):
    code = "file_too_large"


class FileUploadLimitError(SessionServiceError):
    code = "file_upload_limit"


class OpenedWorkspaceFile:
    """Own an already-open regular file and stream only that inode.

    The descriptor is opened relative to the workspace directory descriptor.
    It therefore remains bound to the validated inode even if an agent renames,
    removes, or replaces the visible path before the HTTP body is consumed.
    """

    chunk_size = 64 * 1024

    def __init__(self, fd: int, normalized_path: str) -> None:
        self.normalized_path = normalized_path
        self._fd: int | None = fd
        self._io_lock = threading.Lock()
        self._stream_started = False

    @property
    def closed(self) -> bool:
        with self._io_lock:
            return self._fd is None

    def _read_chunk(self) -> bytes:
        with self._io_lock:
            if self._fd is None:
                return b""
            return os.read(self._fd, self.chunk_size)

    async def chunks(self) -> AsyncIterator[bytes]:
        if self._stream_started:
            raise RuntimeError("Workspace file stream has already been consumed")
        self._stream_started = True
        try:
            while chunk := await asyncio.to_thread(self._read_chunk):
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        with self._io_lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                os.close(fd)

    def read_all(self, limit: int | None = None) -> bytes:
        """Read this already validated inode, optionally retaining a small sample."""
        with self._io_lock:
            if self._fd is None:
                raise RuntimeError("Workspace file is closed")
            os.lseek(self._fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = limit
            while remaining is None or remaining > 0:
                chunk = os.read(
                    self._fd, 64 * 1024 if remaining is None else min(64 * 1024, remaining)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            return b"".join(chunks)

    def stat(self) -> os.stat_result:
        with self._io_lock:
            if self._fd is None:
                raise RuntimeError("Workspace file is closed")
            return os.fstat(self._fd)

    def __del__(self) -> None:
        # Best-effort fallback for a response object that is discarded before
        # ASGI starts it. Normal request paths close explicitly in the response.
        try:
            self.close()
        except OSError:
            pass


class SessionService:
    """Coordinates the durable mapping, sandbox, and runtime for one turn."""

    def __init__(
        self,
        repository: SessionRepository,
        locks: SessionLockRegistry,
        sandbox: SandboxManager,
        runtime: AgentRuntime,
        *,
        delete_workspace: bool = True,
        html_logger: SessionHtmlLogger | None = None,
        delete_after_seconds: int = 7200,
    ) -> None:
        self.repository = repository
        self.locks = locks
        self.sandbox = sandbox
        self.runtime = runtime
        self.delete_workspace = delete_workspace
        self.html_logger = html_logger
        self._upload_locks: dict[str, asyncio.Lock] = {}
        # Test-only synchronization point for proving editor writes reject a
        # pathname replacement that happens after revision validation.
        self._editor_revision_checked_hook: Callable[[], None] | None = None
        if delete_after_seconds <= 0:
            raise ValueError("delete_after_seconds must be positive")
        self.delete_after_seconds = delete_after_seconds

    async def _update_with_retry(
        self,
        session_id: str,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        attempts: int = 4,
    ) -> SessionRecord:
        """Apply a narrow durable mutation without losing a presentation update.

        A running turn owns its lifecycle fields, while title/model/effort can be
        edited through the REST directory concurrently.  Re-read after an
        optimistic-lock conflict and apply only the caller's intended mutation
        to the newest record, rather than turning an otherwise completed turn
        into a failed one.
        """

        for _ in range(attempts):
            current = await self.repository.get(session_id)
            if current is None:
                raise SessionNotFoundError(session_id)
            updated = mutate(current)
            if updated == current:
                return current
            try:
                return await self.repository.update(updated, expected_version=current.version)
            except SessionVersionConflictError:
                continue
        raise SessionServiceError("Session record changed concurrently; retry the request")

    async def _append_log(
        self,
        session_id: str,
        *,
        title: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if self.html_logger is None:
            return
        try:
            await self.html_logger.append(
                session_id,
                title=title,
                content=content,
                metadata=metadata,
            )
        except (OSError, sqlite3.Error):
            # Logging is observational and must not make an otherwise valid turn fail.
            return

    async def _append_progress_log(self, session_id: str, progress: Progress) -> None:
        """Record meaningful progress without logging high-frequency thought heartbeats."""
        if progress.phase == "thinking" and progress.status == "running":
            return
        await self._append_log(
            session_id,
            title="执行进度",
            content=progress.message,
            metadata={
                "阶段": progress.phase,
                "状态": progress.status,
                **({"工具": progress.tool_name} if progress.tool_name else {}),
                **(
                    {"独立耗时秒": round(progress.duration_seconds, 2)}
                    if progress.duration_seconds is not None
                    else {}
                ),
            },
        )

    async def _append_diagnostic_log(self, session_id: str, diagnostic: Diagnostic) -> None:
        """Persist SDK diagnostics without exposing them in client event streams."""
        if self.html_logger is None:
            return
        try:
            await append_runtime_debug(
                self.html_logger,
                session_id,
                f"sdk.{diagnostic.message_type}",
                dataclasses.asdict(diagnostic),
            )
        except (OSError, sqlite3.Error) as exc:
            # Diagnostic retention is observational and must not interrupt a turn.
            logger.warning(
                "Could not persist SDK diagnostic session_id=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )
            return

    @staticmethod
    async def _sandbox_call(operation: Awaitable[T]) -> T:
        try:
            return await operation
        except Exception as exc:
            raise SandboxUnavailableError("Sandbox backend is unavailable") from exc

    async def _create(
        self,
        session_id: str,
        system_prompt: str | None,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
        owner_user_id: str | None = None,
    ) -> SessionRecord:
        info = await self._sandbox_call(self.sandbox.create(session_id))
        context = RuntimeContext(
            session_id,
            info.sandbox_id,
            info.workspace,
            system_prompt,
            provider=provider,
            effort=effort,
        )
        runtime_id: str | None = None
        try:
            runtime_id = await self.runtime.create_session(context)
            record = SessionRecord(
                session_id=session_id,
                owner_user_id=owner_user_id,
                sandbox_id=info.sandbox_id,
                claude_session_id=runtime_id,
                metadata={
                    "system_prompt": system_prompt,
                    "runtime_backend": type(self.runtime).__name__,
                    "runtime_started": False,
                    "last_effort": effort,
                },
            )
            return await self.repository.create(record)
        except BaseException as original:
            # No durable mapping exists, so both external allocations must be
            # compensated.  Attempt both cleanups and preserve the creation
            # failure as the primary error even if a cleanup also fails.
            if runtime_id is not None:
                try:
                    await self.runtime.close(runtime_id, context)
                except BaseException as cleanup_error:
                    original.add_note(f"runtime create compensation failed: {cleanup_error!r}")
            if info.created:
                try:
                    await self.sandbox.delete(
                        info.sandbox_id,
                        self.delete_workspace and info.workspace_created,
                    )
                except BaseException as cleanup_error:
                    original.add_note(f"sandbox create compensation failed: {cleanup_error!r}")
            raise

    async def create_empty(
        self,
        session_id: str,
        *,
        title: str | None = None,
        last_model: str | None = None,
        last_effort: str | None = None,
        owner_user_id: str | None = None,
    ) -> SessionRecord:
        """Create the durable sandbox/runtime mapping before a first chat turn."""

        effort = validate_effort(last_effort)
        record = await self._create(session_id, None, effort=effort, owner_user_id=owner_user_id)
        if title is None and last_model is None:
            return record
        return await self._update_with_retry(
            session_id,
            lambda current: current.with_metadata(title=title, last_model=last_model),
        )

    async def update_presentation(
        self,
        session_id: str,
        *,
        title: object = _UNSET,
        last_model: object = _UNSET,
        last_effort: object = _UNSET,
    ) -> SessionRecord:
        """Persist independently editable title/model fields without touching lifecycle."""

        values: dict[str, object] = {}
        if title is not _UNSET:
            values["title"] = title
        if last_model is not _UNSET:
            values["last_model"] = last_model
        if last_effort is not _UNSET:
            values["last_effort"] = validate_effort(last_effort)
        if not values:
            return await self.get(session_id)

        def mutate(record: SessionRecord) -> SessionRecord:
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            return record.with_metadata(**values)

        return await self._update_with_retry(session_id, mutate)

    def lifecycle_view(self, record: SessionRecord) -> tuple[str, datetime | None]:
        """Derive browser lifecycle state from durable state and reaper policy."""

        if record.state is SessionState.DELETED:
            return "deleted", record.deleted_at
        if record.state is SessionState.ACTIVE:
            return "active", None
        delete_at = record.last_activity_at + timedelta(seconds=self.delete_after_seconds)
        return (
            "expiring" if delete_at <= utc_now() + timedelta(minutes=30) else "paused"
        ), delete_at

    def compatibility_view(self, record: SessionRecord) -> tuple[bool, str | None]:
        """Project a durable mapping against the active runtime and sandbox.

        This deliberately uses only durable fields and manager/runtime type names:
        rendering a session directory must not inspect or start every old sandbox.
        """

        stored_runtime = record.metadata.get("runtime_backend")
        current_runtime = type(self.runtime).__name__
        if stored_runtime != current_runtime:
            return False, "运行时后端不兼容"
        sandbox_id = record.sandbox_id or ""
        current_sandbox = type(self.sandbox).__name__
        expected_prefix = {
            "DockerSandboxManager": "oca-sandbox-",
            "LocalSandboxManager": "local-",
        }.get(current_sandbox)
        if expected_prefix is not None and not sandbox_id.startswith(expected_prefix):
            return False, "沙箱后端不兼容"
        return True, None

    async def transcript(self, session_id: str) -> list[SessionLogEntry]:
        """Return persisted high-level chat entries, excluding progress and diagnostics."""

        await self.get(session_id)
        entries = await self.repository.list_log_entries(session_id)
        return [
            entry
            for entry in entries
            if entry.event_type is None and entry.title in {"用户消息", "Claude Code 输出"}
        ]

    async def _resume_runtime(self, record: SessionRecord, context: RuntimeContext) -> None:
        restore = getattr(self.runtime, "restore_session_state", None)
        if restore is not None:
            await restore(
                record.claude_session_id or "",
                started=bool(record.metadata.get("runtime_started", True)),
            )
        await self.runtime.resume(record.claude_session_id or "", context)

    async def _context(
        self,
        record: SessionRecord,
        system_prompt: str | None = None,
        model: str | None = None,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
    ) -> RuntimeContext:
        if not record.sandbox_id or not record.claude_session_id:
            raise SessionServiceError("Session mapping is incomplete")
        info = await self._sandbox_call(self.sandbox.inspect(record.sandbox_id))
        if info is None or info.state == "deleted":
            raise SessionServiceError("Session sandbox is missing")
        # A session's first system prompt is part of its resumable contract.
        # Never let a later request override the durable prompt at runtime.
        prompt = record.metadata.get("system_prompt") or system_prompt
        return RuntimeContext(
            record.session_id,
            record.sandbox_id,
            Path(info.workspace),
            prompt,
            model,
            provider,
            effort if effort is not None else record.last_effort,
        )

    async def _prepare(
        self,
        session_id: str,
        system_prompt: str | None,
        model: str | None = None,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
    ) -> tuple[SessionRecord, RuntimeContext]:
        record = await self.repository.get(session_id)
        if record is None:
            record = await self._create(session_id, system_prompt, provider, effort)
        if record.state is SessionState.DELETED:
            raise SessionDeletedError("Session has been deleted; use a new session_id")
        expected_backend = record.metadata.get("runtime_backend")
        current_backend = type(self.runtime).__name__
        if expected_backend and expected_backend != current_backend:
            raise SessionBackendMismatchError(
                f"Session belongs to {expected_backend}; use a new session_id for {current_backend}"
            )
        desired_metadata: dict[str, object] = {}
        # REST-created browser sessions are deliberately created before their
        # first prompt.  Persist that first prompt's system instruction here so
        # resumed turns retain the same conversation contract.
        if system_prompt and record.metadata.get("system_prompt") is None:
            desired_metadata["system_prompt"] = system_prompt
        if model and record.last_model != model:
            desired_metadata["last_model"] = model
        if effort is not None and record.last_effort != effort:
            desired_metadata["last_effort"] = effort
        if desired_metadata:

            def merge_metadata(latest: SessionRecord) -> SessionRecord:
                if latest.state is SessionState.DELETED:
                    raise SessionDeletedError("Session has been deleted; use a new session_id")
                values = dict(desired_metadata)
                if latest.metadata.get("system_prompt") is not None:
                    values.pop("system_prompt", None)
                return latest.with_metadata(**values)

            record = await self._update_with_retry(session_id, merge_metadata)
        context = await self._context(record, system_prompt, model, provider, effort)
        if record.state is SessionState.PAUSED:
            record = await self._update_with_retry(
                session_id,
                lambda latest: (
                    transition(latest, SessionState.ACTIVE).with_metadata(
                        resume_pending=True, pause_pending=False
                    )
                    if latest.state is SessionState.PAUSED
                    else latest
                ),
            )
        # Resume is deliberately idempotent and also reconciles an ACTIVE
        # database row left behind by a process interruption during a transition.
        try:
            await self._sandbox_call(self.sandbox.resume(record.sandbox_id or ""))
            await self._resume_runtime(record, context)
        except BaseException:
            await self._update_with_retry(
                session_id, lambda latest: latest.with_metadata(resume_pending=True)
            )
            raise
        if record.metadata.get("resume_pending") or record.metadata.get("pause_pending"):
            record = await self._update_with_retry(
                session_id,
                lambda latest: latest.with_metadata(resume_pending=False, pause_pending=False),
            )
        return record, context

    @staticmethod
    def _interaction_text(event: InteractionRequest) -> str:
        title = "需要你的选择" if event.kind == "choice" else "需要你的确认"
        options = "\n".join(f"{chr(65 + i)}. {value}" for i, value in enumerate(event.options))
        return f"{title}：{event.prompt}\n\n{options}\n\n请回复选项字母或自然语言。\n"

    async def _run_turn_events(
        self,
        request: SessionTurnRequest,
        session_id: str,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
    ) -> AsyncIterator[TextDelta | Progress | SessionTurnCompleted]:
        fingerprint: str | None = None
        completed = False
        runtime_started = False
        visible_output: list[str] = []
        input_tokens: int | None = None
        output_tokens: int | None = None
        stop_reason = "未完成"
        final_progress: Progress | None = None
        finalizing_started_at: float | None = None
        preparing_started_at: float | None = None
        started_at = time.monotonic()
        message = request.message
        system_prompt = request.system_prompt
        try:
            await self._append_log(
                session_id,
                title="用户消息",
                content=message,
                metadata={"模型": request.model},
            )
            preparing_started_at = time.monotonic()
            progress = Progress("starting", "正在准备会话", "started")
            await self._append_progress_log(session_id, progress)
            yield progress
            record, context = await self._prepare(
                session_id, system_prompt, request.model, provider, effort
            )
            fingerprint = hashlib.sha256(message.encode()).hexdigest()
            yield Progress(
                "starting",
                "会话已准备",
                "completed",
                duration_seconds=time.monotonic() - preparing_started_at,
            )
            progress = Progress(
                "thinking", "正在分析请求", "running", elapsed_seconds=time.monotonic() - started_at
            )
            yield progress
            runtime_events = self.runtime.send_message(
                record.claude_session_id or "", message, context
            )
            try:
                async for event in runtime_events:
                    # A runner's local "starting" notification precedes the provider call,
                    # so it cannot prove that a resumable provider transcript exists.
                    runtime_started = (
                        runtime_started
                        or isinstance(event, (TextDelta, InteractionRequest, Usage, Completed))
                        or (
                            isinstance(event, Progress)
                            and event.phase == "starting"
                            and event.status == "completed"
                        )
                    )
                    if isinstance(event, TextDelta):
                        visible_output.append(event.text)
                        yield event
                    elif isinstance(event, Progress):
                        await self._append_progress_log(session_id, event)
                        yield event
                    elif isinstance(event, Diagnostic):
                        await self._append_diagnostic_log(session_id, event)
                    elif isinstance(event, InteractionRequest):
                        interaction = self._interaction_text(event)
                        visible_output.append(interaction)
                        yield TextDelta(interaction)
                    elif isinstance(event, Failed):
                        failure = f"\nRuntime 错误 [{event.code}]：{event.message}\n"
                        visible_output.append(failure)
                        stop_reason = f"失败：{event.code}"
                        yield Progress(
                            "finalizing",
                            "任务执行失败",
                            "failed",
                            elapsed_seconds=time.monotonic() - started_at,
                        )
                        yield TextDelta(failure)
                        break
                    elif isinstance(event, Usage):
                        input_tokens = event.input_tokens
                        output_tokens = event.output_tokens
                    elif isinstance(event, Completed):
                        stop_reason = event.stop_reason
                        completed = True
                        break
            finally:
                await runtime_events.aclose()
        finally:
            finalizing_started_at = time.monotonic()
            try:
                latest = await self.repository.get(session_id)
                if (
                    fingerprint is not None
                    and latest is not None
                    and latest.state is SessionState.ACTIVE
                ):

                    def finish_turn(current: SessionRecord) -> SessionRecord:
                        if current.state is not SessionState.ACTIVE:
                            return current
                        return touch(current).with_metadata(
                            last_user_fingerprint=fingerprint,
                            last_turn_completed=completed,
                            runtime_started=bool(
                                current.metadata.get("runtime_started", False) or runtime_started
                            ),
                        )

                    await self._update_with_retry(session_id, finish_turn)
            finally:
                final_progress = Progress(
                    "finalizing",
                    "正在整理结果",
                    "completed" if completed else "failed",
                    elapsed_seconds=time.monotonic() - started_at,
                    duration_seconds=time.monotonic() - finalizing_started_at,
                )
                await self._append_progress_log(session_id, final_progress)
                await self._append_log(
                    session_id,
                    title="Claude Code 输出",
                    content="".join(visible_output) or "（无可见输出）",
                    metadata={
                        "模型": request.model,
                        "完成": completed,
                        "结束原因": stop_reason,
                        "输入 tokens": input_tokens if input_tokens is not None else "未提供",
                        "输出 tokens": output_tokens if output_tokens is not None else "未提供",
                        "总耗时秒": round(time.monotonic() - started_at, 3),
                    },
                )

        if final_progress is not None:
            yield final_progress
        yield SessionTurnCompleted(
            completed,
            stop_reason,
            input_tokens,
            output_tokens,
            time.monotonic() - started_at,
        )

    async def stream_events(
        self,
        request: SessionTurnRequest,
        session_id: str,
        *,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
    ) -> AsyncIterator[TextDelta | Progress | SessionTurnCompleted]:
        """Reserve a session and expose structured events for WebSocket clients."""
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("This session already has an active turn")
        await lock.acquire()
        inner = self._run_turn_events(request, session_id, provider, effort)
        return _SessionEventLease(inner, lock)

    @staticmethod
    def _upload_target(workspace: Path, filename: str) -> tuple[Path, str]:
        relative = PurePosixPath(filename.replace("\\", "/"))
        if (
            not filename
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.parts[0].startswith(".")
        ):
            raise InvalidWorkspacePathError(f"Invalid workspace path: {filename!r}")
        normalized = relative.as_posix()
        root = workspace.resolve()
        target = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise InvalidWorkspacePathError(
                    f"Workspace upload paths cannot contain symbolic links: {filename!r}"
                )
        if not target.resolve(strict=False).is_relative_to(root):
            raise InvalidWorkspacePathError(f"Invalid workspace path: {filename!r}")
        return target, normalized

    async def upload_files(
        self,
        session_id: str,
        files: Sequence[tuple[str, bytes]],
        *,
        max_files_per_session: int = 10,
    ) -> list[dict[str, object]]:
        if not files:
            raise InvalidWorkspacePathError("At least one file is required")
        if max_files_per_session <= 0:
            raise ValueError("max_files_per_session must be positive")
        # Serialize uploads separately before touching the turn lock.  This
        # lets concurrent browser uploads observe the committed count in order,
        # while an actual agent turn still rejects uploads immediately below.
        upload_lock = self._upload_locks.setdefault(session_id, asyncio.Lock())
        async with upload_lock:
            return await self._upload_files_locked(session_id, files, max_files_per_session)

    async def _upload_files_locked(
        self,
        session_id: str,
        files: Sequence[tuple[str, bytes]],
        max_files_per_session: int,
    ) -> list[dict[str, object]]:
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot upload files while the session is running")
        async with lock:
            record, context = await self._prepare(session_id, None)
            count = record.metadata.get("uploaded_file_count", 0)
            current_count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            if current_count < 0:
                current_count = 0
            next_count = current_count + len(files)
            if next_count > max_files_per_session:
                raise FileUploadLimitError(
                    f"Uploading {len(files)} file(s) would exceed the "
                    f"{max_files_per_session}-file session limit"
                )
            uploaded: list[dict[str, object]] = []
            for filename, content in files:
                target, normalized = self._upload_target(context.workspace, filename)
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_bytes, content)
                uploaded.append({"path": normalized, "size": len(content)})
            await self._update_with_retry(
                session_id,
                lambda latest: latest.with_metadata(uploaded_file_count=next_count),
            )
            await self._append_log(
                session_id,
                title="上传文件",
                content="\n".join(f"{item['path']} ({item['size']} bytes)" for item in uploaded),
                metadata={"文件数": len(uploaded)},
            )
            return uploaded

    async def list_files(self, session_id: str) -> list[dict[str, object]]:
        record = await self.get(session_id)
        if record.state is SessionState.DELETED:
            raise SessionDeletedError(session_id)
        context = await self._context(record)

        def scan() -> list[dict[str, object]]:
            root = context.workspace.resolve()
            visible: list[dict[str, object]] = []
            for directory, dirnames, filenames in os.walk(
                root, topdown=True, onerror=lambda _error: None, followlinks=False
            ):
                directory_path = Path(directory)
                kept_directories: list[str] = []
                for name in dirnames:
                    if name.startswith(".") or name == "__pycache__":
                        continue
                    try:
                        if (directory_path / name).is_symlink():
                            continue
                    except OSError:
                        continue
                    kept_directories.append(name)
                dirnames[:] = sorted(kept_directories)
                for name in sorted(filenames):
                    if name.startswith(".") or name.endswith(".pyc"):
                        continue
                    path = directory_path / name
                    try:
                        relative = path.relative_to(root)
                        if any(
                            part.startswith(".") or part == "__pycache__" for part in relative.parts
                        ):
                            continue
                        if path.is_symlink():
                            continue
                        resolved = path.resolve(strict=True)
                        if not resolved.is_relative_to(root):
                            continue
                        metadata = path.stat(follow_symlinks=False)
                    except (FileNotFoundError, OSError, RuntimeError, ValueError):
                        # Agent tools may create, rename, or remove entries while
                        # this observational scan is in progress.
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        visible.append({"path": relative.as_posix(), "size": metadata.st_size})
            return sorted(visible, key=lambda item: str(item["path"]))

        return await asyncio.to_thread(scan)

    @staticmethod
    def _normalize_visible_file_path(file_path: str) -> tuple[tuple[str, ...], str]:
        portable_path = file_path.replace("\\", "/")
        path_segments = portable_path.split("/")
        relative = PurePosixPath(portable_path)
        if (
            not portable_path
            or not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in path_segments)
            or any(part.startswith(".") or part == "__pycache__" for part in path_segments)
            or relative.name.endswith(".pyc")
        ):
            raise InvalidWorkspacePathError(f"Invalid workspace path: {file_path!r}")
        return relative.parts, relative.as_posix()

    @staticmethod
    def _open_workspace_file(workspace: Path, file_path: str) -> OpenedWorkspaceFile:
        parts, normalized = SessionService._normalize_visible_file_path(file_path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

        def translate_open_error(exc: OSError, parent_fd: int, name: str) -> NoReturn:
            if exc.errno == errno.ELOOP:
                raise InvalidWorkspacePathError(
                    f"Workspace file paths cannot contain symbolic links: {normalized!r}"
                ) from None
            if exc.errno == errno.ENOTDIR:
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    metadata = None
                if metadata is not None and stat.S_ISLNK(metadata.st_mode):
                    raise InvalidWorkspacePathError(
                        f"Workspace file paths cannot contain symbolic links: {normalized!r}"
                    ) from None
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ESTALE}:
                raise SessionNotFoundError(f"Workspace file does not exist: {normalized}") from None
            raise SessionServiceError(f"Could not open workspace file: {normalized}") from exc

        try:
            directory_fd = os.open(workspace, directory_flags)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ESTALE}:
                raise SessionNotFoundError(f"Workspace file does not exist: {normalized}") from None
            if exc.errno == errno.ELOOP:
                raise InvalidWorkspacePathError(
                    "Workspace root cannot be a symbolic link"
                ) from None
            raise SessionServiceError(f"Could not open workspace file: {normalized}") from exc

        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    translate_open_error(exc, directory_fd, part)
                os.close(directory_fd)
                directory_fd = next_fd

            try:
                file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
            except OSError as exc:
                translate_open_error(exc, directory_fd, parts[-1])
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SessionNotFoundError(f"Workspace file does not exist: {normalized}")
                return OpenedWorkspaceFile(file_fd, normalized)
            except BaseException:
                os.close(file_fd)
                raise
        finally:
            os.close(directory_fd)

    async def open_file(self, session_id: str, file_path: str) -> OpenedWorkspaceFile:
        """Open one visible regular file without taking the session turn lock."""
        record = await self.get(session_id)
        if record.state is SessionState.DELETED:
            raise SessionDeletedError(session_id)
        context = await self._context(record)
        return await asyncio.to_thread(self._open_workspace_file, context.workspace, file_path)

    @staticmethod
    def _editor_language(path: str, mime_type: str) -> str:
        suffix = PurePosixPath(path).suffix.lower()
        languages = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".json": "json",
            ".html": "html",
            ".htm": "html",
            ".css": "css",
            ".md": "markdown",
            ".sh": "shell",
            ".bash": "shell",
            ".yml": "yaml",
            ".yaml": "yaml",
            ".xml": "xml",
            ".sql": "sql",
            ".java": "java",
            ".c": "c",
            ".h": "c",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".go": "go",
            ".rs": "rust",
            ".rb": "ruby",
            ".php": "php",
            ".toml": "toml",
        }
        if suffix in languages:
            return languages[suffix]
        if mime_type in {"application/json", "application/xml"}:
            return "json" if mime_type == "application/json" else "xml"
        return "plaintext"

    @staticmethod
    def _is_text_content(content: bytes) -> bool:
        if not content:
            return True
        if b"\0" in content:
            return False
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        controls = sum(
            (ord(char) < 32 and char not in "\n\r\t")
            or ord(char) == 0x7F
            or 0x80 <= ord(char) <= 0x9F
            for char in decoded
        )
        return controls / max(len(decoded), 1) <= 0.02

    async def inspect_editor_file(
        self, session_id: str, file_path: str, *, max_editor_bytes: int
    ) -> dict[str, object]:
        """Classify a safely opened inode for the text editor without a turn lock."""
        if max_editor_bytes <= 0:
            raise ValueError("max_editor_bytes must be positive")
        opened = await self.open_file(session_id, file_path)
        try:
            metadata = opened.stat()
            mime_type = (
                mimetypes.guess_type(opened.normalized_path)[0] or "application/octet-stream"
            )
            language = self._editor_language(opened.normalized_path, mime_type)
            # A bounded sample is enough to reject obvious binary data; never return
            # large content or a revision that could later be mistaken for editable.
            sample_limit = (
                max_editor_bytes + 1
                if metadata.st_size <= max_editor_bytes
                else min(max_editor_bytes, 64 * 1024)
            )
            content = await asyncio.to_thread(opened.read_all, sample_limit)
            base = {
                "path": opened.normalized_path,
                "size": metadata.st_size,
                "mime_type": mime_type,
                "language": language,
                "max_editor_bytes": max_editor_bytes,
            }
            if metadata.st_size > max_editor_bytes or len(content) > max_editor_bytes:
                return {
                    **base,
                    "kind": "text" if self._is_text_content(content) else "binary",
                    "editable": False,
                    "reason": "too_large",
                }
            if not self._is_text_content(content):
                return {**base, "kind": "binary", "editable": False, "reason": "binary"}
            return {
                **base,
                "kind": "text",
                "editable": True,
                "reason": None,
                "content": content.decode("utf-8"),
                "revision": hashlib.sha256(content).hexdigest(),
            }
        finally:
            opened.close()

    @staticmethod
    def _editor_digest(source_fd: int, max_bytes: int) -> str:
        """Hash an editor file with a hard read bound, never buffering it all."""
        metadata = os.fstat(source_fd)
        if metadata.st_size > max_bytes:
            raise FileTooLargeError("Existing file exceeds the editor file-size limit")
        digest = hashlib.sha256()
        total = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, min(64 * 1024, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise FileTooLargeError("Existing file exceeds the editor file-size limit")
            digest.update(chunk)
        return digest.hexdigest()

    def _replace_editor_file(
        self,
        workspace: Path,
        file_path: str,
        content: bytes,
        expected: str,
        force: bool,
        max_editor_bytes: int,
    ) -> dict[str, object]:
        """Atomically replace a visible regular file using descriptor-relative paths."""
        parts, normalized = SessionService._normalize_visible_file_path(file_path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

        def open_checked(name: str, flags: int, parent_fd: int) -> int:
            try:
                return os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise InvalidWorkspacePathError(
                        f"Workspace file paths cannot contain symbolic links: {normalized!r}"
                    ) from None
                if exc.errno == errno.ENOTDIR:
                    try:
                        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except OSError:
                        metadata = None
                    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
                        raise InvalidWorkspacePathError(
                            f"Workspace file paths cannot contain symbolic links: {normalized!r}"
                        ) from None
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ESTALE}:
                    raise SessionNotFoundError(
                        f"Workspace file does not exist: {normalized}"
                    ) from None
                raise SessionServiceError(f"Could not open workspace file: {normalized}") from exc

        try:
            directory_fd = os.open(workspace, directory_flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise InvalidWorkspacePathError(
                    "Workspace root cannot be a symbolic link"
                ) from None
            raise SessionServiceError(f"Could not open workspace file: {normalized}") from exc
        temporary_name: str | None = None
        try:
            for part in parts[:-1]:
                next_fd = open_checked(part, directory_flags, directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            source_fd = open_checked(parts[-1], file_flags, directory_fd)
            try:
                metadata = os.fstat(source_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SessionNotFoundError(f"Workspace file does not exist: {normalized}")
                actual = self._editor_digest(source_fd, max_editor_bytes)
                if actual != expected and not force:
                    raise FileChangedError("The file changed since it was opened")
            finally:
                os.close(source_fd)
            temporary_name = f".webagent-editor-{os.getpid()}-{time.time_ns()}"
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                stat.S_IMODE(metadata.st_mode),
                dir_fd=directory_fd,
            )
            try:
                written = 0
                while written < len(content):
                    written += os.write(temporary_fd, content[written:])
                os.fchmod(temporary_fd, stat.S_IMODE(metadata.st_mode))
                os.fsync(temporary_fd)
                temporary_metadata = os.fstat(temporary_fd)
            finally:
                os.close(temporary_fd)
            # The temporary inode is now durable.  Make the last observation
            # of the visible path immediately before the replacement, so a
            # concurrent rename during hashing or temporary-file creation is
            # rejected instead of overwritten.
            visible = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise FileChangedError("The file was replaced while it was being saved")
            if self._editor_revision_checked_hook is not None:
                self._editor_revision_checked_hook()
            _rename_exchange(directory_fd, temporary_name, parts[-1])
            displaced = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            if (displaced.st_dev, displaced.st_ino) != (metadata.st_dev, metadata.st_ino):
                # The name changed after the last observation.  The exchange
                # preserved that inode under temporary_name, so restore it
                # instead of losing it to an unconditional replacement.
                current = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (
                    temporary_metadata.st_dev,
                    temporary_metadata.st_ino,
                ):
                    raise SessionServiceError("Editor target changed during atomic replacement")
                _rename_exchange(directory_fd, temporary_name, parts[-1])
                raise FileChangedError("The file was replaced while it was being saved")
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_name = None
            return {
                "path": normalized,
                "size": len(content),
                "revision": hashlib.sha256(content).hexdigest(),
            }
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ESTALE}:
                raise SessionNotFoundError(f"Workspace file does not exist: {normalized}") from None
            if exc.errno == errno.ELOOP:
                raise InvalidWorkspacePathError(
                    f"Workspace file paths cannot contain symbolic links: {normalized!r}"
                ) from None
            raise SessionServiceError(f"Could not replace workspace file: {normalized}") from exc
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    async def save_editor_file(
        self,
        session_id: str,
        file_path: str,
        *,
        content: str,
        expected_revision: str,
        force: bool,
        max_editor_bytes: int,
    ) -> dict[str, object]:
        encoded = content.encode("utf-8")
        if len(encoded) > max_editor_bytes:
            raise FileTooLargeError("Text content exceeds the editor file-size limit")
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot edit files while the session is running")
        async with lock:
            record = await self.get(session_id)
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            context = await self._context(record)
            return await asyncio.to_thread(
                self._replace_editor_file,
                context.workspace,
                file_path,
                encoded,
                expected_revision,
                force,
                max_editor_bytes,
            )

    async def get(self, session_id: str) -> SessionRecord:
        record = await self.repository.get(session_id)
        if record is None:
            raise SessionNotFoundError(session_id)
        return record

    async def pause(self, session_id: str) -> SessionRecord:
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot pause a running session")
        async with lock:
            record = await self.get(session_id)
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            if record.state is SessionState.ACTIVE:
                # Pause is a compensating workflow: make the lifecycle decision
                # durable before touching either external backend.  A later
                # reaper tick can replay all idempotent effects if inspect,
                # runtime.pause, or sandbox.pause fails partway through.
                record = await self._update_with_retry(
                    session_id,
                    lambda latest: (
                        transition(latest, SessionState.PAUSED).with_metadata(pause_pending=True)
                        if latest.state is SessionState.ACTIVE
                        else latest
                    ),
                )
            if record.state is SessionState.PAUSED and not record.metadata.get("pause_pending"):
                return record

            try:
                context = await self._context(record)
                await self.runtime.pause(record.claude_session_id or "", context)
                await self._sandbox_call(self.sandbox.pause(record.sandbox_id or ""))
            except Exception:
                # The PAUSED/pending marker was written before the first side
                # effect.  Do not overwrite it here: preserving it makes every
                # partial failure discoverable and replayable by the reaper.
                raise
            return await self._update_with_retry(
                session_id, lambda latest: latest.with_metadata(pause_pending=False)
            )

    async def resume(self, session_id: str) -> SessionRecord:
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot resume a running session")
        async with lock:
            record = await self.get(session_id)
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            if record.state is SessionState.PAUSED:
                record = await self._update_with_retry(
                    session_id,
                    lambda latest: (
                        transition(latest, SessionState.ACTIVE).with_metadata(
                            resume_pending=True, pause_pending=False
                        )
                        if latest.state is SessionState.PAUSED
                        else latest
                    ),
                )
            context = await self._context(record)
            try:
                await self._sandbox_call(self.sandbox.resume(record.sandbox_id or ""))
                await self._resume_runtime(record, context)
            except BaseException:
                await self._update_with_retry(
                    session_id, lambda latest: latest.with_metadata(resume_pending=True)
                )
                raise
            return await self._update_with_retry(
                session_id,
                lambda latest: latest.with_metadata(resume_pending=False, pause_pending=False),
            )

    async def delete(self, session_id: str) -> SessionRecord:
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot delete a running session")
        deleted: SessionRecord | None = None
        async with lock:
            record = await self.get(session_id)
            if (
                record.state is SessionState.DELETED
                and record.metadata.get("cleanup_pending") is False
            ):
                deleted = record
            else:

                def tombstone(latest: SessionRecord) -> SessionRecord:
                    if latest.state is SessionState.DELETED:
                        return latest.with_metadata(cleanup_pending=True)
                    return transition(latest, SessionState.DELETED).with_metadata(
                        cleanup_pending=True
                    )

                deleted = await self._update_with_retry(session_id, tombstone)
                info = await self._sandbox_call(self.sandbox.inspect(deleted.sandbox_id or ""))
                if info is not None and info.state != "deleted":
                    context = await self._context(deleted)
                    await self.runtime.close(deleted.claude_session_id or "", context)
                # Deletion is idempotent and must run even when inspect no
                # longer finds the container, so manager-specific workspace
                # cleanup still gets a chance to run.
                await self._sandbox_call(
                    self.sandbox.delete(deleted.sandbox_id or "", self.delete_workspace)
                )
                deleted = await self._update_with_retry(
                    session_id,
                    lambda latest: latest.with_metadata(cleanup_pending=False),
                )
        # A successful tombstone has no in-process work left to serialize.  Do
        # this only after releasing the lock so the registry can actually drop it.
        self.locks.discard_if_idle(session_id)
        return deleted
