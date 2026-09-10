# 宿主机代理检测与转发

本地一键启动在启动机器人容器前读取宿主机代理设置、验证容器可达性，再启动应用。检测结果由程序设置中的
`shared_http_proxy=HOST` 作为统一代理使用。Codex、Claude、Binance、Polymarket 和标准研究插件的私有
代理字段默认 `INHERIT`，因此共用这一条路径；每个插件可改为 `DIRECT`、`HOST`、`ENVIRONMENT`、
`SYSTEM`（仅原生 macOS）或明确的 HTTP/HTTPS URL，覆盖只影响该插件。界面显示的实际地址会去掉凭据。

OpenAI 兼容 API 是例外：其 `COMPATIBLE_HTTP_PROXY` 默认 `DIRECT`，不读取统一代理。只有用户在这个插件
自己的配置中明确填写代理方式或 URL 后，它才使用代理。

## 检测范围和顺序

1. 当前启动进程的 `https_proxy/HTTPS_PROXY`、`http_proxy/HTTP_PROXY`、`all_proxy/ALL_PROXY`。
2. macOS：`scutil --proxy` 的系统 HTTP/HTTPS 设置，通常也是 Chrome/Safari 使用的系统代理。
3. Windows：当前用户的 Windows Internet Settings（WinINET，包括按协议分别设置的代理）。
4. Linux：`/etc/environment` 中的代理字段、GNOME GSettings、KDE kioslaverc，按此顺序。

检测不执行配置文本或 PAC 脚本。PAC/WPAD、仅 SOCKS、无法自动取得认证信息的桌面配置，会明确报告需要手动
配置 HTTP/HTTPS 代理，不伪装为直连。Firefox 独立配置和浏览器扩展自己的路由未必属于系统设置，不能保证
自动取得；在对应 Provider 的配置中填写 HTTP 代理地址。常规启动中没有检测到系统代理则为 DIRECT。
未通过一键启动的云端容器没有宿主机检测文件时，HOST 由通用代理解析器检测当前运行环境，不依赖外部脚本。
`ENVIRONMENT` 可显式忽略快照，始终使用当前进程环境 / Python 标准库可读取的原生系统代理。
Linux 容器通常通过 HTTP(S)_PROXY/ALL_PROXY/NO_PROXY 环境变量配置；这些变量未设置时直连。
原生 macOS/Windows 可读取标准库支持的系统代理；不执行 PAC，也不读取浏览器扩展私有配置。
来源明确显示“runtime environment / system”，HOST 无快照还显示“no host snapshot”，不声称读到了物理宿主机。

远程模式不转换 localhost、不生成转发、不探测手机代理。比如容器内 sidecar 共用网络命名空间的代理
`http://127.0.0.1:8118` 必须原样使用；不同容器则应使用实际可达的服务名。环境变量取值在客户端发起操作时
读取；修改云平台注入的环境变量通常需要重建/重启容器。显式插件 URL/DIRECT 仍优先。

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -e HTTPS_PROXY=http://proxy.internal:3128 -e NO_PROXY=localhost,127.0.0.1,::1 \
  -v prediction-agent-data:/data ghcr.io/xnetsc/prediction-market-agent:latest
```

例中没有宿主机检测文件，统一代理的 HOST 会直接读取容器代理。若数据卷里有旧文件，在程序设置把统一代理
改为 `ENVIRONMENT` 即可忽略；也可只在某个插件填写 `ENVIRONMENT`。代理错误不会变成直连。

`NO_PROXY` 继承可读取的排除列表，并始终补上 localhost/127.0.0.1/::1，避免官方本地回调进入代理。
不同客户端对复杂通配符/CIDR 的支持不完全等同于浏览器，PAC 的按 URL 路由不能用固定 HTTP 代理冒充。
Windows 的 `<local>` 不直接传给跨平台客户端；本地回环例外由程序明确补齐。

## 容器地址转换

宿主机 `localhost/127.0.0.1/::1` 不能直接复制成容器回环地址。Docker Desktop 使用
`host.docker.internal`；Linux 使用 Compose 提供的 `host.proxy.internal:host-gateway`。
启动前用同一镜像的独立检查容器确认 TCP 可达性，再通过最终代理地址发起一次真实 HTTPS 请求；不启动
机器人逻辑、不读取账号材料。默认验证 `https://api.ipify.org?format=json`，可用
`PREDICTION_AGENT_PROXY_TEST_URL` 改成其他公开 HTTPS 健康检查地址。只保存验证时间和目标主机名，
不保存响应正文；仅端口可连接不算成功。

例如 macOS 的系统代理 `http://127.0.0.1:7892`，通常转换为
`http://host.docker.internal:7892`。7892 只是示例，程序读取实际配置，不固定该端口。
Docker 拉取镜像由 daemon 自己的代理设置控制；应用的宿主机检测不能修复 daemon 在拉取阶段的网络错误，
也不擅自修改 Docker Desktop 或系统网络设置。

## 容器访问不到时：宿主机脚本转发

若容器不能访问、但宿主机能访问该代理，一键启动器自动运行：

- macOS/Linux：`deploy/proxy-forward.sh` 调用 Python 3.9+ 的纯标准库实现；缺少 Python 时明确提示安装。
- Windows：`deploy/proxy-forward.ps1` 使用 PowerShell/.NET，不下载原生程序、不修改 ExecutionPolicy。

转发监听端口由操作系统动态分配，每次启动生成独立随机访问凭据。只允许已认证连接，且上游固定为检测到的
代理；转发凭据不透传给上游，若上游需要 Basic 认证则使用原代理凭据替换。HTTP CONNECT 隧道的 TLS 数据
原样转发，不截取 TLS、注入证书或关闭校验。宿主机代理本身不可用时明确失败，不自动直连。

Linux 默认仅绑定 Docker bridge 的宿主机网关地址。Docker Desktop 的虚拟宿主机地址通常不是物理系统的
可绑定网卡地址，因此 macOS/Windows 的备用转发绑定 IPv4 全接口，但必须通过随机凭据验证才接受流量。
这可能触发系统防火墙提示；程序不会调整防火墙。应限制 Docker/本机网络访问，不要把转发端口发布到公网。
若策略连这个转发入口也阻断，或代理无法完成 HTTPS CONNECT/TLS 请求，复查会报告失败。交互式本地启动
要求用户输入 `1` 明确改为直连并继续，或输入 `2` 退出；非交互启动默认退出，不会静默直连。自动化只有
显式设置 `PREDICTION_AGENT_PROXY_FAILURE_ACTION=direct` 才会选择直连。
Rootless Docker、远程 Docker context、受限 Docker bridge 等网络布局需要单独配置，不能假定宿主机相同。

成功后启动器把转发地址写入快照，并绑定本次机器人容器 ID。容器停止/消失、重新检测替换快照时，
脚本退出并释放监听；启动后五分钟仍未绑定容器也退出。代理服务需要持续到容器结束，与仅等待授权期间
使用的登录回调端口生命周期不同。Docker 自动重启后若转发已退出，应重新执行一键启动器。

## 文件、配置与手动运行例子

自动生成文件在 `runtime-data/.deployment/`：

- `host-proxy.json`：检测来源、时间、状态、原代理、容器地址和排除列表，可能包含凭据，禁止提交；
- `proxy-forward.log` / Windows 的 `proxy-forward.errors.log`：不输出代理密码或随机凭据；
- `proxy-forward.pid` / `proxy-forward.ready`：启动器进程状态。

POSIX 检测文件以 0600 写入，部署目录以 0700 创建。程序设置的 `host_proxy_file` 默认是工作目录下的
`.deployment/host-proxy.json`。Codex/Claude 仍保留自己的高级检测文件字段，供独立 `HOST` 覆盖使用；
默认 `INHERIT` 时与其他插件使用统一文件。程序不会把检测结果复制进任何插件的私有 JSON。
成功验证的快照另含 `validated_at` 与 `validation_target`；用户选择直连时也会记录该选择，但不会改写系统代理。

常规使用只需再次运行 `./start-local.sh` 或 `./start-local.ps1`。自定义部署可分步运行：

```bash
sh deploy/prepare-host-proxy.sh ghcr.io/xnetsc/prediction-market-agent:latest
# 在当前系统启动机器人容器后，用真实容器名/ID 绑定生命周期：
sh deploy/prepare-host-proxy.sh ghcr.io/xnetsc/prediction-market-agent:latest --attach-container prediction-market-agent
```

需要前台诊断转发脚本时（该快照必须已由检测器生成；不要与自动转发同时运行）：

```bash
sh deploy/proxy-forward.sh runtime-data/.deployment/host-proxy.json \
  --bind 0.0.0.0 --container-host host.docker.internal
```

```powershell
./deploy/proxy-forward.ps1 -Snapshot ./runtime-data/.deployment/host-proxy.json -ContainerHost host.docker.internal
```

上述前台例子针对 Docker Desktop；Linux 应绑定实际 Docker bridge 网关并使用 host.proxy.internal。
只重启容器不会让已退出的宿主机脚本复活，也不会重新读取宿主机系统设置，重新执行一键启动器才会完整检测。

参考：[Docker Desktop 容器访问宿主机](https://docs.docker.com/desktop/features/networking/)、
[Claude 官方代理环境变量](https://code.claude.com/docs/en/corporate-proxy)。
