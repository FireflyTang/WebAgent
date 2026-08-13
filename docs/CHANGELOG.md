# Changelog

本文件从开发日志中蒸馏用户可见变化，不保留机器地址、Endpoint、PID、临时 Session、旧测试数字或调试流水。

## Unreleased

### Breaking changes

- 移除外部 OpenAI-compatible `/v1/chat/completions` 与 `/v1/models`、共享 Bearer API key、SSE/curl smoke，以及 Zhipu legacy runtime。
- 仓库现专注浏览器 WebAgent 路线；浏览器 Provider 目录 `/v1/web/models` 保留，Provider 配置仅随每个 Web turn 提交。

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
