# Skill evolution integration and results

English | [中文](m6-skill-evolution-integration.md)

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

## Background

Parts of the current evolution flow use placeholder clustering, patches, and evaluation, and the UI cannot express a skipped evaluation.

## Goals

- Integrate Skill evolution, branch evaluation, persistence, and traceable result presentation.

## Design authority

- [Evolution architecture](../../../plans/fullstack-design/01-architecture.md)
- [Backend implementation](../../../plans/fullstack-design/07-backend-implementation.md)

## Implementation

- Integrate Skill clustering, verification, ranking, multi-file edits, multi-round feedback, branch evaluation, and final evaluation.
- Extend process persistence and result projections to represent skipped evaluations accurately.

## Implementation steps

1. Add recoverable evolution state and safe real-service adapters.
2. Integrate existing orchestration and UI contracts; verify end-to-end and regression behavior locally.

## Dependencies and boundary

- Depends on m5-11. Skill only; do not migrate historical tasks or write upstream problem tables.

## Verification

```sh
pnpm test:api
pnpm contract:check
pnpm typecheck:web
```

- Backend tests and cross-stack contracts pass; live platform validation requires a separate isolated repository and test credentials.

## Acceptance criteria

- Candidates and final evaluation use branches and compare mean scores over identical case sets; invalid results are not normal rejections.
- Tasks, feedback waves, and skipped states remain traceable in the API, and recovery cannot switch to mock services.

## Gates and CI

- Backend tests, generated-contract checks, Web type checking, and repository flow gates.

## Non-goals

- No historical data migration, expansion to other evolution types, deployment, or production service access.
