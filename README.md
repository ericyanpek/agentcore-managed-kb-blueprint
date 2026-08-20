# Amazon Bedrock Managed Knowledge Base：摄入、调优、检索、评测

**中文（默认）** | [English](README.en.md)

本仓库回答四个问题，每个都有实测数字或可运行代码支撑：

| # | 问题 | 在哪里 |
| --- | --- | --- |
| 1 | 知识怎么进 KB，增量更新怎么做对 | [摄入 Pipeline](#3-摄入-pipeline) · `kbp/preparation` `kbp/ingestion` |
| 2 | Managed KB 有哪些参数可调，哪些不可改 | [参数控制边界](#4-参数控制边界) |
| 3 | 入库后检索怎么优化 | [检索优化](#5-检索优化) · `docs/METADATA_EXPERIMENT.md` |
| 4 | 凭什么决定这版 KB 上线还是回退 | [评测与发布判据](#6-评测与发布判据) · `scripts/20` |

> **责任边界：** Managed KB 托管解析、Embedding、索引、存储和检索。事实源、
> 授权、质量评测和发布决策仍归调用方。这条边界决定了下面每一节的分工。

语料使用 AWS Well-Architected 游戏行业视角白皮书，实测于 2026-08 完成。

发布流水线的基础设施实现（CDK、Step Functions、DynamoDB 原子指针）见
[平台工程实现](#7-平台工程实现)——那是第 1 和第 4 项的执行载体，不是本仓库的重点。

## 1. 先看结论

| 问题 | 本项目结论 |
| --- | --- |
| 50 个 Markdown 后续只修改 5 个，会更新多少？ | 本地重新扫描全部 50 个并比较 Manifest；S3 和 Direct Ingestion 只处理变化的 5 个。 |
| AWS 官方 PDF 能否直接摄入？ | 中文 PDF 经 Smart Parsing 后出现大量 CJK 丢失；预抽取为 UTF-8 Markdown 后恢复。 |
| Semantic Chunking 是否一定更好？ | 结构感知预分块提高 Top-10 证据覆盖和溯源，但 MRR 下降；保留为 Canary，不替换基线。 |
| Metadata 是否提升召回？ | 未测得 Metadata Embedding 增益；Runtime Filter 将 36 条可过滤查询的 MRR 从 0.241 提升到 0.556。 |
| Agentic Retrieval 是否必然追加检索？ | 否。`maxAgentIteration` 是上限；`actions=[]` 表示 Planner 未触发后续动作，不等于系统异常。 |
| Managed Embedding 能否自定义 Chunk 大小？ | 当前实测 API 要求省略 `chunkingConfiguration`，使用服务默认 300 Tokens / 20% Overlap。 |
| 凭什么判断新版本 KB 可以上线？ | 固定 Golden Set 的配对检索比较：四项指标 95% 置信区间下界均 > -0.02 才上线，任一指标 CI 完全落在负区间即回退。 |

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
cd infra && npm ci && npx cdk deploy --all --require-approval never && cd ..
./scripts/03_ingest.sh
./scripts/04_test_retrieval.sh
./scripts/06_quality_diagnostics.sh
./scripts/07_verify.sh
```

基础路径会创建测试资源、摄入原始 PDF 并验证检索。流式 Agentic Retrieval
实现位于 `scripts/08_agentic_retrieval.py`。

## 3. 摄入 Pipeline

### 3.1 四层状态，缺一层就说不清"新内容是否已生效"

```text
Git / CMS             system of record
  -> S3 Canonical     candidate copy the connector can see
  -> Published Manifest   approved release contract
  -> KB Index         derived index from the last successful ingestion
```

一致性条件是三者对齐：`S3 期望版本 == Manifest 版本 == KB 已索引版本`。
异步摄入期间可以短暂不一致，所以**只改 S3 不能证明新内容可检索**，
必须轮询文档终态。

### 3.2 增量的含义：云端写入增量，不是跳过本地扫描

50 篇文档改了 5 篇时，本地仍重新扫描全部 50 篇——只有这样才能可靠发现删除、
重复 `document_id` 和 metadata-only 变更。云端只上传并摄入变化的 5 篇。

```bash
.venv/bin/python -m cli.publish \
  --source-dir examples/corpus \
  --corpus-id demo \
  --canonical-bucket <canonical-bucket> \
  --registry-bucket <registry-bucket> \
  --knowledge-base-id <knowledge-base-id> \
  --data-source-id <data-source-id> \
  --state-machine-arn <state-machine-arn> \
  --release-table <release-table> \
  --source-commit "$(git rev-parse HEAD)" \
  --dry-run
```

`kbp/preparation/corpus.py` 做准备与质量门禁，`kbp/preparation/diff.py` 比对
Manifest 得出 added/modified/deleted，`kbp/ingestion/batching.py` 切批并绑定
sidecar。去掉 `--dry-run` 触发真实执行。

### 3.3 准备阶段的质量门禁

任一失败即拒绝发布，因为这些问题进了索引就只能靠重建修复：

| 门禁 | 为什么 |
| --- | --- |
| 非空正文 | 空文档产生无意义向量 |
| 无 `U+FFFD` | 中文 PDF 经 Smart Parsing 后大量 CJK 丢失，见 3.4 |
| UTF-8 BOM 剥离 | BOM 会让 front matter 整块失效且不报错，`document_id` 与治理字段静默退回默认值 |
| 日期字段必须完整 | `2026-08` 会变成数值 202608，与 8 位日期比较时排序错误，且同字段混用两种类型会破坏 metadata filter |
| `document_id` 唯一 | 重复标识相互覆盖 |
| 单文档 ≤ 30 MB / sidecar ≤ 10 KB | Managed KB 配额 |

### 3.4 中文 PDF 必须预抽取

原始中文 PDF 经 `SMART_PARSING` 后非图片分块大量丢失 CJK 字符，
sidecar 声明 `language=zh-CN` 而系统字段 `_language_code` 全部回落为 `en`。
预抽取为 UTF-8 Markdown 后，四组定向检索 Top score 从 0.456-0.570 升到
0.619-0.735，`U+FFFD` 归零。

```bash
./scripts/09_prepare_text_repair.sh
./scripts/10_ingest_text_repair.sh
./scripts/11_test_text_repair.sh
```

企业 Markdown 语料天然位于修复后状态，工程重心因此不在解析质量，
而在更新编排与发布门禁。详见
[企业 Markdown Pipeline](docs/MD_CORPUS_PIPELINE.md)。

## 4. 参数控制边界

Managed KB 的调优空间比 Classic KB 窄得多。分清"创建后不可改"和"每次检索可调"
是选型和调优的前提。

### 4.1 创建时决定，之后不可改

| 参数 | 取值 | 改动代价 |
| --- | --- | --- |
| `KnowledgeBaseConfiguration` | `type: MANAGED` | **createOnly**，改动会替换 KB 并丢失整个索引 |
| `embeddingModelType` | `MANAGED` / `CUSTOM` | 同上 |
| `serverSideEncryptionConfiguration.kmsKeyArn` | CMK ARN | 同上 |
| Data Source `type` | Managed KB 只接受 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` | createOnly |
| Chunking 策略 | Managed Embedding 下须省略 `chunkingConfiguration` | createOnly |

**`type: 'S3'` 的 Data Source 会被 Managed KB 直接拒绝**
（`Unsupported data source type for MANAGED knowledge base type`）。
CloudFormation schema 语法上允许这个组合，服务端语义拒绝——只有真机部署能发现。
桶与前缀写在 `connectorParameters` 这个 JSON 文档里，不是 `s3Configuration`：

```json
{"type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
 "managedKnowledgeBaseConnectorConfiguration": {
   "connectorParameters": {
     "type": "S3", "version": "1",
     "connectionConfiguration": {"bucketName": "…"},
     "filterConfiguration": {"inclusionPrefixes": ["canonical/demo/"]}}}}
```

### 4.2 Chunking 与 Embedding：几乎没有旋钮

Managed Embedding 当前使用服务默认的 300 Tokens / 20% Overlap，
实测 API 要求省略 `chunkingConfiguration`。需要控制分块就只有两条路：
改用 `CUSTOM` Embedding，或在**摄入前**自己预分块
（见[语义分块实验](docs/SEMANTIC_CHUNKING_EXPERIMENT.md)，结论是收益局部）。

解析同样只有 `SMART_PARSING`，没有替代项——这正是中文 PDF 必须预抽取的原因。

### 4.3 删除保护：只管 sync job，管不到直接删除

`deletionProtectionThreshold` 是**百分比**（0–100），语义是"一次 sync job 最多
允许删除索引中多大比例的文档"。超限时**跳过删除阶段**而非失败。

两个后果容易误判：

- 它不约束 `DeleteKnowledgeBaseDocuments`。走直接删除的发布流水线**完全不受它保护**。
- "跳过而非失败"意味着它是提示而非阻断。

所以本仓库的删除比例门禁（[第 6 节](#6-评测与发布判据)）是直接删除路径上的
唯一控制，不是"第二道防线"。

### 4.4 每次检索可调

| 参数 | 范围 | 影响 |
| --- | --- | --- |
| `numberOfResults` | 1–100 | 召回覆盖 vs 上下文成本 |
| `rerankingModelType` | `MANAGED` / `CUSTOM` / `NONE` | 见[第 5 节](#5-检索优化)实测 |
| `filter` | Metadata 表达式 | 缩小候选集，实测增益最大的一项 |
| `userContext` / Guardrail | — | 授权与内容控制 |

Managed KB 检索必须用 `managedSearchConfiguration`。
用 `vectorSearchConfiguration` 会**静默返回零命中**而不报错。

### 4.5 配额中最值得注意的三条

| 配额 | Managed KB | 与 Classic 的差别 |
| --- | ---: | --- |
| 并发 ingestion job / KB | 50 | Classic 是 1 |
| Data Source / KB | 200 | Classic 是 5 |
| `IngestKnowledgeBaseDocuments` 文档数 / 请求 | **10** | 用户指南写 25，那是其他 KB 类型；提交 11 个会被服务端拒绝 |

Managed KB 配额表**没有列出 `StartIngestionJob` 速率限制**。Classic 的
0.1 rps 不适用于 Managed，把它照搬过来会导致不必要的串行化。

### 4.6 责任边界

```text
S3 / CMS / Git (system of record)
  -> Data preparation + metadata + approval
  -> Managed Connector / Direct Ingestion
  -> Managed parsing + embedding + index + retrieval
  -> Application / Gateway (authentication, authorization, generation, citations)
```

Metadata 的权威副本应保存在事实源旁的 Sidecar；Managed KB 中的 Vector Index 是
可重建的派生状态。Gateway Tool 授权、KB Filter 和最终用户授权是三层不同控制，
不能相互替代。

## 5. 检索优化

以下都是同一份语料（479 份字节相同的文档）、44 条查询、408 次 `Retrieve`、
两种 Rerank 模式的对照实测。完整数字见
[Metadata 对照实验](docs/METADATA_EXPERIMENT.md)。

### 5.1 增益最大的一项：Runtime Metadata Filter

| 手段 | 效果 |
| --- | --- |
| Metadata 参与 Embedding | **未测得增益**（未过滤召回下） |
| Runtime Filter | 36 条可过滤查询 MRR **0.241 → 0.556**，Recall@10 **0.078 → 0.342** |
| 按稳定控制编号定位章节 | MRR 达到 **1.000** |

结论直接决定 metadata 设计：让检索时能过滤的字段（`domain`、`topic`、
`section_path`）成为一等公民，而不是指望把 metadata 塞进 Embedding。

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

### 5.2 Metadata 字段策略

`kbp/preparation/corpus.py` 的默认分工来自上面的实测：

- 治理与授权字段（`document_id`、`classification`、`owner`、`lifecycle_status`、
  `content_sha256`、`source_path`）一律 `includeForEmbedding=false`。
- 只有 `title`、`section_path`、`domain`、`topic` 参与 Embedding。
- 目录层级自动映射为 `domain`/`topic`/`section_path`，让运行时过滤有稳定业务键
  可用——这是 5.1 那组增益的前提。

两条安全边界必须在应用层落实：Filter 只缩小候选集，**不保证返回结果**；
权限 Filter 命中空集时必须 Fail closed。持有 `bedrock:Retrieve` 的调用者
能看到全部已摄入内容，S3 对象权限不会自动成为检索层权限。

### 5.3 预分块：收益局部，不足以替换基线

结构感知预分块提高 Top-10 证据覆盖和溯源能力，但 **MRR 下降**。
因此保留为 Canary 而非默认。设计与数字见
[语义分块实验](docs/SEMANTIC_CHUNKING_EXPERIMENT.md)。

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

### 5.4 两个会误判覆盖度的陷阱

**`actions=[]` 不等于异常。** Agentic Retrieval 中 `maxAgentIteration` 是上限而非
保证。实测里 Planning 返回 `actions=[]` 未触发第二轮检索，结果是宽泛问题漏掉了
跨章节内容——同样内容用定向 `Retrieve` 能命中。覆盖度敏感的评估要用定向子查询
或提高结果数量，不能只用一次宽泛提问下结论。

**"文档里没有"不等于语料没有。** 首轮反作弊测试得到措辞合理的"文档不支持"回答，
后续诊断证明源文档确实包含相关内容，根因是 Smart Parsing 破坏了中文分块。
Grounded failure 只能作为索引状态的证据，不能作为语料覆盖范围的结论。

## 6. 评测与发布判据

发布决策要回答一个问题：**这一版 KB 的检索质量是否不差于上一版。**
Source Diff 和 Manifest Diff 都答不了——Managed KB 不暴露可比较的底层向量索引，
分块、Embedding 和排序的行为变化只能通过检索回归观察。

### 6.1 三种 Diff，缺一不可

| Diff | 回答什么 | 怎么算 |
| --- | --- | --- |
| Source Diff | 作者改了什么 | Git Diff；PDF 用对象 SHA-256 |
| Release Diff | 哪些文档该增改删 | 比较前后 Manifest 的内容与 metadata SHA-256 |
| **Retrieval Diff** | **新版本改变了哪些召回结果** | 固定 Golden Set 配对比较 |

### 6.2 判据指标

`scripts/20_expand_metadata_retrieval.py` 已实现配对比较，
每个指标输出均值差、bootstrap 95% 置信区间（5000 次重采样，固定 seed 可复现），
以及逐用例的 improved / tied / regressed 计数：

| 指标 | 代码字段 | 关注什么 |
| --- | --- | --- |
| Hit@1 | `hitAt1` | 首条是否命中，对直接问答影响最大 |
| MRR | `reciprocalRank` | 正确答案的排序位置 |
| Recall@10 | `recallAt10` | 覆盖度，对需要多段证据的问题关键 |
| nDCG@10 | `nDcgAt10` | 兼顾命中与位置的综合排序质量 |

### 6.3 上线还是回退

用配对比较而非绝对值——同一组查询在两个版本上跑，看差值分布：

| 情形 | 判据 | 动作 |
| --- | --- | --- |
| 无显著退化 | 四项指标的 95% CI 下界均 > -0.02 | **上线** |
| 有显著退化 | 任一指标 95% CI **完全落在负区间** | **回退** |
| 结果不确定 | CI 跨越 0 且均值差 < -0.02 | 扩大 Golden Set 重测，不凭这组数据发布 |

**为什么看 CI 而不只看均值差**：44 条查询的样本量下，均值差 -0.03 可能只是噪声。
CI 完全落在负区间才说明退化是系统性的。反过来，CI 跨越 0 时"看起来变好了"
同样不可信。

阈值 -0.02 是本仓库语料规模下的起点，不是普适值。Golden Set 越大越可以收紧。

### 6.4 发布前的完整门禁顺序

破坏性操作必须在门禁之后。这个顺序是实测教训——早期实现把删除放在门禁之前，
一次超限删除已经删掉 8/13 篇文档才在门禁失败：

```text
1. review source-of-record change    (a Git PR is the approval trail)
2. preparation gates                 encoding, empty body, date type, unique id
3. deletion-ratio gate               <-- must precede any deletion
4. ingest                            direct channel for upserts, Delete API for removals
5. document terminal status          only INDEXED counts as success
6. retrieval diff                    paired comparison on the golden set
7. ACL regression                    zero cross-tenant leakage
8. manifest promotion                conditional write, atomic
```

第 5 步值得单独强调：`DocumentStatus` 有 12 个值，**只有 `INDEXED` 是完全成功**。
`PARTIALLY_INDEXED` 意味着部分分块失败——内容不完整但 API 不报错，
判为成功等于静默数据损坏。

### 6.5 不同变更类型的回归范围

| 变更 | 必须执行 |
| --- | --- |
| 改几个文档 | 全量准备检查；受影响 Golden Queries；全局关键 Smoke；ACL |
| 只改 Metadata/ACL | Filter 正负测试、跨租户泄漏、字段类型与缺失测试 |
| 删除文档 | stale-content 排除、引用失效、删除与权限回归 |
| 更新完整 PDF | 该 PDF 全部相关 Golden Queries；全局 Smoke；解析与 Citation 抽样 |
| Parser/Chunking/Embedding 变化 | 完整 Golden Set、Latency/Cost、A/B 与回滚演练 |

### 6.6 回滚

Manifest 记录每个文档的 `s3VersionId`，所以回滚是版本精确的：
读旧 Manifest → 恢复对应 S3 版本 → 重新摄入 → 条件写切回指针。
被替换的版本保留为 `SUPERSEDED` 而非删除，观察期内可快速回退。

## 7. 平台工程实现

**这一节是前六节的执行载体，不是本仓库的重点。** 只想理解摄入、调优、检索和
评测的读者可以跳过。之所以做到这个程度，是因为第 6.4 节的门禁顺序如果靠代码
自觉去保证，迟早会有人绕过——把它编码成状态机拓扑就绕不过去。

### 7.1 三个 Stack 与发布状态机

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| Foundation Stack | `infra/lib/foundation-stack.ts` | Canonical 桶、Registry 桶、Release 表 |
| KB Stack | `infra/lib/knowledge-base-stack.ts` | Managed KB 与 Connector 数据源 |
| Release Stack | `infra/lib/release-stack.ts` | 门禁 Lambda 与状态机 |
| 状态机 | `infra/lib/state-machine.ts` | 门禁编排，失败即 fail-closed |
| 门禁纯函数 | `kbp/ingestion/gates.py` | 所有判定逻辑，可单测 |
| 发布 CLI | `cli/publish.py` | 准备、上传、启动执行 |

**Fail-closed 靠拓扑而非纪律**：每个 Choice 状态的非通过分支直接指向终态 Fail，
没有"记录警告后继续"的路径。指针推进用 DynamoDB 条件写，`expectedPreviousReleaseId`
锁定执行开始时观察到的指针；并发发布中败者在 `PromoteRelease` 被拒。

设计取舍见 [ADR 目录](docs/adr/)，端到端验收记录见
[`tests/integration/test_release_pipeline.md`](tests/integration/test_release_pipeline.md)
（四条路径：正常发布、损坏文档阻断、超限删除硬失败、并发条件写拒绝）。

### 7.2 延伸主题（已移出主线）

这些内容对前六节不是必需的，按需查阅：

| 主题 | 文档 |
| --- | --- |
| 选型：Managed vs Classic vs 自建 | [KB 平台选型](docs/KB_PLATFORM_SELECTION_GUIDE.md) |
| AWS 官方 RAG 最佳实践梳理 | [最佳实践报告](docs/AWS_KB_RAG_BEST_PRACTICES.md) |
| 企业治理与审批模型 | [企业治理蓝图](docs/ENTERPRISE_GOVERNANCE_BLUEPRINT.md) |
| 控制项清单 | [控制基线](docs/CONTROL_BASELINE.md) |
| 可观测性与事件模型 | [可观测性蓝图](docs/OBSERVABILITY_BLUEPRINT.md) · [事件 Schema](schemas/observability-event.schema.json) · [证据模板](experiments/observability-evidence.template.md) |
| 企业场景实验路线 | [实验目录](experiments/README.md) |
| 数据准备 Skill | [kb-rag-data-preparation](.agents/skills/kb-rag-data-preparation/SKILL.md) |
| 早期阶段交接记录 | [Handoff Report](HANDOFF_REPORT.md) |

### 7.3 官方基线

复核于 2026-08-04，上线前必须按目标账户和 Region 重新确认：

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

### 7.4 运行证据与安全

运行响应保存在被 Git 忽略的 `artifacts/<RUN_ID>/`，可能包含账户 ID、ARN、桶名、
资源 ID、源路径或检索内容，一律不提交。发布前执行：

```bash
./scripts/12_repository_safety_check.sh
python3 scripts/13_check_readme_sync.py
```

中英文 README 必须在同一次变更中同步更新；CI 会检查链接、章节和命令块。
完整策略见 [SECURITY.md](SECURITY.md)。

## 8. 已知限制

- 原始中文 PDF 不适合作为当前生产 Corpus，应使用修复后的 Markdown 版本。
- Semantic Chunking 只有局部收益，尚未达到替换基线的发布门槛。
- Metadata Filter 能显著缩小候选范围，但不能替代认证或保证一定返回结果。
- Transaction Search、vended log delivery 和应用 ADOT 必须按账户、Region 和资源
  分别验证，不能仅检查 Console 页面。
- **没有独立的漂移检测**：被篡改的 S3 对象只有在下一次发布的 Manifest 覆盖到它时
  才会被门禁 A 发现。
- Golden Set 目前 44 条查询，样本量偏小；第 6.3 节的 -0.02 阈值随之偏保守。
- Managed KB 创建实测耗时约 24 分钟，远超官方文档所述的 2–5 分钟，CI 超时不应按
  5 分钟设定。
- 本仓库不包含 AWS 凭据、真实账户证据或客户数据，也不会自动部署生产资源。

## 9. 清理与许可证

清理脚本要求显式确认：

```bash
CONFIRM_DESTROY=DELETE-games-industry-lens-managed-kb-20260803 \
  ./scripts/99_cleanup.sh
```

项目代码和原创文档采用 [MIT License](LICENSE)。AWS 服务名称和官方文档内容的
权利归其各自权利人所有。
