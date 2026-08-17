#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("run-repo-session.sh")
THREAD_ID = "12345678-1234-1234-1234-123456789abc"


class SessionWrapperTests(unittest.TestCase):
    def run_wrapper(self, selector_failure: bool = False) -> tuple[list[dict], str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "xhigh"\n',
                encoding="utf-8",
            )
            prompt = root / "prompt.md"
            prompt.write_text("main task", encoding="utf-8")
            instruction = root / "instruction.txt"
            instruction.write_text("用平衡模型，高强度推理", encoding="utf-8")
            output = root / "final.md"
            calls = root / "calls.jsonl"
            mock_codex = root / "codex"
            mock_codex.write_text(
                f"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
record = {{'args': args, 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}}
with pathlib.Path(os.environ['CALLS']).open('a') as stream:
    stream.write(json.dumps(record) + '\\n')
if '--ephemeral' in args:
    if os.environ.get('SELECTOR_FAILURE') == '1':
        raise SystemExit(3)
    result = pathlib.Path(args[args.index('--output-last-message') + 1])
    result.write_text('{{"model":"gpt-5.6-terra","reasoning_effort":"high"}}')
else:
    result = pathlib.Path(args[args.index('--output-last-message') + 1])
    result.write_text('done')
    print('{{"type":"thread.started","thread_id":"{THREAD_ID}"}}')
""",
                encoding="utf-8",
            )
            mock_codex.chmod(mock_codex.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment.update(
                {
                    "CALLS": str(calls),
                    "CODEX_BIN": str(mock_codex),
                    "CODEX_HOME": str(codex_home),
                    "CODEX_REPOSITORY": "acme/widgets",
                    "CODEX_PROMPT_FILE": str(prompt),
                    "CODEX_SELECTION_INPUT_FILE": str(instruction),
                    "CODEX_OUTPUT_FILE": str(output),
                    "CODEX_SESSION_STATE_ROOT": str(root / "state"),
                    "RUNNER_TEMP": str(root),
                    "SELECTOR_FAILURE": "1" if selector_failure else "0",
                }
            )
            completed = subprocess.run(
                [str(SCRIPT)],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            return records, completed.stdout + completed.stderr

    def test_fresh_selector_overrides_local_defaults(self) -> None:
        records, logs = self.run_wrapper()
        self.assertEqual(len(records), 2)
        selector, main = records
        self.assertIn("--ephemeral", selector["args"])
        self.assertNotEqual(selector["cwd"], main["cwd"])
        self.assertIn("--model", main["args"])
        self.assertEqual(main["args"][main["args"].index("--model") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', main["args"])
        self.assertIn("model=gpt-5.6-terra, effort=high", logs)

    def test_selector_failure_falls_back_to_local_defaults(self) -> None:
        records, logs = self.run_wrapper(selector_failure=True)
        self.assertEqual(len(records), 2)
        main = records[1]
        self.assertEqual(main["args"][main["args"].index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="xhigh"', main["args"])
        self.assertIn("using local Codex defaults", logs)


if __name__ == "__main__":
    unittest.main()
