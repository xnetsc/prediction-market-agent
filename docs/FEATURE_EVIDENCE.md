# 功能、文档、例子与测试证据矩阵

本表用于检查“能力是否实现、是否说明、是否有可复制例子、是否有测试”。测试名可在所列测试文件中直接检索。

| 功能族 | 实现位置 | 文档 | 例子 | 自动验证 |
|---|---|---|---|---|
| 六类插件自动扫描与 SDK 目录 | `sdk_config.py`、`plugins/discovery.py` | [PLUGIN_SDK.md](PLUGIN_SDK.md) | [plugin_sdk.json](../examples/plugin_sdk.json) | [test_plugin_sdk.py](../tests/test_plugin_sdk.py)、[test_architecture_contracts.py](../tests/test_architecture_contracts.py) |
| 启用/禁用、优先级、刷新、删除后注销、teardown | `managed_config.py`、`plugin_management.py` | [PLUGIN_SDK.md](PLUGIN_SDK.md) | 六类示例的 `initialize_plugin` / `teardown` | `test_disabled_file...`、`test_management...`、`test_manual_refresh...` |
| 插件私有 JSON、动态字段、description/default/secret、读写回调 | `plugins/discovery.py`、`plugin_config_io.py`、管理界面 | [CONFIGURATION.md](CONFIGURATION.md) | [plugin_configs](../examples/plugin_configs) | `PluginConfigurationTests`、配置 UI 冒烟 |
| 标准预测市场 API 与差异能力清单 | `plugins/base.py`、`plugins/registry.py` | [API_PLUGINS.md](API_PLUGINS.md) | [static_demo.py](../examples/api_plugins/static_demo.py) | `test_api_plugin_protocol...`、生产 API 集成矩阵 |
| Binance 读取、报价、下单、撤单、赎回、双向划转 | `plugins/binance*.py` | [API_PLUGINS.md](API_PLUGINS.md) | [binance.json](../examples/plugin_configs/binance.json) | [test_api_integration.py](../tests/test_api_integration.py) 的读取与六个真实拒绝子测试 |
| Polymarket 读取、订单、撤单、赎回、转出 | `plugins/polymarket*.py` | [API_PLUGINS.md](API_PLUGINS.md) | [polymarket.json](../examples/plugin_configs/polymarket.json) | 官方 SDK 工作流 mock + 三个始终联网的写端点拒绝子测试 |
| 所有写动作线上传输、无执行模式、本地不伪造成功 | `broker.py` 与各 API write transport | [ARCHITECTURE.md](ARCHITECTURE.md)、[INTEGRATION_TESTS.md](INTEGRATION_TESTS.md) | [static_demo.py](../examples/api_plugins/static_demo.py) 的真实 HTTP transport | 架构契约扫描、真实拒绝矩阵、失败持久化测试 |
| 平台订单状态在 API 插件内归一化 | `plugins/binance_write.py`、`plugins/polymarket_write.py` | [API_PLUGINS.md](API_PLUGINS.md) | `static_demo.py` 的标准状态检查 | Binance/Polymarket 状态回归 + 通用网关拒绝私有状态测试 |
| Codex、Claude、OpenAI-compatible 与顺序故障转移 | `provider_plugins/`、`decision.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [static_provider.py](../examples/decision_provider_plugins/static_provider.py)、[codex.json](../examples/plugin_configs/codex.json) | Provider 优先级、运行期故障转移、结构化输出测试 |
| 每轮输入输出、Agent 步骤、会话滑窗和历史召回 | `memory.py`、`decision.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md)、[HTTP_AUDIT.md](HTTP_AUDIT.md) | static Provider + static evidence 组合 | `DecisionAndMemoryTests`、`AgentLoopTests` |
| 自主多步工具 Agent，三类 Provider 共用工具协议 | `decision.py`、`research.py` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [static_evidence.py](../examples/research_tool_plugins/static_evidence.py) | 工具→回灌→继续→最终决策及动态 schema 测试 |
| 多平台交叉检索与平台能力差异输入 | `engine.py`、`standard_research.py` | [ARCHITECTURE.md](ARCHITECTURE.md)、[API_PLUGINS.md](API_PLUGINS.md) | static API/工具插件 | Binance+Polymarket 同注册表、并发读取与同引擎测试 |
| 文本决策策略、可切换策略与哈希追踪 | `strategy_plugin.py`、`strategy_plugins/` | [DECISION_PLUGINS.md](DECISION_PLUGINS.md) | [example_strategy.py](../examples/decision_strategy_plugins/example_strategy.py)、[example_strategy.md](../examples/decision_strategy_plugins/example_strategy.md) | 策略筛选、提示词注入和插件初始化测试 |
| 时区信息延迟策略及新闻研究 | `timezone_latency.py` 与策略文本 | [STRATEGY_RESEARCH.md](STRATEGY_RESEARCH.md) | [strategy_timezone_latency.json](../examples/plugin_configs/strategy_timezone_latency.json) | 策略插件扫描、加载与文本哈希测试 |
| 按目标风控、资金/净结果、Agent 白名单、动态 Python | `risk.py`、`risk_plugins/` | [RISK_AND_HOOKS.md](RISK_AND_HOOKS.md) | [reject_operation.py](../examples/risk_plugins/reject_operation.py)、[cap_trade_size.py](../examples/risk_rules/cap_trade_size.py) | 净结果、敞口、动态规则、无插件不加政策及 SDK 边界测试 |
| Agent、quote/order/fill/cancel/redeem/transfer Hook | `hooks.py`、`hook_plugins/` | [RISK_AND_HOOKS.md](RISK_AND_HOOKS.md) | [audit_hook.py](../examples/hooks/audit_hook.py) | Hook 顺序、注册/注销和插件生命周期测试 |
| 专用决策账本与可解释性界面 | `memory.py`、`dashboard.py`、`reporting.py` | [HTTP_AUDIT.md](HTTP_AUDIT.md) | 已保存 ledger 记录即为可查询样例 | ledger 全链路字段、筛选 API、HTML UI 与后续盘口测试 |
| dotenv 自动加载、直连/系统/显式代理 | `models.py`、`plugin_config_io.py` 与各联网插件 | [CONFIGURATION.md](CONFIGURATION.md) | `.env.example` 和各插件 JSON | dotenv 优先级、配置清单和生产联网测试 |
| 零跳过及架构边界持续检查 | `tests/test_architecture_contracts.py` | [INTEGRATION_TESTS.md](INTEGRATION_TESTS.md) | 本测试文件本身可作为新增契约模板 | 全套 pytest；检测 skip/xfail、执行模式、SDK 私有字段和账本 UI |

示例里的静态数据只用于说明插件协议，不会被生产程序静默选作替代数据。真实内置 API 插件始终使用其
私有 JSON 指向的线上端点。
