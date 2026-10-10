# AgentCore Runtime 上下文/状态保持 —— 官方机制调研笔记

纯文档调研，不涉及代码改动或建资源。目的：回答「Kiro CLI 这种外部 headless CLI 二进制，能不能在
AgentCore Runtime 上实现多轮对话上下文保持」。所有结论均标注官方链接原文，查不到的明确写"文档未说明"。

---

## A. runtimeSessionId 的语义

### A1. 同一个 `runtimeSessionId` 的多次调用是否路由到同一个 microVM？

**是，官方明确保证。** 三处独立表述互相印证：

> "Runs in a dedicated microVM with completely isolated CPU, memory, and filesystem resources"
> "Preserves context across multiple interactions within the same conversation"

—— [microVMs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)

> "A new session is created on the first invoke with a unique runtimeSessionId provided by your
> application. AgentCore Runtime provisions a dedicated execution environment (microVM) for each
> session. Context is preserved between invocations to the same session."

—— [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)

> "**MicroVM stickiness**: Amazon Bedrock AgentCore uses the session header to route requests to
> the same microVM instance. Clients must capture the session ID returned in the response and
> include it in all subsequent requests to ensure session affinity. Without a consistent session ID,
> each request may be routed to a new microVM, which may result in additional latency due to cold starts."

—— 同上，「Session headers by protocol」节。**协议不同、header 名不同**：HTTP/A2A/AG-UI 用
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`，MCP 用 `Mcp-Session-Id`。

### A2. 实例内的文件系统状态和内存进程状态在两次调用之间是否保留？

**只要 microVM 没被终止（session 仍 Active/Idle），保留；一旦终止，清空重建。** 关键原文：

> "By default, the compute (microVM) associated with a session is **ephemeral**. Any data stored in
> memory or written to disk persists only for the compute lifecycle. This includes conversation
> history, user preferences, intermediate calculation results, and any other state information your
> agent maintains."

—— [Use isolated sessions for agents §Understanding ephemeral context](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)

> "After session termination, the entire microVM is terminated and memory is sanitized to remove all
> session data, eliminating cross-session contamination risks."
> "A subsequent request with the same `runtimeSessionId` after termination will create a new
> execution environment."

—— [microVMs §Sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)

即：**同一 session、compute 未终止期间** —— 内存中的进程状态、`/tmp` 写入的文件都在，不会重建容器；
**compute 一旦终止**（idle 超时 / maxLifetime 到期 / 显式 Stop / 健康检查判定不健康）—— 整个
microVM 被销毁、内存被 sanitize，下一次调用即使用同一个 `runtimeSessionId` 也是全新环境。

安全文档补一句更硬的表述："Commands and agent code cannot access other customers' workloads or
escape the VM boundary. After session completion, **the entire microVM is terminated and memory is
sanitized**." —— [Security best practices for AgentCore Runtime §Session isolation and data
protection](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)

### A3. `idleRuntimeSessionTimeout` / `maxLifetime` 语义 + session 结束后状态处理

官方权威定义表（[Configure Amazon Bedrock AgentCore lifecycle
settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)）：

| 属性 | 取值范围（microVM） | 默认值 | 语义 |
|---|---|---|---|
| `idleRuntimeSessionTimeout` | 60–28800 秒 | 900 秒（15 分钟） | "Timeout in seconds for idle runtime sessions. When a session remains idle for this duration, it will trigger termination." |
| `maxLifetime` | 60–28800 秒 | 28800 秒（8 小时） | "Maximum lifetime for the instance in seconds. Once reached, instances will initialize termination. **The session itself can persist beyond this with a new instance provisioned.**" |

两者关系的约束："`idleRuntimeSessionTimeout` must be less than or equal to `maxLifetime`"；
终止本身"can last up to 15 seconds due to logging and other process completion"。

**session 结束后状态怎么处理**：session 状态机三态 Active / Idle / Terminated，官方原文：

> "**Terminated** - Session ended due to inactivity (15 minutes), reaching maximum lifetime
> (8 hours), or being deemed unhealthy... The session transitions back to Active on the next
> invocation and **a new compute is provisioned**, with the same lifecycle configuration... **The
> session itself remains valid until the AgentCore Runtime ARN is deleted.** If the runtime is
> configured with session storage, filesystem data at the configured mount path persists across
> stop/resume cycles."

—— [Use isolated sessions for agents §AgentCore Runtime session lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)

关键区分：**"session"这个逻辑概念**（由 `runtimeSessionId` 标识）在 Runtime ARN 删除前一直"有效"，
可以随时用同一 ID 重新唤起；但**它背后的 compute（microVM）**是随 idle/maxLifetime 终止就销毁重建的——
"session 没结束，只是换了一个空的 microVM"。除非配置了 session storage（见 C2），否则新 compute 里
文件系统和内存状态都是空的。

### A4. 不传 `runtimeSessionId` 会发生什么？

> "Is identified by a unique `runtimeSessionId` provided by your application, **or by the Runtime
> itself in the first invocation if the `runtimeSessionId` is left empty**"

—— [microVMs §Sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)

即：Runtime 会自动生成一个。但**要维持多轮上下文，调用方必须自己保存这个自动生成的 ID 并在后续调用
中复用**——"Clients must capture the session ID returned in the response and include it in all
subsequent requests to ensure session affinity."（同 A1 引用的 microVM stickiness 段）。不传或每次都
不传同一个值 ⇒ 每次都路由到不同/新的 microVM ⇒ 上下文不会保留。

`runtimeSessionId` 本身还有长度约束：33–256 字符（与我们 README 第 5 条记录的真机实测一致，
`uuid4().hex` 32 字符会被拒）。

---

## B. AgentCore Memory 服务

### B1. 它是什么形态——API 需 agent 主动调用，还是对 Runtime 透明？

**默认是 API 形态，需要 agent 代码主动调用**；只有"harness"（AWS 托管的 Strands agent 循环）这一
条路径下才是透明的。两条路径要分清楚：

- **Runtime + 自定义代码（我们的场景）**：Memory 是一组独立 API（`CreateEvent` / `ListEvents` /
  `RetrieveMemoryRecords` 等），agent 代码要显式调用。官方明确把 Runtime 和 harness 的责任边界写清楚：

  > "**AgentCore Runtime** is a *serverless hosting environment*. ... *The orchestration loop is
  > yours.* **To use any other AgentCore primitive (Memory, Gateway, Browser, Code Interpreter,
  > outbound Identity), you call it from your code**, typically through the AgentCore SDK."

  —— [AgentCore harness vs. Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html)

- **AgentCore harness（配置式，Strands 驱动的托管循环，与我们的自定义容器场景不是同一产品面）**：
  这一层可以做到"透明"：

  > "When memory is enabled, **the harness persists conversation state in AgentCore Memory. On
  > subsequent invocations with the same session ID, the agent loads the stored history before it
  > reasons.**"

  —— [Memory (harness)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)

  这里"透明"针对的是 harness 这个托管 agent loop 本身，不是我们跑在 Runtime 上的自定义容器。

写事件的最小三要素：`memoryId` + `actorId`（标识用户/agent/项目等实体）+ `sessionId`（把相关事件分组）。
——[Create an event](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html)

### B2. short-term / long-term 区别；long-term 三档（实为四档）内置策略

> "**Short-term memory** captures raw events (messages, tool calls) within a session... **Long-term
> memory** extracts durable knowledge via configurable strategies (semantic, summarization, user
> preference, episodic, or custom) and makes them retrievable via semantic search in later sessions."

—— [Memory (harness)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)

内置策略表（[Memory (harness) §Available strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)，
与 [Built-in strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html) 一致）：

| 策略 | 说明 |
|---|---|
| `SEMANTIC` | "Extracts factual knowledge from conversations, retrievable via semantic search." |
| `SUMMARIZATION` | "Creates running summaries of conversations, scoped by actor and session." |
| `USER_PREFERENCE` | "Captures user preferences and settings expressed during conversations." |
| `EPISODIC` | "Records significant events and experiences as discrete episodes." |

（用户问题里说"三档"——官方内置策略实为**四档**：Semantic / Summarization / User Preference /
Episodic，此前部分博客文档只列前三个，[Built-in strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html)
页面 Topics 列表四个都有。另支持 Custom strategy 自定义 LLM+prompt。）

long-term 生成机制："Long-term memory generation is an **asynchronous** process that runs in the
background and automatically extracts insights after raw conversation/context is stored in
short-term memory via `CreateEvent`... **Extraction** → **Consolidation**" —— [Memory
types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-types.html)

### B3. 保留期、region 可用性、定价

**保留期（短期事件）**：`eventExpiryDuration`，**7–365 天**，CDK 文档给出的默认值是 90 天：
"Short-term memory expiration in days (between 7 and 365)... default: 90 days" —— [interface
MemoryProps · AWS
CDK](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore.MemoryProps.html)。
long-term 记录的保留期文档未给出统一数字上限（取决于策略配置与消费方式），本次调研未查到独立于
event 之外的 long-term 硬性到期时间，如需精确值需另查 `CreateMemory`/`UpdateMemory` API 参考。

**Region 可用性**：AgentCore Memory 明确列在 [Supported AWS
Regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) 表中，
US East (N. Virginia/Ohio)、US West (Oregon)、Europe (Frankfurt/Ireland/London/Paris/Stockholm)、
Asia Pacific (Mumbai/Singapore/Sydney/Tokyo/Seoul)、Canada (Central)、South America (São Paulo)
均为 Yes；**US West (N. California)、Europe (Milan/Spain)、Asia Pacific (Malaysia/Hyderabad/Thailand)
为 No**。⚠️ 我们当前 Runtime 部署在 **ap-northeast-1（Tokyo）**——该表 Memory 一列在 Tokyo 是
"✓ Yes"，可用。

**定价**（[Amazon Bedrock AgentCore Pricing §Memory](https://aws.amazon.com/bedrock/agentcore/pricing/)）：

> "1. Short-term memory is priced based on the number of raw events created...
> 2. Long-term memory records is priced based on the number of memories processed and stored each
> month and the number of memory record retrieval calls...
> + For short-term memory, billing is calculated **per create event request**
> + For long-term memory storage, billing is calculated **per stored memory record and billed hourly**
> + For long-term memory retrieval, billing is calculated **per retrieve memory request**"

官方给出的示例单价（同页 Pricing Examples）：短期事件 $0.25/1,000 次、长期记忆存储 $0.75/1,000
条（内置策略默认）或 $0.25/1,000 条（自定义策略覆盖）、长期检索 $0.50/1,000 次。示例整月总计
$42.50（10 万事件 + 1 万条长期存储 + 2 万次检索）。

### B4. 外部 CLI 二进制能不能用上 Memory？

**结论：能用，但必须由「Flask bridge 层」的代码主动调 Memory API，Kiro CLI 二进制本身不会、也不
需要感知 Memory 的存在。** 依据链条：

1. Runtime 层的 Memory 集成方式就是"你的代码调用 SDK/API"（B1 引用的 harness-vs-runtime 原文），
   没有"容器内任意进程自动接入 Memory"这种机制——官方所有自定义 agent 的示例代码都是显式
   `boto3.client('bedrock-agentcore').create_event(...)`。
2. Kiro CLI 是黑盒二进制，我们不可能改它的源码去插入 Memory API 调用。但**我们自己的 Flask
   bridge**（`server.py`，`/invocations` 的处理逻辑）是我们写的 Python 代码，完全可以在"调用 Kiro
   CLI 之前"读一次 Memory（把历史摘要/长期记忆拼进 prompt 前缀），"调用 Kiro CLI 之后"写一次 Memory
   （把这轮的 prompt + 结果存成一个 event）。
3. 官方文档也确认了这条路径的存在——即便对"自定义运行时 HTTP 服务器"（custom runtime server，
   不用 AgentCore Python SDK 的场景），仍可以从 HTTP header 里拿到 `runtimeSessionId`
   （见 A1 troubleshooting 引用），配合 `actorId` 自行组织到 Memory 的 `CreateEvent` 调用中。

即：**Memory 对 Kiro CLI 二进制不透明，但对我们的 bridge 代码是可编程接入的**，这是一个中间层
翻译工作，不是 Kiro CLI 或 AgentCore Runtime 自动帮我们做的。

---

## C. 持久化文件存储

### C1. AgentCore Runtime 能不能挂 EFS / EBS / 任何持久卷？

**能，官方明确支持三类持久化文件系统配置**（[File system configurations for AgentCore
Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)）：

| 类别 | 类型 | 隔离级别 | Compute 类型 | 需要 VPC |
|---|---|---|---|---|
| Managed | Session storage (**Preview**) | 每 session 独立 | 仅 microVM | 不需要 |
| Managed | Capacity provider volume（Amazon **EBS**） | 每 session 独立 | 仅 Instances | Instances 上配置 |
| BYO | Amazon **S3 Files** access point | 跨 session/agent 共享 | microVM | 需要 |
| BYO | Amazon **EFS** access point | 跨 session/agent 共享 | microVM | 需要 |

原文：

> "AgentCore Runtime supports persistent file systems through the `filesystemConfigurations`
> parameter... You don't need custom mount code, privileged containers, or download orchestration."

**我们当前的 microVM 部署形态可用的持久化选项是：session storage（Preview）、S3 Files access
point、EFS access point** ——EBS（capacity provider volume）只在 Instances 计算类型上可用，与我们
用的 microVM 是两条产品线（Instances 是"跑在你自己账号 EC2 上的"重量级持久化路径，最长 14 天
session，本次不展开，属另一条计算类型）。

限制（[同页 §Limits](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)）：
每个 Runtime 最多 5 个文件系统配置总数，最多 1 个 managed session storage，最多 2 个 S3 Files，
最多 2 个 EFS；mount path 必须在 `/mnt/` 下且只有一级子目录（如 `/mnt/workspace`）。

### C2. "Managed session storage"是什么、什么状态、语义

**是 C1 表里 Managed 类别下的一种，当前状态是 Preview。** 语义原文：

> "**Session storage (Preview)** – Per-session storage on microVM runtimes that persists across
> stop/resume cycles. Isolated per session. No VPC required."

> "AgentCore Runtime managed session storage is a fully service-managed capability where AgentCore
> Runtime handles all storage operations. Your agent reads and writes to a local file system mount
> and the runtime environment transparently replicates data to service storage throughout the
> session duration.
>
> 1. **First invoke on a session** – A new isolated compute is provisioned. Your agent sees an empty
>    directory at the mount path.
> 2. **Agent writes files** – ...data is asynchronously replicated to durable storage.
> 3. **Session stops** – The compute is terminated. Any data not yet persisted is flushed to durable
>    storage during graceful shutdown.
> 4. **Resume with same session** – A new compute is provisioned and the file system state is
>    restored from durable storage."

—— [File system configurations for AgentCore
Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html)

生命周期细节（同页 §Lifecycle behavior 表）：**14 天无调用后 idle expiry、数据重置**；**Runtime
版本更新会导致数据被清空、下次调用是干净文件系统**；`DeleteAgentRuntime` 时数据被删除。

定价备注（pricing 页原文）："The managed session storage (currently in public preview) is an early
version of the S3-backed storage; **we will come out with changes including pricing to that
experience before GA**." —— [AgentCore Pricing §Runtime microVMs](https://aws.amazon.com/bedrock/agentcore/pricing/)
即：**Preview 阶段目前没有独立计费项，GA 前会引入定价变更**。

**关键约束**："The mounted path is available **only at the time of agent invocation, not during
initialization**." —— 同页正文。即容器启动时（`/ping` 就绪判断阶段）还看不到 mount，只有真正处理
一次 `/invocations` 请求时才能访问。

### C3. 官方推荐的"agent 状态外置"模式

官方原文直接给出选择依据（[Use isolated sessions for agents §Understanding ephemeral
context](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)）：

> "To persist filesystem data across session stop/resume cycles, configure **session storage**...
> For structured data that needs to be retained beyond the session lifetime (such as user
> conversation history, learned preferences, or important insights), use **AgentCore Memory**."

同一份博客也给出了"不用官方机制时的土办法及其代价"作对照：

> "This limitation can be addressed by writing checkpoint logic to upload files to Amazon S3 before
> stopping a session and downloading them on resume... This workaround can work, but it doesn't
> address the limitations at the filesystem level and complexity is being added into agent code."

—— [Persist session state with filesystem configuration and execute shell
commands](https://aws.amazon.com/blogs/machine-learning/persist-session-state-with-filesystem-configuration-and-execute-shell-commands/)

即官方的选择树是：**文件/工作区类状态 → session storage（或 BYO S3/EFS）；结构化对话历史/长期
知识 → AgentCore Memory；两者都不用、自己在 S3/DynamoDB 里手搓 checkpoint 逻辑**是文档承认存在但
明确说"增加复杂度"的退路，不是首选推荐。文档中未出现把 DynamoDB 作为官方推荐外置存储的表述——
这条是我们自己可以做的工程选择，不是 AgentCore 文档指定的模式。

---

## D. Identity / 凭据与 session 的关系

### D1. `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header 是干什么的？

**它与"每 session 独立 microVM 隔离"是两件不同的事——它管的是"以哪个终端用户身份去换取下游
OAuth token"，不是路由或计算隔离。** 原文：

> "**IAM SigV4 Authentication**: The default authentication and authorization mechanism...
> **X-Amzn-Bedrock-AgentCore-Runtime-User-Id Header**: If your solution requires the hosted agent to
> retrieve OAuth tokens on behalf of end users (using Authorization Code Grant), you can specify the
> user identifier by including [this header] in your requests. This header uses the
> `GetWorkloadAccessTokenForUserId` path internally."
>
> "Invoking InvokeAgentRuntime with the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header will
> require a new IAM action: `bedrock-agentcore:InvokeAgentRuntimeForUser`."
>
> "The `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header path **does not verify the userId against
> an authenticated end-user identity** — it relies on the calling workload to pass the correct
> [value]. For production deployments where you have an identity provider configured, use JWT
> Bearer Token authentication instead."

—— [Authenticate and authorize with Inbound Auth and Outbound
Auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)

**它跟 session 隔离的关系**：`runtimeSessionId`（决定路由到哪个 microVM、决定 Memory 里
`actorId`+`sessionId` 的作用域）与 `User-Id` header（决定 Outbound Auth 时用谁的身份去换第三方
OAuth token）是**两条独立的坐标轴**，可以同时使用但互不替代——一个人可以在一个 session 里代表多个
下游身份调用外部服务，也可以用不同的 session 代表同一个下游身份的不同对话。官方没有把两者绑定
的表述。

### D2. 每个 session 独立 microVM——官方原文出处汇总

已在 A1/A2 详细引用，此处汇总最权威的三处原始出处：

1. [microVMs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html) ——
   服务架构总览页，"Runs in a dedicated microVM with completely isolated CPU, memory, and filesystem
   resources"。
2. [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html) ——
   安全设计动机页，"Each user session in AgentCore Runtime receives its own dedicated microVM with
   isolated Compute, memory, and filesystem resources. This prevents one user's agent from accessing
   another user's data."
3. [Security best practices for AgentCore
   Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) ——
   安全基线页，"Each user session runs in a dedicated microVM with isolated CPU, memory, and
   filesystem. Commands and agent code **cannot access other customers' workloads or escape the VM
   boundary**."

三处相互印证、口径一致，可视为该产品的核心设计承诺（非某处孤证）。

---

## 对我们这个 Kiro CLI 场景的直接含义

**结论：同 session 多轮调用「在 Runtime 层面」可行，能让 Kiro 看到上一轮上下文——但前提是我们必须
主动改造 bridge 代码，当前的完全无状态实现（每次 `mkdtemp`+`rmtree`）恰恰是在人为丢弃这个能力，
而不是 Runtime 不支持。**

依据 A1+A2 的组合：

1. **只要同一个 `runtimeSessionId` 在 15 分钟 idle / 8 小时 maxLifetime 窗口内被复用，AgentCore
   Runtime 保证路由到同一个 microVM**，容器进程不会被杀、`/tmp` 里写的文件不会消失（A1、A2）。
2. **我们当前的实现主动抵消了这个能力**：`server.py` 每次处理 `/invocations` 都 `mkdtemp()` 建一
   个全新 `HOME`/cwd 给 Kiro 用，处理完 `rmtree()`。即使 AgentCore 把第二次调用正确路由到了同一个
   microVM，Kiro 在这个新的空目录里也看不到任何上一轮留下的会话文件/配置——**这是我们自己代码
   造成的无状态，不是 Runtime 本身的限制**。
3. **要让 Kiro 看到上一轮对话，最小改动路径是**：把 `mkdtemp()`/`rmtree()` 换成一个固定的、按
   `runtimeSessionId` 命名的工作目录（例如 `/tmp/session-{runtimeSessionId}` 或直接用 C2 的
   managed session storage 挂载路径 `/mnt/workspace`），首次调用建立、后续调用复用、不再删除。
   - 如果只依赖容器内 `/tmp`（不挂 session storage）：**能在同一 microVM 存活期内工作**，但一旦
     microVM 因 idle 超时/maxLifetime/健康检查被终止（A3），状态全部清空，下次调用即使
     `runtimeSessionId` 相同也是从零开始——这是"临时有效、不保真正长期"的方案，适合"一次巡检
     任务内的多轮追问"，不适合"隔了几小时后还要接上上下文"。
   - 如果配置 C2 的 session storage（当前 Preview 状态）：可以做到 microVM 终止-重建后**文件系统
     状态仍在**（14 天 idle expiry 之内），但**目前没有独立计费项、GA 前会变定价**，且"版本更新
     会清空数据"这条对我们经常改镜像迭代的场景有实际影响，需要评估。
4. **对话历史本身（Kiro 内部的会话记忆），如果 Kiro CLI 不支持把会话状态写到指定目录 / 从指定目录
   读回**（这一点是 Kiro 产品自己的行为，AgentCore 文档管不到），光靠"保留文件系统"未必等价于
   "Kiro 记得上一轮说了什么"——这取决于 Kiro CLI 是否把会话历史落盘在我们能控制的 `HOME`/工作目录
   下（本次调研未验证，需要真机测试 Kiro CLI 的会话持久化文件具体落在哪里，是否可被我们的固定
   工作目录方案覆盖到）。
5. **AgentCore Memory（B）是另一条独立可选的路径**，与"microVM 状态保留"完全不依赖同一套机制：
   即使每次都换新的空 microVM，我们的 bridge 代码也可以在调用 Kiro 前后手动读写 Memory API，把
   "上一轮说了什么"作为文本注入到这一轮的 prompt 里。这条路径**不受 idle/maxLifetime 终止影响**，
   是更适合"跨很长时间间隔仍要记住"的场景，但需要我们自己在 bridge 里写读写 Memory 的代码
   （B1/B4 已确认：Memory 对 Kiro 二进制不透明，必须由我们的 Python bridge 显式调用）。

**一句话总结**：Runtime 层「同 session 路由到同一 microVV + 状态不因调用次数而重建」这条能力
确实存在且有官方保证，问题出在我们自己为了追求"无状态、干净"而主动关掉了它；真正要不要打开、
打开到什么程度（仅内存/tmp vs. session storage vs. Memory 服务），是一个工程取舍问题，不是
Runtime 能力缺失问题。
