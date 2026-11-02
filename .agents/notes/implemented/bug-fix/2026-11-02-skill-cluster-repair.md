# Agent Note: Skill 聚类校验与有限纠错

Status: implemented

[English](2026-11-02-skill-cluster-repair.en.md) | 中文

## Problem

真实模型可能重复分配或遗漏报告。单次非法分区直接终止 prepare，用户只看到笼统错误，无法区分违规原因。关联流程为 [m6-19](../../../flow/issues/m6-skill-codeagent-e2e.md)。

## Decision

HTTP 与 CodeAgent 共用模型边界最多生成三次聚类；只对结构或分区校验失败反馈纠错，始终重发完整 canonical 输入。核心继续独立校验 exactly-once、簇标识唯一与同类型，不接受部分结果。反馈区分重复、遗漏、未知 ID、属性不匹配和结构错误，最多保留 50 条明细并显式标记省略，计数完整且不截断 probe ID。

每次校验结果写服务日志，失败终态保留尝试次数与违规计数；日志不包含模型正文或原始标识。CodeAgent 的逐次输入与输出留在本机调用目录，纠错输入包含明细。安全检查、传输失败和取消直接传播，不消耗纠错重试。只有完整合法计划才能保存 checkpoint 并推进簇与轮次。

## Alternatives considered

放宽校验、自动去重或补簇会未经验证地改变问题归属。仅强化提示不能保证覆盖。重跑整个 prepare 会扩大副作用和重复核查；在模型边界有限纠错能共享两种传输且保留核心防线。

## Consequences

每个属性组最多多付出两次模型调用；持续非法仍明确失败。此预算独立于 blocked run 的 retry_attempts，不增加 failed run 的恢复接口。合成测试覆盖 27 条报告中的重复与遗漏、恢复、耗尽、安全边界、HTTP/CLI 反馈与准备阶段持久化；CI 不调用真实模型。
