from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.config import Settings
from app.main import create_app
from app.sessions.service import OpenedWorkspaceFile


def _settings(tmp_path: Path, *, limit: int = 32, max_upload_files: int = 10) -> Settings:
    return Settings(
        sandbox_backend="local",
        runtime_backend="fake",
        database_url=f"sqlite:///{tmp_path / 'editor.db'}",
        workspace_root=tmp_path / "workspaces",
        fake_stream_delay_ms=0,
        session_pause_after_seconds=60,
        session_delete_after_seconds=120,
        session_reaper_interval_seconds=60,
        file_editor_max_bytes=limit,
        file_upload_max_files_per_session=max_upload_files,
    )


def _upload(client: TestClient, name: str, content: bytes) -> None:
    response = client.post("/v1/sessions/editor/files", files=[("files", (name, content))])
    assert response.status_code == 200


def test_editor_classifies_text_binary_and_limit_and_saves_atomically(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        _upload(client, "code.py", b"answer = 1\n")
        opened = client.get("/v1/sessions/editor/files/editor/code.py")
        assert opened.status_code == 200
        payload = opened.json()
        assert payload["kind"] == "text" and payload["editable"] is True
        assert payload["language"] == "python" and payload["content"] == "answer = 1\n"
        saved = client.put(
            "/v1/sessions/editor/files/editor/code.py",
            json={"content": "answer = 2\n", "expected_revision": payload["revision"]},
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == hashlib.sha256(b"answer = 2\n").hexdigest()
        stale = client.put(
            "/v1/sessions/editor/files/editor/code.py",
            json={"content": "answer = 3\n", "expected_revision": payload["revision"]},
        )
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "file_changed"
        forced = client.put(
            "/v1/sessions/editor/files/editor/code.py",
            json={
                "content": "answer = 3\n",
                "expected_revision": payload["revision"],
                "force": True,
            },
        )
        assert forced.status_code == 200
        _upload(client, "blob.bin", b"a\0b")
        binary = client.get("/v1/sessions/editor/files/editor/blob.bin").json()
        assert (
            binary["kind"] == "binary" and binary["reason"] == "binary" and "content" not in binary
        )
        _upload(client, "unknown", "文本\n".encode())
        assert (
            client.get("/v1/sessions/editor/files/editor/unknown").json()["language"] == "plaintext"
        )
        _upload(client, "bad.txt", b"\xff")
        assert client.get("/v1/sessions/editor/files/editor/bad.txt").json()["kind"] == "binary"
        _upload(client, "big.txt", b"x" * 33)
        large = client.get("/v1/sessions/editor/files/editor/big.txt").json()
        assert large["editable"] is False and large["reason"] == "too_large"
        assert "content" not in large and "revision" not in large
        too_large = client.put(
            "/v1/sessions/editor/files/editor/code.py",
            json={"content": "z" * 33, "expected_revision": forced.json()["revision"]},
        )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "file_too_large"
        malformed = client.put(
            "/v1/sessions/editor/files/editor/code.py",
            json={"content": "x", "expected_revision": "UPPER" * 13},
        )
        assert malformed.status_code == 400


def test_editor_setting_persists_and_running_turn_rejects_save(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        initial = client.get("/v1/admin/settings").json()
        updated = client.patch(
            "/v1/admin/settings",
            json={
                "version": initial["version"],
                "file_editor_max_bytes": 99,
                "file_upload_max_bytes": 77,
                "file_upload_max_files_per_session": 7,
            },
        )
        assert updated.status_code == 200 and updated.json()["saved"]["file_editor_max_bytes"] == 99
        assert updated.json()["saved"]["file_upload_max_bytes"] == 77
        assert updated.json()["saved"]["file_upload_max_files_per_session"] == 7
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/admin/settings").json()["active"]["file_editor_max_bytes"] == 99
        assert client.get("/v1/admin/settings").json()["active"]["file_upload_max_bytes"] == 77
        assert (
            client.get("/v1/admin/settings").json()["active"]["file_upload_max_files_per_session"]
            == 7
        )
        assert client.get("/v1/web/config").json()["policies"]["file_editor_max_bytes"] == 99
        assert client.get("/v1/web/config").json()["policies"]["file_upload_max_bytes"] == 77
        assert (
            client.get("/v1/web/config").json()["policies"]["file_upload_max_files_per_session"]
            == 7
        )


def test_uploads_apply_a_per_file_limit_without_partial_writes_or_unbounded_reads(
    tmp_path: Path, monkeypatch
) -> None:
    observed_sizes: list[int] = []
    original_read = UploadFile.read

    async def bounded_read(self, size: int = -1) -> bytes:
        assert 0 < size <= 64 * 1024
        observed_sizes.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", bounded_read)
    limit = 2 * 1024 * 1024
    with TestClient(create_app(_settings(tmp_path))) as client:
        exact = client.post(
            "/v1/sessions/exact/files", files=[("files", ("exact.bin", b"x" * limit))]
        )
        over = client.post(
            "/v1/sessions/over/files", files=[("files", ("over.bin", b"x" * (limit + 1)))]
        )
        atomic_id = client.post("/v1/sessions", json={}).json()["session_id"]
        atomic = client.post(
            f"/v1/sessions/{atomic_id}/files",
            files=[
                ("files", ("accepted.txt", b"would otherwise be written")),
                ("files", ("rejected.bin", b"z" * (limit + 1))),
            ],
        )
        listed = client.get(f"/v1/sessions/{atomic_id}/files")

    assert exact.status_code == 200
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "file_too_large"
    assert "over.bin" in over.json()["error"]["message"]
    assert atomic.status_code == 413
    assert listed.json()["files"] == []
    assert observed_sizes and max(observed_sizes) == 64 * 1024


def test_upload_file_count_is_durable_cumulative_and_atomic_for_each_batch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_upload_files=3)
    with TestClient(create_app(settings)) as client:
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]
        first = client.post(
            f"/v1/sessions/{session_id}/files", files=[("files", ("same.txt", b"first"))]
        )
        overwrite = client.post(
            f"/v1/sessions/{session_id}/files", files=[("files", ("same.txt", b"second"))]
        )
        before = client.get(f"/v1/sessions/{session_id}").json()
        rejected = client.post(
            f"/v1/sessions/{session_id}/files",
            files=[
                ("files", ("first-rejected.txt", b"a")),
                ("files", ("second-rejected.txt", b"b")),
            ],
        )
        files = client.get(f"/v1/sessions/{session_id}/files").json()["files"]

    assert first.status_code == 200 and overwrite.status_code == 200
    assert before["metadata"]["uploaded_file_count"] == 2
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "file_upload_limit"
    assert files == [{"path": "same.txt", "size": 6}]

    with TestClient(create_app(settings)) as client:
        restored = client.get(f"/v1/sessions/{session_id}").json()
    assert restored["metadata"]["uploaded_file_count"] == 2


def test_concurrent_upload_batches_serialize_the_session_file_count(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, max_upload_files=1))) as client:
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]

        def upload(name: str) -> int:
            return client.post(
                f"/v1/sessions/{session_id}/files", files=[("files", (name, b"x"))]
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as workers:
            statuses = list(workers.map(upload, ("one.txt", "two.txt")))
        session = client.get(f"/v1/sessions/{session_id}").json()

    assert sorted(statuses) == [200, 413]
    assert session["metadata"]["uploaded_file_count"] == 1


def test_editor_rejects_path_replacement_and_preserves_mode(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        _upload(client, "mode.txt", b"old")
        session = client.get("/v1/sessions/editor").json()
        target = tmp_path / "workspaces" / session["sandbox_id"] / "mode.txt"
        os.chmod(target, 0o640)
        revision = client.get("/v1/sessions/editor/files/editor/mode.txt").json()["revision"]
        target.write_text("agent edit", encoding="utf-8")
        rejected = client.put(
            "/v1/sessions/editor/files/editor/mode.txt",
            json={"content": "mine", "expected_revision": revision},
        )
        assert rejected.status_code == 409
        current = client.get("/v1/sessions/editor/files/editor/mode.txt").json()
        saved = client.put(
            "/v1/sessions/editor/files/editor/mode.txt",
            json={"content": "mine", "expected_revision": current["revision"]},
        )
        assert saved.status_code == 200
        assert target.read_text() == "mine" and (target.stat().st_mode & 0o777) == 0o640


def test_editor_rejects_replacement_between_revision_check_and_atomic_replace(
    tmp_path: Path, monkeypatch
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        _upload(client, "race.txt", b"original")
        session = client.get("/v1/sessions/editor").json()
        target = tmp_path / "workspaces" / session["sandbox_id"] / "race.txt"
        revision = client.get("/v1/sessions/editor/files/editor/race.txt").json()["revision"]
        replacement = target.with_name("replacement.txt")
        replacement.write_bytes(b"agent replacement")
        trigger = target.with_name("trigger.txt")
        trigger.write_bytes(b"trigger")
        original_replace = os.replace

        def replace_with_competing_target(source, destination, *args, **kwargs) -> None:
            if Path(destination) == target:
                original_replace(replacement, target)
            original_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace_with_competing_target)
        service = client.app.state.session_service
        # The hook runs after the final preflight stat.  Simulate a target
        # replacement from inside a monkeypatched os.replace call, which is
        # precisely the old final-call race.
        service._editor_revision_checked_hook = lambda: os.replace(trigger, target)
        try:
            response = client.put(
                "/v1/sessions/editor/files/editor/race.txt",
                json={"content": "mine", "expected_revision": revision},
            )
        finally:
            service._editor_revision_checked_hook = None

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "file_changed"
    assert target.read_bytes() == b"trigger"


def test_editor_bounds_large_file_reads_and_classifies_controls(
    tmp_path: Path, monkeypatch
) -> None:
    observed_limits: list[int | None] = []
    original_read_all = OpenedWorkspaceFile.read_all

    def record_read_all(self, limit: int | None = None) -> bytes:
        observed_limits.append(limit)
        return original_read_all(self, limit)

    monkeypatch.setattr(OpenedWorkspaceFile, "read_all", record_read_all)
    with TestClient(create_app(_settings(tmp_path, limit=128 * 1024))) as client:
        _upload(client, "controls.txt", b"tab\tand newline\n")
        _upload(client, "del.txt", b"ok\x7fno")
        _upload(client, "c1.txt", "ok\u0085no".encode())
        session = client.get("/v1/sessions/editor").json()
        huge = tmp_path / "workspaces" / session["sandbox_id"] / "huge.txt"
        with huge.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024)

        assert client.get("/v1/sessions/editor/files/editor/controls.txt").json()["kind"] == "text"
        assert client.get("/v1/sessions/editor/files/editor/del.txt").json()["kind"] == "binary"
        assert client.get("/v1/sessions/editor/files/editor/c1.txt").json()["kind"] == "binary"
        large = client.get("/v1/sessions/editor/files/editor/huge.txt")
        rejected = client.put(
            "/v1/sessions/editor/files/editor/huge.txt",
            json={"content": "small", "expected_revision": "0" * 64, "force": True},
        )

    assert large.status_code == 200
    assert large.json()["reason"] == "too_large"
    assert "content" not in large.json() and "revision" not in large.json()
    assert observed_limits[-1] == 64 * 1024
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "file_too_large"


def test_editor_save_rejects_an_active_agent_turn(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        _upload(client, "busy.txt", b"old")
        revision = client.get("/v1/sessions/editor/files/editor/busy.txt").json()["revision"]
        lock = client.app.state.session_service.locks.lock_for("editor")
        client.portal.call(lock.acquire)
        try:
            response = client.put(
                "/v1/sessions/editor/files/editor/busy.txt",
                json={"content": "new", "expected_revision": revision},
            )
        finally:
            client.portal.call(lock.release)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_busy"


def test_editor_replace_never_follows_symlinks_or_leaves_temporary_files(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        _upload(client, "nested/value.txt", b"old")
        session = client.get("/v1/sessions/editor").json()
        workspace = tmp_path / "workspaces" / session["sandbox_id"]
        opened = client.get("/v1/sessions/editor/files/editor/nested/value.txt").json()
        saved = client.put(
            "/v1/sessions/editor/files/editor/nested/value.txt",
            json={"content": "new", "expected_revision": opened["revision"]},
        )
        assert saved.status_code == 200
        assert not list(workspace.rglob(".webagent-editor-*"))

        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "nested").rename(workspace / "real-nested")
        (workspace / "nested").symlink_to(outside, target_is_directory=True)
        intermediate = client.put(
            "/v1/sessions/editor/files/editor/nested/value.txt",
            json={"content": "no", "expected_revision": saved.json()["revision"], "force": True},
        )
        assert intermediate.status_code == 400
        assert intermediate.json()["error"]["code"] == "invalid_workspace_path"

        (workspace / "nested").unlink()
        (workspace / "real-nested").rename(workspace / "nested")
        (workspace / "nested" / "value.txt").unlink()
        (workspace / "nested" / "value.txt").symlink_to(outside / "outside.txt")
        final = client.put(
            "/v1/sessions/editor/files/editor/nested/value.txt",
            json={"content": "no", "expected_revision": saved.json()["revision"], "force": True},
        )
        assert final.status_code == 400
        assert final.json()["error"]["code"] == "invalid_workspace_path"
        assert not list(workspace.rglob(".webagent-editor-*"))
