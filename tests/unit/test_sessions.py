from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.runtime import FakeRuntime
from app.sandbox.local import LocalSandboxManager
from app.sessions import (
    InvalidSessionTransition,
    SessionAlreadyExistsError,
    SessionLockRegistry,
    SessionRecord,
    SessionState,
    SessionVersionConflictError,
    SQLiteSessionRepository,
    touch,
    transition,
)
from app.sessions.service import SessionService


class SQLiteSessionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteSessionRepository(Path(self.directory.name) / "sessions.db")

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.directory.cleanup()

    async def test_create_get_and_json_extensions_survive_reload(self) -> None:
        created_at = datetime(2026, 8, 10, tzinfo=UTC)
        record = SessionRecord(
            session_id="session-a",
            sandbox_id="sandbox-a",
            claude_session_id="runtime-a",
            created_at=created_at,
            last_activity_at=created_at,
            metadata={
                "pending_interaction": {"kind": "choice", "options": ["A", "B"]},
                "last_user_fingerprint": "sha256:abc",
            },
        )

        await self.repository.create(record)
        loaded = await self.repository.get("session-a")

        self.assertEqual(loaded, record)
        self.assertEqual(loaded.pending_interaction["kind"], "choice")
        self.assertEqual(loaded.last_user_fingerprint, "sha256:abc")
        with self.assertRaises(SessionAlreadyExistsError):
            await self.repository.create(record)

    async def test_update_uses_optimistic_version(self) -> None:
        record = await self.repository.create(SessionRecord(session_id="session-a"))
        first = await self.repository.update(record.with_metadata(turn=1))

        self.assertEqual(first.version, 1)
        self.assertEqual((await self.repository.get("session-a")).metadata, {"turn": 1})
        with self.assertRaises(SessionVersionConflictError):
            await self.repository.update(record.with_metadata(turn=2))

    async def test_list_due_filters_by_timestamp_and_state(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        old = SessionRecord(session_id="old", last_activity_at=now - timedelta(hours=2))
        fresh = SessionRecord(session_id="fresh", last_activity_at=now)
        deleted = SessionRecord(
            session_id="deleted",
            state=SessionState.DELETED,
            last_activity_at=now - timedelta(hours=3),
            deleted_at=now - timedelta(hours=1),
        )
        for record in (old, fresh, deleted):
            await self.repository.create(record)

        due = await self.repository.list_due(now - timedelta(hours=1))
        self.assertEqual([record.session_id for record in due], ["old"])
        deleted_due = await self.repository.list_due(
            now - timedelta(hours=1), states=[SessionState.DELETED]
        )
        self.assertEqual([record.session_id for record in deleted_due], ["deleted"])

    async def test_list_sessions_orders_newest_activity_first_and_keeps_tombstones(self) -> None:
        now = datetime(2026, 8, 10, 12, tzinfo=UTC)
        records = [
            SessionRecord(session_id="old", last_activity_at=now - timedelta(minutes=2)),
            SessionRecord(session_id="new", last_activity_at=now),
            SessionRecord(
                session_id="deleted",
                state=SessionState.DELETED,
                last_activity_at=now - timedelta(minutes=1),
                deleted_at=now,
                metadata={"title": "Archived", "last_model": "glm-4.7"},
            ),
        ]
        for record in records:
            await self.repository.create(record)

        listed = await self.repository.list_sessions()

        self.assertEqual([record.session_id for record in listed], ["new", "deleted", "old"])
        self.assertEqual(listed[1].title, "Archived")
        self.assertEqual(listed[1].last_model, "glm-4.7")

    async def test_users_are_normalized_unique_and_can_be_disabled(self) -> None:
        user = await self.repository.create_user("  Alice\tExample  ")

        self.assertEqual(user.name, "Alice Example")
        self.assertEqual(user.normalized_name, "alice example")
        self.assertIsNotNone(await self.repository.find_user_by_name("alice example"))
        with self.assertRaises(SessionAlreadyExistsError):
            await self.repository.create_user("ＡＬＩＣＥ example")
        with self.assertRaises(ValueError):
            await self.repository.create_user(" \n ")
        with self.assertRaises(ValueError):
            await self.repository.create_user("bad\u200bname")

        disabled = await self.repository.set_user_enabled(user.user_id, False)
        self.assertFalse(disabled.enabled)
        self.assertFalse((await self.repository.get_user(user.user_id)).enabled)

    async def test_session_owner_filter_and_managed_settings_survive_reload(self) -> None:
        owner = await self.repository.create_user("Owner")
        await self.repository.create(SessionRecord(session_id="owned", owner_user_id=owner.user_id))
        await self.repository.create(SessionRecord(session_id="legacy"))

        self.assertEqual(
            [record.session_id for record in await self.repository.list_sessions(owner.user_id)],
            ["owned"],
        )
        initial = await self.repository.get_managed_settings()
        updated = await self.repository.update_managed_settings(
            {"docker_memory": "1g"}, expected_version=initial.version
        )
        self.assertEqual(updated.version, initial.version + 1)
        with self.assertRaises(SessionVersionConflictError):
            await self.repository.update_managed_settings(
                {"docker_memory": "2g"}, expected_version=initial.version
            )

        await self.repository.close()
        self.repository = SQLiteSessionRepository(Path(self.directory.name) / "sessions.db")
        restored = await self.repository.get_managed_settings()
        self.assertEqual(restored.values, {"docker_memory": "1g"})
        self.assertEqual((await self.repository.get("owned")).owner_user_id, owner.user_id)

    async def test_ui_events_are_ordered_idempotent_and_survive_repository_restart(self) -> None:
        timestamp = "2026-08-11T08:00:00+00:00"
        first = await self.repository.append_ui_event(
            "session-a",
            turn_id="turn-1",
            sequence=0,
            at=timestamp,
            event={
                "type": "user_message",
                "session_id": "session-a",
                "turn_id": "turn-1",
                "sequence": 0,
                "at": timestamp,
                "content": "first request",
            },
        )
        duplicate = await self.repository.append_ui_event(
            "session-a",
            turn_id="turn-1",
            sequence=0,
            at=timestamp,
            event={
                "type": "user_message",
                "session_id": "session-a",
                "turn_id": "turn-1",
                "sequence": 0,
                "at": timestamp,
                "content": "first request",
            },
        )
        second = await self.repository.append_ui_event(
            "session-a",
            turn_id="turn-1",
            sequence=1,
            at="2026-08-11T08:00:02+00:00",
            event={"type": "turn_started", "sequence": 1},
        )
        await self.repository.append_ui_event(
            "session-b",
            turn_id="turn-1",
            sequence=0,
            at=timestamp,
            event={"type": "user_message", "content": "isolated"},
        )

        self.assertEqual(first, duplicate)
        self.assertEqual(
            [event.sequence for event in await self.repository.list_ui_events("session-a")], [0, 1]
        )
        self.assertEqual(second.event["type"], "turn_started")
        self.assertEqual(
            (await self.repository.list_ui_events("session-a"))[0].event["content"], "first request"
        )
        self.assertEqual(len(await self.repository.list_ui_events("session-b")), 1)

        await self.repository.close()
        self.repository = SQLiteSessionRepository(Path(self.directory.name) / "sessions.db")
        restored = await self.repository.list_ui_events("session-a")
        self.assertEqual(
            [(event.turn_id, event.sequence) for event in restored], [("turn-1", 0), ("turn-1", 1)]
        )
        self.assertEqual(restored[0].at, timestamp)


class SessionStateMachineTests(unittest.TestCase):
    def test_lifecycle_and_tombstone_rule(self) -> None:
        now = datetime(2026, 8, 10, tzinfo=UTC)
        active = SessionRecord(session_id="s", last_activity_at=now)
        paused = transition(active, SessionState.PAUSED, now=now + timedelta(minutes=30))
        resumed = transition(paused, SessionState.ACTIVE, now=now + timedelta(minutes=31))
        deleted = transition(resumed, SessionState.DELETED, now=now + timedelta(hours=2))

        self.assertEqual(paused.paused_at, now + timedelta(minutes=30))
        self.assertIsNone(resumed.paused_at)
        self.assertEqual(resumed.last_activity_at, now + timedelta(minutes=31))
        with self.assertRaises(InvalidSessionTransition):
            transition(deleted, SessionState.ACTIVE, now=now + timedelta(hours=3))
        with self.assertRaises(InvalidSessionTransition):
            touch(paused, now=now)

    def test_compatibility_projection_uses_only_durable_runtime_and_sandbox_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SessionService(
                SQLiteSessionRepository(Path(directory) / "sessions.db"),
                SessionLockRegistry(),
                LocalSandboxManager(Path(directory) / "workspaces"),
                FakeRuntime(),
            )
            current = SessionRecord(
                session_id="current",
                sandbox_id="local-123",
                claude_session_id="runtime",
                metadata={"runtime_backend": "FakeRuntime"},
            )
            legacy_runtime = current.with_metadata(runtime_backend="LegacyRuntime")
            old_sandbox = SessionRecord(
                session_id="old-sandbox",
                sandbox_id="oca-sandbox-123",
                claude_session_id="runtime",
                metadata={"runtime_backend": "FakeRuntime"},
            )

            self.assertEqual(service.compatibility_view(current), (True, None))
            self.assertEqual(
                service.compatibility_view(legacy_runtime), (False, "运行时后端不兼容")
            )
            self.assertEqual(service.compatibility_view(old_sandbox), (False, "沙箱后端不兼容"))


class SessionLockRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_is_serialised_but_other_sessions_are_independent(self) -> None:
        registry = SessionLockRegistry()
        order: list[str] = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with registry.hold("same"):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def second() -> None:
            await first_entered.wait()
            async with registry.hold("same"):
                order.append("second-enter")

        async def other() -> None:
            await first_entered.wait()
            async with registry.hold("other"):
                order.append("other-enter")

        tasks = [asyncio.create_task(coro()) for coro in (first, second, other)]
        await first_entered.wait()
        await asyncio.sleep(0)
        self.assertIn("other-enter", order)
        self.assertNotIn("second-enter", order)
        release_first.set()
        await asyncio.gather(*tasks)
        self.assertEqual(order, ["first-enter", "other-enter", "first-exit", "second-enter"])
