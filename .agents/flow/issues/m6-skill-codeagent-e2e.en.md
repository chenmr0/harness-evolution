# Skill completion and statistics fixes with CodeAgent integration

English | [中文](m6-skill-codeagent-e2e.md)

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

## Background

Skill completion, final-step readability, and input counts must agree. Attribution reports have one-to-many source problem relationships.

## Goals

Fix the final cursor, input statistics, and wizard count semantics; integrate an explicitly enabled CodeAgent CLI.

## Design authority

- [Evolution architecture](../../../plans/fullstack-design/01-architecture.md)
- [Backend implementation](../../../plans/fullstack-design/07-backend-implementation.md)

## Implementation

Advance progress in the completion transaction, derive counts from frozen input, and separate reports from source problems. CLI and HTTP models share operation contracts. Send input over stdin, disable tools and MCP, bound time and output, and clean up descendants.

## Implementation steps

1. Add final readability, idempotence, statistics, and wizard regressions.
2. Add server-configured CLI execution with failure, timeout, cancellation, and credential checks.
3. Run isolated mock smoke followed by authorized real-model, local-Git, mock-evaluation testing.

## Dependencies and boundary

Depends on m5-11. Reuse Skill integration without historical task migration or upstream writes. Business data and credentials remain local and never enter commits or remote issues.

## Verification

```sh
pnpm test:api
pnpm test:web
pnpm typecheck:api
pnpm typecheck:web
pnpm contract:check
```

Use synthetic many-to-one report relationships and isolated Git. Completion must expose the final step at 100% with matching input counts. Disabled models must not start a process.

## Acceptance

- All six API steps are readable and the final UI is clickable; repeated completion does not advance again.
- Show report and source-problem counts separately; preserve all original reports in input statistics.
- Display independent final means and per-case comparisons with legacy compatibility; label change-source counts as problems.
- Real CodeAgent generates reviewable plans and patches; mock scores do not prove real quality gains.

## Gates and CI

Backend and frontend tests, lint, typing, contracts, documentation, and flow gates. CI uses synthetic tests only, without paid model calls.

## Non-goals

No production database, corporate Git, real evaluation, branch auto-merge, or silent probe ID truncation.
