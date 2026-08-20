---
title: 遥测事件 Schema 规范
classification: INTERNAL
owner: data-platform
language: zh-CN
lifecycle_status: ACTIVE
---

# 遥测事件 Schema 规范

所有遥测事件遵循统一的信封结构：顶层字段包含 `event_name`（蛇形命名）、`event_version`（语义版本，如 `1.2.0`）、`client_ts`（客户端 Unix 时间戳，毫秒精度）、`server_ts`（服务端接收时间戳）和 `session_id`（UUIDv4）。`payload` 字段承载事件特定属性，字段名全部小写蛇形，禁止使用保留字 `id`、`type`、`source`。

Schema 版本管理遵循语义版本规则：新增可选字段为 PATCH，新增必填字段或修改字段类型为 MINOR，移除字段或重命名为 MAJOR。MAJOR 版本变更需提前 30 天通知下游消费方，并在过渡期内同时维护旧版和新版事件的 Schema Registry 注册。下游消费方须在 90 天内完成迁移，超期后旧版 Schema 进入 deprecated 状态，仅保留 180 天历史数据回放能力。

高频事件（发送频率 > 10 次/分钟/用户）须通过采样策略控制接入量：默认采样率为 1%，可按实验组动态调整，但不得低于 0.1%。采样决策在客户端执行，采样标识写入信封的 `sample_rate` 字段，供服务端解采样时还原总量。禁止在服务端侧二次采样，以避免产生无法修正的系统性偏差。
