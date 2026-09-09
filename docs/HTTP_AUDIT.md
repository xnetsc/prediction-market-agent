# 决策台账、HTTP 审计与插件管理

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
- 风控的允许、缩减、拒绝或停止原因；
- 最终动作及远端执行响应/失败；
- 同 token 后续决策快照中的初始/最新 midpoint 与变化，或明确显示尚无后续观察。

可按 platform、provider、status、action 筛选，便于比较 Provider、策略文件哈希、风控调整和后续市场表现，
持续优化策略插件。原始完整记录仍可单独查询，不会只保留页面摘要。

浏览器中的逻辑操作仍使用 `/api/summary`、`/api/decisions`、`/api/records`、`/api/manifest`、
`/api/plugins/manage` 和 `/api/settings` 等稳定名称，但不会直接发这些明文 URL 请求。登录后页面把逻辑 URL、
参数和正文一起放进 AES-GCM 信封，统一提交到 `POST /api/secure`，服务端解密分派后再加密响应。

## 插件管理

页面动态展示六类插件。禁用项只显示文件名和来源；启用项调用初始化后显示描述、存储说明及字段表单。
秘密不回显。程序字段、插件目录、启用状态和插件字段都支持保存及删除/恢复：

- `/api/settings`、`/api/settings/reset`
- `/api/plugins/selection`
- `/api/plugins/directories`、`/api/plugins/directories/reset`
- `/api/plugins/config`、`/api/plugins/config/reset`、`/api/plugins/config/delete`
- `/api/plugins/refresh`
- `/api/auth/manage`、`/api/auth/passkeys/*`、`/api/auth/sessions/kick`

管理操作要求有效的 HttpOnly 会话 Cookie、会话请求 token 和 ECDH 派生密钥。保存配置时插件系统只调用
插件的 `save_callback` 或 `delete_callback`。刷新会卸载旧注册表并按磁盘最新状态重建。完整认证和信封协议
见 [AUTHENTICATION.md](AUTHENTICATION.md)。
