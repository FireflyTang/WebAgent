from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .events import Effort, RuntimeEvent, validate_effort


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Per-turn provider connection values supplied by a web client."""

    base_url: str | None = None
    api_key: str | None = None
    auth_env: str = "ANTHROPIC_API_KEY"

    def __post_init__(self) -> None:
        if self.auth_env not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}:
            raise ValueError("auth_env must be ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/") if self.base_url else None)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    session_id: str
    sandbox_id: str
    workspace: Path
    system_prompt: str | None = None
    model: str | None = None
    provider: ProviderConfig | None = None
    effort: Effort | None = None

    def __post_init__(self) -> None:
        validate_effort(self.effort)


class AgentRuntime(Protocol):
    async def create_session(self, context: RuntimeContext) -> str: ...

    def send_message(
        self,
        runtime_session_id: str,
        message: str,
        context: RuntimeContext,
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None: ...

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None: ...

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None: ...
