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

脚本会创建 `.venv`、安装完整 wheel、首次初始化配置并启动 Web 应用。也可使用容器：

```bash
docker compose up --build
```

容器数据保存到 `runtime-data/`，管理界面为 `http://localhost:8765`。`serve` 同时负责管理界面和机器人运行
监督：全局与插件配置就绪且未暂停时自动启动对应平台插件，无需再启动第二个 Worker 进程。

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
