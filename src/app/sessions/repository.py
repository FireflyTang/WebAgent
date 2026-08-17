"""Small asynchronous SQLite repository for persistent session mappings."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .models import SessionRecord, SessionState


class SessionRepositoryError(RuntimeError):
    pass


class SessionAlreadyExistsError(SessionRepositoryError):
    pass


class SessionVersionConflictError(SessionRepositoryError):
    pass


class SessionUiEventConflictError(SessionRepositoryError):
    """同一 UI event key 被赋予不同不可变 payload。"""


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    name: str
    normalized_name: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ManagedSettingsRecord:
    values: Mapping[str, object]
    version: int
    updated_at: datetime


def normalize_user_name(name: str) -> tuple[str, str]:
    """Return the display and lookup forms for one administrator-created user."""

    display_name = " ".join(unicodedata.normalize("NFKC", name).strip().split())
    if not display_name:
        raise ValueError("用户名称不能为空")
    if len(display_name) > 80:
        raise ValueError("用户名称不能超过 80 个字符")
    if any(unicodedata.category(character).startswith("C") for character in display_name):
        raise ValueError("用户名称不能包含控制字符")
    return display_name, display_name.casefold()


@dataclass(frozen=True, slots=True)
class SessionLogEntry:
    """One durable high-level or runtime-diagnostic session log entry."""

    sequence: int
    session_id: str
    created_at: datetime
    title: str
    content: str
    metadata: Mapping[str, object]
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class SessionUiEvent:
    """One durable, replayable browser protocol event for a session turn."""

    session_id: str
    turn_id: str
    sequence: int
    at: str
    event: Mapping[str, object]
    created_at: datetime


class SessionRepository(Protocol):
    async def create(self, record: SessionRecord) -> SessionRecord: ...

    async def get(self, session_id: str) -> SessionRecord | None: ...

    async def update(
        self, record: SessionRecord, *, expected_version: int | None = None
    ) -> SessionRecord: ...

    async def list_sessions(self, owner_user_id: str | None = None) -> list[SessionRecord]: ...

    async def create_user(self, name: str) -> UserRecord: ...

    async def get_user(self, user_id: str) -> UserRecord | None: ...

    async def find_user_by_name(self, normalized_name: str) -> UserRecord | None: ...

    async def list_users(self) -> list[UserRecord]: ...

    async def set_user_enabled(self, user_id: str, enabled: bool) -> UserRecord: ...

    async def get_managed_settings(self) -> ManagedSettingsRecord: ...

    async def update_managed_settings(
        self, values: Mapping[str, object], *, expected_version: int
    ) -> ManagedSettingsRecord: ...

    async def list_due(
        self,
        before: datetime,
        *,
        states: Iterable[SessionState] | None = None,
    ) -> list[SessionRecord]: ...

    async def list_pending_cleanup(self) -> list[SessionRecord]: ...

    async def append_log_entry(
        self,
        session_id: str,
        *,
        title: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
        event_type: str | None = None,
    ) -> SessionLogEntry: ...

    async def list_log_entries(self, session_id: str) -> list[SessionLogEntry]: ...

    async def append_ui_event(
        self,
        session_id: str,
        *,
        turn_id: str,
        sequence: int,
        at: str,
        event: Mapping[str, object],
    ) -> SessionUiEvent: ...

    async def list_ui_events(self, session_id: str) -> list[SessionUiEvent]: ...


def _normalise_database_path(database: str | Path) -> tuple[str, bool]:
    value = str(database)
    if value.startswith("sqlite:///"):
        value = value.removeprefix("sqlite:///")
    if value == ":memory:":
        # A named shared in-memory database remains alive via an anchor
        # connection, while normal operations can still run in worker threads.
        return f"file:webagent_sessions_{uuid4().hex}?mode=memory&cache=shared", True
    return value, value.startswith("file:")


def _encode_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _decode_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class SQLiteSessionRepository:
    """SQLite implementation using ``asyncio.to_thread`` rather than an ORM."""

    def __init__(self, database: str | Path) -> None:
        self._database, self._uri = _normalise_database_path(database)
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._memory_guard = threading.Lock()
        self._anchor: sqlite3.Connection | None = None
        if self._uri and "mode=memory" in self._database:
            self._anchor = self._connect()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            uri=self._uri,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            if not self._uri:
                parent = Path(self._database).parent
                if str(parent) not in ("", "."):
                    await asyncio.to_thread(parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NULL,
                    sandbox_id TEXT NULL,
                    claude_session_id TEXT NULL,
                    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'PAUSED', 'DELETED')),
                    created_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    paused_at TEXT NULL,
                    deleted_at TEXT NULL,
                    version INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "owner_user_id" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN owner_user_id TEXT NULL")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS sessions_owner_idx "
                "ON sessions (owner_user_id, last_activity_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS sessions_due_idx ON sessions (state, last_activity_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_log_entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    event_type TEXT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS session_log_entries_session_idx "
                "ON session_log_entries (session_id, sequence)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_ui_events (
                    insertion_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (session_id, turn_id, sequence)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS session_ui_events_session_idx "
                "ON session_ui_events (session_id, insertion_sequence)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    values_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            now = _encode_timestamp(datetime.now(UTC))
            connection.execute(
                "INSERT OR IGNORE INTO managed_settings "
                "(singleton, values_json, version, updated_at) VALUES (1, '{}', 0, ?)",
                (now,),
            )

    class _ConnectionContext:
        def __init__(self, repository: SQLiteSessionRepository) -> None:
            self.repository = repository
            self.connection: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            if self.repository._anchor is not None:
                self.repository._memory_guard.acquire()
                self.connection = self.repository._anchor
                return self.connection
            self.connection = self.repository._connect()
            return self.connection

        def __exit__(self, *args: object) -> None:
            if self.repository._anchor is not None:
                self.repository._memory_guard.release()
            elif self.connection is not None:
                self.connection.close()

    def _connection(self) -> SQLiteSessionRepository._ConnectionContext:
        return self._ConnectionContext(self)

    async def create(self, record: SessionRecord) -> SessionRecord:
        await self.initialize()
        return await asyncio.to_thread(self._create_sync, record)

    def _create_sync(self, record: SessionRecord) -> SessionRecord:
        if record.version != 0:
            raise ValueError("new session records must have version 0")
        values = self._values(record, version=0)
        try:
            with self._connection() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO sessions (
                            session_id, owner_user_id, sandbox_id, claude_session_id, state, created_at,
                            last_activity_at, paused_at, deleted_at, version, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            raise SessionAlreadyExistsError(record.session_id) from exc
        return record

    async def get(self, session_id: str) -> SessionRecord | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_sync, session_id)

    def _get_sync(self, session_id: str) -> SessionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    async def update(
        self, record: SessionRecord, *, expected_version: int | None = None
    ) -> SessionRecord:
        await self.initialize()
        version = record.version if expected_version is None else expected_version
        return await asyncio.to_thread(self._update_sync, record, version)

    def _update_sync(self, record: SessionRecord, expected_version: int) -> SessionRecord:
        next_version = expected_version + 1
        values = self._values(record, version=next_version) + (record.session_id, expected_version)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE sessions SET
                        owner_user_id = ?, sandbox_id = ?, claude_session_id = ?, state = ?, created_at = ?,
                        last_activity_at = ?, paused_at = ?, deleted_at = ?, version = ?,
                        metadata_json = ?
                    WHERE session_id = ? AND version = ?
                    """,
                    values[1:],
                )
                if cursor.rowcount != 1:
                    raise SessionVersionConflictError(record.session_id)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return SessionRecord(
            session_id=record.session_id,
            owner_user_id=record.owner_user_id,
            sandbox_id=record.sandbox_id,
            claude_session_id=record.claude_session_id,
            state=record.state,
            created_at=record.created_at,
            last_activity_at=record.last_activity_at,
            paused_at=record.paused_at,
            deleted_at=record.deleted_at,
            version=next_version,
            metadata=record.metadata,
        )

    async def list_due(
        self,
        before: datetime,
        *,
        states: Iterable[SessionState] | None = None,
    ) -> list[SessionRecord]:
        await self.initialize()
        wanted = tuple(states) if states is not None else (SessionState.ACTIVE, SessionState.PAUSED)
        return await asyncio.to_thread(self._list_due_sync, before, wanted)

    def _list_due_sync(
        self, before: datetime, states: tuple[SessionState, ...]
    ) -> list[SessionRecord]:
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE last_activity_at <= ? "
                f"AND state IN ({placeholders}) ORDER BY last_activity_at ASC",
                (_encode_timestamp(before), *(state.value for state in states)),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    async def list_pending_cleanup(self) -> list[SessionRecord]:
        """Return tombstones whose external runtime/sandbox cleanup must retry now.

        A failed DELETE has already transitioned the durable record to DELETED,
        so its original ``last_activity_at`` must not delay a retry until the
        normal idle-deletion horizon expires.
        """

        await self.initialize()
        return await asyncio.to_thread(self._list_pending_cleanup_sync)

    def _list_pending_cleanup_sync(self) -> list[SessionRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions WHERE state = ? ORDER BY deleted_at ASC, session_id ASC",
                (SessionState.DELETED.value,),
            ).fetchall()
        records = [self._from_row(row) for row in rows]
        return [record for record in records if record.metadata.get("cleanup_pending") is True]

    async def list_sessions(self, owner_user_id: str | None = None) -> list[SessionRecord]:
        """Return all durable sessions, newest activity first, including tombstones."""

        await self.initialize()
        return await asyncio.to_thread(self._list_sessions_sync, owner_user_id)

    def _list_sessions_sync(self, owner_user_id: str | None) -> list[SessionRecord]:
        with self._connection() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    "SELECT * FROM sessions ORDER BY last_activity_at DESC, session_id ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sessions WHERE owner_user_id = ? "
                    "ORDER BY last_activity_at DESC, session_id ASC",
                    (owner_user_id,),
                ).fetchall()
        return [self._from_row(row) for row in rows]

    async def create_user(self, name: str) -> UserRecord:
        await self.initialize()
        return await asyncio.to_thread(self._create_user_sync, name)

    def _create_user_sync(self, name: str) -> UserRecord:
        display_name, normalized = normalize_user_name(name)
        now = datetime.now(UTC)
        record = UserRecord(str(uuid4()), display_name, normalized, True, now, now)
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO users (user_id, name, normalized_name, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (
                        record.user_id,
                        record.name,
                        record.normalized_name,
                        _encode_timestamp(now),
                        _encode_timestamp(now),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionAlreadyExistsError("用户名称已存在") from exc
        return record

    async def get_user(self, user_id: str) -> UserRecord | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: str) -> UserRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return self._user_from_row(row) if row is not None else None

    async def find_user_by_name(self, normalized_name: str) -> UserRecord | None:
        await self.initialize()
        return await asyncio.to_thread(self._find_user_by_name_sync, normalized_name)

    def _find_user_by_name_sync(self, normalized_name: str) -> UserRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE normalized_name = ?", (normalized_name,)
            ).fetchone()
        return self._user_from_row(row) if row is not None else None

    async def list_users(self) -> list[UserRecord]:
        await self.initialize()
        return await asyncio.to_thread(self._list_users_sync)

    def _list_users_sync(self) -> list[UserRecord]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [self._user_from_row(row) for row in rows]

    async def set_user_enabled(self, user_id: str, enabled: bool) -> UserRecord:
        await self.initialize()
        return await asyncio.to_thread(self._set_user_enabled_sync, user_id, enabled)

    def _set_user_enabled_sync(self, user_id: str, enabled: bool) -> UserRecord:
        now = datetime.now(UTC)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE users SET enabled = ?, updated_at = ? WHERE user_id = ?",
                (int(enabled), _encode_timestamp(now), user_id),
            )
            if cursor.rowcount != 1:
                raise SessionRepositoryError("用户不存在")
        record = self._get_user_sync(user_id)
        assert record is not None
        return record

    async def get_managed_settings(self) -> ManagedSettingsRecord:
        await self.initialize()
        return await asyncio.to_thread(self._get_managed_settings_sync)

    def _get_managed_settings_sync(self) -> ManagedSettingsRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM managed_settings WHERE singleton = 1"
            ).fetchone()
        assert row is not None
        return ManagedSettingsRecord(
            values=json.loads(row["values_json"]),
            version=row["version"],
            updated_at=_decode_timestamp(row["updated_at"]),
        )

    async def update_managed_settings(
        self, values: Mapping[str, object], *, expected_version: int
    ) -> ManagedSettingsRecord:
        await self.initialize()
        return await asyncio.to_thread(
            self._update_managed_settings_sync, dict(values), expected_version
        )

    def _update_managed_settings_sync(
        self, values: Mapping[str, object], expected_version: int
    ) -> ManagedSettingsRecord:
        now = datetime.now(UTC)
        encoded = json.dumps(dict(values), separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE managed_settings SET values_json = ?, version = ?, updated_at = ? "
                "WHERE singleton = 1 AND version = ?",
                (encoded, expected_version + 1, _encode_timestamp(now), expected_version),
            )
            if cursor.rowcount != 1:
                raise SessionVersionConflictError("managed_settings")
        return ManagedSettingsRecord(dict(values), expected_version + 1, now)

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            user_id=row["user_id"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            enabled=bool(row["enabled"]),
            created_at=_decode_timestamp(row["created_at"]),
            updated_at=_decode_timestamp(row["updated_at"]),
        )

    async def append_log_entry(
        self,
        session_id: str,
        *,
        title: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
        event_type: str | None = None,
    ) -> SessionLogEntry:
        await self.initialize()
        return await asyncio.to_thread(
            self._append_log_entry_sync,
            session_id,
            title,
            content,
            dict(metadata or {}),
            event_type,
        )

    def _append_log_entry_sync(
        self,
        session_id: str,
        title: str,
        content: str,
        metadata: Mapping[str, object],
        event_type: str | None,
    ) -> SessionLogEntry:
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not title:
            raise ValueError("log title must not be empty")
        try:
            metadata_json = json.dumps(dict(metadata), separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("log metadata must be JSON serializable") from exc
        created_at = datetime.now(UTC)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    INSERT INTO session_log_entries (
                        session_id, created_at, title, content, metadata_json, event_type
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        _encode_timestamp(created_at),
                        title,
                        content,
                        metadata_json,
                        event_type,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return SessionLogEntry(
            sequence=int(cursor.lastrowid),
            session_id=session_id,
            created_at=created_at,
            title=title,
            content=content,
            metadata=dict(metadata),
            event_type=event_type,
        )

    async def list_log_entries(self, session_id: str) -> list[SessionLogEntry]:
        await self.initialize()
        return await asyncio.to_thread(self._list_log_entries_sync, session_id)

    def _list_log_entries_sync(self, session_id: str) -> list[SessionLogEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, session_id, created_at, title, content, metadata_json, event_type
                FROM session_log_entries WHERE session_id = ? ORDER BY sequence ASC
                """,
                (session_id,),
            ).fetchall()
        return [
            SessionLogEntry(
                sequence=row["sequence"],
                session_id=row["session_id"],
                created_at=_decode_timestamp(row["created_at"]),
                title=row["title"],
                content=row["content"],
                metadata=json.loads(row["metadata_json"]),
                event_type=row["event_type"],
            )
            for row in rows
        ]

    async def append_ui_event(
        self,
        session_id: str,
        *,
        turn_id: str,
        sequence: int,
        at: str,
        event: Mapping[str, object],
    ) -> SessionUiEvent:
        """Append one browser event once, retaining its exact safe protocol payload."""
        await self.initialize()
        return await asyncio.to_thread(
            self._append_ui_event_sync,
            session_id,
            turn_id,
            sequence,
            at,
            dict(event),
        )

    def _append_ui_event_sync(
        self,
        session_id: str,
        turn_id: str,
        sequence: int,
        at: str,
        event: Mapping[str, object],
    ) -> SessionUiEvent:
        if not session_id or not turn_id:
            raise ValueError("session_id and turn_id must not be empty")
        if sequence < 0:
            raise ValueError("UI event sequence must not be negative")
        if not at:
            raise ValueError("UI event timestamp must not be empty")
        try:
            event_json = json.dumps(dict(event), separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("UI event payload must be JSON serializable") from exc
        created_at = datetime.now(UTC)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO session_ui_events (
                        session_id, turn_id, sequence, at, event_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, turn_id, sequence) DO NOTHING
                    """,
                    (session_id, turn_id, sequence, at, event_json, _encode_timestamp(created_at)),
                )
                row = connection.execute(
                    """
                    SELECT session_id, turn_id, sequence, at, event_json, created_at
                    FROM session_ui_events
                    WHERE session_id = ? AND turn_id = ? AND sequence = ?
                    """,
                    (session_id, turn_id, sequence),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if row is None:  # pragma: no cover - SQLite INSERT/SELECT is transactional.
            raise SessionRepositoryError("UI event could not be read after append")
        # 同一个 (session, turn, sequence) 只能代表同一条不可变协议事件.
        # 静默 ON CONFLICT DO NOTHING 会把 dispatcher 的编号错误伪装成成功,
        # 从而让重放历史与实时 UI 分叉.
        if row["at"] != at or row["event_json"] != event_json:
            raise SessionUiEventConflictError(
                "UI event invariant violated: duplicate key has different payload"
            )
        return self._ui_event_from_row(row)

    async def list_ui_events(self, session_id: str) -> list[SessionUiEvent]:
        """Return replay events in their original server dispatch order."""
        await self.initialize()
        return await asyncio.to_thread(self._list_ui_events_sync, session_id)

    def _list_ui_events_sync(self, session_id: str) -> list[SessionUiEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, turn_id, sequence, at, event_json, created_at
                FROM session_ui_events WHERE session_id = ?
                ORDER BY insertion_sequence ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._ui_event_from_row(row) for row in rows]

    @staticmethod
    def _ui_event_from_row(row: sqlite3.Row) -> SessionUiEvent:
        payload = json.loads(row["event_json"])
        if not isinstance(payload, dict):  # pragma: no cover - append validates mappings.
            raise SessionRepositoryError("stored UI event payload is not an object")
        return SessionUiEvent(
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            sequence=row["sequence"],
            at=row["at"],
            event=payload,
            created_at=_decode_timestamp(row["created_at"]),
        )

    async def close(self) -> None:
        if self._anchor is not None:
            await asyncio.to_thread(self._close_anchor)

    def _close_anchor(self) -> None:
        with self._memory_guard:
            if self._anchor is not None:
                self._anchor.close()
                self._anchor = None

    @staticmethod
    def _values(record: SessionRecord, *, version: int) -> tuple[object, ...]:
        try:
            metadata_json = json.dumps(dict(record.metadata), separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("session metadata must be JSON serializable") from exc
        return (
            record.session_id,
            record.owner_user_id,
            record.sandbox_id,
            record.claude_session_id,
            record.state.value,
            _encode_timestamp(record.created_at),
            _encode_timestamp(record.last_activity_at),
            _encode_timestamp(record.paused_at),
            _encode_timestamp(record.deleted_at),
            version,
            metadata_json,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            owner_user_id=row["owner_user_id"],
            sandbox_id=row["sandbox_id"],
            claude_session_id=row["claude_session_id"],
            state=SessionState(row["state"]),
            created_at=_decode_timestamp(row["created_at"]),
            last_activity_at=_decode_timestamp(row["last_activity_at"]),
            paused_at=_decode_timestamp(row["paused_at"]),
            deleted_at=_decode_timestamp(row["deleted_at"]),
            version=row["version"],
            metadata=json.loads(row["metadata_json"]),
        )
