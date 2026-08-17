# WebAgent 产品简报

> v0.3.0 · 非官方、本地优先 Coding Agent Demo · Current / Target 产品口径

## 产品定位

WebAgent 是“通过对话驱动持久代码沙箱”的轻量共享工作台。每个 Session 绑定用户、Transcript、Agent 上下文、模型/effort 选择、诊断历史和独立工作区。用户可以从空目录开始，也可以上传小型项目，再让 Agent 修改文件、运行命令和测试。

它不是普通聊天页，也不是完整在线 IDE。当前重点是让第一次接触 Coding Agent 的用户，能够看懂“任务正在做什么”、拿到文件结果，并在多个独立任务之间自然切换。

## Current State

当前 WebAgent 提供：

- 管理员在 `/admin` 预建、启停用户；用户输入预建姓名进入并可登出。姓名验证只做分流，不是安全认证。
- Session 从创建起绑定用户，工作台按当前用户加载；后台可以查看所有用户、Session、运行任务概览和可管理配置。
- 桌面三栏工作台，以及窄屏 Session 操作轨和文件抽屉；输入框在低高度视口中仍可见。
- 真实 Session 列表、日期分组、自动标题、Transcript、生命周期与持久工作区。自动标题仍由前端截取首条用户消息的前 36 个字符，不是模型总结。
- Provider 设置流程：`测试连接 → 选择默认模型/effort → 保存设置`。动态目录只展示 Provider 返回的模型 ID，不补写描述或推荐。
- 应用持有的后台 turn。不同 Session 可并行；关闭页面不取消任务，重连会回放历史并继续实时订阅；同一 Session 同时只运行一个任务。
- 真实 Stop、结构化步骤与活动、任务/步骤耗时、终态和 usage 展示，不暴露模型思维链。
- 上传文件/目录、树形浏览、二进制下载，以及带常见语言高亮的 UTF-8 文本查看/编辑；Agent 运行时只读，保存使用 revision 冲突提示。当前没有 Diff。
- SQLite Transcript、UI event history 和详细诊断日志。
- FakeRuntime 的确定性演示，和 Docker worker 内的真实 Agent SDK 路径。

## 核心用户流程

### 第一次使用 Web UI

1. 维护者先设置 `RUNTIME_BACKEND=claude`、`SANDBOX_BACKEND=docker` 和可访问 Provider 的 `DOCKER_NETWORK_MODE`，然后重启服务。默认 Fake runtime 即使收到浏览器 Provider 配置也仍是 Fake。
2. 管理员打开 `/admin` 创建用户，用户再打开 WebAgent 并输入自己的预建姓名。
3. 在连接设置填写 Anthropic-compatible Endpoint、Provider API Key 和认证方式。
4. 点击“测试连接”，确认动态模型目录。
5. 选择默认模型与 effort，保存设置。
6. 新建 Session，上传项目或从空工作区开始，发送第一个开发任务。

连接字段修改后必须重新测试。Provider Key 保存在浏览器中，并按 turn 提交；服务端不把 Provider 配置或 Key 复制到 Session metadata、Transcript 或 diagnostic SQLite。Provider 目录失败的 server warning 会记录脱敏 Endpoint、认证方式与 Key 短 hash fingerprint；Agent/Bash 若自行打印 Secret，raw 诊断仍会原样保存。

### 执行与切换

用户发送任务后，页面展示由运行时事实驱动的进度卡。切到另一个 Session 不影响原任务，也可在新 Session 启动任务。关闭或刷新浏览器只断开订阅，任务继续由服务进程持有；重连后回放已经发生的事件并接入实时流。Stop 会取消真实运行时和 worker 命令。

服务进程重启是明确边界：正在运行的 turn 会中止，不能跨进程恢复；已落库历史、Session 和工作区仍保留。

## 产品概念

| 概念 | 当前语义 |
|---|---|
| Session | Transcript、沙箱、Agent 上下文、模型/effort、历史和日志的共同载体 |
| Turn | 一次用户任务；归应用所有，不归某条 WebSocket 所有 |
| WebSocket | Session 事件的可断开订阅和 stop 控制通道 |
| Workspace | Session 独立的持久目录；真实模式映射到非 root Docker worker 的 `/workspace` |
| Transcript | 用户和 Agent 的高层可见消息，用于恢复聊天区 |
| UI events | 带 session/turn/sequence/time 的进度与终态历史，用于重建任务界面 |
| 诊断日志 | 面向排障的详细事件，可能包含完整命令与输出，必须按敏感数据管理 |
| Provider 配置 | 浏览器侧 Endpoint、Key 和认证方式；是 turn 上下文，不是 WebAgent 登录凭据 |
| 用户 | 后台预建的姓名与稳定 ID；用于 Session 归属和浏览器配置分流，不是认证主体 |
| 后台设置 | SQLite 中保存的下一次启动配置；页面同时展示当前运行值和待生效值 |

## 当前边界

- 面向可信本地小范围用户；姓名匹配和 Session owner 不提供账号鉴权，后台也没有权限保护。
- Docker 是 Demo 级防误操作边界，不是为敌对代码设计的强沙箱。
- 同一 Session 没有队列；服务重启后不能继续运行中的任务。
- 文件能力不含 Git 导入、Diff、回退、快照和大文件分块；超出后台阈值的文件只提示下载。
- 日志一次性渲染完整 Session 诊断，没有筛选、分页和保留策略。
- 模型可用性与 effort 支持由 Provider 决定；产品只可信地展示模型 ID。

## Target State

目标是把 Demo 演进为“以工作区和任务为中心的 Coding Agent 协作台”：

1. **单人长期使用**：Session 搜索/归档/改名，持久 TaskRun、队列、重试、跨服务重启恢复，Git 导入、Diff、快照与回退。
2. **安全共享**：账号、项目、角色、Session/Workspace 权限、凭据托管、审批与审计。
3. **规模化执行**：多进程/多实例所有权、租约与心跳，资源配额，日志分页/保留，多 Provider 和多 runtime 策略。
4. **产品化体验**：受控模型元数据、变更审阅、可访问性与 Safari 验收，以及明确的失败恢复操作。

详细拆分见 [限制与路线图](docs/LIMITATIONS_AND_ROADMAP.md)，实现原则见 [架构](docs/ARCHITECTURE.md) 与 [决策记录](docs/DECISIONS.md)。
