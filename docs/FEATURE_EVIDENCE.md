# 功能、文档、例子与测试证据矩阵

本表用于检查“能力是否实现、是否说明、是否有可复制例子、是否有测试”。测试名可在所列测试文件中直接检索。

| 功能族 | 实现位置 | 文档 | 例子 | 自动验证 |
|---|---|---|---|---|
| 六类插件自动扫描与插件目录 | `plugin_system/config.py`、`plugin_system/discovery.py` | [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) | [plugin_directories.json](../examples/plugin_directories.json) | [test_plugin_system.py](../tests/test_plugin_system.py)、[test_architecture_contracts.py](../tests/test_architecture_contracts.py) |
| 启用/禁用、优先级、刷新、删除后注销、teardown | `plugin_system/managed_config.py`、`plugin_system/management.py` | [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) | 六类示例的 `initialize_plugin` / `teardown` | `test_disabled_file...`、`test_management...`、`test_manual_refresh...` |
| 应用 JSON、插件目录及全部配置管理 | `core/config.py`、`plugin_system/management.py`、管理界面 | [CONFIGURATION.md](CONFIGURATION.md) | [application.json](../examples/application.json)、[plugin_directories.json](../examples/plugin_directories.json) | `ConfigurationTests`、配置 UI 冒烟 |
| 配置优先于 SQLite 初始化、两库路径可配置 | `core/config.py`、`runtime/dashboard.py`、`runtime/cloud.py` | [CONFIGURATION.md](CONFIGURATION.md)、[AUTHENTICATION.md](AUTHENTICATION.md) | [application.json](../examples/application.json) | `test_web_application_uses_configured_databases_before_any_access` |
| 插件私有 JSON、动态字段、description/default/secret、读取/保存/删除回调 | `plugin_system/discovery.py`、`plugin_system/config_io.py`、管理界面 | [CONFIGURATION.md](CONFIGURATION.md) | [plugin_configs](../examples/plugin_configs) | `PluginSystemTests`、配置 UI 冒烟 |
| 标准预测市场 API 与差异能力清单 | `plugin_system/contracts.py`、`plugin_system/registry.py` | [API_PLUGINS.md](API_PLUGINS.md) | [static_demo.py](../examples/api_plugins/static_demo.py) | `test_api_plugin_protocol...`、生产 API 集成矩阵 |
| Binance 读取、报价、下单、撤单、赎回、双向划转 | `plugins/api/binance.py`、`plugins/api/_binance/` | [API_PLUGINS.md](API_PLUGINS.md) | [binance.json](../examples/plugin_configs/binance.json) | [test_api_integration.py](../tests/test_api_integration.py) 的读取与六个真实拒绝子测试 |
| Polymarket 读取、订单、撤单、赎回、转出 | `plugins/api/polymarket.py`、`plugins/api/_polymarket/` | [API_PLUGINS.md](API_PLUGINS.md) | [polymarket.json](../examples/plugin_configs/polymarket.json) | 官方客户端工作流 mock + 三个始终联网的写端点拒绝子测试 |
| 所有写动作线上传输、无执行模式、本地不伪造成功 | `runtime/broker.py` 与各 API write transport | [ARCHITECTURE.md](ARCHITECTURE.md)、[INTEGRATION_TESTS.md](INTEGRATION_TESTS.md) | [static_demo.py](../examples/api_plugins/static_demo.py) 的真实 HTTP transport | 架构契约扫描、真实拒绝矩阵、失败持久化测试 |
| 平台订单状态在 API 插件内归一化 | `plugins/api/_binance/write.py`、`plugins/api/_polymarket/write.py` | [API_PLUGINS.md](API_PLUGINS.md) | `static_demo.py` 的标准状态检查 | Binance/Polymarket 状态回归 + 通用网关拒绝私有状态测试 |
| Codex、Claude、OpenAI-compatible 与顺序故障转移 | `plugins/providers/`、`agent/decision.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [static_provider.py](../examples/decision_provider_plugins/static_provider.py)、[codex.json](../examples/plugin_configs/codex.json) | Provider 优先级、运行期故障转移、结构化输出测试 |
| 每轮输入输出、Agent 步骤、会话滑窗和历史召回 | `runtime/memory.py`、`agent/decision.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md)、[HTTP_AUDIT.md](HTTP_AUDIT.md) | static Provider + static evidence 组合 | `DecisionAndMemoryTests`、`AgentLoopTests` |
| 自主多步工具 Agent，三类 Provider 共用工具协议 | `agent/decision.py`、`agent/research.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [static_evidence.py](../examples/research_tool_plugins/static_evidence.py) | 工具→回灌→继续→最终决策及动态 schema 测试 |
| 多平台交叉检索与平台能力差异输入 | `runtime/evaluation.py`、`plugins/research/standard_research.py` | [ARCHITECTURE.md](ARCHITECTURE.md)、[API_PLUGINS.md](API_PLUGINS.md) | static API/工具插件 | Binance+Polymarket 同注册表、并发读取与同引擎测试 |
| 文本决策策略、可切换策略与哈希追踪 | `agent/strategy.py`、`plugins/strategies/` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [example_strategy.py](../examples/decision_strategy_plugins/example_strategy.py)、[example_strategy.md](../examples/decision_strategy_plugins/example_strategy.md) | 策略筛选、提示词注入和插件初始化测试 |
| 时区信息延迟策略及新闻研究 | `timezone_latency.py` 与策略文本 | [STRATEGY_RESEARCH.md](STRATEGY_RESEARCH.md) | [strategy_timezone_latency.json](../examples/plugin_configs/strategy_timezone_latency.json) | 策略插件扫描、加载与文本哈希测试 |
| 按目标风控、资金/净结果、Agent 白名单、动态 Python | `core/risk.py`、`plugins/risk/` | [RISK_AND_HOOKS.md](RISK_AND_HOOKS.md) | [reject_operation.py](../examples/risk_plugins/reject_operation.py)、[cap_trade_size.py](../examples/risk_rules/cap_trade_size.py) | 净结果、敞口、动态规则、无插件不加政策及插件边界测试 |
| Agent、quote/order/fill/cancel/redeem/transfer Hook | `core/hooks.py`、`plugins/hooks/` | [RISK_AND_HOOKS.md](RISK_AND_HOOKS.md) | [audit_hook.py](../examples/hooks/audit_hook.py) | Hook 顺序、注册/注销和插件生命周期测试 |
| 专用决策账本与可解释性界面 | `runtime/memory.py`、`runtime/dashboard.py`、`runtime/reporting.py` | [HTTP_AUDIT.md](HTTP_AUDIT.md) | 已保存 ledger 记录即为可查询样例 | ledger 全链路字段、筛选 API、HTML UI 与后续盘口测试 |
| 首次 admin Passkey、ECDH 加密信封、可选签名计数、闲置/绝对会话、设备与踢出 | `runtime/auth.py`、`runtime/dashboard.py` | [AUTHENTICATION.md](AUTHENTICATION.md) | 首次访问向导与“管理员安全”界面 | [test_authentication.py](../tests/test_authentication.py) 的派生、注册、零/回退计数兼容、加密、重放、期限和会话测试 |
| wheel 程序入口与本地/Railway/Vercel/AWS/阿里云部署 | `__main__.py`、`runtime/cloud.py`、根目录与 `deploy/` 部署文件 | [DEPLOYMENT.md](DEPLOYMENT.md) | `start-local.*`、`compose.yaml`、平台模板 | CLI、ASGI、配置解析、wheel 安装、容器与模板验证 |
| 应用 JSON 自动加载、直连/系统/显式代理 | `core/config.py`、`plugin_system/config_io.py` 与各联网插件 | [CONFIGURATION.md](CONFIGURATION.md) | [application.json](../examples/application.json) 和各插件 JSON | 配置类型/默认/删除、配置清单和生产联网测试 |
| 零跳过及架构边界持续检查 | `tests/test_architecture_contracts.py` | [INTEGRATION_TESTS.md](INTEGRATION_TESTS.md) | 本测试文件本身可作为新增契约模板 | 全套 pytest；检测 skip/xfail、执行模式、插件系统私有字段和账本 UI |
| 非商业免费、商业另行授权 | [LICENSE](../LICENSE)、`pyproject.toml` | README 的“许可”章节 | `LICENSE` 中的 Required Notice | 架构契约检查许可证正文、项目通知、README 和包元数据；wheel 收录验证 |

示例里的静态数据只用于说明插件协议，不会被生产程序静默选作替代数据。真实内置 API 插件始终使用其
私有 JSON 指向的线上端点。
