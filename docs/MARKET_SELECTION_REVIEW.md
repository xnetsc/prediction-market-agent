# 预测市场选标、盘口与粗筛复核（2026-09-24）

## 这次现场记录能证明什么

只读检查本机保留的 Polymarket 决策台账：最近 8 个完成的发现轮次（ID 840–847）各扫描了 40–188 个事件，却各只对 3–16 个事件调用了 `VERIFY_TOPICS`；合计 51 次核验、41 个不同事件。所有被核验事件都有多个开放合约，有的达 318 个，但旧工具每个事件只读取按目录流动性排序的首个开放合约的首个结果。ID 843 的“已核验盘口均因价差、流动性或剩余时间未通过门槛”仅描述了该轮抽查，**不是整个 Polymarket 的结论**。ID 845 同期还选出三个赛事候选，抽查价差约 2.60%–4.55%；“被选中”也不等于“可盈利或已成交”。

这解释了反复出现同类总结的一个系统性来源：事件级流动性、单个合约的盘口和整个事件的可交易性被混读。代码现在把抽查合约 ID、结果、开放合约数、未核验数、近端档位及数量分开记录；多合约事件要用 `TOPIC_DETAIL` / `OUTCOME_BOOK` 核对真正相关的合约，不能因一个边缘比分盘没有双边报价而宣判整场事件失败。每次仍只自动抽查一个合约，避免同步读取数百盘口造成新延迟。

## 选标与决策口径

1. **粗筛只分配研究注意力。** 仅有目录标题、事件流动性和历史反馈时，缺盘口、结算条款或公平概率应是 `NEEDS_DATA` 或 `DEFER`，不应据此高置信 `REJECT` 整个事件。样本不足的历史反馈不改提示；达到样本门槛后才给有边界的校准意见。后续 HOLD 是研究成本代理，不是已实现亏损；未进入决策的候选存在选择偏差，保留安全抽样复查假阴性。
2. **先找可检验的错价来源，再算交易。** 候选线索包括有明确发布时间的外部事实、条款误读、相关合约不一致、与另一平台的同义合约价差。价格低于 0.10、成交量大或“概率相加不等于 1”本身不构成优势。结构性机会须先确认互斥且穷尽、同等结算、全部腿可按同等数量成交并扣除费用。[市场流动性与效率研究](https://business.columbia.edu/faculty/research/liquidity-and-prediction-market-efficiency)、[预测市场校准与到期时间研究](https://academic.oup.com/ej/article-abstract/123/568/491/5079498) 都不支持跨品类套用一个固定极端赔率规则。
3. **用真实执行路径算成本。** Polymarket 展示价可能是中间价，也可能在大价差时改显最近成交价；买入用 ask、卖出用 bid，按目标数量逐档计算。当前 ask 买入、当前 bid 卖出合计跨过**一整个**当前价差，不是两个。持有到结算没有卖出盘口成本，但仍有入场费、结算风险和资金占用；提前退出需估计未来卖出盘口及费用。[官方价格与盘口说明](https://docs.polymarket.com/concepts/prices-orderbook)、[下单机制](https://docs.polymarket.com/trading/place-orders)。
4. **逐市场看时间和费用。** 市场自己的 `endDate`、事件 `endDate`、是否接受订单及实际判定/赎回时间不能混为一谈；偏好的几天内揭标不是硬性截止。Polymarket 部分市场按价格和类别收动态 taker 费，不能把现有适配器的 `fee_bps=0` 解释成“本单免手续费”。代码已将市场是否收费及原始费率表传给研究/决策上下文；真实成交费用和内部账本的精确对账仍需以平台成交回报为准，当前零费率记账**尚不代表真实净收益**。[官方市场细节](https://docs.polymarket.com/market-data/market-details)、[官方费率](https://docs.polymarket.com/trading/fees)。Kalshi 盘口只公开 YES/NO 买单，另一侧卖价由互补价推导；这也说明不能跨平台硬套原始盘口数组解释。[Kalshi 官方盘口说明](https://docs.kalshi.com/getting_started/orderbook_responses)。
5. **短规则常驻，细则按需取。** 发现与决策的核心提示词只列工具索引，业务工具 `READ_MARKET_PLAYBOOK(section)` 按需返回 `selection`、`execution`、`resolution`、`structure`、`feedback` 或 `field_notes` 一段，并随模型工具轨迹留痕。粗筛反馈是有样本门槛的有界指导，不会自行改动下单风控；真正调门槛应先比较回放/留出样本上的假阴性、实际成交与已结算净收益。

## 个人复盘与社交帖如何使用

以下均是**一手陈述或自有数据分析，不是平台保证**。可信度取决于是否披露样本、成交、费用、负例、复现代码和利益关系：

- 一位交易者公开 5 分钟盘回测，称理想成交下偶见盈利，但真实下单的未成交、部分成交、滑点与 maker 逆向选择可吃掉优势；值得检验的是“理论价差 vs 实际成交”，不能照抄其具体胜率或策略。[原帖](https://www.reddit.com/r/Polymarket/comments/1un85mg/i_spent_7_months_testing_every_strategy_on/)。另有交易者报告模拟盈利但实盘 FAK/FOK 成交不足，属于未独立核实的延迟假设。[原帖](https://www.reddit.com/r/Polymarket/comments/1twrp8y/750_a_day_on_paper_but_low_fill_rate_live/)。
- 一个 Kalshi LLM 交易机器人复盘称含糊提示词曾被误当硬止损，提示词更严也未带来信息优势，最终披露负 P&L；这支持“明确硬规则/偏好、不要靠更长推理捏造 edge”，不能证明所有模型或市场都不可交易。[复盘](https://leeharden.com/posts/kalshi-trading-bot-after-action)。另一位做市者公开选中宽价差/难退出组合盘等自身程序错误，提醒要对方向、盘口和退出可行性写断言。[复盘](https://rlafuente.com/posts/2025-3-5-i-lost-150-market-making-on-kalshi)。
- 一个长篇 Polymarket 复盘展示“高胜率复制钱包”在留出样本失效、模拟盈亏按官方结算重算后翻负的过程；作者同时推广自己开发的工具，因此只采纳可复验的核算/留出样本方法，不采纳其普遍盈利率结论。[复盘](https://williamokwach.substack.com/p/can-retail-actually-beat-prediction)。
- X 上的 [Telonex 钱包分析](https://x.com/telonex/status/2022251717270573513) 指出 maker/taker 盈亏差异，但只涵盖一周、按钱包归因、**明确未扣费用**，作者同时销售行情数据；它不构成当前机器人转做市的证据。[方法与局限](https://telonex.io/research/top-crypto-traders-polymarket-15m)。带夸大收益标题、课程/返佣/工具导流却不公布亏损和未成交的帖，及一面谴责推广一面推广自己服务的[示例](https://x.com/camolNFT/status/2032870128287687112)，不进入规则。

## 后续验收指标

按平台、市场家族和计划退出方式分别统计：扫描事件数、粗筛动作与置信度、实际核验合约覆盖率、被挑选候选的全决策比例、成交/未成交/部分成交、下单延迟、计划价对实际成交均价、费用、持有时间、官方结算净收益与安全抽样中的漏选。只在样本够、负例可查、不同时间段复测有效时，才改变评估器提示或阈值。对短周期“抢行情”的个人经验尤其要计入自身整轮模型延迟，否则纸面 edge 不可迁移。
