# 端到端验收记录

日期：2026-08-18
账户：已脱敏（`<account>`）
Region：us-east-1
Corpus：`demo-02`，语料为 `examples/corpus/` 的 13 篇 Markdown
提交：`63e4653` 之后的工作树

本记录在一套**全新部署**上完成。此前一轮验收因为用手写脚本反复修补 S3 导致
状态不可信，已销毁重建。本轮所有状态变更只通过 `cli/publish.py`，唯一例外是
路径二故意篡改一个对象——那是被测行为本身。

## 部署

```
cdk deploy --all -c corpusId=demo-02
```

三个 Stack 均 `CREATE_COMPLETE`。

资源标识按 `SECURITY.md` 脱敏——该策略禁止提交存活的知识库、数据源和桶标识。

| 输出 | 值 |
| --- | --- |
| KnowledgeBaseId | `<kb-id>` |
| DataSourceId | `<data-source-id>` |
| Canonical 桶 | `managedkbfoundation-canonicalbucket<suffix>` |
| Registry 桶 | `managedkbfoundation-registrybucket<suffix>` |
| Release 表 | `ManagedKbRelease-ReleaseTable<suffix>` |

**Managed KB 创建耗时约 24 分钟**，远超用户指南所述的 2–5 分钟。这一次是在同区域
刚删除另一个 Managed KB 之后创建的，两者可能相关，但未验证因果。CI 中若设超时，
不应按 5 分钟设定。

## 路径一：正常发布

```
cli.publish --source-dir examples/corpus --corpus-id demo-02
```

执行 `demo-02-20260818T085926Z-221a97e7`：**SUCCEEDED**。

九步全部通过，13 篇文档达到 `INDEXED`，冒烟检索命中。Registry：

```
POINTER.activeReleaseId = demo-02-20260818T085926Z-221a97e7
RELEASE#…status         = ACTIVE
```

## 路径二：损坏文档阻断

用一个与 Manifest 不符的哈希覆盖 `security/fraud/account-takeover.md`
（**本次发布未变更的文档**），然后发布对另一篇文档的修改。

执行 `demo-02-20260818T090630Z-d07d2d2b`：**FAILED**。

```
ReadPointer → MergePointer → IsChangeSetEmpty → CreateReleaseRecord
→ VerifyS3Consistency → GateAChoice → FailRelease → ReleaseFailed
```

门禁 A 拦下，执行从未进入摄入，POINTER 未变。

**已知局限**：篡改要等到下一次发布才会被发现，且只有当该次发布的 Manifest 覆盖
到被篡改文档时才会。系统没有独立的漂移检测。恢复语料后再次发布会报
"No changes detected"，被篡改的对象仍留在桶里——这是本轮实测观察到的。

## 路径三：超限删除硬失败

以 5 篇的语料发布，相当于删除 8/13（62%），超过 50% 阈值，且不带
`--allow-bulk-deletion`。

```
refusing to delete 8 of 13 documents (62%), over the 50% threshold.
Nothing has been changed. Re-run with --allow-bulk-deletion to proceed.
```

退出码 1。S3 内容对象数**前后均为 13**，状态机从未启动。

这一条此前是失败的：删除发生在门禁之前，同样的操作会先删掉 8 篇再在门禁 A 失败。
修复见 `63e4653`。

## 路径四：并发发布被条件写拒绝

修改两篇文档后，间隔 3 秒启动两次发布。两者 `releaseId` 不同，
`expectedPreviousReleaseId` 相同。

| 执行 | 结果 |
| --- | --- |
| `…090138Z-5f400619` | SUCCEEDED |
| `…090141Z-5f400619` | FAILED |

败者走完全部四道门禁，在 `PromoteRelease` 被拒：

```
ConcurrentPromotionError: active pointer for demo-02 is no longer
demo-02-20260818T085926Z-221a97e7; another release won the race
```

POINTER 指向胜者。原子 Promotion 在真机并发下成立。

## 增量与空变更

- 改 1 篇发布：SUCCEEDED，`parentReleaseId` 指向前一版本，前一版本转 `SUPERSEDED`。
- 语料未变时发布：报 "No changes detected"，不创建 Release 记录，不启动执行。

## 结论

四条路径全部符合预期。贯穿全部失败路径的一致行为是：执行终止于
`ReleaseFailed`，POINTER 从未被错误修改。

需要与本记录一并阅读的两项：

- 上文路径二的已知局限（无独立漂移检测）。
- A1/A2 两条假设已由官方文档给出结论，未再跑探针，见 `docs/adr/`。
