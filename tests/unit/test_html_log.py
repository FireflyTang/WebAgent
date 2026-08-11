from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.sessions.html_log import SessionHtmlLogger
from app.sessions.repository import SQLiteSessionRepository
from app.sessions.runtime_debug import append_runtime_debug


@pytest.mark.asyncio
async def test_session_html_log_escapes_and_appends_sections(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)

    await logger.append(
        "demo/<session>",
        title="User <input>",
        content="<script>alert('not executable')</script>\nline two",
        metadata={"model": "<unsafe>", "turn": 1},
    )
    await logger.append("demo/<session>", title="Assistant", content="done", metadata={"ok": True})

    document = await logger.read("demo/<session>")
    assert document is not None
    assert document.count("<!doctype html>") == 1
    assert document.count("<section>") == 2
    assert "<script>" not in document
    assert "&lt;script&gt;alert(&#x27;not executable&#x27;)&lt;/script&gt;" in document
    assert "User &lt;input&gt;" in document
    assert "&lt;unsafe&gt;" in document
    assert "时间：" in document
    assert "<pre>done</pre>" in document
    assert await repository.list_log_entries("demo/<session>")
    assert not (tmp_path / "session-logs").exists()
    await repository.close()


@pytest.mark.asyncio
async def test_session_html_log_isolated_and_missing_returns_none(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)
    await asyncio.gather(
        logger.append("one", title="one", content="first"),
        logger.append("two", title="two", content="second"),
    )

    assert await logger.read("absent") is None
    assert "second" not in (await logger.read("one") or "")
    assert "first" not in (await logger.read("two") or "")
    await repository.close()


@pytest.mark.asyncio
async def test_diagnostic_html_groups_noisy_sdk_events_and_keeps_key_events_ordered(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)
    await logger.append("diagnostic", title="用户消息", content="ordinary interaction")
    stream_one = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.stream",
        content='{"subtype":"content_block_delta"}',
        metadata={"事件类型": "sdk.stream"},
        event_type="sdk.stream",
    )
    await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.system",
        content='{"subtype":"init"}',
        metadata={"事件类型": "sdk.system"},
        event_type="sdk.system",
    )
    thinking = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.thinking_block",
        content='{"thinking":"[已省略 thinking 内容]","thinking_length":42}',
        metadata={"thinking": "已省略"},
        event_type="sdk.thinking_block",
    )
    tool_use = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.tool_use",
        content='{"tool_name":"Bash","tool_use_id":"tool-1"}',
        metadata={"工具": "Bash", "工具调用": "tool-1"},
        event_type="sdk.tool_use",
    )
    tool_result = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.tool_result",
        content='{"tool_result":{"exit_code":0}}',
        metadata={"工具": "Bash", "结果": "ok"},
        event_type="sdk.tool_result",
    )
    result = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.result",
        content='{"usage":{"input_tokens":3,"output_tokens":5},"result":"stop"}',
        metadata={"用量": '{"input_tokens": 3, "output_tokens": 5}'},
        event_type="sdk.result",
    )
    failure = await logger.append(
        "diagnostic",
        title="运行时诊断：sdk.result",
        content='{"subtype":"error"}',
        metadata={"结果": "error"},
        event_type="sdk.result",
    )

    document = await logger.read_diagnostics("diagnostic")

    assert "诊断摘要" in document
    assert "诊断事件：8 条" in document
    assert "ordinary interaction" in document
    assert "高频 SDK 诊断批次：3 条" in document
    assert "<details class='diagnostic-batch'>" in document
    assert "<details class='diagnostic-batch' open>" not in document
    batch = document.split("<details class='diagnostic-batch'>", 1)[1].split("</details>", 1)[0]
    assert batch.count("<pre>") == 1
    assert "<article>" not in batch
    assert f"#{stream_one.sequence}–#{thinking.sequence}" in document
    assert "{&quot;subtype&quot;:&quot;content_block_delta&quot;}" in batch
    assert "{&quot;thinking&quot;:&quot;[已省略 thinking 内容]&quot;" in batch
    assert "思考诊断（内容已省略）" in document
    assert "工具调用" in document
    assert "工具结果" in document
    assert "最终结果" in document
    assert "运行错误" in document
    assert "原始 JSON" in document
    assert document.index(f"#{stream_one.sequence} ") < document.index(f"<h2>#{tool_use.sequence} ")
    assert document.index(f"<h2>#{tool_use.sequence} ") < document.index(
        f"<h2>#{tool_result.sequence} "
    )
    assert document.index(f"<h2>#{tool_result.sequence} ") < document.index(
        f"<h2>#{result.sequence} "
    )
    assert document.index(f"<h2>#{result.sequence} ") < document.index(f"<h2>#{failure.sequence} ")
    assert not (tmp_path / "session-logs").exists()
    await repository.close()


@pytest.mark.asyncio
async def test_debug_transcript_exposes_legacy_chat_and_complete_tool_values(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    logger = SessionHtmlLogger(repository)
    await logger.append(
        "transcript",
        title="用户消息",
        content="请运行测试",
        metadata={"模型": "claude-test"},
    )
    tool_use = await append_runtime_debug(
        logger,
        "transcript",
        "sdk.tool_use",
        {
            "tool_name": "Bash",
            "tool_use_id": "bash-1",
            "tool_input": {
                "command": "pytest -q && echo done",
                "description": "运行单元测试",
            },
        },
    )
    tool_result = await append_runtime_debug(
        logger,
        "transcript",
        "sdk.tool_result",
        {
            "tool_name": "Bash",
            "tool_use_id": "bash-1",
            "tool_result": {"exit_code": 0, "stdout": "2 passed", "stderr": "warning"},
        },
    )
    result = await append_runtime_debug(
        logger,
        "transcript",
        "sdk.result",
        {
            "subtype": "success",
            "visible_text": "测试完成",
            "usage": {"input_tokens": 3, "output_tokens": 5},
            "duration_ms": 1200,
            "duration_api_ms": 900,
        },
    )
    await logger.append(
        "transcript",
        title="Claude Code 输出",
        content="测试完成",
        metadata={"模型": "claude-test", "总耗时秒": 1.2},
    )

    document = await logger.read_diagnostics("transcript")

    assert "请运行测试" in document
    assert "Claude Code 输出" in document
    assert "测试完成" in document
    assert "Bash 命令" in document
    assert "pytest -q &amp;&amp; echo done" in document
    assert "运行单元测试" in document
    assert "工具输入（完整）" in document
    assert "工具结果（完整）" in document
    assert "stdout（完整）" in document
    assert "2 passed" in document
    assert "stderr（完整）" in document
    assert "warning" in document
    assert "最终 result / 用量 / 耗时" in document
    assert "1200" in document
    assert "原始 JSON" in document
    assert document.index("请运行测试") < document.index(f"<h2>#{tool_use.sequence} ")
    assert document.index(f"<h2>#{tool_use.sequence} ") < document.index(
        f"<h2>#{tool_result.sequence} "
    )
    assert document.index(f"<h2>#{tool_result.sequence} ") < document.index(
        f"<h2>#{result.sequence} "
    )
    assert document.index(f"<h2>#{result.sequence} ") < document.rindex("Claude Code 输出")
    await repository.close()
