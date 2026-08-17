from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.identity import require_user
from app.sessions.repository import UserRecord, normalize_user_name

router = APIRouter(prefix="/v1/users", tags=["users"])


class VerifyUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def user_view(user: UserRecord) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "name": user.name,
        "enabled": user.enabled,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }


@router.post("/verify")
async def verify_user(payload: VerifyUserRequest, request: Request) -> dict[str, object]:
    try:
        _, normalized = normalize_user_name(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = await request.app.state.repository.find_user_by_name(normalized)
    if user is None:
        raise HTTPException(status_code=404, detail="后台中没有这个用户")
    if not user.enabled:
        raise HTTPException(status_code=403, detail="用户已停用")
    return user_view(user)


@router.get("/me")
async def current_user(request: Request) -> dict[str, object]:
    return user_view(await require_user(request))
