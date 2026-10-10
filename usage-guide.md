# Kiro Inspector 使用指南

在仓库根目录下用 bash 执行。需要 AWS CLI、Python 3（含 boto3）和 docker buildx。以下以东京区域为例：

```bash
export AWS_REGION=ap-northeast-1
```

细节和排障见 `wecom-eventbridge-deploy.md`。

## 1. 打包镜像并上传

AgentCore Runtime 只运行 **arm64** 镜像。在 x86 机器上构建，要先装一次 QEMU。

```bash
# 把 Kiro CLI 放进 build/（只需要 kiro-cli-chat 这一个文件）
curl -fsSLo /tmp/kirocli.zip https://prod.download.cli.kiro.dev/stable/2.21.4/kirocli-aarch64-linux.zip
mkdir -p build/kiro/bin && unzip -o -j /tmp/kirocli.zip kirocli/bin/kiro-cli-chat -d build/kiro/bin

docker run --privileged --rm tonistiigi/binfmt --install arm64    # x86 构建机只需执行一次
REGION=$AWS_REGION TAG=v7 bash scripts/build_push.sh             # 每次构建用新 tag；最后一行输出镜像 URI
```

`build_push.sh` 会自动建 ECR 仓库、登录 ECR，然后用 `buildx --platform linux/arm64 --push` 构建并上传。

## 2. 部署和调试

**第一次部署**：先按部署手册第 4 节步骤 3 建执行角色，再创建 runtime：

```bash
export KIRO_API_KEY='<Kiro API key>'
python3 scripts/create_rt.py --region $AWS_REGION --image <上一步输出的 URI>   # KIRO_API_KEY 自动从环境读取
```

**已有 runtime，只换镜像**：

```bash
python3 scripts/update_rt.py --region $AWS_REGION --runtime-id <RUNTIME_ID> --image <URI> --wait
```

**调用一次**：等 runtime 变成 READY 后，再等约 60 秒（旧容器还会接一会儿请求），然后：

```bash
R="--region $AWS_REGION --runtime-id <RUNTIME_ID>"
python3 scripts/invoke_rt.py $R --no-notify             # 只看巡检结果，不推送
python3 scripts/invoke_rt.py $R "巡检本区域 RDS 待维护项"  # 自定义提示词，并推送到已配置的渠道
```

调用成功的判据：`exit_code=0`，且返回的 `delivery` 里 `ok=true`。容器日志在 CloudWatch 的 `/aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT`。

## 3. 设置定时任务

用 EventBridge Scheduler 每天调用一次 runtime。一条命令就能建好（默认每天北京时间 9:00）：

```bash
python3 scripts/create_schedule.py $R                  # 建 / 更新每日定时
python3 scripts/create_schedule.py $R --test-in 3      # 另建一个 3 分钟后执行一次的测试任务
```

要改时间，去控制台 **EventBridge → Scheduler → 计划 → `kiro-inspector-daily`** 改 cron 表达式。要改巡检内容，看附录 A。

定时调用走异步：runtime 先立即返回，巡检在后台跑完后推送结果。要暂停，就在控制台把这个计划停用（Disable）。

---

## 附录 A：巡检提示词在哪里改

提示词没有写死在镜像里。每次调用时随 payload 传进去，改完不用重新打包。

- **定时任务**：提示词存在 Scheduler 里。打开控制台 **EventBridge → Scheduler → 计划 → `kiro-inspector-daily` → 编辑 → 目标**，找到输入 JSON 中的 `Payload`，改其中的 `prompt`。`Payload` 本身是一个 JSON 字符串，引号要保留转义（`\"`）。`async`、`state_mode` 两个字段不要动。

  也可以用命令改。脚本会整体覆盖已有计划，时间也会重置成 `--cron` 的值（默认每天 9:00），所以在控制台改过时间的话要一起带上：

  ```bash
  python3 scripts/create_schedule.py $R --prompt-file my-prompt.txt --cron 'cron(0 9 * * ? *)'
  ```

- **手动调用**：提示词直接写在命令里，或者用 `--prompt-file` 从文件读取。不带提示词时，用脚本内置的默认提示词（检查 RDS 和 ElastiCache 待维护项），不会读取 Scheduler 里的那份。

  ```bash
  python3 scripts/invoke_rt.py $R --prompt-file my-prompt.txt
  ```

- **固定规则**：`build/server.py` 里的 `BASE_SYSTEM`（只读、不编造、中文报告），以及开了推送时追加的"把报告写入文件"说明，每次调用都会拼在上面的提示词前后。要改这部分，得重新打包镜像。

其他脚本里的 `DEFAULT_PROMPT` 只是兜底用的默认值：`create_schedule.py` 的那份，只有运行这个脚本又没带 `--prompt` / `--prompt-file` 时才会写进 Scheduler。

提示词里不要写密钥。推送渠道由环境变量控制（附录 C），不用在提示词里写怎么发送。

## 附录 B：Kiro API key

登录 [app.kiro.dev](https://app.kiro.dev/) 生成 API key。key 只在生成时显示一次，要当场复制保存。

只有 Pro / Pro+ / Pro Max / Power 订阅可以生成 API key。由管理员管理的订阅，需要管理员先开通这项功能。见 [Kiro 官方 headless 文档](https://kiro.dev/docs/cli/headless/)。

## 附录 C：通知渠道

在 runtime 环境变量里配置推送渠道。密钥类变量先 `export`，再用 `update_rt.py` 的 `--set` 写入；非密钥变量用 `--set-value`。

| 渠道 | 环境变量 | 申请方式 |
|---|---|---|
| 企业微信智能机器人（推荐） | `WECOM_BOT_ID`、`WECOM_BOT_SECRET`、`WECOM_CHAT_ID` | 管理后台 → 智能机器人 → API 模式选「长连接」，拿到 BotID 和 Secret。单聊时 `WECOM_CHAT_ID` 填成员 userid，对方要先给机器人发过一条消息。巡检要用专用机器人，因为新的长连接会把旧连接踢掉。[文档](https://developer.work.weixin.qq.com/document/path/101463) |
| 企业微信群 webhook | `WECOM_WEBHOOK_KEY` | 内部群 → 右上角「…」→ 消息推送 → 添加，复制 URL 里的 key。[文档](https://open.work.weixin.qq.com/help2/pc/14931) |
| 飞书 | `LARK_APP_ID`、`LARK_APP_KEY`、`LARK_RECEIVE_ID` | 在飞书开放平台建自建应用，开通发消息权限。`LARK_RECEIVE_ID` 默认填群的 chat_id。[文档](https://open.feishu.cn/document/server-docs/im-v1/message/create) |

示例：

```bash
export WECOM_BOT_ID=... WECOM_BOT_SECRET=...
python3 scripts/update_rt.py $R --set WECOM_BOT_ID --set WECOM_BOT_SECRET --set-value WECOM_CHAT_ID=<userid> --wait
```

凭据齐全的渠道会自动启用。某个渠道临时不用，可以设 `--set-value WECOM_CHANNEL=OFF` 或 `LARK_CHANNEL=OFF` 关掉，凭据不用删。
