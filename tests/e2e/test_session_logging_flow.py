from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.config import Settings
from app.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'flow.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        fake_long_task_delay_ms=0,
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )


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


def _send_turn(websocket, content: str) -> None:
    websocket.send_json(
        {
            "type": "message",
            "model": "test-model",
            "content": content,
            "provider": {
                "base_url": "https://provider.example",
                "api_key": "test-key",
                "auth_env": "ANTHROPIC_AUTH_TOKEN",
            },
        }
    )
    while websocket.receive_json()["type"] != "done":
        pass


def test_debug_log_keeps_legacy_chat_entries_in_turn_order(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        uploaded = client.post(
            "/v1/sessions/logged-flow/files",
            files=[("files", ("需求.txt", "加法演示".encode(), "text/plain"))],
        )
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "logged-flow"})
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"
            _send_turn(websocket, "创建计算器，实现加法")
            _send_turn(websocket, "增加减法")
        transcript = client.get("/v1/sessions/logged-flow/transcript")
        log = client.get("/v1/sessions/logged-flow/log")

    assert uploaded.status_code == 200
    assert transcript.status_code == 200
    messages = transcript.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message["content"] for message in messages][::2] == ["创建计算器，实现加法", "增加减法"]
    assert "测试通过" in messages[1]["content"]
    assert all(message["model"] == "test-model" for message in messages)
    assert log.status_code == 200
    assert "用户输入" in log.text
    assert "创建计算器，实现加法" in log.text
    assert "Assistant 输出" in log.text
    assert "测试通过" in log.text
    assert "总耗时秒" in log.text
    assert "上传文件" not in log.text
