# Telegram 频道消息推送 for Feishu v1.0.0
![Telegram Channel for Feishu Logo](https://raw.githubusercontent.com/PANGPANGDigital/Telegram-Channel-for-Feishu/main/assets/logo.png)

<p align="center">
  <img src="https://raw.githubusercontent.com/PANGPANGDigital/Telegram-Channel-for-Feishu/main/assets/logo.png" width="200">
</p>

<h1 align="center">
Telegram Channel for Feishu
</h1>

<p align="center">
🚀 Monitor Telegram channels and push messages to Feishu Webhook
</p>

<p align="center">
Telegram 频道监控 + 飞书通知机器人
</p>


这是一个轮询 Telegram 公开频道页面，并通过飞书自定义机器人 Webhook 发送富文本通知的服务。

本项目不登录 Telegram 个人账号，也不需要创建或加入 Telegram 机器人。程序通过本地 HTTP 代理访问公开频道的 `https://t.me/s/<频道>` 页面，按配置间隔检查新消息。首次启动会推送正在监控的频道列表，并尝试推送每个频道当前最新的一条文字消息。

## 功能

- 轮询多个 Telegram 公开频道
- 支持 `@ChannelName` 和 `https://t.me/ChannelName` 配置格式
- 新消息推送到一个飞书 Webhook
- 飞书使用 `post` 富文本消息
- 支持飞书 Webhook 签名密钥
- 支持 Telegram 公开频道页面的本地 HTTP/HTTPS 代理
- 首次启动和后续重启都会推送当前监控频道列表
- 使用 Docker 命名卷持久化初始化状态和已推送消息 ID
- Docker Compose 使用 host 网络
- Dockerfile 基础镜像使用 DaoCloud DockerHub 镜像

说明：该方案依赖 Telegram 公开频道网页的 HTML 页面，不使用 Telegram 官方 Bot API。若 Telegram 调整公开页面结构或触发访问限制，解析和轮询可能需要适配；建议保持轮询间隔在 30 秒或更长。

## 前置准备

### 1. 创建飞书自定义机器人

创建群机器人并复制 Webhook 地址。如果启用了安全设置中的签名校验，同时复制签名密钥。

### 2. 准备群晖 NAS 的本地代理

确认代理服务监听在 NAS 主机地址和端口，例如：

```text
http://127.0.0.1:7890
```

如果代理运行在 NAS 的另一个 Docker 容器中，不要填写 `127.0.0.1`，应填写该代理容器在实际网络中的可访问地址。由于本项目使用 host 网络，代理必须允许来自 NAS 的连接。

## 配置

### 编辑 `config/config.yaml`

```yaml
proxy:
  enabled: true
  url: http://127.0.0.1:7890

channels:
  - name: 示例频道
    username: https://t.me/NewlearnerChannel
  - name: 另一个频道
    username: "@AnotherChannel"
```

### 编辑 `docker-compose.yml`

直接填写飞书环境变量，不需要创建 `.env` 文件：

```yaml
environment:
  FEISHU_WEBHOOK_URL: "https://open.feishu.cn/open-apis/bot/v2/hook/replace-me"
  FEISHU_SIGNING_SECRET: ""
```

## 群晖 NAS 部署

将整个项目目录复制到 NAS，例如 `/volume1/docker/telegram-feishu`，然后执行：

```bash
cd /volume1/docker/telegram-feishu
docker compose build
docker compose up -d
```

查看日志：

```bash
docker compose logs -f telegram-feishu
```

停止服务：

```bash
docker compose down
```

项目不会暴露端口，程序通过轮询 Telegram 公开频道页面接收消息。状态保存在 Compose 创建的 `telegram_feishu_data` 命名卷中；普通的 `docker compose down` 不会删除该卷，不要执行 `docker compose down -v`，否则下次启动会被视为首次启动。

## 推送行为

### 首次启动

推送一条飞书富文本消息，包含：

- 当前项目版本
- 公开频道网页轮询模式
- 正在监控的频道列表
- 每个频道当前最新文字消息（首次启动时）

### 后续重启

推送一条当前监控频道列表，并恢复已推送消息 ID。不会因为容器重启重复推送已经处理过的消息。

### 新消息

推送频道名称、消息时间、文字内容和 Telegram 原消息链接。纯图片、视频或文件等没有文字/说明文字的消息会被忽略。

## 常见问题

### 日志显示收不到频道消息

确认频道是公开频道；确认频道 username 与 `config/config.yaml` 中配置一致；确认 Telegram 代理可以访问 `https://t.me/s/<频道>` 页面。

### 飞书返回签名错误

确认 `FEISHU_SIGNING_SECRET` 与飞书机器人安全设置中的签名密钥完全一致。没有启用签名校验时保持为空。

### 想监听私有频道

当前方案只访问公开频道页面，不支持私有频道。私有频道需要改用 Telegram 用户账号登录的 MTProto 方案。
