# 插件系统与生命周期

## 固定类别与自动扫描

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

readiness 回调由插件自己判断当前私有配置能否投入机器人运行，并返回 `PluginReadiness` 和具体缺失原因；
插件系统不知道 API Key、钱包、策略路径或某字段的业务含义。直接工厂调用和生产联网集成测试不会被
readiness 拦截，所以测试仍可把不完整凭证发到真实服务器验证响应；正式 `serve`/`run` 的运行主管才以
readiness 决定是否激活。

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

## 启用、禁用、排序、刷新

插件系统把有序名单保存在应用配置 `management_file` 指定的文件。启用时导入并初始化；禁用时调用旧实例的 `teardown`，
然后注销。手动刷新先卸载所有已加载插件，再重新读取插件目录和管理名单。刷新后删除的文件、移除的
目录或不再启用的插件均不会残留显示或实例。卸载失败会显式报错。

管理页面覆盖六类插件：安装新源码、启用/禁用、优先级、当前策略、刷新、动态配置表单、字段删除和整个
配置删除。安装接口先验证类别、名称、Python 语法及顶层 `initialize_plugin`，只允许写入该类别当前配置的
目录且不覆盖现有文件；安装后保持禁用，所以不会立即导入不受信任代码。启用、私有配置保存、暂停和刷新
都会停止旧 runtime、执行 teardown、重新扫描并按 readiness 自动决定是否启动。

研究工具和 Hook 可以全部禁用；API、Provider 和当前策略也允许暂时保存为空，以便形成可编辑的“不完整”
状态。此时 Web 管理服务继续运行，但机器人不会启动；填齐后自动启动。若某个风险目标没有启用任何适用
规则，插件系统不替插件增加默认拒绝策略。

## 完整例子

- `examples/api_plugins/static_demo.py`：标准化读取、私有 JSON、线上 HTTP 写传输、网络规则和卸载。
- `examples/decision_provider_plugins/static_provider.py`：严格 schema 的结构化 Provider。
- `examples/decision_strategy_plugins/example_strategy.py`：策略文本和私有候选筛选。
- `examples/research_tool_plugins/static_evidence.py`：动态加入 Agent 控制 schema 的工具。
- `examples/risk_plugins/reject_operation.py`：命名目标和标准规则结果。
- `examples/hooks/audit_hook.py`：注册及逐项注销 Hook。
- `examples/plugin_directories.json` 与 `examples/plugin_configs/`：目录和字段值样例。

插件与动态 Python 风控在机器人进程内运行，属于受信任代码边界。
