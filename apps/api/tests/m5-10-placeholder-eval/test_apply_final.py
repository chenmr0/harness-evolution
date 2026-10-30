"""Submission, apply, lifecycle, and finalization behavior for m5-10."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import insert, update

from app.config import DatabaseDialect, Settings
from app.db.base import Database
from app.db.crud.evolution import EvolutionRepository
from app.db.crud.probe_query import ProbeHitRow
from app.db.factory import create_database
from app.db.migrate import upgrade_database
from app.db.models.evolution import (
    EvolutionRoundRecord,
    EvolutionRunRecord,
    evolution_rounds,
    evolution_runs,
    probe_clusters,
)
from app.db.models.jobs import JobRunRecord
from app.errors import AppError, ErrorCode
from app.service.evolution.advance import EvolutionJobLifecycle
from app.service.evolution.apply_final import (
    EvolutionApplyService,
    EvolutionFinalizeService,
    decision_sentence,
)
from app.service.evolution.engine.contracts import EvalGatewayConfig, EvalResult, RunBlocked
from app.service.evolution.eval_poller import EvolutionEvalPoller, EvolutionEvalSubmitter
from app.service.evolution.git_candidate import CandidateTrace
from app.third_party_api.git.workspace import GitOperationError, PreparedWorkspace

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@dataclass
class CapturedSubmission:
    """One captured follow-up job."""

    kind: str
    payload: Mapping[str, Any]
    ref_id: str | None
    created_by: str | None


@dataclass
class JobCapture:
    """Job scheduler double used by lifecycle assertions."""

    items: list[CapturedSubmission] = field(default_factory=list)
    failures_remaining: int = 0

    async def submit(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        ref_id: str | None,
        created_by: str | None,
    ) -> CapturedSubmission:
        """Capture one scheduling call."""
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("simulated terminal follow-up submission failure")
        if any(item.kind == kind and item.ref_id == ref_id for item in self.items):
            raise AppError(
                status_code=409,
                code=ErrorCode.RUN_BUSY,
                message="another stage job is already active for this run",
            )
        item = CapturedSubmission(kind, dict(payload), ref_id, created_by)
        self.items.append(item)
        return item


class EmptyProbeReader:
    """Return no source evidence for isolated state tests."""

    async def list_rows_by_ids(self, probe_ids: Sequence[str]) -> tuple[object, ...]:
        del probe_ids
        return ()


class CandidateDouble:
    """Exercise apply decisions through real repository state transitions."""

    def __init__(self, repository: EvolutionRepository) -> None:
        self._runs = repository
        self.calls: list[str] = []

    async def accept(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        trace: CandidateTrace,
        *,
        current_score: float,
        candidate_score: float,
        reason: str,
        job_id: str | None = None,
        retry_attempts: int = 0,
    ) -> EvolutionRoundRecord:
        del run, trace, current_score, candidate_score, job_id, retry_attempts
        self.calls.append(reason)
        await self._runs.mark_round_deciding(round_.id)
        return await self._runs.accept_round(
            round_.id,
            final_commit="c" * 40,
            final_message="accepted",
            finished_at=NOW,
        )

    async def reject(
        self,
        run: EvolutionRunRecord,
        round_: EvolutionRoundRecord,
        *,
        reason: str,
        job_id: str | None = None,
    ) -> EvolutionRoundRecord:
        del run, job_id
        self.calls.append(reason)
        await self._runs.mark_round_deciding(round_.id)
        return await self._runs.reject_round(round_.id, finished_at=NOW)


class WorkspaceDouble:
    """Record final branch publication without touching a real remote."""

    push_enabled = True

    def __init__(self) -> None:
        self.published: list[tuple[str, bool]] = []

    async def publish_run_branch(
        self,
        prepared: PreparedWorkspace,
        run: EvolutionRunRecord,
        *,
        expected_run_id: str | None = None,
        require_trailer: bool = False,
    ) -> None:
        assert expected_run_id == run.id
        self.published.append((prepared.branch, require_trailer))


@pytest_asyncio.fixture
async def state(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, EvolutionRepository]]:
    """Create one running full run with a due evaluating round."""
    database = create_database(
        Settings(
            db_dialect=DatabaseDialect.SQLITE,
            data_dir=tmp_path / "db",
            jwt_secret=SecretStr("m5-10-apply-final-secret"),
        ),
    )
    await database.start()
    await upgrade_database(database)
    repository = EvolutionRepository(database)
    await repository.create_run(
        {
            "id": "run-apply",
            "title": "placeholder apply",
            "mode": "full",
            "engine_key": "full",
            "eval_config": {},
            "target_config_id": None,
            "target_type": "skill",
            "target_name": "deep-research",
            "target_version": "v2.0.0",
            "repo_url": str(tmp_path / "source.git"),
            "sub_dir": "skills/deep-research",
            "base_ref": "v2.0.0",
            "base_commit": "a" * 40,
            "branch_name": "evo/20261028-run-apply",
            "repo_path": str(tmp_path / "repo"),
            "workspace_path": str(tmp_path / "workspace"),
            "status": "running",
            "progress_unit": 4,
            "stats": {},
            "created_by": None,
            "created_at": NOW,
        },
    )
    async with database.transaction() as session:
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="cluster-apply",
                run_id="run-apply",
                seq=1,
                probe_cluster_description="placeholder failure",
                probe_cluster_type="error",
                confidence=0.8,
                impact=0.9,
                severity=0.9,
                rank=1,
                created_at=NOW,
            ),
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="round-apply",
                run_id="run-apply",
                probe_cluster_id="cluster-apply",
                seq=1,
                status="evaluating",
                candidate_ref="evo/20261028-run-apply/candidate/r1",
                candidate_commit="b" * 40,
                candidate_message="candidate",
                push_status="succeeded",
                eval_external_id="fake:run-apply:round-apply:" + "b" * 40,
                eval_status="completed",
                eval_attempts=1,
                eval_next_poll_at=None,
                eval_started_at=NOW - timedelta(minutes=5),
                eval_finished_at=NOW - timedelta(minutes=1),
                eval_current=80.0,
                eval_candidate=90.0,
                eval_result={
                    "currentScore": 80.0,
                    "candidateScore": 90.0,
                    "externalVerdict": "accept",
                    "metrics": [],
                },
            ),
        )
    try:
        yield database, repository
    finally:
        await database.dispose()


def _job(kind: str, run_id: str = "run-apply") -> JobRunRecord:
    """Build one terminal-style job carrying the round payload."""
    return JobRunRecord(
        id=f"job-{kind}",
        kind=kind,
        ref_id=run_id,
        status="running",
        progress=0,
        executor="local",
        external_job_id=None,
        attempt=1,
        payload={"evolutionRoundId": "round-apply"},
        result=None,
        error=None,
        created_by=None,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        updated_at=NOW,
    )


async def test_submitter_persists_external_id_and_is_idempotent(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """Submission writes both the external id and the first durable poll deadline."""
    _database, repository = state
    run = await repository.get_run("run-apply")
    round_ = await repository.get_round("round-apply")
    assert run is not None and round_ is not None
    async with _database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(
                eval_external_id=None,
                eval_status="pending",
                eval_result=None,
                eval_finished_at=None,
            ),
        )
    round_ = await repository.get_round("round-apply")
    assert round_ is not None

    class Gateway:
        config = EvalGatewayConfig.from_mapping({})
        submissions = 0

        async def submit(self, request: Any) -> Any:
            self.submissions += 1
            from app.service.evolution.engine.contracts import EvalSubmission

            return EvalSubmission(eval_id=f"fake:{request.evolution_round_id}")

    gateway = Gateway()
    submitter = EvolutionEvalSubmitter(
        repository,
        cast(Any, lambda _run: gateway),
        events=None,
        now=lambda: NOW,
    )

    first = await submitter.submit(run, round_)
    second = await submitter.submit(run, first)

    assert first.eval_external_id == "fake:round-apply"
    assert first.eval_status == "evaluating"
    assert first.eval_next_poll_at == NOW + timedelta(minutes=5)
    assert second.eval_external_id == first.eval_external_id
    assert gateway.submissions == 1


async def test_submitter_failure_blocks_evaluation_without_raw_secret(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """Provider submission failure enters blocked(eval) instead of terminal failure."""
    database, repository = state
    run = await repository.get_run("run-apply")
    round_ = await repository.get_round("round-apply")
    assert run is not None and round_ is not None
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(
                eval_external_id=None,
                eval_status="pending",
                eval_result=None,
                eval_finished_at=None,
            ),
        )
    round_ = await repository.get_round("round-apply")
    assert round_ is not None

    class FailingGateway:
        async def submit(self, request: Any) -> Any:
            del request
            raise RuntimeError("token=s3cr3t")

    submitter = EvolutionEvalSubmitter(
        repository,
        cast(Any, lambda _run: FailingGateway()),
        events=None,
        now=lambda: NOW,
    )

    with pytest.raises(RunBlocked) as raised:
        await submitter.submit(run, round_)

    run = await repository.get_run("run-apply")
    round_ = await repository.get_round("round-apply")
    assert raised.value.stage == "eval"
    assert run is not None and round_ is not None
    assert run.status == "blocked"
    assert run.blocked_stage == "eval"
    assert round_.eval_status == "failed"
    assert "s3cr3t" not in (round_.eval_error or "")


async def test_apply_accepts_once_and_skipped_rounds_are_terminal(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """The apply job turns one completed result into one accepted round."""
    _database, repository = state
    candidates = CandidateDouble(repository)
    service = EvolutionApplyService(
        repository,
        candidates,  # type: ignore[arg-type]
        EmptyProbeReader(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    first = await service.handle(_job("evolution_apply"))
    second = await service.handle(_job("evolution_apply"))

    assert first["status"] == "accepted"
    assert first["accepted"] is True
    assert second["applied"] is False
    assert len(candidates.calls) == 1
    round_ = await repository.get_round("round-apply")
    assert round_ is not None
    assert round_.status == "accepted"
    run = await repository.get_run("run-apply")
    assert run is not None
    assert run.progress_unit == 5


async def test_apply_rejects_with_external_verdict_and_advances_once(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A reject verdict removes the candidate through the real decision boundary."""
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(
                eval_result={
                    "currentScore": 90.0,
                    "candidateScore": 80.0,
                    "externalVerdict": "reject",
                    "metrics": [],
                },
            ),
        )
    service = EvolutionApplyService(
        repository,
        CandidateDouble(repository),  # type: ignore[arg-type]
        EmptyProbeReader(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = await service.handle(_job("evolution_apply"))

    assert result["status"] == "rejected"
    round_ = await repository.get_round("round-apply")
    run = await repository.get_run("run-apply")
    assert round_ is not None and run is not None
    assert round_.status == "rejected"
    assert run.progress_unit == 5


async def test_finalize_writes_final_publishes_and_completes_run(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A terminal round produces a final row before the run becomes complete."""
    _database, repository = state
    await CandidateDouble(repository).accept(
        await repository.get_run("run-apply"),  # type: ignore[arg-type]
        await repository.get_round("round-apply"),  # type: ignore[arg-type]
        CandidateTrace("cluster-apply", "placeholder failure", (), (), "analysis"),
        current_score=80,
        candidate_score=90,
        reason="accepted",
    )
    workspace = WorkspaceDouble()
    service = EvolutionFinalizeService(
        repository,
        workspace,  # type: ignore[arg-type]
        events=None,
        now=lambda: NOW,
    )

    result = await service.handle(_job("evolution_finalize"))

    assert result["finalized"] is True
    final = await repository.get_final("run-apply")
    run = await repository.get_run("run-apply")
    assert final is not None
    assert final.verdict is True
    assert final.init_score == 80
    assert final.best_score == 90
    assert run is not None
    assert run.status == "completed"
    assert run.push_status == "succeeded"
    assert workspace.published == [("evo/20261028-run-apply", True)]


async def test_lifecycle_schedules_next_round_or_final_not_both(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """Terminal lifecycle advances pending work and finalizes once all rounds end."""
    _database, repository = state
    jobs = JobCapture()
    lifecycle = EvolutionJobLifecycle(
        repository,
        jobs,
        WorkspaceDouble(),  # type: ignore[arg-type]
        schedule_candidate_advance=True,
    )
    job = replace(
        _job("evolution_apply"),
        status="completed",
        result={"status": "accepted"},
    )

    await lifecycle.on_terminal(job)

    assert jobs.items == []
    async with _database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(status="accepted", accepted=True),
        )
        await session.execute(
            update(evolution_runs)
            .where(evolution_runs.c.id == "run-apply")
            .values(progress_unit=5),
        )
    await lifecycle.on_terminal(job)
    assert [item.kind for item in jobs.items] == ["evolution_finalize"]


async def test_lifecycle_advances_a_pending_round_before_finalizing(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A completed decision advances pending work and finalizes only after it ends."""
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            insert(probe_clusters).values(
                probe_cluster_id="cluster-apply-2",
                run_id="run-apply",
                seq=2,
                probe_cluster_description="second placeholder failure",
                probe_cluster_type="error",
                confidence=0.7,
                impact=0.7,
                severity=0.7,
                rank=2,
                created_at=NOW,
            ),
        )
        await session.execute(
            insert(evolution_rounds).values(
                id="round-apply-2",
                run_id="run-apply",
                probe_cluster_id="cluster-apply-2",
                seq=2,
                status="pending",
                push_status="pending",
                eval_status="pending",
            ),
        )
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(status="accepted", accepted=True),
        )
        await session.execute(
            update(evolution_runs)
            .where(evolution_runs.c.id == "run-apply")
            .values(progress_unit=5),
        )
    jobs = JobCapture()
    lifecycle = EvolutionJobLifecycle(
        repository,
        jobs,
        WorkspaceDouble(),  # type: ignore[arg-type]
        schedule_candidate_advance=True,
    )
    terminal_job = replace(
        _job("evolution_apply"),
        status="completed",
        result={"status": "accepted"},
    )

    await lifecycle.on_terminal(terminal_job)
    assert [item.kind for item in jobs.items] == ["evolution_advance"]

    jobs.items.clear()
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply-2")
            .values(status="rejected"),
        )
        await session.execute(
            update(evolution_runs)
            .where(evolution_runs.c.id == "run-apply")
            .values(progress_unit=6),
        )
    await lifecycle.on_terminal(terminal_job)
    assert [item.kind for item in jobs.items] == ["evolution_finalize"]


async def test_terminal_followup_recovers_when_lifecycle_submit_fails(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A terminal decision remains recoverable even if its next job was never enqueued."""
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(status="accepted", accepted=True, finished_at=NOW),
        )
        await session.execute(
            update(evolution_runs)
            .where(evolution_runs.c.id == "run-apply")
            .values(progress_unit=5),
        )
    jobs = JobCapture(failures_remaining=1)
    poller = EvolutionEvalPoller(
        repository,
        lambda _run: (_ for _ in ()).throw(AssertionError("gateway must not run")),
        jobs,
        events=None,
        now=lambda: NOW,
    )

    assert await poller.run_once() == 0
    assert jobs.items == []
    assert await poller.run_once() == 1
    assert [item.kind for item in jobs.items] == ["evolution_finalize"]


def test_decision_text_uses_numeric_score_direction() -> None:
    """External verdict stays authoritative without making the score prose lie."""
    accepted_lower = EvalResult(
        current_score=90.0,
        candidate_score=80.0,
        external_verdict="accept",
    )
    rejected_higher = EvalResult(
        current_score=80.0,
        candidate_score=90.0,
        external_verdict="reject",
    )

    assert "低于基线" in decision_sentence(accepted_lower, accepted=True)
    assert "高于基线" in decision_sentence(rejected_higher, accepted=False)


async def test_accepted_lower_score_is_not_reported_as_a_final_improvement(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """An authoritative accept may lower the score, so final verdict must stay false."""
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(evolution_rounds.c.id == "round-apply")
            .values(
                eval_current=90.0,
                eval_candidate=80.0,
                eval_result={
                    "currentScore": 90.0,
                    "candidateScore": 80.0,
                    "externalVerdict": "accept",
                    "metrics": [],
                },
            ),
        )
    await CandidateDouble(repository).accept(
        await repository.get_run("run-apply"),  # type: ignore[arg-type]
        await repository.get_round("round-apply"),  # type: ignore[arg-type]
        CandidateTrace("cluster-apply", "placeholder failure", (), (), "analysis"),
        current_score=90,
        candidate_score=80,
        reason="accepted by external verdict",
    )
    service = EvolutionFinalizeService(
        repository,
        WorkspaceDouble(),  # type: ignore[arg-type]
        events=None,
        now=lambda: NOW,
    )

    await service.handle(_job("evolution_finalize"))

    final = await repository.get_final("run-apply")
    assert final is not None
    assert final.verdict is False
    assert final.best_score == 90.0


async def test_finalize_push_failure_preserves_final_and_blocks(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A final publication failure remains recoverable and never marks complete."""
    _database, repository = state
    await CandidateDouble(repository).accept(
        await repository.get_run("run-apply"),  # type: ignore[arg-type]
        await repository.get_round("round-apply"),  # type: ignore[arg-type]
        CandidateTrace("cluster-apply", "placeholder failure", (), (), "analysis"),
        current_score=80,
        candidate_score=90,
        reason="accepted",
    )

    class FailingWorkspace(WorkspaceDouble):
        async def publish_run_branch(
            self,
            prepared: PreparedWorkspace,
            run: EvolutionRunRecord,
            *,
            expected_run_id: str | None = None,
            require_trailer: bool = False,
        ) -> None:
            del prepared, run, expected_run_id, require_trailer
            raise GitOperationError("remote rejected token=s3cr3t", stage="push")

    service = EvolutionFinalizeService(
        repository,
        FailingWorkspace(),  # type: ignore[arg-type]
        events=None,
        now=lambda: NOW,
    )

    with pytest.raises(RunBlocked):
        await service.handle(_job("evolution_finalize"))

    final = await repository.get_final("run-apply")
    run = await repository.get_run("run-apply")
    assert final is not None
    assert run is not None
    assert run.status == "blocked"
    assert run.blocked_stage == "push"
    assert "s3cr3t" not in (run.error_message or "")


async def test_finalize_write_failure_is_recoverable(
    state: tuple[Database, EvolutionRepository],
) -> None:
    """A transient final write blocks recoverably and succeeds on retry."""
    _database, repository = state
    await CandidateDouble(repository).accept(
        await repository.get_run("run-apply"),  # type: ignore[arg-type]
        await repository.get_round("round-apply"),  # type: ignore[arg-type]
        CandidateTrace("cluster-apply", "placeholder failure", (), (), "analysis"),
        current_score=80,
        candidate_score=90,
        reason="accepted",
    )

    class FlakyRepository:
        def __init__(self) -> None:
            self.fail = True

        async def write_final(self, values: Mapping[str, Any]) -> Any:
            if self.fail:
                raise RuntimeError("transient final write failure")
            return await repository.write_final(values)

        def __getattr__(self, name: str) -> Any:
            return getattr(repository, name)

    flaky = FlakyRepository()
    service = EvolutionFinalizeService(
        cast(Any, flaky),
        WorkspaceDouble(),  # type: ignore[arg-type]
        events=None,
        now=lambda: NOW,
    )

    with pytest.raises(RunBlocked) as raised:
        await service.handle(_job("evolution_finalize"))

    blocked = await repository.get_run("run-apply")
    assert raised.value.stage == "push"
    assert raised.value.error_stage == "final"
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.error_stage == "final"
    assert await repository.get_final("run-apply") is None

    flaky.fail = False
    await repository.mark_resumed("run-apply")
    result = await service.handle(_job("evolution_finalize"))

    completed = await repository.get_run("run-apply")
    assert result["finalized"] is True
    assert completed is not None
    assert completed.status == "completed"
    assert await repository.get_final("run-apply") is not None


@pytest.mark.parametrize(
    "current,candidate,external,accepted",
    [
        (80.0, 90.0, "reject", True),
        (90.0, 80.0, "accept", False),
        (80.0, 80.0, "accept", False),
    ],
)
async def test_apply_uses_strict_score_not_external_verdict(
    state: tuple[Database, EvolutionRepository],
    current: float,
    candidate: float,
    external: str,
    accepted: bool,
) -> None:
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(
                eval_result={
                    "currentScore": current,
                    "candidateScore": candidate,
                    "externalVerdict": external,
                    "metrics": [],
                }
            )
        )
    service = EvolutionApplyService(repository, CandidateDouble(repository), EmptyProbeReader())  # type: ignore[arg-type]
    result = await service.handle(_job("evolution_apply"))
    assert result["accepted"] is accepted


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("fail_first", [True, False])
@pytest.mark.parametrize("bad_evidence", [True, False])
async def test_independent_final_is_durable_and_requires_an_accepted_change(
    state: tuple[Database, EvolutionRepository],
    accepted: bool,
    fail_first: bool,
    bad_evidence: bool,
) -> None:
    from app.db.crud.skill_evolution import SkillEvolutionRepository
    from app.service.evolution.engine.contracts import EvalStatus, EvalSubmission

    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(
                status="accepted" if accepted else "rejected",
                accepted=accepted,
                commit_hash="c" * 40 if accepted else None,
            )
        )
        await session.execute(
            update(evolution_runs)
            .where(
                evolution_runs.c.id == "run-apply",
            )
            .values(progress_unit=5, eval_config={"skillEvolution": "real"})
        )
    states = SkillEvolutionRepository(database)
    await states.initialize("run-apply", {"files": {"SKILL.md": "base"}})
    now = [NOW]
    requests = []

    class Gateway:
        async def submit(self, request: Any) -> EvalSubmission:
            requests.append(request)
            return EvalSubmission(f"final-pair-{len(requests)}")

        async def get_status(self, eval_id: str) -> EvalStatus:
            if fail_first and not bad_evidence and eval_id == "final-pair-1":
                return EvalStatus("failed")
            return EvalStatus("completed")

        async def get_result(self, eval_id: str) -> EvalResult:
            if fail_first and bad_evidence and eval_id == "final-pair-1":
                from app.third_party_api.evaluation.models import InvalidEvaluationEvidence

                raise InvalidEvaluationEvidence("Evaluation case sets are not comparable")
            return EvalResult(40.0, 55.0, "accept")

    class Workspaces(WorkspaceDouble):
        async def publish_skill_snapshot(self, path: Path, *, branch: str, commit: str) -> str:
            assert commit == ("c" * 40 if accepted else "a" * 40)
            return branch

    workspaces = Workspaces()

    def service() -> EvolutionFinalizeService:
        return EvolutionFinalizeService(
            repository,
            workspaces,  # type: ignore[arg-type]
            gateway_for_run=lambda run: Gateway(),  # type: ignore[arg-type,return-value]
            skill_states=states,
            now=lambda: now[0],
        )

    first = await service().handle(_job("evolution_finalize"))
    assert first["finalized"] is False
    assert await repository.get_final("run-apply") is None
    # Reconstructing the service simulates restart: no second external submission.
    await service().handle(_job("evolution_finalize"))
    assert len(requests) == 1
    now[0] += timedelta(minutes=5)
    if fail_first:
        with pytest.raises(RunBlocked) as error:
            await service().handle(_job("evolution_finalize"))
        assert error.value.error_stage == "final_eval"
        blocked = await repository.get_run("run-apply")
        assert blocked is not None and blocked.blocked_stage == "eval"
        await repository.mark_resumed("run-apply")
        await service().handle(_job("evolution_finalize"))
        assert len(requests) == 2
        assert requests[0].idempotency_key != requests[1].idempotency_key
        now[0] += timedelta(minutes=5)
    result = await service().handle(_job("evolution_finalize"))
    assert result["finalized"] is True
    final = await repository.get_final("run-apply")
    assert final is not None
    assert (final.init_score, final.best_score) == (40.0, 55.0)
    assert final.verdict is accepted
    assert requests[0].baseline_ref == "evo-snapshot/run-apply/baseline"
    assert requests[0].ref == "evo-snapshot/run-apply/final-best"


async def test_feedback_precedes_independent_final_evaluation(
    state: tuple[Database, EvolutionRepository],
) -> None:
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(status="rejected")
        )
    async with database.transaction() as session:
        await session.execute(
            update(evolution_runs)
            .where(
                evolution_runs.c.id == "run-apply",
            )
            .values(eval_config={"skillEvolution": "real"})
        )
    calls = []

    class Feedback:
        async def advance_if_needed(self, run: Any, rounds: Any) -> bool:
            calls.append((run.id, len(rounds)))
            return True

    def gateway(run: Any) -> Any:
        raise AssertionError("final test must not run before validation feedback finishes")

    service = EvolutionFinalizeService(
        repository,
        WorkspaceDouble(),  # type: ignore[arg-type]
        feedback=Feedback(),
        gateway_for_run=gateway,
        skill_states=object(),  # type: ignore[arg-type]
    )
    result = await service.handle(_job("evolution_finalize"))
    assert result["finalized"] is False
    assert calls == [("run-apply", 1)]
    assert await repository.get_final("run-apply") is None


async def test_apply_and_retry_preserve_frozen_feedback_provenance(
    state: tuple[Database, EvolutionRepository],
) -> None:
    from app.db.crud.skill_evolution import SkillEvolutionRepository
    from app.service.evolution.retry import EvolutionRetryService

    database, repository = state
    states = SkillEvolutionRepository(database)
    saved = await states.initialize("run-apply", {"reports": []})
    await states.save_checkpoint(
        "run-apply",
        {
            "cluster_map": {"cluster-apply": {"source_probe_ids": ["frozen-probe"]}},
            "source_reports": [
                {
                    "probe_id": "frozen-probe",
                    "session_id": "frozen-session",
                    "probe_description": "frozen description",
                    "probe_analysis": "frozen analysis",
                    "from_bad_cases": [{"bad_case_id": "frozen-case"}],
                }
            ],
        },
        expected_revision=saved.revision,
    )

    class NoLiveReads:
        async def list_rows_by_ids(self, ids: Sequence[str]) -> tuple[ProbeHitRow, ...]:
            raise AssertionError("must not reread mutable upstream probes")

    candidates = CandidateDouble(repository)
    apply = EvolutionApplyService(
        repository,
        candidates,  # type: ignore[arg-type]
        NoLiveReads(),
        skill_states=states,
    )
    retry = EvolutionRetryService(
        repository,
        candidates,  # type: ignore[arg-type]
        JobCapture(),  # type: ignore[arg-type]
        probes=NoLiveReads(),
        skill_states=states,
    )
    run = await repository.get_run("run-apply")
    round_ = await repository.get_round("round-apply")
    assert run is not None and round_ is not None
    for service in (apply, retry):
        trace = await service._trace(run, round_)
        assert trace.probes == ("frozen-probe",)
        assert trace.bad_cases == ("frozen-case",)
        assert trace.sources[0].session_id == "frozen-session"
        assert trace.analysis == "frozen analysis"


async def test_real_run_never_falls_back_to_synthetic_final_after_restart(
    state: tuple[Database, EvolutionRepository],
) -> None:
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(status="rejected")
        )
        await session.execute(
            update(evolution_runs)
            .where(
                evolution_runs.c.id == "run-apply",
            )
            .values(eval_config={"skillEvolution": "real"}, progress_unit=5)
        )
    service = EvolutionFinalizeService(repository, WorkspaceDouble())  # type: ignore[arg-type]
    with pytest.raises(RunBlocked) as error:
        await service.handle(_job("evolution_finalize"))
    assert error.value.error_stage == "final_eval"
    assert await repository.get_final("run-apply") is None


async def test_legacy_skill_keeps_legacy_final_when_real_server_is_enabled(
    state: tuple[Database, EvolutionRepository],
) -> None:
    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(status="rejected")
        )
        await session.execute(
            update(evolution_runs)
            .where(
                evolution_runs.c.id == "run-apply",
            )
            .values(progress_unit=5)
        )

    def forbidden_gateway(run: Any) -> Any:
        raise AssertionError("legacy run must not use newly enabled real evaluation")

    service = EvolutionFinalizeService(
        repository,
        WorkspaceDouble(),  # type: ignore[arg-type]
        gateway_for_run=forbidden_gateway,
    )
    result = await service.handle(_job("evolution_finalize"))
    assert result["finalized"] is True
    final = await repository.get_final("run-apply")
    assert final is not None and not final.verdict


@pytest.mark.parametrize("final_phase", [True, False])
async def test_ambiguous_submission_requires_reconciliation_not_new_attempt(
    state: tuple[Database, EvolutionRepository],
    final_phase: bool,
) -> None:
    from app.db.crud.skill_evolution import SkillEvolutionRepository
    from app.service.evolution.retry import EvolutionRetryService
    from app.third_party_api.evaluation.models import AmbiguousEvaluationSubmission

    database, repository = state
    async with database.transaction() as session:
        await session.execute(
            update(evolution_rounds)
            .where(
                evolution_rounds.c.id == "round-apply",
            )
            .values(
                status="rejected" if final_phase else "evaluating",
                eval_status="pending",
                eval_external_id=None,
            )
        )
        await session.execute(
            update(evolution_runs)
            .where(
                evolution_runs.c.id == "run-apply",
            )
            .values(eval_config={"skillEvolution": "real"}, progress_unit=5 if final_phase else 4)
        )
    states = SkillEvolutionRepository(database)
    await states.initialize("run-apply", {"files": {"SKILL.md": "base"}})
    requests = []

    class Gateway:
        async def submit(self, request: Any) -> Any:
            requests.append(request)
            raise AmbiguousEvaluationSubmission("provider accepted status is unknown")

    class Workspaces(WorkspaceDouble):
        async def publish_skill_snapshot(self, path: Path, *, branch: str, commit: str) -> str:
            return branch

    with pytest.raises(RunBlocked) as error:
        if final_phase:
            service = EvolutionFinalizeService(
                repository,
                Workspaces(),  # type: ignore[arg-type]
                gateway_for_run=lambda run: Gateway(),  # type: ignore[arg-type,return-value]
                skill_states=states,
            )
            await service.handle(_job("evolution_finalize"))
        else:
            submitter = EvolutionEvalSubmitter(
                repository,
                lambda run: Gateway(),  # type: ignore[arg-type,return-value]
                events=None,
            )
            run = await repository.get_run("run-apply")
            round_ = await repository.get_round("round-apply")
            assert run is not None and round_ is not None
            await submitter.submit(run, round_)
    assert error.value.retryable is False
    assert error.value.error_stage.endswith("_submission_ambiguous")
    retry = EvolutionRetryService(
        repository,
        CandidateDouble(repository),  # type: ignore[arg-type]
        JobCapture(),  # type: ignore[arg-type]
    )
    with pytest.raises(AppError) as admission:
        await retry.request_retry("run-apply", None)
    assert admission.value.code == ErrorCode.RUN_NOT_RETRYABLE
    with pytest.raises(RunBlocked) as replay:
        await retry.handle(_job("evolution_retry"))
    assert replay.value.retryable is False
    assert len(requests) == 1
    saved = await states.get("run-apply")
    assert saved is not None and "final_evaluation" not in saved.checkpoint
    round_ = await repository.get_round("round-apply")
    assert round_ is not None and round_.eval_attempts == 1
