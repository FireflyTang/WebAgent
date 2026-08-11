from pathlib import Path

import pytest

from app.runtime import Completed, FakeRuntime, InteractionRequest, RuntimeContext, TextDelta


async def collect(runtime: FakeRuntime, runtime_id: str, message: str, context: RuntimeContext):
    return [event async for event in runtime.send_message(runtime_id, message, context)]


@pytest.mark.asyncio
async def test_fake_runtime_creates_and_updates_calculator(tmp_path: Path) -> None:
    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    runtime = FakeRuntime(0, 0)
    runtime_id = await runtime.create_session(context)

    first = await collect(runtime, runtime_id, "创建一个计算器，实现加法", context)
    assert any(isinstance(event, Completed) for event in first)
    assert "def add" in (tmp_path / "calculator.py").read_text()
    assert "测试通过" in "".join(event.text for event in first if isinstance(event, TextDelta))

    second = await collect(runtime, runtime_id, "现在增加减法", context)
    assert "def subtract" in (tmp_path / "calculator.py").read_text()
    assert "test_subtract" in (tmp_path / "test_calculator.py").read_text()
    assert "第 2 轮" in "".join(event.text for event in second if isinstance(event, TextDelta))


@pytest.mark.asyncio
async def test_fake_runtime_choice_round_trip(tmp_path: Path) -> None:
    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    runtime = FakeRuntime(0, 0)
    runtime_id = await runtime.create_session(context)

    events = await collect(runtime, runtime_id, "mock:choice", context)
    # The structured interaction is formatted as text by this demo runtime, while
    # its pending state is persisted for the next HTTP turn.
    assert not any(isinstance(event, InteractionRequest) for event in events)
    assert "A. 最小修改" in "".join(event.text for event in events if isinstance(event, TextDelta))

    continued = await collect(runtime, runtime_id, "A", context)
    assert "已记录你的选择" in "".join(
        event.text for event in continued if isinstance(event, TextDelta)
    )


@pytest.mark.asyncio
async def test_fake_runtime_pause_and_resume(tmp_path: Path) -> None:
    context = RuntimeContext("session-a", "sandbox-a", tmp_path)
    runtime = FakeRuntime(0, 0)
    runtime_id = await runtime.create_session(context)

    await runtime.pause(runtime_id, context)
    paused = await collect(runtime, runtime_id, "hello", context)
    assert paused[0].code == "runtime_paused"

    await runtime.resume(runtime_id, context)
    resumed = await collect(runtime, runtime_id, "hello", context)
    assert any(isinstance(event, Completed) for event in resumed)
