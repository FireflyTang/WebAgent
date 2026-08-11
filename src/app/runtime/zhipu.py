"""Optional Zhipu-backed implementation of the coding-agent runtime contract.

The Zhipu endpoint is OpenAI-compatible, but this runtime deliberately asks for
a small JSON action plan rather than exposing the provider response directly.
That lets the surrounding application retain workspace and test ownership.
"""

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from app.util.files import write_json_atomic

from .base import RuntimeContext
from .events import Completed, Failed, RuntimeEvent, TextDelta, Usage


class ZhipuRuntime:
    """Use a GLM model to propose files, then apply them in one workspace."""

    endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    state_filename = ".zhipu-runtime.json"
    openai_compat_model = "claude-code-agent"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "glm-4.5-air",
        endpoint: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Zhipu API key is required")
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint or self.endpoint
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(90.0))

    @classmethod
    def _state_path(cls, context: RuntimeContext) -> Path:
        return context.workspace / cls.state_filename

    def _load_state(self, context: RuntimeContext) -> dict[str, Any]:
        path = self._state_path(context)
        if not path.exists():
            return {"turn": 0, "status": "active"}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A state file is only an aid to lifecycle recovery; corrupt state
            # must not turn into arbitrary code execution or an opaque crash.
            return {"turn": 0, "status": "active"}
        return state if isinstance(state, dict) else {"turn": 0, "status": "active"}

    def _save_state(self, context: RuntimeContext, state: dict[str, Any]) -> None:
        context.workspace.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            self._state_path(context),
            {"turn": int(state.get("turn", 0)), "status": state.get("status", "active")},
        )

    async def create_session(self, context: RuntimeContext) -> str:
        state = self._load_state(context)
        state["status"] = "active"
        self._save_state(context, state)
        return f"zhipu-{uuid.uuid4()}"

    @staticmethod
    def _instructions(context: RuntimeContext) -> str:
        prompt = (
            "You are a coding agent working in a dedicated workspace. Return ONLY valid JSON "
            "with exactly these fields: assistant_message (string), files (array of objects with "
            "path and content strings), and run_tests (boolean). Paths must be relative workspace "
            "paths. Do not use Markdown fences. Make only files needed for the task."
        )
        if context.system_prompt:
            prompt += f"\nAdditional project instructions:\n{context.system_prompt}"
        return prompt

    @staticmethod
    def _extract_plan(payload: Any) -> tuple[str, list[dict[str, str]], bool]:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Zhipu response did not contain choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ValueError("Zhipu response content was not a string")
        try:
            plan = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("Zhipu response was not valid JSON") from exc
        if not isinstance(plan, dict) or set(plan) != {"assistant_message", "files", "run_tests"}:
            raise ValueError(
                "Zhipu response must have exactly assistant_message, files, and run_tests"
            )
        message, files, run_tests = plan["assistant_message"], plan["files"], plan["run_tests"]
        if (
            not isinstance(message, str)
            or not isinstance(files, list)
            or not isinstance(run_tests, bool)
        ):
            raise ValueError("Zhipu response fields have invalid types")
        validated: list[dict[str, str]] = []
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "content"}:
                raise ValueError("Each file must contain exactly path and content")
            if not isinstance(file["path"], str) or not isinstance(file["content"], str):
                raise ValueError("File path and content must be strings")
            validated.append({"path": file["path"], "content": file["content"]})
        return message, validated, run_tests

    @staticmethod
    def _workspace_path(workspace: Path, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if not relative_path or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"Unsafe workspace path: {relative_path!r}")
        root = workspace.resolve()
        target = (root / candidate).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Unsafe workspace path: {relative_path!r}")
        return target

    @staticmethod
    async def _run_tests(workspace: Path) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "unittest",
            "discover",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode(errors="replace")

    @staticmethod
    def _workspace_snapshot(workspace: Path) -> str:
        """Provide enough current state for a useful second turn."""
        parts: list[str] = []
        total = 0
        if not workspace.exists():
            return "(empty workspace)"
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or len(parts) >= 20:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = path.relative_to(workspace).as_posix()
            snippet = content[:6000]
            entry = f"--- {relative} ---\n{snippet}"
            if total + len(entry) > 20000:
                break
            parts.append(entry)
            total += len(entry)
        return "\n".join(parts) if parts else "(empty workspace)"

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") == "paused":
            yield Failed("runtime_paused", "Runtime is paused; resume it first", True)
            return
        if state.get("status") == "closed":
            yield Failed("runtime_closed", "Runtime session is closed")
            return

        state["turn"] = int(state.get("turn", 0)) + 1
        request = {
            "model": self._model_for(context),
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": self._instructions(context)},
                {
                    "role": "user",
                    "content": f"Current workspace:\n{self._workspace_snapshot(context.workspace)}\n\n"
                    f"User task:\n{message}",
                },
            ],
            "temperature": 0.1,
        }
        try:
            response = await self.client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            assistant_message, files, run_tests = self._extract_plan(response.json())
        except httpx.HTTPError as exc:
            self._save_state(context, state)
            yield Failed("zhipu_request_failed", str(exc), retryable=True)
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._save_state(context, state)
            yield Failed("zhipu_invalid_response", str(exc))
            return

        try:
            targets = [
                (self._workspace_path(context.workspace, item["path"]), item["content"])
                for item in files
            ]
            for target, content in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as exc:
            self._save_state(context, state)
            yield Failed("workspace_write_failed", str(exc))
            return

        if assistant_message:
            yield TextDelta(assistant_message.rstrip() + "\n")
        test_code = 0
        if run_tests:
            try:
                test_code, output = await self._run_tests(context.workspace)
            except OSError as exc:
                self._save_state(context, state)
                yield Failed("test_execution_failed", str(exc))
                return
            if output:
                yield TextDelta(output)
        usage_data = response.json().get("usage", {})
        yield Usage(usage_data.get("prompt_tokens"), usage_data.get("completion_tokens"))
        self._save_state(context, state)
        yield Completed("stop" if test_code == 0 else "error")

    def _model_for(self, context: RuntimeContext) -> str:
        """Use a real provider model when the stable OpenAI alias is supplied."""
        if context.model and context.model != self.openai_compat_model:
            return context.model
        return self.model

    async def aclose(self) -> None:
        await self.client.aclose()

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") != "closed":
            state["status"] = "paused"
            self._save_state(context, state)

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") == "closed":
            raise RuntimeError("Cannot resume a closed runtime")
        state["status"] = "active"
        self._save_state(context, state)

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        state["status"] = "closed"
        self._save_state(context, state)
