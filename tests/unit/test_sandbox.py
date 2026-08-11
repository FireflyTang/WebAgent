import asyncio
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.sandbox import LocalSandboxManager
from app.sandbox.docker import DockerSandboxError, DockerSandboxManager


def test_default_worker_image_is_public_webagent_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCKER_IMAGE", raising=False)
    assert Settings(_env_file=None).docker_image == "webagent-worker:latest"
    assert DockerSandboxManager(tmp_path / "workspaces").image == "webagent-worker:latest"


@pytest.mark.asyncio
async def test_local_sandbox_isolated_and_idempotent(tmp_path: Path) -> None:
    manager = LocalSandboxManager(tmp_path / "workspaces")
    first = await manager.create("session-a")
    again = await manager.create("session-a")
    other = await manager.create("session-b")

    assert (first.sandbox_id, first.workspace, first.state) == (
        again.sandbox_id,
        again.workspace,
        again.state,
    )
    assert first.created is True and first.workspace_created is True
    assert again.created is False and again.workspace_created is False
    assert first.workspace != other.workspace
    (first.workspace / "only-a.txt").write_text("A")
    assert not (other.workspace / "only-a.txt").exists()

    await manager.pause(first.sandbox_id)
    assert (await manager.inspect(first.sandbox_id)).state == "paused"
    await manager.resume(first.sandbox_id)
    assert (await manager.inspect(first.sandbox_id)).state == "active"

    await manager.delete(first.sandbox_id)
    assert (await manager.inspect(first.sandbox_id)).state == "deleted"
    assert not first.workspace.exists()


@pytest.mark.asyncio
async def test_local_create_removes_workspace_when_metadata_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalSandboxManager(tmp_path / "workspaces")
    sandbox_id = manager._id("metadata-failure")

    def fail_write(_data: dict[str, object]) -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(manager, "_write", fail_write)
    with pytest.raises(OSError, match="injected metadata failure"):
        await manager.create("metadata-failure")
    assert not (tmp_path / "workspaces" / sandbox_id).exists()


@pytest.mark.asyncio
async def test_local_create_preserves_preexisting_workspace_when_metadata_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalSandboxManager(tmp_path / "workspaces")
    workspace = tmp_path / "workspaces" / manager._id("metadata-failure-existing")
    workspace.mkdir(parents=True)
    sentinel = workspace / "keep.txt"
    sentinel.write_text("preexisting")

    def fail_write(_data: dict[str, object]) -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(manager, "_write", fail_write)
    with pytest.raises(OSError, match="injected metadata failure"):
        await manager.create("metadata-failure-existing")
    assert sentinel.read_text() == "preexisting"


@pytest.mark.asyncio
async def test_docker_create_and_resume_clean_worker_only_in_target_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    calls: list[list[str]] = []

    async def no_existing(_sandbox_id: str):
        return None

    async def run(
        args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_status", no_existing)
    monkeypatch.setattr(manager, "_run", run)
    sandbox = await manager.create("session-a")
    cleanup = [
        "container",
        "exec",
        sandbox.sandbox_id,
        "python",
        manager.worker_exec,
        "cleanup",
    ]
    assert cleanup in calls

    async def active(_sandbox_id: str):
        return "running", False

    monkeypatch.setattr(manager, "_status", active)
    sandbox.workspace.chmod(0o700)
    reused = await manager.create("session-a")
    assert reused.created is False
    assert stat.S_IMODE(sandbox.workspace.stat().st_mode) & 0o777 == 0o777
    await manager.resume(sandbox.sandbox_id)
    assert calls.count(cleanup) == 3


@pytest.mark.asyncio
async def test_docker_create_makes_workspace_root_cross_uid_writable_without_changing_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    sandbox_id = manager._sandbox_id("cross-uid")
    workspace = tmp_path / "workspaces" / sandbox_id
    workspace.mkdir(parents=True, mode=0o700)
    workspace.chmod(0o700)
    owner = (workspace.stat().st_uid, workspace.stat().st_gid)

    async def no_existing(_sandbox_id: str):
        return None

    async def run(
        args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_status", no_existing)
    monkeypatch.setattr(manager, "_run", run)
    sandbox = await manager.create("cross-uid")

    assert sandbox.workspace_created is False
    assert stat.S_IMODE(workspace.stat().st_mode) & 0o777 == 0o777
    assert (workspace.stat().st_uid, workspace.stat().st_gid) == owner


@pytest.mark.asyncio
async def test_docker_create_rolls_back_container_and_workspace_after_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    calls: list[list[str]] = []

    async def no_existing(_sandbox_id: str):
        return None

    async def run(
        args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        calls.append(args)
        if args[:2] == ["container", "start"]:
            raise DockerSandboxError("injected start failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_status", no_existing)
    monkeypatch.setattr(manager, "_run", run)
    sandbox_id = manager._sandbox_id("rollback")
    with pytest.raises(DockerSandboxError, match="injected start failure"):
        await manager.create("rollback")

    assert ["container", "rm", "--force", sandbox_id] in calls
    assert not (tmp_path / "workspaces" / sandbox_id).exists()


@pytest.mark.asyncio
async def test_docker_create_rollback_preserves_preexisting_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    sandbox_id = manager._sandbox_id("rollback-existing")
    workspace = tmp_path / "workspaces" / sandbox_id
    workspace.mkdir(parents=True)
    workspace.chmod(0o750)
    sentinel = workspace / "keep.txt"
    sentinel.write_text("preexisting")

    async def no_existing(_sandbox_id: str):
        return None

    async def run(
        args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        del check, timeout_seconds
        if args[:2] == ["container", "start"]:
            raise DockerSandboxError("injected start failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(manager, "_status", no_existing)
    monkeypatch.setattr(manager, "_run", run)
    with pytest.raises(DockerSandboxError, match="injected start failure"):
        await manager.create("rollback-existing")
    assert sentinel.read_text() == "preexisting"
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o750


@pytest.mark.asyncio
async def test_docker_stream_exec_accepts_line_larger_than_streamreader_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = DockerSandboxManager(tmp_path / "workspaces")
    large_line = b"x" * (70 * 1024) + b"\n"

    class Process:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode: int | None = None
            self.stdout.feed_data(large_line)
            self.stdout.feed_eof()
            self.stderr.feed_data(b"diagnostic" * (128 * 1024))
            self.stderr.feed_eof()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    process = Process()

    async def create_process(*args: object, **kwargs: object) -> Process:
        del args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    output = [line async for line in manager.stream_exec("sandbox", ["command"])]
    assert output == [large_line.decode()]
