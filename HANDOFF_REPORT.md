# Amazon Bedrock Managed Knowledge Base 治理蓝图 Handoff Report

日期：2026-08-05

## 1. 本次完成

- 建立企业治理蓝图，覆盖服务定位、控制/数据面、账户、Region、租户、身份、
  网络、数据、可靠性、成本、RACI、成熟度和 Gateway 跨服务契约。
- 建立带控制 ID、`MUST/SHOULD`、最低证据、风险分级、发布门禁和例外模板的
  控制基线。
- 建立 E00-E07 递进实验路线，包含官方 sample、前置条件、正负测试、成功标准、
  成本和 Cleanup。
- 建立 AgentCore/Managed KB 可观测性专项蓝图，区分 service/application
  telemetry，并覆盖 Transaction Search、vended logs、ADOT、三信号证据和
  Firehose/S3 Tables 长期分析。
- 建立可复用 Observability Evidence 模板和版本化长期分析 JSON Schema。
- 将 E00-E07 全部升级为功能与可观测性双验收，而非只在 E05 检查日志。
- 锁定并核实两个 AWS 官方 sample 仓库的 Commit 和 Managed KB 路径。
- 将官方事实、AWS 推荐、本项目实测、架构建议和待验证假设分开。
- 未创建、修改或删除任何 AWS 资源。

## 2. 来源快照

| 来源 | 快照 |
| --- | --- |
| `awslabs/agentcore-samples` | `fa72a1ed57c0c6c8dcb943b08e66813f8d8e4645`，2026-08-04T00:03:48Z |
| `aws-samples/amazon-bedrock-samples` | `0f072b52163145db5d9903846fb537be7b73df1f`，2026-07-21T18:46:37Z |
| Managed KB GA | AWS What's New，2026-06 |
| 文档/配额/价格复核 | 2026-08-04 |
| AgentCore/Managed KB Observability 文档复核 | 2026-08-05 |

事实优先级：

`Developer Guide / Release Notes > API Reference > AWS official samples >
local experiments > architecture recommendations`

## 3. 尚未验证的假设

| ID | 假设 | 当前状态 | 推荐实验 |
| --- | --- | --- | --- |
| A1 | Managed KB 的 `StartIngestionJob` Rate 是否沿用 Classic KB 0.1 rps | 官方 Managed 配额未单列；现有脚本不能排除同 Data Source 并发限制 | E06 使用多个 Disposable Data Source |
| A2 | Connector Sync 如何处理仅通过 Direct API 存在的文档 | 官方建议同步事实源，但边界需目标账户验证 | E01 使用 S3 Prefix 外探针并读取最终状态 |
| A3 | Managed Embedding 当前是否允许显式 Fixed/No Chunking | Sample 与本项目 2026-08-03 API 结果不一致 | E00/E01 记录 Region、SDK 和 Request |
| A4 | Managed Search 的 `overrideSearchType` 在各入口是否可用 | Gateway Connector 文档与 sample 文案存在差异 | E04 Direct/Gateway 契约测试 |
| A5 | ACL/Group/用户撤权的端到端生效窗口 | 取决于 Connector Sync、应用 Session 和身份源 | E03 撤权计时实验 |
| A6 | Managed KB 跨 Region 恢复 RTO/RPO | 服务索引不作为客户可复制备份 | E06 从复制事实源重建 |
| A7 | 目标账户/Region 的 Transaction Search、vended log delivery 和 ADOT 是否完整 | 本次只定义控制与证据，未读取或修改账户配置 | E00/E02/E05 按证据模板实测 |
| A8 | CloudWatch Logs -> Firehose -> S3 Tables 是否满足目标吞吐、延迟、成本和删除要求 | 架构可行但未部署；长期分析不是所有场景必需 | E05/E06 用脱敏事件做负载和删除演练 |

## 4. 与 Gateway 蓝图的对齐与待统一

已对齐：

- Gateway 是运行时 Tool 入口和策略执行点；Managed KB 是知识检索数据平面。
- 入口认证、Gateway Tool 授权和 KB 文档授权是三层不同控制。
- PrivateLink、Guardrails、语义搜索和 Tool 可见性均不能替代最终授权。
- Runtime 通过 Gateway 时不应同时获得直接 KB 权限。
- 两个蓝图都要求 IaC、默认拒绝、跨账户观测、成本 Tags 和未过期例外。

待统一：

- Gateway 风险等级使用 R0-R3，本蓝图使用 K0-K3。汇总 Agent 应形成统一风险
  词典，同时保留“工具操作风险”和“知识数据风险”两个维度。
- 已形成 Gateway、KB、应用和长期分析共享的 Correlation Schema 基线；Identity、
  Gateway 和 Evaluations 蓝图仍需确认字段 Owner 与实际传播能力。
- Gateway Policy 能固定 Target 和参数，但 Metadata/ACL Filter 的唯一真值应
  位于 Identity/Application/Data Governance 哪一层，需要 Identity 与 Policy
  蓝图共同决定。
- `AgenticRetrieveStream` 的 IAM Action 当前不能按 KB ARN 收敛，应由 IAM/
  Gateway 蓝图给出组织级补偿控制。

## 5. 当前实现与生产基线的差距

以下问题来自对 `scripts/21` 至 `23` 的审查，不应在修复前把该流程描述为
Production-ready：

1. 删除比例使用删除后的文档数为分母；全量删除可能计算为 0。
2. 删除保护只写入告警，没有阻止执行脚本继续删除。
3. Direct Ingestion 只记录 API `ACCEPTED`，未轮询最终文档状态。
4. Direct Ingestion Payload 未显式关联 S3 Metadata Sidecar。
5. S3 删除失败被忽略，随后仍可能提升已发布 Manifest。
6. 发布 Manifest 只保存在被忽略的本地目录，不适合临时 CI Runner。
7. 当前 API 已支持 `DeleteKnowledgeBaseDocuments`，但文档仍声明只有 Sync
   可以删除。
8. A2 探针使用 `CUSTOM` Payload 测试 S3 Data Source，并错误使用
   `vectorSearchConfiguration`；错误被吞成零命中。
9. A1 对同一个 Data Source 连续启动 Job，混淆 Rate Limit 与并发限制。
10. 新增流水线没有单元测试覆盖状态、删除、Metadata 和 Manifest Promotion。

建议先把 Direct Ingest/Get/Delete 和 Manifest Promotion 实现为 Fail Closed
状态机，再执行 E01/E06。

## 6. 建议下一个 Agent 处理

### Identity Agent

- 定义 Verified Claims 到 KB User Context/Metadata Filter 的规范。
- 定义 Email、Group、Tenant、离职、撤权和 Session 失效语义。

### Gateway / Policy Agent

- 定义 Managed KB Connector 的允许 Override 白名单。
- 明确 `Retrieve` 与 `AgenticRetrieveStream` 的 Cedar/Resource 范围和补偿控制。
- 增加直接绕过 Gateway 的自动负向测试。

### Observability Agent

- 在目标 Sandbox 执行 E00/E02/E05，验证 Transaction Search、资源级 log
  delivery、ADOT 和跨信号 ID 传播，不得只检查 Console Dashboard。
- 使用 `observability-event.schema.json` 验证脱敏事件，并对 delivery failure、
  Schema rejection、Firehose error backup 和 retention/delete 做故障注入。
- 与 Identity Agent 确认 principal/tenant 的 Tokenization 与访问权限；Schema
  当前不保存这些高敏字段。

### Evaluations Agent

- 将本仓库 Retrieve-only Golden Set 与 AgentCore Evaluation 的会话级指标衔接。
- 给 K2/K3 定义人工标注、Judge 校准和发布阻断阈值。

### Platform Implementation Agent

- 修复 `scripts/21` 至 `23` 的十项状态一致性问题。
- 将发布 Manifest 存入版本化 S3 或 DynamoDB，并支持原子 Promotion。
- 增加 IaC、单元测试和 Disposable Data Source 集成测试。

## 7. 交付物

- `README.md` / `README.en.md`
- `docs/ENTERPRISE_GOVERNANCE_BLUEPRINT.md`
- `docs/CONTROL_BASELINE.md`
- `experiments/README.md`
- `docs/AWS_SAMPLE_CATALOG.md`
- `docs/OBSERVABILITY_BLUEPRINT.md`
- `experiments/observability-evidence.template.md`
- `schemas/observability-event.schema.json`
- `HANDOFF_REPORT.md`

## 8. 未执行事项

- 未部署 AWS 资源。
- 未运行 E00-E07。
- 未修改现有生产/实验 KB。
- 未对现有 Pipeline 实施上述修复。
- 未启用 Transaction Search、vended log delivery、ADOT、Firehose 或 S3 Tables。
- 未读取或提交真实 Metrics、Logs、Traces、账户 ID、ARN 或请求 Payload。
- 本 Handoff 生成时尚未提交或推送；后续发布状态以 Git 历史为准。
