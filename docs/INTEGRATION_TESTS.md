# 测试与验收

完整测试默认包含生产联网，不使用跳过开关：

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q
```

当前矩阵覆盖：

- 配置字段说明、默认值、秘密保留和 JSON 存储约束；
- 六类示例插件全部扫描、初始化、工厂调用及卸载；
- 禁用不导入、启用/禁用、排序、手动刷新、删除文件后的卸载注销；
- Provider 多步工具 schema、优先级与运行时故障转移；
- 策略文件、动态工具、风控合并、网络规则、Hook 和在线执行网关；
- SQLite 会话、滑动召回、动作记录、决策台账与界面查询；
- Binance、Polymarket 各自生产读取和双平台同进程读取/决策；
- 真实生产写拒绝探测：Binance quote/order/cancel/redeem/双向 transfer，以及 Polymarket
  order/cancel/Relayer submit。探测必须收到服务器 HTTP/平台拒绝；若在本地提前失败，测试不通过。
- 架构契约：插件系统不得出现平台/凭证/策略/风控私有字段，运行时不得出现执行模式或本地写分支，测试套件
  不得出现 skip/xfail，决策账本和专用 UI 必须持续存在。

写拒绝探测使用无效测试资源标识和极小标准化金额，目的在于证明请求穿过插件签名/代理/网络规则并到达
服务器。当前无写权限凭证的拒绝是测试预期；权限或服务器行为改变会让测试失败并要求人工复核，而不会
被当成通过。

最近一次本地确定性测试结果：`70 passed, 0 skipped`。生产联网测试另行强制执行，结果为
`2 passed, 2 failed, 9 subtests passed, 0 skipped`：Polymarket 读取和三个写路由拒绝探测全部通过；
Binance 的两个用例在最先访问公开时间接口时收到官方主机 `HTTP 451`，因此按“不得把远端错误伪装成通过”
的约束保留为失败。该结果说明当前运行网络受 Binance 地域策略限制，不是本地跳过或模拟结果。
