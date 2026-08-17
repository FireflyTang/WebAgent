from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.monitoring.system import CommandResult, DockerProbe, SystemMonitor
from app.sessions.models import SessionRecord, SessionState


class _HostProbe:
    def __init__(self, *, memory_percent: float = 20, disk_percent: float = 30) -> None:
        self.memory_percent = memory_percent
        self.disk_percent = disk_percent

    def sample(self) -> dict[str, dict[str, object]]:
        return {
            "host": {
                "cpu_percent": 12.5,
                "memory_used_bytes": 200,
                "memory_total_bytes": 1000,
                "memory_percent": self.memory_percent,
                "load_1m": 0.1,
                "load_5m": 0.2,
                "load_15m": 0.3,
                "disk_used_bytes": 300,
                "disk_total_bytes": 1000,
                "disk_percent": self.disk_percent,
            },
            "process": {"rss_bytes": 1234, "fd_count": 8},
        }


class _Repository:
    def __init__(self, records: list[SessionRecord] | None = None, *, error: str | None = None):
        self.records = records or []
        self.error = error

    async def list_sessions(self, owner_user_id: str | None = None) -> list[SessionRecord]:
        del owner_user_id
        if self.error:
            raise RuntimeError(self.error)
        return self.records


class _Diagnostics:
    def __init__(self, value: object) -> None:
        self.value = value

    def diagnostics(self) -> object:
        return self.value


class _Docker:
    def __init__(self, value: dict[str, object], *, enabled: bool = True) -> None:
        self.value = value
        self.enabled = enabled
        self.image = "worker:test"

    async def sample(self) -> dict[str, object]:
        return self.value


def _docker_sample(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "available": True,
        "managed_containers": 1,
        "cpu_percent": 1.5,
        "memory_used_bytes": 1024,
        "pids": 2,
        "error": None,
        "image_available": True,
        "disabled": False,
        "containers": [
            {
                "container_name": "c1",
                "container_id": "id1",
                "state": "running",
                "session_id": "s1",
            }
        ],
    }
    result.update(overrides)
    return result


async def _sleep_forever() -> None:
    await asyncio.Event().wait()


async def _container_issue_report(
    tmp_path: Path,
    records: list[SessionRecord],
    containers: list[dict[str, str]],
) -> dict[str, object]:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository(records),
        _Diagnostics(
            {"writer_running": True, "pending_events": 0, "fatal_conflicts": 0, "closed": False}
        ),
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": now.isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(
            _docker_sample(containers=containers, managed_containers=len(containers)),
            enabled=True,
        ),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        return monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task


@pytest.mark.asyncio
async def test_docker_probe_aggregates_managed_stats_and_image_health() -> None:
    async def command_runner(argv: list[str], timeout: float) -> CommandResult:
        assert timeout == 0.25
        if argv[1:3] == ["image", "inspect"]:
            return CommandResult(0, "[]", "")
        if argv[1] == "ps":
            return CommandResult(
                0,
                "sandbox-1|c1|running|Up 2 minutes|s1\n"
                "sandbox-2|c2|running|Up 2 minutes (Paused)|s2\n"
                "sandbox-3|c3|exited|Exited (0) 1 minute ago|s3\n",
                "",
            )
        assert argv[1] == "stats"
        rows = [
            {"CPUPerc": "10.5%", "MemUsage": "512MiB / 1GiB", "PIDs": "4"},
            {"CPUPerc": "2%", "MemUsage": "1GiB / 2GiB", "PIDs": "6"},
        ]
        return CommandResult(0, "\n".join(json.dumps(row) for row in rows), "")

    probe = DockerProbe(
        "docker", "worker:test", enabled=True, command_runner=command_runner, timeout_seconds=0.25
    )
    sampled = await probe.sample()

    assert sampled["available"] is True
    assert sampled["managed_containers"] == 3
    assert sampled["cpu_percent"] == 12.5
    assert sampled["memory_used_bytes"] == 1536 * 1024**2
    assert sampled["pids"] == 10
    assert [container["session_id"] for container in sampled["containers"]] == ["s1", "s2", "s3"]
    assert [container["state"] for container in sampled["containers"]] == [
        "running",
        "paused",
        "exited",
    ]
    assert [container["container_name"] for container in sampled["containers"]] == [
        "sandbox-1",
        "sandbox-2",
        "sandbox-3",
    ]


@pytest.mark.asyncio
async def test_docker_probe_failure_is_data_not_an_exception() -> None:
    async def command_runner(argv: list[str], timeout: float) -> CommandResult:
        del timeout
        if argv[1] == "ps":
            return CommandResult(1, "", "daemon timeout")
        return CommandResult(1, "", "missing image")

    sampled = await DockerProbe(
        "docker", "worker:test", enabled=True, command_runner=command_runner
    ).sample()

    assert sampled["available"] is False
    assert sampled["disabled"] is False
    assert sampled["error"] == "daemon timeout"
    assert sampled["managed_containers"] == 0
    assert sampled["cpu_percent"] is None
    assert sampled["memory_used_bytes"] is None
    assert sampled["pids"] is None


@pytest.mark.asyncio
async def test_docker_stats_failure_keeps_daemon_available() -> None:
    async def command_runner(argv: list[str], timeout: float) -> CommandResult:
        del timeout
        if argv[1:3] == ["image", "inspect"]:
            return CommandResult(0, "image-id", "")
        if argv[1] == "ps":
            return CommandResult(0, "sandbox-1|c1|running|Up 1 minute|s1\n", "")
        return CommandResult(1, "", "stats timeout")

    sampled = await DockerProbe(
        "docker", "worker:test", enabled=True, command_runner=command_runner
    ).sample()

    assert sampled["available"] is True
    assert sampled["image_available"] is True
    assert sampled["managed_containers"] == 1
    assert sampled["stats_error"] == "stats timeout"
    assert sampled["cpu_percent"] is None
    assert sampled["memory_used_bytes"] is None
    assert sampled["pids"] is None


@pytest.mark.asyncio
async def test_stats_failure_degrades_component_and_history_keeps_null(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository([SessionRecord(session_id="s1", sandbox_id="c1")]),
        _Diagnostics(
            {"writer_running": True, "pending_events": 0, "fatal_conflicts": 0, "closed": False}
        ),
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": now.isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(
            _docker_sample(
                stats_error="stats timeout",
                cpu_percent=None,
                memory_used_bytes=None,
                pids=None,
            ),
            enabled=True,
        ),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    docker = report["snapshot"]["docker"]
    assert docker["available"] is True
    assert docker["disabled"] is False
    assert docker["cpu_percent"] is None
    assert docker["memory_used_bytes"] is None
    assert docker["pids"] is None
    assert report["history"][0]["docker_cpu_percent"] is None
    assert report["history"][0]["docker_memory_used_bytes"] is None
    component = next(item for item in report["components"] if item["id"] == "docker")
    assert component["status"] == "degraded"


@pytest.mark.asyncio
async def test_monitor_keeps_background_task_healthy_and_prunes_history(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    clock_value = [now]
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository([SessionRecord(session_id="s1", sandbox_id="c1")]),
        _Diagnostics(
            {
                "writer_running": True,
                "pending_events": 3,
                "fatal_conflicts": 0,
                "closed": False,
                "oldest_pending_at": now.isoformat(),
                "last_write_completed_at": None,
                "last_write_error_at": None,
            }
        ),
        _Diagnostics(
            [
                {
                    "session_id": "s1",
                    "turn_id": "t1",
                    "state": "running",
                    "last_sequence": 4,
                    "subscribers": 0,
                    "background": True,
                }
            ]
        ),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": now.isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(_docker_sample(), enabled=True),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        retention_seconds=10,
        clock=lambda: clock_value[0],
    )
    try:
        for seconds in (0, 6, 12):
            clock_value[0] = now + timedelta(seconds=seconds)
            await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    assert report["status"] == "ok"
    assert [point["at"] for point in report["history"]] == [
        (now + timedelta(seconds=6)).isoformat(),
        (now + timedelta(seconds=12)).isoformat(),
    ]
    assert report["tasks"][0]["background"] is True
    assert report["issues"] == []
    journal = next(item for item in report["components"] if item["id"] == "journal")
    assert journal["status"] == "ok"


@pytest.mark.asyncio
async def test_monitor_thresholds_and_inconsistencies_are_degraded(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository(
            [
                SessionRecord(
                    session_id="s1",
                    sandbox_id="c1",
                    metadata={"pause_pending": True},
                )
            ]
        ),
        _Diagnostics(
            {
                "writer_running": True,
                "pending_events": 100,
                "fatal_conflicts": 0,
                "closed": False,
            }
        ),
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": (now - timedelta(minutes=2)).isoformat(),
                "last_tick_completed_at": (now - timedelta(minutes=2)).isoformat(),
                "last_error_at": None,
                "run_started_at": (now - timedelta(minutes=3)).isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(_docker_sample(containers=[]), enabled=True),  # type: ignore[arg-type]
        host_probe=_HostProbe(memory_percent=91, disk_percent=86),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    assert report["status"] == "degraded"
    assert {issue["code"] for issue in report["issues"]} == {
        "host_memory_high",
        "journal_pending",
        "pause_pending",
        "reaper_stale",
        "session_container_missing",
        "workspace_disk_high",
    }
    statuses = {item["id"]: item["status"] for item in report["components"]}
    assert statuses["journal"] == "degraded"
    assert statuses["reaper"] == "degraded"
    assert statuses["workspace"] == "degraded"


@pytest.mark.asyncio
async def test_small_journal_backlog_only_degrades_after_stall_threshold(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    reaper_task = asyncio.create_task(_sleep_forever())
    journal = _Diagnostics(
        {
            "writer_running": True,
            "pending_events": 2,
            "fatal_conflicts": 0,
            "closed": False,
            "oldest_pending_at": (now - timedelta(seconds=31)).isoformat(),
            "last_write_completed_at": None,
            "last_write_error_at": (now - timedelta(seconds=30)).isoformat(),
        }
    )
    monitor = SystemMonitor(
        _Repository(),
        journal,
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": now.isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(_docker_sample(containers=[], managed_containers=0), enabled=True),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        journal_stall_seconds=30,
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    journal_component = next(
        component for component in report["components"] if component["id"] == "journal"
    )
    assert journal_component["status"] == "degraded"
    assert journal_component["details"]["stalled"] is True
    assert [issue["code"] for issue in report["issues"]] == ["journal_stalled"]


@pytest.mark.asyncio
async def test_reaper_running_first_tick_uses_exact_stall_boundary(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    reaper_task = asyncio.create_task(_sleep_forever())
    reaper = _Diagnostics(
        {
            "last_tick_at": (now - timedelta(seconds=60)).isoformat(),
            "last_tick_completed_at": None,
            "last_error_at": None,
            "run_started_at": (now - timedelta(seconds=70)).isoformat(),
            "stopping": False,
            "interval_seconds": 5,
        }
    )
    monitor = SystemMonitor(
        _Repository(),
        _Diagnostics(
            {"writer_running": True, "pending_events": 0, "fatal_conflicts": 0, "closed": False}
        ),
        _Diagnostics([]),
        reaper,
        reaper_task,
        tmp_path,
        _Docker(_docker_sample(containers=[], managed_containers=0), enabled=True),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        reaper_tick_stall_seconds=60,
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        assert (
            next(
                component
                for component in monitor.report()["components"]
                if component["id"] == "reaper"
            )["status"]
            == "ok"
        )
        reaper.value["last_tick_at"] = (now - timedelta(seconds=60, microseconds=1)).isoformat()
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    assert (
        next(component for component in report["components"] if component["id"] == "reaper")[
            "status"
        ]
        == "degraded"
    )
    assert "reaper_stale" in {issue["code"] for issue in report["issues"]}


@pytest.mark.asyncio
async def test_container_consistency_uses_name_and_reports_deleted_and_missing_label(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    records = [
        SessionRecord(session_id="name-mismatch", sandbox_id="expected-name"),
        SessionRecord(
            session_id="deleted",
            sandbox_id="deleted-container",
            state=SessionState.DELETED,
            deleted_at=now,
        ),
    ]
    containers = [
        {
            "container_name": "wrong-name",
            "container_id": "short-1",
            "state": "running",
            "session_id": "name-mismatch",
        },
        {
            "container_name": "deleted-container",
            "container_id": "short-2",
            "state": "exited",
            "session_id": "deleted",
        },
        {
            "container_name": "unlabelled",
            "container_id": "short-3",
            "state": "running",
            "session_id": "",
        },
    ]
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository(records),
        _Diagnostics(
            {"writer_running": True, "pending_events": 0, "fatal_conflicts": 0, "closed": False}
        ),
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": now.isoformat(),
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(_docker_sample(containers=containers, managed_containers=3), enabled=True),  # type: ignore[arg-type]
        host_probe=_HostProbe(),  # type: ignore[arg-type]
        clock=lambda: now,
    )
    try:
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    assert {issue["code"] for issue in report["issues"]} == {
        "deleted_session_container_present",
        "managed_container_missing_session_label",
        "session_container_name_mismatch",
    }
    assert report["snapshot"]["docker"]["containers"][0] == containers[0]


@pytest.mark.asyncio
async def test_container_consistency_reports_extra_container_with_same_session_label(
    tmp_path: Path,
) -> None:
    report = await _container_issue_report(
        tmp_path,
        [SessionRecord(session_id="session", sandbox_id="expected")],
        [
            {
                "container_name": "expected",
                "container_id": "short-1",
                "state": "running",
                "session_id": "session",
            },
            {
                "container_name": "duplicate",
                "container_id": "short-2",
                "state": "running",
                "session_id": "session",
            },
        ],
    )

    assert [issue["code"] for issue in report["issues"]] == ["duplicate_session_containers"]
    assert "expected" in report["issues"][0]["message"]
    assert "duplicate" in report["issues"][0]["message"]


@pytest.mark.asyncio
async def test_container_consistency_reports_labelled_container_when_sandbox_id_missing(
    tmp_path: Path,
) -> None:
    report = await _container_issue_report(
        tmp_path,
        [SessionRecord(session_id="session", sandbox_id=None)],
        [
            {
                "container_name": "unexpected",
                "container_id": "short-1",
                "state": "running",
                "session_id": "session",
            }
        ],
    )

    assert [issue["code"] for issue in report["issues"]] == ["session_sandbox_missing"]
    assert "unexpected" in report["issues"][0]["message"]


@pytest.mark.asyncio
async def test_monitor_critical_component_failure_is_error(tmp_path: Path) -> None:
    reaper_task = asyncio.create_task(_sleep_forever())
    monitor = SystemMonitor(
        _Repository(error="database locked"),
        _Diagnostics(
            {"writer_running": False, "pending_events": 0, "fatal_conflicts": 1, "closed": False}
        ),
        _Diagnostics([]),
        _Diagnostics(
            {
                "last_tick_at": None,
                "last_tick_completed_at": None,
                "last_error_at": None,
                "run_started_at": None,
                "stopping": False,
                "interval_seconds": 5,
            }
        ),
        reaper_task,
        tmp_path,
        _Docker(
            _docker_sample(
                available=False,
                image_available=False,
                error="daemon unavailable",
                containers=[],
                cpu_percent=None,
                memory_used_bytes=None,
                pids=None,
            ),
            enabled=True,
        ),  # type: ignore[arg-type]
        host_probe=_HostProbe(disk_percent=96),  # type: ignore[arg-type]
    )
    try:
        await monitor.sample_once()
        report = monitor.report()
    finally:
        reaper_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reaper_task

    assert report["status"] == "error"
    statuses = {item["id"]: item["status"] for item in report["components"]}
    assert statuses["sqlite"] == "error"
    assert statuses["docker"] == "error"
    assert statuses["worker_image"] == "error"
    assert statuses["journal"] == "error"
    assert statuses["workspace"] == "error"
    assert report["snapshot"]["docker"]["cpu_percent"] is None
    assert report["history"][0]["docker_cpu_percent"] is None
