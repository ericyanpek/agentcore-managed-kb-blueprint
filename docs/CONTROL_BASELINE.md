# Amazon Bedrock Managed Knowledge Base 最低控制基线

复核日期：2026-08-05

本文将治理蓝图落实为可审计控制。每条控制记录为 `PASS`、`FAIL`、`N/A` 或
`EXCEPTION`；缺少最低证据的 `MUST` 控制不得判定为 `PASS`。

## 1. 风险等级

| 等级 | 示例 | 影响 |
| --- | --- | --- |
| K0 | 公开资料、无身份差异 | 低敏、可公开恢复 |
| K1 | 企业内部知识、统一员工权限 | 可能造成内部信息披露 |
| K2 | 部门、租户、合同或客户资料 | 越权、隐私、监管或业务损失 |
| K3 | 法规、医疗、金融、重大决策或生产操作依据 | 高影响错误、不可接受披露或合规风险 |

## 2. 控制表

| ID | 强度 | 控制要求 | 最低证据 |
| --- | --- | --- | --- |
| KB-GOV-001 | MUST | Developer Guide/Release Notes/API 是能力真值，sample 和本地实测仅作为次级证据 | 来源 URL、复核日期、sample SHA |
| KB-GOV-002 | MUST | 每个 KB、Data Source、Corpus 有业务 Owner、技术 Owner、风险等级和生命周期 | 资产记录、Tags、IaC |
| KB-GOV-003 | MUST | 服务事实、AWS 推荐、架构建议和未验证假设明确区分 | 设计评审记录 |
| KB-ARC-001 | MUST | KB 按账户、Region、租户、数据分类、KMS 和故障域划分 | ADR、数据流图 |
| KB-ARC-002 | MUST | 事实源与服务索引边界明确，索引可从事实源和 Manifest 重建 | Manifest、Checksum、重建 Runbook |
| KB-ARC-003 | SHOULD | 同信任域优先以 Data Source 做发布隔离；跨监管或 IAM 边界拆 KB | 边界决策表 |
| KB-IAM-001 | MUST | 使用 IAM Role 和短时凭证，禁止 IAM User 长期 Access Key | 身份清单、Secret Scan |
| KB-IAM-002 | MUST | KB service role 使用 `SourceAccount` 与 `SourceArn` 防 confused deputy | Trust Policy、Access Analyzer |
| KB-IAM-003 | MUST | S3、KMS、Secret 和模型权限限定到必要 Action/ARN | IAM Policy、Access Analyzer |
| KB-IAM-004 | MUST | 跨账户调用同时配置 KB Resource Policy 和调用方 Identity Policy | 两侧 Policy、允许/拒绝测试 |
| KB-IAM-005 | MUST | 选择 Gateway 强制入口时，Runtime/Agent Role 无直接 KB 绕过权限 | Role Policy、直接调用拒绝证据 |
| KB-IAM-006 | MUST | User Context、tenant、role 和 classification 只来自可信认证 Claims | 身份映射代码与伪造测试 |
| KB-IAM-007 | MUST | ACL awareness 不作为唯一授权，应用先认证并 Fail Closed | 身份流程图、空/错 Context 测试 |
| KB-IAM-008 | SHOULD | 离职、组变更、Email Reuse 和凭证吊销有最大生效窗口 | IAM/HR 流程、撤权演练 |
| KB-NET-001 | SHOULD | K1-K3 私有工作负载使用适用 Bedrock Interface VPC Endpoint | VPCE、路由和 DNS 证据 |
| KB-NET-002 | MUST | PrivateLink/VPC 不替代 IAM、Resource Policy 和应用授权 | 架构评审、负向测试 |
| KB-NET-003 | MUST | SaaS Connector 的出站域名、证书、代理和数据驻留经过审查 | Connector 登记、网络图 |
| KB-NET-004 | SHOULD | Endpoint Policy 限制 Principal、Action 和资源 | VPCE Policy |
| KB-DATA-001 | MUST | 入库前完成数据分类、PII/PHI、Secret 和恶意内容扫描 | 扫描报告、Owner 审批 |
| KB-DATA-002 | MUST | Source、Canonical、Metadata、Corpus Version 和 SHA-256 可追溯 | Manifest、发布记录 |
| KB-DATA-003 | MUST | Metadata Schema 定义类型、允许值、Owner、授权语义、Embedding、保留和迁移 | Metadata Dictionary |
| KB-DATA-004 | MUST | ACL/tenant/classification 等治理字段不参与 Embedding，除非有批准的实验结论 | Sidecar、Schema Gate |
| KB-DATA-005 | MUST | Raw Retrieved References、Trace 和日志按源数据分类保护 | 日志配置、抽样记录 |
| KB-DATA-006 | MUST | 受监管数据评估 Customer-managed KMS，并记录 Key 生命周期 | KMS Policy、ADR、Grant 监控 |
| KB-DATA-007 | MUST | 内容删除覆盖事实源、索引、缓存和下游副本；Legal Hold 优先 | 删除工单、stale-content 测试 |
| KB-DATA-008 | SHOULD | S3 Versioning/Object Lock 或等价能力保护可恢复事实源 | Bucket 配置、恢复演练 |
| KB-ING-001 | MUST | Parser、Chunking、Embedding、Reranker 和 Transformation 独立版本化 | Manifest、配置快照 |
| KB-ING-002 | MUST | 编码、空文档、大小、重复 ID、Sidecar 配对与 Schema 门禁通过 | Preparation Report |
| KB-ING-003 | MUST | Direct Ingest/Delete 轮询每份文档最终状态，不能只记录 API 接受 | `GetKnowledgeBaseDocuments` 证据 |
| KB-ING-004 | MUST | Connector Sync 等待终态并检查 failed/skipped 和文档统计 | Ingestion Job 响应、日志 |
| KB-ING-005 | MUST | 删除比例保护是可执行门禁，不是仅告警；删除失败时 Fail Closed | 计划、拒绝测试、审批 |
| KB-ING-006 | MUST | 发布 Manifest 只在摄入、ACL、检索和 Freshness 门禁成功后原子提升 | Promotion 记录、失败重试测试 |
| KB-ING-007 | SHOULD | 大规模变更使用 Canary/蓝绿 Data Source 或 KB | 双版本清单、切换和回滚证据 |
| KB-ING-008 | MUST | Direct Ingestion 显式关联所需 Metadata，不假设相邻 Sidecar 自动生效 | API 请求、返回 Metadata 检查 |
| KB-RET-001 | MUST | 生产前建立 Retrieve-only Golden Set 和人工标注 Evidence | Versioned Query Set |
| KB-RET-002 | MUST | 每次发布测试 Recall/Hit Rate、MRR/nDCG、Provenance、Latency 和零结果 | Retrieval Report |
| KB-RET-003 | MUST | K1-K3 执行允许、拒绝、跨租户、缺失字段和 Filter 错误测试 | ACL Leakage Report |
| KB-RET-004 | MUST | 过期、删除和旧版本不得进入结果 | Freshness/Stale Regression |
| KB-RET-005 | MUST | No-answer 用例不允许模型先验冒充 KB 证据 | No-answer Report |
| KB-RET-006 | SHOULD | Agentic Retrieval 与固定 Retrieve 基线比较质量、迭代、延迟和成本 | A/B Report、Trace |
| KB-RET-007 | MUST | Agentic `maxAgentIteration` 只描述为上限；`actions=[]` 不判定为系统异常 | Trace 判读规则 |
| KB-RET-008 | MUST | 生成答案包含稳定 Citation ID，且抽样检查 Citation Precision/Coverage | Answer Evaluation |
| KB-GRD-001 | MUST | Guardrails 不作为认证、业务授权或租户隔离 | Threat Model、控制映射 |
| KB-GRD-002 | MUST | Agentic Retrieval 使用 Guardrail 时接受仅 `BLOCK` 的产品边界 | 配置、BLOCK 正负测试 |
| KB-GRD-003 | SHOULD | K2-K3 答案定义人工复核、升级和拒答条件 | 业务流程、抽样记录 |
| KB-GW-001 | MUST | Gateway Target 固定 KB ID/Retriever 与治理参数，仅开放必要 Override | Target 配置、Tool Schema |
| KB-GW-002 | MUST | Gateway Tool 授权和 KB 文档授权分别测试 | Policy Test、ACL Test |
| KB-GW-003 | MUST | Gateway Role 只获得目标 KB 所需 Action；Agentic Action 通配限制有补偿控制 | IAM Policy、ADR |
| KB-GW-004 | MUST | Gateway、KB、应用 Trace 使用可关联 ID | 一次端到端 Trace |
| KB-OBS-001 | MUST | 启用 KB 运行指标、摄入日志和错误/Throttle 告警；两类信号分别配置和验证 | CloudWatch 配置、测试事件 |
| KB-OBS-002 | MUST | CloudTrail Management Events 可查询；K1-K3 启用所需 Data Events | Trail/Event Data Store |
| KB-OBS-003 | MUST | 日志不包含 Secret、Token 或未批准的 Raw Chunk/PII | 日志抽样、脱敏配置 |
| KB-OBS-004 | SHOULD | 日志/安全账户集中接收跨账户遥测 | OAM、订阅或 SIEM 配置 |
| KB-OBS-005 | MUST | 应用记录 p50/p95/p99、零结果、Filter、Corpus Version 和成本维度 | Dashboard、Metric Schema |
| KB-OBS-006 | MUST | Service-provided telemetry 与 application telemetry 分开设计；自定义 Agent/Proxy 的业务步骤显式评估 ADOT/OTEL | Telemetry 数据流、Instrumentation 配置 |
| KB-OBS-007 | MUST | 每个使用 AgentCore service trace 的账户和 Region 启用并验证 CloudWatch Transaction Search | 配置状态、测试 Trace |
| KB-OBS-008 | MUST | Gateway、Memory 和其他需要逐请求日志的资源维护显式 vended log delivery inventory | Delivery 配置、Destination、测试日志 |
| KB-OBS-009 | MUST | 每次实验包含成功与可控失败路径，并分别给出 Metrics、Logs、Traces 或明确的 `N/A/GAP` | Observability Evidence |
| KB-OBS-010 | MUST | 同一请求通过 request/session/trace ID 跨应用、Gateway、Tool 和 KB 关联；缺失传播有显式映射 | 端到端 Trace、Correlation Report |
| KB-OBS-011 | MUST | 日志、Span 和长期事件按数据分类实施字段白名单、KMS、访问控制、留存、删除与 Legal Hold | Schema、抽样、Retention Policy |
| KB-OBS-012 | SHOULD | 需要长期分析时使用版本化 Schema；Firehose 仅作为投递层，Iceberg/S3 Tables 作为分析层，禁止高基数 ID 分区 | Schema、Partition Spec、ADR |
| KB-OBS-013 | MUST | 对日志/Trace delivery failure、throttle、retry exhaustion、lag 和无数据建立管道自身监控 | Alarm、故障注入证据 |
| KB-OBS-014 | SHOULD | S3 Tables/Iceberg 配置 compaction、snapshot expiration、unreferenced file cleanup 和记录保留策略 | Maintenance 配置、删除演练 |
| KB-REL-001 | MUST | KB、Data Source、Role、KMS、Resource Policy、日志和告警通过 IaC 管理 | CloudFormation/CDK 与 Pipeline |
| KB-REL-002 | MUST | Retry 有界且按错误分类；Validation/AccessDenied 不盲目重试 | 故障注入报告 |
| KB-REL-003 | MUST | 上线前复核账户/Region 配额并压测目标并发 | Quota Snapshot、Load Report |
| KB-REL-004 | MUST | 从事实源重建的 RTO/RPO 已定义；K2-K3 执行恢复演练 | DR Runbook、演练记录 |
| KB-REL-005 | SHOULD | 多 Region 使用独立 KB/Index 并持续验证两侧语料一致 | 双 Region Manifest、回归 |
| KB-FIN-001 | MUST | KB 和关联资源具有 Owner、CostCenter、Environment、RiskTier 等 Tags | Tag Inventory |
| KB-FIN-002 | MUST | 预算包含存储、Retrieve、Agentic 内部 Retrieve、自定义模型、Gateway 和日志 | 成本模型、Budget |
| KB-FIN-003 | SHOULD | 存储增速、调用量和 Agentic 迭代异常进入告警 | Cost Anomaly/Alarm |
| KB-SDLC-001 | MUST | IaC、IAM、Metadata Schema、Query Set 和发布 Manifest 版本控制并双人审查 | Pull Request |
| KB-SDLC-002 | MUST | 发布包含正向、负向、越权、陈旧、故障、回滚和清理测试 | Release Report |
| KB-SDLC-003 | MUST | SDK/CLI/sample 版本固定，升级前执行契约回归 | Lock File、Sample SHA |
| KB-IR-001 | MUST | 有隔离 Target/Principal、撤销凭证、保全证据和内容回滚 Runbook | Incident Runbook |
| KB-IR-002 | MUST | Break-glass 使用短时凭证、MFA、审批、告警和事后复核 | Role 配置、演练 |
| KB-IR-003 | MUST | 例外有 Owner、补偿控制和到期时间 | Exception Register |

## 3. 风险等级附加要求

| 控制 | K0 | K1 | K2 | K3 |
| --- | --- | --- | --- | --- |
| 资源隔离 | 可共享 | 按业务域 | 按租户/监管边界 | 独立账户/KB 优先 |
| Customer-managed KMS | 可选 | 评估 | 默认要求 | 必须 |
| PrivateLink | 可选 | 建议 | 必须评估 | 必须 |
| CloudTrail Data Events | 可选 | 建议 | 必须 | 必须 |
| 三信号请求证据 | 抽样 | 每次发布 | 每次发布 | 每次发布加人工复核 |
| 长期遥测保留 | 按业务 | 分类评估 | 明确保留/删除 | 明确保留、Legal Hold 与删除 |
| ACL/Filter 负测 | 基础 | 角色 | 跨租户全量 | 全量加人工复核 |
| 人工内容审批 | 建议 | 必须 | 双人 | 双人加职责分离 |
| 人工答案复核 | 否 | 按场景 | 高影响请求 | 必须定义 |
| 蓝绿发布 | 建议 | 建议 | 必须 | 必须 |
| DR 演练 | 按业务 | 年度 | 半年度 | 按监管/SLO |

## 4. 发布门禁

```text
Knowledge Base:
Data Source / Corpus:
Release ID:
Owner:
Risk tier:
Region:
Change ID:

[ ] 所有适用 MUST 控制为 PASS 或有未过期 EXCEPTION
[ ] Source/Canonical/Metadata Checksum 与 Schema 门禁通过
[ ] Ingestion/Direct Document 最终状态为成功，failed/skipped 为零
[ ] Golden Set、No-answer、Citation 和 Latency 门禁通过
[ ] ACL、跨租户、缺失 Filter 和绕过 Gateway 测试通过
[ ] 删除、过期和旧版本未进入检索结果
[ ] 日志抽样未发现 Secret、Token 或未批准的 Raw Chunk/PII
[ ] 成功与失败路径具有 Metrics、Logs、Traces 证据或批准的 N/A/GAP
[ ] Transaction Search、vended log delivery 和应用 ADOT 状态已分别确认
[ ] request/session/trace ID 可跨适用资源关联，观测投递管道告警已验证
[ ] Quota、预算、告警、Runbook 和 Owner 已确认
[ ] Canary/回滚路径已验证，前一版本仍可恢复
[ ] Manifest 已在全部门禁之后原子提升
```

## 5. 例外记录模板

```text
Control ID:
Resource ARN or logical ID:
Business justification:
Risk:
Compensating controls:
Approver:
Owner:
Created:
Expires:
Remediation plan:
Evidence link:
```

例外 `MUST` 有到期时间。到期未续批时，发布流水线应阻止新版本或恢复基线。

## 6. 定期复核

| 周期 | 最低活动 |
| --- | --- |
| 每次发布 | 全部发布门禁、成本估算、清理确认 |
| 每月 | Ingestion 失败、存储增速、调用量、Throttle、陈旧内容、delivery lag/error |
| 每季度 | IAM/Resource Policy、ACL Schema、Owner、例外、Gateway 绕过、Transaction Search 与 delivery inventory |
| 每半年 | Golden Set 代表性、Parser/Chunking/模型版本、恢复演练 |
| 每年 | 账户/Region 架构、数据驻留、供应商 Connector、威胁模型 |
