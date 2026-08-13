from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.runtime.base import ProviderConfig
from app.runtime.events import Progress, TextDelta, validate_effort
from app.runtime.model_catalog import ModelCatalog, ModelCatalogUnavailableError
from app.sessions.active_turns import ActiveTurn
from app.sessions.models import SessionTurnRequest
from app.sessions.service import SessionService, SessionServiceError, SessionTurnCompleted
from app.sessions.ui_events import (
    ActiveTurnBusyError,
    ActiveTurnNotRunningError,
    ActiveTurnRegistry,
    TurnSubscription,
    UiEventJournal,
    ui_event_key,
)

router = APIRouter(tags=["web-demo"])
web_root = Path(__file__).resolve().parent.parent / "web"
_AUTH_ENVS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
logger = logging.getLogger(__name__)


class ProviderModelsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=4096)
    auth_env: str = "ANTHROPIC_AUTH_TOKEN"

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        return value.strip()

    def model_post_init(self, __context: object) -> None:
        if self.auth_env not in _AUTH_ENVS:
            raise ValueError("auth_env must be ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY")


@router.get("/", include_in_schema=False)
async def web_index() -> FileResponse:
    return FileResponse(web_root / "index.html")


@router.get("/v1/web/config", include_in_schema=False)
async def web_config(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "runtime": settings.runtime_backend,
        "sandbox": settings.sandbox_backend,
        "provider_auth_modes": list(_AUTH_ENVS),
        "policies": {
            "pause_after_seconds": settings.session_pause_after_seconds,
            "delete_after_seconds": settings.session_delete_after_seconds,
            "runtime": settings.runtime_backend,
            "sandbox": settings.sandbox_backend,
        },
    }


@router.post("/v1/web/models", include_in_schema=False)
async def provider_models(payload: ProviderModelsRequest) -> dict[str, object]:
    catalog = ModelCatalog(
        api_key=payload.api_key,
        base_url=payload.base_url,
        auth_env=payload.auth_env,
    )
    try:
        models = await catalog.discover()
    except ModelCatalogUnavailableError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail=_provider_catalog_failure(catalog, payload.auth_env, exc),
        ) from exc
    finally:
        await catalog.aclose()
    return {"models": list(models), "default_model": models[0]}


def _provider_catalog_failure(
    catalog: ModelCatalog, auth_env: str, exc: ModelCatalogUnavailableError
) -> str:
    """Log safe discovery context and return a user-actionable error."""
    endpoint = getattr(catalog, "safe_endpoint", "<未配置>")
    fingerprint = getattr(catalog, "api_key_fingerprint", "<未知>")
    logger.warning(
        "Provider model catalog unavailable endpoint=%s auth_env=%s api_key_fingerprint=%s reason=%s",
        endpoint,
        auth_env,
        fingerprint,
        exc.reason,
    )
    return f"Provider 模型目录不可用：{endpoint}（{exc.reason}）"


async def _error(
    websocket: WebSocket,
    code: str,
    message: str,
    *,
    recoverable: bool = False,
    send_lock: asyncio.Lock | None = None,
) -> None:
    try:
        payload: dict[str, object] = {"type": "error", "code": code, "message": message}
        if recoverable:
            payload["recoverable"] = True
        await _send_json(websocket, payload, send_lock)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _send_json(
    websocket: WebSocket,
    event: dict[str, object],
    send_lock: asyncio.Lock | None = None,
) -> None:
    if send_lock is None:
        await websocket.send_json(event)
        return
    async with send_lock:
        await websocket.send_json(event)


async def _produce_turn(turn: ActiveTurn, registry: ActiveTurnRegistry) -> None:
    """Run and dispatch a turn independently of every WebSocket subscription."""
    try:
        async for event in turn.stream:
            if isinstance(event, TextDelta):
                intent = turn.intent("delta", content=event.text)
            elif isinstance(event, Progress):
                intent = turn.intent(
                    "progress",
                    phase=event.phase,
                    message=event.message,
                    status=event.status,
                    tool_name=event.tool_name,
                    tool_use_id=event.tool_use_id,
                    parent_tool_use_id=event.parent_tool_use_id,
                    task_id=event.task_id,
                    elapsed_seconds=event.elapsed_seconds,
                    duration_seconds=event.duration_seconds,
                    current=event.current,
                    total=event.total,
                )
            elif isinstance(event, SessionTurnCompleted):
                intent = turn.intent(
                    "done",
                    completed=event.completed,
                    stop_reason=event.stop_reason,
                    usage={
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                    },
                    duration_seconds=event.duration_seconds,
                )
            else:  # pragma: no cover - SessionService's event union is closed.
                continue
            # A hot producer must yield to a newly attached stop controller.
            # Re-checking the terminal gate after the yield drops a prepared
            # intent when stop won the race.
            await asyncio.sleep(0)
            dispatched = registry.dispatch(turn, intent)
            if dispatched is not None and dispatched["type"] == "done":
                break
    except asyncio.CancelledError:
        raise
    except SessionServiceError as exc:
        registry.dispatch(turn, turn.intent("error", code=exc.code, message=str(exc)))
        registry.dispatch(
            turn,
            turn.intent("done", completed=False, stop_reason=exc.code, usage=None),
        )
    except Exception:
        logger.exception(
            "Background turn failed session_id=%s turn_id=%s",
            turn.session_id,
            turn.turn_id,
        )
        registry.dispatch(turn, turn.intent("error", code="turn_failed", message="任务执行失败"))
        registry.dispatch(
            turn,
            turn.intent("done", completed=False, stop_reason="turn_failed", usage=None),
        )
    finally:
        await turn.stream.aclose()


async def _synchronize_subscription(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    journal: UiEventJournal,
    registry: ActiveTurnRegistry,
    subscription: TurnSubscription,
) -> bool:
    """Replay history after subscribing, then hand off to the live FIFO."""
    await _send_json(
        websocket,
        {"type": "sync_begin", "session_id": subscription.session_id},
        send_lock,
    )
    seen = set()
    for event in await journal.list_events(subscription.session_id):
        key = ui_event_key(event)
        if key in seen:
            continue
        seen.add(key)
        await _send_json(websocket, event, send_lock)

    # Capture one finite batch. Events accepted while this batch is sent stay
    # in the subscription FIFO and become live events after ready.
    buffered: list[dict[str, object]] = []
    while True:
        try:
            event = subscription.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if event is None:
            await websocket.close(code=1013)
            return False
        if ui_event_key(event) not in seen:
            seen.add(ui_event_key(event))
            buffered.append(event)
    for event in buffered:
        await _send_json(websocket, event, send_lock)

    snapshot = registry.snapshot(subscription.session_id)
    await _send_json(
        websocket,
        {
            "type": "ready",
            "session_id": subscription.session_id,
            **snapshot,
        },
        send_lock,
    )
    return True


async def _forward_subscription(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    subscription: TurnSubscription,
) -> None:
    while True:
        event = await subscription.queue.get()
        if event is None:
            try:
                await websocket.close(code=1013)
            except RuntimeError:
                pass
            return
        try:
            await _send_json(websocket, event, send_lock)
        except (RuntimeError, WebSocketDisconnect):
            return


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()
    receive_task: asyncio.Task[Any] | None = None
    sender_task: asyncio.Task[None] | None = None
    subscription: TurnSubscription | None = None
    provider_catalog: ModelCatalog | None = None
    provider_identity: tuple[str, str, str] | None = None
    journal: UiEventJournal
    registry: ActiveTurnRegistry

    try:
        hello = await websocket.receive_json()
        session_id = _text(hello.get("session_id")) if isinstance(hello, dict) else None
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await _error(websocket, "invalid_message", "首帧必须是 hello")
            await websocket.close(code=4400)
            return
        if session_id is None or len(session_id) > 256:
            await _error(websocket, "invalid_session_id", "session_id 无效")
            await websocket.close(code=4400)
            return

        service: SessionService = websocket.app.state.session_service
        journal = getattr(websocket.app.state, "ui_event_journal", None)
        if journal is None:
            # 独立 router 单元测试会直接挂载 Web API 而不走 create_app lifespan.
            # 仍把 journal 提升到 app state, 避免退回每 WebSocket 一个 writer.
            journal = UiEventJournal(service.repository)
            websocket.app.state.ui_event_journal = journal
            await journal.start()
        registry = getattr(websocket.app.state, "active_turns", None)
        if registry is None:
            registry = ActiveTurnRegistry(journal)
            websocket.app.state.active_turns = registry
        # Subscription ownership is deliberately established before the
        # history snapshot. Events accepted during the SQLite read are buffered
        # here and de-duplicated at the replay/live hand-off.
        subscription = registry.subscribe(session_id)
        if not await _synchronize_subscription(
            websocket, send_lock, journal, registry, subscription
        ):
            return
        sender_task = asyncio.create_task(
            _forward_subscription(websocket, send_lock, subscription),
            name=f"websocket-events-{session_id}",
        )
        while True:
            receive_task = asyncio.create_task(websocket.receive_json())
            done, _pending = await asyncio.wait(
                {receive_task, sender_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if sender_task in done:
                if not receive_task.done():
                    receive_task.cancel()
                    try:
                        await receive_task
                    except asyncio.CancelledError:
                        pass
                receive_task = None
                await sender_task
                return
            try:
                payload: object = receive_task.result()
            except ValueError:
                await _error(
                    websocket,
                    "invalid_json",
                    "WebSocket 消息不是有效 JSON",
                    recoverable=True,
                    send_lock=send_lock,
                )
                receive_task = None
                continue
            receive_task = None
            if not isinstance(payload, dict):
                await _error(
                    websocket,
                    "invalid_message",
                    "消息必须是 JSON 对象",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            if payload.get("type") == "ping":
                await _send_json(websocket, {"type": "pong"}, send_lock)
                continue
            if payload.get("type") == "stop":
                requested_turn = _text(payload.get("turn_id"))
                if requested_turn is None:
                    await _error(
                        websocket,
                        "turn_not_running",
                        "指定任务未在运行",
                        recoverable=True,
                        send_lock=send_lock,
                    )
                    continue
                try:
                    await registry.stop_turn(session_id, requested_turn)
                except ActiveTurnNotRunningError:
                    await _error(
                        websocket,
                        "turn_not_running",
                        "指定任务未在运行",
                        recoverable=True,
                        send_lock=send_lock,
                    )
                continue
            if payload.get("type") != "message":
                await _error(
                    websocket,
                    "invalid_message",
                    "不支持的 WebSocket 消息类型",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            content = _text(payload.get("content"))
            if content is None:
                await _error(
                    websocket,
                    "invalid_message",
                    "消息内容不能为空",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            provider_payload = payload.get("provider")
            try:
                provider = ProviderModelsRequest.model_validate(provider_payload)
            except ValidationError:
                await _error(
                    websocket,
                    "invalid_provider",
                    "Provider 配置无效",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            model = _text(payload.get("model"))
            if model is None:
                await _error(
                    websocket,
                    "invalid_model",
                    "必须选择模型",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            try:
                effort = validate_effort(payload.get("effort"))
            except ValueError as exc:
                await _error(
                    websocket,
                    "invalid_effort",
                    str(exc),
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            identity = (provider.base_url.rstrip("/"), provider.api_key, provider.auth_env)
            if identity != provider_identity:
                if provider_catalog is not None:
                    await provider_catalog.aclose()
                provider_catalog = ModelCatalog(
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                    auth_env=provider.auth_env,
                )
                provider_identity = identity
            try:
                available_models = await provider_catalog.discover()
            except ModelCatalogUnavailableError as exc:
                await _error(
                    websocket,
                    "provider_unavailable",
                    _provider_catalog_failure(provider_catalog, provider.auth_env, exc),
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            if model not in available_models:
                await _error(
                    websocket,
                    "invalid_model",
                    f"不支持的模型：{model}",
                    recoverable=True,
                    send_lock=send_lock,
                )
                continue
            system_prompt = _text(payload.get("system_prompt"))
            try:
                request = SessionTurnRequest(
                    message=content,
                    model=model,
                    system_prompt=system_prompt,
                )
                provider_config = ProviderConfig(
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    auth_env=provider.auth_env,
                )
                await registry.start_turn(
                    session_id=session_id,
                    turn_id=str(uuid4()),
                    content=content,
                    stream_factory=lambda request=request, provider_config=provider_config, effort=effort: (
                        service.stream_events(
                            request,
                            session_id,
                            provider=provider_config,
                            effort=effort,
                        )
                    ),
                    producer=lambda turn: _produce_turn(turn, registry),
                )
            except ValidationError as exc:
                await _error(
                    websocket,
                    "invalid_message",
                    str(exc),
                    recoverable=True,
                    send_lock=send_lock,
                )
            except ActiveTurnBusyError:
                await _error(
                    websocket,
                    "session_busy",
                    "当前任务仍在运行",
                    recoverable=True,
                    send_lock=send_lock,
                )
            except SessionServiceError as exc:
                await _error(
                    websocket,
                    exc.code,
                    str(exc),
                    recoverable=True,
                    send_lock=send_lock,
                )
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except ValueError:
        await _error(
            websocket,
            "invalid_json",
            "WebSocket 消息不是有效 JSON",
            send_lock=send_lock,
        )
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
        if sender_task is not None and not sender_task.done():
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
        if subscription is not None:
            registry.unsubscribe(subscription)
        if provider_catalog is not None:
            await provider_catalog.aclose()
