"""Real SQLite authority tests for the Task-10 worker call-cost bridge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, update
from sqlalchemy import select
from sqlalchemy.orm import Session

from gameforge.apps.worker.cost_bridge import (
    AgentStepReservationToken,
    CallReservationToken,
    WorkerAgentStepCostGateway,
    WorkerCallCostGateway,
    WorkerConservativeAttemptUsageProvider,
)
from gameforge.apps.worker.model_bridge import _record_from_result
from gameforge.apps.worker.response_publication import WorkerResponseConsumptionPublisher
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import (
    BudgetReservationV1,
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    BudgetV1,
    CacheHitObservationV1,
    CostAmountV1,
    LatencyObservationV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import (
    Conflict,
    IntegrityViolation,
    InvalidStateTransition,
    QuotaExceeded,
)
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    RunAttempt,
    RunEvent,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunModelRouteLinkV1,
    RunQueuedDataV1,
    RunRecord,
    canonical_payload_hash,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.model_router import ModelRequestV2, request_hash
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.model_router.m4_router import M4RouterResultV1
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    RunAttemptRow,
    RunEventRow,
    RunLeaseRow,
    RunRow,
)
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.runs import (
    SqlRunRepository,
    _attempt_values,
    _event_values,
    _lease_values,
    _run_values,
)
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4c.test_terminal_publisher import _registry_and_definition, _run_record
from tests.runtime.model_router.test_routing_v2 import NOW, _request


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
HUMAN = AuditActor(principal_id="human:maker", principal_kind="human")
_CURSOR_KEY = b"worker-cost-response-publication-test"
_AGENT_GRAPH_VERSION = "repair-graph@1"
_AGENT_TOOL_VERSION = "repair-tool@1"
_PROMPT_RENDERER_VERSION = "canonical-prompt-renderer@1"


def _amount(dimension: str, value: int) -> CostAmountV1:
    units = {
        "input_token": "token",
        "output_token": "token",
        "cache_read_token": "token",
        "cache_write_token": "token",
        "request": "request",
        "agent_step": "step",
        "wall_time_ns": "ns",
    }
    return CostAmountV1(dimension=dimension, value=Decimal(value), unit=units[dimension])


def _amounts(values: tuple[CostAmountV1, ...]) -> dict[str, Decimal]:
    return {item.dimension: item.value for item in values}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class _Harness:
    engine: Engine
    uow: SqliteUnitOfWork
    objects: LocalObjectStore
    clock: FrozenUtcClock
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    fence: AttemptWriteFence
    request: ModelRequestV2
    decision: RoutingDecisionV1
    catalog: ModelCatalogSnapshotV1
    policy: RoutingPolicyV1
    hold_id: str
    budget_id: str

    def gateway(self, *, fence: AttemptWriteFence | None = None) -> WorkerCallCostGateway:
        return WorkerCallCostGateway(
            unit_of_work=self.uow,
            run=self.run,
            attempt=self.attempt,
            fence=fence or self.fence,
            actor=WORKER,
            clock=self.clock,
        )

    def step_gateway(
        self,
        *,
        fence: AttemptWriteFence | None = None,
    ) -> WorkerAgentStepCostGateway:
        return WorkerAgentStepCostGateway(
            unit_of_work=self.uow,
            run=self.run,
            attempt=self.attempt,
            fence=fence or self.fence,
            actor=WORKER,
            clock=self.clock,
        )

    def put_decision(self, decision: RoutingDecisionV1) -> None:
        with self.uow.begin() as transaction:
            transaction.cost.put_routing_decision(decision)

    def reserve(
        self,
        *,
        decision: RoutingDecisionV1 | None = None,
        model_request: ModelRequestV2 | None = None,
        transport_attempt: int = 1,
        route_ordinal: int = 1,
        gateway: WorkerCallCostGateway | None = None,
        deadline: datetime | None = None,
    ) -> CallReservationToken:
        selected_decision = decision or self.decision
        selected_request = model_request or self.request
        return (gateway or self.gateway()).reserve_call(
            decision=selected_decision,
            model_request=selected_request,
            deadline_utc=deadline or (NOW + timedelta(seconds=10)),
            call_ordinal=1,
            route_ordinal=route_ordinal,
            transport_attempt=transport_attempt,
        )


def _capabilities(
    session: Session,
    clock: FrozenUtcClock,
    objects: LocalObjectStore,
) -> TransactionCapabilities:
    bindings = SqlObjectBindingRepository(session, objects, "local")
    return TransactionCapabilities(
        refs=None,
        audit=SqlAuditSink(session),
        approvals=None,
        lineage=None,
        object_bindings=bindings,
        runs=SqlRunRepository(session),
        cost=SqlCostLedger(session, clock=clock),
        artifacts=SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=_CURSOR_KEY, clock=clock),
            clock=clock,
        ),
    )


def _catalog_policy(request: ModelRequestV2) -> tuple[ModelCatalogSnapshotV1, RoutingPolicyV1]:
    descriptor = ModelDescriptorV1(
        provider=request.model_snapshot.provider,
        model_snapshot=canonical_model_snapshot_id(request.model_snapshot),
        tier="best",
        capabilities=("reasoning",),
        context_limit=4_096,
        max_output_tokens=64,
        prompt_cache_support=True,
        status="active",
    )
    fallback_snapshot = request.model_snapshot.model_copy(
        update={"model": f"{request.model_snapshot.model}-fallback"}
    )
    fallback_descriptor = ModelDescriptorV1(
        provider=fallback_snapshot.provider,
        model_snapshot=canonical_model_snapshot_id(fallback_snapshot),
        tier="fast",
        capabilities=("reasoning",),
        context_limit=4_096,
        max_output_tokens=64,
        prompt_cache_support=True,
        status="active",
    )
    catalog_body = {
        "catalog_version": 1,
        "models": (descriptor, fallback_descriptor),
        "created_at": NOW,
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_body,
        catalog_digest=compute_model_catalog_digest(catalog_body),
    )
    rule = RoutingRuleV1(
        rule_id="repair",
        task_kind=request.agent_node_id,
        required_capabilities=("reasoning",),
        primary_model_snapshot=descriptor.model_snapshot,
        allowed_fallback_chain=(fallback_descriptor.model_snapshot,),
        budget_predicates=(),
    )
    policy_body = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": (rule,),
        "failure_classifier_version": "failure-classifier@1",
    }
    policy = RoutingPolicyV1(
        **policy_body,
        routing_policy_digest=compute_routing_policy_digest(policy_body),
    )
    return catalog, policy


def _decision(
    *,
    request: ModelRequestV2,
    catalog: ModelCatalogSnapshotV1,
    policy: RoutingPolicyV1,
    budget_set_snapshot_id: str,
    execution_source: str = "online",
    fallback_index: int = 0,
) -> RoutingDecisionV1:
    rule = policy.rules[0]
    chain = (rule.primary_model_snapshot, *rule.allowed_fallback_chain)
    model_id = canonical_model_snapshot_id(request.model_snapshot)
    if chain[fallback_index] != model_id:
        raise ValueError("test request model does not match selected route")
    descriptor = next(item for item in catalog.models if item.model_snapshot == model_id)
    return RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=request_hash(request),
        rule_id="repair",
        model_snapshot=model_id,
        tier=descriptor.tier,
        reason_code=(
            "primary_rule"
            if fallback_index == 0 and execution_source == "online"
            else "fallback_rule"
            if execution_source == "online"
            else "exact_cache_hit"
        ),
        budget_set_snapshot_id=budget_set_snapshot_id,
        fallback_from=None if fallback_index == 0 else chain[fallback_index - 1],
        fallback_index=fallback_index,
        policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        execution_source=execution_source,
        decided_at=NOW,
    )


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    engine = get_engine(f"sqlite:///{tmp_path / 'worker-cost.db'}")
    migrations_api.upgrade(str(engine.url), "head")
    clock = FrozenUtcClock(NOW)
    objects = LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=clock,
        cursor_signing_key=_CURSOR_KEY,
    )
    uow = SqliteUnitOfWork(
        engine,
        lambda session: _capabilities(session, clock, objects),
    )
    request = _request()
    catalog, policy = _catalog_policy(request)

    budget = BudgetV1(
        budget_id="budget:run-1",
        scope_kind="run",
        scope_id="run-1",
        policy_version="worker-call-budget@1",
        limits=(
            _amount("input_token", 100_000),
            _amount("output_token", 10_000),
            _amount("cache_read_token", 100_000),
            _amount("cache_write_token", 100_000),
            _amount("request", 100),
            _amount("agent_step", 100),
            _amount("wall_time_ns", 1_000_000_000_000),
        ),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        deadline_utc=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(minutes=1),
    )
    snapshot = BudgetSnapshotV1(
        snapshot_id="budget-snapshot:run-1",
        budget_id=budget.budget_id,
        scope_kind=budget.scope_kind,
        scope_id=budget.scope_id,
        policy_version=budget.policy_version,
        budget_revision_at_freeze=budget.revision,
        limits=budget.limits,
        reserved=budget.reserved,
        consumed=budget.consumed,
        captured_at=NOW,
    )
    principal_budget = BudgetV1(
        budget_id="budget:principal:worker",
        scope_kind="principal",
        scope_id="service:worker:1",
        policy_version="worker-step-budget@1",
        limits=(_amount("agent_step", 100),),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        deadline_utc=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(minutes=1),
    )
    system_budget = BudgetV1(
        budget_id="budget:system:worker",
        scope_kind="system",
        scope_id="global",
        policy_version="worker-call-budget@1",
        limits=(
            _amount("request", 100),
            _amount("wall_time_ns", 1_000_000_000_000),
        ),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        deadline_utc=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(minutes=1),
    )

    def shared_snapshot(item: BudgetV1) -> BudgetSnapshotV1:
        return BudgetSnapshotV1(
            snapshot_id=f"budget-snapshot:{item.budget_id}",
            budget_id=item.budget_id,
            scope_kind=item.scope_kind,
            scope_id=item.scope_id,
            policy_version=item.policy_version,
            budget_revision_at_freeze=item.revision,
            limits=item.limits,
            reserved=item.reserved,
            consumed=item.consumed,
            captured_at=NOW,
        )

    budget_set = BudgetSetSnapshotV1(
        budget_set_snapshot_id="budget-set:run-1",
        run_id="run-1",
        selection_policy_version="worker-call-selection@1",
        snapshots=(snapshot, shared_snapshot(principal_budget), shared_snapshot(system_budget)),
        captured_at=NOW,
    )
    hold_id = "hold:run-1"
    hold_member = BudgetReservationV1(
        reservation_id="reservation:hold:run-1",
        reservation_group_id=hold_id,
        budget_id=budget.budget_id,
        reserved=(
            _amount("input_token", 10_000),
            _amount("output_token", 1_000),
            _amount("cache_read_token", 10_000),
            _amount("cache_write_token", 10_000),
            _amount("request", 10),
            _amount("agent_step", 10),
            _amount("wall_time_ns", 100_000_000_000),
        ),
        status="reserved",
        revision=1,
    )
    principal_hold_member = BudgetReservationV1(
        reservation_id="reservation:hold:principal:worker",
        reservation_group_id=hold_id,
        budget_id=principal_budget.budget_id,
        reserved=(_amount("agent_step", 10),),
        status="reserved",
        revision=1,
    )
    system_hold_member = BudgetReservationV1(
        reservation_id="reservation:hold:system:worker",
        reservation_group_id=hold_id,
        budget_id=system_budget.budget_id,
        reserved=(
            _amount("request", 10),
            _amount("wall_time_ns", 100_000_000_000),
        ),
        status="reserved",
        revision=1,
    )
    hold_members = (hold_member, principal_hold_member, system_hold_member)
    hold = ReservationGroupV1(
        reservation_group_id=hold_id,
        scope="run_budget_hold",
        run_id="run-1",
        budget_set_snapshot_id=budget_set.budget_set_snapshot_id,
        request_hash=request_hash(request),
        idempotency_key="run-hold:run-1",
        budget_reservation_ids=tuple(item.reservation_id for item in hold_members),
        status="reserved",
        revision=1,
        created_at=NOW,
    )
    with uow.begin() as transaction:
        for item in (budget, principal_budget, system_budget):
            transaction.cost.put_budget(item)
        transaction.cost.freeze_budget_set(budget_set, hold, hold_members)

    _, definition = _registry_and_definition()
    base = _run_record(definition)
    plan_body = {
        "agent_graph_version": _AGENT_GRAPH_VERSION,
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id=request.agent_node_id,
                prompt_version=request.prompt_version,
                tool_version=_AGENT_TOOL_VERSION,
                allowed_model_snapshots=tuple(item.model_snapshot for item in catalog.models),
            ),
        ),
        "model_catalog_version": catalog.catalog_version,
        "model_catalog_digest": catalog.catalog_digest,
        "routing_policy_version": policy.policy_version,
        "routing_policy_digest": policy.routing_policy_digest,
    }
    plan = ExecutionVersionPlanV1(
        **plan_body,
        plan_digest=execution_version_plan_digest(plan_body),
    )
    payload = base.payload.model_copy(
        update={
            "budget_set_snapshot_id": budget_set.budget_set_snapshot_id,
            "llm_execution_mode": "record",
            "execution_version_plan": plan,
            "cassette_artifact_id": None,
        }
    )
    run = base.model_copy(
        update={
            "run_id": "run-1",
            "revision": 3,
            "payload": payload,
            "payload_hash": canonical_payload_hash(payload),
            "budget_set_snapshot_id": budget_set.budget_set_snapshot_id,
            "run_budget_hold_group_id": hold_id,
            "next_event_seq": 4,
            "created_at": _iso(NOW),
            "updated_at": _iso(NOW),
            "queue_deadline_utc": _iso(NOW + timedelta(minutes=10)),
            "overall_deadline_utc": _iso(NOW + timedelta(hours=1)),
        }
    )
    attempt = RunAttempt(
        run_id=run.run_id,
        attempt_no=1,
        status="running",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
        started_at=_iso(NOW),
        attempt_deadline_utc=_iso(NOW + timedelta(minutes=30)),
    )
    lease = RunLease(
        lease_id="lease:run-1:1",
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        fencing_token=attempt.fencing_token,
        lease_version=1,
        owner_principal_id=WORKER.principal_id,
        acquired_at=_iso(NOW),
        heartbeat_at=_iso(NOW),
        expires_at=_iso(NOW + timedelta(minutes=30)),
        status="active",
    )
    with Session(engine) as session, session.begin():
        session.add(RunRow(**_run_values(run)))
        session.flush()
        session.add(RunAttemptRow(**_attempt_values(attempt)))
        session.flush()
        session.add(RunLeaseRow(**_lease_values(lease)))
        session.flush()
        events = (
            RunEvent(
                run_id=run.run_id,
                seq=1,
                event_type="run.queued",
                occurred_at=_iso(NOW),
                data_schema_version="run-queued@1",
                data=RunQueuedDataV1(
                    run_kind=run.kind,
                    queue_deadline_utc=run.queue_deadline_utc,
                    overall_deadline_utc=run.overall_deadline_utc,
                ),
            ),
            RunEvent(
                run_id=run.run_id,
                seq=2,
                event_type="attempt.leased",
                attempt_no=attempt.attempt_no,
                occurred_at=_iso(NOW),
                data_schema_version="attempt-leased@1",
                data=AttemptLeasedDataV1(
                    attempt_no=attempt.attempt_no,
                    lease_expires_at=lease.expires_at,
                ),
            ),
            RunEvent(
                run_id=run.run_id,
                seq=3,
                event_type="attempt.started",
                attempt_no=attempt.attempt_no,
                occurred_at=_iso(NOW),
                data_schema_version="attempt-started@1",
                data=AttemptStartedDataV1(
                    attempt_no=attempt.attempt_no,
                    started_at=attempt.started_at,
                    attempt_deadline_utc=attempt.attempt_deadline_utc,
                ),
            ),
        )
        session.add_all(RunEventRow(**_event_values(event)) for event in events)
    decision = _decision(
        request=request,
        catalog=catalog,
        policy=policy,
        budget_set_snapshot_id=budget_set.budget_set_snapshot_id,
    )
    with uow.begin() as transaction:
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)
        transaction.cost.put_routing_decision(decision)
    fence = AttemptWriteFence(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        expected_run_revision=run.revision,
        lease_id=lease.lease_id,
        fencing_token=attempt.fencing_token,
    )
    selected = _Harness(
        engine=engine,
        uow=uow,
        objects=objects,
        clock=clock,
        run=run,
        attempt=attempt,
        lease=lease,
        fence=fence,
        request=request,
        decision=decision,
        catalog=catalog,
        policy=policy,
        hold_id=hold_id,
        budget_id=budget.budget_id,
    )
    try:
        yield selected
    finally:
        engine.dispose()


def _router_result(
    decision: RoutingDecisionV1,
    *,
    source: str = "online",
    tokens: TokenUsageObservationV1 | None = None,
    transport_attempts: int = 1,
) -> M4RouterResultV1:
    return M4RouterResultV1(
        response_normalized="fixed",
        raw_response={"id": "response-1"},
        finish_reason="stop",
        tool_calls=(),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=20),
        token_usage=tokens
        or TokenUsageObservationV1(
            status="reported",
            input_tokens=10,
            output_tokens=2,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=12,
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
        execution_source=source,
        routing_decision_id=decision.decision_id,
        transport_attempt_count=transport_attempts,
        transport_retry_count=max(0, transport_attempts - 1),
    )


def _attempt_groups(harness: _Harness) -> tuple[ReservationGroupV1, ...]:
    with Session(harness.engine) as session:
        return SqlCostLedger(session, clock=harness.clock).list_attempt_reservation_groups(
            run_id=harness.run.run_id,
            attempt_no=harness.attempt.attempt_no,
        )


def _remaining_hold(harness: _Harness) -> dict[str, Decimal]:
    with Session(harness.engine) as session:
        return _amounts(
            SqlCostLedger(session, clock=harness.clock).remaining_hold_amounts(harness.hold_id)
        )


def test_each_transport_reserve_commits_a_distinct_group_before_use(harness: _Harness) -> None:
    initial = _remaining_hold(harness)

    first = harness.reserve(transport_attempt=1)
    visible_after_first = _attempt_groups(harness)
    second = harness.reserve(transport_attempt=2)
    visible_after_second = _attempt_groups(harness)

    assert first.reservation_group_id != second.reservation_group_id
    assert [(item.transport_attempt, item.status) for item in visible_after_first] == [
        (1, "reserved")
    ]
    assert sorted((item.transport_attempt, item.status) for item in visible_after_second) == [
        (1, "reserved"),
        (2, "reserved"),
    ]
    remaining = _remaining_hold(harness)
    assert all(
        remaining[dimension] < initial[dimension]
        for dimension in ("input_token", "output_token", "request", "wall_time_ns")
    )
    assert remaining["agent_step"] == initial["agent_step"]


def test_agent_step_reserve_and_reconcile_are_exact_and_route_free(
    harness: _Harness,
) -> None:
    gateway = harness.step_gateway()
    token = gateway.reserve_step(
        request_hash=request_hash(harness.request),
        execution_source="online",
        deadline_utc=NOW + timedelta(seconds=10),
        call_ordinal=1,
        agent_node_id=harness.request.agent_node_id,
    )

    assert isinstance(token, AgentStepReservationToken)
    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.get_reservation_group(token.reservation_group_id)
        members = ledger.list_budget_reservations(token.reservation_group_id)
    assert group is not None
    assert group.scope == "agent_step"
    assert group.transport_attempt is None
    assert group.request_hash == request_hash(harness.request)
    assert _amounts(members[0].reserved) == {"agent_step": Decimal(1)}
    assert {item.budget_id for item in members} == {
        "budget:run-1",
        "budget:principal:worker",
    }

    gateway.reconcile_step(reservation=token)

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        settled = ledger.get_reservation_group(token.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
        budget = ledger.get_budget(harness.budget_id)
    assert settled is not None and settled.status == "reconciled"
    assert len(usage) == 1
    assert usage[0].scope == "agent_step"
    assert usage[0].transport_attempt is None
    assert usage[0].routing_decision_kind is None
    assert usage[0].routing_decision_id is None
    assert usage[0].execution_source == "online"
    assert usage[0].wall_time_ns == 0
    assert budget is not None
    assert _amounts(budget.consumed) == {"agent_step": Decimal(1)}


def test_agent_step_fault_rolls_back_prior_call_settlement_in_shared_uow(
    harness: _Harness,
) -> None:
    call_gateway = harness.gateway()
    step_gateway = harness.step_gateway()
    call_token = harness.reserve(gateway=call_gateway)
    step_token = step_gateway.reserve_step(
        request_hash=request_hash(harness.request),
        execution_source="online",
        deadline_utc=NOW + timedelta(seconds=10),
        call_ordinal=1,
        agent_node_id=harness.request.agent_node_id,
    )
    corrupt_step_token = AgentStepReservationToken(
        reservation_group_id=step_token.reservation_group_id,
        request_hash=step_token.request_hash,
        execution_source=step_token.execution_source,
        call_ordinal=step_token.call_ordinal,
        agent_node_id="another-agent-node",
    )

    with pytest.raises(IntegrityViolation, match="CostLedger authority"):
        with harness.uow.begin() as transaction:
            call_gateway.reconcile_in_transaction(
                transaction=transaction,
                reservation=call_token,
                decision=harness.decision,
                result=_router_result(harness.decision),
                wall_time_ns=123_456,
            )
            step_gateway.reconcile_step_in_transaction(
                transaction=transaction,
                reservation=corrupt_step_token,
            )

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        call_group = ledger.get_reservation_group(call_token.reservation_group_id)
        step_group = ledger.get_reservation_group(step_token.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
    assert call_group is not None and call_group.status == "reserved"
    assert step_group is not None and step_group.status == "reserved"
    assert usage == ()


def test_record_publication_step_fault_leaves_no_shard_or_consumption(
    harness: _Harness,
) -> None:
    rendered_payload = canonical_json(harness.request.model_dump(mode="json")).encode("utf-8")
    stored = harness.objects.put_verified(rendered_payload)
    prompt = build_artifact_v2(
        kind="source_rendered",
        version_tuple=VersionTuple(
            prompt_version=harness.request.prompt_version,
            model_snapshot=None,
            agent_graph_version=_AGENT_GRAPH_VERSION,
            tool_version=_PROMPT_RENDERER_VERSION,
        ),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "source-rendered@1",
            "renderer_version": _PROMPT_RENDERER_VERSION,
            "agent_tool_version": _AGENT_TOOL_VERSION,
        },
        created_at=_iso(NOW),
    )
    link = RunIntermediateArtifactLinkV1(
        run_id=harness.run.run_id,
        attempt_no=harness.attempt.attempt_no,
        call_ordinal=1,
        route_ordinal=1,
        artifact_id=prompt.artifact_id,
        role="prompt_rendered",
        request_hash=request_hash(harness.request).removeprefix("sha256:"),
        fencing_token=harness.fence.fencing_token,
        published_at=_iso(NOW),
    )
    route = RunModelRouteLinkV1(
        run_id=link.run_id,
        attempt_no=link.attempt_no,
        call_ordinal=link.call_ordinal,
        route_ordinal=link.route_ordinal,
        prompt_artifact_id=link.artifact_id,
        request_hash=link.request_hash,
        routing_decision_kind="native",
        routing_decision_id=harness.decision.decision_id,
        fencing_token=link.fencing_token,
        published_at=link.published_at,
    )
    with harness.uow.begin() as transaction:
        transaction.object_bindings.bind_verified(stored.ref, stored.location, None)
        transaction.artifacts.put(prompt)
        transaction.runs.put_intermediate_link(link)
        transaction.runs.put_model_route_link(route)

    call_gateway = harness.gateway()
    step_gateway = harness.step_gateway()
    call_token = harness.reserve(gateway=call_gateway)
    step_token = step_gateway.reserve_step(
        request_hash=request_hash(harness.request),
        execution_source="online",
        deadline_utc=NOW + timedelta(seconds=10),
        call_ordinal=link.call_ordinal,
        agent_node_id=harness.request.agent_node_id,
    )
    result = _router_result(harness.decision)
    record = _record_from_result(
        request=harness.request,
        decision=harness.decision,
        result=result,
        recorded_at=NOW,
    )

    class _FailAfterStepSettlement:
        def reconcile_step_in_transaction(self, *, transaction, reservation):
            step_gateway.reconcile_step_in_transaction(
                transaction=transaction,
                reservation=reservation,
            )
            raise RuntimeError("fault after atomic agent-step settlement")

    publisher = WorkerResponseConsumptionPublisher(
        unit_of_work=harness.uow,
        run=harness.run,
        cost=call_gateway,
        object_store=harness.objects,
        clock=harness.clock,
        audit_chain_id="run:run-1",
    )

    with pytest.raises(RuntimeError, match="fault after atomic agent-step"):
        publisher.publish_response_consumption(
            fence=harness.fence,
            link=link,
            decision=harness.decision,
            result=result,
            record=record,
            reservation=call_token,
            step_cost=_FailAfterStepSettlement(),  # type: ignore[arg-type]
            step_reservation=step_token,
            wall_time_ns=123_456,
            actor=WORKER,
        )

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        call_group = ledger.get_reservation_group(call_token.reservation_group_id)
        step_group = ledger.get_reservation_group(step_token.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
        consumption = SqlRunRepository(session).get_model_response_consumption(
            harness.run.run_id,
            harness.attempt.attempt_no,
            link.call_ordinal,
            link.route_ordinal,
        )
        shards = tuple(
            session.scalars(select(ArtifactRow).where(ArtifactRow.kind == "cassette_bundle"))
        )
    assert call_group is not None and call_group.status == "reserved"
    assert step_group is not None and step_group.status == "reserved"
    assert usage == ()
    assert consumption is None
    assert shards == ()

    # This mirrors WorkerModelBridge's exception path: provider work was incurred,
    # so cost settles outside the rolled-back publication UoW without making the
    # response or staged shard authoritative.
    call_gateway.reconcile_usage(
        reservation=call_token,
        decision=harness.decision,
        result=result,
        wall_time_ns=123_456,
    )
    step_gateway.reconcile_step(reservation=step_token)
    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        assert len(ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)) == 2
        assert (
            SqlRunRepository(session).get_model_response_consumption(
                harness.run.run_id,
                harness.attempt.attempt_no,
                link.call_ordinal,
                link.route_ordinal,
            )
            is None
        )
        assert (
            tuple(session.scalars(select(ArtifactRow).where(ArtifactRow.kind == "cassette_bundle")))
            == ()
        )


def test_fallback_response_atomically_settles_first_route_agent_step(
    harness: _Harness,
) -> None:
    fallback_request = harness.request.model_copy(
        update={
            "model_snapshot": harness.request.model_snapshot.model_copy(
                update={"model": f"{harness.request.model_snapshot.model}-fallback"}
            )
        }
    )
    fallback_decision = _decision(
        request=fallback_request,
        catalog=harness.catalog,
        policy=harness.policy,
        budget_set_snapshot_id=harness.run.budget_set_snapshot_id,
        fallback_index=1,
    )
    harness.put_decision(fallback_decision)

    def prompt_route(
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
        route_ordinal: int,
    ) -> tuple[RunIntermediateArtifactLinkV1, RunModelRouteLinkV1, object, object]:
        payload = canonical_json(request.model_dump(mode="json")).encode("utf-8")
        stored = harness.objects.put_verified(payload)
        prompt = build_artifact_v2(
            kind="source_rendered",
            version_tuple=VersionTuple(
                prompt_version=request.prompt_version,
                model_snapshot=None,
                agent_graph_version=_AGENT_GRAPH_VERSION,
                tool_version=_PROMPT_RENDERER_VERSION,
            ),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "source-rendered@1",
                "renderer_version": _PROMPT_RENDERER_VERSION,
                "agent_tool_version": _AGENT_TOOL_VERSION,
            },
            created_at=_iso(NOW),
        )
        link = RunIntermediateArtifactLinkV1(
            run_id=harness.run.run_id,
            attempt_no=harness.attempt.attempt_no,
            call_ordinal=1,
            route_ordinal=route_ordinal,
            artifact_id=prompt.artifact_id,
            role="prompt_rendered",
            request_hash=request_hash(request).removeprefix("sha256:"),
            fencing_token=harness.fence.fencing_token,
            published_at=_iso(NOW),
        )
        route = RunModelRouteLinkV1(
            run_id=link.run_id,
            attempt_no=link.attempt_no,
            call_ordinal=link.call_ordinal,
            route_ordinal=link.route_ordinal,
            prompt_artifact_id=link.artifact_id,
            request_hash=link.request_hash,
            routing_decision_kind="native",
            routing_decision_id=decision.decision_id,
            fencing_token=link.fencing_token,
            published_at=link.published_at,
        )
        return link, route, prompt, stored

    primary_link, primary_route, primary_prompt, primary_stored = prompt_route(
        harness.request, harness.decision, 1
    )
    fallback_link, fallback_route, fallback_prompt, fallback_stored = prompt_route(
        fallback_request, fallback_decision, 2
    )
    with harness.uow.begin() as transaction:
        for prompt, stored in (
            (primary_prompt, primary_stored),
            (fallback_prompt, fallback_stored),
        ):
            transaction.object_bindings.bind_verified(stored.ref, stored.location, None)
            transaction.artifacts.put(prompt)
        transaction.runs.put_intermediate_link(primary_link)
        transaction.runs.put_model_route_link(primary_route)
        transaction.runs.put_intermediate_link(fallback_link)
        transaction.runs.put_model_route_link(fallback_route)

    call_gateway = harness.gateway()
    step_gateway = harness.step_gateway()
    step_token = step_gateway.reserve_step(
        request_hash=request_hash(harness.request),
        execution_source="online",
        deadline_utc=NOW + timedelta(seconds=10),
        call_ordinal=1,
        agent_node_id=harness.request.agent_node_id,
    )
    call_token = harness.reserve(
        decision=fallback_decision,
        model_request=fallback_request,
        route_ordinal=2,
        gateway=call_gateway,
    )
    result = _router_result(fallback_decision)
    record = _record_from_result(
        request=fallback_request,
        decision=fallback_decision,
        result=result,
        recorded_at=NOW,
    )
    WorkerResponseConsumptionPublisher(
        unit_of_work=harness.uow,
        run=harness.run,
        cost=call_gateway,
        object_store=harness.objects,
        clock=harness.clock,
        audit_chain_id="run:run-1",
    ).publish_response_consumption(
        fence=harness.fence,
        link=fallback_link,
        decision=fallback_decision,
        result=result,
        record=record,
        reservation=call_token,
        step_cost=step_gateway,
        step_reservation=step_token,
        wall_time_ns=123_456,
        actor=WORKER,
    )

    with Session(harness.engine) as session:
        consumption = SqlRunRepository(session).get_model_response_consumption(
            harness.run.run_id, 1, 1, 2
        )
        usage = SqlCostLedger(session, clock=harness.clock).list_usage(
            run_id=harness.run.run_id, attempt_no=1
        )
    assert consumption is not None and consumption.route_ordinal == 2
    assert len(usage) == 2


def test_stale_agent_step_rejects_without_partial_reserve(harness: _Harness) -> None:
    gateway = harness.step_gateway(
        fence=harness.fence.model_copy(
            update={"expected_run_revision": harness.fence.expected_run_revision + 1}
        )
    )
    before = _remaining_hold(harness)

    with pytest.raises((Conflict, InvalidStateTransition, QuotaExceeded)):
        gateway.reserve_step(
            request_hash=request_hash(harness.request),
            execution_source="online",
            deadline_utc=NOW + timedelta(seconds=10),
            call_ordinal=1,
            agent_node_id=harness.request.agent_node_id,
        )

    assert _attempt_groups(harness) == ()
    assert _remaining_hold(harness) == before


def test_conservative_provider_settles_stranded_agent_step_without_route(
    harness: _Harness,
) -> None:
    token = harness.step_gateway().reserve_step(
        request_hash=request_hash(harness.request),
        execution_source="cassette_replay",
        deadline_utc=NOW + timedelta(seconds=10),
        call_ordinal=1,
        agent_node_id=harness.request.agent_node_id,
    )
    with Session(harness.engine) as session, session.begin():
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.hold_unknown_group(token.reservation_group_id)
        members = ledger.list_budget_reservations(group.reservation_group_id)
        usage = WorkerConservativeAttemptUsageProvider(ledger=ledger).conservative_usage(
            group=group,
            reservations=members,
            recorded_at=NOW + timedelta(seconds=1),
        )
        ledger.settle_unknown_group(group.reservation_group_id, usage)

    assert usage.scope == "agent_step"
    assert usage.execution_source == "cassette_replay"
    assert usage.routing_decision_id is None
    assert usage.transport_attempt is None
    assert usage.wall_time_ns == 0


def test_actual_usage_reconciles_exact_typed_dimensions(harness: _Harness) -> None:
    reservation = harness.reserve()
    harness.gateway().reconcile_usage(
        reservation=reservation,
        decision=harness.decision,
        result=_router_result(harness.decision),
        wall_time_ns=123_456,
    )

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.get_reservation_group(reservation.reservation_group_id)
        members = ledger.list_budget_reservations(reservation.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
        budget = ledger.get_budget(harness.budget_id)
    assert group is not None and group.status == "reconciled"
    assert len(usage) == 1
    assert usage[0].token_usage.input_tokens == 10
    assert usage[0].token_usage.output_tokens == 2
    assert usage[0].wall_time_ns == 123_456
    assert {item.budget_id for item in members} == {
        "budget:run-1",
        "budget:system:worker",
    }
    assert {
        "cache_read_token",
        "cache_write_token",
    }.issubset(
        _amounts(next(item for item in members if item.budget_id == harness.budget_id).reserved)
    )
    assert budget is not None
    assert _amounts(budget.consumed) == {
        "input_token": Decimal(10),
        "output_token": Decimal(2),
        "request": Decimal(1),
        "wall_time_ns": Decimal(123_456),
    }


def test_failed_transport_settles_at_reserved_upper_bound(harness: _Harness) -> None:
    reservation = harness.reserve()
    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        members = ledger.list_budget_reservations(reservation.reservation_group_id)
        maxima = _amounts(
            next(item for item in members if item.budget_id == harness.budget_id).reserved
        )

    harness.gateway().settle_failed_transport(
        reservation=reservation,
        decision=harness.decision,
        wall_time_ns=1,
    )

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.get_reservation_group(reservation.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
    assert group is not None and group.status == "conservatively_settled"
    assert len(usage) == 1
    assert usage[0].token_usage.input_tokens == int(maxima["input_token"])
    assert usage[0].token_usage.output_tokens == int(maxima["output_token"])
    assert usage[0].wall_time_ns == int(maxima["wall_time_ns"])


@pytest.mark.parametrize("terminal_status", ["reconciled", "released", "conservative"])
def test_terminal_call_reservation_history_is_never_fresh_execution_permission(
    harness: _Harness,
    terminal_status: str,
) -> None:
    gateway = harness.gateway()
    reservation = harness.reserve(gateway=gateway)
    if terminal_status == "reconciled":
        gateway.reconcile_usage(
            reservation=reservation,
            decision=harness.decision,
            result=_router_result(harness.decision),
            wall_time_ns=1,
        )
    elif terminal_status == "released":
        gateway.cancel_reservation(reservation=reservation)
    else:
        gateway.settle_failed_transport(
            reservation=reservation,
            decision=harness.decision,
            wall_time_ns=1,
        )

    with pytest.raises(InvalidStateTransition, match="not fresh execution permission"):
        harness.reserve(gateway=gateway)

    groups = _attempt_groups(harness)
    assert len(groups) == 1
    assert groups[0].reservation_group_id == reservation.reservation_group_id


@pytest.mark.parametrize(
    "authority_loss", ["stale", "cancelled", "call_deadline", "attempt_deadline"]
)
def test_stale_cancelled_or_expired_authority_rejects_without_partial_reserve(
    harness: _Harness,
    authority_loss: str,
) -> None:
    gateway = harness.gateway()
    deadline = NOW + timedelta(seconds=10)
    if authority_loss == "stale":
        gateway = harness.gateway(
            fence=harness.fence.model_copy(
                update={"expected_run_revision": harness.fence.expected_run_revision + 1}
            )
        )
    elif authority_loss == "cancelled":
        with Session(harness.engine) as session, session.begin():
            session.execute(
                update(RunRow)
                .where(RunRow.run_id == harness.run.run_id)
                .values(
                    cancel_requested_at=_iso(NOW),
                    cancel_requested_by=HUMAN.model_dump(mode="json"),
                )
            )
    elif authority_loss == "call_deadline":
        deadline = NOW
    else:
        with Session(harness.engine) as session, session.begin():
            session.execute(
                update(RunAttemptRow)
                .where(
                    RunAttemptRow.run_id == harness.run.run_id,
                    RunAttemptRow.attempt_no == harness.attempt.attempt_no,
                )
                .values(attempt_deadline_utc=_iso(NOW))
            )

    before = _remaining_hold(harness)
    with pytest.raises((Conflict, InvalidStateTransition, QuotaExceeded)):
        harness.reserve(gateway=gateway, deadline=deadline)

    assert _attempt_groups(harness) == ()
    assert _remaining_hold(harness) == before


def test_unknown_settlement_provider_uses_exact_persisted_route_and_upper_bound(
    harness: _Harness,
) -> None:
    reservation = harness.reserve()
    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.get_reservation_group(reservation.reservation_group_id)
        assert group is not None
        members = ledger.list_budget_reservations(group.reservation_group_id)
        usage = WorkerConservativeAttemptUsageProvider(ledger=ledger).conservative_usage(
            group=group,
            reservations=members,
            recorded_at=NOW + timedelta(seconds=1),
        )

    maxima = _amounts(
        next(item for item in members if item.budget_id == harness.budget_id).reserved
    )
    assert usage.routing_decision_id == harness.decision.decision_id
    assert usage.token_usage.input_tokens == int(maxima["input_token"])
    assert usage.token_usage.output_tokens == int(maxima["output_token"])
    assert usage.wall_time_ns == int(maxima["wall_time_ns"])


def test_full_response_cache_reconciles_zero_token_usage_without_conservative_charge(
    harness: _Harness,
) -> None:
    request = _request("cache-hit")
    decision = _decision(
        request=request,
        catalog=harness.catalog,
        policy=harness.policy,
        budget_set_snapshot_id=harness.run.budget_set_snapshot_id,
        execution_source="full_response_cache",
    )
    harness.put_decision(decision)
    reservation = harness.reserve(decision=decision, model_request=request)

    harness.gateway().reconcile_usage(
        reservation=reservation,
        decision=decision,
        result=_router_result(
            decision,
            source="full_response_cache",
            tokens=TokenUsageObservationV1(status="reported", total_tokens=0),
            transport_attempts=0,
        ),
        wall_time_ns=0,
    )

    with Session(harness.engine) as session:
        ledger = SqlCostLedger(session, clock=harness.clock)
        group = ledger.get_reservation_group(reservation.reservation_group_id)
        usage = ledger.list_usage(run_id=harness.run.run_id, attempt_no=1)
        budget = ledger.get_budget(harness.budget_id)
    assert group is not None and group.status == "reconciled"
    assert len(usage) == 1
    assert usage[0].token_usage.total_tokens == 0
    assert budget is not None
    consumed = _amounts(budget.consumed)
    assert consumed.get("input_token", Decimal(0)) == 0
    assert consumed.get("output_token", Decimal(0)) == 0
