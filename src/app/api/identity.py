from __future__ import annotations

from fastapi import HTTPException, Request

from app.sessions.repository import UserRecord

USER_HEADER = "x-webagent-user-id"


async def optional_user(request: Request) -> UserRecord | None:
    user_id = request.headers.get(USER_HEADER, "").strip()
    if not user_id:
        return None
    user = await request.app.state.repository.get_user(user_id)
    if user is None or not user.enabled:
        raise HTTPException(status_code=403, detail="用户不存在或已停用")
    return user


async def require_user(request: Request) -> UserRecord:
    user = await optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先输入用户名称")
    return user


async def require_owned_session(request: Request, session_id: str) -> UserRecord | None:
    user = await optional_user(request)
    if user is None:
        return None
    record = await request.app.state.repository.get(session_id)
    if record is None or record.owner_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Session 不存在")
    return user
