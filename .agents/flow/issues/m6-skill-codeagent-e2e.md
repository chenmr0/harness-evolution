# Skill 终态与统计修复及 CodeAgent 联调

[English](m6-skill-codeagent-e2e.en.md) | 中文

```yaml flow
kind: issue
id: m6-19
milestone: m6
depends:
  - m5-11
priority: P1
status: in-progress
scope:
  - apps/api/**
  - apps/web/**
  - tests/e2e/**
  - docs/**
  - .agents/flow/**
  - .agents/notes/**
adr:
  - ../../notes/implemented/feature/2026-10-30-skill-evolution-integration.md
github:
  number: 2
  url: https://github.com/chenmr0/harness-evolution/issues/2
```

## 背景

Skill 完成状态、终评可读性及输入统计必须保持一致；真实归因报告与来源问题具有一对多关系。

## 目标

修复终评游标、输入统计与向导计数语义，并接入显式启用的 CodeAgent CLI。

## 设计依据

- [自进化架构](../../../plans/fullstack-design/01-architecture.md)
- [后端实现](../../../plans/fullstack-design/07-backend-implementation.md)

## 实现内容

完成事务同步进度；统计取冻结快照；向导区分报告与来源问题。CodeAgent 与 HTTP 模型复用操作契约，通过标准输入传递数据，仅启用阶段专用 MCP 工具，限制时间和输出并清理子进程。

## 实施步骤

1. 补终评可读、幂等、统计及前端计数回归。
2. 接入服务端配置的 CLI 并验证失败、超时、取消和凭据边界。
3. 先执行隔离 mock 冒烟，再执行已授权的真实模型、本地 Git 与 mock 评测测试。

## 依赖与交付边界

依赖 m5-11。沿用现有 Skill 集成，不迁移历史任务，不写上游表。真实业务数据与调用凭据仅存本机，不提交或写入远端 issue。

## 独立验证

```sh
pnpm test:api
pnpm test:web
pnpm typecheck:api
pnpm typecheck:web
pnpm contract:check
```

使用合成多对一报告关联和隔离 Git 仓；完成后终评可读、进度为 100%、输入统计与查询一致，禁用模型不启动进程。

## 验收标准

- 六步 API 可读且页面终评可点击；重复完成不增加游标。
- 报告数与来源问题数分别显示；输入统计保留全部原始报告。
- 终评表展示独立终评的均值与逐例对比并兼容旧格式；修改点来源数标明为问题数。
- 真实 CodeAgent 生成可检视方案与补丁；mock 评测不作为真实质量提升证据。

## 门禁与 CI

后端与前端测试、lint、类型检查、契约及文档流程门禁；CI 只执行合成测试，不调用付费模型。

## 非目标

不连接生产库、公司 Git 或真实评测，不自动合并分支，不静默截断超长 probe_id。
