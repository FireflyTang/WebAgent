import json
from pathlib import Path

import httpx
import pytest

from app.runtime.base import RuntimeContext
from app.runtime.events import Completed, Failed, TextDelta, Usage
from app.runtime.zhipu import ZhipuRuntime


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test")


async def collect(runtime, runtime_id, message, context):
    return [event async for event in runtime.send_message(runtime_id, message, context)]


@pytest.mark.asyncio
async def test_zhipu_runtime_writes_plan_runs_tests_and_persists_state(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "glm-4.5-air"
        assert body["thinking"] == {"type": "disabled"}
        assert request.headers["authorization"] == "Bearer test-key"
        plan = {
            "assistant_message": "Implemented it.",
            "files": [
                {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
                {
                    "path": "test_calculator.py",
                    "content": "import unittest\nfrom calculator import add\n\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
                },
            ],
            "run_tests": True,
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(plan)}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    async with client_for(handler) as client:
        runtime = ZhipuRuntime("test-key", client=client)
        runtime_id = await runtime.create_session(context)
        events = await collect(runtime, runtime_id, "make an adder", context)

    assert (tmp_path / "calculator.py").exists()
    assert "Implemented it." in "".join(e.text for e in events if isinstance(e, TextDelta))
    assert any(isinstance(e, Usage) and e.input_tokens == 11 for e in events)
    assert any(isinstance(e, Completed) and e.stop_reason == "stop" for e in events)
    assert json.loads((tmp_path / ".zhipu-runtime.json").read_text()) == {
        "turn": 1,
        "status": "active",
    }


@pytest.mark.asyncio
async def test_zhipu_runtime_rejects_invalid_or_unsafe_model_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        plan = {
            "assistant_message": "oops",
            "files": [{"path": "../escape.py", "content": "bad"}],
            "run_tests": False,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    async with client_for(handler) as client:
        runtime = ZhipuRuntime("test-key", client=client)
        runtime_id = await runtime.create_session(context)
        events = await collect(runtime, runtime_id, "bad", context)

    assert isinstance(events[0], Failed)
    assert events[0].code == "workspace_write_failed"
    assert not (tmp_path.parent / "escape.py").exists()


@pytest.mark.asyncio
async def test_zhipu_runtime_pause_resume_and_invalid_json(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    async with client_for(handler) as client:
        runtime = ZhipuRuntime("test-key", client=client)
        runtime_id = await runtime.create_session(context)
        await runtime.pause(runtime_id, context)
        paused = await collect(runtime, runtime_id, "hello", context)
        assert paused[0].code == "runtime_paused"
        await runtime.resume(runtime_id, context)
        invalid = await collect(runtime, runtime_id, "hello", context)
        await runtime.close(runtime_id, context)

    assert isinstance(invalid[0], Failed)
    assert invalid[0].code == "zhipu_invalid_response"
    assert json.loads((tmp_path / ".zhipu-runtime.json").read_text())["status"] == "closed"


@pytest.mark.asyncio
async def test_zhipu_runtime_context_model_overrides_runtime_default(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "glm-5.2"
        plan = {"assistant_message": "ok", "files": [], "run_tests": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    context = RuntimeContext("session-a", "sandbox-a", tmp_path, model="glm-5.2")
    async with client_for(handler) as client:
        runtime = ZhipuRuntime("test-key", model="glm-4.7", client=client)
        runtime_id = await runtime.create_session(context)
        events = await collect(runtime, runtime_id, "hello", context)

    assert any(isinstance(event, Completed) for event in events)


@pytest.mark.asyncio
async def test_zhipu_runtime_openai_model_alias_keeps_runtime_default(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "glm-4.7"
        plan = {"assistant_message": "ok", "files": [], "run_tests": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan)}}]})

    context = RuntimeContext("session-a", "sandbox-a", tmp_path, model="claude-code-agent")
    async with client_for(handler) as client:
        runtime = ZhipuRuntime("test-key", model="glm-4.7", client=client)
        runtime_id = await runtime.create_session(context)
        events = await collect(runtime, runtime_id, "hello", context)

    assert any(isinstance(event, Completed) for event in events)
