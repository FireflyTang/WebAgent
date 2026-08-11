import assert from "node:assert/strict";
import test from "node:test";

import {
  applyReadySnapshot,
  emptyProtocolView,
  reconcileSessionSummary,
  reduceProtocolEvent,
} from "./protocol.js";

const terminalView = (status = "completed") => {
  let view = emptyProtocolView();
  view = reduceProtocolEvent(view, {
    type: "turn_started",
    turn_id: "turn-1",
    sequence: 1,
  });
  return reduceProtocolEvent(view, {
    type: "done",
    turn_id: "turn-1",
    sequence: 4,
    completed: status === "completed",
    stop_reason:
      status === "aborted"
        ? "aborted"
        : status === "stopped"
          ? "stopped"
          : "stop",
  });
};

test("a stale finishing list snapshot cannot override a terminal event for the same turn", () => {
  const view = terminalView();
  const session = reconcileSessionSummary(
    {
      session_id: "session-1",
      task_state: "finishing",
      active_turn_id: "turn-1",
      last_turn_sequence: 3,
    },
    view,
  );

  assert.equal(session.task_state, "idle");
  assert.equal(session.active_turn_id, null);
  assert.equal(session.last_turn_sequence, 4);
});

test("a ready finishing snapshot cannot overwrite replayed terminal state", () => {
  const original = terminalView("stopped");
  const { view, terminalWins } = applyReadySnapshot(original, {
    type: "ready",
    task_state: "finishing",
    turn_id: "turn-1",
    last_sequence: 3,
  });

  assert.equal(terminalWins, true);
  assert.equal(view.tasks["turn-1"].status, "stopped");
  assert.equal(view.tasks["turn-1"].lastSequence, 4);
});

test("terminal protection is scoped to its turn and accepts a newer running turn", () => {
  const original = terminalView();
  const { view, terminalWins } = applyReadySnapshot(original, {
    type: "ready",
    task_state: "running",
    turn_id: "turn-2",
    last_sequence: 2,
  });

  assert.equal(terminalWins, false);
  assert.equal(view.latestTaskId, "turn-2");
  assert.equal(view.tasks["turn-2"].status, "running");
});
