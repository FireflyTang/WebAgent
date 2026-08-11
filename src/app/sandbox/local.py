import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from app.util.files import write_json_atomic

from .base import SandboxInfo


class LocalSandboxManager:
    """Provider-neutral Demo sandbox backed by dedicated local directories."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.metadata_root = self.workspace_root.parent / "sandboxes"

    def _id(self, session_id: str) -> str:
        return "local-" + hashlib.sha256(session_id.encode()).hexdigest()[:16]

    def _metadata_path(self, sandbox_id: str) -> Path:
        return self.metadata_root / f"{sandbox_id}.json"

    def _read(self, sandbox_id: str) -> dict[str, Any] | None:
        path = self._metadata_path(sandbox_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            workspace = self.workspace_root / sandbox_id
            if not workspace.exists():
                return None
            recovered = {
                "sandbox_id": sandbox_id,
                "workspace": str(workspace),
                "state": "active",
            }
            self._write(recovered)
            return recovered

    def _write(self, data: dict[str, Any]) -> None:
        self.metadata_root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._metadata_path(data["sandbox_id"]), data)

    async def create(self, session_id: str) -> SandboxInfo:
        sandbox_id = self._id(session_id)
        existing = self._read(sandbox_id)
        if existing and existing["state"] != "deleted":
            return SandboxInfo(
                sandbox_id,
                Path(existing["workspace"]),
                existing["state"],
                created=False,
                workspace_created=False,
            )
        workspace = self.workspace_root / sandbox_id
        workspace_created = not workspace.exists()
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        data = {"sandbox_id": sandbox_id, "workspace": str(workspace), "state": "active"}
        try:
            self._write(data)
        except BaseException:
            if workspace_created:
                await asyncio.to_thread(shutil.rmtree, workspace, True)
            raise
        return SandboxInfo(
            sandbox_id,
            workspace,
            "active",
            created=True,
            workspace_created=workspace_created,
        )

    async def inspect(self, sandbox_id: str) -> SandboxInfo | None:
        data = self._read(sandbox_id)
        if data is None:
            return None
        return SandboxInfo(
            data["sandbox_id"],
            Path(data["workspace"]),
            data["state"],
            created=False,
            workspace_created=False,
        )

    async def pause(self, sandbox_id: str) -> None:
        data = self._read(sandbox_id)
        if data is None or data["state"] == "deleted":
            return
        data["state"] = "paused"
        self._write(data)

    async def resume(self, sandbox_id: str) -> None:
        data = self._read(sandbox_id)
        if data is None or data["state"] == "deleted":
            raise RuntimeError(f"Sandbox {sandbox_id} cannot be resumed")
        Path(data["workspace"]).mkdir(parents=True, exist_ok=True)
        data["state"] = "active"
        self._write(data)

    async def delete(self, sandbox_id: str, delete_workspace: bool = True) -> None:
        data = self._read(sandbox_id)
        if data is None:
            return
        if delete_workspace:
            workspace = Path(data["workspace"])
            if workspace.parent == self.workspace_root and workspace.name == sandbox_id:
                await asyncio.to_thread(shutil.rmtree, workspace, True)
        data["state"] = "deleted"
        self._write(data)
