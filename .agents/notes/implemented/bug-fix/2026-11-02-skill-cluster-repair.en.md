# Agent Note: Skill cluster validation and bounded repair

Status: implemented

English | [中文](2026-11-02-skill-cluster-repair.md)

## Problem

Real models can duplicate or omit reports. A single invalid partition terminates prepare with a generic error that does not identify the violation. The owning flow issue is [m6-19](../../../flow/issues/m6-skill-codeagent-e2e.en.md).

## Decision

HTTP and CodeAgent share stage-specific submission tools. The restricted CLI connects to one stdio MCP server exposing only the current tool. HTTP requests bind the same argument schema. Only validated tool results advance the workflow. Transport, timeout, cancellation and safety failures end the operation.

Clustering numbers reports from one within each attribute group. Exact coverage is checked before mapping to frozen report IDs and generating cluster IDs. Review validates snapshot citations and repair plans. Edits target unique original spans in the accepted Skill snapshot. The backend checks conflicts and file safety for the entire batch before constructing changes. Feedback cites validation evidence and each issue must reference the current version.

## Alternatives considered

Generating full records requires copying identifiers and unchanged file content. Dedicated tools take model judgments and construct existing domain records on the server. Arbitrary script execution would expand the capability boundary.

## Consequences

Each operation allows three submission attempts. Argument errors return corrective feedback and only one valid proposal advances. The CLI records submission outcomes. Changing the adapter revision activates existing resume identity checks.
