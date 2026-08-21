# AWS Knowledge Base / RAG 最佳实践与运营治理指南

研究日期：2026-08-04

## 1. 报告范围

本报告汇总 AWS 官方产品文档、AWS Well-Architected Lens、AWS Prescriptive
Guidance 和 AWS 官方博客中关于 Knowledge Base 与 Retrieval-Augmented
Generation（RAG）的实践，并结合本仓库对 Amazon Bedrock AgentCore Managed
Knowledge Base 的实测结果，说明质量、治理、更新、性能和运营各环节的实施要求。

文中使用三类结论：

- **AWS 官方能力**：来自产品文档或 API 文档，描述当前产品边界。
- **AWS 官方建议**：来自 Well-Architected、Prescriptive Guidance 或官方博客。
- **本项目建议/实测**：基于本仓库测试形成的工程判断，不代表 AWS 服务承诺。

产品能力会变化。上线前应再次核对目标区域的 API、Service Quotas、模型可用性和
定价，并用目标账户做小规模验证。

## 2. 执行摘要

1. **RAG 质量首先是数据工程问题。** 优化顺序应为：源内容、解析、分块、
   Metadata、Embedding、索引、检索、Rerank，最后才是 Prompt 和生成模型。
   查询阶段不能恢复摄入阶段已经丢失的表格结构、标题关系或字符。
2. **检索和生成必须分开评测。** 先证明正确证据能被召回，再评估答案是否忠实、
   完整并正确引用。只看最终答案会掩盖检索缺陷，也会把模型先验知识误判为 KB
   能力。
3. **每类语料应有独立的摄入策略。** FAQ、技术手册、表格型 PDF、法规和工单
   不应共享一个未经验证的 Fixed Size 配置。复杂 PDF 应优先使用高级解析；若
   Managed KB 的 `SMART_PARSING` 产生乱码，应预抽取为 UTF-8 Markdown/HTML。
4. **Metadata 是质量和安全的共同控制面。** 文档版本、有效期、分类、Owner、
   租户和审批状态既用于降低检索噪声，也用于实现可信的查询时授权过滤。
5. **更新应被视为一次发布。** 增量同步不等于安全发布。每次内容、Metadata、
   Parser、Chunking、Embedding 或 Reranker 变更都应经过质量门禁、Golden Set
   回归、权限测试和可回滚的 Promote。
6. **Managed KB 降低基础设施运维，但不消除 RAGOps。** Vector Store、
   Embedding、Parser 和 Reranker 可由服务托管；数据质量、权限身份、评测集、
   更新策略、业务 SLO 和成本仍由客户负责。
7. **Agentic Retrieval 适合多意图和探索性问题，不是每次都更好。**
   `maxAgentIteration` 是上限而非保证执行次数。应以任务成功率、迭代数、延迟
   和成本共同判断是否启用。
8. **原生 Managed KB 指标不足以单独管理性能。** CloudWatch 提供调用量、
   错误、Throttle、原始数据量和 Agentic 迭代数，但不直接提供完整检索延迟
   指标；应用侧必须记录 P50/P95/P99，并结合 X-Ray 和 Streaming Trace 分析。

## 3. 端到端参考模型

```text
Source of Truth
  -> Content QA / Classification / ACL / Metadata
  -> Parse
  -> Chunk
  -> Embed
  -> Index
  -> Metadata pre-filter
  -> Hybrid/Semantic retrieval
  -> Rerank
  -> Context assembly
  -> Generate
  -> Citation / Guardrail / Authorization enforcement
  -> Evaluation / Observability / Feedback
```

质量问题应按链路从左向右定位。建议使用以下责任边界：

| 层级 | 主要责任 | 典型失败 |
| --- | --- | --- |
| Source | 内容 Owner、业务 SME | 过期、矛盾、缺少上下文 |
| Ingestion | 数据/RAG 平台团队 | 乱码、表格破坏、Chunk 越界 |
| Retrieval | RAG 平台与应用团队 | Recall 低、噪声高、越权 |
| Generation | 应用/模型团队 | 幻觉、遗漏、引用不准确 |
| Operations | 平台、安全、业务 Owner | 更新失控、无回滚、无 SLO |

## 4. Managed KB 与自定义 Bedrock Knowledge Bases 边界

选择 Managed KB 的主要收益是减少 Vector Store、Embedding、Parser、Reranker
和检索基础设施的配置与运维。需要精细控制分块、搜索类型、索引实现或外部向量库
时，应评估自定义 Knowledge Base。

| 决策项 | AgentCore Managed KB | 自定义 Bedrock Knowledge Base |
| --- | --- | --- |
| Vector Store | 服务托管 | S3 Vectors、OpenSearch、Aurora、第三方等 |
| Embedding | Managed 或 Custom，依当前 API 能力 | 显式选择模型和维度 |
| Parsing | `SMART_PARSING` | Default、BDA、Foundation Model Parser 等 |
| Chunking | 受 Managed 模式和 Embedding 类型约束 | Fixed、Semantic、Hierarchical、No Chunking 等 |
| Search | Managed Search，使用 Hybrid Search | 可用时选择 Hybrid 或 Semantic |
| Rerank | Managed 默认开启，可按请求配置 | 可选 Rerank 模型和候选策略 |
| Agentic Retrieval | 支持 | 当前仅 Managed KB 支持 |
| 基础设施运维 | 低 | 中到高 |
| 控制力 | 中 | 高 |

本项目在 2026-08-03 实测发现：使用 `embeddingModelType=MANAGED` 创建 Data
Source 时，显式提交 `chunkingConfiguration` 会被 API 拒绝，必须省略配置并
使用服务默认的 300 Tokens、20% Overlap。产品文档与 API 可能继续演进，因此
部署时应以目标区域的实际 API 验证为准。

## 5. 内容与摄入质量

### 5.1 源内容优化

AWS Prescriptive Guidance 建议先调整源内容，使其适合检索；模型无法可靠补偿
文档结构缺陷。建议：

- 使用明确、唯一、层级稳定的标题与子标题。
- 对步骤、策略和控制项使用连续编号。
- 为列表项补充主语、适用条件和上下文，避免只有孤立关键词。
- 将复杂表格重写或补充为扁平列表；保留行列含义和单位。
- 为图形、架构图和截图增加可检索的文字描述。
- 删除重复图片、页眉、页脚和无业务意义的模板文本。
- 在 FAQ 中加入贴近用户表达的完整问题或引导句。
- 为每节增加简短摘要，提高不同问法下的语义覆盖。
- 首次出现时展开缩写，明确公司内部术语和同义词。
- 将超大、多主题文档拆成有清晰标题的自包含文档。

### 5.2 Parser 选择与质量门禁

不同语料需要单独建立 Parser 基线：

| 文档类型 | 推荐起点 | 必须验证 |
| --- | --- | --- |
| 简单 TXT/Markdown/HTML | 标准解析 | 标题、列表、编码 |
| 普通文本型 PDF | Smart/高级解析 | 阅读顺序、页眉页脚、CJK |
| 表格和复杂布局 PDF | BDA 或 Foundation Model Parser | 单元格关系、标题、图片说明 |
| 扫描件 | OCR/文档自动化 | OCR 置信度、页面覆盖 |
| 已结构化记录 | 预生成 Markdown/JSONL 或 No Chunking | Schema 和字段完整性 |

建议每次摄入前执行自动质量门禁：

- 文件可打开且 MIME Type、扩展名和大小符合预期。
- UTF-8 文本不存在 Unicode replacement character。
- 文本页数和源 PDF 页数一致，空页/图片页比例在阈值内。
- 目标语言字符比例、总字符数和每页字符数无异常突降。
- 标题、表格、代码块和列表抽样正确。
- SHA-256、版本和 Source URI 已记录。
- PII/PHI、Secret 和恶意 Prompt Injection 内容已扫描。

本项目的 AWS 中文 PDF 本身质量较高，但 Apache FOP 生成的 CJK 字形映射与
Managed `SMART_PARSING` 组合后丢失大量中文字符。这是解析兼容性问题，不是
内容专业质量问题。当前可靠回退是预抽取 UTF-8 Markdown/HTML，将文本版本作为
独立 Data Source 摄入，并保留原文件 Hash 和来源链接。

### 5.3 Chunking

Chunking 应构造可独立支持回答、且保留必要上下文的证据单元。平均长度只是实现
约束之一。

- FAQ、短文章和结构均匀的内容可从 200-300 Tokens、10%-20% Overlap 开始。
- 长篇技术文档适合 Semantic Chunking。
- 章节结构稳定的手册、法规和法律文本适合 Hierarchical Chunking。
- 已由上游生成语义单元或每个对象很短时可考虑 No Chunking。
- 表格不要在行列中间切分；代码不要在函数或配置块中间切分。
- Chunk 中保留文档标题、章节路径、版本和实体标识。

Chunking Strategy 通常在 Data Source 创建后不可变。调整 Chunking 应创建
新的 Data Source 或 KB，重新摄入并与基线版本做离线比较，不应直接覆盖生产。

### 5.4 Embedding 与索引

- 选择覆盖语料语言、领域词汇和最大输入长度的 Embedding Model。
- 更换 Embedding Model、维度或归一化方式必须完整重建索引。
- 使用目标语言和业务查询评测，不以通用 Benchmark 替代。
- 将 Exact ID、错误码、SKU、版本号等词法信号保留在原始文本和 Metadata 中。
- 记录 `embedding_model`、`embedding_version`、`chunking_version` 和
  `parser_version`，确保结果可复现。

### 5.5 本白皮书的数据准备实验

本项目对游戏行业白皮书执行了 Fixed Size 基线与结构感知预分块的 A/B
实验。实验组去除目录噪声，按问题、最佳实践、实施指导和句子边界生成 479 个
独立 Markdown，并增加章节路径、页码和控制编号 Metadata。

8 个查询的实测结果显示：

- Hit Rate 均为 100%。
- Mean Marker Coverage 从 93.75% 提高到 96.88%。
- Mean Relevant Results@10 从 3.88 提高到 5.63。
- MRR 从 0.854 降到 0.768，说明首条排序并未全面改善。
- 实验组中 21.9% 的 Chunk 少于 100 字符，边界仍需合并。

因此预分块改善了证据广度和溯源，但当前不应直接替换基线。完整实验设计、
限制、逐用例结果和 `semantic-v2` 建议见
[游戏行业白皮书语义分块对照实验](SEMANTIC_CHUNKING_EXPERIMENT.md)。

## 6. Metadata、分类与授权

建议为每个文档至少维护：

| 字段 | 用途 |
| --- | --- |
| `document_id` | 稳定主键和去重 |
| `source_uri` | 溯源与人工核验 |
| `checksum` | 变更检测和完整性 |
| `owner` | 内容责任人 |
| `classification` | 数据分类和授权 |
| `tenant` / `business_unit` | 隔离边界 |
| `language` | 语言过滤 |
| `version` | 发布版本 |
| `effective_at` | 生效时间 |
| `expires_at` | 过期控制 |
| `approval_status` | Draft/Approved/Deprecated |
| `parser_version` | 解析可追溯性 |

Metadata Filter 应在向量检索前缩小候选集。典型顺序为：

1. 由可信身份确定 Tenant、Role、Classification 和 ACL。
2. 排除 Draft、Deprecated、未生效和已过期内容。
3. 按语言、产品、区域、版本或 Source 过滤。
4. 在剩余候选上执行 Managed/Hybrid Search 和 Rerank。

ACL Awareness 不是用户认证。应用必须先认证用户，并从可信 Identity Provider
构造 User Context 或 Metadata Filter，不能接受模型或客户端直接声明角色。
Email 型身份还要治理离职账号和 Email Reuse。S3 源对象权限不会自动成为运行时
检索权限；Custom Connector 必须在摄入时提供正确 ACL。

## 7. 检索优化

### 7.1 基线流程

推荐先建立可解释的 Retrieve-only 基线：

1. 选择代表性 Query，记录预期文档和证据段落。
2. 固定 Metadata Filter、`numberOfResults` 和 Rerank 配置。
3. 保存结果的文档 ID、Chunk ID、Score、Rank 和引用位置。
4. 计算 Recall@K、MRR 或 nDCG@K，并让 SME 标注 Context Relevance/Coverage。
5. 只改变一个变量后复测。

Managed KB 使用 `managedSearchConfiguration`，不要与自定义 KB 的
`vectorSearchConfiguration` 混用。关键查询时参数包括：

- `numberOfResults`：范围 1-100。Broad Query 可从 10-20 开始；精确查询可从
  3-5 开始，再通过评测确定。
- `rerankingModelType`：`MANAGED`、`CUSTOM` 或 `NONE`。
- Metadata Filter：精确、范围、列表和逻辑组合。
- Guardrail Configuration。
- User Context / ACL Context。

Managed KB 始终使用 Hybrid Search，不能改为 Semantic-only；它也不支持
`startsWith` 和 `stringContains` Metadata Filter。涉及错误码、ID、专有名词和
数字的查询通常会受益于 Hybrid Search。

### 7.2 Rerank

第一阶段检索负责 Recall，Rerank 负责 Precision。建议：

- 第一阶段取较宽 Top-K，再把较小的高质量集合送入生成。
- Reranker 的候选数量和最终上下文数量分别调优。
- 测量 Rerank 前后 Recall、nDCG、最终答案质量、Token 数和延迟。
- 不默认叠加 Hybrid、Rerank、Query Reformulation 和 Agentic Retrieval。
  质量收益可能趋平，但延迟和费用仍会增加。

### 7.3 Query Reformulation 与 Agentic Retrieval

适用场景：

- 问题包含多个子问题、比较或约束。
- 用户用业务语言提问，而文档使用另一套术语。
- 需要跨多个 Retriever 或多个 KB 聚合证据。
- 首轮证据不足，需要基于充分性判断继续检索。

Agentic Retrieval 的典型链路是 Speculative Retrieval、Planning、Retrieval
或 Full Document Expansion、Sufficiency Evaluation 和 Result。运营建议：

- 使用体积较小、响应快、指令遵循稳定的 Planner Model。
- 为每个 Retriever 编写具体且彼此可区分的 Description。
- 单 KB 可从 3 次 Iteration 上限开始，多 KB 可从 4-5 次开始，再以评测结果调整。
- 以每次请求的 Iteration、Subquery、Retrieved Chunks、Latency 和 Cost 计量。
- 为 Trace 添加 Correlation ID 并投递 CloudWatch。
- 将返回内容视为 Evidence；需要严格引用格式时，由应用自行生成答案和引用。

`maxAgentIteration` 只是上限。规划器可能在首轮就判断证据充分，从而返回
`actions=[]`。本项目实测出现了这种情况，因此不能用“配置了多轮”推导“必然发生
追加检索”。覆盖度敏感的问题应同时测试定向子查询、提高候选数和非 Agentic
基线。

## 8. 生成、引用与安全边界

Managed KB 不支持传统 `RetrieveAndGenerate`。本项目采用两种模式：

- `Retrieve` 后调用 `Converse`：完整控制 Prompt、模型、`maxTokens`、
  Temperature、Top-p、答案格式和引用。
- `AgenticRetrieveStream`：使用 Planning、迭代检索、充分性评估和流式 Trace。

生成阶段建议：

- Prompt 明确要求仅基于提供的 Evidence 回答，并允许“资料不足”。
- 为每个 Chunk 提供稳定 Citation ID，不让模型自行编造 URL。
- 对答案中的事实声明执行 Citation Coverage 和 Citation Precision 检查。
- 将 Evidence 与 Instruction 分隔，降低源文档 Prompt Injection 风险。
- 显式设置 `maxTokens`，限制输入上下文和输出长度。
- 高风险场景增加确定性规则或人工复核，不以模型自评替代审批。

Guardrails 可以约束输入和生成响应，但不会清理 API 返回的 Raw Retrieved
References。因此：

- PII/PHI 应在摄入前删除或脱敏。
- 不在应用日志中记录完整 Raw Chunk。
- 授权必须在检索前执行，不能依赖生成模型拒绝越权内容。
- 对 Raw Reference 的下载、缓存和展示实施与源系统一致的访问控制。

## 9. 质量评测框架

### 9.1 Retrieve-only

| 指标 | 回答的问题 |
| --- | --- |
| Recall@K | 正确证据是否进入前 K 个结果 |
| Precision@K | 前 K 个结果中有多少相关 |
| MRR | 首个正确结果是否足够靠前 |
| nDCG@K | 多级相关性排序是否合理 |
| Context Relevance | 返回内容与 Query 是否相关 |
| Context Coverage | 回答所需证据是否完整 |
| ACL Leakage Rate | 是否返回用户无权访问的内容 |
| Freshness Accuracy | 是否使用当前有效版本 |

### 9.2 Retrieve-and-generate

AWS Knowledge Base Evaluation 可评估或辅助评估：

- Correctness、Completeness、Helpfulness、Logical Coherence。
- Faithfulness：答案是否由 Retrieved Context 支持。
- Citation Precision 和 Citation Coverage。
- Harmfulness、Stereotyping 和 Refusal。

建议另外记录：

- No-answer Accuracy：资料不足时是否正确拒答。
- Unsupported Claim Rate：无证据事实声明比例。
- Answer Stability：同一输入多次运行的一致性。
- End-to-end Task Success：业务任务是否完成。
- Token、Latency 和单请求成本。

### 9.3 Golden Set

Golden Set 应包含：

- 由业务 SME 给出的问题和证据。
- 脱敏后的真实生产查询。
- 历史失败、低分和用户差评案例。
- 多语言、缩写、错别字和同义表达。
- 多意图、比较、跨文档问题。
- 无答案、过期版本和冲突资料。
- Prompt Injection、越权和不同权限角色。

每条用例至少维护 Query、Expected Source/Passage、Expected Answer Elements、
Allowed Identity、Freshness Constraint、Must-not-include 和 Severity。

不要只依赖 LLM-as-a-Judge。高风险用例应有人类标注校准；Judge Model、
Prompt 和版本也必须记录。

## 10. 知识生命周期与更新

### 10.1 Source of Truth

- S3、Confluence、SharePoint 等源系统是事实源，Vector Index 是派生数据。
- Direct Ingestion 不会自动把修改回写到 S3 Source。若同时使用 Direct
  Ingestion 和 Connector Sync，必须同步事实源，否则下一次 Sync 可能覆盖变更。
- 文档删除、归档和 Legal Hold 应在事实源与索引中保持一致。
- 每个内容 Owner 对准确性、生效日期、过期日期和审批状态负责。

### 10.2 增量同步

Bedrock Sync 是增量的：

- 新文件只摄入新内容。
- Content 或 Metadata 改变时重新 Parse、Chunk、Embed 和 Index。
- 删除源文件会删除对应向量，但受配置的删除保护约束。

Bedrock API 仍要求显式调用 `StartIngestionJob`。所谓自动同步通常需要
EventBridge、S3 Event、CI/CD 或定时任务触发，不应与其他产品的默认每日同步
行为混淆。

事件驱动同步应处理：

- S3 Event 至少一次投递导致的重复事件。
- 同一文档短时间连续修改的事件合并。
- Ingestion Job 并发限制和节流。
- Job 状态轮询、超时、失败重试和 Dead-letter Queue。
- Source Event、Ingestion Job 和发布版本之间的 Correlation ID。

### 10.3 更新策略

| 变更 | 推荐策略 |
| --- | --- |
| 小规模内容修订 | 原 Data Source 增量同步，执行回归 |
| 大规模内容或 Metadata 迁移 | Versioned Prefix 或新 Data Source |
| Parser/Chunking/Embedding 变化 | 新 Data Source/KB 完整重建 |
| 权限模型变化 | 新隔离边界，执行全量 ACL 测试 |
| 高风险法规/政策更新 | 蓝绿发布，人工审批后切换 |
| 紧急错误内容 | 先隔离/过滤，再修正事实源和同步 |

Managed Connector 的删除保护阈值应按业务容忍度配置。AWS 资料中的示例默认值与
本项目配置可能不同；本项目使用 50%，即单次同步删除超过 50% 的已索引文档时
阻止删除。删除保护不是备份，仍需要 S3 Versioning、变更记录和恢复演练。

## 11. 安全与治理

### 11.1 必要控制

- 使用 IAM Role，不使用长期 IAM User Access Key。
- Service Role 权限限定到精确 Bucket Prefix、模型和 KB。
- Trust Policy 使用 `aws:SourceAccount` 和 `aws:SourceArn` 防止 Confused
  Deputy；创建后收紧到精确 KB ARN。
- 对受监管数据使用 Customer-managed KMS Key。
- 启用 CloudTrail Management Events；`Retrieve` 等 Runtime 调用需单独启用
  Data Events。
- 在摄入前扫描 PII/PHI、Secret、恶意内容和数据分类。
- 按 Tenant、Classification 或 Owner 边界拆分 Data Source/KB。
- 对 Metadata Schema、ACL 和 Owner 变更实施 Code Review 和审批。

### 11.2 责任分离

| 角色 | 责任 |
| --- | --- |
| Content Owner | 内容准确性、审批、有效期 |
| Data Steward | 分类、Metadata、保留策略 |
| KB Platform Owner | 摄入、索引、SLO、成本 |
| Application Owner | 身份、Filter、Prompt、UI |
| Security | IAM、KMS、审计、威胁模型 |
| SME/Evaluator | Golden Set、人工抽样、发布签字 |

“未分类的数据无法被正确授权”。新文档在 Classification、Owner 和 Approval
Status 完成之前不应进入生产 KB。

## 12. 可观测性、性能与成本

### 12.1 Managed KB 原生观测

CloudWatch Namespace：`AWS/Bedrock/KnowledgeBases`

| 指标 | 用途 |
| --- | --- |
| `Invocations` | 请求量 |
| `ClientErrors` | 4xx/调用配置问题 |
| `ServerErrors` | 服务端失败 |
| `Throttles` | 限流 |
| `TotalIterationCount` | Agentic Retrieval 迭代数 |
| `RawDataSize` | 原始数据规模 |

`TotalIterationCount` 仅适用于 Agentic Retrieval。上述原生指标本身不提供完整
Latency 视图：

- 对 `Retrieve` 启用 X-Ray Trace，分析内部步骤延迟。
- 对 Agentic Retrieval 保存 Streaming Trace，分析 Planning、Subquery 和
  Iteration。
- 应用记录 End-to-end、Embedding、Search、Rerank、Generation 和 TTFT。
- 记录 P50/P95/P99，并按 KB、Retriever、Query Type、Tenant 和版本分维度。

摄入日志可投递到 CloudWatch Logs、S3 或 Firehose。每个文档的生命周期日志应
能关联 Crawl、Sync、Index、Chunk Statistics 和 Error Message。发布自定义
CloudWatch Metrics 的调用身份或 Service Role 需要
`cloudwatch:PutMetricData`，并应限定 Namespace。

### 12.2 SLO 建议

具体阈值必须由业务基线决定，至少定义：

- Availability：成功请求比例，区分 4xx、5xx 和 Throttle。
- Retrieval Latency：P50/P95/P99。
- End-to-end Latency 和 Streaming TTFT。
- Freshness Lag：事实源更新到生产可检索的时间。
- Ingestion Success Rate 和失败文档数。
- Retrieval Recall/Context Coverage。
- Faithfulness/Citation Coverage。
- ACL Leakage Rate：目标必须为 0。

### 12.3 性能调优顺序

1. 使用 Metadata Filter 缩小候选空间。
2. 按 Query Type 调整 `numberOfResults`。
3. 调整 Rerank 候选数和最终上下文数。
4. 删除重复和低信息密度 Chunk。
5. 限制 Prompt Token 和显式设置 Output `maxTokens`。
6. 对 Broad Query 才启用 Reformulation/Agentic Retrieval。
7. 使用 Streaming 改善用户感知；它不会降低总计算时间。
8. 对 429、5xx 和 Timeout 使用 Adaptive Retry + Jitter；不要重试
   Validation、AccessDenied 或 ResourceNotFound。

### 12.4 成本模型

每个请求至少记录：

```text
Retrieval cost
+ Rerank cost
+ Planner cost * Agent iterations
+ Generation input/output token cost
+ Logging/trace/storage cost
```

Rerank 可能减少最终 Context Token，但会新增一次模型延迟和费用。Agentic
Retrieval 的成本应按 Iteration 而不是只按最终 Token 估算。以“单位成功任务
成本”和“单位正确答案成本”衡量，比单纯比较单次 API 价格更有意义。

## 13. RAGOps 发布门禁

建议把 KB 更新实现成可审计 Pipeline：

```text
Change Request
  -> Source checksum / schema / metadata / ACL validation
  -> Parser quality gate
  -> Ingestion
  -> Ingestion completion and zero-failure check
  -> Retrieve-only Golden Set
  -> Generate-and-cite Golden Set
  -> Security and ACL regression
  -> Latency and cost comparison
  -> Human citation sample
  -> Approval
  -> Promote
  -> Monitor
  -> Roll back when thresholds fail
```

发布比较表应包含 Old/New Version：

- 文档数、Chunk 数、失败文档数和 Raw Data Size。
- Recall@K、nDCG@K、Context Coverage。
- Correctness、Faithfulness、Citation Coverage。
- ACL Leakage、无答案准确率。
- P50/P95/P99、Agent Iteration 分布。
- 平均 Token 和单位成功任务成本。

Parser、Chunking 或 Embedding 改动应使用蓝绿 Data Source/KB。通过回归后再
切换应用配置；保留旧版本到观察期结束，确保能够回滚。

## 14. 运营节奏

| 周期 | 工作 |
| --- | --- |
| 每次变更 | 校验、同步、回归、人工抽样、审批、发布 |
| 每日 | Ingestion 失败、Throttle、Client/Server Error、Freshness Lag |
| 每周 | Top 无答案、低分、差评、Agent Iteration 和高成本请求 |
| 每月 | Golden Set 更新、权限复核、过期文档、成本与容量 |
| 每季度 | Parser、Embedding、Reranker、Model 重评和恢复演练 |

用户反馈先进入候选队列，经 Content Owner/SME 去重、核验、分类和审批后，方可
写入生产语料。

## 15. 本项目差距分析

本仓库已经具备：

- 私有、Versioned、加密的 S3 Source。
- 显式 Ingestion Job 和状态轮询。
- PDF Parser 质量诊断和 UTF-8 Markdown 修复路径。
- Managed Search、Managed Rerank、Metadata Filter 和 Agentic Retrieval 测试。
- 原始证据隔离在 Git 忽略的 `artifacts/`。
- 发布前脱敏检查和中英文 README 同步检查。

建议下一阶段补齐：

| 优先级 | 差距 | 目标 |
| --- | --- | --- |
| P0 | 固定 Golden Set 和自动评分门禁 | 阻止检索质量回退 |
| P0 | ACL/Identity 正反向测试 | 证明零越权 |
| P0 | 应用侧 P50/P95/P99 与 Correlation ID | 建立性能 SLO |
| P1 | Event-driven Sync 编排 | 缩短 Freshness Lag |
| P1 | 蓝绿 Data Source/KB Promote | 支持安全更新和回滚 |
| P1 | Ingestion 文档级日志与告警 | 快速定位坏文档 |
| P2 | 成本归因与 Agent Iteration Dashboard | 管理单位任务成本 |
| P2 | 定期过期内容和 Owner 审计 | 防止知识腐化 |

### 30/60/90 天路线图

- **0-30 天**：建立 30-50 条 Golden Set；增加 Parser、Metadata、ACL 和
  No-answer 门禁；记录检索与生成阶段延迟。
- **31-60 天**：实现事件合并、显式 Ingestion、回归和人工审批的更新 Pipeline；
  引入蓝绿 Data Source/KB。
- **61-90 天**：建立 CloudWatch Dashboard、成本归因、季度模型重评和恢复演练；
  用真实反馈扩展 Golden Set。

## 16. 架构评审清单

- [ ] 是否明确 Source of Truth、Owner、分类和更新频率？
- [ ] 是否按语料类型验证 Parser 和 Chunking，而非统一默认值？
- [ ] 是否有可重现的 Parser/Chunking/Embedding/Index 版本？
- [ ] 是否在摄入前扫描 PII/PHI、Secret 和 Prompt Injection？
- [ ] 是否在检索前基于可信身份执行 Metadata/ACL Filter？
- [ ] 是否分别评测 Retrieve-only 和 Retrieve-and-generate？
- [ ] Golden Set 是否包含无答案、过期、冲突、越权和多语言案例？
- [ ] 是否显式触发并监控 Ingestion Job？
- [ ] 大变更是否使用蓝绿版本并可回滚？
- [ ] 是否记录 P50/P95/P99、Throttle、Iteration 和单位任务成本？
- [ ] 是否启用 CloudTrail Data Events，且不记录完整敏感 Raw Chunk？
- [ ] 是否有每日、每周、每月和季度的运营机制？

## 17. AWS 官方来源

### 产品与检索

1. [Build a managed knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-build-managed.html)
2. [Test and query a knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-retrieve.html)
3. [Configure managed retrieval](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-test-config.html)
4. [Test agentic retrieval](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-agentic-retrieve.html)
5. [Rerank models](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank.html)
6. [Advanced parsing](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-advanced-parsing.html)
7. [Sync a data source](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html)
8. [Managed KB observability](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-observability.html)
9. [Managed KB ACL awareness](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-acl.html)

### 质量与评测

10. [Evaluate a knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation-kb.html)
11. [Knowledge base evaluation metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-evaluation-metrics.html)
12. [Evaluate and improve Amazon Bedrock Knowledge Bases](https://aws.amazon.com/blogs/machine-learning/evaluate-and-improve-performance-of-amazon-bedrock-knowledge-bases/)
13. [Evaluating RAG applications with Knowledge Base Evaluation](https://aws.amazon.com/blogs/machine-learning/evaluating-rag-applications-with-amazon-bedrock-knowledge-base-evaluation/)
14. [Evaluate RAG reliability](https://aws.amazon.com/blogs/machine-learning/evaluate-the-reliability-of-retrieval-augmented-generation-applications-using-amazon-bedrock/)

### Managed KB 与 Agentic Retrieval

15. [Build enterprise search with Managed Knowledge Base](https://aws.amazon.com/blogs/machine-learning/build-enterprise-search-for-agents-with-amazon-bedrock-managed-knowledge-base/)
16. [Agentic retrieval for Managed Knowledge Base](https://aws.amazon.com/blogs/machine-learning/agentic-retrieval-for-amazon-bedrock-managed-knowledge-base/)

### 治理、架构与内容

17. [AWS Well-Architected Generative AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/generative-ai-lens/generative-ai-lens.html)
18. [Agentic AI Lens: evaluate agent performance](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentperf03-bp03.html)
19. [AWS Responsible AI Lens](https://docs.aws.amazon.com/wellarchitected/latest/responsible-ai-lens/responsible-ai-lens.html)
20. [Generative AI data lifecycle](https://docs.aws.amazon.com/prescriptive-guidance/latest/strategy-data-considerations-gen-ai/lifecycle.html)
21. [Writing best practices for RAG](https://docs.aws.amazon.com/prescriptive-guidance/latest/writing-best-practices-rag/best-practices.html)
22. [Generative AI security reference architecture](https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture-generative-ai/gen-ai-agents.html)
23. [Data governance in the age of generative AI](https://aws.amazon.com/blogs/big-data/data-governance-in-the-age-of-generative-ai/)
24. [Data authorization for generative AI applications](https://aws.amazon.com/blogs/security/implement-effective-data-authorization-mechanisms-to-secure-your-data-used-in-generative-ai-applications/)

### 更新与运营案例

25. [Automatic sync for Amazon Bedrock Knowledge Bases](https://aws.amazon.com/blogs/machine-learning/build-and-deploy-an-automatic-sync-solution-for-amazon-bedrock-knowledge-bases/)
26. [How Ring scales support with Amazon Bedrock Knowledge Bases](https://aws.amazon.com/blogs/machine-learning/how-ring-scales-global-customer-support-with-amazon-bedrock-knowledge-bases/)
