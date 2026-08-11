"""Small, deliberately scoped OpenAI Chat Completions schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OpenAIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChatMessage(OpenAIModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(OpenAIModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    session_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_semantics(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        unsupported = {
            name
            for name in (
                "tools",
                "tool_choice",
                "temperature",
                "top_p",
                "logprobs",
                "seed",
                "response_format",
                "parallel_tool_calls",
            )
            if value.get(name) is not None
        }
        if value.get("n", 1) != 1:
            unsupported.add("n")
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ValueError(f"Unsupported Chat Completions fields: {names}")
        return value

    @model_validator(mode="after")
    def require_user_message(self) -> ChatCompletionRequest:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("messages must contain at least one user message")
        return self


class Usage(OpenAIModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionChoice(OpenAIModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(OpenAIModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)
    session_id: str | None = None


class ChatCompletionDelta(OpenAIModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class ChatCompletionChunkChoice(OpenAIModel):
    index: int = 0
    delta: ChatCompletionDelta
    finish_reason: str | None = None


class ChatCompletionChunk(OpenAIModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]


class ModelCard(OpenAIModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str = "local"


class ModelListResponse(OpenAIModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class OpenAIErrorDetail(OpenAIModel):
    message: str
    type: str = "invalid_request_error"
    param: str | None = None
    code: str | None = None


class OpenAIErrorResponse(OpenAIModel):
    error: OpenAIErrorDetail


JSONValue = dict[str, Any]
