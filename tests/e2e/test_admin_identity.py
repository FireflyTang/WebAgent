from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.config import Settings
from app.main import create_app
from app.runtime.base import RuntimeContext
from app.runtime.events import Progress, RuntimeEvent


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'admin.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        fake_long_task_delay_ms=0,
        session_pause_after_seconds=3600,
        session_delete_after_seconds=7200,
        session_reaper_interval_seconds=3600,
    )


def _user_headers(user_id: str) -> dict[str, str]:
    return {"x-webagent-user-id": user_id}


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


class _BlockingRuntime:
    def __init__(self) -> None:
        self.cancelled = False

    async def create_session(self, context: RuntimeContext) -> str:
        del context
        return "blocking-runtime"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id, context

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message, context
        try:
            yield Progress("tool", "still running", "running", tool_name="Bash")
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


def test_admin_precreates_users_and_identity_is_case_insensitive(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post("/v1/admin/users", json={"name": "  Alice  Example "})
        assert created.status_code == 201
        user = created.json()
        assert user["name"] == "Alice Example"
        assert user["enabled"] is True

        verified = client.post("/v1/users/verify", json={"name": "ＡＬＩＣＥ example"})
        assert verified.status_code == 200
        assert verified.json()["user_id"] == user["user_id"]
        assert client.post("/v1/admin/users", json={"name": "alice example"}).status_code == 409
        assert client.post("/v1/admin/users", json={"name": "   "}).status_code == 400

        disabled = client.patch(f"/v1/admin/users/{user['user_id']}", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert client.post("/v1/users/verify", json={"name": "Alice Example"}).status_code == 403
        assert client.get("/v1/users/me", headers=_user_headers(user["user_id"])).status_code == 403


def test_session_routes_scope_by_enabled_user_and_keep_headerless_compatibility(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        alice = client.post("/v1/admin/users", json={"name": "Alice"}).json()
        bob = client.post("/v1/admin/users", json={"name": "Bob"}).json()
        alice_headers = _user_headers(alice["user_id"])
        bob_headers = _user_headers(bob["user_id"])

        alice_session = client.post(
            "/v1/sessions", headers=alice_headers, json={"title": "Alice task"}
        ).json()
        bob_session = client.post(
            "/v1/sessions", headers=bob_headers, json={"title": "Bob task"}
        ).json()
        assert alice_session["owner_user_id"] == alice["user_id"]
        assert bob_session["owner_user_id"] == bob["user_id"]

        alice_list = client.get("/v1/sessions", headers=alice_headers).json()["sessions"]
        assert [session["session_id"] for session in alice_list] == [alice_session["session_id"]]
        assert (
            client.get(
                f"/v1/sessions/{bob_session['session_id']}", headers=alice_headers
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"/v1/sessions/{bob_session['session_id']}",
                headers=alice_headers,
                json={"title": "stolen"},
            ).status_code
            == 404
        )

        # Headerless callers are the explicit local-admin/test compatibility mode.
        unscoped = client.get("/v1/sessions").json()["sessions"]
        assert {session["session_id"] for session in unscoped} == {
            alice_session["session_id"],
            bob_session["session_id"],
        }
        assert client.get(f"/v1/sessions/{bob_session['session_id']}").status_code == 200


def test_websocket_hello_enforces_user_and_session_ownership(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        alice = client.post("/v1/admin/users", json={"name": "Alice"}).json()
        bob = client.post("/v1/admin/users", json={"name": "Bob"}).json()
        session = client.post(
            "/v1/sessions", headers=_user_headers(alice["user_id"]), json={}
        ).json()

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json(
                {"type": "hello", "session_id": session["session_id"], "user_id": bob["user_id"]}
            )
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "session_not_found"

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "session_id": session["session_id"],
                    "user_id": alice["user_id"],
                }
            )
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"

        # Omitting user_id preserves the legacy test/background control plane.
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": session["session_id"]})
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"


def test_disabled_user_cannot_start_a_turn_on_an_existing_websocket(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        user = client.post("/v1/admin/users", json={"name": "Alice"}).json()
        session = client.post(
            "/v1/sessions", headers=_user_headers(user["user_id"]), json={}
        ).json()
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "session_id": session["session_id"],
                    "user_id": user["user_id"],
                }
            )
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"
            assert (
                client.patch(
                    f"/v1/admin/users/{user['user_id']}", json={"enabled": False}
                ).status_code
                == 200
            )

            websocket.send_json(_message("must not start"))
            error = websocket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_user"
            assert (
                client.app.state.active_turns.snapshot(session["session_id"])["task_state"]
                == "idle"
            )
            assert (
                client.get(f"/v1/sessions/{session['session_id']}/history").json()["events"] == []
            )


def test_disabled_user_cannot_stop_an_existing_background_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Catalog:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def discover(self) -> tuple[str, ...]:
            return ("test-model",)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(web_api, "ModelCatalog", Catalog)
    with TestClient(create_app(_settings(tmp_path))) as client:
        runtime = _BlockingRuntime()
        client.app.state.session_service.runtime = runtime
        user = client.post("/v1/admin/users", json={"name": "Alice"}).json()
        session = client.post(
            "/v1/sessions", headers=_user_headers(user["user_id"]), json={}
        ).json()
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "session_id": session["session_id"],
                    "user_id": user["user_id"],
                }
            )
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(_message("keep running"))
            while True:
                event = websocket.receive_json()
                if event["type"] == "turn_started":
                    turn_id = event["turn_id"]
                    break

            assert (
                client.patch(
                    f"/v1/admin/users/{user['user_id']}", json={"enabled": False}
                ).status_code
                == 200
            )
            websocket.send_json({"type": "stop", "turn_id": turn_id})
            while True:
                event = websocket.receive_json()
                if event["type"] == "error":
                    break
            assert event["code"] == "invalid_user"
            assert (
                client.app.state.active_turns.snapshot(session["session_id"])["task_state"]
                == "running"
            )
            assert runtime.cancelled is False


def test_managed_settings_persist_validate_and_use_optimistic_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        initial = client.get("/v1/admin/settings").json()
        assert initial["restart_required"] is False
        patched = client.patch(
            "/v1/admin/settings",
            json={"version": initial["version"], "docker_memory": "1g"},
        )
        assert patched.status_code == 200
        assert patched.json()["saved"]["docker_memory"] == "1g"
        assert patched.json()["active"]["docker_memory"] == settings.docker_memory
        assert patched.json()["restart_required"] is True
        assert (
            client.patch(
                "/v1/admin/settings",
                json={"version": initial["version"], "docker_memory": "2g"},
            ).status_code
            == 409
        )

    with TestClient(create_app(settings)) as client:
        restored = client.get("/v1/admin/settings").json()
        assert restored["active"]["docker_memory"] == "1g"
        assert restored["saved"]["docker_memory"] == "1g"
        assert restored["restart_required"] is False


def test_managed_settings_reject_invalid_lifecycle_order_without_persisting(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        initial = client.get("/v1/admin/settings").json()
        response = client.patch(
            "/v1/admin/settings",
            json={
                "version": initial["version"],
                "session_pause_after_seconds": 30,
                "session_delete_after_seconds": 20,
            },
        )
        assert response.status_code == 400
        unchanged = client.get("/v1/admin/settings").json()
        assert unchanged["version"] == initial["version"]
        assert unchanged["saved"] == initial["saved"]

    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
