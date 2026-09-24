# 测试与验收

测试分成确定性回归、生产 API 联网矩阵、浏览器验收、真实 Provider 探针和最终安装态容器五层。各层回答的
问题不同，不能用 mock、纸面交易或单元测试结果代替生产网络结论。

## 确定性回归

日常开发先运行不依赖交易平台网络的完整套件：

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q --ignore=tests/test_api_integration.py
```

2026-09-23 Laya evaluator、两类模型选择方式与故障回退接入后，确定性回归为
`733 passed, 252 subtests passed`，另有 2 条第三方弃用警告。其中覆盖：

- 八类插件发现、禁用不导入、启停/刷新/teardown、动态配置和示例 schema 一致性；
- 平台自有 runtime、立即首轮、可中断等待、失败退避和通用业务事件队列；
- 内置/用户发现与决策策略、策略进化、Provider 与 evaluator 的质量优先/强制顺序及故障回退；
- Codex、Claude、OpenRouter 的模型目录、结构化输出、代理、登录和凭据迁移；
- 两类过滤插件、资金请求、实盘网关与独立纸面交易状态；
- SQLite 决策/异常/盈亏台账、分页/筛选/删除、并发 HTTP、Passkey/ECDH、回环与公网访问隔离；
- 本地启动器、宿主机代理、回调转发、registry 拉取和云部署契约；
- 公共示例字段完整、无退役字段、默认值一致，旧轮询字段升级时忽略并安全清理，管理示例不启用不存在的插件；
- Laya 本地 WebGPU evaluator 的健康协议、单候选四道题上限、结构化输出和 CPU 拒绝；
- 测试树不允许 `skip`、`xfail` 或 `expectedFailure` 装饰器。

2026-09-24 扫描休眠唤醒修复后为 `762 passed, 261 subtests passed`，另有 2 条第三方弃用警告；
`tests/test_scan_schedule.py` 模拟等待期间宿主机休眠，检查恢复后按实际时间到点扫描、停止命令仍能打断等待。

2026-09-24 逐市场粗筛、盘口 WebSocket 与 Laya 串行测速接入后为 `781 passed, 261 subtests passed`，另有 2 条第三方弃用警告。新增测试覆盖持久队列、逐市场历史与定时复查、独立暂停、WebSocket 断线过期/增量以及启动与定时测速独占。服务端另运行 `node --test tests/laya_gpu_queue.test.mjs tests/webtorch_sync.test.mjs`，4 项通过；它验证断开的调用不会提前释放 GPU 队列。宿主 WebGPU 真实测速三样本中位约 330–343ms；容器新镜像能导入 WebSocket 与 Laya 代码并读取宿主 `/health`，但这不替代公网 WebSocket 长期稳定性观察。

完整 `pytest` 默认包含生产联网测试，因此在 Binance 受限网络中会保留真实失败，而不是显示全绿。需要只看
确定性回归时必须显式使用上面的 `--ignore`，不要给联网用例添加 skip。

## 生产 API 联网矩阵

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_api_integration.py
```

这些测试直接实例化 API 插件，不经过 runtime readiness，也不读取 `paper_trading`。它们向真实服务器发送：

- Binance：公开读取以及 quote、order、cancel、redeem、TRANSFER_IN、TRANSFER_OUT；
- Polymarket：公开读取以及 order、cancel、Relayer submit 的未认证拒绝探针。

写探针使用无效资源标识和极小金额，成功标准是请求确实到达服务器并收到预期拒绝；若本地配置检查、mock
或纸面传输提前返回，测试失败。权限或远端行为改变时必须人工复核输入，不能把意外接受当作通过。

2026-09-20 最新结果为 `2 passed, 2 failed, 9 subtests passed`：Polymarket 与真实未认证写拒绝探针通过；
当前宿主出口访问 Binance 公共 `/api/v3/time` 即收到地域限制 HTTP 451，因此 Binance 单平台和双平台用例
如实失败，不能把它记成代码通过。更早一次可访问出口的复跑曾暴露继承代理单次往返约 5.3 秒，使正确
时间戳超过默认 5 秒 `recvWindow`；签名读请求已显式携带 20 秒窗口，随后当时的四项矩阵全部通过。该历史
成功不覆盖本轮 451 结果，也没有为生产联网测试增加 skip、mock 或静默绕路。

## OpenRouter 严格 schema 验收

后端专项位于 `tests/test_model_catalog.py` 和 `tests/test_plugin_system.py`，验证：

- 模型目录请求带 `supported_parameters=structured_outputs`；Codex 与 Claude 选项都要求模型明确声明
  `tools` 和 `structured_outputs`，但不因缺少厂商专有 Web Search 参数隐藏模型；
- `OPENROUTER_MODEL` 为只选字段，不能从 UI 手填绕过目录；
- 三种 Provider 的 CLI/上游请求都携带用户选定的模型，OpenRouter 还会覆盖 CLI 试图传入的其他型号；
- 插件私有回环守卫保持 Codex Responses 与 Claude Messages 协议、工具调用/结果和 continuation 字段；
  模型原生支持时保留 Web Search/Fetch，缺失时转换成 OpenRouter server tools；Codex namespace tools
  在缺少原生支持时展平，JSON 与 SSE 返回中的调用名都会复原；Claude 非 Anthropic 路由会移除不受支持的
  默认 `output_config.effort`，但保留 structured format、thinking、context management 和普通工具；
- 推理请求强制 `provider.require_parameters=true`；
- OpenRouter-Codex 最终提示包含完整 schema；只兼容一个纯 JSON Markdown 围栏，随后仍严格校验类型、
  必填、枚举、范围和额外字段，散文或夹杂解释不会被提取成交易结果；
- 本机回环加入 `NO_PROXY`，桥到 OpenRouter 的远端请求仍经过该插件解析后的统一或独立代理；
- 真实本地 HTTP 代理收到远端目录/推理请求，证明透明守卫没有屏蔽原有网络代理层；
- 普通推理 Key 与可选 Management Key 分别查询 `/key` 用量和 `/credits` 账户余额，凭据不回显且查询沿用该插件代理；
- `openrouter_*` 文件各自拥有独立模型与代理配置，Key 默认复用主配置并可单独覆盖，不把这段逻辑放入通用框架。
- `AUTO/CODEX/CLAUDE` 选择、CLI 缺失判定、同一轮 session resume、下一轮新 session，以及给两种 CLI
  同时提供统一账本和双方私有历史路径；路径从双方 AUTH_DIRECTORY 解析，OpenRouter 子进程也使用所选
  CLI 的同一私有 HOME。
- Codex 非零退出会从 stderr 或 stdout JSON 失败事件中提取实际原因；模型容量过载归为 transient，和
  短时/周额度窗口分开处理。

evaluator 专项位于 `tests/test_typed_evaluator_pipeline.py`：验证 Jev 原生 OpenRouter Decisions 请求、其它
OpenRouter 模型的 `structured_outputs` 过滤与 `require_parameters`、自定义普通聊天模型原生优先的 strict
`state/questions → answers` JSON schema、拒绝/忽略 schema 时的强制函数回退、Choice/Score/Noul 完整解析、
散文拒绝、共享 Key 但不共享代理，
候选输入压缩、不会筛空的平方根质量抽样、0.90 起步且最低 0.80 的动态阈值，以及暂停游标跨批恢复。
该测试使用本机 HTTP fixture，不声称真实账号当前可用。

另使用已配置且未输出的 OpenRouter Key 做过合成状态探针：原生 `~typesafe/jev-latest` 的候选粗筛返回
合法 Choice/Score/Noul；当前配置的 `qwen/qwen3.7-max` 分别经 Codex Responses 与 Claude Messages CLI
路径返回合法 strict schema 对象。Claude 2.1 默认发送但该非 Anthropic 路由不接受的
`output_config.effort` 由私有守卫移除，其它 Messages 字段保留。探针没有市场或交易写入，也没有输出、
复制或写回 Key；继承的容器内代理主机名在宿主机
不可解析的问题已在统一解析层修复：宿主机运行选快照中的原始回环代理，容器运行继续选容器地址。两个
探针随后均以 `INHERIT` 通过，没有改为 DIRECT，也没有修改保存配置。这只证明当时账号与路由可用，不
保证未来额度、模型端点或供应商状态。

2026-09-20 又在运行容器中用保存的 `qwen/qwen3.7-max` 和 Codex CLI 复现了 Responses 路由接受 schema
却返回散文/Markdown 围栏的问题；应用上述双重契约和本地严格复验后，同 Key、模型、CLI 与代理返回合法
对象。探针只要求一个合成枚举和短理由，不读取市场、不写交易。随后原生 `gpt-5.6-terra` 的 shell + schema
探针也通过；此前空白 Codex 错误从客户端私有失败记录确认是 `serverOverloaded`/模型容量过载，不是容器
CLI 或账户 schema 配置损坏。

## 浏览器验收

`tests/dashboard_ui.cjs` 使用本机 Chrome，在 390、768、1440px 检查八区导航、插件中心七类二级页、
模型服务、三种 Provider 常驻账号/额度信息、OpenRouter 受限模型选择与独立配置生成、未保存草稿、启用顺序、
独立采集/异常证据、按需读取的评估器粗筛明细、盈亏账本、决策五步、网络折叠和远程登录弹窗，并断言页面停留期间不会后台轮询
runtime 或模型 controls。模型列表、账号读数和写请求由浏览器测试局部 fixture 拦截，不向运行
部署提交测试 Key、配置、验证码或交易。

```bash
npm install --no-save --package-lock=false playwright
DASHBOARD_TEST_URL=http://127.0.0.1:18765 \
  DASHBOARD_SCREENSHOTS=runtime-data/ui-review node tests/dashboard_ui.cjs
```

2026-09-23 三种宽度均通过（模型选择方式、Laya 插件入口、决策记录、采集与异常、资金页），无 JavaScript 错误和整页横向溢出。目标必须是隔离的管理测试实例；这不替代
iOS Safari、Android 真机、Windows 原生浏览器或真实账号授权验收。

登录、回调和代理的非浏览器专项分别位于：

- `tests/login_probe.test.cjs`、`tests/test_callback_mapping.py`、`tests/test_login_relay.py`；
- `tests/test_helper_terminal.py`、`tests/test_local_callback_transport.py`；
- `tests/test_host_proxy.py`、`tests/test_local_launcher.py`、`tests/test_registry_pull.py`。

## wheel 与容器

```bash
.venv/bin/python -m build --wheel --outdir runtime-data/build-openrouter
```

2026-09-20 最终 wheel 为 453145 字节，SHA-256
`1537acde09bc522df679c0e982b2b4b99ee68250f74405b15170b09ce64aa777`。已检查包内包含
`agent/decision_evaluator.py`、`plugin_system/config_io.py`、`plugins/evaluators/jev.py`、
`plugins/providers/openrouter.py`、`runtime/pnl.py` 和新版 dashboard
静态资源，不包含任何已退役的通用兼容 Provider 模块。

2026-09-23 更新后的 wheel 为 477886 字节，SHA-256
`9a29b716305e61a13bff23f82d1dbf687af202dd67b2efab5345300d73b24583`；已核实包内含独立 Laya evaluator、更新的决策回退和界面资源。旧值保留作历史验收记录，不代表当前包。

`.github/workflows/container.yml` 的 `login-helpers` 作业在 macOS、Ubuntu 和 Windows 原生运行终端助手与
宿主代理测试；`publish` 作业构建安装态镜像，执行 `deploy/check-container.sh`，再发布 amd64/arm64。
容器检查覆盖 CLI、Codex/Claude/npm、健康端点、回环明文通道和公网认证页。当前工作流不运行完整 pytest，
所以本地确定性回归和生产 API 矩阵仍是发布前的独立必做项。

若 Docker Hub 暂时不可达，但本机保留了**同仓库、同 Python 3.13 运行时**的上一版安装态镜像，可用
`deploy/Dockerfile.local-overlay` 离线覆盖当前 `src/` 与随附 Laya 服务构建临时验证镜像。它不安装或更新依赖，也不替代
标准 Dockerfile 的正式构建；必须先核实基础镜像来源和 Python 路径，并在新容器里验证导入与健康状态。
重建当前服务时仍须保留 Compose 的 `/data`、`/root` 挂载；不要用无挂载的新容器覆盖含密钥的运行数据。
使用本地标签重建时须通过 `PREDICTION_AGENT_IMAGE=<本地标签> docker compose up -d --no-deps --force-recreate robot`
显式选择镜像；之后再运行 Compose 时也须带同一变量，否则默认值会切回远端 `latest`。

## 不能由这些结果推出的结论

- 纸面交易立即按真实报价完全成交，不模拟排队、部分成交、额外滑点或真实权限。
- HTTP 代理出口探针不等于交易平台实际看到的 IP；同一代理可按域名、协议或地区分流。
- 一次真实 schema 返回不保证所有 OpenRouter 模型、路由端点或未来请求都可用。
- Chrome 响应式验收不等于所有移动设备、辅助技术或浏览器扩展行为一致。
- 历史决策、模型输出、纸面盈亏和测试通过都不构成收益保证。
