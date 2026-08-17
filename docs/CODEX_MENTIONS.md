# GitHub `@codex` 协作

WebAgent 的公开仓库接入了一个仓库专属的 self-hosted runner。它只响应新创建内容中的精确小写 `@codex`，让受信任维护者可以在 Issue、Pull Request、Review 或行内评论里复用同一个本地 Codex Session。

## 谁可以调用

- 公开仓库默认只允许仓库 owner `FireflyTang`。
- 额外的受信任 GitHub 用户必须逐行加入默认分支的 [`.github/codex-public-allowlist.txt`](../.github/codex-public-allowlist.txt)。
- 未授权用户会收到拒绝评论；workflow 不会 checkout 目标代码，也不会启动 Codex。
- bot 发送者和非精确写法（例如 `@Codex`）不会触发执行。

这个模型假设每位获准调用者都可信，可以让 runner OS 用户执行仓库代码。`workspace-write` 不是主机读取隔离；它不适合开放给不受信任的公开用户。

## 如何使用

在以下任一新内容中写入 `@codex`，并把本次明确要求写在 mention 后面：

- 新 Issue 的标题或正文；
- 新 Pull Request 的标题或正文；
- Issue / Pull Request 的新评论；
- Pull Request review；
- Pull Request 行内 review comment。

示例：

```text
@codex 解释这个错误的根因，不要修改代码
@codex 修复这个问题并补回归测试
@codex 这次用 Terra，推理强度 high，检查这个 PR
```

编辑旧正文不会触发。如果漏写 mention，请新增一条评论。

## 执行与发布

- 问题、解释、讨论或 Review：只回复评论，不修改文件。
- Issue 中明确要求改代码：生成并验证修改，推送隔离分支，创建 Draft Pull Request。
- 同仓库 Pull Request 中明确要求改代码：验证后推送到该 PR 的 head branch。
- fork Pull Request：可以分析和评论，但不会尝试向外部 fork 推送。
- 任何 Codex、测试或发布失败：不会发布部分修改，评论会附上 Actions 日志。
- workflow 从本机 Codex 配置继承默认 model / effort；只有当前 mention 明确指定时才临时覆盖。
- 所有生成修改都需要人工 Review，不自动合并或部署。

runner 使用仓库唯一标签 `codex-webagent`。持久 Session ID 位于：

```text
~/.codex/issue-runner-state/FireflyTang__WebAgent/session-id
```

runner 的实际 Codex home 通过 GitHub Actions repository variable `CODEX_HOME` 注入，不硬编码在公开 workflow 中。

目标 checkout / worktree 是一次性的；上面的 Session ID 才是跨 Issue / PR mention 延续上下文的状态。重大重构、连续误解或大约 10–20 个实质请求后，应在没有 job 运行时归档旧 Session ID，再开始新会话。

## 仓库内实现

- [`.github/workflows/codex-mention.yml`](../.github/workflows/codex-mention.yml)：触发、授权、checkout、验证、发布和回复。
- [`.github/scripts/codex_event.py`](../.github/scripts/codex_event.py)：解析事件、公开仓授权和上下文构造。
- [`.github/scripts/run-repo-session.sh`](../.github/scripts/run-repo-session.sh)：仓库级持久 Session 与 model / effort 选择。
- [`.github/ISSUE_TEMPLATE/request.yml`](../.github/ISSUE_TEMPLATE/request.yml)：普通请求与可选 mention 入口。

runner 专用限制只存在于 workflow 生成的 prompt 中；仓库没有为此新增 `AGENTS.md`，因此不会改变日常本地 Codex 的行为。
