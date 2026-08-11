import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  BracketsCurly,
  CaretDown,
  CaretLeft,
  CaretRight,
  Check,
  FileText,
  Gear,
  Paperclip,
  Plus,
  Question,
  Robot,
  Stop,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import FilesPanel from "./components/FilesPanel.jsx";
import TaskCard, { MarkdownContent } from "./components/TaskCard.jsx";
import {
  applyReadySnapshot,
  CLIENT_WORKING_STATES,
  emptyProtocolView,
  reconcileSessionSummary,
  reduceProtocolEvent,
  SERVER_WORKING_STATES,
} from "./protocol.js";
import "./style.css";

const STORAGE = {
  endpoint: "oca.provider-endpoint",
  providerKey: "oca.provider-api-key",
  auth: "oca.provider-auth-env",
  defaultModel: "oca.default-model",
  defaultEffort: "oca.default-effort",
  active: "oca.active-session",
  left: "oca.left-width",
  right: "oca.right-width",
  lc: "oca.left-collapsed",
  rc: "oca.right-collapsed",
};
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const EFFORT_LABELS = {
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最高",
};
const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
const parseTime = (value) => (value ? Date.parse(value) : null);
const titleOf = (s) => s?.title || s?.metadata?.title || "新会话";
const stateOf = (s) => String(s?.state || "ACTIVE").toLowerCase();
const isSelectableSession = (session) =>
  Boolean(session) &&
  stateOf(session) !== "deleted" &&
  session.compatible !== false;
const normalizedProvider = (value) => ({
  base_url: String(value.base_url || "").trim(),
  api_key: String(value.api_key || "").trim(),
  auth_env: value.auth_env || "ANTHROPIC_AUTH_TOKEN",
});
const providerFingerprint = (value) =>
  JSON.stringify(normalizedProvider(value));
const catalogModel = (catalog, preferred) => {
  const models = Array.isArray(catalog.models) ? catalog.models : [];
  return models.includes(preferred)
    ? preferred
    : models.includes(catalog.default_model)
      ? catalog.default_model
      : models[0] || "";
};
function IconButton({ label, children, className = "", ...props }) {
  return (
    <button
      type="button"
      className={`icon-button ${className}`}
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  );
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(
      data.detail || data.error?.message || `HTTP ${response.status}`,
    );
    error.status = response.status;
    throw error;
  }
  return response;
}

function ModelPicker({ models, value, disabled, invalid = false, onChange }) {
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const root = useRef(null);
  const options = models || [];
  useEffect(() => {
    const close = (e) => {
      if (!root.current?.contains(e.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);
  const pick = (i) => {
    if (options[i]) {
      onChange(options[i]);
      setOpen(false);
    }
  };
  const keys = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      setIndex((i) => {
        const base = open ? i : Math.max(0, options.indexOf(value));
        return options.length
          ? (base + (e.key === "ArrowDown" ? 1 : options.length - 1)) %
              options.length
          : 0;
      });
      return;
    }
    if (e.key === "Enter" && open) {
      e.preventDefault();
      pick(index);
    }
  };
  return (
    <div className="model-picker" ref={root}>
      {open && (
        <div
          className="model-popover"
          role="listbox"
          aria-label="选择模型"
          aria-activedescendant={`model-${index}`}
          onKeyDown={keys}
        >
          <div className="model-caption">选择本 Session 使用的模型</div>
          <div className="model-options">
            {options.map((model, i) => (
              <button
                id={`model-${i}`}
                type="button"
                role="option"
                aria-selected={model === value}
                className={`model-option ${i === index ? "keyboard-focus" : ""}`}
                key={model}
                onMouseEnter={() => setIndex(i)}
                onClick={() => pick(i)}
              >
                <span className="model-mark">
                  <BracketsCurly />
                </span>
                <strong>{model}</strong>
                {model === value && <Check weight="bold" />}
              </button>
            ))}
          </div>
          {!options.length && <div className="empty-state">后端未返回模型</div>}
        </div>
      )}
      <button
        type="button"
        className={`model-trigger ${invalid ? "invalid" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-invalid={invalid || undefined}
        disabled={disabled}
        onClick={() =>
          setOpen((v) => {
            if (!v) setIndex(Math.max(0, options.indexOf(value)));
            return !v;
          })
        }
        onKeyDown={keys}
      >
        <span>
          模型：<strong>{value || "未选择"}</strong>
        </span>
        <CaretDown className={open ? "rotated" : ""} />
      </button>
    </div>
  );
}

function EffortPicker({ value, disabled, onChange }) {
  return (
    <label className="effort-picker">
      <span>强度</span>
      <select
        id="effortPicker"
        aria-label="推理强度"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Provider 默认</option>
        {EFFORTS.map((effort) => (
          <option key={effort} value={effort}>
            {EFFORT_LABELS[effort]}（{effort}）
          </option>
        ))}
      </select>
    </label>
  );
}

function sessionGroups(sessions, serverNow) {
  const now = new Date(serverNow || Date.now()),
    midnight = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
    ).getTime();
  const groups = { 今天: [], 昨天: [], 更早: [] };
  sessions.forEach((s) => {
    const time =
      parseTime(s.last_activity_at || s.updated_at || s.created_at) || 0;
    const days = Math.floor(
      (midnight -
        new Date(
          new Date(time).getFullYear(),
          new Date(time).getMonth(),
          new Date(time).getDate(),
        ).getTime()) /
        86400000,
    );
    groups[days <= 0 ? "今天" : days === 1 ? "昨天" : "更早"].push(s);
  });
  return Object.entries(groups).filter(([, items]) => items.length);
}
function relativeTime(value, serverNow) {
  const diff = Math.max(
    0,
    (parseTime(serverNow) || Date.now()) - (parseTime(value) || Date.now()),
  );
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}
function lifecycle(session) {
  const state = stateOf(session);
  if (state === "deleted") return { label: "已删除", tone: "deleted" };
  if (state === "expiring") return { label: "即将删除", tone: "expiring" };
  if (state === "paused") return { label: "已暂停", tone: "paused" };
  return { label: "待命", tone: "active" };
}

function SessionRail({
  sessions,
  tasks,
  activeId,
  serverNow,
  onSelect,
  onNew,
  onPolicy,
  onHelp,
  onContextMenu,
  creationBusy,
  newButtonId = "newSession",
  navLabel = "Session 列表",
}) {
  const visibleSessions = sessions.filter(isSelectableSession);
  return (
    <aside className="session-rail">
      <div className="brand">
        <span>
          <BracketsCurly weight="bold" />
        </span>
        <div>
          <strong>WebAgent</strong>
          <small>@Username</small>
        </div>
      </div>
      <button
        id={newButtonId}
        type="button"
        className="new-session"
        disabled={creationBusy}
        onClick={onNew}
      >
        <Plus />
        {creationBusy ? "正在创建…" : "新建会话"}
      </button>
      <nav className="session-list" aria-label={navLabel}>
        {sessionGroups(visibleSessions, serverNow).map(([label, items]) => (
          <section className="session-group" key={label}>
            <h2>{label}</h2>
            {items.map((s) => {
              const life = lifecycle(s),
                working =
                  CLIENT_WORKING_STATES.includes(tasks[s.session_id]?.status) ||
                  SERVER_WORKING_STATES.includes(s.task_state);
              return (
                <button
                  type="button"
                  key={s.session_id}
                  data-session-id={s.session_id}
                  className={`session-row ${s.session_id === activeId ? "selected" : ""}`}
                  aria-current={s.session_id === activeId ? "page" : undefined}
                  onClick={() => onSelect(s.session_id)}
                  onContextMenu={(event) => onContextMenu(s, event)}
                >
                  <strong>{titleOf(s)}</strong>
                  <span>
                    <time>
                      {relativeTime(
                        s.last_activity_at || s.created_at,
                        serverNow,
                      )}
                    </time>
                    <em className={working ? "working" : life.tone}>
                      <i />
                      {working ? "运行中" : life.label}
                    </em>
                  </span>
                </button>
              );
            })}
          </section>
        ))}
      </nav>
      <div className="rail-footer">
        <button type="button" onClick={onPolicy}>
          <Gear />
          设置
        </button>
        <button type="button" onClick={onHelp}>
          <Question />
          帮助
        </button>
      </div>
    </aside>
  );
}

function Messages({
  messages,
  task,
  tasksByTurn = {},
  now,
  onStop,
  stopDisabled = false,
  stopHint = "",
}) {
  const attachmentIndexes = new Map();
  messages.forEach((message, index) => {
    if (
      message.role !== "assistant" ||
      !message.turnId ||
      !tasksByTurn[message.turnId]
    )
      return;
    const previous = attachmentIndexes.get(message.turnId);
    if (previous == null || message.content.trim())
      attachmentIndexes.set(message.turnId, index);
  });
  const attachedIds = new Set(attachmentIndexes.keys());
  return (
    <div className="message-stack">
      {messages.map((m, i) => {
        const messageTask =
          m.role === "assistant" &&
          m.turnId &&
          attachmentIndexes.get(m.turnId) === i
            ? tasksByTurn[m.turnId]
            : null;
        if (m.role === "assistant" && !m.content.trim() && !messageTask)
          return null;
        return (
          <article
            className={`message ${m.role}${m.pending ? " pending" : ""}${messageTask && !m.content.trim() ? " working-message" : ""}`}
            data-turn-id={
              m.role === "assistant" ? m.turnId || undefined : undefined
            }
            data-pending={m.pending || undefined}
            key={m.sequence || m.localId || i}
          >
            <div className="avatar">
              {m.role === "user" ? <Robot /> : <BracketsCurly weight="bold" />}
            </div>
            <div className="message-copy">
              <header>
                <strong>{m.role === "user" ? "你" : "WebAgent"}</strong>
                {m.pending && <small className="pending-label">发送中…</small>}
                {m.created_at && (
                  <time>
                    {new Date(m.created_at).toLocaleTimeString("zh-CN", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </time>
                )}
              </header>
              {messageTask && (
                <TaskCard
                  task={messageTask}
                  now={now}
                  onStop={onStop}
                  stopDisabled={stopDisabled}
                  stopHint={stopHint}
                />
              )}{" "}
              {m.role === "assistant" && m.content ? (
                <MarkdownContent
                  content={m.content}
                  streaming={m.streaming}
                  className="message-text"
                />
              ) : m.role === "user" ? (
                <div className="message-text">{m.content}</div>
              ) : null}
            </div>
          </article>
        );
      })}
      {task && !attachedIds.has(task.id) && (
        <article
          className="message assistant working-message"
          data-turn-id={task.id || undefined}
        >
          <div className="avatar">
            <BracketsCurly weight="bold" />
          </div>
          <div className="message-copy">
            <header>
              <strong>WebAgent</strong>
            </header>
            <TaskCard
              task={task}
              now={now}
              onStop={onStop}
              stopDisabled={stopDisabled}
              stopHint={stopHint}
            />
          </div>
        </article>
      )}
      {!messages.length && !task && (
        <div className="welcome">
          <BracketsCurly />
          <h2>准备开始一个任务</h2>
          <p>
            描述你希望修改、排查或构建的内容。WebAgent 会在当前 Session
            的持久沙箱中工作。
          </p>
        </div>
      )}
    </div>
  );
}

function Dialog({ title, onClose, children, id }) {
  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        id={id}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
      >
        <header>
          <h2 id={`${id}-title`}>{title}</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X />
          </IconButton>
        </header>
        {children}
      </section>
    </div>
  );
}

function App() {
  const initialProvider = {
    base_url: localStorage.getItem(STORAGE.endpoint) || "",
    api_key: localStorage.getItem(STORAGE.providerKey) || "",
    auth_env: localStorage.getItem(STORAGE.auth) || "ANTHROPIC_AUTH_TOKEN",
  };
  const storedEffort = localStorage.getItem(STORAGE.defaultEffort) || "",
    initialDefaults = {
      model: localStorage.getItem(STORAGE.defaultModel) || "",
      effort: EFFORTS.includes(storedEffort) ? storedEffort : "",
    };
  const [provider, setProvider] = useState(initialProvider);
  const [draftProvider, setDraftProvider] = useState(initialProvider);
  const [config, setConfig] = useState({
    models: [],
    policies: {},
    provider_auth_modes: ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"],
  });
  const [sessions, setSessions] = useState([]);
  const [serverTimeOffset, setServerTimeOffset] = useState(0);
  const [activeId, setActiveId] = useState(
    () => localStorage.getItem(STORAGE.active) || "",
  );
  const [messagesBySession, setMessagesBySession] = useState({});
  const [tasks, setTasks] = useState({});
  const [taskHistoryBySession, setTaskHistoryBySession] = useState({});
  const [files, setFiles] = useState([]);
  const [fileQueriesBySession, setFileQueriesBySession] = useState({});
  const [fileBusyBySession, setFileBusyBySession] = useState({});
  const [fileFeedbackBySession, setFileFeedbackBySession] = useState({});
  const [connections, setConnections] = useState({});
  const [settings, setSettings] = useState(false);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [logHtml, setLogHtml] = useState(null);
  const [promptsBySession, setPromptsBySession] = useState({});
  const [drawer, setDrawer] = useState(false);
  const [sessionDrawer, setSessionDrawer] = useState(false);
  const [error, setError] = useState("");
  const [sessionErrors, setSessionErrors] = useState({});
  const [providerBusy, setProviderBusy] = useState(false);
  const [providerStatus, setProviderStatus] = useState(
    initialProvider.base_url && initialProvider.api_key
      ? "loading"
      : "unconfigured",
  );
  const [providerTest, setProviderTest] = useState(null);
  const [sessionDefaults, setSessionDefaults] = useState(initialDefaults);
  const [draftDefaults, setDraftDefaults] = useState(initialDefaults);
  const [creationBusy, setCreationBusy] = useState(false);
  const [sessionMenu, setSessionMenu] = useState(null);
  const [leftWidth, setLeftWidth] = useState(
    () => Number(localStorage.getItem(STORAGE.left)) || 228,
  );
  const [rightWidth, setRightWidth] = useState(
    () => Number(localStorage.getItem(STORAGE.right)) || 286,
  );
  const [leftCollapsed, setLeftCollapsed] = useState(
    () => localStorage.getItem(STORAGE.lc) === "1",
  );
  const [rightCollapsed, setRightCollapsed] = useState(
    () => localStorage.getItem(STORAGE.rc) === "1",
  );
  const [now, setNow] = useState(Date.now());
  const serverNow = new Date(now + serverTimeOffset).toISOString();
  const sockets = useRef(new Map()),
    reconnectTimers = useRef(new Map()),
    closing = useRef(new Set()),
    activeRef = useRef(activeId),
    previousActive = useRef(null),
    providerRef = useRef(provider),
    draftProviderRef = useRef(draftProvider),
    taskRef = useRef(tasks),
    messagesRef = useRef(messagesBySession),
    taskHistoryRef = useRef(taskHistoryBySession),
    sessionsRef = useRef(sessions),
    protocolViews = useRef(new Map()),
    pendingTurns = useRef(new Map()),
    attempts = useRef(new Map()),
    sessionLoadVersion = useRef(0),
    providerTestGeneration = useRef(0),
    logGeneration = useRef(0),
    logAbort = useRef(null),
    creationLock = useRef(false),
    conversationRef = useRef(null),
    followMessages = useRef(true);
  activeRef.current = activeId;
  providerRef.current = provider;
  draftProviderRef.current = draftProvider;
  taskRef.current = tasks;
  messagesRef.current = messagesBySession;
  taskHistoryRef.current = taskHistoryBySession;
  sessionsRef.current = sessions;
  const selected = sessions.find((s) => s.session_id === activeId);
  const activeTask = tasks[activeId];
  const running =
    CLIENT_WORKING_STATES.includes(activeTask?.status) ||
    SERVER_WORKING_STATES.includes(selected?.task_state);
  const messages = messagesBySession[activeId] || [],
    taskHistory = taskHistoryBySession[activeId] || {};
  const connection = connections[activeId] || "disconnected";
  const prompt = promptsBySession[activeId] || "";
  const fileQuery = fileQueriesBySession[activeId] || "";
  const fileBusy = Boolean(fileBusyBySession[activeId]);
  const fileFeedback = fileFeedbackBySession[activeId] || "";
  const displayedError = sessionErrors[activeId] || error;
  const selectedModel = String(selected?.last_model || "");
  const selectedModelValid = Boolean(
    selectedModel && config.models.includes(selectedModel),
  );
  const selectedEffort = EFFORTS.includes(selected?.last_effort)
    ? selected.last_effort
    : "";
  const providerReady =
    providerStatus === "configured" && config.models.length > 0;
  const testedProviderMatches = Boolean(
    providerTest &&
      providerTest.fingerprint === providerFingerprint(draftProvider),
  );
  const providerCanSave = Boolean(
    testedProviderMatches &&
      providerTest.models.includes(draftDefaults.model) &&
      (draftDefaults.effort === "" || EFFORTS.includes(draftDefaults.effort)),
  );
  const stopAvailable =
    connection === "connected" &&
    sockets.current.get(activeId)?.readyState === 1;
  const stopHint = "连接恢复后即可停止任务";
  const setPrompt = (value) =>
    setPromptsBySession((all) => {
      const current = all[activeId] || "",
        nextValue = typeof value === "function" ? value(current) : value;
      return { ...all, [activeId]: nextValue };
    });
  const setFileQuery = (value) =>
    setFileQueriesBySession((all) => {
      const current = all[activeId] || "",
        nextValue = typeof value === "function" ? value(current) : value;
      return { ...all, [activeId]: nextValue };
    });
  const setSessionError = useCallback((sid, message) => {
    if (!sid) return;
    setSessionErrors((all) => {
      if (message) return { ...all, [sid]: message };
      if (!all[sid]) return all;
      const next = { ...all };
      delete next[sid];
      return next;
    });
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const closeLog = useCallback(() => {
    logGeneration.current += 1;
    logAbort.current?.abort();
    logAbort.current = null;
    setLogHtml(null);
  }, []);
  useEffect(() => {
    const escape = (e) => {
      if (e.key !== "Escape") return;
      providerTestGeneration.current += 1;
      setProviderBusy(false);
      setSettings(false);
      setDrawer(false);
      setSessionDrawer(false);
      setPolicyOpen(false);
      setHelpOpen(false);
      closeLog();
      setSessionMenu(null);
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [closeLog]);
  useEffect(() => {
    closeLog();
  }, [activeId, closeLog]);
  useEffect(() => {
    if (!sessionMenu) return;
    const close = (event) => {
      if (!event.target.closest?.(".session-context-menu"))
        setSessionMenu(null);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [sessionMenu]);
  useEffect(() => {
    localStorage.setItem(STORAGE.left, String(leftWidth));
    localStorage.setItem(STORAGE.right, String(rightWidth));
    localStorage.setItem(STORAGE.lc, leftCollapsed ? "1" : "0");
    localStorage.setItem(STORAGE.rc, rightCollapsed ? "1" : "0");
    if (activeId) localStorage.setItem(STORAGE.active, activeId);
  }, [leftWidth, rightWidth, leftCollapsed, rightCollapsed, activeId]);
  useEffect(() => {
    request("/v1/web/config")
      .then((r) => r.json())
      .then((data) => setConfig((current) => ({ ...current, ...data })))
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    const candidate = normalizedProvider(provider),
      fingerprint = providerFingerprint(candidate),
      generation = ++providerTestGeneration.current;
    if (!candidate.base_url || !candidate.api_key) {
      setProviderStatus("unconfigured");
      setSettings(true);
      return;
    }
    setProviderStatus("loading");
    request("/v1/web/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(candidate),
    })
      .then((r) => r.json())
      .then((data) => {
        if (
          generation !== providerTestGeneration.current ||
          providerFingerprint(providerRef.current) !== fingerprint
        )
          return;
        const model = catalogModel(data, initialDefaults.model),
          defaults = { model, effort: initialDefaults.effort };
        setSessionDefaults(defaults);
        setDraftDefaults(defaults);
        setConfig((current) => ({
          ...current,
          ...data,
          models: Array.isArray(data.models) ? data.models : [],
          default_model: model,
        }));
        setProviderStatus("configured");
      })
      .catch((e) => {
        if (
          generation !== providerTestGeneration.current ||
          providerFingerprint(providerRef.current) !== fingerprint
        )
          return;
        setProviderStatus("failed");
        setError(e.message);
        setSettings(true);
      });
  }, []);

  const loadSessions = useCallback(async (prefer) => {
    const version = ++sessionLoadVersion.current,
      data = await request("/v1/sessions").then((r) => r.json());
    if (version !== sessionLoadVersion.current) return;
    const listed = (data.sessions || []).map((session) =>
        reconcileSessionSummary(
          session,
          protocolViews.current.get(session.session_id),
        ),
      ),
      selectable = listed.filter(isSelectableSession),
      ids = new Set(selectable.map((session) => session.session_id));
    setSessions(listed);
    const receivedServerNow = parseTime(data.server_now);
    if (receivedServerNow) setServerTimeOffset(receivedServerNow - Date.now());
    setActiveId((current) =>
      prefer && ids.has(prefer)
        ? prefer
        : ids.has(current)
          ? current
          : selectable[0]?.session_id || "",
    );
  }, []);
  const commitProtocolView = useCallback((sid, view) => {
    protocolViews.current.set(sid, view);
    const latest = view.latestTaskId ? view.tasks[view.latestTaskId] : null;
    setMessagesBySession((all) => ({ ...all, [sid]: view.messages }));
    setTaskHistoryBySession((all) => ({ ...all, [sid]: view.tasks }));
    setTasks((all) => {
      if (latest) return { ...all, [sid]: latest };
      const next = { ...all };
      delete next[sid];
      return next;
    });
  }, []);
  const loadFiles = useCallback(async (id = activeRef.current) => {
    if (!id) return;
    setFileBusyBySession((all) => ({ ...all, [id]: true }));
    try {
      const data = await request(
        `/v1/sessions/${encodeURIComponent(id)}/files`,
      ).then((r) => r.json());
      if (id === activeRef.current) setFiles(data.files || []);
    } finally {
      setFileBusyBySession((all) => ({ ...all, [id]: false }));
    }
  }, []);

  const rollbackPending = useCallback(
    (sid) => {
      const pending = pendingTurns.current.get(sid);
      if (!pending) return false;
      pendingTurns.current.delete(sid);
      const base = protocolViews.current.get(sid);
      if (base) {
        const next = {
          ...base,
          messages: base.messages.filter(
            (message) => message.localId !== pending.localId,
          ),
        };
        commitProtocolView(sid, next);
      } else
        setMessagesBySession((all) => ({
          ...all,
          [sid]: (all[sid] || []).filter(
            (message) => message.localId !== pending.localId,
          ),
        }));
      setTasks((all) => {
        if (all[sid]?.id !== null) return all;
        const next = { ...all };
        delete next[sid];
        return next;
      });
      return true;
    },
    [commitProtocolView],
  );
  const reduceIncoming = useCallback((sid, base, event) => {
    let input = base,
      pending = pendingTurns.current.get(sid);
    if (
      event.type === "user_message" &&
      pending &&
      pending.content === event.content
    ) {
      pendingTurns.current.delete(sid);
      input = {
        ...input,
        messages: input.messages.filter(
          (message) => message.localId !== pending.localId,
        ),
      };
    } else if (event.type === "turn_started" && pending) {
      pendingTurns.current.delete(sid);
      input = {
        ...input,
        messages: input.messages.map((message) =>
          message.localId === pending.localId
            ? {
                ...message,
                pending: false,
                turnId: event.turn_id,
                localId: `user-${event.turn_id}`,
              }
            : message,
        ),
      };
    }
    return reduceProtocolEvent(input, event);
  }, []);
  const handleEvent = useCallback(
    (sid, event) => {
      if (!sid) return;
      let base = protocolViews.current.get(sid) || {
        ...emptyProtocolView(messagesRef.current[sid] || []),
        tasks: { ...(taskHistoryRef.current[sid] || {}) },
        latestTaskId: taskRef.current[sid]?.id || null,
      };
      if (event.type === "turn_started") {
        setSessionError(sid, "");
        setSessions((all) =>
          all.map((session) =>
            session.session_id === sid
              ? {
                  ...session,
                  task_state: "running",
                  active_turn_id: event.turn_id,
                }
              : session,
          ),
        );
      }
      if (
        event.type === "error" &&
        event.recoverable === true &&
        pendingTurns.current.has(sid)
      ) {
        rollbackPending(sid);
        base =
          protocolViews.current.get(sid) ||
          emptyProtocolView(messagesRef.current[sid] || []);
      }
      const next = reduceIncoming(sid, base, event);
      commitProtocolView(sid, next);
      if (event.type === "error")
        setSessionError(sid, event.message || "任务执行失败");
      if (
        event.type === "progress" &&
        event.phase === "starting" &&
        event.status === "completed"
      )
        loadSessions().catch(() => {});
      if (event.type === "done") {
        setSessionError(sid, "");
        setSessions((all) =>
          all.map((session) =>
            session.session_id === sid
              ? {
                  ...session,
                  task_state: "idle",
                  active_turn_id: null,
                  last_turn_sequence: Math.max(
                    session.last_turn_sequence || 0,
                    event.sequence || 0,
                  ),
                }
              : session,
          ),
        );
        loadSessions().catch(() => {});
        if (sid === activeRef.current) loadFiles(sid).catch(() => {});
        if (sid !== activeRef.current)
          setTimeout(() => {
            const ws = sockets.current.get(sid);
            if (ws) {
              closing.current.add(sid);
              sockets.current.delete(sid);
              ws.close();
              setConnections((all) => ({ ...all, [sid]: "disconnected" }));
            }
          }, 0);
      }
    },
    [
      commitProtocolView,
      loadFiles,
      loadSessions,
      reduceIncoming,
      rollbackPending,
      setSessionError,
    ],
  );

  const closeSessionSocket = useCallback((sid) => {
    const timer = reconnectTimers.current.get(sid);
    if (timer) clearTimeout(timer);
    reconnectTimers.current.delete(sid);
    const ws = sockets.current.get(sid);
    if (ws) {
      closing.current.add(sid);
      sockets.current.delete(sid);
      ws.close();
    }
    setConnections((all) => ({ ...all, [sid]: "disconnected" }));
  }, []);
  const connectSession = useCallback(
    function openSession(sid) {
      if (!sid) return;
      const existing = sockets.current.get(sid);
      if (existing && existing.readyState !== 2 && existing.readyState !== 3)
        return;
      const timer = reconnectTimers.current.get(sid);
      if (timer) clearTimeout(timer);
      reconnectTimers.current.delete(sid);
      closing.current.delete(sid);
      const count = attempts.current.get(sid) || 0;
      setConnections((all) => ({
        ...all,
        [sid]: count ? "reconnecting" : "connecting",
      }));
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${scheme}://${location.host}/ws/chat`);
      let staging = null;
      let syncing = false;
      sockets.current.set(sid, ws);
      ws.onopen = () =>
        ws.send(JSON.stringify({ type: "hello", session_id: sid }));
      ws.onmessage = (e) => {
        let event;
        try {
          event = JSON.parse(e.data);
        } catch {
          return;
        }
        if (event.session_id && event.session_id !== sid) {
          setSessionError(sid, "收到属于其他 Session 的事件，已忽略");
          return;
        }
        if (event.type === "sync_begin") {
          staging = emptyProtocolView();
          syncing = true;
          return;
        }
        if (event.type === "ready") {
          attempts.current.set(sid, 0);
          let view = staging;
          let terminalWins = false;
          if (view) {
            ({ view, terminalWins } = applyReadySnapshot(view, event));
            commitProtocolView(sid, view);
            pendingTurns.current.delete(sid);
          }
          staging = null;
          syncing = false;
          setSessions((all) =>
            all.map((session) =>
              session.session_id === sid
                ? {
                    ...session,
                    task_state: terminalWins ? "idle" : event.task_state,
                    active_turn_id: terminalWins ? null : event.turn_id,
                    last_turn_sequence: Math.max(
                      session.last_turn_sequence || 0,
                      event.last_sequence || 0,
                    ),
                  }
                : session,
            ),
          );
          setConnections((all) => ({ ...all, [sid]: "connected" }));
          if (
            (terminalWins || event.task_state === "idle") &&
            sid !== activeRef.current
          )
            setTimeout(() => closeSessionSocket(sid), 0);
          return;
        }
        if (syncing && staging) {
          staging = reduceIncoming(sid, staging, event);
          return;
        }
        handleEvent(sid, event);
      };
      ws.onclose = () => {
        if (sockets.current.get(sid) !== ws) return;
        sockets.current.delete(sid);
        if (closing.current.delete(sid)) return;
        const summary = sessionsRef.current.find(
            (session) => session.session_id === sid,
          ),
          working =
            CLIENT_WORKING_STATES.includes(taskRef.current[sid]?.status) ||
            SERVER_WORKING_STATES.includes(summary?.task_state);
        if (sid === activeRef.current || working) {
          setConnections((all) => ({ ...all, [sid]: "reconnecting" }));
          const next = (attempts.current.get(sid) || 0) + 1;
          attempts.current.set(sid, next);
          reconnectTimers.current.set(
            sid,
            setTimeout(() => openSession(sid), Math.min(8000, 500 * 2 ** next)),
          );
        } else setConnections((all) => ({ ...all, [sid]: "disconnected" }));
      };
      ws.onerror = () => {
        if (ws.readyState === WebSocket.CLOSED)
          setConnections((all) => ({ ...all, [sid]: "reconnecting" }));
      };
    },
    [
      closeSessionSocket,
      commitProtocolView,
      handleEvent,
      reduceIncoming,
      setSessionError,
    ],
  );
  useEffect(() => {
    loadSessions().catch((e) => setError(e.message));
  }, [loadSessions]);
  useEffect(() => {
    const previous = previousActive.current;
    previousActive.current = activeId;
    if (previous && previous !== activeId) {
      const summary = sessionsRef.current.find(
        (session) => session.session_id === previous,
      );
      if (
        !CLIENT_WORKING_STATES.includes(taskRef.current[previous]?.status) &&
        !SERVER_WORKING_STATES.includes(summary?.task_state)
      )
        closeSessionSocket(previous);
    }
    if (!activeId || !isSelectableSession(selected)) return;
    setFiles([]);
    loadFiles(activeId).catch((e) => setError(e.message));
  }, [
    activeId,
    closeSessionSocket,
    loadFiles,
    selected?.state,
    selected?.compatible,
  ]);
  useEffect(() => {
    for (const session of sessions) {
      if (!isSelectableSession(session)) continue;
      if (
        session.session_id === activeId ||
        SERVER_WORKING_STATES.includes(session.task_state)
      )
        connectSession(session.session_id);
    }
    for (const sid of sockets.current.keys()) {
      const summary = sessions.find((session) => session.session_id === sid);
      if (
        sid !== activeId &&
        !SERVER_WORKING_STATES.includes(summary?.task_state) &&
        !CLIENT_WORKING_STATES.includes(taskRef.current[sid]?.status)
      )
        closeSessionSocket(sid);
    }
  }, [activeId, closeSessionSocket, connectSession, sessions]);
  useEffect(() => {
    const closeAll = () => {
      for (const timer of reconnectTimers.current.values()) clearTimeout(timer);
      reconnectTimers.current.clear();
      for (const [sid, ws] of sockets.current) {
        closing.current.add(sid);
        ws.close();
      }
      sockets.current.clear();
    };
    window.addEventListener("beforeunload", closeAll);
    return () => {
      window.removeEventListener("beforeunload", closeAll);
      closeAll();
    };
  }, []);
  const scrollRevision = messages
    .map(
      (message) =>
        `${message.localId || message.sequence}:${message.content.length}:${message.streaming ? 1 : 0}`,
    )
    .join("|");
  useLayoutEffect(() => {
    followMessages.current = true;
    const node = conversationRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [activeId]);
  useLayoutEffect(() => {
    if (followMessages.current) {
      const node = conversationRef.current;
      if (node) node.scrollTop = node.scrollHeight;
    }
  }, [scrollRevision, activeTask?.lastSequence, activeTask?.steps.length]);
  useEffect(() => {
    document
      .querySelector(".session-row.selected")
      ?.scrollIntoView({ block: "nearest" });
  }, [activeId, sessions]);

  const invalidateProviderTest = (field, value) => {
    providerTestGeneration.current += 1;
    setProviderBusy(false);
    setDraftProvider((current) => ({ ...current, [field]: value }));
    setProviderTest(null);
    setError("");
    if (!provider.base_url || !provider.api_key)
      setProviderStatus("unconfigured");
  };
  const testProvider = async () => {
    const candidate = normalizedProvider(draftProvider),
      fingerprint = providerFingerprint(candidate),
      generation = ++providerTestGeneration.current;
    setProviderTest(null);
    if (!candidate.base_url || !candidate.api_key) {
      setError("请填写 Provider Endpoint 和 Provider API Key");
      return;
    }
    setProviderBusy(true);
    try {
      const catalog = await request("/v1/web/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(candidate),
      }).then((r) => r.json());
      if (
        generation !== providerTestGeneration.current ||
        providerFingerprint(draftProviderRef.current) !== fingerprint
      )
        return;
      const models = Array.isArray(catalog.models) ? catalog.models : [],
        model = catalogModel({ ...catalog, models }, sessionDefaults.model);
      setProviderTest({ ...catalog, models, fingerprint });
      setDraftDefaults({ model, effort: sessionDefaults.effort });
      if (!provider.base_url || !provider.api_key)
        setProviderStatus("unconfigured");
      setError("");
    } catch (e) {
      if (
        generation !== providerTestGeneration.current ||
        providerFingerprint(draftProviderRef.current) !== fingerprint
      )
        return;
      setProviderTest(null);
      if (!provider.base_url || !provider.api_key) setProviderStatus("failed");
      setError(e.message);
    } finally {
      if (generation === providerTestGeneration.current) setProviderBusy(false);
    }
  };
  const saveProvider = () => {
    if (!providerCanSave) return;
    const candidate = normalizedProvider(draftProvider),
      defaults = { model: draftDefaults.model, effort: draftDefaults.effort },
      { fingerprint: _, ...catalog } = providerTest;
    localStorage.setItem(STORAGE.endpoint, candidate.base_url);
    localStorage.setItem(STORAGE.providerKey, candidate.api_key);
    localStorage.setItem(STORAGE.auth, candidate.auth_env);
    localStorage.setItem(STORAGE.defaultModel, defaults.model);
    localStorage.setItem(STORAGE.defaultEffort, defaults.effort);
    setProvider(candidate);
    setSessionDefaults(defaults);
    setConfig((current) => ({
      ...current,
      ...catalog,
      default_model: defaults.model,
    }));
    setProviderStatus("configured");
    setSettings(false);
    setError("");
  };
  const createSession = async () => {
    if (creationLock.current) return;
    const sourceId = activeId,
      draft = promptsBySession[sourceId] || "";
    creationLock.current = true;
    setCreationBusy(true);
    try {
      const created = await request("/v1/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: "新会话",
          last_model: sessionDefaults.model || null,
          last_effort: sessionDefaults.effort || null,
        }),
      }).then((r) => r.json());
      if (!created?.session_id) throw new Error("服务端未返回 Session ID");
      sessionLoadVersion.current += 1;
      setSessions((all) => [
        created,
        ...all.filter((session) => session.session_id !== created.session_id),
      ]);
      if (draft)
        setPromptsBySession((all) => {
          const next = { ...all, [created.session_id]: draft };
          delete next[sourceId];
          return next;
        });
      setActiveId(created.session_id);
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      creationLock.current = false;
      setCreationBusy(false);
    }
  };
  const selectSession = (id) => {
    setActiveId(id);
    setSessionDrawer(false);
  };
  const openSessionMenu = (session, event) => {
    event.preventDefault();
    const width = 168,
      height = 62;
    setSessionMenu({
      session_id: session.session_id,
      x: clamp(event.clientX, 8, Math.max(8, window.innerWidth - width - 8)),
      y: clamp(event.clientY, 8, Math.max(8, window.innerHeight - height - 8)),
    });
  };
  const deleteSession = async () => {
    const id = sessionMenu?.session_id;
    if (!id) return;
    const summary = sessionsRef.current.find(
        (session) => session.session_id === id,
      ),
      working =
        CLIENT_WORKING_STATES.includes(taskRef.current[id]?.status) ||
        SERVER_WORKING_STATES.includes(summary?.task_state);
    if (working) {
      setError("请先停止正在运行的任务");
      return;
    }
    if (!window.confirm("删除会话后，沙箱文件也会永久删除。确定继续吗？")) {
      setSessionMenu(null);
      return;
    }
    setSessionMenu(null);
    try {
      await request(`/v1/sessions/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      sessionLoadVersion.current += 1;
      closeSessionSocket(id);
      protocolViews.current.delete(id);
      pendingTurns.current.delete(id);
      const remaining = sessionsRef.current.filter(
        (session) => session.session_id !== id,
      );
      setSessions(remaining);
      setMessagesBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setTaskHistoryBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setTasks((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setConnections((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setSessionErrors((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setPromptsBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setFileQueriesBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setFileBusyBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setFileFeedbackBySession((all) => {
        const next = { ...all };
        delete next[id];
        return next;
      });
      setActiveId((current) =>
        current === id
          ? remaining.find(isSelectableSession)?.session_id || ""
          : current,
      );
      if (id === activeRef.current) setFiles([]);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  };
  const changeModel = async (model) => {
    if (!selected) return;
    try {
      const updated = await request(
        `/v1/sessions/${encodeURIComponent(activeId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ last_model: model }),
        },
      ).then((r) => r.json());
      setSessions((all) =>
        all.map((s) => (s.session_id === activeId ? updated : s)),
      );
    } catch (e) {
      setError(e.message);
    }
  };
  const changeEffort = async (effort) => {
    if (!selected) return;
    try {
      const updated = await request(
        `/v1/sessions/${encodeURIComponent(activeId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ last_effort: effort || null }),
        },
      ).then((r) => r.json());
      setSessions((all) =>
        all.map((s) => (s.session_id === activeId ? updated : s)),
      );
    } catch (e) {
      setError(e.message);
    }
  };
  const send = async () => {
    const sid = activeId,
      content = prompt.trim(),
      socket = sockets.current.get(sid);
    if (
      !content ||
      creationBusy ||
      running ||
      connection !== "connected" ||
      !selected
    )
      return;
    if (!provider.base_url || !provider.api_key || !providerReady) {
      setError("请先在连接设置中配置 Provider 并加载模型");
      setSettings(true);
      return;
    }
    if (!selectedModelValid) {
      setError("请为此 Session 选择当前 Provider 支持的模型");
      return;
    }
    if (stateOf(selected) === "deleted") {
      setError("这个沙箱已删除，无法恢复。请新建 Session。");
      return;
    }
    if (!socket || socket.readyState !== 1) {
      attempts.current.set(sid, Math.max(1, attempts.current.get(sid) || 0));
      setConnections((all) => ({ ...all, [sid]: "reconnecting" }));
      connectSession(sid);
      setSessionError(sid, "连接刚刚中断，正在重连；输入内容已保留");
      return;
    }
    setPrompt("");
    setError("");
    setSessionError(sid, "");
    const at = new Date().toISOString(),
      localTurn = `local-${Date.now()}`,
      localId = `user-${localTurn}`,
      base =
        protocolViews.current.get(sid) ||
        emptyProtocolView(messagesRef.current[sid] || []),
      next = reduceProtocolEvent(base, {
        type: "user_message",
        turn_id: localTurn,
        sequence: 0,
        content,
        at,
        pending: true,
      });
    pendingTurns.current.set(sid, { localTurn, localId, content });
    commitProtocolView(sid, next);
    setTasks((all) => ({
      ...all,
      [sid]: {
        id: null,
        status: "starting",
        startedAt: Date.now(),
        endedAt: null,
        lastSequence: 0,
        steps: [],
        usage: null,
      },
    }));
    try {
      socket.send(
        JSON.stringify({
          type: "message",
          content,
          model: selectedModel,
          effort: selectedEffort || null,
          provider,
        }),
      );
    } catch {
      rollbackPending(sid);
      setPromptsBySession((all) => ({ ...all, [sid]: content }));
      if (sockets.current.get(sid) === socket) sockets.current.delete(sid);
      try {
        socket.close();
      } catch {
        // The connection is already unusable; reconnect below.
      }
      attempts.current.set(sid, Math.max(1, attempts.current.get(sid) || 0));
      setConnections((all) => ({ ...all, [sid]: "reconnecting" }));
      setSessionError(sid, "发送失败，输入内容已恢复；正在重新连接");
      connectSession(sid);
      return;
    }
    if (!selected.title || selected.title === "新会话") {
      request(`/v1/sessions/${encodeURIComponent(sid)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: content.slice(0, 36),
          last_model: selectedModel,
        }),
      })
        .then((r) => r.json())
        .then((updated) =>
          setSessions((all) =>
            all.map((s) => (s.session_id === sid ? updated : s)),
          ),
        )
        .catch(() => {});
    }
  };
  const stop = () => {
    const sid = activeRef.current,
      task = taskRef.current[sid],
      ws = sockets.current.get(sid);
    if (!task?.id) return;
    if (!ws || ws.readyState !== 1) {
      setSessionError(sid, stopHint);
      return;
    }
    const view = protocolViews.current.get(sid);
    if (view?.tasks[task.id])
      commitProtocolView(sid, {
        ...view,
        tasks: {
          ...view.tasks,
          [task.id]: { ...view.tasks[task.id], status: "stopping" },
        },
      });
    else
      setTasks((all) => ({
        ...all,
        [sid]: { ...all[sid], status: "stopping" },
      }));
    ws.send(JSON.stringify({ type: "stop", turn_id: task.id }));
  };
  const upload = async (list) => {
    const sid = activeRef.current;
    if (!sid || !list?.length) return;
    setFileBusyBySession((all) => ({ ...all, [sid]: true }));
    setFileFeedbackBySession((all) => ({ ...all, [sid]: "正在上传…" }));
    try {
      const body = new FormData();
      [...list].forEach((file) =>
        body.append("files", file, file.webkitRelativePath || file.name),
      );
      await request(`/v1/sessions/${encodeURIComponent(sid)}/files`, {
        method: "POST",
        body,
      });
      await loadFiles(sid);
      setFileFeedbackBySession((all) => ({
        ...all,
        [sid]: `已上传 ${list.length} 个文件`,
      }));
    } catch (e) {
      setFileFeedbackBySession((all) => ({
        ...all,
        [sid]: `上传失败：${e.message}`,
      }));
    } finally {
      setFileBusyBySession((all) => ({ ...all, [sid]: false }));
    }
  };
  const refreshFiles = async () => {
    const sid = activeRef.current;
    if (!sid) return;
    setFileFeedbackBySession((all) => ({ ...all, [sid]: "正在刷新…" }));
    try {
      await loadFiles(sid);
      setFileFeedbackBySession((all) => ({ ...all, [sid]: "文件列表已刷新" }));
    } catch (e) {
      setFileFeedbackBySession((all) => ({
        ...all,
        [sid]: `刷新失败：${e.message}`,
      }));
    }
  };
  const openFile = async (path) => {
    const sid = activeRef.current;
    if (!sid) return;
    try {
      const encoded = path.split("/").map(encodeURIComponent).join("/"),
        response = await request(
          `/v1/sessions/${encodeURIComponent(sid)}/files/content/${encoded}`,
        ),
        source = await response.blob(),
        activeContent =
          /^(?:text\/html|image\/svg\+xml)(?:;|$)/i.test(source.type) ||
          /\.(?:html?|svg)$/i.test(path),
        blob = activeContent
          ? new Blob([await source.arrayBuffer()], {
              type: "application/octet-stream",
            })
          : source,
        url = URL.createObjectURL(blob),
        anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = path.split("/").at(-1) || "download";
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      setSessionError(sid, e.message);
    }
  };
  const openLog = async () => {
    const sid = activeRef.current,
      session = sessionsRef.current.find((item) => item.session_id === sid);
    if (!sid || !session) return;
    logAbort.current?.abort();
    const controller = new AbortController(),
      generation = ++logGeneration.current;
    logAbort.current = controller;
    try {
      const html = await request(
        `/v1/sessions/${encodeURIComponent(sid)}/log`,
        { signal: controller.signal },
      ).then((r) => r.text());
      if (generation !== logGeneration.current || sid !== activeRef.current)
        return;
      setLogHtml({ html, sessionId: sid, title: titleOf(session) });
    } catch (e) {
      if (e.name !== "AbortError" && generation === logGeneration.current)
        setSessionError(sid, e.message);
    } finally {
      if (generation === logGeneration.current) logAbort.current = null;
    }
  };
  const resize = (side, event) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const start = event.clientX,
      initial = side === "left" ? leftWidth : rightWidth;
    const move = (e) =>
      side === "left"
        ? setLeftWidth(clamp(initial + e.clientX - start, 190, 360))
        : setRightWidth(clamp(initial - e.clientX + start, 240, 480));
    const end = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
  };
  const resizeKey = (side, e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const delta = e.key === "ArrowRight" ? 10 : -10;
    side === "left"
      ? setLeftWidth((v) => clamp(v + delta, 190, 360))
      : setRightWidth((v) => clamp(v - delta, 240, 480));
  };
  const toggleSettings = () => {
    providerTestGeneration.current += 1;
    setProviderBusy(false);
    if (settings) {
      setSettings(false);
      return;
    }
    setDraftProvider({ ...provider });
    setDraftDefaults({ ...sessionDefaults });
    setProviderTest(null);
    setError("");
    setSettings(true);
  };
  const displayedConnection =
    providerStatus === "unconfigured"
      ? "unconfigured"
      : providerStatus === "failed"
        ? "provider_failed"
        : providerStatus === "loading"
          ? "provider_loading"
          : connection;
  const connCopy = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重新连接",
    auth_error: "认证失败",
    disconnected: "未连接",
    failed: "连接失败",
    unconfigured: "未配置",
    provider_failed: "配置失败",
    provider_loading: "验证中",
  }[displayedConnection];
  const life = lifecycle(selected);
  const deleteMinutes = selected?.delete_at
    ? Math.max(
        0,
        Math.ceil(
          ((parseTime(selected.delete_at) || 0) -
            (parseTime(serverNow) || Date.now())) /
            60000,
        ),
      )
    : null;
  const menuSession = sessions.find(
      (session) => session.session_id === sessionMenu?.session_id,
    ),
    menuWorking =
      CLIENT_WORKING_STATES.includes(tasks[sessionMenu?.session_id]?.status) ||
      SERVER_WORKING_STATES.includes(menuSession?.task_state);
  return (
    <div
      className={`app-shell ${leftCollapsed ? "left-collapsed" : ""} ${rightCollapsed ? "right-collapsed" : ""}`}
      style={{
        "--left-width": `${leftWidth}px`,
        "--right-width": `${rightWidth}px`,
      }}
    >
      <SessionRail
        sessions={sessions}
        tasks={tasks}
        activeId={activeId}
        serverNow={serverNow}
        onSelect={selectSession}
        onNew={createSession}
        onPolicy={() => setPolicyOpen(true)}
        onHelp={() => setHelpOpen(true)}
        onContextMenu={openSessionMenu}
        creationBusy={creationBusy}
      />
      <div className="mobile-rail">
        <span>
          <BracketsCurly />
        </span>
        <IconButton
          label="打开 Session 列表"
          onClick={() => {
            setDrawer(false);
            setSessionDrawer(true);
          }}
        >
          <Robot />
        </IconButton>
        <IconButton
          label="新建会话"
          disabled={creationBusy}
          onClick={createSession}
        >
          <Plus />
        </IconButton>
        <IconButton
          label="打开沙箱文件"
          onClick={() => {
            setSessionDrawer(false);
            setDrawer(true);
          }}
        >
          <FileText />
        </IconButton>
        <div />
        <IconButton label="产品设置" onClick={() => setPolicyOpen(true)}>
          <Gear />
        </IconButton>
        <IconButton label="帮助" onClick={() => setHelpOpen(true)}>
          <Question />
        </IconButton>
      </div>
      <div
        className="splitter left-splitter"
        role="separator"
        aria-label="调整 Session 栏宽度"
        aria-orientation="vertical"
        tabIndex="0"
        onPointerDown={(e) => resize("left", e)}
        onKeyDown={(e) => resizeKey("left", e)}
      />
      <button
        type="button"
        className="edge-toggle left-edge"
        aria-label={leftCollapsed ? "展开 Session 栏" : "收起 Session 栏"}
        onClick={() => setLeftCollapsed((v) => !v)}
      >
        {leftCollapsed ? <CaretRight /> : <CaretLeft />}
      </button>
      <main className="workspace">
        <header className="topbar">
          <div>
            <h1>{selected ? titleOf(selected) : "WebAgent"}</h1>
            {!selected && <p>新建 Session 后开始工作</p>}
          </div>
          <div className="connection-controls">
            <div
              id="connectionBadge"
              className={`connection-badge ${displayedConnection}`}
              role="status"
            >
              <i />
              <b>{connCopy}</b>
            </div>
            <IconButton label="连接设置" onClick={toggleSettings}>
              <Gear />
            </IconButton>
            {settings && (
              <div className="connection-popover">
                <header>
                  <strong>Provider 连接设置</strong>
                  <IconButton label="关闭连接设置" onClick={toggleSettings}>
                    <X />
                  </IconButton>
                </header>
                <label>
                  Provider Endpoint
                  <input
                    id="providerEndpoint"
                    type="url"
                    value={draftProvider.base_url}
                    onChange={(e) =>
                      invalidateProviderTest("base_url", e.target.value)
                    }
                    placeholder="https://api.anthropic.com"
                  />
                </label>
                <label>
                  Provider API Key
                  <input
                    id="providerApiKey"
                    type="password"
                    autoComplete="off"
                    value={draftProvider.api_key}
                    onChange={(e) =>
                      invalidateProviderTest("api_key", e.target.value)
                    }
                    placeholder="输入模型服务凭据"
                  />
                </label>
                <label>
                  认证方式
                  <select
                    id="providerAuth"
                    value={draftProvider.auth_env}
                    onChange={(e) =>
                      invalidateProviderTest("auth_env", e.target.value)
                    }
                  >
                    {(
                      config.provider_auth_modes || [
                        "ANTHROPIC_AUTH_TOKEN",
                        "ANTHROPIC_API_KEY",
                      ]
                    ).map((mode) => (
                      <option value={mode} key={mode}>
                        {mode === "ANTHROPIC_AUTH_TOKEN"
                          ? "Bearer token"
                          : "x-api-key"}
                      </option>
                    ))}
                  </select>
                </label>
                <p>
                  先测试 Provider 并读取模型目录；测试不会修改当前已保存连接。
                </p>
                <button
                  id="testProvider"
                  type="button"
                  className="secondary-button provider-test-button"
                  disabled={
                    providerBusy ||
                    !draftProvider.base_url.trim() ||
                    !draftProvider.api_key.trim()
                  }
                  onClick={testProvider}
                >
                  {providerBusy ? "正在测试…" : "测试连接"}
                </button>
                {testedProviderMatches && (
                  <div className="provider-tested">
                    <p className="provider-test-success" role="status">
                      连接测试成功，发现 {providerTest.models.length} 个模型
                    </p>
                    <label>
                      新 Session 默认模型
                      <select
                        id="providerDefaultModel"
                        value={draftDefaults.model}
                        onChange={(e) =>
                          setDraftDefaults((current) => ({
                            ...current,
                            model: e.target.value,
                          }))
                        }
                      >
                        {providerTest.models.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      新 Session 默认强度
                      <select
                        id="providerDefaultEffort"
                        value={draftDefaults.effort}
                        onChange={(e) =>
                          setDraftDefaults((current) => ({
                            ...current,
                            effort: e.target.value,
                          }))
                        }
                      >
                        <option value="">Provider 默认</option>
                        {EFFORTS.map((effort) => (
                          <option key={effort} value={effort}>
                            {EFFORT_LABELS[effort]}（{effort}）
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                )}
                <button
                  id="saveProvider"
                  type="button"
                  className="primary-button"
                  disabled={!providerCanSave || providerBusy}
                  onClick={saveProvider}
                >
                  保存设置
                </button>
              </div>
            )}
          </div>
        </header>
        <section
          id="messages"
          className="conversation"
          ref={conversationRef}
          onScroll={() => {
            const node = conversationRef.current;
            if (node)
              followMessages.current =
                node.scrollHeight - node.scrollTop - node.clientHeight <= 48;
          }}
        >
          <div className="conversation-inner">
            {selected && life.tone !== "active" && (
              <div className={`lifecycle-banner ${life.tone}`}>
                <WarningCircle />
                <span>
                  {life.tone === "deleted"
                    ? "此沙箱已删除，不能恢复。"
                    : life.tone === "paused"
                      ? "沙箱已暂停。发送下一条消息时会自动恢复。"
                      : `沙箱即将删除${deleteMinutes != null ? `，剩余约 ${deleteMinutes} 分钟` : ""}。发送消息会自动恢复。`}
                </span>
                {life.tone === "deleted" && (
                  <button type="button" onClick={createSession}>
                    新建 Session
                  </button>
                )}
              </div>
            )}
            <Messages
              messages={messages}
              task={activeTask}
              tasksByTurn={taskHistory}
              now={now}
              onStop={stop}
              stopDisabled={!stopAvailable}
              stopHint={stopHint}
            />
          </div>
        </section>
        <footer id="composer" className="composer">
          <div className="composer-tools">
            <div className="composer-settings">
              <ModelPicker
                models={config.models}
                value={selectedModel}
                invalid={Boolean(
                  selected && providerReady && !selectedModelValid,
                )}
                disabled={
                  creationBusy || !selected || running || !providerReady
                }
                onChange={changeModel}
              />
              <EffortPicker
                value={selectedEffort}
                disabled={creationBusy || !selected || running}
                onChange={changeEffort}
              />
            </div>
            <button
              id="openLog"
              type="button"
              className="log-button"
              disabled={creationBusy || !selected}
              onClick={openLog}
            >
              <FileText />
              查看 Session 日志
            </button>
          </div>
          <div className="composer-box">
            <IconButton
              label="添加附件"
              disabled={creationBusy || !selected || running}
              onClick={() => document.getElementById("fileInput")?.click()}
            >
              <Paperclip />
            </IconButton>
            <textarea
              id="prompt"
              value={prompt}
              disabled={
                creationBusy || !selected || stateOf(selected) === "deleted"
              }
              rows="2"
              placeholder={selected ? "发送任务…" : "请先新建 Session"}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            {running ? (
              <button
                id="sendButton"
                type="button"
                className="send-button stop"
                onClick={stop}
                title={!stopAvailable ? stopHint : undefined}
                disabled={
                  creationBusy ||
                  !stopAvailable ||
                  ["stopping", "finishing"].includes(activeTask?.status) ||
                  selected?.task_state === "finishing"
                }
              >
                <Stop weight="fill" />
                停止
              </button>
            ) : (
              <button
                id="sendButton"
                type="button"
                className="send-button"
                onClick={send}
                disabled={
                  creationBusy ||
                  !prompt.trim() ||
                  connection !== "connected" ||
                  !selected ||
                  !providerReady ||
                  !selectedModelValid
                }
              >
                发送
              </button>
            )}
          </div>
          <small>
            {running &&
            !stopAvailable &&
            !["stopping", "finishing"].includes(activeTask?.status) &&
            selected?.task_state !== "finishing"
              ? stopHint
              : "Enter 发送 · Shift+Enter 换行"}
          </small>
          {displayedError && (
            <div className="error-toast" role="alert">
              {displayedError}
              <button
                type="button"
                aria-label="关闭错误"
                onClick={() => {
                  setError("");
                  setSessionError(activeId, "");
                }}
              >
                <X />
              </button>
            </div>
          )}
        </footer>
      </main>
      <button
        type="button"
        className="edge-toggle right-edge"
        aria-label={rightCollapsed ? "展开沙箱文件" : "收起沙箱文件"}
        onClick={() => setRightCollapsed((v) => !v)}
      >
        {rightCollapsed ? <CaretLeft /> : <CaretRight />}
      </button>
      <div
        className="splitter right-splitter"
        role="separator"
        aria-label="调整沙箱文件栏宽度"
        aria-orientation="vertical"
        tabIndex="0"
        onPointerDown={(e) => resize("right", e)}
        onKeyDown={(e) => resizeKey("right", e)}
      />
      <FilesPanel
        sessionId={activeId}
        files={files}
        query={fileQuery}
        setQuery={setFileQuery}
        busy={fileBusy}
        feedback={fileFeedback}
        onRefresh={refreshFiles}
        onUpload={upload}
        onOpen={openFile}
      />
      {sessionMenu && (
        <div
          className="session-context-menu"
          role="menu"
          style={{ left: sessionMenu.x, top: sessionMenu.y }}
        >
          <button
            type="button"
            role="menuitem"
            disabled={menuWorking}
            onClick={deleteSession}
          >
            删除会话
          </button>
          {menuWorking && <small>请先停止当前任务</small>}
        </div>
      )}
      {drawer && (
        <div
          className="drawer-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setDrawer(false);
          }}
        >
          <div className="file-drawer">
            <FilesPanel
              sessionId={activeId}
              files={files}
              query={fileQuery}
              setQuery={setFileQuery}
              busy={fileBusy}
              feedback={fileFeedback}
              onRefresh={refreshFiles}
              onUpload={upload}
              onOpen={openFile}
              onClose={() => setDrawer(false)}
              inputPrefix="drawer"
            />
          </div>
        </div>
      )}
      {sessionDrawer && (
        <div
          className="drawer-backdrop session-drawer-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setSessionDrawer(false);
          }}
        >
          <div className="session-drawer">
            <SessionRail
              sessions={sessions}
              tasks={tasks}
              activeId={activeId}
              serverNow={serverNow}
              onSelect={selectSession}
              onNew={createSession}
              onPolicy={() => {
                setSessionDrawer(false);
                setPolicyOpen(true);
              }}
              onHelp={() => {
                setSessionDrawer(false);
                setHelpOpen(true);
              }}
              onContextMenu={openSessionMenu}
              creationBusy={creationBusy}
              newButtonId="mobileNewSession"
              navLabel="移动 Session 列表"
            />
          </div>
        </div>
      )}
      {logHtml !== null && (
        <Dialog
          id="logDialog"
          title={`${logHtml.title} · Session 详细诊断日志 (${logHtml.sessionId})`}
          onClose={closeLog}
        >
          <p className="dialog-note">
            按实际执行顺序展示用户输入、模型输出、工具参数与结果。
          </p>
          <iframe
            id="sessionLogFrame"
            title={`${logHtml.title} Session 详细诊断日志`}
            sandbox=""
            srcDoc={logHtml.html}
          />
          <button
            id="closeLog"
            type="button"
            className="secondary-button"
            onClick={closeLog}
          >
            关闭
          </button>
        </Dialog>
      )}
      {policyOpen && (
        <Dialog
          id="policyDialog"
          title="产品策略"
          onClose={() => setPolicyOpen(false)}
        >
          <div className="policy-grid">
            <span>空闲暂停</span>
            <strong>
              {config.policies?.pause_after_seconds != null
                ? `${Math.round(config.policies.pause_after_seconds / 60)} 分钟`
                : "由服务端管理"}
            </strong>
            <span>沙箱删除</span>
            <strong>
              {config.policies?.delete_after_seconds != null
                ? `${Math.round(config.policies.delete_after_seconds / 60)} 分钟`
                : "由服务端管理"}
            </strong>
            <span>运行时</span>
            <strong>{config.policies?.runtime || config.runtime || "—"}</strong>
            <span>沙箱</span>
            <strong>{config.policies?.sandbox || config.sandbox || "—"}</strong>
          </div>
          <p className="dialog-note">这些策略由服务端配置，当前页面只读。</p>
        </Dialog>
      )}
      {helpOpen && (
        <Dialog
          id="helpDialog"
          title="使用帮助"
          onClose={() => setHelpOpen(false)}
        >
          <div className="help-copy">
            <p>每个 Session 保存独立对话、模型选择与沙箱文件。</p>
            <p>任务运行时可查看结构化步骤和安全活动摘要，也可以随时停止。</p>
            <p>
              文件单击不执行操作，双击会通过文件内容接口交给浏览器打开或下载。
            </p>
          </div>
        </Dialog>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
