# ADR-003: Manifest 权威副本存 S3，状态与指针存 DynamoDB

- 状态：已接受
- 日期：2026-08-18

## 背景

每次发布产生一个 Manifest（记录该版本所有文档的 S3 路径、SHA-256、元数据），需要
持久存储。同时，发布过程需要跟踪当前活动版本（"哪个 Manifest 是现在的线上版本"），
并要求在并发发布时原子性地切换指针，避免两条流水线互相覆盖。

## 决策

存储职责按内容类型分离：

- **S3 registry 桶**：存储 Manifest 文件本身，路径为
  `manifests/<corpusId>/<releaseId>.json`，桶开启版本化，不设过期规则。
  Manifest 一经写入不可修改，内容以 S3 版本 ID 寻址。
- **DynamoDB release table**：存储发布状态（`PREPARING` → `INGESTING` → `TESTING`
  → `ACTIVE` → `SUPERSEDED` / `FAILED`）与活动版本指针（`POINTER` 记录）。
  DynamoDB 持有的是指针与状态，不持有 Manifest 内容；DynamoDB 记录中仅保存
  `manifestS3Uri` 与 `manifestS3VersionId`。

Promotion 时对 `POINTER` 记录执行条件写，`ConditionExpression` 为：

```
attribute_not_exists(activeReleaseId) OR activeReleaseId = :expectedPrevious
```

`expectedPrevious` 在执行开始时读取并存入执行上下文，Promotion 时使用该值。

## 理由

**S3 保存 Manifest 内容的权威副本。** 即使 DynamoDB 表被误删或损坏，版本化 S3
桶中的每份历史 Manifest 仍可完整恢复。重建发布状态只需重新
扫描桶内对象并重写表记录，无需重新执行摄入。相反，Manifest 内容全量存入 DynamoDB
时，表的删除或误操作会同时抹除状态与内容，且 DynamoDB 项目大小上限（400 KB）可能
不足以存储大规模语料的 Manifest。

**指针的原子切换使用 DynamoDB 条件写。** S3 的条件写
（`If-None-Match`）仅保证对象不存在时才写入，无法表达"当前值等于预期值时才更新"的
CAS 语义。DynamoDB 的 `ConditionExpression` 原生支持此语义，后到的并发执行会收到
`ConditionalCheckFailedException` 并进入 `FAILED`，而不是静默覆盖。

**Promotion 的三步顺序约束中间状态。** 实现中（`store.py:
promote_release`）三步顺序为：

1. **将本次发布状态标记为 `ACTIVE`**。读者跟随指针时不会落到一个状态不是 ACTIVE
   的记录，因此必须先写这一步。
2. **条件写 `POINTER`，将指针移向本次发布**。以执行开始时观察到的
   `expectedPrevious` 为前提条件，并发流水线中后到者在此步失败。
3. **将前一次发布标记为 `SUPERSEDED`**。仅在指针实际已移动后才执行。若先于步骤 2
   执行，当步骤 2 随后失败（条件写不满足），就会出现指针未变但旧发布已被标记为
   SUPERSEDED 的矛盾状态——即仍在线上的发布却被标记为已替换。

**失去竞争时回滚 ACTIVE 标记。** 若步骤 2 的条件写失败（另一条流水线先行提升），
实现立即将本次发布从 `ACTIVE` 回写为 `FAILED`，避免出现"有 ACTIVE 记录但指针不
指向它"的孤立状态。

## 后果

- 历史 Manifest 可从 S3 独立恢复，不依赖 DynamoDB 是否完整。
- 并发发布竞争由 DynamoDB 条件写承担，无需应用层锁或队列。
- 后续回滚能力可直接基于现有数据模型实现：读旧 `SUPERSEDED` 记录中的
  `manifestS3Uri`，重新摄入其文档，再次执行条件写切换指针，无需 schema 迁移。
- S3 版本化会随时间积累版本，需要定期评估是否添加生命周期规则；当前实现不设过期，
  视 Manifest 为永久审计证据。

## 备选方案

| 方案 | 落选理由 |
| --- | --- |
| Manifest 内容全量存入 DynamoDB | 受 400 KB 项目大小限制，大语料 Manifest 无法存入；表误删同时丢失内容与状态，恢复路径更复杂 |
| 仅用 S3 存储所有发布数据（内容 + 状态 + 指针） | S3 不支持原子 CAS 条件写，并发安全无法保证；活动指针需要客户端串行或引入额外协调机制 |
| 使用 RDS / ElastiCache 等关系型/缓存存储 | 引入有状态服务依赖，增加运维复杂度；DynamoDB 的无服务器特性更契合参考实现定位 |
