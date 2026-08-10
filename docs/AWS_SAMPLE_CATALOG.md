# AWS 官方 Managed Knowledge Base 样例目录

复核日期：2026-08-04

## 1. 来源锁定

### AgentCore Samples

- 仓库：[awslabs/agentcore-samples](https://github.com/awslabs/agentcore-samples)
- Commit：[`fa72a1ed57c0c6c8dcb943b08e66813f8d8e4645`](https://github.com/awslabs/agentcore-samples/tree/fa72a1ed57c0c6c8dcb943b08e66813f8d8e4645)
- Commit 时间：2026-08-04T00:03:48Z

### Amazon Bedrock Samples

- 仓库：[aws-samples/amazon-bedrock-samples](https://github.com/aws-samples/amazon-bedrock-samples)
- Commit：[`0f072b52163145db5d9903846fb537be7b73df1f`](https://github.com/aws-samples/amazon-bedrock-samples/tree/0f072b52163145db5d9903846fb537be7b73df1f)
- Commit 时间：2026-07-21T18:46:37Z

实验执行时必须记录实际 SHA，不能依赖 `main` 长期稳定。

## 2. 使用原则

1. Developer Guide、Release Notes、API Reference 和 Pricing 决定当前事实。
2. `amazon-bedrock-samples/rag/managed-knowledge-bases` 验证 KB 生命周期和特性。
3. `agentcore-samples/.../04-fmkb-managed-kb` 验证 Gateway/Runtime 集成。
4. Notebook 和 helper 只证明示例路径可行，不构成生产安全或合规结论。
5. 所有 sample 都需补齐最小权限、Tags、负向测试、发布门禁、成本和清理复核。

## 3. AgentCore Managed KB 集成样例

固定路径前缀：

`01-features/03-connect-your-agent-to-anything/04-fmkb-managed-kb/`

| 路径 | 能力 | 企业问题 | 生产化差距 |
| --- | --- | --- | --- |
| `README.md` | Runtime → Gateway → Managed KB 总览 | 如何把 KB 暴露为 MCP Tool | 未覆盖多租户、DR、发布门禁 |
| `01-raw-mcp/` | SigV4 Raw MCP、Tool List/Call、Cleanup | 如何隔离 Gateway 与 Agent 故障 | 创建权限较宽；需组织级准入和证据 |
| `02-strands-agent/` | Runtime 中的 Strands Agent 调用 Gateway | Agent Role 与 Gateway Role 如何分工 | 模型 ARN 较宽；无最终用户授权 |
| `utils/gateway.py` | Gateway、KB Target、最小 Retrieve Role | 管理员固定 KB ID 与参数 | 默认允许 Agent 修改 Top-K；需按风险收敛 |
| `utils/managed_kb.py` | 验证 KB 为 `ACTIVE/MANAGED` | 接入前如何做类型门禁 | 不创建、摄入、评测或治理 KB |
| `02-strands-agent/iam/` | Runtime 日志、模型、Gateway 权限 | Runtime 的最小调用面 | ECR/模型通配需按企业资源收敛 |

已确认的关键契约：

- Gateway 使用 `authorizerType=AWS_IAM` 和 SigV4 MCP。
- Runtime Role 需要指定 Gateway 的 `bedrock-agentcore:InvokeGateway`。
- Gateway Role 执行 `bedrock:Retrieve` 和 `bedrock:GetKnowledgeBase`。
- Sample 默认只暴露 `Retrieve`；Agentic Tool 需要独立配置。
- `AgenticRetrieveStream` 当前 IAM Action 不能按 KB ARN 收敛，需记录通配限制。
- Cleanup 不删除 KB，KB 生命周期由 KB Owner 独立负责。

## 4. Amazon Bedrock Managed KB 样例

固定路径前缀：`rag/managed-knowledge-bases/`

| 能力 | 官方路径 | 企业问题 | 生产化补充 |
| --- | --- | --- | --- |
| 最小创建 | `01-getting-started/01-create-bmkb-s3.ipynb` | 如何创建、摄入和查询 | 改为 IaC、最小 Role、状态门禁 |
| Connector | `02-feature-examples/01-data-connectors/` | 如何接 Web/SaaS 数据 | Secret、出站、Owner、撤权与驻留 |
| Chunking/Parsing | `02-feature-examples/02-chunking-and-parsing/` | Smart Parsing 和多模态如何工作 | Corpus QA、Parser 回归和 Canonical 回退 |
| Retrieval | `02-feature-examples/03-retrieval-optimization/01-retrieval-optimization.ipynb` | Hybrid、Top-K、Rerank | Golden Set、Latency、Cost 和 no-answer |
| Metadata | `.../03-retrieval-optimization/02-metadata-filtering.ipynb` | Metadata 如何缩小候选 | Schema、授权、Fail Closed、类型迁移 |
| Agentic | `.../03-retrieval-optimization/03-agentic-retrieval-deep-dive.ipynb` | 多跳、Trace、迭代和多 KB | 与固定 Retrieve A/B；控制迭代成本 |
| RAG Evaluation | `02-feature-examples/04-rag-evaluation/` | AgentCore Evaluation/RAGAS | 人类标注、ACL/Freshness 和发布门禁 |
| Gateway E2E | `03-use-case-example/01-end-to-end-example-with-ac-gateway/` | 完整 Agent 集成 | 防绕过、跨账户、SLO 和回滚 |
| Multi-KB Routing | `03-use-case-example/02-multi-kb-semantic-routing.ipynb` | 跨知识域路由 | 路由错误、权限并集和成本上限 |
| Gateway + Cedar | `03-use-case-example/03-gateway-with-cedar-policies.ipynb` | Tool 层确定性授权 | KB 文档层仍需独立授权 |
| Security Patterns | `04-security-and-access-controls/notebooks/` | IAM、JWT、Cedar、Interceptor、Filter | 建立唯一身份真值和跨层一致性 |
| Observability | `05-Observability/` | KB Metrics、Gateway OTEL 和日志 | CloudTrail Data Events、敏感日志、集中观测 |
| Responsible AI | `06-Responsible AI/` | Guardrails 和 Grounding | 不替代授权；Raw Chunk 需独立保护 |
| CloudFormation | `07-IaaC/managed_kb_cfn/` | S3 + Role + KB + Data Source | cfn-nag、KMS、Policy、日志、蓝绿 |
| CDK | `07-IaaC/managed_kb_cdk/` | 可配置 IaC | Pin CDK、Aspect、Pipeline 和 Drift |
| Cost Helper | `utils/kb_cost.py` | 实验成本估算 | 以官方 Pricing 复核，不作为账单真值 |

## 5. 安全样例的八层递进

`04-security-and-access-controls/notebooks/` 包含：

| # | 路径 | 验证重点 |
| --- | --- | --- |
| 1 | `01-direct-sdk.ipynb` | IAM 直接调用 |
| 2 | `02-metadata-filters.ipynb` | 文档范围过滤 |
| 3 | `03-gateway-iam.ipynb` | Gateway IAM 边界 |
| 4 | `04-gateway-cedar.ipynb` | Tool/Target Cedar |
| 5 | `05-gateway-jwt-cognito.ipynb` | 最终用户 JWT |
| 6 | `06-gateway-jwt-cedar.ipynb` | JWT Claims 与多租户 Policy |
| 7 | `07-gateway-interceptor.ipynb` | 动态 Filter 注入 |
| 8 | `08-full-governance.ipynb` | JWT + Cedar + Interceptor + Filter |

企业采用时不能简单认为第 8 个 Notebook 即“完整治理”。仍需验证：

- 用户身份是否由受信 Claims 生成，Interceptor 是否允许客户端覆盖。
- Cedar 只保护 Gateway Tool，还是同时存在直接 KB 绕过路径。
- Metadata Filter 缺失、类型错误和空结果是否 Fail Closed。
- 数据层、日志、缓存和完整文档读取是否遵守相同授权。
- 撤权、Group 变更、Session 和 Connector ACL 更新的实际窗口。

## 6. 已观察到的样例漂移与不一致

### 6.1 Direct Ingestion 数量

当前 Developer Guide 的 Direct Ingestion 页面仍出现“API 每次最多 25 份文档”
的通用表述，而 Managed KB 配额和本项目目标 API 模型为每请求 10 份。Managed
流程必须以目标 Region 的 API Model 和 Managed Quota 页为准，并在 E00 验证。

### 6.2 Hybrid Search Override

Bedrock sample 的 Retrieval README 描述 Managed KB Hybrid Search “始终开启且
不可配置”；AgentCore Gateway Connector 文档同时列出 `overrideSearchType`
的 `HYBRID`/`SEMANTIC` 可选值。执行前必须以目标 API 行为做契约测试，不能让
sample 文案决定生产参数。

### 6.3 Managed Embedding 与 Chunking

Bedrock sample 展示 Fixed Size/No Chunking；本仓库 2026-08-03 实测发现
`embeddingModelType=MANAGED` 时显式 `chunkingConfiguration` 被 API 拒绝。
该差异可能是 Region、API 版本或服务演进造成。E00/E01 必须重新验证，并将结果
记录为账户/Region 事实。

### 6.4 Sample 权限

Bedrock sample Quick Start 建议 `AmazonBedrockFullAccess` 便于教学；这不满足
生产最小权限基线。AgentCore Runtime sample 的 ECR 和模型资源也包含通配。
生产必须使用具体 Action/ARN，并通过 Access Analyzer 验证。

### 6.5 创建职责

AgentCore sample 文案曾描述 helper 可“create/reuse managed KB”，但当前
`utils/managed_kb.py` 只校验传入 KB 为 `ACTIVE/MANAGED`，实际创建属于
`amazon-bedrock-samples`。实验文档应按当前代码行为分工。

## 7. 运行 Sample 前检查

```text
[ ] 固定仓库 commit、SDK、CLI、CDK 和 Python 版本
[ ] 使用 Sandbox 账户和临时 Role
[ ] 不使用 AmazonBedrockFullAccess 作为生产模板
[ ] 将 IAM 和 Resource Policy 收敛到具体资源
[ ] 使用公开/脱敏语料并建立 Manifest
[ ] 增加允许、拒绝、跨租户、绕过和 stale-content 测试
[ ] 等待 Ingestion/Document 最终状态
[ ] 检查日志中的 Token、PII 和 Raw Chunk
[ ] 建立预算、配额、告警、回滚和 ExpiresAt
[ ] Cleanup 后按 Tag 和资源清单确认零残留
```
