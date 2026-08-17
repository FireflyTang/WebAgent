# 工程经验

## 任务所有权不要绑定传输连接

WebSocket 很适合低延迟订阅，却不是可靠的任务所有者。页面刷新、浏览器休眠和临时断网都是正常事件。把 producer 提升到应用级 registry 后，断线只取消订阅；Stop 和应用 shutdown 才真正改变执行生命周期。

“页面关闭后继续”也不等于“服务重启后恢复”。后者需要持久 TaskRun、租约、进程外执行器状态和故障语义，不能靠事件回放冒充。

## replay/live 交界比单纯持久化更难

先读取历史、再订阅会漏掉两者之间的新事件。可靠顺序是：先订阅，读取 SQLite prefix 和内存 suffix，发送读取期间的有限 buffered batch，发 ready，再进入 live。不可变的 `(session, turn, sequence)` key 让各层安全去重。

Sequence 必须在实际 dispatch 边界生成，而不是 producer 提前编号；否则 stop 与 burst output 竞争时会产生空洞或晚到事件。

## Stop 必须拥有真实取消路径

只改变按钮或 TaskCard 状态会让 worker 继续消耗资源。取消需要贯穿 registry、service stream、runtime adapter 和容器命令；producer 的 finally 保持唯一 stream-close owner，terminal 使用 first-wins gate。

## Fake runtime 是可靠演示基础设施

确定性 Fake 能在没有网络和模型额度时覆盖 Session、WebSocket、workspace、生命周期与错误合同。它适合精确 UI 测试，但不能替代真实 SDK、Docker、Provider 或浏览器验收。

## Provider 配置与应用鉴权是两件事

Provider Key 授权上游模型调用。它不应与 WebAgent 的身份或授权概念混用，也不应妨碍不同浏览器按 turn 使用不同 Provider。

连接设置应严格执行“测试目录 → 选默认 model/effort → 保存”，字段变化立即使验证失效。目录失败不应静默回退到服务端模型，否则设置看似成功、执行却去了另一个 Provider。

Provider 配置和 Key 不进入 Session metadata、Transcript 或 diagnostic SQLite，但目录失败的 server warning 会留下脱敏 Endpoint、auth mode 与 Key 短 hash fingerprint。raw tool/command 日志会忠实保存输出；如果 Agent 或脚本打印 Secret，仍会泄露。因此不要在文档中承诺“日志不含敏感信息”。

## 进度只呈现可验证事实

稳定 SDK 事件可以支持步骤、工具、子任务、重试、耗时、usage 与 terminal；普通回答文本不能可靠证明测试已运行或计划有几步。没有可信 total 就不展示“3/5”，不展示 thinking，不从模型名称猜测能力。

SDK 对象应在 worker adapter 边界转为项目自有 NDJSON。stdout 保持机器可读，stderr 留给诊断；正常 EOF 没有 Result 仍是失败。

## Session 连续性同时包含对话与文件

只恢复模型会话、不恢复 workspace，下一轮仍会失去代码状态。Session 必须共同绑定 Transcript、runtime identity、workspace 和生命周期。上传可能早于第一次模型调用，不能因此把新 Agent session 错判为 resumable。

不可逆删除应先持久化意图，再执行幂等外部副作用；失败由 reaper 补偿。停止 `docker exec` 客户端也不保证容器内子进程停止，必须验证 worker 内命令清理。

## 浏览器验收要测行为与几何

截图能说明视觉氛围，不能证明窄屏抽屉、低高度输入框、拖拽边栏、键盘 listbox、文件下载和断线重连。Playwright 应同时断言协议事实、交互结果、关键元素边界和无 page error。

文件上传适合 multipart HTTP，实时任务适合 WebSocket；两条传输通过 Session ID 汇合。下载由浏览器处理，但必须验证响应字节、类型、鉴权路径与弹窗行为。

## 发布验证应覆盖真正运行路径

Health 200 和静态资源 200 不能证明 Python 进程、worker 镜像和前端协议来自同一版本。发布门禁需要全套 Python、前端协议/构建、真实 Chromium、Docker integration、依赖审计和 diff 检查。

默认监听与访问地址要分别说明；本地发布应优先 `127.0.0.1`。Docker build、容器 runtime 和 system service 也不会自动继承交互 shell 的代理环境，网络问题应逐层验证。
