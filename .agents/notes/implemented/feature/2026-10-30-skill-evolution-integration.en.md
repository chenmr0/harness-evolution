# Agent Note: Skill evolution integration

Status: implemented

English | [中文](2026-10-30-skill-evolution-integration.md)

## Problem

The existing evolution flow has task state and Git workspaces, but clustering, changes, and evaluation use placeholders. Skill evolution needs traceable model decisions, actual multi-file edits, and recoverable external evaluations.

## Decision

Integrate Skill clustering, deduplication, verification, ranking, patches, and feedback into the existing task flow. Preserve immutable input snapshots and revision-checked process state. Candidates use Git branches for evaluation; acceptance requires a strict increase in the arithmetic mean of total_score across matching cases. Retest the baseline and best version independently at the end. Real model and evaluation services are explicitly enabled and configured by the server; credentials are read from the environment and not stored in task snapshots. Historical data is not migrated.

## Configuration and operation

Set both `SKILL_EVOLUTION_MODEL_ENABLED=1` and `SKILL_EVOLUTION_EVAL_ENABLED=1` on the server to enable the real Skill flow. Configure `SKILL_EVOLUTION_MODEL_URL`, `SKILL_EVOLUTION_MODEL`, `SKILL_EVOLUTION_MODEL_KEY_ENV`, `SKILL_EVOLUTION_EVAL_URL`, `SKILL_EVOLUTION_EVAL_TOKEN_ENV`, and `SKILL_EVOLUTION_EVAL_TEMPLATE_ID` separately. Put secret values in the named environment variables, not requests or the repository. `HE_SKILL_STRATEGY_JSON` configures the evolution policy; otherwise defaults apply. Real evaluation requires push access to the target Git repository. New tasks freeze real mode and cannot resume by silently downgrading to the mock flow.

## Alternatives considered

- Deploy a second evolution service: this would duplicate task state and UI APIs, so the existing orchestration hosts the integrated capabilities.
- Depend on a provider verdict or Git tag: the current response has no verdict and the platform uses branches, so the backend compares scores.
- Overwrite upstream problem reports: feedback belongs to this service's evolution process, and upstream tables remain read-only.

## Consequences

A Skill state table and migration are added, with incremental state persistence and conflict detection. Real services require model, evaluation platform, and repository push configuration; local tests use isolated repositories and mock services and do not replace live integration testing. Existing non-Skill paths retain their prior behavior.
