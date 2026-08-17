"""Render durable SQLite session log entries as a readable HTML document."""

from __future__ import annotations

import html
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .repository import SessionLogEntry, SessionRepository


class SessionHtmlLogger:
    """Store entries in SQLite and render one complete document on demand."""

    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    async def append(
        self,
        session_id: str,
        *,
        title: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
        event_type: str | None = None,
    ) -> SessionLogEntry:
        return await self.repository.append_log_entry(
            session_id,
            title=title,
            content=content,
            metadata=metadata,
            event_type=event_type,
        )

    async def read(self, session_id: str) -> str | None:
        entries = await self.repository.list_log_entries(session_id)
        return self.render(session_id, entries) if entries else None

    async def read_diagnostics(self, session_id: str) -> str:
        """Render the Claude Code debug transcript in durable insertion order.

        Older databases already contain user and final Claude output as normal
        log entries.  Include those two established titles beside structured
        diagnostics so an existing session becomes readable without migration,
        while ordinary progress records remain outside the debug transcript.
        """

        entries = await self.repository.list_log_entries(session_id)
        diagnostics = [
            entry
            for entry in entries
            if entry.event_type is not None or entry.title in {"用户消息", "Claude Code 输出"}
        ]
        return self.render(
            session_id,
            diagnostics,
            empty_message="暂无诊断事件" if not diagnostics else None,
        )

    @staticmethod
    def render(
        session_id: str,
        entries: list[SessionLogEntry],
        *,
        empty_message: str | None = None,
    ) -> str:
        parts = [
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<title>执行转录</title>",
            "<style>body{font:16px/1.55 sans-serif;max-width:920px;margin:20px auto;"
            "padding:0 18px;color:#1f2937}.transcript-header{margin-bottom:16px}.transcript-header h1{"
            "font-size:1.35rem;margin:0}.session-id{margin:3px 0 0;color:#64748b;font-size:.85rem;"
            "overflow-wrap:anywhere}.session-id code{color:#475569}section{border-top:1px solid #d7dde5;padding:16px 0}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px;"
            "border-radius:6px;margin:8px 0}h2{font-size:1.05rem;margin:0 0 8px}.turn{border-left:4px solid #94a3b8;"
            "padding-left:14px}.turn-user{border-left-color:#2563eb}.turn-assistant{border-left-color:#16a34a}"
            "dt{font-weight:bold;float:left;clear:left;margin-right:8px}dd{margin:2px 0}"
            ".summary{background:#f5f7fa;border:1px solid #dbe3eb;padding:10px 14px;"
            "margin:10px 0 18px;border-radius:8px}.summary p{margin:3px 0}.summary-counts{font-weight:600}"
            ".event-meta{font-size:.92em;color:#444}.event-meta code{font-size:.9em}"
            ".diagnostic-batch{border:1px solid #e2e8f0;border-radius:8px;padding:0 10px;"
            "margin:12px 0;background:#fafcff;opacity:.72;font-size:.88rem}.diagnostic-batch>summary{"
            "padding:8px 0;cursor:pointer;font-weight:500}"
            ".transcript-detail{margin:10px 0;padding:10px 12px;background:#f8fafc;"
            "border-left:4px solid #7c98b8}.transcript-detail h3,.transcript-detail h4{"
            "margin:0 0 8px}.bash-command{background:#1e293b;color:#f8fafc;"
            "border:2px solid #f59e0b}.tool-input{border-left:4px solid #3b82f6}.tool-result{"
            "border-left:4px solid #22c55e;max-height:360px;overflow:auto}.tool-stderr{"
            "border-left:4px solid #ef4444;max-height:360px;overflow:auto}.event-success h2{color:#15803d}"
            ".event-error{border-left:4px solid #dc2626;padding-left:12px}.event-error h2{color:#b91c1c}"
            "details.raw{margin-top:10px}details.raw>summary,details.transcript-detail>summary{"
            "cursor:pointer;color:#465b70}.event-meta{display:flex;flex-wrap:wrap;gap:6px 12px;"
            "margin:8px 0}.event-meta>div{display:flex;gap:4px;color:#475569;font-size:.92em}"
            ".event-meta dt{font-weight:600}.event-meta dt,.event-meta dd{margin:0}"
            "@media(max-width:600px){body{margin:12px auto;padding:0 12px;font-size:15px}.summary{padding:8px 10px}"
            ".transcript-detail{padding:8px}.tool-result,.tool-stderr{max-height:240px}}</style>",
            f"</head><body><header class='transcript-header'><h1>执行转录</h1><p class='session-id'>"
            f"Session ID：<code>{html.escape(_short_session_id(session_id))}</code></p></header>\n",
        ]
        if empty_message is not None:
            parts.append(f"<p>{html.escape(empty_message)}</p>\n")
        if entries:
            parts.extend(_render_summary(entries))
        for batch in _batches(entries):
            if batch.is_high_frequency:
                parts.extend(_render_batch(batch.entries))
            else:
                parts.extend(_render_entry(batch.entries[0]))
        parts.append("</body></html>")
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class _DiagnosticBatch:
    """One ordered display group; high-frequency groups stay collapsed by default."""

    entries: tuple[SessionLogEntry, ...]
    is_high_frequency: bool


def _event_type(entry: SessionLogEntry) -> str:
    return entry.event_type or "普通记录"


def _event_label(entry: SessionLogEntry) -> str:
    """Return a short Chinese heading without hiding the original event type."""
    event_type = _event_type(entry).casefold()
    if _entry_is_error(entry):
        return "运行错误"
    if "tool_result" in event_type:
        return "工具结果"
    if "tool_use" in event_type:
        return "工具调用"
    if event_type.endswith(".result") or event_type == "result":
        return "最终结果"
    if "error" in event_type or "failed" in event_type:
        return "运行错误"
    if "thinking" in event_type:
        return "思考诊断（内容已省略）"
    if "system" in event_type:
        return "SDK 系统事件"
    if "stream" in event_type:
        return "SDK 流事件"
    return entry.title


def _is_high_frequency(entry: SessionLogEntry) -> bool:
    """Classify noisy SDK diagnostics that are useful only as an ordered batch."""
    event_type = _event_type(entry).casefold()
    if event_type.endswith(".assistant") or event_type == "assistant":
        document = _document(entry)
        if document is None:
            return True
        return not any(
            document.get(key) for key in ("visible_text", "tool_input", "tool_result", "result")
        )
    return any(marker in event_type for marker in (".stream", ".system", "thinking", ".text_block"))


def _batches(entries: Iterable[SessionLogEntry]) -> list[_DiagnosticBatch]:
    """Keep source order while grouping only consecutive high-frequency diagnostics."""
    batches: list[_DiagnosticBatch] = []
    pending: list[SessionLogEntry] = []
    for entry in entries:
        if _is_high_frequency(entry):
            pending.append(entry)
            continue
        if pending:
            batches.append(_DiagnosticBatch(tuple(pending), True))
            pending = []
        batches.append(_DiagnosticBatch((entry,), False))
    if pending:
        batches.append(_DiagnosticBatch(tuple(pending), True))
    return batches


def _timestamp(entry: SessionLogEntry) -> str:
    return entry.created_at.astimezone().isoformat(timespec="seconds")


def _short_session_id(session_id: str) -> str:
    return session_id if len(session_id) <= 28 else f"{session_id[:14]}…{session_id[-10:]}"


def _render_summary(entries: list[SessionLogEntry]) -> list[str]:
    users = sum(entry.title == "用户消息" for entry in entries)
    assistants = sum(
        entry.title == "Claude Code 输出"
        or (
            _event_type(entry).casefold().endswith(".assistant")
            and isinstance((_document(entry) or {}).get("visible_text"), str)
            and bool((_document(entry) or {}).get("visible_text"))
        )
        for entry in entries
    )
    tools = sum("tool_use" in _event_type(entry).casefold() for entry in entries)
    errors = sum(_entry_is_error(entry) for entry in entries)
    start, end = _timestamp(entries[0]), _timestamp(entries[-1])
    return [
        "<div class='summary'><h2>本次执行</h2>",
        f"<p>时间范围：{html.escape(start)} 至 {html.escape(end)}</p>",
        f"<p class='summary-counts'>用户 {users} · Assistant {assistants} · 工具 {tools} · 错误 {errors}</p></div>\n",
    ]


def _render_metadata(entry: SessionLogEntry) -> list[str]:
    metadata = dict(entry.metadata)
    if entry.event_type is not None:
        # Event type, usage payloads, IDs and previews remain available in the
        # folded raw JSON.  The default transcript only keeps compact status
        # facts that help a human follow the execution.
        visible_keys = {
            "工具",
            "状态",
            "是否失败",
            "总耗时毫秒",
            "API 耗时毫秒",
        }
        if _entry_is_error(entry):
            visible_keys.add("工具结果摘要")
        metadata = {key: value for key, value in metadata.items() if key in visible_keys}
    if not metadata:
        return []
    parts = ["<dl class='event-meta'>\n"]
    for key, value in metadata.items():
        parts.append(
            f"<div><dt>{html.escape(str(key))}：</dt><dd>{html.escape(str(value))}</dd></div>\n"
        )
    parts.append("</dl>\n")
    return parts


def _render_raw(entry: SessionLogEntry) -> str:
    return (
        "<details class='raw'><summary>原始 JSON</summary>"
        f"<pre>{html.escape(entry.content)}</pre></details>\n"
    )


def _document(entry: SessionLogEntry) -> Mapping[str, object] | None:
    """Decode formatter JSON only for the always-visible transcript details."""
    try:
        value = json.loads(entry.content)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _entry_is_error(entry: SessionLogEntry) -> bool:
    document = _document(entry)
    if document is None:
        return False
    if document.get("is_error") is True:
        return True
    for key in ("subtype", "stop_reason"):
        status = document.get(key)
        if isinstance(status, str) and status.casefold() in {"error", "failed", "failure"}:
            return True
    result = document.get("tool_result")
    if not isinstance(result, Mapping):
        return False
    if result.get("is_error") is True:
        return True
    exit_code = result.get("exit_code")
    return (
        isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and exit_code != 0
    )


def _pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _pre(label: str, value: object, css_class: str) -> str:
    return (
        "<div class='transcript-detail'>"
        f"<h3>{html.escape(label)}</h3>"
        f"<pre class='{html.escape(css_class, quote=True)}'>{html.escape(_pretty(value))}</pre>"
        "</div>\n"
    )


def _detail(label: str, value: object, css_class: str) -> str:
    """Keep complete structured arguments available without crowding the transcript."""
    return (
        f"<details class='transcript-detail {html.escape(css_class, quote=True)}'>"
        f"<summary>{html.escape(label)}</summary><pre>{html.escape(_pretty(value))}</pre></details>\n"
    )


def _bash_command(tool_input: object) -> str | None:
    if not isinstance(tool_input, Mapping):
        return None
    for key in ("command", "cmd", "script"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _render_tool_transcript(document: Mapping[str, object]) -> list[str]:
    """Expose complete tool arguments and output before the optional raw event."""
    parts: list[str] = []
    tool_name = document.get("tool_name")
    tool_input = document.get("tool_input")
    if tool_input is not None:
        if isinstance(tool_name, str) and tool_name.casefold() == "bash":
            command = _bash_command(tool_input)
            if command is not None:
                parts.extend(
                    [
                        "<div class='transcript-detail'><h3>Bash 命令</h3>",
                        f"<pre class='bash-command'>{html.escape(command)}</pre></div>\n",
                    ]
                )
        parts.append(_detail("工具输入（完整）— 展开查看", tool_input, "tool-input"))

    tool_result = document.get("tool_result")
    if tool_result is not None:
        parts.append(_pre("工具结果（完整）", tool_result, "tool-result"))
        if isinstance(tool_result, Mapping):
            for key, label, css_class in (
                ("stdout", "stdout（完整）", "tool-result"),
                ("stderr", "stderr（完整）", "tool-stderr"),
            ):
                if key in tool_result:
                    parts.append(_pre(label, tool_result[key], css_class))
    return parts


def _render_result_transcript(entry: SessionLogEntry, document: Mapping[str, object]) -> list[str]:
    """Make terminal result, usage, and duration useful without opening raw JSON."""
    event_type = _event_type(entry).casefold()
    if not (event_type == "result" or event_type.endswith(".result")):
        return []
    result: dict[str, object] = {}
    for key in ("result", "subtype", "stop_reason", "usage", "duration_ms", "duration_api_ms"):
        if key in document and document[key] is not None:
            result[key] = document[key]
    return [_pre("最终 result / 用量 / 耗时", result, "tool-result")] if result else []


def _render_visible_text(document: Mapping[str, object]) -> list[str]:
    visible_text = document.get("visible_text")
    if not isinstance(visible_text, str) or not visible_text:
        return []
    return [
        "<div class='transcript-detail'><h3>Claude Code 可见输出</h3>",
        f"<pre>{html.escape(visible_text)}</pre></div>\n",
    ]


def _render_transcript_details(entry: SessionLogEntry) -> list[str]:
    document = _document(entry)
    if document is None:
        return []
    return [
        *_render_visible_text(document),
        *_render_tool_transcript(document),
        *_render_result_transcript(entry, document),
    ]


def _entry_heading(entry: SessionLogEntry) -> str:
    """Keep the stored event name available beside its readable Chinese label."""
    label = _event_label(entry)
    document = _document(entry)
    tool_name = document.get("tool_name") if document is not None else None
    if not isinstance(tool_name, str):
        tool_name = (
            entry.metadata.get("工具") if isinstance(entry.metadata.get("工具"), str) else None
        )
    if tool_name and "tool_use" in _event_type(entry).casefold():
        return f"工具调用 · {tool_name}"
    if tool_name and "tool_result" in _event_type(entry).casefold():
        return f"工具结果 · {tool_name}"
    if (
        _event_type(entry).casefold().endswith(".assistant")
        and document is not None
        and isinstance(document.get("visible_text"), str)
        and document["visible_text"]
    ):
        return "Assistant 输出"
    return label


def _render_legacy_turn(entry: SessionLogEntry) -> list[str]:
    if entry.title == "用户消息":
        title, css_class = "用户输入", "turn-user"
    elif entry.title == "Claude Code 输出":
        title, css_class = "Assistant 输出", "turn-assistant"
    else:
        title, css_class = entry.title, ""
    return [
        f"<section><div class='turn {css_class}'><h2>#{entry.sequence} {html.escape(title)}</h2>\n",
        f"<p><strong>时间：</strong>{html.escape(_timestamp(entry))}</p>\n",
        *_render_metadata(entry),
        f"<pre>{html.escape(entry.content)}</pre></div></section>\n",
    ]


def _render_entry(entry: SessionLogEntry) -> list[str]:
    if entry.event_type is None:
        return _render_legacy_turn(entry)
    semantic_class = (
        "event-error"
        if _entry_is_error(entry)
        else "event-success"
        if (
            _event_type(entry).casefold().endswith(".result")
            or _event_type(entry).casefold() == "result"
        )
        else ""
    )
    return [
        f"<section class='{semantic_class}'><h2>#{entry.sequence} {html.escape(_entry_heading(entry))}</h2>\n",
        f"<p><strong>时间：</strong>{html.escape(_timestamp(entry))}</p>\n",
        *_render_metadata(entry),
        *_render_transcript_details(entry),
        _render_raw(entry),
        "</section>\n",
    ]


def _render_batch(entries: tuple[SessionLogEntry, ...]) -> list[str]:
    first, last = entries[0], entries[-1]
    count = len(entries)
    kinds = Counter(_event_label(entry) for entry in entries)
    labels = "、".join(f"{label} {number}" for label, number in kinds.items())
    parts = [
        "<details class='diagnostic-batch'><summary>",
        f"高频 SDK 诊断批次：{count} 条（#{first.sequence}–#{last.sequence}，",
        f"{html.escape(_timestamp(first))} 至 {html.escape(_timestamp(last))}）",
        "</summary>\n",
        f"<p>类型：{html.escape(labels)}</p>\n",
        "<pre>",
    ]
    for entry in entries:
        parts.extend(
            [
                f"#{entry.sequence} {_timestamp(entry)} {_event_type(entry)}"
                f"（{_entry_heading(entry)}）\n",
                f"{html.escape(entry.content)}\n\n",
            ]
        )
    parts.append("</pre></details>\n")
    return parts
