from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

Effort = Literal["low", "medium", "high", "xhigh", "max"]
VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def validate_effort(value: object) -> Effort | None:
    """Validate the shared, provider-supported effort contract without defaults."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in VALID_EFFORTS:
        allowed = ", ".join(sorted(VALID_EFFORTS))
        raise ValueError(f"effort 必须是以下值之一：{allowed}")
    return cast(Effort, value)


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    kind: Literal["choice", "permission"]
    prompt: str
    options: list[str]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    """Provider-neutral, user-safe work progress for a runtime turn."""

    phase: Literal["starting", "thinking", "tool", "task", "retry", "finalizing"]
    message: str
    status: Literal["started", "running", "completed", "failed"]
    tool_name: str | None = None
    tool_use_id: str | None = None
    parent_tool_use_id: str | None = None
    task_id: str | None = None
    elapsed_seconds: float | None = None
    duration_seconds: float | None = None
    current: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Structured provider diagnostics excluded from chat and WebSocket output, but kept in debug logs."""

    message_type: str
    subtype: str | None = None
    message_id: str | None = None
    task_id: str | None = None
    usage: dict[str, object] | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    parent_tool_use_id: str | None = None
    tool_input: dict[str, object] | None = None
    tool_result: str | dict[str, object] | list[dict[str, object]] | None = None
    is_error: bool | None = None
    result: str | None = None
    # Provider/runner supplied text which is safe to show to the user.  This
    # is deliberately distinct from thinking payloads: thinking is never
    # represented here and must remain unavailable to the debug transcript.
    visible_text: str | None = None
    thinking_length: int | None = None


@dataclass(frozen=True, slots=True)
class Completed:
    stop_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class Failed:
    code: str
    message: str
    retryable: bool = False


RuntimeEvent: TypeAlias = (
    TextDelta | InteractionRequest | Usage | Progress | Diagnostic | Completed | Failed
)
