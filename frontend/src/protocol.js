export const CLIENT_WORKING_STATES = [
  "starting",
  "running",
  "finishing",
  "stopping",
];
export const SERVER_WORKING_STATES = ["starting", "running", "finishing"];
export const TERMINAL_TASK_STATES = [
  "completed",
  "stopped",
  "failed",
  "aborted",
];

const parseTime = (value) => (value ? Date.parse(value) : null);

const progressLabel = (event) => {
  const message = String(event.message || "").trim();
  const tool = String(event.tool_name || "").trim();
  const parts = message
    .split(/\s*·\s*/)
    .filter(Boolean)
    .filter(
      (part, index, all) =>
        all.findIndex(
          (value) => value.toLocaleLowerCase() === part.toLocaleLowerCase(),
        ) === index,
    );
  const normalized = parts.join(" · ");
  if (!normalized) return tool || "处理任务";
  if (
    !tool ||
    normalized.toLocaleLowerCase().includes(tool.toLocaleLowerCase())
  ) {
    return normalized;
  }
  return `${normalized} · ${tool}`;
};

const progressKey = (event) => {
  if (event.phase === "task") return event.task_id || event.tool_use_id;
  if (event.phase === "tool") return event.tool_use_id || event.task_id;
  return (
    event.tool_use_id ||
    event.task_id ||
    event.phase ||
    `step-${event.sequence}`
  );
};

export const isTerminalTask = (task) =>
  TERMINAL_TASK_STATES.includes(task?.status);

export const emptyProtocolView = (messages = []) => ({
  messages: messages.map((message, index) => ({
    ...message,
    localId: message.localId || `message-${message.sequence ?? index}`,
  })),
  tasks: {},
  latestTaskId: null,
  buffer: null,
  seen: new Set(),
});

export const protocolEventKey = (event) =>
  `${event.turn_id || "none"}:${event.sequence ?? "none"}:${event.type}:${event.code || ""}`;

export function reduceProtocolEvent(previous, event, now = Date.now()) {
  const key = protocolEventKey(event);
  if (previous.seen.has(key)) return previous;

  const view = {
    ...previous,
    messages: [...previous.messages],
    tasks: { ...previous.tasks },
    seen: new Set(previous.seen),
  };
  view.seen.add(key);
  const at = parseTime(event.at) || now;
  const turnId = event.turn_id || null;

  if (event.type === "user_message") {
    view.messages.push({
      role: "user",
      content: event.content || "",
      created_at: event.at,
      sequence: event.sequence,
      model: event.model,
      turnId,
      localId: `user-${turnId || event.sequence || view.messages.length}`,
      pending: event.pending === true,
    });
    return view;
  }

  if (event.type === "turn_started") {
    const localId = `assistant-${turnId}`;
    view.tasks[turnId] = {
      id: turnId,
      status: "running",
      startedAt: at,
      endedAt: null,
      lastSequence: event.sequence || 0,
      steps: [],
      usage: null,
    };
    view.latestTaskId = turnId;
    view.buffer = { turnId, content: "", localId };
    if (!view.messages.some((message) => message.localId === localId)) {
      view.messages.push({
        role: "assistant",
        content: "",
        streaming: false,
        taskAnchor: true,
        created_at: event.at,
        turnId,
        localId,
      });
    }
    return view;
  }

  let task = view.tasks[turnId];
  if (turnId && !task) {
    task = {
      id: turnId,
      status: "running",
      startedAt: at,
      endedAt: null,
      lastSequence: 0,
      steps: [],
      usage: null,
    };
    view.tasks[turnId] = task;
    view.latestTaskId = turnId;
  }

  if (event.type === "delta") {
    let buffer = view.buffer;
    if (!buffer || buffer.turnId !== turnId) {
      buffer = { turnId, content: "", localId: `assistant-${turnId}` };
    }
    const content = event.content || "";
    buffer = { ...buffer, content: buffer.content + content };
    view.buffer = buffer;
    const index = view.messages.findIndex(
      (message) => message.localId === buffer.localId,
    );
    if (index >= 0) {
      view.messages[index] = {
        ...view.messages[index],
        content: view.messages[index].content + content,
        streaming: true,
      };
    } else {
      view.messages.push({
        role: "assistant",
        content,
        streaming: true,
        created_at: event.at,
        turnId,
        localId: buffer.localId,
      });
    }
  }

  if (event.type === "progress" && task) {
    let narrationText = "";
    const explicitStart =
      event.status === "started" &&
      (event.phase === "tool" || event.phase === "task");
    if (
      explicitStart &&
      view.buffer?.turnId === turnId &&
      view.buffer.content.trim()
    ) {
      narrationText = view.buffer.content;
      const consumed = view.buffer.content;
      const localId = view.buffer.localId;
      const messageIndex = view.messages.findIndex(
        (message) => message.localId === localId,
      );
      if (messageIndex >= 0) {
        const message = view.messages[messageIndex];
        const content = message.content.endsWith(consumed)
          ? message.content.slice(0, -consumed.length)
          : "";
        view.messages[messageIndex] = {
          ...message,
          content,
          streaming: false,
          taskAnchor: true,
        };
      }
      view.buffer = {
        turnId,
        content: "",
        localId: `assistant-${turnId}-after-${event.sequence || event.tool_use_id || event.task_id || "step"}`,
      };
    }

    const keyId = progressKey(event);
    const parentId = event.parent_tool_use_id || null;
    const steps = [...task.steps];
    let index = steps.findIndex(
      (item) => item.kind !== "narration" && item.id === keyId,
    );
    if (narrationText) {
      const narrationId = `narration-${turnId}-${event.sequence || keyId}`;
      const row = {
        kind: "narration",
        id: narrationId,
        content: narrationText,
      };
      if (!steps.some((item) => item.id === narrationId)) {
        if (index < 0) steps.push(row);
        else {
          steps.splice(index, 0, row);
          index += 1;
        }
      }
    }

    const label = progressLabel(event);
    const detail =
      event.current != null && event.total != null
        ? `${event.current}/${event.total}`
        : "";
    if (index < 0) {
      steps.push({
        kind: "step",
        id: keyId,
        parentId,
        phase: event.phase,
        label,
        status: "running",
        startedAt: at,
        endedAt: null,
        activities: [],
      });
      index = steps.length - 1;
    }
    const old = steps[index];
    const normalized = ["active", "started", "running"].includes(event.status)
      ? "running"
      : event.status || "running";
    const terminal = TERMINAL_TASK_STATES.includes(normalized);
    const activity = {
      id: `${keyId}-${event.sequence || event.status}`,
      label,
      detail,
    };
    const seen = old.activities.some(
      (item) =>
        item.label === activity.label && item.detail === activity.detail,
    );
    task = {
      ...task,
      status: "running",
      lastSequence: event.sequence || task.lastSequence,
      steps,
    };
    steps[index] = {
      ...old,
      parentId: parentId || old.parentId,
      phase: event.phase || old.phase,
      label,
      status: normalized,
      endedAt: terminal ? at : null,
      activities: seen ? old.activities : [...old.activities, activity],
    };
    view.tasks[turnId] = task;
  }

  if (event.type === "error" && task) {
    if (event.recoverable === true) {
      if (event.code === "turn_not_running" && task.status === "stopping") {
        view.tasks[turnId] = {
          ...task,
          status: "stopped",
          endedAt: at,
          steps: task.steps.map((step) =>
            step.status === "running"
              ? { ...step, status: "stopped", endedAt: at }
              : step,
          ),
        };
      }
    } else {
      view.tasks[turnId] = {
        ...task,
        status: "failed",
        endedAt: at,
        steps: task.steps.map((step) =>
          step.status === "running"
            ? { ...step, status: "failed", endedAt: at }
            : step,
        ),
      };
    }
  }

  if (event.type === "done" && task) {
    const last = view.messages.at(-1);
    if (
      last?.role === "assistant" &&
      last.turnId === turnId &&
      last.streaming
    ) {
      view.messages[view.messages.length - 1] = { ...last, streaming: false };
    } else if (
      !event.completed &&
      !["stopped", "aborted"].includes(event.stop_reason)
    ) {
      view.messages.push({
        role: "assistant",
        content: "任务未完成。",
        created_at: event.at,
        turnId,
        localId: `failed-${turnId}`,
      });
    }
    const status =
      event.stop_reason === "aborted"
        ? "aborted"
        : event.stop_reason === "stopped"
          ? "stopped"
          : event.completed
            ? "completed"
            : "failed";
    view.tasks[turnId] = {
      ...task,
      status,
      endedAt: at,
      lastSequence: event.sequence || task.lastSequence,
      usage: event.usage,
      steps: task.steps.map((step) =>
        ["started", "running", "pending"].includes(step.status)
          ? {
              ...step,
              status: status === "completed" ? "completed" : status,
              endedAt: at,
            }
          : step,
      ),
    };
    view.buffer = null;
  }

  if (task && event.sequence) {
    view.tasks[turnId] = {
      ...view.tasks[turnId],
      lastSequence: Math.max(
        view.tasks[turnId].lastSequence || 0,
        event.sequence,
      ),
    };
  }
  return view;
}

/**
 * A terminal journal event is stronger evidence than a lagging summary snapshot
 * for the same turn. Preserve terminal state while still accepting snapshots for
 * a genuinely newer turn.
 */
export function reconcileSessionSummary(session, view) {
  if (!session || !view?.latestTaskId) return session;
  const task = view.tasks[view.latestTaskId];
  if (
    !isTerminalTask(task) ||
    session.active_turn_id !== task.id ||
    !SERVER_WORKING_STATES.includes(session.task_state)
  ) {
    return session;
  }
  return {
    ...session,
    task_state: "idle",
    active_turn_id: null,
    last_turn_sequence: Math.max(
      session.last_turn_sequence || 0,
      task.lastSequence || 0,
    ),
  };
}

export function applyReadySnapshot(view, event, now = Date.now()) {
  if (!view) return { view, terminalWins: false };
  const turnId = event.turn_id || view.latestTaskId;
  if (!SERVER_WORKING_STATES.includes(event.task_state) || !turnId) {
    return { view, terminalWins: false };
  }

  const task = view.tasks[turnId];
  if (isTerminalTask(task)) return { view, terminalWins: true };

  let next = view;
  if (!task) {
    const created = {
      id: turnId,
      status: event.task_state,
      startedAt: now,
      endedAt: null,
      lastSequence: event.last_sequence || 0,
      steps: [],
      usage: null,
    };
    next = {
      ...view,
      tasks: { ...view.tasks, [turnId]: created },
      latestTaskId: turnId,
    };
    const localId = `assistant-${turnId}`;
    if (!next.messages.some((message) => message.localId === localId)) {
      next = {
        ...next,
        messages: [
          ...next.messages,
          {
            role: "assistant",
            content: "",
            streaming: false,
            taskAnchor: true,
            turnId,
            localId,
          },
        ],
      };
    }
  } else {
    next = {
      ...view,
      tasks: {
        ...view.tasks,
        [turnId]: {
          ...task,
          status: event.task_state,
          lastSequence: Math.max(
            task.lastSequence || 0,
            event.last_sequence || 0,
          ),
        },
      },
      latestTaskId: turnId,
    };
  }
  return { view: next, terminalWins: false };
}
