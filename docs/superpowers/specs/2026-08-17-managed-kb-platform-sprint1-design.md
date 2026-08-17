# Managed KB 平台化 Sprint 1 设计

日期：2026-08-17

## 1. 目标与边界

将本仓库从研究型脚本集合升级为平台工程基线的第一个 Sprint。交付四项资产：

1. **假设探针**：验证 A1（`StartIngestionJob` 对 Managed KB 是否受 0.1 rps 约束）与
   A2（对账 Sync 是否移除仅通过 Direct API 摄入的文档），先于状态机实现执行。
2. **CDK 基础设施**：取代 `scripts/02_provision.sh`，管理 KMS、S3、IAM、
   Managed KB、Data Source 与发布层资源。
3. **Release Registry**：版本化 S3 存储不可变 Manifest，DynamoDB 存储发布状态与
   活动版本指针，条件写实现原子 Promotion。
4. **Fail-closed 发布状态机**：Step Functions Standard，四道门禁全部前置于
   Promotion，任一失败不污染活动版本。

### 1.1 交付性质

**可部署的参考实现**：代码在真实沙箱账户中可跑通 deploy 与一次完整发布，验证架构
正确性。不承担多租户 SLA、7x24 运维与成本优化等生产责任。

### 1.2 明确不在本次范围

| 项目 | 原因 |
| --- | --- |
| A/B 双 Slot 发布与回滚命令 | 数据模型已留位，命令留待后续 Sprint |
| AgentCore Gateway 与身份映射 | 依赖尚未确定的 Identity 方案（见 HANDOFF 第 4 节待统一项） |
| `kbctl` CLI | 本次以 `cli/publish.py` 入口脚本替代，后续演进为子命令 |
| Golden Set 接入为发布门禁 | 本次门禁为冒烟检索，非完整检索质量回归 |
| 周期性对账 Sync | 删除改由 Direct Delete API 承担，对账退为后续能力 |
| 漂移检测、DR、Runbook | 原计划 Sprint 5 内容 |
| 多语料并发发布 | Registry 主键已留位，语义设计留待后续 |

## 2. 已核实的 API 事实

以下事实经 botocore 1.42.94 服务模型、CloudFormation 资源 Schema 与
aws-cdk-lib 2.265.0 类型定义核实（核实日期 2026-08-17），直接约束设计决策。

| 事实 | 对设计的影响 |
| --- | --- |
| `DocumentStatus` 有 12 个枚举值，仅 `INDEXED` 表示完全成功 | `PARTIALLY_INDEXED`、`METADATA_PARTIALLY_INDEXED`、`METADATA_UPDATE_FAILED`、`IGNORED` 均判定为失败 |
| `DocumentMetadata.type` 支持 `S3_LOCATION` | 直接摄入可显式关联 sidecar，关闭 HANDOFF 第 4 项 |
| `clientToken` 约束为 33–256 字符，pattern `[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}` | 仅允许字母数字与连字符；下划线、点、斜杠非法，幂等键必须派生 |
| `KnowledgeBaseDocuments` 与 `DocumentIdentifiers` 列表上限均为 10 | 摄入与删除批次大小上限为 10 |
| CFN 中 `KnowledgeBaseConfiguration/ManagedKnowledgeBaseConfiguration` 为 createOnly | 修改 embedding 配置将替换 KB 并丢失索引，须隔离 Stack 并开启终止保护 |
| `AWS::Bedrock::DataSource` 的 `tagging.taggable` 为 `false` | 成本分账 Tag 只能施加于 KB 与 S3，不能施加于 Data Source |
| `aws-cdk-lib` 2.265.0 含 `ManagedKnowledgeBaseConfigurationProperty` 与 `ManagedKnowledgeBaseServerSideEncryptionConfigurationProperty` | L1 构造可表达 Managed KB 与 CMK 加密，无需自定义资源 |
| 检索侧 Managed KB 使用 `managedSearchConfiguration` | 探针与冒烟检索不得使用 `vectorSearchConfiguration` |
| `arn:aws:states:::aws-sdk:bedrockagent:{ingest,delete,get}KnowledgeBaseDocuments` 与 `bedrockagentruntime:retrieve` 均通过 `ValidateStateMachineDefinition` 校验 | 四个调用可用 Step Functions SDK 集成直接发起；冒烟检索无需 Lambda |

## 3. 执行模型选择

选择 **Step Functions + 最小 Lambda**：状态机通过 SDK 集成直接调用
`IngestKnowledgeBaseDocuments`、`DeleteKnowledgeBaseDocuments`、
`GetKnowledgeBaseDocuments` 与 `Retrieve`，轮询与重试使用原生
`Retry`/`Wait`/`Map`；仅 S3 一致性校验、门禁判定与 Registry 读写使用 Lambda（共 3 个）。

替代方案与落选理由：

| 方案 | 落选理由 |
| --- | --- |
| 单个 Python Orchestrator | Managed KB 文档摄入异步，需等每个文档达终态；较大语料的初次发布可能超过 Lambda 15 分钟上限。重试与幂等亦需自行实现 |
| Step Functions + 全 Lambda 任务 | Lambda 数量与 IAM 复杂度翻倍，而可测试性收益有限：需要测试的判定逻辑本来就在 Lambda 中 |

### 3.1 分层约束

**所有判定逻辑为纯函数。** 门禁算法接收数据结构、返回判定结果，不调用 boto3。
Lambda handler 仅做 I/O 适配：读事件 → 调 AWS → 交纯函数判定 → 写回结果。这使删除
比例、终态聚合与 Promotion 前置条件可由 pytest 直接覆盖，无需 AWS 环境。

**Fail-closed 由状态机拓扑保证，而非代码纪律保证。** 每道门禁是独立的 Choice
State，失败分支指向 `FailRelease`。从门禁到 Promotion 不存在绕行路径。

## 4. 仓库结构

```text
infra/                          # CDK TypeScript
  bin/app.ts
  lib/foundation-stack.ts       # KMS、S3 canonical、S3 registry、日志
  lib/knowledge-base-stack.ts   # Managed KB、Data Source、KB service role
  lib/release-stack.ts          # DynamoDB、Step Functions、Lambda、publisher role
  test/                         # snapshot 与细粒度断言

kbp/                            # knowledge base platform（不可命名为 platform，见 4.2）
  preparation/
    corpus.py                   # 扫描、质量门禁、Manifest 生成（纯函数）
    diff.py                     # Manifest 比对，输出 added/modified/deleted
  ingestion/
    batching.py                 # 变更集到批次计划
    gates.py                    # 删除比例、终态聚合、Promotion 前置（纯函数）
    handlers/                   # Lambda 入口，仅 I/O 适配
  registry/
    manifest.py                 # Manifest schema 与序列化
    store.py                    # DynamoDB 条件写、S3 Manifest 读写
  probes/                       # A1/A2 探针（重写 scripts/23）

cli/publish.py                  # 本地入口：准备、上传、StartExecution、轮询
schemas/release-manifest.schema.json
examples/corpus/                # 验收用固定小规模 Markdown 语料
tests/{unit,infrastructure,integration}/
docs/adr/
```

### 4.1 现有代码处置

| 现有资产 | 处置 |
| --- | --- |
| `scripts/21_prepare_md_corpus.py` | 算法迁入 `kbp/preparation/`，保留纯函数形态 |
| `scripts/22_incremental_ingest.py` | 批次规划迁入 `kbp/ingestion/batching.py`；执行部分由状态机取代 |
| `scripts/23_verify_assumptions.sh` | 重写为 `kbp/probes/`，修正三处错误：使用 Disposable Data Source 隔离并发变量、使用 S3 Payload 而非 `CUSTOM`、使用 `managedSearchConfiguration` |
| `scripts/02_provision.sh` | 删除，由 CDK 取代，避免两个 provisioning 真相并存 |
| `scripts/21_prepare_md_corpus.sh`、`scripts/22_incremental_ingest.sh` | 删除，由 `cli/publish.py` 取代 |
| `scripts/01`、`03`–`20`、`99` | 原地保留，作为已发布实验证据不做改动 |

README 中英文需同步更新命令块，并通过 `scripts/13_check_readme_sync.py` 校验。

### 4.2 包命名约束

顶层包**不得命名为 `platform`**。`platform` 是 Python 标准库模块，顶层同名包会遮蔽
它；botocore 在构造 User-Agent 时调用 `platform.system()`，遮蔽后任何从仓库根目录运行
的 boto3 代码都将抛出 `AttributeError`。已实测确认。故采用 `kbp`。

### 4.3 测试框架迁移

现有 CI 使用 `python -m unittest discover -s tests`，且 `tests/test_data_preparation.py`
通过 `importlib` 直接按路径加载 `scripts/21`、`scripts/22`——这两个路径在本次迁移后
消失。

处置：引入 pytest（可直接运行现存 `unittest.TestCase`，旧测试无需重写），CI 改为
`pytest`。仅将 `test_data_preparation.py` 中 `md_corpus`、`md_ingestion` 两处
`importlib` 加载改为从 `kbp` 包 import，其余加载语句保持不变。门禁边界测试用 pytest
参数化编写。

## 5. CDK 基础设施

### 5.1 Stack 切分

```text
FoundationStack (stateful, 终止保护开启)
  KMS CMK                 canonical 桶、registry 桶、DynamoDB、KB 共用
  S3 canonical            版本化，SSE-KMS，canonical 文档与 sidecar
  S3 registry             版本化，不可变 Release Manifest
  CloudWatch LogGroup     状态机执行日志
    -> 导出 bucket 与 key ARN
KnowledgeBaseStack (stateful, 终止保护开启)
  IAM KB service role     仅读 canonical 桶指定前缀，仅用 CMK 解密
  CfnKnowledgeBase        type=MANAGED, embeddingModelType=MANAGED, CMK 加密
  CfnDataSource           S3 型，指向 canonical 前缀，DataDeletionPolicy=RETAIN
    -> 导出 knowledgeBaseId 与 dataSourceId
ReleaseStack (stateless, 可重建)
  DynamoDB release table  CMK 加密，PITR 开启
  Lambda x3               S3 一致性校验、门禁判定、Registry 读写与 Promotion
  StateMachine            Standard 类型；摄入、删除、终态查询、冒烟检索走 SDK 集成
  IAM publisher role      本地入口 assume 后 StartExecution
```

Stack 间通过 CloudFormation Export 传递标识，不使用跨 Stack 直接对象引用，以便
ReleaseStack 独立重建。

### 5.2 关键决策

- **KB 独立 Stack 并开启终止保护**：`ManagedKnowledgeBaseConfiguration` 为
  createOnly，配置变更会替换 KB 并丢失索引。此切分使"重建发布逻辑"与"重建知识库"
  物理隔离，ReleaseStack 可反复销毁重建而不威胁索引。
- **`DataDeletionPolicy=RETAIN`**：Data Source 被删除时不清空索引，避免单次 CDK
  误操作抹除全部内容。
- **canonical 与 registry 分桶**：两者生命周期语义不同。Manifest 是审计证据，永不
  过期；canonical 文档的非当前版本 30 天清理。分桶避免生命周期规则相互干扰。
- **单 KMS CMK**：单账户单 Region 参考实现下，多把 CMK 只增加密钥策略调试成本，
  不带来隔离收益。

### 5.3 护栏

- `cdk-nag` 使用 `AwsSolutions` 规则包；每条抑制项必须附书面理由。
- `cdk synth` 纳入 CI。
- `infra/test/` 使用 `Template.fromStack()` 断言：KB 使用 CMK、KB role 的 S3 权限被
  前缀收窄、stateful Stack 的 `RemovalPolicy` 为 RETAIN、状态机存在各门禁到
  `FailRelease` 的转移边。

## 6. Release Registry

### 6.1 DynamoDB 表结构

单表，主键预留多语料扩展位：

| PK | SK | 用途 |
| --- | --- | --- |
| `CORPUS#<corpusId>` | `RELEASE#<releaseId>` | 单次发布的完整记录 |
| `CORPUS#<corpusId>` | `POINTER` | 当前活动版本指针，每个语料仅一项 |

Release 记录字段：`status`、`parentReleaseId`、`manifestS3Uri`、
`manifestS3VersionId`、`corpusSha256`、`sourceCommit`、`changeCounts`、
`executionArn`、各门禁结果、`createdAt`、`updatedAt`。

状态流转，括号内为第 7.1 节对应的状态机步骤：

```text
PREPARING   ([2] 创建记录，[3][4] 门禁 A/B 期间)
  -> INGESTING ([5][6] 摄入与删除，[7] 终态轮询)
  -> TESTING   ([8] 冒烟检索)
  -> ACTIVE    ([9] 条件写 POINTER 成功)

任一环节失败 -> FAILED（终态，永不触碰 POINTER）
被新版本替换 -> SUPERSEDED（由 [9] 在切换指针时写入）
```

### 6.2 原子 Promotion

执行开始时读取 POINTER 的 `activeReleaseId`，存入执行上下文作为
`expectedPrevious`。Promotion 时对 POINTER 条件写：

```text
ConditionExpression:
  attribute_not_exists(activeReleaseId) OR activeReleaseId = :expectedPrevious
```

并发流水线中后到者条件写失败，状态机进入 `FAILED`，不静默覆盖。

### 6.3 Manifest 存储分工

Manifest 作为不可变对象写入 registry 桶
`manifests/<corpusId>/<releaseId>.json`，DynamoDB 仅保存 URI 与 VersionId。

**S3 是 Manifest 内容的权威副本；DynamoDB 是发布状态与活动指针的权威副本。**
即使表被误删，历史发布内容仍可自版本化 S3 完整重建。

### 6.4 标识与幂等键

`releaseId` 格式：`<corpusId>-<YYYYMMDDTHHMMSSZ>-<corpusSha256 前 8 位>`。使用紧凑
时间戳格式，避免冒号等字符。

`clientToken` 必须派生，不得直接拼接标识：

```python
token = sha256(f"{releaseId}|{operation}|{batchIndex}".encode()).hexdigest()[:40]
```

产出恒定 40 字符的十六进制串，满足 33–256 长度与仅字母数字的 pattern 约束。取值
确定性使状态机重试同一批次时复用同一 token，从而天然幂等。

### 6.5 回滚能力留位

本次不实现回滚命令，但数据模型已支持：`SUPERSEDED` 记录保留完整 Manifest，其中每个
文档含 `s3VersionId`。后续回滚流程为：读旧 Manifest、恢复 S3 版本、重新摄入、条件写
切换指针。无需 schema 迁移。

## 7. 发布状态机

### 7.1 拓扑

```text
[1] ValidateInput          读 Manifest、校验 schema、读 POINTER 取 expectedPrevious
                           变更集为空 -> Succeed（不创建记录，POINTER 不变）
[2] CreateReleaseRecord    写 PREPARING，条件为 releaseId 不存在
[3] VerifyS3Consistency    门禁 A
      upsert 文档的对象与 sidecar 均存在且 SHA-256 匹配
      delete 文档的对象已自 S3 消失
      不通过 -> FailRelease
[4] CheckDeletionRatio     门禁 B
      比例 = |deleted| / |previousManifest.documents|
      超阈值且未带 allowBulkDeletion -> FailRelease
[5] Map: IngestBatches     并发 1，每批至多 10 个文档
      payload 含 metadata.type=S3_LOCATION 与 sidecar URI
      Retry: ThrottlingException 指数退避
[6] Map: DeleteBatches     每批至多 10 个标识，DeleteKnowledgeBaseDocuments
[7] PollDocumentStatus     门禁 C，Wait 与 GetKnowledgeBaseDocuments 循环
      全部 upsert 达 INDEXED 且全部 delete 达 NOT_FOUND -> 继续
      任一进入失败终态或轮询超时 -> FailRelease
[8] SmokeRetrieve          门禁 D
      对变更文档按 document_id 过滤 Retrieve，确认可检索
[9] PromoteRelease         条件写 POINTER，旧版本转 SUPERSEDED，本版本转 ACTIVE
[F] FailRelease            写 FAILED 与原因，绝不触碰 POINTER
```

四道门禁串行前置于 Promotion，失败分支仅有一个出口。

### 7.2 门禁如何关闭已知缺陷

对应 HANDOFF_REPORT 第 5 节列出的十项问题：

| 编号 | 原问题 | 关闭方式 |
| --- | --- | --- |
| 1 | 删除比例分母使用删除后文档数 | 门禁 B 分母改为发布前 Manifest 文档总数；全量删除得 100% 而非 0% |
| 2 | 删除保护只写告警不阻断 | 门禁 B 为 Choice State，超限直接进 `FailRelease`，无继续路径 |
| 3 | 仅记录 API `ACCEPTED`，未轮询终态 | 门禁 C 轮询至终态，且四类非完全成功终态判为失败 |
| 4 | Direct Ingestion 未显式关联 sidecar | payload 显式携带 `metadata.type=S3_LOCATION` 与 sidecar URI |
| 5 | S3 删除失败被忽略仍提升 Manifest | 门禁 A 反向校验删除对象已消失，失败即中止 |
| 6 | Manifest 仅存本地被忽略目录 | 存入版本化 registry 桶，状态入 DynamoDB |
| 7 | 使用 Sync 处理删除 | 改用 `DeleteKnowledgeBaseDocuments` |
| 8 | A2 探针使用 `CUSTOM` payload 与 `vectorSearchConfiguration` | 探针重写，使用 S3 payload 与 `managedSearchConfiguration` |
| 9 | A1 探针对同一 Data Source 连续提交，混淆并发限制与速率限制 | 探针使用多个 Disposable Data Source 控制 job-in-progress 变量 |
| 10 | 新增流水线无单元测试 | 门禁逻辑纯函数化，由 pytest 覆盖 |

第 3 项需特别说明：`DocumentStatus` 的 12 个枚举值中仅 `INDEXED` 表示完全成功。
`PARTIALLY_INDEXED` 意味着部分分块失败，内容不完整但 API 不报错。将其视作成功等同于
静默数据损坏。

### 7.3 门禁边界语义

以下边界情形必须有确定行为，不得依赖实现者临场判断。

| 情形 | 行为 |
| --- | --- |
| 初次发布（无 previous Manifest） | 门禁 B 分母为零。此时删除集合必然为空，判定直接通过，不计算比例 |
| 变更集为空（无增改删） | 于 `[1] ValidateInput` 提前进入 `Succeed`，不创建 Release 记录，POINTER 不变 |
| 仅删除的发布（无 upsert） | 门禁 D 无变更文档可冒烟，改为验证被删文档按 `document_id` 过滤检索返回空集 |
| 仅 metadata 变更 | 视作 modified，走完整 upsert 路径；门禁 C 需接受 `INDEXED` 且拒绝 `METADATA_PARTIALLY_INDEXED` |

删除比例默认阈值为 **0.5**，沿用现有 `MD_DELETION_PROTECTION_THRESHOLD` 配置项语义。

### 7.4 删除门禁语义

删除比例超阈值时状态机**硬失败**，不修改任何状态。操作者确认后携带
`allowBulkDeletion=true` 重新发起执行。

选择硬失败而非人工审批暂停的理由：无需引入审批回调基础设施，审计痕迹即两次
execution 记录；长时暂停亦不占用 execution 生命周期。

### 7.5 错误处理

- **限流**：`[5]`、`[6]` 对 `ThrottlingException` 指数退避
  （`IntervalSeconds: 2`、`BackoffRate: 2`、`MaxAttempts: 6`）。
- **其他异常**：`Catch` 至 `FailRelease`，附原始错误信息。
- **轮询超时**：门禁 C 设最大轮询次数，超时判失败。此时已摄入文档留在索引中但不
  Promotion。这是有意取舍：索引可能领先于 Manifest，下次发布会重新计算同批变更并
  覆盖，而 POINTER 始终指向已验证版本。
- **状态机类型**：选择 Standard 而非 Express，以支持长时轮询并保留完整执行历史作为
  审计证据。

## 8. 语料准备与发布入口

`cli/publish.py` 作为本地入口，职责为薄壳：

```text
本地扫描语料并执行准备门禁（编码、空文档、大小、document_id 唯一性）
  -> 与活动 Manifest 比对，得出 added/modified/deleted
  -> 上传变更对象与 sidecar 至 canonical 桶
  -> 上传候选 Manifest 至 registry 桶
  -> StartExecution 并轮询执行结果
```

准备阶段保留在本地而非状态机内的理由：本地可快速迭代，且大语料扫描不受 Lambda 资源
约束。该入口后续演进为 `kbctl release publish` 子命令。

准备门禁沿用现有实现的阈值：非空正文、无 `U+FFFD`、单文档抽取文本不超过 30 MB、
sidecar 不超过 10 KB、`document_id` 全语料唯一。

Metadata 策略沿用本仓库实测结论：治理与授权字段（`document_id`、`classification`、
`owner`、`lifecycle_status`、`content_sha256`、`source_path`）一律
`includeForEmbedding=false`；仅 `title`、`section_path`、`domain`、`topic` 参与
Embedding。

## 9. 假设探针

先于状态机实现执行，结论记入 ADR。

### A1：`StartIngestionJob` 是否对 Managed KB 强制 0.1 rps

方法：创建多个 Disposable Data Source，以隔离"同 Data Source 并发限制"与"API 速率
限制"两个变量，记录提交间隔与 `ThrottlingException`。

- 若全部接受且间隔远低于 10 秒，A1 被推翻，后续对账通道无需串行门闩。
- 若在约每 10 秒一次处限流，A1 成立，对账通道须保留限流器。

**A1 不阻塞本次状态机。** 本 Sprint 的摄入与删除均走 Direct API（文档速率 20 rps），
不调用 `StartIngestionJob`；周期性对账 Sync 已列为范围外。此处仍在本 Sprint 测量，
理由是沙箱可用、成本低，且结论可为后续对账通道设计消除返工风险。测量结果不改变第 7
节拓扑。

### A2：对账 Sync 是否移除仅存在于索引的定向摄入文档

方法：在同一 S3 桶创建位于 Connector inclusion prefix 之外的探针对象，以 S3 URI
定向摄入并轮询终态；确认可检索后运行 Data Source Sync，再以
`managedSearchConfiguration` 检索。

- 若同步后检索不到，A2 成立，"先写 S3 再定向摄入"为强制要求。
- 若探针存活，A2 被推翻，两条通道的耦合可放松。

**A2 的结论不改变本次门禁 A。** 门禁 A 要求 upsert 文档的 S3 对象先于摄入存在，这一
顺序在 A2 成立时是正确性要求，在 A2 被推翻时仍是有价值的一致性校验（确保 Manifest
与 canonical 桶不漂移）。因此本次无条件强制该顺序，A2 的价值在于确定后续对账通道能否
放松耦合。

## 10. 测试策略

三层测试对应三类失败模式。

框架为 pytest，迁移方式见第 4.3 节。

### 10.1 单元测试（pytest，无 AWS 依赖）

覆盖门禁纯函数的边界条件，这层应最密：

- 删除比例：分母为零、删除 100%、删除恰好等于阈值
- 终态聚合：`PARTIALLY_INDEXED`、`METADATA_UPDATE_FAILED`、`IGNORED`、`NOT_FOUND`
  各自的判定
- 批次切分：文档数恰为 10、11、0 的边界
- `clientToken` 派生结果满足长度与字符集约束
- Manifest 比对：metadata-only 变更、重复 `document_id`、初次加载

### 10.2 基础设施测试（Jest 与 `Template.fromStack`）

- KB 使用 CMK 加密
- KB service role 的 S3 权限被前缀收窄
- stateful Stack 的 `RemovalPolicy` 为 RETAIN
- 状态机存在门禁 A–D 到 `FailRelease` 的转移边

### 10.3 集成测试（真实沙箱账户）

四条路径：

1. 正常发布走完九步，得到 `ACTIVE` Manifest
2. 注入一个损坏文档，验证门禁阻断且 POINTER 未变
3. 构造超限删除，验证硬失败且未执行删除
4. 并发发起两次执行，验证条件写拒绝后到者

## 11. 验收标准

- 空沙箱账户可重复执行 `cdk deploy` 部署三个 Stack
- 一次真实 Markdown 语料发布走完九步并得到 `ACTIVE` Manifest。验收语料使用
  `examples/corpus/` 下新增的小规模固定语料（12–15 篇，含多级目录以产生
  `domain`/`topic` 字段，含一篇仅 metadata 变更样本），不使用受 Git 忽略的
  `artifacts/` 内容，以保证验收可重复且可提交
- 第 10.3 节四条集成路径行为符合预期；失败路径下 POINTER 未被修改
- A1/A2 探针给出结论并记入 ADR
- pytest 与 Jest 全部通过
- `cdk-nag` 无未附理由的抑制项
- `scripts/02_provision.sh` 已删除，README 中英文同步且通过链接与命令块校验
- CI 已切换至 pytest 且既有数据准备测试仍通过

## 12. ADR 清单

本次需产出的架构决策记录：

| 编号 | 主题 |
| --- | --- |
| ADR-001 | 采用 Step Functions 加最小 Lambda 的执行模型 |
| ADR-002 | Stack 按 stateful 与 stateless 切分，KB 独立并开启终止保护 |
| ADR-003 | Manifest 权威副本存 S3，状态与指针存 DynamoDB |
| ADR-004 | 删除比例超限采取硬失败而非人工审批暂停 |
| ADR-005 | 仅 `INDEXED` 判定为摄入成功 |
| ADR-006 | A1/A2 探针结论及其对限流与通道耦合的影响 |
| ADR-007 | Data Source 不支持 Tag，成本分账落在 KB 与 S3 层 |
