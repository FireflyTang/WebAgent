from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from app.api.sessions import _OpenedWorkspaceFileResponse
from app.runtime.base import ProviderConfig, RuntimeContext
from app.runtime.events import (
    Completed,
    Diagnostic,
    Failed,
    Progress,
    RuntimeEvent,
    TextDelta,
    Usage,
)
from app.sandbox.local import LocalSandboxManager
from app.sessions import SessionLockRegistry, SQLiteSessionRepository
from app.sessions.models import SessionRecord, SessionState, SessionTurnRequest, utc_now
from app.sessions.reaper import LifecycleReaper
from app.sessions.service import (
    InvalidWorkspacePathError,
    OpenedWorkspaceFile,
    SandboxUnavailableError,
    SessionBusyError,
    SessionNotFoundError,
    SessionService,
    SessionTurnCompleted,
)


class ProgressRuntime:
    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        return "progress-runtime"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        yield Progress("tool", "正在运行测试", "started", tool_name="Bash", tool_use_id="tool-1")
        yield TextDelta("visible answer")
        yield Usage(input_tokens=3, output_tokens=5)
        yield Completed("stop")


class ImmediateFailureRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.restore_started: list[bool] = []

    async def restore_session_state(self, runtime_session_id: str, *, started: bool) -> None:
        del runtime_session_id
        self.restore_started.append(started)

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message, context
        # This mirrors a runner which has announced local startup but failed
        # before the provider established a resumable transcript.
        yield Progress("starting", "正在启动 runner", "started")
        yield Failed("agent_sdk_failed", "provider unavailable", retryable=True)


class ProviderContextRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.created: list[ProviderConfig | None] = []
        self.resumed: list[ProviderConfig | None] = []
        self.sent: list[ProviderConfig | None] = []

    async def create_session(self, context: RuntimeContext) -> str:
        self.created.append(context.provider)
        return await super().create_session(context)

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        self.resumed.append(context.provider)

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message
        self.sent.append(context.provider)
        yield Completed()


class EffortContextRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.created: list[str | None] = []
        self.resumed: list[str | None] = []
        self.sent: list[str | None] = []

    async def create_session(self, context: RuntimeContext) -> str:
        self.created.append(context.effort)
        return await super().create_session(context)

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        self.resumed.append(context.effort)

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message
        self.sent.append(context.effort)
        yield Completed()


class FailingLogger:
    async def append(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise sqlite3.OperationalError("database is temporarily unavailable")


class FinalizingLogger:
    def __init__(self) -> None:
        self.remaining_failures = 1

    async def append(self, session_id: str, *, title: str, **kwargs: object) -> None:
        del session_id, kwargs
        if title == "Claude Code 输出" and self.remaining_failures:
            self.remaining_failures -= 1
            raise ValueError("injected final log formatting failure")


class PromptContextRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.prompts: list[str | None] = []

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message
        self.prompts.append(context.system_prompt)
        yield Completed()


class CloseFailsOnceRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("temporary runtime close failure")


class PauseTrackingRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.pause_calls = 0

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context
        self.pause_calls += 1


class LifecycleTrackingRuntime(ProgressRuntime):
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.resume_failures = 0

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        self.closed.append(runtime_session_id)

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context
        if self.resume_failures:
            self.resume_failures -= 1
            raise RuntimeError("temporary resume failure")


class InspectFailsOnceSandbox(LocalSandboxManager):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.fail_next_inspect = True

    async def inspect(self, sandbox_id: str):
        if self.fail_next_inspect:
            self.fail_next_inspect = False
            raise RuntimeError("temporary inspect failure")
        return await super().inspect(sandbox_id)


@pytest.mark.asyncio
async def test_workspace_reads_bypass_running_turn_lock_but_upload_stays_busy(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    locks = SessionLockRegistry()
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    service = SessionService(repository, locks, sandbox, ProgressRuntime())
    record = await service.create_empty("running-files")
    workspace = tmp_path / "workspaces" / (record.sandbox_id or "")
    (workspace / "result.txt").write_text("partial result", encoding="utf-8")
    lock = locks.lock_for("running-files")
    await lock.acquire()
    try:
        assert await service.list_files("running-files") == [{"path": "result.txt", "size": 14}]
        opened = await service.open_file("running-files", "result.txt")
        assert opened.normalized_path == "result.txt"
        assert b"".join([chunk async for chunk in opened.chunks()]) == b"partial result"
        assert opened.closed
        with pytest.raises(SessionBusyError, match="Cannot upload"):
            await service.upload_files("running-files", [("new.txt", b"write")])
    finally:
        lock.release()
        await repository.close()


@pytest.mark.asyncio
async def test_workspace_scan_and_file_resolution_tolerate_disappearing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    service = SessionService(repository, SessionLockRegistry(), sandbox, ProgressRuntime())
    record = await service.create_empty("disappearing-files")
    workspace = tmp_path / "workspaces" / (record.sandbox_id or "")
    (workspace / "stable.txt").write_text("stable", encoding="utf-8")
    vanishing = workspace / "vanishing.txt"
    vanishing.write_text("gone", encoding="utf-8")
    original_stat = Path.stat

    def disappearing_stat(path: Path, *, follow_symlinks: bool = True):
        if path == vanishing:
            vanishing.unlink(missing_ok=True)
            raise FileNotFoundError(vanishing)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", disappearing_stat)
    assert await service.list_files("disappearing-files") == [{"path": "stable.txt", "size": 6}]
    monkeypatch.setattr(Path, "stat", original_stat)

    target = workspace / "read-race.txt"
    target.write_text("gone soon", encoding="utf-8")
    target.unlink()
    with pytest.raises(SessionNotFoundError, match="does not exist"):
        await service.open_file("disappearing-files", "read-race.txt")
    await repository.close()


@pytest.mark.asyncio
async def test_open_workspace_file_stays_on_original_inode_after_path_replacement(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
    )
    record = await service.create_empty("inode-race")
    workspace = tmp_path / "workspaces" / (record.sandbox_id or "")
    target = workspace / "answer.txt"
    target.write_bytes(b"validated inode")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"host secret")

    opened = await service.open_file("inode-race", "answer.txt")
    target.unlink()
    target.symlink_to(outside)

    assert b"".join([chunk async for chunk in opened.chunks()]) == b"validated inode"
    assert opened.closed
    with pytest.raises(InvalidWorkspacePathError, match="symbolic links"):
        await service.open_file("inode-race", "answer.txt")
    await repository.close()


@pytest.mark.asyncio
async def test_open_workspace_file_rejects_hidden_and_directory_symlink_paths(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
    )
    record = await service.create_empty("visible-files")
    workspace = tmp_path / "workspaces" / (record.sandbox_id or "")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    (workspace / ".hidden").mkdir()
    (workspace / ".hidden" / "secret.txt").write_text("hidden", encoding="utf-8")

    with pytest.raises(InvalidWorkspacePathError, match="symbolic links"):
        await service.open_file("visible-files", "escape/secret.txt")
    with pytest.raises(InvalidWorkspacePathError, match="Invalid workspace path"):
        await service.open_file("visible-files", ".hidden/secret.txt")
    await repository.close()


@pytest.mark.asyncio
async def test_opened_workspace_file_closes_on_stream_cancel(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * (OpenedWorkspaceFile.chunk_size + 1))
    opened = OpenedWorkspaceFile(os.open(path, os.O_RDONLY), "large.txt")
    chunks = opened.chunks()

    assert await anext(chunks) == b"x" * OpenedWorkspaceFile.chunk_size
    await chunks.aclose()

    assert opened.closed


@pytest.mark.asyncio
async def test_workspace_file_response_closes_before_body_start_on_response_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "response.txt"
    path.write_bytes(b"response")
    opened = OpenedWorkspaceFile(os.open(path, os.O_RDONLY), "response.txt")
    response = _OpenedWorkspaceFileResponse(
        opened, media_type="text/plain", headers={"Content-Disposition": "inline"}
    )

    async def fail_before_stream(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("response setup failed")

    monkeypatch.setattr(StreamingResponse, "__call__", fail_before_stream)
    with pytest.raises(RuntimeError, match="response setup failed"):
        await response({}, _unused_receive, _unused_send)

    assert opened.closed


@pytest.mark.asyncio
async def test_workspace_file_response_closes_on_client_disconnect(tmp_path: Path) -> None:
    path = tmp_path / "disconnect.txt"
    path.write_bytes(b"response")
    opened = OpenedWorkspaceFile(os.open(path, os.O_RDONLY), "disconnect.txt")
    response = _OpenedWorkspaceFileResponse(
        opened, media_type="text/plain", headers={"Content-Disposition": "inline"}
    )

    async def disconnect_on_body(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            _unused_receive,
            disconnect_on_body,
        )

    assert opened.closed


async def _unused_receive() -> dict[str, object]:
    return {"type": "http.disconnect"}


async def _unused_send(_message: dict[str, object]) -> None:
    return None


@pytest.mark.asyncio
async def test_structured_stream_exposes_progress_and_terminal_summary(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
    )
    request = SessionTurnRequest(message="run", model="test-model")

    structured = await service.stream_events(request, "progress-session")
    events = [event async for event in structured]
    prepared = next(
        event
        for event in events
        if isinstance(event, Progress) and event.phase == "starting" and event.status == "completed"
    )
    assert prepared.duration_seconds is not None and prepared.duration_seconds >= 0
    assert any(isinstance(event, Progress) and event.phase == "tool" for event in events)
    done = next(event for event in events if isinstance(event, SessionTurnCompleted))
    assert done.completed and done.stop_reason == "stop"
    assert (done.input_tokens, done.output_tokens) == (3, 5)
    assert done.duration_seconds >= 0

    await repository.close()


@pytest.mark.asyncio
async def test_unstarted_structured_stream_owns_and_releases_session_lock(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
    )
    request = SessionTurnRequest(message="run", model="test-model")

    reserved = await service.stream_events(request, "never-started")
    with pytest.raises(SessionBusyError):
        await service.stream_events(request, "never-started")
    await reserved.aclose()

    next_turn = await service.stream_events(request, "never-started")
    await next_turn.aclose()
    await repository.close()


@pytest.mark.asyncio
async def test_repository_create_failure_compensates_runtime_and_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    runtime = LifecycleTrackingRuntime()
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)

    async def fail_create(record: SessionRecord) -> SessionRecord:
        del record
        raise sqlite3.OperationalError("injected repository create failure")

    monkeypatch.setattr(repository, "create", fail_create)
    with pytest.raises(sqlite3.OperationalError, match="injected repository"):
        await service.create_empty("compensated-create")

    sandbox_id = sandbox._id("compensated-create")
    assert runtime.closed == ["progress-runtime"]
    assert (await sandbox.inspect(sandbox_id)).state == "deleted"
    assert not (tmp_path / "workspaces" / sandbox_id).exists()
    await repository.close()


@pytest.mark.asyncio
async def test_repository_create_failure_preserves_reused_local_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    existing = await sandbox.create("reused-create")
    sentinel = existing.workspace / "keep.txt"
    sentinel.write_text("preexisting")
    runtime = LifecycleTrackingRuntime()
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)

    async def fail_create(record: SessionRecord) -> SessionRecord:
        del record
        raise sqlite3.OperationalError("injected repository create failure")

    monkeypatch.setattr(repository, "create", fail_create)
    with pytest.raises(sqlite3.OperationalError, match="injected repository"):
        await service.create_empty("reused-create")

    inspected = await sandbox.inspect(existing.sandbox_id)
    assert inspected is not None and inspected.state == "active"
    assert sentinel.read_text() == "preexisting"
    assert runtime.closed == ["progress-runtime"]
    await repository.close()


@pytest.mark.asyncio
async def test_repository_create_failure_preserves_preexisting_local_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    sandbox_id = sandbox._id("preexisting-workspace")
    workspace = tmp_path / "workspaces" / sandbox_id
    workspace.mkdir(parents=True)
    sentinel = workspace / "keep.txt"
    sentinel.write_text("preexisting")
    runtime = LifecycleTrackingRuntime()
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)

    async def fail_create(record: SessionRecord) -> SessionRecord:
        del record
        raise sqlite3.OperationalError("injected repository create failure")

    monkeypatch.setattr(repository, "create", fail_create)
    with pytest.raises(sqlite3.OperationalError, match="injected repository"):
        await service.create_empty("preexisting-workspace")

    inspected = await sandbox.inspect(sandbox_id)
    assert inspected is not None and inspected.state == "deleted"
    assert sentinel.read_text() == "preexisting"
    assert runtime.closed == ["progress-runtime"]
    await repository.close()


@pytest.mark.asyncio
async def test_finalizer_failure_still_releases_session_lock_for_next_turn(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
        html_logger=FinalizingLogger(),  # type: ignore[arg-type]
    )
    request = SessionTurnRequest(message="run", model="test-model")

    first = await service.stream_events(request, "finalizer-lock")
    assert isinstance(await anext(first), Progress)
    with pytest.raises(ValueError, match="injected final log formatting failure"):
        await first.aclose()

    second = await service.stream_events(request, "finalizer-lock")
    events = [event async for event in second]
    assert any(isinstance(event, SessionTurnCompleted) and event.completed for event in events)
    await repository.close()


@pytest.mark.asyncio
async def test_immediate_runtime_failure_is_not_completed_or_marked_resumable(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = ImmediateFailureRuntime()
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        runtime,
    )
    request = SessionTurnRequest(message="run", model="test-model")

    first = await service.stream_events(request, "failed-before-session")
    events = [event async for event in first]
    done = next(event for event in events if isinstance(event, SessionTurnCompleted))
    assert done.completed is False
    assert done.stop_reason == "失败：agent_sdk_failed"
    assert isinstance(events[-2], Progress) and events[-2].status == "failed"
    record = await repository.get("failed-before-session")
    assert record is not None
    assert record.metadata["runtime_started"] is False
    assert record.metadata["last_turn_completed"] is False

    second = await service.stream_events(request, "failed-before-session")
    _ = [event async for event in second]
    assert runtime.restore_started == [False, False]
    await repository.close()


@pytest.mark.asyncio
async def test_sqlite_log_failure_does_not_interrupt_a_turn(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
        html_logger=FailingLogger(),  # type: ignore[arg-type]
    )
    request = SessionTurnRequest(message="run", model="test-model")

    stream = await service.stream_events(request, "log-write-failure")
    events = [event async for event in stream]
    done = next(event for event in events if isinstance(event, SessionTurnCompleted))
    assert done.completed is True
    assert any(isinstance(event, TextDelta) and event.text == "visible answer" for event in events)
    await repository.close()


@pytest.mark.asyncio
async def test_diagnostic_log_failure_warns_without_logging_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
        html_logger=FailingLogger(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="app.sessions.service"):
        await service._append_diagnostic_log(
            "diagnostic-failure", Diagnostic("result", tool_result="credential-secret")
        )

    assert "session_id=diagnostic-failure" in caplog.text
    assert "error_type=OperationalError" in caplog.text
    assert "credential-secret" not in caplog.text
    await repository.close()


@pytest.mark.asyncio
async def test_session_provider_is_turn_scoped_and_not_durable_metadata(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = ProviderContextRuntime()
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        runtime,
    )
    request = SessionTurnRequest(message="run", model="test-model")
    first = ProviderConfig("https://first.example", "first-key", "ANTHROPIC_AUTH_TOKEN")
    second = ProviderConfig("https://second.example", "second-key", "ANTHROPIC_API_KEY")

    events = await service.stream_events(request, "provider-session", provider=first)
    _ = [event async for event in events]
    events = await service.stream_events(request, "provider-session", provider=second)
    _ = [event async for event in events]

    record = await repository.get("provider-session")
    assert runtime.created == [first]
    assert runtime.resumed == [first, second]
    assert runtime.sent == [first, second]
    assert record is not None
    assert "first-key" not in str(record.metadata)
    assert "second-key" not in str(record.metadata)
    await repository.close()


@pytest.mark.asyncio
async def test_session_effort_persists_and_next_resumed_turn_uses_patched_value(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = EffortContextRuntime()
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        runtime,
    )
    request = SessionTurnRequest(message="run", model="test-model")

    first = await service.stream_events(request, "effort-session", effort="medium")
    _ = [event async for event in first]
    updated = await service.update_presentation("effort-session", last_effort="high")
    second = await service.stream_events(request, "effort-session")
    _ = [event async for event in second]

    assert updated.last_effort == "high"
    assert runtime.created == ["medium"]
    assert runtime.resumed == ["medium", "high"]
    assert runtime.sent == ["medium", "high"]
    record = await repository.get("effort-session")
    assert record is not None and record.last_effort == "high"
    await repository.close()


@pytest.mark.asyncio
async def test_presentation_patch_retries_across_terminal_turn_update(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        ProgressRuntime(),
    )
    await service.create_empty("racing-presentation", title="新会话")
    request = SessionTurnRequest(message="run", model="test-model")
    original_update = repository.update
    final_update_ready = asyncio.Event()
    release_final_update = asyncio.Event()
    intercepted = False

    async def gated_update(record, *, expected_version=None):
        nonlocal intercepted
        if "last_user_fingerprint" in record.metadata and not intercepted:
            intercepted = True
            final_update_ready.set()
            await release_final_update.wait()
        return await original_update(record, expected_version=expected_version)

    repository.update = gated_update  # type: ignore[method-assign]
    stream = await service.stream_events(request, "racing-presentation")

    async def collect() -> list[RuntimeEvent | SessionTurnCompleted]:
        return [event async for event in stream]

    consumer = asyncio.create_task(collect())
    await final_update_ready.wait()
    updated = await service.update_presentation("racing-presentation", title="并发更新标题")
    release_final_update.set()
    events = await consumer

    assert updated.title == "并发更新标题"
    assert any(isinstance(event, SessionTurnCompleted) and event.completed for event in events)
    record = await repository.get("racing-presentation")
    assert record is not None
    assert record.title == "并发更新标题"
    assert record.metadata["last_turn_completed"] is True
    await repository.close()


@pytest.mark.asyncio
async def test_rest_created_session_persists_first_system_prompt_for_later_turns(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = PromptContextRuntime()
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        runtime,
    )
    await service.create_empty("web-created")
    first = SessionTurnRequest(message="first", model="test-model", system_prompt="始终使用中文")
    second = SessionTurnRequest(message="second", model="test-model", system_prompt="后续覆盖尝试")

    events = await service.stream_events(first, "web-created")
    _ = [event async for event in events]
    events = await service.stream_events(second, "web-created")
    _ = [event async for event in events]

    record = await repository.get("web-created")
    assert record is not None and record.metadata["system_prompt"] == "始终使用中文"
    assert runtime.prompts == ["始终使用中文", "始终使用中文"]
    await repository.close()


@pytest.mark.asyncio
async def test_reaper_immediately_retries_fresh_failed_tombstone_cleanup(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    locks = SessionLockRegistry()
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    runtime = CloseFailsOnceRuntime()
    service = SessionService(repository, locks, sandbox, runtime, delete_after_seconds=2)
    await service.create_empty("cleanup-retry")
    original_lock = locks.lock_for("cleanup-retry")

    with pytest.raises(RuntimeError, match="temporary runtime close failure"):
        await service.delete("cleanup-retry")
    failed = await repository.get("cleanup-retry")
    assert failed is not None
    assert failed.state is SessionState.DELETED
    assert failed.metadata["cleanup_pending"] is True
    assert (await sandbox.inspect(failed.sandbox_id or "")).state == "active"
    assert [record.session_id for record in await repository.list_pending_cleanup()] == [
        "cleanup-retry"
    ]

    reaper = LifecycleReaper(
        service, pause_after_seconds=1, delete_after_seconds=2, interval_seconds=1
    )
    await reaper.tick()

    diagnostics = reaper.diagnostics()
    assert diagnostics["last_tick_at"] is not None
    assert diagnostics["last_tick_completed_at"] is not None
    assert diagnostics["last_error_at"] is None

    cleaned = await repository.get("cleanup-retry")
    assert cleaned is not None and cleaned.metadata["cleanup_pending"] is False
    assert (await sandbox.inspect(cleaned.sandbox_id or "")).state == "deleted"
    assert runtime.close_calls == 2
    assert locks.lock_for("cleanup-retry") is not original_lock
    await repository.close()


@pytest.mark.asyncio
async def test_pause_is_db_first_and_reaper_replays_failed_inspect(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = PauseTrackingRuntime()
    sandbox = InspectFailsOnceSandbox(tmp_path / "workspaces")
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)
    await service.create_empty("pause-retry")

    with pytest.raises(SandboxUnavailableError, match="Sandbox backend is unavailable"):
        await service.pause("pause-retry")

    pending = await repository.get("pause-retry")
    assert pending is not None
    assert pending.state is SessionState.PAUSED
    assert pending.metadata["pause_pending"] is True
    assert runtime.pause_calls == 0

    reaper = LifecycleReaper(
        service, pause_after_seconds=1, delete_after_seconds=2, interval_seconds=1
    )
    await reaper.tick()

    paused = await repository.get("pause-retry")
    assert paused is not None
    assert paused.state is SessionState.PAUSED
    assert paused.metadata["pause_pending"] is False
    assert runtime.pause_calls == 1
    assert (await sandbox.inspect(paused.sandbox_id or "")).state == "paused"
    await repository.close()


@pytest.mark.asyncio
async def test_pause_success_is_idempotent_after_compensation_completes(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = PauseTrackingRuntime()
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)
    await service.create_empty("normal-pause")

    paused = await service.pause("normal-pause")
    repeated = await service.pause("normal-pause")

    assert paused.state is SessionState.PAUSED
    assert paused.metadata["pause_pending"] is False
    assert repeated == paused
    assert runtime.pause_calls == 1
    assert (await sandbox.inspect(paused.sandbox_id or "")).state == "paused"
    await repository.close()


@pytest.mark.asyncio
async def test_resume_failure_stays_pending_and_retry_clears_both_markers(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = LifecycleTrackingRuntime()
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)
    await service.create_empty("resume-retry")
    await service.pause("resume-retry")
    runtime.resume_failures = 1

    with pytest.raises(RuntimeError, match="temporary resume failure"):
        await service.resume("resume-retry")
    pending = await repository.get("resume-retry")
    assert pending is not None and pending.state is SessionState.ACTIVE
    assert pending.metadata["resume_pending"] is True
    assert pending.metadata["pause_pending"] is False

    resumed = await service.resume("resume-retry")
    assert resumed.metadata["resume_pending"] is False
    assert resumed.metadata["pause_pending"] is False
    await repository.close()


@pytest.mark.asyncio
async def test_reaper_retries_pending_resume_without_pausing_it_in_same_tick(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = LifecycleTrackingRuntime()
    sandbox = LocalSandboxManager(tmp_path / "workspaces")
    service = SessionService(repository, SessionLockRegistry(), sandbox, runtime)
    await service.create_empty("reaper-resume")
    await service.pause("reaper-resume")
    runtime.resume_failures = 1

    with pytest.raises(RuntimeError, match="temporary resume failure"):
        await service.resume("reaper-resume")
    pending = await repository.get("reaper-resume")
    assert pending is not None and pending.metadata["resume_pending"] is True
    stale = replace(pending, last_activity_at=utc_now() - timedelta(seconds=10))
    await repository.update(stale, expected_version=pending.version)

    reaper = LifecycleReaper(
        service, pause_after_seconds=1, delete_after_seconds=2, interval_seconds=1
    )
    await reaper.tick()

    resumed = await repository.get("reaper-resume")
    assert resumed is not None and resumed.state is SessionState.ACTIVE
    assert resumed.metadata["resume_pending"] is False
    assert resumed.metadata["pause_pending"] is False
    inspected = await sandbox.inspect(resumed.sandbox_id or "")
    assert inspected is not None and inspected.state == "active"
    await repository.close()


@pytest.mark.asyncio
async def test_reaper_continues_after_one_record_raises_unexpected_error() -> None:
    class Repository:
        async def list_due(self, before, *, states):
            del before, states
            return [SessionRecord(session_id="broken"), SessionRecord(session_id="later")]

        async def list_pending_cleanup(self):
            return []

    class Service:
        repository = Repository()

        def __init__(self) -> None:
            self.paused: list[str] = []

        async def pause(self, session_id: str) -> None:
            self.paused.append(session_id)
            if session_id == "broken":
                raise RuntimeError("unexpected runtime transport error")

        async def delete(self, session_id: str) -> None:
            del session_id

    service = Service()
    reaper = LifecycleReaper(
        service, pause_after_seconds=1, delete_after_seconds=2, interval_seconds=1
    )
    await reaper.tick()
    assert service.paused == ["broken", "later"]
