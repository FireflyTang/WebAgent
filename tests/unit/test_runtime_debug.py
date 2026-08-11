from __future__ import annotations

from pathlib import Path

import pytest

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
    assert entry.metadata["结果"] == "ok"
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
        },
    )

    assert entry.metadata["可见文本长度"] == len("用户可见的回答")
    assert "用户可见的回答" in entry.content
    assert "must-not-be-persisted" not in entry.content
    assert "also-not-persisted" not in entry.content


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
    assert document.count("<section>") == 2
    assert "运行时诊断：runner.result" in document
    assert "事件类型" in document
    assert "runner.result" in document
    assert "&quot;input_tokens&quot;: 2" in document
    assert "&quot;result&quot;: &quot;done&quot;" in document
    await repository.close()
