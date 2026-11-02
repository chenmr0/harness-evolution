# Agent Note: Skill cluster validation and bounded repair

Status: implemented

English | [中文](2026-11-02-skill-cluster-repair.md)

## Problem

Real models can duplicate or omit reports. A single invalid partition terminates prepare with a generic error that does not identify the violation. The owning flow issue is [m6-19](../../../flow/issues/m6-skill-codeagent-e2e.en.md).

## Decision

The shared HTTP and CodeAgent model boundary generates clusters at most three times. Only shape or partition validation failures trigger repair feedback, always with the complete canonical input. The core independently enforces exactly-once membership, unique cluster IDs and homogeneous types, accepting no partial results. Feedback distinguishes duplicates, omissions, unknown IDs, attribute mismatches and structural errors. It retains at most 50 details with an explicit truncation flag, complete counts and untruncated probe IDs.

Each validation outcome enters service logs; terminal failures retain attempt and violation counts. Logs contain neither model prose nor raw identifiers. CodeAgent keeps per-call inputs and outputs in local call directories, with details in repair inputs. Safety failures, transport failures and cancellation propagate directly without correction retries. Only complete valid plans can save a `checkpoint` and advance clusters and rounds.

CodeAgent and HTTP request probe_cluster_description for clusters; summary/reason remain compatible legacy input fields. The CLI prompt uses the complete schema passed to --json-schema, including the result wrapper, to avoid conflicting format descriptions. After safety and size checks, each call saves actual stdin bytes to prompt.txt and its schema to response-schema.json, with filenames in input.json; business content stays out of metadata logs. The adapter revision is codeagent-stdin-v3, allowing model identity checks to prevent saved plans from silently resuming under a different execution policy.

## Alternatives considered

Relaxing validation, removing duplicates or filling clusters would change semantic membership without verification. Stronger prompts alone cannot guarantee coverage. Retrying all of prepare expands side effects and repeats verification; bounded repair at the model boundary supports both transports while preserving the core guard.

Changing field names in prompt text alone still leaves two output contracts to maintain. Deriving format instructions from the CLI schema eliminates ambiguity in both fields and wrapping. Business input alone cannot reconstruct the instructions received by the model, so prompts and schemas are retained per call.

## Consequences

Each attribute group may cost two additional model calls; persistently invalid output still fails explicitly. This budget is separate from blocked-run retry_attempts and adds no failed-run recovery endpoint. Synthetic tests cover duplicates and omissions among 27 reports, recovery, exhaustion, safety boundaries, HTTP/CLI feedback and prepare persistence; CI makes no real model calls.
