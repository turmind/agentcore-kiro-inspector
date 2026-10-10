# 网络与权限安全基线 —— Kiro 巡检（Amazon Bedrock AgentCore Runtime）

适用对象：开发、本地测试、构建、部署、运维这套巡检的所有人。部署步骤见 [`wecom-eventbridge-deploy.md`](wecom-eventbridge-deploy.md)，本文是那份文档的前置要求。

**硬性要求：全过程不开任何入站端口，不做任何公网可达的网络配置。** 下面第 3 节的禁止项和第 10 节的自检命令是验收标准，每次部署、变更之后都要跑一遍。

文中凡是官方原文都加了引号并附链接；标注「推断」「未验证」的，是官方没明写、我们也没实测过的内容。

---

## 1. 原则：只有出站，没有入站

方案里所有网络连接都由我们这一侧主动发起：

| 链路 | 发起方 → 目标 | 方向 |
|---|---|---|
| 定时触发 | EventBridge Scheduler → AgentCore `InvokeAgentRuntime` API，SigV4 签名 | 出站 |
| 巡检 | runtime 容器 → Kiro 模型服务、本 region 的 AWS 只读 API | 出站 |
| 推送 | runtime 容器 → 企业微信（智能机器人长连接 wss，或 webhook）/ 飞书 | 出站 |
| 拉镜像 | AgentCore 服务 → 本账号 ECR | 由 AWS 发起 |

没有哪条链路需要外部主动连进来。所以不需要、也不允许以下任何配置：入站安全组规则（第 3 节有唯一例外）、公网 IP、Function URL、资源策略放宽、对外发布的容器端口。

---

## 2. 调用入口的真实属性：它是 AWS 的公网 API，不是我们开的端口

AgentCore 的调用入口（`InvokeAgentRuntime`）是 AWS 托管的服务端点。官方网络连接模式 blog 原文：

> Pattern 1："By default, the endpoint of AI agents deployed on AgentCore Runtime is publicly accessible over internet. Customers can use both OAuth (JWT bearer token) or IAM SigV4 to provide access to the agent."
>
> Pattern 2："Configuring VPC connectivity doesn’t make the agent inaccessible from the public internet. To restrict inbound access, apply a resource-based policy as described in Pattern 3."
>
> —— [Network connectivity patterns for agents deployed on Amazon Bedrock AgentCore Runtime](https://aws.amazon.com/blogs/networking-and-content-delivery/network-connectivity-patterns-for-agents-deployed-on-amazon-bedrock-agentcore-runtime/)

据此要讲清三件事：

1. **PUBLIC 和 VPC 两种模式下，调用入口都是 AWS 的公网 API。** VPC 模式改变的只是 runtime **出站**时走哪里（在我们的子网里建 ENI），不会让入口变成私网。
2. **我们不配资源策略（RBP），也不配 OAuth authorizer**，所以调用方只能是**本账号内被 IAM 显式允许**的 principal，而且必须用 SigV4 签名。没有显式允许就会被拒（官方 [resource-based policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/resource-based-policies.html) 页：默认拒绝；显式 Deny 优先于任何 Allow）。
3. 这个入口是 AWS 服务本身的属性，不是我们开放的端口。我们能控制的是「谁能调用」（IAM），不是「端点在不在公网」。

**可选加固（本方案默认不做）：** 建 AgentCore 数据面的 PrivateLink 接口端点（`com.amazonaws.<region>.bedrock-agentcore`），再给 runtime 挂一条 **Deny 型**资源策略，只允许来自指定 VPC 或 VPC 端点的调用（blog Pattern 3/4；官方 `DenyAllExceptVPC` 示例用 `aws:SourceVpc` 条件，PrivateLink 场景用 `aws:SourceVpce`）。

> ⚠️ 推断，未验证：`DenyAllExceptVPC` 会拒绝所有不经该 VPC 的调用。Scheduler universal target 和不在 VPC 里的 Lambda 发出的请求都不带 `aws:SourceVpc`，所以启用后定时触发很可能被挡掉。要启用，必须先把触发链路改到 VPC 内（例如 Lambda 放进 VPC，经 PrivateLink 调用），并单独验证。

---

## 3. 禁止项清单

| # | 禁止 | 原因 | 怎么查（命令见第 10 节） |
|---|---|---|---|
| 1 | 给 runtime 或 endpoint 挂任何 **Allow 型**资源策略（`PutResourcePolicy`） | 资源策略能把调用权放给其他账号或 `*` | `get-resource-policy`，runtime ARN 和 `/runtime-endpoint/DEFAULT` 各查一次 |
| 2 | 配 OAuth / JWT authorizer（`authorizerConfiguration`） | 持 bearer token 的人就能从公网调用，不再受 IAM 约束 | `get-agent-runtime` 的 `authorizerConfiguration` 应为空 |
| 3 | Lambda Function URL；对 Lambda 调 `add-permission`（加资源策略） | 两者都会在 IAM 之外新开一条调用路径 | `lambda list-function-url-configs`、`lambda get-policy` |
| 4 | 安全组**入站**规则 | 本方案没有任何入站需求 | `describe-security-groups` |
| 5 | 任何以 CIDR 或 prefix list 为来源的入站规则（含 `0.0.0.0/0`、办公网段） | 同上 | 同上 |
| 6 | ECR 仓库策略（repository policy） | 拉镜像只走本账号 IAM（执行角色），仓库策略会把拉取权放出去 | `ecr get-repository-policy` |
| 7 | runtime 放在公有子网，或 runtime 子网路由表里有指向 IGW 的路由 | 放公有子网也拿不到公网访问（见第 8 节），反而让子网边界变得模糊 | `create_rt.py` 会直接拒绝 |
| 8 | 用 `create_rt.py --skip-network-checks` 跳过网络检查 | 跳过后上面第 4、5、7 项不再被检查 | 只允许在调用者没有 ec2 Describe 权限时使用，并且必须按第 10 节手工补查 |
| 9 | 本地 Docker 用 `-p <端口>:8080`（不带 `127.0.0.1`）或 `--network host` 起容器 | 见第 4 节 Docker 官方原文 | `docker ps --format '{{.Names}} {{.Ports}}'` |
| 10 | Docker daemon 开 `"allow-direct-routing": true`，或 bridge 网络用 gateway mode `nat-unprotected` | 见第 4 节 | 查 `/etc/docker/daemon.json` 和 `docker network inspect` |
| 11 | 往容器环境变量里放 IAM 用户的长期 AK/SK | 容器内任何进程都能读到环境变量（见第 9 节），而且容器停止后配置仍然留在本机 | `docker inspect` |
| 12 | 把 `InvokeAgentRuntimeCommand` / `InvokeAgentRuntimeCommandShell` / `*ForUser` 授给日常调用方 | 前两个能在 microVM 里执行命令；`ForUser` 允许调用方自报用户身份 | 查调用方的 IAM 策略；建议用第 9 节的 Deny 策略兜底 |

**唯一允许的入站规则：** 如果建了 VPC 接口端点，端点安全组放行**来自 runtime 安全组**的 TCP 443（以安全组 ID 为来源，不是 CIDR）。只在 VPC 内部生效，不涉及公网。runtime 安全组本身不需要任何入站规则。

---

## 4. 本地开发与测试

### 4.1 离线单测

`python3 -B tests/test_notify.py`、`tests/test_trigger.py`、`tests/test_create_rt.py` 全部离线运行，不访问云端。其中需要 HTTP 桩的用例只绑定 `127.0.0.1` 的临时端口，进程退出即释放。

### 4.2 容器冒烟测试：不发布端口

Docker 官方原文：

> "Publishing container ports is insecure by default. Meaning, when you publish a container's ports it becomes available not only to the Docker host, but to the outside world as well. If you include the localhost IP address (127.0.0.1, or ::1) with the publish flag, only the Docker host can access the published container port."
>
> `docker run -p 8080:80` "creates a mapping between port 8080 on any address on the Docker host"
>
> —— [Docker: Port publishing and mapping](https://docs.docker.com/engine/network/port-publishing/)

所以冒烟测试**不用 `-p`，也不用 `--network host`**，直接在容器内部访问：

```bash
docker run -d --name kn-smoke \
  -e KIRO_API_KEY \
  <IMAGE_URI>                      # 不加 -p，不加 --network host

docker exec kn-smoke curl -s http://localhost:8080/ping
docker exec kn-smoke curl -s -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' -d '{"prompt":"hello","notify":false}'

docker rm -f kn-smoke              # 用完立刻删，不留 Exited 容器
```

- 镜像里自带 `curl`（Dockerfile 装了），不需要从宿主机访问。
- `-e KIRO_API_KEY` 只写变量名，值从当前 shell 继承，不会出现在命令行历史里。
- 如果确实要从宿主机访问，只允许 `-p 127.0.0.1:<端口>:8080`。
- **官方排障页的示例是 `docker run -p 8080:8080`（[runtime troubleshooting](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)），本方案不照做。**

### 4.3 Docker 宿主机加固

- 把默认 bridge 网络发布端口的默认地址改成 `127.0.0.1`：在 `/etc/docker/daemon.json` 写 `{"ip": "127.0.0.1"}` 后重启 dockerd。这样即使有人漏写地址，端口也只绑回环。自建 bridge 网络在创建时加 `-o com.docker.network.bridge.host_binding_ipv4=127.0.0.1`。
- Docker 版本须 ≥ 28.0.0。官方警告："In releases older than 28.0.0, hosts within the same L2 segment ... can reach ports published to localhost"（[moby/moby#45610](https://github.com/moby/moby/issues/45610)）。
- 官方原文："By default, for both IPv4 and IPv6, the Docker daemon blocks access to ports that have not been published"。有两项配置会破坏这个默认行为，禁止使用：`"allow-direct-routing": true`；gateway mode `nat-unprotected`（官方原文："unpublished container ports are also accessible using direct routing, no port filtering rules are set up. This mode is included for compatibility with legacy default behaviour"）。

### 4.4 容器里的凭据

- 冒烟测试只需要 `KIRO_API_KEY`。容器不需要 AWS 凭据就能起来并响应 `/ping`。
- 确实要在本地让 Kiro 调 AWS 只读 API 时，用 STS 临时凭据（`aws sts assume-role` 拿一个只读角色，有效期设短），**不用 IAM 用户的长期 AK/SK**。测试完 `docker rm` 删掉容器，因为 `docker inspect` 能读出停止的容器里的环境变量。

---

## 5. 容器内监听 `0.0.0.0:8080` 的性质

`server.py` 监听 `0.0.0.0:8080`，这是 AgentCore 服务契约的要求。官方排障页把 504 的原因归为三项：容器必须暴露 8080 端口、必须提供 `/invocations` 路径、镜像必须是 ARM64（[runtime troubleshooting](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)）。

这个监听位于 AgentCore 托管的 microVM 内部，只接收 AgentCore 服务转发进来的请求，不在我们账号里产生任何可访问的地址。Dockerfile 里的 `EXPOSE 8080` 只是镜像元数据，不会发布端口。它在本地是否可达，只取决于第 4 节的 `docker run` 参数。

---

## 6. 网络模式：PUBLIC 与 VPC

| | PUBLIC | VPC |
|---|---|---|
| runtime 出站 | AWS 托管，直接可访问公网 | 经我们子网里的 ENI 出站；要访问公网必须走 NAT |
| 我们账号里的网络资源 | 无 | ENI（由服务关联角色 `AWSServiceRoleForBedrockAgentCoreNetwork` 创建）、安全组、路由 |
| 入站 | 无 | 无（runtime 安全组不需要入站规则，见第 8 节） |
| 出站可控性 | 不能用 SG / Flow Logs 约束 | 可以用 SG、NAT、VPC 端点、Flow Logs |

CDK 文档对 PUBLIC 模式的描述："By default, runtimes are deployed in PUBLIC network mode, which provides internet access suitable for less sensitive or open-use scenarios"（[aws-bedrockagentcore readme](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)）。

两种模式都满足「不开入站」。区别在出站能不能被我们审计和约束。需要出站白名单或流量审计时，用 VPC 模式，并按第 8 节配置。

---

## 7. 出站目的地清单

用于出站防火墙、代理白名单或 NAT 审计。端口都是 TCP 443。

### 7.1 运行时（runtime 容器发起）

**Kiro CLI**：以 Kiro 官方防火墙清单为准，[kiro.dev/docs/privacy-and-security/firewalls](https://kiro.dev/docs/privacy-and-security/firewalls/)。

| 类别 | 域名 |
|---|---|
| 所有 Kiro 产品 | `app.kiro.dev`、`assets.app.kiro.dev` |
| CLI | `cli.kiro.dev`、`rendering.aperture-public-api.feedback.console.aws.dev`、`prod.us-east-1.auth.desktop.kiro.dev`、`prod.us-east-1.telemetry.desktop.kiro.dev`、`prod.download.desktop.kiro.dev`、`prod.download.cli.kiro.dev`、`desktop-release.q.us-east-1.amazonaws.com` |
| 服务端点 | `runtime.us-east-1.kiro.dev`、`management.us-east-1.kiro.dev`、`telemetry.us-east-1.kiro.dev`，以及对应的 `.eu-central-1.kiro.dev` |
| 旧端点（仍须放行） | `q.us-east-1.amazonaws.com`、`q.eu-central-1.amazonaws.com`。官方原文："The `q.<region>.amazonaws.com` endpoints are legacy and will be deprecated in a future release. Until deprecation is complete, you must still allowlist them" |
| 通配写法 | `*.kiro.dev`、`*.app.kiro.dev`（部分防火墙的通配只匹配一级子域）、`*.kiro.aws.dev`（telemetry） |
| 可选 | `github.com`、`raw.githubusercontent.com`（仅在使用 MCP 时需要；本方案不用） |

> 推断，未验证：从 `kiro-cli-chat` 2.21.4 二进制中提取到的、不在官方清单里的生产域名如下。按官方清单配置后如果仍有连接失败，优先检查这些：`prod.us-east-1.telemetry-v2.kiro.dev`、`prod.eu-central-1.telemetry-v2.kiro.dev`、`client-telemetry.us-east-1.amazonaws.com`、`codewhisperer.<region>.amazonaws.com`、`oidc.<region>.amazonaws.com`、`sts.amazonaws.com`、`s3.amazonaws.com`、`ingestion.aperture-public-api.feedback.console.aws.dev`。我们没有抓包确认 Kiro 实际会连哪些域名。

Kiro 也提供 VPC 端点（`com.amazonaws.us-east-1.q`、`com.amazonaws.us-east-1.codewhisperer`、`com.amazonaws.eu-central-1.q`），但只在 us-east-1 和 eu-central-1 有（[Kiro VPC endpoints](https://kiro.dev/docs/privacy-and-security/vpc-endpoints.md)）。runtime 部署在其他 region 时用不上，Kiro 流量仍然走 NAT。

**消息推送**

| 渠道 | 域名 |
|---|---|
| 企业微信智能机器人长连接 | `openws.work.weixin.qq.com`（wss，443）。长连接是 runtime 主动连出去的，每份报告连一次、发完即断，不需要任何入站 |
| 企业微信消息推送（webhook） | `qyapi.weixin.qq.com` |
| 飞书 | `open.feishu.cn` |
| Lark 国际版 | `open.larksuite.com`（设了 `LARK_API_BASE` 时） |

**AWS API**（执行角色调用，本 region 端点）：`sts`、`rds`、`elasticache`、`memorydb`、`docdb-elastic`、`ec2`、`eks`、`monitoring`（CloudWatch）、`ssm`、`tag`，以及全局服务 `health`、`organizations`；可观测数据写 `logs` 和 `xray`。VPC 模式下，AgentCore 从 ECR 拉镜像也经过我们的 VPC（见第 8 节）。

### 7.2 构建时（构建机发起，不在 runtime 里）

| 用途 | 目的地 |
|---|---|
| 下载 Kiro CLI | `prod.download.cli.kiro.dev` |
| 基础镜像 | `public.ecr.aws` |
| apt 包 | `deb.debian.org` |
| AWS CLI 安装包 | `awscli.amazonaws.com` |
| pip（flask） | `pypi.org`、`files.pythonhosted.org` |
| arm64 模拟器（一次性） | Docker Hub（`tonistiigi/binfmt`） |
| 推送镜像 | 本账号 `<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com`、`api.ecr.<REGION>.amazonaws.com` |

### 7.3 走代理

Kiro 官方说明 CLI 支持 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`。浏览器登录流程会绕过代理，但本方案用 API key 无头运行，不涉及这个流程。推送模块 `notify.py` 用 Python 标准库 `urllib`，按其默认行为也会读 `HTTPS_PROXY`（推断，未验证）。

---

## 8. VPC 模式配置要求

官方依据：[agentcore-vpc](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)、[runtime-filesystem-configurations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)、[vpc-interface-endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)。

### 8.1 子网与路由

- **只用私有子网，并通过 NAT 出网。** 官方原文："AgentCore creates a network interface in your account with a private IP address. Using a public subnet does not provide internet connectivity. To enable internet access, place it in private subnets with a route to a NAT Gateway."，以及 "Connecting ... to public subnets does not provide internet access. Always use private subnets with NAT gateways"。NAT 网关本身放在公有子网里。
- VPC 模式默认不能访问公网。Kiro 模型服务和 IM webhook 都在公网上，所以 NAT 是必需的。
- 至少 2 个不同 AZ 的私有子网。
- **子网必须位于 AgentCore 支持的 AZ ID。** 例如东京是 `apne1-az1`、`apne1-az2`、`apne1-az4`，新加坡是 `apse1-az1`、`apse1-az2`、`apse1-az3`。其他 region 以官方表格为准。子网不在支持的 AZ 会导致创建失败。`create_rt.py` **不检查**这一项，要人工核对：

  ```bash
  aws ec2 describe-subnets --subnet-ids <SUBNET_ID> --query 'Subnets[0].AvailabilityZoneId'
  ```

  注意：AZ 名称（如 `ap-northeast-1a`）在不同账号里对应的物理机房不同，只有 AZ ID 是一致的。

- VPC 的 `enableDnsSupport` 和 `enableDnsHostnames` 都要开。

### 8.2 安全组

| 安全组 | 入站 | 出站 |
|---|---|---|
| runtime SG | **无**。官方原文："Not required. The runtime only initiates outbound connections." | TCP 443 → `0.0.0.0/0`（经 NAT 访问公网） |
| 接口端点 SG（如果建端点） | TCP 443 ← runtime SG | — |

安全组默认出站是全部放开，要把它换成上表的 443。官方建议："Define outbound rules that allow only the minimum required traffic. Do not open broad outbound access unless necessary."（[runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)）

```bash
# runtime SG：删掉默认的全放开出站，只留 443
aws ec2 revoke-security-group-egress  --group-id <RT_SG> --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]'
aws ec2 authorize-security-group-egress --group-id <RT_SG> --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]'
```

### 8.3 VPC 端点

官方对端点的定位："required if your VPC does not have internet access, and strongly recommended even if your VPC has a NAT gateway"。原因之一是 "AgentCore periodically refreshes your container image from ECR"。

| 端点 | 类型 | 说明 |
|---|---|---|
| `com.amazonaws.<region>.ecr.dkr` | Interface | 拉镜像 |
| `com.amazonaws.<region>.ecr.api` | Interface | 拉镜像 |
| `com.amazonaws.<region>.s3` | Gateway | 镜像层存放在 S3 |
| `com.amazonaws.<region>.logs` | Interface | 日志 |
| `com.amazonaws.<region>.bedrock-agentcore` | Interface | AgentCore 数据面，开 private DNS |
| `com.amazonaws.<region>.bedrock-agentcore-control` | Interface | AgentCore 控制面，开 private DNS |

S3 Gateway 端点如果挂了端点策略，要放行 ECR 镜像层所在的桶。官方示例（`<region>` 替换为实际 region）：

```json
{
  "Sid": "AllowECRLayerAccess",
  "Effect": "Allow",
  "Principal": "*",
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::prod-<region>-starport-layer-bucket/*"
}
```

使用 session storage 时，还要按官方页放行 `acr-storage-*-<region>-an` 这类桶。接口端点的默认端点策略是完全放开的，端点策略只能按 IAM principal 限制。

### 8.4 Flow Logs

在 runtime 所在的 VPC（或其子网）开 [VPC Flow Logs](https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html)，作为出站审计依据。官方 VPC 页也推荐开启。

### 8.5 用 IAM / SCP 强制只能建 VPC 模式（可选）

官方示例 `EnforceVPCRuntime`：没有指定子网和安全组时，拒绝创建或更新 runtime（[security-vpc-condition](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-vpc-condition.html)）：

```json
{
  "Sid": "EnforceVPCRuntime",
  "Effect": "Deny",
  "Action": [
    "bedrock-agentcore:CreateAgentRuntime",
    "bedrock-agentcore:UpdateAgentRuntime",
    "bedrock-agentcore:CreateCodeInterpreter",
    "bedrock-agentcore:CreateBrowser"
  ],
  "Resource": "*",
  "Condition": {
    "Null": {
      "bedrock-agentcore:subnets": "true",
      "bedrock-agentcore:securityGroups": "true"
    }
  }
}
```

进一步限定只能用批准过的子网和安全组：

```json
{
  "Sid": "DenyIfNotSpecificSubnets",
  "Effect": "Deny",
  "Action": ["bedrock-agentcore:CreateAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime"],
  "Resource": "*",
  "Condition": {
    "ForAnyValue:StringNotEquals": {
      "bedrock-agentcore:subnets": ["<SUBNET_ID_1>", "<SUBNET_ID_2>"]
    }
  }
},
{
  "Sid": "DenyIfNotSpecificSecurityGroups",
  "Effect": "Deny",
  "Action": ["bedrock-agentcore:CreateAgentRuntime", "bedrock-agentcore:UpdateAgentRuntime"],
  "Resource": "*",
  "Condition": {
    "ForAnyValue:StringNotEquals": {
      "bedrock-agentcore:securityGroups": ["<RT_SG>"]
    }
  }
}
```

注意：启用 `EnforceVPCRuntime` 后，PUBLIC 模式的 runtime 就不能再创建或更新了。

---

## 9. 权限收紧

官方依据：[runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)。

### 9.1 真正的安全边界是执行角色，不是提示词

官方原文："Any code or actor running inside the microVM can access execution role credentials by calling the metadata endpoint (MMDS)"。

本方案用 `--trust-all-tools` 运行 Kiro（没有设置 `KIRO_TRUST_TOOLS` 时的默认值），Kiro 可以执行 shell 命令和 AWS 调用。提示词里写「只读、禁止修改」只是对模型的指示，**不是安全边界**。真正决定 Kiro 能做什么的，是执行角色的权限：

- `iam/runtime-execution-policy.json` 的巡检部分只有 20 个只读动作（`Describe*` / `List*` / `Get*` 类，外加 `sts:GetCallerIdentity`），**没有任何写权限，也没有 `bedrock:InvokeModel`**（模型走 Kiro 自己的 API key）。
- 官方原文：执行角色应该拥有 "equal or fewer privileges than the principals who can invoke it"。
- 官方原文："Do not use CLI-generated policies in production"。策略要按具体 runtime ARN 或资源范围收窄。
- 策略改动后用 IAM Access Analyzer 校验（`aws accessanalyzer validate-policy`）。

### 9.2 MMDSv2

创建 runtime 时要设 `metadataConfiguration.requireMMDSV2=true`。官方说明：2026-06-30 起必须开启，没开或设为 false 的 runtime，调用会被拒绝。`CreateAgentRuntime` API 不接受这个参数，所以 `create_rt.py` 在创建完成后会自动再做一次 Update 来开启。

### 9.3 调用方策略：只给 `InvokeAgentRuntime`，并显式拒绝身份委托类动作

调用方（Scheduler 角色、运维人员）只授予 `bedrock-agentcore:InvokeAgentRuntime`，资源限定到具体 runtime 的 ARN，不用 `runtime/*`。

AWS 托管策略 `BedrockAgentCoreFullAccess` 包含 `GetWorkloadAccessTokenForUserId`（[托管策略说明](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-iam-awsmanpol.html)）。生产环境建议给所有 principal 加一条 Deny。官方示例：

```json
{
  "Sid": "DenyUserIdDelegation",
  "Effect": "Deny",
  "Action": "bedrock-agentcore:InvokeAgentRuntimeForUser",
  "Resource": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:runtime/*"
}
```

我们的扩展（**自拟，不是官方示例**）：把同类的 WebSocket 动作，以及本方案用不到的命令执行、资源策略写入也一起拒绝。动作名以 [Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html) 为准：

```json
{
  "Sid": "DenyUnusedPowerfulActions",
  "Effect": "Deny",
  "Action": [
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:PutResourcePolicy"
  ],
  "Resource": "*"
}
```

这条 Deny 应该挂在日常调用方和运维人员身上。部署管理员如果确实需要排障（`InvokeAgentRuntimeCommand`），走单独的角色。

### 9.4 两层授权

加了资源策略之后，调用要同时通过身份策略和资源策略两层才会放行（官方原文 "Both must allow"）。本方案不配资源策略，所以只有 IAM 身份策略这一层起作用。按默认拒绝规则，没被显式允许的 principal 都调不了。

### 9.5 容器内部的已知暴露面

以下几点如实说明，缓解方向**未实现、未验证**：

- **推送凭据对 Kiro 可见。** `server.py` 启动 Kiro 子进程前，会把 `WECOM_WEBHOOK_KEY`、`LARK_APP_ID`、`LARK_APP_KEY` 从子进程环境变量里删掉。但 Kiro 和 bridge 以同一个用户（root）运行，Kiro 可以通过 `/proc/<bridge 的 PID>/environ` 读到这些值。`KIRO_API_KEY` 则本来就在 Kiro 的环境变量里（Kiro 要靠它认证）。所以准确的说法是：**「不主动传给 Kiro」，而不是「Kiro 读不到」**。缓解方向：Kiro 改用独立 uid 运行，或者用 `KIRO_TRUST_TOOLS` 收窄工具范围。
- **容器以 root 运行。** 官方建议 "Run containers as non-root users"。目前 Dockerfile 没有 `USER` 指令。注意：官方排障页提到，镜像超过 53 层并且 `USER` 用非数字名时会报 424。
- **platform server。** Kiro 的 shell 可以访问 microVM 内 localhost 上的平台服务。官方说明影响 "is limited to the current VM session ... can disrupt the session’s VM lifecycle or provide shell access within that session"，并建议 "Restrict localhost access in agent code"、"Audit network tools for localhost reach"。
- **审计。** AgentCore 的控制面调用会记录到 CloudTrail，用于事后追溯。

---

## 10. 自检命令（每次部署、变更后执行）

先设好变量：`REGION`、`ACCOUNT`、`RUNTIME_ID`、`REPO=bedrock-agentcore-kiro-inspector`。所有命令都是只读的。

```bash
RT_ARN=arn:aws:bedrock-agentcore:$REGION:$ACCOUNT:runtime/$RUNTIME_ID

# 1. runtime 和 endpoint 都没有资源策略：期望报 ResourceNotFound，或 policy 为空
aws bedrock-agentcore-control get-resource-policy --region $REGION --resource-arn $RT_ARN
aws bedrock-agentcore-control get-resource-policy --region $REGION --resource-arn $RT_ARN/runtime-endpoint/DEFAULT

# 2. 没有 authorizer、MMDSv2 已开、网络模式符合预期
aws bedrock-agentcore-control get-agent-runtime --region $REGION --agent-runtime-id $RUNTIME_ID \
  --query '{auth:authorizerConfiguration,mmds:metadataConfiguration,net:networkConfiguration}'

# 3. 只有 DEFAULT 一个 endpoint
aws bedrock-agentcore-control list-agent-runtime-endpoints --region $REGION --agent-runtime-id $RUNTIME_ID \
  --query 'runtimeEndpoints[].name'

# 4. ECR 没有仓库策略：期望报 RepositoryPolicyNotFoundException
aws ecr get-repository-policy --region $REGION --repository-name $REPO

# 5.（原 Lambda 检查，已无 Lambda）

# 6. （VPC 模式）安全组入站：runtime SG 应为空
aws ec2 describe-security-groups --region $REGION --group-ids <RT_SG> \
  --query 'SecurityGroups[].{id:GroupId,in:IpPermissions,out:IpPermissionsEgress}'

# 7. （VPC 模式）VPC 端点和 Flow Logs
aws ec2 describe-vpc-endpoints --region $REGION --filters Name=vpc-id,Values=<VPC_ID> \
  --query 'VpcEndpoints[].[ServiceName,VpcEndpointType,PrivateDnsEnabled]'
aws ec2 describe-flow-logs --region $REGION --filter Name=resource-id,Values=<VPC_ID>

# 8. 本地构建 / 测试机：没有对外监听，没有对外发布的容器端口
ss -ltn                                            # 不应出现 0.0.0.0 或 [::] 上与本项目相关的端口
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'   # 本项目容器 Ports 列应为空或只有 127.0.0.1
```

**判定：** 第 1、4、5 项报「不存在」就是合规；第 6 项中出现任何 CIDR 或 prefix list 来源的入站规则，都算不合规；第 8 项中本项目容器出现 `0.0.0.0:` 或 `:::` 映射，也算不合规。
