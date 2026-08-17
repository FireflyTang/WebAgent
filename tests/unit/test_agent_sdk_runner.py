from __future__ import annotations

import json
from argparse import Namespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import app.runtime.agent_sdk_runner as runner
from app.runtime.agent_sdk_runner import EventMapper


def _events(capsys) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.asyncio
async def test_runner_passes_explicit_effort_to_sdk_options(monkeypatch) -> None:
    captured = {}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        yield ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="session",
            usage={},
            result="",
        )

    monkeypatch.setattr(runner, "query", fake_query)
    result = await runner.run(
        Namespace(
            prompt="work",
            session_id="session",
            resume=None,
            model="glm-5.2",
            effort="high",
            system_prompt=None,
        )
    )

    assert result == 0
    assert captured["prompt"] == "work"
    assert captured["options"].effort == "high"


@pytest.mark.asyncio
async def test_runner_flushes_sampled_stream_diagnostic_when_query_ends_without_result(
    capsys, monkeypatch
) -> None:
    async def fake_query(*, prompt, options):
        del prompt, options
        for index in range(3):
            yield StreamEvent(
                uuid=f"delta-{index}",
                session_id="session",
                parent_tool_use_id=None,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": str(index)},
                },
            )

    monkeypatch.setattr(runner, "query", fake_query)
    result = await runner.run(
        Namespace(
            prompt="work",
            session_id="session",
            resume=None,
            model="glm-5.2",
            effort=None,
            system_prompt=None,
        )
    )

    diagnostics = [
        event
        for event in _events(capsys)
        if event["type"] == "diagnostic" and event["message_type"] == "stream"
    ]
    assert result == 1
    assert sum(event["coalesced_count"] for event in diagnostics) == 3
    assert diagnostics[-1]["message_id"] == "delta-2"


@pytest.mark.asyncio
async def test_runner_missing_result_fails_open_tool_without_completed(monkeypatch, capsys) -> None:
    async def fake_query(*, prompt, options):
        del prompt, options
        yield StreamEvent(
            uuid="tool-start",
            session_id="session",
            parent_tool_use_id=None,
            event={
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Bash"},
            },
        )

    monkeypatch.setattr(runner, "query", fake_query)
    result = await runner.run(
        Namespace(
            prompt="work",
            session_id="session",
            resume=None,
            model="glm-5.2",
            effort=None,
            system_prompt=None,
        )
    )

    events = _events(capsys)
    progress = [event for event in events if event["type"] == "progress"]
    failed = [event for event in events if event["type"] == "failed"]
    assert result == 1
    assert any(
        event["phase"] == "tool"
        and event["status"] == "failed"
        and event["tool_use_id"] == "tool-1"
        and event["tool_name"] == "Bash"
        for event in progress
    )
    assert failed == [
        {
            "type": "failed",
            "code": "agent_sdk_missing_result",
            "message": "Claude Agent SDK ended without a result message",
            "retryable": True,
        }
    ]
    assert any(event["phase"] == "starting" and event["status"] == "failed" for event in progress)
    assert not any(event["type"] == "completed" for event in events)


@pytest.mark.asyncio
async def test_runner_exception_fails_starting_without_completed(monkeypatch, capsys) -> None:
    async def fake_query(*, prompt, options):
        del prompt, options
        raise RuntimeError("runner transport failed")
        yield  # pragma: no cover

    monkeypatch.setattr(runner, "query", fake_query)
    result = await runner.run(
        Namespace(
            prompt="work",
            session_id="session",
            resume=None,
            model="glm-5.2",
            effort=None,
            system_prompt=None,
        )
    )
    events = _events(capsys)
    progress = [event for event in events if event["type"] == "progress"]
    assert result == 1
    assert any(event["phase"] == "starting" and event["status"] == "failed" for event in progress)
    assert events[-1]["type"] == "failed" and events[-1]["code"] == "agent_sdk_failed"
    assert not any(event["type"] == "completed" for event in events)


def test_runner_filters_thinking_and_tool_payloads_but_reports_tool_progress(capsys) -> None:
    mapper = EventMapper()
    mapper.stream(
        StreamEvent(
            uuid="event",
            session_id="session",
            parent_tool_use_id=None,
            event={
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "secret"},
            },
        )
    )
    mapper.stream(
        StreamEvent(
            uuid="tool-start",
            session_id="session",
            parent_tool_use_id=None,
            event={
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Write"},
            },
        )
    )
    mapper.assistant(
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Write", input={"content": "secret"})],
            model="glm",
            parent_tool_use_id=None,
        )
    )
    mapper.user(
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="tool-1", content="secret result", is_error=False)
            ],
            parent_tool_use_id=None,
        )
    )
    events = _events(capsys)
    progress = [event for event in events if event["type"] == "progress"]
    diagnostics = [event for event in events if event["type"] == "diagnostic"]
    diagnostics = [event for event in events if event["type"] == "diagnostic"]
    assert [(event["phase"], event["status"]) for event in progress] == [
        ("thinking", "running"),
        ("thinking", "completed"),
        ("tool", "started"),
        ("tool", "completed"),
    ]
    thinking = next(event for event in diagnostics if event.get("thinking_length") == len("secret"))
    assert "secret" not in json.dumps(thinking)
    assert next(event for event in diagnostics if event["message_type"] == "tool_use")[
        "tool_input"
    ] == {"content": "secret"}
    assert progress[2]["tool_name"] == "Write"
    assert progress[2]["message"] == "正在使用工具"
    assert progress[3]["message"] == "工具已完成"
    assert progress[3]["status"] == "completed"


def test_runner_aggregate_tool_use_closes_thinking_before_tool_start(capsys) -> None:
    mapper = EventMapper()
    mapper._progress("thinking", "正在分析任务", "running", duration_key="thinking")
    mapper.assistant(
        AssistantMessage(
            content=[ToolUseBlock(id="tool-aggregate", name="Read", input={})], model="glm"
        )
    )
    progress = [event for event in _events(capsys) if event["type"] == "progress"]
    assert [(event["phase"], event["status"], event["message"]) for event in progress] == [
        ("thinking", "running", "正在分析任务"),
        ("thinking", "completed", "分析完成"),
        ("tool", "started", "正在使用工具"),
    ]


def test_runner_deduplicates_partial_and_final_text_and_emits_result_metadata(capsys) -> None:
    mapper = EventMapper()
    mapper.stream(
        StreamEvent(
            uuid="event",
            session_id="session",
            parent_tool_use_id=None,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
        )
    )
    mapper.stream(
        StreamEvent(
            uuid="event-2",
            session_id="session",
            parent_tool_use_id=None,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
        )
    )
    mapper.assistant(AssistantMessage(content=[TextBlock("hellohello world")], model="glm"))
    mapper.result(
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=1,
            session_id="session",
            usage={"input_tokens": 2, "output_tokens": 3},
            result="hellohello world",
        )
    )
    events = _events(capsys)
    assert [event for event in events if event["type"] == "text"] == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "hello"},
        {"type": "text", "text": " world"},
    ]
    assert any(
        event == {"type": "usage", "input_tokens": 2, "output_tokens": 3} for event in events
    )
    assistant = next(
        event
        for event in events
        if event["type"] == "diagnostic" and event["message_type"] == "assistant"
    )
    assert assistant["visible_text"] == " world"
    result = next(
        event
        for event in events
        if event["type"] == "diagnostic" and event["message_type"] == "result"
    )
    assert "visible_text" not in result
    assert result["is_error"] is False
    assert result["result"] == "hellohello world"
    assert events[-1] == {"type": "completed", "stop_reason": "stop"}


def test_runner_maps_task_messages_with_stable_task_and_tool_association(capsys) -> None:
    mapper = EventMapper()
    mapper.task_started(
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="task-1",
            description="实现功能",
            uuid="a",
            session_id="session",
            tool_use_id="tool-1",
        )
    )
    mapper.task_progress(
        TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="task-1",
            description="实现功能",
            usage={},
            uuid="b",
            session_id="session",
            tool_use_id="tool-1",
            last_tool_name="Edit",
        )
    )
    mapper.task_updated(
        TaskUpdatedMessage(
            subtype="task_updated",
            data={},
            task_id="task-1",
            patch={"description": "运行测试"},
            status="completed",
            session_id="session",
            uuid="c",
        )
    )
    mapper.task_notification(
        TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-1",
            status="completed",
            output_file="ignored",
            summary="测试通过",
            uuid="d",
            session_id="session",
            tool_use_id="tool-1",
        )
    )
    events = _events(capsys)
    progress = [event for event in events if event["type"] == "progress"]
    diagnostics = [event for event in events if event["type"] == "diagnostic"]
    assert [(event["status"], event["task_id"], event["tool_use_id"]) for event in progress] == [
        ("started", "task-1", "tool-1"),
        ("running", "task-1", "tool-1"),
        ("completed", "task-1", "tool-1"),
        ("completed", "task-1", "tool-1"),
    ]
    assert progress[1]["tool_name"] == "Edit"
    assert progress[2]["message"].startswith("运行测试")
    assert progress[3]["message"] == "测试通过（当前工具：Edit）"
    assert all("current" not in event and "total" not in event for event in progress)
    assert (
        next(event for event in diagnostics if event["message_type"] == "task_progress")["task_id"]
        == "task-1"
    )


def test_runner_uses_tool_local_duration_not_turn_elapsed(capsys, monkeypatch) -> None:
    ticks = iter((10.0, 15.0, 18.5))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    mapper = EventMapper()
    mapper.stream(
        StreamEvent(
            uuid="tool-start",
            session_id="session",
            parent_tool_use_id=None,
            event={
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Write"},
            },
        )
    )
    mapper.user(UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", is_error=False)]))
    progress = [event for event in _events(capsys) if event["type"] == "progress"]
    assert progress[0]["elapsed_seconds"] == 5.0
    assert progress[1]["duration_seconds"] == 3.5


def test_runner_maps_server_tool_blocks_to_progress_and_diagnostic(capsys, monkeypatch) -> None:
    ticks = iter((20.0, 24.0, 30.0))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    mapper = EventMapper()
    mapper.assistant(
        AssistantMessage(
            content=[ServerToolUseBlock(id="server-1", name="web_search", input={"query": "sdk"})],
            model="glm",
        )
    )
    mapper.user(
        UserMessage(
            content=[ServerToolResultBlock(tool_use_id="server-1", content={"result": "ok"})]
        )
    )
    events = _events(capsys)
    progress = [event for event in events if event["type"] == "progress"]
    diagnostics = [event for event in events if event["type"] == "diagnostic"]
    assert [(event["status"], event["tool_name"]) for event in progress] == [
        ("started", "web_search"),
        ("completed", "web_search"),
    ]
    assert progress[1]["duration_seconds"] == 6.0
    use = next(event for event in diagnostics if event["message_type"] == "tool_use")
    result = next(event for event in diagnostics if event["message_type"] == "tool_result")
    assert use["tool_input"] == {"query": "sdk"}
    assert result["is_error"] is False
    assert result["tool_result"] == {"result": "ok"}


def test_runner_preserves_real_tool_result_block_shape_and_error_status(capsys) -> None:
    mapper = EventMapper()
    mapper.assistant(
        AssistantMessage(
            content=[ToolUseBlock(id="bash-1", name="Bash", input={"command": "echo ok"})],
            model="glm",
        )
    )
    mapper.user(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="bash-1",
                    content=[{"type": "text", "text": "real SDK result"}],
                    is_error=True,
                )
            ]
        )
    )

    result = next(
        event
        for event in _events(capsys)
        if event["type"] == "diagnostic" and event["message_type"] == "tool_result"
    )
    assert result == {
        "type": "diagnostic",
        "message_type": "tool_result",
        "tool_name": "Bash",
        "tool_use_id": "bash-1",
        "parent_tool_use_id": None,
        "is_error": True,
        "tool_result": [{"type": "text", "text": "real SDK result"}],
    }


def test_runner_result_closes_open_tool_and_task_with_their_associations(capsys) -> None:
    mapper = EventMapper()
    mapper.stream(
        StreamEvent(
            uuid="tool-start",
            session_id="session",
            parent_tool_use_id="parent-1",
            event={
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Bash"},
            },
        )
    )
    mapper.task_started(
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="task-1",
            description="运行检查",
            uuid="task-start",
            session_id="session",
            tool_use_id="tool-1",
        )
    )
    mapper.result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="",
        )
    )

    progress = [event for event in _events(capsys) if event["type"] == "progress"]
    tool_done = next(
        event for event in progress if event["phase"] == "tool" and event["status"] == "completed"
    )
    task_done = next(
        event for event in progress if event["phase"] == "task" and event["status"] == "completed"
    )
    assert tool_done["tool_use_id"] == "tool-1"
    assert tool_done["tool_name"] == "Bash"
    assert tool_done["parent_tool_use_id"] == "parent-1"
    assert task_done["task_id"] == "task-1"
    assert task_done["tool_use_id"] == "tool-1"


def test_runner_samples_hot_stream_diagnostics_and_flushes_on_result(capsys, monkeypatch) -> None:
    ticks = iter((0.0, 0.1, 0.2, 3.0, 3.1, 3.2))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    mapper = EventMapper()
    for index in range(3):
        mapper.stream(
            StreamEvent(
                uuid=f"delta-{index}",
                session_id="session",
                parent_tool_use_id=None,
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": str(index)},
                },
            )
        )
    mapper.result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="012",
        )
    )
    diagnostics = [
        event
        for event in _events(capsys)
        if event["type"] == "diagnostic" and event["message_type"] == "stream"
    ]
    assert [event["coalesced_count"] for event in diagnostics] == [1, 2]
    assert diagnostics[-1]["message_id"] == "delta-2"


def test_runner_removes_only_sdk_synthetic_continuation_prefix_from_result(capsys) -> None:
    mapper = EventMapper()
    mapper.result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result=(
                "[Your previous response had no visible output. Please continue and produce a "
                "user-visible response.]SDK_FINAL_OK"
            ),
        )
    )
    events = _events(capsys)
    assert [event for event in events if event["type"] == "text"] == [
        {"type": "text", "text": "SDK_FINAL_OK"}
    ]
    result = next(
        event
        for event in events
        if event["type"] == "diagnostic" and event["message_type"] == "result"
    )
    assert result["visible_text"] == "SDK_FINAL_OK"


def test_runner_keeps_nonmatching_visible_text(capsys) -> None:
    mapper = EventMapper()
    mapper.result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="[Your previous response had no visible output.] 用户的正常文本",
        )
    )
    events = _events(capsys)
    assert {
        "type": "text",
        "text": "[Your previous response had no visible output.] 用户的正常文本",
    } in events
