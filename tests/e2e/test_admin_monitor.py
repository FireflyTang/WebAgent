from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_admin_monitor_contract_and_local_docker_disablement(tmp_path: Path) -> None:
    settings = Settings(
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'monitor.db'}",
        workspace_root=tmp_path / "workspaces",
        session_pause_after_seconds=3600,
        session_delete_after_seconds=7200,
        session_reaper_interval_seconds=3600,
    )
    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 2
        while True:
            response = client.get("/v1/admin/monitor")
            assert response.status_code == 200
            payload = response.json()
            if payload["snapshot"] is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    assert payload["snapshot"] is not None
    assert payload["status"] in {"ok", "degraded", "error"}
    assert payload["sample_interval_seconds"] == 5
    assert payload["retention_seconds"] == 3600
    assert len(payload["history"]) == 1
    assert set(payload) == {
        "status",
        "generated_at",
        "sample_interval_seconds",
        "retention_seconds",
        "snapshot",
        "history",
        "components",
        "issues",
        "tasks",
    }
    components = {component["id"]: component for component in payload["components"]}
    assert set(components) == {
        "sqlite",
        "docker",
        "worker_image",
        "journal",
        "reaper",
        "workspace",
    }
    assert components["docker"]["status"] == "disabled"
    assert components["worker_image"]["status"] == "disabled"
    assert components["reaper"]["status"] == "ok"
    assert payload["tasks"] == []
    docker = payload["snapshot"]["docker"]
    assert docker == {
        "available": False,
        "disabled": True,
        "managed_containers": 0,
        "cpu_percent": None,
        "memory_used_bytes": None,
        "pids": None,
        "error": None,
        "containers": [],
    }
    assert payload["history"][0]["docker_cpu_percent"] is None
    assert payload["history"][0]["docker_memory_used_bytes"] is None
