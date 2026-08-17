"""Claude Agent SDK runtime executed exclusively through a sandbox runner."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from .base import RuntimeContext
from .events import Completed, Diagnostic, Failed, Progress, RuntimeEvent, TextDelta, Usage

RunnerLine: TypeAlias = str | bytes


class AgentSdkExecutor(Protocol):
    """Execute the SDK runner inside the session sandbox."""

    def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> AsyncIterator[RunnerLine]: ...


class AgentSDKRuntime:
    """Map stable runner NDJSON to runtime events without importing the SDK on the host."""

    def __init__(
        self,
        *,
        runner_command: str = "/usr/local/bin/oca-agent-sdk-runner",
        executor: AgentSdkExecutor,
    ) -> None:
        if not runner_command:
            raise ValueError("runner_command must not be empty")
        self.runner_command = runner_command
        self.executor = executor
        self._states: dict[str, str] = {}

    @staticmethod
    def _config_dir(context: RuntimeContext) -> Path:
        return context.workspace / ".claude-agent-sdk"

    def _environment(self, context: RuntimeContext) -> dict[str, str]:
        inherited = (
            "PATH",
            "LANG",
            "LC_ALL",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        )
        env = {name: os.environ[name] for name in inherited if os.environ.get(name)}
        provider = context.provider
        if provider is None:
            raise RuntimeError("A provider API credential is required")
        if not provider.api_key:
            raise RuntimeError("A provider API credential is required")
        env[provider.auth_env] = provider.api_key
        env["CLAUDE_CONFIG_DIR"] = str(self._config_dir(context))
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        if provider.base_url:
            env["ANTHROPIC_BASE_URL"] = provider.base_url
        return env

    def _command(
        self, *, message: str, context: RuntimeContext, runtime_session_id: str, resume: bool
    ) -> list[str]:
        command = [self.runner_command, "--prompt", message]
        if resume:
            command.extend(["--resume", runtime_session_id])
        else:
            command.extend(["--session-id", runtime_session_id])
        if context.model:
            command.extend(["--model", context.model])
        if context.effort:
            command.extend(["--effort", context.effort])
        if context.system_prompt:
            command.extend(["--system-prompt", context.system_prompt])
        return command

    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        self._config_dir(context).mkdir(parents=True, exist_ok=True)
        runtime_session_id = str(uuid.uuid4())
        self._states[runtime_session_id] = "new"
        return runtime_session_id

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        state = self._states.get(runtime_session_id, "resumable")
        if state == "paused":
            yield Failed("runtime_paused", "Runtime is paused; resume it first", True)
            return
        if state == "closed":
            yield Failed("runtime_closed", "Runtime session is closed")
            return
        if not context.provider or not context.provider.api_key:
            yield Failed("provider_credentials_missing", "Provider API credential is required")
            return
        completed = False
        lines = self.executor(
            self._command(
                message=message,
                context=context,
                runtime_session_id=runtime_session_id,
                resume=state != "new",
            ),
            cwd=context.workspace,
            env=self._environment(context),
        )
        try:
            async for line in lines:
                event = self._event_from_line(line)
                if event is None:
                    continue
                yield event
                completed = completed or isinstance(event, Completed)
                if isinstance(event, Failed):
                    return
        except (OSError, RuntimeError):
            yield Failed("agent_sdk_failed", "Claude Agent SDK execution failed", True)
            return
        finally:
            await lines.aclose()
            if self._states.get(runtime_session_id) != "closed":
                self._states[runtime_session_id] = "resumable"
        if not completed:
            yield Completed()

    @staticmethod
    def _event_from_line(raw_line: RunnerLine) -> RuntimeEvent | None:
        line = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        try:
            payload: Any = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return Failed("agent_sdk_invalid_event", "Agent SDK runner returned invalid JSON")
        if not isinstance(payload, dict):
            return Failed("agent_sdk_invalid_event", "Agent SDK runner returned invalid JSON")
        kind = payload.get("type")
        if kind == "text":
            text = payload.get("text")
            return TextDelta(text) if isinstance(text, str) and text else None
        if kind == "usage":
            return Usage(
                input_tokens=payload.get("input_tokens")
                if isinstance(payload.get("input_tokens"), int)
                else None,
                output_tokens=payload.get("output_tokens")
                if isinstance(payload.get("output_tokens"), int)
                else None,
            )
        if kind == "progress":
            phase, message, status = (
                payload.get("phase"),
                payload.get("message"),
                payload.get("status"),
            )
            if not all(isinstance(value, str) for value in (phase, message, status)):
                return None
            return Progress(
                phase=phase,
                message=message,
                status=status,
                tool_name=payload.get("tool_name")
                if isinstance(payload.get("tool_name"), str)
                else None,
                tool_use_id=payload.get("tool_use_id")
                if isinstance(payload.get("tool_use_id"), str)
                else None,
                parent_tool_use_id=(
                    payload.get("parent_tool_use_id")
                    if isinstance(payload.get("parent_tool_use_id"), str)
                    else None
                ),
                task_id=(
                    payload.get("task_id") if isinstance(payload.get("task_id"), str) else None
                ),
                elapsed_seconds=(
                    payload.get("elapsed_seconds")
                    if isinstance(payload.get("elapsed_seconds"), (int, float))
                    else None
                ),
                duration_seconds=(
                    payload.get("duration_seconds")
                    if isinstance(payload.get("duration_seconds"), (int, float))
                    else None
                ),
                current=payload.get("current") if isinstance(payload.get("current"), int) else None,
                total=payload.get("total") if isinstance(payload.get("total"), int) else None,
            )
        if kind == "diagnostic":
            message_type = payload.get("message_type")
            if not isinstance(message_type, str):
                return None
            tool_input = payload.get("tool_input")
            tool_result = payload.get("tool_result")
            return Diagnostic(
                message_type=message_type,
                subtype=payload.get("subtype") if isinstance(payload.get("subtype"), str) else None,
                message_id=(
                    payload.get("message_id")
                    if isinstance(payload.get("message_id"), str)
                    else None
                ),
                task_id=payload.get("task_id") if isinstance(payload.get("task_id"), str) else None,
                usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
                duration_ms=payload.get("duration_ms")
                if isinstance(payload.get("duration_ms"), int)
                else None,
                duration_api_ms=(
                    payload.get("duration_api_ms")
                    if isinstance(payload.get("duration_api_ms"), int)
                    else None
                ),
                tool_name=payload.get("tool_name")
                if isinstance(payload.get("tool_name"), str)
                else None,
                tool_use_id=(
                    payload.get("tool_use_id")
                    if isinstance(payload.get("tool_use_id"), str)
                    else None
                ),
                parent_tool_use_id=(
                    payload.get("parent_tool_use_id")
                    if isinstance(payload.get("parent_tool_use_id"), str)
                    else None
                ),
                tool_input=tool_input if isinstance(tool_input, dict) else None,
                tool_result=(tool_result if isinstance(tool_result, (str, dict, list)) else None),
                is_error=(
                    payload.get("is_error") if isinstance(payload.get("is_error"), bool) else None
                ),
                result=payload.get("result") if isinstance(payload.get("result"), str) else None,
                visible_text=(
                    payload.get("visible_text")
                    if isinstance(payload.get("visible_text"), str)
                    else None
                ),
                thinking_length=(
                    payload.get("thinking_length")
                    if isinstance(payload.get("thinking_length"), int)
                    else None
                ),
            )
        if kind == "completed":
            return Completed(
                payload.get("stop_reason")
                if isinstance(payload.get("stop_reason"), str)
                else "stop"
            )
        if kind == "failed":
            return Failed(
                payload.get("code") if isinstance(payload.get("code"), str) else "agent_sdk_error",
                "Claude Agent SDK reported a failed result",
                bool(payload.get("retryable")),
            )
        return None

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        if self._states.get(runtime_session_id) != "closed":
            self._states[runtime_session_id] = "paused"

    async def restore_session_state(self, runtime_session_id: str, *, started: bool) -> None:
        if self._states.get(runtime_session_id) == "closed":
            return
        self._states[runtime_session_id] = "resumable" if started else "new"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        if self._states.get(runtime_session_id) == "closed":
            raise RuntimeError("Cannot resume a closed runtime")
        if self._states.get(runtime_session_id) != "new":
            self._states[runtime_session_id] = "resumable"

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        self._states[runtime_session_id] = "closed"
