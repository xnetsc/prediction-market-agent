# Provider、策略与研究工具

## Provider 优先级

`codex`、`claude`、`openrouter`（界面显示为 OpenRouter）都是自动扫描 Provider 插件。只有已启用项参与运行；
默认按实测质量优先，同分按管理名单顺序；打开“强制按设置顺序”后只按名单顺序。不可用或调用失败会转到
下一项，不会退回硬编码买卖规则。

- Codex：调用本机 `codex exec`，保留实时搜索、文件、命令、skills、插件和用户规则，以结构化输出 schema
  接收最终业务结果；模型和账号使用 Codex 客户端自己的配置。
- Claude：调用本机 Claude 客户端，保留其 Web、文件、命令、skills/插件等 Agent 能力，以 JSON schema
  接收最终业务结果；模型和账号使用 Claude 客户端自己的配置。
- OpenRouter：OpenRouter 只替换 CLI 背后的 LLM、模型 ID、Key 和计费。`OPENROUTER_AGENT_CLI` 可选
  `AUTO`、`CODEX`、`CLAUDE`；AUTO 优先 Codex。两种 CLI 都不可用时该 Provider 不可用。
  插件私有回环守卫保持 Responses/Messages 的输入输出协议，固定模型并注入
  `provider.require_parameters=true`。模型原生支持 CLI 参数时直接使用；不支持时，只在这个 Provider 内把
  版本化 Web Search/Fetch 声明转成 OpenRouter server tools、把 Codex namespace tools 展平并在返回时复原，
  并移除非 Anthropic 路由不接受的 Claude 默认 `output_config.effort` 提示；JSON format、thinking、context
  management、普通工具和消息内容保持不变。不把 Agent 协议降级成 Chat Completions，也不自己重做 CLI
  的 Agent 循环。部分 Responses 兼容路由会接收 `json_schema` 却仍给最终对象包 Markdown 围栏；因此
  OpenRouter-Codex 路径同时把完整 schema 写进最终提示，只剥离一个纯 JSON 围栏并在本地严格复验整个
  schema。夹杂散文、缺字段、额外字段或范围错误仍按 contract failure 处理。

三种 Provider 都严格遵守各自保存的模型选择。Codex/Claude 留空表示明确使用当前客户端默认；
填写后每次命令都传入该型号。OpenRouter 必须选择一个型号，守卫会同时固定 CLI 命令、本地模型目录与
上游请求中的 `model`；不支持的组合直接不可用，不自动改用其他模型。Provider 故障转移只会切换到用户已启用的
另一个 Provider，不改写任何 Provider 自己的模型设置。
Codex 的结构化失败事件可能写在 JSONL stdout 而不是 stderr；运行层同时解析两者，并区分模型容量过载
（transient）、账号额度窗口（rate_limit）和 schema 违约（contract）。界面不会再把容量过载显示成空错误
或误报为周/短时额度耗尽。

每个发现、分析或决策顶层任务创建一个新的官方 CLI 会话；同一轮里的业务工具往返和插件反问续接该会话，
下一轮不沿用。机器人保存完整审计记录，但不替 CLI 做滑动窗口、摘要或自动历史拼接。新会话首条输入明确
给出统一 SQLite 账本、Codex 私有 session/archived-session、Claude 私有 projects/history 的绝对路径，
以及当前轮次、最相关上一轮和显式续接轮次 ID；Codex 与 Claude 可用各自的文件/命令工具互读双方记录，
由当前 CLI 自己决定读取、裁切和保留哪些前置信息。绝对路径来自双方插件配置的 AUTH_DIRECTORY；旧默认
HOME 中仍存在的记录也会列出。OpenRouter 启动 CLI 时套用同一个私有 HOME，不另建一套隐藏历史。

## 登录凭据的导出与导入

登录本身走官方客户端，在你的设备上用浏览器完成。这件事做一次是合理的，每换一个容器就重做一次不是，
所以模型服务页的每个客户端卡片上提供“导出登录凭据 / 导入登录凭据”。

- 导出只打包能恢复登录态的文件：Codex 是 `.codex/auth.json`，Claude 是 `.claude/.credentials.json`。
  客户端家目录里还会堆积本地设置、项目历史和日志，这些与保持登录无关，也不该躺在一个凭据文件里被带走。
- 导出文件**含可直接使用的登录令牌**，界面在确认框里明说了这一点；拿到它的人能以该账号发起请求。
- 未登录时导出按钮不可用。
- 导入会校验文件类型、版本和客户端名，只接受白名单内的条目，写入时目录 0700、文件 0600，并立即重新
  检查登录状态。属于其它客户端的文件会被拒绝。

## 额度与登录状态

模型服务页把三种 Provider 的账号/额度摘要直接显示在卡片上，不再藏在登录维护折叠区。查询均不发送
模型 prompt，也不消耗模型额度：

| Provider | 入口 | 返回 |
| --- | --- | --- |
| Claude | `claude -p "/usage" --output-format json` | 会话与本周的已用百分比和重置时间（`num_turns` 为 0，不产生模型调用） |
| Codex | app-server 协议 `initialize` → `account/rateLimits/read` | `usedPercent`、`resetsAt`、`ordinaryUsageAllowed` |
| OpenRouter | `GET /api/v1/key` | 当前推理 Key 的累计、日、周、月消费以及可选消费上限和剩余额度 |
| OpenRouter（可选） | Management Key 调用 `GET /api/v1/credits` | 账户累计充值、累计消费和余额 |

Codex/Claude 提供“刷新账号状态”，OpenRouter 提供“刷新余额与用量”。OpenRouter Management Key 只用于
余额接口，推理 Key 仍只负责目录、推理和本 Key 状态；两种密钥不互换。恢复探测也使用这些无推理请求的
入口：用发一次 completion 判断还能不能发请求，既要花额度，又在额度已经耗尽时什么都问不出来。

## 可用性监控与选择方式

模型服务会因为配额窗口、会话过期或端点过载而临时不可用，而这些都会自己恢复。固定顺序重试意味着每一次
决策都要为同一个故障多付一次失败往返，所以框架按失败类型给出退避，并在退避结束后放行一次探测：

| 失败类型 | 判定依据 | 初始退避 |
| --- | --- | --- |
| `rate_limit` | 429、rate limit、quota、usage limit、overloaded | 300 秒 |
| `auth` | 401/403、invalid key、expired、未登录 | 900 秒 |
| `transient` | 5xx、超时、连接中断 | 30 秒 |
| `contract` | schema/JSON/非法决策字段 | 15 秒 |

连续失败按指数增长并封顶；一次成功即清零并立刻回到轮换。全部服务都在退避时不会拒绝决策，只是按原顺序
继续尝试。

可用的服务按实测质量排序，质量由三个互相独立的信号合成，都不依赖模型对自己的评价：

- **送达率**：`provider_turns` 里返回可用答案的比例。
- **校准度**：已结算决策的 Brier 分数——声明的概率与实际结算的距离。0.25 相当于永远说 50%，作为中性点。
- **他评**：由**另一个**服务按"数字是否都来自输入、理由与动作是否一致、是否按可执行价加费用比较"打分。
  同一个服务给自己打的分不计入——推理错了的模型通常会给自己的推理高分。

每个分量按样本量向 1.0 收缩，样本不足时不影响排序；合成分数有上下限。他评每轮有条数上限，因为每一次
打分都是一次真实调用。

模型服务页提供共用的选择方式：默认 `QUALITY`，在已启用且可用的实例中质量优先、同分按设置顺序；
`CONFIGURED` 强制按保存的顺序、忽略质量。决策 Provider 与粗筛 evaluator 各自独立排序、每次只调用
一个实例，失败才回退到下一个已启用实例。禁用项不参与任何排序。评估器的质量依据是历史粗筛建议与后续
完整决策动作的一致程度，少量样本会向中性分收缩；它衡量节省调用的有效性，不等于交易收益或模型真值。
没有可配对的后续决策时，同分沿用用户设置顺序。设置保存后会立即作用于运行中的排序，不需要重启。

## 客户端账号与模型

界面“模型服务”提供 Codex/Claude 独立登录、失效提示、检查版本及点击升级。自动检查只查询官方版本，
不自动安装。模型在各插件的 `CODEX_MODEL`、`CLAUDE_MODEL`、`OPENROUTER_MODEL` 字段分别指定；
CLI 留空沿用客户端默认，OpenRouter 留空则尚未配置完成。OpenRouter 模型列表请求
`/models?supported_parameters=structured_outputs`，并只保留同时声明 `structured_outputs` 与 `tools` 的型号。
Codex 与 Claude 均可使用这些型号，不因缺少某个厂商专有 Web Search 参数而隐藏；插件在模型不原生支持时
使用上述 server-tool 兼容层。这样既保证最终答案遵从 schema，也保证工具参数有结构化入口。字段只能从
列表选择，不能手填绕过。
实际推理还会要求具体路由端点支持 schema 参数，避免目录能力与落到的供应端点不一致。模型服务页可生成
`openrouter_*` 插件文件；模型、代理和优先级独立，Key 留空时默认复用主 `openrouter` 配置，也可单独覆盖。
透明守卫仍完全属于该 Provider 插件。样例见 `examples/plugin_configs/codex.json`、`claude.json`、
`openrouter.json`。
网页登录和代理步骤直接显示在 UI 内，补充说明见 [CLIENT_ACCOUNTS.md](CLIENT_ACCOUNTS.md)。
Codex 与 Claude 的独立代理字段和模型/强度在配置页首要分区直接显示；默认继承统一代理，也能分别改为
直连或专用 HTTP(S) 代理。

## Jev typed evaluator

当前随仓库提供并默认选择的 `decision_evaluator` 插件实例名为 `jev`；这只是插件实现，不代表核心内置
支持某个模型。它不是第四个 Agent Provider，也不加入普通 OpenRouter 聊天模型下拉框。该实例只用于候选
粗筛和发现继续状态，不进入逐标的交易决策、不重评 Agent 提案、不输出交易动作。核心只依赖
通用 evaluator 契约，不按 `jev` 名称分支；用户可安装其它实现，仓库的
`examples/decision_evaluator_plugins/static_evaluator.py` 只演示契约且不会默认启用。

粗筛的置信阈值从 0.90 起步，并用“曾被评估器建议 DEFER/REJECT、但被质量保护留给 LLM 的候选”后续真实
结果动态校准；漏掉有效标的会抬高阈值，长期无漏判才会降低，硬下界为 0.80。最终候选至少保留原始规模的
平方根作为质量抽样，因此评估器不能筛空候选池；首选评估器报错时尝试下一个已启用且可用的实例，全部失败时该轮不剔除候选。这个机制只减少
发现 LLM 的输入 token 和部分逐页继续判断调用，最终发现选择仍由 Agent 完成。

`JEV_CONNECTION=OPENROUTER` 时默认从 `JEV_SHARED_PROVIDER` 指定的 OpenRouter Provider 复用
`OPENROUTER_API_KEY`，也可用 `JEV_API_KEY` 单独覆盖。选择官方 Jev 型号时自动请求
`https://openrouter.ai/api/alpha/decisions`；Jev 模型本身只接受 Decisions 协议。选择其它 OpenRouter 聊天
模型时，列表只显示明确声明 `structured_outputs` 的型号，插件改用官方 Chat Completions、strict
`response_format.json_schema` 和 `provider.require_parameters=true`，不能因路由变化丢掉 schema。

`JEV_CONNECTION=CUSTOM` 时填写兼容 OpenAI Chat Completions 的 Base URL 和模型名，Key 可留空；插件同样
把 `state/questions` 写入 JSON 输入并要求完整 `answers`。它先发送原生 strict `json_schema`；只有端点拒绝
该参数，或虽接受却没有返回合规 JSON 时，才在同一插件内回退到强制 `submit_typed_answers` 函数参数。
Choice 必须属于题目枚举，Score/Noul 必须为数值；两种方式都不接受散文、缺项和非法类型。

无论连接方式，`JEV_HTTP_PROXY`、超时和批大小均独立；OpenRouter Key 是 Jev 与 Provider 的唯一配置关联，
不会读取或覆盖 Provider、平台、统一代理之外的任何代理层。OpenRouter Decisions 接口处于 alpha。

## Laya 本地 WebGPU evaluator

`laya` 是另一个 `decision_evaluator` 插件实例，默认不启用，也不是交易决策 Provider。模型运行在容器外的
WebGPU 服务；界面“模型服务”里的连接测试只检查地址和运行后端，不会自动启用插件。需要在“插件中心 →
决策评估器”勾选 `laya`、保存启用状态，再配置 `LAYA_BASE_URL`（例如容器访问宿主服务的
`http://host.proxy.internal:8899/v1` 是本项目 Compose 的默认示例；其他部署须填写实测可达的地址）。其代理默认 `DIRECT`，可独立改为 `INHERIT` 或专用代理，不会
修改 Jev、决策 Provider 或平台的代理。

随附服务默认监听 `0.0.0.0` 以便容器访问；它没有鉴权，必须通过防火墙或隔离网络限制来源，不能暴露公网。只需宿主机使用时可传 `--host 127.0.0.1`。

插件启动时核对 `/health`：模型须已就绪、运行在 `webgpu`、型号为 `convaiinnovations/laya`，并声明
`choice`、`score`、`noul` 及至少四道结构化题的能力。每次只传一个候选的四道问题，严格验证
`state/questions → answers` 结果；服务断开、退回 CPU 或回答不合规时本轮粗筛失败，不把它冒充交易判断。
Laya 与 Jev 可分别启用、设置顺序；粗筛池每次只调用排在最前的可用实例，失败时才回退。一次失败会
短暂冷却，避免每页都重复等待同一个断开的服务；回退事件会出现在采集与分析异常中。

## 多步 Agent 与工具

Agent 先收到市场、能力、持仓、风险清单、策略、历史位置和续接 ID，再在配置步数内选择业务工具或 `DECIDE`。工具
名称不是内核枚举：启用的 `research_tool` 插件动态贡献名称、参数说明和执行器，控制 schema 也据此动态
生成。内置 `standard_research` 只提供预测市场专用的跨平台市场搜索、当前市场刷新、K 线和业务历史查询；
通用网页搜索、网页读取、文件、命令和 skills 由 Codex/Claude CLI 自己编排，框架不重实现。框架另外始终
提供市场契约、交易与账务工具，见 [两类过滤插件](RISK_FILTERS.md)。

## 内置策略与自动进化

未启用决策策略插件时，框架使用内置决策策略，而不是空提示词。它要求模型对可执行的那一侧报价加费用后
比较、默认 HOLD、按结算判词而非新闻标题判断，并在硬约束里禁止编造输入和绕过风控结论。内置策略是运行
时不变量，没有配置字段，但可在插件中心导出当前全文。

内置策略按已结算结果校准自己：把决策按声明概率分档，比较该档实际的 YES 结算率与该档隐含概率，偏离
足够且样本足够时写成一条注明桶名与样本数的教训。权重按样本量向基线收缩，一段短期连胜不会把权重打满。
校准只在插件能可靠判定结算赢家的平台上累积。

用户策略插件同样可以获得这层叠加，由 `strategy_evolution` 开关控制，默认开启；关闭后只使用
用户原文。用户的策略文件永不被框架改写。

## 文本策略插件

每个策略插件的私有 JSON 指定 `STRATEGY_FILE` 以及该策略自己的主题状态、市场状态、outcome 和流动性
筛选。UTF-8 文本被放入 Agent 的受信任策略上下文，文件绝对路径和 SHA-256 同时写入决策台账。因此
所谓“决策逻辑”主要表现为策略系统指引，但不等于绕过结构化 schema、工具、风控或 API 能力。

内置 `general_agent` 是通用证据型策略；`timezone_latency` 把时区和信息传播延迟案例转成可切换策略。
后者不是小额高频规则，研究依据和限制见[案例研究](STRATEGY_RESEARCH.md)。

策略插件不是启动必需项。界面可明确选择“不使用决策策略”；保存空选择后不会回退到先前插件，通用框架
改用内置决策策略。内置策略仍执行硬闸、报价/费用比较与默认 HOLD 约束，但没有用户插件的筛选字段或原文。

每次决策台账保存策略名和哈希，可用界面比较不同策略版本的提案、风险调整、执行和后续盘口。
