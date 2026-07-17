"""Blob-first boundary tests for the Task-9 terminal publication engine.

These tests deliberately make the database UnitOfWork phase observable.  A terminal
draft may read authority inside a short UoW, and its fresh commit may write authority
inside another UoW, but every ``put_verified`` must occur between those phases.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Callable

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.apps.worker.publication import (
    WorkerArtifactPort,
    WorkerBlobStager,
)
from gameforge.contracts.lineage import (
    AuditActor,
    ObjectBinding,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import ObjectStat, StoredObject
from gameforge.platform.terminal_staging import (
    BlobMaterial,
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
)
from gameforge.platform.runs.commands import (
    RunCommandCapabilities,
    RunCommandService,
)
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    PublishAttemptOutcomeRequest,
    ReapExpiredLeaseRequest,
    RunFailurePublication,
    RunLifecycleCapabilities,
    RunLifecycleService,
    RunResultPublication,
    SweepRunTimeoutRequest,
)
from gameforge.runtime.clock import FrozenUtcClock
from tests.platform.m4.test_run_fencing import (
    NOW_DT,
    WORKER,
    _AllowSubmissionAuthorization,
    _Publication,
    _fence,
    _start,
)
from tests.platform.m4.test_run_retry_cancel_timeout import (
    _as_queued,
    _cancel_command,
    _prepared_failure,
    _prepared_success,
    _publish_failure,
    _run_harness,
)


_HUMAN = AuditActor(principal_id="human:a", principal_kind="human")


class _TrackingUow:
    """Wrap the rollback-capable test UoW and expose its active phase."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.active = False
        self.begin_count = 0

    @contextmanager
    def begin(self):
        assert not self.active, "the terminal flow must not nest database UnitOfWork scopes"
        self.begin_count += 1
        self.active = True
        try:
            with self._delegate.begin() as transaction:  # type: ignore[attr-defined]
                yield transaction
        finally:
            self.active = False


class _ReadScopeTracker:
    """Independent read snapshot; it never enters the write UnitOfWork."""

    def __init__(self) -> None:
        self.active = False
        self.begin_count = 0

    @contextmanager
    def begin(self):
        assert not self.active
        self.active = True
        self.begin_count += 1
        try:
            yield object()
        finally:
            self.active = False


class _PutVerifiedSpy:
    """Tiny ObjectStore whose writes are forbidden while the DB UoW is active."""

    def __init__(
        self,
        uow: _TrackingUow,
        reads: _ReadScopeTracker,
        *,
        fail_on_call: int | None = None,
    ) -> None:
        self._uow = uow
        self._reads = reads
        self._fail_on_call = fail_on_call
        self.calls: list[bytes] = []
        self._stats: dict[ObjectLocation, ObjectStat] = {}

    def put_verified(self, source: bytes) -> StoredObject:
        assert not self._uow.active, "ObjectStore.put_verified ran inside the DB write UoW"
        assert not self._reads.active, "ObjectStore.put_verified ran inside the read snapshot"
        payload = bytes(source)
        self.calls.append(payload)
        if self._fail_on_call == len(self.calls):
            raise OSError("injected partial terminal blob-stage failure")
        ref = object_ref_for_bytes(payload)
        stored = StoredObject(
            ref=ref,
            location=ObjectLocation(
                store_id="stage-test",
                key=ref.key,
                backend_generation=f"generation:{len(self.calls)}",
            ),
        )
        self._stats[stored.location] = ObjectStat(
            ref=stored.ref,
            location=stored.location,
            verified_at="2026-07-16T12:00:00Z",
        )
        return stored

    def stat(self, location: ObjectLocation) -> ObjectStat:
        return self._stats[location]


class _BlobStager:
    """Production stager with an optional test-only after-stage race hook."""

    def __init__(
        self,
        objects: _PutVerifiedSpy,
        *,
        after_stage: Callable[[], None] | None = None,
    ) -> None:
        self._delegate = WorkerBlobStager(objects)
        self._after_stage = after_stage
        self.calls = 0

    def stage(
        self, drafts: tuple[TerminalPublicationDraft, ...]
    ) -> tuple[StagedTerminalPublication, ...]:
        self.calls += 1
        staged = self._delegate.stage(drafts)
        if self._after_stage is not None:
            self._after_stage()
        return staged


class _ThreePhasePublication(_Publication):
    """Authority-free planner plus transaction-bound commit test double."""

    def __init__(
        self,
        state: object,
        repo: object,
        uow: _TrackingUow,
        reads: _ReadScopeTracker,
    ) -> None:
        super().__init__(state, repo)
        self._uow = uow
        self._reads = reads
        self.projection_epoch = 1
        self.projection_epoch_by_kind: dict[str, int] = {}
        self.plan_calls: list[str] = []
        self.commit_calls = 0

    def _draft(
        self,
        *,
        publication_kind: str,
        run: object,
        attempt_no: int | None,
        occurred_at: str,
        result: object,
        material_count: int,
    ) -> TerminalPublicationDraft:
        assert self._uow.active != self._reads.active, (
            "terminal planning must use exactly one read or write snapshot"
        )
        self.plan_calls.append(publication_kind)
        payloads = tuple(
            f"{publication_kind}:{run.run_id}:{attempt_no}:{ordinal}".encode()  # type: ignore[attr-defined]
            for ordinal in range(1, material_count + 1)
        )
        materials = tuple(
            BlobMaterial(
                slot=f"blob:{ordinal}",
                payload=payload,
                expected_ref=object_ref_for_bytes(payload),
            )
            for ordinal, payload in enumerate(payloads, start=1)
        )
        operation_projection = (
            {
                "operation": "test.commit",
                "publication_kind": publication_kind,
                "projection_epoch": self.projection_epoch
                + self.projection_epoch_by_kind.get(publication_kind, 0),
            },
        )
        result_projection = result.model_dump(mode="json")  # type: ignore[attr-defined]
        canonical_projection = {
            "publication_kind": publication_kind,
            "run_id": run.run_id,  # type: ignore[attr-defined]
            "attempt_no": attempt_no,
            "occurred_at": occurred_at,
            "materials": tuple(
                {
                    "slot": material.slot,
                    "expected_ref": material.expected_ref.model_dump(mode="json"),
                }
                for material in materials
            ),
            "operations": operation_projection,
            "result": result_projection,
        }
        return TerminalPublicationDraft(
            publication_kind=publication_kind,
            run_id=run.run_id,  # type: ignore[attr-defined]
            attempt_no=attempt_no,
            occurred_at=occurred_at,
            projection_digest=canonical_sha256(canonical_projection),
            materials=materials,
            operations=(("commit", publication_kind),),
            operation_projection=operation_projection,
            result_projection=result_projection,
            result=result,
        )

    def plan_run_result(self, **kwargs: object) -> TerminalPublicationDraft:
        run = kwargs["run"]
        attempt = kwargs["attempt"]
        result = RunResultPublication(
            result_artifact_id=f"artifact:run-result:{run.run_id}:{attempt.attempt_no}",
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=None,
        )
        return self._draft(
            publication_kind="run_result",
            run=run,
            attempt_no=attempt.attempt_no,
            occurred_at=str(kwargs["occurred_at"]),
            result=result,
            material_count=2,
        )

    def plan_attempt_failure(self, **kwargs: object) -> TerminalPublicationDraft:
        run = kwargs["run"]
        attempt = kwargs["attempt"]
        result = AttemptFailurePublication(
            failure_artifact_id=f"artifact:attempt-failure:{run.run_id}:{attempt.attempt_no}",
            cassette_bundle_artifact_id=None,
        )
        return self._draft(
            publication_kind="attempt_failure",
            run=run,
            attempt_no=attempt.attempt_no,
            occurred_at=str(kwargs["occurred_at"]),
            result=result,
            material_count=1,
        )

    def plan_run_failure(self, **kwargs: object) -> TerminalPublicationDraft:
        run = kwargs["run"]
        attempt = kwargs["attempt"]
        result = RunFailurePublication(
            failure_artifact_id=f"artifact:run-failure:{run.run_id}",
            terminal_cassette_artifact_id=None,
        )
        return self._draft(
            publication_kind="run_failure",
            run=run,
            attempt_no=(attempt.attempt_no if attempt is not None else None),
            occurred_at=str(kwargs["occurred_at"]),
            result=result,
            material_count=1,
        )

    def plan_active_failure_aggregate(
        self,
        *,
        run,
        attempt,
        prepared,
        retry_decision,
        attempt_policy,
        run_policy,
        occurred_at,
        actor,
    ) -> tuple[TerminalPublicationDraft, ...]:
        attempt_draft = self.plan_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            policy=attempt_policy,
            occurred_at=occurred_at,
            actor=actor,
        )
        if run_policy is None:
            return (attempt_draft,)
        attempt_result = attempt_draft.result
        return (
            attempt_draft,
            self.plan_run_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                policy=run_policy,
                attempt_failure_artifact_id=attempt_result.failure_artifact_id,
                occurred_at=occurred_at,
                actor=actor,
            ),
        )

    def commit_many(self, publications):
        for fresh_draft, staged in publications:
            if fresh_draft.projection_digest != staged.projection_digest:
                raise IntegrityViolation("fresh terminal projection differs from staged projection")
        return tuple(self.commit(fresh, staged) for fresh, staged in publications)

    def commit(
        self,
        fresh_draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
    ) -> object:
        assert self._uow.active, "terminal authority commit must run inside the DB UoW"
        if fresh_draft.projection_digest != staged.projection_digest:
            raise IntegrityViolation("fresh terminal projection differs from staged projection")
        assert {material.slot for material in fresh_draft.materials} == {
            receipt.slot for receipt in staged.receipts
        }
        self.commit_calls += 1
        self.state.publisher_actions.append(  # type: ignore[attr-defined]
            f"commit:{fresh_draft.publication_kind}"
        )
        return fresh_draft.result


class _SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = list(values)
        self._last = values[-1]

    def now_utc(self) -> datetime:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _lifecycle_service(
    harness,
    publication,
    uow,
    reads,
    stager,
    *,
    now: datetime = NOW_DT + timedelta(seconds=1),
    clock: object | None = None,
) -> RunLifecycleService:
    capabilities = RunLifecycleCapabilities(
        runs=harness.repo,
        registry=harness.registry,
        accounting=harness.accounting,
        publication=publication,
    )
    return RunLifecycleService(
        unit_of_work=uow,
        bind_capabilities=lambda transaction: capabilities,
        clock=clock or FrozenUtcClock(now.replace(microsecond=0)),
        stage_publications=stager,
        planning_scope=reads.begin,
        bind_planning_capabilities=lambda transaction: capabilities,
    )


def _command_service(
    harness,
    publication,
    uow,
    reads,
    stager,
    *,
    now: datetime = NOW_DT + timedelta(seconds=1),
) -> RunCommandService:
    capabilities = RunCommandCapabilities(
        runs=harness.repo,
        registry=harness.registry,
        admission=harness.accounting,
        publication=publication,
        accounting=harness.accounting,
        submission_authorization=_AllowSubmissionAuthorization(),
    )
    return RunCommandService(
        unit_of_work=uow,
        bind_capabilities=lambda transaction: capabilities,
        clock=FrozenUtcClock(now.replace(microsecond=0)),
        stage_publications=stager,
        planning_scope=reads.begin,
        bind_planning_capabilities=lambda transaction: capabilities,
    )


def test_active_success_stages_every_blob_between_read_and_write_uows() -> None:
    harness = _run_harness()
    _start(harness)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    result = service.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_success(harness),
            actor=WORKER,
        )
    )

    assert result.run.status == "succeeded"
    assert publication.plan_calls == ["run_result", "run_result"]
    assert publication.commit_calls == 1
    assert stager.calls == 1
    assert len(objects.calls) == 2
    assert reads.begin_count == 1
    assert uow.begin_count == 1


def test_success_superseded_after_stage_restages_typed_terminal_without_success_writes() -> None:
    harness = _run_harness()
    _start(harness)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    superseded = _prepared_failure(
        harness,
        cause_code="subject_superseded",
        failure_class="subject_superseded",
        intrinsic_retry_eligible=False,
    )
    stager = _BlobStager(
        objects,
        after_stage=lambda: setattr(
            harness.state,
            "forced_preflight_outcome",
            superseded,
        ),
    )
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    result = service.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_success(harness),
            actor=WORKER,
        )
    )

    assert result.run.status == "cancelled"
    assert result.run.result_artifact_id is None
    assert result.result_artifact_id is None
    assert result.retry_decision is not None
    assert result.retry_decision.cause_code == "subject_superseded"
    assert result.attempt is not None and result.attempt.status == "cancelled"
    assert result.attempt_failure_artifact_id is not None
    assert result.run_failure_artifact_id is not None
    assert harness.state.publisher_actions == [
        "commit:attempt_failure",
        "commit:run_failure",
    ]
    assert publication.plan_calls == [
        "run_result",
        "attempt_failure",
        "run_failure",
        "attempt_failure",
        "run_failure",
        "attempt_failure",
        "run_failure",
    ]
    assert publication.commit_calls == 2
    assert stager.calls == 2
    assert len(objects.calls) == 4
    assert objects.calls[0].startswith(b"run_result:")
    assert objects.calls[1].startswith(b"run_result:")
    assert reads.begin_count == 2
    assert uow.begin_count == 2


@pytest.mark.parametrize(
    ("cause_code", "failure_class", "retry_eligible", "expected_status", "draft_count"),
    [
        ("dependency_unavailable", "transient_dependency", True, "retry_wait", 1),
        ("execution_failed", "execution", False, "failed", 2),
    ],
)
def test_active_failure_stages_complete_retry_or_terminal_aggregate(
    cause_code: str,
    failure_class: str,
    retry_eligible: bool,
    expected_status: str,
    draft_count: int,
) -> None:
    harness = _run_harness()
    _start(harness)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    result = service.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_failure(
                harness,
                cause_code=cause_code,
                failure_class=failure_class,
                intrinsic_retry_eligible=retry_eligible,
            ),
            actor=WORKER,
        )
    )

    assert result.run.status == expected_status
    assert publication.commit_calls == draft_count
    assert len(objects.calls) == draft_count
    assert reads.begin_count == 1
    assert uow.begin_count == 1


def test_terminal_aggregate_restages_run_drift_before_committing_attempt() -> None:
    harness = _run_harness()
    _start(harness)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(
        objects,
        after_stage=lambda: publication.projection_epoch_by_kind.__setitem__("run_failure", 1),
    )
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    result = service.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_failure(
                harness,
                cause_code="execution_failed",
                failure_class="execution",
                intrinsic_retry_eligible=False,
            ),
            actor=WORKER,
        )
    )

    assert result.run.status == "failed"
    assert publication.plan_calls == [
        "attempt_failure",
        "run_failure",
        "attempt_failure",
        "run_failure",
        "attempt_failure",
        "run_failure",
        "attempt_failure",
        "run_failure",
    ]
    assert publication.commit_calls == 2
    assert len(objects.calls) == 4
    assert stager.calls == 2
    assert reads.begin_count == 2
    assert uow.begin_count == 2


@pytest.mark.parametrize(
    ("overall_deadline", "reap_at", "expected_status", "draft_count"),
    [
        ("2026-07-14T12:01:00Z", NOW_DT + timedelta(seconds=11), "retry_wait", 1),
        ("2026-07-14T12:00:20Z", NOW_DT + timedelta(seconds=21), "timed_out", 2),
    ],
)
def test_reaper_stages_retry_or_terminal_aggregate_outside_write_uow(
    overall_deadline: str,
    reap_at: datetime,
    expected_status: str,
    draft_count: int,
) -> None:
    harness = _run_harness(
        overall_deadline_utc=overall_deadline,
        lease_expires_at="2026-07-14T12:00:10Z",
    )
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    service = _lifecycle_service(
        harness,
        publication,
        uow,
        reads,
        stager,
        now=reap_at,
    )

    result = service.reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id="run:1",
            expected_run_revision=2,
            actor=AuditActor(
                principal_id="system:lease-reaper",
                principal_kind="system",
            ),
        )
    )

    assert result.run.status == expected_status
    assert publication.commit_calls == draft_count
    assert len(objects.calls) == draft_count
    assert reads.begin_count == 1
    assert uow.begin_count == 1


def test_reaper_stage_crossing_overall_deadline_restages_typed_timeout() -> None:
    harness = _run_harness(
        overall_deadline_utc="2026-07-14T12:00:20Z",
        lease_expires_at="2026-07-14T12:00:10Z",
    )
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    clock = _SequenceClock(
        NOW_DT + timedelta(seconds=11),
        NOW_DT + timedelta(seconds=21),
    )
    service = _lifecycle_service(
        harness,
        publication,
        uow,
        reads,
        stager,
        clock=clock,
    )

    result = service.reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id="run:1",
            expected_run_revision=2,
            actor=AuditActor(
                principal_id="system:lease-reaper",
                principal_kind="system",
            ),
        )
    )

    assert result.run.status == "timed_out"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "overall_deadline_exhausted"
    assert len(objects.calls) == 3
    assert publication.commit_calls == 2
    assert stager.calls == 2
    assert reads.begin_count == 2
    assert uow.begin_count == 2


@pytest.mark.parametrize("initial_status", ["queued", "retry_wait", "active"])
def test_timeout_terminal_stages_all_blobs_outside_write_uow(
    initial_status: str,
) -> None:
    if initial_status == "queued":
        harness = _run_harness(overall_deadline_utc="2026-07-14T12:01:00Z")
        _as_queued(harness, queue_deadline="2026-07-14T12:00:05Z")
        timeout_at = NOW_DT + timedelta(seconds=6)
    elif initial_status == "retry_wait":
        harness = _run_harness(overall_deadline_utc="2026-07-14T12:00:05Z")
        _start(harness)
        _publish_failure(harness, at=NOW_DT + timedelta(seconds=1))
        timeout_at = NOW_DT + timedelta(seconds=6)
    else:
        harness = _run_harness(
            attempt_timeout_ns=5_000_000_000,
            overall_deadline_utc="2026-07-14T12:01:00Z",
        )
        _start(harness)
        timeout_at = NOW_DT + timedelta(seconds=6)

    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    service = _lifecycle_service(
        harness,
        publication,
        uow,
        reads,
        stager,
        now=timeout_at,
    )
    run = harness.state.runs["run:1"]

    result = service.sweep_timeout(
        SweepRunTimeoutRequest(
            run_id=run.run_id,
            expected_run_revision=run.revision,
            actor=AuditActor(
                principal_id="system:timeout-sweeper",
                principal_kind="system",
            ),
        )
    )

    expected_count = 2 if initial_status == "active" else 1
    assert result.run.status == "timed_out"
    assert publication.commit_calls == expected_count
    assert len(objects.calls) == expected_count
    assert reads.begin_count == 1
    assert uow.begin_count == 1


@pytest.mark.parametrize("initial_status", ["queued", "retry_wait"])
def test_inactive_cancel_stages_manifest_outside_write_uow(initial_status: str) -> None:
    harness = _run_harness()
    if initial_status == "queued":
        _as_queued(harness)
    else:
        _start(harness)
        _publish_failure(harness)
        assert harness.state.runs["run:1"].status == "retry_wait"

    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(objects)
    service = _command_service(
        harness,
        publication,
        uow,
        reads,
        stager,
        now=(
            NOW_DT + timedelta(seconds=2)
            if initial_status == "retry_wait"
            else NOW_DT + timedelta(seconds=1)
        ),
    )

    result = service.submit(
        run_id="run:1",
        command=_cancel_command(harness),
        actor=_HUMAN,
    )

    assert result.status == "accepted"
    assert harness.state.runs["run:1"].status == "cancelled"
    assert publication.plan_calls == ["run_failure", "run_failure"]
    assert publication.commit_calls == 1
    assert stager.calls == 1
    assert len(objects.calls) == 1
    assert reads.begin_count == 1
    assert uow.begin_count == 1


def test_partial_blob_stage_failure_leaves_all_database_authority_unchanged() -> None:
    harness = _run_harness()
    _start(harness)
    before = deepcopy(harness.state)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads, fail_on_call=2)
    stager = _BlobStager(objects)
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    with pytest.raises(OSError, match="partial terminal blob-stage failure"):
        service.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )

    assert harness.state == before
    assert len(objects.calls) == 2
    assert publication.plan_calls == ["run_result"]
    assert publication.commit_calls == 0
    assert reads.begin_count == 1
    assert uow.begin_count == 0


def test_persistent_projection_drift_exhausts_bound_with_zero_authority_writes() -> None:
    harness = _run_harness()
    _start(harness)
    before = deepcopy(harness.state)
    uow = _TrackingUow(harness.unit_of_work)
    reads = _ReadScopeTracker()
    publication = _ThreePhasePublication(harness.state, harness.repo, uow, reads)
    objects = _PutVerifiedSpy(uow, reads)
    stager = _BlobStager(
        objects,
        after_stage=lambda: setattr(
            publication,
            "projection_epoch",
            publication.projection_epoch + 1,
        ),
    )
    service = _lifecycle_service(harness, publication, uow, reads, stager)

    with pytest.raises(IntegrityViolation, match="did not stabilize"):
        service.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )

    assert harness.state == before
    assert publication.plan_calls == ["run_result"] * 6
    assert publication.commit_calls == 0
    assert len(objects.calls) == 6
    assert stager.calls == 3
    assert reads.begin_count == 3
    assert uow.begin_count == 3


class _ReceiptObjectStore:
    def __init__(
        self,
        stat: ObjectStat | dict[ObjectLocation, ObjectStat],
        events: list[str],
    ) -> None:
        self._stats = stat if isinstance(stat, dict) else {stat.location: stat}
        self._events = events

    def stat(self, location: ObjectLocation) -> ObjectStat:
        self._events.append("stat")
        if location in self._stats:
            return self._stats[location]
        return next(iter(self._stats.values()))


class _ReceiptBindings:
    def __init__(
        self,
        binding: ObjectBinding,
        events: list[str],
        *,
        resolved: ObjectBinding | None = None,
    ) -> None:
        self._binding = binding
        self._resolved = resolved
        self._events = events
        self.calls = 0
        self.resolve_store_ids: list[str | None] = []

    def resolve(self, ref, store_id=None):
        self._events.append("resolve")
        self.resolve_store_ids.append(store_id)
        if self._resolved is not None:
            return self._resolved
        raise FileNotFoundError(ref.key)

    def bind_verified(self, ref, location, expected_revision):
        self.calls += 1
        self._events.append("bind")
        assert expected_revision is None
        return self._binding


class _RetiredReceiptBindings:
    def __init__(self, rebound: ObjectBinding, events: list[str]) -> None:
        self._rebound = rebound
        self._events = events
        self.expected_revisions: list[int | None] = []
        self.resolve_store_ids: list[str | None] = []

    def resolve(self, ref, store_id=None):
        self._events.append("resolve")
        self.resolve_store_ids.append(store_id)
        raise FileNotFoundError(ref.key)

    def bind_verified(self, ref, location, expected_revision):
        self._events.append("bind")
        self.expected_revisions.append(expected_revision)
        if expected_revision is None:
            raise Conflict(
                "ObjectBinding revision or state changed",
                object_key=ref.key,
                store_id=location.store_id,
                expected_revision=None,
                actual_revision=3,
                actual_status="retired",
                actual_backend_generation="retired-generation",
            )
        assert expected_revision == 3
        return self._rebound


class _ReceiptArtifacts:
    def __init__(self, events: list[str], *, existing: object | None = None) -> None:
        self._events = events
        self._existing = existing
        self.calls = 0

    def get(self, artifact_id: str):
        if self._existing is not None and self._existing.artifact_id == artifact_id:
            return self._existing
        return None

    def put(self, artifact):
        self.calls += 1
        self._events.append("artifact")
        return artifact


def _receipt_fixture(
    *,
    receipt_generation: str = "g1",
    stat_generation: str | None = None,
    binding_generation: str = "g1",
):
    payload = b"terminal-resealed-artifact"
    ref = object_ref_for_bytes(payload)
    receipt_location = ObjectLocation(
        store_id="stage-test",
        key=ref.key,
        backend_generation=receipt_generation,
    )
    stat_location = receipt_location.model_copy(
        update={"backend_generation": stat_generation or receipt_generation}
    )
    binding_location = receipt_location.model_copy(
        update={"backend_generation": binding_generation}
    )
    artifact = build_artifact_v2(
        kind="checker_run",
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            tool_version="checker@1",
        ),
        lineage=(),
        payload_hash=ref.sha256,
        object_ref=ref,
        meta={"payload_schema_id": "checker-report@1"},
        created_at="2026-07-16T12:00:00Z",
    )
    receipt = StagedReceipt(slot="domain:0", ref=ref, location=receipt_location)
    stat = ObjectStat(
        ref=ref,
        location=stat_location,
        verified_at="2026-07-16T12:00:00Z",
    )
    binding = ObjectBinding(
        object_ref=ref,
        location=binding_location,
        status="active",
        revision=1,
        verified_at="2026-07-16T12:00:00Z",
    )
    return artifact, receipt, stat, binding


def test_staged_receipt_stat_mismatch_fails_before_binding_or_artifact_write() -> None:
    artifact, receipt, stat, binding = _receipt_fixture(stat_generation="substituted")
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    with pytest.raises(IntegrityViolation, match="stat differs"):
        port.put_staged(artifact, receipt)

    assert events == ["stat"]
    assert bindings.calls == 0
    assert artifacts.calls == 0


def test_staged_receipt_binding_substitution_fails_before_artifact_write() -> None:
    artifact, receipt, stat, binding = _receipt_fixture(binding_generation="substituted")
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    with pytest.raises(IntegrityViolation, match="another staged generation"):
        port.put_staged(artifact, receipt)

    assert events == ["stat", "resolve", "bind"]
    assert bindings.calls == 1
    assert artifacts.calls == 0


def test_existing_artifact_keeps_its_active_binding_when_new_generation_is_staged() -> None:
    artifact, receipt, receipt_stat, binding = _receipt_fixture(
        receipt_generation="g2",
        binding_generation="g1",
    )
    retained_stat = ObjectStat(
        ref=artifact.object_ref,
        location=binding.location,
        verified_at="2026-07-16T12:00:00Z",
    )
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events, resolved=binding)
    artifacts = _ReceiptArtifacts(events, existing=artifact)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(
            {
                receipt.location: receipt_stat,
                binding.location: retained_stat,
            },
            events,
        ),
    )

    retained = port.put_staged(artifact, receipt)

    assert retained == artifact
    assert events == ["stat", "resolve", "stat"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.calls == 0
    assert artifacts.calls == 0


def test_new_artifact_reuses_existing_active_binding_for_same_object_ref() -> None:
    artifact, receipt, receipt_stat, binding = _receipt_fixture(
        receipt_generation="g2",
        binding_generation="g1",
    )
    shared = build_artifact_v2(
        kind="review_report",
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            tool_version="review@1",
        ),
        lineage=(),
        payload_hash=artifact.object_ref.sha256,
        object_ref=artifact.object_ref,
        meta={"payload_schema_id": "review-report@1"},
        created_at="2026-07-16T12:00:00Z",
    )
    retained_stat = ObjectStat(
        ref=artifact.object_ref,
        location=binding.location,
        verified_at="2026-07-16T12:00:00Z",
    )
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events, resolved=binding)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(
            {
                receipt.location: receipt_stat,
                binding.location: retained_stat,
            },
            events,
        ),
    )

    retained = port.put_staged(shared, receipt)

    assert retained == shared
    assert events == ["stat", "resolve", "stat", "artifact"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.calls == 0
    assert artifacts.calls == 1


def test_retired_same_store_binding_is_reactivated_with_exact_revision_cas() -> None:
    artifact, receipt, stat, binding = _receipt_fixture()
    rebound = binding.model_copy(update={"revision": 4})
    events: list[str] = []
    bindings = _RetiredReceiptBindings(rebound, events)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    published = port.put_staged(artifact, receipt)

    assert published == artifact
    assert events == ["stat", "resolve", "bind", "bind", "stat", "artifact"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.expected_revisions == [None, 3]
    assert artifacts.calls == 1
