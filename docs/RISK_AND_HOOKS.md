# 风控与 Hook

## 出厂不启用任何风控插件

`plugin_selection.default.json` 的 `enabled.risk` 是空的。装完即用的状态下**没有任何风控规则生效**：
没有仓位上限、没有止损、没有动作白名单。内核本身不附加政策，限制全部来自你自己启用并配置的插件。

这是有意的。默认塞一份没人选过、也没人配过的风控清单，只会让界面上出现看起来像防护的东西，而它既不是
你定的，也可能什么都不检查。要限制就自己选、自己配。

三个内置插件各自需要配置才会起作用：`portfolio_limits` 要 `TOTAL_CAPITAL`，`agent_actions` 要
`ALLOWED_TOOLS`，`dynamic_python` 要指向至少一个受信任的 `.py` 规则文件。

`dynamic_python` 在启用但未指定规则文件时会明确报告未就绪，理由写着“不施加任何限制”，而不是显示绿色的
已就绪——一个启用了却什么都不检查的风控，显示成已就绪是最容易误导人的状态。

## 通用协调器

内核只认识命名目标及四种结果：`ALLOW`、`ADJUST`、`REJECT`、`HALT`。它负责按目标分派并合并动态
规则的结果，不知道损益公式、金额、白名单内容或网络地址。没有启用规则适用于某目标时，协调器返回
`ALLOW`，表示“不增加政策”；是否限制该动作完全取决于已启用插件，而不是核心层默认拒绝。

## 内置风控插件

- `portfolio_limits`：插件自己的 JSON 定义资金分配、净结果公式所需阈值、单仓、账户敞口和最小订单；
  它提供恰好一个组合贡献，创建初始账户、账户规则和全局规则。用户原始指定的资本与亏损参数只存在于
  当前私有配置，不是内核或文档默认值。
- `agent_actions`：插件 JSON 定义 Agent 工具和交易动作白名单。
- `dynamic_python`：插件 JSON 指定受信任 `.py` 文件；每个文件导出
  `evaluate(operation, context)`，可拒绝、停止或缩减，异常失败关闭。
- 每个 API 插件还从自己的 JSON 构造 `network:<platform>` 规则，按 scheme、host、method、path 检查
  真实请求。

没有硬编码永久写拦截，也没有执行模式。规则允许的动作会进入线上传输；API 权限、签名和业务参数由
远端最终判定。`examples/risk_plugins/` 和 `examples/risk_rules/` 分别给出完整插件与动态脚本例子。

## Hook

标准 Hook 包含 Agent tool/decision、quote、order、fill、cancel、redeem、transfer 的 before/after。
`jsonl_audit` 插件可按自己的 JSON 选择事件和输出文件。启用时注册，禁用或刷新时逐项注销。Hook 可以
观察或记录，但不能绕过风控和 API 网络规则。
