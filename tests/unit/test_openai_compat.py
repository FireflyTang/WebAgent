from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.chat_completions import router as chat_router
from app.api.health import router as health_router
from app.api.models import router as models_router
from app.config import Settings
from app.openai_compat.schemas import ChatCompletionRequest
from app.openai_compat.sse import iter_openai_sse


class StubHandler:
    async def complete(self, request, session_id: str) -> str:
        return f"reply for {session_id}"

    async def stream(self, request, session_id: str):
        yield 'hello "world"'
        yield "!"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.state.settings = Settings(api_key="test-key")
    app.state.chat_completion_handler = StubHandler()
    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(chat_router)
    return TestClient(app)


def test_schema_rejects_non_text_content_and_userless_request() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="x", messages=[{"role": "user", "content": [{"type": "text"}]}])
    with pytest.raises(ValidationError, match="at least one user"):
        ChatCompletionRequest(model="x", messages=[{"role": "system", "content": "rules"}])


def test_healthz_is_public_and_models_requires_bearer(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    unauthorized = client.get("/v1/models")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["error"]["code"] == "invalid_api_key"
    response = client.get("/v1/models", headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "claude-code-agent"


def test_non_stream_completion_returns_generated_session(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={"model": "claude-code-agent", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.headers["X-Session-ID"] == response.json()["session_id"]
    assert response.json()["choices"][0]["message"]["content"].startswith("reply for")


def test_conflicting_session_ids_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key", "X-Session-ID": "header"},
        json={
            "model": "claude-code-agent",
            "session_id": "body",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 400


def test_semantic_fields_are_rejected_instead_of_silently_ignored(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={
            "model": "claude-code-agent",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "do_work"}}],
            "tool_choice": "required",
            "n": 2,
        },
    )
    # This fixture mounts routers without the application-level error handler;
    # FastAPI still proves that request validation rejects the semantic fields.
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sse_has_role_escaped_deltas_finish_and_done() -> None:
    events = [
        event.decode()
        async for event in iter_openai_sse(
            ['quote: "\n', "ok"], model="x", completion_id="fixed", created=1
        )
    ]
    payloads = [event.removeprefix("data: ").removesuffix("\n\n") for event in events]
    chunks = [json.loads(payload) for payload in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"]["content"] == 'quote: "\n'
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_sse_runtime_failure_is_visible_and_still_finishes() -> None:
    async def failing_stream():
        yield "started"
        raise RuntimeError("injected failure")

    events = [
        event.decode()
        async for event in iter_openai_sse(
            failing_stream(), model="x", completion_id="fixed", created=1
        )
    ]
    joined = "".join(events)
    assert "Runtime stream failed" in joined
    assert '"finish_reason":"stop"' in joined
    assert joined.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_sse_first_chunk_disconnect_closes_unstarted_delta_owner() -> None:
    class DeltaOwner:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise AssertionError("delta stream must not start before the role chunk")

        async def aclose(self) -> None:
            self.closed = True

    deltas = DeltaOwner()
    stream = iter_openai_sse(deltas, model="x")
    first = await anext(stream)
    assert b'"role":"assistant"' in first
    await stream.aclose()
    assert deltas.closed is True
