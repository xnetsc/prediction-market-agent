# 运行环境与公网出口诊断

入口：**程序设置 → 服务器与网络**。信息来自运行机器人的服务端进程，不是浏览器/手机。
本地/私网访问沿用现有访问策略；公网须通过管理员 Passkey 和加密会话。没有匿名环境信息 API。

## 环境详情

展开“系统、进程、路径与存储详情”查看应用/Python 版本、系统与架构、主机名、PID、逻辑 CPU 数、
管理服务启动时间及运行时长、工作目录、数据库路径/文件大小和工作目录所在磁盘的剩余空间。
时间在服务器以 UTC 记录，界面转为浏览器时区。容器标记仅作提示，不能证明宿主机系统；CPU/磁盘信息
由操作系统报告，不保证等于容器配额。主机名解析地址不是完整网卡清单，也不是公网出口结论。
ASGI 服务地址可能是容器内部端口；请求来源可能是前置反向代理，不冒充浏览器公网 IP。

不枚举全部环境变量，不读取数据库记录、账号文件或钱包内容；插件只贡献自己的网络路径。

## 公网查询

1. 先点击“对比两种出口”。服务器用直连和统一继承代理分别访问完全相同的一组检测目标，并列显示结果。
   Binance、Polymarket 等选择 `INHERIT` 的插件使用统一继承代理；统一设置最终为直连时第二项会明确标注。
2. 如需检查插件自己的独立覆盖，从“单独检查哪个连接”选择具体插件并查询。页面先用中文解释，代理地址、
   NO_PROXY 和内部标识需展开查看。“运行环境代理”与“统一继承代理”不是同一概念：后者按照应用当前配置
   解析 `HOST`、`ENVIRONMENT`、`DIRECT` 或显式 URL。
3. 查询时服务器向配置的 HTTPS 查询服务发出普通 GET，不发送平台 Key、
   Cookie、钱包地址、签名、模型或交易请求。代理认证仅用于连接代理，不作为业务认证发送。
4. 先查看公网 IP 和查询时间、复制所需 IP；再按需展开查询来源、IPv4/IPv6、临时源端口、耗时和失败原因。

内置服务依据其公开接口提供预置：

| 服务 | 接口 | 字段 |
| --- | --- | --- |
| ipify IPv4 | `https://api.ipify.org?format=json` | `ip` |
| ipify 双栈 | `https://api64.ipify.org?format=json` | `ip`（这次连接为 IPv4 或 IPv6，不代表同时探测两者） |
| ifconfig.me | `https://ifconfig.me/all.json` | `ip_addr`、可选 `port` |

参考：[ipify 官方文档](https://www.ipify.org/)、[ifconfig.me 接口说明](https://ifconfig.me/)。
第三方服务会看到查询连接的出口地址；不保证永久可用。每个服务独立显示成功/失败，不用一个成功结果
掩盖其他失败。响应限 16 KiB，不跟随重定向，不显示原始异常里的敏感内容。

查询结果只在当前服务实例内存中保留，重启清空。不定时查询；打开/刷新页面只更新本地元数据和缓存。
配置路径或查询配置改变时，旧结果标为过期并不再提供复制按钮。动态 IP 在配置不变时也可能变化，
因此每条结果都必须结合查询时间理解，配置白名单前手动重查。每实例同时只执行一轮查询。

## 白名单与端口的限制

- 它测量的是“这条配置网络路径访问这些查询服务”的出口，不是交易平台亲自确认的来源 IP。
- 同一代理可能按目标域名、地区或连接分流；NAT、IPv4/IPv6、动态 IP、多个副本都会造成不同出口。
  同协议出现多个 IP 时界面明确警告，不自动选一个当作唯一结果。直连仅表示不使用应用 HTTP 代理，
  不会绕过系统 VPN、透明代理或基础设施 NAT。
- 云端部署需要固定出口时，应配置静态 NAT/固定出口代理，并在交易平台核对其实际收到的地址。
  本功能不改平台白名单，不判断凭据权限，也不发下单/转账来验证权限。
- `port` 是查询服务报告的这次连接临时源端口，可能受其前置代理影响；不是监听端口、不用于证明入站
  可达性，通常也不是 API IP 白名单所需值。服务没提供时显示“未提供”，不能本地编造。

## 配置与例子

下方“程序运行配置”可编辑并保存 `environment_probe_services` 和 `environment_probe_timeout`。
这两个诊断选项在下一次查询生效，不要求重启；其他启动参数继续遵循其原有重启规则。

`environment_probe_services` 是 JSON 数组文本，每项包含 `name`、HTTPS `url`、`ip_field` 和可选
`port_field`；当前支持 JSON 顶层字段。最多 8 项，空数组 `[]` 禁用公网查询，超时可设 1–30 秒。
自定义 URL 不应含认证材料。修改后的格式在查询前验证；格式错误不发请求。

```json
[
  {"name":"ipify IPv4","url":"https://api.ipify.org?format=json","ip_field":"ip"},
  {"name":"ifconfig.me","url":"https://ifconfig.me/all.json","ip_field":"ip_addr","port_field":"port"}
]
```

完整应用配置例子见 `examples/application.json`。统一代理在程序设置中修改；插件自己的字段决定继承、
直连或独立覆盖。兼容 API 默认不继承。通用诊断页面不会覆盖任何配置。未就绪插件显示原因，未启用插件
不为诊断而导入或初始化。

## 插件扩展契约

`PluginSpec.network_routes_callback` 可选，返回 `tuple[DiagnosticNetworkRoute, ...]`。回调只描述路径，
不创建交易实例、不发业务请求、不产生新的循环。插件负责从自己的存储解析实际代理与 NO_PROXY，
通用程序不猜字段名；未实现此可选能力的插件不显示诊断路径，也不假设它直连。
原始代理可包含认证信息，禁止自行写入普通 manifest；通用页面会去掉代理 userinfo。

```python
from prediction_market_agent.plugin_system.network_diagnostics import DiagnosticNetworkRoute

def network_routes():
    network = read_plugin_network_config()  # 插件自己的配置读取/解析函数
    return (DiagnosticNetworkRoute("平台配置路径", network.proxy, network.no_proxy),)

# 在现有完整 PluginSpec 初始化中添加：
# network_routes_callback=network_routes
```

若插件选择用普通 JSON 代理字段，可使用辅助函数 `configured_proxy_route`；需要支持 `INHERIT` 时同时传入
初始化上下文提供的解析回调。字段名和读取函数由插件指定，不是通用框架约定。内置 Binance、Polymarket、Codex、Claude、兼容 API
和标准研究插件均已贡献各自配置路径。该测试探针是独立普通 HTTP 传输，不模拟业务签名、网络规则、
平台客户端内部重试或服务商鉴权，不能宣称等同于完整平台请求。
