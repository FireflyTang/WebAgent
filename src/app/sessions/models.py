"""Persistent session domain models.

The record deliberately stores runtime-specific, evolving fields in ``metadata``.
That keeps the durable session-to-sandbox-to-runtime mapping small while allowing
new runtimes to retain data such as a pending question or user-message digest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from app.runtime.events import Effort, validate_effort


class SessionState(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DELETED = "DELETED"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SessionTurnRequest:
    """WebAgent's transport-neutral input for one session turn."""

    message: str
    model: str
    system_prompt: str | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("message must not be empty")
        if not self.model.strip():
            raise ValueError("model must not be empty")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """The durable mapping for one externally-addressable conversation."""

    session_id: str
    owner_user_id: str | None = None
    state: SessionState = SessionState.ACTIVE
    sandbox_id: str | None = None
    claude_session_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    last_activity_at: datetime = field(default_factory=utc_now)
    paused_at: datetime | None = None
    deleted_at: datetime | None = None
    version: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.version < 0:
            raise ValueError("version must be non-negative")
        # Copy so caller-side mutation cannot silently alter a record already
        # handed to the repository.
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def pending_interaction(self) -> Any | None:
        """Optional runtime prompt/permission state stored in metadata."""

        return self.metadata.get("pending_interaction")

    @property
    def last_user_fingerprint(self) -> str | None:
        """Digest of the latest accepted user input, if recorded by a service."""

        value = self.metadata.get("last_user_fingerprint")
        return value if isinstance(value, str) else None

    @property
    def title(self) -> str | None:
        """Optional user-managed title, kept with the durable session mapping."""

        value = self.metadata.get("title")
        return value if isinstance(value, str) else None

    @property
    def last_model(self) -> str | None:
        """The last model selected for this session, if any."""

        value = self.metadata.get("last_model")
        return value if isinstance(value, str) else None

    @property
    def last_effort(self) -> Effort | None:
        """The persisted effort to apply to a later WebSocket turn, if selected."""

        try:
            return validate_effort(self.metadata.get("last_effort"))
        except ValueError:
            return None

    def with_metadata(self, **values: Any) -> SessionRecord:
        metadata = dict(self.metadata)
        metadata.update(values)
        return replace(self, metadata=metadata)
