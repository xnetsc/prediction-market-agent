# API 插件

## 标准接口

API 插件归一化 `Topic`、`Market`、`Outcome`、`OrderBook`、`Candle`、跨市场候选、结算判断和写传输。
能力清单声明实时盘口、K 线、搜索、结算、订单类型、写工作流、转账方向、数据特征和限制。Agent 会看到
每个平台自己的清单，因此不会假定所有 API 提供相同信息或动作。

通用写接口传递标准化计价金额或份额；签名、链 ID、代币精度、请求字段和响应差异由插件内部抹平。
API 插件还必须把平台订单状态归一化为 `OPEN`、`FILLED`、`CANCELED`、`REJECTED` 或 `FAILED`；原始
平台状态可另存为 `platformStatus`，通用执行网关不识别平台私有状态字符串。
网络主机/方法/路径规则同样由 API 插件私有 JSON 构造，内核没有平台白名单。所有写工作流只走线上
传输；不存在本地撮合分支。

API 插件还提供两组互补能力：标准业务接口供通用框架随时主动调用；正式后台运行所需的 runtime 只负责
该平台何时执行扫描。没有 runtime 的 API 插件仍可被显式调用，但运行监督器会把它标为未就绪，不会假装
已经自动运行。runtime 到点后调用框架注入的 `discover_markets(maximum_topics)` 回调而不是自己拉列表，再把返回的标准化
主题通过 `submit_scan` 提交给通用 `RobotEventLoop`。宽扫多深、调用哪些读接口由发现策略决定，插件不再
关心访问了哪些数据；后续详情/盘口、跨平台研究、Agent、风控与写动作同样回到通用框架。平台配置不完整或被暂停时 runtime 不启动，但管理
界面和其他就绪平台照常工作。

## 配置只问你才知道的东西

固定且公开的地址不该让人手敲：敲错一个字符，凭证和订单就发到别处去了。所以两个插件的端点都带默认值，
仍然可改（测试网、地区域名、自建 RPC）：

- Binance：`BINANCE_API_BASE_URL` 默认官方地址，账户类型默认 `SPOT`，滑点默认 100 bps
- Polymarket：Gamma / CLOB / Data / Relayer / RPC / ChainId **直接取自官方客户端的 `PRODUCTION`**，
  不是抄进代码的字面量——平台哪天换地址，跟着客户端升级就跟上了

**装完之后需要手填的只剩凭据**，也就是只有你有的东西：

| 插件 | 必须填 |
| --- | --- |
| Binance | API Key、API Secret、预测钱包地址、预测钱包 ID |
| Polymarket | 私钥、API Key / Secret / Passphrase、Funder 地址 |

这些字段标着 `needed_to_run`：**表单可以填一半先存**，但插件不给它们就报未就绪、平台不启动。界面上
它们显示「运行必需」，而这个标记和 readiness 用的是同一份声明，不会各说各话。

## 资金：报告可用、请求补足

两个方法，语义都很窄，别混：

| 方法 | 回答什么 |
| --- | --- |
| `account_funds()` | **此刻在这个预测市场能花多少**，以及这个数字是平台确认的还是配置来的（`source`） |
| `ensure_funds(amount, currency, *, reason, allow_pending, timeout_seconds)` | 我希望可用金额达到 `amount`，你看着办 |
| `funding_status(request_id)` | 之前那次请求后来怎么样了 |

**`available` 只算此刻能花的钱。** 放在别处、需要划一笔才能用的钱不算，哪怕那笔划转十拿九稳——把它
算进去，等于告诉模型可以拿还没到账的钱去下单，而发现差别的会是那张订单。所以各插件另把"能从哪补"
放在 `detail` 里，明确不是 `available`。

`source` 分 `platform`（平台确认过）和 `declared`（配置里填的，可能早就和现实脱节）。把配置数字
说成平台的答案，比承认读不到更糟。

### 请求是有状态的，而且要留证

`ensure_funds` 很少能在调用内完成——可能要人批准，可能要外部转账落地。所以它返回**状态**而不是是非，
外加一个 `request_id`：

| 状态 | 含义 |
| --- | --- |
| `satisfied` | 已经够了 |
| `pending` | 有人要批或要转，拿 `request_id` 之后查 |
| `partial` | 到了一部分，`available` 说到了多少 |
| `refused` | 被拒绝了 |
| `failed` | 出错，或者过期了 |

**能不能延迟、延迟多久，由调用方定**（`allow_pending`、`timeout_seconds`），不是插件自己说了算——
只有调用方知道还有没有人在等。`allow_pending=False` 时插件做不完就必须直说，不能挂起一个没人会回来
看的请求。过了期限的请求算 `failed`：几小时后才批准的转账，喂的是一个早已不存在的意图。

新请求会顶掉旧的，旧的记为 `refused`（superseded）——还拿着旧 id 的调用方能查到它为什么永远不会完成，
而不是查无此事。

### 等到钱之后，不是直接下单

资金请求是**中断**，不是暂停：提出请求的那一轮就结束了，模型不会挂在那里等。所以答案回来时，框架必须
主动把它捡起来 —— 每轮开始时查一遍还没结论的请求，有结论的就接续。

但**光告诉模型"钱到了"毫无业务价值**：等待期间价格在动，当初要钱的那个判断可能已经不成立了。所以接续
时做三件事：

1. **重新读市场** —— 不是拿旧数据继续，是重新拉一次 topic 和盘口
2. 把**当初的判断原样交回去**：要了多少、为什么要、当时看到的盘口和剩余时间
3. 明确告诉模型：**这是延迟的资金答复，不是新机会。先核对当初的理由在现在的价格面前还成不成立；不成立
   就说出来并 HOLD**

标的已经下架、或者本轮决策名额用完时，记录照样关闭，不会在后面每一轮反复触发同一笔交易。

### 两边都要说人话

`reason` 是机器人为什么要这笔钱，**由 AI 给**，显示给批准的人看：不给理由就让人签字，是让人凭信任签。

批准或拒绝的人也要回一句，通过 `funding_status` 回到模型：

- 同意时的附言是**指令**——"我只剩这些了，后面别再要"，模型没有别的途径知道这件事
- **拒绝必须给理由**，否则模型下一轮原样再问，操作员永远在答同一个提示

这条强制是**插件的规则**，写在插件自己的处理函数里，不是框架的。

### 转不转得进去，是插件自己的事

框架不管平台能不能自动划转。能自动的就自动，不能的就让用户自己处理——两个内置插件正好各占一边，
见下文。

## Binance

插件读取 Prediction 主题、详情、盘口和 Spot 参考 K 线；写端实现 quote、BUY、SELL、CANCEL、
REDEEM、TRANSFER_IN、TRANSFER_OUT。私有 JSON 拥有 REST URL、Key/Secret、预测钱包信息、资金
账户、滑点和代理。插件不会用本地“参数不齐”预检代替服务器验证；配置会原样进入签名请求，
服务器拒绝会记录为执行错误。

代理字段默认 `INHERIT`，使用程序设置中的统一代理；改成 `DIRECT` 或完整 HTTP(S) URL 只覆盖 Binance。

同一 Binance JSON 还拥有扫描间隔、错误退避初值/上限、每轮主题/决策上限和主题分页大小，默认分别为
60 秒、30 秒、900 秒、10、6、100。其中主题上限和分页大小现在表示本平台允许框架消耗的提交上限与请求
粒度，属于限频属性；选哪些标的由发现策略决定。正式机器人 readiness 要求 URL/规则等基础字段和 Key、Secret、
wallet address/id 齐全；直接集成测试不走该 readiness 门，因此仍能验证真实网络拒绝。

## Polymarket

插件使用 Gamma、CLOB、Data 和 Relayer/RPC，写端基于官方 `polymarket-client`：

- 限价/市价 BUY、SELL；
- authenticated cancel；
- 从可赎回持仓映射 token 到 condition 后提交赎回；
- pUSD OUTBOUND 转账。

私有 JSON 拥有所有端点、链、钱包私钥、CLOB L2、funder、用户/Builder Relayer、Builder Code、转出
地址和代理。当前适配器不能可靠判定 winner，因此能力清单明确把 `settlement_status` 设为
false；这不是占位成功值。

代理字段默认 `INHERIT`，使用程序设置中的统一代理；改成 `DIRECT` 或完整 HTTP(S) URL 只覆盖 Polymarket。

Polymarket 的同一私有 JSON 提供与 Binance 等价的六个 runtime 字段和相同默认值。正式 runtime 还要求
private key、CLOB L2 三件套和 funder address；可选 Builder/Relayer 凭证与转出地址只影响相应工作流。

官方参考：[Gasless 交易](https://docs.polymarket.com/trading/gasless)、
[市场结算](https://docs.polymarket.com/concepts/resolution)。

## 多平台协同

插件按管理名单同时注册。Agent 可通过研究工具跨平台检索候选，并自行判断共同外部依据、平台特殊规则
或事件完全无关的情况。订单仍分别交给每个平台传输，动作和服务器结果按 `platform` 关联到同一决策
审计体系。
