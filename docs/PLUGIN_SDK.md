# 插件 SDK 与生命周期

## 固定类别与自动扫描

`PLUGIN_SDK_CONFIG_FILE` 的 `categories` 必须包含 `api`、`decision_provider`、
`decision_strategy`、`research_tool`、`risk`、`hook` 六个目录列表。`${PACKAGE_ROOT}` 可展开为安装包
目录。每个目录顶层非下划线 `.py` 文件都是候选插件，插件名等于文件名的小写 stem。

扫描分两阶段：所有候选只读取文件名；仅有序启用名单中的候选才会被导入并调用
`initialize_plugin(context)`。所以禁用插件没有导入副作用，界面只显示类别、文件名和来源路径。

## 初始化契约

初始化必须返回 `PluginSpec`：类别、名称、描述、工厂、可选 `PluginConfiguration` 和必填
`teardown()`。API/Provider/策略/研究工具工厂接收通用 config；风险与 Hook 还接收服务字典。每种插件
的运行对象协议见内置实现和 `examples/` 对应目录。

插件配置固定为一个 JSON 文件，但路径和读写实现归插件。`PluginConfiguration` 返回：

- 完整字段 schema；
- `load_callback() -> dict`；
- `save_callback(dict) -> None`；
- `{"kind":"json_file","location":"..."}` 存储说明。

SDK 只验证 JSON 对象和字段类型、调用回调，不自行读写该位置。无配置插件返回 `None`。每个
`PluginConfigField` 必须提供 `name`、`label`、`field_type`、非空 `description`；支持 `string`、
`integer`、`number`、`boolean`、`enum`、`secret`，每个字段可有 `default`，也可为 `required`。

## 启用、禁用、排序、刷新

SDK 把有序名单保存在 `BOT_MANAGEMENT_FILE`。启用时导入并初始化；禁用时调用旧实例的 `teardown`，
然后注销。手动刷新先卸载所有已加载插件，再重新读取 SDK 目录和管理名单。刷新后删除的文件、移除的
目录或不再启用的插件均不会残留显示或实例。卸载失败会显式报错。

管理页面覆盖六类插件：启用/禁用、优先级、当前策略、刷新和动态配置表单。交易进程与管理服务是不同
进程；管理页重建自己的目录实例，交易进程需重启才能采用新配置。

研究工具和 Hook 可以全部禁用；禁用后的模块不会导入。API、Provider 与一个当前策略是构成可运行
交易流程的结构性依赖。若某个风险目标没有启用任何适用规则，SDK 不替插件增加默认拒绝策略。

## 完整例子

- `examples/api_plugins/static_demo.py`：标准化读取、私有 JSON、线上 HTTP 写传输、网络规则和卸载。
- `examples/decision_provider_plugins/static_provider.py`：严格 schema 的结构化 Provider。
- `examples/decision_strategy_plugins/example_strategy.py`：策略文本和私有候选筛选。
- `examples/research_tool_plugins/static_evidence.py`：动态加入 Agent 控制 schema 的工具。
- `examples/risk_plugins/reject_operation.py`：命名目标和标准规则结果。
- `examples/hooks/audit_hook.py`：注册及逐项注销 Hook。
- `examples/plugin_sdk.json` 与 `examples/plugin_configs/`：目录和字段值样例。

插件与动态 Python 风控在机器人进程内运行，属于受信任代码边界。
