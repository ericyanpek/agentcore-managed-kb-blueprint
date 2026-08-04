# AgentCore Managed Knowledge Base 蓝图

**中文（默认）** | [English](README.en.md)

本项目使用公开的 AWS Well-Architected 游戏行业视角 PDF，创建并验证一个
Amazon Bedrock AgentCore Managed Knowledge Base。

[实测报告](docs/RESULTS.md)记录了 AWS 资源配置、摄入统计、检索评分、
Agentic Retrieval 结果、质量问题和治理建议。

[Knowledge Base / RAG 平台选型指南](docs/KB_PLATFORM_SELECTION_GUIDE.md)
提供跨云、ISV 和自建向量数据库的选型框架。

[AWS Knowledge Base / RAG 最佳实践与运营治理指南](docs/AWS_KB_RAG_BEST_PRACTICES.md)
汇总 AWS 官方文档、博客和白皮书中的质量评测、知识更新、权限治理、检索优化、
性能、成本与 RAGOps 发布建议。

[游戏行业白皮书语义分块对照实验](docs/SEMANTIC_CHUNKING_EXPERIMENT.md)
比较服务默认 Fixed Size 基线与结构感知预分块在证据覆盖、排序、溯源和延迟上的
实测表现。

[Managed Knowledge Base Metadata 对照实验](docs/METADATA_EXPERIMENT.md)
使用三组字节相同的语料，比较无 Metadata、仅过滤 Metadata 和参与 Embedding
的语义 Metadata，并说明存储、更新和治理方法。

[项目级 KB/RAG 数据准备 Skill](.agents/skills/kb-rag-data-preparation/SKILL.md)
封装从解析、结构恢复、分块和 Metadata 到摄入、评测、发布与回滚的方法论及
Corpus Profiler。

[安全与发布规范](SECURITY.md)说明仓库发布边界和脱敏要求。

中文与英文 README 必须保持同步。任何 README 改动都必须在同一次变更中同步更新
两个语言版本。可运行 `python3 scripts/13_check_readme_sync.py` 检查双语章节、
命令块和链接；GitHub CI 也会要求两份 README 成对修改。

## 本地环境

仓库不包含 AWS 凭据、账户标识、资源 ID 或原始运行响应。执行脚本前，创建被
Git 忽略的本地配置和 Python 环境：

```bash
cp config/test.env.example config/test.env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws sts get-caller-identity
```

根据目标区域和资源命名修改 `config/test.env`。如果账户 ID 和桶名留空，
Provisioning 阶段会根据当前 AWS 身份推导账户 ID 和默认的全局唯一桶名。如果
设置了 `AWS_ACCOUNT_ID`，脚本会拒绝与当前 AWS 身份不一致的配置。AWS 凭据
继续使用标准 AWS CLI credential chain，不要把访问密钥写入本仓库。

## 执行顺序

```bash
./scripts/01_prepare_source.sh
./scripts/02_provision.sh
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

`scripts/08_agentic_retrieval.py` 提供流式 Agentic Retrieval 实现，需要使用
包含 `agentic_retrieve_stream` 的 SDK 版本。

中文 PDF 会触发 Managed KB Smart Parsing 的兼容性问题，导致大部分 CJK 字符
丢失。修复路径会预抽取 UTF-8 Markdown，并通过独立数据源摄入：

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

抽取质量门禁要求文档包含 146 页、没有 Unicode replacement character、
图片页或空文本页不超过 1 页，且 CJK 字符比例至少为 50%。修复后的文档使用
`document_id=aws-games-industry-lens-2026-07-31-text-v1`。在保留原始诊断
PDF 数据源期间，应用必须使用该值进行过滤。

Managed Embedding 不接受显式 Semantic Chunking。本仓库提供摄入前的结构感知
预分块实验：按问题、最佳实践、实施指导和句子边界生成独立 Markdown，通过隔离
Data Source 与 Fixed Size 基线比较。

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

实验组提高了 Top-10 证据覆盖和可溯源性，但 MRR 下降，因此当前作为 Canary
Corpus 保留，不替换 `text-v1` 基线。

Metadata 实验将同一组 479 份 canonical Markdown 分别以无 Sidecar、全部
`includeForEmbedding=false`、选定语义字段 `includeForEmbedding=true` 摄入：

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

扩展实验包含 44 条查询、两种 Rerank 模式和 408 次 Retrieve。未过滤召回仍未
测得 Metadata Embedding 增益；Runtime Filter 则将 36 条可过滤查询的 MRR 从
0.241 提升到 0.556，并将 `best_practice_id` 章节定位的 MRR 提升到 1.000。
Metadata Filter 只缩小语义候选集，不保证返回结果；权限 Filter 空集必须
Fail Closed，确定性文档读取应转到 S3 或内容系统。因此默认策略是保留完整
Metadata、治理字段不参与 Embedding，并优先使用稳定控制编号进行运行时过滤。

修改原始 PDF 后，使用 `./scripts/05_sync_updates.sh` 进行同步。同步修复后的
文本数据源时，需要显式选择其 Data Source：

```bash
source artifacts/20260803/state.env
TARGET_DATA_SOURCE_ID="${TEXT_DATA_SOURCE_ID}" ./scripts/05_sync_updates.sh
```

清理脚本要求显式确认：

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

## 测试设计

- Knowledge Base 类型：`MANAGED`
- Embedding：服务托管
- Reranker：服务托管
- 数据源：独立、私有、启用版本控制和 SSE-S3 加密的 S3 桶
- Connector：限定到单一 Prefix 的 Managed S3 Connector
- Parsing：`SMART_PARSING`
- 图片抽取：为 PDF 视觉内容启用
- Chunking：服务默认 Fixed Size，300 Tokens，20% Overlap
- 删除保护：单次同步删除超过 50% 的已索引文档时阻止删除
- Data Deletion Policy：`DELETE`
- 检索：Managed Search，10 个候选结果，Managed Reranking
- 生成基线：先执行 `Retrieve`，再调用 Amazon Nova Lite `Converse`；
  最大输出 1,200 Tokens，Temperature 0.1

Chunking 策略在 Data Source 创建后不可变。产品文档中的 Managed Knowledge
Base 支持 Default、Fixed Size 和 No Chunking，不支持 Semantic 或
Hierarchical Chunking。

2026-08-03 实测中，为使用 `embeddingModelType=MANAGED` 的 Knowledge Base
显式提交 `chunkingConfiguration` 时，`CreateDataSource` API 返回：

```text
A chunking strategy cannot be specified with a managed embedding model.
Omit chunkingConfiguration to use the default.
```

因此当前可执行配置不提交 `chunkingConfiguration`，使用服务默认的 300 Tokens
和 20% Overlap。需要显式控制 Chunk 大小时，应使用 Custom Embedding Model
并创建新的 Data Source。

## 运行产物

所有命令响应和测试证据保存在被 Git 忽略的 `artifacts/<RUN_ID>/` 目录：

- `source/`：下载的 PDF、SHA-256、PDF Metadata 和摄入 Metadata
- `aws/`：资源描述、IAM 证据和 Ingestion Job 统计
- `tests/`：原始检索与生成响应，以及精简摘要
- `state.env`：生成的非敏感资源标识

这些文件仍可能包含账户 ID、IAM ARN、桶名、资源 ID、源文件路径和检索内容。
它们属于运行证据，不是可发布的测试 Fixture。暂存发布文件前，运行
`./scripts/12_repository_safety_check.sh`。

修复流程的证据包括：

- `source/games-industry-lens.zh-CN.md`：包含物理页标记的 UTF-8 文本
- `tests/pdf-to-markdown-report.json`：本地抽取质量指标
- `tests/text-repair-retrieval-summary.json`：四项带 Filter 的 Retrieve 检查
- `tests/text-repair-regression-summary.json`：摄入和 Agentic 回归摘要
- `tests/agentic-player-behavior-analytics-summary.json`：玩家行为分析的检索
  参数、覆盖度评估和证据链接
- `tests/agentic-player-behavior-analytics-events.ndjson`：玩家行为分析查询的
  原始流事件
- `tests/semantic-chunking-preparation-report.json`：语义预分块质量统计
- `tests/semantic-chunking-comparison.json`：八项 A/B 检索的指标与逐项结果
- `tests/metadata-experiment-preparation-report.json`：三组内容一致性和 Sidecar 门禁
- `tests/metadata-experiment-comparison.json`：Metadata 质量与 Filter 对照结果
- `tests/metadata-expanded-query-set.json`：44 条扩展召回 Query Set
- `tests/metadata-expanded-comparison.json`：408 次调用的指标、类别和配对对照
- `tests/metadata-expanded-comparison.md`：自动生成的扩展实验精简报告

## 存储与 Pipeline

源 PDF 和对应的 Sidecar Metadata 文件是配置 Prefix 下的普通 S3 对象。
Vector Index、Embedding、Parser 执行、Reranker 和检索基础设施均由服务托管，
不会暴露为客户自己的 S3 Vectors Bucket 或 OpenSearch Collection。

Metadata 的权威副本是与源文件相邻、同名追加 `.metadata.json` 的 S3 Sidecar；
摄入后字段被复制到 Managed KB 内部索引并附着到 Chunk。`includeForEmbedding`
只控制字段是否参与向量输入，不控制其是否存储或可过滤。Sidecar 更新后必须显式
运行新的 Ingestion Job。

摄入 Pipeline 不需要 Lambda、Step Functions 或独立 ETL Job。需要在控制面
显式执行：

1. 上传或更新源对象。
2. 调用 `StartIngestionJob`。
3. 轮询 `GetIngestionJob`，直到状态为 `COMPLETE` 或 `FAILED`。
4. 检查文档统计和日志。

后续摄入是增量的：Connector 会检测新增、修改和删除的内容。S3 Versioning
允许回滚误覆盖，Connector 的删除保护可以防止大规模源文件删除立即移除多数
已索引内容。

## 配置边界

创建 Knowledge Base 时确定：

- Managed 或 Self-managed Storage。
- Managed 或 Custom Embedding Model。
- Managed Vector Store 可选的 KMS Key。

创建 Data Source 时确定：

- Connector 类型和连接参数。
- Parsing 策略。Managed Knowledge Base 仅支持 `SMART_PARSING`。
- Chunking 策略。使用 Managed Embedding 时，当前 API 强制使用服务默认值；
  在允许配置的模式下，修改该策略需要创建新的 Data Source。
- 图片、音频和视频的 Media Extraction。

每次 Runtime Retrieval 可以调整：

- `numberOfResults`：1-100；增加结果数可以提高 Recall，但也会增加延迟。
- `rerankingModelType`：`MANAGED`、`CUSTOM` 或 `NONE`。
- Metadata Filter：精确、范围、列表和逻辑组合。
- Guardrail 配置。
- User Context。

Managed Knowledge Base 不支持传统 `RetrieveAndGenerate` API。应使用以下模式：

- `Retrieve` 后调用 `Converse`，完整控制 Prompt 和引用格式。本仓库执行了该路径。
- 使用 `AgenticRetrieveStream` 执行多步 Query Planning、迭代检索、充分性评估
  和流式 Trace。这要求 SDK 包含 2026 API；本仓库在 `requirements.txt` 中固定
  了经过验证的 SDK 版本。

`Converse` 阶段可以控制生成模型、Prompt、`maxTokens`、Temperature、Top-p
和引用格式。`AgenticRetrieveStream` 则控制 Planning Model、Retriever 列表、
最大结果数和 Agent 迭代上限。

`maxAgentIteration` 是上限，不是必须执行的轮数。两次修复后的 Agentic 回归都
完成了 Speculative Retrieval 和 Planning，但返回 `actions=[]`。因此，即使
索引健康，宽泛问题也可能遗漏相关章节。覆盖度敏感的评估应使用定向子查询或
更高的结果数量。

## 更新与治理策略

- 按安全或所有权边界拆分 Data Source 或 Knowledge Base。
- 将 `classification`、`document_id`、Owner、Version 和 Effective Date
  保存为可过滤 Metadata。
- 在应用层使用 Metadata Filter 强制授权；S3 源权限不会自动保留到检索层。
- 将源文件更新视为受审查的发布：上传、校验 Checksum、同步、运行检索回归，
  然后再切换生产流量。
- 为 `Retrieve` 和 `AgenticRetrieveStream` 启用 CloudTrail Data Events；
  KB 和摄入变更默认属于 Management Events。
- 将摄入日志投递到 CloudWatch Logs 或 S3，并对失败 Job 告警。
- 受监管数据使用 Customer-managed KMS Key，并在摄入前清理 PII/PHI。
  Guardrail 不会清理 API 返回的原始 Retrieved References。
- Service Role 使用限定到精确 Bucket Prefix 的最小权限，并在创建后将 Trust
  Policy 收紧到精确 KB ARN。
- 定期执行陈旧文档检查、权限复核、检索质量评测、成本审计和删除恢复演练。

## 许可证

项目代码和原创文档采用 [MIT License](LICENSE)。项目级 Skill 所引用的上游
方法论保留其各自的版权和归属说明。AWS 服务名称和官方文档内容的权利归其各自
权利人所有。
