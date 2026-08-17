from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import web as web_api
from app.config import Settings
from app.main import create_app
from app.runtime.base import RuntimeContext
from app.runtime.events import Completed, Progress, RuntimeEvent, TextDelta
from app.sessions.active_turns import ActiveTurn
from app.sessions.repository import SQLiteSessionRepository
from app.sessions.ui_events import UiEventJournal


class BlockingRuntime:
    def __init__(self) -> None:
        self.cancelled = 0
        self.cancelled_event = threading.Event()

    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        return "blocking-runtime"

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        return None

    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        if message == "finish":
            yield TextDelta("finished")
            yield Completed()
            return
        try:
            yield Progress("tool", "正在运行长任务", "running", tool_name="Bash")
            await asyncio.Event().wait()
        finally:
            self.cancelled += 1
            self.cancelled_event.set()


class FailingRuntime(BlockingRuntime):
    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message, context
        raise RuntimeError("runtime exploded")
        yield  # pragma: no cover - keeps this an async generator for the protocol


class BurstRuntime(BlockingRuntime):
    async def send_message(
        self, runtime_session_id: str, message: str, context: RuntimeContext
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id, message, context
        try:
            for index in range(100):
                yield Progress("tool", f"输出 {index}", "running", tool_name="Bash")
            await asyncio.Event().wait()
        finally:
            self.cancelled += 1


@pytest.fixture(autouse=True)
def _provider_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    class Catalog:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def discover(self) -> tuple[str, ...]:
            return ("test-model",)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(web_api, "ModelCatalog", Catalog)


def _message(content: str) -> dict[str, object]:
    return {
        "type": "message",
        "content": content,
        "model": "test-model",
        "provider": {
            "base_url": "https://provider.example",
            "api_key": "test-key",
            "auth_env": "ANTHROPIC_AUTH_TOKEN",
        },
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_backend="fake",
        sandbox_backend="local",
        database_url=f"sqlite:///{tmp_path / 'turns.db'}",
        workspace_root=tmp_path / "workspaces",
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
    )


def _receive_until(websocket, kind: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if event["type"] == kind:
            return events


def _receive_until_matching(websocket, predicate) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    while True:
        event = websocket.receive_json()
        events.append(event)
        if predicate(event):
            return events


def _receive_ready(websocket) -> tuple[dict[str, object], list[dict[str, object]]]:
    begin = websocket.receive_json()
    assert begin["type"] == "sync_begin"
    replay: list[dict[str, object]] = []
    while True:
        event = websocket.receive_json()
        if event["type"] == "ready":
            return event, replay
        replay.append(event)


def _history_until_terminal(client: TestClient, session_id: str) -> list[dict[str, object]]:
    for _ in range(50):
        response = client.get(f"/v1/sessions/{session_id}/history")
        events = response.json()["events"]
        if events and events[-1].get("type") == "done":
            return events
        time.sleep(0.01)
    return client.get(f"/v1/sessions/{session_id}/history").json()["events"]


def test_stop_cancels_stream_emits_ordered_terminal_event_and_allows_next_turn(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "turn"})
            ready, replay = _receive_ready(websocket)
            assert ready["task_state"] == "idle"
            assert replay == []

            websocket.send_json(_message("block"))
            opening = _receive_until_matching(
                websocket,
                lambda event: event["type"] == "progress" and event.get("tool_name") == "Bash",
            )
            started = next(event for event in opening if event["type"] == "turn_started")
            assert started["type"] == "turn_started"
            turn_id = started["turn_id"]
            assert all(
                event["session_id"] == "turn" and event["turn_id"] == turn_id for event in opening
            )
            assert [event["sequence"] for event in opening] == list(range(len(opening)))
            assert all(event["at"] for event in opening)

            websocket.send_json(_message("second"))
            busy = _receive_until(websocket, "error")[-1]
            assert busy["type"] == "error" and busy["code"] == "session_busy"
            assert busy["recoverable"] is True
            assert "turn_id" not in busy

            websocket.send_json({"type": "stop", "turn_id": turn_id})
            stopped = _receive_until(websocket, "done")[-1]
            assert stopped["type"] == "done"
            assert stopped["completed"] is False
            assert stopped["stop_reason"] == "stopped"
            assert stopped["sequence"] == opening[-1]["sequence"] + 1
            assert runtime.cancelled == 1

            websocket.send_json(_message("finish"))
            completed_events = _receive_until(websocket, "done")
            next_started = next(
                event for event in completed_events if event["type"] == "turn_started"
            )
            done = completed_events[-1]
            assert next_started["type"] == "turn_started"
            assert next_started["turn_id"] != turn_id
            assert all(event["turn_id"] == next_started["turn_id"] for event in completed_events)
            assert done["completed"] is True
        history = client.get("/v1/sessions/turn/history")

    persisted = history.json()["events"]
    assert persisted == [*opening, stopped, *completed_events]
    assert persisted[-1]["type"] == "done" and persisted[-1]["completed"] is True
    assert "test-key" not in str(persisted)


def test_running_turn_allows_file_reads_but_keeps_upload_busy(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        uploaded = client.post(
            "/v1/sessions/running-files/files",
            files=[("files", ("result.txt", b"partial result", "text/plain"))],
        )
        assert uploaded.status_code == 200
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "running-files"})
            _receive_ready(websocket)
            websocket.send_json(_message("block"))
            events = _receive_until_matching(
                websocket,
                lambda event: event["type"] == "progress" and event.get("tool_name") == "Bash",
            )
            turn_id = events[0]["turn_id"]

            listed = client.get("/v1/sessions/running-files/files")
            assert listed.status_code == 200
            assert listed.json()["files"] == [{"path": "result.txt", "size": 14}]
            content = client.get("/v1/sessions/running-files/files/content/result.txt")
            assert content.status_code == 200
            assert content.content == b"partial result"
            busy = client.post(
                "/v1/sessions/running-files/files",
                files=[("files", ("new.txt", b"write", "text/plain"))],
            )
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "session_busy"

            websocket.send_json({"type": "stop", "turn_id": turn_id})
            assert _receive_until(websocket, "done")[-1]["stop_reason"] == "stopped"


def test_disconnect_detaches_and_reconnected_socket_can_stop_background_turn(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "disconnect"})
            _receive_ready(websocket)
            websocket.send_json(_message("block"))
            opening = _receive_until_matching(
                websocket,
                lambda event: event["type"] == "progress" and event.get("tool_name") == "Bash",
            )
        turn_id = opening[0]["turn_id"]
        assert not runtime.cancelled_event.wait(timeout=0.1)
        before_stop = client.get("/v1/sessions/disconnect/history").json()["events"]
        assert not any(event["type"] == "done" for event in before_stop)
        assert client.app.state.active_turns.snapshot("disconnect")["task_state"] == "running"
        projected = client.get("/v1/sessions/disconnect").json()
        assert projected["task_state"] == "running"
        assert projected["active_turn_id"] == turn_id

        with client.websocket_connect("/ws/chat") as reconnected:
            reconnected.send_json({"type": "hello", "session_id": "disconnect"})
            ready, replayed = _receive_ready(reconnected)
            assert ready["task_state"] == "running"
            assert ready["turn_id"] == turn_id
            assert replayed == before_stop
            reconnected.send_json({"type": "stop", "turn_id": turn_id})
            stopped = _receive_until(reconnected, "done")[-1]
            assert stopped["stop_reason"] == "stopped"
            assert runtime.cancelled_event.wait(timeout=1)
            assert client.portal.call(
                lambda: client.app.state.active_turns.wait_turn_released(turn_id, timeout=1)
            )
            assert client.get("/v1/sessions/disconnect").json()["task_state"] == "idle"
            reconnected.send_json(_message("finish"))
            resumed = _receive_until(reconnected, "done")
        events = client.get("/v1/sessions/disconnect/history").json()["events"]

    assert runtime.cancelled == 1
    assert resumed[-1]["completed"] is True
    assert events[: len(opening)] == opening
    assert events[-1]["type"] == "done"
    assert events[-1]["completed"] is True
    stopped_events = [event for event in events if event.get("stop_reason") == "stopped"]
    assert len(stopped_events) == 1
    assert stopped_events[0]["sequence"] == opening[-1]["sequence"] + 1


def test_runtime_exception_emits_ordered_error_and_failed_done(tmp_path: Path) -> None:
    runtime = FailingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "failure"})
            _receive_ready(websocket)
            websocket.send_json(_message("fail"))
            events = _receive_until(websocket, "done")
        history = client.get("/v1/sessions/failure/history")

    started = next(event for event in events if event["type"] == "turn_started")
    error = next(event for event in events if event["type"] == "error")
    done = events[-1]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert all(
        event["session_id"] == "failure" and event["turn_id"] == started["turn_id"] and event["at"]
        for event in events
    )
    assert error["code"] == "turn_failed"
    assert done["completed"] is False
    assert done["stop_reason"] == "turn_failed"
    assert history.status_code == 200
    assert history.json()["events"] == events
    assert history.json()["events"][-1] == done
    assert "test-key" not in str(history.json())


def test_ui_history_write_failure_does_not_cancel_live_turn(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime

        async def unavailable(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("sqlite temporarily unavailable")

        client.app.state.session_service.repository.append_ui_event = unavailable  # type: ignore[method-assign]
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "best-effort"})
            _receive_ready(websocket)
            websocket.send_json(_message("finish"))
            events = _receive_until(websocket, "done")

    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "turn_started"
    assert any(event["type"] == "delta" for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["completed"] is True


def test_transient_ui_history_failures_retry_in_sequence_order(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        repository = client.app.state.session_service.repository
        append = repository.append_ui_event
        failures_remaining = 2

        async def flaky(*args: object, **kwargs: object):
            nonlocal failures_remaining
            if failures_remaining:
                failures_remaining -= 1
                raise RuntimeError("sqlite temporarily unavailable")
            return await append(*args, **kwargs)

        repository.append_ui_event = flaky  # type: ignore[method-assign]
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "retry-history"})
            _receive_ready(websocket)
            websocket.send_json(_message("finish"))
            events = _receive_until(websocket, "done")
        history = client.get("/v1/sessions/retry-history/history")

    assert history.status_code == 200
    persisted = history.json()["events"]
    assert persisted[0]["type"] == "user_message"
    assert persisted == events
    assert [event["sequence"] for event in persisted] == list(range(len(events)))


def test_history_overlays_pending_suffix_until_transient_writer_recovers(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        repository = client.app.state.session_service.repository
        append = repository.append_ui_event
        failures_remaining = 100

        async def blocked(*args: object, **kwargs: object):
            nonlocal failures_remaining
            if failures_remaining:
                failures_remaining -= 1
                raise RuntimeError("sqlite temporarily unavailable")
            return await append(*args, **kwargs)

        repository.append_ui_event = blocked  # type: ignore[method-assign]
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "pending-overlay"})
            _receive_ready(websocket)
            websocket.send_json(_message("finish"))
            dispatched = _receive_until(websocket, "done")

        immediate = client.get("/v1/sessions/pending-overlay/history")
        assert immediate.status_code == 200
        assert immediate.json()["events"][0]["type"] == "user_message"
        assert immediate.json()["events"] == dispatched

        failures_remaining = 0
        journal = client.app.state.ui_event_journal
        assert client.portal.call(lambda: journal.wait_idle(timeout=1))
        durable = client.get("/v1/sessions/pending-overlay/history")

    assert durable.json()["events"] == immediate.json()["events"]


@pytest.mark.asyncio
async def test_subscribe_before_snapshot_bridges_history_live_race_exactly_once() -> None:
    repository = SQLiteSessionRepository(":memory:")
    original_list = repository.list_ui_events
    list_started = asyncio.Event()
    release_list = asyncio.Event()

    async def gated_list(session_id: str):
        list_started.set()
        await release_list.wait()
        return await original_list(session_id)

    repository.list_ui_events = gated_list  # type: ignore[method-assign]
    journal = UiEventJournal(repository)
    await journal.start()
    registry = web_api.ActiveTurnRegistry(journal)
    subscription = registry.subscribe("sync-race")

    class RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.closed = False

        async def send_json(self, event: dict[str, object]) -> None:
            self.sent.append(dict(event))

        async def close(self, code: int = 1000) -> None:
            del code
            self.closed = True

    socket = RecordingSocket()
    synchronize = asyncio.create_task(
        web_api._synchronize_subscription(
            socket,
            asyncio.Lock(),
            journal,
            registry,
            subscription,  # type: ignore[arg-type]
        )
    )
    await list_started.wait()

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

    # These events are accepted after the history pending snapshot, while the
    # DB query is gated. The pre-existing subscription is therefore the bridge.
    turn = await registry.start_turn(
        session_id="sync-race",
        turn_id="turn",
        content="during snapshot",
        stream_factory=stream_factory,
        producer=producer,
    )
    release_list.set()
    assert await synchronize is True

    replayed = [event for event in socket.sent if event["type"] not in {"sync_begin", "ready"}]
    assert [event["type"] for event in replayed] == ["user_message", "turn_started"]
    assert len({(event["turn_id"], event["sequence"]) for event in replayed}) == 2
    assert socket.sent[-1]["type"] == "ready"
    assert socket.sent[-1]["task_state"] == "running"
    assert socket.sent[-1]["turn_id"] == turn.turn_id

    progress = registry.dispatch(turn, turn.intent("progress", message="after ready"))
    assert progress is not None
    assert await subscription.queue.get() == progress
    await registry.stop_turn("sync-race", turn.turn_id)
    assert await registry.wait_turn_released(turn.turn_id, timeout=1)
    registry.unsubscribe(subscription)
    await registry.close_all()
    await journal.close()
    await repository.close()


def test_slow_ui_history_write_does_not_delay_live_delta(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        repository = client.app.state.session_service.repository
        append = repository.append_ui_event

        async def slow(*args: object, **kwargs: object):
            await asyncio.sleep(1)
            return await append(*args, **kwargs)

        repository.append_ui_event = slow  # type: ignore[method-assign]
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "slow-history"})
            _receive_ready(websocket)
            started = time.monotonic()
            websocket.send_json(_message("finish"))
            events = _receive_until_matching(websocket, lambda event: event["type"] == "delta")
            assert time.monotonic() - started < 0.3
            assert events[-1]["content"] == "finished"


def test_blocked_global_journal_writer_does_not_block_another_session_live_turn(
    tmp_path: Path,
) -> None:
    runtime = BlockingRuntime()
    writer_started = threading.Event()
    release_writer = threading.Event()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        repository = client.app.state.session_service.repository
        append = repository.append_ui_event

        async def blocked(*args: object, **kwargs: object):
            writer_started.set()
            await asyncio.to_thread(release_writer.wait)
            return await append(*args, **kwargs)

        repository.append_ui_event = blocked  # type: ignore[method-assign]
        try:
            with (
                client.websocket_connect("/ws/chat") as first,
                client.websocket_connect("/ws/chat") as second,
            ):
                first.send_json({"type": "hello", "session_id": "journal-a"})
                second.send_json({"type": "hello", "session_id": "journal-b"})
                _receive_ready(first)
                _receive_ready(second)

                first.send_json(_message("finish"))
                assert (
                    _receive_until_matching(first, lambda event: event["type"] == "turn_started")[
                        -1
                    ]["type"]
                    == "turn_started"
                )
                assert writer_started.wait(timeout=1)

                started = time.monotonic()
                second.send_json(_message("finish"))
                second_events = _receive_until(second, "done")
                assert time.monotonic() - started < 0.3
                assert any(event["type"] == "delta" for event in second_events)
                assert second_events[-1]["completed"] is True
        finally:
            release_writer.set()


def test_send_disconnect_after_normal_done_keeps_one_normal_history_terminal() -> None:
    async def idle():
        if False:
            yield None

    async def idle_factory():
        return idle()

    async def scenario() -> list[dict[str, object]]:
        repository = SQLiteSessionRepository(":memory:")
        journal = UiEventJournal(repository)
        await journal.start()
        registry = web_api.ActiveTurnRegistry(journal)
        subscription = registry.subscribe("normal-send-disconnect")

        async def produce(turn: ActiveTurn) -> None:
            registry.dispatch(
                turn,
                turn.intent("done", completed=True, stop_reason="stop", usage=None),
            )
            await turn.stream.aclose()

        turn = await registry.start_turn(
            session_id="normal-send-disconnect",
            turn_id="turn",
            content="finish",
            stream_factory=idle_factory,
            producer=produce,
        )
        registry.unsubscribe(subscription)  # equivalent to a failed/closed socket
        assert await registry.wait_turn_released(turn.turn_id, timeout=1)
        assert await journal.wait_turn_durable(turn.session_id, turn.turn_id, timeout=1)
        persisted = [dict(item.event) for item in await repository.list_ui_events(turn.session_id)]
        await registry.close_all()
        await journal.close()
        await repository.close()
        return persisted

    terminals = [event for event in asyncio.run(scenario()) if event["type"] == "done"]
    assert terminals == [
        {
            "type": "done",
            "session_id": "normal-send-disconnect",
            "turn_id": "turn",
            "sequence": 2,
            "at": terminals[0]["at"],
            "completed": True,
            "stop_reason": "stop",
            "usage": None,
        }
    ]


def test_slow_normal_done_persistence_and_send_disconnect_never_adds_aborted_terminal() -> None:
    async def idle():
        if False:
            yield None

    async def idle_factory():
        return idle()

    async def scenario() -> list[dict[str, object]]:
        repository = SQLiteSessionRepository(":memory:")
        original_append = repository.append_ui_event

        async def slow_append(*args: object, **kwargs: object):
            await asyncio.sleep(1)
            return await original_append(*args, **kwargs)

        repository.append_ui_event = slow_append  # type: ignore[method-assign]
        journal = UiEventJournal(repository)
        await journal.start()
        registry = web_api.ActiveTurnRegistry(journal)
        subscription = registry.subscribe("slow-done-disconnect")

        async def produce(turn: ActiveTurn) -> None:
            registry.dispatch(
                turn,
                turn.intent("done", completed=True, stop_reason="stop", usage=None),
            )
            await turn.stream.aclose()

        turn = await registry.start_turn(
            session_id="slow-done-disconnect",
            turn_id="turn",
            content="finish",
            stream_factory=idle_factory,
            producer=produce,
        )
        registry.unsubscribe(subscription)
        assert await registry.wait_turn_released(turn.turn_id, timeout=1)
        assert await journal.wait_turn_durable(turn.session_id, turn.turn_id, timeout=4)
        persisted = [dict(item.event) for item in await repository.list_ui_events(turn.session_id)]
        await registry.close_all()
        await journal.close()
        await repository.close()
        return persisted

    terminals = [event for event in asyncio.run(scenario()) if event["type"] == "done"]
    assert len(terminals) == 1
    assert terminals[0]["completed"] is True
    assert terminals[0]["stop_reason"] == "stop"


@pytest.mark.asyncio
async def test_asgi_task_cancellation_only_unsubscribes_and_global_stop_still_works() -> None:
    repository = SQLiteSessionRepository(":memory:")
    journal = UiEventJournal(repository)
    await journal.start()
    registry = web_api.ActiveTurnRegistry(journal)
    producer_finally = asyncio.Event()
    progress_sent = asyncio.Event()

    class Service:
        def __init__(self) -> None:
            self.repository = repository

        async def stream_events(self, *args: object, **kwargs: object):
            del args, kwargs

            async def stream():
                try:
                    yield Progress("tool", "正在运行", "running", tool_name="Bash")
                    await asyncio.Event().wait()
                finally:
                    producer_finally.set()

            return stream()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    session_service=Service(), ui_event_journal=journal, active_turns=registry
                )
            )
            self.incoming = [
                {"type": "hello", "session_id": "asgi-cancel"},
                _message("block"),
            ]
            self.sent: list[dict[str, object]] = []

        async def accept(self) -> None:
            return None

        async def close(self, code: int = 1000) -> None:
            del code

        async def receive_json(self) -> object:
            if self.incoming:
                return self.incoming.pop(0)
            await asyncio.Event().wait()

        async def send_json(self, event: dict[str, object]) -> None:
            self.sent.append(event)
            if event.get("type") == "progress":
                progress_sent.set()

    websocket = FakeWebSocket()
    handler = asyncio.create_task(web_api.websocket_chat(websocket))
    await progress_sent.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    started = next(event for event in websocket.sent if event["type"] == "turn_started")
    assert registry.snapshot("asgi-cancel")["task_state"] == "running"
    assert not producer_finally.is_set()
    assert not [
        event for event in await journal.list_events("asgi-cancel") if event["type"] == "done"
    ]

    await registry.stop_turn("asgi-cancel", started["turn_id"])
    assert await registry.wait_turn_released(started["turn_id"], timeout=1)
    assert producer_finally.is_set()
    assert await journal.wait_turn_durable("asgi-cancel", started["turn_id"], timeout=1)
    terminals = [
        event for event in await journal.list_events("asgi-cancel") if event["type"] == "done"
    ]
    assert len(terminals) == 1
    assert terminals[0]["stop_reason"] == "stopped"
    await registry.close_all()
    await journal.close()
    await repository.close()


def test_stop_preempts_burst_output_and_discards_old_turn_events(tmp_path: Path) -> None:
    runtime = BurstRuntime()
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.app.state.session_service.runtime = runtime
        with client.websocket_connect("/ws/chat") as websocket:
            websocket.send_json({"type": "hello", "session_id": "burst"})
            ready, _ = _receive_ready(websocket)
            assert ready["task_state"] == "idle"
            websocket.send_json(_message("burst"))
            started = _receive_until_matching(
                websocket, lambda event: event["type"] == "turn_started"
            )[-1]
            assert started["type"] == "turn_started"
            first_output = _receive_until_matching(
                websocket,
                lambda event: event["type"] == "progress" and event.get("tool_name") == "Bash",
            )[-1]
            assert first_output["tool_name"] == "Bash"
            websocket.send_json({"type": "stop", "turn_id": started["turn_id"]})

            events = _receive_until(websocket, "done")

    assert events[-1]["stop_reason"] == "stopped"
    assert runtime.cancelled == 1
