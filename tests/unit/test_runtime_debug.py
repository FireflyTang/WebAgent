from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from app.runtime.agent_sdk import AgentSDKRuntime
from app.runtime.agent_sdk_runner import EventMapper
from app.runtime.events import Diagnostic
from app.sessions.html_log import SessionHtmlLogger
from app.sessions.repository import SQLiteSessionRepository
from app.sessions.runtime_debug import append_runtime_debug, format_runtime_debug


def test_runtime_debug_formatter_keeps_readable_json_and_omits_thinking() -> None:
    entry = format_runtime_debug(
        "sdk.stream_event",
        {
            "type": "content_block_delta",
            "tool_name": "Bash",
            "tool_use_id": "tool-7",
            "visible_text": "可见的 Claude 文本",
            "thinking": "private chain of thought",
            "usage": {"input_tokens": 4, "output_tokens": 9},
            "duration_ms": 1250,
            "duration_api_ms": 800,
            "result": "ok",
            "event": {"thinking_delta": "also hidden", "text": "visible"},
        },
    )

    assert entry.title == "运行时诊断：sdk.stream_event"
    assert entry.metadata["事件类型"] == "sdk.stream_event"
    assert entry.metadata["工具"] == "Bash"
    assert entry.metadata["工具调用"] == "tool-7"
    assert entry.metadata["用量"] == '{"input_tokens": 4, "output_tokens": 9}'
    assert "结果" not in entry.metadata
    assert entry.metadata["可见文本长度"] == len("可见的 Claude 文本")
    assert entry.metadata["总耗时毫秒"] == 1250
    assert entry.metadata["API 耗时毫秒"] == 800
    assert entry.metadata["thinking"] == "已省略"
    assert "private chain of thought" not in entry.content
    assert "also hidden" not in entry.content
    assert entry.content.startswith("{\n")
    assert '"text": "visible"' in entry.content


def test_runtime_debug_keeps_visible_text_but_omits_provider_configuration() -> None:
    entry = format_runtime_debug(
        "sdk.assistant",
        {
            "visible_text": "用户可见的回答",
            "api_key": "must-not-be-persisted",
            "environment": {"ANTHROPIC_API_KEY": "also-not-persisted"},
            "provider": {
                "base_url": "https://provider.example/private",
                "api_key": "provider-key-must-not-be-persisted",
                "auth_env": "ANTHROPIC_AUTH_TOKEN",
            },
        },
    )

    assert entry.metadata["可见文本长度"] == len("用户可见的回答")
    assert "用户可见的回答" in entry.content
    assert "must-not-be-persisted" not in entry.content
    assert "also-not-persisted" not in entry.content
    assert "provider-key-must-not-be-persisted" not in entry.content
    assert "https://provider.example/private" not in entry.content
    assert "[已省略 Provider 配置]" in entry.content


def test_runtime_debug_summarizes_real_diagnostic_tool_results_without_copying_raw() -> None:
    succeeded = format_runtime_debug(
        "sdk.tool_result",
        Diagnostic(
            "tool_result",
            tool_name="Bash",
            tool_use_id="tool-7",
            tool_result={"exit_code": 0, "stdout": "a long raw payload"},
        ),
    )
    failed = format_runtime_debug(
        "sdk.tool_result",
        Diagnostic("tool_result", tool_result={"is_error": True, "stderr": "failure detail"}),
    )
    text = format_runtime_debug(
        "sdk.tool_result", Diagnostic("tool_result", tool_result="line one\nline two")
    )

    assert succeeded.metadata["工具结果摘要"] == "成功（退出码 0）"
    assert failed.metadata["工具结果摘要"] == "失败"
    assert text.metadata["工具结果摘要"] == "文本 17 字符：line one line two"
    assert "a long raw payload" not in succeeded.metadata["工具结果摘要"]
    assert "a long raw payload" in succeeded.content


def test_runtime_debug_masks_only_credential_syntax_inside_visible_text_fields() -> None:
    entry = format_runtime_debug(
        "sdk.tool_result",
        {
            "tool_input": {
                "command": "curl -H 'Authorization: Bearer bearer-secret' -H 'Authorization: Basic basic-secret' "
                "-H 'x-api-key: header-secret' https://alice:password@provider.example/run?api_key=query-secret "
                "&access_token=access-secret API_KEY=inline-secret safe-command",
            },
            "tool_result": {
                "stdout": "ok TOKEN: output-secret\nnon-sensitive output",
                "stderr": "Set-Cookie: session=private-cookie\nwarning",
                "headers": {"Cookie": "structured-cookie-secret"},
            },
            "visible_text": "PASSWORD='reply-secret' and ordinary answer",
        },
    )

    assert "bearer-secret" not in entry.content
    assert "basic-secret" not in entry.content
    assert "header-secret" not in entry.content
    assert "password@provider.example" not in entry.content
    assert "query-secret" not in entry.content
    assert "access-secret" not in entry.content
    assert "inline-secret" not in entry.content
    assert "output-secret" not in entry.content
    assert "private-cookie" not in entry.content
    assert "structured-cookie-secret" not in entry.content
    assert "reply-secret" not in entry.content
    assert "safe-command" in entry.content
    assert "non-sensitive output" in entry.content
    assert "ordinary answer" in entry.content


@pytest.mark.asyncio
async def test_real_runner_tool_results_survive_decoder_and_log_without_inline_secrets(
    tmp_path: Path, capsys
) -> None:
    mapper = EventMapper()
    mapper.assistant(
        AssistantMessage(
            content=[ToolUseBlock(id="bash-text", name="Bash", input={"command": "echo ok"})],
            model="glm",
        )
    )
    mapper.user(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="bash-text",
                    content="REFRESH_TOKEN=tool-secret\nordinary string output",
                    is_error=False,
                )
            ]
        )
    )
    mapper.assistant(
        AssistantMessage(
            content=[ToolUseBlock(id="bash-list", name="Bash", input={"command": "echo list"})],
            model="glm",
        )
    )
    mapper.user(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="bash-list",
                    content=[
                        {
                            "type": "text",
                            "text": "Set-Cookie: session=list-secret\nordinary list output",
                        }
                    ],
                    is_error=True,
                )
            ]
        )
    )
    runner_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    diagnostics = [
        AgentSDKRuntime._event_from_line(json.dumps(event))
        for event in runner_events
        if event.get("type") == "diagnostic" and event.get("message_type") == "tool_result"
    ]
    decoded = [event for event in diagnostics if isinstance(event, Diagnostic)]
    assert [event.is_error for event in decoded] == [False, True]

    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)
    for event in decoded:
        await append_runtime_debug(
            logger, "runner-log", f"sdk.{event.message_type}", dataclasses.asdict(event)
        )
    document = await logger.read_diagnostics("runner-log")

    assert "tool-secret" not in document
    assert "list-secret" not in document
    assert "ordinary string output" in document
    assert "ordinary list output" in document
    assert "工具结果 · Bash" in document
    assert "event-error" in document
    await repository.close()


@pytest.mark.asyncio
async def test_runtime_debug_appends_to_the_same_session_html_log(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)
    await logger.append("debug-session", title="用户消息", content="hello")
    entry = await append_runtime_debug(
        logger,
        "debug-session",
        "runner.result",
        {"subtype": "success", "usage": {"input_tokens": 2}, "result": "done"},
    )

    document = await logger.read("debug-session")
    assert document is not None
    assert entry.event_type == "runner.result"
    assert document.count("<!doctype html>") == 1
    assert document.count("<section") == 2
    assert "最终结果" in document
    assert "状态：" in document
    assert "原始 JSON" in document
    assert "&quot;input_tokens&quot;: 2" in document
    assert "&quot;result&quot;: &quot;done&quot;" in document
    await repository.close()
