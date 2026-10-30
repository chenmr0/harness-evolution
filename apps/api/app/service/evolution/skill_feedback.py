"""Restartable validation-feedback waves before independent final evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Protocol

from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.db.models.skill_evolution import SkillEvolutionStateRecord
from app.service.evolution.skill_core import SkillEvolutionCore, feedback_reports
from app.service.evolution.skill_identity import skill_identity


class FeedbackStateStore(Protocol):
    """Atomic checkpoint and append boundary, with no writes to upstream probes."""

    async def get(self, run_id: str) -> SkillEvolutionStateRecord | None: ...

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        *,
        expected_revision: int,
    ) -> SkillEvolutionStateRecord: ...

    async def append_feedback_plan(
        self,
        run_id: str,
        plan: dict[str, Any],
        checkpoint: dict[str, Any],
        *,
        expected_revision: int,
    ) -> SkillEvolutionStateRecord: ...


class SkillFeedbackService:
    """Continue only improved Skill waves with grounded actionable feedback."""

    def __init__(self, core: SkillEvolutionCore, states: FeedbackStateStore) -> None:
        self._core = core
        self._states = states

    async def advance_if_needed(
        self,
        run: EvolutionRunRecord,
        rounds: Sequence[EvolutionRoundRecord],
    ) -> bool:
        """Atomically append the next wave, or durably record a source stop reason."""
        if run.target_type != "skill" or run.mode != "full":
            return False
        if any(r.status not in {"accepted", "rejected", "skipped"} for r in rounds):
            return True
        record = await self._states.get(run.id)
        if record is None:
            raise ValueError("Skill feedback checkpoint missing")
        checkpoint = record.checkpoint
        if checkpoint.get("identity") != skill_identity(self._core):
            raise ValueError("Skill model or strategy changed since preparation")
        if "stop_reason" in checkpoint:
            return False
        if "final_evaluation" in checkpoint:
            raise ValueError("cannot resume optimization after finalization")
        waves = checkpoint.setdefault(
            "feedback_rounds",
            [
                {
                    "number": 1,
                    "start_seq": 1,
                    "end_seq": len(rounds),
                }
            ],
        )
        wave = waves[-1]
        # A stale final job may predate an already-committed next wave.
        if len(rounds) < wave["end_seq"]:
            return True
        wave_rounds = [r for r in rounds if wave["start_seq"] <= r.seq <= wave["end_seq"]]
        wave["completed"] = True
        wave["improved"] = any(r.accepted for r in wave_rounds)
        reason = (
            "no_improvement"
            if not wave["improved"]
            else "max_rounds"
            if wave["number"] >= self._core.strategy.max_rounds
            else None
        )
        if reason is not None:
            checkpoint["stop_reason"] = reason
            await self._states.save_checkpoint(
                run.id, checkpoint, expected_revision=record.revision
            )
            return False
        if "feedback_input" not in wave:
            wave["feedback_input"] = validation_feedback(
                checkpoint,
                rounds,
                wave,
                list(record.input_snapshot["reports"]),
            )
            record = await self._states.save_checkpoint(
                run.id,
                checkpoint,
                expected_revision=record.revision,
            )
        feedback = wave["feedback_input"]
        if "attribution" not in wave:
            if not callable(getattr(self._core.model, "attribute", None)):
                checkpoint["stop_reason"] = "feedback_unsupported"
                await self._states.save_checkpoint(
                    run.id,
                    checkpoint,
                    expected_revision=record.revision,
                )
                return False
            wave["attribution"] = await self._core.attribute(feedback, checkpoint["best_files"])
            record = await self._states.save_checkpoint(
                run.id,
                checkpoint,
                expected_revision=record.revision,
            )
        attribution = wave["attribution"]
        if attribution["status"] != "actionable":
            checkpoint["stop_reason"] = attribution["status"]
            await self._states.save_checkpoint(
                run.id, checkpoint, expected_revision=record.revision
            )
            return False
        if "next_plan" not in wave:
            generated = feedback_reports(attribution, feedback, run.id)
            plan = await self._core.plan(generated, checkpoint["best_files"], wave["number"] + 1)
            wave["next_reports"] = generated
            wave["next_plan"] = asdict(plan)
            record = await self._states.save_checkpoint(
                run.id,
                checkpoint,
                expected_revision=record.revision,
            )
        plan_data = wave["next_plan"]
        checkpoint.setdefault("source_reports", list(record.input_snapshot["reports"])).extend(
            wave["next_reports"],
        )
        waves.append(
            {
                "number": wave["number"] + 1,
                "start_seq": wave["end_seq"] + 1,
                "end_seq": wave["end_seq"] + len(plan_data["clusters"]),
            }
        )
        await self._states.append_feedback_plan(
            run.id,
            plan_data,
            checkpoint,
            expected_revision=record.revision,
        )
        return True


def validation_feedback(
    checkpoint: dict[str, Any],
    rounds: Sequence[EvolutionRoundRecord],
    wave: dict[str, Any],
    initial_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive evidence exclusively from validation pair IDs attached to candidate rounds."""
    evaluated = [r for r in rounds if r.eval_external_id and r.eval_result]
    accepted = [r for r in evaluated if r.accepted]
    if not accepted:
        raise ValueError("feedback requires an accepted evaluated candidate")
    current_round = accepted[-1]
    entries = checkpoint.get("evaluations", {})
    current_entry = entries[current_round.eval_external_id]
    current_ref = current_entry["request"]["candidateCommit"]
    selected = [r for r in evaluated if wave["start_seq"] <= r.seq <= wave["end_seq"]]
    # Keep original baseline and the currently accepted version even across waves.
    chosen = {r.seq: r for r in [evaluated[0], *selected, current_round]}
    evaluations: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for round_ in sorted(chosen.values(), key=lambda item: item.seq):
        entry = entries[round_.eval_external_id]
        result = entry["result"]
        pair = next(m for m in result["metrics"] if m.get("kind") == "case_scores")
        for role, pair_key in (("baseline", "current"), ("candidate", "candidate")):
            score = pair[pair_key]["score"]
            report = pair[pair_key].get("report", {})
            ref = entry["request"][role + "Commit"]
            name = f"validation/round/{round_.seq}/{role}"
            run_id = entry[role + "Id"]
            evaluations.append(dict(name=name, run_id=run_id, ref=ref, score=score, report=report))
            items = [("score", {"value": score})]
            items += [("metric", item) for item in report.get("metrics", [])]
            items += [("bad_case", item) for item in report.get("bad_cases", [])]
            if report.get("bad_cases_complete") is True:
                items.append(
                    (
                        "case_coverage",
                        {
                            "complete": True,
                            "bad_case_count": len(report["bad_cases"]),
                        },
                    )
                )
            for kind, data in items:
                evidence.append(
                    dict(
                        id=f"e{len(evidence)}",
                        kind=kind,
                        evaluation=name,
                        run_id=run_id,
                        ref=ref,
                        data=data,
                    )
                )
    return {
        "round_number": wave["number"],
        "current_ref": current_ref,
        "current_score": current_entry["result"]["candidateScore"],
        "evaluations": evaluations,
        "evidence": evidence,
        "initial_reports": initial_reports,
        "history": [
            dict(
                round_number=r.seq,
                cluster_id=r.probe_cluster_id,
                accepted=r.accepted,
                reason=r.status,
                ref=r.candidate_commit,
                score=r.eval_candidate,
                previous_score=r.eval_current,
            )
            for r in rounds
        ],
    }
