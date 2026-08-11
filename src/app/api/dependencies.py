"""Shared API dependencies; session/runtime services are injected by the app."""

from __future__ import annotations

import secrets
from typing import Protocol

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.openai_compat.schemas import ChatCompletionRequest

bearer_scheme = HTTPBearer(auto_error=False)


def settings_from_request(request: Request) -> Settings:
    return getattr(request.app.state, "settings", get_settings())


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(settings_from_request),
) -> None:
    if credentials is None or not secrets.compare_digest(credentials.credentials, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "Invalid API key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


class ChatCompletionHandler(Protocol):
    async def complete(self, request: ChatCompletionRequest, session_id: str) -> str: ...

    def stream(self, request: ChatCompletionRequest, session_id: str): ...


def get_chat_completion_handler(request: Request) -> ChatCompletionHandler:
    handler = getattr(request.app.state, "chat_completion_handler", None)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": {
                    "message": "Chat runtime is not configured",
                    "type": "api_error",
                    "code": "runtime_unavailable",
                }
            },
        )
    return handler
