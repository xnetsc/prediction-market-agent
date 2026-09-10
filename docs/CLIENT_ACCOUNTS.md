# 客户端账号、模型与回调映射

界面的“模型服务”并列提供 Codex、Claude 客户端账号与兼容 HTTP API。登录向导内有完整步骤，
无需先阅读此文档。Codex/Claude 卡片展开“登录选项与客户端维护”即可选择登录方式：自动选择、
本机网页回调、设备码 / 验证码。已登录时维护按钮默认收起，主要按钮是“配置模型”。兼容 API 卡片的
“配置 API 服务”直接在模型页展示 URL、Key、模型、代理及 OpenRouter / OpenAI 预置。
默认自动：手机/平板或非回环访问地址选择远程码流程，桌面 localhost/127.0.0.1/::1 选择本地回调。
这是可覆盖的环境提示，不是服务器同机证明；任何本地回调仍必须通过逐流程映射验证。
手动选择对各客户端独立生效，当前页面保留选择，重新加载后恢复自动；登录向导里也能切换，
切换会取消旧客户端登录流程再开始新的流程。此选择与 Web UI 管理员 Passkey 验证完全无关。
本站不接收官网密码；远程模式允许输入本次验证码，只有客户端确认登录后才显示成功。

## 模型和推理强度列表

模型配置中新增自动加载的模型与推理强度列表。Codex 使用本客户端的 App Server 初始化及 `model/list`
（含分页）；Claude 使用流式控制协议初始化结果中的 `models`。仅读取元数据，不发送用户 prompt 或
启动推理。目录查询进程使用插件已有的账号目录、客户端路径和代理，最多等待 25 秒，完成/错误后结束。
结果按模型保留强度选项，最多缓存一分钟。未知已保存值保留并提示重新选择，不静默覆盖配置。
`CODEX_EFFORT` 传为 `-c model_reasoning_effort="..."`，`CLAUDE_EFFORT` 传为 `--effort ...`；空字符串不
传此覆盖项。不要把两个客户端同名强度理解为相同计算量。客户端/账号未公布的选项不自行补全。

依据：[Codex App Server 模型发现](https://learn.chatgpt.com/docs/app-server)、
[Claude 模型与强度配置](https://code.claude.com/docs/en/model-config)、
[Claude ModelInfo 契约](https://code.claude.com/docs/en/agent-sdk/typescript)。

## 手机与远程服务器：设备码 / 验证码

不需要手机监听端口、安装助手，也不需要远程部署开放额外端口。所有管理请求仍经过 Web UI 同一入口，
公网保持 Passkey 与加密会话。手机识别只用于选择登录向导，不参与认证或服务器拓扑判断；桌面访问远程
服务器时也可显式选择该入口，不能仅凭浏览器 User-Agent 确定服务器是否同机。

- Codex：启动官方 `codex login --device-auth`，打开官方设备授权页并输入向导显示的设备码。客户端
  自己轮询授权结果。个人安全设置或工作空间权限需要允许设备码登录；不支持时明确失败，不伪装成功。
- Claude：由官方 `claude auth login --claudeai` 选择无浏览器验证码流程。向导展示 CLI 实际返回的
  官方 URL；网页授权后把完整验证码粘贴回向导，程序仅写入本次 CLI 的标准输入，等待退出及登录状态确认。
  核对的客户端使用 `https://platform.claude.com/oauth/code/callback`，并提示 `Paste code here if prompted`。
  程序不改写 redirect_uri，不自行交换 OAuth token，不重实现官方登录协议。

验证码提交绑定当前 flow ID，拒绝旧流程、多行或重复提交；错误码、取消、超时不算成功。设备码和官方链接
在完成/取消/超时后从状态中清除，输入框提交即清空，验证码不写入配置或审计账本。远程模式不创建
机器人回调映射/助手监听；CLI 内部的授权资源由 CLI 自己清理。

例：手机打开 `https://robot.example/#models` → Claude 保持“自动选择”并点击登录 → 官方页面授权 → 返回输入
验证码 → 等待“已登录”。服务器只需开放管理 HTTPS 入口；不要把回调 URL 中的 localhost 换成服务器域名。
原有本地回调逻辑继续保留；两者是并列方式。

登录等待期间 CLI 子进程必须持续运行，后续提交和轮询须回到同一实例；多副本需要会话粘性。
会冻结/回收进程的函数平台不能仅凭同端口 HTTP 就保证此流程存活，需部署到常驻容器或支持持续进程的实例。
凭据目录必须使用持久存储，不能依赖临时容器文件系统。

依据：[Codex 远程设备认证](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)、
[Claude 登录说明](https://code.claude.com/docs/en/authentication)。

回调页的 `Authorization received` 只表示回调已转交客户端，不代表账号已登录。
Codex 插件还会跟随同一 localhost 主机、端口上的 `/success` 完成跳转，让官方登录进程正常结束，
随后执行客户端登录状态检查并更新 dashboard；助手转发与直接映射共用这段逻辑。
例如 `/auth/callback?…` 返回 `302 Location: /success?…` 时，插件在容器内部完成这个请求，
不要求用户手动打开成功地址。跳转不得跨主机、端口或进入未声明路径，非 2xx 最终响应不视为送达成功。
凭据已保存但页面仍等待时，应区分回调收尾故障与账号授权失败，不要直接要求用户反复授权。

## 浏览器验证实际回调地址

插件从官方客户端本次流程中取得原始 `redirect_uri`。例如 Codex 使用
`http://localhost:1455/auth/callback`，Claude 可使用动态端口 `http://localhost:<端口>/callback`。
不修改 OAuth 的注册回调，不用管理页面端口替代回调端口。

容器插件在容器网卡的同一端口创建本次流程专属验证/转发入口，客户端仍监听容器自己的 loopback。
浏览器向回调地址的同一协议、主机和端口请求随机验证路径，每次探测生成新的 32 字节随机 challenge，
要求响应同时匹配 challenge、本次流程随机证明及原回调地址；旧响应不能通过新探测。服务端还检查自己的
入口确实收到过这次探测。验证成功后才隐藏助手并显示官方链接。真正的 OAuth 回调也经过这个入口，
校验原始 state 后转交本次客户端。取消、超时、登录完成或插件卸载都会关闭入口、使证明失效。

- 宿主机上的另一个客户端即便占用了相同端口，也无法返回本次容器流程的证明。
- 缺少 Docker 同端口映射、入口监听失败、错误证明、旧证明或浏览器 CORS/本地网络权限限制，都不会通过。
- 浏览器阻止请求时显示“无法确认”，不等同于断言端口关闭；允许本地网络访问后可以重新检测。
- 证明只用于判定回调路由，不是账号认证结果；不改变管理页面自己的本地/公网认证策略。

`CODEX_CALLBACK_BIND_HOST` / `CLAUDE_CALLBACK_BIND_HOST` 属于 Provider 私有配置。
`AUTO` 在 Docker 中采用容器网卡地址；也可明确指定绑定地址，留空不创建验证入口。
这不管理 Docker daemon，也不会自动抢占宿主机端口或终止本机客户端。
Docker 必须把浏览器访问的原端口映射到容器同端口，不能映射到另一个端口后宣称通过。

### 本地容器自动临时映射

`start-local.sh` / `start-local.ps1` 会同时启动宿主机上的回调转发管理进程，不需要下载或运行独立登录助手。
它读取当前容器的登录状态，仅在等待回调时，为客户端本次实际选择的端口创建临时转发容器。绑定宿主机
`127.0.0.1:<本次端口>`，目标为当前机器人容器的同端口；没有额外固定探测端口，也不预留整段端口。
转发容器不挂载凭据或 Docker socket，不以特权运行，只传递本次入口的 TCP 流量。

例如本次 Claude 选择 40165，下次选择 43821，则分别只在各自等待期间发布对应端口；这些只是示例数字，
不属于配置默认值。端口已被其他程序占用时明确报告冲突，不终止该程序，也不把另一个实例误判为当前流程。
页面自动重试映射探测，只有拿到本次证明才开放网页登录。

仅运行 `docker compose up` 不会启动宿主机管理进程。自定义 Docker 部署可另外在宿主机前台运行：

```bash
sh deploy/local-callbacks.sh prediction-market-agent 8765
```

Windows 对应命令：

```powershell
./deploy/local-callbacks.ps1 -Container prediction-market-agent -ApiPort 8765
```

参数分别为容器名称/ID、容器内管理端口，不是 OAuth 回调端口。一键启动的运行日志和 PID 位于
`runtime-data/local-callbacks*`，无需宿主机 Python。容器停止后管理进程退出并清理自己创建的转发容器；
容器替换或 Docker 重启后重新运行一键启动脚本。不要自行添加永久回调端口映射，否则该映射不会随流程关闭。

### 动态端口和生命周期

端口由官方客户端本次 `redirect_uri` 决定，程序不保存固定回调端口配置。Claude 客户端动态选择端口。
当前核对的 Codex CLI 0.154.0 使用默认 1455 和注册过的备用 1457，没有公开任意随机端口选项；
不能修改 OAuth URL 或仅随机改宿主机端口，否则与官方回跳地址不一致。未来客户端选择其他端口时，
转发逻辑直接读取新地址，不需要修改框架。端口示例不构成程序中的白名单。

本次回调已转交、登录取消/失败/超时、插件卸载时，验证/转发入口关闭；助手也会检查流程是否仍在等待，
结束后释放监听。宿主机管理进程通过状态变更撤销临时映射，转发容器另有到期退出兜底。
收尾会给已接收的回调响应短暂传输时间，并非绝对零延迟关闭；空闲状态不保留回调监听，仅管理页面端口常驻。

在另一台电脑打开局域网管理页时，官方 localhost 仍指浏览器电脑，不是服务器。
原生非容器安装没有这条容器映射证明时，也不冒充已经验证；可用助手桥接。

## 助手与凭据

无法验证直连时，向导显示系统选择、完整的一次性命令和“复制命令”按钮：

1. macOS/Linux 选择 Bash，Windows 选择 PowerShell；这是浏览器电脑的系统，不是服务器的系统。
2. 复制命令，在本机终端粘贴并回车。脚本直接从当前机器人获取，完整校验后执行，无需手动下载文件、解压、
   安装客户端或打开未知发布者的可执行文件；不会关闭 Gatekeeper、设置 ExecutionPolicy 或请求管理员权限。
3. 终端显示 Ready 后回到向导打开官网授权，保持终端开启。服务器端确认客户端已登录后向导显示成功。
4. 完成、取消或超时后监听关闭。停止终端可用 Ctrl+C，并在向导取消该流程；失败时重新开始以生成新命令。

macOS/Linux 需要 curl、Bash 和 Python 3.9+，缺少 Python 会明确提示，不自动安装。若当前 Python 没有
`cryptography`，脚本创建权限受限的临时 venv，通过 pip 从 PyPI 安装 `cryptography>=44,<47`；
正常结束或信号退出后清理，不改系统 Python。缺少 venv/pip 支持时同样明确报错。Windows 使用系统
PowerShell 5.1+/.NET，不需要 Python。企业脚本策略、代理或防火墙仍可能阻止执行，脚本不会绕过这些限制。
curl/pip 使用本机的相关代理配置；Python 助手的请求使用系统代理设置，PowerShell 使用系统网络栈，
均不会套用服务器容器内的代理地址，也不关闭 TLS 验证。

### 命令与脚本的字节完整性

管理界面通过已鉴权的操作获得一次性脚本凭据及脚本 SHA-256。curl/PowerShell 请求把凭据放在
`Authorization: Bearer …`，不放在 URL 查询串。示例获取地址（不是可重复使用的公共下载链接）：

```text
https://robot.example/api/plugin-helper/decision_provider/claude/script/bash
https://robot.example/api/plugin-helper/decision_provider/codex/script/powershell
```

Bash 命令的管道为 `curl → Python 原始字节缓冲及 SHA-256 校验 → bash`。脚本内容不存进 shell 字符串、
不使用 eval、不插入命令参数、不二次展开 `$`、反引号、引号或反斜杠；完整匹配后才写入 bash 的标准输入。
PowerShell 从 RawContentStream 读取原始字节、校验同一摘要，再严格按 UTF-8 解码创建 ScriptBlock。
中文、LF/CRLF 都必须与生成时的字节一致；下载截断、网络错误、字符集转换或内容被修改都会停止，
不会执行已经收到的半截脚本。摘要依赖可信管理页面提供，不能替代站点 TLS 或抵御管理站点本身被控制。

脚本中嵌入的配置使用 Base64 编码避免源码插值；这是编码，不是保密措施。命令、脚本和终端历史都应视作
本次配对凭据，不要分享。取脚本凭据仅可使用一次、绑定所选 shell 和当前流程；生成新命令会撤销旧的下载
凭据，取消/过期后同样失效。实际运行使用不同的短时通道密钥，公开状态接口不返回这些值。
源代码脚本随 wheel/镜像打包，不再依赖 `HELPER_DIRECTORY`、PyInstaller、ZIP 或原生助手发行目录；
旧私有 JSON 中的 HELPER_DIRECTORY 字段不会再控制功能，无需移动或覆盖已有账号数据。

助手持有短时随机能力令牌，仅能在 `/api/plugin-helper/decision_provider/<名称>` 与该登录流程交换加密消息；
不能读取管理配置或调用业务接口。Python 使用 AES-GCM；PowerShell 为兼容系统 .NET 使用
AES-256-CBC + HMAC-SHA256 Encrypt-then-MAC，独立加密/认证子密钥，校验 MAC 后才解密；两者均验证
方向标记和随机 nonce，服务器拒绝请求重放。回调只转发到官方客户端捕获的 loopback 地址，验证 state，
不记录授权码。
远程助手需要 HTTPS 管理地址；明文 HTTP 配对仅允许回环管理地址。
默认凭据目录为工作目录中的 `credentials/codex`、`credentials/claude`，由插件创建独立 HOME 并持久保存。
实际请求发现明确凭据失效时，UI 提示重新登录；超时或限流不直接推断为凭据过期。

## 模型、代理和升级

三个 Provider 各自设置模型与代理使用方式。CLI 模型留空使用客户端默认；兼容 API 要填写模型 ID。
Codex/Claude 默认 `INHERIT`，跟随程序设置中的统一代理；手动 DIRECT/URL 优先。统一代理默认 HOST，
跟随一键启动器捕获的宿主机代理；不可达时由宿主机
Bash/Python 或 PowerShell 转发脚本处理。模型服务卡片显示脱敏地址和来源，详细检测范围、限制及配置见
[宿主机代理说明](HOST_PROXY.md)。兼容 API 默认 DIRECT、不继承统一代理；只能在自身配置中显式开启。
这与登录回调助手是两条独立通道。
OpenRouter 预置包含可修改 URL、空密钥与空模型；打开配置自动获取模型列表，顶部支持名称/ID 关键词
搜索。修改连接配置后先保存，再点击“刷新模型列表”；选中模型后保存。不支持目录的服务仍可手动输入。
切换预置会在保存时清除旧密钥，避免把旧服务密钥发给新地址。

客户端每六小时检查官方 npm 最新版，间隔可配置；升级必须点击确认。下载和检查使用对应插件代理。
新版安装到 `clients/<名称>` 的独立版本目录，通过版本检查后才切换指针；安装失败保留旧版本。
登录目录和安装目录都由插件字段管理，切换目录不会自动搬迁或删除原目录。

参考：[Codex 认证](https://developers.openai.com/codex/auth/)、
[Claude 认证](https://code.claude.com/docs/en/authentication)、
[OpenRouter 模型列表](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)。
