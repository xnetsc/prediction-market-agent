# Provider、策略与研究工具

## Provider 优先级

`codex`、`claude`、`openai_compatible` 都是自动扫描 Provider 插件。管理名单顺序就是运行优先级；启动
不可用或调用失败会转到下一项，不会退回硬编码买卖规则。

- Codex：调用本机 `codex exec`，每次使用结构化输出 schema 和只读工作目录。
- Claude：调用本机 Claude 客户端及其 JSON schema 输出。
- OpenAI-compatible：调用配置的 Chat Completions 兼容端点，可用于 OpenAI、OpenRouter 或其他服务。

客户端可以保有自身登录状态；机器人仍保存每轮完整输入、原始输出和工具步骤，负责跨 Provider 的业务
会话、滑动窗口和召回。

## 客户端账号与模型

界面“模型服务”提供 Codex/Claude 独立登录、失效提示、检查版本及点击升级。自动检查只查询官方版本，
不自动安装。模型在各插件的 `CODEX_MODEL`、`CLAUDE_MODEL`、`COMPATIBLE_MODEL` 字段分别指定；
CLI 留空沿用客户端默认，兼容 API 留空则尚未配置完成。CLI 模型支持直接填写模型名，不猜测账号模型权限。
兼容 API 默认预置 OpenRouter，也可套用 OpenAI 预置并修改 URL；密钥留空待用户填写。切换预置保存时清除
旧服务密钥；打开配置自动读取已保存 URL 的 `/models`，可按模型名称/ID 搜索、选择或手动输入。
修改连接配置后保存，再点击“刷新模型列表”。样例见
`examples/plugin_configs/codex.json`、`claude.json`、`openai_compatible.json` 和 `openrouter.json`。
网页登录和代理步骤直接显示在 UI 内，补充说明见 [CLIENT_ACCOUNTS.md](CLIENT_ACCOUNTS.md)。
Codex 与 Claude 的独立代理字段和模型/强度在配置页首要分区直接显示；默认继承统一代理，也能分别改为
直连或专用 HTTP(S) 代理。

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

策略不是启动必需项。界面可明确选择“不使用决策策略”；保存空选择后不会回退到先前策略，通用框架
使用中性候选透传，只把平台提交的候选项交给模型，不附加策略插件的筛选或提示词。

每次决策台账保存策略名和哈希，可用界面比较不同策略版本的提案、风险调整、执行和后续盘口。
