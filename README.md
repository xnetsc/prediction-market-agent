# 多平台预测市场 Agent 交易机器人

这是一个可连接真实市场、由 Agent 自主收集证据并给出结构化决策的通用机器人。内置 Binance
Prediction 与 Polymarket API 插件，以及 Codex、Claude、OpenRouter Provider 插件。多个
市场平台共享同一 Agent 决策与交叉验证流程，但各自保留能力清单、账户、凭证、URL、代理和
执行配置。

实盘模式下，产生写动作时一定调用启用 API 插件的线上传输；默认关闭的纸面交易模式只在最终写传输处
模拟成交，市场、报价、模型、过滤和结算判断仍使用真实链路，并使用独立账户镜像。内核不内置实盘本金、
止损或具体交易策略；API 插件决定自己的传输，过滤插件决定账户和动作约束，策略插件
决定候选筛选与系统决策文本。没有规则插件适用于某个目标时，通用协调器不额外增加允许或拒绝政策。
当前运维值都在各插件私有 JSON 中；API 凭证权限变化可能改变真实请求结果，运行前应检查管理界面中的
插件配置与能力清单。

## 核心能力

- 自动扫描 `api`、`decision_provider`、`decision_evaluator`、`decision_strategy`、`market_discovery`、
  `research_tool`、`agent_policy`、`risk` 八类插件。
- API 平台插件各自拥有定时扫描、成功间隔和失败退避，但只决定**什么时候**扫。**扫什么**由通用发现引擎
  和发现策略决定：逐页扫描、可选 typed evaluator 先做低成本粗筛，高置信自洽时直接控制续扫，其余交给
  LLM 复核，再由 Agent 从压缩后且保留质量抽样的候选中挑出真正值得分析的标的。候选数不再由旧业务配额
  截断，只保留页数、时间、调用数等资源保护。扫描结果
  进入通用事件循环，再统一执行跨平台调研、Agent 决策、风控和线上动作。
- 插件系统保存有序启用名单；禁用插件不导入、不初始化，启用、禁用、删除后刷新均执行完整卸载生命周期。
- **策略会自我进化。** 发现策略和决策策略都自带内置默认版本，并按机器人自己已完成决策的实测结果持续
  调整——发现层分别测真实买卖成交活动和可观测成本后的已结算净收益，决策层测校准度“声明 70% 的那些
  标的实际赢了几次”。提案、风控拒绝和失败订单不冒充成交，未知收益也不按零收益处理。
  详见下方[自我进化的策略](#自我进化的策略)。
- 决策策略是可选增强；不安装插件时由内置策略工作，不阻止主链启动。
- 插件初始化函数可返回动态字段 schema、插件自有 JSON 读取/保存回调与卸载回调；每个字段必须有说明，
  可选必填、默认值、枚举或秘密类型。
- Binance 和 Polymarket 通过同一个标准化 API 契约提供各自真实具备的行情、能力和写工作流。
- Codex/Claude/OpenRouter 自动故障转移，并且**按失败类型退避、恢复后自动回到轮换**：限流等一个配额窗口，
  掉线只等几十秒，不会每次决策都为同一个故障再撞一次墙。可用的服务按实测质量排序——送达率、已结算
  校准（Brier）、以及由**另一个**服务给出的评分；模型给自己打的分不计入。任何 Provider 都使用相同的
  多步工具 Agent。故障转移只在用户已启用的 Provider 之间进行；每个 Provider 始终使用该服务中用户选定的模型，
  不会为求可用而暗中换型号。Codex/Claude 留空是用户明确选择客户端默认；OpenRouter 不允许留空。
- 当前随仓库提供并默认选择的 `jev` evaluator 插件只负责发现阶段的低成本粗筛：选 Jev 时直接向 OpenRouter Decisions
  发送 `state/questions`；选择其它 OpenRouter 模型时，只显示明确支持 structured output 的型号并强制
  `answers` JSON schema；自定义方式也可接普通聊天模型，先使用其原生 JSON schema，端点拒绝或忽略约束时
  再由该 evaluator 插件的兼容层改用强制函数参数承载同一 schema。三条路径都自行解析 Choice、
  Score、Noul，不通过 Agent。它压缩交给发现 LLM 的候选 JSON，但不参与逐标的交易决策、不复核提案、
  不生成交易动作；0.90 起始置信阈值按质量抽样结果动态校准且不低于 0.80。Jev 模型原生只接受这套协议，
  普通聊天模型则由插件约束成同一协议。
  OpenRouter 方式自动使用官方 Base URL 并默认复用主 OpenRouter Key；
  自定义方式填写自己的 Base URL、模型名和可选 Key。代理始终属于 Jev 插件自己。
- 另有独立的 `laya` 本地 WebGPU evaluator 插件，默认不启用。先在模型服务页测试连接，再到插件中心的
  “决策评估器”启用并保存服务地址；它与 Jev 可分别选择，不会进入最终交易决策 Provider 池。
- 预测市场研究工具由插件动态贡献，工具名会动态进入 Agent 控制 schema；内置跨市场、行情刷新、K 线和
  业务历史查询工具集。网页、文件、命令和 skills 使用官方 CLI 自带能力。
- SQLite 保存每轮完整输入输出、Agent 工具轨迹、风险判定、执行请求/结果、独立采集证据、运行异常、
  决策删除审计，以及成交/结算/充提构成的不可变盈亏事件台账。充提只算外部资金流，不冒充盈利；缺失
  成本或盘口标记保持未知，不按零计算。
- 可选纸面交易使用用户明确填写的模拟金额，按平台真实报价和费率在本地立即成交；所有模拟余额、订单和
  台账都标记为 simulated，并与实盘账户镜像分开。它不模拟排队、部分成交或滑点，不能替代实盘验收。
- Web 界面通过回环或明确的局域网私有 IP 访问时无需认证或加解密（地址范围见[认证说明](docs/AUTHENTICATION.md)）；其他主机首次访问注册 admin Passkey，每次登录把 P-256 ECDH 参数绑定进 WebAuthn challenge，登录后的
  业务请求与响应使用 AES-GCM 会话密钥加密，并可管理 Passkey 与设备会话。
- Web 界面可筛选查看“上下文 → 证据 → 模型提案 → 风控调整 → 最终动作 → 执行 → 后续盘口”，并查看、
  修改、删除全部程序配置、插件目录、插件启用状态和动态私有配置；也可安装新插件、暂停全部机器人或
  单独暂停某个平台。
- 决策页单独展示不会随决策删除而消失的市场采集、评估器粗筛、候选选择和降级异常；盈亏账本按平台、
  实盘/纸面和币种分列总览、开放仓位与逐笔事件，不混加不同币种或账户模式。
- 独立资金管理页按平台展示真实可用余额、钱包/充值/转出等常驻操作、待处理资金请求和精确路线；常驻操作
  不冒充顶部待处理事件。Polymarket 从官方 Bridge
  实时读取资产全名、缩写、网络、chain id、合约与最低金额；网络和代币绑定选择，部分到账只交回决策
  一次，由它选择继续、等待补足或继续并补足。
- 模型服务页统一管理客户端、一个或多个 OpenRouter 配置、启用顺序与模型服务扩展；OpenRouter 卡片可直接
  选择自动、Codex CLI 或 Claude CLI；插件中心展示其余七类能力，附用途与流程
  提示；决策先显示结论与原因，技术详情按需展开。操作步骤见 [Web 控制台](docs/WEB_UI.md)。
- 全系统只有两类插件会拒绝框架业务动作：`agent_policy` 管 LLM 通过框架协议发起的业务工具，`risk` 管
  一切市场 API 动作（含只读）。每一类都可同时启用多个并串成一条链。Codex/Claude 自带的搜索、文件、
  命令、skills/插件由官方 CLI 权限、sandbox 与 rules/hooks 管理，本框架不会虚假宣称已经拦截。

## 自我进化的策略

大多数交易机器人的提示词写完就定死了。这个机器人的两条策略链路都会**根据自己的历史结论持续改写**，
而且改的方式经得起追问。

**测什么**

| 链路 | 衡量的问题 | 数据来源 |
| --- | --- | --- |
| 标的发现 | 是否产生真实 BUY/SELL 成交；已结算后净收益是多少 | 精确关联的发现、决策、执行与赎回台账 |
| 交易决策 | 声明 70% 的那些标的，实际结算赢了几次 | 已结算赎回记录 |

**怎么防止它从噪声里学出迷信**

- **统计为准，模型不写经验。** 教训条款由统计模板生成，每一句都带桶名和样本数，可以逐条追回原始记录。
  让模型总结几笔交易，得到的是听起来很有道理的过拟合。
- **样本门槛 + 向基线收缩。** 桶样本不足不予保留；权重按样本量向基线收缩并封顶，一段短期连胜不会把
  权重打满。
- **偏离消失自动淘汰。** 不再成立的条款会被退役，长期未复证的会过期。
- **硬闸不可学。** 成本、结算风险、不可交易的临近截止时间和关闭状态写在内置内核里，任何学到的条款
  都不能放松；偏好的揭标天数不是硬上限。

**你的策略不会被改写**

进化是一层可随时关闭的叠加，不是对你文件的编辑。关掉开关，模型看到的就只有你写的原文，一个字不差。
内置策略没有配置面，但**当前真正发给模型的全文随时可以导出**——内核、先验权重及其样本数、生效的教训
条款、完整测量表。你用自己的插件期间，内置策略仍在被测量，导出里单独列出它的状态，所以切回来时它
不是旧的。

每个发现、分析和决策轮次都使用一个新的官方 Codex/Claude CLI 会话，同一轮的后续业务工具结果续接该
会话。框架不替 CLI 总结或裁切历史：首条输入给出统一业务账本、Codex 与 Claude 双方私有会话记录的绝对
路径，以及应续接的上一轮 ID，由 CLI 用自己的文件、命令、搜索、skills/插件能力决定读取哪些前置信息。
这些路径从 `CODEX_AUTH_DIRECTORY` / `CLAUDE_AUTH_DIRECTORY` 显式解析，不依赖服务进程默认 HOME；若旧的
默认目录仍含记录也一并列出。OpenRouter 运行所选 CLI 时使用同一私有 HOME，因此账号配置、skills 和会话
位置不会因模型改走 OpenRouter 而漂移。框架只注入预测市场业务工具、schema、策略和风控约束，并校验、
审计 CLI 返回的结构化动作。

## 快速安装与启动

新手请直接看 [上手指南](docs/GETTING_STARTED.md)：从零到它开始写决策记录，一步一步，每步都写了做完该看到什么。

普通用户不需要安装 Python、创建 venv 或构建源码。先取得仓库中的启动脚本：

macOS/Linux：

```bash
git clone https://github.com/xnetsc/prediction-market-agent.git
cd prediction-market-agent
./start-local.sh
```

Windows PowerShell：

```powershell
git clone https://github.com/xnetsc/prediction-market-agent.git
Set-Location prediction-market-agent
.\start-local.ps1
```

脚本会检查并按需安装、启动 Docker，然后拉取
`ghcr.io/xnetsc/prediction-market-agent:latest`，启动完整机器人并等待健康检查通过。应用数据保存在仓库的
`runtime-data/`，升级镜像不会删除配置、登录凭据或 SQLite 台账。启动完成后直接访问
`http://127.0.0.1:8765`；以后再次启动只需重新运行同一个脚本。

每次 Git 推送都会触发 GitHub Actions 构建并发布 amd64、arm64 镜像。Docker 安装、代理检测、停止、升级、
端口修改和云部署说明见 [部署文档](docs/DEPLOYMENT.md)。

## 使用

管理控制台分为运行概览、决策账本、盈亏账本、资金管理、模型服务、插件中心、程序设置、安全与会话。支持手机和平板，
窄屏使用抽屉导航，宽表局部横向滚动，插件配置按需展开。操作说明见 [Web 管理界面](docs/WEB_UI.md)。
“程序设置”可查看服务端环境详情、对比直连与统一继承代理访问相同目标时的公网出口，并单独查询环境代理/插件路径的 IP 与可选源端口，
用于辅助核对平台 IP 白名单；不保证目标平台使用同一出口。[环境诊断与配置例子](docs/ENVIRONMENT.md)。

一键脚本已经在容器中启动管理界面和机器人运行主管，无需再运行 `serve`。管理界面默认位于
`http://127.0.0.1:8765`，回环或明确的私网 IP 访问无需 Passkey 或应用层加解密。
公网访问必须使用 HTTPS 和 admin Passkey。客户端登录向导会验证本次实际回调映射，匹配后免助手；
本地容器一键脚本会临时发布客户端本次回调端口，流程结束后释放，无需另下载助手。端口跟随官方客户端，
不在机器人中写死；Codex 当前客户端的端口限制见下述说明。
模型、代理、OpenRouter 配置、Jev Key 复用和升级见[客户端账号说明](docs/CLIENT_ACCOUNTS.md)。
手机/远程登录支持 Codex 设备码、Claude 官方验证码，不需要手机助手或额外公网端口。
远程容器无宿主机快照时，客户端插件自行检测当前环境代理；也可使用 ENVIRONMENT 显式忽略快照。
启动器会检测宿主机系统代理，并作为统一代理默认供 Codex、Claude、OpenRouter、Jev、平台和研究插件继承；
每个插件仍可在 UI 单独选择直连或自己的代理。
[检测范围与转发说明](docs/HOST_PROXY.md)。
需要远程助手时，向导提供 Bash/Python 或 PowerShell 的一次性命令和复制按钮；完整脚本经 SHA-256
校验后才执行，不提供 ZIP/原生可执行文件下载，不改变系统安全策略。
`serve` 同时承载管理界面和运行主管：至少一个 AI Provider 与一个平台插件报告可启动后，自动启动相应
平台自己的事件循环；策略、研究和两类过滤插件是可选增强。通用框架不检查插件字段，只读取插件
readiness 与 runtime 启动结果；
配置不全的平台保持停止并显示原因。配置保存、启用/禁用、刷新或暂停会立即重新评估，无需另开 Worker。
需要从终端检查状态、测试 Provider 或显式触发一次扫描时，在仓库目录执行：

```bash
docker compose exec robot prediction-market-agent doctor
docker compose exec robot prediction-market-agent provider-test
docker compose exec robot prediction-market-agent status
docker compose exec robot prediction-market-agent report --since-hours 12
docker compose exec robot prediction-market-agent once
```

查看日志、停止机器人或拉取最新版并重启：

```bash
docker compose logs -f robot
docker compose down
./start-local.sh
```

Windows 更新时将最后一条替换为 `.\start-local.ps1`。`once` 是明确的人工/外部单次触发，不创建后台
定时器。容器已经运行 `serve`，不要再对同一份数据启动第二个 `serve` 或 `run` 实例。

## 文档与例子

- [上手指南：从零到它开始做决策](docs/GETTING_STARTED.md)
- [功能、文档、例子与测试证据矩阵](docs/FEATURE_EVIDENCE.md)
- [架构与插件边界](docs/ARCHITECTURE.md)
- [配置参考](docs/CONFIGURATION.md)
- [安装、启动与部署](docs/DEPLOYMENT.md)
- [插件系统与生命周期](docs/PLUGIN_SYSTEM.md)
- [API 插件](docs/API_PLUGINS.md)
- [Provider、策略与研究工具](docs/DECISION_PLUGINS.md)
- [两类过滤插件](docs/RISK_FILTERS.md)
- [决策台账与管理界面](docs/CONSOLE_AND_LEDGER.md)
- [模型客户端账号、回调与 OpenRouter](docs/CLIENT_ACCOUNTS.md)
- [宿主机代理检测与转发](docs/HOST_PROXY.md)
- [运行环境与公网出口诊断](docs/ENVIRONMENT.md)
- [管理员 Passkey、ECDH 与加密会话](docs/AUTHENTICATION.md)
- [集成测试](docs/INTEGRATION_TESTS.md)
- [时区信息延迟案例研究](docs/STRATEGY_RESEARCH.md)
- [敏感信息与发布安全](SECURITY.md)
- `examples/`：各类插件、两类过滤插件各自的 Python 规则脚本、插件 JSON 和插件目录配置的完整例子。

## 源码开发与测试（可选）

下面只适用于需要修改源码、构建 wheel 或运行测试的开发者；普通安装和运行不使用 venv：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --index-url https://pypi.org/simple -e .
.venv/bin/prediction-market-agent init
```

wheel 是完整机器人安装包。安装后入口为 `prediction-market-agent`，也可使用等价入口
`python -m prediction_market_agent`。`init` 在当前工作目录创建应用配置和插件启用状态；其他命令默认读取
`config/application.json`，也可通过 `--config /path/to/application.json` 指定。

开发验收命令：

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_api_integration.py
```

生产集成矩阵不读取 `paper_trading`，会直接实例化 API 插件，向 Binance 发送真实
quote/order/cancel/redeem/双向 transfer 请求，并向 Polymarket
写端点发送真实未认证请求；使用无效资源标识和极小金额，当前预期是服务器拒绝，本地提前失败不算通过。
它没有纸面交易分支或测试跳过开关。API 权限或远端行为改变前应重新复核这些测试输入。历史测试和模型输出
都不构成收益保证。

插件私有配置由插件回调读写；内置插件使用 `config/plugins/*.json`。运行配置和私有配置均已被版本控制
忽略；可从 `examples/` 中的脱敏样例开始，禁止提交真实密钥、钱包材料或签名文件。

## 许可

本项目以 [PolyForm Noncommercial License 1.0.0](LICENSE) 公开源代码：许可证允许其定义范围内的
非商业用途，但商业用途不在免费授权范围内。任何商业使用、商业产品或服务集成、收费部署或其他预期
商业应用，均须事先取得仓库所有者的单独书面商业授权。

由于该许可限制商业用途，本项目属于 source-available 软件，不是 OSI 定义下的开源软件。具体权利、
义务和例外以 `LICENSE` 正文为准。
