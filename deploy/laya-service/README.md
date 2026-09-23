# 本地决策模型服务（laya）

粗筛用的决策模型，跑在你自己的机器上，**不产生模型调用费用**。模型推理在本机完成；首次启动下载模型，服务启动及此后每 6 小时会向 GitHub 检查 webtorch SDK 更新。

## 为什么不在当前机器人容器里跑

它需要浏览器 WebGPU。当前机器人镜像没有配置这套 GPU 推理环境，也不会启动 Laya。
Docker 容器能否使用 GPU，取决于宿主系统、显卡和容器运行配置；不能说所有容器都不支持。
本方案把它作为独立服务，供用户在确认 WebGPU 可用的机器上自行运行。

管理页只提供说明、下载和连接测试。`http://host.docker.internal:8899/v1` 是一种示例地址，
**不是对当前机器的检测结果**；请在页面填写机器人容器实际能访问的地址。

## 怎么启动

```bash
bash start.sh
```

第一次会装 Playwright、按需装一个无头 Chromium、把模型（约 800MB）下载到 `models/laya/`。
之后每次启动都从本地读取模型权重，不再重复下载权重。

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
http://host.docker.internal:8899/v1     # 适用于能解析并连接此宿主机名的容器环境
http://127.0.0.1:8899/v1                # 仅适用于调用方与服务共享网络命名空间
```

服务默认只监听本机回环地址。若容器或另一台机器无法访问，不要直接把无认证接口暴露到公网；
应按部署环境配置受控的网络转发，再把可访问地址填到页面。

API 是 OpenRouter 的 chat-completions 形状，所以任何能调 OpenRouter 的东西都能调它：

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
| `--models` | `./models` | 模型落盘位置 |
| `--endpoint` | `https://huggingface.co` | 首次下载模型的来源，可换镜像 |
| `--headless false` | 无头 | 想看看浏览器里发生了什么时用 |

需要 `curl` 访问 GitHub 检查 SDK；模型只在首次缺失时下载。代理：SDK 检查使用 curl 的代理环境变量；服务也会读 `HTTPS_PROXY` / `HTTP_PROXY`。
