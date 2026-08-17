"""Readable, filtered runtime diagnostics for a session HTML log."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .html_log import SessionHtmlLogger
from .repository import SessionLogEntry

_THINKING_KEYS = frozenset({"thinking", "thinking_delta", "signature"})
_OMITTED_THINKING = "[已省略 thinking 内容]"
_OMITTED_CREDENTIAL = "[已省略凭据]"
_OMITTED_ENVIRONMENT = "[已省略运行环境]"
_OMITTED_PROVIDER = "[已省略 Provider 配置]"
_RESULT_PREVIEW_LIMIT = 180
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "anthropic_api_key",
        "anthropic_auth_token",
        "x_api_key",
        "token",
        "secret",
        "password",
    }
)
_COOKIE_KEYS = frozenset({"cookie", "set_cookie"})
_ENVIRONMENT_KEYS = frozenset({"env", "environment", "environment_variables"})
_PROVIDER_KEYS = frozenset({"provider", "provider_config", "model_provider"})
_CONTENT_SECRET_KEYS = frozenset({"command", "stdout", "stderr", "visible_text", "result"})
_REDACTED_INLINE_SECRET = "[已省略凭据]"
_INLINE_ASSIGNMENT = re.compile(
    r"(?im)\b(ANTHROPIC_API_KEY|API_KEY|X_API_KEY|X-API-KEY|TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|SECRET|PASSWORD)\b"
    r"(\s*(?:=|:)\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s]+)"
)
_AUTHORIZATION = re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+")
_URL_BASIC_AUTH = re.compile(r"(?i)(https?://)[^\s/@]+@")
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api_key|key|token|access_token|refresh_token|secret|password)=)[^&#\s]+"
)
_COOKIE_HEADER = re.compile(r"(?im)^\s*((?:Set-)?Cookie)\s*:\s*[^\r\n]*")


def _mask_inline_secrets(value: str) -> str:
    """Mask credential syntax in text fields, without trying to classify all text."""
    value = _AUTHORIZATION.sub(rf"\1{_REDACTED_INLINE_SECRET}", value)
    value = _INLINE_ASSIGNMENT.sub(rf"\1\2{_REDACTED_INLINE_SECRET}", value)
    value = _URL_BASIC_AUTH.sub(rf"\1{_REDACTED_INLINE_SECRET}@", value)
    value = _URL_QUERY_SECRET.sub(rf"\1{_REDACTED_INLINE_SECRET}", value)
    return _COOKIE_HEADER.sub(rf"\1: {_REDACTED_INLINE_SECRET}", value)


def _safe_tool_result(value: object) -> object:
    """Mask text values in the SDK's actual string-or-list tool-result shape."""
    if isinstance(value, str):
        return _mask_inline_secrets(value)
    if isinstance(value, Mapping):
        return {
            str(name): (
                _mask_inline_secrets(item)
                if str(name).lower() in {"text", "stdout", "stderr"} and isinstance(item, str)
                else _safe_value(item, key=str(name))
            )
            for name, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_tool_result(item) for item in value]
    return _safe_value(value)


@dataclass(frozen=True, slots=True)
class RuntimeDebugEntry:
    """A rendered diagnostic section suitable for :class:`SessionHtmlLogger`."""

    title: str
    content: str
    metadata: dict[str, object]


def _safe_value(value: object, *, key: str | None = None) -> object:
    """Convert SDK-shaped values to transcript data without private runtime state."""
    if key is not None:
        normalized_key = key.lower().replace("-", "_")
        if normalized_key == "tool_result":
            return _safe_tool_result(value)
        if normalized_key in _THINKING_KEYS:
            return _OMITTED_THINKING
        if normalized_key in _CREDENTIAL_KEYS or normalized_key.endswith(
            ("_api_key", "_auth_token", "_access_token")
        ):
            return _OMITTED_CREDENTIAL
        if normalized_key in _COOKIE_KEYS:
            return _OMITTED_CREDENTIAL
        if normalized_key in _ENVIRONMENT_KEYS:
            return _OMITTED_ENVIRONMENT
        if normalized_key in _PROVIDER_KEYS:
            return _OMITTED_PROVIDER
        if normalized_key in _CONTENT_SECRET_KEYS and isinstance(value, str):
            return _mask_inline_secrets(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _safe_value(dataclasses.asdict(value), key=key)
    if isinstance(value, Mapping):
        return {str(name): _safe_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _field(payload: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def _tool_result_summary(document: Mapping[str, object]) -> str | None:
    """Return a compact, user-readable summary without duplicating raw output.

    The Agent SDK's :class:`Diagnostic` shape places tool output below
    ``tool_result``.  Preserve the complete value in the raw JSON, while the
    always-visible metadata surfaces terminal state first.
    """

    value = document.get("tool_result")
    nested = value if isinstance(value, Mapping) else {}

    def status(name: str) -> object | None:
        direct = document.get(name)
        return direct if direct is not None else nested.get(name)

    is_error = status("is_error")
    interrupted = status("interrupted")
    exit_code = status("exit_code")
    if is_error is True:
        return "失败"
    if interrupted is True:
        return "已中断"
    if isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool):
        rendered_code = str(exit_code)
        return (
            f"成功（退出码 {rendered_code}）"
            if exit_code == 0
            else f"失败（退出码 {rendered_code}）"
        )
    if isinstance(value, str):
        compact = " ".join(value.split())
        if len(compact) > _RESULT_PREVIEW_LIMIT:
            compact = f"{compact[:_RESULT_PREVIEW_LIMIT]}…"
        return f"文本 {len(value)} 字符：{compact}" if compact else f"文本 {len(value)} 字符"
    if isinstance(value, Mapping):
        keys = [str(key) for key in value]
        preview = "、".join(keys[:4])
        suffix = "…" if len(keys) > 4 else ""
        return f"对象（{len(keys)} 个字段{f'：{preview}{suffix}' if preview else ''}）"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"列表（{len(value)} 项）"
    return None


def format_runtime_debug(event_type: str, payload: object) -> RuntimeDebugEntry:
    """Format one raw provider/runner event for the durable session debug view.

    ``event_type`` is intentionally caller-supplied: callers may use stable
    names such as ``sdk.stream_event`` or ``runner.progress`` even when the
    raw payload has no type field.  The complete non-thinking payload remains
    available as indented JSON in the section's ``pre`` body.
    """
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event_type must be a non-empty string")
    rendered = _safe_value(payload)
    document = rendered if isinstance(rendered, Mapping) else {"value": rendered}
    metadata: dict[str, object] = {"事件类型": event_type}
    tool = _field(document, "tool_name", "name")
    if tool is not None:
        metadata["工具"] = tool
    tool_use_id = _field(document, "tool_use_id", "id")
    if tool_use_id is not None:
        metadata["工具调用"] = tool_use_id
    usage = _field(document, "usage")
    if usage is not None:
        metadata["用量"] = json.dumps(usage, ensure_ascii=False, sort_keys=True)
    duration_ms = _field(document, "duration_ms")
    if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
        metadata["总耗时毫秒"] = duration_ms
    duration_api_ms = _field(document, "duration_api_ms")
    if isinstance(duration_api_ms, int) and not isinstance(duration_api_ms, bool):
        metadata["API 耗时毫秒"] = duration_api_ms
    status = _field(document, "subtype", "stop_reason")
    if status is not None:
        metadata["状态"] = status
    is_error = _field(document, "is_error")
    if isinstance(is_error, bool):
        metadata["是否失败"] = is_error
    visible_text = _field(document, "visible_text")
    if isinstance(visible_text, str) and visible_text:
        metadata["可见文本长度"] = len(visible_text)
    tool_result_summary = _tool_result_summary(document)
    if tool_result_summary is not None:
        metadata["工具结果摘要"] = tool_result_summary
    if _contains_thinking(rendered):
        metadata["thinking"] = "已省略"
    return RuntimeDebugEntry(
        title=f"运行时诊断：{event_type}",
        content=json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        metadata=metadata,
    )


def _contains_thinking(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _THINKING_KEYS or _contains_thinking(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_thinking(item) for item in value)
    return False


async def append_runtime_debug(
    logger: SessionHtmlLogger,
    session_id: str,
    event_type: str,
    payload: object,
) -> SessionLogEntry:
    """Store one diagnostic event for on-demand HTML rendering from SQLite."""
    entry = format_runtime_debug(event_type, payload)
    return await logger.append(
        session_id,
        title=entry.title,
        content=entry.content,
        metadata=entry.metadata,
        event_type=event_type,
    )
