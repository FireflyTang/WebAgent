from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.config import Settings
from app.main import create_app


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        sandbox_backend="local",
        runtime_backend="fake",
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        fake_long_task_delay_ms=0,
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )


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


def test_web_page_uploads_and_lists_session_files(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert '<div id="root"></div>' in page.text
        assets = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"', page.text)
        assert {Path(asset).suffix for asset in assets} == {".css", ".js"}
        assert all(client.get(asset).status_code == 200 for asset in assets)

        uploaded = client.post(
            "/v1/sessions/web-files/files",
            files=[
                ("files", ("src/example.py", b"VALUE = 42\n", "text/x-python")),
                ("files", ("README.md", "演示项目\n".encode(), "text/markdown")),
            ],
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["files"] == [
            {"path": "src/example.py", "size": 11},
            {"path": "README.md", "size": 13},
        ]

        listed = client.get("/v1/sessions/web-files/files")
        assert listed.status_code == 200
        assert listed.json()["files"] == [
            {"path": "README.md", "size": 13},
            {"path": "src/example.py", "size": 11},
        ]
        content = client.get("/v1/sessions/web-files/files/content/src/example.py")
        assert content.status_code == 200
        assert content.content == b"VALUE = 42\n"
        assert content.headers["content-type"].startswith("text/x-python")
        assert content.headers["content-disposition"].startswith("inline;")
        session = client.get("/v1/sessions/web-files").json()
        workspace = tmp_path / "workspaces" / session["sandbox_id"]
        assert (workspace / "src/example.py").read_text() == "VALUE = 42\n"


def test_upload_rejects_workspace_escape(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        response = client.post(
            "/v1/sessions/web-invalid/files",
            files=[("files", ("../escape.py", b"bad", "text/x-python"))],
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_workspace_path"
    assert not (tmp_path / "escape.py").exists()


def test_upload_rejects_symbolic_link_escape(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        created = client.post(
            "/v1/sessions/web-symlink/files",
            files=[("files", ("README.md", b"demo", "text/markdown"))],
        )
        assert created.status_code == 200
        session = client.get("/v1/sessions/web-symlink").json()
        workspace = tmp_path / "workspaces" / session["sandbox_id"]
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "escape").symlink_to(outside, target_is_directory=True)

        response = client.post(
            "/v1/sessions/web-symlink/files",
            files=[("files", ("escape/pwn.txt", b"bad", "text/plain"))],
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_workspace_path"
    assert not (outside / "pwn.txt").exists()


def test_websocket_chat_streams_and_reuses_session(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "web-chat"})
            assert websocket.receive_json() == {"type": "sync_begin", "session_id": "web-chat"}
            ready = websocket.receive_json()
            assert ready["type"] == "ready"
            assert ready["session_id"] == "web-chat"
            assert ready["task_state"] == "idle"

            websocket.send_json(_message("创建计算器，实现加法"))
            first = ""
            while True:
                event = websocket.receive_json()
                if event["type"] == "delta":
                    first += event["content"]
                if event["type"] == "done":
                    break
            assert "测试通过" in first

            websocket.send_json(_message("增加减法"))
            second = ""
            while True:
                event = websocket.receive_json()
                if event["type"] == "delta":
                    second += event["content"]
                if event["type"] == "done":
                    break
            assert "第 2 轮" in second


def test_websocket_session_replays_and_keeps_workspace_after_app_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    session_id = "web-restart"

    with TestClient(create_app(settings)) as first_client:
        with first_client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": session_id})
            assert websocket.receive_json() == {"type": "sync_begin", "session_id": session_id}
            assert websocket.receive_json()["type"] == "ready"

            websocket.send_json(_message("创建计算器，实现加法"))
            first_output = ""
            while True:
                event = websocket.receive_json()
                if event["type"] == "delta":
                    first_output += event["content"]
                if event["type"] == "done":
                    break

        first_session = first_client.get(f"/v1/sessions/{session_id}").json()

    assert "第 1 轮" in first_output
    assert "测试通过" in first_output
    workspace = tmp_path / "workspaces" / first_session["sandbox_id"]
    assert (workspace / "calculator.py").exists()

    with TestClient(create_app(settings)) as second_client:
        with second_client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": session_id})
            assert websocket.receive_json() == {"type": "sync_begin", "session_id": session_id}
            replayed = []
            while True:
                event = websocket.receive_json()
                if event["type"] == "ready":
                    assert event["session_id"] == session_id
                    assert event["task_state"] == "idle"
                    break
                replayed.append(event)
            assert any(event["type"] == "done" for event in replayed)

            websocket.send_json(_message("增加减法"))
            second_output = ""
            while True:
                event = websocket.receive_json()
                if event["type"] == "delta":
                    second_output += event["content"]
                if event["type"] == "done":
                    break

        second_session = second_client.get(f"/v1/sessions/{session_id}").json()
        transcript = second_client.get(f"/v1/sessions/{session_id}/transcript").json()

    assert "第 2 轮" in second_output
    assert "test_subtract" in second_output
    assert second_session["sandbox_id"] == first_session["sandbox_id"]
    assert second_session["runtime_session_id"] == first_session["runtime_session_id"]
    assert [message["role"] for message in transcript["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["content"] for message in transcript["messages"]][::2] == [
        "创建计算器，实现加法",
        "增加减法",
    ]
    calculator = workspace / "calculator.py"
    assert calculator.exists()
    assert "def add" in calculator.read_text()
    assert "def subtract" in calculator.read_text()
