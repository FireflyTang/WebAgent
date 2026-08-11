"""Docker contract tests; run only on a host with the worker image available."""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.runtime.base import RuntimeContext
from app.sandbox.docker import DockerSandboxError, DockerSandboxManager
from app.sessions import SessionLockRegistry, SQLiteSessionRepository
from app.sessions.models import SessionRecord
from app.sessions.service import SessionService


async def _assert_no_sleep_17(manager: DockerSandboxManager, sandbox_id: str) -> None:
    probe = await manager.exec(
        sandbox_id,
        [
            "python",
            "-c",
            "import pathlib; print(sum(b'sleep\\x0017\\x00' in p.read_bytes() "
            "for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'))) ",
        ],
    )
    assert probe.exit_code == 0
    assert probe.stdout.strip() == "0"


async def _orphan_state(
    manager: DockerSandboxManager, sandbox_id: str, token: str
) -> tuple[int, int]:
    """Return this sandbox's worker marker and recognizable child process count."""
    probe = await manager.exec(
        sandbox_id,
        [
            "python",
            "-c",
            (
                "import pathlib; "
                f"marker = pathlib.Path('/tmp/oca-exec-{token}.pid'); "
                "needle = b'sleep\\x0073\\x00'; "
                "count = sum(needle in path.read_bytes() "
                "for path in pathlib.Path('/proc').glob('[0-9]*/cmdline')); "
                "print(int(marker.exists()), count)"
            ),
        ],
    )
    assert probe.exit_code == 0
    marker, processes = probe.stdout.split()
    return int(marker), int(processes)


async def _wait_for_orphan_state(
    manager: DockerSandboxManager,
    sandbox_id: str,
    token: str,
    expected: tuple[int, int],
) -> None:
    for _ in range(40):
        marker, processes = await _orphan_state(manager, sandbox_id, token)
        if marker == expected[0] and processes == expected[1]:
            return
        await asyncio.sleep(0.1)
    assert await _orphan_state(manager, sandbox_id, token) == expected


async def _start_detached_worker(
    manager: DockerSandboxManager, sandbox_id: str, token: str
) -> None:
    """Leave a worker helper running after the host Docker CLI has exited."""
    started = await manager._run(
        [
            "container",
            "exec",
            "--detach",
            sandbox_id,
            "python",
            manager.worker_exec,
            "run",
            token,
            "sleep",
            "73",
        ],
        check=False,
    )
    assert started.returncode == 0, started.stderr
    # The marker appears both in the wrapper argv and the child command line.
    await _wait_for_orphan_state(manager, sandbox_id, token, (1, 2))


def _inspect(sandbox_id: str) -> dict[str, object]:
    result = subprocess.run(
        ["docker", "container", "inspect", sandbox_id],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_sandbox_lifecycle_labels_workspace_and_exec(tmp_path: Path) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    session_a = f"docker-test-a-{uuid4().hex}"
    session_b = f"docker-test-b-{uuid4().hex}"
    first = await manager.create(session_a)
    second = await manager.create(session_b)
    try:
        assert first.sandbox_id != second.sandbox_id
        assert first.workspace != second.workspace
        assert stat.S_IMODE(first.workspace.stat().st_mode) & 0o777 == 0o777

        inspected = await asyncio.to_thread(_inspect, first.sandbox_id)
        labels = inspected["Config"]["Labels"]
        host_config = inspected["HostConfig"]
        assert labels[manager.managed_label] == "true"
        assert labels[manager.session_label] == session_a
        assert host_config["NetworkMode"] == "none"
        assert host_config["PidsLimit"] == 128
        assert "ALL" in host_config["CapDrop"]
        assert "no-new-privileges" in host_config["SecurityOpt"]

        written = await manager.exec(first.sandbox_id, ["sh", "-c", "printf A > only-a.txt"])
        assert written.exit_code == 0
        assert (first.workspace / "only-a.txt").read_text() == "A"
        assert not (second.workspace / "only-a.txt").exists()

        streamed = [
            line
            async for line in manager.stream_exec(
                first.sandbox_id,
                ["sh", "-c", 'printf "diagnostic\\n" >&2; printf "$DEMO_TOKEN\\n"'],
                env={"DEMO_TOKEN": "forwarded"},
            )
        ]
        assert streamed == ["forwarded\n"]

        large_line = "x" * (70 * 1024)
        streamed = [
            line
            async for line in manager.stream_exec(
                first.sandbox_id,
                ["python", "-c", f"print({large_line!r})"],
            )
        ]
        assert streamed == [large_line + "\n"]

        sdk_version = await manager.exec(
            first.sandbox_id,
            [
                "/opt/claude-agent-sdk/bin/python",
                "-c",
                "from importlib.metadata import version; print(version('claude-agent-sdk'))",
            ],
        )
        assert sdk_version.exit_code == 0
        assert sdk_version.stdout.strip() == "0.2.135"

        runner_help = await manager.exec(first.sandbox_id, ["oca-agent-sdk-runner", "--help"])
        assert runner_help.exit_code == 0
        assert "--session-id" in runner_help.stdout
        assert "--resume" in runner_help.stdout

        with pytest.raises(DockerSandboxError, match="timed out"):
            _ = [
                line
                async for line in manager.stream_exec(
                    first.sandbox_id,
                    ["sh", "-c", "echo timeout-started; sleep 17"],
                    timeout_seconds=0.2,
                )
            ]
        await _assert_no_sleep_17(manager, first.sandbox_id)

        interrupted = manager.stream_exec(
            first.sandbox_id,
            ["sh", "-c", "echo close-started; sleep 17"],
        )
        assert await anext(interrupted) == "close-started\n"
        await interrupted.aclose()
        await _assert_no_sleep_17(manager, first.sandbox_id)

        paused = await manager.pause(first.sandbox_id)
        assert paused is None
        assert (await manager.inspect(first.sandbox_id)).state == "paused"
        await manager.pause(first.sandbox_id)
        await manager.resume(first.sandbox_id)
        assert (await manager.inspect(first.sandbox_id)).state == "active"

        repeated = await manager.create(session_a)
        assert repeated.sandbox_id == first.sandbox_id
        assert repeated.workspace == first.workspace
        assert repeated.created is False
    finally:
        await manager.delete(first.sandbox_id)
        await manager.delete(first.sandbox_id)
        await manager.delete(second.sandbox_id)

    assert not first.workspace.exists()
    assert not second.workspace.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_worker_can_write_preexisting_host_workspace_with_restrictive_mode(
    tmp_path: Path,
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    session_id = f"docker-cross-uid-{uuid4().hex}"
    sandbox_id = manager._sandbox_id(session_id)
    workspace = tmp_path / "workspaces" / sandbox_id
    workspace.mkdir(parents=True, mode=0o700)
    workspace.chmod(0o700)
    sentinel = workspace / "keep.txt"
    sentinel.write_text("preexisting")
    owner = (workspace.stat().st_uid, workspace.stat().st_gid)

    sandbox = await manager.create(session_id)
    try:
        written = await manager._run(
            [
                "container",
                "exec",
                "--user",
                "12345:12345",
                "--workdir",
                "/workspace",
                sandbox.sandbox_id,
                "sh",
                "-c",
                "printf portable > cross-uid.txt",
            ],
            check=False,
        )
        assert written.returncode == 0, written.stderr
        assert (workspace / "cross-uid.txt").read_text() == "portable"
        assert sentinel.read_text() == "preexisting"
        assert (workspace.stat().st_uid, workspace.stat().st_gid) == owner
        assert stat.S_IMODE(workspace.stat().st_mode) & 0o777 == 0o777
    finally:
        await manager.delete(sandbox.sandbox_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_failure_preserves_real_reused_docker_sandbox_and_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runtime:
        def __init__(self) -> None:
            self.closed: list[str] = []

        async def create_session(self, context: RuntimeContext) -> str:
            del context
            return "new-runtime"

        async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
            del context
            self.closed.append(runtime_session_id)

    manager = DockerSandboxManager(tmp_path / "workspaces")
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = Runtime()
    session_id = f"repo-failure-{uuid4().hex}"
    existing = await manager.create(session_id)
    sentinel = existing.workspace / "keep.txt"
    sentinel.write_text("preexisting")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        manager,
        runtime,  # type: ignore[arg-type]
    )

    async def fail_create(record: SessionRecord) -> SessionRecord:
        del record
        raise RuntimeError("injected repository failure")

    monkeypatch.setattr(repository, "create", fail_create)
    try:
        with pytest.raises(RuntimeError, match="injected repository failure"):
            await service.create_empty(session_id)
        inspected = await manager.inspect(existing.sandbox_id)
        assert inspected is not None and inspected.state == "active"
        assert sentinel.read_text() == "preexisting"
        assert runtime.closed == ["new-runtime"]
    finally:
        await manager.delete(existing.sandbox_id)
        await repository.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_failure_removes_new_container_but_preserves_real_workspace_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runtime:
        def __init__(self) -> None:
            self.closed: list[str] = []

        async def create_session(self, context: RuntimeContext) -> str:
            del context
            return "new-runtime"

        async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
            del context
            self.closed.append(runtime_session_id)

    manager = DockerSandboxManager(tmp_path / "workspaces")
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    runtime = Runtime()
    session_id = f"repo-workspace-failure-{uuid4().hex}"
    sandbox_id = manager._sandbox_id(session_id)
    workspace = tmp_path / "workspaces" / sandbox_id
    workspace.mkdir(parents=True)
    sentinel = workspace / "keep.txt"
    sentinel.write_text("preexisting")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        manager,
        runtime,  # type: ignore[arg-type]
    )

    async def fail_create(record: SessionRecord) -> SessionRecord:
        del record
        raise RuntimeError("injected repository failure")

    monkeypatch.setattr(repository, "create", fail_create)
    try:
        with pytest.raises(RuntimeError, match="injected repository failure"):
            await service.create_empty(session_id)
        assert await manager.inspect(sandbox_id) is None
        assert sentinel.read_text() == "preexisting"
        assert runtime.closed == ["new-runtime"]
    finally:
        await manager.delete(sandbox_id, delete_workspace=False)
        await repository.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_cleans_only_its_orphaned_worker_processes(tmp_path: Path) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    first = await manager.create(f"orphan-a-{uuid4().hex}")
    second = await manager.create(f"orphan-b-{uuid4().hex}")
    first_token = uuid4().hex
    second_token = uuid4().hex
    try:
        await _start_detached_worker(manager, first.sandbox_id, first_token)
        await _start_detached_worker(manager, second.sandbox_id, second_token)

        # Resume must run cleanup even for an already-running container.  The
        # first detached exec has no host CLI left to cancel it, matching the
        # service-crash orphan shape.
        await manager.resume(first.sandbox_id)
        await _wait_for_orphan_state(manager, first.sandbox_id, first_token, (0, 0))
        assert await _orphan_state(manager, second.sandbox_id, second_token) == (1, 2)

        cancelled = await manager.exec(
            second.sandbox_id,
            ["python", manager.worker_exec, "cancel", second_token],
        )
        assert cancelled.exit_code == 0
        await _wait_for_orphan_state(manager, second.sandbox_id, second_token, (0, 0))
    finally:
        await manager.delete(first.sandbox_id)
        await manager.delete(second.sandbox_id)
