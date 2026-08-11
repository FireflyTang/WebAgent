from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="flow-log-key",
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


def test_debug_log_keeps_legacy_chat_entries_in_turn_order(tmp_path: Path) -> None:
    chat_headers = {"Authorization": "Bearer flow-log-key", "X-Session-ID": "logged-flow"}
    request = {
        "model": "claude-code-agent",
        "messages": [{"role": "user", "content": "创建计算器，实现加法"}],
    }

    with TestClient(create_app(_settings(tmp_path))) as client:
        uploaded = client.post(
            "/v1/sessions/logged-flow/files",
            files=[("files", ("需求.txt", "加法演示".encode(), "text/plain"))],
        )
        first = client.post("/v1/chat/completions", headers=chat_headers, json=request)
        request["messages"] = [{"role": "user", "content": "增加减法"}]
        second = client.post("/v1/chat/completions", headers=chat_headers, json=request)
        transcript = client.get("/v1/sessions/logged-flow/transcript")
        log = client.get("/v1/sessions/logged-flow/log")

    assert uploaded.status_code == 200
    assert first.status_code == 200
    assert second.status_code == 200
    assert transcript.status_code == 200
    messages = transcript.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message["content"] for message in messages][::2] == ["创建计算器，实现加法", "增加减法"]
    assert "测试通过" in messages[1]["content"]
    assert all(message["model"] == "claude-code-agent" for message in messages)
    assert log.status_code == 200
    assert "用户消息" in log.text
    assert "创建计算器，实现加法" in log.text
    assert "Claude Code 输出" in log.text
    assert "测试通过" in log.text
    assert "总耗时秒" in log.text
    assert "上传文件" not in log.text
