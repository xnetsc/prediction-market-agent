# 本地决策模型服务（laya）

粗筛用的决策模型，跑在你自己的机器上，**不花钱、不出网**（除了第一次下载模型）。

## 为什么不在容器里跑

它需要 GPU。webtorch 是编译成 WebAssembly 的 CPython，用 WebGPU 算；而 macOS 上的 Docker
不把 Metal 显卡透传给容器（容器里没有 `/dev/dri`），所以在容器里只能退回 CPU——800MB 的编码器
在 CPU 上逐个候选地跑，不是"慢一点"，是不可用。

所以：**服务跑在宿主机，机器人在容器里通过 `http://host.docker.internal:8899/v1` 调用它。**

## 怎么启动

```bash
bash start.sh
```

第一次会做三件事，之后都不再做：装 Playwright、按需装一个无头 Chromium、把模型（约 800MB）下载到
`models/laya/`。之后每次启动都从本地读，不再联网取模型。

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

在机器人界面的模型配置里，把粗筛评估器的地址填成：

```
http://host.docker.internal:8899/v1     # 机器人在容器里
http://127.0.0.1:8899/v1                # 机器人和它在同一台机器上
```

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

代理：服务会读 `HTTPS_PROXY` / `HTTP_PROXY`（Node 默认不读，这里替你处理了）。
