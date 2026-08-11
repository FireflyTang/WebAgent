from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class SandboxInfo:
    sandbox_id: str
    workspace: Path
    state: Literal["active", "paused", "deleted"]
    # Defaults preserve the pre-ownership contract for third-party managers:
    # a three-argument result from create() is treated as newly allocated.
    created: bool = True
    workspace_created: bool = True


class SandboxManager(Protocol):
    async def create(self, session_id: str) -> SandboxInfo: ...
    async def inspect(self, sandbox_id: str) -> SandboxInfo | None: ...
    async def pause(self, sandbox_id: str) -> None: ...
    async def resume(self, sandbox_id: str) -> None: ...
    async def delete(self, sandbox_id: str, delete_workspace: bool = True) -> None: ...
