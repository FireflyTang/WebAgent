from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TypeVar

from app.openai_compat.schemas import ChatCompletionRequest
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
from .models import SessionRecord, SessionState, utc_now
from .repository import SessionLogEntry, SessionRepository, SessionVersionConflictError
from .runtime_debug import append_runtime_debug
from .state_machine import touch, transition

T = TypeVar("T")
logger = logging.getLogger(__name__)
_UNSET = object()


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


class _TextOnlyEventIterator(AsyncIterator[str]):
    """Project text deltas while retaining explicit ownership of the event lease."""

    def __init__(self, events: AsyncIterator[TextDelta | object]) -> None:
        self._events = events
        self._closed = False
        self._operation_lock = asyncio.Lock()

    def __aiter__(self) -> _TextOnlyEventIterator:
        return self

    async def __anext__(self) -> str:
        async with self._operation_lock:
            if self._closed:
                raise StopAsyncIteration
            try:
                while True:
                    event = await anext(self._events)
                    if isinstance(event, TextDelta):
                        return event.text
            except BaseException:
                await self._close_unlocked()
                raise

    async def _close_unlocked(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._events.aclose()
        except AttributeError:
            # AsyncIterator's protocol does not declare aclose, but all event
            # streams constructed by SessionService are explicit leases.
            return

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

    @staticmethod
    def _inputs(request: ChatCompletionRequest) -> tuple[str, str | None]:
        user = next(
            message.content for message in reversed(request.messages) if message.role == "user"
        )
        system = next(
            (message.content for message in request.messages if message.role == "system"), None
        )
        return user, system

    async def _create(
        self,
        session_id: str,
        system_prompt: str | None,
        provider: ProviderConfig | None = None,
        effort: Effort | None = None,
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
    ) -> SessionRecord:
        """Create the durable sandbox/runtime mapping before a first chat turn."""

        effort = validate_effort(last_effort)
        record = await self._create(session_id, None, effort=effort)
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
        request: ChatCompletionRequest,
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
        message, system_prompt = self._inputs(request)
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
        request: ChatCompletionRequest,
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

    async def stream(
        self,
        request: ChatCompletionRequest,
        session_id: str,
        *,
        provider: ProviderConfig | None = None,
    ) -> AsyncIterator[str]:
        events = await self.stream_events(request, session_id, provider=provider)
        return _TextOnlyEventIterator(events)

    async def complete(
        self,
        request: ChatCompletionRequest,
        session_id: str,
        *,
        provider: ProviderConfig | None = None,
    ) -> str:
        stream = await self.stream(request, session_id, provider=provider)
        return "".join([chunk async for chunk in stream])

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
    ) -> list[dict[str, object]]:
        if not files:
            raise InvalidWorkspacePathError("At least one file is required")
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot upload files while the session is running")
        async with lock:
            _, context = await self._prepare(session_id, None)
            uploaded: list[dict[str, object]] = []
            for filename, content in files:
                target, normalized = self._upload_target(context.workspace, filename)
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(target.write_bytes, content)
                uploaded.append({"path": normalized, "size": len(content)})
            await self._append_log(
                session_id,
                title="上传文件",
                content="\n".join(f"{item['path']} ({item['size']} bytes)" for item in uploaded),
                metadata={"文件数": len(uploaded)},
            )
            return uploaded

    async def list_files(self, session_id: str) -> list[dict[str, object]]:
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot list files while the session is running")
        async with lock:
            record = await self.get(session_id)
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            context = await self._context(record)

            def scan() -> list[dict[str, object]]:
                visible: list[dict[str, object]] = []
                for path in sorted(context.workspace.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(context.workspace)
                    if (
                        any(
                            part.startswith(".") or part == "__pycache__" for part in relative.parts
                        )
                        or path.suffix == ".pyc"
                    ):
                        continue
                    visible.append({"path": relative.as_posix(), "size": path.stat().st_size})
                return visible

            return await asyncio.to_thread(scan)

    async def file_path(self, session_id: str, file_path: str) -> tuple[Path, str]:
        """Return an existing user-visible file from this session's workspace."""
        lock = self.locks.lock_for(session_id)
        if lock.locked():
            raise SessionBusyError("Cannot read files while the session is running")
        async with lock:
            record = await self.get(session_id)
            if record.state is SessionState.DELETED:
                raise SessionDeletedError(session_id)
            context = await self._context(record)
            target, normalized = self._upload_target(context.workspace, file_path)

            def resolve_file() -> Path:
                if not target.is_file() or target.is_symlink():
                    raise SessionNotFoundError(f"Workspace file does not exist: {normalized}")
                return target

            return await asyncio.to_thread(resolve_file), normalized

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
