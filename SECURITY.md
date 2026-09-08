# 敏感信息与发布安全

## 不进入版本控制的内容

- `.env` 及其本机变体；仓库只保留空白、可公开的 `.env.example`。
- `config/plugins/*.json`：API、Provider、研究、风控和 Hook 的私有运行配置。
- `*.key`、`*.pem`、`*.p12`、`*.pfx`、`*.jks`、`*.keystore` 等签名材料。
- SQLite 会话库、账户镜像、日志、PID、构建目录和测试缓存。

公开示例只放在 `examples/plugin_configs/`，秘密字段必须为空。插件管理界面对秘密字段不回显，保存空值
时保留已有秘密；这项界面行为不能代替版本控制与运维侧的密钥管理。

## 发布前检查

```bash
git status --short
git diff --cached --name-only
git grep -n -I -E '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|api[_-]?key[[:space:]]*[:=][[:space:]]*[^"[:space:]]+)'
```

还应人工复核暂存区、提交信息和远端仓库说明。若秘密曾进入 Git 历史，应立即在平台侧轮换秘密；仅删除
当前文件不能撤销已经公开的历史内容。

## 线上请求

程序没有线上/非线上执行模式。交易、撤单、赎回和转账会调用已启用 API 插件的真实传输；远端是否接受
由插件配置、账户状态与平台权限决定。运行生产测试或交易进程前，应确认所用账户、输入与权限符合预期。
