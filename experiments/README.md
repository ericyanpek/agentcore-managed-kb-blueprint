# Amazon Bedrock Managed Knowledge Base 企业实验路线

复核日期：2026-08-05

## 1. 实验原则

实验用于回答企业架构问题，不是 Notebook 清单。除非用户明确授权，本路线只提供
设计和可执行步骤，不创建 AWS 资源。

每次实验必须记录：

```text
Experiment ID:
Date:
AWS account / Region:
Operator role:
Sample repository / commit:
Local repository commit:
Resources:
Corpus / manifest checksum:
Expected result:
Actual result:
Positive tests:
Negative tests:
Control IDs:
Logs / trace / CloudTrail evidence:
Cost:
Cleanup status:
Decision:
Open assumptions:
```

证据不得提交真实 Secret、Token、账户敏感信息、客户数据或未脱敏 Raw Chunk。

每次实验还必须复制
[`observability-evidence.template.md`](observability-evidence.template.md)，
对一条成功路径和一条可控失败路径同时验证功能与可观测性：

- **Metrics**：namespace、名称、dimensions、统计值和到达延迟。
- **Logs**：类型、显式 delivery 状态、destination、留存、KMS 和事件引用。
- **Traces**：Transaction Search 状态、trace/root/child spans 和缺失链路。
- **Correlation**：request/session/trace ID 能否关联同一请求。
- **Application telemetry**：自定义 Agent/Proxy 是否需要并已验证 ADOT/OTEL。
- **Pipeline health**：delivery failure、throttle、lag 和 Schema 拒绝是否告警。

不适用记录 `N/A`；预期存在但未配置或无法取得记录 `GAP`。不得用 Metrics 替代
Logs/Traces，也不得把“无证据”解释为“无错误”。详细要求见
[可观测性蓝图](../docs/OBSERVABILITY_BLUEPRINT.md)。

## 2. 来源与环境

锁定来源：

- `awslabs/agentcore-samples@fa72a1ed57c0c6c8dcb943b08e66813f8d8e4645`
  （2026-08-04T00:03:48Z）。
- `aws-samples/amazon-bedrock-samples@0f072b52163145db5d9903846fb537be7b73df1f`
  （2026-07-21T18:46:37Z）。

执行时必须重新记录实际 SHA。完整映射见
[AWS 官方样例目录](../docs/AWS_SAMPLE_CATALOG.md)。

环境要求：

- 使用隔离 Sandbox 账户和临时 IAM Role。
- 资源标记 `Owner`、`Environment=lab`、`ExpiresAt`、`CostCenter` 和
  `RiskTier`。
- 基础实验只使用 K0 公开或脱敏语料。
- 开始前设置预算，记录 Pricing、Region 和 Service Quotas 复核时间。
- 每个实验执行 sample cleanup，再按 Tag/资源清单确认零残留。

建议账户：

| 账户 | 用途 |
| --- | --- |
| `agentcore-lab-content` | S3 事实源、Manifest、跨账户数据源 |
| `agentcore-lab-platform` | Managed KB、Gateway、评测与应用 |
| `agentcore-lab-security` | CloudTrail、集中日志和跨账户 Observability |

## 3. 路线总览

| ID | 实验 | 主要问题 | 关键产物 |
| --- | --- | --- | --- |
| E00 | 来源、Region、SDK、配额与成本 | 当前账户能否可靠复现 | 来源锁、环境和预算基线 |
| E01 | 最小功能与资源生命周期 | 创建、摄入、检索、删除是否闭环 | IaC、状态机、Cleanup |
| E02 | 身份、最小权限与 Gateway | 谁能调用，能否绕过 | IAM/Resource Policy 契约 |
| E03 | 私网、跨账户与多租户 | 网络和租户边界是否真实隔离 | 网络图、跨租户负测 |
| E04 | 数据、Metadata、ACL 与安全 | 内容能否正确、可追溯、不可越权 | Metadata 与质量报告 |
| E05 | 可观测性、审计和故障注入 | 能否定位失败和重建事件 | Trace、告警、Runbook |
| E06 | 容量、配额、成本和恢复 | 能否按目标规模运营 | 压测、成本、DR 报告 |
| E07 | 低风险企业准入演练 | 能否按控制基线正式发布 | 完整证据包和 ADR |

## E00：来源、Region、CLI/SDK、配额与成本基线

**官方 Sample**

- `aws-samples/amazon-bedrock-samples/rag/managed-knowledge-bases/README.md`
- `awslabs/agentcore-samples/01-features/03-connect-your-agent-to-anything/04-fmkb-managed-kb/README.md`

**前置条件**：目标账户、Region 和 Operator Role 已批准；不创建长期资源。

**步骤**

1. 锁定 Developer Guide、Release Notes、Pricing、Quotas 和 sample SHA。
2. 记录 AWS CLI、boto3/botocore、AgentCore CLI 和 Python 版本。
3. 检查 Region 的 Managed KB、目标模型、Gateway 和 PrivateLink 能力。
4. 获取账户配额，建立 1 GB、10 GB 和目标规模成本模型。
5. 建立命名、Tag、Evidence 和 ExpiresAt 规范。

**正向测试**：只读 Discovery API 成功；SDK 包含 `MANAGED`、
`managedSearchConfiguration` 和 `AgenticRetrieveStream`。

**负向测试**：旧 SDK、未批准 Region、错误 Profile、缺失模型权限和遗漏
Agentic 内部 Retrieve 成本时，基线门禁必须失败。

**观测验证**：记录目标账户/Region 的 Transaction Search、各资源默认与显式
log delivery、CloudTrail、ADOT 依赖和遥测成本基线；用错误 Profile 证明
AccessDenied 能被日志/审计捕获，不创建长期资源。

**成功标准**：能力、Region、Quota、Price 均有来源与日期；未使用管理员长期凭证。

**控制**：`KB-GOV-001`、`KB-IAM-001`、`KB-REL-003`、`KB-FIN-002`、
`KB-SDLC-003`

**成本与 Cleanup**：只读调用；确认未创建资源。

## E01：最小功能与资源生命周期

**官方 Sample**

- `aws-samples/.../01-getting-started/01-create-bmkb-s3.ipynb`
- `aws-samples/.../07-IaaC/managed_kb_cfn/managed-kb-s3-cfn.yaml`
- `aws-samples/.../07-IaaC/managed_kb_cdk/`

**前置条件**：K0 语料、版本化 S3、最小 service role；IaC 不使用
`AmazonBedrockFullAccess`。

**步骤**

1. 通过 IaC 创建 KMS、S3、Role、Managed KB 和 S3 Data Source。
2. 上传带 Sidecar 的 canonical Markdown，启动并等待 Ingestion Job。
3. 执行 `Retrieve`，保存 Rank、Score、Metadata 和 Source。
4. 更新 Content/Metadata，验证增量摄入与旧版本排除。
5. 用 `DeleteKnowledgeBaseDocuments` 定向删除，再运行 Sync 对账。
6. 清理并按资源清单确认零残留。

**正向测试**：内容、Metadata、引用与 Manifest 一致；Direct Document 最终状态
成功；空账户可用 IaC 重建。

**负向测试**：空文档、损坏 UTF-8、超限 Sidecar、重复 ID、缺权限、错误 KMS、
错误 Data Source Type、异步文档失败和 S3 删除失败均不得提升 Manifest。

**观测验证**：对一次成功摄入和一次可控摄入失败保存 KB 指标、摄入日志和应用
trace；用 ingestion job ID、document ID 和 release ID 关联，验证 failed/skipped
不会被成功统计掩盖。

**成功标准**：生命周期可重复、可回滚、可清理；没有把 API `ACCEPTED` 当成成功。

**控制**：`KB-ARC-002`、`KB-ING-001` 至 `KB-ING-008`、`KB-REL-001`

**成本与 Cleanup**：记录 GB-hour、Retrieve 和日志；删除 KB、Data Source、
对象、Role、Log Group 和未保留 Key。

## E02：身份、最小权限与 Gateway

**官方 Sample**

- `awslabs/.../04-fmkb-managed-kb/01-raw-mcp/`
- `awslabs/.../04-fmkb-managed-kb/02-strands-agent/`
- `aws-samples/.../03-use-case-example/01-end-to-end-example-with-ac-gateway/`
- `aws-samples/.../04-security-and-access-controls/notebooks/03-gateway-iam.ipynb`

**前置条件**：E01 KB 可用；Gateway、Runtime、KB service role 使用独立 Role。

**步骤**

1. 用直接 SDK 建立 Retrieve 对照。
2. 创建 IAM-auth Gateway，只暴露 `Retrieve`；固定 KB ID 和默认参数。
3. 分别执行 SigV4 Raw MCP 与 Runtime Agent 调用。
4. Gateway Role 收紧到具体 KB；Runtime Role 只允许指定 Gateway。
5. 配置跨账户 Resource Policy，验证双边授权。
6. 增加 Agentic Tool 时记录通配 IAM Action 和补偿控制。

**正向测试**：三条调用路径返回同一 Corpus Version；批准的跨账户 Role 成功。

**负向测试**：Runtime 直接调用 KB、未授权账户、覆盖 KB ID/Filter/Top-K、错误
Region/签名和删除 Target 后的旧调用均失败。

**观测验证**：对 Gateway 成功调用和权限拒绝分别取得 Gateway metrics、显式
vended logs 和 service trace；证明 request/trace/session ID 传播到应用和 KB
调用，缺失 direct-KB 权限的拒绝出现在 CloudTrail 或应用证据中。

**成功标准**：每一跳 Principal/Action/Resource 清晰；不存在未批准绕过路径。

**控制**：`KB-IAM-002` 至 `KB-IAM-007`、`KB-GW-001` 至 `KB-GW-004`

**成本与 Cleanup**：记录 Gateway、Runtime 和模型成本；删除 Target、Gateway、
Runtime/ECR/CodeBuild 和实验 Role。

## E03：私网、跨账户与多租户

**官方 Sample**

- `aws-samples/.../04-security-and-access-controls/notebooks/05-gateway-jwt-cognito.ipynb`
- `.../06-gateway-jwt-cedar.ipynb`
- `.../07-gateway-interceptor.ipynb`
- `.../08-full-governance.ipynb`

**前置条件**：至少两个账户、两个测试租户和独立语料；Private DNS/VPCE 已设计。

**步骤**

1. 从私有 Subnet 经 `bedrock-agent-runtime` VPCE 调用 KB。
2. 限制 Endpoint Policy，并验证跨账户 Resource Policy。
3. 从可信 JWT Claims 注入 tenant/classification Filter。
4. 比较共享 KB + Filter 与独立 KB 的 Blast Radius。
5. 撤销用户组、JWT、Resource Policy 和 Gateway Target，测量撤权窗口。

**正向测试**：正确租户只获取自己的文档；私网路径和批准跨账户 Role 成功。

**负向测试**：客户端伪造 tenant、缺失/错误 Filter、跨租户 Marker、错误 VPCE、
公共绕过、旧 JWT 和撤权后 Session 均 Fail Closed。

**观测验证**：成功租户请求与跨租户拒绝使用虚构 tenant marker；验证日志和
span 只记录批准的身份派生字段，不记录 JWT/Token，并能关联 Gateway policy、
KB filter 和撤权时间窗。

**成功标准**：ACL Leakage Rate 为 0；实测撤权窗口满足风险等级。

**控制**：`KB-ARC-001`、`KB-IAM-004` 至 `KB-IAM-008`、`KB-NET-001`
至 `KB-NET-004`、`KB-RET-003`

**成本与 Cleanup**：记录 VPCE-hour、数据处理和日志；删除实验 Endpoint、
JWT App、租户语料和临时 Resource Policy。

## E04：数据、Metadata、ACL、Guardrails 与质量

**官方 Sample**

- `aws-samples/.../02-feature-examples/02-chunking-and-parsing/`
- `aws-samples/.../02-feature-examples/03-retrieval-optimization/`
- `aws-samples/.../04-security-and-access-controls/notebooks/02-metadata-filters.ipynb`
- `aws-samples/.../06-Responsible AI/`
- 本仓库 `scripts/09`、`14`、`17` 至 `20`

**前置条件**：Versioned Golden Set，包含准确、模糊、无答案、越权、过期和
Prompt Injection；Metadata Dictionary 已审批。

**步骤**

1. 对同源语料建立 PDF、canonical Markdown 和结构感知 Candidate。
2. 固定 Query/Top-K/Rerank，比较解析、分块和 Metadata 单变量。
3. 比较 filter-only 与 selected semantic Metadata。
4. 启用 ACL awareness，传递可信 User Context。
5. 测试 Guardrail `BLOCK`、Raw Chunk、答案和日志边界。
6. 验证 Content/ACL/Metadata 更新后的 Freshness。

**正向测试**：Expected Evidence 进入 Top-K；Provenance 完整；ACL 只返回允许文档。

**负向测试**：无答案、Prompt Injection、Raw Reference 脱敏误判、Sidecar 类型
错误、缺失权限字段、删除/过期 Marker 均满足 Fail Closed 或零命中预期。

**观测验证**：成功召回与零结果/Guardrail BLOCK 分别记录 corpus、query set、
filter、top-k、reranker 和 citation 指标；应用 trace 标记检索阶段，日志不得
包含未批准 Raw Chunk、完整 Prompt Injection 或真实身份字段。

**成功标准**：质量提升有成对实验；ACL Leakage 和 stale-content 命中为 0。

**控制**：`KB-DATA-001` 至 `KB-DATA-008`、`KB-RET-001` 至
`KB-RET-008`、`KB-GRD-001` 至 `KB-GRD-003`

**成本与 Cleanup**：记录各 Candidate 存储/Query；保留胜出版本和 Canary，
删除其余实验 Data Source。

## E05：可观测性、审计与故障注入

**官方 Sample**

- `aws-samples/.../05-Observability/01-cloudwatch-metrics.ipynb`
- `aws-samples/.../05-Observability/02-agentcore-observability.ipynb`

**前置条件**：目标账户/Region 的 Transaction Search、CloudWatch、所需
CloudTrail Data Events、资源级 vended log delivery、应用 ADOT、日志 KMS/留存
和 Correlation ID 已设计。

**步骤**

1. 确认 Transaction Search，启用摄入日志、KB 指标、资源 log delivery 和所需
   Runtime Data Events。
2. 通过 Gateway 执行 Retrieve 与 Agentic Retrieve。
3. 生成 Ingestion Failure、AccessDenied、Validation、Throttle、零结果、
   Gateway Failure 和 Planner `actions=[]`。
4. 验证 request/session/trace ID 与 principal/tenant/KB/Data Source/Corpus
   Version 的关联。
5. 验证自定义 Agent/Proxy 的 ADOT spans 和业务字段。
6. 建立服务与 delivery pipeline 告警和 Runbook，抽样检查敏感日志。
7. 按需验证 CloudWatch Logs -> Firehose -> S3 Tables，检查 Schema、错误备份、
   分区、留存和 table maintenance；不需要长期分析时记录不采用的 ADR。

**正向测试**：成功请求可跨 Gateway、KB、应用关联；告警有明确 Owner。

**负向测试**：缺 Data Events、测试 PII Marker、异步文档失败漏报均使门禁失败；
`actions=[]` 不触发无限重试；关闭测试资源的 log delivery 或提交不兼容事件时，
pipeline health 告警必须触发。

**观测验证**：完整填写证据模板；Metrics、Logs、Traces、ADOT、Correlation 和
Pipeline Health 均为 `PASS`、批准的 `N/A` 或有 Owner/期限的 `GAP`。

**成功标准**：主要失败有信号、告警和 Runbook；K2/K3 调用可重建；服务正常但
证据丢失时也能告警。

**控制**：`KB-OBS-001` 至 `KB-OBS-014`、`KB-IR-001`、`KB-GW-004`

**成本与 Cleanup**：记录日志、查询和 Data Events；删除临时高详细度日志。

## E06：容量、配额、成本与恢复

**官方 Sample**

- `aws-samples/.../utils/kb_cost.py`
- `aws-samples/.../02-feature-examples/04-rag-evaluation/`
- `aws-samples/.../07-IaaC/`

**前置条件**：批准压测窗口、请求上限和 Budget；Recovery Region 能力已复核。

**步骤**

1. 按文档量、Query Mix、Top-K 和 Agentic 比例建立容量模型。
2. 分别压测 Standard/Agentic Retrieval，记录 p50/p95/p99 和 Throttle。
3. 用多个 Disposable Data Source 测 Job 并发，避免混淆同源并发限制。
4. 比较 Managed/Custom Reranker 与 Agentic 迭代的质量和成本。
5. 复制事实源到 Recovery Region，用 IaC 重建、摄入并执行 Golden Set。
6. 演练切换和恢复，测量 RTO/RPO。

**正向测试**：目标负载满足 SLO；Recovery Region 的 Corpus/Metadata 一致。

**负向测试**：故意触发 Throttle、模型不可用、Region 依赖缺失、陈旧复制和
Budget 告警；系统有界退避且不误发布。

**观测验证**：在负载和恢复两阶段验证 p50/p95/p99、Throttle、retry、token/
query/log 成本以及 trace sampling；确认高吞吐下无遥测静默丢失，delivery lag
和 Firehose/Iceberg 错误可见。

**成功标准**：容量假设由实测支持；总成本可归属；RTO/RPO 达标。

**控制**：`KB-REL-002` 至 `KB-REL-005`、`KB-FIN-001` 至 `KB-FIN-003`

**成本与 Cleanup**：压测设置硬上限；删除 Recovery KB 和复制数据，保留脱敏报告。

## E07：真实但低风险企业准入演练

选择一个 K1 只读用例，例如内部已批准技术标准检索。

**官方 Sample**

- `aws-samples/.../03-use-case-example/01-end-to-end-example-with-ac-gateway/`
- `aws-samples/.../03-use-case-example/02-multi-kb-semantic-routing.ipynb`
- `aws-samples/.../03-use-case-example/03-gateway-with-cedar-policies.ipynb`

**步骤**

1. 完成 Owner、数据分类、风险、账户、Region 和成本审批。
2. 用 IaC 部署事实源、KB、Gateway、IAM、KMS、日志和告警。
3. 执行 E01-E06 中适用的正向和负向测试。
4. 运行完整发布门禁，切换 Canary，再切换正式 Target。
5. 演练回滚、用户撤权、内容删除、凭证轮换和 Region 恢复。
6. 生成控制证据包，移交正式 Owner 或完整清理。

**观测验证**：使用与生产相同的 Dashboard、Alarm、log delivery、Transaction
Search、ADOT、Correlation Schema 和留存策略执行一次成功业务请求与一次批准的
失败演练，并证明另一团队可从证据包完成定位。

**成功标准**

- 所有适用 `MUST` 为 `PASS` 或有未过期例外。
- 另一团队可只依赖文档、IaC 和 Runbook 重复部署、验证、回滚和清理。
- Gateway、KB、应用与内容 Owner 的责任没有空白或重复冲突。

**控制**：全部适用控制，重点是 `KB-SDLC-001` 至 `KB-SDLC-003`、
`KB-IR-001` 至 `KB-IR-003`

**成本与 Cleanup**：输出最终月度估算、Budget、正式资源转交清单；未转交资源
全部清理并复核。

## 5. 证据包结构

```text
evidence/
  <experiment-id>/
    metadata.yaml
    architecture.md
    inventory.json
    manifest.json
    tests/
    policies/
    iam/
    network/
    observability/
      observability-evidence.md
      schema-validation.json
    cost/
    cleanup.md
    decision.md
```

## 6. 必须形成的 ADR

- Managed KB 与 Customer-managed KB 的选型。
- 账户、Region、KB、Data Source 和租户边界。
- Direct API 与 Connector Sync 的发布状态机。
- Gateway 强制入口与直接 API 例外。
- Metadata/ACL Source of Truth 和 Fail Closed 规则。
- Parser/Chunking/Embedding/Reranker 版本策略。
- Golden Set、SLO、预算和 Agentic Retrieval 使用条件。
- Service/application telemetry、Transaction Search、log delivery、Correlation
  Schema、长期分析与留存策略。
- 多 Region RTO/RPO、恢复和退役策略。
