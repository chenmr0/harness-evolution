"""Multi-wave execution, evidence isolation and recovery regression coverage."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.crud.skill_evolution import SkillCheckpointConflict
from app.db.models.evolution import EvolutionRoundRecord, EvolutionRunRecord
from app.db.models.skill_evolution import SkillEvolutionStateRecord
from app.service.evolution.skill_contracts import SkillStrategy
from app.service.evolution.skill_core import SkillEvolutionCore
from app.service.evolution.skill_feedback import SkillFeedbackService
from app.service.evolution.skill_identity import skill_identity

FILES = {"SKILL.md": "# Demo\n"}
REPORTS = [{"probe_id": "p1", "attribute_type": "prompt", "probe_description": "Format"}]


class Model:
    def __init__(self) -> None:
        self.attributes = 0
        self.plans = 0

    async def cluster(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.plans += 1
        return [
            {
                "cluster_id": "new",
                "attribute_type": "prompt",
                "probe_ids": [r["probe_id"] for r in reports],
                "summary": "Next issue",
            }
        ]

    async def verify(self, *args: Any) -> dict[str, Any]:
        return {"confidence": 0.9, "severity": 0.8, "reason": "Current evidence"}

    async def patch(self, *args: Any) -> dict[str, str | None]:
        return {}

    async def attribute(self, data: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
        self.attributes += 1
        assert all(e["name"].startswith("validation/") for e in data["evaluations"])
        assert "final-secret" not in str(data)
        item = next(e for e in data["evidence"] if e["ref"] == data["current_ref"])
        return {
            "status": "actionable",
            "reason": "Still a problem",
            "issues": [
                {
                    "attribute_type": "prompt",
                    "probe_description": "Next format issue",
                    "probe_analysis": "Grounded analysis",
                    "evidence_ids": [item["id"]],
                }
            ],
        }


class Store:
    def __init__(self, core: SkillEvolutionCore) -> None:
        self.value: Any = SimpleNamespace(
            revision=0,
            input_snapshot={"reports": REPORTS},
            checkpoint={
                "identity": skill_identity(core),
                "best_files": FILES,
                "evaluations": {"pair1": pair(), "FINAL": {"secret": "final-secret"}},
            },
        )
        self.fail_append = False
        self.appended = 0

    async def get(self, run_id: str) -> SkillEvolutionStateRecord:
        return cast(SkillEvolutionStateRecord, deepcopy(self.value))

    async def save_checkpoint(
        self, run_id: str, checkpoint: dict[str, Any], *, expected_revision: int
    ) -> SkillEvolutionStateRecord:
        if expected_revision != self.value.revision:
            raise SkillCheckpointConflict("conflict")
        self.value.checkpoint = deepcopy(checkpoint)
        self.value.revision += 1
        return await self.get(run_id)

    async def append_feedback_plan(
        self,
        run_id: str,
        plan: dict[str, Any],
        checkpoint: dict[str, Any],
        *,
        expected_revision: int,
    ) -> SkillEvolutionStateRecord:
        if self.fail_append:
            self.fail_append = False
            raise RuntimeError("simulated crash before transaction commit")
        checkpoint["plan"] = deepcopy(plan)
        result = await self.save_checkpoint(run_id, checkpoint, expected_revision=expected_revision)
        self.appended += 1
        return result


def pair() -> dict[str, Any]:
    return {
        "request": {"baselineCommit": "base", "candidateCommit": "accepted"},
        "baselineId": "eval-base",
        "candidateId": "eval-candidate",
        "result": {
            "candidateScore": 2.0,
            "metrics": [
                {
                    "kind": "case_scores",
                    "current": {"score": 1.0, "report": {}},
                    "candidate": {"score": 2.0, "report": {"metrics": [], "bad_cases": []}},
                }
            ],
        },
    }


def run() -> EvolutionRunRecord:
    return cast(EvolutionRunRecord, SimpleNamespace(id="run1", target_type="skill", mode="full"))


def round_(seq: int = 1, accepted: bool = True) -> EvolutionRoundRecord:
    return cast(
        EvolutionRoundRecord,
        SimpleNamespace(
            seq=seq,
            status="accepted" if accepted else "rejected",
            accepted=accepted,
            eval_external_id="pair1",
            eval_result={"completed": True},
            probe_cluster_id=f"c{seq}",
            candidate_commit="accepted",
            eval_candidate=2.0,
            eval_current=1.0,
        ),
    )


async def test_two_wave_loop_stops_at_limit_and_restart_does_not_replan() -> None:
    model = Model()
    core = SkillEvolutionCore(model, SkillStrategy(max_rounds=2))
    store = Store(core)
    service = SkillFeedbackService(core, store)
    assert await service.advance_if_needed(run(), [round_()])
    assert model.attributes == model.plans == store.appended == 1
    assert store.value.checkpoint["plan"]["clusters"][0]["cluster_id"] == "r2:prompt:new"
    assert store.value.checkpoint["source_reports"][-1]["probe_id"] == "feedback-r2-0"
    # Old final jobs cannot re-append after a committed wave.
    assert await service.advance_if_needed(run(), [round_()])
    assert store.appended == 1
    assert not await service.advance_if_needed(run(), [round_(), round_(2)])
    assert store.value.checkpoint["stop_reason"] == "max_rounds"
    assert not await service.advance_if_needed(run(), [round_(), round_(2)])
    assert model.attributes == model.plans == 1


async def test_crash_before_append_reuses_attribution_and_plan() -> None:
    model = Model()
    core = SkillEvolutionCore(model, SkillStrategy(max_rounds=2))
    store = Store(core)
    store.fail_append = True
    service = SkillFeedbackService(core, store)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await service.advance_if_needed(run(), [round_()])
    assert await service.advance_if_needed(run(), [round_()])
    assert model.attributes == model.plans == store.appended == 1
    assert len(store.value.checkpoint["source_reports"]) == 2


@pytest.mark.parametrize(
    "max_rounds,accepted,reason", [(1, True, "max_rounds"), (2, False, "no_improvement")]
)
async def test_stop_policy(max_rounds: int, accepted: bool, reason: str) -> None:
    model = Model()
    core = SkillEvolutionCore(model, SkillStrategy(max_rounds=max_rounds))
    store = Store(core)
    assert not await SkillFeedbackService(core, store).advance_if_needed(
        run(), [round_(accepted=accepted)]
    )
    assert store.value.checkpoint["stop_reason"] == reason
    assert model.attributes == model.plans == 0


async def test_insufficient_evidence_stops_without_new_candidates() -> None:
    class Insufficient(Model):
        async def attribute(self, *args: Any) -> dict[str, Any]:
            return {"status": "insufficient_evidence", "reason": "No case evidence", "issues": []}

    core = SkillEvolutionCore(Insufficient(), SkillStrategy(max_rounds=2))
    store = Store(core)
    assert not await SkillFeedbackService(core, store).advance_if_needed(run(), [round_()])
    assert store.value.checkpoint["stop_reason"] == "insufficient_evidence"
    assert store.appended == 0


async def test_changed_model_policy_fails_closed() -> None:
    core = SkillEvolutionCore(Model(), SkillStrategy(max_rounds=2))
    store = Store(core)
    store.value.checkpoint["identity"] = "different"
    with pytest.raises(ValueError, match="changed"):
        await SkillFeedbackService(core, store).advance_if_needed(run(), [round_()])
