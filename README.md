# Kiro CLI on Bedrock AgentCore Runtime —— AI SRE 巡检 agent

把 **原味 Kiro CLI**（v2.21.4，未 strip、未改动）装进容器跑在 **Bedrock AgentCore Runtime** 上，
用 **Kiro API key** 做 headless 授权，巡检 AWS Health 事件 + RDS / ElastiCache / DocumentDB / MemoryDB
的待维护动作，结果推送到企业微信 / 飞书（配了哪个发哪个，都配都发）。
每天 09:00 由 EventBridge Scheduler 定时触发。

交付入口：**[`usage-guide.md`](usage-guide.md)（使用指南：巡检内容、通知配置、手动调用与结果检查）**；[`wecom-eventbridge-deploy.md`](wecom-eventbridge-deploy.md)（部署与运维手册）；[`SECURITY-NETWORK.md`](SECURITY-NETWORK.md)（网络与权限安全基线，部署前必读：全程不开入站端口、不做公网可达配置）。

**设计约束（决定了整个形态）**：

| 要求 | 落地方式 |
|------|---------|
| 巡检内容不写死在镜像里 | 提示词全部来自**调用时的 payload**：扁平 `{"prompt":"..."}` 与嵌套 `{"input":{"prompt":"..."}}` 两种格式都兼容 |
| 无状态、不挂 EFS | `KIRO_STATE_MODE=ephemeral`（默认）：每次调用 `tempfile.mkdtemp()` 做一次性 cwd + `HOME`，跑完 `rmtree` |
| 发送渠道可开关 | 凭据齐全即启用：`WECOM_BOT_ID`+`WECOM_BOT_SECRET`（智能机器人长连接，优先）或 `WECOM_WEBHOOK_KEY` → 企业微信，`LARK_APP_ID`+`LARK_APP_KEY` → 飞书；`WECOM_CHANNEL`/`LARK_CHANNEL=OFF` 强制关。**由 bridge（`notify.py`）推送，Kiro 只把报告写进文件**。推送凭据不传入 Kiro 子进程的环境变量，但 Kiro 与 bridge 同为 root，仍可经 `/proc/<pid>/environ` 读到（见 SECURITY-NETWORK 第 9.5 节） |
| 模型 / reasoning effort 可换 | `KIRO_MODEL`（默认 `gpt-5.6-luna`）、`KIRO_EFFORT`（默认 `max`） |
| 需要多轮上下文时可切 | `KIRO_STATE_MODE=session`：HOME/cwd 按 AgentCore session id 固定 + 自动续会话（见「上下文保持」一节） |
| 续会话方式可换 | `KIRO_RESUME_STRATEGY`：`probe`（默认，先 `--list-sessions` 查 id 再 `--resume-id`）/ `latest`（不带 id 的 `--resume`，**挂 EFS 时必须用这个**，见第 14 条） |
| 需要跨 microVM 保持上下文 | 挂 EFS access point + payload 传 `{"thread":"<名字>"}` 让状态目录与 session id 解耦（代价是强制 VPC + NAT，见第 14 条） |

**实测结果**：ap-northeast-1，云上 runtime 状态 READY，
两次真实调用 **42.0s / 30.5s**，`exit_code=0`。Kiro 自主调 `use_aws` 拿到真实待维护项
（一个 Aurora 集群的 os-upgrade + 其 writer 实例的 system-update），
自主用 shell 走飞书两步 API 发送成功（`code: 0` + `message_id`）。
（这是 v4 镜像的实测。v5 改为 bridge 推送，Kiro 不再自己发；v5 的云上实测见 `wecom-eventbridge-deploy.md` 第 9 节。）

多轮上下文实测（`KIRO_STATE_MODE=session`，同一 `runtimeSessionId`）：T1 建立上下文 →
T2 `resumed=true` 准确复述批次号与范围（uptime 37.78s → 58.59s，证明同一实例）→
T3 换新 session 答"不知道"。**跨 microVM** 实测（EFS + `thread`）：A 存暗号 → B 组**全新
session id**、`/proc/uptime` 仅 34.40s（新 microVM）仍准确答出 → C 组换 thread 名答 NONE。

## 文件

| 路径 | 说明 |
|------|------|
| `usage-guide.md` | **使用指南**：配置巡检提示词、飞书/企微通知、手动调用、结果检查和暂停 |
| `build/server.py` | Flask bridge：`/invocations` + `/ping`，AgentCore 合约实现 |
| `build/notify.py` | 企业微信（智能机器人长连接 / webhook）/ 飞书推送模块（长连接用 `websockets`，其余只用标准库）：`send_report()` 从不抛异常、按字节上限切分；也可命令行 `--channels` / `--dry-run` |
| `build/Dockerfile` | `--platform=linux/arm64` + python:3.12-slim，只 COPY 一个 `kiro-cli-chat` + `server.py` + `notify.py`，pip 装 `flask` / `websockets==16.0` |
| `scripts/create_rt.py` | 从零新建 runtime：凭据只从 shell 环境读（`--set KEY`）、不打印；建完自动 Update 开 MMDSv2；VPC 模式先做只读网络检查（公有子网 / CIDR 入站等不合规直接拒绝）；`--dry-run` 只读 |
| `scripts/invoke_rt.py` | 手动调用一次（SigV4，调用者自己的凭据），含 sessionId 长度处理；`--async` / `--no-notify` / `--thread` |
| `iam/runtime-trust-policy.json` | runtime 执行角色的信任策略：信任 `bedrock-agentcore.amazonaws.com` + SourceAccount/SourceArn 条件 |
| `iam/runtime-execution-policy.json` | runtime 执行角色的权限：ECR pull / Logs / X-Ray / PutMetricData / WorkloadIdentity + `ReadOnlyInspection` 只读段（20 个只读动作） |
| `iam/deployer-policy.json` | 部署者在 `BedrockAgentCoreFullAccess` 之外要补的权限（13 个 Sid），末条 `NeverOpenAnInboundPath` 显式 Deny 资源策略 / `*ForUser` / Function URL / ECR 仓库策略 |
| `SECURITY-NETWORK.md` | **网络与权限安全基线**（10 节）：只有出站、禁止项 12 条、本地测试不发布端口、出站目的地清单、VPC 模式 SG/端点/Flow Logs、权限收紧、自检命令 |
| `agents/sre.json` | Kiro agent 配置示例（≈ skills）：`prompt` + `resources` + 工具白名单，三样均实测生效 |
| `wecom-eventbridge-deploy.md` | **部署与运维手册**（9 节）：企业微信两种接法（智能机器人长连接 / webhook）、代码结构、部署者 IAM、从零部署 8 步、更新、排障、回滚与完整卸载、已验证 / 未验证 |
| `scripts/build_push.sh` | 构建 arm64 镜像推 ECR（仓库不存在则建） |
| `scripts/update_rt.py` | 换镜像 / 改环境变量，其余配置原样回填（`--set` 的值从 shell 环境读、不打印） |
| `scripts/create_schedule.py` | 建每日 09:00 定时（Scheduler 直调 InvokeAgentRuntime，无 Lambda）；`--test-in` / `--delete` / `--dry-run` |
| `iam/scheduler-trust-policy.json` `iam/scheduler-invoke-policy.json` | Scheduler role 的信任策略 + 调用权限 |
| `tests/test_notify.py` `tests/test_trigger.py` `tests/test_create_rt.py` | 离线测试 34 + 6 + 14 个（`python3 -B tests/<x>.py`） |

镜像里的 `kiro/bin/kiro-cli-chat`（829MB）**未入库**，从官方分发包解出来放到 `build/kiro/bin/` 再 build。
build 前按
`wecom-eventbridge-deploy.md` 步骤 2 下载解包并核 sha256，否则 `scripts/build_push.sh` 会报找不到二进制。

## 查文档查不出、只有真机才捉到的结论

### 1. Kiro CLI「3.0」不是新版本，且对本方案是倒退

`stable` 是唯一有 manifest 的 channel（beta / preview / rc / canary / nightly 全部返回空），
2026-09-14 实测时 2.21.4 是最新（**2026-09-27 复查已发布 2.24.1，未测**）。所谓 CLI 3.0 是**同一个 2.21.4 二进制的 `--v3` / `--agent-engine v3` 开关**。

v3 的 breaking change 表第一行就是 **`aws_tool` 被移除（Impact: High）**，要改用 MCP server。
实测 v3 下内置 AWS 工具直接报 `Failed`，只能靠 shell 退回跑 `aws` CLI 才拿到答案。
→ **本方案走 v1 默认引擎**，内置 `use_aws` 零外部依赖。

另一条：**v2 engine 下 `--model gpt-5.6-luna` 报 `failed to set model: Method not found`**，
v1（默认）正常生效。选引擎会连带限制可用模型。

### 2. 只需要 `kiro-cli-chat` 一个二进制

分发包解压 986MB（arm64）里三个二进制：`kiro-cli-chat` 829MB（真正干活的）、
`kiro-cli` 103MB（launcher）、`kiro-cli-term` 78MB（PTY）。
**`kiro-cli-chat` 单独就能跑 `chat --no-interactive`**，另两个不需要 → 最终镜像 **606MB**，
落在 AgentCore 的 2GB 镜像上限里还有余量。

`strip` 只省 30MB（体积来自内嵌资源不是 debug symbol），不值得，且用户要求保持原味。

### 3. `/ping` 的 `time_of_last_update` 不能每次刷新

AgentCore 靠这个字段判断 session 是否 idle。**每次 `/ping` 都刷新它 → idle timeout 永不触发 →
session 一直活到 `maxLifetime` 耗尽 session 配额**。正确做法是模块级 `_STATE_SINCE`，
**只在健康状态真的变化时**才更新；长任务期间返回 `HealthyBusy` 保活。

### 4. AWS Health API 需要 Business/Enterprise Support

本账号直调报 `SubscriptionRequiredException`。但**维护需求走各服务自己的 API 不依赖 Health**
（`rds describe-pending-maintenance-actions` / `elasticache describe-service-updates` 等），
巡检主链路不受影响 → 提示词里两块要分开写，不要把维护巡检建在 Health API 上。

### 5. AgentCore 两个 client 名字不同

创建用 `bedrock-agentcore-control`，调用用 `bedrock-agentcore`。
另外 `runtimeSessionId` 长度硬要求 **33–256 字符**（`uuid4().hex` 只有 32，会被拒）。

### 6. botocore 静态表查不到 AgentCore 的 region

`get_available_regions('bedrock-agentcore*')` **返回空**，不能用来判断可用性。
改用实调 `list_agent_runtimes` 探测，实测 **ap-southeast-1 / us-west-2 / us-east-1 /
ap-northeast-1 / ap-southeast-2 / eu-central-1 全部可用**。

### 7. 一处 AWS 文档渲染缺陷（不是真实约束）

`agentRuntimeName` 的 pattern 文档写成 `a-zA-Z{0,47}`，与官方示例 `strands_agent`（带下划线）
矛盾 —— 是文档管线吞掉了字符类方括号，实际 `kiro_inspector` 建成功。看到自相矛盾的 pattern
先按示例试，不要照着改名。

### 8. payload 指令能压过环境变量拼进去的指令

`LARK_CHANNEL=ON` 时提示词里带发送指令，但 payload 里写"不要发飞书，直接把结果回给我"
它照办了 → 渠道开关是**默认行为**，单次调用可以在 payload 里覆盖。
（这是 v4 的旧机制：由 Kiro 自己发。v5 起改为 bridge 推送，单次关闭用 payload `{"notify": false}` 或 `{"channels": [...]}`，不再依赖模型听话。）

## 上下文保持（skills / agent 配置 / 多轮记忆）

三件独立的事，别混在一起谈：**Runtime 层的实例粘性** → **Kiro 层的会话续接** → **Kiro 层的 agent 配置**。
前两者叠起来才有"多轮记忆"，第三个是"每轮的固定人格与工具面"。

下面只写真机结论。

**结论先行（想直接抄配置的看这里）**：要"隔几小时/隔天还能接上"就挂 **EFS access point**，
三个环境变量 + payload 一个字段：

```
KIRO_STATE_MODE=session
KIRO_STATE_ROOT=/mnt/kirostate        # = filesystemConfigurations 里的 mountPath
KIRO_RESUME_STRATEGY=latest           # EFS 上必须，否则每次白等 120 秒且 resume 永不生效
payload: {"input": {"prompt": "...", "thread": "sre-oncall"}}
```

外加 `networkMode=VPC`（API 强制）+ NAT（VPC 模式默认不通公网）+ 两条 EFS IAM 权限。
只要"一次任务内的多轮追问"就别挂 EFS，`KIRO_STATE_ROOT` 留在容器本地 `/tmp` 即可，
`networkMode=PUBLIC` 省掉整套网络。**managed sessionStorage 对 Kiro 不可用**（第 12 条）。

### 9. 同 session 复用同一实例：文档说的成立，`hostname` 不能当判据，`uptime` 才能

同一个 `runtimeSessionId` 连续调用 → 写进 `/tmp` 的标记文件下一轮**读得到**；换新 session → `NOT-FOUND`。
但 `hostname` 两种情况都是 `localhost`，**不能用来区分实例**；判据是 `/proc/uptime`：
同 session 第二次调用 uptime 从 32.45s 涨到 55.85s（与间隔吻合），新 session 归零到 32.11s。

### 10. Kiro 的会话记忆边界是 `$HOME` 那个 sqlite，不是 cwd —— 这正是原实现丢记忆的原因

会话落在 **`$HOME/.local/share/kiro-cli/data.sqlite3`**，表 `conversations_v2` 的
**`key` = cwd、`conversation_id` = session id**。由此四条实测：

- `--resume` / `--resume-id` 在 `--no-interactive` 下**真的能续上**（对照组不加就完全不知道上一轮）；
- **`--resume-id` 按 id 直查，跨 cwd 有效**（换一个全新 cwd 仍答出上一轮的批次号）；
- **换 `$HOME` 就彻底断**（同一个 id 也答"这是我们对话的开始"）→ **原实现每次 `mkdtemp` 换 HOME，
  等于每次都是新记忆，不是 Runtime 不支持**；
- `--resume` 续上后会把该会话的 `key` 改写成当前 cwd（`--list-sessions` 里 cwd 跟着变）。

所以 session 模式的做法是：**HOME 和 cwd 都按 AgentCore session id 固定**，
并把 conversation id 落在 `$HOME/.bridge-conversation-id`，下一轮拿它去 `--resume-id`。

### 11. 最危险的一条：`--resume-id` 传了不存在的 id 不报错，静默开新会话并掉到 v3 引擎

换 HOME 后拿旧 id 去 resume，**exit code 仍是 0**，但输出里冒出 `KAS (Kiro Agent Server)` 启动日志、
`--list-sessions` 里该会话 `source` 变成 **`v3`**，还伴随 `[tool] status: Failed` —— 正是我们刻意
避开的 v3 引擎（第 1 条）。表现是"上下文丢了 + 工具报错"，但没有任何一处报错指向真实原因。

→ bridge 里必须**先 `--list-sessions` 校验 id 存在再传** `--resume-id`（已实现为 `resolve_resume_id()`）。

### 12. 挂 managed session storage 直接把 Kiro 打死：`database is locked`

`filesystemConfigurations=[{'sessionStorage':{'mountPath':'/mnt/kirostate'}}]` 建得上、runtime 也 READY，
但把 `KIRO_STATE_ROOT` 指到该 mount 后**每次调用必挂**：

```
exit_code=1, stderr="error: Failed to open database: database is locked"
```

三次调用全部精确 10.02s 失败（启动即死的形状，不是超时）。**同一个镜像、只把 `KIRO_STATE_ROOT`
改回容器本地 `/tmp/kiro-sessions` 就 `exit_code=0`** —— 归因干净：SQLite 拿不到文件锁，
session storage 那套"异步复制到远端持久存储"的语义扛不住 sqlite 的锁。

代价要认清：**上下文只活在 microVM 存活期内**（idle 60s / maxLifetime 到期即清空），
适合"一次巡检任务内的多轮追问"，不适合"隔几小时接上"。

**但"跨实例持久只能走 AgentCore Memory"这个判断是错的，第 14 条推翻了它** —— 换成 EFS
access point 就成了，SQLite 在 EFS 上是好的，坏的只有 `--list-sessions`。
所以第 12 条的失败**不能推广成"网络存储放不了 sqlite"**，它只是 managed session storage
这个具体实现的问题。

### 13. agent 配置（≈ skills）是 `$HOME/.kiro/agents/<name>.json`，三样都实测生效

`agent create` 落盘在 `$HOME/.kiro/agents/`，schema 关键字段：

| 字段 | 作用 | 实测 |
|------|------|------|
| `prompt` | 常驻 system prompt | 强制首行输出 `[SRE-AGENT-ACTIVE]` → 生效 |
| `resources` | 拼进上下文的文件 | `runbook.md` 里的批次号前缀/轮换窗口被准确答出 → 生效 |
| `tools` / `allowedTools` | 工具白名单 / 免确认名单 | 生效 |
| `mcpServers` + `includeMcpJson` | 挂 MCP server | 未测 |

用 `--agent <name>` 选择（bridge 里是 `KIRO_AGENT` 环境变量）。

**坑：`resources` 写相对路径 `file://runbook.md` 时，换 cwd 就静默拿不到**——agent 照样加载、
`prompt` 照样生效，只有 resources 那部分空掉且不报错（实测答"无基线"）。
改绝对路径 `file:///opt/kiro/runbook.md` 才稳。要随镜像分发的 runbook 就 COPY 进镜像用绝对路径引用。

### 14. 换 EFS access point：SQLite 活了，跨 microVM 也活了；坏的只有 `--list-sessions`

同一个 mountPath、同一个镜像，只把 `sessionStorage` 换成 `efsAccessPoint`，
`database is locked` 就消失了。容器里 `mount` 看到的是本地 NFS 代理转发：

```
127.0.0.1:/ on /mnt/kirostate type nfs4 (rw,vers=4.1,port=20377,addr=127.0.0.1,local_lock=none)
```

`data.sqlite3` 正常建、`exit_code=0`。**所以第 12 条的失败不是"网络存储放不了 sqlite"这条通则，
是 managed session storage 这个具体实现的问题** —— 同样是远端存储的 EFS 没这个毛病。

**但 EFS 上有另一个失败模式，而且它静默**：`--list-sessions` 那个只读 sqlite 查询会永久 hang。
证据是四次调用的算术，`elapsed` 减去 Kiro 本体耗时恒定：

| 调用 | elapsed | Kiro 本体 | 差 |
|------|---------|-----------|-----|
| T1 | 145.5s | 25.17s | 120.3 |
| T2 | 139.5s | 19.17s | 120.3 |
| T3 | 140.9s | 20.53s | 120.4 |
| T4 | 129.3s | **8.96s** | 120.3 |

**恒定 120.3s = 2 × 60s**，正好是 bridge 里两次 `--list-sessions`（`timeout=60`）各卡满。
Kiro 主进程写得进读得出，偏偏这个查询拿不到锁。后果是 `resolve_resume_id()` 永远返回空 →
`resumed` 永远 `False` → **上下文表面上"不生效"，而 exit code 一路 0，没有一处报错**。

解法是绕开这个查询：`KIRO_RESUME_STRATEGY=latest` 改用不带 id 的 `--resume`（续该 cwd 最新会话）。
因为 HOME+cwd 本身就按 session/thread 隔离，"这个目录里最新的"就已经是对的那个会话，
不需要查 id。改完 **17.1s / 9.9s，120 秒死等消失，`resumed=true`**。

代价是放弃了第 11 条那个校验，但同时也不需要它了：不传 id 就没有"传错 id"的风险。

**跨 microVM 实测（EFS 唯一真正的卖点，三组对照）**：payload 加 `{"thread": "<名字>"}`
让状态目录名与 session id 解耦（不加的话目录名就是 session id，换 session 必然换目录，
EFS 白挂）：

| 组 | session id | thread | `/proc/uptime` | 结果 |
|----|-----------|--------|----------------|------|
| A 存 | `thr-a-…` | `sre-oncall` | — | 存入暗号 `CROSS-VM-7788` |
| B 取 | **全新** `thr-b-…` | 同 `sre-oncall` | **34.40s**（刚启动的新 microVM） | `resumed=True`，准确答出 `CROSS-VM-7788` |
| C 对照 | 全新 `thr-c-…` | `different-thread` | — | `resumed=False`，答 `NONE` |

B 组那一帧是决定性的：**uptime 34 秒说明这是个刚起来的 microVM，上一个已经没了，
而对话内容还在** —— 容器本地 `/tmp` 和 managed session storage 都做不到这件事。
C 组证明不是模型瞎猜。

**API 直接给出的两条前置条件**（比翻文档快，报错原文就是答案）：

```
ValidationException: Filesystem configurations with EFS or S3 Files access points
                     require VPC network mode
ValidationException: Execution role is missing required filesystem permissions.
                     Ensure the role has elasticfilesystem:DescribeAccessPoints
                     and elasticfilesystem:DescribeMountTargets
```

即 EFS 挂载**强制 `networkMode=VPC`**（`PUBLIC` 直接被拒），而 VPC 模式默认不通公网 →
要出网调外部 API（模型 endpoint、飞书）**必须自备 NAT**。这是挂 EFS 的真实代价：
从"给个 mountPath 就完事"变成"要维护 VPC + 私有子网 + NAT + EFS mount target + 两个 SG"。
IAM 侧除了 `ClientMount`/`ClientWrite`/`ClientRootAccess`，还要 `DescribeAccessPoints`
和 `DescribeMountTargets`（这两个不在直觉里，是报错逼出来的）。

**三种存储怎么选**：

| 存储 | SQLite | 活多久 | 代价 |
|------|--------|--------|------|
| 容器本地 `/tmp` | 好 | microVM 存活期（idle 60s / maxLifetime） | 无，`networkMode=PUBLIC` 即可 |
| managed sessionStorage | **挂**（`database is locked`） | — | 不可用（对 Kiro 而言） |
| EFS access point | 好（但 `--list-sessions` hang） | **跨 microVM、跨天** | 强制 VPC + NAT + EFS 一套 |

## 操作坑

- **`/bin/sh` 不支持 bash 语法**：`${VAR:0:4}` 这类切片、`mkdir -p a/{b,c}` brace expansion
  全部失效（后者会真的建出一个名叫 `{b,c}` 的目录）。默认 shell 是 `sh` 时用 POSIX 写法或显式 `for` 循环。
- **buildx 跑 arm64 要先装 emulation**：`docker run --privileged tonistiigi/binfmt --install arm64`，
  并用独立 builder。**`nohup` 起 buildx 会连日志文件都不创建**，改用
  `setsid ... > /tmp/x.log 2>&1 < /dev/null &`。
- **`time` 的输出会混进 curl 的 json 文件**导致 `JSONDecodeError: Extra data` → 只解析第一行。
- **`aws bedrock-agentcore invoke-agent-runtime --payload` 不吃非 ASCII**
  （`string argument should contain only ASCII characters`）→ 中文 prompt 必须写文件走
  `--payload fileb://payload.json`，且 `--cli-read-timeout` 要显式加大（默认 60s 不够巡检跑完）。
- **改完 runtime 后 `status` 会很快回到 READY，但旧容器可能还在服务** → 实测前等 45~60s，
  否则会测到上一个版本的行为。

## 部署步骤

不在这里重复，以交付文档为准：

- 部署前先读 [`SECURITY-NETWORK.md`](SECURITY-NETWORK.md)，第 3 节禁止项与第 10 节自检是验收标准。
- 从零部署：[`wecom-eventbridge-deploy.md`](wecom-eventbridge-deploy.md) 第 4 节（离线测试 → 选网络 → 执行角色 → Kiro 下载与 sha 校验 → `build_push.sh` → `create_rt.py` → `invoke_rt.py` → `create_schedule.py` → 验证）。
- 更新现有 runtime（例如 v4 → v5）：同一文档第 5 节，`update_rt.py`。
- 回滚与完整卸载：同一文档第 8 节。

凭据（Kiro API key / 企业微信 key / 飞书 app secret）只通过 runtime 的 `environmentVariables` 注入，**不入库**、不出现在命令行参数里。
