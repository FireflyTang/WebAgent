from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sessions.runtime_debug import append_runtime_debug


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        sandbox_backend="local",
        runtime_backend="fake",
        database_url=f"sqlite:///{tmp_path / 'sessions.db'}",
        workspace_root=tmp_path / "workspaces",
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )


def test_session_log_endpoint_renders_diagnostics_and_empty_existing_sessions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        asyncio.run(client.app.state.session_service.create_empty("log-session"))
        asyncio.run(
            client.app.state.session_logger.append(
                "log-session",
                title="Turn",
                content="hello <world>",
                metadata={"turn": 1},
                event_type="sdk.result",
            )
        )
        response = client.get("/v1/sessions/log-session/log")
        asyncio.run(client.app.state.session_service.create_empty("empty-session"))
        empty = client.get("/v1/sessions/empty-session/log")
        missing = client.get("/v1/sessions/no-log/log")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"] == "inline"
    assert "hello &lt;world&gt;" in response.text
    assert empty.status_code == 200
    assert "暂无诊断事件" in empty.text
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "session_not_found"


def test_session_log_endpoint_groups_live_sdk_diagnostics_in_sequence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        logger = client.app.state.session_logger
        asyncio.run(client.app.state.session_service.create_empty("grouped-log"))
        asyncio.run(logger.append("grouped-log", title="用户消息", content="ordinary interaction"))
        asyncio.run(
            append_runtime_debug(
                logger,
                "grouped-log",
                "sdk.stream",
                {"subtype": "content_block_delta", "thinking": "private thought"},
            )
        )
        asyncio.run(
            append_runtime_debug(
                logger,
                "grouped-log",
                "sdk.system",
                {"subtype": "init"},
            )
        )
        asyncio.run(
            append_runtime_debug(
                logger,
                "grouped-log",
                "sdk.tool_use",
                {"tool_name": "Bash", "tool_use_id": "tool-1", "tool_input": {"cmd": "pwd"}},
            )
        )
        first = client.get("/v1/sessions/grouped-log/log")
        asyncio.run(
            append_runtime_debug(
                logger,
                "grouped-log",
                "sdk.result",
                {"result": "stop", "usage": {"input_tokens": 2, "output_tokens": 4}},
            )
        )
        second = client.get("/v1/sessions/grouped-log/log")

    assert first.status_code == 200
    assert "诊断事件：4 条" in first.text
    assert "高频 SDK 诊断批次：2 条" in first.text
    assert "ordinary interaction" in first.text
    assert "工具调用" in first.text
    assert "Bash 命令" in first.text
    assert "pwd" in first.text
    assert "private thought" not in first.text
    assert second.status_code == 200
    assert "诊断事件：5 条" in second.text
    assert "最终结果" in second.text
    assert second.text.index("SDK 流事件") < second.text.index("工具调用")
