# AgentCore Managed Knowledge Base 实测报告

## 1. 范围与结果

- 测试时间：2026-08-03
- AWS 账户：`<AWS_ACCOUNT_ID>`（公开报告已脱敏）
区域：`us-east-1`

本次测试创建并保留以下资源：

| 资源 | 标识 | 状态 |
| --- | --- | --- |
| Managed Knowledge Base | `<KNOWLEDGE_BASE_ID>` | `ACTIVE` |
| Managed S3 data source | `<PDF_DATA_SOURCE_ID>` | `AVAILABLE` |
| S3 bucket | `<SOURCE_BUCKET>` | Active |
| IAM service role | `AmazonBedrockExecutionRoleForManagedKB-GamesLens` | Active |

源文件是 146 页 PDF，SHA-256：

```text
cf789f73f933008a32594d572eea196b66f288aa91755e03edd13df2a67d8c8f
```

初次摄入结果：

| 指标 | 数量 |
| --- | ---: |
| 扫描 PDF | 1 |
| 扫描 metadata sidecar | 1 |
| 新增索引文档 | 1 |
| 修改 | 0 |
| 删除 | 0 |
| 失败 | 0 |
| 跳过 | 0 |

## 2. 实际存储

客户可见的源数据是两个普通 S3 对象：

```text
s3://<SOURCE_BUCKET>/
  documents/games-industry-lens/2026-07-31/
    games-industry-lens.pdf
    games-industry-lens.pdf.metadata.json
```

S3 已启用：

- Block Public Access 全部四项。
- SSE-S3 默认加密。
- Versioning。
- 30 天清理非当前版本。
- 7 天终止未完成 multipart upload。

Managed KB 的 embedding、向量索引、托管 reranker 和检索基础设施由
AWS 管理，不会暴露为客户自己的 S3 Vectors bucket 或 OpenSearch
collection。这是它与账户中已有 `VECTOR + S3_VECTORS` KB 的核心差异。

检索结果中的 HTTPS S3 URI 是来源标识，不代表对象已公开；桶仍然是私有的。

## 3. 实际配置

### Knowledge Base

```json
{
  "type": "MANAGED",
  "managedKnowledgeBaseConfiguration": {
    "embeddingModelType": "MANAGED"
  }
}
```

使用托管 embedding 的优势是无需选择模型、维度、容量或向量存储，也可使用
托管 reranker。代价是 embedding 类型创建后不可修改，底层索引不可直接管理。

### Data Source

- 类型：`MANAGED_KNOWLEDGE_BASE_CONNECTOR`
- 连接器：Amazon S3
- Inclusion prefix：`documents/games-industry-lens/2026-07-31/`
- Parsing：`SMART_PARSING`
- PDF 图片提取：启用
- 删除保护：一次同步删除超过 50% 文档时跳过删除阶段
- Data deletion policy：`DELETE`

当前服务端不允许在 `embeddingModelType=MANAGED` 时显式提交
`chunkingConfiguration`。实测错误：

```text
A chunking strategy cannot be specified with a managed embedding model.
Omit chunkingConfiguration to use the default.
```

因此本次使用服务默认 fixed-size chunking：300 tokens、20% overlap。需要显式
分块参数时，应创建使用 custom embedding 的新 KB/数据源。

## 4. 摄入 Pipeline

不需要显式创建 Lambda、Step Functions、Glue 或独立 ETL pipeline。需要显式
控制的是四个边界：

1. 上传或替换 S3 源文件与 sidecar metadata。
2. 创建数据源时确定 parser、media extraction、删除保护和前缀。
3. 调用 `StartIngestionJob`。
4. 轮询 `GetIngestionJob`，只在 `COMPLETE` 后开放检索。

后续同步是增量的：连接器检测新增、修改和删除内容。同步本身仍需显式调用
`StartIngestionJob`，除非再用 EventBridge Scheduler、CI/CD 或工作流系统触发。

## 5. 检索测试

### Managed Retrieve

三组中文问题均返回 10 个经过托管 reranker 排序的结果：

| 问题 | Top score | 第 10 名 score |
| --- | ---: | ---: |
| 区域故障与玩家会话恢复 | 0.554 | 0.329 |
| 发布日流量尖峰与成本 | 0.570 | 0.293 |
| 数据保护、反作弊与事件响应 | 0.456 | 0.342 |

`document_id=aws-games-industry-lens-2026-07-31` 的 metadata filter 成功，
返回的 10 个结果全部属于目标文档。

### Retrieve 后调用 Converse

经典 `RetrieveAndGenerate` API 不支持 Managed KB。实测错误：

```text
This operation is not supported for managed knowledge bases.
```

可控基线采用 `Retrieve -> Converse`：

- 取前 5 个分块。
- 每个分块带引用编号、score、来源 URI。
- Nova Lite，`maxTokens=1200`、temperature 0.1、top-p 0.9。
- 三次调用输入约 1,783-1,945 tokens，输出约 523-613 tokens。

这种方式适合需要自定义 prompt、引用格式和输出策略的应用。

### Agentic Retrieval

通过包含最新 API 的 AWS MCP SDK 成功执行 `AgenticRetrieveStream`：

- Managed planner。
- Managed reranker。
- `maxAgentIteration=3`。
- `maxNumberOfResults=10`。
- `document_id` filter。
- 生成回答并返回原生 citation spans。
- 共收到 161 个流事件。
- Speculative Retrieval 与 Planning 均为 `SUCCEEDED`。

本机 boto3 `1.42.94` 尚未包含此操作，AWS CLI 也未暴露该流式命令。生产代码
应固定到包含 `agentic_retrieve_stream` 的较新 SDK 版本，而不是依赖当前系统 SDK。

#### 反作弊覆盖度测试

使用隔离环境中的 boto3 `1.43.62` 对同一 KB 发起了真实
`AgenticRetrieveStream` 请求：

```bash
.venv-agentic/bin/python scripts/08_agentic_retrieval.py \
  --knowledge-base-id '<KNOWLEDGE_BASE_ID>' \
  --query '针对游戏内反作弊，有哪些策略和最佳实践？请区分预防、检测、响应和持续改进，并只使用知识库内容。' \
  --max-results 10 \
  --max-iterations 3 \
  --output artifacts/20260803/tests/agentic-anti-cheat-events.ndjson
```

本次收到 57 个流事件：4 个 trace、52 个 response、1 个最终 result。
Speculative Retrieval 和 Planning 均为 `SUCCEEDED`，最终 planning
`actions=[]`。这说明请求使用了 agentic API 和服务端规划，但该问题没有触发额外
检索动作或多轮查询分解，不能把本次结果描述为实际执行了多轮检索。

最终返回 10 个分块、0 个 citation span。回答明确指出文档只支持 DDoS、
AWS WAF、区域后端和性能等基础设施内容，不足以回答客户端完整性、行为检测、
封禁与回滚、作弊模式迭代等游戏内反作弊实践。这是合理的 grounded failure：
服务没有把常识性反作弊建议伪装成知识库事实。

后续 PDF 诊断发现，源文档实际包含玩家行为监控、服务器端会话票证验证和绕过
配对系统案例。因此这里的 grounded failure 只说明当前索引未提供证据，不能用于
判定源文档没有相关内容；根因是 Smart Parsing 破坏了中文非图片分块。

原始事件见
`artifacts/20260803/tests/agentic-anti-cheat-events.ndjson`，摘要见
`artifacts/20260803/tests/agentic-anti-cheat-summary.json`。

#### 欺诈与检测覆盖度测试

第二次测试将问题收窄到账号/身份欺诈、支付与虚拟经济欺诈、异常行为检测、
监控告警和事件响应。调用仍使用 `maxAgentIteration=3`、
`maxNumberOfResults=10` 和同一 `document_id` filter。

本次收到 245 个流事件：4 个 trace、240 个 response、1 个最终 result；
返回 10 个分块和 16 个 citation spans。Speculative Retrieval 与 Planning
均成功，但 planning 仍为 `actions=[]`，没有执行追加检索。

文档直接支持的内容包括：

- MFA、JWT、Amazon Cognito 和 API Gateway 身份访问控制。
- GuardDuty、CloudWatch、OpenSearch 和第三方日志监控工具。
- Kinesis Data Streams、Apache Flink、S3、Glue、Athena 分析组件。
- Shield Advanced、AWS WAF 和 Global Accelerator 基础设施防护。
- Player Wallet、Marketplace 和 Inventory 等业务组件。

引用不等同于业务结论完全成立。把 Comprehend 描述为异常检测手段、把 Gaming
Analytics Pipeline 描述为欺诈识别管道，以及把 DDoS 防护归入游戏欺诈检测，
都属于生成模型基于组件用途做的推断，并非当前分块中明确给出的欺诈控制。

知识库仍未覆盖账号接管/多账号关联规则、支付和拒付欺诈、虚拟经济交易监控、
风险评分与阈值、欺诈事件升级和自动化处置。原始事件见
`artifacts/20260803/tests/agentic-fraud-detection-events.ndjson`，摘要见
`artifacts/20260803/tests/agentic-fraud-detection-summary.json`。

这里的“未覆盖”同样只针对当前索引。源 PDF 的中文安全章节未被正确摄入，因此
需要修复数据源并重新运行回归测试后，才能对文档真实覆盖范围下结论。

## 6. 质量诊断

对同一成本/可靠性问题执行模态对照：

| 范围 | 结果数 | Top score |
| --- | ---: | ---: |
| 全部 | 34 | 0.676 |
| 仅图片描述 | 20 | 0.692 |
| 排除图片描述 | 20 | 0.605 |

全部 34 个结果中有 21 个图片描述、13 个非图片文本。

进一步诊断确认源 PDF 本身没有乱码：四种嵌入字体均为 Type0/Identity-H 且包含
`ToUnicode` 映射，pypdf 与 pdfplumber 都能正确抽取中文，Poppler 页面渲染也
正常。问题发生在 Managed KB 的 `SMART_PARSING` 摄入阶段。Sidecar metadata
设置了 `language=zh-CN`，但 20 个非图片诊断结果的系统字段 `_language_code`
全部为 `en`；分块大量丢失中文，只留下 AWS 产品名、链接、数字和标点。

因此准确结论是 Smart Parsing 对该中文 Apache FOP PDF 的语言识别或 CJK 文本
处理存在兼容性问题。具体内部故障点不通过 API 暴露。图片描述由 Smart Parsing
生成，可读性和相关度更好，但会包含模型推断的比例、峰值或架构含义。这些推断
不一定是原文明确事实。

生产建议：

- 保留图片提取来解决本 PDF 的文本编码问题。
- 优先把源 PDF 预抽取为 UTF-8 Markdown/HTML 后摄入，以绕过 Smart Parsing
  对 PDF 中文文本层的处理问题。
- 对数字、合规和决策类回答，要求引用原页或原图复核。
- 将 `_media_type` 纳入离线评测和查询过滤。
- 不把自动图像描述中的 estimated/proportional 数值直接写入事实库。
- 优先补充可稳定解析的英文 PDF、HTML 或标准化 Markdown 版本。
- 建立带标准答案和允许来源页的回归集，不能只用 relevance score 判断正确性。

完整诊断证据见
`artifacts/20260803/tests/pdf-smart-parsing-diagnostic.json`。Managed KB 只支持
`SMART_PARSING`，无法在当前数据源切换为 Bedrock Data Automation 或 foundation
model parser；需要其他 parser 时应使用 custom knowledge base。

### UTF-8 Markdown 修复

已将 146 页 PDF 预抽取为带物理页码标题的 UTF-8 Markdown，并通过独立 S3 prefix
创建第二个数据源：

- Data source ID：`<TEXT_DATA_SOURCE_ID>`
- Document ID：`aws-games-industry-lens-2026-07-31-text-v1`
- Markdown SHA-256：
  `863c8f0dbd2ebe27644c5eb81020d8f1f4e8d8993804235d2d14bbaca5f72d0d`
- 中文字符比例：65.0%
- Unicode replacement character：0
- Ingestion job：`<TEXT_INGESTION_JOB_ID>`，`COMPLETE`
- 新增文档 1，失败 0，跳过 0

四组过滤到新 document ID 的 Retrieve 测试均返回 10 个结果，所有结果都包含
中文且没有 replacement character。Top score 分别为 0.619、0.735、0.648、
0.719，覆盖玩家行为检测、配对绕过、欺诈检测以及强密码/MFA。

Agentic Retrieval 回归：

| 场景 | 流事件 | 结果 | Citation spans | Planning actions |
| --- | ---: | ---: | ---: | --- |
| 反作弊 | 199 | 10 | 24 | `[]` |
| 欺诈与检测 | 233 | 10 | 13 | `[]` |
| 玩家行为分析 | 257 | 10 | 22 | `[]` |

修复后的反作弊回答能够引用行为日志、异常进度/交易/通信、GuardDuty、Lookout
for Metrics、SageMaker AI、事件响应和封禁账户。欺诈回答能够引用欺诈账户、
虚拟经济机器人和自动化异常检测，不再依赖乱码或图片推断。

#### 玩家行为数据分析

玩家行为分析请求的 10 个结果全部来自修复后的 Markdown 数据源
`<TEXT_DATA_SOURCE_ID>`，并由
`document_id=aws-games-industry-lens-2026-07-31-text-v1`
过滤。Agentic Retrieval 返回 257 个流事件、22 个 citation spans；9 个结果
实际被引用。Speculative Retrieval 和 Planning 均成功，但
`planningActions=[]`，没有执行第二轮检索。

检索结果应按目的拆成三类数据产品：

- 产品与参与度分析：采集会话、进度、成就、购买、功能互动、社交活动和玩家
  反馈；在 Redshift 或 S3 数据湖中清理、转换和聚合，再用 QuickSight 分析
  留存、流失、盈利和功能使用。
- 玩家安全分析：跟踪异常进度、异常游戏内交易和可疑通信；将结构化
  CloudWatch 日志送入游戏分析管道，使用 CloudWatch Logs、OpenSearch 或合作
  伙伴工具调查；语音聊天可导出到 S3 并通过 Transcribe 转成可审核文本。
- 性能与体验遥测：采集服务器负载、网络流量、错误、ping、抖动、丢帧、API
  延迟和游戏循环完成情况；使用 CloudWatch/X-Ray，将遥测时间戳与支持工单和
  服务器日志关联，并对资源预算阈值告警。这类数据不能代替产品分析或滥用检测。

本轮 broad query 的 Top 10 没有召回源文档中已索引的 Lookout for Metrics 和
SageMaker AI 段落。此前定向 Retrieve 已证明该段落存在：Lookout for Metrics
用于检测登录、交易量、收入或留存率异常，SageMaker AI 用于自定义作弊、欺诈、
毒性和内容审核模型。这再次说明 `actions=[]` 可能造成跨章节覆盖缺口。

文档直接给出的治理要求仅包括数据识别/分类、静态数据保护和传输中保护。事件
schema、身份关联、sessionization、迟到事件处理、具体异常阈值、模型漂移、
实验设计、保留期限、用户同意和 GDPR/CCPA 删除流程均未在当前文档中规定。

完整摘要见
`artifacts/20260803/tests/agentic-player-behavior-analytics-summary.json`，原始流事件见
`artifacts/20260803/tests/agentic-player-behavior-analytics-events.ndjson`。

剩余风险：

- 系统 `_language_code` 仍误标为 `en`，但 UTF-8 文本未再损坏。
- 旧 PDF 数据源仍保留，应用必须强制使用新 `document_id` filter。
- Broad fraud query 没有命中源文档中的强密码/MFA；定向 Retrieve 可以命中，
  说明 `actions=[]` 的首轮停止仍可能造成覆盖缺口。
- 回答把 DRM 归入反作弊预防，属于比原文“内容保护”更宽泛的归类。
- Markdown 中有显式 `PDF 第 N 页` 标题，但 `_excerpt_page_number` 不再由服务
  原生填充。

完整摘要见
`artifacts/20260803/tests/text-repair-regression-summary.json`。

## 7. 更新策略

| 变化类型 | 建议操作 |
| --- | --- |
| 同一文档内容更新 | 覆盖同一 S3 key，保留 version，启动增量同步 |
| Metadata 更新 | 替换 `.metadata.json`，启动增量同步 |
| 新文档 | 上传到 inclusion prefix，启动增量同步 |
| 删除文档 | 删除源对象，启动同步；受 50% 删除保护约束 |
| Parser/media 设置变化 | 更新数据源支持更新的字段；完整读取后提交配置 |
| Chunking 变化 | 新建数据源 |
| Managed/custom embedding 切换 | 新建 KB |
| 高风险大版本 | 新建前缀和数据源，回归测试后切换应用配置 |

生产更新应采用发布流程：校验文件和 checksum、上传、同步、检查统计、运行检索
回归、人工抽查引用，然后再让应用流量使用新版本。

## 8. 治理策略

- IAM 服务角色仅允许列出和读取指定 S3 prefix。
- Trust policy 已从 `knowledge-base/*` 收紧到 KB ARN
  `arn:aws:bedrock:<REGION>:<AWS_ACCOUNT_ID>:knowledge-base/<KNOWLEDGE_BASE_ID>`。
- Metadata 至少包含 `document_id`、classification、owner/domain、language、
  version/effective date，并在检索时强制授权过滤。
- S3/SharePoint 等源系统权限不会自动继承到检索层；任何拥有
  `bedrock:Retrieve` 的调用者都可能看到已摄入内容。
- 管理操作默认进入 CloudTrail management events；`Retrieve` 和
  `AgenticRetrieveStream` 应配置 CloudTrail data events。
- 生产环境应配置 ingestion log delivery、失败告警和 AgentCore
  Observability。
- PII/PHI 应在摄入前清理。Guardrail 不会清理 API 返回的 raw retrieved
  references。
- 受监管数据应使用 customer-managed KMS，并定义 key rotation、日志保留和
  break-glass 流程。
- 定期执行权限复核、陈旧文档检查、删除恢复演练、检索质量评测和成本审计。

## 9. 优势与限制

主要优势：

- 不需要部署和维护向量数据库。
- 托管 embedding、reranker、Smart Parsing 和多模态索引。
- 原生连接器、增量同步、metadata filtering 和删除保护。
- Agentic Retrieval 支持查询分解、迭代检索、trace 和原生引用。
- 与 AgentCore agent、Gateway 和 Observability 的集成路径更短。

主要限制：

- 底层向量索引与 embedding 细节不可见。
- Managed embedding 下分块参数受限。
- `RetrieveAndGenerate` 不可用，需要 Agentic Retrieval 或手工 RAG。
- 新 API 对 CLI/SDK 版本要求高。
- Smart Parsing 的图像描述可能混入推断信息，仍需领域评测和引用治理。

## 10. 复现与清理

完整执行顺序见 [README.md](../README.md)。原始 AWS 响应和测试证据仅保存在本地
忽略目录 `artifacts/<RUN_ID>/`，不会发布到 GitHub。

资源目前保留供继续测试。清理命令受到确认字符串保护：

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```
