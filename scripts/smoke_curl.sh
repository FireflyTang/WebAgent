#!/usr/bin/env bash
set -euo pipefail

DEMO_BASE_URL="${DEMO_BASE_URL:-http://127.0.0.1:8000}"
DEMO_API_KEY="${DEMO_API_KEY:-demo-local-key}"
DEMO_SESSION_ID="${DEMO_SESSION_ID:-curl-smoke-$$}"

auth_header="Authorization: Bearer ${DEMO_API_KEY}"
session_header="X-Session-ID: ${DEMO_SESSION_ID}"

first_output="$(mktemp)"
second_output="$(mktemp)"
gone_output="$(mktemp)"
session_created=0
cleanup() {
  if test "${session_created}" = "1"; then
    curl -sS -X DELETE "${DEMO_BASE_URL}/v1/sessions/${DEMO_SESSION_ID}" \
      -H "${auth_header}" >/dev/null 2>&1 || true
  fi
  rm -f -- "${first_output}" "${second_output}" "${gone_output}"
}
trap cleanup EXIT

curl -fsS "${DEMO_BASE_URL}/healthz" >/dev/null
curl -fsS "${DEMO_BASE_URL}/v1/models" -H "${auth_header}" >/dev/null

session_created=1
curl -fsS "${DEMO_BASE_URL}/v1/chat/completions" \
  -H "${auth_header}" \
  -H 'Content-Type: application/json' \
  -H "${session_header}" \
  -d '{
    "model": "claude-code-agent",
    "stream": false,
    "messages": [{"role": "user", "content": "创建一个计算器，实现加法并运行测试"}]
  }' >"${first_output}"

python3 - "${first_output}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
content = payload["choices"][0]["message"]["content"]
assert "测试通过" in content, content
print("first turn: calculator created and tests passed")
PY

curl -fsS -X POST "${DEMO_BASE_URL}/v1/sessions/${DEMO_SESSION_ID}/pause" \
  -H "${auth_header}" | python3 -c 'import json,sys; assert json.load(sys.stdin)["state"] in {"paused", "expiring"}'

curl -fsSN "${DEMO_BASE_URL}/v1/chat/completions" \
  -H "${auth_header}" \
  -H 'Content-Type: application/json' \
  -H "${session_header}" \
  -d '{
    "model": "claude-code-agent",
    "stream": true,
    "messages": [{"role": "user", "content": "现在增加减法功能并重新运行测试"}]
  }' >"${second_output}"

python3 - "${second_output}" <<'PY'
import sys

content = open(sys.argv[1], encoding="utf-8").read()
assert '"content":"第 2 轮' in content, content
assert "data: [DONE]\n\n" in content, content
print("second turn: paused session resumed, subtraction added, SSE completed")
PY

curl -fsS "${DEMO_BASE_URL}/v1/sessions/${DEMO_SESSION_ID}" -H "${auth_header}" \
  | python3 -c 'import json,sys; assert json.load(sys.stdin)["state"] == "active"'

curl -fsS -X DELETE "${DEMO_BASE_URL}/v1/sessions/${DEMO_SESSION_ID}" \
  -H "${auth_header}" | python3 -c 'import json,sys; assert json.load(sys.stdin)["state"] == "deleted"'
session_created=0

status="$({ curl -sS -o "${gone_output}" -w '%{http_code}' "${DEMO_BASE_URL}/v1/chat/completions" \
  -H "${auth_header}" \
  -H 'Content-Type: application/json' \
  -H "${session_header}" \
  -d '{"model":"claude-code-agent","messages":[{"role":"user","content":"继续"}]}'; } 2>/dev/null)"
test "${status}" = "410"

printf 'smoke passed: session=%s\n' "${DEMO_SESSION_ID}"
