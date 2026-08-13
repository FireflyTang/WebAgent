from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.api.web import router as web_router
from app.runtime.events import TextDelta
from app.runtime.model_catalog import ModelCatalog, ModelCatalogUnavailableError
from app.sessions.models import SessionTurnRequest
from app.sessions.service import SessionTurnCompleted


class CapturingUiEventRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def append_ui_event(self, session_id: str, **kwargs: object) -> None:
        self.events.append({"session_id": session_id, **kwargs})

    async def list_ui_events(self, session_id: str) -> list[object]:
        del session_id
        return []


class CapturingService:
    def __init__(self) -> None:
        self.requests: list[SessionTurnRequest] = []
        self.providers = []
        self.efforts = []
        self.repository = CapturingUiEventRepository()

    async def stream_events(
        self, request: SessionTurnRequest, session_id: str, *, provider=None, effort=None
    ) -> AsyncIterator[TextDelta | SessionTurnCompleted]:
        del session_id
        self.requests.append(request)
        self.providers.append(provider)
        self.efforts.append(effort)

        async def events() -> AsyncIterator[TextDelta | SessionTurnCompleted]:
            yield TextDelta("ok")
            yield SessionTurnCompleted(True, "stop", None, None, 0.0)

        return events()


@pytest.mark.asyncio
async def test_model_catalog_discovers_caches_and_uses_auth_env() -> None:
    calls = 0
    now = 100.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url == "https://provider.example/v1/models"
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(200, json={"data": [{"id": "glm-4.7"}, {"id": "glm-5.2"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = ModelCatalog(
            api_key="key",
            base_url="https://provider.example/",
            auth_env="ANTHROPIC_API_KEY",
            cache_seconds=300,
            client=client,
            clock=lambda: now,
        )
        assert await catalog.discover() == ("glm-4.7", "glm-5.2")
        assert await catalog.discover() == ("glm-4.7", "glm-5.2")
        now = 400.0
        assert await catalog.discover() == ("glm-4.7", "glm-5.2")
    assert calls == 2


@pytest.mark.asyncio
async def test_model_catalog_uses_bearer_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, json={"data": [{"id": "provider-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = ModelCatalog(
            api_key="token",
            base_url="https://provider.example",
            auth_env="ANTHROPIC_AUTH_TOKEN",
            client=client,
        )
        assert await catalog.discover() == ("provider-model",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(401), "上游返回 HTTP 状态 401"),
        (httpx.Response(404), "上游返回 HTTP 状态 404"),
        (
            httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
            "上游返回的 JSON 无效",
        ),
        (httpx.Response(200, json={"data": []}), "上游模型目录为空"),
    ],
)
async def test_strict_discovery_reports_upstream_failure_reason(
    response: httpx.Response, reason: str
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        catalog = ModelCatalog(
            api_key="provider-secret",
            base_url="https://provider.example",
            auth_env="ANTHROPIC_AUTH_TOKEN",
            client=client,
        )
        with pytest.raises(ModelCatalogUnavailableError, match=reason) as failure:
            await catalog.discover()

    assert failure.value.endpoint == "https://provider.example/v1/models"
    assert "provider-secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_strict_discovery_timeout_is_retryable_and_can_recover() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json={"data": [{"id": "recovered-model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = ModelCatalog(
            api_key="provider-secret",
            base_url="https://provider.example",
            auth_env="ANTHROPIC_AUTH_TOKEN",
            client=client,
        )
        with pytest.raises(ModelCatalogUnavailableError, match="上游请求超时"):
            await catalog.discover()
        assert await catalog.discover() == ("recovered-model",)

    assert calls == 2


def test_web_config_models_endpoint_and_websocket_forward_provider_model(
    monkeypatch, caplog
) -> None:
    settings = SimpleNamespace(
        runtime_backend="claude",
        sandbox_backend="docker",
        session_pause_after_seconds=1800,
        session_delete_after_seconds=7200,
    )
    service = CapturingService()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_service = service
    catalog_instances = []

    class FakeCatalog:
        def __init__(self, *, api_key, base_url, auth_env, **kwargs):
            self.api_key = api_key
            self.base_url = base_url
            self.auth_env = auth_env
            catalog_instances.append(self)

        async def discover(self):
            if self.api_key == "provider-down":
                raise ModelCatalogUnavailableError(
                    "上游返回 HTTP 状态 401", endpoint=self.safe_endpoint
                )
            return ("glm-4.7", "glm-5-turbo", "glm-5.2")

        @property
        def safe_endpoint(self):
            return f"{self.base_url.rstrip('/')}/v1/models"

        @property
        def api_key_fingerprint(self):
            return hashlib.sha256(self.api_key.encode()).hexdigest()[:10]

        async def aclose(self):
            return None

    monkeypatch.setattr(web_api, "ModelCatalog", FakeCatalog)
    app.include_router(web_router)

    with TestClient(app) as client:
        # The browser's scoped discovery route is the only model catalog API.
        # The former public compatibility endpoints must stay absent.
        assert client.get("/v1/models").status_code == 404
        assert client.post("/v1/chat/completions", json={}).status_code == 404

        config = client.get("/v1/web/config")
        assert config.status_code == 200
        assert config.json() == {
            "runtime": "claude",
            "sandbox": "docker",
            "provider_auth_modes": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"],
            "policies": {
                "pause_after_seconds": 1800,
                "delete_after_seconds": 7200,
                "runtime": "claude",
                "sandbox": "docker",
            },
        }
        models = client.post(
            "/v1/web/models",
            json={
                "base_url": "https://provider.example/",
                "api_key": "token",
                "auth_env": "ANTHROPIC_AUTH_TOKEN",
            },
        )
        assert models.json() == {
            "models": ["glm-4.7", "glm-5-turbo", "glm-5.2"],
            "default_model": "glm-4.7",
        }
        assert catalog_instances[-1].base_url == "https://provider.example/"
        assert catalog_instances[-1].auth_env == "ANTHROPIC_AUTH_TOKEN"
        key_header_mode = client.post(
            "/v1/web/models",
            json={
                "base_url": "https://provider.example",
                "api_key": "x-api-key-value",
                "auth_env": "ANTHROPIC_API_KEY",
            },
        )
        assert key_header_mode.status_code == 200
        assert catalog_instances[-1].auth_env == "ANTHROPIC_API_KEY"
        assert "x-api-key-value" not in key_header_mode.text
        unavailable = client.post(
            "/v1/web/models",
            json={
                "base_url": "https://provider.example",
                "api_key": "provider-down",
                "auth_env": "ANTHROPIC_AUTH_TOKEN",
            },
        )
        assert unavailable.status_code == 503
        assert "provider-down" not in unavailable.text
        assert unavailable.json() == {
            "detail": "Provider 模型目录不可用：https://provider.example/v1/models（上游返回 HTTP 状态 401）"
        }
        assert "endpoint=https://provider.example/v1/models" in caplog.text
        assert "auth_env=ANTHROPIC_AUTH_TOKEN" in caplog.text
        assert "api_key_fingerprint=" in caplog.text
        assert "provider-down" not in caplog.text
        assert (
            client.post(
                "/v1/web/models",
                json={"base_url": "not-a-url", "api_key": "token"},
            ).status_code
            == 422
        )

        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "model-session"})
            assert websocket.receive_json()["type"] == "sync_begin"
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "message",
                    "model": "glm-5.2",
                    "content": "hello",
                    "effort": "medium",
                    "provider": {
                        "base_url": "https://provider.example",
                        "api_key": "key",
                        "auth_env": "ANTHROPIC_API_KEY",
                    },
                }
            )
            user = websocket.receive_json()
            started = websocket.receive_json()
            delta = websocket.receive_json()
            done = websocket.receive_json()
            assert user["type"] == "user_message" and user["sequence"] == 0
            assert started["type"] == "turn_started"
            assert delta["type"] == "delta" and delta["content"] == "ok"
            assert done["type"] == "done" and done["completed"] is True
            assert [started["sequence"], delta["sequence"], done["sequence"]] == [1, 2, 3]
            assert all(
                event["turn_id"] == started["turn_id"] and event["at"]
                for event in (started, delta, done)
            )
            assert service.requests[-1].model == "glm-5.2"
            assert service.providers[-1].base_url == "https://provider.example"
            assert service.providers[-1].api_key == "key"
            assert service.providers[-1].auth_env == "ANTHROPIC_API_KEY"
            assert service.efforts[-1] == "medium"

            websocket.send_json(
                {
                    "type": "message",
                    "model": "glm-4.7",
                    "content": "invalid effort",
                    "effort": "turbo",
                    "provider": {
                        "base_url": "https://provider.example",
                        "api_key": "key",
                        "auth_env": "ANTHROPIC_API_KEY",
                    },
                }
            )
            invalid_effort = websocket.receive_json()
            assert invalid_effort == {
                "type": "error",
                "code": "invalid_effort",
                "message": "effort 必须是以下值之一：high, low, max, medium, xhigh",
                "recoverable": True,
            }

            websocket.send_json(
                {
                    "type": "message",
                    "model": "not-configured",
                    "content": "hello",
                    "provider": {
                        "base_url": "https://provider.example",
                        "api_key": "key",
                        "auth_env": "ANTHROPIC_API_KEY",
                    },
                }
            )
            rejected = websocket.receive_json()
            assert rejected["code"] == "invalid_model"
            assert rejected["recoverable"] is True
            assert len(service.requests) == 1

            websocket.send_json(
                {
                    "type": "message",
                    "model": "glm-4.7",
                    "content": "provider unavailable",
                    "provider": {
                        "base_url": "https://provider.example",
                        "api_key": "provider-down",
                        "auth_env": "ANTHROPIC_AUTH_TOKEN",
                    },
                }
            )
            unavailable_event = websocket.receive_json()
            assert unavailable_event == {
                "type": "error",
                "code": "provider_unavailable",
                "message": "Provider 模型目录不可用：https://provider.example/v1/models（上游返回 HTTP 状态 401）",
                "recoverable": True,
            }
            assert "provider-down" not in str(unavailable_event)
            assert len(service.requests) == 1

            # A request-level validation error does not close the socket.
            websocket.send_json(
                {
                    "type": "message",
                    "model": "glm-4.7",
                    "content": "retry",
                    "provider": {
                        "base_url": "https://provider.example",
                        "api_key": "key",
                        "auth_env": "ANTHROPIC_API_KEY",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "user_message"
            assert websocket.receive_json()["type"] == "turn_started"
            assert websocket.receive_json()["type"] == "delta"
            assert websocket.receive_json()["type"] == "done"
            assert len(service.requests) == 2
