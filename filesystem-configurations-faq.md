# AgentCore Runtime `filesystemConfigurations` 前置条件与限制 — 8 问核实

来源：`https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html`（主文档）+ `runtime-troubleshooting.html` + `agentcore-vpc.html`，全部用 aws-knowledge 抓取原文验证，链接可达。标注「官方明写」= 文档原文直接给出；「推断」= 文档未直接说，基于其他条款或真机结果推出。

## 1. EFS / S3 Files 是否强制 VPC

官方明写。"Prerequisites for bring-your-own file systems" 章节原文：

> "Your agent runtime must use `networkMode: VPC`."

S3 Files access point 同一要求，见 Shared requirements 表格（VPC mode required 一行 EFS/S3 Files 均勾选）。**Managed session storage 不需要 VPC**（"No VPC or additional IAM permissions required."）。

## 2. VPC 出公网 / NAT

官方明写（`agentcore-vpc.html`）：VPC 模式下默认无互联网访问：

> "Connecting Amazon Bedrock AgentCore Runtime and built-in tools to public subnets does not provide internet access. Always use private subnets with NAT gateways for internet connectivity."

即需要私有子网 + NAT Gateway + IGW 架构。另外主文档 "Managed session storage networking" 章节明写：VPC 模式下用 session storage 也需要出网到 S3（"your VPC must allow outbound connectivity to S3"），并给出可选的 S3 Gateway Endpoint 自定义 policy 示例（scope 到 `arn:aws:s3:::acr-storage-*-region-an`）。

`requireServiceS3Endpoint` 字段：控制是否自动配一个 service-managed S3 gateway endpoint（下载代码/镜像用）。2026-05-05 起新建的 runtime 不再自动带；该字段只在 `UpdateAgentRuntime` 对"rollout 前建的 runtime"生效，`CreateAgentRuntime` 传会报 `ValidationException`。

建议加 S3 Gateway VPC Endpoint（免费）+ ECR (`ecr.dkr`/`ecr.api`) + CloudWatch Logs (`logs`) 三个 Interface Endpoint。

## 3. mountPath 是否可以按 session 动态换目录

官方明写为**静态配置**，没有「按 session 自动挂到不同子目录」的用户可配项：

> Pattern: `/mnt/[a-zA-Z0-9._-]+/?`；长度 6–200 字符；必须在 `/mnt/` 下且只有一级子目录；每个 mount path 唯一且不能互为子目录。

但 **managed session storage 本身是 per-session 隔离**（"Storage options at a glance" 表：Isolation = Per-session），同一个静态 mountPath 配置在不同 session 下服务侧自动指向不同底层存储，用户不需要也不能手动控制路径差异。

**BYO 文件系统（EFS/S3 Files）是 shared across sessions and agents**（同上表 Isolation = Shared），官方明写要自己处理并发：

> "For bring-your-own file systems, ensure your agent handles concurrent access appropriately. Use file-per-session naming patterns or advisory file locks to avoid conflicts."

即容器内代码自建子目录/命名规则是唯一手段，服务不提供按 session 自动分区。

## 4. EFS/S3 Files 的 POSIX uid/gid

官方明写：

> "When you create an access point (S3 Files or EFS), you specify a POSIX user ID (UID) and group ID (GID). All file operations through the access point run as this identity. Set the UID/GID to match the user your container process runs as (typically 1000:1000 for non-root containers, or 0:0 for root)."

Troubleshooting 页对应条目"I get 'Permission Denied' when writing to my mounted filesystem"给出同样的两个修复方向：改 access point 的 posixUser 匹配容器进程 uid/gid，或把目录权限设 777。

EFS 的 `elasticfilesystem:ClientRootAccess`：AgentCore 官方 IAM 示例（主文档 Prerequisites 章节 + troubleshooting 页的 Access denied 修复示例）**只用 `ClientMount` + `ClientWrite`，没有出现 `ClientRootAccess`**。该权限在 EFS 通用文档里用于 NFS client 侧的 root squash 覆盖场景（`efs/latest/ug/accessing-fs-nfs-permissions.html`），不是 AgentCore 特有要求；EFS file system policy（root squash / 强制 TLS）AgentCore 文档未强制要求配置。

## 5. IAM 权限

主文档 Prerequisites 章节给出的最终版权限示例（比早期草案多了一项 `GetAccessPoint`）：

**S3 Files**
```json
{
  "Effect": "Allow",
  "Action": ["s3files:ClientMount", "s3files:ClientWrite", "s3files:GetAccessPoint"],
  "Resource": "arn:aws:s3files:<region>:<account-id>:file-system/<file-system-id>",
  "Condition": {"ArnEquals": {"s3files:AccessPointArn": "arn:aws:s3files:<region>:<account-id>:file-system/<file-system-id>/access-point/<access-point-id>"}}
}
```

**EFS**
```json
{
  "Effect": "Allow",
  "Action": ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
  "Resource": "arn:aws:elasticfilesystem:<region>:<account-id>:file-system/<file-system-id>",
  "Condition": {"ArnEquals": {"elasticfilesystem:AccessPointArn": "arn:aws:elasticfilesystem:<region>:<account-id>:access-point/<access-point-id>"}}
}
```

官方明写：**只读场景省略 `ClientWrite` 即可**；`s3files:GetAccessPoint` 是 access point 校验专用权限（建 runtime 时用）。

Security groups：官方明写——

> "Allow outbound TCP on port 2049 from your agent runtime security group to the mount target security group. Allow inbound TCP on port 2049 on the mount target security group from the agent runtime security group."

## 6. S3 Files access point 是只读还是可读写

官方明写为**可双向读写**，不是只读快照：

> "Bidirectional sync between file system and backing S3 bucket."
> "Close-to-open consistency for NFS clients; S3 eventual consistency for bucket-side access."
> Max file size 48 TiB；max directory depth 1,000 层。
> 不支持：硬链接、S3 archival storage classes (Glacier)、自定义 S3 object metadata、pNFS。

Mount 方式：**NFSv4.2 over TLS，IAM 认证强制开启且不可关闭**（"TLS and IAM are always enabled and cannot be disabled for S3 Files."）。EFS 侧是 NFSv4.1 over TLS，IAM 认证是可选（有 `ClientMount` 带 `AccessPointArn` 条件才启用）。

取舍定位（官方 "Storage options at a glance" 表）：S3 Files → "Datasets accessible through both standard file operations and S3 APIs"；EFS → "Shared tool libraries, model weights, read-write multi-agent collaboration"，且 **EFS 是 Full POSIX（硬链接、符号链接、advisory file locking）**，S3 Files 不支持硬链接。

## 7. Managed session storage 的一致性 / 复制语义（数据库文件、文件锁场景）

官方明写的限制清单（Filesystem semantics 章节）：

> **Unsupported operations**：Hard links；Device files/FIFOs/UNIX sockets（`mknod`）；Extended attributes (xattr)；`fallocate`（sparse file preallocation）；**File locking across sessions** — "Advisory locks work within a running session but are not persisted across stop/resume. Tools that use file-based locking (such as `git`) are unaffected."
>
> "Permissions are stored but not enforced within the session... access checks always succeed because the agent runs as the only user in the microVM."

网络语义：session storage 数据存在 AgentCore 管理的 S3（"Session data is stored in AgentCore S3"），VPC 模式下需要出网到 S3 做同步；容器侧看到的是标准 Linux 文件系统语义（ls/cat/mkdir/git/npm/pip/cargo 均可用）。

**⚠️ 与我们真机 PoC 结果的矛盾，需如实标注**：文档字面意思是 advisory lock **在单个 session 内**是可用的（只是不跨 stop/resume 持久化）。但我们在 `agentcore-kiro-inspector` 的真机测试中，把 SQLite 数据库文件挂到 managed session storage 后，**同一个 session 内每次调用都立即报 `database is locked`**（三次全部精确 ~10.02s，是启动即死的形状不是超时），换回容器本地临时目录后同一份代码 `exit_code=0`。这不是"跨 session 锁不持久"的场景，是单 session 内首次拿锁就失败——**官方文档没有直接解释这个现象，也没有提及数据库文件/SQLite 场景的适用性警告**，只能标注为「真机验证到的限制，官方未给出对应措辞，推测与其异步复制到 S3 的存储实现有关，但未确认根因」。结论对客户的表述应该是：**不要把 SQLite 之类依赖文件锁的本地数据库直接放在 managed session storage 上**，这是真机结论不是文档结论。

Session storage limits（Quotas 页）：单 session 最大存储 1GB（不可调）、元数据约 50MB、最大目录深度 200 层、文件名最长 255 字节。生命周期：14 天不调用则清空；runtime 版本更新会重置为全新文件系统。

## 8. 官方 reference architecture / 持久化对话状态最佳实践

官方明确区分 **Runtime 层临时状态 vs AgentCore Memory 的持久化定位**（blog《Securely launch and scale your agents and tools on Amazon Bedrock AgentCore Runtime》）：

> "This doesn't obviate the need for external state management (see the following section on using AgentCore Runtime with AgentCore Memory)... Session state is ephemeral and should not be used for long-term durability (use AgentCore Memory for context durability)."（后半句出自 `runtime-how-it-works.html` 的 microVMs 章节）

即官方推荐的**跨 session 长期记忆**方案是 AgentCore Memory（`create_event` / `get_last_k_turns` / `retrieve_memories`），不是靠 session storage 硬撑。

Session storage 的定位是**单 session 内、stop/resume 之间**的项目文件/已装依赖持久化（官方 use case 示例是"coding agent with persistent workspace"：第一次调用建项目文件，stop 后 compute 终止但存储保留，resume 时新 compute 挂载回同一份文件+对话历史）。

两篇具体落地的官方 blog：
- `https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/`（2026-04-02）：session storage + `InvokeAgentRuntimeCommand` 组合，Strands `FileSessionManager` 把对话历史存到 `/mnt/workspace/.sessions`。
- `https://aws.amazon.com/blogs/machine-learning/its-safe-to-close-your-laptop-now-hosting-coding-agents-on-amazon-bedrock-agentcore/`（2026-06-08）：coding agent 更完整的架构叙述。

## 补充：Troubleshooting 页的挂载专属条目

`runtime-troubleshooting.html` 有 4 条挂载相关 FAQ（原文摘录）：

| 症状 | 原因 | 修复 |
|---|---|---|
| "Access denied" | 执行角色缺 `ClientMount`/`ClientWrite` | 加 IAM 权限 + `AccessPointArn` 条件 |
| "ResourceNotFound" / "Failed to resolve" | access point 或 mount target 被删/不可用 | 核对 ARN 存在、mount target 状态 Available |
| Mount 挂起后失败（约 30 秒） | 安全组挡了 2049 端口，或该 AZ 没 mount target | 放通 TCP 2049；核对 AZ 是否重叠 |
| 写入 "Permission denied" | 缺 `ClientWrite`，或 POSIX UID/GID 不匹配 | 补权限或对齐 access point 的 posixUser |

官方明写：**每个挂载有 30 秒超时**，且**所有配置的文件系统并行挂载——单个挂载失败会导致整次调用失败**（`InvokeAgentRuntime` 返回 HTTP 424 Failed Dependency）。

另外两条不算挂载但值得一起记：**单 runtime 最多 5 个 filesystemConfigurations**（S3 Files 最多 2 个、EFS 最多 2 个、session storage 最多 1 个），可以混搭；EFS/S3 Files 均**不支持跨账号 VPC**（file system、access point、mount target 必须与 agent runtime 同账号同 VPC）。
