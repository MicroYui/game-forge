from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    FailureClassifierRefV1,
    GraphSelectionV1,
    PlaytestProvideInputPayloadV1,
    RetryPolicyRefV1,
    RunCommandRecordV1,
    RunCommandV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunPayloadEnvelope,
    RunQueuedDataV1,
    RunRecord,
    canonical_payload_hash,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    FindingHeadRow,
    FindingRevisionRow,
    RunAttemptRow,
    RunEventRow,
    RunIntermediateArtifactLinkRow,
    RunLeaseRow,
    RunRow,
)
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-07-14T09:00:00Z"
LEASE_EXPIRES = "2026-07-14T09:01:00Z"


def _payload(*, input_artifact_id: str = "artifact:input") -> RunPayloadEnvelope:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=input_artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=(),
        defect_classes=(),
    )
    return RunPayloadEnvelope(
        payload_schema_version="checker-run@1",
        input_artifact_ids=(input_artifact_id,),
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:1", tool_version="checker@1"),
        execution_version_plan=None,
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=HASH_A,
        resolved_profiles=(),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        llm_execution_mode="not_applicable",
        params=params,
    )


def _run(
    run_id: str = "run:1",
    *,
    idempotency_key: str = "request:1",
    request_hash: str = HASH_A,
    payload: RunPayloadEnvelope | None = None,
    created_at: str = NOW,
) -> RunRecord:
    selected_payload = payload or _payload()
    return RunRecord(
        run_id=run_id,
        kind=RunKindRef(kind="checker.run", version=1),
        status="queued",
        revision=1,
        idempotency_scope="principal:human:a",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        payload=selected_payload,
        payload_hash=canonical_payload_hash(selected_payload),
        run_kind_definition_digest=HASH_A,
        outcome_policy_set_digest=HASH_B,
        failure_classifier=FailureClassifierRefV1(
            classifier_version=1,
            classifier_digest=HASH_A,
        ),
        initiated_by=AuditActor(principal_id="human:a", principal_kind="human"),
        queue_deadline_utc="2026-07-14T09:10:00Z",
        attempt_timeout_ns=1_000_000_000,
        overall_deadline_utc="2026-07-14T10:00:00Z",
        next_attempt_no=1,
        next_fencing_token=1,
        next_event_seq=2,
        budget_set_snapshot_id="budget-set:1",
        run_budget_hold_group_id=f"hold:{run_id}",
        retry_policy=RetryPolicyRefV1(
            retry_policy_id="default",
            retry_policy_version=1,
            retry_policy_digest=HASH_B,
        ),
        max_attempts=3,
        created_at=created_at,
        updated_at=created_at,
    )


def _queued_event(run: RunRecord) -> RunEvent:
    return RunEvent(
        run_id=run.run_id,
        seq=1,
        event_type="run.queued",
        occurred_at=run.created_at,
        data_schema_version="run-queued@1",
        data=RunQueuedDataV1(
            run_kind=run.kind,
            queue_deadline_utc=run.queue_deadline_utc,
            overall_deadline_utc=run.overall_deadline_utc,
        ),
    )


def _capabilities(session: Session) -> TransactionCapabilities:
    repository = SqlRunRepository(session)
    return TransactionCapabilities(
        refs=repository,
        audit=repository,
        approvals=repository,
        lineage=repository,
        object_bindings=repository,
        runs=repository,
        cost=repository,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    migrations_api.upgrade(url, "head")
    selected = get_engine(url)
    yield selected
    selected.dispose()


def _create(engine: Engine, run: RunRecord) -> RunRecord:
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        return transaction.runs.create_queued(run, _queued_event(run))


def test_create_queued_persists_immutable_run_and_initial_event_without_fake_attempt(
    engine: Engine,
) -> None:
    expected = _run()

    assert _create(engine, expected) == expected

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(expected.run_id) == expected
        assert repository.get_event(expected.run_id, 1) == _queued_event(expected)
        assert repository.list_events(expected.run_id) == (_queued_event(expected),)
        assert repository.get_attempt(expected.run_id, 1) is None
        assert repository.get_current_lease(expected.run_id) is None
        assert session.scalar(select(func.count()).select_from(RunAttemptRow)) == 0
        assert session.scalar(select(func.count()).select_from(RunLeaseRow)) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "leased"),
        ("revision", 2),
        ("current_attempt_no", 1),
        ("next_attempt_no", 2),
        ("next_fencing_token", 2),
        ("next_event_seq", 1),
    ],
)
def test_create_queued_rejects_preallocated_or_noninitial_run_heads(
    engine: Engine,
    field: str,
    value: object,
) -> None:
    candidate = _run().model_copy(update={field: value})

    with pytest.raises(IntegrityViolation):
        _create(engine, candidate)


def test_create_idempotency_replays_original_resource_and_conflicts_on_new_hash(
    engine: Engine,
) -> None:
    original = _run()
    assert _create(engine, original) == original
    with Session(engine) as session:
        assert SqlRunRepository(session).get_by_idempotency(
            scope=original.idempotency_scope,
            key=original.idempotency_key,
        ) == original

    replay = _run(
        "run:retry-generated-id",
        idempotency_key=original.idempotency_key,
        request_hash=original.request_hash,
        created_at="2026-07-14T09:00:01Z",
    )
    assert _create(engine, replay) == original

    conflicting = replay.model_copy(update={"request_hash": HASH_C})
    with pytest.raises(Conflict, match="idempotency"):
        _create(engine, conflicting)

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RunRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunEventRow)) == 1


def test_same_run_id_with_different_immutable_payload_is_never_overwritten(
    engine: Engine,
) -> None:
    original = _run()
    _create(engine, original)
    changed = _run(
        original.run_id,
        idempotency_key="request:other",
        request_hash=HASH_C,
        payload=_payload(input_artifact_id="artifact:changed"),
    )

    with pytest.raises(IntegrityViolation, match="immutable"):
        _create(engine, changed)

    with Session(engine) as session:
        assert SqlRunRepository(session).get(original.run_id) == original


def test_run_reader_fails_closed_when_payload_or_binding_is_changed_in_place(
    engine: Engine,
) -> None:
    expected = _run()
    _create(engine, expected)
    corrupted_payload = expected.payload.model_dump(mode="json")
    corrupted_payload["budget_set_snapshot_id"] = "budget-set:corrupt"
    with engine.begin() as connection:
        connection.execute(
            update(RunRow)
            .where(RunRow.run_id == expected.run_id)
            .values(payload=corrupted_payload)
        )

    with Session(engine) as session, pytest.raises(IntegrityViolation, match="stored Run"):
        SqlRunRepository(session).get(expected.run_id)


def test_claim_allocates_attempt_fencing_event_and_lease_from_persisted_heads(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        candidate = transaction.runs.get_claim_candidate(NOW)
        assert candidate == queued
        claim = transaction.runs.claim(
            queued.run_id,
            expected_revision=queued.revision,
            worker_principal_id="service:worker:1",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit-group:1",
            trace_id="trace:1",
        )

    assert claim.run.status == "leased"
    assert claim.run.revision == 2
    assert claim.run.current_attempt_no == 1
    assert claim.run.next_attempt_no == 2
    assert claim.run.next_fencing_token == 2
    assert claim.run.next_event_seq == 3
    assert claim.run.concurrency_permit_group_id == "permit-group:1"
    assert claim.attempt.attempt_no == 1
    assert claim.attempt.fencing_token == 1
    assert claim.attempt.next_call_ordinal == 1
    assert claim.lease.lease_id == "lease:1"
    assert claim.lease.fencing_token == 1
    assert claim.event.seq == 2
    assert claim.event.attempt_no == 1

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(queued.run_id) == claim.run
        assert repository.get_attempt(queued.run_id, 1) == claim.attempt
        assert repository.get_current_lease(queued.run_id) == claim.lease
        assert repository.get_event(queued.run_id, 2) == claim.event
        assert session.scalar(select(func.count()).select_from(RunAttemptRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunLeaseRow)) == 1


def test_two_connections_cannot_claim_the_same_run_or_duplicate_heads(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)

    def claim_from_connection(worker: str) -> str:
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            candidate = transaction.runs.get_claim_candidate(NOW)
            if candidate is None:
                return "none"
            result = transaction.runs.claim(
                candidate.run_id,
                expected_revision=candidate.revision,
                worker_principal_id=worker,
                lease_id=f"lease:{worker}",
                acquired_at=NOW,
                expires_at=LEASE_EXPIRES,
                permit_group_id=f"permit:{worker}",
            )
            return result.lease.lease_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim_from_connection, ("worker:a", "worker:b")))

    assert sorted("none" if value == "none" else "claimed" for value in outcomes) == [
        "claimed",
        "none",
    ]
    with Session(engine) as session:
        run = SqlRunRepository(session).get(queued.run_id)
        assert run is not None
        assert run.next_attempt_no == 2
        assert run.next_fencing_token == 2
        assert run.next_event_seq == 3
        assert session.scalar(select(func.count()).select_from(RunAttemptRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunLeaseRow)) == 1


def test_task13_claim_skips_and_rejects_retry_wait_until_task14_guards_exist(
    engine: Engine,
) -> None:
    retry_wait = _run(
        "run:retry-wait",
        idempotency_key="request:retry-wait",
        created_at="2026-07-14T08:00:00Z",
    )
    _create(engine, retry_wait)
    with engine.begin() as connection:
        connection.execute(
            update(RunRow)
            .where(RunRow.run_id == retry_wait.run_id)
            .values(
                status="retry_wait",
                retry_not_before_utc="2026-07-14T08:30:00Z",
            )
        )
    queued = _run(
        "run:queued-after-retry",
        idempotency_key="request:queued-after-retry",
        created_at="2026-07-14T09:00:01Z",
    )
    _create(engine, queued)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.get_claim_candidate(NOW) == queued
        with pytest.raises(InvalidStateTransition, match="only a queued Run"):
            transaction.runs.claim(
                retry_wait.run_id,
                expected_revision=1,
                worker_principal_id="service:worker",
                lease_id="lease:retry",
                acquired_at=NOW,
                expires_at=LEASE_EXPIRES,
                permit_group_id="permit:retry",
            )


def test_active_run_must_point_to_the_attempt_and_fencing_predecessor_heads(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        claim = transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    newer_attempt = claim.attempt.model_copy(
        update={"attempt_no": 2, "fencing_token": 2}
    )
    with Session(engine) as session:
        session.add(RunAttemptRow(**newer_attempt.model_dump(mode="json")))
        session.execute(
            update(RunRow)
            .where(RunRow.run_id == queued.run_id)
            .values(next_attempt_no=3, next_fencing_token=3)
        )
        session.commit()

    with Session(engine) as session, pytest.raises(
        IntegrityViolation,
        match="consumed attempt head",
    ):
        SqlRunRepository(session).get(queued.run_id)


def test_run_event_head_rejects_a_deleted_historical_sequence(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    with engine.begin() as connection:
        connection.execute(
            delete(RunEventRow).where(
                RunEventRow.run_id == queued.run_id,
                RunEventRow.seq == 1,
            )
        )

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        with pytest.raises(IntegrityViolation, match="event head"):
            repository.get(queued.run_id)
        with pytest.raises(IntegrityViolation, match="event head"):
            repository.list_events(queued.run_id)


def test_prompt_link_allocates_call_ordinal_atomically_without_preallocating_retry(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    with Session(engine) as session:
        session.add_all(
            [
                _artifact(
                    "artifact:prompt:1",
                    payload_hash=HASH_A,
                    kind="source_rendered",
                ),
                _artifact(
                    "artifact:prompt:2",
                    payload_hash=HASH_B,
                    kind="source_rendered",
                ),
            ]
        )
        session.commit()
    first = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id="artifact:prompt:1",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    second = first.model_copy(
        update={
            "call_ordinal": 2,
            "artifact_id": "artifact:prompt:2",
            "request_hash": HASH_B,
        }
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.put_intermediate_link(first) == first
        assert transaction.runs.put_intermediate_link(first) == first
        assert transaction.runs.put_intermediate_link(second) == second

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        attempt = repository.get_attempt(queued.run_id, 1)
        run = repository.get(queued.run_id)
        assert attempt is not None and attempt.next_call_ordinal == 3
        assert run is not None and run.next_attempt_no == 2
        assert session.scalar(select(func.count()).select_from(RunAttemptRow)) == 1


def test_prompt_link_and_attempt_head_roll_back_together(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    with Session(engine) as session:
        session.add(
            _artifact(
                "artifact:prompt:rollback",
                payload_hash=HASH_A,
                kind="source_rendered",
            )
        )
        session.commit()
    link = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id="artifact:prompt:rollback",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )

    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.put_intermediate_link(link)
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        attempt = repository.get_attempt(queued.run_id, 1)
        assert attempt is not None and attempt.next_call_ordinal == 1
        assert repository.get_intermediate_link(queued.run_id, 1, 1) is None


def test_attempt_call_head_rejects_a_deleted_historical_prompt_link(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    with Session(engine) as session:
        session.add_all(
            [
                _artifact(
                    "artifact:prompt:history:1",
                    payload_hash=HASH_A,
                    kind="source_rendered",
                ),
                _artifact(
                    "artifact:prompt:history:2",
                    payload_hash=HASH_B,
                    kind="source_rendered",
                ),
            ]
        )
        session.commit()
    first = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        artifact_id="artifact:prompt:history:1",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    second = first.model_copy(
        update={
            "call_ordinal": 2,
            "artifact_id": "artifact:prompt:history:2",
            "request_hash": HASH_B,
        }
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.put_intermediate_link(first)
        transaction.runs.put_intermediate_link(second)
    with engine.begin() as connection:
        connection.execute(
            delete(RunIntermediateArtifactLinkRow).where(
                RunIntermediateArtifactLinkRow.run_id == queued.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == 1,
                RunIntermediateArtifactLinkRow.call_ordinal == 1,
            )
        )

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        with pytest.raises(IntegrityViolation, match="call-ordinal head"):
            repository.get_attempt(queued.run_id, 1)
        with pytest.raises(IntegrityViolation, match="call-ordinal head"):
            repository.get_intermediate_link(queued.run_id, 1, 2)


def _artifact(
    artifact_id: str,
    *,
    payload_hash: str,
    kind: str = "run_result",
) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id,
        lineage_schema_version="lineage@1",
        kind=kind,
        version_tuple={},
        lineage=[],
        payload_hash=payload_hash,
        created_at=NOW,
        meta={},
        object_ref=None,
    )


def _claim(engine: Engine) -> tuple[RunRecord, int]:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        claim = transaction.runs.claim(
            queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    return claim.run, claim.attempt.next_call_ordinal


def test_intermediate_finding_and_command_records_are_insert_or_exact_compare(
    engine: Engine,
) -> None:
    run, ordinal = _claim(engine)
    finding_revision = FindingRevisionV1(
        finding_id="finding:1",
        revision=1,
        created_at=NOW,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="graph-checker@1",
            producer_run_id=run.run_id,
            oracle_type="deterministic",
            defect_class="dangling_reference",
            severity="major",
            snapshot_id="snapshot:1",
            entities=["quest:1"],
            relations=[],
            evidence={"missing_entity_id": "item:missing"},
            minimal_repro={"entity_id": "quest:1"},
            status="confirmed",
            confidence=1.0,
            message="missing item",
        ),
    )
    finding_digest = finding_revision_digest(finding_revision)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact(
                    "artifact:prompt",
                    payload_hash=HASH_A,
                    kind="source_rendered",
                ),
                _artifact("artifact:evidence", payload_hash=HASH_B),
            ]
        )
        session.add(
            FindingRevisionRow(
                finding_id="finding:1",
                revision=1,
                revision_schema_version=finding_revision.revision_schema_version,
                supersedes_revision=finding_revision.supersedes_revision,
                created_at=finding_revision.created_at,
                payload=finding_revision.payload.model_dump(mode="json"),
                finding_digest=finding_digest,
            )
        )
        session.flush()
        session.add(
            FindingHeadRow(
                finding_id="finding:1",
                current_revision=1,
                current_digest=finding_digest,
                row_revision=1,
                updated_at=NOW,
            )
        )
        session.commit()

    intermediate = RunIntermediateArtifactLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        call_ordinal=ordinal,
        artifact_id="artifact:prompt",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    finding = RunFindingLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        ordinal=1,
        finding_id="finding:1",
        finding_revision=1,
        finding_digest=finding_digest,
        evidence_artifact_id="artifact:evidence",
    )
    command = RunCommandV1(
        command_id="command:1",
        client_id="browser:1",
        client_seq=1,
        idempotency_key="input:1",
        expected_run_revision=run.revision,
        type="provide_input",
        payload_schema_id="playtest-provide-input@1",
        payload=PlaytestProvideInputPayloadV1(
            interaction_id="interaction:1",
            expected_state_hash=HASH_A,
            choice_id="choice:a",
        ),
    )
    command_record = RunCommandRecordV1(
        run_id=run.run_id,
        command=command,
        request_hash=canonical_payload_hash(command),
        actor=AuditActor(principal_id="human:a", principal_kind="human"),
        status="pending",
        revision=1,
        created_at=NOW,
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.put_intermediate_link(intermediate) == intermediate
        assert transaction.runs.put_intermediate_link(intermediate) == intermediate
        assert transaction.runs.put_finding_link(finding) == finding
        assert transaction.runs.put_finding_link(finding) == finding
        assert transaction.runs.put_command(command_record) == command_record
        assert transaction.runs.put_command(command_record) == command_record

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get_intermediate_link(run.run_id, 1, ordinal) == intermediate
        assert repository.get_finding_link(run.run_id, 1, 1) == finding
        assert repository.get_command(run.run_id, command.command_id) == command_record

    changed_link = intermediate.model_copy(update={"request_hash": HASH_B})
    changed_finding = finding.model_copy(update={"finding_digest": HASH_A})
    changed_command = command_record.model_copy(update={"actor": AuditActor(
        principal_id="human:b", principal_kind="human"
    )})
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="immutable"):
            transaction.runs.put_intermediate_link(changed_link)
        with pytest.raises(IntegrityViolation):
            transaction.runs.put_finding_link(changed_finding)
        with pytest.raises(IntegrityViolation, match="immutable"):
            transaction.runs.put_command(changed_command)
