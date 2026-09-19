# 测试与验收

测试分成确定性回归、生产 API 联网矩阵、浏览器验收、真实 Provider 探针和最终安装态容器五层。各层回答的
问题不同，不能用 mock、纸面交易或单元测试结果代替生产网络结论。

## 确定性回归

日常开发先运行不依赖交易平台网络的完整套件：

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q --ignore=tests/test_api_integration.py
```

2026-09-19 本轮文档与 OpenRouter 校对后的结果为 `604 passed, 244 subtests passed`。其中覆盖：

- 七类插件发现、禁用不导入、启停/刷新/teardown、动态配置和示例 schema 一致性；
- 平台自有 runtime、立即首轮、可中断等待、失败退避和通用业务事件队列；
- 内置/用户发现与决策策略、策略进化、Provider 健康与质量排序；
- Codex、Claude、OpenRouter 的模型目录、结构化输出、代理、登录和凭据迁移；
- 两类过滤插件、资金请求、实盘网关与独立纸面交易状态；
- SQLite 台账、分页/筛选/删除、并发 HTTP、Passkey/ECDH、回环与公网访问隔离；
- 本地启动器、宿主机代理、回调转发、registry 拉取和云部署契约；
- 公共示例字段完整、无退役字段、默认值一致，管理示例不启用不存在的插件；
- 测试树不允许 `skip`、`xfail` 或 `expectedFailure` 装饰器。

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

2026-09-19 结果为 `2 passed, 2 failed, 9 subtests passed`：Polymarket 两项通过；Binance 两项都在最先
访问官方公开时间接口时收到 HTTP 451，因此后续写矩阵没有执行。这个结果只说明当前运行网络受 Binance
地域策略限制，不说明 Binance 适配器通过，也不允许在本地吞掉错误。

## OpenRouter 严格 schema 验收

后端专项位于 `tests/test_model_catalog.py` 和 `tests/test_plugin_system.py`，验证：

- 模型目录请求带 `supported_parameters=structured_outputs`，响应项再次逐个检查该能力；
- `OPENROUTER_MODEL` 为只选字段，不能从 UI 手填绕过目录；
- 插件私有回环桥把 Responses schema 转成 Chat Completions `response_format.json_schema`；
- 推理请求强制 `provider.require_parameters=true`；
- 本机回环加入 `NO_PROXY`，桥到 OpenRouter 的远端请求仍经过该插件解析后的统一或独立代理；
- 真实本地 HTTP 代理收到远端目录/推理请求，证明桥接没有屏蔽原有网络代理层；
- `openrouter_*` 文件各自拥有独立配置，不把多 Key/模型逻辑放入通用框架。

另使用生产容器中已保存的 OpenRouter Key 做过单次 schema 探针，模型 `z-ai/glm-5.3` 返回严格对象
`{"number": 7, "word": "ok"}`。探针没有输出、复制或写回 Key，测试前后 `robot_paused=true`。这一结果
证明当时账号与路由可用，不保证未来额度、模型端点或供应商状态。

## 浏览器验收

`tests/dashboard_ui.cjs` 使用本机 Chrome，在 390、768、1440px 检查六区导航、插件中心六类二级页、
模型服务、OpenRouter 受限模型选择与独立配置生成、未保存草稿、启用顺序、决策五步、网络折叠和远程登录
弹窗。模型列表和写请求由浏览器测试局部 fixture 拦截，不向运行部署提交测试 Key、配置、验证码或交易。

```bash
npm install --no-save --package-lock=false playwright
DASHBOARD_TEST_URL=http://127.0.0.1:18765 \
  DASHBOARD_SCREENSHOTS=runtime-data/ui-review node tests/dashboard_ui.cjs
```

2026-09-19 三种宽度均通过，无 JavaScript 错误和整页横向溢出。目标必须是隔离的管理测试实例；这不替代
iOS Safari、Android 真机、Windows 原生浏览器或真实账号授权验收。

登录、回调和代理的非浏览器专项分别位于：

- `tests/login_probe.test.cjs`、`tests/test_callback_mapping.py`、`tests/test_login_relay.py`；
- `tests/test_helper_terminal.py`、`tests/test_local_callback_transport.py`；
- `tests/test_host_proxy.py`、`tests/test_local_launcher.py`、`tests/test_registry_pull.py`。

## wheel 与容器

```bash
.venv/bin/python -m build --wheel --outdir runtime-data/build-openrouter
```

2026-09-19 wheel 为 397617 字节，SHA-256
`c4c66dc6b7d06824c3ad4989b23c72e6c2361851a7c957a733a01060d2a50a95`。已检查包内包含
`plugins/providers/openrouter.py` 和新版 dashboard 静态资源，不包含任何已退役的通用兼容 Provider 模块。

`.github/workflows/container.yml` 的 `login-helpers` 作业在 macOS、Ubuntu 和 Windows 原生运行终端助手与
宿主代理测试；`publish` 作业构建安装态镜像，执行 `deploy/check-container.sh`，再发布 amd64/arm64。
容器检查覆盖 CLI、Codex/Claude/npm、健康端点、回环明文通道和公网认证页。当前工作流不运行完整 pytest，
所以本地确定性回归和生产 API 矩阵仍是发布前的独立必做项。

## 不能由这些结果推出的结论

- 纸面交易立即按真实报价完全成交，不模拟排队、部分成交、额外滑点或真实权限。
- HTTP 代理出口探针不等于交易平台实际看到的 IP；同一代理可按域名、协议或地区分流。
- 一次真实 schema 返回不保证所有 OpenRouter 模型、路由端点或未来请求都可用。
- Chrome 响应式验收不等于所有移动设备、辅助技术或浏览器扩展行为一致。
- 历史决策、模型输出、纸面盈亏和测试通过都不构成收益保证。
