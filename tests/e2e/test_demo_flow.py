from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.openai_compat.schemas import ChatCompletionRequest
from app.runtime import FakeRuntime
from app.sandbox import LocalSandboxManager
from app.sessions import SessionLockRegistry, SQLiteSessionRepository
from app.sessions.service import SessionBusyError, SessionService


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'demo.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        fake_long_task_delay_ms=0,
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )


def auth(session_id: str) -> dict[str, str]:
    return {"Authorization": "Bearer test-key", "X-Session-ID": session_id}


def request(content: str, *, stream: bool = False) -> dict[str, object]:
    return {
        "model": "claude-code-agent",
        "stream": stream,
        "messages": [{"role": "user", "content": content}],
    }


def test_curl_style_multi_turn_pause_resume_delete(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        first = client.post(
            "/v1/chat/completions", headers=auth("demo-a"), json=request("创建计算器，实现加法")
        )
        assert first.status_code == 200
        assert "测试通过" in first.json()["choices"][0]["message"]["content"]

        paused = client.post("/v1/sessions/demo-a/pause", headers=auth("demo-a"))
        assert paused.json()["state"] == "expiring"

        second = client.post(
            "/v1/chat/completions", headers=auth("demo-a"), json=request("增加减法", stream=True)
        )
        assert second.status_code == 200
        assert "第 2 轮" in second.text
        assert "data: [DONE]\n\n" in second.text
        assert client.get("/v1/sessions/demo-a", headers=auth("demo-a")).json()["state"] == "active"

        deleted = client.delete("/v1/sessions/demo-a", headers=auth("demo-a"))
        assert deleted.json()["state"] == "deleted"
        gone = client.post("/v1/chat/completions", headers=auth("demo-a"), json=request("继续"))
        assert gone.status_code == 410
        assert gone.json()["error"]["code"] == "session_deleted"


def test_sessions_use_different_workspaces(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        client.post(
            "/v1/chat/completions", headers=auth("demo-a"), json=request("创建计算器")
        ).raise_for_status()
        client.post(
            "/v1/chat/completions", headers=auth("demo-b"), json=request("记录另一个任务")
        ).raise_for_status()

        a = client.get("/v1/sessions/demo-a", headers=auth("demo-a")).json()
        b = client.get("/v1/sessions/demo-b", headers=auth("demo-b")).json()
        assert a["sandbox_id"] != b["sandbox_id"]
        workspace_a = tmp_path / "workspaces" / a["sandbox_id"]
        workspace_b = tmp_path / "workspaces" / b["sandbox_id"]
        assert (workspace_a / "calculator.py").exists()
        assert not (workspace_b / "calculator.py").exists()


def test_application_errors_use_openai_root_shape(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path))) as client:
        unauthorized = client.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "invalid_api_key"

        unsupported = client.post(
            "/v1/chat/completions",
            headers=auth("errors"),
            json={
                "model": "claude-code-agent",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function"}],
            },
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["error"]["code"] == "invalid_request"

        conflict = client.post(
            "/v1/chat/completions",
            headers={**auth("header-id")},
            json={
                "model": "claude-code-agent",
                "session_id": "body-id",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert conflict.status_code == 400
        assert "error" in conflict.json()


def test_unavailable_docker_returns_openai_503(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.sandbox_backend = "docker"
    settings.docker_binary = "/definitely-missing/docker"

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth("missing-docker"),
            json=request("创建计算器"),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sandbox_unavailable"


def test_fake_session_continues_after_application_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/v1/chat/completions", headers=auth("restart-demo"), json=request("创建计算器")
        )
        assert first.status_code == 200

    with TestClient(create_app(settings)) as client:
        second = client.post(
            "/v1/chat/completions", headers=auth("restart-demo"), json=request("增加减法")
        )
        assert second.status_code == 200
        content = second.json()["choices"][0]["message"]["content"]
        assert "第 2 轮" in content
        session = client.get("/v1/sessions/restart-demo", headers=auth("restart-demo")).json()
        assert session["version"] >= 2


async def test_stream_reserves_session_before_http_iteration(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "busy.db")
    service = SessionService(
        repository,
        SessionLockRegistry(),
        LocalSandboxManager(tmp_path / "workspaces"),
        FakeRuntime(0, 0),
    )
    request_body = ChatCompletionRequest(
        model="claude-code-agent", messages=[{"role": "user", "content": "hello"}], stream=True
    )

    first = await service.stream(request_body, "busy-session")
    try:
        import pytest

        with pytest.raises(SessionBusyError):
            await service.stream(request_body, "busy-session")
        assert "第 1 轮" in "".join([chunk async for chunk in first])
    finally:
        await first.aclose()
        await repository.close()
