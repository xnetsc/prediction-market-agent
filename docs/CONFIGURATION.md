# 配置中心

机器人不从 `.env` 读取运行参数。所有通用运行配置保存在最早加载的应用 JSON 中，默认位置为
`config/application.json`；启动命令可通过 `--config` 指定其他文件。第一次安装运行
`prediction-market-agent init` 会创建完整配置。文件不存在时使用程序内置默认值，管理界面保存后自动创建。

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
- 修改插件私有字段、删除单个字段值，或删除整个插件配置文件。

运行中的交易进程不会热替换自身配置；界面会明确提示需要重启。管理服务自己的监听地址、端口、数据库
路径等配置同样在重启后生效。

## 应用配置字段

| JSON 字段 | 用途 |
|---|---|
| `working_directory` | 所有相对配置和运行数据路径的基准目录 |
| `management_file` | 插件启用、禁用、优先级和当前策略文件 |
| `plugin_directories_file` | 六类插件扫描目录文件 |
| `max_topics_per_cycle` | 每平台每轮候选主题上限 |
| `max_decisions_per_cycle` | 每平台每轮 Agent 决策上限 |
| `interval_seconds` | 连续运行的轮询间隔 |
| `run_until_epoch` | 可选 Unix 停止时间；0 表示持续运行 |
| `state_file` | 已确认远端结果的账户镜像；多平台自动加后缀 |
| `session_db` | 会话、研究步骤、执行动作和决策台账 SQLite |
| `auth_db` | admin Passkey、公钥计数器和登录会话 SQLite |
| `admin_session_hours` | 连续无操作失效小时数，默认 72；有效请求会刷新闲置计时 |
| `admin_absolute_session_hours` | 从登录起的绝对有效上限，默认 168 小时（7 天） |
| `agent_max_tool_steps` | 每次最终决策前最多研究工具步骤 |
| `agent_tool_result_chars` | 单个工具结果进入上下文的字符预算 |
| `context_window_chars` | Provider 输入窗口预算 |
| `history_per_market` | 自动召回的同市场历史条数 |
| `dashboard_host`、`dashboard_port` | 管理服务监听地址和端口 |
| `dashboard_refresh_seconds` | 页面自动刷新间隔 |

应用配置示例见 `examples/application.json`。通用 Config 不包含平台 URL、API Key、私钥、代理、资金、止损、
网络路径、Agent 动作策略或具体交易策略；这些只能由对应插件定义。

## 插件扫描目录与启用状态

`plugin_directories_file` 指向包含六个固定类别的 JSON。每个类别是有序目录列表；文件不存在时使用安装包
内置插件目录。示例见 `examples/plugin_directories.json`。

`management_file` 保存每类插件的有序启用名单和当前决策策略。禁用插件不会被导入或初始化，因此只能在
启用后显示和管理其动态私有字段。

## 插件私有配置

插件有配置时，初始化函数返回字段 schema、JSON 存储说明以及读取、保存、删除回调。插件系统不知道文件
如何持久化，只调用插件提供的回调。内置插件当前使用 `config/plugins/*.json`：

- API 插件：端点、认证、钱包、代理、网络规则和平台参数；
- Provider 插件：客户端路径或 API 端点、模型、凭证、代理、超时；
- 策略插件：策略文本路径和候选筛选字段；
- 研究插件：搜索端点、代理、网络约束及结果限制；
- 风控插件：账户分配、损益规则、动作白名单和动态 Python 文件；
- Hook 插件：订阅事件和输出位置。

每个字段必须有非空 `description`，可以声明默认值、必填、枚举或秘密类型。秘密字段不会回显；保存空秘密
保持原值，显式点击“删除此字段值”才会清除。删除必填且没有默认值的字段会让对应插件处于待配置状态，
直到用户重新填写。

公开仓库只提交 `examples/plugin_configs/` 下的 placeholder 样例；真实密钥、钱包和签名文件不得进入版本
控制、日志或文档。
