# 敏感信息与发布安全

## 不进入版本控制的内容

- `config/application.json` 和 `config/plugin_directories.json`：本机运行覆盖值及插件目录。
- `config/plugins/*.json`：API、Provider、研究、Agent 行为风控和业务风控的私有运行配置。
- `*.key`、`*.pem`、`*.p12`、`*.pfx`、`*.jks`、`*.keystore` 等签名材料。
- SQLite 决策库、Passkey/认证库、账户镜像、日志、PID、构建目录和测试缓存。

公开示例只放在 `examples/plugin_configs/`，秘密字段必须使用 placeholder。插件管理界面对秘密字段不回显，保存空值
时保留已有秘密；这项界面行为不能代替版本控制与运维侧的密钥管理。

## 发布前检查

```bash
git status --short
git diff --cached --name-only
git grep -n -I -E '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|api[_-]?key[[:space:]]*[:=][[:space:]]*[^"[:space:]]+)'
```

还应人工复核暂存区、提交信息和远端仓库说明。若秘密曾进入 Git 历史，应立即在平台侧轮换秘密；仅删除
当前文件不能撤销已经公开的历史内容。

## 实盘与纸面交易

`paper_trading` 默认关闭。关闭时，交易、撤单、赎回和转账会调用已启用 API 插件的真实传输；远端是否
接受由插件配置、账户状态与平台权限决定。开启时，读取和报价仍访问真实平台，但最终写动作由本地纸面
传输处理，并写入独立的模拟账户镜像。界面和工具结果会标记 simulated；切换模式不会把模拟余额带入实盘。

生产 API 集成测试直接实例化 API 插件，不经过纸面交易包装。运行生产测试或实盘进程前，应确认所用账户、
输入与权限符合预期；纸面结果不证明真实订单能成交，也不构成收益保证。

## 管理面

回环主机访问默认免认证并使用明文 JSON；本地 Compose 仅绑定 127.0.0.1。公网代理必须校验允许的 Host 并
保留原始公网域名，不得把它重写为 localhost 或回环 IP。其他主机首次访问必须注册 admin Passkey，且至少
保留一个凭据。线上 Origin 必须为 HTTPS。每次登录使用由 Passkey
签名绑定的 P-256 ECDH 交换派生 AES-GCM 会话密钥，业务请求/响应采用认证加密并拒绝 nonce 重放。连续
72 小时未操作会失效，有效操作刷新闲置计时，但登录满 7 天后无条件重新认证。TLS 仍负责保护页面代码、
认证引导数据、Cookie 头和流量元数据，应用层信封不能代替 HTTPS。管理员可在界面查看各 Passkey 的设备
会话并踢出单个或多个会话。
