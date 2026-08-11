#!/opt/claude-agent-sdk/bin/python
"""Container-only Claude Agent SDK runner with a stable, filtered NDJSON protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)


def _emit(kind: str, **fields: object) -> None:
    print(json.dumps({"type": kind, **fields}, ensure_ascii=False), flush=True)


def _usage(value: dict[str, Any] | None) -> None:
    if not isinstance(value, dict):
        return
    input_tokens, output_tokens = value.get("input_tokens"), value.get("output_tokens")
    if isinstance(input_tokens, int) or isinstance(output_tokens, int):
        _emit("usage", input_tokens=input_tokens, output_tokens=output_tokens)


class EventMapper:
    _synthetic_continuation = (
        "[Your previous response had no visible output. Please continue and produce a "
        "user-visible response.]"
    )

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.visible_text = ""
        self.tool_names: dict[str, str] = {}
        self.tasks: dict[str, dict[str, str | None]] = {}
        self.last_thinking_at = 0.0
        self._started: dict[str, float] = {}
        # Completion can be synthesized by ResultMessage after the SDK omits a
        # matching tool/task terminal message.  Keep its association so the
        # resulting Progress still belongs to the visible card.
        self._phase_fields: dict[str, dict[str, object]] = {}
        self._stream_diagnostic_count = 0
        self._stream_diagnostic_fields: dict[str, object] | None = None
        self._last_stream_diagnostic_at = 0.0
        self._has_stream_diagnostic = False

    def _diagnostic(self, message_type: str, **fields: object) -> None:
        """Emit inspectable SDK metadata, deliberately excluding environment and thinking text."""
        _emit("diagnostic", message_type=message_type, **fields)

    def _stream_diagnostic(self, **fields: object) -> None:
        """Sample hot stream diagnostics so SQLite logging cannot gate token flow."""
        self._stream_diagnostic_count += 1
        self._stream_diagnostic_fields = fields
        now = time.monotonic()
        if not self._has_stream_diagnostic or now - self._last_stream_diagnostic_at >= 2:
            self._flush_stream_diagnostic(now)

    def _flush_stream_diagnostic(self, now: float | None = None) -> None:
        if self._stream_diagnostic_count == 0 or self._stream_diagnostic_fields is None:
            return
        self._diagnostic(
            "stream",
            coalesced_count=self._stream_diagnostic_count,
            **self._stream_diagnostic_fields,
        )
        self._stream_diagnostic_count = 0
        self._stream_diagnostic_fields = None
        self._last_stream_diagnostic_at = time.monotonic() if now is None else now
        self._has_stream_diagnostic = True

    def _progress(
        self,
        phase: str,
        message: str,
        status: str,
        *,
        duration_key: str | None = None,
        **fields: object,
    ) -> None:
        now = time.monotonic()
        key = duration_key or phase
        if status in {"started", "running"}:
            self._started.setdefault(key, now)
            retained = self._phase_fields.setdefault(key, {})
            retained.update({name: value for name, value in fields.items() if value is not None})
        elif status in {"completed", "failed"}:
            retained = self._phase_fields.pop(key, {})
            fields = {
                **retained,
                **{name: value for name, value in fields.items() if value is not None},
            }
        duration = None
        if status in {"completed", "failed"}:
            started = self._started.pop(key, None)
            if started is not None:
                duration = round(now - started, 2)
        payload: dict[str, object] = {
            "phase": phase,
            "message": message,
            "status": status,
            "elapsed_seconds": round(now - self.started_at, 2),
            **fields,
        }
        if duration is not None:
            payload["duration_seconds"] = duration
        _emit("progress", **payload)

    def _complete_open_phases(self, *, status: str = "completed") -> None:
        message = "阶段已完成" if status == "completed" else "阶段已失败"
        for key in tuple(self._started):
            phase = key.split(":", 1)[0]
            if phase in {"thinking", "retry", "tool", "task"} or (
                status == "failed" and phase == "starting"
            ):
                self._progress(phase, message, status, duration_key=key)

    def _partial_text(self, text: str) -> None:
        """Stream deltas are append-only, so equal chunks must remain visible."""
        if not text:
            return
        self.visible_text += text
        self._complete_thinking_if_active()
        _emit("text", text=text)

    def _complete_thinking_if_active(self) -> None:
        """End analysis before a visible tool phase begins, exactly once."""
        if "thinking" in self._started:
            self._progress("thinking", "分析完成", "completed", duration_key="thinking")

    def _visible_increment(self, text: str, current: str) -> str:
        """Return the newly visible suffix without retaining private thinking."""
        # This exact prefix is generated by the Claude CLI/SDK when it retries a
        # silent turn. It is an internal instruction, not model-visible output.
        text = text.removeprefix(self._synthetic_continuation)
        if not text:
            return ""
        if text.startswith(current):
            return text[len(current) :]
        if current.endswith(text):
            return ""
        return text

    def _aggregate_text(self, text: str) -> str:
        """Emit and return only the new assistant/result visible text."""
        increment = self._visible_increment(text, self.visible_text)
        if increment:
            self.visible_text += increment
            _emit("text", text=increment)
        return increment

    def _assistant_visible_text(self, blocks: list[Any]) -> str | None:
        """Preview the exact new TextBlock suffixes before emitting diagnostics."""
        current = self.visible_text
        chunks: list[str] = []
        for block in blocks:
            if not isinstance(block, TextBlock):
                continue
            increment = self._visible_increment(block.text, current)
            if increment:
                chunks.append(increment)
                current += increment
        return "".join(chunks) or None

    def stream(self, message: StreamEvent) -> None:
        event = message.event
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        diagnostic: dict[str, object] = {
            "subtype": kind if isinstance(kind, str) else None,
            "message_id": message.uuid,
            "parent_tool_use_id": message.parent_tool_use_id,
        }
        if kind == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self._partial_text(delta["text"])
            elif delta.get("type") == "thinking_delta":
                thinking = delta.get("thinking")
                if isinstance(thinking, str):
                    diagnostic["thinking_length"] = len(thinking)
                now = time.monotonic()
                if now - self.last_thinking_at >= 2:
                    self.last_thinking_at = now
                    self._progress("thinking", "正在分析任务", "running", duration_key="thinking")
            self._stream_diagnostic(**diagnostic)
            return
        if kind == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self._complete_thinking_if_active()
                tool_id = block.get("id") if isinstance(block.get("id"), str) else None
                name = block.get("name") if isinstance(block.get("name"), str) else "工具"
                if tool_id:
                    self.tool_names[tool_id] = name
                self._progress(
                    "tool",
                    "正在使用工具",
                    "started",
                    tool_name=name,
                    tool_use_id=tool_id,
                    parent_tool_use_id=message.parent_tool_use_id,
                    duration_key=f"tool:{tool_id or name}",
                )
                diagnostic.update(tool_name=name, tool_use_id=tool_id)
        self._diagnostic("stream", **diagnostic)

    def assistant(self, message: AssistantMessage) -> None:
        visible_text = self._assistant_visible_text(message.content)
        self._diagnostic(
            "assistant",
            subtype=message.stop_reason,
            message_id=message.message_id or message.uuid,
            usage=message.usage,
            parent_tool_use_id=message.parent_tool_use_id,
            **({"visible_text": visible_text} if visible_text is not None else {}),
        )
        self._usage_and_blocks(message.content, message.parent_tool_use_id, message.usage)

    def user(self, message: UserMessage) -> None:
        self._diagnostic(
            "user", message_id=message.uuid, parent_tool_use_id=message.parent_tool_use_id
        )
        if isinstance(message.content, list):
            self._usage_and_blocks(message.content, message.parent_tool_use_id, None)

    def _usage_and_blocks(
        self, blocks: list[Any], parent_tool_use_id: str | None, usage: dict[str, Any] | None
    ) -> None:
        _usage(usage)
        for block in blocks:
            if isinstance(block, TextBlock):
                self._aggregate_text(block.text)
                self._diagnostic(
                    "text_block", message_id=None, subtype="text", thinking_length=None
                )
            elif isinstance(block, ThinkingBlock):
                self._diagnostic(
                    "thinking_block", subtype="thinking", thinking_length=len(block.thinking)
                )
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                self._complete_thinking_if_active()
                self._diagnostic(
                    "tool_use",
                    tool_name=block.name,
                    tool_use_id=block.id,
                    parent_tool_use_id=parent_tool_use_id,
                    tool_input=block.input,
                )
                if block.id in self.tool_names:
                    continue
                self.tool_names[block.id] = block.name
                self._progress(
                    "tool",
                    "正在使用工具",
                    "started",
                    tool_name=block.name,
                    tool_use_id=block.id,
                    parent_tool_use_id=parent_tool_use_id,
                    duration_key=f"tool:{block.id}",
                )
            elif isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                name = self.tool_names.get(block.tool_use_id, "工具")
                is_error = block.is_error if isinstance(block, ToolResultBlock) else False
                self._progress(
                    "tool",
                    "工具已失败" if is_error else "工具已完成",
                    "failed" if is_error else "completed",
                    tool_name=name,
                    tool_use_id=block.tool_use_id,
                    parent_tool_use_id=parent_tool_use_id,
                    duration_key=f"tool:{block.tool_use_id}",
                )
                self._diagnostic(
                    "tool_result",
                    tool_name=name,
                    tool_use_id=block.tool_use_id,
                    parent_tool_use_id=parent_tool_use_id,
                    tool_result=block.content,
                )

    def task_started(self, message: TaskStartedMessage) -> None:
        self.tasks[message.task_id] = {
            "description": message.description,
            "tool_use_id": message.tool_use_id,
            "tool_name": None,
        }
        self._task_progress(message.task_id, "started")
        self._diagnostic(
            "task_started",
            subtype=message.subtype,
            message_id=message.uuid,
            task_id=message.task_id,
            tool_use_id=message.tool_use_id,
        )

    def task_progress(self, message: TaskProgressMessage) -> None:
        task = self.tasks.setdefault(
            message.task_id,
            {"description": None, "tool_use_id": message.tool_use_id, "tool_name": None},
        )
        task["description"] = message.description or task["description"]
        task["tool_use_id"] = message.tool_use_id or task["tool_use_id"]
        task["tool_name"] = message.last_tool_name or task["tool_name"]
        self._task_progress(message.task_id, "running")
        self._diagnostic(
            "task_progress",
            subtype=message.subtype,
            message_id=message.uuid,
            task_id=message.task_id,
            usage=message.usage,
            tool_name=message.last_tool_name,
            tool_use_id=message.tool_use_id,
        )

    def task_updated(self, message: TaskUpdatedMessage) -> None:
        task = self.tasks.setdefault(
            message.task_id, {"description": None, "tool_use_id": None, "tool_name": None}
        )
        if isinstance(message.patch.get("description"), str):
            task["description"] = message.patch["description"]
        if isinstance(message.patch.get("last_tool_name"), str):
            task["tool_name"] = message.patch["last_tool_name"]
        status = {
            "pending": "started",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "killed": "failed",
            "paused": "running",
        }.get(message.status or "running", "running")
        self._task_progress(message.task_id, status)
        self._diagnostic(
            "task_updated",
            subtype=message.subtype,
            message_id=message.uuid,
            task_id=message.task_id,
        )

    def task_notification(self, message: TaskNotificationMessage) -> None:
        task = self.tasks.setdefault(
            message.task_id,
            {"description": None, "tool_use_id": message.tool_use_id, "tool_name": None},
        )
        task["tool_use_id"] = message.tool_use_id or task["tool_use_id"]
        status = "completed" if message.status == "completed" else "failed"
        self._task_progress(message.task_id, status, summary=message.summary)
        self._diagnostic(
            "task_notification",
            subtype=message.subtype,
            message_id=message.uuid,
            task_id=message.task_id,
            tool_use_id=message.tool_use_id,
        )

    def _task_progress(self, task_id: str, status: str, *, summary: str | None = None) -> None:
        task = self.tasks[task_id]
        message = summary or task["description"] or "正在处理子任务"
        if task["tool_name"]:
            message = f"{message}（当前工具：{task['tool_name']}）"
        self._progress(
            "task",
            message,
            status,
            task_id=task_id,
            tool_use_id=task["tool_use_id"],
            tool_name=task["tool_name"],
            duration_key=f"task:{task_id}",
        )

    def system(self, message: SystemMessage) -> None:
        self._diagnostic("system", subtype=message.subtype)
        subtype = message.subtype.lower()
        if "retry" in subtype:
            self._progress("retry", "正在重试", "running", duration_key="retry")
        elif "task" in subtype or "subagent" in subtype:
            self._progress("task", "正在处理子任务", "running")
        elif subtype == "init":
            self._progress("starting", "Agent 已启动", "completed")

    def result(self, message: ResultMessage) -> None:
        self._flush_stream_diagnostic()
        visible_text = (
            self._visible_increment(message.result, self.visible_text)
            if isinstance(message.result, str)
            else ""
        )
        self._diagnostic(
            "result",
            subtype=message.subtype,
            message_id=message.uuid,
            usage=message.usage,
            duration_ms=message.duration_ms,
            duration_api_ms=message.duration_api_ms,
            **({"visible_text": visible_text} if visible_text else {}),
        )
        self._complete_open_phases(status="failed" if message.is_error else "completed")
        _usage(message.usage)
        if message.is_error:
            self._progress("finalizing", "正在整理结果", "started", duration_key="finalizing")
            self._progress("finalizing", "任务执行失败", "failed", duration_key="finalizing")
            _emit(
                "failed",
                code="agent_sdk_result_error",
                message="Claude Agent SDK reported a failed result",
            )
            return
        self._progress("finalizing", "正在整理结果", "started", duration_key="finalizing")
        self._progress("finalizing", "正在整理结果", "completed", duration_key="finalizing")
        if isinstance(message.result, str):
            self._aggregate_text(message.result)
        _emit("completed", stop_reason=message.stop_reason or "stop")


async def run(args: argparse.Namespace) -> int:
    mapper = EventMapper()
    mapper._progress("starting", "正在启动 Claude Agent SDK", "started", duration_key="starting")
    system_prompt: dict[str, str] = {"type": "preset", "preset": "claude_code"}
    if args.system_prompt:
        system_prompt["append"] = args.system_prompt
    options = ClaudeAgentOptions(
        cwd="/workspace",
        session_id=args.session_id,
        resume=args.resume,
        model=args.model,
        system_prompt=system_prompt,
        setting_sources=[],
        include_partial_messages=True,
        include_hook_events=True,
        permission_mode="bypassPermissions",
        effort=args.effort,
        env=dict(__import__("os").environ),
    )
    try:
        async for message in query(prompt=args.prompt, options=options):
            if isinstance(message, StreamEvent):
                mapper.stream(message)
            elif isinstance(message, AssistantMessage):
                mapper.assistant(message)
            elif isinstance(message, UserMessage):
                mapper.user(message)
            elif isinstance(message, TaskStartedMessage):
                mapper.task_started(message)
            elif isinstance(message, TaskProgressMessage):
                mapper.task_progress(message)
            elif isinstance(message, TaskUpdatedMessage):
                mapper.task_updated(message)
            elif isinstance(message, TaskNotificationMessage):
                mapper.task_notification(message)
            elif isinstance(message, SystemMessage):
                mapper.system(message)
            elif isinstance(message, ResultMessage):
                mapper.result(message)
                return 0 if not message.is_error else 1
    except Exception:
        mapper._flush_stream_diagnostic()
        mapper._complete_open_phases(status="failed")
        _emit(
            "failed",
            code="agent_sdk_failed",
            message="Claude Agent SDK execution failed",
            retryable=True,
        )
        return 1
    mapper._flush_stream_diagnostic()
    mapper._complete_open_phases(status="failed")
    _emit(
        "failed",
        code="agent_sdk_missing_result",
        message="Claude Agent SDK ended without a result message",
        retryable=True,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    first = parser.add_mutually_exclusive_group(required=True)
    first.add_argument("--session-id")
    first.add_argument("--resume")
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--system-prompt")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
