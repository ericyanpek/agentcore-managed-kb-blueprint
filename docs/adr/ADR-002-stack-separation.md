# ADR-002: Stack 按 stateful 与 stateless 切分，KB 独立并开启终止保护

- 状态：已接受
- 日期：2026-08-18

## 背景

CDK 部署产生三个 Stack：`FoundationStack`（KMS、S3 canonical、S3 registry、
CloudWatch LogGroup）、`KnowledgeBaseStack`（IAM KB service role、
`CfnKnowledgeBase`、`CfnDataSource`）、`ReleaseStack`（DynamoDB、Lambda x3、
状态机、IAM publisher role）。

初始设计将 KB 与发布基础设施合并为一个 Stack，但调研 CloudFormation 资源 Schema
后发现存在结构性约束，迫使重新划分。

## 决策

将三个 Stack 按**是否持有状态**划分：

- **FoundationStack**：持有状态，终止保护开启。KMS CMK、两个 S3 桶均设
  `RemovalPolicy.RETAIN`；LogGroup 设 `DESTROY`（日志不是可靠性数据）。
- **KnowledgeBaseStack**：持有状态，终止保护开启。KB 资源与 Data Source 均设
  `RemovalPolicy.RETAIN`；KB 独立成 Stack 是核心决策（见下文）。
- **ReleaseStack**：不持有无法重建的状态，可自由销毁重建。DynamoDB 表设
  `RemovalPolicy.RETAIN` 以保留审计记录；状态机与 Lambda 本身无状态。

Stack 间通过 CloudFormation Export 传递标识，不使用跨 Stack 直接对象引用，以便
ReleaseStack 独立重建时不依赖其他 Stack 的对象。

## 理由

**`ManagedKnowledgeBaseConfiguration` 为 create-only 是决定性约束。** 经核实的
CloudFormation 资源 Schema（核实日期 2026-08-17）显示，
`KnowledgeBaseConfiguration/ManagedKnowledgeBaseConfiguration` 字段标记为
`createOnly`。这意味着修改 embedding 配置——例如更换 embedding 模型类型——会触发
CloudFormation 替换该资源，新 KB 的向量索引为空，原有索引内容全部丢失。

将 KB 与发布基础设施置于同一 Stack 意味着：重建发布逻辑（更新 Lambda 代码、调整
状态机拓扑）等任何操作都潜在地威胁向量索引。隔离成独立 Stack 并开启终止保护，使
`cdk deploy ReleaseStack` 与 KB 物理解耦。

**单 KMS CMK 满足单账户单 Region 参考实现的需求。** 三个 Stack 共用同一把 CMK
是有意选择：多把 CMK 只增加密钥策略调试成本，不带来隔离收益。CMK 本身存于
FoundationStack 并导出 ARN，其他 Stack 通过 `kms.Key.fromKeyArn()` 引用。

**`DataDeletionPolicy=RETAIN` 防止误操作清空索引。** Data Source 删除时设为 RETAIN，
即删除 CFN 资源不会清空知识库中对应的文档。这是防御性设定：索引内容重建代价高，
而 RETAIN 不妨碍通过 API 显式删除文档。

**canonical 与 registry 分桶是生命周期语义不同的必然结果。** Manifest 是审计证据，
永不过期；canonical 桶的非当前版本设 30 天自动清理（文档可重新上传）。分桶避免
生命周期规则相互干扰，也让权限边界更清晰（KB service role 只读 canonical 桶）。

## 后果

- `cdk destroy ReleaseStack` 可安全执行，不触及向量索引与历史 Manifest。
- KMS CMK、两个 S3 桶、DynamoDB 表、KB 与 Data Source 均为 `RETAIN`，执行
  `cdk destroy` 后这些资源仍存在于账户中，需要手工清理——这是有意取舍，防止误操作
  不可逆地销毁索引或审计证据。
- Stack 间解耦通过 CloudFormation Export 实现，删除 KnowledgeBaseStack 前须先
  删除消费其 Export 的 ReleaseStack（CloudFormation 不允许删除被引用 Export 的
  Stack）。
- 终止保护意味着销毁 FoundationStack 或 KnowledgeBaseStack 前需要先手工关闭保护。

## 备选方案

| 方案 | 落选理由 |
| --- | --- |
| 单一 Stack | KB 资源与发布基础设施耦合，任何发布逻辑变更都潜在威胁向量索引；`cdk destroy` 无法安全执行 |
| KB 与 Foundation 合并为一个 stateful Stack | 减少 Stack 数量，但语义混乱：KMS 与 S3 的生命周期与 KB 不同，且 KB 独立成 Stack 更便于日后迁移 embedding 配置时执行受控替换 |
