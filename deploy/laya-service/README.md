# 本地决策模型服务（laya）

粗筛用的决策模型，跑在你自己的机器上，**不产生模型调用费用**。模型推理在本机完成；首次启动下载模型，服务启动及此后每 6 小时会向 GitHub 检查 webtorch SDK 更新。

## 为什么不在当前机器人容器里跑

它需要浏览器 WebGPU。当前机器人镜像没有配置这套 GPU 推理环境，也不会启动 Laya。
Docker 容器能否使用 GPU，取决于宿主系统、显卡和容器运行配置；不能说所有容器都不支持。
本方案把它作为独立服务，供用户在确认 WebGPU 可用的机器上自行运行。

管理页只提供说明、下载和连接测试。`http://host.proxy.internal:8899/v1` 是本项目 Compose 提供的示例地址，
**不是对当前机器的检测结果**；请在页面填写机器人容器实际能访问的地址。

## 怎么启动

```bash
bash start.sh
```

第一次会装 Playwright、按需装一个无头 Chromium、把模型（约 800MB）下载到 `models/laya/`。
之后每次启动都从本地读取模型权重，不再重复下载权重。

文本模型和视觉模型的选源都由这个服务应用处理，不属于 SDK。若设置了完整下载地址，服务会先确认
该地址可用，再与 Hugging Face、魔搭和 Hugging Face 国内镜像一起测速，最终使用最快的可用来源；
Hub 都没有时仍可回退到完整地址。没有完整地址时，三个 Hub 都不可用就会明确失败。一次下载固定
使用一个来源，不会把不同仓库版本的文件混在一起。文本模型的完整地址可通过
`LAYA_MODEL_URL` 指向 `model.safetensors` 或其所在目录。

文本请求始终使用这个原有模型。只有第一次收到图片状态时，服务才会额外获取 Laya Vision 的
FP16 ONNX 导出（约 407MB）到 `models/laya-vision/` 并加载；没有图片请求就不下载、不占这部分显存。
服务会同时探测固定版本的 GitHub Release、Hugging Face 中国镜像、Hugging Face 官方站和魔搭，
只接受清单中尺寸与 SHA-256 完整的同一份导出，并在可用来源中按小样本下载速度自动选择；无需用户选择。
镜像只能改善连通性，不能替代不存在或需要授权的仓库；错误信息会区分授权、缺少导出和超时。
完整本地副本在断网时也可继续使用。

服务启动及此后每 6 小时会检查 [GitHub 的 webpytorch 主分支](https://github.com/xnetsc/webpytorch)。
只有版本变化或资源包缺文件时，才下载本服务使用的 `webtorch/`、浏览器运行文件 `dist/`、`LICENSE` 和 `NOTICE`，逐文件校验 Git blob 哈希后整包替换并重新载入模型；如果新版载入失败，则恢复上一版 SDK。网络检查失败也不会阻止已打包版本启动。`models/`、浏览器数据和机器人凭据不在更新范围内。
仓库另有每日定时的同步工作流；它把相同的依赖更新提交到仓库，再触发容器镜像重建。运行中的服务与新镜像分别按 GitHub 版本检查，不依赖本地源码目录。

模型按仓库自己的布局存在 `models/laya/`：

```
model.safetensors
rl_agent_config.json
encoder/config.json
tokenizer/tokenizer.json
```

**不要把它摊平。** SDK 是靠读 `model.safetensors` 的头来认出"这是决策模型"的，嵌套目录是它支持的形状；
在根目录另放一份 `config.json` 反而会让它当成语言模型去加载，然后在一个根本不存在的张量上失败。
同理，服务必须支持 Range 请求——那次"认不出模型"就是因为范围读被当成整文件返回了。

看到这行就是好了：

```
laya ready: convaiinnovations/laya on webgpu
```

`on webgpu` 很重要。如果是 `on cpu`，说明没拿到 GPU（页面不是安全上下文、或者浏览器没装上），
这时候它还能答，但慢到不能用。

## 怎么接到机器人

在机器人界面填写 Laya 服务地址并测试。下面只是常见示例，不是自动检测：

```
http://host.proxy.internal:8899/v1       # 本项目 Compose 默认提供的容器主机名
http://host.docker.internal:8899/v1     # 部分 Docker Desktop 环境提供
http://127.0.0.1:8899/v1                # 仅适用于调用方与服务共享网络命名空间
```

服务默认监听 `0.0.0.0`，以便容器访问宿主机。Laya API 无鉴权，启动后可能被同一网络内的设备访问：
务必用本机防火墙或隔离网络限制来源，不能直接暴露公网。不需要容器访问时可用
`bash start.sh --host 127.0.0.1` 收紧为仅本机访问。

API 是 OpenRouter 的 chat-completions 形状，所以任何能调 OpenRouter 的东西都能调它：

当前本地模型的 `max_prefixes` 为 6，`/health` 中的 `surface.takes.questions.max` 会给出实际题目上限；“20”指默认配置下单道选择题选项过多时的质量风险，不是每次最多 20 道题。机器人中的 Laya 评估器会自己排队、限制题数并缩短自动粗筛输入。Laya **服务自身**仅在启动时自动测速一次（一次预热、三次计时），不再周期测速；机器人不发起测速。测速等待已接收的推理结束，随后整组独占 WebGPU 队列；测速已排队或执行中，新推理请求立即返回 HTTP 429。服务端对已断开客户端仍在运行的 GPU 工作继续保持独占。

`GET /health` 的 `benchmark` 字段给出实时状态（`queued`、`running`、`ok`、`failed`）和最近测速结果。`POST /benchmark` 可手动触发一次，返回 202；若测速已经排队或执行中，则返回 `accepted: false`，不会重复排队。无需额外的 `GET /benchmark`。测速时，`/v1/chat/completions` 的 429 响应包含 `Retry-After: 1`、`error.code: benchmark_in_progress` 与完整 `benchmark` 状态；客户端可查看该状态自行决定等待、重试或切换其他评估器。机器人遇到该 429 不会在同一次调用里盲目重试，而会走已配置的评估器回退。

一次请求中的每道题都会与共享状态拼成自己的双向编码序列，因此状态文本的 token 数不等于实际编码工作量。响应 `usage.sequence_tokens` 给出每道题的序列长度，`encoder_tokens`、`encoder_passes` 和 `batched` 给出实际执行情况；标准的 `prompt_tokens` 是所有题目序列长度之和。`/health.benchmark.usage` 公开同一组指标，控制台测速状态会显示编码 token 与次数。完全相同的序列在同一请求内只编码一次。

图片与文本沿用同一个 `state/questions → answers` 接口。图片以 base64 传入，不能填写远程 URL：

```json
{
  "state": {
    "type": "multimodal",
    "text": "读取这张图表",
    "images": [{"type": "image", "media_type": "image/png", "data": "<base64>"}]
  },
  "questions": {
    "trend": {"type": "noul", "instructions": "走势正在上升", "criteria": {"true": "上升", "false": "未上升"}}
  }
}
```

相同图片的视觉特征按 SHA-256 缓存在服务页面内，后续问题会跳过视觉编码；响应中的
`usage.vision_cache_hits` / `vision_cache_misses` 可核验是否命中。`/health.latency_by_state`
分别记录文本与视觉调用延迟。GPU 队列仍只允许一个任务运行，但排队中的文本请求优先于尚未开始的
视觉请求，避免可选的慢路径拖长原有粗筛。官方 ONNX 导出仍然逐题执行文本图，因此这里没有虚称
多问题批处理或共享文本前缀。

SDK 代码不捆绑模型权重。`thaitea/laya-vision` 权重使用 CC BY-NC-SA 4.0；部署和使用前须按自己的
场景确认署名、非商业及相同方式共享要求。

该服务默认无鉴权且监听 `0.0.0.0`。`POST /benchmark` 也可被能访问该服务的设备调用；请通过防火墙限制来源，不要暴露公网。

```bash
curl -s http://127.0.0.1:8899/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "convaiinnovations/laya",
  "messages": [{"role": "user", "content": "{\"state\":{\"ticket\":\"重复扣款\"},\"questions\":{\"route\":{\"type\":\"choice\",\"instructions\":\"谁来处理\",\"criteria\":{\"billing\":\"账务\",\"tech\":\"技术\"}}}}"}]
}'
```

## 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | `8899` | 监听端口 |
| `--host` | `0.0.0.0` | 监听地址；如需仅本机访问，设为 `127.0.0.1` |
| `--models` | `./models` | 模型落盘位置 |
| `--endpoint` | `https://huggingface.co` | Hugging Face 候选端点；仍会与魔搭和国内镜像测速 |
| `--headless false` | 无头 | 想看看浏览器里发生了什么时用 |

需要 `curl` 访问 GitHub 检查 SDK；模型只在首次缺失时下载。代理：SDK 检查使用 curl 的代理环境变量；服务也会读 `HTTPS_PROXY` / `HTTP_PROXY`。

本仓库的自动同步每小时检查一次 SDK 主分支；检测到随附文件变化后提交 vendor 更新，并显式触发多架构容器构建。工作流也接受 `webtorch-main-updated` repository dispatch 以支持即时通知。容器发布前会从实际运行的 UI 下载 `laya-service.zip`，核对启动文件、SDK 提交标记和当前服务代码。
