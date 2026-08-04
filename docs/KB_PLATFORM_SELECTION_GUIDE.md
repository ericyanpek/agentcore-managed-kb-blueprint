# Knowledge Base / RAG 平台全维度选型指南

研究日期：2026-08-03

适用读者：AWS Solutions Architect、企业架构师、GenAI/RAG 平台负责人

## 1. 先统一产品边界

官方产品名是 **Amazon Bedrock Managed Knowledge Base**。它与 Amazon Bedrock
AgentCore Gateway、Observability 和 MCP 原生集成，因此也经常被简称为
“AgentCore Managed Knowledge Base”。它不同于传统的、由客户选择向量存储的
Amazon Bedrock `VECTOR` Knowledge Base。

市场上的“Knowledge Base”至少分为四类，不能只比较向量检索性能：

| 类别 | 代表产品 | 供应商负责范围 |
| --- | --- | --- |
| 云厂商端到端托管 KB | Bedrock Managed KB、Azure AI Search Knowledge Base / Foundry IQ、Vertex AI Search / Agent Search | 连接、解析、索引、检索，部分产品还管理生成、ACL 和 Agent 集成 |
| 托管 RAG ISV | Vectara、Pinecone Assistant、LlamaCloud、deepset | 通常管理解析、Embedding、检索、Rerank、生成或其中大部分 |
| 企业搜索产品 | Glean、Coveo、Elastic Search AI | 强连接器、权限感知、搜索体验和企业治理 |
| 自建 RAG + 向量数据库 | pgvector、OpenSearch/Elastic、Pinecone DB、Weaviate、Qdrant、Milvus/Zilliz、MongoDB Atlas Vector Search | 数据库只解决索引和检索；其余 Pipeline 由客户负责 |

“使用托管向量数据库”不等于“使用托管 Knowledge Base”。如果团队仍需自己实现
连接器、解析、Chunking、Embedding、同步、ACL、Rerank、引用、生成、评测和
可观测性，它仍属于自建 RAG。

## 2. 执行摘要

### 默认建议

1. **AWS/AgentCore 优先**：需要快速给 AgentCore、Strands、LangChain 或任意
   MCP Agent 提供企业知识，且能接受 AWS 托管边界时，优先 Bedrock Managed KB。
2. **Microsoft 数据面优先**：数据集中在 Microsoft 365、OneLake、Azure Blob，
   团队已有 Azure AI Search 能力并希望精调索引和检索时，优先 Azure AI Search
   Knowledge Base / Foundry IQ。
3. **Gemini/Google 搜索体验优先**：需要 Gemini Grounding、网站搜索或 Google
   数据生态时，优先 Vertex AI Search（新文档也可能称 Agent Search）；需要更
   可编程的 RAG Corpus 和向量后端时使用 Vertex AI RAG Engine。
4. **跨云托管 RAG 优先**：不希望绑定单一 Hyperscaler，同时重视检索、Rerank、
   引用和事实一致性时，重点评估 Vectara；文件型 Assistant 可评估 Pinecone
   Assistant。
5. **员工企业搜索优先**：需要大量 SaaS 连接器、继承用户权限和开箱即用的搜索
   产品体验时，优先评估 Glean/Coveo，而不是先建设通用向量数据库。
6. **控制、驻留或差异化优先**：需要私有部署、Air Gap、特殊 Chunking、专用
   Embedding、复杂混合排序、极端规模或复用现有数据库时，自建 RAG 更合理。

### 不建议直接给出单一“市场最佳”

平台优劣高度依赖以下四个约束：

- 数据是否已经集中在 AWS、Microsoft 365、Google Workspace 或某一数据库。
- 是否必须继承源系统的文档级 ACL，并进行实时权限复核。
- 是否需要控制 Parser、Chunk、Embedding、索引、Rerank 和 Agentic Planner。
- 团队愿意承担多少检索工程、平台运维和安全治理责任。

## 3. 三大云端到端能力对比

### 3.1 核心矩阵

| 维度 | Amazon Bedrock Managed KB | Azure AI Search KB / Foundry IQ | Google Vertex AI Search / RAG Engine |
| --- | --- | --- | --- |
| 产品定位 | Agent 优先的完全托管 RAG | 搜索引擎优先、逐步增加 Agentic KB | Search 面向开箱搜索；RAG Engine 面向开发者 RAG |
| 生产成熟度 | 2026-06 GA；产品较新 | Agentic Retrieval 部分 API 已 GA，Portal/Foundry 部分能力仍为 Preview | Vertex AI Search 较成熟；RAG Engine 与新 Agent Search 命名和能力仍在快速演进 |
| 数据连接 | S3、SharePoint、Confluence、Google Drive、OneDrive、Web Crawler、Custom | Blob、ADLS、OneLake、Azure SQL、Cosmos DB 等 Indexer；Knowledge Source 可为索引或远程源 | Search 支持网站和文档 Data Store；RAG Engine 可从 GCS、Google Drive 等导入 |
| 自动同步 | 原生增量同步；显式 StartIngestionJob；连接器可自动计划 | Indexer/Scheduler、Push API 或知识源自动生成资源 | Data Store 导入/同步；RAG Engine ImportRagFiles |
| 文档解析 | Smart Parsing；文本、扫描件、图像、音频、视频 | Skillset、Document Intelligence/Content Understanding、图片 verbalization | Search 托管解析；RAG Engine 可配置 Parser/Chunking，复杂文档通常组合 Document AI |
| Chunking | 托管默认或按 KB 类型提供有限选择；创建后变更通常需要重建数据源 | Skillset/Index projection 高度可配置 | Search 偏托管；RAG Engine 可配置 Chunk 大小与重叠 |
| Embedding | Managed 或自选 Bedrock Embedding | Azure OpenAI 或 Foundry 模型；Integrated Vectorization | Vertex Embedding；RAG Engine 可配置模型和向量后端 |
| 向量存储 | Managed 模式完全隐藏并自动扩展 | Azure AI Search Index 对客户可见并可精调 | Search Data Store 托管；RAG Engine 可使用 Managed DB、Vertex Vector Search 等 |
| 检索 | Hybrid、Managed Ranking、Metadata Filter | BM25 + Vector + Semantic Ranker，索引和查询参数控制最细 | Search 提供语义+关键词+Rerank；RAG Engine 提供可编程 Context Retrieval |
| Agentic Retrieval | 原生多 KB Query Planning、迭代检索、充分性评估、Trace | Knowledge Base 负责 Query Planning、并行子查询、Semantic Rerank 和 Answer Synthesis | Search/Gemini Grounding 偏托管检索；复杂多步编排通常由 Agent/应用层承担 |
| 生成 | AgenticRetrieveStream 可直接生成带引用回答；也可 Retrieve 后自定义生成 | 可返回 Grounding Data，也可启用 Answer Synthesis | Gemini Grounding 原生返回 Grounding Metadata；RAG Engine 接 GenerateContent |
| 文档 ACL | 多数连接器支持 ACL Awareness 和部分实时源系统校验；Web Crawler 例外 | Entra、Security Filter、Permission Metadata 和知识源能力组合实现 | 依具体 Search/Connector；RAG Engine 通常需要应用层 Filter/IAM 设计 |
| 重要安全边界 | ACL Awareness 不是身份认证；应用必须传入可信用户身份 | Search RBAC 保护服务，文档级权限仍取决于索引字段和查询身份传播 | Cloud IAM 保护资源，文档级隔离必须验证 Data Store/应用 Filter 的语义 |
| Agent 接口 | AgentCore Gateway 原生 MCP Target；也有 SDK/API | Knowledge Base 暴露 MCP Tool，可连接 Foundry Agent | Vertex/Gemini SDK Tool 和 Retrieval API；MCP 需按产品能力集成 |
| 可观测性 | AgentCore Observability、Retrieval/Agentic Trace、KB Metrics、CloudTrail | Azure Monitor、Diagnostic Logs、Search Metrics；Agentic Token/Query 可观测 | Cloud Logging/Monitoring、Grounding Metadata；应用层补充 Trace/Eval |
| 更新与删除 | 增量摄入、删除策略、删除保护；配置变更有重建边界 | Indexer、Alias/双索引切换、可精细控制索引生命周期 | Data Store/RAG Corpus 导入删除；生产切换通常需应用层版本策略 |
| 网络与加密 | IAM、KMS、VPC/PrivateLink 能力按服务和连接器核验 | Entra ID、Managed Identity、Private Endpoint、CMK 能力按 SKU 核验 | IAM、VPC-SC、CMEK 和区域能力按具体 Search/RAG 产品核验 |
| 计费形态 | 原始数据存储、Retrieve、Agentic Retrieve；托管 Parser/Embedding/Reranker 默认包含；自选模型另计 | Search Unit/容量、Semantic/Agentic Token、Azure OpenAI/Foundry 模型、富化 Pipeline | Search Index/Query 或 Agent Search 用量、Gemini Token；RAG Engine 向量后端和模型另计 |
| 底层可控性 | 最低到中 | 高 | Search 低到中；RAG Engine 中到高 |
| 可移植性 | 较低；原始源文件和 Metadata 可保留 | 中；索引 Schema 和 Skillset 有 Azure 依赖 | 中低；Data Store/RAG Corpus API 有 GCP 依赖 |
| 最适合 | AWS 原生 Agent、MCP Tool、快速企业 RAG | 搜索工程团队、Microsoft 数据面、精细相关性调优 | Gemini 应用、网站/文档搜索、GCP 原生 RAG |

### 3.2 各自主要优劣势

#### Amazon Bedrock Managed Knowledge Base

优势：

- 最少基础设施：无需选择或维护向量数据库、Embedding 和 Reranker。
- 原生 `Retrieve` 与 `AgenticRetrieveStream`，后者支持多 KB、规划、迭代和 Trace。
- 原生 AgentCore Gateway MCP Target，Agent 集成代码和权限配置少。
- 多模态 Smart Parsing 和六类原生连接器，加上 Custom Ingestion。
- 增量同步、Metadata Filter、ACL Awareness、KMS、CloudTrail/Observability 路径完整。
- 使用量计费中，托管 Parser、Embedding、Reranker 默认包含，早期项目 TCO 清晰。

限制：

- 底层向量索引、Embedding 细节和召回算法不可见，深度调优能力弱。
- 配置存在不可变边界；Embedding/Chunking/Parser 变更可能需要新 KB 或数据源。
- Managed KB 不支持传统 `RetrieveAndGenerate`；需 `Retrieve -> Converse` 或
  `AgenticRetrieveStream`。
- ACL Awareness 不是授权系统，必须由应用完成用户认证和可信 Identity 传播。
- 2026-06 才 GA，区域、SDK/API 和运维经验仍少于成熟搜索产品。
- Agentic 最大迭代次数只是上限，Planner 可能首轮返回 `actions=[]`。

本项目的实测风险：

- 一个中文 Apache FOP PDF 在 Smart Parsing 阶段出现 CJK 文本损坏；预抽取为
  UTF-8 Markdown 后恢复。这是单一文档兼容性证据，不应外推为所有中文 PDF。
- Managed Embedding 实测强制使用默认 300 Token、20% Overlap，不能显式提交
  Chunking Configuration。
- 三次 Agentic Retrieval 回归均完成 Planning，但 `actions=[]`，Broad Query
  会漏掉索引中已存在的相关章节；高覆盖场景仍需 Query Decomposition 回归。

#### Azure AI Search Knowledge Base / Foundry IQ

优势：

- 三家中对 Index Schema、Analyzer、Vectorizer、Skillset、Hybrid Query、
  Semantic Ranker 和结果调优控制最强。
- Integrated Vectorization 将 Chunking、Embedding 和索引更新放进 Indexer。
- Agentic Retrieval 支持 Knowledge Source、Query Planning、并行子查询、
  L2 Semantic Reranking 和 Answer Synthesis。
- 与 Azure OpenAI、Foundry Agent、Entra ID、OneLake、Blob 和 Azure 数据库整合深。
- 适合已有传统搜索、关键词精确匹配、过滤、排序和 Facet 需求的企业。

限制：

- 不是单一“零运维 KB”：通常仍需组合 Search Service、Index、Indexer、
  Skillset、Embedding/LLM Deployment 和 Foundry。
- Agentic Retrieval 的 GA/Preview 状态按 API、Portal 和具体能力不同，必须逐项核验 SLA。
- Search Unit、Semantic/Agentic Token、Embedding 和 Generation 分项计费，
  容量规划比完全 Serverless 方案复杂。
- 文档级权限常依赖 Permission Metadata、Security Filter 和 Identity 传播，
  错误设计可能造成越权或零结果。

#### Google Vertex AI Search / Agent Search / RAG Engine

优势：

- Vertex AI Search 提供“Google Search for your data”式托管语义、关键词和 Rerank。
- Gemini 可以直接 Ground 到最多 10 个 Vertex AI Search Data Store，并返回引用元数据。
- 可把私有数据 Grounding 与 Google Search Grounding 组合。
- RAG Engine 为开发者提供 RAG Corpus、Import、Retrieve Context 和 GenerateContent 集成。
- RAG Engine 可连接 Vertex AI Vector Search 等后端，适合从托管 Search 逐步转向自定义。

限制：

- Search、Agent Search、Gemini Enterprise Agent Platform 和 RAG Engine
  存在产品定位及命名重叠，采购前需要确认当前 Console/API 的正式产品边界。
- Vertex AI Search 偏黑盒相关性；RAG Engine 更灵活但需要更多 Pipeline 组装。
- Source ACL 继承和实时权限检查不宜按产品宣传推断，应对每种 Connector 做越权 POC。
- 复杂多跳 Agentic Retrieval 通常需要 Agent/应用层编排，不应默认等价于 AWS/Azure
  的 Knowledge Base Planner。

## 4. 代表性 ISV 对比

| ISV 类型/产品 | 主要能力 | 优势 | 主要限制 | 适用场景 |
| --- | --- | --- | --- | --- |
| Vectara | Parsing、Embedding、Hybrid、Rerank、Generation、Citation、HHEM 一体化 | 云中立；多语言；检索和事实一致性工具完整；上线快 | 专有模型和 API 锁定；应用仍负责构造正确 ABAC Filter 并同步源权限 | 跨云、高质量托管 RAG、客户支持/研究 |
| Pinecone Assistant | 文件上传、托管解析/索引、Chat、Citation、Context API、Metadata Filter | 极简开发体验；可只取 Context 接自有 LLM；Pinecone 向量能力成熟 | 原生企业连接器和 ACL 继承较弱；Multimodal PDF 仍可能是 Preview；需核验存储区域 | 文件型助手、产品内嵌 Q&A、快速 POC |
| Glean/Coveo | 大量 SaaS Connector、企业搜索、用户权限感知、搜索/助手 UX | 企业权限和连接器是核心能力；员工开箱体验强 | 低层 Parser/Embedding/Index 控制弱；商业授权和平台锁定；不一定适合作为通用 RAG API | 企业内部搜索、员工 Copilot |
| LlamaCloud/LlamaParse | 高质量复杂文档解析、Extraction、Managed Ingestion、Agentic Document Workflow | 对表格、扫描件、复杂布局和结构化抽取有优势；与多种向量库/模型组合 | 往往不是完整企业搜索产品；ACL、HA、生成治理仍需其他组件 | 合同、财务、保险、医疗等复杂文档 |
| deepset/Haystack Enterprise | 可组合 RAG/Agent Pipeline、部署、监控、治理；支持私有/VPC/On-Prem | 高可扩展性和可部署性；避免单一模型/数据库绑定 | 平台工程投入高于一体化 SaaS；质量取决于客户 Pipeline | 受监管、私有部署、复杂 RAG 工程 |
| Elastic Search AI | 全文、Hybrid、Vector、Filter、Connector、Security 与 Observability | 搜索成熟；已有 Elastic 数据和团队时复用成本低 | 需要自行组装 Parser、Embedding、Rerank、Generation 和 Eval；许可/SKU 需核验 | 已有 Elastic、日志和内容搜索统一平台 |

ISV 选型必须额外审查：

- 数据实际存储区域、子处理商、模型调用是否跨区域。
- 是否支持 PrivateLink/VPC Peering/BYOC/On-Prem/Air Gap。
- Source ACL 是实时继承、同步副本，还是仅由调用方传 Metadata Filter。
- 删除请求能否覆盖原文件、Chunk、Embedding、缓存、日志和备份。
- 导出能力是否包含原文、Chunk、Metadata、Embedding 和离线评测集。
- SLA、RPO/RTO、限流、突发容量和模型升级变更通知。

## 5. 自建 RAG 与向量数据库对比

### 5.1 代表性底座

| 底座 | 强项 | 弱项 | 建议使用条件 |
| --- | --- | --- | --- |
| PostgreSQL + pgvector | 关系数据、事务、Join、RLS 与向量共库；团队通常已有 PostgreSQL | Hybrid/Rerank 需自己编排；高维大规模 ANN 会与 OLTP 争资源 | 已有 PostgreSQL、规模可控、权限/业务数据关联重要 |
| OpenSearch / Elasticsearch | BM25、Analyzer、Filter、Facet、Hybrid 和搜索运维成熟 | 集群容量和相关性调优复杂；不是完整 RAG Pipeline | 关键词精度、日志/内容统一检索、已有搜索团队 |
| Pinecone DB | Serverless/Managed、低运维、Namespace/Metadata Filter、Dense/Sparse | 只有检索底座；Parser/ACL/Generation 自建；SaaS 锁定 | 想自定义 RAG，但不想运维向量集群 |
| Weaviate / Qdrant | 开源或 Cloud；Hybrid、Filter、Multi-tenancy；开发体验好 | 自托管需 HA、升级、备份和安全运维；连接器和引用自建 | 云中立、中大型定制 RAG、需要开源可移植性 |
| Milvus / Zilliz Cloud | 分布式、大规模、多向量、Dense/Sparse Hybrid、多种索引 | 自建 Milvus 运维复杂；小规模场景可能过度设计 | 超大规模、多模态、多向量、专职平台团队 |
| MongoDB Atlas Vector Search | 文档数据和向量同库，应用开发与 Filter 方便 | 搜索/ANN 与主业务容量耦合；完整 RAG Pipeline 自建 | 已使用 MongoDB 且知识与业务文档天然同模型 |
| 云原生 Vector Search | Vertex Vector Search、S3 Vectors、Aurora/OpenSearch 等 | 云绑定；仍需要上层 RAG Pipeline | 已有云承诺、希望在成本与控制之间折中 |

### 5.2 自建时客户必须拥有的能力

向量数据库不会自动提供以下生产能力：

1. Connector、增量同步、Webhook、Rate Limit 和源系统失败重试。
2. OCR、表格/图片/音视频解析、文档规范化和恶意文件隔离。
3. Chunking、Embedding 版本、批量重建和双索引迁移。
4. Dense + Sparse Hybrid、Query Rewrite、Rerank、Threshold 和去重。
5. 身份认证、ACL/RLS、Tenant Isolation 和越权测试。
6. Prompt、引用映射、Grounded Failure、Guardrail 和安全输出。
7. Golden Set、Recall@K、NDCG、Citation Precision、Faithfulness 和回归门禁。
8. Trace、Metric、Audit、Cost Attribution、Backup、RPO/RTO 和容量规划。
9. 删除传播、Retention、Legal Hold、数据主体删除和模型/索引血缘。

只比较“向量写入成本”和“Top-K 查询延迟”会系统性低估自建 TCO。

## 6. 场景评分

评分范围：1（弱）到 5（强）。这是架构初筛，不替代基于客户数据的 POC。

| 场景/能力 | AWS Managed KB | Azure AI Search KB | GCP Search/RAG | 托管 RAG ISV | 自建 RAG |
| --- | ---: | ---: | ---: | ---: | ---: |
| 最短上线时间 | 5 | 3 | 4 | 5 | 1 |
| AWS AgentCore/MCP 集成 | 5 | 2 | 2 | 3 | 3 |
| Microsoft 365/OneLake 整合 | 4 | 5 | 2 | 4 | 2 |
| Gemini/Google Search Grounding | 2 | 2 | 5 | 2 | 3 |
| Parser/Chunk/Index 深度控制 | 2 | 5 | 4 | 3-4 | 5 |
| 内置多跳 Agentic Retrieval | 5 | 5 | 3 | 2-4 | 1（默认） |
| 企业连接器和源 ACL | 4 | 4 | 3 | 2-5 | 1（默认） |
| 云中立/可移植 | 2 | 2 | 2 | 4 | 5 |
| On-Prem/Air Gap | 1 | 1 | 1 | 2-4 | 5 |
| 最低平台运维 | 5 | 3 | 4 | 4-5 | 1 |
| 复杂文档差异化 | 3-4 | 4 | 4 | 4-5 | 5（投入后） |
| 搜索相关性精细调优 | 2-3 | 5 | 3-4 | 4 | 5 |

## 7. 决策树

```text
是否必须 On-Prem / Air Gap / 完全控制数据面？
  是 -> 自建 RAG，优先复用 pgvector/OpenSearch；超大规模再评估 Milvus/Qdrant/Weaviate
  否 ->
    是否主要是员工企业搜索，并要求大量 SaaS Connector 和源 ACL？
      是 ->
        Microsoft 数据面 -> Azure AI Search / Foundry IQ
        AWS AgentCore 数据面 -> Bedrock Managed KB
        跨云开箱搜索 -> Glean/Coveo
      否 ->
        是否需要深度控制 Parser、Chunk、Embedding、Index 和 Rerank？
          是 ->
            Azure 原生 -> Azure AI Search
            GCP 原生 -> Vertex AI RAG Engine
            云中立 -> LlamaCloud/deepset + 自选 Vector DB
          否 ->
            AWS Agent/MCP -> Bedrock Managed KB
            Gemini/网站 Grounding -> Vertex AI Search / Agent Search
            云中立托管 RAG -> Vectara/Pinecone Assistant
```

## 8. TCO 比较方法

### 成本项

| 成本层 | 完全托管 KB | ISV | 自建 |
| --- | --- | --- | --- |
| 数据源与同步 | 通常包含或按摄入计费 | 套餐/Connector 计费 | 开发和运行 Connector |
| 解析/OCR/多模态 | 包含、按页/分钟或模型计费 | 套餐或用量 | OCR/模型/计算成本 |
| Embedding | 可能包含或按 Token | 通常包含或用量 | 模型 Token + 批处理 |
| 向量存储 | GB/月或平台用量 | Corpus/Vector/Pod/Serverless | DB 节点、存储、备份 |
| 检索/Rerank | Query/Token/Compute | Query/Token | 集群 + Rerank 模型 |
| 生成 | 模型 Token | 通常模型/Token | 模型 Token |
| 工程和运维 | 低 | 低到中 | 高 |
| 安全和合规 | 配置与审计 | 合同审查 + 配置 | 全生命周期自建 |

### 计算原则

- 同时计算每月现金成本和 FTE 成本，至少覆盖 3 年。
- 分开计算普通 `Retrieve` 和 Agentic/Multi-hop Query；后者会放大检索和模型调用。
- 把全量重建、Embedding 升级、权限同步和删除重放纳入成本，而不只算稳态查询。
- 对固定容量服务计算低利用率浪费；对 Serverless 服务计算突发 Query 和 Token 风险。
- 设定成本保护：单请求最大子查询数、Top-K、Rerank 数、上下文 Token 和输出 Token。

## 9. POC/RFP 验收框架

### 统一测试集

- 100-300 个经过领域专家审核的问题。
- 简单事实、关键词/编号、多跳、跨文档、时间有效性、无答案和对抗性问题。
- 中文、英文、混合语言。
- 原生 PDF、扫描 PDF、表格、图像、PPTX、HTML、音频/视频。
- 至少三种权限角色，包含权限变更、用户离职和 Email 重用场景。
- 文档新增、覆盖、删除、大规模误删和回滚。

### 必测指标

| 领域 | 指标 |
| --- | --- |
| 摄入 | 成功率、吞吐、Freshness Lag、增量同步正确率、删除传播时间 |
| 检索 | Recall@K、MRR/NDCG、Context Precision、跨章节/多跳覆盖率 |
| 生成 | Answer Correctness、Faithfulness、Citation Precision/Recall、Grounded Failure |
| 权限 | Unauthorized Retrieval 必须为 0；权限收回生效时间 |
| 性能 | P50/P95/P99、并发、Cold Start、Agentic 迭代分布 |
| 稳定性 | 限流、Connector 失败、模型失败、索引重建和区域故障恢复 |
| 成本 | 每摄入文档、每普通查询、每 Agentic 查询、每成功回答成本 |
| 运维 | Trace 完整率、告警、审计、版本回滚、故障平均恢复时间 |

### 硬性淘汰条件

- 任意越权检索或 Raw Chunk 泄漏。
- 删除/权限撤销无法在业务 SLA 内生效。
- 无法导出源文档、Metadata 和评测证据。
- Parser 对关键语言或关键格式没有稳定回退路径。
- 无法设置 Query/Token/Agentic Iteration 成本上限。
- Preview 能力被当成生产核心路径，但没有替代方案。

## 10. 面向当前 AWS 客户的建议

对当前 AgentCore Managed KB 测试环境，建议采用“两阶段选择”：

1. 把 Bedrock Managed KB 作为 AWS 原生快速路径，继续验证 S3、SharePoint/
   OneDrive/Confluence ACL、中文复杂 PDF、增量同步和 Agentic Query Coverage。
2. 选择一个高控制对照组。若客户主要在 Microsoft 数据面，使用 Azure AI Search；
   若要求云中立，使用 Vectara 或 `LlamaParse + pgvector/OpenSearch`。
3. 使用完全相同的文档、Metadata、ACL 和 Golden Set 做盲测，不接受厂商 Demo Corpus。
4. 对 Agentic Retrieval 单独统计 Planning Actions、子查询数和遗漏章节，不能只看最终回答。
5. 在生产方案中保留预抽取 UTF-8 Markdown/HTML 的回退 Pipeline，即使平台宣称支持
   Smart/Advanced Parsing。

## 11. 主要来源

### AWS

- [Amazon Bedrock Managed Knowledge Base GA](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-managed-knowledge-base/)
- [Knowledge bases for Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [Create a managed knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-create.html)
- [Agentic retrieval](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-agentic-retrieve.html)
- [AgentCore Gateway Managed KB connector](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-managed-kb.html)
- [ACL awareness](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-acl.html)
- [Managed KB architecture and pricing discussion](https://aws.amazon.com/blogs/machine-learning/build-enterprise-search-for-agents-with-amazon-bedrock-managed-knowledge-base/)

### Microsoft Azure

- [Azure AI Search agentic retrieval overview](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview)
- [Agentic retrieval pipeline tutorial](https://learn.microsoft.com/en-us/azure/search/search-agentic-retrieval-how-to-pipeline)
- [Integrated vectorization](https://learn.microsoft.com/en-us/azure/search/search-get-started-portal-import-vectors)
- [Answer synthesis](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-how-to-answer-synthesis)
- [Connect Foundry Agents to Knowledge Bases](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/how-to/tools/knowledge-retrieval?view=foundry)

### Google Cloud

- [Grounding with Vertex AI Search](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/grounding/grounding-with-vertex-ai-search)
- [Google Cloud RAG product overview](https://cloud.google.com/use-cases/retrieval-augmented-generation)
- [Vertex AI RAG Engine with Vertex AI Vector Search](https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/use-vertexai-vector-search)

### ISV 与向量数据库

- [Vectara Knowledge architecture](https://docs.vectara.com/docs/platform-architecture/knowledge)
- [Vectara Hybrid Search](https://docs.vectara.com/docs/search-and-retrieval/hybrid-search)
- [Vectara Citations](https://docs.vectara.com/docs/search-and-retrieval/citations)
- [Pinecone Assistant](https://docs.pinecone.io/guides/assistant/overview)
- [Pinecone Assistant files and metadata](https://docs.pinecone.io/guides/assistant/files-overview)
- [Glean agent permissions](https://docs.glean.com/agents/concepts/sharing-permissions)
- [Weaviate Hybrid Search](https://docs.weaviate.io/weaviate/concepts/search/hybrid-search)
- [Zilliz/Milvus Hybrid Search](https://docs.zilliz.com/reference/restful/hybrid-search-v2)
- [Milvus](https://github.com/milvus-io/milvus)
- [pgvector](https://github.com/pgvector/pgvector)

## 12. 研究限制

- 产品状态、区域、价格和 Preview/GA 边界变化很快，应在正式提案当天重新核验。
- 供应商自报的相关性提升或性能数字没有统一 Corpus、硬件和 Recall 条件，不能横向使用。
- 本报告的评分是架构初筛；最终选择必须基于客户自己的数据、ACL 和 Golden Set。
- 当前 AWS 实测只覆盖一个中文游戏行业 PDF，能证明兼容性风险和回退策略，不能代表
  所有语言、文档类型或工作负载。
