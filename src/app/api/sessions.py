from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from app.api.identity import optional_user, require_owned_session
from app.sessions.models import SessionRecord
from app.sessions.repository import SessionLogEntry
from app.sessions.service import FileTooLargeError, OpenedWorkspaceFile, SessionService
from app.sessions.ui_events import ActiveTurnRegistry, UiEventJournal

router = APIRouter(prefix="/v1/sessions", tags=["demo-sessions"])


class _OpenedWorkspaceFileResponse(StreamingResponse):
    """Close the descriptor on every ASGI exit, including disconnect/error."""

    def __init__(
        self,
        opened: OpenedWorkspaceFile,
        *,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        self._opened = opened
        super().__init__(opened.chunks(), media_type=media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._opened.close()


def _service(request: Request) -> SessionService:
    return request.app.state.session_service


def _logger(request: Request):
    return request.app.state.session_logger


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    last_model: str | None = Field(default=None, max_length=200)
    last_effort: str | None = Field(default=None, max_length=10)


class SessionPatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    last_model: str | None = Field(default=None, max_length=200)
    last_effort: str | None = Field(default=None, max_length=10)


class EditorFileSaveRequest(BaseModel):
    content: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    force: bool = False


_UPLOAD_READ_CHUNK_BYTES = 64 * 1024


def _safe_upload_name(upload: UploadFile) -> str:
    raw_name = upload.filename or "unnamed"
    name = PurePosixPath(raw_name.replace("\\", "/")).name or "unnamed"
    return "".join(character if character.isprintable() else "?" for character in name)[:200]


async def _read_upload_bounded(upload: UploadFile, max_bytes: int) -> bytes:
    """Read one multipart part without an unbounded ``UploadFile.read()`` call."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(_UPLOAD_READ_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLargeError(
                f"Upload {_safe_upload_name(upload)!r} exceeds the {max_bytes}-byte per-file limit"
            )
        chunks.append(chunk)


def _view(
    record: SessionRecord,
    service: SessionService,
    active_turns: ActiveTurnRegistry,
) -> dict[str, object]:
    state, delete_at = service.lifecycle_view(record)
    compatible, compatibility_reason = service.compatibility_view(record)
    task = active_turns.snapshot(record.session_id)
    return {
        "session_id": record.session_id,
        "owner_user_id": record.owner_user_id,
        "sandbox_id": record.sandbox_id,
        "runtime_session_id": record.claude_session_id,
        "state": state,
        "compatible": compatible,
        "compatibility_reason": compatibility_reason,
        "delete_at": delete_at.isoformat() if delete_at else None,
        "title": record.title,
        "last_model": record.last_model,
        "last_effort": record.last_effort,
        "created_at": record.created_at.isoformat(),
        "last_activity_at": record.last_activity_at.isoformat(),
        "paused_at": record.paused_at.isoformat() if record.paused_at else None,
        "deleted_at": record.deleted_at.isoformat() if record.deleted_at else None,
        "version": record.version,
        "metadata": dict(record.metadata),
        "task_state": task["task_state"],
        "active_turn_id": task["turn_id"],
        "last_turn_sequence": task["last_sequence"],
    }


@router.get("")
async def list_sessions(request: Request) -> dict[str, object]:
    service = _service(request)
    user = await optional_user(request)
    records = await service.repository.list_sessions(user.user_id if user is not None else None)
    now = datetime.now(UTC).isoformat()
    return {
        "server_now": now,
        "sessions": [_view(record, service, request.app.state.active_turns) for record in records],
    }


@router.post("", status_code=201)
async def create_session(payload: SessionCreateRequest, request: Request) -> dict[str, object]:
    service = _service(request)
    user = await optional_user(request)
    try:
        record = await service.create_empty(
            str(uuid4()),
            title=payload.title,
            last_model=payload.last_model,
            last_effort=payload.last_effort,
            owner_user_id=user.user_id if user is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _view(record, service, request.app.state.active_turns)


@router.patch("/{session_id}")
async def patch_session(
    session_id: str, payload: SessionPatchRequest, request: Request
) -> dict[str, object]:
    await require_owned_session(request, session_id)
    fields = payload.model_fields_set
    try:
        record = await _service(request).update_presentation(
            session_id,
            **({"title": payload.title} if "title" in fields else {}),
            **({"last_model": payload.last_model} if "last_model" in fields else {}),
            **({"last_effort": payload.last_effort} if "last_effort" in fields else {}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _view(record, _service(request), request.app.state.active_turns)


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    service = _service(request)
    return _view(await service.get(session_id), service, request.app.state.active_turns)


@router.get("/{session_id}/transcript")
async def get_session_transcript(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    entries = await _service(request).transcript(session_id)
    return {
        "session_id": session_id,
        "messages": [_transcript_message(entry) for entry in entries],
    }


def _transcript_message(entry: SessionLogEntry) -> dict[str, object]:
    """Project a durable high-level log entry into the stable transcript shape."""
    return {
        "sequence": entry.sequence,
        "role": "user" if entry.title == "用户消息" else "assistant",
        "content": entry.content,
        "created_at": entry.created_at.isoformat(),
        "model": entry.metadata.get("模型"),
    }


@router.get("/{session_id}/history")
async def get_session_history(session_id: str, request: Request) -> dict[str, object]:
    """Return replayable browser UI events for this session only."""
    await require_owned_session(request, session_id)
    service = _service(request)
    await service.get(session_id)
    journal: UiEventJournal = request.app.state.ui_event_journal
    return {
        "session_id": session_id,
        "events": await journal.list_events(session_id),
    }


@router.get("/{session_id}/log", response_class=HTMLResponse)
async def get_session_log(session_id: str, request: Request) -> HTMLResponse:
    await require_owned_session(request, session_id)
    await _service(request).get(session_id)
    document = await _logger(request).read_diagnostics(session_id)
    return HTMLResponse(document, headers={"Content-Disposition": "inline"})


@router.get("/{session_id}/files")
async def list_session_files(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    return {"session_id": session_id, "files": await _service(request).list_files(session_id)}


@router.get("/{session_id}/files/content/{file_path:path}")
async def get_session_file_content(
    session_id: str, file_path: str, request: Request
) -> StreamingResponse:
    """Return one authenticated session file for browser-controlled viewing/downloading."""
    await require_owned_session(request, session_id)
    opened = await _service(request).open_file(session_id, file_path)
    media_type = mimetypes.guess_type(opened.normalized_path)[0] or "application/octet-stream"
    filename = quote(PurePosixPath(opened.normalized_path).name)
    try:
        return _OpenedWorkspaceFileResponse(
            opened,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"},
        )
    except BaseException:
        opened.close()
        raise


@router.get("/{session_id}/files/editor/{file_path:path}")
async def get_session_editor_file(
    session_id: str, file_path: str, request: Request
) -> dict[str, object]:
    """Return bounded UTF-8 editor content from the inode opened by the service."""
    await require_owned_session(request, session_id)
    return await _service(request).inspect_editor_file(
        session_id, file_path, max_editor_bytes=request.app.state.settings.file_editor_max_bytes
    )


@router.put("/{session_id}/files/editor/{file_path:path}")
async def save_session_editor_file(
    session_id: str, file_path: str, payload: EditorFileSaveRequest, request: Request
) -> dict[str, object]:
    await require_owned_session(request, session_id)
    return await _service(request).save_editor_file(
        session_id,
        file_path,
        content=payload.content,
        expected_revision=payload.expected_revision,
        force=payload.force,
        max_editor_bytes=request.app.state.settings.file_editor_max_bytes,
    )


@router.post("/{session_id}/files")
async def upload_session_files(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    await require_owned_session(request, session_id)
    max_bytes = request.app.state.settings.file_upload_max_bytes
    # Read and validate every part before calling the service, so a rejected
    # batch cannot leave an earlier uploaded file behind in the workspace.
    payload = [(file.filename or "", await _read_upload_bounded(file, max_bytes)) for file in files]
    uploaded = await _service(request).upload_files(
        session_id,
        payload,
        max_files_per_session=request.app.state.settings.file_upload_max_files_per_session,
    )
    return {"session_id": session_id, "files": uploaded}


@router.post("/{session_id}/pause")
async def pause_session(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    service = _service(request)
    return _view(await service.pause(session_id), service, request.app.state.active_turns)


@router.post("/{session_id}/resume")
async def resume_session(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    service = _service(request)
    return _view(await service.resume(session_id), service, request.app.state.active_turns)


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, object]:
    await require_owned_session(request, session_id)
    service = _service(request)
    return _view(await service.delete(session_id), service, request.app.state.active_turns)
