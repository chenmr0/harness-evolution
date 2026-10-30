# Skill 进化集成与结果展示

[English](m6-skill-evolution-integration.en.md) | 中文

```yaml flow
kind: issue
id: m6-18
milestone: m6
depends:
  - m5-11
priority: P2
status: backlog
scope:
  - apps/api/**
  - apps/web/**
  - packages/contracts/**
  - .agents/flow/**
  - .agents/notes/**
adr: []
github:
  number: null
  url: null
```

## 背景

现有进化流程的聚类、补丁和评测部分依赖占位实现，页面也无法表达跳过评测。

## 目标

- 接入 Skill 进化、分支评测、持久化及可追溯结果展示。

## 设计依据

- [自进化架构](../../../plans/fullstack-design/01-architecture.md)
- [后端实现](../../../plans/fullstack-design/07-backend-implementation.md)

## 实现内容

- 迁入 Skill 聚类、核查、排序、多文件修改和多轮反馈；对接分支评测及终评。
- 扩展过程落库和结果投影，准确展示跳过的评测。

## 实施步骤

1. 增加可恢复的进化状态和安全的真实服务适配。
2. 接入现有编排与页面契约，完成本地端到端及回归验证。

## 依赖与交付边界

- 依赖：m5-11。只覆盖 Skill，不迁移历史任务；外部问题表保持只读。

## 独立验证

```sh
pnpm test:api
pnpm contract:check
pnpm typecheck:web
```

- 后端测试与前后端契约通过；真实平台验证须另配隔离仓库和测试凭据。

## 验收标准

- 候选和终评使用分支，比较同一 case 集的平均分，异常结果不被当作正常拒绝。
- 完整任务、反馈轮次和跳过状态在接口中可追溯，恢复不切换为模拟服务。

## 门禁与 CI

- 后端测试、契约生成检查、Web 类型检查和仓库流程门禁。

## 非目标

- 不合并历史数据，不扩展其他进化类型，不部署或连接生产服务。
