# 两类过滤插件

系统里**只有这两类插件会拒绝动作**。除此之外没有任何其它过滤：没有 Hook，没有拦截器，内核本身不附加政策。

## 各管什么

| 类别 | 管什么 | 问的问题 |
| --- | --- | --- |
| **Agent 行为风控** `agent_policy` | LLM 通过框架注入协议发起的预测市场业务工具调用 | 这个 Agent 被允许发起这类业务调用吗 |
| **业务风控** `risk` | 一切市场 API 动作，**包含只读**：下单、撤单、赎回、转账，以及查行情、查订单簿、列标的 | 这次调用可以对市场发生吗 |

分界线是**谁在问**和**发生了什么**，不是读写：

- 业务风控挂在市场 API 插件上，与调用者无关——模型提的、结算扫单产生的、手动触发一轮产生的，都要过。
  只读调用也要过：它照样消耗平台的限速额度，也决定了这一轮后面能看到什么。
- Agent 行为风控挂在框架执行的业务工具调用上，其中只有一部分是市场 API。Codex/Claude 自己的搜索、
  文件、命令、skills 和插件由官方 CLI 的 sandbox、permission mode、rules/hooks 与用户配置管理，不经过
  本框架冒充或二次实现，因此也不会被 `agent_policy` 虚假宣称已拦截。

一笔由模型提出的下单会**先后经过两类检查**，这是有意的重复而不是冗余：前者管权限，后者管后果。

## 同一类可以同时启用多个，串成一条链

每一类都允许同时启用多个插件。它们按界面上的"顺序"号串成一条链，**全部通过才放行**：

- 任意一个返回 `REJECT` 或 `HALT` → 这次调用/动作整体失败，**链上后面的插件不再执行**（结论已经定了，
  继续跑只会让用户自己的 Python 产生多余的副作用）。
- 任意一个**抛异常** → 同样算拒绝。一个崩掉的检查并没有"放行"，它只是没能回答；因为检查坏了就放行，
  是唯一一种会让缺失的防护变成真金白银流出去的失败方式。
- 任意一个返回内核不认识的结果 → 也算拒绝，理由同上。
- 全部返回 `ADJUST` 时取最严格的那个值，**但只有 Agent 行为风控能真正缩减**，见下一节。

链是"与"关系，没有优先级也没有覆盖。所以一条宽的公司级规则和一条窄的部门级规则可以同时启用，
互相不需要知道对方存在。

## ADJUST 只在 Agent 行为风控有效

`ADJUST`（把金额缩减到某个值）要起作用，必须发生在向平台要报价**之前**。Agent 行为风控审的是模型提出
的交易动作，那时还没报价，缩减后再去报价即可，所以这一类用得上。

业务风控审的是已经成形的市场 API 调用：金额已经和平台给出的 `quoteId` 绑死，只读调用更没有"金额"可言，
**缩不动**。所以业务风控插件返回 `ADJUST` 时，这次动作会被**拒绝**，并提示改到 Agent 行为风控里做，
而不是放行。放行才是最坏的：规则作者以为自己限住了额度，实际整笔原样发出去了。

要限额，就把规则写成 `agent_policy` 类。

## 出厂不启用任何过滤插件

`plugin_selection.default.json` 的 `enabled.risk` 和 `enabled.agent_policy` 都是空的。装完即用的状态下
**没有任何风控规则生效**：没有仓位上限、没有止损、没有动作白名单。限制全部来自你自己启用并配置的插件。

这是有意的。默认塞一份没人选过、也没人配过的风控清单，只会让界面上出现看起来像防护的东西，而它既不是
你定的，也可能什么都不检查。要限制就自己选、自己配。

两个内置插件各自需要配置才会起作用：`agent_actions` 要 `ALLOWED_TOOLS`，`custom_rules` 要指向至少
一个受信任的 `.py` 规则文件。

**没有内置的止损、止盈或仓位上限,一处都没有。** 这是刻意的:在哪里止损是个判断,写死在框架里就成了
一个没人选过、也看不见的判断。要止损有两条路,都在你自己手里:

| 想怎么止 | 写在哪 |
| --- | --- |
| 按规则硬拦 | 业务风控插件,在链上按 `place_order`/`transfer` 的金额拒绝 |
| 让模型自己判断 | 决策策略的文本里写明标准,模型用 `READ_ACCOUNT` 读账自行执行 |

`custom_rules` 在启用但未指定规则文件时会明确报告未就绪，理由写着"不施加任何限制"，而不是显示绿色的
已就绪——一个启用了却什么都不检查的风控，显示成已就绪是最容易误导人的状态。

## 模型手上有哪些工具

决策要能自己看、自己比、自己动手，所以市场契约和执行网关的**每一个能力**都有对应工具，另加账务与比价：

| 类别 | 工具 |
| --- | --- |
| 平台与市场 | `LIST_PLATFORMS`、`LIST_TOPICS`、`GET_TOPIC`、`GET_ORDER_BOOK`、`SYNC_TIME` |
| 行情与比较 | `GET_KLINES`、`REFRESH_MARKET`、`SEARCH_MARKETS`、`COMPARE_OUTCOMES` |
| 账务与历史 | `READ_ACCOUNT`（本机账本）、`RECALL_HISTORY` |
| 平台资金 | `ACCOUNT_FUNDS`（平台此刻能花多少）、`ENSURE_FUNDS`（我要有这么多）、`FUNDING_STATUS`（那次请求后来怎样） |
| 结算判定 | `OUTCOME_WON` |
| 交易与资金 | `GET_QUOTE`、`PLACE_ORDER`、`CANCEL_ORDERS`、`REDEEM`、`TRANSFER` |

## 账户的钱从哪来

买卖要有本金，但平台获取方式不同。Binance Prediction 没有当前适配器可用的预测钱包余额接口，因此
`BINANCE_TRADING_CAPITAL` 是用户声明的可用金额，来源明确标记为 `declared`；转账或交易后需要手工更新。
Polymarket 不再接受配置本金，插件直接查询钱包抵押品余额，查询失败就返回 0 和错误，不拿旧数字冒充余额。

这些都是**账户状态，不是风控上限**——框架不拿它们当用户定义的仓位规则。模型 `READ_ACCOUNT` 看到的
`starting_capital` 和 `net_result` 以账户初始化时的可用金额为基准。Binance 在钱包和预测账户之间双向划转，
Polymarket 直接用钱包里的抵押品交易；入金由插件生成并核验官方 Bridge 路线，转出由插件按 Bridge
实时支持的目标链与代币构造 quote/withdraw 路线。

开启纸面交易时，所有平台统一改用 `paper_trading_funds` 作为明确标记的 simulated 金额，并写入独立纸面
状态文件；关闭后恢复各平台自己的实盘资金来源。纸面金额仍不是硬性风控规则。

`TRANSFER` 是运行中的资金通路：`INBOUND` 给交易账户入金；`OUTBOUND` 把卖出和赎回赚到的转到指定
钱包。各平台支持哪个自动方向见 `LIST_PLATFORMS`。Polymarket 的外部充值需要先向 Bridge 取得专属地址
并核验源链交易，因此由资金管理页和 `ENSURE_FUNDS` 请求链承载；通用 `TRANSFER` 不猜充值合约。

`READ_ACCOUNT` 读的是**本机账本**；要问平台此刻真正能花多少，用 `ACCOUNT_FUNDS`，它的 `source` 会
说这个数字是平台确认的还是配置里填的。资金请求的完整协议（状态、`request_id`、超时、双向理由）见
[API 插件](API_PLUGINS.md)。

这些工具**不绕过任何过滤**：读经受保护的插件、写经网关，每一次都过业务风控；工具调用本身进来时先过
Agent 行为风控。`agent_actions` 的交易动作白名单管的正是这些调用。

通用网页搜索、网页读取、文件、命令和 skills 不在这张业务工具表中；它们属于官方 CLI。要限制这些能力，
配置 Codex/Claude 自身的 sandbox、权限、rules/hooks 或企业策略，而不是在本框架填一个实际看不到这些
调用的白名单。

## 通用协调器

内核只认识命名目标及四种结果：`ALLOW`、`ADJUST`、`REJECT`、`HALT`。它按目标把动作分发给链上每个插件，
不知道损益公式、金额、白名单内容或网络地址。

实际被分发的目标只有两族：

| 目标 | 由谁发起 |
| --- | --- |
| `agent:actions` | 每一次 LLM 工具调用 |
| `market:<平台>` | 每一次市场 API 调用 |

插件用 `target` 声明自己管哪些目标，可以写成通配：`market:*` 覆盖所有平台，`market:binance` 只覆盖一个。
没有任何已启用插件适用于某目标时，协调器返回 `ALLOW`，意思是"不增加政策"——是否限制完全取决于已启用
插件，而不是核心层默认拒绝。

## 内置插件

- `agent_actions`（Agent 行为风控）：插件 JSON 定义 Agent 工具和交易动作白名单。
- `custom_rules`（业务风控）：插件 JSON 指定受信任 `.py` 文件；每个文件导出
  `evaluate(operation, context)`，异常失败关闭。目标为 `market:*`，覆盖全部平台的全部市场 API 动作。
  规则收到的 `operation` 是市场 API 调用名（`place_order`、`cancel_orders`、`redeem`、`transfer`、
  `get_order_book`、`get_candles`、`list_topics` 等）。可拒绝或停止，不能缩减。
- `custom_rules`（Agent 行为风控）：同样加载受信任 `.py` 文件，但目标是 `agent:actions`。规则收到的
  `operation` 是工具名或交易动作，`context["kind"]` 为 `tool` 或 `trade`。这一类可以缩减。

两个 `custom_rules` 同名但分属不同类别，各自在自己的类别页配置，配置文件分别是
`config/plugins/risk_custom_rules.json` 和 `config/plugins/agent_policy_custom_rules.json`。

没有硬编码永久写拦截。实盘中，规则允许的动作会进入线上传输，API 权限、签名和业务参数由远端最终判定；
纸面交易中，同一过滤链执行完后，最终写动作进入明确标记的本地纸面传输。例子按类别分开放：

| 例子 | 类别 | 演示 |
| --- | --- | --- |
| `examples/risk_plugins/reject_operation.py` | 业务风控 | 完整插件，`market:*` 目标，拒绝指定市场 API 动作 |
| `examples/risk_rules/refuse_large_orders.py` | 业务风控 | 规则脚本，拒绝转账和超额下单 |
| `examples/agent_policy_plugins/refuse_tool.py` | Agent 行为风控 | 完整插件，`agent:actions` 目标，拒绝指定工具或动作 |
| `examples/agent_policy_rules/cap_trade_size.py` | Agent 行为风控 | 规则脚本，用 `ADJUST` 把每笔买入压到 1 USDT |

插件声明的 `target` 必须落在协调器真正分发的两族目标上（`agent:actions` 或 `market:<平台>`，可用通配）。
自己发明一个目标名的插件能加载、界面也显示已启用，但**永远不会被调用**。
