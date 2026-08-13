from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import websockets


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
            connection.request("GET", "/healthz")
            if connection.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Uvicorn did not become healthy")


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        payload = json.dumps({"data": [{"id": "provider-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


async def _receive_ready(
    websocket, session_id: str
) -> tuple[dict[str, object], list[dict[str, object]]]:
    begin = json.loads(await websocket.recv())
    assert begin == {"type": "sync_begin", "session_id": session_id}
    replay: list[dict[str, object]] = []
    while True:
        event = json.loads(await websocket.recv())
        if event["type"] == "ready":
            return event, replay
        replay.append(event)


async def _run_turn(uri: str, provider_url: str, session_id: str) -> list[dict[str, object]]:
    async with websockets.connect(uri, open_timeout=5, close_timeout=5) as websocket:
        await websocket.send(json.dumps({"type": "hello", "session_id": session_id}))
        ready, _replay = await _receive_ready(websocket, session_id)
        assert ready["task_state"] == "idle"
        await websocket.send(
            json.dumps(
                {
                    "type": "message",
                    "content": "创建一个计算器，实现加法并运行测试",
                    "model": "provider-model",
                    "provider": {
                        "base_url": provider_url,
                        "api_key": f"key-{session_id}",
                        "auth_env": "ANTHROPIC_AUTH_TOKEN",
                    },
                }
            )
        )
        events: list[dict[str, object]] = []
        while True:
            event = json.loads(await websocket.recv())
            events.append(event)
            if event["type"] == "done":
                return events


async def _abort_after_tool_progress(
    uri: str, provider_url: str, session_id: str, *, tcp_abort: bool
) -> list[dict[str, object]]:
    async with websockets.connect(uri, open_timeout=5, close_timeout=5) as websocket:
        await websocket.send(json.dumps({"type": "hello", "session_id": session_id}))
        ready, _replay = await _receive_ready(websocket, session_id)
        assert ready["task_state"] == "idle"
        await websocket.send(
            json.dumps(
                {
                    "type": "message",
                    "content": "创建一个计算器，实现加法并运行测试",
                    "model": "provider-model",
                    "provider": {
                        "base_url": provider_url,
                        "api_key": f"key-{session_id}",
                        "auth_env": "ANTHROPIC_AUTH_TOKEN",
                    },
                }
            )
        )
        events: list[dict[str, object]] = []
        while True:
            event = json.loads(await websocket.recv())
            events.append(event)
            if event["type"] == "progress" and event.get("tool_name") == "Write":
                if tcp_abort:
                    websocket.transport.abort()
                return events


async def _run_abort_and_survivor(
    uri: str, provider_url: str, *, tcp_abort: bool
) -> tuple[list[dict[str, object]], ...]:
    return await asyncio.wait_for(
        asyncio.gather(
            _abort_after_tool_progress(uri, provider_url, "one", tcp_abort=tcp_abort),
            _run_turn(uri, provider_url, "two"),
        ),
        timeout=10,
    )


def _history(port: int, session_id: str) -> list[dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", f"/v1/sessions/{session_id}/history")
    response = connection.getresponse()
    assert response.status == 200
    payload = json.loads(response.read())
    return payload["events"]


def _history_until_done(port: int, session_id: str) -> list[dict[str, object]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = _history(port, session_id)
        if any(event["type"] == "done" for event in events):
            return events
        time.sleep(0.05)
    return _history(port, session_id)


@pytest.mark.parametrize("tcp_abort", [False, True], ids=["graceful-close", "tcp-abort"])
def test_disconnect_keeps_its_background_turn_and_other_session_completes(
    tmp_path: Path, tcp_abort: bool
) -> None:
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    port = _free_port()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(Path.cwd() / "src"),
            "RUNTIME_BACKEND": "fake",
            "SANDBOX_BACKEND": "local",
            "DATABASE_URL": f"sqlite:///{tmp_path / 'sessions.db'}",
            "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "FAKE_STREAM_DELAY_MS": "40",
            "FAKE_LONG_TASK_DELAY_MS": "40",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(port)
        provider_url = f"http://127.0.0.1:{provider.server_port}"
        uri = f"ws://127.0.0.1:{port}/ws/chat"
        one, two = asyncio.run(_run_abort_and_survivor(uri, provider_url, tcp_abort=tcp_abort))
        completed_history = _history_until_done(port, "one")
        retry_one = asyncio.run(_run_turn(uri, provider_url, "one"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)

    assert one[0]["type"] == "user_message"
    assert all(event["session_id"] == "one" for event in one)
    assert one[-1]["type"] == "progress"
    completed = [event for event in completed_history if event["type"] == "done"]
    assert len(completed) == 1
    assert completed[0]["completed"] is True
    assert completed[0]["stop_reason"] == "stop"
    assert two[0]["type"] == "user_message"
    assert all(event["session_id"] == "two" for event in two)
    assert two[-1]["type"] == "done"
    assert two[-1]["completed"] is True
    assert any(event["type"] == "progress" for event in two)
    assert retry_one[0]["type"] == "user_message"
    assert all(event["session_id"] == "one" for event in retry_one)
    assert retry_one[-1]["completed"] is True
