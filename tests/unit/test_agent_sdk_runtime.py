from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest

from app.config import Settings
from app.main import _runtime
from app.runtime.agent_sdk import AgentSDKRuntime
from app.runtime.base import ProviderConfig, RuntimeContext
from app.runtime.events import Completed, Diagnostic, Failed, Progress, TextDelta, Usage
from app.sandbox.docker import DockerSandboxManager


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


async def collect(runtime: AgentSDKRuntime, runtime_id: str, context: RuntimeContext):
    return [event async for event in runtime.send_message(runtime_id, "do work", context)]


def provider_context(tmp_path: Path, **kwargs) -> RuntimeContext:
    provider = kwargs.pop("provider", ProviderConfig(api_key="test-key"))
    return RuntimeContext(
        "external",
        "sandbox",
        tmp_path,
        provider=provider,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_sdk_runtime_runs_only_in_injected_executor_and_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-subscription-token")
    executor = RecordingExecutor([json.dumps({"type": "completed"})])
    runtime = AgentSDKRuntime(executor=executor)
    context = provider_context(
        tmp_path,
        system_prompt="be concise",
        model="glm-4.7",
        effort="medium",
        provider=ProviderConfig(base_url="https://gateway.example/", api_key="test-key"),
    )
    runtime_id = await runtime.create_session(context)
    await runtime.resume(runtime_id, context)

    await collect(runtime, runtime_id, context)
    await collect(runtime, runtime_id, context)

    first, _, first_env = executor.calls[0]
    second = executor.calls[1][0]
    assert first[:3] == ["/usr/local/bin/oca-agent-sdk-runner", "--prompt", "do work"]
    assert first[first.index("--session-id") + 1] == runtime_id
    assert second[second.index("--resume") + 1] == runtime_id
    assert first[first.index("--model") + 1] == "glm-4.7"
    assert first[first.index("--effort") + 1] == "medium"
    assert second[second.index("--effort") + 1] == "medium"
    assert first[first.index("--system-prompt") + 1] == "be concise"
    assert first_env["ANTHROPIC_API_KEY"] == "test-key"
    assert first_env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert first_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-agent-sdk")
    assert "ANTHROPIC_AUTH_TOKEN" not in first_env
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "host-subscription-token"


@pytest.mark.asyncio
async def test_sdk_runtime_maps_runner_events_without_raw_provider_data(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        [
            json.dumps(
                {
                    "type": "progress",
                    "phase": "thinking",
                    "message": "分析",
                    "status": "completed",
                    "task_id": "task-1",
                    "duration_seconds": 1.25,
                }
            ),
            json.dumps(
                {
                    "type": "diagnostic",
                    "message_type": "tool_use",
                    "subtype": "assistant",
                    "message_id": "message-1",
                    "tool_name": "Write",
                    "tool_use_id": "tool-1",
                    "tool_input": {"path": "demo.txt"},
                    "visible_text": "正在写入 demo.txt",
                    "usage": {"input_tokens": 3},
                    "is_error": True,
                    "result": "runner final result",
                }
            ),
            json.dumps({"type": "text", "text": "hello"}),
            json.dumps({"type": "usage", "input_tokens": 3, "output_tokens": 5}),
            json.dumps({"type": "completed", "stop_reason": "end_turn"}),
        ]
    )
    runtime = AgentSDKRuntime(executor=executor)
    context = provider_context(tmp_path)
    runtime_id = await runtime.create_session(context)

    events = await collect(runtime, runtime_id, context)

    assert any(
        isinstance(event, Progress) and event.task_id == "task-1" and event.duration_seconds == 1.25
        for event in events
    )
    assert any(
        isinstance(event, Diagnostic)
        and event.tool_input == {"path": "demo.txt"}
        and event.tool_use_id == "tool-1"
        and event.visible_text == "正在写入 demo.txt"
        and event.is_error is True
        and event.result == "runner final result"
        for event in events
    )
    assert any(isinstance(event, TextDelta) and event.text == "hello" for event in events)
    assert any(isinstance(event, Usage) and event.output_tokens == 5 for event in events)
    assert isinstance(events[-1], Completed) and events[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_sdk_runtime_hides_runner_failure_details_and_handles_lifecycle(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor(
        [json.dumps({"type": "failed", "code": "internal", "message": "tool input secret"})]
    )
    runtime = AgentSDKRuntime(executor=executor)
    context = provider_context(tmp_path)
    runtime_id = await runtime.create_session(context)
    events = await collect(runtime, runtime_id, context)
    assert isinstance(events[0], Failed)
    assert events[0].message == "Claude Agent SDK reported a failed result"

    await runtime.pause(runtime_id, context)
    assert (await collect(runtime, runtime_id, context))[0].code == "runtime_paused"
    await runtime.close(runtime_id, context)
    assert (await collect(runtime, runtime_id, context))[0].code == "runtime_closed"


@pytest.mark.asyncio
async def test_sdk_runtime_closes_executor_iterator_after_terminal_failure(tmp_path: Path) -> None:
    closed = False

    async def executor(command, *, cwd, env):
        nonlocal closed
        del command, cwd, env
        try:
            yield json.dumps({"type": "failed", "code": "broken"})
            yield json.dumps({"type": "text", "text": "must not read"})
        finally:
            closed = True

    runtime = AgentSDKRuntime(executor=executor)
    context = provider_context(tmp_path)
    runtime_id = await runtime.create_session(context)
    events = await collect(runtime, runtime_id, context)

    assert isinstance(events[0], Failed)
    assert closed is True


@pytest.mark.asyncio
async def test_sdk_runtime_uses_per_turn_provider_context(tmp_path: Path) -> None:
    executor = RecordingExecutor([json.dumps({"type": "completed"})])
    runtime = AgentSDKRuntime(executor=executor)
    first = RuntimeContext(
        "external",
        "sandbox",
        tmp_path,
        provider=ProviderConfig(
            base_url="https://first.example/",
            api_key="first-key",
            auth_env="ANTHROPIC_AUTH_TOKEN",
        ),
    )
    runtime_id = await runtime.create_session(first)
    await collect(runtime, runtime_id, first)

    second = RuntimeContext(
        "external",
        "sandbox",
        tmp_path,
        provider=ProviderConfig(
            base_url="https://second.example/",
            api_key="second-key",
            auth_env="ANTHROPIC_API_KEY",
        ),
    )
    await collect(runtime, runtime_id, second)

    first_env = executor.calls[0][2]
    second_command, _, second_env = executor.calls[1]
    assert first_env["ANTHROPIC_AUTH_TOKEN"] == "first-key"
    assert first_env["ANTHROPIC_BASE_URL"] == "https://first.example"
    assert "ANTHROPIC_API_KEY" not in first_env
    assert second_env["ANTHROPIC_API_KEY"] == "second-key"
    assert second_env["ANTHROPIC_BASE_URL"] == "https://second.example"
    assert second_command[second_command.index("--resume") + 1] == runtime_id


@pytest.mark.asyncio
async def test_sdk_runtime_without_settings_or_turn_credential_fails_clearly(
    tmp_path: Path,
) -> None:
    runtime = AgentSDKRuntime(executor=RecordingExecutor())
    context = RuntimeContext("external", "sandbox", tmp_path)
    runtime_id = await runtime.create_session(context)

    events = await collect(runtime, runtime_id, context)

    assert isinstance(events[0], Failed)
    assert events[0].code == "provider_credentials_missing"


def test_claude_backend_starts_without_a_server_provider_key(tmp_path: Path) -> None:
    runtime = _runtime(
        Settings(runtime_backend="claude", sandbox_backend="docker"),
        DockerSandboxManager(tmp_path / "workspaces", docker_binary="missing-docker"),
    )

    assert isinstance(runtime, AgentSDKRuntime)
