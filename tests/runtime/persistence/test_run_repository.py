from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, delete, event, func, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.cassette_import import (
    LegacyImportRoutingDecisionV1,
    LegacyImportVerificationPolicyRefV1,
)
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.jobs import (
    AttemptProgressDataV1,
    CancelRequestedDataV1,
    CancelRunPayloadV1,
    CheckerRunPayloadV1,
    CommandAcceptedDataV1,
    CommandOutcomeDataV1,
    ExecutionVersionPlanV1,
    FailureClassifierRefV1,
    GraphSelectionV1,
    LeaseExpiredDataV1,
    MAX_COLLECTION_ITEMS,
    PlaytestProvideInputPayloadV1,
    PlannedAgentNodeVersionV1,
    RetryDecisionV1,
    RetryScheduledDataV1,
    RetryPolicyRefV1,
    RunCommandRecordV1,
    RunCommandV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunPayloadEnvelope,
    RunQueuedDataV1,
    RunRecord,
    RunSucceededDataV1,
    RunTerminatedDataV1,
    RunToolIntermediateLinkV1,
    canonical_payload_hash,
    execution_version_plan_digest,
)
from gameforge.contracts.identity import DomainScope
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.contracts.lineage import AuditActor, VersionTuple
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    RoutingPolicyV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    FindingHeadRow,
    FindingRevisionRow,
    RunAttemptRow,
    RunCommandRow,
    RunEventRow,
    RunFindingLinkRow,
    RunIntermediateArtifactLinkRow,
    RunLeaseRow,
    LegacyImportRoutingDecisionRow,
    RunModelResponseConsumptionRow,
    RunModelRouteLinkRow,
    RunRow,
    RunToolIntermediateLinkRow,
    RoutingDecisionRow,
    UsageEntryRow,
)
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.runs import RunAttemptStart, RunClaim, SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.runtime.cost.test_repository import (
    REQUEST_HASH as COST_REQUEST_HASH,
    _budget,
    _budget_set,
    _call,
    _catalog_policy_decision,
    _hold,
    _usage,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-07-14T09:00:00Z"
LEASE_EXPIRES = "2026-07-14T09:01:00Z"
STARTED = "2026-07-14T09:00:00.100000Z"
HEARTBEAT = "2026-07-14T09:00:00.200000Z"
PROGRESSED = "2026-07-14T09:00:00.300000Z"
ENDED = "2026-07-14T09:00:00.400000Z"
ATTEMPT_DEADLINE = "2026-07-14T09:00:01.100000Z"
RENEWED_EXPIRES = "2026-07-14T09:00:00.900000Z"


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


def _record_payload() -> RunPayloadEnvelope:
    node = PlannedAgentNodeVersionV1(
        agent_node_id="checker",
        prompt_version="checker@1",
        tool_version="checker@1",
        allowed_model_snapshots=("model:a",),
    )
    plan_fields = {
        "agent_graph_version": "graph@1",
        "nodes": (node,),
        "model_catalog_version": 1,
        "model_catalog_digest": HASH_A,
        "routing_policy_version": 1,
        "routing_policy_digest": HASH_B,
    }
    plan = ExecutionVersionPlanV1(
        **plan_fields,
        plan_digest=execution_version_plan_digest(plan_fields),
    )
    wire = _payload().model_dump(mode="python")
    wire.update(
        execution_version_plan=plan,
        llm_execution_mode="record",
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:1",
            prompt_version="checker@1",
            model_snapshot="model:a",
            agent_graph_version="graph@1",
            tool_version="checker@1",
        ),
    )
    return RunPayloadEnvelope.model_validate(wire)


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


def _model_call_capabilities(session: Session) -> TransactionCapabilities:
    runs = SqlRunRepository(session)
    cost = SqlCostLedger(session)
    return TransactionCapabilities(
        refs=runs,
        audit=runs,
        approvals=runs,
        lineage=runs,
        object_bindings=runs,
        runs=runs,
        cost=cost,
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
    expected = _run().model_copy(
        update={"resource_domain_scope": DomainScope(domain_ids=("aureus",))}
    )

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

    with pytest.raises(ValidationError, match="resource_domain_scope"):
        RunRecord.model_validate(
            {**_run("run:all-scope").model_dump(mode="python"), "resource_domain_scope": "all"}
        )


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
        assert (
            SqlRunRepository(session).get_by_idempotency(
                scope=original.idempotency_scope,
                key=original.idempotency_key,
            )
            == original
        )

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

    conflicting_scope = replay.model_copy(
        update={"resource_domain_scope": DomainScope(domain_ids=("aureus",))}
    )
    with pytest.raises(Conflict, match="resource domain scope"):
        _create(engine, conflicting_scope)

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
            update(RunRow).where(RunRow.run_id == expected.run_id).values(payload=corrupted_payload)
        )

    with Session(engine) as session, pytest.raises(IntegrityViolation, match="stored Run"):
        SqlRunRepository(session).get(expected.run_id)


def test_claim_allocates_attempt_fencing_event_and_lease_from_persisted_heads(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        candidate = transaction.runs.get_claim_candidate(now_utc=NOW)
        assert candidate == queued
        claim = transaction.runs.claim(
            run_id=queued.run_id,
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


def test_claim_candidate_excludes_a_run_with_persisted_cancel_intent(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with engine.begin() as connection:
        connection.execute(
            update(RunRow)
            .where(RunRow.run_id == queued.run_id)
            .values(
                cancel_requested_at=NOW,
                cancel_requested_by={
                    "principal_id": "human:a",
                    "principal_kind": "human",
                },
            )
        )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.get_claim_candidate(now_utc=NOW) is None


def test_claim_candidate_cursor_rotates_and_wraps_without_becoming_queue_authority(
    engine: Engine,
) -> None:
    runs = tuple(
        _run(
            f"run:{index}",
            idempotency_key=f"request:{index}",
            created_at=f"2026-07-14T09:00:0{index}Z",
        )
        for index in range(4)
    )
    for run in runs:
        _create(engine, run)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.list_claim_candidates(
            now_utc="2026-07-14T09:00:04Z",
            limit=3,
            after_created_at=runs[1].created_at,
            after_run_id=runs[1].run_id,
        ) == (runs[2], runs[3], runs[0])

        # A cursor is only an ordering hint and need not identify a retained row.
        assert (
            transaction.runs.list_claim_candidates(
                now_utc="2026-07-14T09:00:04Z",
                limit=2,
                after_created_at="2026-07-14T09:00:09Z",
                after_run_id="run:already-gone",
            )
            == runs[:2]
        )

        with pytest.raises(IntegrityViolation, match="cursor"):
            transaction.runs.list_claim_candidates(
                now_utc="2026-07-14T09:00:04Z",
                limit=2,
                after_created_at=runs[1].created_at,
            )


def test_two_connections_cannot_claim_the_same_run_or_duplicate_heads(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)

    def claim_from_connection(worker: str) -> str:
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            candidate = transaction.runs.get_claim_candidate(now_utc=NOW)
            if candidate is None:
                return "none"
            result = transaction.runs.claim(
                run_id=candidate.run_id,
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


def test_task14_claim_selects_a_due_retry_wait_before_a_later_queued_run(
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
        candidate = transaction.runs.get_claim_candidate(now_utc=NOW)
        assert candidate is not None
        assert candidate.run_id == retry_wait.run_id
        assert candidate.status == "retry_wait"
        claimed = transaction.runs.claim(
            run_id=retry_wait.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:retry",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:retry",
        )
        assert claimed.run.status == "leased"
        assert claimed.run.retry_not_before_utc is None


def test_active_run_must_point_to_the_attempt_and_fencing_predecessor_heads(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        claim = transaction.runs.claim(
            run_id=queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    newer_attempt = claim.attempt.model_copy(update={"attempt_no": 2, "fencing_token": 2})
    with Session(engine) as session:
        session.add(RunAttemptRow(**newer_attempt.model_dump(mode="json")))
        session.execute(
            update(RunRow)
            .where(RunRow.run_id == queued.run_id)
            .values(next_attempt_no=3, next_fencing_token=3)
        )
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(
            IntegrityViolation,
            match="consumed attempt head",
        ),
    ):
        SqlRunRepository(session).get(queued.run_id)


def test_run_event_head_rejects_a_deleted_historical_sequence(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
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
            run_id=queued.run_id,
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


def test_tool_context_link_is_exact_idempotent_and_does_not_consume_call_head(
    engine: Engine,
) -> None:
    queued = _run(payload=_record_payload())
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    artifact_id = "artifact:context:1"
    artifact = _artifact(
        artifact_id,
        payload_hash=HASH_A,
        kind="source_raw",
    )
    artifact.meta = {
        "payload_schema_id": "agent-prompt-context@1",
        "producer_run_id": queued.run_id,
        "producer_attempt_no": 1,
        "target_call_ordinal": 1,
        "agent_node_id": "checker",
        "prompt_version": "checker@1",
    }
    with Session(engine) as session:
        session.add(artifact)
        session.commit()
    link = RunToolIntermediateLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        target_call_ordinal=1,
        artifact_id=artifact_id,
        agent_node_id="checker",
        prompt_version="checker@1",
        payload_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.put_tool_intermediate_link(link) == link
        assert transaction.runs.put_tool_intermediate_link(link) == link
        attempt = transaction.runs.get_attempt(queued.run_id, 1)
        assert attempt is not None and attempt.next_call_ordinal == 1

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="differs from retained"):
            transaction.runs.put_tool_intermediate_link(
                link.model_copy(update={"payload_hash": HASH_B})
            )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RunToolIntermediateLinkRow)) == 1


def test_tool_context_list_is_bounded_and_forged_artifact_has_no_side_effect(
    engine: Engine,
) -> None:
    queued = _run(payload=_record_payload())
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    artifact_id = "artifact:forged-context"
    forged = _artifact(artifact_id, payload_hash=HASH_A, kind="source_raw")
    with Session(engine) as session:
        session.add(forged)
        session.commit()
    forged_link = RunToolIntermediateLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        target_call_ordinal=1,
        artifact_id=artifact_id,
        agent_node_id="checker",
        prompt_version="checker@1",
        payload_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="exact Agent prompt-context"):
            transaction.runs.put_tool_intermediate_link(forged_link)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(RunToolIntermediateLinkRow)) == 0


def test_prompt_link_and_attempt_head_roll_back_together(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
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


def test_fallback_prompt_routes_are_contiguous_and_do_not_advance_call_head(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
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
                _artifact("artifact:prompt:route:1", payload_hash=HASH_A, kind="source_rendered"),
                _artifact("artifact:prompt:route:2", payload_hash=HASH_B, kind="source_rendered"),
                _artifact("artifact:prompt:route:3", payload_hash=HASH_A, kind="source_rendered"),
            ]
        )
        session.commit()
    first = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id="artifact:prompt:route:1",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    second_route = first.model_copy(
        update={
            "route_ordinal": 2,
            "artifact_id": "artifact:prompt:route:2",
            "request_hash": HASH_B,
        }
    )
    skipped_route = second_route.model_copy(
        update={"route_ordinal": 3, "artifact_id": "artifact:prompt:route:3"}
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.put_intermediate_link(first)
        with pytest.raises(Conflict, match="contiguous"):
            transaction.runs.put_intermediate_link(skipped_route)
        assert transaction.runs.put_intermediate_link(second_route) == second_route
        assert transaction.runs.put_intermediate_link(second_route) == second_route

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        attempt = repository.get_attempt(queued.run_id, 1)
        assert attempt is not None and attempt.next_call_ordinal == 2
        assert repository.list_prompt_render_links(queued.run_id, attempt_no=1) == (
            first,
            second_route,
        )
        assert repository.get_intermediate_link(queued.run_id, 1, 1, 2) == second_route


def test_concurrent_duplicate_fallback_route_is_one_idempotent_row(engine: Engine) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
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
                _artifact("artifact:prompt:race:1", payload_hash=HASH_A, kind="source_rendered"),
                _artifact("artifact:prompt:race:2", payload_hash=HASH_B, kind="source_rendered"),
            ]
        )
        session.commit()
    first = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id="artifact:prompt:race:1",
        role="prompt_rendered",
        request_hash=HASH_A,
        fencing_token=1,
        published_at=NOW,
    )
    fallback = first.model_copy(
        update={
            "route_ordinal": 2,
            "artifact_id": "artifact:prompt:race:2",
            "request_hash": HASH_B,
        }
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.put_intermediate_link(first)

    def publish() -> RunIntermediateArtifactLinkV1:
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            return transaction.runs.put_intermediate_link(fallback)

    with ThreadPoolExecutor(max_workers=2) as pool:
        retained = tuple(pool.map(lambda _: publish(), range(2)))

    assert retained == (fallback, fallback)
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunIntermediateArtifactLinkRow)
                .where(
                    RunIntermediateArtifactLinkRow.run_id == queued.run_id,
                    RunIntermediateArtifactLinkRow.attempt_no == 1,
                    RunIntermediateArtifactLinkRow.call_ordinal == 1,
                    RunIntermediateArtifactLinkRow.route_ordinal == 2,
                )
            )
            == 1
        )


def test_model_route_and_response_consumption_close_exact_authorities(
    engine: Engine,
) -> None:
    budget = _budget()
    budget_set = _budget_set(budget).model_copy(update={"budget_set_snapshot_id": "budget-set:1"})
    hold, hold_reservation = _hold(budget)
    hold = hold.model_copy(update={"budget_set_snapshot_id": budget_set.budget_set_snapshot_id})
    call, call_reservation = _call(budget, hold)
    catalog, policy, original_decision = _catalog_policy_decision()
    decision = type(original_decision).create(
        **original_decision.model_dump(
            exclude={"decision_schema_version", "decision_id", "budget_set_snapshot_id"}
        ),
        budget_set_snapshot_id=budget_set.budget_set_snapshot_id,
    )
    usage = _usage(
        call,
        call_reservation,
        routing_decision_id=decision.decision_id,
    )
    route_plan_fields = {
        "agent_graph_version": "graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="checker",
                prompt_version="checker@1",
                tool_version="checker@1",
                allowed_model_snapshots=(decision.model_snapshot,),
            ),
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    route_payload_wire = _record_payload().model_dump(mode="python")
    route_payload_wire.update(
        execution_version_plan=ExecutionVersionPlanV1(
            **route_plan_fields,
            plan_digest=execution_version_plan_digest(route_plan_fields),
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:1",
            prompt_version="checker@1",
            model_snapshot=decision.model_snapshot,
            agent_graph_version="graph@1",
            tool_version="checker@1",
        ),
    )
    queued = _run(
        "run-1",
        payload=RunPayloadEnvelope.model_validate(route_payload_wire),
    )
    _create(engine, queued)
    _start_result(engine, queued)
    with Session(engine) as session:
        shard_artifact = _artifact(
            "artifact:cassette:record-shard:1",
            payload_hash=HASH_C,
            kind="cassette_bundle",
        )
        shard_artifact.lineage = ["artifact:prompt:model-route:1"]
        shard_artifact.meta = {"payload_schema_id": "cassette-record-shard@1"}
        session.add_all(
            [
                _artifact(
                    "artifact:prompt:model-route:1",
                    payload_hash=HASH_A,
                    kind="source_rendered",
                ),
                _artifact(
                    "artifact:prompt:model-route:2",
                    payload_hash=HASH_B,
                    kind="source_rendered",
                ),
                shard_artifact,
            ]
        )
        session.commit()

    bare_request_hash = COST_REQUEST_HASH.removeprefix("sha256:")
    prompt = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id="artifact:prompt:model-route:1",
        role="prompt_rendered",
        request_hash=bare_request_hash,
        fencing_token=1,
        published_at=NOW,
    )
    route = RunModelRouteLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        prompt_artifact_id=prompt.artifact_id,
        request_hash=prompt.request_hash,
        routing_decision_kind="native",
        routing_decision_id=decision.decision_id,
        fencing_token=1,
        published_at=NOW,
    )
    consumption = RunModelResponseConsumptionV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        execution_source="online",
        reservation_group_id=call.reservation_group_id,
        transport_attempt=1,
        cassette_shard_artifact_id="artifact:cassette:record-shard:1",
        consumed_at=NOW,
    )
    with SqliteUnitOfWork(engine, _model_call_capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_reservation_group(hold, (hold_reservation,))
        transaction.cost.put_reservation_group(call, (call_reservation,))
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        transaction.cost.put_routing_decision(decision)
        transaction.runs.put_intermediate_link(prompt)
        assert transaction.runs.put_model_route_link(route) == route
        assert transaction.runs.put_model_route_link(route) == route
        assert transaction.cost.reconcile_group(usage).status == "reconciled"
        assert transaction.runs.put_model_response_consumption(consumption) == consumption
        assert transaction.runs.put_model_response_consumption(consumption) == consumption

        fallback_prompt = prompt.model_copy(
            update={
                "route_ordinal": 2,
                "artifact_id": "artifact:prompt:model-route:2",
                "request_hash": HASH_B,
            }
        )
        with pytest.raises(Conflict, match="already-consumed"):
            transaction.runs.put_intermediate_link(fallback_prompt)

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get_model_route_link(queued.run_id, 1, 1, 1) == route
        assert repository.list_model_route_links(queued.run_id, attempt_no=1) == (route,)
        assert repository.get_model_response_consumption(queued.run_id, 1, 1, 1) == consumption
        assert repository.list_model_response_consumptions(
            queued.run_id,
            attempt_no=1,
        ) == (consumption,)
        assert session.scalar(select(func.count()).select_from(RunModelRouteLinkRow)) == 1
        assert session.scalar(select(func.count()).select_from(RunModelResponseConsumptionRow)) == 1

    noncanonical_now = NOW.replace("Z", "+00:00")
    with engine.begin() as connection:
        connection.execute(
            update(RunIntermediateArtifactLinkRow)
            .where(
                RunIntermediateArtifactLinkRow.run_id == queued.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == 1,
                RunIntermediateArtifactLinkRow.call_ordinal == 1,
                RunIntermediateArtifactLinkRow.route_ordinal == 1,
            )
            .values(published_at=noncanonical_now)
        )
        connection.execute(
            update(RunModelRouteLinkRow)
            .where(
                RunModelRouteLinkRow.run_id == queued.run_id,
                RunModelRouteLinkRow.attempt_no == 1,
                RunModelRouteLinkRow.call_ordinal == 1,
                RunModelRouteLinkRow.route_ordinal == 1,
            )
            .values(published_at=noncanonical_now)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="stored RunModelRouteLink"),
    ):
        SqlRunRepository(session).get_model_route_link(queued.run_id, 1, 1, 1)

    with engine.begin() as connection:
        connection.execute(
            update(RunIntermediateArtifactLinkRow)
            .where(
                RunIntermediateArtifactLinkRow.run_id == queued.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == 1,
                RunIntermediateArtifactLinkRow.call_ordinal == 1,
                RunIntermediateArtifactLinkRow.route_ordinal == 1,
            )
            .values(published_at=NOW)
        )
        connection.execute(
            update(RunModelRouteLinkRow)
            .where(
                RunModelRouteLinkRow.run_id == queued.run_id,
                RunModelRouteLinkRow.attempt_no == 1,
                RunModelRouteLinkRow.call_ordinal == 1,
                RunModelRouteLinkRow.route_ordinal == 1,
            )
            .values(published_at=NOW)
        )
        connection.execute(
            update(RunModelResponseConsumptionRow)
            .where(
                RunModelResponseConsumptionRow.run_id == queued.run_id,
                RunModelResponseConsumptionRow.attempt_no == 1,
                RunModelResponseConsumptionRow.call_ordinal == 1,
                RunModelResponseConsumptionRow.route_ordinal == 1,
            )
            .values(consumed_at=noncanonical_now)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="stored RunModelResponseConsumption"),
    ):
        SqlRunRepository(session).get_model_response_consumption(queued.run_id, 1, 1, 1)


def test_response_consumption_rejects_an_older_committed_route(
    engine: Engine,
) -> None:
    budget = _budget()
    budget_set = _budget_set(budget).model_copy(update={"budget_set_snapshot_id": "budget-set:1"})
    original_catalog, original_policy, original_decision = _catalog_policy_decision()
    primary = original_catalog.models[0]
    fallback_snapshot = canonical_model_snapshot_id(
        ModelSnapshot(
            provider=primary.provider,
            model="gpt-5.6-sol-fallback",
            snapshot_tag="2026-07",
        )
    )
    fallback = ModelDescriptorV1(
        **primary.model_dump(exclude={"model_snapshot", "tier"}),
        model_snapshot=fallback_snapshot,
        tier="fast",
    )
    catalog_body = {
        "catalog_version": original_catalog.catalog_version,
        "models": (primary, fallback),
        "created_at": original_catalog.created_at,
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_body,
        catalog_digest=compute_model_catalog_digest(catalog_body),
    )
    rule = original_policy.rules[0].model_copy(
        update={"allowed_fallback_chain": (fallback_snapshot,)}
    )
    policy_body = {
        "policy_version": original_policy.policy_version,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": (rule,),
        "failure_classifier_version": original_policy.failure_classifier_version,
    }
    policy = RoutingPolicyV1(
        **policy_body,
        routing_policy_digest=compute_routing_policy_digest(policy_body),
    )
    decision = RoutingDecisionV1.create(
        run_id=original_decision.run_id,
        attempt_no=original_decision.attempt_no,
        request_hash=COST_REQUEST_HASH,
        rule_id=rule.rule_id,
        model_snapshot=primary.model_snapshot,
        tier=primary.tier,
        reason_code="primary_rule",
        budget_set_snapshot_id=budget_set.budget_set_snapshot_id,
        fallback_from=None,
        fallback_index=0,
        policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        execution_source="online",
        decided_at=original_decision.decided_at,
    )
    later_decision = RoutingDecisionV1.create(
        run_id=decision.run_id,
        attempt_no=decision.attempt_no,
        request_hash=f"sha256:{HASH_C}",
        rule_id=rule.rule_id,
        model_snapshot=fallback.model_snapshot,
        tier=fallback.tier,
        reason_code="fallback_rule",
        budget_set_snapshot_id=decision.budget_set_snapshot_id,
        fallback_from=decision.model_snapshot,
        fallback_index=1,
        policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        execution_source="online",
        decided_at=decision.decided_at + timedelta(microseconds=1),
    )
    plan_fields = {
        "agent_graph_version": "graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="checker",
                prompt_version="checker@1",
                tool_version="checker@1",
                allowed_model_snapshots=(decision.model_snapshot, later_decision.model_snapshot),
            ),
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    payload_wire = _record_payload().model_dump(mode="python")
    payload_wire.update(
        llm_execution_mode="live",
        execution_version_plan=ExecutionVersionPlanV1(
            **plan_fields,
            plan_digest=execution_version_plan_digest(plan_fields),
        ),
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:1",
            prompt_version="checker@1",
            model_snapshot=decision.model_snapshot,
            agent_graph_version="graph@1",
            tool_version="checker@1",
        ),
    )
    queued = _run("run-1", payload=RunPayloadEnvelope.model_validate(payload_wire))
    _create(engine, queued)
    _start_result(engine, queued)
    with Session(engine) as session:
        session.add_all(
            (
                _artifact(
                    "artifact:prompt:old-route:1",
                    payload_hash=HASH_A,
                    kind="source_rendered",
                ),
                _artifact(
                    "artifact:prompt:old-route:2",
                    payload_hash=HASH_B,
                    kind="source_rendered",
                ),
            )
        )
        session.commit()

    bare_request_hash = COST_REQUEST_HASH.removeprefix("sha256:")
    first_prompt = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id="artifact:prompt:old-route:1",
        role="prompt_rendered",
        request_hash=bare_request_hash,
        fencing_token=1,
        published_at=NOW,
    )
    second_prompt = first_prompt.model_copy(
        update={
            "route_ordinal": 2,
            "artifact_id": "artifact:prompt:old-route:2",
            "request_hash": HASH_C,
        }
    )
    first_route = RunModelRouteLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        prompt_artifact_id=first_prompt.artifact_id,
        request_hash=first_prompt.request_hash,
        routing_decision_kind="native",
        routing_decision_id=decision.decision_id,
        fencing_token=1,
        published_at=NOW,
    )
    second_route = first_route.model_copy(
        update={
            "route_ordinal": 2,
            "prompt_artifact_id": second_prompt.artifact_id,
            "request_hash": second_prompt.request_hash,
            "routing_decision_id": later_decision.decision_id,
        }
    )
    stale_consumption = RunModelResponseConsumptionV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        execution_source="online",
        reservation_group_id="reservation:unused-because-route-is-stale",
        transport_attempt=1,
        cassette_shard_artifact_id=None,
        consumed_at=NOW,
    )
    with SqliteUnitOfWork(engine, _model_call_capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        transaction.cost.put_routing_decision(decision)
        transaction.cost.put_routing_decision(later_decision)
        transaction.runs.put_intermediate_link(first_prompt)
        transaction.runs.put_model_route_link(first_route)
        transaction.runs.put_intermediate_link(second_prompt)
        transaction.runs.put_model_route_link(second_route)

        with pytest.raises(Conflict, match="latest committed route"):
            transaction.runs.put_model_response_consumption(stale_consumption)


def test_legacy_replay_route_and_usage_keep_exact_import_decision_without_native(
    engine: Engine,
) -> None:
    budget = _budget()
    budget_set = _budget_set(budget).model_copy(update={"budget_set_snapshot_id": "budget-set:1"})
    hold, hold_reservation = _hold(budget)
    hold = hold.model_copy(update={"budget_set_snapshot_id": budget_set.budget_set_snapshot_id})
    call, call_reservation = _call(budget, hold)
    catalog, policy, _ = _catalog_policy_decision()
    legacy = LegacyImportRoutingDecisionV1.create(
        source_wire_sha256=HASH_C,
        request_hash=COST_REQUEST_HASH,
        agent_node_id="checker",
        model_snapshot=catalog.models[0].model_snapshot,
        execution_profile_binding_digests=(HASH_A,),
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
        verification_policy=LegacyImportVerificationPolicyRefV1(
            policy_id="legacy-import",
            policy_version=1,
            policy_digest=HASH_B,
        ),
    )
    plan_fields = {
        "agent_graph_version": "graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="checker",
                prompt_version="checker@1",
                tool_version="checker@1",
                allowed_model_snapshots=(legacy.model_snapshot,),
            ),
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    plan = ExecutionVersionPlanV1(
        **plan_fields,
        plan_digest=execution_version_plan_digest(plan_fields),
    )
    cassette_id = "artifact:cassette:legacy:1"
    payload_wire = _payload().model_dump(mode="python")
    payload_wire.update(
        input_artifact_ids=("artifact:input", cassette_id),
        execution_version_plan=plan,
        llm_execution_mode="replay",
        cassette_artifact_id=cassette_id,
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:1",
            prompt_version="checker@1",
            model_snapshot=legacy.model_snapshot,
            agent_graph_version="graph@1",
            tool_version="checker@1",
            cassette_id=cassette_id,
        ),
    )
    queued = _run("run-1", payload=RunPayloadEnvelope.model_validate(payload_wire))
    _create(engine, queued)
    _start_result(engine, queued)
    with Session(engine) as session:
        session.add(
            _artifact(
                "artifact:prompt:legacy-route:1",
                payload_hash=HASH_A,
                kind="source_rendered",
            )
        )
        session.commit()

    bare_request_hash = COST_REQUEST_HASH.removeprefix("sha256:")
    prompt = RunIntermediateArtifactLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id="artifact:prompt:legacy-route:1",
        role="prompt_rendered",
        request_hash=bare_request_hash,
        fencing_token=1,
        published_at=NOW,
    )
    route = RunModelRouteLinkV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        prompt_artifact_id=prompt.artifact_id,
        request_hash=prompt.request_hash,
        routing_decision_kind="legacy_import",
        routing_decision_id=legacy.decision_id,
        fencing_token=1,
        published_at=NOW,
    )
    usage = _usage(
        call,
        call_reservation,
        routing_decision_id=legacy.decision_id,
    ).model_copy(
        update={
            "execution_source": "cassette_replay",
            "routing_decision_kind": "legacy_import",
        }
    )
    consumption = RunModelResponseConsumptionV1(
        run_id=queued.run_id,
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        execution_source="cassette_replay",
        reservation_group_id=call.reservation_group_id,
        transport_attempt=None,
        cassette_shard_artifact_id=None,
        consumed_at=NOW,
    )
    with SqliteUnitOfWork(engine, _model_call_capabilities).begin() as transaction:
        transaction.cost.put_budget(budget)
        transaction.cost.put_budget_set(budget_set)
        transaction.cost.put_reservation_group(hold, (hold_reservation,))
        transaction.cost.put_reservation_group(call, (call_reservation,))
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_legacy_import_routing_decision(legacy)
        transaction.runs.put_intermediate_link(prompt)
        assert transaction.runs.put_model_route_link(route) == route
        assert transaction.cost.reconcile_group(usage).status == "reconciled"
        assert transaction.runs.put_model_response_consumption(consumption) == consumption

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get_model_route_link(queued.run_id, 1, 1, 1) == route
        assert repository.get_model_response_consumption(queued.run_id, 1, 1, 1) == consumption
        assert session.get(LegacyImportRoutingDecisionRow, legacy.decision_id) is not None
        usage_row = session.scalar(
            select(UsageEntryRow).where(
                UsageEntryRow.reservation_group_id == call.reservation_group_id
            )
        )
        assert usage_row is not None
        assert usage_row.routing_decision_kind == "legacy_import"
        assert usage_row.routing_decision_id == legacy.decision_id
        assert session.scalar(select(func.count()).select_from(RoutingDecisionRow)) == 0

    corrupted = legacy.model_dump(mode="json")
    corrupted["agent_node_id"] = "another-node"
    with engine.begin() as connection:
        connection.execute(
            update(LegacyImportRoutingDecisionRow)
            .where(LegacyImportRoutingDecisionRow.decision_id == legacy.decision_id)
            .values(payload=corrupted)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="legacy import routing decision"),
    ):
        SqlRunRepository(session).get_model_route_link(queued.run_id, 1, 1, 1)


def test_attempt_call_head_rejects_a_deleted_historical_prompt_link(
    engine: Engine,
) -> None:
    queued = _run()
    _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.claim(
            run_id=queued.run_id,
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
            run_id=queued.run_id,
            expected_revision=1,
            worker_principal_id="service:worker",
            lease_id="lease:1",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id="permit:1",
        )
    return claim.run, claim.attempt.next_call_ordinal


def _claim_result(engine: Engine, run: RunRecord | None = None) -> RunClaim:
    queued = run or _run()
    if run is None:
        _create(engine, queued)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        return transaction.runs.claim(
            run_id=queued.run_id,
            expected_revision=queued.revision,
            worker_principal_id="service:worker",
            lease_id=f"lease:{queued.run_id}",
            acquired_at=NOW,
            expires_at=LEASE_EXPIRES,
            permit_group_id=f"permit:{queued.run_id}",
        )


def _start_result(engine: Engine, run: RunRecord | None = None) -> RunAttemptStart:
    claimed = _claim_result(engine, run)
    fence = AttemptWriteFence(
        run_id=claimed.run.run_id,
        attempt_no=claimed.attempt.attempt_no,
        expected_run_revision=claimed.run.revision,
        lease_id=claimed.lease.lease_id,
        fencing_token=claimed.attempt.fencing_token,
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        return transaction.runs.start_attempt(
            run_id=fence.run_id,
            attempt_no=fence.attempt_no,
            expected_run_revision=fence.expected_run_revision,
            lease_id=fence.lease_id,
            fencing_token=fence.fencing_token,
            started_at=STARTED,
            attempt_deadline_utc=ATTEMPT_DEADLINE,
        )


def _fence(run: RunRecord, *, lease_id: str | None = None) -> AttemptWriteFence:
    assert run.current_attempt_no is not None
    return AttemptWriteFence(
        run_id=run.run_id,
        attempt_no=run.current_attempt_no,
        expected_run_revision=run.revision,
        lease_id=lease_id or f"lease:{run.run_id}",
        fencing_token=run.next_fencing_token - 1,
    )


@pytest.mark.parametrize("authority", ("run", "attempt", "lease", "event"))
def test_lifecycle_readers_reject_noncanonical_persisted_utc(
    engine: Engine,
    authority: str,
) -> None:
    started = _start_result(engine)
    noncanonical = STARTED.replace("Z", "+00:00")
    with engine.begin() as connection:
        if authority == "run":
            connection.execute(
                update(RunRow)
                .where(RunRow.run_id == started.run.run_id)
                .values(updated_at=noncanonical)
            )
        elif authority == "attempt":
            connection.execute(
                update(RunAttemptRow)
                .where(
                    RunAttemptRow.run_id == started.run.run_id,
                    RunAttemptRow.attempt_no == started.attempt.attempt_no,
                )
                .values(started_at=noncanonical)
            )
        elif authority == "lease":
            connection.execute(
                update(RunLeaseRow)
                .where(RunLeaseRow.lease_id == started.lease.lease_id)
                .values(acquired_at=NOW.replace("Z", "+00:00"))
            )
        else:
            connection.execute(
                update(RunEventRow)
                .where(
                    RunEventRow.run_id == started.run.run_id,
                    RunEventRow.seq == started.event.seq,
                )
                .values(occurred_at=noncanonical)
            )

    with Session(engine) as session, pytest.raises(IntegrityViolation, match="stored Run"):
        repository = SqlRunRepository(session)
        if authority == "run":
            repository.get(started.run.run_id)
        elif authority == "attempt":
            repository.get_attempt(started.run.run_id, started.attempt.attempt_no)
        elif authority == "lease":
            repository.get_current_lease(started.run.run_id)
        else:
            repository.get_event(started.run.run_id, started.event.seq)


def _retry_decision(run: RunRecord) -> RetryDecisionV1:
    return RetryDecisionV1(
        cause_code="dependency_unavailable",
        failure_class="transient_dependency",
        intrinsic_retry_eligible=True,
        decision="retry",
        reason_code="transient_eligible",
        retry_not_before_utc="2026-07-14T09:00:00.500000Z",
        classifier=run.failure_classifier,
        retry_policy=run.retry_policy,
        evaluated_at_utc=ENDED,
    )


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
    changed_command = command_record.model_copy(
        update={"actor": AuditActor(principal_id="human:b", principal_kind="human")}
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        with pytest.raises(IntegrityViolation, match="immutable"):
            transaction.runs.put_intermediate_link(changed_link)
        with pytest.raises(IntegrityViolation):
            transaction.runs.put_finding_link(changed_finding)
        with pytest.raises(IntegrityViolation, match="immutable"):
            transaction.runs.put_command(changed_command)

    noncanonical_now = NOW.replace("Z", "+00:00")
    with engine.begin() as connection:
        connection.execute(
            update(RunIntermediateArtifactLinkRow)
            .where(
                RunIntermediateArtifactLinkRow.run_id == run.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == 1,
                RunIntermediateArtifactLinkRow.call_ordinal == ordinal,
                RunIntermediateArtifactLinkRow.route_ordinal == 1,
            )
            .values(published_at=noncanonical_now)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="stored RunIntermediateArtifactLink"),
    ):
        SqlRunRepository(session).get_intermediate_link(run.run_id, 1, ordinal)

    with engine.begin() as connection:
        connection.execute(
            update(RunIntermediateArtifactLinkRow)
            .where(
                RunIntermediateArtifactLinkRow.run_id == run.run_id,
                RunIntermediateArtifactLinkRow.attempt_no == 1,
                RunIntermediateArtifactLinkRow.call_ordinal == ordinal,
                RunIntermediateArtifactLinkRow.route_ordinal == 1,
            )
            .values(published_at=NOW)
        )
        connection.execute(
            update(RunCommandRow)
            .where(
                RunCommandRow.run_id == run.run_id,
                RunCommandRow.command_id == command.command_id,
            )
            .values(created_at=noncanonical_now)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="stored RunCommand"),
    ):
        SqlRunRepository(session).get_command(run.run_id, command.command_id)

    with engine.begin() as connection:
        connection.execute(
            update(RunCommandRow)
            .where(
                RunCommandRow.run_id == run.run_id,
                RunCommandRow.command_id == command.command_id,
            )
            .values(created_at=NOW)
        )
        connection.execute(
            update(FindingRevisionRow)
            .where(
                FindingRevisionRow.finding_id == finding.finding_id,
                FindingRevisionRow.revision == finding.finding_revision,
            )
            .values(created_at=noncanonical_now)
        )
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="linked Finding revision"),
    ):
        SqlRunRepository(session).get_finding_link(run.run_id, 1, finding.ordinal)


def _persist_exact_finding_link(engine: Engine) -> RunFindingLinkV1:
    run, _ = _claim(engine)
    finding_revision = FindingRevisionV1(
        finding_id="finding:exact",
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
        session.add(_artifact("artifact:evidence:exact", payload_hash=HASH_B))
        session.add(
            FindingRevisionRow(
                finding_id=finding_revision.finding_id,
                revision=finding_revision.revision,
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
                finding_id=finding_revision.finding_id,
                current_revision=finding_revision.revision,
                current_digest=finding_digest,
                row_revision=1,
                updated_at=NOW,
            )
        )
        session.commit()

    link = RunFindingLinkV1(
        run_id=run.run_id,
        attempt_no=1,
        ordinal=1,
        finding_id=finding_revision.finding_id,
        finding_revision=finding_revision.revision,
        finding_digest=finding_digest,
        evidence_artifact_id="artifact:evidence:exact",
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.runs.put_finding_link(link)
    return link


def test_finding_link_can_be_read_by_its_exact_run_and_finding_revision(
    engine: Engine,
) -> None:
    expected = _persist_exact_finding_link(engine)

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert (
            repository.get_finding_link_by_revision(
                run_id=expected.run_id,
                finding_id=expected.finding_id,
                finding_revision=expected.finding_revision,
            )
            == expected
        )
        assert (
            repository.get_finding_link_by_revision(
                run_id="run:missing",
                finding_id=expected.finding_id,
                finding_revision=expected.finding_revision,
            )
            is None
        )
        assert (
            repository.get_finding_link_by_revision(
                run_id=expected.run_id,
                finding_id="finding:missing",
                finding_revision=expected.finding_revision,
            )
            is None
        )
        assert (
            repository.get_finding_link_by_revision(
                run_id=expected.run_id,
                finding_id=expected.finding_id,
                finding_revision=2,
            )
            is None
        )


def test_finding_links_can_be_enumerated_by_exact_evidence_artifact(
    engine: Engine,
) -> None:
    expected = _persist_exact_finding_link(engine)

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.list_finding_links_by_evidence_artifact_ids(
            (expected.evidence_artifact_id,),
            max_items=1,
        ) == (expected,)
        assert (
            repository.list_finding_links_by_evidence_artifact_ids(
                ("artifact:evidence:missing",),
                max_items=1,
            )
            == ()
        )


def test_finding_link_enumeration_chunks_the_maximum_selection_below_sqlite_bind_limit(
    engine: Engine,
) -> None:
    selected = tuple(
        f"artifact:evidence:missing:{ordinal:04d}" for ordinal in range(MAX_COLLECTION_ITEMS)
    )
    selected_statement_parameter_counts: list[int] = []

    def _observe_statement(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        if "run_finding_links.evidence_artifact_id IN" in statement:
            selected_statement_parameter_counts.append(len(parameters))

    event.listen(engine, "before_cursor_execute", _observe_statement)
    try:
        with Session(engine) as session:
            assert (
                SqlRunRepository(session).list_finding_links_by_evidence_artifact_ids(
                    selected,
                    max_items=MAX_COLLECTION_ITEMS,
                )
                == ()
            )
    finally:
        event.remove(engine, "before_cursor_execute", _observe_statement)

    assert len(selected_statement_parameter_counts) >= 2
    assert max(selected_statement_parameter_counts) < 999


def test_finding_link_enumeration_rejects_a_bound_above_the_contract_ceiling(
    engine: Engine,
) -> None:
    with (
        Session(engine) as session,
        pytest.raises(IntegrityViolation, match="contract bound"),
    ):
        SqlRunRepository(session).list_finding_links_by_evidence_artifact_ids(
            ("artifact:evidence:one",),
            max_items=MAX_COLLECTION_ITEMS + 1,
        )


def test_exact_finding_link_reader_reuses_retained_revision_integrity_checks(
    engine: Engine,
) -> None:
    expected = _persist_exact_finding_link(engine)
    with engine.begin() as connection:
        connection.execute(
            update(RunFindingLinkRow)
            .where(
                RunFindingLinkRow.run_id == expected.run_id,
                RunFindingLinkRow.attempt_no == expected.attempt_no,
                RunFindingLinkRow.ordinal == expected.ordinal,
            )
            .values(finding_digest=HASH_A)
        )

    with (
        Session(engine) as session,
        pytest.raises(
            IntegrityViolation,
            match="Finding revision",
        ),
    ):
        SqlRunRepository(session).get_finding_link_by_revision(
            run_id=expected.run_id,
            finding_id=expected.finding_id,
            finding_revision=expected.finding_revision,
        )


def test_finding_link_semantic_revision_cannot_be_duplicated_at_another_ordinal(
    engine: Engine,
) -> None:
    retained = _persist_exact_finding_link(engine)
    duplicate = retained.model_copy(update={"ordinal": 2})

    with pytest.raises(IntegrityViolation, match="another ordinal"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.put_finding_link(duplicate)

    with Session(engine) as session:
        assert (
            SqlRunRepository(session).get_finding_link_by_revision(
                run_id=retained.run_id,
                finding_id=retained.finding_id,
                finding_revision=retained.finding_revision,
            )
            == retained
        )


def test_attempt_start_renew_and_progress_use_exact_cas_and_roll_back_together(
    engine: Engine,
) -> None:
    started = _start_result(engine)
    assert started.run.status == "running"
    assert started.run.revision == 3
    assert started.attempt.status == "running"
    assert started.attempt.attempt_deadline_utc == ATTEMPT_DEADLINE

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        renewed = transaction.runs.renew_lease(
            run_id=started.run.run_id,
            attempt_no=started.attempt.attempt_no,
            lease_id=started.lease.lease_id,
            fencing_token=started.attempt.fencing_token,
            expected_lease_version=1,
            heartbeat_at=HEARTBEAT,
            expires_at=RENEWED_EXPIRES,
        )
    assert renewed.lease_version == 2

    with pytest.raises(Conflict, match="expired"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.renew_lease(
                run_id=started.run.run_id,
                attempt_no=started.attempt.attempt_no,
                lease_id=started.lease.lease_id,
                fencing_token=started.attempt.fencing_token,
                expected_lease_version=renewed.lease_version,
                heartbeat_at="2026-07-14T09:00:01Z",
                expires_at="2026-07-14T09:00:01.050000Z",
            )

    progress_event = RunEvent(
        run_id=started.run.run_id,
        seq=started.run.next_event_seq,
        event_type="attempt.progress",
        attempt_no=started.attempt.attempt_no,
        occurred_at=PROGRESSED,
        data_schema_version="attempt-progress@1",
        data=AttemptProgressDataV1(
            attempt_no=started.attempt.attempt_no,
            phase_code="checker",
            completed_units=1,
            total_units=2,
        ),
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        progress = transaction.runs.append_progress(
            fence=_fence(started.run),
            event=progress_event,
        )
    assert progress.run.revision == 4
    assert progress.run.next_event_seq == progress_event.seq + 1

    rolled_back_event = progress_event.model_copy(
        update={"seq": progress.run.next_event_seq, "occurred_at": ENDED}
    )
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.append_progress(
                fence=_fence(progress.run),
                event=rolled_back_event,
            )
            raise RuntimeError("rollback sentinel")

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(started.run.run_id) == progress.run
        assert repository.get_event(started.run.run_id, rolled_back_event.seq) is None
        with pytest.raises(Conflict):
            repository.renew_lease(
                run_id=started.run.run_id,
                attempt_no=started.attempt.attempt_no,
                lease_id=started.lease.lease_id,
                fencing_token=started.attempt.fencing_token,
                expected_lease_version=1,
                heartbeat_at=PROGRESSED,
                expires_at=RENEWED_EXPIRES,
            )


def test_attempt_start_requires_the_exact_frozen_timeout_deadline(engine: Engine) -> None:
    claimed = _claim_result(engine)
    with pytest.raises(IntegrityViolation, match="exact frozen timeout"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.start_attempt(
                run_id=claimed.run.run_id,
                attempt_no=claimed.attempt.attempt_no,
                expected_run_revision=claimed.run.revision,
                lease_id=claimed.lease.lease_id,
                fencing_token=claimed.attempt.fencing_token,
                started_at=STARTED,
                attempt_deadline_utc="2026-07-14T09:00:01.099999Z",
            )

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(claimed.run.run_id) == claimed.run
        assert repository.get_attempt(claimed.run.run_id, 1) == claimed.attempt
        assert repository.list_events(claimed.run.run_id)[-1] == claimed.event


def test_command_accept_claim_complete_and_acceptance_rollback_are_atomic(
    engine: Engine,
) -> None:
    queued = _run("run:command", idempotency_key="request:command")
    _create(engine, queued)
    started = _start_result(engine, queued)
    command = RunCommandV1(
        command_id="command:input",
        client_id="browser:command",
        client_seq=1,
        idempotency_key="input:command",
        expected_run_revision=started.run.revision,
        type="provide_input",
        payload_schema_id="playtest-provide-input@1",
        payload=PlaytestProvideInputPayloadV1(
            interaction_id="interaction:command",
            expected_state_hash=HASH_A,
            choice_id="choice:a",
        ),
    )
    record = RunCommandRecordV1(
        run_id=started.run.run_id,
        command=command,
        request_hash=canonical_payload_hash(command),
        actor=AuditActor(principal_id="human:a", principal_kind="human"),
        status="pending",
        revision=1,
        created_at=PROGRESSED,
    )
    accepted_event = RunEvent(
        run_id=started.run.run_id,
        seq=started.run.next_event_seq,
        event_type="run.command_accepted",
        occurred_at=PROGRESSED,
        data_schema_version="command-accepted@1",
        data=CommandAcceptedDataV1(
            command_id=command.command_id,
            command_type=command.type,
            command_revision=1,
        ),
    )

    with pytest.raises(IntegrityViolation, match="nonterminal command cannot publish a cassette"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.accept_command(
                expected_run_revision=started.run.revision,
                record=record,
                events=(accepted_event,),
                terminal_cassette_artifact_id="artifact:not-terminal",
            )

    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.accept_command(
                expected_run_revision=started.run.revision,
                record=record,
                events=(accepted_event,),
            )
            raise RuntimeError("rollback sentinel")
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get_command(started.run.run_id, command.command_id) is None
        assert repository.get_event(started.run.run_id, accepted_event.seq) is None
        assert repository.get(started.run.run_id) == started.run

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        accepted = transaction.runs.accept_command(
            expected_run_revision=started.run.revision,
            record=record,
            events=(accepted_event,),
        )
    assert accepted.run.revision == started.run.revision + 1
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        claimed = transaction.runs.claim_command(
            fence=_fence(accepted.run),
            command_id=command.command_id,
            claimed_at=PROGRESSED,
        )
    assert claimed.status == "claimed"
    outcome_event = RunEvent(
        run_id=accepted.run.run_id,
        seq=accepted.run.next_event_seq,
        event_type="run.command_applied",
        attempt_no=started.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="command-outcome@1",
        data=CommandOutcomeDataV1(
            command_id=command.command_id,
            command_type=command.type,
            command_revision=claimed.revision + 1,
            outcome_code="input_applied",
        ),
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        completed = transaction.runs.complete_command(
            fence=_fence(accepted.run),
            command_id=command.command_id,
            expected_command_revision=claimed.revision,
            outcome="applied",
            outcome_code="input_applied",
            occurred_at=ENDED,
            event=outcome_event,
        )
    assert completed.status == "applied"
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert (
            repository.get_command_by_idempotency(
                run_id=started.run.run_id,
                idempotency_key=command.idempotency_key,
            )
            == completed
        )
        assert (
            repository.get_command_by_client_sequence(
                run_id=started.run.run_id,
                client_id=command.client_id,
                client_seq=command.client_seq,
            )
            == completed
        )
        persisted_run = repository.get(started.run.run_id)
        assert persisted_run is not None
        assert persisted_run.revision == accepted.run.revision + 1
        assert repository.get_event(started.run.run_id, outcome_event.seq) == outcome_event


def test_inactive_cancel_command_persists_terminal_cassette_atomically(
    engine: Engine,
) -> None:
    queued = _run(
        "run:command-cancel",
        idempotency_key="request:command-cancel",
        payload=_record_payload(),
    )
    _create(engine, queued)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact(
                    "artifact:command-cancel-failure",
                    payload_hash=HASH_B,
                    kind="run_failure",
                ),
                _artifact(
                    "artifact:command-cancel-cassette",
                    payload_hash=HASH_C,
                    kind="cassette_bundle",
                ),
            ]
        )
        session.commit()
    command = RunCommandV1(
        command_id="command:cancel",
        client_id="browser:cancel",
        client_seq=1,
        idempotency_key="cancel:command",
        expected_run_revision=queued.revision,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )
    cancel_event = RunEvent(
        run_id=queued.run_id,
        seq=queued.next_event_seq,
        event_type="run.cancel_requested",
        occurred_at=STARTED,
        data_schema_version="cancel-requested@1",
        data=CancelRequestedDataV1(
            command_id=command.command_id,
            reason_code="user_requested",
        ),
    )
    terminal_event = RunEvent(
        run_id=queued.run_id,
        seq=cancel_event.seq + 1,
        event_type="run.cancelled",
        occurred_at=STARTED,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            failure_artifact_id="artifact:command-cancel-failure",
            cause_code="cancelled",
        ),
    )
    record = RunCommandRecordV1(
        run_id=queued.run_id,
        command=command,
        request_hash=canonical_payload_hash(command),
        actor=AuditActor(principal_id="human:a", principal_kind="human"),
        status="applied",
        revision=1,
        created_at=STARTED,
        applied_at=STARTED,
        result_event_seq=cancel_event.seq,
    )
    accept_args = {
        "expected_run_revision": queued.revision,
        "record": record,
        "events": (cancel_event, terminal_event),
        "terminal_status": "cancelled",
        "terminal_failure_artifact_id": "artifact:command-cancel-failure",
        "terminal_cassette_artifact_id": "artifact:command-cancel-cassette",
    }

    mismatched_events = (
        (
            cancel_event.model_copy(
                update={
                    "data": CancelRequestedDataV1(
                        command_id=command.command_id,
                        reason_code="different_reason",
                    )
                }
            ),
            terminal_event,
        ),
        (
            cancel_event,
            terminal_event.model_copy(
                update={
                    "data": RunTerminatedDataV1(
                        failure_artifact_id="artifact:different-failure",
                        cause_code="cancelled",
                    )
                }
            ),
        ),
        (
            cancel_event,
            terminal_event.model_copy(
                update={
                    "data": RunTerminatedDataV1(
                        failure_artifact_id="artifact:command-cancel-failure",
                        cause_code="different_cause",
                    )
                }
            ),
        ),
        (
            cancel_event,
            terminal_event.model_copy(
                update={
                    "data": RunTerminatedDataV1(
                        attempt_no=1,
                        failure_artifact_id="artifact:command-cancel-failure",
                        cause_code="cancelled",
                    )
                }
            ),
        ),
    )
    for events in mismatched_events:
        with pytest.raises(IntegrityViolation, match="cancel"):
            with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
                transaction.runs.accept_command(**{**accept_args, "events": events})

    with pytest.raises(IntegrityViolation, match="terminal_cassette_artifact_id"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.accept_command(**{**accept_args, "terminal_cassette_artifact_id": ""})
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.accept_command(**accept_args)
            raise RuntimeError("rollback sentinel")
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(queued.run_id) == queued
        assert repository.get_command(queued.run_id, command.command_id) is None
        assert repository.list_events(queued.run_id) == (_queued_event(queued),)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        accepted = transaction.runs.accept_command(**accept_args)
    assert accepted.run.status == "cancelled"
    assert accepted.run.failure_artifact_id == "artifact:command-cancel-failure"
    assert accepted.run.terminal_cassette_artifact_id == "artifact:command-cancel-cassette"
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(queued.run_id) == accepted.run
        assert repository.get_command(queued.run_id, command.command_id) == record
        assert repository.list_events(queued.run_id) == (
            _queued_event(queued),
            cancel_event,
            terminal_event,
        )


def test_retry_close_releases_fence_without_preallocating_and_is_rollback_safe(
    engine: Engine,
) -> None:
    queued = _run(
        "run:retry",
        idempotency_key="request:retry",
        payload=_record_payload(),
    )
    _create(engine, queued)
    started = _start_result(engine, queued)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:attempt-failure", payload_hash=HASH_A),
                _artifact(
                    "artifact:retry-attempt-cassette",
                    payload_hash=HASH_B,
                    kind="cassette_bundle",
                ),
            ]
        )
        session.commit()
    decision = _retry_decision(started.run)
    retry_event = RunEvent(
        run_id=started.run.run_id,
        seq=started.run.next_event_seq,
        event_type="attempt.retry_scheduled",
        attempt_no=started.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="retry-scheduled@1",
        data=RetryScheduledDataV1(
            attempt_no=started.attempt.attempt_no,
            failure_artifact_id="artifact:attempt-failure",
            cause_code=decision.cause_code,
            failure_class=decision.failure_class,
            retry_decision=decision,
            retry_not_before_utc=decision.retry_not_before_utc or "",
        ),
    )
    close_args = {
        "fence": _fence(started.run),
        "ended_at": ENDED,
        "attempt_status": "failed",
        "lease_status": "closed",
        "failure_class": decision.failure_class,
        "failure_artifact_id": "artifact:attempt-failure",
        "attempt_cassette_artifact_id": "artifact:retry-attempt-cassette",
        "retry_decision": decision,
        "events": (retry_event,),
    }
    with pytest.raises(IntegrityViolation, match="RECORD attempt"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.close_attempt_for_retry(
                **{**close_args, "attempt_cassette_artifact_id": None}
            )
    with pytest.raises(RuntimeError, match="rollback sentinel"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.close_attempt_for_retry(**close_args)
            raise RuntimeError("rollback sentinel")
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(started.run.run_id) == started.run
        assert repository.get_attempt(started.run.run_id, 1) == started.attempt
        assert repository.get_current_lease(started.run.run_id) == started.lease
        assert repository.get_event(started.run.run_id, retry_event.seq) is None

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        closed = transaction.runs.close_attempt_for_retry(**close_args)
    assert closed.run.status == "retry_wait"
    assert closed.run.current_attempt_no is None
    assert closed.run.next_attempt_no == 2
    assert closed.run.next_fencing_token == 2
    assert closed.attempt.status == "failed"
    assert closed.attempt.cassette_bundle_artifact_id == ("artifact:retry-attempt-cassette")
    assert closed.lease.status == "closed"
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(closed.run.run_id) == closed.run
        assert repository.get_attempt(closed.run.run_id, 1) == closed.attempt

    retry_at = decision.retry_not_before_utc or ""
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        assert transaction.runs.get_claim_candidate(now_utc=retry_at) == closed.run
        claimed = transaction.runs.claim(
            run_id=closed.run.run_id,
            expected_revision=closed.run.revision,
            worker_principal_id="service:worker:retry",
            lease_id="lease:retry:2",
            acquired_at=retry_at,
            expires_at="2026-07-14T09:00:00.800000Z",
            permit_group_id="permit:retry:2",
        )
    assert claimed.attempt.attempt_no == 2
    assert claimed.attempt.fencing_token == 2


def test_inactive_terminal_close_publishes_one_terminal_head_and_rejects_stale_cas(
    engine: Engine,
) -> None:
    queued = _run(
        "run:inactive",
        idempotency_key="request:inactive",
        payload=_record_payload(),
    )
    _create(engine, queued)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:run-failure", payload_hash=HASH_B),
                _artifact(
                    "artifact:inactive-run-cassette",
                    payload_hash=HASH_C,
                    kind="cassette_bundle",
                ),
            ]
        )
        session.commit()
    decision = RetryDecisionV1(
        cause_code="cancelled",
        failure_class="cancelled",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=queued.failure_classifier,
        retry_policy=queued.retry_policy,
        evaluated_at_utc=STARTED,
    )
    event = RunEvent(
        run_id=queued.run_id,
        seq=queued.next_event_seq,
        event_type="run.cancelled",
        occurred_at=STARTED,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            failure_artifact_id="artifact:run-failure",
            cause_code="cancelled",
        ),
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        terminal = transaction.runs.terminate_inactive_run(
            run_id=queued.run_id,
            expected_run_revision=queued.revision,
            run_status="cancelled",
            failure_artifact_id="artifact:run-failure",
            terminal_cassette_artifact_id="artifact:inactive-run-cassette",
            retry_decision=decision,
            event=event,
        )
    assert terminal.run.status == "cancelled"
    assert terminal.run.failure_artifact_id == "artifact:run-failure"
    assert terminal.run.terminal_cassette_artifact_id == ("artifact:inactive-run-cassette")
    assert terminal.attempt is None
    assert terminal.lease is None
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        with pytest.raises(Conflict):
            transaction.runs.terminate_inactive_run(
                run_id=queued.run_id,
                expected_run_revision=queued.revision,
                run_status="cancelled",
                failure_artifact_id="artifact:run-failure",
                terminal_cassette_artifact_id="artifact:inactive-run-cassette",
                retry_decision=decision,
                event=event,
            )
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(queued.run_id) == terminal.run
        assert repository.list_events(queued.run_id) == (_queued_event(queued), event)


def test_active_success_and_failure_terminal_paths_close_attempt_and_lease(
    engine: Engine,
) -> None:
    success_run = _run(
        "run:success",
        idempotency_key="request:success",
        payload=_record_payload(),
    )
    _create(engine, success_run)
    succeeded_attempt = _start_result(engine, success_run)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:result", payload_hash=HASH_A),
                _artifact("artifact:terminal-attempt", payload_hash=HASH_B),
                _artifact("artifact:terminal-run", payload_hash=HASH_C),
                _artifact(
                    "artifact:success-attempt-cassette",
                    payload_hash=HASH_A,
                    kind="cassette_bundle",
                ),
                _artifact(
                    "artifact:success-run-cassette",
                    payload_hash=HASH_B,
                    kind="cassette_bundle",
                ),
                _artifact(
                    "artifact:failure-attempt-cassette",
                    payload_hash=HASH_B,
                    kind="cassette_bundle",
                ),
                _artifact(
                    "artifact:failure-run-cassette",
                    payload_hash=HASH_C,
                    kind="cassette_bundle",
                ),
            ]
        )
        session.commit()
    success_event = RunEvent(
        run_id=success_run.run_id,
        seq=succeeded_attempt.run.next_event_seq,
        event_type="run.succeeded",
        attempt_no=succeeded_attempt.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="run-succeeded@1",
        data=RunSucceededDataV1(
            attempt_no=succeeded_attempt.attempt.attempt_no,
            result_artifact_id="artifact:result",
        ),
    )
    with pytest.raises(IntegrityViolation, match="distinct"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.complete_attempt_success(
                fence=_fence(succeeded_attempt.run),
                ended_at=ENDED,
                result_artifact_id="artifact:result",
                attempt_cassette_artifact_id="artifact:success-attempt-cassette",
                terminal_cassette_artifact_id="artifact:success-attempt-cassette",
                event=success_event,
            )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        succeeded = transaction.runs.complete_attempt_success(
            fence=_fence(succeeded_attempt.run),
            ended_at=ENDED,
            result_artifact_id="artifact:result",
            attempt_cassette_artifact_id="artifact:success-attempt-cassette",
            terminal_cassette_artifact_id="artifact:success-run-cassette",
            event=success_event,
        )
    assert succeeded.run.status == "succeeded"
    assert succeeded.run.terminal_cassette_artifact_id == "artifact:success-run-cassette"
    assert succeeded.attempt is not None and succeeded.attempt.status == "succeeded"
    assert succeeded.attempt.cassette_bundle_artifact_id == ("artifact:success-attempt-cassette")
    assert succeeded.lease is not None and succeeded.lease.status == "closed"
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(success_run.run_id) == succeeded.run
        assert repository.get_attempt(success_run.run_id, 1) == succeeded.attempt

    failed_run = _run(
        "run:failed",
        idempotency_key="request:failed",
        payload=_record_payload(),
    )
    _create(engine, failed_run)
    failed_attempt = _start_result(engine, failed_run)
    decision = RetryDecisionV1(
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=failed_run.failure_classifier,
        retry_policy=failed_run.retry_policy,
        evaluated_at_utc=ENDED,
    )
    failure_event = RunEvent(
        run_id=failed_run.run_id,
        seq=failed_attempt.run.next_event_seq,
        event_type="run.failed",
        attempt_no=failed_attempt.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            attempt_no=failed_attempt.attempt.attempt_no,
            failure_artifact_id="artifact:terminal-run",
            cause_code="execution_failed",
        ),
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        failed = transaction.runs.close_attempt_terminal(
            fence=_fence(failed_attempt.run),
            ended_at=ENDED,
            attempt_status="failed",
            lease_status="closed",
            run_status="failed",
            failure_class="execution",
            attempt_failure_artifact_id="artifact:terminal-attempt",
            run_failure_artifact_id="artifact:terminal-run",
            attempt_cassette_artifact_id="artifact:failure-attempt-cassette",
            terminal_cassette_artifact_id="artifact:failure-run-cassette",
            retry_decision=decision,
            leading_events=(),
            terminal_event=failure_event,
        )
    assert failed.run.status == "failed"
    assert failed.run.terminal_cassette_artifact_id == "artifact:failure-run-cassette"
    assert failed.attempt is not None and failed.attempt.failure_artifact_id == (
        "artifact:terminal-attempt"
    )
    assert failed.attempt.cassette_bundle_artifact_id == ("artifact:failure-attempt-cassette")
    assert failed.lease is not None and failed.lease.status == "closed"
    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(failed_run.run_id) == failed.run
        assert repository.get_attempt(failed_run.run_id, 1) == failed.attempt

    reaped_run = _run("run:reaped-cancel", idempotency_key="request:reaped-cancel")
    _create(engine, reaped_run)
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        reaped_claim = transaction.runs.claim(
            run_id=reaped_run.run_id,
            expected_revision=reaped_run.revision,
            worker_principal_id="service:worker:reaped",
            lease_id="lease:reaped-cancel",
            acquired_at=NOW,
            expires_at="2026-07-14T09:00:00.250000Z",
            permit_group_id="permit:reaped-cancel",
        )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        reaped_start = transaction.runs.start_attempt(
            run_id=reaped_run.run_id,
            attempt_no=reaped_claim.attempt.attempt_no,
            expected_run_revision=reaped_claim.run.revision,
            lease_id=reaped_claim.lease.lease_id,
            fencing_token=reaped_claim.attempt.fencing_token,
            started_at=STARTED,
            attempt_deadline_utc=ATTEMPT_DEADLINE,
        )
    reaped_decision = RetryDecisionV1(
        cause_code="cancelled",
        failure_class="cancelled",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=reaped_run.failure_classifier,
        retry_policy=reaped_run.retry_policy,
        evaluated_at_utc=ENDED,
    )
    expiry_event = RunEvent(
        run_id=reaped_run.run_id,
        seq=reaped_start.run.next_event_seq,
        event_type="attempt.lease_expired",
        attempt_no=reaped_start.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="lease-expired@1",
        data=LeaseExpiredDataV1(
            attempt_no=reaped_start.attempt.attempt_no,
            failure_artifact_id="artifact:terminal-attempt",
            will_retry=False,
        ),
    )
    cancelled_event = RunEvent(
        run_id=reaped_run.run_id,
        seq=expiry_event.seq + 1,
        event_type="run.cancelled",
        attempt_no=reaped_start.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            attempt_no=reaped_start.attempt.attempt_no,
            failure_artifact_id="artifact:terminal-run",
            cause_code="cancelled",
        ),
    )
    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        cancelled = transaction.runs.close_attempt_terminal(
            fence=AttemptWriteFence(
                run_id=reaped_run.run_id,
                attempt_no=reaped_start.attempt.attempt_no,
                expected_run_revision=reaped_start.run.revision,
                lease_id=reaped_claim.lease.lease_id,
                fencing_token=reaped_start.attempt.fencing_token,
            ),
            ended_at=ENDED,
            attempt_status="cancelled",
            lease_status="expired",
            run_status="cancelled",
            failure_class="cancelled",
            attempt_failure_artifact_id="artifact:terminal-attempt",
            run_failure_artifact_id="artifact:terminal-run",
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=None,
            retry_decision=reaped_decision,
            leading_events=(expiry_event,),
            terminal_event=cancelled_event,
        )
    assert cancelled.attempt is not None and cancelled.attempt.status == "cancelled"
    assert cancelled.lease is not None and cancelled.lease.status == "expired"


def test_cancelled_attempt_may_close_after_its_deadline_with_deadline_decision(
    engine: Engine,
) -> None:
    queued = _run(
        "run:cancelled-at-deadline",
        idempotency_key="request:cancelled-at-deadline",
    )
    _create(engine, queued)
    started = _start_result(engine, queued)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:cancelled-attempt", payload_hash=HASH_A),
                _artifact("artifact:cancelled-run", payload_hash=HASH_B),
            ]
        )
        session.commit()
    decision = RetryDecisionV1(
        cause_code="cancelled",
        failure_class="cancelled",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="attempt_deadline_exhausted",
        classifier=queued.failure_classifier,
        retry_policy=queued.retry_policy,
        evaluated_at_utc=ATTEMPT_DEADLINE,
    )
    event = RunEvent(
        run_id=queued.run_id,
        seq=started.run.next_event_seq,
        event_type="run.cancelled",
        attempt_no=started.attempt.attempt_no,
        occurred_at=ATTEMPT_DEADLINE,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            attempt_no=started.attempt.attempt_no,
            failure_artifact_id="artifact:cancelled-run",
            cause_code="cancelled",
        ),
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        terminal = transaction.runs.close_attempt_terminal(
            fence=_fence(started.run),
            ended_at=ATTEMPT_DEADLINE,
            attempt_status="cancelled",
            lease_status="closed",
            run_status="cancelled",
            failure_class="cancelled",
            attempt_failure_artifact_id="artifact:cancelled-attempt",
            run_failure_artifact_id="artifact:cancelled-run",
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=None,
            retry_decision=decision,
            leading_events=(),
            terminal_event=event,
        )

    assert terminal.run.status == "cancelled"
    assert terminal.attempt is not None and terminal.attempt.status == "cancelled"
    assert terminal.lease is not None and terminal.lease.status == "closed"


def test_terminal_attempt_cassette_cas_failure_rolls_back_run_and_event(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = _run(
        "run:cassette-cas",
        idempotency_key="request:cassette-cas",
        payload=_record_payload(),
    )
    _create(engine, queued)
    started = _start_result(engine, queued)
    with Session(engine) as session:
        session.add_all(
            [
                _artifact("artifact:cassette-cas-result", payload_hash=HASH_A),
                _artifact(
                    "artifact:existing-attempt-cassette",
                    payload_hash=HASH_B,
                    kind="cassette_bundle",
                ),
                _artifact(
                    "artifact:desired-attempt-cassette",
                    payload_hash=HASH_C,
                    kind="cassette_bundle",
                ),
                _artifact(
                    "artifact:desired-run-cassette",
                    payload_hash=HASH_A,
                    kind="cassette_bundle",
                ),
            ]
        )
        session.commit()

    original_close = SqlRunRepository._close_active_attempt

    def race_attempt_cassette(self: SqlRunRepository, **kwargs: object) -> None:
        self._session.execute(
            update(RunAttemptRow)
            .where(
                RunAttemptRow.run_id == started.run.run_id,
                RunAttemptRow.attempt_no == started.attempt.attempt_no,
            )
            .values(cassette_bundle_artifact_id="artifact:existing-attempt-cassette")
        )
        original_close(self, **kwargs)

    monkeypatch.setattr(
        SqlRunRepository,
        "_close_active_attempt",
        race_attempt_cassette,
    )

    event = RunEvent(
        run_id=started.run.run_id,
        seq=started.run.next_event_seq,
        event_type="run.succeeded",
        attempt_no=started.attempt.attempt_no,
        occurred_at=ENDED,
        data_schema_version="run-succeeded@1",
        data=RunSucceededDataV1(
            attempt_no=started.attempt.attempt_no,
            result_artifact_id="artifact:cassette-cas-result",
        ),
    )
    for invalid_args, field_name in (
        (
            {
                "attempt_cassette_artifact_id": "",
                "terminal_cassette_artifact_id": "artifact:desired-run-cassette",
            },
            "attempt_cassette_artifact_id",
        ),
        (
            {
                "attempt_cassette_artifact_id": "artifact:desired-attempt-cassette",
                "terminal_cassette_artifact_id": "",
            },
            "terminal_cassette_artifact_id",
        ),
    ):
        with pytest.raises(IntegrityViolation, match=field_name):
            with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
                transaction.runs.complete_attempt_success(
                    fence=_fence(started.run),
                    ended_at=ENDED,
                    result_artifact_id="artifact:cassette-cas-result",
                    event=event,
                    **invalid_args,
                )

    with pytest.raises(Conflict, match="Attempt CAS"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.runs.complete_attempt_success(
                fence=_fence(started.run),
                ended_at=ENDED,
                result_artifact_id="artifact:cassette-cas-result",
                attempt_cassette_artifact_id="artifact:desired-attempt-cassette",
                terminal_cassette_artifact_id="artifact:desired-run-cassette",
                event=event,
            )

    with Session(engine) as session:
        repository = SqlRunRepository(session)
        assert repository.get(started.run.run_id) == started.run
        attempt = repository.get_attempt(started.run.run_id, started.attempt.attempt_no)
        assert attempt is not None
        assert attempt.status == "running"
        assert attempt.cassette_bundle_artifact_id is None
        assert repository.get_current_lease(started.run.run_id) == started.lease
        assert repository.get_event(started.run.run_id, event.seq) is None
