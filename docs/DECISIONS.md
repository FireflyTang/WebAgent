# 设计决策

状态使用 **Accepted**、**Superseded**。Superseded 条目保留原因与替代方案，避免旧文档继续指导实现。

## D-001 — 每 Session 一个持久 sandbox（Accepted）

真实模式默认每个 Session 一个非 root Docker worker 和独立绑定 workspace。生命周期操作同步到 worker；Local 后端仅作开发回退。理由是 Session 的对话连续性必须与文件连续性一致。

## D-002 — 保留确定性 FakeRuntime（Accepted）

FakeRuntime 不依赖网络、Provider 或模型波动，可验证 session、文件、测试与生命周期的精确纵向切片。它不是“假的 UI”；除模型推理外仍走真实应用路径。

## D-004 — Provider 与执行器分离（Accepted）

Claude Agent SDK/CLI、Docker、Session 和事件协议是固定层；Anthropic-compatible Endpoint、Key、认证方式、模型 ID 与 effort 是可替换上下文。Web UI 必须先测试客户端 Provider，再选择默认 model/effort 并保存。

模型目录只展示动态 ID，不硬编码描述、能力、价格或推荐。

## D-005 — WebSocket 传控制与事件，HTTP 传资源（Accepted）

WebSocket 用于 message、stop、progress、delta 和 terminal 的低延迟双向流。REST/multipart 用于 Session、history、文件、诊断日志与模型目录，避免把文件编码进消息流。

## D-006 — 默认允许网络访问（Superseded）

早期 Demo 为现场访问默认监听所有接口。v0.3.0 的应用 Settings 默认 `HOST=127.0.0.1`；Uvicorn CLI 不会自动把该值解释为自己的 bind 参数，启动命令仍显式传 `--host 127.0.0.1`。如需网络访问，使用者应明确改变监听并在前方配置认证代理。Web 路由没有用户鉴权。

## D-007 — 日志写 SQLite、读取时生成 HTML（Accepted）

诊断事件按序写入主数据库，不为每个 Session 维护 HTML 文件；请求时动态渲染当前内容。Transcript 和 UI events 分别承担聊天恢复与任务卡恢复。

应用不把 Provider 配置或 Key 复制到 Session metadata、Transcript 或 diagnostic SQLite。Provider 目录失败的 server warning 会记录脱敏 Endpoint、auth mode 和 Key 短 hash fingerprint。raw runtime/tool 诊断会保留完整命令与输出；若 Agent 或 Bash 自行打印 Secret，它会出现在日志中，因此日志必须作为敏感数据处理。

## D-008 — Agent SDK 是默认真实 runtime（Accepted）

SDK 在 Docker worker 内执行，经项目自有 NDJSON adapter 映射为稳定事件。CLI 仅是显式 fallback，不把 SDK 对象或非稳定内部字段直接暴露给 WebSocket/API。

## D-009 — Transcript、UI events、诊断日志分层（Accepted）

Transcript 只存用户/Agent 可见消息；UI events 存任务界面协议；诊断日志用于完整排障。三者的隐私、保留和展示需求不同，不互相伪装。

## D-010 — turn 由 WebSocket 持有（Superseded）

旧实现将 producer 生命周期绑到 socket，断线即取消。v0.3.0 改为应用级 `ActiveTurnRegistry` 持有 producer 与 service stream；WebSocket 只是可断开订阅。关闭页面后任务继续，重连可回放与接实时流。

## D-011 — 每活动 Session 一个 WebSocket（Accepted）

当前选中 Session 建立连接；运行中的后台 Session 可以保留连接以获得即时反馈，断开也不影响应用持有的任务。按 Session 分连接简化 turn、sequence、错误和 stop 隔离；当前演示并发量不值得引入单 socket 多路订阅协议。

## D-012 — UI Event Journal 负责有序回放，不负责执行恢复（Accepted）

dispatch 时分配 sequence，先 accept 再 publish；单 writer 把 FIFO 事件写入 SQLite。重连先订阅再合并 durable prefix、pending suffix 与 buffered live events。completed/stopped/failed 只说明事件历史完整，不代表 runtime 可跨服务进程重启恢复。

## D-013 — 外部生命周期副作用采用幂等补偿（Accepted）

pause 先落持久状态，sandbox 副作用失败保留 pending 并由 reaper 重试；delete 即使 inspect 缺失也尝试幂等删除。不可逆意图必须早于外部清理。

## D-014 — EOF 没有 Result 是失败（Accepted）

SDK runner 正常 EOF 若缺少 Result，仍按协议失败处理并关闭未完成工具/任务状态，不能伪造 completed。

## D-015 — 自动标题保持简单透明（Accepted）

Session 首条消息提交后，由前端截取首句前 36 个字符作为标题。当前不调用模型总结，也不宣称语义标题；未来若改为后端或模型生成，需要单独的失败和隐私合同。
