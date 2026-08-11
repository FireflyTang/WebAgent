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
            f"<title>Session {html.escape(session_id)}</title>",
            "<style>body{font:16px/1.55 sans-serif;max-width:1100px;margin:24px auto;"
            "padding:0 18px;color:#222}section{border-top:2px solid #bbb;padding:16px 0}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:12px}"
            "dt{font-weight:bold;float:left;clear:left;margin-right:8px}dd{margin:2px 0}"
            ".summary{background:#f5f7fa;border:1px solid #dbe3eb;padding:12px 16px;"
            "margin:12px 0 20px;border-radius:8px}.summary p{margin:4px 0}"
            ".event-meta{font-size:.92em;color:#444}.event-meta code{font-size:.9em}"
            ".diagnostic-batch{border:1px solid #d9dfe6;border-radius:8px;padding:0 12px;"
            "margin:14px 0;background:#fafcff}.diagnostic-batch>summary{padding:10px 0;"
            "cursor:pointer;font-weight:bold}"
            ".transcript-detail{margin:12px 0;padding:10px 12px;background:#f8fafc;"
            "border-left:4px solid #7c98b8}.transcript-detail h3,.transcript-detail h4{"
            "margin:0 0 8px}.bash-command{background:#1e293b;color:#f8fafc;"
            "border:2px solid #f59e0b}.tool-input{border-left:4px solid #3b82f6}.tool-result{"
            "border-left:4px solid #22c55e}.tool-stderr{border-left:4px solid #ef4444}"
            "details.raw{margin-top:10px}details.raw>summary{cursor:pointer;color:#465b70}</style>",
            f"</head><body><h1>Session：{html.escape(session_id)}</h1>\n",
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
    outcome = str(entry.metadata.get("结果", "")).casefold()
    if "error" in outcome or "failed" in outcome:
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


def _render_summary(entries: list[SessionLogEntry]) -> list[str]:
    counts = Counter(_event_label(entry) for entry in entries)
    kinds = "、".join(f"{label} {count}" for label, count in counts.items())
    start, end = _timestamp(entries[0]), _timestamp(entries[-1])
    return [
        "<div class='summary'><h2>诊断摘要</h2>",
        f"<p>诊断事件：{len(entries)} 条</p>",
        f"<p>时间范围：{html.escape(start)} 至 {html.escape(end)}</p>",
        f"<p>关键类型：{html.escape(kinds)}</p></div>\n",
    ]


def _render_metadata(entry: SessionLogEntry) -> list[str]:
    metadata = dict(entry.metadata)
    if entry.event_type is not None:
        metadata.setdefault("事件类型", entry.event_type)
    if not metadata:
        return []
    parts = ["<dl class='event-meta'>\n"]
    for key, value in metadata.items():
        parts.append(f"<dt>{html.escape(str(key))}：</dt><dd>{html.escape(str(value))}</dd>\n")
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


def _pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _pre(label: str, value: object, css_class: str) -> str:
    return (
        "<div class='transcript-detail'>"
        f"<h3>{html.escape(label)}</h3>"
        f"<pre class='{html.escape(css_class, quote=True)}'>{html.escape(_pretty(value))}</pre>"
        "</div>\n"
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
        parts.append(_pre("工具输入（完整）", tool_input, "tool-input"))

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
    return label if entry.title == label else f"{entry.title}（{label}）"


def _render_entry(entry: SessionLogEntry) -> list[str]:
    if entry.event_type is None:
        return [
            f"<section><h2>{html.escape(entry.title)}</h2>\n",
            f"<p><strong>时间：</strong>{html.escape(_timestamp(entry))}</p>\n",
            *_render_metadata(entry),
            f"<pre>{html.escape(entry.content)}</pre></section>\n",
        ]
    return [
        f"<section><h2>#{entry.sequence} {html.escape(_entry_heading(entry))}</h2>\n",
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
