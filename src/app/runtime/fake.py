import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.util.files import write_json_atomic

from .base import RuntimeContext
from .events import Completed, Failed, InteractionRequest, Progress, RuntimeEvent, TextDelta, Usage


class FakeRuntime:
    """Deterministic coding workflow used by the no-credential demo."""

    def __init__(self, stream_delay_ms: int = 25, long_task_delay_ms: int = 100) -> None:
        self.stream_delay = max(stream_delay_ms, 0) / 1000
        self.long_task_delay = max(long_task_delay_ms, 0) / 1000

    @staticmethod
    def _state_path(context: RuntimeContext) -> Path:
        return context.workspace / ".fake-runtime.json"

    def _load_state(self, context: RuntimeContext) -> dict[str, Any]:
        path = self._state_path(context)
        if not path.exists():
            return {"turn": 0, "status": "active", "pending": None, "features": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"turn": 0, "status": "active", "pending": None, "features": []}

    def _save_state(self, context: RuntimeContext, state: dict[str, Any]) -> None:
        context.workspace.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._state_path(context), state)

    async def create_session(self, context: RuntimeContext) -> str:
        context.workspace.mkdir(parents=True, exist_ok=True)
        state = self._load_state(context)
        state["status"] = "active"
        self._save_state(context, state)
        return f"fake-claude-{uuid.uuid4()}"

    async def _text(self, text: str, *, slow: bool = False) -> AsyncIterator[TextDelta]:
        delay = self.long_task_delay if slow else self.stream_delay
        # Split at line boundaries to make streaming observable and deterministic.
        chunks = text.splitlines(keepends=True) or [text]
        for chunk in chunks:
            if delay:
                await asyncio.sleep(delay)
            yield TextDelta(chunk)

    @staticmethod
    def _write_calculator(workspace: Path, include_subtract: bool) -> list[str]:
        calculator = [
            '"""A tiny calculator created by the demo coding agent."""',
            "",
            "",
            "def add(a: float, b: float) -> float:",
            '    """Return the sum of two numbers."""',
            "    return a + b",
        ]
        tests = [
            "import unittest",
            "",
            "from calculator import add" + (", subtract" if include_subtract else ""),
            "",
            "",
            "class CalculatorTests(unittest.TestCase):",
            "    def test_add(self):",
            "        self.assertEqual(add(2, 3), 5)",
        ]
        if include_subtract:
            calculator.extend(
                [
                    "",
                    "",
                    "def subtract(a: float, b: float) -> float:",
                    '    """Return the difference between two numbers."""',
                    "    return a - b",
                ]
            )
            tests.extend(
                ["", "    def test_subtract(self):", "        self.assertEqual(subtract(7, 4), 3)"]
            )
        tests.extend(["", "", "if __name__ == '__main__':", "    unittest.main()"])
        (workspace / "calculator.py").write_text("\n".join(calculator) + "\n", encoding="utf-8")
        (workspace / "test_calculator.py").write_text("\n".join(tests) + "\n", encoding="utf-8")
        return ["calculator.py", "test_calculator.py"]

    @staticmethod
    async def _run_tests(workspace: Path) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-v",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode(errors="replace")

    @staticmethod
    def _format_interaction(event: InteractionRequest) -> str:
        title = "需要你的选择" if event.kind == "choice" else "需要你的确认"
        options = "\n".join(f"{chr(65 + i)}. {option}" for i, option in enumerate(event.options))
        return f"{title}：{event.prompt}\n\n{options}\n\n请回复选项字母，或直接说明你的选择。\n"

    async def send_message(
        self,
        runtime_session_id: str,
        message: str,
        context: RuntimeContext,
    ) -> AsyncIterator[RuntimeEvent]:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") == "paused":
            yield Failed("runtime_paused", "Runtime is paused; resume it first", True)
            return
        if state.get("status") == "closed":
            yield Failed("runtime_closed", "Runtime session is closed", False)
            return

        state["turn"] = int(state.get("turn", 0)) + 1
        normalized = message.strip().lower()

        if "mock:error" in normalized:
            self._save_state(context, state)
            yield Failed("mock_error", "FakeRuntime injected an error", False)
            return

        pending = state.get("pending")
        if pending:
            if pending["kind"] == "choice":
                option = None
                if normalized in {"a", "最小修改", "方案a", "方案 a"}:
                    option = "A（最小修改）"
                elif normalized in {"b", "兼容重构", "方案b", "方案 b"}:
                    option = "B（兼容重构）"
                elif normalized in {"c", "暂不修改", "方案c", "方案 c"}:
                    option = "C（暂不修改）"
                if option is None:
                    event = InteractionRequest("choice", pending["prompt"], pending["options"])
                    async for chunk in self._text(
                        "无法识别选择，请回复 A/B/C。\n" + self._format_interaction(event)
                    ):
                        yield chunk
                    self._save_state(context, state)
                    yield Completed()
                    return
                state["pending"] = None
                async for chunk in self._text(f"已记录你的选择：{option}。继续执行当前任务。\n"):
                    yield chunk
            else:
                allowed = normalized in {"a", "允许", "同意", "yes", "y"} or "允许" in normalized
                denied = normalized in {"b", "拒绝", "no", "n"} or "拒绝" in normalized
                if not (allowed or denied):
                    event = InteractionRequest("permission", pending["prompt"], pending["options"])
                    async for chunk in self._text(
                        "无法识别确认结果。\n" + self._format_interaction(event)
                    ):
                        yield chunk
                    self._save_state(context, state)
                    yield Completed()
                    return
                state["pending"] = None
                result = "允许本次操作" if allowed else "拒绝操作"
                async for chunk in self._text(f"已记录：{result}。\n"):
                    yield chunk

        if "mock:choice" in normalized or "选择" in normalized or "方案" in normalized:
            event = InteractionRequest(
                "choice", "请选择实现方式", ["最小修改", "保持兼容并重构", "暂不修改"]
            )
            state["pending"] = {
                "kind": event.kind,
                "prompt": event.prompt,
                "options": event.options,
            }
            self._save_state(context, state)
            async for chunk in self._text(self._format_interaction(event)):
                yield chunk
            yield Completed()
            return

        if "permission" in normalized or "权限" in normalized:
            event = InteractionRequest("permission", "Agent 希望运行项目测试", ["允许本次", "拒绝"])
            state["pending"] = {
                "kind": event.kind,
                "prompt": event.prompt,
                "options": event.options,
            }
            self._save_state(context, state)
            async for chunk in self._text(self._format_interaction(event)):
                yield chunk
            yield Completed()
            return

        wants_subtract = any(word in normalized for word in ("减法", "subtract", "减去"))
        wants_calculator = any(
            word in normalized
            for word in ("calculator", "计算器", "加法", "开发任务", "mock:write")
        )
        slow = "mock:slow" in normalized
        if wants_calculator or wants_subtract:
            yield Progress("thinking", "正在分析需求", "running")
            async for chunk in self._text(
                f"第 {state['turn']} 轮：分析需求并检查当前 workspace。\n", slow=slow
            ):
                yield chunk
            include_subtract = wants_subtract or "subtract" in state.get("features", [])
            yield Progress("tool", "正在写入项目文件", "running", tool_name="Write")
            files = self._write_calculator(context.workspace, include_subtract)
            if include_subtract and "subtract" not in state["features"]:
                state["features"].append("subtract")
            yield Progress("tool", "项目文件已写入", "completed", tool_name="Write")
            async for chunk in self._text(f"已写入：{', '.join(files)}。\n", slow=slow):
                yield chunk
            yield Progress("task", "正在运行 unittest", "running", tool_name="Bash")
            code, output = await self._run_tests(context.workspace)
            summary = "测试通过" if code == 0 else "测试失败"
            yield Progress(
                "task",
                f"unittest：{summary}",
                "completed" if code == 0 else "failed",
                tool_name="Bash",
            )
            async for chunk in self._text(f"运行 unittest：{summary}。\n{output}", slow=slow):
                yield chunk
            self._save_state(context, state)
            yield Usage(input_tokens=0, output_tokens=0)
            yield Progress("finalizing", "正在完成任务", "completed" if code == 0 else "failed")
            yield Completed("stop" if code == 0 else "error")
            return

        result_path = context.workspace / "demo-result.txt"
        previous = result_path.read_text(encoding="utf-8") if result_path.exists() else ""
        result_path.write_text(
            previous + f"turn {state['turn']}: {message.strip()}\n", encoding="utf-8"
        )
        self._save_state(context, state)
        async for chunk in self._text(
            f"第 {state['turn']} 轮已处理。已把任务记录到 demo-result.txt，"
            "你可以继续要求创建计算器、增加减法或运行 mock:choice。\n",
            slow=slow,
        ):
            yield chunk
        yield Usage(input_tokens=0, output_tokens=0)
        yield Completed()

    async def pause(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") != "closed":
            state["status"] = "paused"
            self._save_state(context, state)

    async def resume(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        if state.get("status") == "closed":
            raise RuntimeError("Cannot resume a closed runtime")
        state["status"] = "active"
        self._save_state(context, state)

    async def close(self, runtime_session_id: str, context: RuntimeContext) -> None:
        del runtime_session_id
        state = self._load_state(context)
        state["status"] = "closed"
        self._save_state(context, state)
