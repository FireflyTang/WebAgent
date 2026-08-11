"""Application-owned lifecycle state for one cancellable session turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TurnIntent:
    """尚未派发的 turn 事件; sequence 只能在 dispatch 边界生成."""

    kind: str
    payload: dict[str, object]


@dataclass(slots=True)
class ActiveTurn:
    """Owns the producer and service stream independently of any WebSocket."""

    session_id: str
    turn_id: str
    stream: AsyncIterator[Any]
    sequence: int = 0
    task: asyncio.Task[None] | None = None
    terminal_dispatched: bool = False
    cancel_requested: bool = False
    closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def intent(self, kind: str, **payload: object) -> TurnIntent:
        """构造不带 sequence 的 producer intent。"""
        return TurnIntent(kind, dict(payload))

    def dispatch(self, intent: TurnIntent, *, at: str) -> dict[str, object] | None:
        """把 intent 变为可发送/可入 journal 的唯一编号事件。"""
        # A terminal is the final protocol event, not merely the first ``done``.
        # This also drops an intent that a burst producer had already prepared
        # when a concurrent stop selected the terminal state.
        if self.terminal_dispatched:
            return None
        if intent.kind == "done":
            self.terminal_dispatched = True
        self.sequence += 1
        return {
            "type": intent.kind,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "at": at,
            **intent.payload,
        }

    def terminal(self, *, at: str, **payload: object) -> dict[str, object] | None:
        """first-wins 的终态派发; normal/failed/stopped/aborted 共用此门."""
        return self.dispatch(self.intent("done", **payload), at=at)

    def request_cancel(self) -> bool:
        """只请求一次 producer 取消, 不等待 stream cleanup."""
        if self.cancel_requested:
            return False
        self.cancel_requested = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
        return True

    async def wait_closed(self) -> None:
        """只 join producer, 不发取消; producer finally 是 stream close 的唯一 owner."""
        async with self._close_lock:
            if self.closed:
                return
            if self.task is not None:
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            elif self.task is None:
                # 创建后、producer task 尚未登记前的极窄窗口没有其他 owner。
                await self.stream.aclose()
            # task 完成时 _produce_turn 的 finally 已完成 stream.aclose()。
            self.closed = True

    async def cancel_and_wait(self) -> None:
        """取消并等待 producer/stream 完整结束。"""
        self.request_cancel()
        await self.wait_closed()
