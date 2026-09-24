# 配置中心

机器人不从 `.env` 读取运行参数。所有通用运行配置保存在最早加载的应用 JSON 中，默认位置为
`config/application.json`；启动命令可通过 `--config` 指定其他文件。第一次安装运行
`prediction-market-agent init` 会创建完整配置。文件不存在时使用程序内置默认值，管理界面保存后自动创建。
应用启动配置与机器人运行就绪是两个阶段：Web 服务可在插件选择或私有配置尚未完成时启动；机器人主链
只要求至少一个决策 Provider 可用，并且至少一个 API 平台 runtime 成功启动。策略、研究和两类过滤插件
均为可选增强。通用框架不按字段名判断插件私有配置，只调用插件自己的 readiness/start/status 回调。

启动顺序固定为：定位 `--config` → 校验全部应用字段 → 解析 `working_directory` → 把 `session_db`、
`auth_db` 等相对路径变成绝对路径 → 加载插件目录和启用状态 → 初始化 SQLite、插件和 Web 应用。数据库
功能不会在路径确定之前运行。界面修改数据库路径后必须重启，运行中不迁移或切换数据库。

## 管理界面

运行 `prediction-market-agent serve` 后，界面的“程序运行配置”列出每个字段的名称、说明、类型、默认值、
当前生效值及是否为用户覆盖值。界面支持：

- 保存全部运行配置；
- 删除单个覆盖值并恢复默认值；
- 删除整个应用配置文件并恢复全部默认值；
- 修改插件启用顺序和当前策略；
- 逐类别编辑插件扫描目录，或删除自定义目录文件恢复内置目录；
- 从界面把新插件源码安装到当前类别的配置目录；
- 全局暂停机器人，或单独暂停一个或多个 API 平台；
- 修改插件私有字段、删除单个字段值，或删除整个插件配置文件。

应用字段属于最早期启动配置，保存后重启生效，尤其不会运行中迁移数据库。插件启用名单、插件私有配置、
插件刷新和暂停状态保存后则立即停止旧实例、重新评估，并自动启动已经就绪的平台 runtime。

## 应用配置字段

| JSON 字段 | 用途 |
|---|---|
| `working_directory` | 所有相对配置和运行数据路径的基准目录 |
| `management_file` | 插件启用、禁用、优先级和当前策略文件 |
| `plugin_directories_file` | 八类插件扫描目录文件 |
| `state_file` | 账户镜像基准路径；多平台及纸面交易会自动使用彼此独立的后缀 |
| `session_db` | 会话、研究步骤、执行动作和决策台账 SQLite |
| `auth_db` | admin Passkey、公钥计数器和登录会话 SQLite |
| `admin_session_hours` | 连续无操作失效小时数，默认 72；有效请求会刷新闲置计时 |
| `admin_absolute_session_hours` | 从登录起的绝对有效上限，默认 168 小时（7 天） |
| `agent_language` | 模型写给人看的散文语言；不翻译 schema、枚举、ID、URL 或市场原文 |
| `paper_trading` | `off` 为实盘写传输；`on` 时只在最终写动作处本地模拟成交 |
| `paper_trading_funds` | 纸面账户初始模拟金额；只在纸面交易开启时生效，不代表平台余额 |
| `strategy_horizon_days` | 内置发现/决策策略偏好的揭标天数；是偏好，不是风控硬上限 |
| `strategy_max_trade_usdt` | 内置决策策略偏好的单笔金额；硬上限应由过滤插件实现 |
| `agent_max_tool_steps` | 每次最终决策前最多研究工具步骤 |
| `agent_tool_result_chars` | 单个工具结果进入上下文的字符预算 |
| `history_per_market` | 业务历史查询工具的默认返回条数；不会自动拼接上下文 |
| `discovery_max_scan_seconds` | 单批发现硬时限；正常停止由当前状态判断，不是候选配额 |
| `discovery_max_pages` | 单批发现硬分页保护；未处理状态可留到后续批次 |
| `decision_max_attempts` | 单批 outcome 评估硬保护；盘口读取失败不消耗一次尝试 |
| `decision_max_cycle_seconds` | 单批决策处理硬时限 |
| `shared_http_proxy` | 统一代理；支持联网的内置插件默认继承 |
| `shared_no_proxy` | 统一代理的绕过主机，回环地址始终自动加入 |
| `host_proxy_file` | 一键启动器写入的宿主机代理检测/转发信息文件 |
| `dashboard_host`、`dashboard_port` | 管理服务监听地址和端口 |
| `environment_probe_services` | 手动公网出口查询的 HTTPS 服务列表 JSON；空数组禁用 |
| `environment_probe_timeout` | 每个出口查询服务的超时秒数，范围 1–30 |

旧版 `dashboard_refresh_seconds` 已退役：控制台不再后台轮询。升级时旧 JSON 中该字段会被忽略，并在下次
保存任一程序设置时自动清除；它不会重新出现在界面或影响请求频率。

应用配置示例见 `examples/application.json`。通用 Config 只提供各插件可选择继承的统一网络代理，不包含
任何平台专属代理、平台 URL、API Key、私钥、实盘账户资金、扫描间隔、分页/每轮规模、失败退避、
Agent 动作策略或具体交易策略；这些只能由对应插件定义。通用配置中的 `paper_trading_funds` 只是假设的
纸面账户金额，不是任何平台的实盘资金。OpenRouter 的插件私有代理字段默认 `INHERIT`。

`shared_http_proxy` 支持 `HOST`、`ENVIRONMENT`、`DIRECT`、`SYSTEM`（仅原生 macOS）或完整 HTTP(S) URL。
平台、客户端和研究插件的私有代理字段默认 `INHERIT`；改成 `DIRECT` 或 URL 后只覆盖该插件。

## 插件扫描目录与启用状态

`plugin_directories_file` 指向包含八个固定类别的 JSON。每个类别是有序目录列表；文件不存在时先使用
`${WORKING_DIRECTORY}/plugins/<类别>` 的可写安装目录，再使用安装包内置目录。界面的“安装新插件”可选择
其中一个当前配置目录写入新 `.py` 文件；不覆盖同名文件，安装后保持禁用。示例见
`examples/plugin_directories.json`。

`management_file` 保存每类插件的有序启用名单、可为空的当前决策策略、全局暂停及逐平台暂停名单。禁用插件不会被
导入或初始化，因此只能在启用后显示和管理其动态私有字段。API/Provider/策略允许暂时为空；运行状态页会
显示全局或平台阻塞原因，补齐后自动激活。完整文件结构见 `examples/plugin_selection.json`。

`model_selection_mode` 默认 `QUALITY`：决策 Provider 和发现评估器各自在已启用且可用的实例中优先选实测质量较高者，同分按启用顺序。设为 `CONFIGURED` 时两类都强制按启用顺序、忽略质量。每次只调用一个实例，失败才尝试下一个；这个设置不会改变各 Provider 内已选的具体模型。

管理选择文件的 `screening_paused` 默认为 `false`，只控制持久化逐市场粗筛队列的自动消费；决策页可保存切换，重启后保持。它不暂停平台扫描、交易决策或单市场定时复查，也不改变 `robot_paused` 和 `paused_platforms`。

显式保存空 `decision_strategy` 表示不启用用户策略插件，不能回退到进程启动时的旧插件；此时使用框架内置
决策策略。内置策略没有配置卡片，但当前生效全文可以导出。

## 插件私有配置

插件有配置时，初始化函数返回字段 schema、JSON 存储说明以及读取、保存、删除回调。插件系统不知道文件
如何持久化，只调用插件提供的回调。内置插件当前使用 `config/plugins/*.json`：

- API 插件：端点、认证、钱包、代理、交易账户起始资金和平台参数；
  Polymarket 的 `POLYMARKET_MARKET_WS_URL` 默认是官方市场订阅端点，盘口只从 WebSocket 的完整快照及后续增量构造；插件代理仍按 `POLYMARKET_HTTP_PROXY` 的 `INHERIT`/直连/独立配置解析。HTTP 保留市场目录、账户、交易及必要对账，不再用于即时盘口读取；断线或数据过期时不返回旧盘口。
- Provider 插件：客户端路径、模型、凭证、代理和超时；OpenRouter 的远端端点固定在其插件实现内；
- typed evaluator 插件：发现粗筛的类型协议、模型、直接请求/解析参数和独立代理；它不参与交易决策。
  当前 `jev` 插件实例默认选 Jev；
  OpenRouter 中的 Jev 型号走原生 Decisions，其它明确支持 structured output 的型号走 strict schema Chat
  Completions，并默认只复用指定 Provider 的推理 Key；自定义方式填写 Chat Completions Base URL、模型名和
  可选 Key，原生 strict schema 不可用或被忽略时回退到强制函数参数。各方式都不复用 Provider 的模型或代理；
  另有默认禁用的 `laya` 实例，直接连接外部 WebGPU 服务，独立校验健康状态与 Choice/Score/Noul 协议；测速由 Laya 服务启动时及每 300 秒自行执行，状态包含在 `/health`，测速期间新推理请求收到带状态的 429；
- 策略插件：策略文本路径和候选筛选字段；
- 标的发现插件：发现文本、读取预算及其私有参数；
- 研究插件：预测市场跨平台查询、行情、K 线和业务历史的结果限制；通用搜索/网页由官方 CLI 管理；
- 业务风控插件：账户分配、损益规则和自定义 Python 规则文件；
- Agent 行为风控插件：允许的工具名与交易动作白名单。

每个字段必须有非空 `description`，可以声明默认值、枚举或秘密类型，以及 `required`（不填就存不下）
和 `needed_to_run`（存得下但插件跑不起来）——两者的区别见 [插件系统](PLUGIN_SYSTEM.md)。秘密字段不会回显；保存空秘密
保持原值，显式点击“删除此字段值”才会清除。删除必填且没有默认值的字段会让对应插件处于待配置状态，
直到用户重新填写。

公开仓库只提交 `examples/plugin_configs/` 下的 placeholder 样例；真实密钥、钱包和签名文件不得进入版本
控制、日志或文档。

Binance 和 Polymarket 还分别在自己的同一 JSON 中提供 `*_SCAN_INTERVAL_SECONDS`、
`*_ERROR_BACKOFF_SECONDS`、`*_ERROR_BACKOFF_MAX_SECONDS` 和 `*_TOPIC_PAGE_SIZE`，均有默认值。旧的
`*_MAX_TOPICS_PER_CYCLE` / `*_MAX_DECISIONS_PER_CYCLE` 已退役，不再显示或参与停止；资源保护统一由上面的
发现页数/时限和决策尝试/时限承担。动态 UI 从 API 插件初始化返回的 schema 自动生成字段。

Polymarket 的私有配置还包含 `POLYMARKET_BRIDGE_URL`，默认指向官方 Bridge。充值和提现资产不在配置中
写死：插件运行时从 Bridge 读取完整名称、缩写、网络、chain id、合约、精度与最低金额。默认接收地址只
是提现表单的便捷值；最终提交仍使用用户在绑定路线中选择的目标链/币种和本次接收地址。

`bot_management.json` 增加 `strategy_evolution`（默认 `true`）。它只决定**用户自己的**发现或
决策策略插件是否附加运行时学到的叠加层；关闭后这些插件只使用用户写的原文，策略文件本身任何时候都
不会被改写。内置策略不受该开关影响，始终测量并始终应用自己的叠加层。
