"""Session persistence, lifecycle primitives, and in-process locking."""

from .locks import SessionLockRegistry, SessionLocks
from .models import SessionRecord, SessionState, SessionTurnRequest, utc_now
from .repository import (
    SessionAlreadyExistsError,
    SessionLogEntry,
    SessionRepository,
    SessionRepositoryError,
    SessionVersionConflictError,
    SQLiteSessionRepository,
)
from .state_machine import (
    InvalidSessionTransition,
    SessionStateMachine,
    can_transition,
    touch,
    transition,
)

__all__ = [
    "InvalidSessionTransition",
    "SQLiteSessionRepository",
    "SessionAlreadyExistsError",
    "SessionLockRegistry",
    "SessionLocks",
    "SessionLogEntry",
    "SessionRecord",
    "SessionRepository",
    "SessionRepositoryError",
    "SessionState",
    "SessionStateMachine",
    "SessionTurnRequest",
    "SessionVersionConflictError",
    "can_transition",
    "touch",
    "transition",
    "utc_now",
]
