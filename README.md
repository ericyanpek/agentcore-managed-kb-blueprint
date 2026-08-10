# AgentCore Managed Knowledge Base 治理与实测蓝图

**中文（默认）** | [English](README.en.md)

面向 AWS Solutions Architect、AI Platform 和 RAG 工程团队的可复现项目：使用
AWS Well-Architected 游戏行业视角白皮书，验证 Amazon Bedrock AgentCore
Managed Knowledge Base 的数据准备、摄入、检索、Agentic Retrieval、Metadata、
更新和企业治理能力。

> **核心定位：** Managed KB 托管解析、Embedding、索引、存储和检索基础设施，
> 但事实源、最终授权、质量评测、发布治理和应用生成仍由客户负责。

本仓库同时提供三类资产：

- **实测证据**：记录成功、失败、负面结果和可复现参数。
- **工程工具**：从 PDF 修复、分块、Metadata 到增量摄入和回归测试。
- **治理蓝图**：控制基线、实验路线、可观测性和发布门禁。

## 1. 先看结论

| 问题 | 本项目结论 |
| --- | --- |
| 50 个 Markdown 后续只修改 5 个，会更新多少？ | 本地重新扫描全部 50 个并比较 Manifest；S3 和 Direct Ingestion 只处理变化的 5 个。 |
| AWS 官方 PDF 能否直接摄入？ | 中文 PDF 经 Smart Parsing 后出现大量 CJK 丢失；预抽取为 UTF-8 Markdown 后恢复。 |
| Semantic Chunking 是否一定更好？ | 结构感知预分块提高 Top-10 证据覆盖和溯源，但 MRR 下降；保留为 Canary，不替换基线。 |
| Metadata 是否提升召回？ | 未测得 Metadata Embedding 增益；Runtime Filter 将 36 条可过滤查询的 MRR 从 0.241 提升到 0.556。 |
| Agentic Retrieval 是否必然追加检索？ | 否。`maxAgentIteration` 是上限；`actions=[]` 表示 Planner 未触发后续动作，不等于系统异常。 |
| Managed Embedding 能否自定义 Chunk 大小？ | 当前实测 API 要求省略 `chunkingConfiguration`，使用服务默认 300 Tokens / 20% Overlap。 |
| 当前更新 Pipeline 能否直接生产使用？ | 不能。它是研究型 MVP，尚缺最终状态轮询、定向删除和原子 Manifest Promotion。 |

完整数字、查询案例和证据见[实测报告](docs/RESULTS.md)。

## 2. 快速开始

前提：Python 3.12、AWS CLI、目标 Region 的 AWS 临时凭证，以及创建测试资源的
最小权限。执行会产生 AWS 费用；请先使用 Sandbox 账户和预算告警。

```bash
cp config/test.env.example config/test.env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
aws sts get-caller-identity
```

修改被 Git 忽略的 `config/test.env` 后，运行基础实验：

```bash
./scripts/01_prepare_source.sh
./scripts/02_provision.sh
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

基础路径会创建测试资源、摄入原始 PDF 并验证检索。流式 Agentic Retrieval
实现位于 `scripts/08_agentic_retrieval.py`。

## 3. 实验复现

### 3.1 中文 PDF 修复

将 PDF 预抽取为带物理页标记的 UTF-8 Markdown，再通过独立 Data Source 摄入：

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

质量门禁包括 146 页、无 Unicode replacement character、最多 1 个空文本页，
以及至少 50% CJK 字符比例。

### 3.2 结构感知分块

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

设计与结果见[语义分块对照实验](docs/SEMANTIC_CHUNKING_EXPERIMENT.md)。

### 3.3 Metadata 对照

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

实验覆盖 44 条查询、两种 Rerank 模式和 408 次 Retrieve。结果与字段治理见
[Metadata 对照实验](docs/METADATA_EXPERIMENT.md)。

### 3.4 企业 Markdown 增量摄入

```bash
PYTHON_BIN=python3.12 ./scripts/21_prepare_md_corpus.sh
DRY_RUN=1 PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
PYTHON_BIN=python3.12 ./scripts/22_incremental_ingest.sh
```

该流程通过 Manifest 识别 added/modified/deleted，并区分 Direct Ingestion 与
Connector 对账。当前生产差距见
[企业 Markdown Pipeline](docs/MD_CORPUS_PIPELINE.md)。

## 4. 架构与配置边界

### 创建 Knowledge Base 时

- 选择 Managed 或 Self-managed Storage。
- 选择 Managed 或 Custom Embedding Model。
- 为 Managed Vector Store 选择可选的 KMS Key。

### 创建 Data Source 时

- 确定 Connector、连接参数和删除策略。
- Managed KB 仅支持 `SMART_PARSING`。
- Chunking 策略创建后不可变；Managed Embedding 当前使用服务默认值。
- 可配置图片、音频和视频的 Media Extraction。

### 每次检索时

- `numberOfResults`：1-100。
- `rerankingModelType`：`MANAGED`、`CUSTOM` 或 `NONE`。
- Metadata Filter、Guardrail 和 User Context。
- `Retrieve` 后调用 `Converse`，或使用 `AgenticRetrieveStream`。

### 数据与责任边界

```text
S3 / CMS / Git (system of record)
  -> Data preparation + metadata + approval
  -> Managed Connector / Direct Ingestion
  -> Managed parsing + embedding + index + retrieval
  -> Application / Gateway (authentication, authorization, generation, citations)
```

Metadata 的权威副本应保存在事实源旁的 Sidecar 或内容系统；Managed KB 中的
Vector Index 是可重建的派生状态。Gateway Tool 授权、KB Filter 和最终用户授权
是三层不同控制，不能相互替代。

## 5. 治理与文档导航

### 决策与实测

| 文档 | 用途 |
| --- | --- |
| [实测报告](docs/RESULTS.md) | 摄入、检索、Agentic Retrieval、质量问题与治理结论 |
| [平台选型指南](docs/KB_PLATFORM_SELECTION_GUIDE.md) | AWS、Azure、GCP、ISV 与自建向量数据库对比 |
| [AWS KB/RAG 最佳实践](docs/AWS_KB_RAG_BEST_PRACTICES.md) | 质量、更新、权限、性能、成本与 RAGOps |
| [AWS 官方样例目录](docs/AWS_SAMPLE_CATALOG.md) | 固定 Sample SHA、能力映射与生产化差距 |

### 企业治理

| 文档 | 用途 |
| --- | --- |
| [企业治理蓝图](docs/ENTERPRISE_GOVERNANCE_BLUEPRINT.md) | 账户、Region、租户、IAM、网络、数据和 Gateway 契约 |
| [最低控制基线](docs/CONTROL_BASELINE.md) | `MUST/SHOULD` 控制、证据、风险等级、例外和发布门禁 |
| [企业实验路线](experiments/README.md) | E00-E07 正负测试、成本、Cleanup 和 ADR |
| [可观测性蓝图](docs/OBSERVABILITY_BLUEPRINT.md) | Metrics、Logs、Traces、ADOT、Transaction Search 和长期分析 |
| [Handoff Report](HANDOFF_REPORT.md) | 未验证假设、跨服务待统一项和后续工作 |

### 可复用资产

| 资产 | 用途 |
| --- | --- |
| [KB/RAG 数据准备 Skill](.agents/skills/kb-rag-data-preparation/SKILL.md) | 解析、分块、Metadata、摄入、评测、发布与回滚方法 |
| [Observability Evidence 模板](experiments/observability-evidence.template.md) | 每次实验的成功/失败和三信号证据 |
| [Observability Event Schema](schemas/observability-event.schema.json) | 长期脱敏分析事件契约 |
| [安全与发布规范](SECURITY.md) | GitHub 发布边界和脱敏要求 |

## 6. 运行证据与安全

运行响应保存在被 Git 忽略的 `artifacts/<RUN_ID>/`：

| 目录 | 内容 |
| --- | --- |
| `source/` | 原始资料、Checksum、Canonical Markdown 和 Metadata |
| `aws/` | 资源描述、IAM 和 Ingestion Job 证据 |
| `tests/` | 检索事件、对照实验结果和精简报告 |
| `state.env` | 本次运行生成的非 Secret 资源标识 |

这些证据仍可能包含账户 ID、ARN、桶名、资源 ID、源路径或检索内容，不应提交。
发布前执行：

```bash
./scripts/12_repository_safety_check.sh
python3 scripts/13_check_readme_sync.py
```

中英文 README 必须在同一次变更中同步更新；CI 会检查链接、章节和命令块。

## 7. 当前官方基线

以下信息复核于 2026-08-04，上线前必须按目标账户和 Region 重新确认：

- 已公布 Region 包括 `us-east-1`、`us-west-2`、`eu-west-1`、
  `eu-central-1`、`eu-west-2`、`ap-southeast-2`、`ap-northeast-1`
  和 `us-gov-west-1`。
- 默认配额包括每账户/Region 10,000 个 KB、每 KB 200 个 Data Source、
  50 个并发 Ingestion Job、10 TB Raw Data、每 KB 每分钟 600 次 Retrieve，
  以及每账户每分钟 60 次 Agentic Retrieve。
- 美国 Region 价格示例：Index Storage `$5/GB-month`、Standard Retrieve
  `$1/1,000 calls`、Agentic Retrieve `$4/1,000 calls`；Agentic 内部 Retrieve
  仍单独计费。
- CloudFormation/CDK L1 支持 `ManagedKnowledgeBaseConfiguration`。

官方来源、发布日期和固定 Sample Commit 见
[AWS 官方样例目录](docs/AWS_SAMPLE_CATALOG.md)。

## 8. 已知限制

- 原始中文 PDF 不适合作为当前生产 Corpus，应使用修复后的 Markdown 版本。
- Semantic Chunking 只有局部收益，尚未达到替换基线的发布门槛。
- Metadata Filter 能显著缩小候选范围，但不能替代认证或保证一定返回结果。
- `scripts/21` 至 `23` 仍是研究实现，不能作为生产发布状态机。
- Transaction Search、vended log delivery 和应用 ADOT 必须按账户、Region 和
  资源分别验证，不能仅检查 Console 页面。
- 本仓库不包含 AWS 凭据、真实账户证据或客户数据，也不会自动部署生产资源。

## 9. 清理与许可证

清理脚本要求显式确认：

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

项目代码和原创文档采用 [MIT License](LICENSE)。AWS 服务名称和官方文档内容的
权利归其各自权利人所有。
