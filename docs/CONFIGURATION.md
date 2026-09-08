# 配置参考

## 通用运行时 `.env`

| 变量 | 用途 |
|---|---|
| `PREDICTION_AGENT_ENV_FILE` | 可选的显式 `.env` 路径；否则查找当前目录和项目根目录 |
| `PLUGIN_SDK_CONFIG_FILE` | 六类插件目录的 SDK JSON |
| `BOT_MANAGEMENT_FILE` | SDK 保存的有序启用名单和当前策略 |
| `MARKET_API_PLUGINS` | 管理文件未设置 API 类别时的首次回退名单 |
| `DECISION_PROVIDERS` | Provider 首次回退顺序 |
| `RESEARCH_TOOL_PLUGINS` | 研究工具首次回退顺序 |
| `RISK_PLUGINS`、`HOOK_PLUGINS` | 风控和 Hook 首次回退顺序 |
| `PREDICTION_AGENT_MAX_TOPICS_PER_CYCLE` | 每平台每轮最多加载的候选主题数 |
| `PREDICTION_AGENT_MAX_DECISIONS_PER_CYCLE` | 每平台每轮最多决策数 |
| `PREDICTION_AGENT_INTERVAL_SECONDS` | 连续运行的轮询间隔 |
| `PREDICTION_AGENT_RUN_UNTIL_EPOCH` | 可选停止时间；零表示持续运行 |
| `PREDICTION_AGENT_STATE_FILE` | 账户镜像文件；多平台自动加入平台后缀 |
| `PREDICTION_AGENT_SESSION_DB` | 会话、工具、执行与决策台账 SQLite |
| `AGENT_MAX_TOOL_STEPS` | 每次最终决策前最多工具轮数 |
| `AGENT_TOOL_RESULT_CHARS` | 单个工具结果进入上下文的字符预算 |
| `CONTEXT_WINDOW_CHARS` | Provider 输入窗口预算 |
| `HISTORY_PER_MARKET` | 自动召回的同市场历史条数 |
| `DASHBOARD_HOST`、`DASHBOARD_PORT` | 仅回环地址的审计服务监听设置 |
| `DASHBOARD_REFRESH_SECONDS` | 页面自动刷新间隔 |

`.env.example` 是完整公共样例。通用 Config 刻意没有 API Key、私钥、URL、代理、执行开关、资金、止损、
网络路径、工具白名单、动态脚本路径或策略阈值。

## 插件私有 JSON

插件有配置时，初始化函数返回字段 schema、JSON 文件存储说明以及读取/保存回调。SDK 不拼接路径、不
打开配置文件，只调用回调。当前内置配置位于 `config/plugins/`，但位置由各插件初始化函数决定：

- API：端点、认证、钱包、代理、网络方法/路径规则及平台参数。
- Provider：客户端路径或 API 端点、模型、凭证、代理、超时。
- 策略：策略文本路径和该策略自己的候选筛选字段。
- 研究工具：搜索端点、代理、SSRF 规则、响应/结果/K 线/历史限制。
- 风控：账户分配、损益规则、动作白名单、动态 Python 文件等具体政策。
- Hook：订阅事件和输出位置。

所有字段由插件提供非空 `description`；可声明默认值、必填、枚举或秘密。秘密字段不会回显到浏览器，
保存空秘密会保留原值。示例 JSON 见 `examples/plugin_configs/`。

`config/plugins/*.json` 是本机私有运行配置，已在 `.gitignore` 中排除。公开仓库只提交
`examples/plugin_configs/` 下的脱敏样例；真实 API Key、私钥、钱包、认证头和签名文件不得复制到样例、
日志、文档或提交记录中。

研究工具和 Hook 类别允许启用名单为空；此时 Agent 可直接进入 `DECIDE`，且不会产生对应插件行为。
API、Provider 和当前决策策略是形成可运行交易流程所需的结构性组件。组合账户/执行控制由启用的风险
贡献插件提供，具体资金和约束仍只在该插件中。

## 代理

代理不是公共参数。每个需要网络的插件各自定义代理字段：`DIRECT` 表示直连，`SYSTEM` 在 macOS 读取
系统 HTTPS 代理，也可填写显式 `http://` 或 `https://` 地址。不同 API、Provider、研究工具可使用不同
路由。

## 线上执行

机器人没有线上/非线上切换。所有 BUY、SELL、CANCEL、REDEEM、TRANSFER 都调用相应 API 插件的
线上传输；缺字段、签名错误、权限不足或远端拒绝均作为真实执行失败写入决策台账和动作记录。
