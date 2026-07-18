"""Blob-first boundary tests for the Task-9 terminal publication engine.

These tests deliberately make the database UnitOfWork phase observable.  A terminal
draft may read authority inside a short UoW, and its fresh commit may write authority
inside another UoW, but every ``put_verified`` must occur between those phases.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from typing import Callable

import pytest
from pydantic import BaseModel

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.apps.worker.publication import (
    WorkerArtifactPort,
    WorkerBlobStore,
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
    FrozenList,
    PreverifiedAbsentArtifactBinding,
    PreverifiedArtifactBinding,
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
    deep_freeze_value,
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
    TerminalAuthorityDrift,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from tests.platform.m4.test_run_fencing import (
    NOW_DT,
    WORKER,
    _AllowSubmissionAuthorization,
    _NoBlobStager,
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


def test_deep_freeze_preserves_list_serialization_and_closes_all_mutators() -> None:
    class ListEnvelope(BaseModel):
        values: list[dict[str, int]]

    sealed = deep_freeze_value(ListEnvelope(values=[{"value": 1}]))
    assert isinstance(sealed, ListEnvelope)
    assert isinstance(sealed.values, FrozenList)
    assert isinstance(sealed.values, list)
    assert sealed.model_dump(mode="json", warnings="error") == {"values": [{"value": 1}]}
    values = sealed.values
    with pytest.raises(TypeError, match="immutable"):
        values[0]["value"] = 2
    mutators = (
        lambda: values.__setitem__(0, {"value": 2}),
        lambda: values.__delitem__(0),
        lambda: values.append({"value": 2}),
        lambda: values.clear(),
        lambda: values.extend(({"value": 2},)),
        lambda: values.insert(0, {"value": 2}),
        lambda: values.pop(),
        lambda: values.remove(values[0]),
        lambda: values.reverse(),
        lambda: values.sort(key=str),
        lambda: values.__iadd__([{"value": 2}]),
        lambda: values.__imul__(2),
        lambda: values.__ior__([{"value": 2}]),
    )
    for mutate in mutators:
        with pytest.raises(TypeError, match="immutable"):
            mutate()

    frozen_tuple = deep_freeze_value(([1],))
    assert isinstance(frozen_tuple, tuple)
    assert isinstance(frozen_tuple[0], FrozenList)


def test_worker_blob_store_bounds_the_exact_prepared_generation_read() -> None:
    declared = b"prepared"
    object_ref = object_ref_for_bytes(declared)
    location = ObjectLocation(
        store_id="prepared-test",
        key=object_ref.key,
        backend_generation="generation:1",
    )
    read_sizes: list[int] = []

    class TrackingStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    class OversizedGenerationStore:
        @staticmethod
        def stat(selected: ObjectLocation) -> ObjectStat:
            assert selected == location
            return ObjectStat(
                ref=object_ref,
                location=location,
                verified_at="2026-07-16T12:00:00Z",
            )

        @staticmethod
        def open(selected: ObjectLocation) -> TrackingStream:
            assert selected == location
            return TrackingStream(declared + b"!")

    with pytest.raises(IntegrityViolation, match="stream size"):
        WorkerBlobStore(OversizedGenerationStore()).read(object_ref, location)

    assert read_sizes == [object_ref.size_bytes + 1]


def test_worker_blob_store_accepts_bounded_binary_short_reads() -> None:
    declared = b"prepared"
    object_ref = object_ref_for_bytes(declared)
    location = ObjectLocation(
        store_id="prepared-test",
        key=object_ref.key,
        backend_generation="generation:short-read",
    )

    class ShortReadStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 2))

    class ShortReadStore:
        @staticmethod
        def stat(selected: ObjectLocation) -> ObjectStat:
            assert selected == location
            return ObjectStat(
                ref=object_ref,
                location=location,
                verified_at="2026-07-16T12:00:00Z",
            )

        @staticmethod
        def open(selected: ObjectLocation) -> ShortReadStream:
            assert selected == location
            return ShortReadStream(declared)

    assert WorkerBlobStore(ShortReadStore()).read(object_ref, location) == declared


def _staging_draft(*, run_id: str, slot: str, payload: bytes) -> TerminalPublicationDraft:
    material = BlobMaterial(
        slot=slot,
        payload=payload,
        expected_ref=object_ref_for_bytes(payload),
    )
    projection = {
        "publication_kind": "run_result",
        "run_id": run_id,
        "attempt_no": 1,
        "occurred_at": "2026-07-16T12:00:00Z",
        "materials": (
            {
                "slot": material.slot,
                "expected_ref": material.expected_ref.model_dump(mode="json"),
            },
        ),
        "operations": (),
        "result": {},
    }
    return TerminalPublicationDraft(
        publication_kind="run_result",
        run_id=run_id,
        attempt_no=1,
        occurred_at="2026-07-16T12:00:00Z",
        projection_digest=canonical_sha256(projection),
        materials=(material,),
        operations=(),
        operation_projection=(),
        result_projection={},
        result={},
    )


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


def test_worker_blob_stager_deduplicates_one_ref_across_terminal_drafts() -> None:
    uow = _TrackingUow(object())
    reads = _ReadScopeTracker()
    objects = _PutVerifiedSpy(uow, reads)
    payload = b"shared-terminal-payload"

    staged = WorkerBlobStager(objects).stage(
        (
            _staging_draft(run_id="run:one", slot="output:one", payload=payload),
            _staging_draft(run_id="run:two", slot="output:two", payload=payload),
        )
    )

    assert objects.calls == [payload]
    assert staged[0].receipts[0].slot == "output:one"
    assert staged[1].receipts[0].slot == "output:two"
    assert staged[0].receipts[0].ref == staged[1].receipts[0].ref
    assert staged[0].receipts[0].location == staged[1].receipts[0].location


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
        planning_subject_digest = canonical_sha256(
            {
                "publication_kind": publication_kind,
                "run_id": run.run_id,  # type: ignore[attr-defined]
                "attempt_no": attempt_no,
                "occurred_at": occurred_at,
            }
        )
        runtime_authority_digest = canonical_sha256(
            {
                "projection_epoch": self.projection_epoch
                + self.projection_epoch_by_kind.get(publication_kind, 0)
            }
        )
        canonical_projection = {
            "publication_kind": publication_kind,
            "run_id": run.run_id,  # type: ignore[attr-defined]
            "attempt_no": attempt_no,
            "occurred_at": occurred_at,
            "planning_subject_digest": planning_subject_digest,
            "runtime_authority_digest": runtime_authority_digest,
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
            planning_subject_digest=planning_subject_digest,
            runtime_authority_digest=runtime_authority_digest,
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

    def _require_current_authority(
        self,
        drafts: tuple[TerminalPublicationDraft, ...],
        expected_kinds: tuple[str, ...],
    ) -> None:
        if tuple(draft.publication_kind for draft in drafts) != expected_kinds:
            raise TerminalAuthorityDrift("fresh terminal selector differs")
        for draft in drafts:
            current = canonical_sha256(
                {
                    "projection_epoch": self.projection_epoch
                    + self.projection_epoch_by_kind.get(draft.publication_kind, 0)
                }
            )
            if draft.runtime_authority_digest != current:
                raise TerminalAuthorityDrift("fresh terminal authority differs")

    def commit_planned_run_result(self, draft, staged, **kwargs):
        del kwargs
        self._require_current_authority((draft,), ("run_result",))
        return self.commit(draft, staged)

    def commit_planned_active_failure_aggregate(self, drafts, staged, **kwargs):
        expected = (
            ("attempt_failure",)
            if kwargs["retry_decision"].decision == "retry"
            else ("attempt_failure", "run_failure")
        )
        self._require_current_authority(drafts, expected)
        return self.commit_many(tuple(zip(drafts, staged, strict=True)))

    def commit_planned_run_failure(self, draft, staged, **kwargs):
        del kwargs
        self._require_current_authority((draft,), ("run_failure",))
        return self.commit(draft, staged)

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


def test_lifecycle_terminal_operation_rejects_missing_staging_authority() -> None:
    harness = _run_harness()
    _start(harness)
    capabilities = RunLifecycleCapabilities(
        runs=harness.repo,
        registry=harness.registry,
        accounting=harness.accounting,
        publication=harness.publication,
    )

    service = RunLifecycleService(
        unit_of_work=harness.unit_of_work,
        bind_capabilities=lambda _transaction: capabilities,
        clock=FrozenUtcClock(NOW_DT),
    )
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="stager is unavailable"):
        service.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )
    assert harness.state == before


@pytest.mark.parametrize(
    ("remove_apply", "message"),
    ((False, "capability is partial"), (True, "capability is required")),
)
def test_lifecycle_terminal_operation_rejects_missing_closure_capabilities(
    remove_apply: bool,
    message: str,
) -> None:
    harness = _run_harness()
    _start(harness)
    publication = harness.publication
    publication.preflight_complete_attempt_success = None  # type: ignore[method-assign]
    if remove_apply:
        publication.apply_preflighted_terminal_closure = None  # type: ignore[method-assign]
    capabilities = RunLifecycleCapabilities(
        runs=harness.repo,
        registry=harness.registry,
        accounting=harness.accounting,
        publication=publication,
    )
    service = RunLifecycleService(
        unit_of_work=harness.unit_of_work,
        bind_capabilities=lambda _transaction: capabilities,
        clock=FrozenUtcClock(NOW_DT + timedelta(seconds=1)),
        planning_scope=harness.unit_of_work.begin,
        bind_planning_capabilities=lambda _transaction: capabilities,
        stage_publications=_NoBlobStager(),
    )
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match=message):
        service.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )
    assert harness.state == before


def test_lifecycle_terminal_operation_requires_bounded_attempt_authority() -> None:
    harness = _run_harness()
    _start(harness)
    harness.repo.get_attempt_write_authority = None  # type: ignore[method-assign]
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="bounded attempt write authority"):
        harness.lifecycle.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )
    assert harness.state == before


def test_command_submission_rejects_missing_terminal_staging_authority() -> None:
    harness = _run_harness()
    _as_queued(harness)
    capabilities = RunCommandCapabilities(
        runs=harness.repo,
        registry=harness.registry,
        admission=harness.accounting,
        publication=harness.publication,
        accounting=harness.accounting,
        submission_authorization=_AllowSubmissionAuthorization(),
    )
    service = RunCommandService(
        unit_of_work=harness.unit_of_work,
        bind_capabilities=lambda _transaction: capabilities,
        clock=FrozenUtcClock(NOW_DT),
    )
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="requires terminal staging authority"):
        service.submit(
            run_id="run:1",
            command=_cancel_command(harness),
            actor=_HUMAN,
        )
    assert harness.state == before


def test_command_submission_requires_bounded_run_authority() -> None:
    harness = _run_harness()
    _as_queued(harness)
    harness.repo.get_run_write_authority = None  # type: ignore[method-assign]
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="bounded Run write authority"):
        harness.commands.submit(
            run_id="run:1",
            command=_cancel_command(harness),
            actor=_HUMAN,
        )
    assert harness.state == before


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
    assert publication.plan_calls == ["run_result"]
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
    assert publication.plan_calls == ["run_failure"]
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
    assert publication.plan_calls == ["run_result"] * 3
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
        self.expected_revisions: list[int | None] = []

    def resolve(self, ref, store_id=None):
        self._events.append("resolve")
        self.resolve_store_ids.append(store_id)
        if self._resolved is not None:
            return self._resolved
        raise FileNotFoundError(ref.key)

    def bind_preverified(self, stat, expected_revision):
        self.calls += 1
        self._events.append("bind")
        self.expected_revisions.append(expected_revision)
        assert stat.ref == self._binding.object_ref
        if expected_revision is not None:
            assert self._resolved is not None
            assert expected_revision == self._resolved.revision
            if stat.location == self._resolved.location:
                return self._resolved
            return self._binding.model_copy(
                update={
                    "location": stat.location,
                    "revision": expected_revision + 1,
                    "verified_at": stat.verified_at,
                }
            )
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

    def bind_preverified(self, stat, expected_revision):
        self._events.append("bind")
        self.expected_revisions.append(expected_revision)
        if expected_revision is None:
            raise Conflict(
                "ObjectBinding revision or state changed",
                object_key=stat.ref.key,
                store_id=stat.location.store_id,
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
    receipt = StagedReceipt(
        slot="domain:0",
        ref=ref,
        location=receipt_location,
        verified_at="2026-07-16T12:00:00Z",
    )
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


def test_staged_receipt_without_preverified_stat_fails_before_database_write() -> None:
    artifact, receipt, stat, binding = _receipt_fixture()
    receipt = StagedReceipt(slot=receipt.slot, ref=receipt.ref, location=receipt.location)
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    with pytest.raises(IntegrityViolation, match="lacks its preverified"):
        port.put_staged(artifact, receipt)

    assert events == []
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

    assert events == ["resolve", "bind"]
    assert bindings.calls == 1
    assert artifacts.calls == 0


def test_existing_artifact_retains_its_preverified_active_generation() -> None:
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

    retained = port.put_staged(
        artifact,
        receipt,
        PreverifiedArtifactBinding(binding=binding, stat=retained_stat),
    )

    assert retained == artifact
    assert events == ["resolve", "bind"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.expected_revisions == [binding.revision]
    assert bindings.calls == 1
    assert artifacts.calls == 0


def test_new_artifact_reuses_its_preverified_active_generation() -> None:
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

    retained = port.put_staged(
        shared,
        receipt,
        PreverifiedArtifactBinding(binding=binding, stat=retained_stat),
    )

    assert retained == shared
    assert events == ["resolve", "bind", "artifact"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.expected_revisions == [binding.revision]
    assert bindings.calls == 1
    assert artifacts.calls == 1


def test_new_artifact_accepts_the_exact_already_active_staged_generation() -> None:
    artifact, receipt, stat, binding = _receipt_fixture()
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events, resolved=binding)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    retained = port.put_staged(artifact, receipt)

    assert retained == artifact
    assert events == ["resolve", "bind", "artifact"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.expected_revisions == [binding.revision]
    assert artifacts.calls == 1


def test_new_artifact_never_remaps_an_unplanned_active_generation() -> None:
    artifact, receipt, stat, binding = _receipt_fixture(
        receipt_generation="g2",
        binding_generation="g1",
    )
    events: list[str] = []
    bindings = _ReceiptBindings(binding, events, resolved=binding)
    artifacts = _ReceiptArtifacts(events)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=_ReceiptObjectStore(stat, events),
    )

    with pytest.raises(TerminalAuthorityDrift, match="active ObjectBinding"):
        port.put_staged(artifact, receipt)

    assert events == ["resolve"]
    assert bindings.calls == 0
    assert artifacts.calls == 0


def test_worker_artifact_port_accepts_bounded_binary_short_reads() -> None:
    artifact, _receipt, _stat, binding = _receipt_fixture()
    payload = b"terminal-resealed-artifact"
    read_sizes: list[int] = []

    class ShortReadStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(min(size, 3))

    class ReadStore:
        @staticmethod
        def open(location: ObjectLocation) -> ShortReadStream:
            assert location == binding.location
            return ShortReadStream(payload)

    port = WorkerArtifactPort(
        artifacts=_ReceiptArtifacts([], existing=artifact),
        object_bindings=_ReceiptBindings(binding, [], resolved=binding),
        object_store=ReadStore(),
    )

    assert port.read_bytes(artifact.artifact_id) == payload
    assert read_sizes[0] == artifact.object_ref.size_bytes + 1
    assert all(size <= artifact.object_ref.size_bytes + 1 for size in read_sizes)


def test_worker_artifact_port_short_reads_still_enforce_the_hard_byte_cap() -> None:
    artifact, _receipt, _stat, binding = _receipt_fixture()
    payload = b"terminal-resealed-artifact"
    returned_sizes: list[int] = []

    class OversizedShortReadStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            chunk = super().read(min(size, 3))
            returned_sizes.append(len(chunk))
            return chunk

    class ReadStore:
        @staticmethod
        def open(location: ObjectLocation) -> OversizedShortReadStream:
            assert location == binding.location
            return OversizedShortReadStream(payload + b"!")

    port = WorkerArtifactPort(
        artifacts=_ReceiptArtifacts([], existing=artifact),
        object_bindings=_ReceiptBindings(binding, [], resolved=binding),
        object_store=ReadStore(),
    )

    with pytest.raises(IntegrityViolation, match="bytes differ"):
        port.read_bytes(artifact.artifact_id)

    assert sum(returned_sizes) == artifact.object_ref.size_bytes + 1


class _SharedGenerationBindings:
    def __init__(self) -> None:
        self.active: ObjectBinding | None = None
        self.expected_revisions: list[int | None] = []

    def resolve(self, ref, store_id=None):
        if (
            self.active is None
            or self.active.object_ref != ref
            or self.active.location.store_id != store_id
        ):
            raise FileNotFoundError(ref.key)
        return self.active

    def bind_preverified(self, stat, expected_revision):
        self.expected_revisions.append(expected_revision)
        if self.active is None:
            assert expected_revision is None
            self.active = ObjectBinding(
                object_ref=stat.ref,
                location=stat.location,
                status="active",
                revision=1,
                verified_at=stat.verified_at,
            )
            return self.active
        assert expected_revision == self.active.revision
        assert stat.ref == self.active.object_ref
        assert stat.location == self.active.location
        return self.active


class _SharedGenerationArtifacts:
    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def get(self, artifact_id: str):
        return self.items.get(artifact_id)

    def put(self, artifact):
        self.items[artifact.artifact_id] = artifact
        return artifact


def test_exact_shared_generation_supports_terminal_prompt_context_and_record_shard() -> None:
    payload = b"one-content-addressed-generation"
    ref = object_ref_for_bytes(payload)
    location = ObjectLocation(
        store_id="stage-test",
        key=ref.key,
        backend_generation="shared-generation",
    )
    bindings = _SharedGenerationBindings()
    artifacts = _SharedGenerationArtifacts()
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=object(),
    )
    kinds_and_schemas = (
        ("checker_run", "checker-report@1"),
        ("source_rendered", "source-rendered@1"),
        ("source_raw", "agent-prompt-context@1"),
        ("cassette_bundle", "cassette-record-shard@1"),
    )

    for index, (kind, payload_schema_id) in enumerate(kinds_and_schemas, start=1):
        artifact = build_artifact_v2(
            kind=kind,
            version_tuple=VersionTuple(tool_version=f"producer@{index}"),
            lineage=(),
            payload_hash=ref.sha256,
            object_ref=ref,
            meta={"payload_schema_id": payload_schema_id},
            created_at="2026-07-16T12:00:00Z",
        )
        retained = port.put_staged(
            artifact,
            StagedReceipt(
                slot=f"shared:{index}",
                ref=ref,
                location=location,
                verified_at="2026-07-16T12:00:00Z",
            ),
        )
        assert retained == artifact

    assert bindings.active is not None
    assert bindings.active.location == location
    assert bindings.expected_revisions == [None, 1, 1, 1]
    assert len(artifacts.items) == len(kinds_and_schemas)


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
    assert events == ["resolve", "bind", "bind", "artifact"]
    assert bindings.resolve_store_ids == [receipt.location.store_id]
    assert bindings.expected_revisions == [None, 3]
    assert artifacts.calls == 1


class _BatchSealBindings:
    def __init__(self) -> None:
        self.write_calls = 0

    @staticmethod
    def resolve_many(refs):
        return {ref.key: None for ref in refs}

    def bind_terminal_preverified_many(self, writes):
        self.write_calls += 1
        return tuple(
            ObjectBinding(
                object_ref=stat.ref,
                location=stat.location,
                status="active",
                revision=1,
                verified_at=stat.verified_at,
            )
            for stat, _expected in writes
        )


class _BatchSealArtifacts:
    def __init__(self) -> None:
        self.write_calls = 0

    @staticmethod
    def get_many(artifact_ids):
        return dict.fromkeys(artifact_ids)

    def put_many(self, artifacts):
        self.write_calls += 1
        return tuple(artifacts)


def test_artifact_batch_rejects_legacy_repositories_without_explicit_test_opt_in() -> None:
    artifact, receipt, _stat, _binding = _receipt_fixture()
    port = WorkerArtifactPort(
        artifacts=_BatchSealArtifacts(),
        object_bindings=_BatchSealBindings(),
        object_store=object(),
    )

    with pytest.raises(IntegrityViolation, match="preflight/apply capability is required"):
        port.preflight_staged_many(
            (
                (
                    artifact,
                    receipt,
                    PreverifiedAbsentArtifactBinding(object_ref=artifact.object_ref),
                ),
            )
        )


def test_artifact_batch_preflight_is_port_bound_and_one_shot_before_writes() -> None:
    artifact, receipt, _stat, _binding = _receipt_fixture()
    bindings = _BatchSealBindings()
    artifacts = _BatchSealArtifacts()
    owner = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=object(),
        allow_legacy_test_repositories=True,
    )
    another_port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=object(),
        allow_legacy_test_repositories=True,
    )
    seal = owner.preflight_staged_many(
        (
            (
                artifact,
                receipt,
                PreverifiedAbsentArtifactBinding(object_ref=artifact.object_ref),
            ),
        )
    )

    assert type(seal).__slots__ == ("__weakref__",)
    for field_name in ("_writes", "_owner", "_transaction_identity", "_consumed"):
        with pytest.raises(AttributeError):
            object.__setattr__(seal, field_name, object())
        assert not hasattr(seal, field_name)
    for unregistered in (replace(seal), copy(seal)):
        with pytest.raises(IntegrityViolation, match="trusted preflight seal"):
            owner.put_preflighted_many(unregistered)
    assert bindings.write_calls == 0
    assert artifacts.write_calls == 0

    with pytest.raises(IntegrityViolation, match="another transaction-bound port"):
        another_port.put_preflighted_many(seal)
    assert bindings.write_calls == 0
    assert artifacts.write_calls == 0

    assert owner.put_preflighted_many(seal) == (artifact,)
    assert bindings.write_calls == 1
    assert artifacts.write_calls == 1
    with pytest.raises(IntegrityViolation, match="already consumed"):
        owner.put_preflighted_many(seal)
    assert bindings.write_calls == 1
    assert artifacts.write_calls == 1


def test_artifact_batch_preflight_rejects_a_new_transaction_on_the_same_port() -> None:
    class TransactionSession:
        def __init__(self) -> None:
            self.current = object()

        @staticmethod
        def get_nested_transaction():
            return None

        def get_transaction(self):
            return self.current

    class DirectBindings(SqlObjectBindingRepository):
        def __init__(self, session: TransactionSession) -> None:
            self._session = session
            self.write_calls = 0

        @staticmethod
        def resolve_many(refs):
            return {ref.key: None for ref in refs}

        def bind_terminal_preverified_many(self, writes):
            self.write_calls += 1
            return tuple(writes)

        preflight_terminal_preverified_many = None
        apply_terminal_preverified_many = None

    class DirectArtifacts(SqlArtifactRepository):
        def __init__(self, session: TransactionSession) -> None:
            self._session = session
            self.write_calls = 0

        @staticmethod
        def get_many(artifact_ids):
            return dict.fromkeys(artifact_ids)

        def put_many(self, artifacts):
            self.write_calls += 1
            return tuple(artifacts)

        preflight_put_many = None
        put_preflighted_many = None

    artifact, receipt, _stat, _binding = _receipt_fixture()
    session = TransactionSession()
    bindings = DirectBindings(session)
    artifacts = DirectArtifacts(session)
    port = WorkerArtifactPort(
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=object(),
        allow_legacy_test_repositories=True,
    )
    seal = port.preflight_staged_many(
        (
            (
                artifact,
                receipt,
                PreverifiedAbsentArtifactBinding(object_ref=artifact.object_ref),
            ),
        )
    )
    session.current = object()

    with pytest.raises(IntegrityViolation, match="another transaction instance"):
        port.put_preflighted_many(seal)
    assert bindings.write_calls == 0
    assert artifacts.write_calls == 0
