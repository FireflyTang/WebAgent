"""Pure lifecycle rules for durable sessions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import SessionRecord, SessionState, utc_now


class InvalidSessionTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.ACTIVE: frozenset(
        {SessionState.ACTIVE, SessionState.PAUSED, SessionState.DELETED}
    ),
    SessionState.PAUSED: frozenset({SessionState.ACTIVE, SessionState.DELETED}),
    SessionState.DELETED: frozenset(),
}


def can_transition(current: SessionState, target: SessionState) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


def transition(
    record: SessionRecord, target: SessionState, *, now: datetime | None = None
) -> SessionRecord:
    """Return an updated record, rejecting resurrection of a tombstone.

    Persistence and version increments remain the repository's responsibility;
    this function has no side effects and is therefore easy to exercise with a
    deterministic clock.
    """

    if not can_transition(record.state, target):
        raise InvalidSessionTransition(
            f"cannot transition session {record.session_id!r} "
            f"from {record.state.value} to {target.value}"
        )

    current_time = now or utc_now()
    if target is SessionState.ACTIVE:
        return replace(
            record,
            state=SessionState.ACTIVE,
            last_activity_at=current_time,
            paused_at=None,
        )
    if target is SessionState.PAUSED:
        return replace(record, state=SessionState.PAUSED, paused_at=current_time)
    return replace(record, state=SessionState.DELETED, deleted_at=current_time)


def touch(record: SessionRecord, *, now: datetime | None = None) -> SessionRecord:
    """Refresh activity for an active turn without allowing a paused/deleted turn."""

    if record.state is not SessionState.ACTIVE:
        raise InvalidSessionTransition(f"cannot touch a {record.state.value} session")
    return replace(record, last_activity_at=now or utc_now())


class SessionStateMachine:
    """Small namespace-style facade for callers that prefer an object API."""

    can_transition = staticmethod(can_transition)
    transition = staticmethod(transition)
    touch = staticmethod(touch)
