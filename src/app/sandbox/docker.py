"""Docker-backed sandboxes for the container integration milestone.

The manager deliberately talks to the Docker CLI instead of importing the
Docker SDK.  That keeps the application dependency set small and means the
same implementation works with a local Docker socket or a normal Docker CLI
context selected by the host operator.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import stat
import subprocess
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .base import SandboxInfo


class DockerSandboxError(RuntimeError):
    """A Docker command required for sandbox lifecycle management failed."""


@dataclass(frozen=True, slots=True)
class DockerExecResult:
    """Result of a command run inside a sandbox workspace."""

    exit_code: int
    stdout: str
    stderr: str


class DockerSandboxManager:
    """One labelled, non-root Docker container and bind-mounted workspace per session."""

    managed_label = "com.webagent.managed"
    session_label = "com.webagent.session-id"
    worker_exec = "/usr/local/bin/oca-worker-exec"
    max_stream_line_bytes = 8 * 1024 * 1024
    max_stderr_bytes = 64 * 1024

    def __init__(
        self,
        workspace_root: Path,
        *,
        image: str = "webagent-worker:latest",
        docker_binary: str = "docker",
        network_mode: str = "none",
        cpus: str = "1.0",
        memory: str = "512m",
        pids_limit: int = 128,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.image = image
        self.docker_binary = docker_binary
        self.network_mode = network_mode
        self.cpus = cpus
        self.memory = memory
        self.pids_limit = pids_limit

    @staticmethod
    def _sandbox_id(session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return f"oca-sandbox-{digest}"

    def _workspace(self, sandbox_id: str) -> Path:
        return self.workspace_root / sandbox_id

    @staticmethod
    def _make_workspace_root_writable(workspace: Path) -> int:
        """Make a bind-mounted workspace root writable across host/container UIDs.

        Docker bind mounts hide the image's ownership and mode for ``/workspace``.
        The host service and the image's non-root worker commonly have different
        numeric UIDs (for example 1001 and 1000 on GitHub-hosted runners), so the
        mount root must grant both users directory access.  Preserve ownership,
        special bits, and all pre-existing contents; only add rwx permission bits
        to the workspace root itself.

        Return the previous permission mode so a failed create can compensate a
        pre-existing directory without changing its durable state.
        """
        previous_mode = stat.S_IMODE(workspace.stat().st_mode)
        workspace.chmod(previous_mode | 0o777)
        return previous_mode

    def _run_sync(
        self, args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.docker_binary, *args],
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerSandboxError(f"Docker command could not run: {exc}") from exc
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Docker error"
            raise DockerSandboxError(f"docker {' '.join(args[:2])} failed: {detail}")
        return completed

    async def _run(
        self, args: list[str], *, check: bool = True, timeout_seconds: float = 60
    ) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(
            self._run_sync, args, check=check, timeout_seconds=timeout_seconds
        )

    async def _status(self, sandbox_id: str) -> tuple[str, bool] | None:
        result = await self._run(
            ["container", "inspect", "--format", "{{.State.Status}} {{.State.Paused}}", sandbox_id],
            check=False,
        )
        if result.returncode != 0:
            return None
        status, paused = result.stdout.strip().split(maxsplit=1)
        return status, paused.lower() == "true"

    async def _cleanup_stale_worker_exec(self, sandbox_id: str) -> None:
        """Clear orphaned worker command groups in exactly this sandbox.

        The in-container helper validates recorded PID start times before
        signalling a process group, so this cannot target a different sandbox
        or a PID that has since been reused.
        """
        result = await self._run(
            ["container", "exec", sandbox_id, "python", self.worker_exec, "cleanup"],
            check=False,
            timeout_seconds=10,
        )
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or "unknown worker cleanup error"
            )
            raise DockerSandboxError(
                f"Could not clean stale worker process in {sandbox_id}: {detail}"
            )

    async def create(self, session_id: str) -> SandboxInfo:
        sandbox_id = self._sandbox_id(session_id)
        existing = await self._status(sandbox_id)
        if existing is not None:
            workspace = self._workspace(sandbox_id)
            previous_mode = await asyncio.to_thread(self._make_workspace_root_writable, workspace)
            try:
                await self.resume(sandbox_id)
            except BaseException as original:
                try:
                    await asyncio.to_thread(workspace.chmod, previous_mode)
                except BaseException as cleanup_error:
                    original.add_note(f"workspace permission rollback failed: {cleanup_error!r}")
                raise
            return SandboxInfo(
                sandbox_id,
                workspace,
                "active",
                created=False,
                workspace_created=False,
            )

        workspace = self._workspace(sandbox_id)
        workspace_created = not workspace.exists()
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        container_created = False
        previous_mode: int | None = None
        try:
            previous_mode = await asyncio.to_thread(self._make_workspace_root_writable, workspace)
            await self._run(
                [
                    "container",
                    "create",
                    "--name",
                    sandbox_id,
                    "--label",
                    f"{self.managed_label}=true",
                    "--label",
                    f"{self.session_label}={session_id}",
                    "--network",
                    self.network_mode,
                    "--cpus",
                    self.cpus,
                    "--memory",
                    self.memory,
                    "--pids-limit",
                    str(self.pids_limit),
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--mount",
                    f"type=bind,src={workspace},dst=/workspace",
                    self.image,
                ]
            )
            container_created = True
            await self._run(["container", "start", sandbox_id])
            await self._cleanup_stale_worker_exec(sandbox_id)
        except BaseException as original:
            if container_created:
                try:
                    await self._run(["container", "rm", "--force", sandbox_id], check=False)
                except BaseException as cleanup_error:
                    original.add_note(f"container create rollback failed: {cleanup_error!r}")
            if workspace_created:
                try:
                    await asyncio.to_thread(shutil.rmtree, workspace, True)
                except BaseException as cleanup_error:
                    original.add_note(f"workspace create rollback failed: {cleanup_error!r}")
            elif previous_mode is not None:
                try:
                    await asyncio.to_thread(workspace.chmod, previous_mode)
                except BaseException as cleanup_error:
                    original.add_note(f"workspace permission rollback failed: {cleanup_error!r}")
            raise
        return SandboxInfo(
            sandbox_id,
            workspace,
            "active",
            created=True,
            workspace_created=workspace_created,
        )

    async def inspect(self, sandbox_id: str) -> SandboxInfo | None:
        status = await self._status(sandbox_id)
        if status is None:
            return None
        _, paused = status
        return SandboxInfo(
            sandbox_id,
            self._workspace(sandbox_id),
            "paused" if paused else "active",
            created=False,
            workspace_created=False,
        )

    async def pause(self, sandbox_id: str) -> None:
        status = await self._status(sandbox_id)
        if status is None or status[1]:
            return
        if status[0] == "running":
            await self._run(["container", "pause", sandbox_id])

    async def resume(self, sandbox_id: str) -> None:
        status = await self._status(sandbox_id)
        if status is None:
            raise DockerSandboxError(f"Sandbox {sandbox_id} cannot be resumed")
        container_status, paused = status
        if paused:
            await self._run(["container", "unpause", sandbox_id])
        elif container_status in {"created", "exited"}:
            await self._run(["container", "start", sandbox_id])
        await self._cleanup_stale_worker_exec(sandbox_id)

    async def delete(self, sandbox_id: str, delete_workspace: bool = True) -> None:
        result = await self._run(["container", "rm", "--force", sandbox_id], check=False)
        if result.returncode != 0 and "No such container" not in result.stderr:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DockerSandboxError(f"Could not remove sandbox {sandbox_id}: {detail}")
        if delete_workspace:
            workspace = self._workspace(sandbox_id)
            if workspace.parent == self.workspace_root and workspace.name == sandbox_id:
                await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def exec(
        self, sandbox_id: str, argv: list[str], *, timeout_seconds: float = 60
    ) -> DockerExecResult:
        """Run an argv command in ``/workspace`` without a shell or interpolation."""
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("argv must contain one or more non-empty strings")
        result = await self._run(
            ["container", "exec", "--workdir", "/workspace", sandbox_id, *argv],
            check=False,
            timeout_seconds=timeout_seconds,
        )
        return DockerExecResult(result.returncode, result.stdout, result.stderr)

    async def stream_exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 60,
    ) -> AsyncIterator[str]:
        """Yield stdout lines for an argv command in the container.

        Environment values are forwarded only as Docker ``--env`` arguments and
        are intentionally omitted from every exception message. Stderr is drained
        separately so CLI diagnostics can never be mistaken for protocol JSON.
        """
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("argv must contain one or more non-empty strings")
        if not cwd.startswith("/"):
            raise ValueError("cwd must be an absolute container path")
        env_args: list[str] = []
        for name, value in (env or {}).items():
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("env must contain non-empty names and NUL-free values")
            env_args.extend(["--env", f"{name}={value}"])
        execution_token = uuid.uuid4().hex
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_binary,
                "container",
                "exec",
                "--workdir",
                cwd,
                *env_args,
                sandbox_id,
                "python",
                self.worker_exec,
                "run",
                execution_token,
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DockerSandboxError(f"Docker command could not run: {exc}") from exc
        assert process.stderr is not None

        async def drain_stderr() -> bytes:
            retained = bytearray()
            while chunk := await process.stderr.read(64 * 1024):
                remaining = self.max_stderr_bytes - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
            return bytes(retained)

        stderr_task = asyncio.create_task(drain_stderr())
        completed_normally = False
        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    assert process.stdout is not None
                    buffered = bytearray()
                    while chunk := await process.stdout.read(64 * 1024):
                        buffered.extend(chunk)
                        while (newline := buffered.find(b"\n")) >= 0:
                            line = bytes(buffered[: newline + 1])
                            del buffered[: newline + 1]
                            if len(line) > self.max_stream_line_bytes:
                                raise DockerSandboxError(
                                    "Docker exec emitted a protocol line larger than 8 MiB"
                                )
                            yield line.decode(errors="replace")
                        if len(buffered) > self.max_stream_line_bytes:
                            raise DockerSandboxError(
                                "Docker exec emitted a protocol line larger than 8 MiB"
                            )
                    if buffered:
                        yield bytes(buffered).decode(errors="replace")
                    returncode = await process.wait()
                    completed_normally = True
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise DockerSandboxError("Docker exec timed out") from exc
            if returncode != 0:
                raise DockerSandboxError(f"Docker exec failed with exit code {returncode}")
        finally:
            # A disconnected consumer closes this async generator early.  Do
            # not leave the Docker CLI (and its attached exec) behind then.
            if process.returncode is None:
                process.terminate()
                await process.wait()
            if not completed_normally:
                await self._run(
                    [
                        "container",
                        "exec",
                        sandbox_id,
                        "python",
                        self.worker_exec,
                        "cancel",
                        execution_token,
                    ],
                    check=False,
                    timeout_seconds=10,
                )
            await stderr_task
