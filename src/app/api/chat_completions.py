"""Chat Completions transport adapter with a pluggable session/runtime handler."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterable, Iterable
from inspect import isawaitable

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import StreamingResponse

from app.api.dependencies import ChatCompletionHandler, get_chat_completion_handler, require_api_key
from app.openai_compat.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)
from app.openai_compat.sse import iter_openai_sse, new_completion_id

router = APIRouter(prefix="/v1", tags=["openai"])


def resolve_session_id(body_session_id: str | None, header_session_id: str | None) -> str:
    if body_session_id and header_session_id and body_session_id != header_session_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="X-Session-ID and session_id must match")
    return header_session_id or body_session_id or str(uuid.uuid4())


@router.post("/chat/completions", dependencies=[Depends(require_api_key)])
async def create_chat_completion(
    request: ChatCompletionRequest,
    response: Response,
    x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
    handler: ChatCompletionHandler = Depends(get_chat_completion_handler),
):
    if request.model != "claude-code-agent":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown model: {request.model}")
    session_id = resolve_session_id(request.session_id, x_session_id)
    response.headers["X-Session-ID"] = session_id

    if request.stream:
        deltas = handler.stream(request, session_id)
        if isawaitable(deltas):
            deltas = await deltas
        if not isinstance(deltas, (AsyncIterable, Iterable)):
            raise TypeError(
                "chat completion handler stream() must return an iterable of text deltas"
            )
        stream_response = StreamingResponse(
            iter_openai_sse(deltas, model=request.model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Session-ID": session_id},
        )
        return stream_response

    content = await handler.complete(request, session_id)
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=request.model,
        choices=[ChatCompletionChoice(message=ChatMessage(role="assistant", content=content))],
        usage=Usage(),
        session_id=session_id,
    )
