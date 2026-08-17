from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import shutil
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.sessions.models import SessionRecord, SessionState


def _percent(used: int, total: int) -> float | None:
    return round(used * 100 / total, 2) if total > 0 else None


def _read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


class HostProbe:
    """Read Linux host/process counters directly from procfs."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self._previous_cpu: tuple[int, int] | None = None

    def _cpu_percent(self) -> float | None:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        counters = [int(value) for value in fields]
        total = sum(counters)
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None or total <= previous[0]:
            return None
        measured = (1 - (idle - previous[1]) / (total - previous[0])) * 100
        return round(min(100.0, max(0.0, measured)), 2)

    def sample(self) -> dict[str, dict[str, object]]:
        memory = _read_key_values(Path("/proc/meminfo"))
        total_memory = int(memory["MemTotal"].split()[0]) * 1024
        available_memory = int(memory.get("MemAvailable", memory["MemFree"]).split()[0]) * 1024
        used_memory = max(0, total_memory - available_memory)
        load_1m, load_5m, load_15m = os.getloadavg()
        disk_path = self.workspace_root
        while not disk_path.exists() and disk_path != disk_path.parent:
            disk_path = disk_path.parent
        disk = shutil.disk_usage(disk_path)
        process = _read_key_values(Path("/proc/self/status"))
        rss_bytes = int(process["VmRSS"].split()[0]) * 1024
        try:
            fd_count: int | None = sum(1 for _ in Path("/proc/self/fd").iterdir())
        except OSError:
            fd_count = None
        return {
            "host": {
                "cpu_percent": self._cpu_percent(),
                "memory_used_bytes": used_memory,
                "memory_total_bytes": total_memory,
                "memory_percent": _percent(used_memory, total_memory),
                "load_1m": round(load_1m, 2),
                "load_5m": round(load_5m, 2),
                "load_15m": round(load_15m, 2),
                "disk_used_bytes": disk.used,
                "disk_total_bytes": disk.total,
                "disk_percent": _percent(disk.used, disk.total),
            },
            "process": {"rss_bytes": rss_bytes, "fd_count": fd_count},
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str], float], Awaitable[CommandResult]]


async def _run_command(argv: list[str], timeout_seconds: float) -> CommandResult:
    def run() -> CommandResult:
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(1, "", str(exc))
        return CommandResult(result.returncode, result.stdout, result.stderr)

    return await asyncio.to_thread(run)


_SIZE_FACTORS = {
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000**2,
    "mib": 1024**2,
    "gb": 1000**3,
    "gib": 1024**3,
    "tb": 1000**4,
    "tib": 1024**4,
}


def _size_bytes(value: str) -> int:
    normalized = value.strip().lower()
    for suffix in sorted(_SIZE_FACTORS, key=len, reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)].strip()
            return int(float(number) * _SIZE_FACTORS[suffix])
    return int(float(normalized))


class DockerProbe:
    def __init__(
        self,
        docker_binary: str,
        image: str,
        *,
        enabled: bool,
        command_runner: CommandRunner = _run_command,
        timeout_seconds: float = 1.5,
    ) -> None:
        self.docker_binary = docker_binary
        self.image = image
        self.enabled = enabled
        self.command_runner = command_runner
        self.timeout_seconds = timeout_seconds

    async def sample(self) -> dict[str, object]:
        empty = {
            "available": False,
            "disabled": not self.enabled,
            "managed_containers": 0,
            "cpu_percent": None,
            "memory_used_bytes": None,
            "pids": None,
            "error": None,
            "image_available": False,
            "containers": [],
        }
        if not self.enabled:
            return empty
        ps_args = [
            self.docker_binary,
            "ps",
            "-a",
            "--filter",
            "label=com.webagent.managed=true",
            "--format",
            '{{.Names}}|{{.ID}}|{{.State}}|{{.Status}}|{{.Label "com.webagent.session-id"}}',
        ]
        image_args = [
            self.docker_binary,
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            self.image,
        ]
        ps, image = await asyncio.gather(
            self.command_runner(ps_args, self.timeout_seconds),
            self.command_runner(image_args, self.timeout_seconds),
        )
        if ps.returncode != 0:
            message = (ps.stderr or ps.stdout or "Docker unavailable").strip()
            return {**empty, "error": message[:240]}
        containers: list[dict[str, str]] = []
        for line in ps.stdout.splitlines():
            container_name, separator, remainder = line.partition("|")
            container_id, second_separator, remainder = remainder.partition("|")
            state, third_separator, remainder = remainder.partition("|")
            status, fourth_separator, session_id = remainder.partition("|")
            if not separator or not second_separator or not third_separator or not fourth_separator:
                return {**empty, "error": "Could not parse Docker container list"}
            if "(Paused)" in status:
                state = "paused"
            containers.append(
                {
                    "container_name": container_name,
                    "container_id": container_id,
                    "state": state,
                    "session_id": session_id,
                }
            )
        container_ids = [
            container["container_id"]
            for container in containers
            if container["state"] in {"running", "paused"}
        ]
        result = {
            **empty,
            "available": True,
            "disabled": False,
            "managed_containers": len(containers),
            "cpu_percent": 0.0,
            "memory_used_bytes": 0,
            "pids": 0,
            "image_available": image.returncode == 0,
            "containers": containers,
        }
        if image.returncode != 0:
            result["image_error"] = (image.stderr or image.stdout or "image unavailable").strip()[
                :240
            ]
        if not container_ids:
            return result
        stats = await self.command_runner(
            [
                self.docker_binary,
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *container_ids,
            ],
            self.timeout_seconds,
        )
        if stats.returncode != 0:
            result["stats_error"] = (
                stats.stderr or stats.stdout or "Docker stats unavailable"
            ).strip()[:240]
            result.update(cpu_percent=None, memory_used_bytes=None, pids=None)
            return result
        cpu_percent = 0.0
        memory_used = 0
        pids = 0
        try:
            for line in stats.stdout.splitlines():
                row = json.loads(line)
                cpu_percent += float(str(row.get("CPUPerc", "0")).rstrip("%") or 0)
                memory_used += _size_bytes(str(row.get("MemUsage", "0")).split("/")[0])
                pids += int(row.get("PIDs", 0))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            result["stats_error"] = f"Could not parse Docker stats: {exc}"
            result.update(cpu_percent=None, memory_used_bytes=None, pids=None)
            return result
        result.update(cpu_percent=round(cpu_percent, 2), memory_used_bytes=memory_used, pids=pids)
        return result


class Repository(Protocol):
    async def list_sessions(self, owner_user_id: str | None = None) -> list[SessionRecord]: ...


class DiagnosticSource(Protocol):
    def diagnostics(self) -> object: ...


class SystemMonitor:
    def __init__(
        self,
        repository: Repository,
        journal: DiagnosticSource,
        active_turns: DiagnosticSource,
        reaper: DiagnosticSource,
        reaper_task: asyncio.Task[object],
        workspace_root: Path,
        docker_probe: DockerProbe,
        *,
        host_probe: HostProbe | None = None,
        sample_interval_seconds: float = 5,
        retention_seconds: int = 3600,
        journal_stall_seconds: float = 30,
        reaper_startup_grace_seconds: float = 30,
        reaper_tick_stall_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            sample_interval_seconds <= 0
            or retention_seconds <= 0
            or journal_stall_seconds <= 0
            or reaper_startup_grace_seconds <= 0
            or reaper_tick_stall_seconds <= 0
        ):
            raise ValueError("monitor sampling and retention must be positive")
        self.repository = repository
        self.journal = journal
        self.active_turns = active_turns
        self.reaper = reaper
        self.reaper_task = reaper_task
        self.workspace_root = workspace_root.resolve()
        self.docker_probe = docker_probe
        self.host_probe = host_probe or HostProbe(workspace_root)
        self.sample_interval_seconds = sample_interval_seconds
        self.retention_seconds = retention_seconds
        self.journal_stall_seconds = journal_stall_seconds
        self.reaper_startup_grace_seconds = reaper_startup_grace_seconds
        self.reaper_tick_stall_seconds = reaper_tick_stall_seconds
        self.clock = clock
        self._history: deque[tuple[datetime, dict[str, object]]] = deque(
            maxlen=math.ceil(retention_seconds / sample_interval_seconds) + 1
        )
        self._report: dict[str, object] = self._initial_report()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def _initial_report(self) -> dict[str, object]:
        now = self.clock().isoformat()
        components = [
            {
                "id": component_id,
                "name": name,
                "status": "disabled"
                if component_id in {"docker", "worker_image"} and not self.docker_probe.enabled
                else "degraded",
                "message": "未启用"
                if not self.docker_probe.enabled and component_id in {"docker", "worker_image"}
                else "正在采样",
                "checked_at": now,
                "details": {},
            }
            for component_id, name in (
                ("sqlite", "SQLite"),
                ("docker", "Docker"),
                ("worker_image", "Worker image"),
                ("journal", "UI event journal"),
                ("reaper", "Lifecycle reaper"),
                ("workspace", "Workspace disk"),
            )
        ]
        return {
            "status": "degraded",
            "generated_at": now,
            "sample_interval_seconds": self.sample_interval_seconds,
            "retention_seconds": self.retention_seconds,
            "snapshot": None,
            "history": [],
            "components": components,
            "issues": [],
            "tasks": [],
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="system-monitor")

    async def close(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def report(self) -> dict[str, object]:
        return copy.deepcopy(self._report)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = self._initial_report()
                failed["status"] = "error"
                failed["generated_at"] = self.clock().isoformat()
                failed["issues"] = [
                    {
                        "code": "monitor_sample_failed",
                        "severity": "error",
                        "title": "监控采样失败",
                        "message": str(exc),
                        "session_id": None,
                        "turn_id": None,
                    }
                ]
                self._report = failed
            remaining = self.sample_interval_seconds - (asyncio.get_running_loop().time() - started)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(0.01, remaining))
            except TimeoutError:
                pass

    async def sample_once(self) -> None:
        at = self.clock()
        at_text = at.isoformat()
        host_error: str | None = None
        try:
            local = await asyncio.to_thread(self.host_probe.sample)
        except Exception as exc:
            host_error = str(exc)
            local = {
                "host": {
                    "cpu_percent": None,
                    "memory_used_bytes": 0,
                    "memory_total_bytes": 0,
                    "memory_percent": None,
                    "load_1m": 0.0,
                    "load_5m": 0.0,
                    "load_15m": 0.0,
                    "disk_used_bytes": 0,
                    "disk_total_bytes": 0,
                    "disk_percent": None,
                },
                "process": {"rss_bytes": 0, "fd_count": None},
            }
        docker, records_result = await asyncio.gather(
            self.docker_probe.sample(), self._list_sessions_safely()
        )
        records, sqlite_error = records_result
        tasks = self.active_turns.diagnostics()
        assert isinstance(tasks, list)
        journal = self.journal.diagnostics()
        assert isinstance(journal, dict)
        reaper = self.reaper.diagnostics()
        assert isinstance(reaper, dict)
        snapshot = {"at": at_text, **local, "docker": self._docker_view(docker)}
        point = self._history_point(snapshot)
        self._history.append((at, point))
        cutoff = at - timedelta(seconds=self.retention_seconds)
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        components = self._components(
            at_text, snapshot, docker, journal, reaper, records, sqlite_error, host_error
        )
        issues = self._issues(
            at_text,
            snapshot,
            docker,
            records,
            tasks,
            journal,
            reaper,
            sqlite_error,
            host_error,
        )
        statuses = {component["status"] for component in components}
        overall = (
            "error"
            if "error" in statuses
            else "degraded"
            if issues or "degraded" in statuses
            else "ok"
        )
        self._report = {
            "status": overall,
            "generated_at": at_text,
            "sample_interval_seconds": self.sample_interval_seconds,
            "retention_seconds": self.retention_seconds,
            "snapshot": snapshot,
            "history": [item for _, item in self._history],
            "components": components,
            "issues": issues,
            "tasks": tasks,
        }

    async def _list_sessions_safely(self) -> tuple[list[SessionRecord], str | None]:
        try:
            return await self.repository.list_sessions(), None
        except Exception as exc:
            return [], str(exc)

    @staticmethod
    def _docker_view(docker: Mapping[str, object]) -> dict[str, object]:
        return {
            "available": docker["available"],
            "disabled": docker.get("disabled", False),
            "managed_containers": docker["managed_containers"],
            "cpu_percent": docker["cpu_percent"],
            "memory_used_bytes": docker["memory_used_bytes"],
            "pids": docker["pids"],
            "error": docker.get("error") or docker.get("stats_error"),
            "containers": copy.deepcopy(docker.get("containers", [])),
        }

    @staticmethod
    def _history_point(snapshot: Mapping[str, object]) -> dict[str, object]:
        host = snapshot["host"]
        process = snapshot["process"]
        docker = snapshot["docker"]
        assert (
            isinstance(host, Mapping)
            and isinstance(process, Mapping)
            and isinstance(docker, Mapping)
        )
        return {
            "at": snapshot["at"],
            "host_cpu_percent": host["cpu_percent"],
            "host_memory_percent": host["memory_percent"],
            "process_rss_bytes": process["rss_bytes"],
            "docker_cpu_percent": docker["cpu_percent"],
            "docker_memory_used_bytes": docker["memory_used_bytes"],
            "managed_containers": docker["managed_containers"],
        }

    def _components(
        self,
        at: str,
        snapshot: Mapping[str, object],
        docker: Mapping[str, object],
        journal: Mapping[str, object],
        reaper: Mapping[str, object],
        records: list[SessionRecord],
        sqlite_error: str | None,
        host_error: str | None,
    ) -> list[dict[str, object]]:
        host = snapshot["host"]
        assert isinstance(host, Mapping)

        def component(
            component_id: str,
            name: str,
            status: str,
            message: str,
            details: Mapping[str, object],
        ) -> dict[str, object]:
            return {
                "id": component_id,
                "name": name,
                "status": status,
                "message": message,
                "checked_at": at,
                "details": dict(details),
            }

        if self.docker_probe.enabled:
            docker_status = (
                "error"
                if not docker["available"] or docker.get("error")
                else "degraded"
                if docker.get("stats_error")
                else "ok"
            )
            image_status = "ok" if docker.get("image_available") else "error"
        else:
            docker_status = image_status = "disabled"
        writer_running = bool(journal["writer_running"])
        pending = int(journal["pending_events"])
        conflicts = int(journal["fatal_conflicts"])
        journal_stalled = self._journal_stalled(at, journal)
        journal_status = (
            "error"
            if not writer_running or conflicts
            else "degraded"
            if pending >= 100 or journal_stalled
            else "ok"
        )
        reaper_running = not self.reaper_task.done() and not bool(reaper["stopping"])
        reaper_stale = self._reaper_stale(at, reaper)
        reaper_status = (
            "error"
            if not reaper_running
            else "degraded"
            if reaper["last_error_at"] or reaper_stale
            else "ok"
        )
        disk_percent = host["disk_percent"]
        disk_status = (
            "error"
            if host_error or (isinstance(disk_percent, (int, float)) and disk_percent >= 95)
            else "degraded"
            if isinstance(disk_percent, (int, float)) and disk_percent >= 85
            else "ok"
        )
        return [
            component(
                "sqlite",
                "SQLite",
                "error" if sqlite_error else "ok",
                sqlite_error or "查询正常",
                {"sessions": len(records)},
            ),
            component(
                "docker",
                "Docker",
                docker_status,
                "未启用"
                if docker_status == "disabled"
                else str(
                    docker.get("error")
                    or docker.get("stats_error")
                    or f"{docker['managed_containers']} 个受管容器"
                ),
                {"managed_containers": docker["managed_containers"]},
            ),
            component(
                "worker_image",
                "Worker image",
                image_status,
                "未启用"
                if image_status == "disabled"
                else str(docker.get("image_error") or "镜像可用"),
                {"image": self.docker_probe.image},
            ),
            component(
                "journal",
                "UI event journal",
                journal_status,
                "运行正常"
                if journal_status == "ok"
                else f"{pending} 个事件待写入，{conflicts} 个冲突",
                {**journal, "stalled": journal_stalled},
            ),
            component(
                "reaper",
                "Lifecycle reaper",
                reaper_status,
                "运行正常"
                if reaper_status == "ok"
                else "Reaper 未运行"
                if not reaper_running
                else "Reaper 最近采样异常或逾期",
                {**reaper, "task_running": reaper_running, "stale": reaper_stale},
            ),
            component(
                "workspace",
                "Workspace disk",
                disk_status,
                host_error
                or (f"磁盘使用率 {disk_percent}%" if disk_status != "ok" else "磁盘可用"),
                {"path": str(self.workspace_root), "used_percent": host["disk_percent"]},
            ),
        ]

    def _journal_stalled(self, at: str, journal: Mapping[str, object]) -> bool:
        oldest = journal.get("oldest_pending_at")
        if not isinstance(oldest, str) or int(journal["pending_events"]) == 0:
            return False
        return datetime.fromisoformat(at) - datetime.fromisoformat(oldest) > timedelta(
            seconds=self.journal_stall_seconds
        )

    def _reaper_stale(self, at: str, reaper: Mapping[str, object]) -> bool:
        now = datetime.fromisoformat(at)
        started = reaper.get("run_started_at")
        tick = reaper.get("last_tick_at")
        completed = reaper.get("last_tick_completed_at")
        if isinstance(tick, str) and (
            not isinstance(completed, str)
            or datetime.fromisoformat(tick) > datetime.fromisoformat(completed)
        ):
            return now - datetime.fromisoformat(tick) > timedelta(
                seconds=self.reaper_tick_stall_seconds
            )
        interval = float(reaper["interval_seconds"])
        threshold = timedelta(seconds=interval + self.reaper_startup_grace_seconds)
        if isinstance(completed, str):
            return now - datetime.fromisoformat(completed) > threshold
        return isinstance(started, str) and now - datetime.fromisoformat(started) > threshold

    def _issues(
        self,
        at: str,
        snapshot: Mapping[str, object],
        docker: Mapping[str, object],
        records: list[SessionRecord],
        tasks: list[dict[str, object]],
        journal: Mapping[str, object],
        reaper: Mapping[str, object],
        sqlite_error: str | None,
        host_error: str | None,
    ) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []

        def add(
            code: str,
            severity: str,
            title: str,
            message: str,
            session_id: str | None = None,
            turn_id: str | None = None,
        ) -> None:
            issues.append(
                {
                    "code": code,
                    "severity": severity,
                    "title": title,
                    "message": message,
                    "session_id": session_id,
                    "turn_id": turn_id,
                }
            )

        if sqlite_error:
            add("sqlite_unavailable", "error", "SQLite 查询失败", sqlite_error)
        if host_error:
            add("host_probe_failed", "error", "主机采样失败", host_error)
        if int(journal["fatal_conflicts"]):
            add(
                "journal_conflict",
                "error",
                "UI event 存在永久冲突",
                f"{journal['fatal_conflicts']} 个冲突已隔离",
            )
        pending = int(journal["pending_events"])
        journal_stalled = self._journal_stalled(at, journal)
        if pending >= 100 or journal_stalled:
            add(
                "journal_stalled" if journal_stalled and pending < 100 else "journal_pending",
                "warning",
                "UI event 写入停滞" if journal_stalled else "UI event 等待写入",
                f"{pending} 个事件待写入",
            )
        if reaper["last_error_at"]:
            add(
                "reaper_error",
                "warning",
                "生命周期任务最近执行失败",
                str(reaper["last_error_at"]),
            )
        if self._reaper_stale(at, reaper):
            add(
                "reaper_stale",
                "warning",
                "生命周期任务采样逾期",
                "最近一次完成时间明显超过配置周期",
            )
        host = snapshot["host"]
        assert isinstance(host, Mapping)
        memory_percent = host["memory_percent"]
        disk_percent = host["disk_percent"]
        if isinstance(memory_percent, (int, float)) and memory_percent >= 90:
            add(
                "host_memory_high",
                "warning",
                "主机内存使用率较高",
                f"当前使用率 {memory_percent}%",
            )
        if isinstance(disk_percent, (int, float)) and disk_percent >= 85:
            add(
                "workspace_disk_critical" if disk_percent >= 95 else "workspace_disk_high",
                "error" if disk_percent >= 95 else "warning",
                "Workspace 磁盘空间不足",
                f"当前使用率 {disk_percent}%",
            )
        records_by_id = {record.session_id: record for record in records}
        for record in records:
            for key, code, title in (
                ("pause_pending", "pause_pending", "暂停操作待重试"),
                ("resume_pending", "resume_pending", "恢复操作待重试"),
                ("cleanup_pending", "cleanup_pending", "清理操作待重试"),
            ):
                if record.metadata.get(key) is True:
                    add(code, "warning", title, "Reaper 将继续重试", record.session_id)
        for task in tasks:
            session_id = str(task["session_id"])
            turn_id = str(task["turn_id"]) if task["turn_id"] is not None else None
            record = records_by_id.get(session_id)
            if record is None:
                add(
                    "active_session_missing",
                    "warning",
                    "运行任务缺少 Session",
                    "当前任务没有持久 Session",
                    session_id,
                    turn_id,
                )
            elif record.state is SessionState.DELETED:
                add(
                    "active_session_deleted",
                    "warning",
                    "已删除 Session 仍有任务",
                    "当前任务所属 Session 已删除",
                    session_id,
                    turn_id,
                )
        containers = docker.get("containers")
        if docker.get("available") and isinstance(containers, list):
            valid_containers = [
                container for container in containers if isinstance(container, Mapping)
            ]
            containers_by_name = {
                str(container["container_name"]): container
                for container in valid_containers
                if container.get("container_name")
            }
            containers_by_session: dict[str, list[Mapping[str, object]]] = {}
            for container in valid_containers:
                session_label = container.get("session_id")
                if session_label:
                    containers_by_session.setdefault(str(session_label), []).append(container)
                else:
                    add(
                        "managed_container_missing_session_label",
                        "warning",
                        "受管容器缺少 Session label",
                        f"容器 {container.get('container_name') or container.get('container_id')} 缺少关联标识",
                    )
            for record in records:
                named = (
                    containers_by_name.get(record.sandbox_id)
                    if record.sandbox_id is not None
                    else None
                )
                labelled = containers_by_session.get(record.session_id, [])
                if record.state is SessionState.DELETED:
                    if named is not None or labelled:
                        add(
                            "deleted_session_container_present",
                            "warning",
                            "已删除 Session 仍有受管容器",
                            f"容器 {record.sandbox_id or labelled[0].get('container_name')} 尚未移除",
                            record.session_id,
                        )
                    continue
                if record.sandbox_id is None:
                    if labelled:
                        names = ", ".join(
                            str(container.get("container_name")) for container in labelled
                        )
                        add(
                            "session_sandbox_missing",
                            "warning",
                            "Session 缺少 sandbox 映射",
                            f"同 label 容器仍存在：{names}",
                            record.session_id,
                        )
                    continue
                exact = [
                    container
                    for container in labelled
                    if container.get("container_name") == record.sandbox_id
                ]
                if len(labelled) > 1:
                    names = ", ".join(
                        str(container.get("container_name")) for container in labelled
                    )
                    add(
                        "duplicate_session_containers",
                        "warning",
                        "Session 对应多个受管容器",
                        f"期望仅有 {record.sandbox_id}，同 label 容器为：{names}",
                        record.session_id,
                    )
                    if not exact:
                        continue
                if exact:
                    container = exact[0]
                    expected_state = "paused" if record.state is SessionState.PAUSED else "running"
                    if container.get("state") != expected_state:
                        add(
                            "session_container_state_mismatch",
                            "warning",
                            "Session 与容器状态不一致",
                            f"期望 {expected_state}，实际 {container.get('state')}",
                            record.session_id,
                        )
                    continue
                if named is None and not labelled:
                    add(
                        "session_container_missing",
                        "warning",
                        "Session 缺少受管容器",
                        "持久 Session 尚未找到对应 Docker 容器",
                        record.session_id,
                    )
                    continue
                if named is not None:
                    add(
                        "session_container_label_mismatch",
                        "warning",
                        "容器 Session label 不一致",
                        f"容器 {record.sandbox_id} 标记为 {named.get('session_id') or '空'}",
                        record.session_id,
                    )
                else:
                    observed = (
                        ", ".join(str(container.get("container_name")) for container in labelled)
                        or "无同 label 容器"
                    )
                    add(
                        "session_container_name_mismatch",
                        "warning",
                        "Session 与容器名称不一致",
                        f"期望 {record.sandbox_id}，实际 {observed}",
                        record.session_id,
                    )
            for session_id in containers_by_session.keys() - records_by_id.keys():
                add(
                    "orphan_managed_container",
                    "warning",
                    "受管容器缺少 Session",
                    "Docker 容器没有对应持久 Session",
                    session_id,
                )
        return issues
