# 安装、启动与部署

## 程序入口

wheel 安装后提供 `prediction-market-agent` 命令；`python -m prediction_market_agent` 完全等价。核心入口：

- `init`：在当前目录初始化应用配置和插件启用状态；
- `serve`：启动管理、配置和审计 Web 应用；
- `once`：执行一个完整机器人周期后退出，适合函数计算和定时器；
- `run`：持续按配置间隔执行，适合本机、虚拟机和 Railway；
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

容器数据保存到 `runtime-data/`，管理界面为 `http://localhost:8765`。持续交易循环应作为同一持久工作目录的
第二个进程运行 `prediction-market-agent run`，避免 Web 进程重启中断循环。

除回环地址本地开发外，Passkey 要求 HTTPS。Railway、Vercel、API Gateway 和函数计算的公网入口都应保留
平台 TLS 终止与转发的原始 HTTPS scheme/host；不要用裸 HTTP 公网地址初始化管理员。

## Railway

仓库根目录的 `railway.toml` 和 `Dockerfile` 可由 Railway 从 GitHub 直接导入，配置字段遵循
[Railway Config as Code](https://docs.railway.com/config-as-code/reference)。容器自动在 `/data` 初始化，
并使用 Railway 提供的 `PORT`。部署前必须在服务上创建 Volume 并挂载到 `/data`，否则配置、Passkey、SQLite
台账和状态会随实例替换丢失。默认入口只启动 Web 管理服务，便于先注册 Passkey 和完成插件配置。确认配置后，
如需在同一个 Service 和同一个 Volume 中同时运行 Web 与持续交易，将 Railway 的 Custom Start Command 改为：

```bash
/app/deploy/container-with-worker.sh
```

该入口让两个进程共享 `/data`，避免误以为两个独立 Service 可以直接挂载同一个本地 SQLite Volume。若改用
独立 Worker Service，应先把会话、状态和任务协调迁移到两个 Service 都能访问的外部数据库/队列。
镜像仅信任回环地址和 Railway 官方私网段的转发头，以便把 Railway 的 `X-Forwarded-Proto: https` 还原为
Passkey 所需的 HTTPS Origin；若在其他反向代理后使用同一镜像，应按该代理的实际来源网段覆盖
`FORWARDED_ALLOW_IPS`，不要无条件信任任意客户端转发头。

## Vercel

`vercel.json` 与 `api/index.py` 提供可直接从 GitHub Import 的
[Python Function](https://vercel.com/docs/functions/runtimes/python) ASGI 入口。Vercel Function 是请求驱动
且有执行时长限制，因此只承载 Web 应用；周期交易应由 Vercel Cron 调用独立的单轮执行入口，不能运行
`run` 常驻循环。

Vercel 没有可挂载的持久本地工作目录。当前入口会使用 `/tmp/prediction-market-agent`，只适合构建验证或接入
持久存储前的预览；实例回收后本地配置、Passkey 和 SQLite 会丢失。生产部署必须为运行目录接入持久存储，
不能把临时文件系统当数据库。本限制会在首次页面中明确显示，避免把成功部署误认为可持续运行。

## AWS Lambda

`deploy/aws/template.yaml` 同时声明 API Gateway HTTP 入口和
[EventBridge Scheduler](https://docs.aws.amazon.com/lambda/latest/dg/with-eventbridge-scheduler.html) 单轮执行。部署：

```bash
sam build --template-file deploy/aws/template.yaml
sam deploy --guided
```

HTTP 事件由同一个 ASGI 应用处理，非 HTTP 计划事件执行一次 `TradingEngine.run_once()`。生产使用需挂载 EFS
到 `/data`；未挂载时仅使用 `/tmp/prediction-market-agent`，冷启动后数据不保证存在。EventBridge 频率必须
大于单轮最坏执行时间，避免并发轮次重复处理同一市场。

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
`/mnt/prediction-market-agent` 会被程序自动选为工作目录。HTTP 与默认禁用的定时触发器属于同一函数，因而
共享配置、Passkey 和 SQLite。启用 `scheduled-cycle` 后，函数计算把事件调用发到保留的 `/invoke` 路径，
程序每次只运行一轮；普通公网请求即使访问 `/invoke` 也缺少平台保留控制头，不能触发交易周期。

## 持久化与平台选择

机器人不是无状态网页：应用配置、插件私有配置、Passkey、会话、账户镜像和决策台账都需要持久化。推荐：

- 本机、虚拟机、Railway：常驻 Web + Worker，共享持久目录；
- AWS Lambda、阿里云函数计算：HTTP 函数 + 定时单轮函数，共享 EFS/NAS；
- Vercel：适合 Web 入口和预览，生产前必须接入外部持久存储，不能依赖 `/tmp`。

部署成功不代表平台凭证、Provider 或风控配置完整。首次登录后应在界面逐项检查，再用 `doctor` 和
`provider-test` 验证；真实交易仍由启用插件和其私有配置决定。
