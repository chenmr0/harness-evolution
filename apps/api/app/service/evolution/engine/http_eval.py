"""Durable paired evaluations using independent baseline and candidate platform runs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

from app.db.crud.skill_evolution import SkillCheckpointConflict, SkillEvolutionRepository
from app.service.evolution.engine.contracts import (
    EvalGatewayConfig,
    EvalRequest,
    EvalResult,
    EvalStatus,
    EvalSubmission,
    freeze_json,
)
from app.third_party_api.evaluation.http import HttpEvaluationClient
from app.third_party_api.evaluation.models import (
    AmbiguousEvaluationSubmission,
    EvaluationError,
    compare_case_scores,
)


class HttpEvalGateway:
    """Keep provider IDs and score evidence in CAS-protected per-run checkpoints."""

    def __init__(
        self,
        config: EvalGatewayConfig,
        *,
        run_id: str,
        states: SkillEvolutionRepository,
        client: HttpEvaluationClient | None = None,
        server_config: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind immutable config, durable state and an optional mock platform client."""
        self.config = config
        self._run_id = run_id
        self._states = states
        if client is None and server_config is None:
            raise EvaluationError("Real evaluation requires trusted server configuration")
        trusted = dict(server_config or {})
        if "expectedCaseIds" in config:
            trusted["expectedCaseIds"] = config["expectedCaseIds"]
        self._client = client or HttpEvaluationClient(trusted)
        self._idempotency_guaranteed = trusted.get("idempotencyGuaranteed") is True
        template = config.get("templateId", trusted.get("templateId"))
        if not isinstance(template, str) or not template.strip():
            raise EvaluationError("Real evaluation requires a templateId")
        self._template = template

    async def submit(self, request: EvalRequest) -> EvalSubmission:
        """Resume a pair without losing the baseline ID if candidate submission fails."""
        if (
            request.run_id != self._run_id
            or not request.baseline_ref
            or not request.baseline_commit
        ):
            raise EvaluationError("Real evaluation requires a baseline branch and commit")
        if not request.idempotency_key.strip():
            raise EvaluationError("Evaluation idempotency key is required")
        eval_id = (
            "http:"
            + hashlib.sha256((self._run_id + ":" + request.idempotency_key).encode()).hexdigest()
        )
        identity = {
            "baselineRef": request.baseline_ref,
            "baselineCommit": request.baseline_commit,
            "candidateRef": request.ref,
            "candidateCommit": request.commit,
            "templateId": self._template,
            "integrationIdentity": self._client.integration_identity,
        }
        await self._merge(eval_id, {"request": identity})
        for role, ref in (("baseline", request.baseline_ref), ("candidate", request.ref)):
            entry = await self._entry(eval_id)
            if role + "Id" not in entry:
                key = request.idempotency_key + "/" + role
                self._client.validate_submission(ref, self._template, key)
                claimed = await self._merge(eval_id, {role + "SubmissionIntent": {"key": key}})
                if not claimed and not self._idempotency_guaranteed:
                    if role + "Id" in await self._entry(eval_id):
                        continue
                    raise AmbiguousEvaluationSubmission(
                        "Evaluation submission outcome is unknown; reconcile the provider task "
                        "before retrying. Automatic POST retransmission is disabled."
                    )
                try:
                    external_id = await self._client.submit(ref, self._template, key)
                except Exception:
                    raise AmbiguousEvaluationSubmission(
                        "Evaluation submission outcome is unknown; reconcile the provider task "
                        "before retrying. Automatic POST retransmission is disabled."
                    ) from None
                await self._merge(eval_id, {role + "Id": external_id})
        return EvalSubmission(eval_id)

    async def get_status(self, eval_id: str) -> EvalStatus:
        """Complete only when both runs complete; either failure fails the pair."""
        entry = await self._entry(eval_id)
        statuses = [
            await self._client.get_status(self._external_id(entry, role))
            for role in ("baseline", "candidate")
        ]
        if "failed" in statuses:
            return EvalStatus("failed", "An evaluation in the comparison failed")
        if statuses == ["completed", "completed"]:
            return EvalStatus("completed")
        return EvalStatus("running")

    async def get_result(self, eval_id: str) -> EvalResult:
        """Require comparable case sets and persist the exact evidence behind the verdict."""
        entry = await self._entry(eval_id)
        if "result" in entry:
            return EvalResult.from_payload(entry["result"])
        baseline = await self._client.get_score(self._external_id(entry, "baseline"))
        candidate = await self._client.get_score(self._external_id(entry, "candidate"))
        accepted = compare_case_scores(baseline, candidate)
        result = EvalResult(
            current_score=baseline.score,
            candidate_score=candidate.score,
            external_verdict="accept" if accepted else "reject",
            metrics=(
                cast(
                    Any,
                    freeze_json(
                        {
                            "kind": "case_scores",
                            "current": baseline.to_payload(),
                            "candidate": candidate.to_payload(),
                        }
                    ),
                ),
            ),
        )
        await self._merge(eval_id, {"result": result.to_payload()})
        return result

    @staticmethod
    def _external_id(entry: dict[str, Any], role: str) -> str:
        value = entry.get(role + "Id")
        if not isinstance(value, str) or not value.strip():
            raise EvaluationError("Evaluation comparison submission is incomplete")
        return value

    async def _entry(self, eval_id: str) -> dict[str, Any]:
        state = await self._states.get(self._run_id)
        entries = state.checkpoint.get("evaluations", {}) if state is not None else {}
        entry = entries.get(eval_id) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            raise EvaluationError("Evaluation comparison is not recorded for this run")
        identity = entry.get("request")
        if (
            not isinstance(identity, dict)
            or identity.get("integrationIdentity") != self._client.integration_identity
            or identity.get("templateId") != self._template
        ):
            raise EvaluationError("Evaluation provider identity changed since submission")
        return entry

    async def _merge(self, eval_id: str, changes: dict[str, Any]) -> bool:
        for _ in range(10):
            state = await self._states.get(self._run_id)
            if state is None:
                raise EvaluationError("Skill evaluation checkpoint is missing")
            checkpoint = state.checkpoint
            entries = checkpoint.setdefault("evaluations", {})
            if not isinstance(entries, dict):
                raise EvaluationError("Evaluation checkpoint is invalid")
            entry = entries.setdefault(eval_id, {})
            if not isinstance(entry, dict):
                raise EvaluationError("Evaluation comparison checkpoint is invalid")
            if any(key in entry and entry[key] != value for key, value in changes.items()):
                raise EvaluationError("Evaluation idempotency identity or evidence conflicts")
            if all(key in entry for key in changes):
                return False
            entry.update(changes)
            try:
                await self._states.save_checkpoint(
                    self._run_id,
                    checkpoint,
                    expected_revision=state.revision,
                )
                return True
            except SkillCheckpointConflict:
                continue
        raise EvaluationError("Evaluation checkpoint is busy; retry with the same identity")
