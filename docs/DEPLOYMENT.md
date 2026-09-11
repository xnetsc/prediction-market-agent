# 安装、启动与部署

## 程序入口

wheel 安装后提供 `prediction-market-agent` 命令；`python -m prediction_market_agent` 完全等价。核心入口：

- `init`：在当前目录初始化应用配置和插件启用状态；
- `serve`：启动管理、配置和审计 Web 应用；
- `once`：由操作员或外部系统明确触发一次主动扫描与完整处理后退出；
- `run`：启动已就绪平台插件自己的扫描循环，并让通用事件循环处理插件提交的事件；
- `doctor`、`status`、`provider-test`、`report`：安装检查、状态、Provider 连通和报告。

任何命令都可用 `--config /path/application.json` 指定配置。`serve` 还支持 `--listen-host`、
`--listen-port`，用于容器平台分配的监听地址和端口。

## 本地一键启动

macOS/Linux：

```bash
./start-local.sh
```

Windows PowerShell：

```powershell
./start-local.ps1
```

脚本会检查 Docker CLI、Docker 服务和 Compose；缺少时安装，服务未启动则启动，并等待就绪。
macOS 从 Docker 官方 DMG 安装 Docker Desktop；Windows 下载并验证官方签名安装器；Linux 使用
Docker 官方安装脚本安装 Engine，必要时安装 Compose 插件。安装会使用系统的管理员权限提示，Docker
Desktop 首次条款/WSL 配置或系统要求的重启由用户在系统界面完成，再运行脚本即可继续。
参考 [macOS 安装](https://docs.docker.com/desktop/setup/install/mac-install/)、
[Windows 安装](https://docs.docker.com/desktop/setup/install/windows-install/)、
[Linux Engine 安装](https://docs.docker.com/engine/install/)。

之后脚本始终拉取已安装完整应用的 GHCR 镜像并启动容器，不创建 Python 环境，也不在本地构建。
启动应用容器之前检测宿主机代理并从检查容器通过最终地址发起真实 HTTPS 请求；无法直达时先启动宿主机
转发再做相同验证。检测到代理但验证失败时，交互式启动让用户明确选择直连继续或退出，非交互默认退出。
正常直达不需要宿主机 Python，macOS/Linux 的备用代理转发才需要 Python 3.9+；Windows 使用 PowerShell/.NET。
检测结果成为程序的统一代理，默认由 Codex、Claude、平台和研究插件继承；各插件保留 UI 独立覆盖，
兼容 API 默认直连且不继承。见[宿主机代理](HOST_PROXY.md)。
已有 Docker 时，仅启动应用容器的命令：

```bash
docker compose pull
docker compose up -d --no-build --wait
```

一键脚本还会启动宿主机回调转发管理进程，按本次客户端回调地址临时发布端口，结束后撤销；
这一步不是单独执行 Compose 的一部分。需要本地 Codex/Claude 网页直连登录时，优先用一键脚本，
不用另外下载助手，也不需要本机 Python。自定义容器的手动转发命令见
[客户端账号说明](CLIENT_ACCOUNTS.md#本地容器自动临时映射)。

容器数据保存到 `runtime-data/`，管理界面为 `http://localhost:8765`。`serve` 同时负责管理界面和机器人运行
监督：全局与插件配置就绪且未暂停时自动启动对应平台插件，无需再启动第二个 Worker 进程。

Compose 默认只绑定 `127.0.0.1:8765`，该访问免 Passkey 与业务加解密。可用环境变量 `PREDICTION_AGENT_PORT`
修改本机端口，`PREDICTION_AGENT_IMAGE` 指定其他标签或固定 digest。应用/插件配置仍在挂载目录内，环境变量
只控制容器部署。`docker compose logs -f` 查看日志，`docker compose down` 停止容器，保留宿主机数据。
旧源码运行数据不会自动迁移；要迁移时将原配置、插件与数据库复制到 `runtime-data/`，并调整其中绝对路径。

镜像包含 Python 应用、全部内置插件、Node.js、npm、Git、Codex CLI 和 Claude CLI。在界面的“模型服务”
点击对应客户端登录，本地一键启动器临时发布本次回调端口；向导验证确实到达当前容器的本次流程后无需助手。
不固定预留回调端口，不改写官方回跳地址；当前 Codex 客户端仍受其自身默认/备用端口限制。客户端验证方式
默认自动按浏览器环境和访问地址选择，可手动切换为本地回调或设备码/验证码；远程码流程只需管理 HTTPS 入口。
Codex 使用设备码，Claude 使用官方验证码页面和客户端输入，不影响管理员 Passkey 鉴权。
本地无法验证时，也可在同一向导中选择系统、复制一次性终端命令运行，继续免手动授权码的回调方式。
命令会校验脚本完整字节后执行，不需要下载 ZIP 或打开原生可执行文件；Bash/Python 和 PowerShell
的依赖与故障提示直接出现在登录向导中。
独立凭据默认保存在 `runtime-data/credentials/codex/` 和 `runtime-data/credentials/claude/`；
升级安装在 `runtime-data/clients/`，不会继承宿主机或旧 `/root` 账号。兼容 API 的 URL、Key、模型、代理
仍由其私有配置独立提供。回调映射与容器限制见 [CLIENT_ACCOUNTS.md](CLIENT_ACCOUNTS.md)。

远程云部署无需宿主机代理采集脚本：HOST 在没有快照时读取服务器自身环境，显式 ENVIRONMENT 始终忽略
宿主机快照，不改写回环地址或启动转发。云平台注入 HTTP(S)_PROXY/NO_PROXY 即可；详情见 [代理说明](HOST_PROXY.md)。

### 镜像仓库不可达时

拉镜像的是 Docker 守护进程，它既不读 shell 的 `HTTPS_PROXY`，也没有任何"只对这一次生效"的代理开关——
官方文档里给守护进程配代理的每一种方式（`daemon.json` 的 `proxies`、`dockerd --http-proxy`、systemd
drop-in、Desktop 设置）都要改配置并重启一个你可能与别的工作共用的服务。启动脚本不做这种改动。

所以 `start-local.sh` / `start-local.ps1` 的拉取走 `deploy/registry-pull.py`：

1. 先正常 `docker pull`。能拉通就到此为止，后面的逻辑一次都不会执行。
2. 失败才预检。代理沿用项目已有的 `host-proxy.sh` / `host-proxy.ps1` 检测（env、macOS scutil、Windows
   注册表、Linux GNOME/KDE，含 PAC/WPAD 的识别），再分别探测直连和走代理能否到达 registry。
3. 代理能到而守护进程没用代理时，在 **127.0.0.1** 上把上游 registry 转发出来。Docker 本来就把回环地址
   当作 insecure registry，不需要任何配置；拉完立刻把规范名移到新镜像上，转发器随即关闭。
4. 直连和代理都不通时，询问是直连重试还是退出。非交互环境不会卡住，直接失败并给出诊断。

几点边界：

- **转发地址不会写进任何文件。** 本地镜像始终带规范名 `ghcr.io/...`，所以下次更新重新走一遍上述判断，
  不依赖一个早已消失的端口。`compose.yaml` 用 `pull_policy: missing`，单独 `docker compose up` 仍会从
  原始地址取镜像。
- **失败绝不冒充成功。** 规范标签只有在拉取成功、且校验它确实指向刚拉下来的那个镜像之后才移动；否则
  报错退出，而不是留着旧镜像让你以为已经更新。
- **Docker Desktop 上这条回退路走不通。** 它的守护进程在虚拟机里，虚拟机的 127.0.0.1 不是宿主机的
  （实测为 `connection refused`）；改用 `host.docker.internal` 能连上，但那不是回环名，Docker 会要求
  HTTPS 并校验证书，装证书又回到改守护进程配置。此时脚本会说明原因，由你决定是否在 Desktop 设置里
  自行开启代理。Desktop 默认跟随系统代理，多数情况下本来就不会走到这一步。
- 宿主机没有 Python 时跳过预检，退回原来的 `docker compose pull`。

## GitHub Actions 镜像发布

`.github/workflows/container.yml` 在每次 push（所有分支和标签）或手动触发时执行：构建最终安装态镜像，
启动隔离容器验证 CLI、Web 健康、回环明文访问及公网认证，然后发布 `linux/amd64` 与 `linux/arm64`。
默认镜像名 `ghcr.io/xnetsc/prediction-market-agent`；默认分支更新 `latest`，每次提交发布 `sha-<完整提交号>`，
分支和 Git 标签也有对应镜像标签。发布使用工作流自带 `GITHUB_TOKEN` 的 `packages: write` 权限，无需保存 PAT。
实现依据 [GitHub 镜像发布文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

发布前的 login-helpers 作业在 macOS/Linux 执行 Bash 助手测试，在 Windows 执行系统 Windows PowerShell
助手测试，覆盖真实本机 TCP 回调、加密互通、过期释放及管道字节校验。不再编译/下载 PyInstaller 二进制；
脚本直接作为 Python 包资源进入最终镜像。作业先执行完整项目依赖安装，因此依赖解析失败会阻止镜像发布；
WebAuthn 3 与主程序、Bash 助手统一使用兼容的 `cryptography>=49,<51` 范围。

GHCR 新包默认可能为 private，首次发布后须在包设置中设为 public，之后即可匿名拉取；仓库 public 不自动代表
包也 public。镜像只 COPY 明确列出的源码、元数据和入口脚本，真实配置、数据库、备份不进入镜像。

无需克隆完整仓库时也可直接运行：

```bash
docker run -d --name prediction-market-agent --restart unless-stopped \
  -p 127.0.0.1:8765:8765 -v prediction-agent-data:/data \
  -v prediction-agent-home:/root ghcr.io/xnetsc/prediction-market-agent:latest
```

除回环地址本地开发外，Passkey 要求 HTTPS。Railway、Vercel、API Gateway 和函数计算的公网入口都应保留
平台 TLS 终止与转发的原始 HTTPS scheme/host；不要用裸 HTTP 公网地址初始化管理员。

## Railway

仓库根目录的 `railway.toml` 和 `Dockerfile` 可由 Railway 从 GitHub 直接导入，配置字段遵循
[Railway Config as Code](https://docs.railway.com/config-as-code/reference)。容器自动在 `/data` 初始化，
并使用 Railway 提供的 `PORT`。部署前必须在服务上创建 Volume 并挂载到 `/data`，否则配置、Passkey、SQLite
台账和状态会随实例替换丢失。默认入口运行 `serve`：可以先注册 Passkey、补全配置；配置就绪后，对应平台插件
会在同一长驻进程内自动启动。`container-with-worker.sh` 仅是兼容旧部署的别名，也会进入相同单进程入口。
镜像仅信任回环地址和 Railway 官方私网段的转发头，以便把 Railway 的 `X-Forwarded-Proto: https` 还原为
Passkey 所需的 HTTPS Origin；若在其他反向代理后使用同一镜像，应按该代理的实际来源网段覆盖
`FORWARDED_ALLOW_IPS`，不要无条件信任任意客户端转发头。

## Vercel

`vercel.json` 与 `api/index.py` 提供可直接从 GitHub Import 的
[Python Function](https://vercel.com/docs/functions/runtimes/python) ASGI 入口。Vercel Function 是请求驱动
且有执行时长限制，因此只能承载管理界面和部署预览，不能可靠保持平台插件拥有的后台扫描线程。项目不提供
Vercel Cron 代替插件内部调度，因为那会把平台扫描策略错误地移到框架或部署平台。

Vercel 没有可挂载的持久本地工作目录。当前入口会使用 `/tmp/prediction-market-agent`，只适合构建验证或接入
持久存储前的管理界面预览；实例回收后本地配置、Passkey 和 SQLite 会丢失。完整机器人应部署到本机、虚拟机、
Railway 或其他长驻容器环境。

## AWS Lambda

`deploy/aws/template.yaml` 声明 API Gateway HTTP 管理入口。部署：

```bash
sam build --template-file deploy/aws/template.yaml
sam deploy --guided
```

模板没有 EventBridge 计划任务。Lambda 在请求结束后可能冻结实例，无法保证平台插件的后台扫描线程持续运行；
因此该适配只用于管理界面、部署验证或显式的非 HTTP 单轮调用，不是完整机器人的长驻部署方式。生产管理数据还需
挂载 EFS 到 `/data`；未挂载时仅使用 `/tmp/prediction-market-agent`，冷启动后数据不保证存在。

## 阿里云函数计算

`deploy/aliyun/s.yaml` 使用
[函数计算 Web 函数自定义运行时](https://help.aliyun.com/en/functioncompute/custom-runtime/)，监听官方默认
`0.0.0.0:9000`。构建脚本通过 `linux/amd64` Python 3.11 容器生成与 `custom.debian12` 兼容的依赖，
避免把 macOS/ARM 原生 wheel 上传到函数。先构建依赖、验证模板，再部署：

```bash
./deploy/aliyun/build.sh
npx -y @serverless-devs/s@latest -t deploy/aliyun/s.yaml verify
npx -y @serverless-devs/s@latest -t deploy/aliyun/s.yaml deploy
```

模板通过 `vpcConfig: auto`、`nasConfig: auto` 创建持久 NAS，默认挂载点
`/mnt/prediction-market-agent` 会被程序自动选为工作目录。模板不声明定时触发器；函数空闲时同样可能冻结后台
线程，因此这里只提供管理界面和部署验证。平台保留的 `/invoke` 路径可执行一次显式调用，但不能取代平台插件
内部的周期调度。

## 持久化与平台选择

机器人不是无状态网页：应用配置、插件私有配置、Passkey、会话、账户镜像和决策台账都需要持久化。推荐：

- 本机、虚拟机、Railway、长驻容器：完整机器人，`serve` 单进程同时承载 Web、平台插件扫描线程与通用事件循环；
- AWS Lambda、阿里云函数计算：适合管理入口、显式单轮调用和部署验证，不能保证插件扫描线程常驻；
- Vercel：适合管理入口和预览，不能依赖 `/tmp`，也不能承载完整机器人的周期扫描。

部署成功不代表平台凭证、Provider 或风控配置完整。首次登录后应在界面逐项检查，再用 `doctor` 和
`provider-test` 验证；真实交易仍由启用插件和其私有配置决定。
