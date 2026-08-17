import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./admin.css";

const LABELS = {
  session_pause_after_seconds: "空闲暂停（秒）",
  session_delete_after_seconds: "暂停后删除（秒）",
  session_reaper_interval_seconds: "清理扫描间隔（秒）",
  session_delete_workspace: "删除 Session 时删除工作区",
  claude_timeout_seconds: "任务超时（秒）",
  docker_cpus: "容器 CPU 上限",
  docker_memory: "容器内存上限",
  docker_pids_limit: "容器 PID 上限",
  file_editor_max_bytes: "文本编辑器上限（kB）",
  file_upload_max_bytes: "单文件上传上限（kB）",
  file_upload_max_files_per_session: "Session 累计上传文件数上限",
};

const BYTES_PER_KILOBYTE = 1024;
const BYTE_LIMIT_KEYS = new Set([
  "file_editor_max_bytes",
  "file_upload_max_bytes",
]);

function settingsDraft(saved = {}) {
  return Object.fromEntries(
    Object.entries(saved).map(([key, value]) => [
      key,
      BYTE_LIMIT_KEYS.has(key) && Number.isFinite(value)
        ? value / BYTES_PER_KILOBYTE
        : value,
    ]),
  );
}

function settingsPayload(draft) {
  return Object.fromEntries(
    Object.entries(draft).map(([key, value]) => {
      const limitInKilobytes = Number(value);
      return [
        key,
        BYTE_LIMIT_KEYS.has(key) && Number.isFinite(limitInKilobytes)
          ? Math.round(limitInKilobytes * BYTES_PER_KILOBYTE)
          : value,
      ];
    }),
  );
}

function activeSettingValue(key, value) {
  if (BYTE_LIMIT_KEYS.has(key) && Number.isFinite(value)) {
    return `${value / BYTES_PER_KILOBYTE} kB`;
  }
  return String(value ?? "—");
}

const ADMIN_VIEWS = new Set([
  "overview",
  "monitor",
  "users",
  "sessions",
  "settings",
]);

function viewFromHash() {
  const view = window.location.hash.slice(1);
  return ADMIN_VIEWS.has(view) ? view : "overview";
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      data.detail || data.error?.message || `HTTP ${response.status}`,
    );
  }
  return response.json();
}

function Card({ title, note, children, className = "", ...props }) {
  return (
    <section className={`admin-card ${className}`} {...props}>
      <header>
        <div>
          <h2>{title}</h2>
          {note && <p>{note}</p>}
        </div>
      </header>
      {children}
    </section>
  );
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (Math.abs(size) >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatPercent(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : "—";
}

function dockerStatusNote(docker) {
  if (docker.disabled) return "Docker 未启用";
  if (!docker.available) return "Docker 不可用";
  if (docker.error) return "Docker 负载采集失败";
  return "Docker 可用";
}

const STATUS_LABELS = {
  ok: "正常",
  degraded: "有风险",
  error: "异常",
  disabled: "未启用",
};

function Sparkline({ history, valueKey, label, format = formatPercent }) {
  const values = history
    .map((point) => point[valueKey])
    .filter((value) => Number.isFinite(value));
  const maximum = Math.max(...values, 1);
  const minimum = Math.min(...values, 0);
  const range = Math.max(maximum - minimum, 1);
  const points = values
    .map((value, index) => {
      const x = values.length === 1 ? 100 : (index / (values.length - 1)) * 100;
      const y = 38 - ((value - minimum) / range) * 34;
      return `${x},${y}`;
    })
    .join(" ");
  const latest = values.at(-1);

  return (
    <div className="monitor-chart">
      <span>{label}</span>
      <strong>{format(latest)}</strong>
      <svg
        viewBox="0 0 100 40"
        role="img"
        aria-label={`${label}最近一小时曲线`}
      >
        <line x1="0" y1="38" x2="100" y2="38" />
        {values.length > 0 && <polyline points={points} />}
      </svg>
      <small>{values.length ? `${values.length} 个采样点` : "等待采样"}</small>
    </div>
  );
}

function StatusPill({ status }) {
  return (
    <span className={`monitor-status status-${status}`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}

function MonitorView({
  data,
  loading,
  error,
  autoRefresh,
  onAutoRefresh,
  onRefresh,
}) {
  const snapshot = data?.snapshot;
  const history = data?.history || [];
  const components = data?.components || [];
  const issues = data?.issues || [];
  const tasks = data?.tasks || [];

  return (
    <section id="monitor" className="monitor-view">
      <div className={`monitor-summary summary-${data?.status || "loading"}`}>
        <div>
          <span>总体状态</span>
          <strong>{data ? STATUS_LABELS[data.status] : "载入中"}</strong>
          <small>
            {data
              ? `更新于 ${formatTime(data.generated_at)} · 保留 ${Math.round(data.retention_seconds / 60)} 分钟`
              : "正在读取主机与 WebAgent 组件状态"}
          </small>
        </div>
        <div className="monitor-controls">
          <label>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => onAutoRefresh(event.target.checked)}
            />
            5 秒自动刷新
          </label>
          <button type="button" onClick={onRefresh} disabled={loading}>
            {loading ? "刷新中…" : "立即刷新"}
          </button>
        </div>
      </div>

      {error && (
        <div className="admin-notice error" role="alert">
          监控数据获取失败：{error}
        </div>
      )}

      {!snapshot && !error && (
        <Card title="正在初始化监控" note="首次异步采样完成后会自动显示负载。">
          <p className="monitor-loading" role="status">
            正在加载监控数据…
          </p>
        </Card>
      )}

      {snapshot && (
        <div className="monitor-load-grid">
          <Card title="主机负载" note={`采样时间：${formatTime(snapshot.at)}`}>
            <dl className="monitor-facts">
              <div>
                <dt>CPU</dt>
                <dd>{formatPercent(snapshot.host.cpu_percent)}</dd>
              </div>
              <div>
                <dt>内存</dt>
                <dd>
                  {formatBytes(snapshot.host.memory_used_bytes)} /{" "}
                  {formatBytes(snapshot.host.memory_total_bytes)}
                </dd>
              </div>
              <div>
                <dt>负载</dt>
                <dd>
                  {snapshot.host.load_1m.toFixed(2)} /{" "}
                  {snapshot.host.load_5m.toFixed(2)} /{" "}
                  {snapshot.host.load_15m.toFixed(2)}
                </dd>
              </div>
              <div>
                <dt>磁盘</dt>
                <dd>{formatPercent(snapshot.host.disk_percent)}</dd>
              </div>
              <div>
                <dt>服务 RSS</dt>
                <dd>{formatBytes(snapshot.process.rss_bytes)}</dd>
              </div>
              <div>
                <dt>文件描述符</dt>
                <dd>{snapshot.process.fd_count ?? "—"}</dd>
              </div>
            </dl>
          </Card>
          <Card title="容器负载" note={dockerStatusNote(snapshot.docker)}>
            <dl className="monitor-facts">
              <div>
                <dt>托管容器</dt>
                <dd>{snapshot.docker.managed_containers}</dd>
              </div>
              <div>
                <dt>CPU</dt>
                <dd>{formatPercent(snapshot.docker.cpu_percent)}</dd>
              </div>
              <div>
                <dt>内存</dt>
                <dd>{formatBytes(snapshot.docker.memory_used_bytes)}</dd>
              </div>
              <div>
                <dt>进程数</dt>
                <dd>{snapshot.docker.pids ?? "—"}</dd>
              </div>
            </dl>
            {snapshot.docker.error && (
              <p className="monitor-inline-error">{snapshot.docker.error}</p>
            )}
          </Card>
        </div>
      )}

      <Card
        title="最近一小时"
        note={`约每 ${data?.sample_interval_seconds ?? 5} 秒采样一次。`}
      >
        <div className="monitor-chart-grid">
          <Sparkline
            history={history}
            valueKey="host_cpu_percent"
            label="主机 CPU"
          />
          <Sparkline
            history={history}
            valueKey="host_memory_percent"
            label="主机内存"
          />
          <Sparkline
            history={history}
            valueKey="docker_cpu_percent"
            label="容器 CPU"
          />
          <Sparkline
            history={history}
            valueKey="docker_memory_used_bytes"
            label="容器内存"
            format={formatBytes}
          />
        </div>
      </Card>

      <Card title="组件健康" note="只读检查，不会触发修复操作。">
        <div className="admin-table-wrap">
          <table>
            <thead>
              <tr>
                <th>组件</th>
                <th>状态</th>
                <th>说明</th>
                <th>检查时间</th>
              </tr>
            </thead>
            <tbody>
              {components.map((component) => (
                <tr key={component.id}>
                  <td>
                    <strong>{component.name}</strong>
                    <small>{component.id}</small>
                  </td>
                  <td>
                    <StatusPill status={component.status} />
                  </td>
                  <td>
                    {component.message}
                    <small>
                      {Object.entries(component.details || {})
                        .map(([key, value]) => `${key}: ${String(value)}`)
                        .join(" · ")}
                    </small>
                  </td>
                  <td>{formatTime(component.checked_at)}</td>
                </tr>
              ))}
              {!components.length && (
                <tr>
                  <td colSpan="4" className="empty-cell">
                    正在检查组件
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="monitor-bottom-grid">
        <Card title="当前任务" note="后台运行表示当前没有浏览器订阅。">
          <div className="admin-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Session / Turn</th>
                  <th>状态</th>
                  <th>订阅</th>
                  <th>运行方式</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={`${task.session_id}:${task.turn_id}`}>
                    <td>
                      <strong>{task.session_id}</strong>
                      <small>
                        {task.turn_id} · seq {task.last_sequence}
                      </small>
                    </td>
                    <td>{task.state}</td>
                    <td>{task.subscribers}</td>
                    <td>
                      {task.background ? (
                        <span className="background-badge">后台运行</span>
                      ) : (
                        "前台"
                      )}
                    </td>
                  </tr>
                ))}
                {!tasks.length && (
                  <tr>
                    <td colSpan="4" className="empty-cell">
                      当前没有运行任务
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="关键异常" note="仅展示当前需要关注的问题。">
          <div className="monitor-issues">
            {issues.map((issue) => (
              <article
                key={`${issue.code}:${issue.session_id || "global"}:${issue.turn_id || ""}`}
                className={`issue-${issue.severity}`}
              >
                <div>
                  <strong>{issue.title}</strong>
                  <StatusPill
                    status={issue.severity === "warning" ? "degraded" : "error"}
                  />
                </div>
                <p>{issue.message}</p>
                {(issue.session_id || issue.turn_id) && (
                  <small>
                    {issue.session_id || "—"} · {issue.turn_id || "—"}
                  </small>
                )}
              </article>
            ))}
            {!issues.length && (
              <p className="monitor-empty">当前没有关键异常</p>
            )}
          </div>
        </Card>
      </div>
    </section>
  );
}

function AdminApp() {
  const [activeView, setActiveView] = useState(viewFromHash);
  const [monitor, setMonitor] = useState(null);
  const [monitorLoading, setMonitorLoading] = useState(false);
  const [monitorError, setMonitorError] = useState("");
  const [monitorAutoRefresh, setMonitorAutoRefresh] = useState(true);
  const monitorRequestActive = useRef(false);
  const [overview, setOverview] = useState(null);
  const [users, setUsers] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [settings, setSettings] = useState(null);
  const [draft, setDraft] = useState({});
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [overviewData, userData, sessionData, settingData] =
        await Promise.all([
          api("/v1/admin/overview"),
          api("/v1/admin/users"),
          api("/v1/admin/sessions"),
          api("/v1/admin/settings"),
        ]);
      setOverview(overviewData);
      setUsers(userData.users || []);
      setSessions(sessionData.sessions || []);
      setSettings(settingData);
      setDraft(settingsDraft(settingData.saved));
    } catch (failure) {
      setError(failure.message);
    }
  }, []);

  const loadMonitor = useCallback(async () => {
    if (monitorRequestActive.current) return;
    monitorRequestActive.current = true;
    setMonitorLoading(true);
    setMonitorError("");
    try {
      setMonitor(await api("/v1/admin/monitor"));
    } catch (failure) {
      setMonitorError(failure.message);
    } finally {
      monitorRequestActive.current = false;
      setMonitorLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (activeView === "monitor") loadMonitor();
  }, [activeView, loadMonitor]);

  useEffect(() => {
    if (activeView !== "monitor" || !monitorAutoRefresh) return undefined;
    let timer;
    const startPolling = () => {
      window.clearInterval(timer);
      if (document.visibilityState === "visible") {
        timer = window.setInterval(loadMonitor, 5000);
      }
    };
    const handleVisibility = () => {
      if (document.visibilityState === "visible") loadMonitor();
      startPolling();
    };

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [activeView, loadMonitor, monitorAutoRefresh]);

  useEffect(() => {
    const syncView = () => {
      const nextView = viewFromHash();
      if (window.location.hash !== `#${nextView}`) {
        window.history.replaceState(null, "", `#${nextView}`);
      }
      setActiveView(nextView);
    };

    syncView();
    window.addEventListener("hashchange", syncView);
    return () => window.removeEventListener("hashchange", syncView);
  }, []);

  const createUser = async (event) => {
    event.preventDefault();
    if (!newName.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api("/v1/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName.trim() }),
      });
      setNewName("");
      setMessage("用户已创建");
      await load();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleUser = async (user) => {
    setBusy(true);
    setError("");
    try {
      await api(`/v1/admin/users/${encodeURIComponent(user.user_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !user.enabled }),
      });
      setMessage(user.enabled ? "用户已停用" : "用户已启用");
      await load();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  };

  const saveSettings = async (event) => {
    event.preventDefault();
    if (!settings) return;
    setBusy(true);
    setError("");
    try {
      const next = await api("/v1/admin/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: settings.version,
          ...settingsPayload(draft),
        }),
      });
      setSettings(next);
      setDraft(settingsDraft(next.saved));
      setMessage("配置已保存；请重启服务使其生效");
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <a className="admin-brand" href="/">
          <span>{"{}"}</span>
          <div>
            <strong>WebAgent</strong>
            <small>管理后台</small>
          </div>
        </a>
        <nav aria-label="后台分区">
          {[
            ["overview", "概览"],
            ["monitor", "监控"],
            ["users", "用户"],
            ["sessions", "Sessions"],
            ["settings", "服务设置"],
          ].map(([view, label]) => (
            <a
              key={view}
              className={activeView === view ? "active" : undefined}
              href={`#${view}`}
              aria-current={activeView === view ? "page" : undefined}
            >
              {label}
            </a>
          ))}
        </nav>
        <p>Provider 凭据由每位用户在浏览器中单独配置，不在后台管理。</p>
      </aside>
      <main className="admin-main">
        <header className="admin-topbar">
          <div>
            <p>SERVER CONTROL</p>
            <h1>管理后台</h1>
          </div>
          <button
            type="button"
            onClick={activeView === "monitor" ? loadMonitor : load}
            disabled={busy || (activeView === "monitor" && monitorLoading)}
          >
            刷新数据
          </button>
        </header>
        {(error || message) && (
          <div
            className={`admin-notice ${error ? "error" : "success"}`}
            role="status"
          >
            {error || message}
          </div>
        )}
        {activeView === "overview" && (
          <div id="overview" className="metric-grid">
            <div>
              <span>用户</span>
              <strong>{overview?.users?.total ?? "—"}</strong>
              <small>{overview?.users?.enabled ?? 0} 位启用</small>
            </div>
            <div>
              <span>Sessions</span>
              <strong>{overview?.sessions?.total ?? "—"}</strong>
              <small>
                {Object.entries(overview?.sessions?.states || {})
                  .map(([state, count]) => `${state} ${count}`)
                  .join(" · ") || "暂无"}
              </small>
            </div>
            <div>
              <span>运行任务</span>
              <strong>{overview?.running_tasks ?? "—"}</strong>
              <small>当前活跃</small>
            </div>
            <div>
              <span>运行环境</span>
              <strong className="metric-text">
                {overview?.runtime || "—"}
              </strong>
              <small>{overview?.sandbox || "—"} sandbox</small>
            </div>
          </div>
        )}
        {activeView === "monitor" && (
          <MonitorView
            data={monitor}
            loading={monitorLoading}
            error={monitorError}
            autoRefresh={monitorAutoRefresh}
            onAutoRefresh={setMonitorAutoRefresh}
            onRefresh={loadMonitor}
          />
        )}
        {activeView === "users" && (
          <Card id="users" title="用户" note="只有启用的姓名可以通过首页验证。">
            <form className="inline-form" onSubmit={createUser}>
              <input
                aria-label="新用户姓名"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder="输入姓名"
                maxLength="80"
              />
              <button type="submit" disabled={busy || !newName.trim()}>
                新增用户
              </button>
            </form>
            <div className="admin-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>姓名</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.user_id}>
                      <td>
                        <strong>{user.name}</strong>
                        <small>{user.user_id}</small>
                      </td>
                      <td>
                        <span
                          className={`status-pill ${user.enabled ? "enabled" : "disabled"}`}
                        >
                          {user.enabled ? "已启用" : "已停用"}
                        </span>
                      </td>
                      <td>{formatTime(user.created_at)}</td>
                      <td>
                        <button
                          className="table-action"
                          type="button"
                          disabled={busy}
                          onClick={() => toggleUser(user)}
                        >
                          {user.enabled ? "停用" : "启用"}
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!users.length && (
                    <tr>
                      <td colSpan="4" className="empty-cell">
                        暂无用户
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        )}
        {activeView === "sessions" && (
          <Card
            id="sessions"
            title="Sessions"
            note="跨用户只读查看，不包含 Provider 凭据。"
          >
            <div className="admin-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>标题</th>
                    <th>用户</th>
                    <th>状态</th>
                    <th>任务</th>
                    <th>最后活动</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => (
                    <tr key={session.session_id}>
                      <td>
                        <strong>{session.title || "新会话"}</strong>
                        <small>{session.session_id}</small>
                      </td>
                      <td>{session.owner_name || "—"}</td>
                      <td>{session.state}</td>
                      <td>{session.task_state || "idle"}</td>
                      <td>
                        {formatTime(
                          session.last_activity_at || session.updated_at,
                        )}
                      </td>
                    </tr>
                  ))}
                  {!sessions.length && (
                    <tr>
                      <td colSpan="5" className="empty-cell">
                        暂无 Session
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        )}
        {activeView === "settings" && (
          <Card
            id="settings"
            title="服务设置"
            note="保存值不会热更新正在运行的服务。"
          >
            {settings?.restart_required && (
              <div className="restart-banner">
                已保存配置与当前运行配置不同，等待重启生效。
              </div>
            )}
            <form className="settings-form" onSubmit={saveSettings}>
              {Object.entries(LABELS).map(([key, label]) => (
                <label key={key}>
                  <span>
                    {label}
                    <small>
                      当前生效：
                      {activeSettingValue(key, settings?.active?.[key])}
                    </small>
                  </span>
                  {typeof draft[key] === "boolean" ? (
                    <input
                      type="checkbox"
                      checked={draft[key]}
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          [key]: event.target.checked,
                        }))
                      }
                    />
                  ) : (
                    <input
                      value={draft[key] ?? ""}
                      type={typeof draft[key] === "number" ? "number" : "text"}
                      min={
                        BYTE_LIMIT_KEYS.has(key) ||
                        key === "file_upload_max_files_per_session"
                          ? "1"
                          : undefined
                      }
                      step={
                        key === "docker_cpus"
                          ? "0.1"
                          : BYTE_LIMIT_KEYS.has(key)
                            ? "1"
                            : undefined
                      }
                      onChange={(event) =>
                        setDraft((current) => ({
                          ...current,
                          [key]:
                            typeof current[key] === "number"
                              ? Number(event.target.value)
                              : event.target.value,
                        }))
                      }
                    />
                  )}
                </label>
              ))}
              <div className="settings-submit">
                <div>
                  <strong>重启后生效</strong>
                  <p>保存后请由运维人员重启 WebAgent 服务。</p>
                </div>
                <button type="submit" disabled={busy || !settings}>
                  保存设置
                </button>
              </div>
            </form>
          </Card>
        )}
      </main>
    </div>
  );
}

createRoot(document.getElementById("admin-root")).render(<AdminApp />);
