# 限制与路线图

WebAgent v0.3.0 是可信本地环境中的轻量共享 Demo。本页同时记录当前已知限制与可能的产品化方向，避免把未来能力写成已完成特性。

## 当前限制

### 安全与部署

- 用户由后台预建，姓名匹配只用于 Session 分流；没有密码、Token、注册、角色、鉴权或授权，后台接口也不设防。
- Docker worker 设置了非 root 用户与资源限制，但没有生产级 seccomp/AppArmor、只读根文件系统、网络策略、镜像供应链验证或敌对代码隔离承诺。
- Local sandbox 只分开目录，不隔离宿主机权限。
- 仅支持单 Uvicorn worker。Session 锁、active turn、订阅和 Journal pending suffix 都是进程内状态。
- Provider 配置与 Key 不写入 Session metadata、Transcript 或 diagnostic SQLite；Provider 目录失败的 server warning 会记录脱敏 Endpoint、auth mode 和 Key 短 hash fingerprint。常见 Bearer/Basic、API key、token 和 Cookie 文本会被遮罩，但 raw 诊断仍可能包含其他敏感命令、输出或文件内容；所有日志与 workspace 必须视为敏感。

### 任务与恢复

- 同一 Session 同时只能运行一个 turn，没有排队、优先级或待执行任务取消。
- 浏览器刷新、关闭或短暂断线不取消应用持有的任务；重连可回放并继续实时订阅。
- **服务进程退出或重启会取消运行中的任务。** 当前没有持久 TaskRun、执行租约、进程外 worker 调度或 runtime snapshot。
- Journal 的内存 pending suffix 在进程崩溃时可能丢失；已写入 SQLite 的历史仍可回放。
- 多进程、多实例、跨设备接管与跨节点 stop 尚未实现。

### Session、文件与日志

- Session 没有搜索、分页、收藏、归档、手工改名、复制或已删除恢复。
- 自动标题只是前端截取第一条用户消息的前 36 个字符。
- 文件上传面向小型源码；默认单文件上限 2 MiB、每个 Session 累计 10 个用户上传文件，均可在后台修改并于重启后生效。当前无断点续传或内容级去重。
- 文件树支持上传、浏览、下载和有界 UTF-8 文本编辑；二进制及超限文件不进编辑器。当前不含删除、重命名、Diff、Git 导入、快照或回退。
- 诊断日志会生成完整 HTML 执行转录，常见凭据形式会遮罩，但它仍不是通用 DLP。当前没有分页、筛选、导出、保留期限或细粒度审计策略。

### Runtime、Provider 与协议

- Claude Agent SDK 仍是固定的 0.x 依赖；项目用 worker 内 NDJSON adapter 隔离上层协议，但升级仍需完整回归。
- 模型动态目录只保证 ID。能力、上下文、价格、描述、推荐和 effort 支持没有统一可信协议。
- Provider 的模型目录和能力表达并不统一；WebAgent 不猜测或补齐缺失信息。
- 旧 CLI runtime 创建的 Session 不自动迁移为 SDK Session；需新建 Session 或设计显式迁移。

### 浏览器与体验

- 自动化以 Chromium 为主要真实浏览器；当前 Linux 环境的 WebKit 依赖缺失导致一项跳过，不能等价证明真实 macOS Safari。
- 后台只提供基础运行配置，保存后需重启生效；没有受控重启、配置审计、项目级审阅流程或逐工具 approval。
- 页面不会展示模型 thinking，也不会从回答正文推断隐藏计划或测试状态。

## Roadmap

### 近期：把 Demo 变成可靠的共享工具

- 持久 TaskRun 投影、运行所有权与心跳，支持服务重启后接管或清晰重试。
- Session 搜索、分页、归档、改名与任务队列。
- Git clone/import、Diff、变更审阅、workspace 快照与回退。
- 文件大小/Session 配额、流式上传下载；日志分页、筛选、导出与保留策略。
- 真实 Safari 手工验收和可访问性审计。

### 中期：可信共享

- 密码或外部身份源、注册/邀请、团队、项目、角色与资源级授权。
- Provider Secret 服务端托管、轮换和最小权限，不再依赖浏览器 localStorage。
- 工具 approval、只读审阅、审计日志与敏感信息治理。
- 受控模型元数据和 Provider 路由策略。

### 长期：平台化执行

- 多 worker/多实例调度、数据库租约、分布式事件总线、故障转移与配额。
- 多 runtime、多 Agent 编排、任务树、重试策略和结果合并。
- 组织级成本、用量、合规、镜像与 sandbox 策略控制面。

## 不采用的“伪完成”

- 不把历史 TaskCard 重建描述成运行快照恢复。
- 不把 WebSocket 在线状态描述成任务是否执行的唯一事实。
- 不从模型名称猜测能力，也不把目录失败回退成另一个 Provider 的模型列表。
- 不把 Docker 默认配置称为强安全边界。
- 不把 Linux WebKit 或截图复核称为真实 Safari 验收。
