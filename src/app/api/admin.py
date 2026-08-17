from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.sessions.repository import (
    SessionAlreadyExistsError,
    SessionRepositoryError,
    SessionVersionConflictError,
)

from .sessions import _view
from .users import user_view

router = APIRouter(tags=["admin"])
web_root = Path(__file__).resolve().parent.parent / "web"

MANAGED_KEYS = (
    "session_pause_after_seconds",
    "session_delete_after_seconds",
    "session_reaper_interval_seconds",
    "session_delete_workspace",
    "claude_timeout_seconds",
    "docker_cpus",
    "docker_memory",
    "docker_pids_limit",
    "file_editor_max_bytes",
    "file_upload_max_bytes",
    "file_upload_max_files_per_session",
)


class UserCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class UserPatchRequest(BaseModel):
    enabled: bool


class ManagedSettingsPatch(BaseModel):
    version: int = Field(ge=0)
    session_pause_after_seconds: int | None = Field(default=None, ge=1)
    session_delete_after_seconds: int | None = Field(default=None, ge=1)
    session_reaper_interval_seconds: int | None = Field(default=None, ge=1)
    session_delete_workspace: bool | None = None
    claude_timeout_seconds: int | None = Field(default=None, ge=30)
    docker_cpus: float | None = Field(default=None, gt=0)
    docker_memory: str | None = Field(default=None, min_length=2, max_length=32)
    docker_pids_limit: int | None = Field(default=None, ge=16)
    file_editor_max_bytes: int | None = Field(default=None, gt=0)
    file_upload_max_bytes: int | None = Field(default=None, gt=0)
    file_upload_max_files_per_session: int | None = Field(default=None, gt=0)


@router.get("/admin", include_in_schema=False)
async def admin_index() -> FileResponse:
    return FileResponse(web_root / "admin.html")


@router.get("/v1/admin/overview")
async def overview(request: Request) -> dict[str, object]:
    records = await request.app.state.repository.list_sessions()
    users = await request.app.state.repository.list_users()
    states: dict[str, int] = {}
    running = 0
    for record in records:
        state, _ = request.app.state.session_service.lifecycle_view(record)
        states[state] = states.get(state, 0) + 1
        if request.app.state.active_turns.snapshot(record.session_id)["task_state"] != "idle":
            running += 1
    return {
        "server_now": datetime.now(UTC).isoformat(),
        "users": {"total": len(users), "enabled": sum(user.enabled for user in users)},
        "sessions": {"total": len(records), "states": states},
        "running_tasks": running,
        "runtime": request.app.state.settings.runtime_backend,
        "sandbox": request.app.state.settings.sandbox_backend,
    }


@router.get("/v1/admin/monitor")
async def monitor(request: Request) -> dict[str, object]:
    """Return the latest in-memory operational sample without live external probes."""

    return request.app.state.system_monitor.report()


@router.get("/v1/admin/users")
async def list_users(request: Request) -> dict[str, object]:
    return {"users": [user_view(user) for user in await request.app.state.repository.list_users()]}


@router.post("/v1/admin/users", status_code=201)
async def create_user(payload: UserCreateRequest, request: Request) -> dict[str, object]:
    try:
        return user_view(await request.app.state.repository.create_user(payload.name))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="用户名称已存在") from exc


@router.patch("/v1/admin/users/{user_id}")
async def patch_user(
    user_id: str, payload: UserPatchRequest, request: Request
) -> dict[str, object]:
    try:
        return user_view(
            await request.app.state.repository.set_user_enabled(user_id, payload.enabled)
        )
    except SessionRepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _settings_view(request: Request, record) -> dict[str, object]:
    current = request.app.state.settings
    saved = {key: getattr(current, key) for key in MANAGED_KEYS}
    saved.update(record.values)
    active = {key: getattr(current, key) for key in MANAGED_KEYS}
    return {
        "active": active,
        "saved": saved,
        "version": record.version,
        "updated_at": record.updated_at.isoformat(),
        "restart_required": saved != active,
    }


@router.get("/v1/admin/settings")
async def get_settings(request: Request) -> dict[str, object]:
    record = await request.app.state.repository.get_managed_settings()
    return _settings_view(request, record)


@router.patch("/v1/admin/settings")
async def patch_settings(payload: ManagedSettingsPatch, request: Request) -> dict[str, object]:
    current = await request.app.state.repository.get_managed_settings()
    values = dict(current.values)
    for key in MANAGED_KEYS:
        value = getattr(payload, key)
        if value is not None:
            values[key] = value
    try:
        candidate = Settings(**{**request.app.state.settings.model_dump(), **values})
        if candidate.session_delete_after_seconds <= candidate.session_pause_after_seconds:
            raise ValueError("沙箱删除时间必须大于空闲暂停时间")
        updated = await request.app.state.repository.update_managed_settings(
            values, expected_version=payload.version
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SessionVersionConflictError as exc:
        raise HTTPException(status_code=409, detail="配置已被其他操作更新，请刷新") from exc
    return _settings_view(request, updated)


@router.get("/v1/admin/sessions")
async def admin_sessions(request: Request) -> dict[str, object]:
    service = request.app.state.session_service
    users = {user.user_id: user.name for user in await request.app.state.repository.list_users()}
    return {
        "sessions": [
            {
                **_view(record, service, request.app.state.active_turns),
                "owner_name": users.get(record.owner_user_id),
            }
            for record in await request.app.state.repository.list_sessions()
        ]
    }
