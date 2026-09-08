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

## Binance

插件读取 Prediction 主题、详情、盘口和 Spot 参考 K 线；写端实现 quote、BUY、SELL、CANCEL、
REDEEM、TRANSFER_IN、TRANSFER_OUT。私有 JSON 拥有 REST URL、Key/Secret、预测钱包信息、资金
账户、滑点、代理和网络规则。插件不会用本地“参数不齐”预检代替服务器验证；配置会原样进入签名请求，
服务器拒绝会记录为执行错误。

## Polymarket

插件使用 Gamma、CLOB、Data 和 Relayer/RPC，写端基于官方 `polymarket-client`：

- 限价/市价 BUY、SELL；
- authenticated cancel；
- 从可赎回持仓映射 token 到 condition 后提交赎回；
- pUSD OUTBOUND 转账。

私有 JSON 拥有所有端点、链、钱包私钥、CLOB L2、funder、用户/Builder Relayer、Builder Code、转出
地址、代理和网络规则。当前适配器不能可靠判定 winner，因此能力清单明确把 `settlement_status` 设为
false；这不是占位成功值。

官方参考：[Python SDK](https://github.com/Polymarket/py-sdk)、
[Gasless 交易](https://docs.polymarket.com/trading/gasless)、
[市场结算](https://docs.polymarket.com/concepts/resolution)。

## 多平台协同

插件按管理名单同时注册。Agent 可通过研究工具跨平台检索候选，并自行判断共同外部依据、平台特殊规则
或事件完全无关的情况。订单仍分别交给每个平台传输，动作和服务器结果按 `platform` 关联到同一决策
审计体系。
