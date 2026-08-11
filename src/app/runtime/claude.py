"""Bounded Claude Code CLI implementation of the runtime contract.

The CLI is deliberately run with a fresh per-sandbox ``CLAUDE_CONFIG_DIR`` and
an explicitly selected Anthropic credential variable.  In particular, it does
not inherit a developer's Claude Code login/configuration from the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from .base import ProviderConfig, RuntimeContext
from .events import Completed, Failed, RuntimeEvent, TextDelta, Usage

CliLine: TypeAlias = str | bytes


class ClaudeCliExecutor(Protocol):
    """Runs a command in the session sandbox and yields stdout lines."""

    def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> AsyncIterator[CliLine]: ...


class ClaudeCodeRuntime:
    """Claude Code CLI runtime using ``stream-json`` and CLI resume IDs.

    ``executor`` is injectable so a container/sandbox implementation can own
    process execution.  The default executor uses the local subprocess only as
    a development fallback; callers deploying a real sandbox should inject its
    executor instead.
    """

    _allowed_auth_envs = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})
    _openai_compat_model = "claude-code-agent"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        auth_env: str = "ANTHROPIC_API_KEY",
        claude_command: str = "claude",
        executor: ClaudeCliExecutor | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Claude API credential is required")
        if auth_env not in self._allowed_auth_envs:
            allowed = ", ".join(sorted(self._allowed_auth_envs))
            raise ValueError(f"auth_env must be one of: {allowed}")
        if not claude_command:
            raise ValueError("claude_command must not be empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.model = model
        self.auth_env = auth_env
        self.claude_command = claude_command
        self.executor = executor or self._subprocess_executor
        self._states: dict[str, str] = {}

    @staticmethod
    def _config_dir(context: RuntimeContext) -> Path:
        return context.workspace / ".claude-cli-config"

    def _environment(self, context: RuntimeContext) -> dict[str, str]:
        # Start from a narrow operational allowlist. In particular, do not leak
        # host Claude/Codex feature flags, login state, or unrelated secrets into
        # the worker. Proxy and certificate settings are kept for local gateways.
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
        provider = context.provider or ProviderConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            auth_env=self.auth_env,
        )
        if not provider.api_key:
            raise RuntimeError("A provider API credential is required")
        env[provider.auth_env] = provider.api_key
        env["CLAUDE_CONFIG_DIR"] = str(self._config_dir(context))
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        if provider.base_url:
            env["ANTHROPIC_BASE_URL"] = provider.base_url
        return env

    def _command(
        self,
        *,
        message: str,
        context: RuntimeContext,
        runtime_session_id: str,
        resume: bool,
    ) -> list[str]:
        command = [
            self.claude_command,
            "--bare",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        model = self._model_for(context)
        if model:
            command.extend(["--model", model])
        if context.system_prompt:
            command.extend(["--system-prompt", context.system_prompt])
        if resume:
            command.extend(["--resume", runtime_session_id])
        else:
            # Supplying the id makes the persisted ID a real Claude CLI session
            # identifier, rather than an application-only alias.
            command.extend(["--session-id", runtime_session_id])
        command.append(message)
        return command

    def _model_for(self, context: RuntimeContext) -> str | None:
        """Keep the stable OpenAI-facing model alias out of provider CLI args."""
        if context.model and context.model != self._openai_compat_model:
            return context.model
        return self.model

    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        self._config_dir(context).mkdir(parents=True, exist_ok=True)
        runtime_session_id = str(uuid.uuid4())
        self._states[runtime_session_id] = "new"
        return runtime_session_id

    async def send_message(
        self,
        runtime_session_id: str,
        message: str,
        context: RuntimeContext,
    ) -> AsyncIterator[RuntimeEvent]:
        # An ID restored from the session repository is already a real CLI
        # session, so process restart must resume it rather than try to create
        # a second session with the same ID.
        state = self._states.get(runtime_session_id, "resumable")
        if state == "paused":
            yield Failed("runtime_paused", "Runtime is paused; resume it first", True)
            return
        if state == "closed":
            yield Failed("runtime_closed", "Runtime session is closed")
            return

        # ``create_session`` reserves a CLI session ID.  Its first turn creates
        # that ID; every later (or restored) turn resumes it.
        resume = state != "new"
        completed = False
        visible_text = ""
        try:
            async for raw_line in self.executor(
                self._command(
                    message=message,
                    context=context,
                    runtime_session_id=runtime_session_id,
                    resume=resume,
                ),
                cwd=context.workspace,
                env=self._environment(context),
            ):
                result_text = self._successful_result_text(raw_line)
                event = self._event_from_line(raw_line)
                if event is None:
                    continue
                if isinstance(event, tuple):
                    if result_text and result_text not in visible_text:
                        visible_text += result_text
                        yield TextDelta(result_text)
                    elif not visible_text and any(isinstance(item, Completed) for item in event):
                        notice = "任务已完成，但运行时未返回可见文本。"
                        visible_text = notice
                        yield TextDelta(notice)
                    for item in event:
                        yield item
                        completed = completed or isinstance(item, Completed)
                        if isinstance(item, Failed):
                            return
                else:
                    if isinstance(event, TextDelta):
                        visible_text += event.text
                    yield event
                    completed = completed or isinstance(event, Completed)
                    if isinstance(event, Failed):
                        return
        except (OSError, RuntimeError):
            yield Failed("claude_cli_failed", "Claude Code CLI execution failed", True)
            return
        finally:
            if self._states.get(runtime_session_id) != "closed":
                self._states[runtime_session_id] = "resumable"
        if not completed:
            yield Completed()

    @staticmethod
    def _successful_result_text(raw_line: CliLine) -> str | None:
        line = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return None
        if payload.get("is_error") is True or payload.get("subtype") in {"error", "failure"}:
            return None
        result = payload.get("result")
        return result if isinstance(result, str) and result else None

    @staticmethod
    def _event_from_line(raw_line: CliLine) -> RuntimeEvent | tuple[RuntimeEvent, ...] | None:
        line = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        if not line.strip():
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return Failed("claude_cli_invalid_event", "Claude Code returned invalid stream JSON")
        if not isinstance(payload, dict):
            return Failed("claude_cli_invalid_event", "Claude Code returned invalid stream JSON")

        event_type = payload.get("type")
        if event_type == "stream_event" and isinstance(payload.get("event"), dict):
            payload = payload["event"]
            event_type = payload.get("type")
        if event_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                return TextDelta(text) if isinstance(text, str) and text else None
            return None  # thinking/tool deltas are deliberately not exposed.
        if event_type == "assistant":
            return ClaudeCodeRuntime._assistant_text(payload.get("message", payload))
        if event_type == "result":
            usage = ClaudeCodeRuntime._usage(payload.get("usage"))
            subtype = payload.get("subtype")
            if subtype in {"error", "failure"} or payload.get("is_error") is True:
                return Failed("claude_cli_result_error", "Claude Code reported a failed result")
            return tuple(item for item in (usage, Completed()) if item is not None)
        if event_type == "error":
            return Failed("claude_cli_error", "Claude Code reported an error", True)
        return None

    @staticmethod
    def _assistant_text(message: Any) -> RuntimeEvent | None:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return TextDelta(content) if content else None
        if not isinstance(content, list):
            return None
        text = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        return TextDelta(text) if text else None

    @staticmethod
    def _usage(value: Any) -> Usage | None:
        if not isinstance(value, dict):
            return None
        input_tokens = value.get("input_tokens")
        output_tokens = value.get("output_tokens")
        if not isinstance(input_tokens, int) and not isinstance(output_tokens, int):
            return None
        return Usage(
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        )

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        if self._states.get(runtime_session_id) != "closed":
            self._states[runtime_session_id] = "paused"

    async def restore_session_state(self, runtime_session_id: str, *, started: bool) -> None:
        state = self._states.get(runtime_session_id)
        if state == "closed":
            return
        if not started:
            self._states[runtime_session_id] = "new"
        elif state is None:
            self._states[runtime_session_id] = "resumable"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        state = self._states.get(runtime_session_id)
        if state == "closed":
            raise RuntimeError("Cannot resume a closed runtime")
        # SessionService calls resume as an idempotent reconciliation step even
        # immediately after create_session. Preserve "new" so the first CLI turn
        # uses --session-id; unknown/restored and paused IDs use --resume.
        if state != "new":
            self._states[runtime_session_id] = "resumable"

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del context
        self._states[runtime_session_id] = "closed"

    async def _subprocess_executor(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> AsyncIterator[CliLine]:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        while line := await process.stdout.readline():
            yield line
        stderr = await process.stderr.read() if process.stderr is not None else b""
        if await process.wait() != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace"))
