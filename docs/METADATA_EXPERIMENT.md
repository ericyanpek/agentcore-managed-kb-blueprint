# Managed Knowledge Base Metadata 对照实验

实验日期：2026-08-04

## 1. 目标

本实验回答三个问题：

1. Managed Knowledge Base 的文档 Metadata 存在哪里，如何发布和更新？
2. 只用于过滤/治理的 Metadata 是否会改变检索质量？
3. 将语义 Metadata 加入 Embedding 是否能改善游戏行业白皮书的检索？

## 2. Metadata 存储模型

Metadata 不是只存在一个位置，而是四层派生关系：

| 层 | 存储位置 | 角色 |
| --- | --- | --- |
| Source of Truth | 与正文相邻的 S3 `<文件名>.metadata.json` | 可重建、可版本化的权威输入 |
| Ingestion | Connector + `StartIngestionJob` | 校验 Sidecar 并复制字段 |
| Retrieval Index | Managed KB 内部托管索引 | 将字段附着到 Chunk，用于 Filter、返回和可选 Embedding |
| Evidence | `artifacts/<RUN_ID>/` 的 Job/检索响应 | 审计摄入、字段和 Filter 是否生效 |

对于 `chunk-0203.md`，Sidecar 必须命名为
`chunk-0203.md.metadata.json`，并放在同一个 S3 Prefix。AWS 官方限制单个
Sidecar 不超过 10 KB。

`includeForEmbedding` 决定字段如何进入索引：

- `false`：字段仍被存储、返回并可过滤，但不改变 Chunk 的向量输入。
- `true`：字段键值会与 Chunk 文本拼接后参与 Embedding；Retrieve 返回的
  `content.text` 仍是原始正文。

因此推荐把授权、生命周期、Checksum、页码和 Owner 保持为 `false`。标题、
主题、章节路径和稳定控制编号可以 A/B 测试后再决定是否设为 `true`。

官方参考：[Connect to Amazon S3 for your knowledge base](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html)。

## 3. 实验设计

### 3.1 三个 Variant

| Variant | Sidecar | 参与 Embedding 的 Metadata |
| --- | --- | --- |
| `no-metadata` | 无 | 无 |
| `filter-metadata` | 完整 | 无，全部为 `false` |
| `embedded-metadata` | 完整 | 标题、领域、语言、支柱、主题、章节路径、问题 ID、最佳实践 ID |

三组各有 479 份 Markdown，文件名和正文 UTF-8 Bytes 完全一致。实验从
`semantic-v1` 删除重复的 Source URL、Chunk ID 和章节 Header，避免这些内容
已经出现在正文中而掩盖 Metadata 的效果。

三个 Variant 使用同一个 Managed KB，但分别位于独立 S3 Prefix 和 Data Source。
主检索使用服务自动生成的 `_data_source_id` Filter 隔离，因此无 Metadata 组
不需要加入人为路由字段。

### 3.2 固定参数

- Managed Embedding
- `SMART_PARSING`
- 服务默认 Fixed Size Chunking
- Managed Search
- Managed Reranking
- Top-K 10
- 同一组 8 个中文问题和 Evidence Markers

实验同时执行 `pillar`、`topic`、`best_practice_id` 和
`classification + lifecycle_status + version_date` Filter 验证。

## 4. Metadata 字段设计

| 类型 | 本实验字段 | 建议 |
| --- | --- | --- |
| 授权/治理 | `classification` | 只过滤，不 Embedding |
| 生命周期 | `version_date`、`lifecycle_status` | 只过滤，不 Embedding |
| 责任 | `owner` | 只过滤，不 Embedding |
| 溯源 | 页码、源/内容 SHA-256、`document_id` | 只返回/过滤 |
| 路由 | `domain`、`language`、`pillar` | 默认只过滤，按查询实验 |
| 语义 | `title`、`topic`、`section_path`、控制编号 | 可做 Embedding A/B |

`filter-metadata` 与 `embedded-metadata` 平均每个 Sidecar 有 19.67 个字段。
Sidecar 最大 3,586 Bytes，低于 10 KB 限制。

## 5. 数据准备与摄入

| 指标 | 无 Metadata | Filter Metadata | Embedded Metadata |
| --- | ---: | ---: | ---: |
| Markdown 文档 | 479 | 479 | 479 |
| 扫描 Sidecar | 0 | 479 | 479 |
| 新索引文档 | 479 | 479 | 479 |
| 失败 | 0 | 0 | 0 |
| 跳过 | 0 | 0 | 0 |

准备阶段确认三组正文集合 SHA-256 完全一致。共享语料仍有 35 个重复正文和最短
4 字符的过短 Chunk；它们不影响 Metadata 单变量控制，但属于 `semantic-v2`
应修复的数据质量问题。

## 6. 检索结果

| 指标 | 无 Metadata | Filter Metadata | Embedded Metadata |
| --- | ---: | ---: | ---: |
| Hit Rate | 1.000 | 1.000 | 1.000 |
| Mean Marker Coverage | 0.906 | 0.906 | 0.906 |
| MRR | 0.875 | 0.875 | 0.875 |
| Mean Relevant@10 | 4.125 | 4.125 | 4.125 |
| Mean Top Score | 0.6983 | 0.6977 | 0.6981 |
| 返回 Metadata 完整度 | 0% | 100% | 100% |
| 单次均值延迟 | 1,042.5 ms | 981.8 ms | 804.0 ms |

8 个用例的命中、覆盖、排名和相关结果数完全相同。Top Score 的微小变化不足以
形成质量结论。每个 Query/Variant 只运行一次，延迟数据也不能证明性能差异。

本实验没有观察到语义 Metadata 参与 Embedding 的增益。可能原因包括查询文本
已经与正文高度匹配、Metadata 与正文重复、Managed Reranker 消除了候选排序
差异，以及 8 条查询样本过小。结论只能是“本次未测得改善”，不能解释为该能力
永远无效。

## 7. Filter 结果

两个有 Metadata 的 Variant 均通过：

| Filter | 返回数 | 满足 Filter |
| --- | ---: | ---: |
| `pillar=安全性` | 10 | 10/10 |
| 精确 `topic` | 1 | 1/1 |
| `best_practice_id=GAMESEC05-BP01` | 2 | 2/2 |
| 公开 + Active + 版本下限 | 10 | 10/10 |

负对照中，无 Metadata 组叠加 `classification=PUBLIC` 后返回 0 条。这证明
Sidecar 的确定性价值是路由、权限、生命周期、溯源与治理，而不是保证提升
Semantic Relevance。

## 8. 扩展召回实验

为避免 8 条自然语言问题掩盖 Metadata 的作用，第二轮将 Query Set 扩大到 44 条：

| 类别 | 数量 | 目的 |
| --- | ---: | --- |
| 自然业务问题 | 8 | 验证正常问答质量不退化 |
| 控制项子章节 | 12 | 用 `best_practice_id` 定位“实施步骤/客户示例/资源” |
| 主题子章节 | 12 | 用 `topic` 定位主题下的具体子章节 |
| 问题查找 | 12 | 用 `question_id` 查找问题概述和最佳实践 |

每条查询先对三个 Variant 分别运行 Managed Rerank 和 No Rerank，共 264 次未过滤
Retrieve；再对 36 条可确定业务键的生成查询，在两个有 Metadata 的 Variant 上
运行相同两种模式，共 144 次带 Filter Retrieve。总计 408 次调用，Top-K 固定为
10。

### 8.1 未过滤召回

| 模式 | Variant | Hit@1 | Hit@10 | MRR | Recall@10 | nDCG@10 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Managed Rerank | No Metadata | 0.318 | 0.409 | 0.358 | 0.170 | 0.190 |
| Managed Rerank | Filter Metadata | 0.318 | 0.409 | 0.356 | 0.170 | 0.188 |
| Managed Rerank | Embedded Metadata | 0.318 | 0.409 | 0.356 | 0.170 | 0.188 |
| No Rerank | No Metadata | 0.273 | 0.341 | 0.297 | 0.098 | 0.131 |
| No Rerank | Filter Metadata | 0.273 | 0.341 | 0.297 | 0.098 | 0.131 |
| No Rerank | Embedded Metadata | 0.273 | 0.341 | 0.297 | 0.098 | 0.131 |

Embedded Metadata 相对 No Metadata 在 Managed Rerank 下的 MRR 均值差为
`-0.002`，95% Bootstrap CI 为 `[-0.006, 0.000]`；No Rerank 下四项主要指标
全部持平。额外使用精确 `topic` 文本查询时，Filter-only 与 Embedded 两组返回
相同的 Top-10 文档顺序和近乎相同的 Score。

因此，扩大 Query Set 和移除 Reranker 后仍未观察到 `includeForEmbedding=true`
的增益。本结果只适用于本次 Managed KB、同一 KB 内的三个 Data Source 和当前
服务版本。相同正文可能触发服务内部的 Embedding 复用或去重；如果要严格验证
Metadata Embedding，应使用隔离的 KB/索引复验，不能把本结果推广为该能力始终
无效。

### 8.2 Runtime Filter 增益

| 模式 / Variant | 未过滤 MRR | Filter MRR | Delta | 未过滤 Recall@10 | Filter Recall@10 | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Managed / Filter Metadata | 0.241 | 0.556 | +0.315 | 0.078 | 0.342 | +0.264 |
| Managed / Embedded Metadata | 0.241 | 0.556 | +0.315 | 0.078 | 0.342 | +0.264 |
| No Rerank / Filter Metadata | 0.222 | 0.556 | +0.333 | 0.022 | 0.342 | +0.319 |
| No Rerank / Embedded Metadata | 0.222 | 0.556 | +0.333 | 0.022 | 0.342 | +0.319 |

Filter-only Variant 的配对 Bootstrap 结果：

| 模式 | 指标 | 均值差 | 95% CI | 改善 / 持平 / 退化 |
| --- | --- | ---: | --- | ---: |
| Managed | MRR | +0.315 | [+0.162, +0.463] | 12 / 24 / 0 |
| Managed | Recall@10 | +0.264 | [+0.125, +0.403] | 10 / 26 / 0 |
| No Rerank | MRR | +0.333 | [+0.194, +0.500] | 12 / 24 / 0 |
| No Rerank | Recall@10 | +0.319 | [+0.181, +0.472] | 12 / 24 / 0 |

增益集中在 `best_practice_id`：

| 类别 | Managed MRR | Filter 后 | Managed Recall@10 | Filter 后 |
| --- | ---: | ---: | ---: | ---: |
| 控制项子章节 | 0.056 | 1.000 | 0.167 | 0.958 |
| 问题查找 | 0.667 | 0.667 | 0.067 | 0.067 |
| 主题子章节 | 0.000 | 0.000 | 0.000 | 0.000 |

12 个控制项子章节全部在 Rank 1 命中，且没有用例退化。一个控制项对应两份预期
文档，但 Top-K 只返回其中一份，因此 Recall@10 为 0.958 而不是 1.000。

### 8.3 为什么有些 Filter 返回空集

Metadata Filter 是候选约束，不是确定性主键读取。Managed Search 在 Filter 后
仍执行语义检索和内部相关性门槛：

- 原始 `topic-subsection-01` 查询叠加精确 `topic` Filter 时返回 0 条。
- 保持同一个 Filter，只把查询改成目标正文片段后，返回 3 条正确文档。
- 这证明 Sidecar 和 Filter 值有效；空集来自过滤后候选正文与查询的语义匹配
  不足，而不是 Filter 没有命中。

因此，Runtime Filter 最适合在应用已经解析出稳定业务键后缩小候选集。若要按
`document_id` 确定性读取全部文档，不能把向量 Retrieve 当作文档数据库查询；
应从 S3/内容系统读取，或维护可按主键访问的 Canonical Store。

## 9. 结论与选择

1. **生产语料应有 Metadata。** 无 Metadata 时无法执行分类、版本、主题或控制项
   Filter，也没有业务溯源字段。
2. **默认不让治理字段参与 Embedding。** 这可避免租户、ACL、时间戳、Checksum
   等噪声影响向量。
3. **语义字段按 Query Set 实验。** 本语料暂时选择 `filter-metadata` 作为默认，
   因为它获得全部治理能力，而 44 条扩展回归仍没有显示 Embedded Variant 的
   质量增益。
4. **Filter 必须在应用层强制。** S3 对象权限不会自动继承到 KB Retrieve 调用。
5. **不能用 Metadata 代替正文质量。** 4 字符短块和重复正文仍需在下一轮分块中
   修复。
6. **优先过滤稳定、低基数业务键。** `best_practice_id` 的实测收益明确；长
   `topic` 只适合作为路由提示，仍需保证查询与目标正文语义相似。
7. **Filter 空结果应 Fail Closed。** 权限 Filter 不能因空集而降级为无 Filter
   重试；内容查找可改写查询或转到 Canonical Store。

## 10. 更新与治理

推荐发布流程：

1. 在内容系统或版本化 Manifest 维护 Metadata 字典和 Owner。
2. 原文与 Sidecar 在同一次变更中更新，计算内容/源文件 Checksum。
3. 检查 Sidecar 配对、10 KB 限制、必填字段、枚举和类型稳定性。
4. 上传到 Canary Prefix，显式调用 `StartIngestionJob`。
5. 检查正文/Metadata 扫描数、失败和跳过数。
6. Retrieve 验证字段返回、正向 Filter、越权负对照和陈旧版本排除。
7. 运行 Golden Set 后切换应用的 Data Source/Version Filter。
8. 保留旧版本到回滚窗口结束，再按保留策略删除。

Sidecar 变化不会自动成为可检索状态；必须重新运行摄入。Metadata 类型发生变化
时应创建新字段或新 Corpus 版本，避免同一 Key 在索引中出现不一致类型。

## 11. 复现

```bash
./scripts/17_prepare_metadata_experiment.sh
./scripts/18_ingest_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/19_compare_metadata_experiment.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/20_expand_metadata_retrieval.sh
```

扩展实验原始产物：

- `metadata-expanded-query-set.json`：44 条版本化 Query Set 和弱标签。
- `metadata-expanded-<mode>-<variant>-<case>.json`：264 份未过滤响应。
- `metadata-runtime-filter-<mode>-<variant>-<case>.json`：144 份 Filter 响应。
- `metadata-expanded-comparison.json`：逐项指标、类别汇总和配对 Bootstrap。
- `metadata-expanded-comparison.md`：自动生成的精简结果。

原始响应保存在被 Git 忽略的 `artifacts/<RUN_ID>/`，不得提交账户 ID、ARN、
Bucket、Data Source ID 或 `_source_uri`。
