# v0.3.0 测试报告

> 当前发布矩阵；历史阶段性数字、真实 Endpoint、机器地址、进程信息和提交哈希已移除。

## 结果

| 门禁 | 当前结果 | 覆盖重点 |
|---|---:|---|
| Python 全套 | **193 passed, 1 skipped, 1 warning** | unit、E2E、browser、Docker integration |
| Browser 专项 | **34 passed, 1 skipped** | Chromium 全链、窄屏设置入口与 WebKit smoke |
| 前端 protocol | **3 passed** | replay/live reducer 与事件隔离 |
| Docker integration | **5 passed** | worker 镜像、跨 UID workspace、执行、隔离与清理合同 |
| npm audit | **0 vulnerabilities** | 当前 lockfile 依赖审计 |
| Fake/local curl smoke | **passed** | OpenAI-compatible SSE、两轮 Session、文件与生命周期 |

唯一 skip 是当前 Linux 测试环境缺少 Playwright WebKit 所需宿主动态库。Chromium 测试通过；该 skip 不等价于真实 macOS Safari 已验证。

## 从新机器运行

先安装锁定的开发依赖。基础 Python 套件不要求浏览器引擎或 worker 镜像：

```bash
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv lock --check
uv run pytest -q -m "not integration" --ignore=tests/browser
```

前端检查与 browser 专项分别运行；安装 browser 前显式安装 Chromium、WebKit 及其系统依赖：

```bash
cd frontend
npm ci
npm run check
npm audit

cd ..
uv run playwright install --with-deps chromium webkit
uv run pytest -q tests/browser
```

Fake/local curl smoke 不要求 Docker，但客户端脚本不会替你启动或配置服务。先在终端 1 启动明确使用 Fake runtime 和 Local sandbox 的隔离服务：

```bash
RUNTIME_BACKEND=fake SANDBOX_BACKEND=local \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

保持服务运行，再在终端 2 执行客户端 smoke：

```bash
./scripts/smoke_curl.sh
```

代码和文档格式检查可独立执行：

```bash
git diff --check
```

Docker integration 要求先构建 worker，并由有 Docker 权限的用户运行：

```bash
docker build -t webagent-worker:latest -f Dockerfile.worker .
uv run pytest -q -m integration tests/integration
```

## CI 策略

每次 push 和 Pull Request 都运行：

- Python lock、format、lint 和非 Docker/非 browser 测试；
- 前端 dependency audit、format、protocol tests、production build 与 committed bundle 检查；
- wheel build、干净环境安装及 bundled web assets 验证；
- 安装 Chromium/WebKit 后运行 browser suite。

Docker integration 会构建 worker 镜像，只在手工 `workflow_dispatch` 和已发布 Release 事件中运行，避免普通 push/PR 隐含依赖 Docker release 环境。

## 关键验收范围

- Session create/list/get/update/pause/resume/delete、墓碑、workspace 与 Transcript 连续性。
- OpenAI-compatible blocking/SSE、错误收尾和 Fake 两轮文件任务。
- Web Provider 的 Test → model/effort → Save、Bearer/`x-api-key`、错误恢复与动态 ID 目录。
- 应用持有的 turn：断开/关闭页面后继续、重连 replay/live 无遗漏无重复、真实 stop、服务 shutdown 取消。
- 多 Session 并行和事件/stop 隔离；同一 Session busy 合同。
- completed/failed/stopped history、Journal FIFO、SQLite 瞬时失败与 invariant 冲突隔离。
- Docker worker non-root、资源/网络配置、命令终止、orphan cleanup 与 workspace 隔离。
- 文件/目录上传、树形展示与统一双击下载。
- 桌面三栏、边栏拖拽/折叠、窄屏抽屉、低高度 composer、键盘 listbox 和无 page error。
- Diagnostic log 与 Transcript 分层；Provider 配置/Key 不进入 Session metadata、Transcript 或 diagnostic SQLite，目录失败 warning 只含脱敏 Endpoint、auth mode 和 Key 短 hash fingerprint；raw 任务命令/输出仍保留完整排障语义。

## 未由该矩阵证明

- 生产级安全、多租户隔离、恶意代码沙箱与公网部署。
- 多 Uvicorn worker、多实例调度或服务进程重启后继续运行中的任务。
- 任意 Provider/模型都支持所选 effort，或跨模型 resume 一致。
- 真实 macOS Safari 和大文件/大仓库规模。
