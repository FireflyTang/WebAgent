#!/usr/bin/env python3
"""Run one worker command in a cancellable process group."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _pidfile(token: str) -> Path:
    if not token or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("invalid execution token")
    return _pidfile_root() / f"oca-exec-{token}.pid"


def _cancelfile(token: str) -> Path:
    _pidfile(token)  # Reuse the strict token validation.
    return _pidfile_root() / f"oca-exec-{token}.cancel"


def _pidfile_root() -> Path:
    return Path("/tmp")


def _process_start_time(pid: int) -> str | None:
    """Return Linux proc start ticks, which prevents stale PID reuse kills."""
    try:
        # ``comm`` can contain spaces/parentheses, so split after its final ')'.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rpartition(")")[2].split()
        return fields[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def _read_record(path: Path) -> tuple[int, str | None] | None:
    try:
        raw = path.read_text(encoding="ascii")
        value = json.loads(raw)
        if isinstance(value, dict) and isinstance(value.get("pid"), int):
            start_time = value.get("start_time")
            return value["pid"], start_time if isinstance(start_time, str) else None
        # Pre-record worker images used a bare PID.  Explicit cancel may still
        # honor it, but bulk stale cleanup will only remove its marker.
        return int(raw), None
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _terminate_group(pid: int, *, expected_start_time: str | None = None) -> bool:
    if expected_start_time is not None and _process_start_time(pid) != expected_start_time:
        return False
    try:
        # Children are launched in their own session.  Refuse to signal any
        # reused/non-worker process group even if its PID happens to match.
        if os.getpgid(pid) != pid:
            return False
    except ProcessLookupError:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def run(token: str, command: list[str]) -> int:
    path = _pidfile(token)
    cancel_path = _cancelfile(token)
    if cancel_path.exists():
        cancel_path.unlink(missing_ok=True)
        return 130
    process = subprocess.Popen(command, start_new_session=True)
    start_time = _process_start_time(process.pid)
    path.write_text(json.dumps({"pid": process.pid, "start_time": start_time}), encoding="ascii")
    if cancel_path.exists():
        _terminate_group(process.pid, expected_start_time=start_time)

    def stop(_signum: int, _frame: object) -> None:
        _terminate_group(process.pid, expected_start_time=start_time)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return process.wait()
    finally:
        path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)


def cancel(token: str) -> int:
    path = _pidfile(token)
    cancel_path = _cancelfile(token)
    cancel_path.touch(exist_ok=True)
    record = _read_record(path)
    if record is None:
        return 0
    pid, start_time = record
    _terminate_group(pid, expected_start_time=start_time)
    path.unlink(missing_ok=True)
    return 0


def cleanup() -> int:
    """Terminate only verifiably stale worker command groups in this container."""
    for path in _pidfile_root().glob("oca-exec-*.pid"):
        record = _read_record(path)
        if record is not None:
            pid, start_time = record
            # A legacy bare PID cannot be proven to identify the original
            # command after host death.  Remove its stale token but never kill
            # a potentially unrelated process.
            if start_time is not None:
                _terminate_group(pid, expected_start_time=start_time)
        path.unlink(missing_ok=True)
    for path in _pidfile_root().glob("oca-exec-*.cancel"):
        path.unlink(missing_ok=True)
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "cleanup":
        return cleanup()
    if len(sys.argv) < 3:
        return 2
    mode, token = sys.argv[1:3]
    if mode == "cancel":
        return cancel(token)
    if mode == "run" and len(sys.argv) > 3:
        return run(token, sys.argv[3:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
