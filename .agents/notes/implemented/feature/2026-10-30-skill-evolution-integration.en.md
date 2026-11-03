# Agent Note: Skill evolution integration

Status: implemented

English | [中文](2026-10-30-skill-evolution-integration.md)

## Problem

The existing evolution flow has task state and Git workspaces, but clustering, changes, and evaluation use placeholders. Skill evolution needs traceable model decisions, actual multi-file edits, and recoverable external evaluations.

## Decision

Integrate Skill clustering, deduplication, verification, ranking, patches, and feedback into the existing task flow. Preserve immutable input snapshots and revision-checked process state. Candidates use Git branches for evaluation; acceptance requires a strict increase in the mean over all hard observations in the report, rounded half up to two decimals, across matching sets of scored cases. Each hard occurrence counts, including repeats within a case. Other metrics do not count and an empty hard set has no score. Raw metrics remain available for audit. Retest the baseline and best version independently at the end. Real model and evaluation services are explicitly enabled and configured by the server; credentials are read from the environment and not stored in task snapshots. Historical data is not migrated.

## Configuration and operation

Set both `SKILL_EVOLUTION_MODEL_ENABLED=1` and `SKILL_EVOLUTION_EVAL_ENABLED=1` on the server to enable the real Skill flow. Configure `SKILL_EVOLUTION_MODEL_URL`, `SKILL_EVOLUTION_MODEL`, `SKILL_EVOLUTION_MODEL_KEY_ENV`, `SKILL_EVOLUTION_EVAL_URL`, `SKILL_EVOLUTION_EVAL_TOKEN_ENV`, and `SKILL_EVOLUTION_EVAL_TEMPLATE_ID` separately. Put secret values in the named environment variables, not requests or the repository. `HE_SKILL_STRATEGY_JSON` configures the evolution policy; otherwise defaults apply. Real evaluation requires push access to the target Git repository. New tasks freeze real mode and cannot resume by silently downgrading to the mock flow.

CodeAgent and HTTP models share stage prompts and output validation. The CLI receives snapshots over stdin with only the stage submission tool enabled and built-in tools, inherited settings and session persistence disabled. Logs contain only bounded call metadata; inputs and structured results remain in the isolated artifact directory. Timeout or cancellation cleans up the process group. Freeze binary and endpoint identity to prevent transport changes during recovery. Completion persists the final cursor atomically; counts derive from frozen reports rather than current upstream data, with reports and source problems shown separately.

The CLI uses stage JSON Schemas for clustering, verification, patches, and feedback. Reject Markdown replacements consisting only of headings and omission markers so placeholders cannot erase rules. This bounded completeness check does not replace real evaluation or content review.

The final table projects means and case-ID-paired scores from independent final evaluations while retaining flat-metric compatibility. Iteration evaluations cannot substitute for final evidence, and missing or mismatched cases are not filled with zero. Change-source counts retain the backend's distinct Bad Case semantics and use a problem-count label rather than an attribution-report count.

## Alternatives considered

- Deploy a second evolution service: this would duplicate task state and UI APIs, so the existing orchestration hosts the integrated capabilities.
- Depend on a provider verdict or Git tag: the current response has no verdict and the platform uses branches, so the backend compares scores.
- Overwrite upstream problem reports: feedback belongs to this service's evolution process, and upstream tables remain read-only.

## Consequences

A Skill state table and migration are added, with incremental state persistence and conflict detection. Real services require model, evaluation platform, and repository push configuration; local tests use isolated repositories and mock services and do not replace live integration testing. Existing non-Skill paths retain their prior behavior.
