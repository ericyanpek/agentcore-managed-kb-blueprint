---
title: 数据分类标准
classification: INTERNAL
owner: governance
language: zh-CN
lifecycle_status: ACTIVE
---

# 数据分类标准

公司数据按敏感程度分为四级：PUBLIC（可公开）、INTERNAL（内部使用）、CONFIDENTIAL（保密）和 RESTRICTED（严格受限）。分级依据包括数据主体的隐私保护要求、监管合规义务和业务竞争敏感性三个维度，任一维度触达高级别则整体采用较高级别。玩家个人可识别信息（PII）最低定为 CONFIDENTIAL，支付卡数据和身份证件信息定为 RESTRICTED。

数据创建者有义务在数据集或文档创建时标注分类标签。标签以 YAML 前置元数据形式写入文档，以列字段形式写入数据表 DDL，以对象标签（Object Tag）形式写入存储桶和对象。未标注的数据默认视为 CONFIDENTIAL 处理，由数据治理团队在季度审计中补全标注。已标注数据若因内容变更导致分级不匹配，任何人可提交降级申请，升级无需申请但须通知数据治理团队。

跨分级数据组合（如将 INTERNAL 字段与 CONFIDENTIAL 字段联合输出）的合并结果须采用较高分级。分析查询结果集的分类由查询中引用的最高级别字段决定，此规则也适用于通过 BI 工具导出的报表和数据集快照，导出权限须经数据所有者（Data Owner）审批，审批记录保留 3 年。
