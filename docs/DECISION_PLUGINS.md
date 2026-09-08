# Provider、策略与研究工具

## Provider 优先级

`codex`、`claude`、`openai_compatible` 都是自动扫描 Provider 插件。管理名单顺序就是运行优先级；启动
不可用或调用失败会转到下一项，不会退回硬编码买卖规则。

- Codex：调用本机 `codex exec`，每次使用结构化输出 schema 和只读工作目录。
- Claude：调用本机 Claude 客户端及其 JSON schema 输出。
- OpenAI-compatible：调用配置的 Chat Completions 兼容端点，可用于 OpenAI、OpenRouter 或其他服务。

客户端可以保有自身登录状态；机器人仍保存每轮完整输入、原始输出和工具步骤，负责跨 Provider 的业务
会话、滑动窗口和召回。

## 多步 Agent 与工具

Agent 先收到市场、能力、持仓、风险清单、策略和历史，再在配置步数内选择研究工具或 `DECIDE`。工具
名称不是内核枚举：启用的 `research_tool` 插件动态贡献名称、参数说明和执行器，控制 schema 也据此动态
生成。内置 `standard_research` 提供网页搜索、URL 读取、跨平台市场搜索、当前市场刷新、K 线和历史召回；
它的端点、代理、SSRF 与大小/数量限制都在自己的 JSON。

## 文本策略插件

每个策略插件的私有 JSON 指定 `STRATEGY_FILE` 以及该策略自己的主题状态、市场状态、outcome 和流动性
筛选。UTF-8 文本被放入 Agent 的受信任策略上下文，文件绝对路径和 SHA-256 同时写入决策台账。因此
所谓“决策逻辑”主要表现为策略系统指引，但不等于绕过结构化 schema、工具、风控或 API 能力。

内置 `general_agent` 是通用证据型策略；`timezone_latency` 把时区和信息传播延迟案例转成可切换策略。
后者不是小额高频规则，研究依据和限制见[案例研究](STRATEGY_RESEARCH.md)。

每次决策台账保存策略名和哈希，可用界面比较不同策略版本的提案、风险调整、执行和后续盘口。
