from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sessions.models import SessionRecord, utc_now


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="directory-key",
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'directory.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        fake_long_task_delay_ms=0,
        session_pause_after_seconds=3600,
        session_delete_after_seconds=7200,
        session_reaper_interval_seconds=3600,
    )


def _headers() -> dict[str, str]:
    return {}


def _openai_headers(session_id: str) -> dict[str, str]:
    return {"Authorization": "Bearer directory-key", "X-Session-ID": session_id}


def test_session_directory_create_patch_and_transcript_contract(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        first = client.post(
            "/v1/sessions",
            headers=_headers(),
            json={"title": "First task", "last_model": "glm-4.7", "last_effort": "medium"},
        )
        second = client.post("/v1/sessions", headers=_headers(), json={"title": "Second task"})
        assert first.status_code == 201
        assert second.status_code == 201
        first_id = first.json()["session_id"]
        second_id = second.json()["session_id"]

        directory = client.get("/v1/sessions", headers=_headers())
        assert directory.status_code == 200
        payload = directory.json()
        assert payload["server_now"].endswith("+00:00")
        assert [item["session_id"] for item in payload["sessions"]][:2] == [second_id, first_id]
        assert first.json()["state"] == "active"
        assert first.json()["delete_at"] is None
        assert first.json()["title"] == "First task"
        assert first.json()["last_model"] == "glm-4.7"
        assert first.json()["last_effort"] == "medium"
        assert first.json()["compatible"] is True
        assert first.json()["compatibility_reason"] is None

        asyncio.run(
            client.app.state.repository.create(
                SessionRecord(
                    session_id="legacy-zhipu",
                    sandbox_id="local-legacy",
                    claude_session_id="zhipu-legacy",
                    metadata={"runtime_backend": "ZhipuRuntime"},
                )
            )
        )
        listed_legacy = client.get("/v1/sessions", headers=_headers()).json()
        legacy = next(
            item for item in listed_legacy["sessions"] if item["session_id"] == "legacy-zhipu"
        )
        assert legacy["compatible"] is False
        assert legacy["compatibility_reason"] == "运行时后端不兼容"
        assert (
            client.get("/v1/sessions/legacy-zhipu", headers=_headers()).json()["compatible"]
            is False
        )

        patched = client.patch(
            f"/v1/sessions/{first_id}",
            headers=_headers(),
            json={"title": "Renamed", "last_model": "glm-5.2", "last_effort": "high"},
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "Renamed"
        assert patched.json()["last_model"] == "glm-5.2"
        assert patched.json()["last_effort"] == "high"

        title_only = client.patch(
            f"/v1/sessions/{first_id}", headers=_headers(), json={"title": "Title only"}
        )
        model_only = client.patch(
            f"/v1/sessions/{first_id}", headers=_headers(), json={"last_model": "glm-5"}
        )
        assert title_only.json()["title"] == "Title only"
        assert title_only.json()["last_model"] == "glm-5.2"
        assert model_only.json()["title"] == "Title only"
        assert model_only.json()["last_model"] == "glm-5"
        assert model_only.json()["last_effort"] == "high"

        invalid_effort = client.patch(
            f"/v1/sessions/{first_id}", headers=_headers(), json={"last_effort": "turbo"}
        )
        assert invalid_effort.status_code == 400
        assert "effort" in invalid_effort.json()["error"]["message"]

        chat = client.post(
            "/v1/chat/completions",
            headers=_openai_headers(first_id),
            json={
                "model": "claude-code-agent",
                "messages": [{"role": "user", "content": "create a calculator"}],
            },
        )
        assert chat.status_code == 200
        transcript = client.get(f"/v1/sessions/{first_id}/transcript", headers=_headers())

    assert transcript.status_code == 200
    assert next((item["role"], item["content"]) for item in transcript.json()["messages"]) == (
        "user",
        "create a calculator",
    )
    assert transcript.json()["messages"][1]["role"] == "assistant"


def test_session_rest_no_longer_requires_demo_bearer_key(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post("/v1/sessions", json={"title": "No bearer"})
        listed = client.get("/v1/sessions")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["title"] == "No bearer"


def test_session_history_has_no_transcript_fallback_before_web_events(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post("/v1/sessions", json={}).json()
        session_id = created["session_id"]
        asyncio.run(
            client.app.state.session_logger.append(
                session_id,
                title="用户消息",
                content="old transcript must not become a UI event",
            )
        )
        history = client.get(f"/v1/sessions/{session_id}/history")

    assert history.status_code == 200
    assert history.json() == {"session_id": session_id, "events": []}


def test_paused_session_exposes_derived_delete_deadline(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        created = client.post("/v1/sessions", headers=_headers(), json={}).json()
        paused = client.post(f"/v1/sessions/{created['session_id']}/pause", headers=_headers())

    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert paused.json()["delete_at"] is not None


def test_session_list_projects_all_lifecycle_states_in_activity_order(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        active = client.post("/v1/sessions", headers=_headers(), json={}).json()["session_id"]
        paused = client.post("/v1/sessions", headers=_headers(), json={}).json()["session_id"]
        expiring = client.post("/v1/sessions", headers=_headers(), json={}).json()["session_id"]
        deleted = client.post("/v1/sessions", headers=_headers(), json={}).json()["session_id"]
        assert client.post(f"/v1/sessions/{paused}/pause", headers=_headers()).status_code == 200
        assert client.post(f"/v1/sessions/{expiring}/pause", headers=_headers()).status_code == 200
        assert client.delete(f"/v1/sessions/{deleted}", headers=_headers()).status_code == 200

        async def arrange_activity() -> None:
            repository = client.app.state.repository
            now = utc_now()
            for session_id, at in (
                (active, now),
                (paused, now - timedelta(minutes=10)),
                (deleted, now - timedelta(minutes=20)),
                (expiring, now - timedelta(minutes=110)),
            ):
                record = await repository.get(session_id)
                assert record is not None
                await repository.update(
                    replace(record, last_activity_at=at), expected_version=record.version
                )

        asyncio.run(arrange_activity())
        response = client.get("/v1/sessions", headers=_headers())

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert [session["session_id"] for session in sessions] == [active, paused, deleted, expiring]
    assert [session["state"] for session in sessions] == ["active", "paused", "deleted", "expiring"]
    assert sessions[0]["delete_at"] is None
    assert sessions[1]["delete_at"] is not None
    assert sessions[2]["delete_at"] is not None
    assert sessions[3]["delete_at"] is not None
