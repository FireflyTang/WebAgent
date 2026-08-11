"""OpenAI-compatible Server-Sent Event serialization."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable

from .schemas import ChatCompletionChunk, ChatCompletionChunkChoice, ChatCompletionDelta

logger = logging.getLogger(__name__)


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def encode_sse(data: str) -> bytes:
    """Encode one SSE data event, preserving the required blank-line terminator."""
    return f"data: {data}\n\n".encode()


def chunk_event(chunk: ChatCompletionChunk) -> bytes:
    return encode_sse(chunk.model_dump_json(exclude_none=True))


async def iter_openai_sse(
    deltas: AsyncIterable[str] | Iterable[str],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield role, content, finish and ``[DONE]`` events in OpenAI order."""
    completion_id = completion_id or new_completion_id()
    created = created if created is not None else int(time.time())
    try:
        yield chunk_event(
            ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=model,
                choices=[ChatCompletionChunkChoice(delta=ChatCompletionDelta(role="assistant"))],
            )
        )
        try:
            if hasattr(deltas, "__aiter__"):
                async for text in deltas:  # type: ignore[union-attr]
                    if text:
                        yield chunk_event(_content_chunk(completion_id, created, model, text))
            else:
                for text in deltas:  # type: ignore[union-attr]
                    if text:
                        yield chunk_event(_content_chunk(completion_id, created, model, text))
        except Exception:
            logger.exception("Runtime stream failed after the SSE response started")
            visible_error = "\nRuntime stream failed; retry or continue with the same session.\n"
            yield chunk_event(_content_chunk(completion_id, created, model, visible_error))
        yield chunk_event(
            ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=model,
                choices=[
                    ChatCompletionChunkChoice(delta=ChatCompletionDelta(), finish_reason="stop")
                ],
            )
        )
        yield encode_sse("[DONE]")
    finally:
        close = getattr(deltas, "aclose", None) or getattr(deltas, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def _content_chunk(completion_id: str, created: int, model: str, text: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChatCompletionChunkChoice(delta=ChatCompletionDelta(content=text))],
    )
