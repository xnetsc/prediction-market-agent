# 决策台账、HTTP 审计与插件管理

运行：

```bash
.venv/bin/python -m prediction_paper_bot serve
```

打开 `http://127.0.0.1:8765`。服务只允许绑定回环地址。

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

读取接口：

- `GET /api/summary`、`GET /api/report`
- `GET /api/decisions?limit=50&offset=0&platform=&provider=&status=&action=`
- `GET /api/records?kind=turns|steps|actions&limit=50&offset=0&platform=`
- `GET /api/manifest`
- `GET /api/plugins/manage`

## 插件管理

页面动态展示六类插件。禁用项只显示文件名和来源；启用项调用初始化后显示描述、存储说明及字段表单。
秘密不回显。启用/禁用、优先级、当前策略、配置保存和刷新分别使用：

- `POST /api/plugins/selection`
- `POST /api/plugins/config`
- `POST /api/plugins/refresh`

写管理接口只更改本机插件状态，不调用交易 API，并要求页面启动时生成的同源 token。保存配置时 SDK
只调用插件的 `save_callback`。刷新会卸载旧注册表并按磁盘最新状态重建。
