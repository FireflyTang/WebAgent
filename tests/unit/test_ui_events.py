from __future__ import annotations

import asyncio

import pytest

from app.sessions.active_turns import ActiveTurn
from app.sessions.repository import (
    SessionRepositoryError,
    SessionUiEventConflictError,
    SQLiteSessionRepository,
)
from app.sessions.ui_events import (
    ActiveTurnNotRunningError,
    ActiveTurnRegistry,
    UiEventJournal,
)


def _event(sequence: int, *, content: str = "event") -> dict[str, object]:
    return {
        "type": "progress",
        "session_id": "session",
        "turn_id": "turn",
        "sequence": sequence,
        "at": f"2026-01-01T00:00:0{sequence}+00:00",
        "content": content,
    }


class _RecordingRepository:
    def __init__(self, *, failures: int = 0, gate: asyncio.Event | None = None) -> None:
        self.failures = failures
        self.gate = gate
        self.started = asyncio.Event()
        self.events: list[dict[str, object]] = []

    async def append_ui_event(self, session_id: str, **kwargs: object) -> None:
        assert session_id == "session"
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary sqlite failure")
        event = kwargs["event"]
        assert isinstance(event, dict)
        self.events.append(dict(event))


@pytest.mark.asyncio
async def test_journal_retries_fifo_and_snapshot_is_complete_before_db_recovers() -> None:
    repository = _RecordingRepository(failures=3)
    journal = UiEventJournal(repository)  # type: ignore[arg-type]
    await journal.start()
    journal.accept(_event(1))
    journal.accept(_event(2))

    # 前 N 次 SQLite 失败时 history overlay 仍有完整 FIFO suffix, 不能等待 writer.
    assert [event["sequence"] for event in journal.snapshot("session")] == [1, 2]
    assert await journal.wait_idle(timeout=1)
    assert [event["sequence"] for event in repository.events] == [1, 2]
    assert journal.snapshot("session") == []
    await journal.close()


@pytest.mark.asyncio
async def test_journal_diagnostics_track_oldest_failure_and_success_without_payload() -> None:
    repository = _RecordingRepository(failures=100)
    journal = UiEventJournal(repository)  # type: ignore[arg-type]
    await journal.start()
    journal.accept(_event(1, content="must not appear in diagnostics"))
    await repository.started.wait()
    for _ in range(20):
        failed = journal.diagnostics()
        if failed["last_write_error_at"] is not None:
            break
        await asyncio.sleep(0.01)

    assert failed["pending_events"] == 1
    assert failed["oldest_pending_at"] is not None
    assert failed["last_write_completed_at"] is None
    assert failed["last_write_error_at"] is not None
    assert "must not appear" not in repr(failed)

    repository.failures = 0
    assert await journal.wait_idle(timeout=1)
    recovered = journal.diagnostics()
    assert recovered["pending_events"] == 0
    assert recovered["oldest_pending_at"] is None
    assert recovered["last_write_completed_at"] is not None
    assert recovered["last_write_error_at"] is None
    await journal.close()


@pytest.mark.asyncio
async def test_journal_writer_survives_empty_wait_and_never_blocks_accept_behind_slow_head() -> (
    None
):
    gate = asyncio.Event()
    repository = _RecordingRepository(gate=gate)
    journal = UiEventJournal(repository)  # type: ignore[arg-type]
    await journal.start()
    writer = journal._writer
    assert writer is not None
    assert not writer.done()

    journal.accept(_event(1))
    await repository.started.wait()
    journal.accept(_event(2))
    assert [event["sequence"] for event in journal.snapshot("session")] == [1, 2]
    diagnostics = journal.diagnostics()
    assert diagnostics["writer_running"] is True
    assert diagnostics["pending_events"] == 2
    assert diagnostics["fatal_conflicts"] == 0
    assert diagnostics["closed"] is False
    assert diagnostics["oldest_pending_at"] is not None
    assert diagnostics["last_write_completed_at"] is None
    assert diagnostics["last_write_error_at"] is None
    assert not writer.done()

    gate.set()
    assert await journal.wait_idle(timeout=1)
    assert [event["sequence"] for event in repository.events] == [1, 2]
    assert not writer.done()  # 空队列时 writer 等待 wake, 不会退役或被 flush 重启.
    await journal.close()


@pytest.mark.asyncio
async def test_active_turn_terminal_is_first_wins_and_sequence_is_dispatch_only() -> None:
    async def stream():
        if False:
            yield None

    turn = ActiveTurn("session", "turn", stream())
    assert turn.sequence == 0
    progress = turn.dispatch(turn.intent("progress", message="working"), at="at-1")
    normal = turn.terminal(at="at-2", completed=True, stop_reason="stop")
    aborted = turn.terminal(at="at-3", completed=False, stop_reason="aborted")
    assert progress is not None and progress["sequence"] == 1
    assert normal is not None and normal["sequence"] == 2
    assert aborted is None
    assert turn.dispatch(turn.intent("progress", message="too late"), at="at-4") is None
    assert turn.terminal_dispatched is True
    await turn.cancel_and_wait()


@pytest.mark.asyncio
async def test_registry_subscription_detach_does_not_cancel_and_new_subscriber_can_stop() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    await journal.start()
    registry = ActiveTurnRegistry(journal)
    first = registry.subscribe("background")
    producer_cancelled = asyncio.Event()

    async def idle_stream():
        if False:
            yield None

    async def stream_factory():
        return idle_stream()

    async def producer(turn: ActiveTurn) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            producer_cancelled.set()
            await turn.stream.aclose()

    turn = await registry.start_turn(
        session_id="background",
        turn_id="turn",
        content="keep running",
        stream_factory=stream_factory,
        producer=producer,
    )
    registry.unsubscribe(first)
    await asyncio.sleep(0)
    assert registry.snapshot("background")["task_state"] == "running"
    assert registry.diagnostics() == [
        {
            "session_id": "background",
            "turn_id": "turn",
            "state": "running",
            "last_sequence": 1,
            "subscribers": 0,
            "background": True,
        }
    ]
    assert not producer_cancelled.is_set()

    second = registry.subscribe("background")
    stopped = await registry.stop_turn("background", "turn")
    assert stopped["stop_reason"] == "stopped"
    assert (await second.queue.get())["type"] == "done"  # type: ignore[index]
    assert await registry.wait_turn_released(turn.turn_id, timeout=1)
    assert producer_cancelled.is_set()
    terminals = [
        event for event in await journal.list_events("background") if event["type"] == "done"
    ]
    assert len(terminals) == 1 and terminals[0]["stop_reason"] == "stopped"
    registry.unsubscribe(second)
    await registry.close_all()
    await journal.close()
    await repository.close()


@pytest.mark.asyncio
async def test_control_locks_are_reclaimed_after_missing_stops() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    registry = ActiveTurnRegistry(journal)

    for index in range(5000):
        with pytest.raises(ActiveTurnNotRunningError, match="not running"):
            await registry.stop_turn(f"missing-{index}", "turn")

    assert registry._control_locks == {}
    await repository.close()


@pytest.mark.asyncio
async def test_control_lock_refcount_preserves_waiter_mutual_exclusion_and_reclaims() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    registry = ActiveTurnRegistry(journal)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with registry._control("same-session"):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def waiter() -> None:
        await first_entered.wait()
        async with registry._control("same-session"):
            order.append("waiter-enter")

    first_task = asyncio.create_task(first())
    waiter_task = asyncio.create_task(waiter())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert registry._control_locks["same-session"][1] == 2
    release_first.set()
    await asyncio.gather(first_task, waiter_task)

    assert order == ["first-enter", "first-exit", "waiter-enter"]
    assert registry._control_locks == {}
    await repository.close()


@pytest.mark.asyncio
async def test_slow_subscriber_overflow_detaches_only_subscriber_not_turn() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    await journal.start()
    registry = ActiveTurnRegistry(journal, subscriber_queue_size=1)
    subscription = registry.subscribe("overflow")

    async def idle_stream():
        if False:
            yield None

    async def stream_factory():
        return idle_stream()

    async def producer(turn: ActiveTurn) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await turn.stream.aclose()

    turn = await registry.start_turn(
        session_id="overflow",
        turn_id="turn",
        content="fill queue",
        stream_factory=stream_factory,
        producer=producer,
    )
    # user_message filled the one-item queue; turn_started overflowed it and
    # replaced the dropped live item with a reconnect sentinel.
    assert subscription.closed is True
    assert await subscription.queue.get() is None
    assert registry.snapshot("overflow")["task_state"] == "running"
    await registry.stop_turn("overflow", turn.turn_id)
    assert await registry.wait_turn_released(turn.turn_id, timeout=1)
    assert [event["type"] for event in await journal.list_events("overflow")] == [
        "user_message",
        "turn_started",
        "done",
    ]
    await registry.close_all()
    await journal.close()
    await repository.close()


@pytest.mark.asyncio
async def test_registry_shutdown_aborts_and_joins_background_turn_before_journal_close() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    await journal.start()
    registry = ActiveTurnRegistry(journal)
    producer_finally = asyncio.Event()

    async def idle_stream():
        if False:
            yield None

    async def stream_factory():
        return idle_stream()

    async def producer(turn: ActiveTurn) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            producer_finally.set()
            await turn.stream.aclose()

    turn = await registry.start_turn(
        session_id="shutdown",
        turn_id="turn",
        content="long task",
        stream_factory=stream_factory,
        producer=producer,
    )
    await registry.close_all()
    assert producer_finally.is_set()
    assert registry.snapshot("shutdown")["task_state"] == "idle"
    terminals = [
        event for event in await journal.list_events("shutdown") if event["type"] == "done"
    ]
    assert len(terminals) == 1
    assert terminals[0]["stop_reason"] == "aborted"
    assert await journal.wait_turn_durable("shutdown", turn.turn_id, timeout=1)
    await journal.close()
    await repository.close()


@pytest.mark.asyncio
async def test_normal_done_waits_for_slow_producer_finally_without_cancelling_it() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cancelled = False

    async def stream():
        nonlocal cancelled
        try:
            if False:
                yield None
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    turn = ActiveTurn("session", "turn", stream())

    async def consume() -> None:
        async for _ in turn.stream:
            pass

    turn.task = asyncio.create_task(consume())
    event = turn.terminal(at="at", completed=True, stop_reason="stop")
    assert event is not None
    joined = asyncio.create_task(turn.wait_closed())
    await cleanup_started.wait()
    assert turn.cancel_requested is False
    assert cancelled is False
    release_cleanup.set()
    await joined
    assert turn.closed is True


@pytest.mark.asyncio
async def test_repository_rejects_conflicting_duplicate_ui_event() -> None:
    repository = SQLiteSessionRepository(":memory:")
    event = _event(1, content="first")
    await repository.append_ui_event("session", turn_id="turn", sequence=1, at="at", event=event)
    conflicting = {**event, "content": "other"}
    with pytest.raises(SessionRepositoryError, match="invariant"):
        await repository.append_ui_event(
            "session", turn_id="turn", sequence=1, at="at", event=conflicting
        )
    await repository.close()


@pytest.mark.asyncio
async def test_journal_close_drains_accepted_events_before_cancelling_writer() -> None:
    repository = _RecordingRepository()
    journal = UiEventJournal(repository)  # type: ignore[arg-type]
    await journal.start()
    journal.accept(_event(1))
    await journal.close()
    assert [event["sequence"] for event in repository.events] == [1]
    assert journal._writer is not None and journal._writer.cancelled()


@pytest.mark.asyncio
async def test_journal_isolates_permanent_conflict_and_continues_other_session() -> None:
    class ConflictRepository(_RecordingRepository):
        async def append_ui_event(self, session_id: str, **kwargs: object) -> None:
            if session_id == "session":
                raise SessionUiEventConflictError("existing row differs")
            event = kwargs["event"]
            assert isinstance(event, dict)
            self.events.append(dict(event))

    repository = ConflictRepository()
    journal = UiEventJournal(repository)  # type: ignore[arg-type]
    await journal.start()
    first = _event(1)
    second = {**_event(1), "session_id": "other", "turn_id": "other-turn"}
    journal.accept(first)
    journal.accept(second)
    assert await journal.wait_idle(timeout=1)
    assert journal.fatal_conflicts == (journal.accept(first),)
    assert repository.events == [second]
    with pytest.raises(SessionUiEventConflictError):
        journal.accept({**first, "content": "conflicting"})
    await journal.close()


@pytest.mark.asyncio
async def test_journal_reclaims_durable_canonical_payloads_but_rechecks_later_duplicates() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    await journal.start()
    for sequence in range(1, 201):
        journal.accept(_event(sequence))
    assert await journal.wait_idle(timeout=2)
    assert journal._accepted == {}

    first = _event(1)
    journal.accept(first)  # durable duplicate is safely rechecked by SQLite.
    assert await journal.wait_idle(timeout=1)
    journal.accept({**first, "content": "conflicting after durable success"})
    assert await journal.wait_idle(timeout=1)
    assert len(journal.fatal_conflicts) == 1
    assert journal.fatal_conflicts[0].sequence == 1
    await journal.close()
    await repository.close()
