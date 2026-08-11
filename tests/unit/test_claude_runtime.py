from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest

from app.runtime.base import RuntimeContext
from app.runtime.claude import ClaudeCodeRuntime
from app.runtime.events import Completed, Failed, TextDelta, Usage


class RecordingExecutor:
    def __init__(self, lines: list[str] | None = None, error: Exception | None = None) -> None:
        self.lines = lines or []
        self.error = error
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    async def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
    ) -> AsyncIterator[str]:
        self.calls.append((list(command), cwd, dict(env)))
        for line in self.lines:
            yield line
        if self.error:
            raise self.error


async def collect(
    runtime: ClaudeCodeRuntime, runtime_id: str, message: str, context: RuntimeContext
):
    return [event async for event in runtime.send_message(runtime_id, message, context)]


@pytest.mark.asyncio
async def test_claude_cli_starts_then_resumes_with_isolated_explicit_auth(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-subscription-token")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/claude")
    executor = RecordingExecutor(
        [json.dumps({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 5}})]
    )
    context = RuntimeContext("external", "sandbox", tmp_path, "be concise")
    runtime = ClaudeCodeRuntime(
        "test-key", base_url="https://gateway.example/", model="claude-test", executor=executor
    )
    runtime_id = await runtime.create_session(context)
    await runtime.resume(runtime_id, context)  # SessionService reconciliation path.

    first = await collect(runtime, runtime_id, "first turn", context)
    await collect(runtime, runtime_id, "second turn", context)

    assert any(isinstance(event, Usage) for event in first)
    assert any(isinstance(event, TextDelta) and "未返回可见文本" in event.text for event in first)
    assert isinstance(first[-1], Completed)
    first_command, first_cwd, first_env = executor.calls[0]
    second_command, _, second_env = executor.calls[1]
    assert first_command[:5] == ["claude", "--bare", "-p", "--output-format", "stream-json"]
    assert ["--session-id", runtime_id] == first_command[
        first_command.index("--session-id") : first_command.index("--session-id") + 2
    ]
    assert ["--resume", runtime_id] == second_command[
        second_command.index("--resume") : second_command.index("--resume") + 2
    ]
    assert "--model" in first_command and "claude-test" in first_command
    assert "--system-prompt" in first_command and "be concise" in first_command
    assert first_cwd == tmp_path
    assert first_env["ANTHROPIC_API_KEY"] == "test-key"
    assert first_env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert first_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-cli-config")
    assert first_env.get("ANTHROPIC_AUTH_TOKEN") is None
    assert second_env["ANTHROPIC_API_KEY"] == "test-key"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "host-subscription-token"


@pytest.mark.asyncio
async def test_claude_cli_maps_only_text_and_result_events(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        [
            json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "secret"},
                }
            ),
            json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello "}}
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "world"},
                            {"type": "thinking", "thinking": "secret"},
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "usage": {"input_tokens": 2, "output_tokens": 4}}),
        ]
    )
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", executor=executor)
    runtime_id = await runtime.create_session(context)
    events = await collect(runtime, runtime_id, "hi", context)

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "hello world"
    assert any(isinstance(event, Usage) and event.output_tokens == 4 for event in events)
    assert isinstance(events[-1], Completed)


@pytest.mark.asyncio
async def test_claude_cli_context_model_overrides_runtime_default(tmp_path: Path) -> None:
    executor = RecordingExecutor([json.dumps({"type": "result", "result": "done"})])
    context = RuntimeContext("external", "sandbox", tmp_path, model="glm-5.2")
    runtime = ClaudeCodeRuntime("key", model="glm-4.7", executor=executor)
    runtime_id = await runtime.create_session(context)

    await collect(runtime, runtime_id, "hi", context)

    command = executor.calls[0][0]
    assert command[command.index("--model") + 1] == "glm-5.2"


@pytest.mark.asyncio
async def test_claude_cli_openai_model_alias_keeps_runtime_default(tmp_path: Path) -> None:
    executor = RecordingExecutor([json.dumps({"type": "result", "result": "done"})])
    context = RuntimeContext("external", "sandbox", tmp_path, model="claude-code-agent")
    runtime = ClaudeCodeRuntime("key", model="glm-4.7", executor=executor)
    runtime_id = await runtime.create_session(context)

    await collect(runtime, runtime_id, "hi", context)

    command = executor.calls[0][0]
    assert command[command.index("--model") + 1] == "glm-4.7"


@pytest.mark.asyncio
async def test_claude_cli_uses_result_as_visible_fallback_without_duplicate(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": "done"}),
        ]
    )
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", executor=executor)
    runtime_id = await runtime.create_session(context)

    events = await collect(runtime, runtime_id, "hi", context)

    assert [event.text for event in events if isinstance(event, TextDelta)] == ["done"]

    fallback_executor = RecordingExecutor(
        [json.dumps({"type": "result", "subtype": "success", "result": "summary"})]
    )
    fallback_runtime = ClaudeCodeRuntime("key", executor=fallback_executor)
    fallback_id = await fallback_runtime.create_session(context)
    fallback_events = await collect(fallback_runtime, fallback_id, "hi", context)
    assert [event.text for event in fallback_events if isinstance(event, TextDelta)] == ["summary"]

    chunked_executor = RecordingExecutor(
        [
            json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "do"}}
            ),
            json.dumps(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ne"}}
            ),
            json.dumps({"type": "result", "subtype": "success", "result": "done"}),
        ]
    )
    chunked_runtime = ClaudeCodeRuntime("key", executor=chunked_executor)
    chunked_id = await chunked_runtime.create_session(context)
    chunked_events = await collect(chunked_runtime, chunked_id, "hi", context)
    assert "".join(event.text for event in chunked_events if isinstance(event, TextDelta)) == "done"


@pytest.mark.asyncio
async def test_claude_cli_does_not_expose_failed_result_diagnostics(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        [
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "errors": ["internal tool detail"],
                }
            )
        ]
    )
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", executor=executor)
    runtime_id = await runtime.create_session(context)

    events = await collect(runtime, runtime_id, "hi", context)

    assert isinstance(events[0], Failed)
    assert "internal tool detail" not in events[0].message


@pytest.mark.asyncio
async def test_claude_cli_lifecycle_and_failures_are_local(tmp_path: Path) -> None:
    executor = RecordingExecutor(error=OSError("not installed"))
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", auth_env="ANTHROPIC_AUTH_TOKEN", executor=executor)
    runtime_id = await runtime.create_session(context)

    failed = await collect(runtime, runtime_id, "hi", context)
    assert isinstance(failed[0], Failed) and failed[0].code == "claude_cli_failed"
    assert executor.calls[0][2]["ANTHROPIC_AUTH_TOKEN"] == "key"
    assert "ANTHROPIC_API_KEY" not in executor.calls[0][2]

    await runtime.pause(runtime_id, context)
    paused = await collect(runtime, runtime_id, "hi", context)
    assert paused[0].code == "runtime_paused"
    await runtime.resume(runtime_id, context)
    await runtime.close(runtime_id, context)
    closed = await collect(runtime, runtime_id, "hi", context)
    assert closed[0].code == "runtime_closed"
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.resume(runtime_id, context)


@pytest.mark.asyncio
async def test_claude_cli_restored_session_uses_resume(tmp_path: Path) -> None:
    executor = RecordingExecutor([json.dumps({"type": "result"})])
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", executor=executor)

    await collect(runtime, "persisted-cli-session", "continue", context)

    assert (
        executor.calls[0][0][executor.calls[0][0].index("--resume") + 1] == "persisted-cli-session"
    )


@pytest.mark.asyncio
async def test_claude_cli_restored_upload_only_session_still_uses_session_id(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor([json.dumps({"type": "result"})])
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime = ClaudeCodeRuntime("key", executor=executor)

    await runtime.restore_session_state("upload-only-session", started=False)
    await runtime.resume("upload-only-session", context)
    await collect(runtime, "upload-only-session", "first turn", context)

    command = executor.calls[0][0]
    assert command[command.index("--session-id") + 1] == "upload-only-session"


def test_claude_cli_rejects_unapproved_auth_environment() -> None:
    with pytest.raises(ValueError, match="auth_env"):
        ClaudeCodeRuntime("key", auth_env="CLAUDE_CODE_OAUTH_TOKEN")
