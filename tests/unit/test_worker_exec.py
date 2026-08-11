from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture()
def worker_exec_module():
    source = Path(__file__).parents[2] / "ops" / "worker_exec.py"
    spec = importlib.util.spec_from_file_location("test_worker_exec", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_targets_only_verified_worker_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_exec_module
) -> None:
    token = "a" * 32
    marker = tmp_path / f"oca-exec-{token}.pid"
    marker.write_text(json.dumps({"pid": 1234, "start_time": "start-1"}), encoding="ascii")
    unrelated = tmp_path / "not-a-worker.pid"
    unrelated.write_text("keep", encoding="ascii")
    killed: list[tuple[int, str | None]] = []

    monkeypatch.setattr(worker_exec_module, "_pidfile_root", lambda: tmp_path)
    monkeypatch.setattr(
        worker_exec_module,
        "_terminate_group",
        lambda pid, *, expected_start_time=None: killed.append((pid, expected_start_time)) or True,
    )

    assert worker_exec_module.cleanup() == 0
    assert killed == [(1234, "start-1")]
    assert not marker.exists()
    assert unrelated.read_text() == "keep"


def test_pid_start_time_mismatch_never_signals_reused_pid(
    monkeypatch: pytest.MonkeyPatch, worker_exec_module
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_exec_module, "_process_start_time", lambda _pid: "new-start")
    monkeypatch.setattr(worker_exec_module.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    assert worker_exec_module._terminate_group(1234, expected_start_time="old-start") is False
    assert calls == []


def test_cancel_before_run_leaves_marker_that_prevents_late_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_exec_module
) -> None:
    token = "b" * 32
    monkeypatch.setattr(worker_exec_module, "_pidfile_root", lambda: tmp_path)

    assert worker_exec_module.cancel(token) == 0
    marker = tmp_path / f"oca-exec-{token}.cancel"
    assert marker.exists()

    def unexpected_spawn(*args: object, **kwargs: object):
        del args, kwargs
        raise AssertionError("a pre-cancelled execution must not spawn")

    monkeypatch.setattr(worker_exec_module.subprocess, "Popen", unexpected_spawn)
    assert worker_exec_module.run(token, ["sleep", "17"]) == 130
    assert not marker.exists()
