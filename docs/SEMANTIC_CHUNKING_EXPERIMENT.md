# 游戏行业白皮书语义分块对照实验

实验日期：2026-08-04

## 1. 目标

本实验以 AWS Well-Architected Framework 游戏行业视角中文白皮书为对象，检验
第五章“内容与摄入质量”中的四项假设：

- 去除目录、版权页和低价值附录能否降低检索噪声。
- 将物理页 Markdown 改造成结构感知的语义 Chunk，能否提高证据覆盖。
- 为每个 Chunk 增加章节路径、最佳实践编号、页码和稳定 Metadata，能否改善
  溯源和过滤。
- 在 AgentCore Managed Knowledge Base 不能直接配置 Semantic Chunking 的
  条件下，预分块是否是可行替代方案。

## 2. 原始语料问题

修复乱码后的基线语料已经是正确的 UTF-8 Markdown，但仍有以下结构问题：

1. 只有 `PDF 第 N 页` 是 Markdown 标题，原文的支柱、问题、最佳实践、
   `实施指导`、`客户示例` 和 `实施步骤` 没有形成标题层级。
2. 一个最佳实践可能跨越多个物理页；Fixed Size Chunk 会在页码或段落中间切分。
3. 目录页重复了大量标题和编号，可能与正文竞争排序。
4. `GAMESEC05-BP01` 等控制编号没有独立 Metadata，不能直接过滤或审计。
5. 基线结果没有结构化页码 Metadata，只能从 Chunk 文本推断来源。
6. 表格、列表和 PDF 自动换行仍存在词语断裂。

## 3. 技术方案

新增的 `structure-aware-semantic-v1` 不是 Bedrock 原生 Semantic Chunking。
它是在摄入前执行的确定性预处理：

1. 排除物理页 1-7 的封面/目录和 142-146 的低价值尾页。
2. 识别 `GAMExxx` 问题编号和 `GAMExxx-BPxx` 最佳实践编号。
3. 以 `最佳实践`、`实施指导`、`客户示例`、`实施步骤` 和 `资源` 建立子边界。
4. 在段落和句子边界组合内容，目标 420 字符、最大 600 字符。
5. 每个 Chunk 写成独立 Markdown 文件，并生成 Sidecar Metadata。
6. 将实验语料放入独立 S3 Prefix 和独立 Data Source。

每个实验 Chunk 包含：

- 支柱、问题、最佳实践和子段落组成的 `section_path`。
- `source_page_start` 和 `source_page_end`。
- 独立 `document_id` 和共享 `corpus_id`。
- `best_practice_id`、`question_id`、语言、分类和源 PDF Checksum。

Managed KB 仍会执行 `SMART_PARSING` 和服务默认 Fixed Size Chunking，因此
此方案只能提高输入边界的语义完整性，不能保证“一个预处理文件等于一个最终
Vector Chunk”。

## 4. 实验设计

### 4.1 受控参数

| 参数 | 基线 | 实验组 |
| --- | --- | --- |
| Knowledge Base | 同一个 Managed KB | 同一个 Managed KB |
| Embedding | Managed | Managed |
| Search | Managed Search | Managed Search |
| Reranker | Managed | Managed |
| Top-K | 10 | 10 |
| 查询集 | 同一组 8 个中文问题 | 同一组 8 个中文问题 |
| Corpus Filter | `document_id=text-v1` | `corpus_id=semantic-v1` |

### 4.2 摄入变量

| 变量 | 基线 | 实验组 |
| --- | --- | --- |
| 源文件 | 1 个 Markdown | 479 个 Markdown |
| 内容范围 | 146 个物理页 | 物理页 8-141 |
| 上游边界 | 物理页 | 问题/最佳实践/子段落/句子 |
| 目标大小 | 服务默认 300 Tokens | 上游目标 420 字符，服务仍可能再切 |
| 章节 Metadata | 无 | 有 |
| 页码 Metadata | 无 | 有 |

因此这是“数据准备方案”的对照实验，不是只改变 Chunking Algorithm 的单变量
科学实验。目录去噪、结构边界和 Metadata 的效果不能从当前结果中完全拆开。

### 4.3 查询集

查询覆盖：

- 玩家行为与滥用检测。
- 绕过配对系统。
- 账号、交易和虚拟经济欺诈。
- 账户接管与 MFA。
- 玩家遥测分析。
- ML 自动检测。
- 不良行为者响应与封禁。
- 基础设施故障对玩家行为的影响。

Marker Coverage 使用预定义最佳实践编号和关键证据短语进行自动评分。
`Relevant Result` 定义为包含至少一个 Marker 的结果。这种方法可复现，但会把
语义相关、未出现完全相同短语的结果误判为不相关。

## 5. 数据准备结果

| 指标 | 结果 |
| --- | ---: |
| 纳入物理页 | 134 |
| 语义章节 | 332 |
| 生成 Chunk | 479 |
| 识别最佳实践编号 | 79 |
| 丢失最佳实践编号 | 0 |
| 最长 Chunk 正文字符 | 600 |
| 平均 Chunk 正文字符 | 236.1 |
| 中位 Chunk 正文字符 | 188 |
| 少于 100 字符的 Chunk | 105 |
| Unicode Replacement Character | 0 |

摄入统计：

| 指标 | 结果 |
| --- | ---: |
| 扫描文档 | 479 |
| 扫描 Metadata | 479 |
| 新索引文档 | 479 |
| 失败 | 0 |
| 跳过 | 0 |
| 删除 | 0 |

105 个短 Chunk 占 21.9%，多数来自重复或独立的 `实施指导`、`最佳实践` 等结构
标签。这说明 v1 的边界识别偏激进，也是下一轮必须修复的质量问题。

## 6. 检索结果

### 6.1 汇总

| 指标 | Fixed Size 基线 | 语义预分块 | 差值 |
| --- | ---: | ---: | ---: |
| Hit Rate | 1.000 | 1.000 | 0.000 |
| Mean Marker Coverage | 0.938 | 0.969 | +0.031 |
| Mean Reciprocal Rank | 0.854 | 0.768 | -0.086 |
| Mean Relevant Results@10 | 3.88 | 5.63 | +1.75 |
| Mean Top Score | 0.705 | 0.690 | -0.015 |
| Mean Latency | 974.5 ms | 805.3 ms | -169.1 ms |
| Median Latency | 875.3 ms | 866.0 ms | -9.3 ms |

Mean Relevant Results@10 提高约 45.2%，说明实验组在 Top-10 中提供了更多包含
目标证据的 Chunk。Marker Coverage 提高 3.1 个百分点，主要来自“基础设施故障
与玩家行为”用例覆盖了此前缺失的“异常终止”证据。

MRR 下降 0.086，说明更高的证据广度没有稳定转化为更好的首条排序。Mean Top
Score 也略降，但不同 Chunk 边界下的 Score 不应单独作为质量结论。

延迟只为每个用例、每个 Variant 采样一次，且基线首个请求可能包含冷启动影响。
中位延迟只下降约 1.1%，当前不能证明预分块能降低运行时延迟。

### 6.2 分用例

| 用例 | 基线覆盖 | 实验组覆盖 | 基线首个相关排名 | 实验组首个相关排名 | 相关结果数变化 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 玩家行为检测 | 1.00 | 1.00 | 1 | 1 | 3 -> 6 |
| 配对绕过 | 1.00 | 1.00 | 1 | 1 | 6 -> 7 |
| 欺诈检测 | 0.75 | 0.75 | 2 | 2 | 1 -> 1 |
| 账户接管 | 1.00 | 1.00 | 1 | 2 | 4 -> 5 |
| 玩家遥测分析 | 1.00 | 1.00 | 3 | 7 | 4 -> 3 |
| ML 自动检测 | 1.00 | 1.00 | 1 | 1 | 5 -> 6 |
| 滥用响应 | 1.00 | 1.00 | 1 | 1 | 4 -> 9 |
| 故障与玩家行为 | 0.75 | 1.00 | 1 | 1 | 4 -> 8 |

“玩家遥测分析”查询的实验组 Top Results 首先返回 `GAMESEC05` 玩家行为安全
分析，而预设 Marker 更偏向 `GAMEOPS06-BP01` 运营遥测。两者对用户问题都
相关，这同时暴露两个问题：

- 纯词法 Marker 会低估语义相关结果，需要 SME 多级相关性标注。
- Broad Query 应通过 Topic Metadata、Query Decomposition 或 Rerank 调优区分
  “安全检测分析”和“游戏运营优化分析”。

## 7. 结论

在本次语料、查询集和服务版本范围内，结果支持以下结论：

1. **预处理改善了溯源信息。** 实验组可以返回明确章节路径和页码；基线仅提供
   物理页标题。
2. **本次测试中的证据广度提高。** Top-10 相关结果数和 Marker Coverage 均有
   提升，增益主要出现在需要跨多个控制项归纳的安全问题。
3. **排序并未全面改善。** MRR 下降，账户接管和玩家遥测用例的首个目标 Marker
   排名变差。
4. **不能证明性能改善。** 单次延迟样本不足，中位数基本持平。
5. **不能直接归因于 Semantic Chunking。** 实验同时改变了内容范围、结构边界
   和 Metadata。
6. **暂不替换生产基线。** `semantic-v1` 应作为 Canary Corpus，完成 v2 和更大
   Golden Set 后再决定 Promote。

## 8. semantic-v2 建议

下一轮建议只改变一个变量，并逐步评测：

1. 将只有结构标签的短 Section 合并到后续正文，要求少于 100 字符的 Chunk
   比例低于 5%。
2. 从可嵌入正文中删除重复 Source URL 和 Chunk ID，只保留标题、章节路径和
   正文；溯源字段放入 Metadata。
3. 修复跨行标题，例如完整保留 `GAMESEC06-BP02` 标题。
4. 增加 `pillar`、`topic`、`control_family` 和 `evidence_type` Metadata。
5. 为 Broad Query 先识别意图，再按安全、运营、可靠性执行子查询。
6. 将 Golden Set 扩展到 30-50 条，由游戏行业 SME 标注 0/1/2/3 级相关性，
   使用 nDCG@10，而不是只依赖词法 Marker。
7. 每个 Variant 至少重复 20 次性能测试，预热后报告 P50/P95/P99。
8. 分别实验“仅目录去噪”“仅章节 Metadata”“仅预分块”，隔离各变量贡献。

若需要真正的 Bedrock Semantic 或 Hierarchical Chunking，应新建自定义
Knowledge Base，使用 Custom Embedding/Vector Store 路径，与 Managed KB
基线做第三组比较。当前 Managed Embedding API 已实测拒绝显式
`chunkingConfiguration`。

## 9. 复现

```bash
./scripts/14_prepare_semantic_chunks.sh
./scripts/15_ingest_semantic_chunks.sh
PYTHON_BIN=.venv-agentic/bin/python ./scripts/16_compare_semantic_chunking.sh
```

本地运行证据保存在被 Git 忽略的 `artifacts/<RUN_ID>/`：

- `tests/semantic-chunking-preparation-report.json`
- `aws/semantic-chunking-ingestion-job.json`
- `tests/semantic-chunking-comparison.json`
- `tests/semantic-chunking-comparison.md`
- `tests/semantic-chunking-<variant>-<case>.json`

这些文件可能包含账户资源标识和源对象位置，不应提交到 Git。
