# 决策台账、管理界面与插件管理

这里的"审计"指的是**决策台账**——机器人每次判断留下的可追溯记录，以及管理界面自己的 HTTP 接口。
系统**不记录也不审计出站 HTTP 请求**：没有请求日志、没有 HTTP 层拦截器。要约束机器人对外发出什么请求，
只有两条途径，见 [两类过滤插件](RISK_FILTERS.md)：

| 途径 | 管什么 |
| --- | --- |
| 业务风控插件 | 每一次市场 API 调用，含只读 |
| Agent 行为风控插件 | 模型发起的每一次工具调用，其中一部分会发出 HTTP 请求 |

运行：

```bash
.venv/bin/prediction-market-agent serve
```

本地默认打开 `http://127.0.0.1:8765`；容器部署可在程序配置中监听 `0.0.0.0`。

## 决策可解释性

专用 `decision_ledger` 每行代表一次 outcome 决策，并用 `decision_id` 关联 Provider 轮次、Agent 工具步骤
和执行动作。页面的“决策台账”拉通显示：

- 当时的标准化市场、盘口、持仓、API 能力和策略版本；
- 每个研究工具的参数、结果与错误；
- 模型原始输出和结构化提案；
- Agent 行为风控的允许、缩减、拒绝或停止原因（`risk_decision` 字段）；
- 业务风控的拒绝理由：以执行失败的形式出现在 `execution` 里，状态为 `EXECUTION_ERROR`，原因字符串保留
  插件给出的理由。它**不写入** `risk_decision`——该字段只记 `agent:actions` 一路；
- 额度不足导致没下单时，动作层单独记为 `BUY_REJECTED`，`result.status` 为 `RISK_REJECTED`；
- 最终动作及远端执行响应/失败；
- 同 token 后续决策快照中的初始/最新 midpoint 与变化，或明确显示尚无后续观察。

可按 platform、provider、status、action 筛选，便于比较 Provider、策略文件哈希、风控调整和后续市场表现，
持续优化策略插件。原始完整记录仍可单独查询，不会只保留页面摘要。

浏览器中的逻辑操作仍使用 `/api/summary`、`/api/decisions`、`/api/records`、`/api/manifest`、
`/api/plugins/manage`、`/api/runtime` 和 `/api/settings` 等稳定名称，但不会直接发这些明文 URL 请求。登录后页面把逻辑 URL、
参数和正文一起放进 AES-GCM 信封，统一提交到 `POST /api/secure`，服务端解密分派后再加密响应。
回环或明确私网 IP 地址页面则通过 `POST /api/local` 发送明文 JSON `{ "url": "/api/settings", "body": null }`，无需会话、
Passkey 或加解密；写操作使用相同封装并在 `body` 中提供字段。两条通道复用同一业务分派逻辑。

## 插件管理

页面动态管理七类插件。Decision Provider 集中显示在“模型服务”，其余六类显示在“插件中心”；禁用项只显示文件名和来源，启用项调用初始化后显示描述、存储说明及字段表单。
秘密不回显。程序字段、插件目录、启用状态和插件字段都支持保存及删除/恢复：

- `/api/settings`、`/api/settings/reset`
- `/api/plugins/selection`
- `/api/plugins/directories`、`/api/plugins/directories/reset`
- `/api/plugins/config`、`/api/plugins/config/reset`、`/api/plugins/config/delete`
- `/api/plugins/install`
- `/api/plugins/refresh`
- `/api/plugins/controls`、`/api/plugins/controls/action`
- `/api/plugins/config/choices`
- `/api/runtime`、`/api/runtime/control`
- `/api/auth/manage`、`/api/auth/passkeys/*`、`/api/auth/sessions/kick`

非本地地址访问的管理操作要求有效的 HttpOnly 会话 Cookie、会话请求 token 和 ECDH 派生密钥。保存配置时插件系统只调用
插件的 `save_callback` 或 `delete_callback`。刷新会卸载旧注册表并按磁盘最新状态重建。完整认证和信封协议
见 [AUTHENTICATION.md](AUTHENTICATION.md)。

“机器人运行控制”显示 AI 主链 readiness、每个平台自己报告的启动原因、暂停和 runtime/事件队列状态。
全局或逐平台暂停写入 `management_file` 并立即执行启停；恢复后只有插件报告可启动的平台会启动。安装新插件时，页面提交
类别、目标目录、名称和源码；服务端校验后写入，不覆盖已有文件，新文件默认禁用。
