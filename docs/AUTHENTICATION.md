# 管理员 Passkey 与加密会话

## 首次初始化

访问目标是 `localhost`、`127.0.0.0/8` 或 `::1` 时直接进入管理页面，不注册 Passkey、不创建登录会话、
不执行 ECDH/AES-GCM。页面把同样的业务操作以 JSON 发送到 `POST /api/local`。该入口只接受回环主机，
拒绝来自其他 Origin 的浏览器请求。判定使用请求 Host，不采用 `X-Forwarded-Host` 或 `X-Forwarded-For`。
本地 Compose 只绑定 `127.0.0.1`；公网反向代理必须验证允许的域名并保留真实公网 Host，不能改成 localhost。

以下认证流程适用于其他访问主机。客户端 socket IP 可能因容器或反向代理变成内部地址，不用于选择免认证路径。

Web 应用没有默认密码。非回环地址访问且认证数据库尚无凭据时，第一个访问者只能进入初始化页，并必须注册首个 admin
Passkey；注册完成前，决策、记录、插件和配置数据均不可读取。当前只有一个 admin 用户，但该用户可以持有
多个 Passkey。删除操作在事务中检查数量，最后一个 Passkey 永远不能删除。

Passkey 依赖安全上下文：线上必须使用 HTTPS；本地开发可使用 `http://localhost`、`127.0.0.1` 或 `::1`。
Passkey 绑定注册时的 RP ID 和 Origin，迁移域名之前应在旧域名保留可用入口或制定凭据迁移方案。

## Passkey 绑定 ECDH

注册首个 Passkey 或每次登录时，浏览器生成临时 P-256 ECDH 密钥对，服务端也生成临时 P-256 密钥对。
WebAuthn challenge 是随机熵、客户端公钥和服务端公钥规范表示的 SHA-256 摘要，因此 Passkey 对
`clientDataJSON` 的签名同时确认本次密钥交换。WebAuthn 验证成功后，双方执行 ECDH，再使用相同 challenge
作为 HKDF-SHA-256 salt 派生 256 位 AES-GCM 会话密钥。服务端不会接受未通过该 Passkey ceremony 的密钥。

认证器签名计数器不是登录条件。部分硬件、平台认证器和同步 Passkey 不实现单调计数，可能持续返回 `0`、
在设备之间切换后回退，或重复返回同一数值。服务端仍保存见过的最高值作为诊断信息，但不会因计数为零、
不递增或回退而拒绝已通过 challenge、Origin、RP ID、用户验证和公钥签名校验的登录。计数异常只能作为
克隆或设备故障的弱信号，不能单独证明凭据被克隆。

浏览器把不可导出的 AES `CryptoKey` 按 `session_id` 保存在 IndexedDB；服务端只把随机会话 token 放在
`HttpOnly`、`SameSite=Strict` Cookie 中。会话连续 72 小时未操作则失效，每次有效请求刷新闲置计时；
从登录时刻起最长 7 天，无论是否持续操作都必须重新登录。两项分别由 `admin_session_hours`（默认 72）和
`admin_absolute_session_hours`（默认 168）配置。

## 加密业务通道

登录后的业务操作统一发送到 `POST /api/secure`。请求和响应负载分别以固定 AAD 做 AES-GCM 认证加密，
12 字节随机 nonce 使用一次后写入 SQLite；相同会话重放 nonce 会被拒绝。健康检查、认证状态、WebAuthn
options/verify 和清除失配 Cookie 是认证引导端点，因此不承载决策、交易或配置数据，也不经过业务信封。
TLS 仍然是必要前提：它保护 HTML、认证引导元数据、HTTP 头和流量特征。

如果浏览器 Cookie 还在、IndexedDB 中的会话密钥却已清除，页面会注销该失配会话并要求重新使用
Passkey 登录，不能退化为明文业务请求。

## Passkey 和设备会话管理

登录后“管理员安全”界面支持：

- 新增、重命名和删除 Passkey；
- 查看各 Passkey 对应的登录设备、浏览器 User-Agent 和来源地址；
- 查看登录、最近请求、闲置到期和绝对到期时间；数据库统一存 UTC，浏览器用自身时区显示；
- 单独或批量踢出会话；删除 Passkey 会同时踢出该 Passkey 创建的全部会话；
- 退出当前会话。

`auth_db` 决定认证 SQLite 的位置，`session_db` 决定决策与审计 SQLite 的位置。二者都属于最早加载的
应用配置：进程先完整解析 `--config` 并把相对路径解析为绝对路径，之后才创建任何数据库连接。界面修改
路径只影响重启后的新进程，不会在运行中一半写旧库、一半写新库。
