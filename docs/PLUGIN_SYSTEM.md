# 插件系统与生命周期

## 固定类别与自动扫描

类别为 `api`、`decision_provider`、`decision_strategy`、`market_discovery`、`research_tool`、
`agent_policy`、`risk`。`market_discovery` 决定每轮把决策名额给哪些标的；不安装插件时由框架内置策略工作，内置策略
没有配置面，但当前全文可在插件中心导出。

应用配置的 `plugin_directories_file` 所指向 JSON，其 `categories` 必须包含 `api`、`decision_provider`、
`decision_strategy`、`market_discovery`、`research_tool`、`agent_policy`、`risk` 七个目录列表。`${PACKAGE_ROOT}` 可展开为安装包
目录。`${WORKING_DIRECTORY}` 展开为机器人工作目录。默认每类先扫描工作目录下可写的 `plugins/<类别>`，
再扫描安装包内置目录。每个目录顶层非下划线 `.py` 文件都是候选插件，插件名等于文件名的小写 stem。

插件删掉一个配置字段时，用 `retired_fields` 声明它的旧名字：已经存下来的配置里还留着那个值，而未知
字段本来是硬错误，升级时会直接让插件起不来。只有插件自己声明退役的名字会被忽略，拼写错误照样报错——
被悄悄丢掉的设置，是操作员以为生效、实际没生效的那一种。

扫描分两阶段：所有候选只读取文件名；仅有序启用名单中的候选才会被导入并调用
`initialize_plugin(context)`。所以禁用插件没有导入副作用，界面只显示类别、文件名和来源路径。

## 初始化契约

初始化必须返回 `PluginSpec`：类别、名称、描述、工厂、可选 `PluginConfiguration`、可选 readiness、
可选 runtime、可选 `controls`、可选 `notices_callback` / `notice_action_callback`、可选
`network_routes_callback`，以及必填 `teardown()`。API/Provider/策略/研究工具工厂接收通用 config；
两类过滤插件还接收服务字典。每种插件的运行对象协议见内置实现和 `examples/` 对应目录。

## 插件向用户说话

插件的配置页原本只能提问，不能回答——插件有话要说（一个数字要给人看、一个决定只有人能做）时无处可放，
结果要么自己闷头做了，要么悄悄失败。

现在**框架在插件页面打开时主动问它**：你有没有什么要告诉用户的，有没有什么要用户拍板的。不在初始化时
声明，因为插件要说什么取决于它后来遇到了什么；启动时定死的清单，要么承诺了永远不来的消息，要么恰好
报不出真正要紧的那条。没话说的插件回答空，界面上什么都不出现。

框架不理解其中任何内容。标题、正文、按钮文字全由插件给，框架只负责画出来并把按下转达回去：

| 插件返回 | 框架做什么 |
| --- | --- |
| `kind: display` | 只展示 `content` |
| `kind: confirm` | 展示 `content`，加上插件命名的按钮 |
| `action_label` / `dismiss_label` | 按钮文字，插件自己定 |
| `action_fields` / `dismiss_fields` | 按下前要填的输入框；`required` 只是页面上画个标记 |

只有两个动词：`confirm`（照它说的做）和 `dismiss`（放下它——拒绝和"看过了"是同一件事，操作员都是
"不用再管了"，区别只在插件给的文字）。**某项是否必填由插件在自己的处理函数里把关**，不是框架——
框架不知道那个字段是干什么的。

插件的消息记在插件自己那里，位置由插件自己定；`core/`、`plugin_system/`、`runtime/` 里没有任何一处
引用它。

readiness 回调由插件自己判断当前实例能否投入机器人运行，并返回 `PluginReadiness` 和原因；插件系统不按
API Key、钱包、`required` 或任何字段名猜测私有配置。直接工厂调用和生产联网集成测试不会被 readiness
拦截，所以测试仍可把不完整凭证发到真实服务器验证响应。正式运行主链只要求至少一个 Provider 可用及
至少一个 API runtime 真正启动；其它类别的插件不可用时只记录状态。

API 平台要参与正式后台机器人时返回 `PluginRuntime(start, stop, status)`；未返回 runtime 的平台只保留
显式调用能力，并由运行监督器显示未就绪。`start` 获得通用事件提交服务，但事件线程、定时、分页规模与失败退避由平台插件自己实现；插件扫描后提交标准化主题。通用事件循环负责后续跨平台研究、
Agent、风控和写动作。`stop` 必须中断等待并等线程结束；`teardown` 是最终资源兜底。

插件配置固定为一个 JSON 文件，但路径和读写实现归插件。`PluginConfiguration` 返回：

- 完整字段 schema；
- `load_callback() -> dict`；
- `save_callback(dict) -> None`；
- `delete_callback() -> None`；
- `{"kind":"json_file","location":"..."}` 存储说明。

插件系统只验证 JSON 对象和字段类型、调用回调，不自行读写该位置。无配置插件返回 `None`。每个
`PluginConfigField` 必须提供 `name`、`label`、`field_type`、非空 `description`；支持 `string`、
`integer`、`number`、`boolean`、`enum`、`secret`，每个字段可有 `default`。

**`required` 和 `needed_to_run` 是两件事，别合并：**

| 标志 | 管什么 | 不给会怎样 |
| --- | --- | --- |
| `required` | 能不能**存** | 保存时报错 |
| `needed_to_run` | 能不能**跑** | 保存没问题，但 readiness 报未就绪，平台不启动 |

分开是因为答案不同。API Key 显然不可或缺，但表单填一半必须存得下来——否则只能一次填完。合成一个标志，
就得在"表单存不了"和"页面把凭据显示成可选、插件却不给它就不启动"之间二选一，而后者正是这个字段要
消灭的谎。

**`needed_to_run` 是唯一声明处。** readiness 从字段表推导缺了哪些，不另列一份——同一个事实写两遍
必然漂移，而漂移的那一份，就是页面在告诉操作员凭据可以不填。

初始化上下文提供 `context.proxy_settings(value, field_name=...)`。需要联网且希望跟随程序统一代理的插件，
可把自己的代理字段默认设为 `INHERIT`，在真正创建网络客户端时调用该函数；插件也可完全不使用它。
返回值含已解析的 `proxy`、`no_proxy` 和脱敏显示信息。兼容 API 插件有意保留自己的 `DIRECT` 默认值，
直接调用基础解析函数，因此不会意外继承统一代理。

## 动态选项、预置和管理动作

配置还可返回 `choice_fields` 和 `choices_callback(field_name)`。回调返回
`[{"value":"model-id","label":"显示名"}]`，界面动态提供选择，同时保留手动输入；服务地址和凭据由插件自行读取。

需要联动选项时，配置可提供 `context_choices_callback(field_name, values)`。字段以
`choices_depend_on=("OTHER_FIELD",)` 声明依赖，通用接口仅传入所声明的非秘密字段草稿，不保存它们。
设置 `selection_only=True` 后界面只提供下拉选择，并在显示表单时加载列表。该字段必须列在 `choice_fields`；
原有 `choices_callback(field_name)` 插件保持兼容。例如：

```python
PluginConfigField("MODEL", "模型", "string", "选择客户端公布的模型。", default="", selection_only=True)
PluginConfigField("EFFORT", "推理强度", "string", "选择当前模型支持的强度。", default="",
                  selection_only=True, choices_depend_on=("MODEL",))
# PluginConfiguration(..., choice_fields=("MODEL", "EFFORT"),
#                     context_choices_callback=plugin_owned_choices)
```

回调返回格式仍为 value/label 列表；客户端读法、缓存、模型能力和参数传递全由具体插件实现。参考
`plugins/providers/_model_catalog.py` 及 Codex/Claude 初始化函数。选项是 UI 辅助，不是新的通用风控策略。
运行概览的启动向导以 Tab 分开启动必需与可选增强，只读取插件 readiness 和 runtime 状态并链接回该插件配置，不从字段 schema 推断
“还缺什么”，也不会通过猜测字段值或写入默认凭据来替用户完成配置。
`presets` 是 `{"name":"preset","label":"名称","values":{...}}` 的元组。界面套用后仍需保存，
秘密字段若需清空通过 `clear_secrets` 指定；正常留空保存依旧保留原秘密。预置不得包含真实凭据。

可选 `PluginControls(status_callback, action_callback, helper_callback=None)` 为插件提供动态管理动作。
状态回调返回 JSON（例如 `{"state":"ready","message":"可用","actions":[{"id":"check","label":"检查"}]}`）；
动作回调接收 `(action, values)` 并返回 JSON，动作名与语义归插件。内置 Provider 用它实现账号登录、
取消、退出、版本检查和升级；模型字段仍属于原配置，不增加另一套配置存储。
如果返回助手回调，插件必须自己验证短时能力令牌及消息，不得把公共助手端点变成任意业务分派入口。
通用路由还把 `GET /api/plugin-helper/{kind}/{name}/script/{platform}` 的 Bearer 凭据与
`{"script_platform": "<platform>"}` 转给同一回调，成功时输出返回值中的 `script` 字符串，禁止缓存。
脚本内容、平台标识、一次性凭据、完整性摘要和 shell 命令均由插件处理；通用路由不理解 Provider 私有协议。
内置实现和端口映射证明见 `plugins/providers/_client_control.py`、`_login_relay.py`、
`_callback_gateway.py`；完整字段样例见 `examples/plugin_configs/codex.json` 与 `claude.json`。
插件的检查线程、登录进程和回调监听器必须在 `teardown` 中停止；禁用插件不会创建这些资源。

## 启用、禁用、排序、刷新

插件系统把有序名单保存在应用配置 `management_file` 指定的文件。启用时导入并初始化；禁用时调用旧实例的 `teardown`，
然后注销。手动刷新先卸载所有已加载插件，再重新读取插件目录和管理名单。刷新后删除的文件、移除的
目录或不再启用的插件均不会残留显示或实例。卸载失败会显式报错。

插件中心按 `#plugins/类别` 提供 API、标的发现、策略、研究、Agent 行为风控和业务风控六个二级页面，
每类有用途、三步流程和
用户操作提示。Decision Provider 在底层仍是同一种自动扫描插件，但启用、顺序、私有配置、客户端账号、
兼容 API、模型和新扩展安装只在 `#models` 管理，不在插件中心重复出现。
技术来源、错误原因和私有配置默认折叠；禁用插件仍只依据文件信息显示。页面操作例子见 [Web UI](WEB_UI.md)。

管理页面合计覆盖七类插件：安装新源码、启用/禁用、优先级、当前策略、刷新、动态配置表单、字段删除和整个
配置删除。安装接口先验证类别、名称、Python 语法及顶层 `initialize_plugin`，只允许写入该类别当前配置的
目录且不覆盖现有文件；安装后保持禁用，所以不会立即导入不受信任代码。启用、私有配置保存、暂停和刷新
都会停止旧 runtime、执行 teardown、重新扫描并按 readiness 自动决定是否启动。

研究工具和两类过滤插件可以全部禁用；API、Provider 和当前策略也允许暂时保存为空，以便形成可编辑的
“不完整”状态。此时 Web 管理服务继续运行，但机器人不会启动；填齐后自动启动。若某个目标没有启用任何
适用插件，插件系统不替插件增加默认拒绝策略。同一类过滤插件可以同时启用多个，按界面顺序串成一条链，
任意一个拒绝或抛异常就整体失败。

## 完整例子

可选 `PluginSpec.network_routes_callback` 描述插件自有网络路径，供环境诊断使用。返回
`DiagnosticNetworkRoute(label, proxy, no_proxy)` 元组，不携带业务请求头、不创建交易实例；
字段解析仍由插件负责。未启用插件不会为查询而初始化。完整契约与例子见 [环境诊断](ENVIRONMENT.md#插件扩展契约)。

- `examples/api_plugins/static_demo.py`：标准化读取、私有 JSON、线上 HTTP 写传输和卸载。
- `examples/decision_provider_plugins/static_provider.py`：严格 schema 的结构化 Provider。
- `examples/decision_strategy_plugins/example_strategy.py`：策略文本和私有候选筛选。
- `examples/research_tool_plugins/static_evidence.py`：动态加入 Agent 控制 schema 的工具。
- `examples/risk_plugins/reject_operation.py`：业务风控完整插件，`market:*` 目标和标准规则结果。
- `examples/risk_rules/refuse_large_orders.py`：业务风控 `custom_rules` 加载的受信任 Python 规则脚本。
- `examples/agent_policy_plugins/refuse_tool.py`：Agent 行为风控完整插件，`agent:actions` 目标。
- `examples/agent_policy_rules/cap_trade_size.py`：Agent 行为风控 `custom_rules` 规则脚本，演示 `ADJUST` 缩减。
- `examples/plugin_directories.json` 与 `examples/plugin_configs/`：目录和字段值样例。

插件与自定义 Python 规则在机器人进程内运行，属于受信任代码边界。
