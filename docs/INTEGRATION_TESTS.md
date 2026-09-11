# 测试与验收

完整测试默认包含生产联网，不使用跳过开关：

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests examples
PYTHONPATH=src .venv/bin/python -m pytest -q
```

当前依赖修复后的完整结果为 `160 passed, 2 failed, 101 subtests passed, 0 skipped`。两项失败都来自
Binance 公共时间接口的真实 `HTTP 451`；Polymarket 与其余本地/联网测试通过。另在 Python 3.12 干净
环境从 PyPI 安装项目后执行 `pip check`，确认 `cryptography 50.0.1`、`webauthn 3.0.0` 与
`polymarket-client 0.3.0` 无依赖冲突；Linux Python 3.13 slim 容器安装同一 wheel 也通过。
终端助手另覆盖系统代理被故意设为不可达时，宿主机回环管理端点仍直接可达；远程 HTTPS 管理地址仍使用
系统代理。该分支在本机 Bash 与 PowerShell 各 7 项通过，Windows PowerShell 5.1 由 Actions 实际运行。
出口诊断的相同目标断言不依赖并发 worker 的完成顺序，并已连续运行 50 次通过。

当前矩阵覆盖：

- 配置字段说明、默认值、秘密保留和 JSON 存储约束；
- 各类示例插件全部扫描、初始化、工厂调用及卸载；
- 禁用不导入、启用/禁用、排序、手动刷新、删除文件后的卸载注销；
- 从管理界面服务安装新插件源码、写入配置目录、保持禁用且不执行模块顶层代码；
- 平台插件自己的扫描线程、可中断等待、成功间隔和失败退避；
- 通用业务事件循环接收标准化扫描结果，再执行策略、研究、Agent、风控和平台写动作；
- 全局与按平台暂停、配置不完整阻止对应平台启动、就绪平台独立启动；
- Provider 多步工具 schema、优先级与运行时故障转移；
- 策略文件、动态工具、过滤插件串链和在线执行网关；
- SQLite 会话、滑动召回、动作记录、决策台账与界面查询；
- Binance、Polymarket 各自生产读取和双平台同进程读取/决策；
- 真实生产写拒绝探测：Binance quote/order/cancel/redeem/双向 transfer，以及 Polymarket
  order/cancel/Relayer submit。探测必须收到服务器 HTTP/平台拒绝；若在本地提前失败，测试不通过。
- 架构契约：插件系统不得出现平台/凭证/策略/风控私有字段，运行时不得出现执行模式或本地写分支，测试套件
  不得出现 skip/xfail，决策账本和专用 UI 必须持续存在；通用运行层不得拥有平台扫描间隔或失败退避，
  AWS/阿里云模板不得用外部计划任务替代平台插件调度。

正式运行就绪检查与生产网络集成测试是两条独立路径：`serve`、`run` 和正式 `once` 会先检查全局结构、
插件私有配置及暂停状态；`test_api_integration.py` 直接实例化 API 插件并访问线上接口，不经过运行监督器，
因此缺少交易权限仍会把请求发送到服务器并验证真实拒绝，而不会在本地被“配置不完整”截断。

写拒绝探测使用无效测试资源标识和极小标准化金额，目的在于证明请求穿过插件签名/代理并到达
服务器。当前无写权限凭证的拒绝是测试预期；权限或服务器行为改变会让测试失败并要求人工复核，而不会
被当成通过。

最近一次本地非交易确定性回归结果：`149 passed, 87 subtests passed, 0 skipped`。命令为
`PYTHONPATH=src .venv/bin/python -m pytest -q --ignore=tests/test_api_integration.py`；
这是管理、客户端和运行架构回归，不是完整生产联网验收。包含私网/回环明文、公网认证、跨域拒绝、
客户端账号/升级、模型预置及列表、助手能力令牌和回调映射证明。
新增回归覆盖回调 302 → 同源 `/success` → 客户端进程退出 → 状态变为 authenticated；
直接转发和助手通道均覆盖，并拒绝跨主机/端口/未声明路径及非 2xx 完成响应。
同时覆盖临时容器精确归属、端口冲突、不挂载 Docker socket、流程切换/结束移除映射、每次探测随机 challenge、
真实 TCP 监听在回调完成/超时/取消后关闭，以及助手发现服务端流程结束后释放端口。Shell 启动器在本机执行过；
PowerShell 实现尚未在 Windows 上实际运行，不能将这些测试等同于 Windows 端到端验收。

浏览器探测函数另由 `node --test tests/login_probe.test.cjs` 覆盖匹配、旧证明、其他客户端 HTML、
错误端口、CORS 拒绝、超时及禁止跨目标探测。实测临时 Docker 同端口映射下的真实 Codex 登录入口，
完整管理 UI 自动验证成功并隐藏助手；未映射 Claude 动态端口时显示助手而不开放官方登录链接。
实测仅启动官方登录等待并取消，未授权用户账号。可在隔离容器中运行
`python tests/probe_browser_login.py --client codex --serve-port 17778`，同时发布 17778:17778 和 1455:1455，
然后在浏览器打开 `http://127.0.0.1:17778` 重现；页面与等待最多保留两分钟，不访问宿主机凭据。

上述未授权探测之后，用户在独立持久容器中完成了真实 Codex 网页授权，客户端确认凭据已保存；旧转发实现未跟随
`/success` 导致 dashboard 等待。补发该本地完成请求后，后台真实状态恢复为 authenticated。
这一用户流程验证了故障原因与恢复，不等同于修改后又执行过一次新的真实账号授权；新实现另由上述回归验证。

随后本机 Chrome 实测 Claude 动态回调端口自动发布，页面验证当前容器的本次证明后不再要求助手，
用户完成了真实网页登录，dashboard 确认为 authenticated。两客户端登录完成后临时转发容器均已退出。
去掉测试容器旧的永久回调端口映射、保留原数据目录替换容器后，两客户端仍为 authenticated，
当前只发布管理页面 18765，不常驻回调端口。这是本机 macOS Docker 的真实账号验证，尚未发布新镜像，
也不代表 Windows、远程下载助手或所有平台账号路径已完成验收。

远程助手随后改为一次性终端脚本：新增 `tests/test_helper_terminal.py`，Bash 与 macOS 上的
PowerShell 7.6.6 分别运行均为 `6 passed, 6 subtests passed, 0 skipped`。测试实际执行界面生成格式的
命令，通过本机 HTTP 服务获取脚本、核对 SHA-256，再建立真实 TCP 回调监听并与 Python 服务端交换
加密消息。覆盖引号、反斜杠、美元变量/命令替换、反引号、中文和 LF/CRLF；篡改、截断、HTTP 失败不执行；
取消、过期、端口冲突及密文篡改不报告成功，退出后监听释放。插件测试还覆盖旧 HELPER_DIRECTORY
配置兼容读取、一次性获取、当前 flow 绑定和公共脚本端点的能力令牌校验。

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_helper_terminal.py
TEST_HELPER_SHELL=powershell TEST_HELPER_EXECUTABLE=/path/to/pwsh \
  PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_helper_terminal.py
```

Actions 已改为 macOS/Linux Bash 和 Windows 系统 PowerShell 原生脚本测试，不再编译助手二进制。
本机 PowerShell 7 测试不能替代 Windows PowerShell 5.1 实测；新的 Actions 尚未推送运行，不宣称已通过。

宿主机代理新增 `tests/test_host_proxy.py`：覆盖 macOS/Windows/Linux 配置解析、环境变量优先级、
PAC/SOCKS 显式错误、容器回环地址改写、不可达时请求转发、私有配置手动覆盖、凭据脱敏和客户端环境。
真实 TCP 测试覆盖无凭据拒绝、带随机凭据的 CONNECT/二进制转发、上游代理认证替换、快照变化关闭监听，
以及生命周期监测继承启动器 Docker 权限。Python 与 macOS PowerShell 7.6.6 路径分别通过 6 项测试及
10 个子测试，无跳过。启动器测试确认代理检测早于 Compose 启动；四个 PowerShell 文件语法解析通过。

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_host_proxy.py
TEST_HELPER_SHELL=powershell TEST_HELPER_EXECUTABLE=/path/to/pwsh \
  PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_host_proxy.py
```

本机实际读取 macOS 系统代理，容器预检成功；另实际建立认证转发，容器经宿主机脚本、本机代理访问公开
HTTPS npm 软件源成功，验证后关闭临时转发。管理测试容器中 Codex/Claude 均保持 authenticated，
代理来源为 macOS system。该验证不调用模型或交易，不代表 Windows、所有代理协议/域名或远程 Docker
环境均已验收。Windows 原生代理测试已加入 Actions，尚未推送运行。详情见 [宿主机代理](HOST_PROXY.md)。

远程登录新增真实子进程 fixture 测试：Codex 设备码等待/取消/成功；Claude 标准输入验证码成功、错误码、
跨流程拒绝、多行拒绝、超时清理及逾期提交拒绝。另实际启动容器内官方 Codex 0.154.0 和 Claude 2.1.267，
使用隔离临时凭据目录，确认前者返回设备码及官方设备页，后者返回官方 code callback 页和输入提示；
两个流程均无机器人回调监听。检测完取消并清理，没有替用户完成官网授权，不能声称真实远程账号已登录验收。

`tests/dashboard_ui.cjs` 已在本机 Chrome 的 390、768、1440px 三种视口通过：六区导航、插件表单展开、
长表局部滚动、两种远程向导、移动设备提示，没有整页溢出或 JS 异常。页面演示数据只在测试浏览器 DOM
里生成，不写数据库、不提交验证码、不改配置。截图位于被忽略的 `runtime-data/ui-review/`；这不替代
iOS Safari / Android 真机测试。运行方法见 [Web UI 文档](WEB_UI.md)。新增 CSS/JS 已构建进入 wheel。
静态资源路由仅允许指定资源，公开访问 CSS 不会开放受保护业务端点。

界面可用性重整后再次运行同一非交易套件，结果仍为 `142 passed, 81 subtests passed`。三尺寸 Chrome
增加插件中心六类二级导航及说明、模型服务独立管理 Provider、模型页兼容 API 直达、配置表单页内保留、预置切换、模型列表选择、保存
URL/Key/模型/启用顺序载荷检查、决策五步展开与中文状态、网络技术详情默认折叠。保存与模型列表请求
在测试浏览器拦截并返回 fixture 响应，不把测试 Key 或配置修改发送给运行服务；后端读写另由原有测试
验证。没有执行交易、调用付费模型或登录新账号。三尺寸截图和工作记录不进入 Git。

环境诊断新增 `tests/test_environment.py`：7 项测试、4 个子测试通过，覆盖公网地址校验、可选源端口、
服务配置、空列表禁用、代理认证隔离、NO_PROXY、凭据脱敏、多源分歧、部分失败、缓存过期、插件仅贡献
网络配置及受保护访问。三尺寸 Chrome 还验证环境详情展开、路径选项加载和页面无横向溢出。
本机管理容器实际分别沿直连和 Codex 当前配置代理访问 ipify IPv4、ipify 双栈与 ifconfig.me；六次结果
均为 `154.23.242.4`，ifconfig.me 返回临时源端口。此地址只是当时观测，不是默认配置或固定出口承诺。

## 2026-09-10 最终代理与 UI 回归

- 启动代理检查：容器内真实 HTTPS 成功、失败时不泄露底层错误、用户明确改为直连、非交互退出及 Shell
  调用顺序均有测试；当前 macOS `7892` 路径实际通过 ipify。
- 出口诊断：`direct` 与 `inherited` 使用相同服务清单，缓存与配置指纹独立；实际三项服务均成功并观察到
  `154.23.242.4`。代理访问 Binance 时间接口仍返回 451，不把地区限制伪装成通过。
- 平台循环：Binance/Polymarket 均验证首轮立即扫描、长间隔可即时停止、扫描结果进入通用事件循环。
- UI：390/768/1440px 均通过；模型 Provider 只在模型服务页管理，插件中心为其余六类；暂停不在启动
  必需清单；直连/继承出口并列卡片和模型/插件保存反馈均无脚本错误。
- 完整 pytest 没有 skip/xfail：`159 passed, 2 failed, 101 subtests passed`。两项失败都是 Binance 官方
  HTTP 451；包含生产读取与无权限写请求测试，没有删减失败用例。
当前隔离管理部署中 Binance/Polymarket 网络配置未就绪，因此未宣称测得它们的实际出口；没有调用模型、
下单、转账或修改白名单。新模块与 environment.js 已确认进入 wheel。操作与限制见
[运行环境诊断](ENVIRONMENT.md)。

运行指引与客户端选项新增 `tests/test_setup_and_client_models.py`：缺失字段、单个就绪模型回退、暂停、
部分运行、管理专用启动、模型特定强度、客户端初始化/分页、无 prompt 请求、失败信息脱敏和实际 CLI
强度参数传递均有回归。全套非交易回归为 `149 passed, 87 subtests passed`，无跳过；后续相关专项
25 项复测通过。浏览器三尺寸还实际读取本机容器两个客户端的模型列表，并检查模型改变后的强度列表。
Codex 当前返回 6 个模型；Claude 返回 5 个条目（含默认别名），各自保留支持的强度，不发送推理请求。
账号/订阅变化、网络失败或旧客户端缺少接口会明确报错，这不是对所有账号权限的保证。

兼容 API 的可编辑模型选择框另在三种 Chrome 宽度验证：打开自动加载、模型名称/ID 多关键词与大小写
筛选、鼠标选择、方向键/回车、Esc、无匹配时手填 ID、接口错误及重试。页面重开读取当前保存值，
存在未保存草稿时提示保留；目录响应不会改写输入。浏览器查询真实 OpenRouter 公共目录，再在测试
浏览器内注入只读列表响应，避免依赖用户正在编辑的账号/代理；当前返回 436 模型。插件本身另以默认
空密钥预置查询真实接口，同样返回 436。用户配置原样保留；页面当前代理拼写错误时仍明确报错。
保存、服务器配置变化及请求失败用浏览器局部 fixture 验证，不向部署提交测试配置或推理/交易请求。
追加点击箭头展开/收起且不强制聚焦输入、search 类型/自动填充属性、首次加载失败弹层说明检查；
三尺寸通过。真实管理容器无效代理配置下，弹层同样可见且显示错误，输入不获得强制焦点。
用户原 Chrome 配置下的系统/扩展密码管理器弹窗仍需手动确认，不能由无账号 Chrome 测试代替。

容器工作流另用 `deploy/check-container.sh` 检查最终安装态。最近一次交易 API 生产联网测试结果为
`2 passed, 2 failed, 9 subtests passed, 0 skipped`：Polymarket 读取和三个写路由拒绝探测全部通过；
Binance 的两个用例在最先访问公开时间接口时收到官方主机 `HTTP 451`，因此按“不得把远端错误伪装成通过”
的约束保留为失败。该结果说明当前运行网络受 Binance 地域策略限制，不是本地跳过或模拟结果。
