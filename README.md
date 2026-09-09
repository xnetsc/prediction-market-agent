# 多平台预测市场 Agent 交易机器人

这是一个可连接真实市场、由 Agent 自主收集证据并给出结构化决策的通用机器人。内置 Binance
Prediction 与 Polymarket API 插件，以及 Codex、Claude、OpenAI-compatible Provider 插件。多个
市场平台共享同一 Agent 决策与交叉验证流程，但各自保留能力清单、账户、凭证、URL、代理、网络规则和
执行配置。

通用内核没有线上/模拟选择：产生写动作时一定调用启用 API 插件的线上传输。内核不内置本金、止损、
网络白名单或具体交易策略；API 插件决定自己的传输与网络规则，风控插件决定账户和动作约束，策略插件
决定候选筛选与系统决策文本。没有规则插件适用于某个目标时，通用协调器不额外增加允许或拒绝政策。
当前运维值都在各插件私有 JSON 中；API 凭证权限变化可能改变真实请求结果，运行前应检查管理界面中的
插件配置与能力清单。

## 核心能力

- 自动扫描 `api`、`decision_provider`、`decision_strategy`、`research_tool`、`risk`、`hook` 六类插件。
- 插件系统保存有序启用名单；禁用插件不导入、不初始化，启用、禁用、删除后刷新均执行完整卸载生命周期。
- 插件初始化函数可返回动态字段 schema、插件自有 JSON 读取/保存回调与卸载回调；每个字段必须有说明，
  可选必填、默认值、枚举或秘密类型。
- Binance 和 Polymarket 通过同一个标准化 API 契约提供各自真实具备的行情、能力和写工作流。
- Codex/Claude/兼容 API 按列表优先级自动故障转移；任何 Provider 都使用相同的多步工具 Agent。
- 研究工具由插件动态贡献，工具名会动态进入 Agent 控制 schema；内置网页、URL、跨市场、行情刷新、
  K 线和历史召回工具集。
- SQLite 保存每轮完整输入输出、Agent 工具轨迹、风险判定、执行请求/结果以及专门的决策台账。
- Web 界面强制首次注册 admin Passkey；每次登录把 P-256 ECDH 参数绑定进 WebAuthn challenge，登录后的
  业务请求与响应使用 AES-GCM 会话密钥加密，并可管理 Passkey 与设备会话。
- Web 界面可筛选查看“上下文 → 证据 → 模型提案 → 风控调整 → 最终动作 → 执行 → 后续盘口”，并查看、
  修改、删除全部程序配置、插件目录、插件启用状态和动态私有配置。
- quote、order、fill、cancel、redeem、transfer、Agent tool/decision 均有 before/after Hook。

## 安装

```bash
cd /path/to/prediction-market-agent
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple .
.venv/bin/prediction-market-agent init
```

wheel 是机器人的完整安装包。安装后唯一的程序入口是 `prediction-market-agent`；也可以用等价入口
`python -m prediction_market_agent`。`init` 在当前工作目录创建应用配置和插件启用状态，后续命令默认读取
`config/application.json`，也可通过 `--config /path/to/application.json` 指定。

插件私有配置由插件回调读写；内置插件使用 `config/plugins/*.json`。运行配置和私有配置均已被
版本控制忽略；可从 `examples/` 中的脱敏样例开始，禁止提交真实密钥、钱包材料或签名文件。

## 使用

安装后先启动管理界面：

```bash
.venv/bin/prediction-market-agent doctor
.venv/bin/prediction-market-agent serve
```

管理界面默认位于 `http://127.0.0.1:8765`。首次访问必须注册 admin Passkey；线上部署必须使用 HTTPS。
完成配置后，在另一个终端运行一次决策或启动持续机器人：

```bash
.venv/bin/prediction-market-agent once
.venv/bin/prediction-market-agent run
```

其他运维入口：

```bash
.venv/bin/prediction-market-agent provider-test
.venv/bin/prediction-market-agent status
.venv/bin/prediction-market-agent report --since-hours 12
```

插件配置保存后，管理服务会刷新插件实例；独立运行的
交易进程需重启才能采用新实例。

## 文档与例子

- [功能、文档、例子与测试证据矩阵](docs/FEATURE_EVIDENCE.md)
- [架构与插件边界](docs/ARCHITECTURE.md)
- [配置参考](docs/CONFIGURATION.md)
- [安装、启动与部署](docs/DEPLOYMENT.md)
- [插件系统与生命周期](docs/PLUGIN_SYSTEM.md)
- [API 插件](docs/API_PLUGINS.md)
- [Provider、策略与研究工具](docs/DECISION_PLUGINS.md)
- [风控与 Hook](docs/RISK_AND_HOOKS.md)
- [决策台账与 HTTP 界面](docs/HTTP_AUDIT.md)
- [管理员 Passkey、ECDH 与加密会话](docs/AUTHENTICATION.md)
- [集成测试](docs/INTEGRATION_TESTS.md)
- [时区信息延迟案例研究](docs/STRATEGY_RESEARCH.md)
- [敏感信息与发布安全](SECURITY.md)
- `examples/`：六类插件、动态 Python 规则、插件 JSON 和插件目录配置的完整例子。

## 验收命令

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_api_integration.py
```

生产集成矩阵会向 Binance 发送真实 quote/order/cancel/redeem/双向 transfer 请求，并向 Polymarket
写端点发送真实未认证请求；使用无效资源标识和极小金额，当前预期是服务器拒绝，本地提前失败不算通过。
它没有执行模式或测试跳过开关。API 权限或远端行为改变前应重新复核这些测试输入。历史测试和模型输出
都不构成收益保证。

## 许可

本项目以 [PolyForm Noncommercial License 1.0.0](LICENSE) 公开源代码：许可证允许其定义范围内的
非商业用途，但商业用途不在免费授权范围内。任何商业使用、商业产品或服务集成、收费部署或其他预期
商业应用，均须事先取得仓库所有者的单独书面商业授权。

由于该许可限制商业用途，本项目属于 source-available 软件，不是 OSI 定义下的开源软件。具体权利、
义务和例外以 `LICENSE` 正文为准。
