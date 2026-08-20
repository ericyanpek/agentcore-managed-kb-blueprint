# ADR-001: 采用 Step Functions 加最小 Lambda 的执行模型

- 状态：已接受
- 日期：2026-08-18

## 背景

Managed KB 文档摄入是异步的：`IngestKnowledgeBaseDocuments` 返回后文档并不立即处于
可检索状态，必须持续轮询直至每个文档达到终态（`INDEXED` 或失败态）。大规模语料的初次
发布可能需要数十分钟轮询等待，超出 Lambda 函数 15 分钟执行上限。

此外，摄入过程中可能遭遇限流，需要带指数退避的重试；门禁判定失败后须将状态落盘，不能
静默丢弃；整个发布需要完整执行历史作为审计证据。

## 决策

使用 **Step Functions Standard 状态机**作为主编排器，仅在以下三处使用 Lambda：

1. **S3 一致性校验**（门禁 A）：需要访问 S3，列出对象并校验 SHA-256。
2. **门禁判定**（门禁 B/C/D）：纯数据转换，接受状态机传入的数据结构并返回布尔判定。
3. **Registry 读写**（创建记录、状态推进、原子 Promotion）：需要访问 DynamoDB。

四个 Bedrock API 调用（`IngestKnowledgeBaseDocuments`、`DeleteKnowledgeBaseDocuments`、
`GetKnowledgeBaseDocuments`、`Retrieve`）全部通过状态机 SDK 集成直接发起，无需
Lambda 包装。轮询循环使用原生 `Wait`/`Choice` 状态，节流重试使用原生 `Retry` 配置
（`IntervalSeconds: 2`，`BackoffRate: 2`，`MaxAttempts: 6`）。

## 理由

**15 分钟上限是决定性约束。** 大规模语料的初次发布中，逐步轮询数千个文档的终态
可能需要远超 15 分钟，这在 Lambda 内无解。Step Functions Standard 状态机单次执行
最长可跑一年，轮询次数由 `maxPollAttempts` 参数控制，与运行时资源无关。

**四个 Bedrock 调用均已通过 `ValidateStateMachineDefinition` 校验。** 服务动作
`arn:aws:states:::aws-sdk:bedrockagent:ingestKnowledgeBaseDocuments`、
`deleteKnowledgeBaseDocuments`、`getKnowledgeBaseDocuments` 与
`arn:aws:states:::aws-sdk:bedrockagentruntime:retrieve` 均可作为 SDK 集成直接用于
状态机，冒烟检索步骤无需任何 Lambda。

**Fail-closed 由拓扑保证而非代码纪律保证。** 每道门禁是独立的 `Choice` 状态，失败
分支直接指向 `FailRelease`，不存在绕行路径。这比在 Lambda 函数中逐级检查返回值更可靠。

**判定逻辑以纯函数形态存在于 Lambda 中，而非嵌入状态机 JSON。** 门禁算法（删除比例、
终态聚合、冒烟检索评估）接受数据结构、返回判定结果，不调用任何 AWS SDK。这使得它们
可由 pytest 直接覆盖，无需 AWS 环境。Lambda 的角色仅是 I/O 适配器。

**Standard 类型保留完整执行历史。** 每次发布的执行记录以 90 天为默认保留期，是有
代价的审计证据，比自行写日志更可靠。Express 类型虽成本更低，但不支持长时轮询。

## 后果

- 发布操作不再受 Lambda 15 分钟上限约束，初次全量发布可正常运行。
- 限流重试、轮询超时、错误捕获统一由状态机拓扑管理，减少手工编写的容错代码。
- 三个 Lambda 函数的 IAM 权限各自最小化：S3 校验函数仅读 canonical 桶；门禁判定
  函数无任何 AWS 权限；Registry 函数仅读写 DynamoDB 表。
- Step Functions Standard 有按状态迁移次数计费，长时间轮询会产生可观费用；本参考
  实现未对此做成本优化。
- 端到端集成测试依赖真实 AWS 账户，pytest 单元测试与 Jest 基础设施测试均无需连接 AWS。

## 备选方案

| 方案 | 落选理由 |
| --- | --- |
| 单个 Python Orchestrator（长驻 Lambda 或 ECS Task） | 受 Lambda 15 分钟上限约束；重试逻辑与幂等机制须自行实现，测试复杂度与状态机相当但不带原生审计历史 |
| Step Functions + 每步一个独立 Lambda | Lambda 数量与 IAM 策略条目翻倍；真正需要测试的判定逻辑本就在 Lambda 中，拆分不带来可测试性收益，只增加 IAM 调试成本 |
