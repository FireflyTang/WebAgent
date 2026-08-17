# Changelog

本文件从开发日志中蒸馏用户可见变化，不保留机器地址、Endpoint、PID、临时 Session、旧测试数字或调试流水。

## Unreleased

### Breaking changes

- 移除外部 OpenAI-compatible `/v1/chat/completions` 与 `/v1/models`、共享 Bearer API key、SSE/curl smoke，以及 Zhipu legacy runtime。
- 仓库现专注浏览器 WebAgent 路线；浏览器 Provider 目录 `/v1/web/models` 保留，Provider 配置仅随每个 Web turn 提交。

### Added

- Session 列表右键/键盘上下文菜单重命名；运行中可改名，手动标题在持久层稳定覆盖自动标题。
- 公开仓库 owner-gated `@codex` Issue/PR/Review 协作：仓库专属 runner、公开 allowlist、持久 Session、测试后 PR/commit 发布和失败回告。
- `/admin` 基础后台：概览、用户预建与启停、全量 Session 查看和 SQLite 托管的重启生效配置。
- `/admin` 轻量运行监控：最近一小时内存负载、组件健康、当前/后台任务，以及 Journal、Reaper、生命周期和 Docker 一致性异常。
- 姓名验证入口、登出、Session 用户归属，以及按用户隔离的浏览器 Provider/默认模型/effort 配置。
- 沙箱 UTF-8 文本查看与编辑、常见语言语法高亮、revision 冲突确认，以及后台可配置的大文件阈值。
- 文本编辑器行号、长文件独立滚动、固定操作区，以及 Session 累计上传数量、单文件大小限制。

### Changed

- Agent 任务运行期间允许刷新文件树和下载文件；上传继续使用 Session 独占锁。
- 文件下载在校验时即安全打开目标 inode，并从同一文件描述符流式返回，避免并发替换或删除造成越界读取与响应竞态。
- 已保存 Provider 增加 15 秒真实双链路心跳；全局连接状态与 Session WebSocket 解耦，网络恢复后自动恢复徽标与会话订阅。
- Session 日志改为可刷新的大尺寸执行转录：默认展示用户输入、Assistant 输出、工具名、Bash 命令和工具结果，完整参数、SDK 高频事件与原始 JSON 按需展开。
- 诊断链路保留真实 ToolResult 的错误状态和最终 result，并对常见 Bearer/Basic、API key、token 与 Cookie 字符串做内容级遮罩。

### Known boundaries

- 姓名验证不是认证；当前没有注册、密码、角色或权限，后台接口完全开放，只适合可信环境。

## 0.3.0 — 2026-08-11

### Added

- WebAgent 品牌与 `@Username` 占位，英文默认/中文互链 README 和宣传截图。
- React 三栏 Session 工作台、窄屏文件抽屉、低高度固定输入区、可记忆的边栏布局。
- 真实 Session 目录、Transcript、模型/effort 持久化、自动标题与生命周期操作。
- 客户端 Provider 测试、动态模型 ID、默认 model/effort 与 Bearer/`x-api-key`。
- 应用级 active turn、断线后台执行、重连历史/实时无缝衔接、多 Session 并行与真实 stop。
- SQLite UI Event Journal 和动态 HTML diagnostic log。
- 文件/目录上传、树形浏览与统一双击下载。
- Markdown/GFM Agent 回复、结构化步骤/活动、独立耗时、usage 与失败终态。
- Docker worker 内 Claude Agent SDK adapter、EOF failure 与孤儿命令清理合同。

### Changed

- 项目统一命名 WebAgent，版本统一为 0.3.0，并明确 unofficial/local-demo 定位。
- WebSocket 从任务所有者改为可断开的 Session 订阅；应用 shutdown 成为运行任务的最终边界。
- 真实 runtime 从 CLI 主路径迁移到 SDK；CLI 保留为显式 fallback。
- Provider 配置由 Web turn 作为客户端上下文提交。
- 诊断事件由独立 HTML 文件迁移到 SQLite，读取时动态生成页面。
- 默认监听收紧为 `127.0.0.1`。

### Known boundaries

- 服务进程重启仍会中止运行中的 turn；完成历史和 workspace 可恢复，执行状态不可恢复。
- 无网页用户鉴权/多租户隔离；Docker 不是敌对代码的生产级安全边界。
- 模型目录仅展示 ID；文件区不含编辑/Diff/Git；诊断日志可能包含敏感 raw 命令与输出。
