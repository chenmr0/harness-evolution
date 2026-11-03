# Agent Note: Skill 进化能力接入

Status: implemented

[English](2026-10-30-skill-evolution-integration.en.md) | 中文

## Problem

现有进化流程具备任务状态和 Git 工作区，但聚类、修改与评测仍依赖占位实现。Skill 进化需要可追踪的模型决策、实际多文件修改和可恢复的外部评测。

## Decision

在现有任务流程中接入 Skill 聚类、去重、核查、排序、补丁与反馈；保存不可变输入快照和带版本检查的过程状态。候选使用 Git 分支供平台评测，按相同 case 的 total_score 算术平均分严格提升决定接纳，并在结束时独立复测基线和最佳版本。真实模型和评测由服务端显式启用并配置，凭据从环境读取，不写入任务快照；历史数据不迁移。

## 配置与运行

服务端同时设置 `SKILL_EVOLUTION_MODEL_ENABLED=1` 和 `SKILL_EVOLUTION_EVAL_ENABLED=1` 才启用真实 Skill 流程；分别配置 `SKILL_EVOLUTION_MODEL_URL`、`SKILL_EVOLUTION_MODEL`、`SKILL_EVOLUTION_MODEL_KEY_ENV`、`SKILL_EVOLUTION_EVAL_URL`、`SKILL_EVOLUTION_EVAL_TOKEN_ENV` 与 `SKILL_EVOLUTION_EVAL_TEMPLATE_ID`。密钥内容放在对应环境变量中，不写入请求或仓库。`HE_SKILL_STRATEGY_JSON` 可配置进化策略；默认使用内置策略。真实评测要求可推送到目标 Git 仓库。新建任务冻结真实模式，恢复时不可降级到模拟流程。

CodeAgent 与 HTTP 模型共享阶段提示和输出校验。CLI 通过标准输入接收报告快照，仅启用阶段提交工具，关闭内置工具、设置继承和会话持久化；日志只保留脱敏调用元数据，输入与结构化产物存隔离目录；超时或取消时清理整个进程组。冻结模型二进制与端点身份防止恢复时切换实现。完成事务同步终态游标，输入计数来自冻结报告而非当前上游，报告与来源问题分别展示。

CLI 使用阶段 JSON Schema 描述聚类、核查、补丁和反馈输出；Markdown 替换若只有标题和省略符则拒绝，避免占位文本抹掉规则。这是有界完整性检查，不代替真实评测或内容检视。

终评表从独立终评记录投影均值和按 case ID 配对的逐例分数，同时保留旧扁平指标兼容；不把迭代评测冒充终评，缺失或不匹配的逐例证据不补零。修改点来源计数沿用后端去重 Bad Case 语义，标签使用“个问题”，避免与归因报告份数混淆。

## Alternatives considered

- 单独部署第二套进化服务：会重复维护任务状态与页面接口，故沿用现有编排并迁入能力。
- 依赖平台 verdict 或 Git tag：当前平台响应不提供 verdict，评测引用使用分支，故由后端比较分数。
- 覆盖上游问题报告：反馈产生的数据属于本服务的进化过程，保持上游表只读。

## Consequences

新增 Skill 状态表和迁移，过程状态逐项保存并支持冲突检测。真实服务需要配置模型、评测平台及仓库推送权限；本地测试采用隔离仓库与模拟服务，不能代替真实平台联调。原有非 Skill 路径继续使用既有实现。
