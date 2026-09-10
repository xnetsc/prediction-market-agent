# 插件系统与生命周期

## 固定类别与自动扫描

类别为 `api`、`decision_provider`、`decision_strategy`、`market_discovery`、`research_tool`、`risk`、
`hook`。`market_discovery` 决定每轮把决策名额给哪些标的；不安装插件时由框架内置策略工作，内置策略
没有配置面，但当前全文可在插件中心导出。

应用配置的 `plugin_directories_file` 所指向 JSON，其 `categories` 必须包含 `api`、`decision_provider`、
`decision_strategy`、`research_tool`、`risk`、`hook` 六个目录列表。`${PACKAGE_ROOT}` 可展开为安装包
目录。`${WORKING_DIRECTORY}` 展开为机器人工作目录。默认每类先扫描工作目录下可写的 `plugins/<类别>`，
再扫描安装包内置目录。每个目录顶层非下划线 `.py` 文件都是候选插件，插件名等于文件名的小写 stem。

扫描分两阶段：所有候选只读取文件名；仅有序启用名单中的候选才会被导入并调用
`initialize_plugin(context)`。所以禁用插件没有导入副作用，界面只显示类别、文件名和来源路径。

## 初始化契约

初始化必须返回 `PluginSpec`：类别、名称、描述、工厂、可选 `PluginConfiguration`、可选 readiness、
可选 runtime 和必填 `teardown()`。API/Provider/策略/研究工具工厂接收通用 config；风险与 Hook 还接收服务字典。每种插件
的运行对象协议见内置实现和 `examples/` 对应目录。

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
`integer`、`number`、`boolean`、`enum`、`secret`，每个字段可有 `default`，也可为 `required`。

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

插件中心按 `#plugins/类别` 提供 API、策略、研究、风控和 Hook 五个二级页面，每类有用途、三步流程和
用户操作提示。Decision Provider 在底层仍是同一种自动扫描插件，但启用、顺序、私有配置、客户端账号、
兼容 API、模型和新扩展安装只在 `#models` 管理，不在插件中心重复出现。
技术来源、错误原因和私有配置默认折叠；禁用插件仍只依据文件信息显示。页面操作例子见 [Web UI](WEB_UI.md)。

管理页面合计覆盖六类插件：安装新源码、启用/禁用、优先级、当前策略、刷新、动态配置表单、字段删除和整个
配置删除。安装接口先验证类别、名称、Python 语法及顶层 `initialize_plugin`，只允许写入该类别当前配置的
目录且不覆盖现有文件；安装后保持禁用，所以不会立即导入不受信任代码。启用、私有配置保存、暂停和刷新
都会停止旧 runtime、执行 teardown、重新扫描并按 readiness 自动决定是否启动。

研究工具和 Hook 可以全部禁用；API、Provider 和当前策略也允许暂时保存为空，以便形成可编辑的“不完整”
状态。此时 Web 管理服务继续运行，但机器人不会启动；填齐后自动启动。若某个风险目标没有启用任何适用
规则，插件系统不替插件增加默认拒绝策略。

## 完整例子

可选 `PluginSpec.network_routes_callback` 描述插件自有网络路径，供环境诊断使用。返回
`DiagnosticNetworkRoute(label, proxy, no_proxy)` 元组，不携带业务请求头、不创建交易实例；
字段解析仍由插件负责。未启用插件不会为查询而初始化。完整契约与例子见 [环境诊断](ENVIRONMENT.md#插件扩展契约)。

- `examples/api_plugins/static_demo.py`：标准化读取、私有 JSON、线上 HTTP 写传输、网络规则和卸载。
- `examples/decision_provider_plugins/static_provider.py`：严格 schema 的结构化 Provider。
- `examples/decision_strategy_plugins/example_strategy.py`：策略文本和私有候选筛选。
- `examples/research_tool_plugins/static_evidence.py`：动态加入 Agent 控制 schema 的工具。
- `examples/risk_plugins/reject_operation.py`：命名目标和标准规则结果。
- `examples/hooks/audit_hook.py`：注册及逐项注销 Hook。
- `examples/plugin_directories.json` 与 `examples/plugin_configs/`：目录和字段值样例。

插件与动态 Python 风控在机器人进程内运行，属于受信任代码边界。
