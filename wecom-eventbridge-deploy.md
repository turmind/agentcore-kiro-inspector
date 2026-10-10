# Kiro 巡检 on Amazon Bedrock AgentCore Runtime —— 部署与运维手册

本手册覆盖：企业微信 / 飞书推送、每天定时巡检、从零部署、更新、验证、排障和完整卸载。

> **先读 [`SECURITY-NETWORK.md`](SECURITY-NETWORK.md)。** 全过程不开任何入站端口，不做任何公网可达的网络配置。那份文档第 3 节的禁止项和第 10 节的自检命令是本手册每一步的验收标准。

**整体流程**：EventBridge Scheduler 每天 09:00（Asia/Shanghai）调用一次 `InvokeAgentRuntime`，payload 里带 `async:true`。runtime 几秒内返回 `accepted`，然后在后台运行 Kiro 巡检。Kiro 只负责把报告写进文件，bridge（`server.py`）再按环境变量把报告推送到企业微信和/或飞书。所有链路都由我们这一侧主动发起（出站），没有入站。

日常改巡检内容、切通知渠道或手动运行，直接看[使用指南](usage-guide.md)。

---

## 1. 企业微信的两种接法

v6 起支持两种，两者都配时用智能机器人：

| | 智能机器人长连接（1.1） | 消息推送 webhook（1.2） |
|---|---|---|
| 变量 | `WECOM_BOT_ID` + `WECOM_BOT_SECRET` + `WECOM_CHAT_ID` | `WECOM_WEBHOOK_KEY` |
| 发到哪 | 单聊（某个成员）或群 | 只能是建 webhook 的那个内部群 |
| 出站 | `wss://openws.work.weixin.qq.com` | `https://qyapi.weixin.qq.com` |

### 1.1 智能机器人长连接

依据：[智能机器人长连接](https://developer.work.weixin.qq.com/document/path/101463)。

- **后台配置**：在管理后台打开智能机器人的配置页，开启「API 模式」并选择「长连接」，然后取 BotID 和 Secret。这个 Secret 是长连接专用的，和回调模式的 Token / EncodingAESKey 不是一回事。API 模式只能二选一：切到「设置接收消息回调地址」后，长连接会失效。
- **每个机器人同一时间只能有一个长连接**，新连接订阅成功后会把旧连接踢掉。所以巡检必须用专用机器人，不能和其他常驻客户端共用。bridge 每份报告只建一次连接：连接 → 订阅 → 逐条发送 → 断开。订阅有频率保护，不要反复重试。
- **`WECOM_CHAT_ID`**：单聊填成员的 userid，`WECOM_CHAT_TYPE=1`（默认）；群聊填群 chatid，`WECOM_CHAT_TYPE=2`。**对方必须先给机器人发过一条消息，机器人才能主动推给他。**
- **限制**（`notify.py` 已处理）：只发 markdown，每条最多 20480 字节，超长时切成多条，每条间隔 0.5 秒；每个会话每分钟最多 30 条、每小时最多 1000 条。
- **排查订阅失败**：回执里的 errcode 直接写在 `delivery` 里（`step: "subscribe"`）。常见报错 `853000 invalid bot_id or secret`：先核对 BotID 和 Secret 是否是同一个机器人的配对，再查后台模式与密钥。我们遇到过 Secret 正确、BotID 填错也报这个码，不能仅凭错误码断言是 Secret 或长连接模式有错。

### 1.2 消息推送 webhook（申请 key）

> 代码已支持（`notify.py` 的 `send_wecom_webhook`），并通过离线测试，**未在真实企业微信上验证**。实际跑通的是 1.1 的长连接。

- **入口**：打开**内部群** → 右上角「…」→ 消息推送 → 添加，PC 端和手机端都有。v4.1.41 起「群机器人」改名为「消息推送」。见[设置消息推送](https://open.work.weixin.qq.com/help2/pc/14931)。
- **前提条件**（出处同上）：
  - 只支持内部群。
  - 群开了「仅群主管理」时，普通成员看不到这个入口。
  - 管理员可以在「管理后台 → 应用管理 → 消息推送」里设置哪些成员能创建消息推送。
- **取 Webhook URL**：**只有创建者**能在消息推送详情里看到，格式是 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<KEY>`。配置 `WECOM_WEBHOOK_KEY` 时，填 `<KEY>` 或整个 URL 都可以。见[消息推送配置说明](https://developer.work.weixin.qq.com/document/path/99110)。
- 官方要求保护好这个地址，不要分享到 github 等公开位置。所以 key 只放在 runtime 环境变量里，不进代码库，也不出现在命令行参数里。
- **平台限制**（`notify.py` 都已处理）：
  - 单条长度：text 2048 字节，markdown / markdown_v2 4096 字节。
  - 频率：每个消息推送每分钟最多 20 条。
  - markdown_v2 不支持字体颜色和 @；客户端低于 4.1.36（安卓低于 4.1.38）时显示为纯文本。
  - markdown 不支持缩进。
  - 群开了全员禁言时推送会失败。

---

## 2. 代码结构

```
SECURITY-NETWORK.md          网络与权限安全基线（部署前必读）
build/Dockerfile             arm64 镜像：python 3.12-slim + AWS CLI + flask + kiro-cli-chat
build/server.py              Flask bridge：/invocations + /ping。负责调 Kiro、读报告文件、调 notify
build/notify.py              推送模块，只用标准库，可以 import，也可以命令行调用
agents/sre.json              Kiro agent 配置示例（KIRO_AGENT 用）
scripts/build_push.sh        构建 arm64 镜像并推到 ECR（不建仓库策略）
scripts/create_rt.py         从零新建 runtime，建完自动开 MMDSv2；VPC 模式先做只读网络检查
scripts/update_rt.py         换镜像、改环境变量、开 MMDSv2，其余配置原样回填
scripts/invoke_rt.py         用 SigV4 手动调用一次
scripts/create_schedule.py   建每日定时（也能建一次性测试调度、删除）
iam/runtime-trust-policy.json      runtime 执行角色的信任策略
iam/runtime-execution-policy.json  runtime 执行角色的权限：ECR 拉取、日志、X-Ray、20 个只读巡检动作
iam/scheduler-trust-policy.json    Scheduler 角色的信任策略
iam/scheduler-invoke-policy.json   Scheduler 直接调 runtime（universal 模式）
iam/deployer-policy.json           部署者在 BedrockAgentCoreFullAccess 之外需要的权限（见第 3.2 节）
tests/                       test_notify.py 34 个、test_trigger.py 6 个、test_create_rt.py 14 个，全部离线
```


### 2.1 渠道开关

- 配了 `WECOM_BOT_ID` + `WECOM_BOT_SECRET`（长连接），或者配了 `WECOM_WEBHOOK_KEY`，就发企业微信，两种都配时走长连接；`LARK_APP_ID` 和 `LARK_APP_KEY` 都配了就发飞书；两者都配了两边都发；都没配就不发。
- 不删凭据也能关掉某个渠道：设 `WECOM_CHANNEL=OFF` 或 `LARK_CHANNEL=OFF`。
- v4 镜像要设 `LARK_CHANNEL=ON` 才会让 Kiro 发飞书；本版本不再需要这个开关，留着也不影响。

### 2.2 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `KIRO_API_KEY` | 是 | Kiro 无头认证。没配时 `/invocations` 返回 500 |
| `WECOM_BOT_ID`、`WECOM_BOT_SECRET` | 否 | 企业微信智能机器人凭据（长连接）。两个都是机密，只能用 `--set` |
| `WECOM_CHAT_ID` | 用长连接时必填 | 单聊填 userid，群聊填 chatid |
| `WECOM_CHAT_TYPE` | 否 | `1` 单聊（默认）/ `2` 群聊 / `0` 由企业微信判断 |
| `WECOM_WS_URL` | 否 | 只在测试时用，把长连接指向本地假服务器 |
| `WECOM_WEBHOOK_KEY` | 否 | 企业微信 key 或整个 URL |
| `WECOM_MSGTYPE` | 否 | 只对 webhook 生效：`markdown_v2`（默认）/ `markdown` / `text` |
| `WECOM_MAX_CHUNKS` | 否 | 一份报告最多发几条，默认 5 |
| `LARK_APP_ID`、`LARK_APP_KEY` | 否 | 飞书应用凭据 |
| `LARK_RECEIVE_ID` / `LARK_RECEIVE_ID_TYPE` | 否 | 接收方 id / 类型（默认 `chat_id`） |
| `LARK_API_BASE` | 否 | 默认 `https://open.feishu.cn`，Lark 国际版改成 `https://open.larksuite.com` |
| `WECOM_CHANNEL` / `LARK_CHANNEL` | 否 | 设 `OFF` 关闭对应渠道 |
| `KIRO_MODEL` / `KIRO_EFFORT` | 否 | 默认 `gpt-5.6-luna` / `max`（写在 Dockerfile 里） |
| `KIRO_TIMEOUT_SECONDS` | 否 | 单次 Kiro 运行上限，默认 780 秒 |
| `KIRO_STATE_MODE` / `KIRO_STATE_ROOT` / `KIRO_RESUME_STRATEGY` | 否 | 会话状态保存方式，见 README 第 9–15 条 |
| `KIRO_AGENT` | 否 | 使用的 Kiro agent 配置名 |
| `KIRO_TRUST_TOOLS` | 否 | 不设时 Kiro 以 `--trust-all-tools` 运行（见 SECURITY-NETWORK 第 9.1 节） |

`create_rt.py` 会自动加上 `AWS_REGION` 和 `AWS_DEFAULT_REGION`，并拒绝 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_SESSION_TOKEN`：runtime 只用执行角色，不注入任何 AWS 长期凭据。

### 2.3 分工与凭据可见性

- Kiro 只把报告写到 bridge 指定的 `report_path`，不自己发消息。
- bridge 启动 Kiro 子进程前，会把推送凭据（`WECOM_BOT_ID`、`WECOM_BOT_SECRET`、`WECOM_WEBHOOK_KEY`、`LARK_APP_ID`、`LARK_APP_KEY`）从子进程的环境变量里删掉，所以 Kiro 不会**被直接传入**这些值。**但这不等于 Kiro 读不到**：两者以同一个用户运行，Kiro 能通过 `/proc` 读到 bridge 进程的环境变量。`KIRO_API_KEY` 本来就在 Kiro 的环境变量里。详见 SECURITY-NETWORK 第 9.5 节。
- Kiro 结束后，由 bridge 调 `notify.send_report()` 推送。
- 没写报告文件时，推送 Kiro 的最终回答；Kiro 失败或超时时，推送「巡检未完成（原因）」。

### 2.4 在代码里调用 notify

```python
import notify
notify.enabled_channels()                 # ["wecom", "lark"]
r = notify.send_report(text)              # 发到所有已启用的渠道
r = notify.send_report(text, ["wecom"])   # 只发指定渠道
# [{"channel": "wecom", "ok": True, "mode": "bot", "messages": 1, "msgtype": "markdown"},
#  {"channel": "lark",  "ok": True, "message_id": "om_..."}]
```

`send_report` 的行为：
- 不会抛异常。失败时返回 `ok: False` 和 `error`，错误信息里的 key / secret 会被替换成 `***`。
- 企业微信超长时按行切分，多条前面加 `(i/n)`；webhook 每条间隔 1 秒，长连接每条间隔 0.5 秒；超过 `WECOM_MAX_CHUNKS` 条就截断并注明。长连接失败时结果里有 `step`（`connect` / `subscribe` / `send`）。
- 飞书固定发一条 text。

命令行用法（手动测试用）：

```
python3 build/notify.py --channels                   # 看启用了哪些渠道
python3 build/notify.py --file report.md --dry-run   # 看会切成几条、每条多少字节，不发送
python3 build/notify.py --file report.md             # 退出码：0 成功 / 1 有渠道失败 / 2 没配渠道
```

### 2.5 payload 字段

| 字段 | 说明 |
|---|---|
| `prompt` | 必填，巡检指令 |
| `async` | `true` 时立即返回 `{"status":"accepted","job_id":...}`；跑完后日志里打一行 `[job] {...}`，其中 `delivery` 是推送结果 |
| `notify` | `false` 时这次不推送 |
| `channels` | 例如 `["wecom"]`，只推送到已启用渠道中的这几个 |
| `state_mode` | `ephemeral` / `session` |
| `new_conversation` | `true` 时不续接之前的会话 |
| `thread` | 会话名，与 session id 解耦（见 README 第 15 条） |

扁平格式（`{"prompt": ...}`）和 `input` 嵌套格式（`{"input": {"prompt": ...}}`）都支持。`invoke_rt.py`、Scheduler 和 Lambda 发的都是扁平格式。

---

## 3. 前提条件与部署者权限

### 3.1 构建 / 部署机

- 一台 x86 或 arm64 Linux 机器，装好：Docker（≥ 28.0.0，见 SECURITY-NETWORK 第 4.3 节）+ buildx、AWS CLI v2、Python 3 + boto3（实测 1.43.25，需要认识 `bedrock-agentcore-control` 的 `metadataConfiguration`）、`curl`、`unzip`、`jq`。
- 出站能访问 SECURITY-NETWORK 第 7.2 节列的构建时目的地。**不需要任何入站**。
- 以下命令都在仓库根目录下执行。部署者身份建议用 SSO 或 assume-role 拿到的临时凭据。

### 3.2 部署者 IAM

部署者 = 托管策略 [`BedrockAgentCoreFullAccess`](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/BedrockAgentCoreFullAccess.html) + 本目录的 [`iam/deployer-policy.json`](iam/deployer-policy.json)（替换 `<REGION>`、`<ACCOUNT>` 后使用）。

托管策略覆盖与缺口（按 v21 核对）：

| 类别 | FullAccess 已包含 | FullAccess 不包含，由 deployer-policy 补齐 |
|---|---|---|
| AgentCore | `bedrock-agentcore:*`（含 Create / Update / Delete / Invoke） | —（deployer-policy 反而**显式 Deny** 掉不需要的高危动作，见下） |
| IAM | 读角色；`iam:PassRole` 仅限 `role/*BedrockAgentCore*` 且只能传给 `bedrock-agentcore`；创建 AgentCore 的服务关联角色 | 创建 / 修改 / 删除本方案的 2 个角色；把 Scheduler 角色 PassRole 给 `scheduler` |
| ECR | `DescribeRepositories`、`DescribeImages`、`ListImages` | 登录 token、建仓库、推镜像、删仓库 |
| Scheduler | 无 | schedule group 与 schedule 的增删改查 |
| EC2 | 无 | `create_rt.py` 网络预检和自检用的 Describe 类动作（只读） |
| Logs | 读 `/aws/bedrock-agentcore/*` | 卸载时删日志组 |

要点：

- **执行角色名必须包含 `BedrockAgentCore`**（本方案用 `KiroInspectorBedrockAgentCoreRole`），否则 FullAccess 的 PassRole 不覆盖，`create_rt.py` 会给出提示。沿用名字不含这个字符串的已有角色时，要额外授予对它的 `iam:PassRole`（条件 `iam:PassedToService=bedrock-agentcore.amazonaws.com`）。
- `deployer-policy.json` 的最后一条 `NeverOpenAnInboundPath` 是 **Deny**：`PutResourcePolicy`、`InvokeAgentRuntimeForUser`、`InvokeAgentRuntimeWithWebSocketStreamForUser`、`GetWorkloadAccessTokenForUserId`、Lambda Function URL、`lambda:AddPermission`、`ecr:SetRepositoryPolicy`。部署者即使拿着 FullAccess，也无法做出 SECURITY-NETWORK 第 3 节禁止的配置。FullAccess 包含 `GetWorkloadAccessTokenForUserId`，官方托管策略说明建议生产环境只给 `ForJWT`（[security-iam-awsmanpol](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-iam-awsmanpol.html)）。
- VPC 模式首次使用时，AgentCore 会自动创建服务关联角色 `AWSServiceRoleForBedrockAgentCoreNetwork`，由它在子网里建 ENI（FullAccess 已包含相应权限）。如果需要手动创建，官方给出的权限是（[agentcore-vpc](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)）：

  ```json
  {
      "Action": "iam:CreateServiceLinkedRole",
      "Effect": "Allow",
      "Resource": "arn:aws:iam::*:role/aws-service-role/network.bedrock-agentcore.amazonaws.com/AWSServiceRoleForBedrockAgentCoreNetwork",
      "Condition": {
          "StringLike": {
              "iam:AWSServiceName": "network.bedrock-agentcore.amazonaws.com"
          }
      }
  }
  ```

- `deployer-policy.json` 已通过 IAM Access Analyzer `validate-policy` 校验（0 findings）。

### 3.3 变量

| 变量 | 含义 | 示例 / 来源 |
|---|---|---|
| `REGION` | 部署 region | `ap-northeast-1` |
| `ACCOUNT` | 账号 ID | `aws sts get-caller-identity --query Account --output text` |
| `TAG` | 镜像 tag，每次构建用新值 | `v5` |
| `IMAGE_URI` | `build_push.sh` 最后一行的输出 | `<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/bedrock-agentcore-kiro-inspector:v5` |
| `RUNTIME_ID` | `create_rt.py` 输出 JSON 里的 `runtime_id` | `kiro_inspector-xxxxxxxxxx` |
| `SUBNET_1`、`SUBNET_2`、`RT_SG` | VPC 模式：私有子网、runtime 安全组 | 见 SECURITY-NETWORK 第 8 节 |

---

## 4. 从零部署

### 步骤 1：离线测试

```
python3 -B tests/test_notify.py && python3 -B tests/test_trigger.py && python3 -B tests/test_create_rt.py
```

期望 21 + 15 + 18 个用例全部 OK。`-B` 避免在代码目录里生成 `__pycache__`。

### 步骤 2：选网络模式

两种模式都没有入站，区别见 SECURITY-NETWORK 第 6 节。需要出站审计或出站白名单时选 VPC 模式，并先按 SECURITY-NETWORK 第 8 节准备好私有子网、NAT、安全组、VPC 端点和 Flow Logs。**VPC 模式下，子网所在的 AZ ID 是否在 AgentCore 支持列表里要人工核对**，脚本不检查这一项：

```bash
aws ec2 describe-subnets --region $REGION --subnet-ids $SUBNET_1 $SUBNET_2 --query 'Subnets[].[SubnetId,AvailabilityZoneId]'
```

### 步骤 3：建执行角色

```bash
mkdir -p /tmp/kiro-iam
for f in runtime-trust-policy runtime-execution-policy; do
  sed -e "s/<REGION>/$REGION/g" -e "s/<ACCOUNT>/$ACCOUNT/g" iam/$f.json > /tmp/kiro-iam/$f.json
done

# 先校验，期望 findings 为空
aws accessanalyzer validate-policy --region $REGION --policy-type IDENTITY_POLICY \
  --policy-document file:///tmp/kiro-iam/runtime-execution-policy.json --query 'findings'

aws iam create-role --role-name KiroInspectorBedrockAgentCoreRole \
  --assume-role-policy-document file:///tmp/kiro-iam/runtime-trust-policy.json
aws iam put-role-policy --role-name KiroInspectorBedrockAgentCoreRole \
  --policy-name KiroInspectorExecutionPolicy --policy-document file:///tmp/kiro-iam/runtime-execution-policy.json

rm -rf /tmp/kiro-iam
```

- 信任策略只允许 `bedrock-agentcore.amazonaws.com` 扮演，并用 `aws:SourceAccount` 和 `aws:SourceArn` 限定为本账号本 region 的 AgentCore 资源。
- 执行策略里的巡检权限只有 20 个只读动作，没有任何写权限，也没有 `bedrock:InvokeModel`（模型走 Kiro 自己的 API key）。需要巡检更多服务时，只追加 `Describe*` / `List*` / `Get*` 类动作。
- 刚创建的角色可能要几秒后才能被服务扮演。下一步如果报角色不能扮演，等 10 秒重试。

### 步骤 4：取 Kiro CLI，构建并推到 ECR

```bash
curl -fsSLo /tmp/kirocli.zip https://prod.download.cli.kiro.dev/stable/2.21.4/kirocli-aarch64-linux.zip
echo "27833a50da478339788b909a789cb2da84c50573b4743cfce5f0cec3cbbfcd0d  /tmp/kirocli.zip" | sha256sum -c
mkdir -p build/kiro/bin && unzip -o -j /tmp/kirocli.zip kirocli/bin/kiro-cli-chat -d build/kiro/bin
sha256sum build/kiro/bin/kiro-cli-chat   # 期望 13a8a907f86f9ff482990cb7aab22848d591de923427224fff3c851e7ab157f6
docker run --privileged --rm tonistiigi/binfmt --install arm64   # 每台 x86 构建机只需执行一次
REGION=$REGION TAG=v5 bash scripts/build_push.sh                 # 最后一行输出 IMAGE_URI
```

- 两个 sha256 的来源：`latest` manifest 只列出当前最新版的校验值，按版本号取 manifest 会返回 403，所以查不到 2.21.4 的官方值。上面两个值是我们本地下载后算出来的，与项目里一直在用的 `kiro-cli-chat` 一致。
- Kiro 已经发布 **2.24.1**，我们没测过。查最新版和对应 sha 的命令：
  `curl -fsSL https://prod.download.cli.kiro.dev/stable/latest/manifest.json | jq -r '.version, (.packages[] | select(.targetTriple=="aarch64-unknown-linux-gnu" and .fileType=="zip") | .sha256)'`。
  升级前先按 README 第 1 条重新验证 v1 引擎、`use_aws` 和模型。
- `build_push.sh` 做的事：
  - ECR 仓库不存在就创建，开启 scanOnPush。**不建仓库策略**，拉取权限只经本账号 IAM（执行角色）。
  - 登录密码先写到临时文件，再交给 `docker login`（非 TTY 环境直接用管道会失败）。
  - `buildx --platform linux/arm64 --push`，最后打印镜像 digest、大小和 URI。
  - 构建过程不起任何容器端口。
- runtime 按 tag 固定镜像，所以每次构建都用新 tag。在 x86 上 buildx 走 QEMU 模拟 arm64，比较慢，可以放到后台跑：
  `setsid env REGION=$REGION TAG=v5 bash scripts/build_push.sh > /tmp/bp.log 2>&1 < /dev/null &`

### 步骤 5：创建 runtime

凭据只从当前 shell 的环境变量读取（`--set KEY`），不出现在命令行参数里，也不会被打印。`KIRO_API_KEY` 必填。三个密钥（`KIRO_API_KEY`、`WECOM_WEBHOOK_KEY`、`LARK_APP_KEY`）如果用 `--set-value K=V` 传，脚本会直接报错。

```bash
export KIRO_API_KEY='<Kiro API key>'
export WECOM_WEBHOOK_KEY='<企业微信 key>'          # 可选

# PUBLIC 模式
python3 scripts/create_rt.py --region $REGION --image $IMAGE_URI --set WECOM_WEBHOOK_KEY --dry-run
python3 scripts/create_rt.py --region $REGION --image $IMAGE_URI --set WECOM_WEBHOOK_KEY

# VPC 模式
python3 scripts/create_rt.py --region $REGION --image $IMAGE_URI --set WECOM_WEBHOOK_KEY \
  --network VPC --subnet $SUBNET_1 --subnet $SUBNET_2 --security-group $RT_SG \
  --dry-run

unset KIRO_API_KEY WECOM_WEBHOOK_KEY
```

- 飞书：`export LARK_APP_ID=... LARK_APP_KEY=...` 后加 `--set LARK_APP_ID --set LARK_APP_KEY`；接收方用 `--set-value LARK_RECEIVE_ID=<chat_id>`。
- 角色默认用 `KiroInspectorBedrockAgentCoreRole`（`--role-name` 或 `--role-arn` 可改）。runtime 名默认 `kiro_inspector`。`--idle-timeout` 默认 60（`update_rt.py` 也可用 `--idle-timeout` 修改）、`--max-lifetime` 默认 28800（单位秒，范围 60–28800）。
- **`--dry-run` 只做只读调用**（STS、列出 runtime、EC2 Describe），并用 SDK 模型校验两个请求。确认输出的 `network`、`env_keys`（只有变量名）、`checks` 都符合预期后，去掉 `--dry-run` 再执行。
- **VPC 模式下自动做的只读检查**（在创建任何资源之前）：
  - 报错并停止：子网跨多个 VPC；VPC 的 DNS 两项没开；子网路由表有指向 IGW 的路由（公有子网）；runtime SG 的入站规则用了 CIDR 或 prefix list，或引用了其他安全组（runtime 不需要任何入站）。
  - 只警告：只有一个 AZ；子网没有指向 NAT 的默认路由；runtime SG 出站全开。
  - **不检查** AZ ID 是否在 AgentCore 支持列表里（见步骤 2）。
  - `--skip-network-checks` 会跳过上述全部检查，只允许在部署者确实没有 EC2 Describe 权限时使用，并且必须按 SECURITY-NETWORK 第 10 节手工补查。
- 执行流程：`CreateAgentRuntime` → 等待 READY → 带完整配置调 `UpdateAgentRuntime` 开启 `requireMMDSV2` → 等待 READY。分两步是因为 Create API 不接受 `metadataConfiguration`，而没开 MMDSv2 的 runtime 调用会被拒绝。
- 退出码：0 成功，stdout 输出一段 JSON（含 `runtime_id`、`runtime_arn`、`mmdsv2`）；2 表示参数错误、检查未通过或同名 runtime 已存在（已存在时请改用 `update_rt.py`）；1 表示 runtime 已建好但开 MMDSv2 失败，按脚本打印的 `update_rt.py --region .. --runtime-id .. --mmdsv2 --wait` 重试即可。
- 脚本不传 `requireServiceS3Endpoint`：新建的 runtime 传这个字段会报错（见第 5 节）。

### 步骤 6：手动调用一次

READY 之后新容器不一定马上接管请求，**等 60 秒再调用**。

```bash
python3 scripts/invoke_rt.py --region $REGION --runtime-id $RUNTIME_ID --no-notify   # 先不推送，只看结果
python3 scripts/invoke_rt.py --region $REGION --runtime-id $RUNTIME_ID               # 推送，响应里的 delivery 字段就是推送结果
```

- 不带 prompt 时，默认检查本 region 的 RDS / ElastiCache 待维护项。自定义巡检内容用位置参数或 `--prompt-file`。
- 默认同步调用，读超时 900 秒；`--async` 立即返回 `accepted`。
- 使用调用者自己的 AWS 凭据做 SigV4 签名，调用者需要对该 runtime 有 `bedrock-agentcore:InvokeAgentRuntime` 权限。

### 步骤 7：建每日定时

```bash
python3 scripts/create_schedule.py --region $REGION --runtime-id $RUNTIME_ID --dry-run     # 只读，打印将要发出的请求
python3 scripts/create_schedule.py --region $REGION --runtime-id $RUNTIME_ID --test-in 3   # 建每日调度，另外 3 分钟后触发一次测试
```

- 会创建三样东西：
  - schedule group `kiro-inspector`。
  - 角色 `KiroInspectorSchedulerRole`：信任策略限定本账号和这个 group；权限只允许对这一个 runtime 调 `InvokeAgentRuntime`。策略文件里的占位符由脚本自动替换。
  - schedule `kiro-inspector-daily`：`cron(0 9 * * ? *)`，时区 Asia/Shanghai。
- 调用目标是 universal target `arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime`：
  - `RuntimeSessionId` = `kiro-inspector-scheduled-run-<aws.scheduler.execution-id>`，每次都是新 session，也就是新 microVM。
  - `Payload` = `{"prompt":…,"async":true,"state_mode":"ephemeral"}`。
- 重试次数设为 0，FlexibleTimeWindow 为 OFF。重试等于再跑一遍巡检、再发一份报告；某天没跑成，从指标上能看出来。
- `--test-in` 建的是一次性 `at()` 调度，名字是 `kiro-inspector-test-<时间>`，执行后自动删除。
- 常用参数：`--prompt` / `--prompt-file`（改巡检内容）、`--cron` / `--timezone`、`--disabled`。重复执行就是更新；`--delete` 删除。
- 这一步不开任何网络入口，runtime 只通过 SigV4 调用。

**如果 `TargetErrorCount` 大于 0**，先试把参数名改成 camelCase：`--param-case camel --test-in 3`。（原来备用的 Lambda 中转方案已于 2026-10-09 删除：Scheduler 直调已经验证通过，一直没用上。）

可选：配 SQS 死信队列，需要给 Scheduler 角色加 `sqs:SendMessage`。死信消息属性里有 `ERROR_CODE` / `ERROR_MESSAGE`，见[配置 DLQ](https://docs.aws.amazon.com/scheduler/latest/UserGuide/configuring-schedule-dlq.html)。脚本没配。

### 步骤 8：验证

**功能验证**（大约 3 分钟加上一次巡检的时间）：

1. 群里收到报告。
2. 日志里出现 `[job]` 行，并且 `delivery` 里各渠道都是 `"ok": true`：
   `aws logs tail /aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT --region $REGION --since 30m --filter-pattern '"[job]"'`
3. CloudWatch 命名空间 `AWS/Scheduler`，维度 `ScheduleGroup=kiro-inspector`：`InvocationAttemptCount` 有值，并且 `TargetErrorCount` 为 0。指标定义见[官方说明](https://docs.aws.amazon.com/scheduler/latest/UserGuide/monitoring-cloudwatch.html)。

**安全验证**：执行 SECURITY-NETWORK 第 10 节的全部自检命令，结果全部合规才算部署完成。

---

## 5. 更新已有 runtime

```bash
# 智能机器人长连接（v6 起）
export WECOM_BOT_ID='<BotID>' WECOM_BOT_SECRET='<Secret>' WECOM_CHAT_ID='<userid 或 chatid>'
python3 scripts/update_rt.py --region $REGION --runtime-id $RUNTIME_ID --image $IMAGE_URI \
    --set WECOM_BOT_ID --set WECOM_BOT_SECRET --set WECOM_CHAT_ID --wait
unset WECOM_BOT_ID WECOM_BOT_SECRET WECOM_CHAT_ID

# 或者 webhook
export WECOM_WEBHOOK_KEY='<企业微信 key>'
python3 scripts/update_rt.py --region $REGION --runtime-id $RUNTIME_ID --image $IMAGE_URI --set WECOM_WEBHOOK_KEY --dry-run
python3 scripts/update_rt.py --region $REGION --runtime-id $RUNTIME_ID --image $IMAGE_URI --set WECOM_WEBHOOK_KEY --wait
unset WECOM_WEBHOOK_KEY
```

- `UpdateAgentRuntime` 是全量替换。脚本会先读出现有配置，把角色、网络、description、protocol、lifecycle、metadata、filesystem、request header、authorizer 和现有环境变量全部带回去，只改你指定的项。`--dry-run` 只打印变量名和镜像。
- 其他参数：`--set-value K=V`（非密钥）、`--unset KEY`、`--mmdsv2`（开 MMDSv2；没开时脚本会给出警告）。
- 现有 `LARK_*` 变量会保留，所以更新后两个渠道都会发。只想发企业微信，再加 `--set-value LARK_CHANNEL=OFF`。
- 状态变成 READY 后，旧容器还会继续服务一会儿，**等 45~60 秒再测**，并用新的 session id 调用（`invoke_rt.py` 默认每次生成新的 session id）。
- 执行角色名如果不含 `BedrockAgentCore`，回填角色时部署者需要对该角色有额外的 `iam:PassRole`（见第 3.2 节）。
- **`requireServiceS3Endpoint` 的风险**：官方 [VpcConfig](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_VpcConfig.html) 说明，这个字段从 2026-05-05 起逐步下线：之后新建的 runtime，在 Create 或 Update 里传它都会报 ValidationException；旧 runtime 可以通过 Update 把它设为 `false`，但官方要求 "Before opting out, ensure your VPC provides the Amazon S3 access required for agent startup"。`update_rt.py` 的做法是先原样带回这个字段，只有当 ValidationException 明确点名它时才去掉并重试一次。**这段重试逻辑没有真机验证过。** 更新 VPC 模式的 runtime 前，先确认 VPC 能访问 S3（S3 Gateway 端点或 NAT）。

---

## 6. 运行时行为

- `async` 调用返回后，Kiro 在后台线程运行，期间 `/ping` 返回 `HealthyBusy`。官方说明：`Healthy` 状态空闲 15 分钟会被回收，`HealthyBusy` 可以活过这个 idle timeout，见 [runtime-long-run](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html)。bridge 里的 `time_of_last_update` 只在状态真正变化时才更新（README 第 3 条）。
- 单次 Kiro 运行上限是 `KIRO_TIMEOUT_SECONDS`，默认 780 秒。超时也会推送「巡检未完成（超时）」。
- 推送失败不影响巡检本身，结果只记录在 `[job]` 行的 `delivery` 里。

---

## 7. 排障

官方依据：[runtime troubleshooting](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)。

| 现象 | 原因与处理 |
|---|---|
| 调用报 runtime 没开 MMDSv2 | 官方说明：服务会拒绝 "invocations targeting runtimes without metadataConfiguration set, or with requireMMDSV2 set to false or null"（适用于 InvokeAgentRuntime、ExecuteCommand 等 5 个 API）。执行 `update_rt.py --region $REGION --runtime-id $RUNTIME_ID --mmdsv2 --wait`。官方原文："After you update, new invocations will succeed. Existing sessions are not affected." |
| 调用返回 424，大约 30 秒后失败 | 文件系统挂载失败。官方原文："Each mount has a 30-second timeout ... a single failure causes the entire invocation to fail"。本方案不挂文件系统；如果出现，检查 runtime 配置里是否被加了 `filesystemConfigurations` |
| 424，镜像层数很多 | 官方说明：镜像超过 53 层并且 `USER` 用非数字名时会报 424 |
| 504 | 官方列出三个原因：容器没暴露 8080、没有 `/invocations` 路径、镜像不是 ARM64。本镜像三项都满足，出现 504 先确认推送的是 `linux/arm64` |
| 改了镜像或环境变量，行为还是旧的 | 旧 session 仍然由旧容器服务。等 45~60 秒，并换一个新的 session id 调用 |
| 创建 runtime 失败，提示子网或 AZ | 子网不在 AgentCore 支持的 AZ ID 里（见第 4 节步骤 2） |
| `AccessDenied ... iam:PassRole` | 执行角色名不含 `BedrockAgentCore`，或部署者缺少对 Scheduler / Trigger 角色的 PassRole（见第 3.2 节） |
| Scheduler `TargetErrorCount` 大于 0 | 按第 4 节步骤 7 改成 camel 参数名 |
| delivery 里企业微信 `step: "subscribe"`、`errcode: 853000` | 企业微信不认这组 BotID / Secret。先核对完整配对，再查「API 模式 → 长连接」和密钥；本次 v8→v9 的实际修复只有 BotID，Secret 未变（第 1.1 节） |
| delivery 里企业微信 `step: "send"` 报错 | 订阅通过、发送被拒。常见原因是对方还没给机器人发过消息，或 `WECOM_CHAT_ID` / `WECOM_CHAT_TYPE` 填错 |
| 企业微信 `disconnected_event` | 同一个机器人有别的长连接订阅了，把巡检的连接踢掉了。给巡检单独建机器人 |
| VPC 模式下调用超时，或 Kiro 连不上 | 子网默认路由不指向 NAT，或 SG 出站没放行 443。放在公有子网也拿不到公网访问（SECURITY-NETWORK 第 8.1 节）。出站目的地见 SECURITY-NETWORK 第 7 节 |

---

## 8. 回滚与卸载

### 8.1 回滚到上一个镜像

```bash
python3 scripts/update_rt.py --region $REGION --runtime-id $RUNTIME_ID --image <仓库 URI>:<上一个 tag> --unset WECOM_WEBHOOK_KEY --wait
python3 scripts/create_schedule.py --region $REGION --delete
```

v4 镜像里没有 `notify.py`。回到 v4 后恢复旧行为：由 Kiro 自己用 shell 发飞书，前提是设了 `LARK_CHANNEL=ON`。

### 8.2 完整卸载（按顺序）

```bash
# 1. 定时：先列出 group 里的全部 schedule（含没触发过的测试调度），再删每日调度
aws scheduler list-schedules --region $REGION --group-name kiro-inspector --query 'Schedules[].Name'
python3 scripts/create_schedule.py --region $REGION --delete
aws scheduler delete-schedule --region $REGION --group-name kiro-inspector --name <剩余的测试调度名>

# 2. schedule group（删 group 也会删掉 group 里剩下的所有 schedule）
aws scheduler delete-schedule-group --region $REGION --name kiro-inspector

# 3.（原 Lambda 步骤，已无 Lambda）

# 4. Scheduler 角色：先删 inline 策略、解绑托管策略，再删角色（角色不存在就跳过）
for r in KiroInspectorSchedulerRole; do
  for p in $(aws iam list-role-policies --role-name $r --query 'PolicyNames[]' --output text); do
    aws iam delete-role-policy --role-name $r --policy-name $p; done
  for a in $(aws iam list-attached-role-policies --role-name $r --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name $r --policy-arn $a; done
  aws iam delete-role --role-name $r
done

# 5. runtime（DEFAULT endpoint 随之删除），然后确认已不存在
aws bedrock-agentcore-control delete-agent-runtime --region $REGION --agent-runtime-id $RUNTIME_ID
aws bedrock-agentcore-control get-agent-runtime --region $REGION --agent-runtime-id $RUNTIME_ID   # 期望 ResourceNotFoundException

# 6. 执行角色（方法同第 4 步）
r=KiroInspectorBedrockAgentCoreRole
for p in $(aws iam list-role-policies --role-name $r --query 'PolicyNames[]' --output text); do
  aws iam delete-role-policy --role-name $r --policy-name $p; done
aws iam delete-role --role-name $r

# 7. ECR 仓库（--force 连同镜像一起删）
aws ecr delete-repository --region $REGION --repository-name bedrock-agentcore-kiro-inspector --force

# 8. 日志组
aws logs delete-log-group --region $REGION --log-group-name /aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT

# 9. （VPC 模式）ENI：由服务管理，不要手动删除
aws ec2 describe-network-interfaces --region $REGION --filters Name=subnet-id,Values=$SUBNET_1,$SUBNET_2 \
  --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,Description]'
```

- 官方说明：同一子网 + 安全组组合下的 agent 共用 ENI；"When you delete an agent, the associated ENI may persist in your VPC for up to 8 hours before it is automatically removed"。
- VPC、子网、NAT、安全组、VPC 端点这些不是本方案的脚本创建的，按你们自己的变更流程处理。服务关联角色 `AWSServiceRoleForBedrockAgentCoreNetwork` 可能还被其他 agent 使用，不在卸载范围内。
- 卸载完成后逐项确认：`aws scheduler list-schedules --region $REGION --group-name kiro-inspector` 报 group 不存在；第 3 步、第 5 步的查询报 `ResourceNotFoundException`；`aws ecr describe-repositories --region $REGION --repository-names bedrock-agentcore-kiro-inspector` 报 `RepositoryNotFoundException`。

---

## 9. 已验证 / 未验证

**已验证**

- 离线测试 34 + 6 + 14 = 54 个用例全部通过（2026-10-10 移除 EFS 后的数字；`test_notify.py` 新增 12 个，用本地假长连接服务器覆盖：帧格式、群聊、回调干扰、订阅失败、发送失败、20480 字节切分、缺 chat id、被踢、无回执超时、连不上、bridge 剥离机器人凭据）。
- IAM Access Analyzer `validate-policy`：`iam/` 下的策略文件（替换占位符后）全部 0 findings；故意写错动作名的负对照能正常报 `INVALID_ACTION`。
- dry-run：
  - `create_rt.py` PUBLIC 模式、VPC 模式都 exit 0（VPC 模式只有一条「出站全开」的警告）。
  - 反例都 exit 2：同名 runtime 已存在、使用公有子网、参数组合错误。
  - `update_rt.py --mmdsv2` 的请求能回填 lifecycle / metadata / filesystem 和原有环境变量。
  - `create_schedule.py` 两种模式的请求都通过 botocore 参数校验。
- 本手册和 SECURITY-NETWORK 里用到的 AWS CLI 子命令和参数，都用 `aws <服务> <命令> help` 核对过存在。
- 本地真 Kiro 端到端：34.15 秒，exit 0，本地假接收端收到 1 条 markdown_v2（665 字节），`[job]` 里 delivery 为 wecom ok。
- 云上端到端（ap-northeast-1）：`build_push.sh` 推镜像 → `update_rt.py` 更新 → `invoke_rt.py` 同步调用 `exit_code=0` → Scheduler universal target（`arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime`）一次性触发成功 → 企业微信智能机器人长连接和飞书都收到报告。
- **同一企业微信机器人只能有一条长连接**（真机坐实）：新连接订阅成功后，旧连接会收到 `disconnected_event` 并被踢下线，且不会自动重连。
- 会话续接、EFS 实验等运行时行为见 README 第 9–17 条的真机记录（EFS 已不在本方案内，只保留实验结论）。

**未验证**

- `create_rt.py` 真正创建 runtime（只做过 dry-run）。
- `update_rt.py` 在 ValidationException 点名 `requireServiceS3Endpoint` 时去掉该字段重试的逻辑。
- Kiro 2.24.1。
- 企业微信里消息实际显示成什么样：服务端回执是 errcode 0，但我们看不到接收端的界面。
- 企业微信 webhook 真推送：测试 runtime 没配 `WECOM_WEBHOOK_KEY`。拿到 key 后先 `export WECOM_WEBHOOK_KEY=...`，再执行 `update_rt.py --set WECOM_WEBHOOK_KEY --wait`（不带 `--image` 就保留现有镜像，见第 5 节）。真实企业微信群里的显示效果也还没看过（只用本地假接收端验证过报文格式）。
- Kiro 实际会连哪些出站域名（没有抓包；SECURITY-NETWORK 第 7.1 节以 Kiro 官方清单为准）。
- `notify.py` 是否会走 `HTTPS_PROXY`。
- 启用 `DenyAllExceptVPC` 资源策略后，Scheduler 调用路径是否会被挡住。
