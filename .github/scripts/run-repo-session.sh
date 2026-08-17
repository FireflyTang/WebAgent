#!/usr/bin/env bash
set -euo pipefail

: "${CODEX_REPOSITORY:?Set CODEX_REPOSITORY to OWNER/REPOSITORY}"
: "${CODEX_PROMPT_FILE:?Set CODEX_PROMPT_FILE to the prompt file}"
: "${CODEX_SELECTION_INPUT_FILE:?Set CODEX_SELECTION_INPUT_FILE to the current instruction file}"
: "${CODEX_OUTPUT_FILE:?Set CODEX_OUTPUT_FILE to the final-message file}"
: "${CODEX_HOME:?Set CODEX_HOME to the authenticated Codex home}"

if [[ ! -r "$CODEX_PROMPT_FILE" ]]; then
  echo "Prompt file is not readable: $CODEX_PROMPT_FILE" >&2
  exit 2
fi
if [[ ! -r "$CODEX_SELECTION_INPUT_FILE" ]]; then
  echo "Selection input file is not readable: $CODEX_SELECTION_INPUT_FILE" >&2
  exit 2
fi

codex_bin=${CODEX_BIN:-codex}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_root=${CODEX_SESSION_STATE_ROOT:-"$CODEX_HOME/issue-runner-state"}
repo_key=${CODEX_REPOSITORY//\//__}
state_dir="$state_root/$repo_key"
session_file="$state_dir/session-id"
events_file=${CODEX_EVENTS_FILE:-"${CODEX_OUTPUT_FILE}.jsonl"}

if ! local_codex_settings=$(python3 - "$CODEX_HOME/config.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
config = {}
if path.exists():
    with path.open("rb") as stream:
        config = tomllib.load(stream)

for key in ("model", "model_reasoning_effort"):
    value = config.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise SystemExit(f"{key} in {path} must be a single-line string")
    print(f"{key}={value}")
PY
); then
  echo "Could not read local Codex model defaults from $CODEX_HOME/config.toml" >&2
  exit 2
fi

local_model=""
local_effort=""
while IFS='=' read -r setting value; do
  case "$setting" in
    model) local_model=$value ;;
    model_reasoning_effort) local_effort=$value ;;
  esac
done <<< "$local_codex_settings"

model_override=""
effort_override=""
selector_result=$(mktemp "${RUNNER_TEMP:-/tmp}/codex-selected-settings.XXXXXX.json")
trap 'rm -f -- "$selector_result"' EXIT
if python3 "$script_dir/select_codex_settings.py" \
  --instruction "$CODEX_SELECTION_INPUT_FILE" \
  --output "$selector_result" \
  --codex-bin "$codex_bin"; then
  if selected_settings=$(python3 - "$selector_result" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"model={result['model']}")
print(f"reasoning_effort={result['reasoning_effort']}")
PY
  ); then
    while IFS='=' read -r setting value; do
      case "$setting" in
        model) model_override=$value ;;
        reasoning_effort) effort_override=$value ;;
      esac
    done <<< "$selected_settings"
  else
    echo "warning: could not read selector output; using local Codex defaults" >&2
  fi
else
  echo "warning: fresh Luna setting selection failed; using local Codex defaults" >&2
fi

selected_model=${model_override:-$local_model}
selected_effort=${effort_override:-$local_effort}
if [[ -n "$selected_model" && ! "$selected_model" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$ ]]; then
  echo "Invalid Codex model selection: $selected_model" >&2
  exit 2
fi
if [[ -n "$selected_effort" && ! "$selected_effort" =~ ^(minimal|low|medium|high|xhigh|max|ultra)$ ]]; then
  echo "Invalid Codex reasoning-effort selection: $selected_effort" >&2
  exit 2
fi
echo "Codex execution settings: model=${selected_model:-CLI default}, effort=${selected_effort:-model default}"

codex_options=(--ignore-user-config)
if [[ -n "$selected_model" ]]; then
  codex_options+=(--model "$selected_model")
fi
if [[ -n "$selected_effort" ]]; then
  codex_options+=(--config "model_reasoning_effort=\"$selected_effort\"")
fi

mkdir -p "$state_dir"
chmod 700 "$state_dir"

exec 9>"$state_dir/session.lock"
flock 9

run_status=0
if [[ -s "$session_file" ]]; then
  session_id=$(tr -d '[:space:]' < "$session_file")
  if [[ ! "$session_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "Invalid saved Codex session ID in $session_file" >&2
    exit 2
  fi

  set +e
  "$codex_bin" exec \
    "${codex_options[@]}" \
    --sandbox workspace-write \
    resume \
    --json \
    --output-last-message "$CODEX_OUTPUT_FILE" \
    "$session_id" \
    - < "$CODEX_PROMPT_FILE" | tee "$events_file"
  run_status=${PIPESTATUS[0]}
  set -e
else
  set +e
  "$codex_bin" exec \
    "${codex_options[@]}" \
    --json \
    --sandbox workspace-write \
    --output-last-message "$CODEX_OUTPUT_FILE" \
    - < "$CODEX_PROMPT_FILE" | tee "$events_file"
  run_status=${PIPESTATUS[0]}
  set -e

  session_id=$(jq -r 'select(.type == "thread.started") | .thread_id // empty' "$events_file" | head -n 1)
  if [[ ! "$session_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "Codex did not report a valid persistent session ID." >&2
    exit 2
  fi

  session_tmp="$state_dir/session-id.tmp.$$"
  printf '%s\n' "$session_id" > "$session_tmp"
  chmod 600 "$session_tmp"
  mv "$session_tmp" "$session_file"
fi

exit "$run_status"
