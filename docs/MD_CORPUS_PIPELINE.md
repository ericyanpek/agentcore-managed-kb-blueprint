# 企业 Markdown 语料的 Managed Knowledge Base 更新 Pipeline

## 1. 范围与证据等级

本文回答一个具体问题：一家企业已有大量 Markdown 文档（Wiki 导出、Docs-as-Code
仓库、Runbook 集合），要在 Amazon Bedrock AgentCore Managed Knowledge Base 上
构建 MVP，如何设计一条可持续更新的 Pipeline。

沿用本仓库的证据分级：

- **AWS 官方能力**：来自产品文档与配额页，给出链接。
- **AWS 官方建议**：来自 AWS 博客与参考架构。
- **本项目实测**：本仓库 2026-08-03 实测结论。
- **待验证假设**：影响架构选择但当前无证据，标注为 A1/A2，见第 7 节。

本文的 Pipeline 实现见 `scripts/21_prepare_md_corpus.sh`、
`scripts/22_incremental_ingest.sh` 和 `scripts/23_verify_assumptions.sh`。
截至 2026-08-04，脚本只在本地语料上验证了准备、变更检测和批次规划，
**尚未在 Managed Knowledge Base 上执行端到端摄入，删除保护、异步文档终态、
Metadata 关联和 Manifest Promotion 也尚未达到生产门禁要求**。

### 1.1 先用用户问题理解 Pipeline

| 用户问题 | 准确答案 |
| --- | --- |
| KB 的事实依据就是 S3 最新文件吗？ | S3 是 Connector 可见的发布副本；KB 检索的是最后一次成功摄入的索引快照。只更新 S3 不会自动更新检索结果。 |
| 首次有 50 个 Markdown 怎么摄入？ | 本地检查 50 个文件；初始 Manifest 将它们全部标记为 `added`；按每批 10 个拆成 5 次 Direct Ingestion。 |
| 一周后只修改 5 个 Markdown 呢？ | 本地仍扫描全部 50 个来确认增改删，但只上传并定向摄入 5 个 `modified` 文件；其余 45 个不重新摄入。 |
| 50 页 PDF 只改几页呢？ | 单个 PDF 是一个摄入文档，整份 PDF 重新解析、分块和 Embedding；不会重建 KB 中其他文档。若 KB 只有这一份 PDF，效果上等于全 Corpus 更新。 |

企业 Pipeline 至少需要区分四层状态：

```text
Git / CMS                     企业内容权威源
  -> S3 Source / Canonical    Connector 读取的候选发布副本
  -> Published Manifest       已批准的 Release 契约
  -> Managed KB Index         最后一次成功摄入的派生索引
```

目标一致性条件是：

```text
S3 desired version == published manifest version == KB indexed version
```

在异步摄入期间三者可以短暂不一致，因此 `S3 latest` 不能单独证明新内容已经可检索。
每次发布必须记录文档终态和检索证据。

### 1.2 50 个 Markdown 的两次发布

首次发布：

```text
扫描并检查 50 个文件
  -> previous manifest 不存在
  -> added=50, modified=0, deleted=0
  -> 上传 50 个 Markdown + 50 个 Sidecar
  -> 5 个 Direct Ingestion batch（每批 10 个）
  -> 文档终态 + Golden Set + ACL 门禁
  -> Promotion Release v1
```

一周后只修改其中 5 个：

```text
重新扫描并检查全部 50 个文件
  -> 与 Release v1 Manifest 比较 content/metadata SHA-256
  -> added=0, modified=5, deleted=0, unchanged=45
  -> 只上传 5 个 Markdown + 5 个 Sidecar
  -> 1 个 Direct Ingestion batch
  -> 受影响查询 + 全局 Smoke + ACL 门禁
  -> Promotion Release v2
```

这里的“增量”是**云端写入和摄入增量**，不是跳过本地 Corpus 检查。本地重新扫描
全部 50 个文件，才能可靠发现删除、重复 `document_id`、Metadata-only 更新和
Corpus 级约束变化。

## 2. Markdown 语料是比 PDF 更好的起点

本仓库对同一份 146 页中文 PDF 做过模态对照。`SMART_PARSING` 摄入后，非图片
分块大量丢失 CJK 字符，Sidecar 声明 `language=zh-CN` 而系统字段
`_language_code` 全部回落为 `en`。预抽取为 UTF-8 Markdown 并通过独立数据源
摄入后，四组定向检索的 Top score 从 0.456-0.570 升到 0.619-0.735，
Unicode replacement character 归零。详见
[RESULTS.md](RESULTS.md) 第 6 节。

企业 Markdown 语料不经过 PDF 文本层与版面重建，因此不受本仓库已观测到的
PDF 解析问题影响。Markdown MVP 仍需验证解析结果，但主要工程工作集中在更新
编排、治理与发布门禁。

需要保留的一项检查是编码与结构门禁。Markdown 由人编写，会出现空文档、
损坏字符、重复标识和超限文件。`scripts/21_prepare_md_corpus.py` 对每份文档
强制以下门禁，任一失败即以非零码退出：

| 门禁 | 阈值 | 依据 |
| --- | --- | --- |
| 非空正文 | 去除 Front Matter 后必须有内容 | 空文档会产生无意义向量 |
| 无 `U+FFFD` | 0 个 | 与本仓库 PDF 修复的抽取门禁一致 |
| 单文档抽取文本 | ≤ 30 MB | Managed KB 配额 |
| Sidecar 大小 | ≤ 10 KB | 与既有 Metadata 实验一致 |
| `document_id` 唯一 | 全语料唯一 | 重复标识会相互覆盖 |

## 3. 配额前提：Managed KB 与 Classic KB 不同

搜索「Bedrock Knowledge Base 自动同步」会命中 AWS 2026-04-27 的
[Build and deploy an automatic sync solution](https://aws.amazon.com/blogs/machine-learning/build-and-deploy-an-automatic-sync-solution-for-amazon-bedrock-knowledge-bases/)。
该方案是 S3 → EventBridge → Lambda → DynamoDB 变更台账 + SQS（`BatchSize: 1`）
→ Step Functions → `StartIngestionJob`，其中 Step Functions 通过
`list_ingestion_jobs` 判断 `STARTING`/`IN_PROGRESS` 数量是否为 0，非 0 则
`Wait 300s` 后重试，DLQ 设 `maxReceiveCount: 5`。

**该方案的串行化设计建立在 Classic Knowledge Base 的配额上。** Managed
Knowledge Base 于 2026 年 6 月 GA，配额显著不同：

| 配额项 | Classic KB | Managed KB |
| --- | ---: | ---: |
| 并发 Ingestion Job / KB | 1 | **50** |
| 并发 Ingestion Job / 账户 | 5 | 未单列 |
| 并发 Ingestion Job / Data Source | 1 | 未单列 |
| Data Source / KB | 5 | **200** |
| KB / 账户 / 区域 | — | 10,000 |
| `IngestKnowledgeBaseDocuments` 文件数 / 请求 | 25 | 10 |
| `IngestKnowledgeBaseDocuments` 速率 | 5 rps | **20 rps** |
| `StartIngestionJob` 速率 | **0.1 rps（不可调）** | 未单列 |
| 单文件抽取文本上限 | 50 MB | 30 MB |
| 存储上限 | 100 GB / Job | **10 TB / KB** |
| `Retrieve` 速率 | — | 600 / 分钟 / KB |
| `AgenticRetrieveStream` 速率 | — | 60 / 分钟 / 账户 |

来源：[Managed KB 服务配额](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-quotas.html)
与 [Amazon Bedrock 服务配额](https://docs.aws.amazon.com/general/latest/gr/bedrock.html)，
均为 Not adjustable 项。

Managed KB 将每个 KB 的并发 Job 配额从 1 提高到 50。若直接沿用上述参考架构中
「等待活跃 Job 归零」的门闩，可并行摄入会被串行化，Freshness Lag 可能从秒级
扩大到分钟级，且不产生配额收益。

反过来，`StartIngestionJob` 的 0.1 rps 在通用配额页**只列出 Classic 版本，
没有 Managed 版本**。本文不据此断言 Managed KB 同样受 0.1 rps 约束，该项
记为待验证假设 A1。

## 4. 两条摄入通道及其分工

企业 Markdown 语料的更新不应只有 `StartIngestionJob` 一条路径。Managed KB
提供定向变更与全量对账两类通道：

| 维度 | 定向通道 | 对账通道 |
| --- | --- | --- |
| API | `IngestKnowledgeBaseDocuments` / `DeleteKnowledgeBaseDocuments` | `StartIngestionJob` |
| 粒度 | 已知文档集合 | 整个 Data Source |
| 速率 | 20 rps | 未单列（假设 A1） |
| 能否删除 | **是，使用 Delete API** | 是 |
| 适用 | 已知哪些文件增改删 | 周期性对账、修正漂移 |

官方 [Direct ingestion 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-direct-ingestion-add.html)
给出两条硬约束，决定了通道分工不是优化而是正确性要求：

1. S3 类型 Data Source **只接受 S3 位置**，不接受 inline 内容。inline 仅对
   Custom 类型 Data Source 可用。
2. 直接摄入到 S3 型 Data Source 的文档**不会被写回 S3 桶**。官方原文建议同时
   把这些文档加入 S3 数据源，否则在后续 Sync 时可能被移除或覆盖。

因此正确顺序是**先写 S3，再定向摄入**，而不是用定向摄入替代 S3 写入：

```text
Git / CMS（事实源）
  -> CI 渲染与规范化（21_prepare_md_corpus）
  -> 门禁：编码、空文档、大小、document_id 唯一性
  -> 变更检测：与已发布 manifest 比对 SHA-256，得出 added/modified/deleted
  -> 上传变更对象与 .metadata.json sidecar 到 S3
  -> 定向摄入 added + modified（IngestKnowledgeBaseDocuments）
  -> 定向删除 deleted（DeleteKnowledgeBaseDocuments）
  -> 周期性对账 Sync（StartIngestionJob）
  -> 检索回归与 ACL 回归
  -> 提升 manifest 为已发布基线
```

`scripts/22_incremental_ingest.py` 输出该计划，`scripts/22_incremental_ingest.sh`
执行；设 `DRY_RUN=1` 可在不调用任何变更 API 的前提下产出计划。**当前脚本尚未
实现上述目标状态**：它仍用 Sync 处理删除，不轮询 Direct API 文档终态，也没有
在 Direct Ingestion Payload 中显式关联 Metadata Sidecar。

删除保护是这条链上容易被误解的一环。目标设计是在删除比例超过 50% 时 Fail
Closed，并要求人工审批。当前规划器的分母使用删除后的文档数，全量删除时可能
错误计算为 0；执行脚本也不会根据 Guardrail 停止删除。因此当前实现的删除保护
只是提示，不是控制。修复并完成负向测试前，不得用于生产删除。删除保护也不是
备份，仍需 S3 Versioning 与恢复演练。

## 5. 变更检测靠 manifest 比对，不靠 S3 事件

`21_prepare_md_corpus.py` 为每份文档记录内容与 Sidecar 的 SHA-256，写入
`manifest.json`；下一次运行与上一次**已发布**的 manifest 比对，输出
added/modified/deleted 三态。

选择 manifest 比对而非 S3 事件的理由：

- S3 事件是至少一次投递，需要额外的去重与事件合并逻辑；manifest 比对是幂等的
  状态比较，重复运行得到相同结果。
- 「已发布 manifest」与「当前 manifest」的差集天然给出删除集合。S3 事件流在
  丢失事件时无法自我修复。
- 发布失败时不提升基线，下一次运行会重新计算同一批变更，无需补偿事务。

事实源是 Git 或 CMS，Sidecar 是治理字段的权威副本，向量索引是派生数据。
任何只改索引不改事实源的操作都会在下次对账时被回滚，这是设计意图。

### 5.1 Source Diff、Release Diff 与 Retrieval Diff

企业知识库至少要管理三种不同的 Diff：

| Diff | 回答的问题 | 实现 |
| --- | --- | --- |
| Source Diff | 作者修改了什么？ | Markdown 使用 Git Diff；PDF 使用对象 SHA-256/S3 Version ID |
| Release Diff | 哪些文档应新增、修改或删除？ | 比较前后 Manifest 的内容与 Metadata SHA-256 |
| Retrieval Diff | 新版本改变了哪些召回结果？ | 固定 Golden Set，比较 Hit Rate、Recall、MRR/nDCG、Citation 和 Latency |

Managed KB 不暴露客户可直接比较的底层向量索引，因此 Source/Manifest Diff
不能替代 Retrieval Diff。向量、分块和排序的行为变化必须通过检索回归观察。

### 5.2 Release Manifest 的生产字段

当前脚本生成的 Manifest 已包含 `documentId`、路径、内容/Metadata SHA-256 和
Corpus SHA-256。生产版本还应增加：

```json
{
  "releaseId": "knowledge-domain-2026-08-10.1",
  "parentReleaseId": "knowledge-domain-2026-08-03.1",
  "sourceCommit": "<git-commit>",
  "pipelineVersion": "data-preparation-v2",
  "querySetVersion": "golden-set-v3",
  "documents": [
    {
      "documentId": "fraud-detection",
      "s3Key": "canonical/fraud-detection.md",
      "s3VersionId": "<version-id>",
      "contentSha256": "<sha256>",
      "metadataSha256": "<sha256>"
    }
  ],
  "status": "CANDIDATE"
}
```

建议状态机：

```text
DRAFT -> PREPARED -> INGESTING -> INGESTED
      -> QUALITY_PASSED -> PUBLISHED
```

`PUBLISHED` Pointer 可存入版本化 S3；存在并发发布时，使用 DynamoDB Conditional
Write 或等价的 Compare-and-Swap 防止两个 Pipeline 相互覆盖。当前脚本把
Published Manifest 放在被 Git 忽略的 `artifacts/<RUN_ID>/published/`，不能跨
临时 CI Runner 稳定保存，也不是原子发布存储。

### 5.3 S3 Versioning、PDF Diff 与回滚

S3 Bucket 应启用 Versioning；高风险语料再评估 Object Lock。更新同一个 Key
会生成新的 Version ID，但 Connector 通常消费该 Key 的当前版本。回滚旧内容时：

1. 找到 Manifest 记录的旧 S3 Version ID。
2. 将旧版本复制为同一 Key 的新 Current Version，或切回旧 Release Prefix。
3. 重新摄入并等待文档终态。
4. 执行 Smoke、stale-content 和 ACL 回归。
5. 将 Current Release Pointer 切回旧 Manifest。

PDF 的二进制 Diff 只能说明整个文件发生变化，不能说明业务语义改了哪里。需要
可读 Diff 和细粒度更新时，应同时保留不可变原始 PDF，并预抽取为稳定章节：

```text
original/manual.pdf
canonical/manual/anti-cheat.md
canonical/manual/fraud-detection.md
canonical/manual/player-analytics.md
```

优先按章节、控制项或业务主题拆分，不建议直接按页拆分。插页和跨页段落会让页码
整体漂移，导致大量无业务意义的变更。

## 6. Metadata 策略依据本仓库实测结果

本仓库以 479 份字节相同的语料、44 条查询、408 次 `Retrieve` 做过三组对照
（无 Sidecar、全部 `includeForEmbedding=false`、语义字段
`includeForEmbedding=true`）。结论见
[METADATA_EXPERIMENT.md](METADATA_EXPERIMENT.md)：

- 让 Metadata 参与 Embedding，在未过滤召回下**未测得增益**。
- 真正的增益来自 Runtime Filter：36 条可过滤查询的 MRR 从 0.241 升到 0.556，
  Recall@10 从 0.078 升到 0.342；按稳定控制编号定位章节时 MRR 达到 1.000。

因此 `21_prepare_md_corpus.py` 的默认策略是：

- 治理与授权字段（`document_id`、`classification`、`owner`、`lifecycle_status`、
  `content_sha256`、`source_path`）一律 `includeForEmbedding=false`。
- 仅 `title`、`section_path`、`domain`、`topic` 参与 Embedding，可通过
  `--embedded-fields` 调整。
- 目录层级自动映射为 `domain`/`topic`/`section_path`，让运行时过滤有稳定业务键
  可用，这是上述 MRR 增益的前提。

两条来自实测的安全边界必须在应用层落实：Metadata Filter 只缩小语义候选集，
**不保证返回结果**；权限 Filter 命中空集时必须 Fail Closed，确定性文档读取
应转向 S3 或内容系统而非检索层。S3 源对象权限不会自动成为检索层权限，任何
持有 `bedrock:Retrieve` 的调用者都可能看到已摄入内容。

## 7. 待验证假设

以下两条影响架构选择，当前无证据。现有 `scripts/23_verify_assumptions.sh`
保留为实验草稿，但其 A1 混淆同 Data Source 并发限制与 API Rate，A2 使用
`CUSTOM` Payload 测试 S3 Data Source，并把 Managed KB 查询错误配置为
`vectorSearchConfiguration`。重写前不能用其结果支持架构决策。

### A1 `StartIngestionJob` 对 Managed KB 是否强制 0.1 rps

通用配额页列出 Classic 的 0.1 rps 且不可调，未发布 Managed 等价项，同时把
并发 Job/KB 从 1 提到 50。正确方法是使用多个 Disposable Data Source 控制
Job-in-progress 变量，再记录提交间隔与 `ThrottlingException`；不能只对同一个
Data Source 连续提交 Job。

- 若**全部被接受**且间隔远低于 10 秒，则 A1 被推翻，参考架构中的串行门闩在
  Managed KB 上不必要，规划器可以去掉限流间隔。
- 若在约每 10 秒一次处被限流，则 A1 成立，需保留限流器。

当前规划器取 `--throttle-interval-seconds 10`，即**按悲观假设配置**。

### A2 对账 Sync 是否移除仅存在于索引的定向摄入文档

官方警告的表述覆盖的是「文档同时存在于 S3」的情形，未明确说明「文档在桶中但
不在 Connector Inclusion Prefix」时的行为。正确方法是在同一个 S3 Bucket
创建位于 Inclusion Prefix 外的探针对象，使用 S3 URI 定向摄入并轮询最终状态；
确认可检索后运行 Data Source Sync，再用 `managedSearchConfiguration` 检索。

- 若同步后检索不到，A2 成立，「先写 S3 再定向摄入」是强制要求，且对账不能在
  前缀尚未写全时运行。
- 若探针存活，A2 被推翻，两条通道的耦合可以放松。

当前规划器默认 `--assume-sync-removes-direct-documents`，即**按悲观假设执行**，
并在计划中输出对应 Guardrail。

## 8. 多项目场景：多个 KB 还是一个 KB 多个 Data Source

企业通常不止一个项目。Managed KB 的配额把这个问题的成本结构改变了：单 KB 可挂
**200 个 Data Source**，账户可建 **10,000 个 KB**，两侧都不是瓶颈，因此选择
依据是隔离语义而非容量。

本仓库的相关实测资产是：同一个 Managed KB 内并存 4 组以上 Data Source（原始
PDF、`text-v1` 修复版、语义预分块 Canary、Metadata 三组 Variant），检索侧通过
服务自动生成的 `_data_source_id` 以及自定义 `document_id` 过滤实现隔离，且
Metadata 实验证实了 Runtime Filter 的定位增益。**但本仓库从未测试过多个独立
KB 的对比**，因此下表中「多 KB」一侧的结论属于依据官方能力的推演，不是实测。

| 决策因素 | 倾向单 KB 多 Data Source | 倾向多个 KB |
| --- | --- | --- |
| 隔离强度 | 应用层 Filter 可信即可 | 需要 IAM 资源级硬隔离 |
| 跨项目检索 | 需要一次查询覆盖多域 | 各项目独立检索 |
| 权限边界 | 同一批调用者 | 不同团队/租户/合规域 |
| Embedding 与分块 | 全局一致 | 各项目需不同配置 |
| 成本与配额归属 | 无需分账 | 需按 KB 归集 |
| 删除与保留策略 | 一致 | 各自的保留期与 Legal Hold |

推荐的判定顺序：

1. **合规或租户边界要求资源级隔离** → 拆 KB。Metadata Filter 是应用层控制，
   任何持有 `bedrock:Retrieve` 的调用者都绕得过应用；只有 IAM 到 KB ARN 才是
   资源级边界。
2. **Embedding 类型或分块策略需要不同** → 必须拆。Managed/Custom Embedding
   创建后不可改，分块策略在 Data Source 创建后不可变。
3. **需要跨项目联合检索** → 倾向单 KB，用 `domain`/`topic` 过滤；
   `AgenticRetrieveStream` 也支持跨多个 Retriever 聚合，但这会引入第 9 节的
   覆盖度风险。
4. **其余情况** → 单 KB 多 Data Source，按项目一个 Data Source 加独立 S3
   前缀。这样蓝绿发布很便宜：新建 Data Source、摄入、回归、切换应用配置、
   保留旧版本至观察期结束。

Data Source 是比 KB 更合适的默认切分单位，因为 Parser、Chunking、删除保护和
前缀都在 Data Source 层确定，而 200 的配额让「每项目一个甚至每版本一个」在
成本上可行。

## 9. 检索侧的两个已知风险

这两条来自本仓库实测，会影响 MVP 的验收口径。

**Agentic Retrieval 的 `actions=[]`。** 三次修复后回归中，Speculative
Retrieval 与 Planning 均 `SUCCEEDED`，但 planning 返回 `actions=[]`，未触发
第二轮检索。`maxAgentIteration` 是上限而非保证。后果是宽泛问题可能漏掉跨章节
内容：本仓库中 Lookout for Metrics 与 SageMaker AI 段落经定向 `Retrieve` 可以
命中，但 Broad Query 的 Top 10 未召回。覆盖度敏感的评估应使用定向子查询或提高
结果数量，不能只用一次宽泛提问判定知识库覆盖范围。

**Grounded failure 不等于文档没有内容。** 本仓库首轮反作弊测试得到一个措辞
合理的「文档不支持」回答，后续 PDF 诊断却证明源文档确实包含玩家行为监控与
会话票证验证内容，根因是 Smart Parsing 破坏了中文分块。因此「模型说知识库里
没有」只能作为索引状态的证据，不能作为语料覆盖范围的结论。

## 10. 发布门禁与运营

沿用 [AWS_KB_RAG_BEST_PRACTICES.md](AWS_KB_RAG_BEST_PRACTICES.md) 第 13 节的
RAGOps 门禁，对 Markdown 语料具体化为：

1. 事实源变更评审（Git PR 即天然的变更请求与审批留痕）。
2. 准备门禁：编码、空文档、大小、`document_id` 唯一性（脚本 21 强制）。
3. 变更检测：与已发布 manifest 比对，人工确认删除集合规模。
4. 摄入：定向通道处理增改，对账通道处理删除。
5. 零失败校验：检查 Ingestion Job 统计的 failed 与 skipped。
6. 检索回归：Golden Set 的 Retrieve-only 指标与带 Filter 的定位查询。
7. ACL 回归：目标泄漏率为 0。
8. 人工抽样核对引用。
9. 提升 manifest 为已发布基线。失败则不提升，下次运行自动重算同批变更。

不同变更的回归范围不应完全相同：

| 变更 | 每次发布必须执行 |
| --- | --- |
| 修改 5 个 Markdown | 全量确定性准备检查；受影响 Golden Queries；全局关键 Smoke；ACL/No-answer |
| 只修改 Metadata/ACL | Filter 正负测试、跨租户泄漏、Metadata 类型与缺失字段测试 |
| 删除文档 | stale-content 排除、引用失效、删除与权限回归 |
| 更新一个完整 PDF | 该 PDF 全部相关 Golden Queries；全局 Smoke；解析和 Citation 抽样 |
| Parser/Chunking/Embedding/Schema 变化 | 完整 Golden Set、Latency/Cost、蓝绿 Data Source 和回滚演练 |

当前 `scripts/22_incremental_ingest.sh` 尚未自动运行上述业务回归，也只记录 Direct
API `ACCEPTED`，随后即复制 Published Manifest。生产实现必须把文档最终状态、
Ingestion failed/skipped、检索质量和 ACL 门禁放在 Manifest Promotion 之前。

运营节奏与陈旧检测：`lifecycle_status` 与 `expires_on` 作为可过滤字段写入
Sidecar，使「过期文档」成为一次 Metadata Filter 查询而非全量扫描。`Retrieve`
与 `AgenticRetrieveStream` 属 Runtime 调用，需单独启用 CloudTrail Data
Events；KB 与摄入的管理操作默认进入 Management Events。

## 11. 复现

```bash
cp config/test.env.example config/test.env
# 设置 MD_CORPUS_SOURCE_DIR、MD_CORPUS_ID、MD_S3_PREFIX、MD_DATA_SOURCE_ID

PYTHON_BIN=python3.12 ./scripts/21_prepare_md_corpus.sh
DRY_RUN=1 PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
```

验证第 7 节的两条假设（需要控制面权限，且应指向一个可丢弃的 Data Source）：

```bash
PROBE_DATA_SOURCE_ID=<disposable-data-source-id> ./scripts/23_verify_assumptions.sh
```

证据写入被 Git 忽略的 `artifacts/<RUN_ID>/`：
`tests/md-corpus-preparation-report.json`、`tests/md-ingestion-plan.json`、
`tests/md-ingestion-result.json`、`tests/assumption-verification.json`，
已发布基线为 `published/md-corpus-manifest.json`。
