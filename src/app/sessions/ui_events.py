"""应用级浏览器事件日志队列。

WebSocket 发送不能等待 SQLite: 一个进程只维护一个 FIFO writer, 所有连接只做
O(1) 的 ``accept``. 未落库的队头始终保留在内存, 失败后按退避重试; 读取 history
时可把这段 pending suffix 覆盖在数据库已持久 prefix 之后。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from .active_turns import ActiveTurn, TurnIntent
from .repository import SessionRepository, SessionUiEventConflictError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UiEventKey:
    session_id: str
    turn_id: str
    sequence: int


def ui_event_key(event: Mapping[str, object]) -> UiEventKey:
    session_id = event.get("session_id")
    turn_id = event.get("turn_id")
    sequence = event.get("sequence")
    if not isinstance(session_id, str) or not isinstance(turn_id, str):
        raise ValueError("turn event is missing a session or turn identifier")
    if not isinstance(sequence, int):
        raise ValueError("turn event is missing a sequence")
    return UiEventKey(session_id, turn_id, sequence)


class UiEventJournal:
    """单进程、单 writer 的有序 UI event journal。"""

    _retry_initial_seconds = 0.02
    _retry_max_seconds = 0.25
    _drain_timeout_seconds = 1.25

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository
        self._pending: deque[dict[str, object]] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._changed = asyncio.Event()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False
        self._accepted: dict[UiEventKey, str] = {}
        self._fatal_conflicts: list[UiEventKey] = []

    async def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._run(), name="ui-event-journal")

    def accept(self, event: Mapping[str, object]) -> UiEventKey:
        """接收已派发事件, 不等待任何 I/O."""
        if self._closed:
            raise RuntimeError("UI event journal is closed")
        key = ui_event_key(event)
        at = event.get("at")
        if not isinstance(at, str) or not at:
            raise ValueError("turn event is missing a timestamp")
        copied = copy.deepcopy(dict(event))
        canonical = json.dumps(copied, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        existing = self._accepted.get(key)
        if existing is not None:
            if existing != canonical:
                raise SessionUiEventConflictError(
                    "UI event journal received conflicting duplicate event"
                )
            return key
        self._accepted[key] = canonical
        self._pending.append(copied)
        self._idle.clear()
        self._wake.set()
        return key

    def snapshot(self, session_id: str) -> list[dict[str, object]]:
        """返回该会话尚未落库的 FIFO suffix, 不等待 writer."""
        return [
            copy.deepcopy(event) for event in self._pending if event.get("session_id") == session_id
        ]

    async def list_events(self, session_id: str) -> list[dict[str, object]]:
        """Return the durable prefix plus the accepted in-memory suffix.

        The pending snapshot is deliberately taken before the SQLite read.  An
        event accepted while SQLite is being read is either visible in that DB
        snapshot or remains pending; a WebSocket that subscribed first also has
        it in its live queue.  Callers de-duplicate with the immutable event key.
        """
        pending = self.snapshot(session_id)
        persisted = await self._repository.list_ui_events(session_id)
        events: list[dict[str, object]] = []
        seen: set[UiEventKey] = set()
        for entry in persisted:
            event = dict(entry.event)
            key = ui_event_key(event)
            if key not in seen:
                seen.add(key)
                events.append(event)
        for event in pending:
            key = ui_event_key(event)
            if key not in seen:
                seen.add(key)
                events.append(event)
        return events

    def has_terminal(self, turn_id: str) -> bool:
        return any(
            event.get("type") == "done" and event.get("turn_id") == turn_id
            for event in self._pending
        )

    @property
    def fatal_conflicts(self) -> tuple[UiEventKey, ...]:
        """测试/诊断钩子: 已隔离的永久 SQLite 冲突键。"""
        return tuple(self._fatal_conflicts)

    async def wait_idle(self, *, timeout: float | None = None) -> bool:
        """测试/关闭钩子: 等待当前 pending 队列排空."""
        try:
            if timeout is None:
                await self._idle.wait()
            else:
                await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def wait_turn_durable(
        self, session_id: str, turn_id: str, *, timeout: float | None = None
    ) -> bool:
        """测试钩子: 等待某一 turn 不再位于 journal 的 pending 队列."""

        async def wait() -> None:
            while True:
                if not any(
                    event.get("session_id") == session_id and event.get("turn_id") == turn_id
                    for event in self._pending
                ):
                    return
                self._changed.clear()
                if not any(
                    event.get("session_id") == session_id and event.get("turn_id") == turn_id
                    for event in self._pending
                ):
                    return
                await self._changed.wait()

        try:
            if timeout is None:
                await wait()
            else:
                await asyncio.wait_for(wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def drain(self, *, timeout: float | None = None) -> bool:
        return await self.wait_idle(
            timeout=self._drain_timeout_seconds if timeout is None else timeout
        )

    async def close(self) -> None:
        """关闭阶段才取消长期 writer; 先给已接收事件一个有界 drain 机会."""
        if self._closed:
            return
        self._closed = True
        await self.drain()
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        retry_seconds = self._retry_initial_seconds
        while True:
            if not self._pending:
                self._idle.set()
                self._wake.clear()
                if not self._pending:
                    await self._wake.wait()
                retry_seconds = self._retry_initial_seconds
                continue
            event = self._pending[0]
            try:
                key = ui_event_key(event)
                at = event["at"]
                assert isinstance(at, str)
                await self._repository.append_ui_event(
                    key.session_id,
                    turn_id=key.turn_id,
                    sequence=key.sequence,
                    at=at,
                    event=event,
                )
            except asyncio.CancelledError:
                raise
            except SessionUiEventConflictError:
                key = ui_event_key(event)
                # 与已有 SQLite 历史冲突不会自行恢复. 隔离这个单项并继续后项,
                # 避免一个旧/损坏 turn 阻塞同一 Demo 中所有其它 Session.
                logger.critical(
                    "Permanent browser UI event conflict isolated session_id=%s turn_id=%s sequence=%s",
                    key.session_id,
                    key.turn_id,
                    key.sequence,
                )
                self._pending.popleft()
                self._fatal_conflicts.append(key)
                self._changed.set()
                retry_seconds = self._retry_initial_seconds
                continue
            except Exception:
                # 绝不记录 event 内容: 它可含用户文本. 队头不出队, 后项不能越过它.
                logger.warning(
                    "Could not persist browser UI event session_id=%s turn_id=%s sequence=%s",
                    event.get("session_id"),
                    event.get("turn_id"),
                    event.get("sequence"),
                )
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(self._retry_max_seconds, retry_seconds * 2)
                continue
            self._pending.popleft()
            # Once SQLite has accepted a canonical event, its unique key is
            # the durable idempotence/conflict authority.  Keeping every
            # successful payload here would grow forever; a later duplicate is
            # safely rechecked by SQLite (same payload idempotent, different
            # payload a permanent conflict).
            self._accepted.pop(key, None)
            self._changed.set()
            retry_seconds = self._retry_initial_seconds


class ActiveTurnBusyError(RuntimeError):
    """The session already has an application-owned turn."""


class ActiveTurnNotRunningError(RuntimeError):
    """The requested session/turn pair is not currently cancellable."""


@dataclass(eq=False, slots=True)
class TurnSubscription:
    """One detachable, bounded live event feed for a single session."""

    session_id: str
    queue: asyncio.Queue[dict[str, object] | None]
    closed: bool = False

    def offer(self, event: Mapping[str, object]) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(copy.deepcopy(dict(event)))
            return True
        except asyncio.QueueFull:
            # A slow browser must not retain a turn or block other subscribers.
            # Drop one queued item only to make room for the close sentinel; the
            # complete stream is recoverable from the journal on reconnect.
            self.closed = True
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - QueueFull proved otherwise.
                pass
            self.queue.put_nowait(None)
            return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            self.queue.get_nowait()
            self.queue.put_nowait(None)


class ActiveTurnRegistry:
    """Application-scope owner, dispatcher, and fan-out hub for session turns."""

    def __init__(self, journal: UiEventJournal, *, subscriber_queue_size: int = 1024) -> None:
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber_queue_size must be positive")
        self._journal = journal
        self._subscriber_queue_size = subscriber_queue_size
        self._by_session: dict[str, ActiveTurn] = {}
        self._starting: dict[str, str] = {}
        self._control_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._subscribers: dict[str, dict[int, TurnSubscription]] = {}
        self._cleanup_tasks: dict[int, asyncio.Task[None]] = {}
        self._changed = asyncio.Event()
        self._closing = False

    @asynccontextmanager
    async def _control(self, session_id: str) -> AsyncIterator[None]:
        entry = self._control_locks.get(session_id)
        if entry is None:
            lock = asyncio.Lock()
            users = 0
        else:
            lock, users = entry
        self._control_locks[session_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current = self._control_locks.get(session_id)
            if current is not None and current[0] is lock:
                remaining = current[1] - 1
                if remaining == 0:
                    self._control_locks.pop(session_id, None)
                else:
                    self._control_locks[session_id] = (lock, remaining)

    def subscribe(self, session_id: str) -> TurnSubscription:
        """Attach before reading history so concurrently accepted events are buffered."""
        if self._closing:
            raise RuntimeError("active turn registry is closing")
        subscription = TurnSubscription(
            session_id, asyncio.Queue(maxsize=self._subscriber_queue_size)
        )
        self._subscribers.setdefault(session_id, {})[id(subscription)] = subscription
        return subscription

    def unsubscribe(self, subscription: TurnSubscription) -> None:
        values = self._subscribers.get(subscription.session_id)
        if values is not None:
            values.pop(id(subscription), None)
            if not values:
                self._subscribers.pop(subscription.session_id, None)
        subscription.close()

    def _broadcast(self, event: Mapping[str, object]) -> None:
        session_id = event.get("session_id")
        if not isinstance(session_id, str):  # pragma: no cover - dispatch constructs it.
            raise ValueError("turn event is missing session_id")
        values = self._subscribers.get(session_id)
        if not values:
            return
        for key, subscription in tuple(values.items()):
            if not subscription.offer(event):
                values.pop(key, None)
        if not values:
            self._subscribers.pop(session_id, None)

    def _publish(self, event: Mapping[str, object]) -> dict[str, object]:
        copied = copy.deepcopy(dict(event))
        # Journal acceptance is the protocol commit point.  Socket fan-out is
        # best effort and can never cancel or delay the application-owned turn.
        self._journal.accept(copied)
        self._broadcast(copied)
        return copied

    def dispatch(self, turn: ActiveTurn, intent: TurnIntent) -> dict[str, object] | None:
        """Allocate sequence once, commit to the journal, then fan out."""
        if self._by_session.get(turn.session_id) is not turn:
            return None
        event = turn.dispatch(intent, at=datetime.now(UTC).isoformat())
        return self._publish(event) if event is not None else None

    async def start_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        content: str,
        stream_factory: Callable[[], Awaitable[AsyncIterator[object]]],
        producer: Callable[[ActiveTurn], Awaitable[None]],
    ) -> ActiveTurn:
        """Atomically reserve one session, create its stream, and launch its runner."""
        async with self._control(session_id):
            if self._closing:
                raise RuntimeError("active turn registry is closing")
            self._starting[session_id] = turn_id
            try:
                current = self._by_session.get(session_id)
                if current is not None:
                    if not current.terminal_dispatched:
                        raise ActiveTurnBusyError("This session already has an active turn")
                    await current.wait_closed()
                    if self._by_session.get(session_id) is current:
                        self._by_session.pop(session_id, None)

                stream = await stream_factory()
                turn = ActiveTurn(session_id, turn_id, stream)
                self._by_session[session_id] = turn
                user_event: dict[str, object] = {
                    "type": "user_message",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "sequence": 0,
                    "at": datetime.now(UTC).isoformat(),
                    "content": content,
                }
                self._publish(user_event)
                started = self.dispatch(turn, turn.intent("turn_started"))
                assert started is not None
                producer_started = asyncio.Event()

                async def run_producer() -> None:
                    producer_started.set()
                    await producer(turn)

                turn.task = asyncio.create_task(
                    run_producer(), name=f"session-turn-{session_id}-{turn_id}"
                )
                self._supervise_release(turn)
                # Do not expose a cancellable task that has never entered the
                # guarded SessionService generator: cancelling such a task can
                # skip the generator's finally and strand its pre-acquired lock.
                await producer_started.wait()
                self._changed.set()
                return turn
            except BaseException:
                active = self._by_session.get(session_id)
                if active is not None and active.turn_id == turn_id and active.task is None:
                    self._by_session.pop(session_id, None)
                    await active.stream.aclose()
                raise
            finally:
                self._starting.pop(session_id, None)
                self._changed.set()

    def _supervise_release(self, turn: ActiveTurn) -> None:
        key = id(turn)
        if key in self._cleanup_tasks:
            return

        async def release() -> None:
            try:
                await turn.wait_closed()
            except Exception:
                logger.exception("Turn cleanup failed turn_id=%s", turn.turn_id)
            finally:
                if self._by_session.get(turn.session_id) is turn:
                    self._by_session.pop(turn.session_id, None)
                self._changed.set()

        async def wait_for_producer() -> None:
            if turn.task is not None:
                try:
                    await asyncio.shield(turn.task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            await release()

        task = asyncio.create_task(wait_for_producer(), name=f"turn-release-{turn.turn_id}")
        self._cleanup_tasks[key] = task

        def done(completed: asyncio.Task[None]) -> None:
            self._cleanup_tasks.pop(key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                logger.warning("Turn release cancelled turn_id=%s", turn.turn_id)
            except Exception:
                logger.exception("Turn release failed turn_id=%s", turn.turn_id)

        task.add_done_callback(done)

    async def stop_turn(self, session_id: str, turn_id: str) -> dict[str, object]:
        """Cancel a background turn by global session/turn identity."""
        async with self._control(session_id):
            turn = self._by_session.get(session_id)
            if turn is None or turn.turn_id != turn_id or turn.terminal_dispatched:
                raise ActiveTurnNotRunningError("The requested turn is not running")
            # There is deliberately no await between cancellation request and
            # terminal selection.  On this event loop stop and natural done are
            # serialized by ActiveTurn's first-wins dispatch gate.
            turn.request_cancel()
            event = self.dispatch(
                turn,
                turn.intent("done", completed=False, stop_reason="stopped", usage=None),
            )
            if event is None:  # pragma: no cover - guarded above on one event loop.
                raise ActiveTurnNotRunningError("The requested turn is not running")
            return event

    def snapshot(self, session_id: str) -> dict[str, object]:
        turn = self._by_session.get(session_id)
        if turn is not None:
            return {
                "task_state": "finishing" if turn.terminal_dispatched else "running",
                "turn_id": turn.turn_id,
                "last_sequence": turn.sequence,
            }
        starting = self._starting.get(session_id)
        if starting is not None:
            return {"task_state": "starting", "turn_id": starting, "last_sequence": 0}
        return {"task_state": "idle", "turn_id": None, "last_sequence": 0}

    async def wait_turn_released(self, turn_id: str, *, timeout: float | None = None) -> bool:
        """Test hook: wait until cleanup has released the turn and Session lock."""

        async def wait() -> None:
            while any(turn.turn_id == turn_id for turn in self._by_session.values()):
                self._changed.clear()
                if not any(turn.turn_id == turn_id for turn in self._by_session.values()):
                    return
                await self._changed.wait()

        try:
            if timeout is None:
                await wait()
            else:
                await asyncio.wait_for(wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def close_all(self) -> None:
        """Abort application-owned work only for application shutdown."""
        self._closing = True
        # A start that passed the closing check already owns (or is about to
        # own) a SessionService stream. Let it install and launch its producer
        # before taking the shutdown snapshot; closing an unstarted guarded
        # async generator would not reliably execute its lock-release finally.
        while self._starting:
            self._changed.clear()
            if not self._starting:
                break
            await self._changed.wait()
        turns = tuple(self._by_session.values())
        for turn in turns:
            if not turn.terminal_dispatched:
                turn.request_cancel()
                self.dispatch(
                    turn,
                    turn.intent("done", completed=False, stop_reason="aborted", usage=None),
                )
        await asyncio.gather(*(turn.wait_closed() for turn in turns), return_exceptions=True)
        for turn in turns:
            if self._by_session.get(turn.session_id) is turn:
                self._by_session.pop(turn.session_id, None)
        tasks = tuple(self._cleanup_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for values in tuple(self._subscribers.values()):
            for subscription in tuple(values.values()):
                subscription.close()
        self._subscribers.clear()
        self._changed.set()
