from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.config import Settings
from app.main import create_app
from app.runtime.base import RuntimeContext
from app.runtime.events import Completed, Diagnostic, Progress, RuntimeEvent, TextDelta, Usage


class WebProgressRuntime:
    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        return "web-progress-runtime"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        yield Progress(
            "tool",
            "正在写入文件",
            "completed",
            tool_name="Write",
            tool_use_id="write-1",
            task_id="task-1",
            duration_seconds=2.5,
        )
        yield Diagnostic("tool_use", tool_name="Write", tool_input={"private": "diagnostic-only"})
        yield TextDelta("done")
        yield Usage(input_tokens=7, output_tokens=11)
        yield Completed("stop")


@pytest.fixture(autouse=True)
def _provider_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    class Catalog:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def discover(self) -> tuple[str, ...]:
            return ("test-model",)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(web_api, "ModelCatalog", Catalog)


def _message(content: str) -> dict[str, object]:
    return {
        "type": "message",
        "content": content,
        "model": "test-model",
        "provider": {
            "base_url": "https://provider.example",
            "api_key": "test-key",
            "auth_env": "ANTHROPIC_AUTH_TOKEN",
        },
    }


def test_websocket_emits_progress_and_done_summary(tmp_path: Path) -> None:
    settings = Settings(
        sandbox_backend="local",
        runtime_backend="fake",
        database_url=f"sqlite:///{tmp_path / 'progress.db'}",
        workspace_root=tmp_path / "workspaces",
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.session_service.runtime = WebProgressRuntime()
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "p"})
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(_message("go"))
            messages = []
            while True:
                event = websocket.receive_json()
                messages.append(event)
                if event["type"] == "done":
                    break
        log = client.get("/v1/sessions/p/log")
        assert log.status_code == 200
        assert "运行时诊断：sdk.tool_use" in log.text
        assert "diagnostic-only" in log.text
        assert "test-key" not in log.text

    progress = next(
        message
        for message in messages
        if message["type"] == "progress" and message["phase"] == "tool"
    )
    done = next(message for message in messages if message["type"] == "done")
    assert progress["tool_name"] == "Write"
    assert progress["task_id"] == "task-1"
    assert progress["duration_seconds"] == 2.5
    assert all("diagnostic-only" not in str(message) for message in messages)
    assert {"starting", "thinking", "tool", "finalizing"}.issubset(
        {message["phase"] for message in messages if message["type"] == "progress"}
    )
    assert done["completed"] is True
    assert done["stop_reason"] == "stop"
    assert done["usage"] == {"input_tokens": 7, "output_tokens": 11}
    assert done["duration_seconds"] >= 0
