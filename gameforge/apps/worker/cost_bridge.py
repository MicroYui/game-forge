"""Persistent reserve-before-use accounting for worker model transports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.cost import (
    BudgetReservationV1,
    CacheHitObservationV1,
    CostAmountV1,
    LatencyObservationV1,
    MonetaryObservationV1,
    PriceBook,
    PriceQuoteV1,
    PriceUnavailableV1,
    ReservationGroupV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import (
    AttemptFenceStateRejected,
    IntegrityViolation,
    InvalidStateTransition,
    QuotaExceeded,
)
from gameforge.contracts.jobs import RunAttempt, RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.model_router import ModelRequestV1, ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.lifecycle import (
    AttemptWriteFence,
    validate_attempt_write_fence,
)
from gameforge.runtime.model_router.m4_router import M4RouterResultV1
from gameforge.runtime.cost.price_book import UnavailablePriceBook


ModelRoutingDecision = RoutingDecisionV1 | LegacyImportRoutingDecisionV1
ModelRoutingRequest = ModelRequestV1 | ModelRequestV2


class UnitOfWork(Protocol):
    def begin(self) -> object: ...


@dataclass(frozen=True, slots=True)
class CallReservationToken:
    reservation_group_id: str
    decision_id: str
    call_ordinal: int
    route_ordinal: int
    transport_attempt: int


@dataclass(frozen=True, slots=True)
class AgentStepReservationToken:
    """Exact admission for one logical, plan-bound Agent node invocation."""

    reservation_group_id: str
    request_hash: str
    execution_source: str
    call_ordinal: int
    agent_node_id: str


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}:sha256:{canonical_sha256(value)}"


def _utc(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("worker cost clock must return UTC")
    return value.astimezone(UTC)


def _parse_utc(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IntegrityViolation(f"{label} is not UTC")
    return parsed.astimezone(UTC)


def _decision_id_from_group(group: ReservationGroupV1) -> str:
    prefix = "model-route:"
    marker = ":call:"
    value = group.idempotency_key
    if not value.startswith(prefix) or marker not in value:
        raise IntegrityViolation("attempt-call idempotency does not bind a routing decision")
    decision_id, suffix = value[len(prefix) :].split(marker, 1)
    if not decision_id or not suffix:
        raise IntegrityViolation("attempt-call routing idempotency is malformed")
    return decision_id


def _amount(
    identity: CostAmountV1,
    value: int | Decimal,
) -> CostAmountV1:
    return identity.model_copy(update={"value": Decimal(value)})


class WorkerConservativeAttemptUsageProvider:
    """Settle stranded calls at their exact reserved upper-bound dimensions."""

    def __init__(self, *, ledger: object, price_book: PriceBook | None = None) -> None:
        self._ledger = ledger
        self._price_book = price_book or UnavailablePriceBook()

    def conservative_usage(
        self,
        *,
        group: ReservationGroupV1,
        reservations: tuple[BudgetReservationV1, ...],
        recorded_at: datetime,
    ) -> UsageEntryV1:
        if group.scope == "agent_step":
            return _agent_step_usage(
                group=group,
                reservations=reservations,
                execution_source=_step_execution_source(group),
                recorded_at=recorded_at,
                kind="conservative",
            )
        if group.scope != "attempt_call":
            raise IntegrityViolation("stranded cost group has an unsupported scope")
        decision_id = _decision_id_from_group(group)
        decision = self._ledger.get_routing_decision(decision_id)  # type: ignore[attr-defined]
        if decision is None:
            decision = self._ledger.get_legacy_import_routing_decision(  # type: ignore[attr-defined]
                decision_id
            )
        if not isinstance(decision, (RoutingDecisionV1, LegacyImportRoutingDecisionV1)):
            raise IntegrityViolation("stranded call has no exact RoutingDecision authority")
        if decision.request_hash != group.request_hash or (
            isinstance(decision, RoutingDecisionV1)
            and (decision.run_id != group.run_id or decision.attempt_no != group.attempt_no)
        ):
            raise IntegrityViolation("stranded call differs from its RoutingDecision")
        quote = _price_quote_for_reservations(
            ledger=self._ledger,
            price_book=self._price_book,
            decision=decision,
            group=group,
            reservations=reservations,
        )
        return _conservative_usage(
            group=group,
            reservations=reservations,
            decision=decision,
            recorded_at=recorded_at,
            price_quote=quote,
        )


class WorkerAgentStepCostGateway:
    """Reserve and settle one discrete Agent node step before its first route.

    A step is the logical invocation identified by ``agent_node_id`` and
    ``call_ordinal``. Provider fallback and transport retry remain child
    ``attempt_call`` groups and therefore never multiply this discrete charge.
    The step group reserves only the exact ``agent_step=1`` dimension; transport
    groups retain provider/request/token/wall-time accounting, avoiding a nested
    double reservation of provider wall time.
    """

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWork,
        run: RunRecord,
        attempt: RunAttempt,
        fence: AttemptWriteFence,
        actor: AuditActor,
        clock: UtcClock,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._run = run
        self._attempt = attempt
        self._fence = fence
        self._actor = actor
        self._clock = clock

    def reserve_step(
        self,
        *,
        request_hash: str,
        execution_source: str,
        deadline_utc: datetime,
        call_ordinal: int,
        agent_node_id: str,
    ) -> AgentStepReservationToken:
        if execution_source not in {
            "online",
            "full_response_cache",
            "cassette_replay",
        }:
            raise IntegrityViolation("agent-step execution source is invalid")
        if call_ordinal < 1 or not agent_node_id:
            raise IntegrityViolation("agent-step logical call identity is invalid")
        now = _utc(self._clock)
        if deadline_utc.tzinfo is None or deadline_utc.utcoffset() != UTC.utcoffset(deadline_utc):
            raise IntegrityViolation("agent-step reservation deadline must be UTC")
        deadline = deadline_utc.astimezone(UTC)
        if now >= deadline:
            raise QuotaExceeded("agent-step reservation deadline is exhausted")

        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            runs = transaction.runs
            ledger = transaction.cost
            run = runs.get(self._run.run_id)
            attempt = runs.get_attempt(self._run.run_id, self._attempt.attempt_no)
            lease = runs.get_current_lease(self._run.run_id)
            if (
                not isinstance(run, RunRecord)
                or not isinstance(attempt, RunAttempt)
                or lease is None
            ):
                raise IntegrityViolation("agent-step reservation authority disappeared")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=self._fence,
                actor=self._actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if run.cancel_requested_at is not None:
                raise AttemptFenceStateRejected("cancel-requested Run cannot start an agent step")
            parent = ledger.get_reservation_group(run.run_budget_hold_group_id)
            if (
                parent is None
                or parent.scope != "run_budget_hold"
                or parent.status != "reserved"
                or parent.run_id != run.run_id
                or parent.budget_set_snapshot_id != run.budget_set_snapshot_id
            ):
                raise IntegrityViolation("agent-step reservation has no active exact Run hold")
            parent_members = ledger.list_budget_reservations(parent.reservation_group_id)
            identity = {
                "run_id": run.run_id,
                "attempt_no": attempt.attempt_no,
                "call_ordinal": call_ordinal,
                "agent_node_id": agent_node_id,
                "request_hash": request_hash,
                "execution_source": execution_source,
                "fencing_token": attempt.fencing_token,
            }
            group_id = _stable_id("agent-step-reservation", identity)
            members: list[BudgetReservationV1] = []
            for parent_member in parent_members:
                step_identity = next(
                    (
                        amount
                        for amount in parent_member.reserved
                        if amount.dimension == "agent_step"
                    ),
                    None,
                )
                if step_identity is None:
                    continue
                if step_identity.value < 1:
                    raise QuotaExceeded(
                        "agent-step upper bound exceeds its frozen Run hold",
                        budget_id=parent_member.budget_id,
                    )
                members.append(
                    BudgetReservationV1(
                        reservation_id=_stable_id(
                            "agent-step-budget-reservation",
                            {**identity, "budget_id": parent_member.budget_id},
                        ),
                        reservation_group_id=group_id,
                        budget_id=parent_member.budget_id,
                        reserved=(_amount(step_identity, 1),),
                        status="reserved",
                        revision=1,
                    )
                )
            if not members:
                raise IntegrityViolation(
                    "Run hold has no budget governing the agent-step cost dimension"
                )
            idempotency_digest = _stable_id("identity", identity)
            group = ReservationGroupV1(
                reservation_group_id=group_id,
                scope="agent_step",
                run_id=run.run_id,
                budget_set_snapshot_id=run.budget_set_snapshot_id,
                parent_hold_group_id=run.run_budget_hold_group_id,
                attempt_no=attempt.attempt_no,
                request_hash=request_hash,
                fencing_token=attempt.fencing_token,
                idempotency_key=(f"agent-step:{execution_source}:{idempotency_digest}"),
                budget_reservation_ids=tuple(item.reservation_id for item in members),
                status="reserved",
                revision=1,
                created_at=now,
                expires_at=deadline,
            )
            retained = ledger.reserve_many(group, tuple(members))
            if retained.status != "reserved":
                raise InvalidStateTransition(
                    "terminal agent-step reservation history is not fresh execution permission"
                )
        return AgentStepReservationToken(
            reservation_group_id=group_id,
            request_hash=request_hash,
            execution_source=execution_source,
            call_ordinal=call_ordinal,
            agent_node_id=agent_node_id,
        )

    def reconcile_step(self, *, reservation: object) -> None:
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            self.reconcile_step_in_transaction(
                transaction=transaction,
                reservation=reservation,
            )

    def reconcile_step_in_transaction(
        self,
        *,
        transaction: object,
        reservation: object,
    ) -> ReservationGroupV1:
        """Settle a step inside the caller's response-publication UoW.

        The live/RECORD response publisher uses this surface so the step charge,
        call charge, optional shard, response-consumption row, and audit record
        share one commit boundary.  The standalone ``reconcile_step`` wrapper is
        retained for incurred no-response/exception paths.
        """

        if not isinstance(reservation, AgentStepReservationToken):
            raise IntegrityViolation("unknown agent-step reservation token")
        identity = {
            "run_id": self._run.run_id,
            "attempt_no": self._attempt.attempt_no,
            "call_ordinal": reservation.call_ordinal,
            "agent_node_id": reservation.agent_node_id,
            "request_hash": reservation.request_hash,
            "execution_source": reservation.execution_source,
            "fencing_token": self._fence.fencing_token,
        }
        expected_group_id = _stable_id("agent-step-reservation", identity)
        expected_idempotency_key = (
            f"agent-step:{reservation.execution_source}:{_stable_id('identity', identity)}"
        )
        ledger = transaction.cost  # type: ignore[attr-defined]
        group = ledger.get_reservation_group(reservation.reservation_group_id)
        if (
            not isinstance(group, ReservationGroupV1)
            or reservation.reservation_group_id != expected_group_id
            or group.reservation_group_id != expected_group_id
            or group.scope != "agent_step"
            or group.run_id != self._run.run_id
            or group.attempt_no != self._attempt.attempt_no
            or group.budget_set_snapshot_id != self._run.budget_set_snapshot_id
            or group.parent_hold_group_id != self._run.run_budget_hold_group_id
            or group.request_hash != reservation.request_hash
            or group.fencing_token != self._fence.fencing_token
            or group.transport_attempt is not None
            or group.idempotency_key != expected_idempotency_key
            or _step_execution_source(group) != reservation.execution_source
        ):
            raise IntegrityViolation(
                "agent-step reservation token differs from CostLedger authority"
            )
        members = ledger.list_budget_reservations(group.reservation_group_id)
        usage = _agent_step_usage(
            group=group,
            reservations=members,
            execution_source=reservation.execution_source,
            recorded_at=_utc(self._clock),
            kind="actual",
        )
        settled = ledger.reconcile_group(usage)
        if settled.status != "reconciled":
            raise IntegrityViolation("agent-step usage did not reconcile exactly")
        return settled


class WorkerCallCostGateway:
    """Open a fresh UoW for every reserve, release, and incurred-usage settlement."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWork,
        run: RunRecord,
        attempt: RunAttempt,
        fence: AttemptWriteFence,
        actor: AuditActor,
        clock: UtcClock,
        price_book: PriceBook | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._run = run
        self._attempt = attempt
        self._fence = fence
        self._actor = actor
        self._clock = clock
        self._price_book = price_book or UnavailablePriceBook()

    def reserve_call(
        self,
        *,
        decision: ModelRoutingDecision,
        model_request: ModelRoutingRequest,
        deadline_utc: datetime,
        call_ordinal: int,
        route_ordinal: int,
        transport_attempt: int,
    ) -> CallReservationToken:
        now = _utc(self._clock)
        if deadline_utc.tzinfo is None or deadline_utc.utcoffset() != UTC.utcoffset(deadline_utc):
            raise IntegrityViolation("model reservation deadline must be UTC")
        deadline = deadline_utc.astimezone(UTC)
        if now >= deadline:
            raise QuotaExceeded("model reservation deadline is exhausted")
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            runs = transaction.runs
            ledger = transaction.cost
            run = runs.get(self._run.run_id)
            attempt = runs.get_attempt(self._run.run_id, self._attempt.attempt_no)
            lease = runs.get_current_lease(self._run.run_id)
            if (
                not isinstance(run, RunRecord)
                or not isinstance(attempt, RunAttempt)
                or lease is None
            ):
                raise IntegrityViolation("model reservation authority disappeared")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=self._fence,
                actor=self._actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if run.cancel_requested_at is not None:
                raise AttemptFenceStateRejected(
                    "cancel-requested Run cannot start a model reservation"
                )
            self._validate_decision(
                run=run,
                attempt=attempt,
                decision=decision,
                model_request=model_request,
                ledger=ledger,
            )
            descriptor = self._descriptor(ledger=ledger, decision=decision)
            upper_bounds = self._upper_bounds(
                model_request=model_request,
                descriptor=descriptor,
                now=now,
                deadline=deadline,
            )
            parent = ledger.get_reservation_group(run.run_budget_hold_group_id)
            if (
                parent is None
                or parent.scope != "run_budget_hold"
                or parent.status != "reserved"
                or parent.run_id != run.run_id
                or parent.budget_set_snapshot_id != run.budget_set_snapshot_id
            ):
                raise IntegrityViolation("model reservation has no active exact Run hold")
            parent_members = ledger.list_budget_reservations(parent.reservation_group_id)
            monetary_identities = tuple(
                amount
                for member in parent_members
                for amount in member.reserved
                if amount.dimension == "monetary"
            )
            if monetary_identities:
                quote = self._price_book.lookup(
                    descriptor.provider,
                    decision.model_snapshot,
                    now,
                )
                if isinstance(quote, PriceUnavailableV1):
                    raise IntegrityViolation(
                        "monetary call budget requires an exact retained price quote",
                        reason_code=quote.reason_code,
                    )
                if not isinstance(quote, PriceQuoteV1):
                    raise IntegrityViolation("price-book authority returned an invalid quote")
                _validate_price_quote(
                    quote,
                    provider=descriptor.provider,
                    model_snapshot=decision.model_snapshot,
                    observed_at=now,
                )
                if any(item.currency != quote.currency for item in monetary_identities):
                    raise IntegrityViolation(
                        "price quote currency differs from an applicable monetary budget"
                    )
                upper_bounds["monetary"] = _quoted_amount(
                    quote,
                    input_tokens=upper_bounds.get("input_token", 0),
                    output_tokens=upper_bounds.get("output_token", 0),
                    cache_read_tokens=upper_bounds.get("cache_read_token", 0),
                    cache_write_tokens=upper_bounds.get("cache_write_token", 0),
                )
            identity = {
                "run_id": run.run_id,
                "attempt_no": attempt.attempt_no,
                "call_ordinal": call_ordinal,
                "route_ordinal": route_ordinal,
                "transport_attempt": transport_attempt,
                "decision_id": decision.decision_id,
                "request_hash": decision.request_hash,
                "fencing_token": attempt.fencing_token,
            }
            group_id = _stable_id("call-reservation", identity)
            members: list[BudgetReservationV1] = []
            for parent_member in parent_members:
                retained = {item.dimension: item for item in parent_member.reserved}
                projected: list[CostAmountV1] = []
                for dimension, value in upper_bounds.items():
                    parent_identity = retained.get(dimension)
                    if parent_identity is None:
                        continue
                    if Decimal(value) > parent_identity.value:
                        raise QuotaExceeded(
                            "model call upper bound exceeds its frozen Run hold",
                            budget_id=parent_member.budget_id,
                            dimension=dimension,
                        )
                    projected.append(_amount(parent_identity, value))
                if not projected:
                    continue
                members.append(
                    BudgetReservationV1(
                        reservation_id=_stable_id(
                            "call-budget-reservation",
                            {**identity, "budget_id": parent_member.budget_id},
                        ),
                        reservation_group_id=group_id,
                        budget_id=parent_member.budget_id,
                        reserved=tuple(projected),
                        status="reserved",
                        revision=1,
                    )
                )
            if not members:
                raise IntegrityViolation(
                    "Run hold has no budget governing any model-call cost dimension"
                )
            group = ReservationGroupV1(
                reservation_group_id=group_id,
                scope="attempt_call",
                run_id=run.run_id,
                budget_set_snapshot_id=run.budget_set_snapshot_id,
                parent_hold_group_id=run.run_budget_hold_group_id,
                attempt_no=attempt.attempt_no,
                request_hash=decision.request_hash,
                transport_attempt=transport_attempt,
                fencing_token=attempt.fencing_token,
                idempotency_key=(
                    f"model-route:{decision.decision_id}:call:{call_ordinal}:"
                    f"route:{route_ordinal}:transport:{transport_attempt}"
                ),
                budget_reservation_ids=tuple(item.reservation_id for item in members),
                status="reserved",
                revision=1,
                created_at=now,
                expires_at=deadline,
            )
            retained_group = ledger.reserve_many(group, tuple(members))
            if retained_group.reservation_group_id != group_id:
                raise IntegrityViolation("CostLedger retained another reservation group")
            if retained_group.status != "reserved":
                raise InvalidStateTransition(
                    "terminal model reservation history is not fresh execution permission"
                )
        return CallReservationToken(
            reservation_group_id=group_id,
            decision_id=decision.decision_id,
            call_ordinal=call_ordinal,
            route_ordinal=route_ordinal,
            transport_attempt=transport_attempt,
        )

    def reconcile_usage(
        self,
        *,
        reservation: object,
        decision: ModelRoutingDecision,
        result: M4RouterResultV1,
        wall_time_ns: int,
    ) -> None:
        token = self._token(reservation, decision=decision)
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            self.reconcile_in_transaction(
                transaction=transaction,
                reservation=token,
                decision=decision,
                result=result,
                wall_time_ns=wall_time_ns,
            )

    def reconcile_in_transaction(
        self,
        *,
        transaction: object,
        reservation: object,
        decision: ModelRoutingDecision,
        result: M4RouterResultV1,
        wall_time_ns: int,
    ) -> ReservationGroupV1:
        """Reconcile inside a response publisher's shard+consumption UoW."""

        token = self._token(reservation, decision=decision)
        ledger = transaction.cost  # type: ignore[attr-defined]
        group, members = self._load_group(ledger=ledger, token=token)
        retained_decision = (
            ledger.get_routing_decision(decision.decision_id)
            if isinstance(decision, RoutingDecisionV1)
            else ledger.get_legacy_import_routing_decision(decision.decision_id)
        )
        if retained_decision != decision:
            raise IntegrityViolation("usage RoutingDecision authority changed")
        usage = _actual_usage(
            group=group,
            reservations=members,
            decision=decision,
            result=result,
            wall_time_ns=wall_time_ns,
            recorded_at=_utc(self._clock),
            price_quote=self._usage_quote(
                ledger=ledger,
                group=group,
                reservations=members,
                decision=decision,
            ),
        )
        settled = ledger.reconcile_group(usage)
        if settled.status == "held_unknown":
            conservative = _conservative_usage(
                group=settled,
                reservations=members,
                decision=decision,
                recorded_at=_utc(self._clock),
                price_quote=self._usage_quote(
                    ledger=ledger,
                    group=group,
                    reservations=members,
                    decision=decision,
                ),
            )
            settled = ledger.settle_unknown_group(
                settled.reservation_group_id,
                conservative,
            )
        return settled

    def settle_failed_transport(
        self,
        *,
        reservation: object,
        decision: ModelRoutingDecision,
        wall_time_ns: int,
    ) -> None:
        del wall_time_ns  # Unknown provider-side usage settles at the reserved upper bound.
        token = self._token(reservation, decision=decision)
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            ledger = transaction.cost
            group, members = self._load_group(ledger=ledger, token=token)
            if group.status == "reserved":
                group = ledger.hold_unknown_group(group.reservation_group_id)
            if group.status == "held_unknown":
                ledger.settle_unknown_group(
                    group.reservation_group_id,
                    _conservative_usage(
                        group=group,
                        reservations=members,
                        decision=decision,
                        recorded_at=_utc(self._clock),
                        price_quote=self._usage_quote(
                            ledger=ledger,
                            group=group,
                            reservations=members,
                            decision=decision,
                        ),
                    ),
                )

    def cancel_reservation(self, *, reservation: object) -> None:
        if not isinstance(reservation, CallReservationToken):
            raise IntegrityViolation("unknown model reservation token")
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            group = transaction.cost.get_reservation_group(  # type: ignore[attr-defined]
                reservation.reservation_group_id
            )
            if group is None:
                raise IntegrityViolation("cancelled model reservation disappeared")
            if group.status == "reserved":
                transaction.cost.release_unused_group(group.reservation_group_id)  # type: ignore[attr-defined]
            elif group.status != "released":
                raise IntegrityViolation("used model reservation cannot be cancelled")

    @staticmethod
    def _token(
        reservation: object,
        *,
        decision: ModelRoutingDecision,
    ) -> CallReservationToken:
        if not isinstance(reservation, CallReservationToken):
            raise IntegrityViolation("unknown model reservation token")
        if reservation.decision_id != decision.decision_id:
            raise IntegrityViolation("model reservation token belongs to another decision")
        return reservation

    @staticmethod
    def _load_group(*, ledger: object, token: CallReservationToken):
        group = ledger.get_reservation_group(token.reservation_group_id)  # type: ignore[attr-defined]
        if (
            not isinstance(group, ReservationGroupV1)
            or group.transport_attempt != token.transport_attempt
            or _decision_id_from_group(group) != token.decision_id
        ):
            raise IntegrityViolation("model reservation token differs from CostLedger authority")
        members = ledger.list_budget_reservations(group.reservation_group_id)  # type: ignore[attr-defined]
        return group, members

    @staticmethod
    def _validate_decision(
        *,
        run: RunRecord,
        attempt: RunAttempt,
        decision: ModelRoutingDecision,
        model_request: ModelRoutingRequest,
        ledger: object,
    ) -> None:
        if decision.request_hash != request_hash(model_request):
            raise IntegrityViolation("model reservation request differs from its route authority")
        if isinstance(decision, RoutingDecisionV1):
            if (
                ledger.get_routing_decision(decision.decision_id) != decision  # type: ignore[attr-defined]
                or decision.run_id != run.run_id
                or decision.attempt_no != attempt.attempt_no
                or decision.budget_set_snapshot_id != run.budget_set_snapshot_id
            ):
                raise IntegrityViolation(
                    "model reservation differs from its exact native route authority"
                )
            return
        plan = run.payload.execution_version_plan
        if (
            run.payload.llm_execution_mode != "replay"
            or ledger.get_legacy_import_routing_decision(decision.decision_id)  # type: ignore[attr-defined]
            != decision
            or decision.agent_node_id != model_request.agent_node_id
            or decision.model_snapshot != canonical_model_snapshot_id(model_request.model_snapshot)
            or plan is None
            or decision.model_catalog_version != plan.model_catalog_version
            or decision.model_catalog_digest != plan.model_catalog_digest
        ):
            raise IntegrityViolation(
                "model reservation differs from its verified legacy route authority"
            )

    @staticmethod
    def _descriptor(*, ledger: object, decision: ModelRoutingDecision):
        catalog = ledger.get_model_catalog(  # type: ignore[attr-defined]
            (
                decision.catalog_version
                if isinstance(decision, RoutingDecisionV1)
                else decision.model_catalog_version
            ),
            (
                decision.catalog_digest
                if isinstance(decision, RoutingDecisionV1)
                else decision.model_catalog_digest
            ),
        )
        if catalog is None:
            raise IntegrityViolation("model reservation exact catalog is unavailable")
        descriptor = next(
            (item for item in catalog.models if item.model_snapshot == decision.model_snapshot),
            None,
        )
        if descriptor is None or descriptor.status != "active":
            raise IntegrityViolation("model reservation descriptor is unavailable")
        return descriptor

    @staticmethod
    def _upper_bounds(
        *,
        model_request: ModelRoutingRequest,
        descriptor: object,
        now: datetime,
        deadline: datetime,
    ) -> dict[str, int | Decimal]:
        context_limit = int(getattr(descriptor, "context_limit"))
        descriptor_output = int(getattr(descriptor, "max_output_tokens"))
        requested_output = model_request.params.get(
            "max_output_tokens",
            model_request.params.get("max_tokens", descriptor_output),
        )
        if (
            isinstance(requested_output, bool)
            or not isinstance(requested_output, int)
            or requested_output < 1
            or requested_output > descriptor_output
        ):
            raise IntegrityViolation("model request has an invalid output-token bound")
        rendered_bytes = len(canonical_json(model_request.model_dump(mode="json")).encode())
        input_bound = min(context_limit, max(1, rendered_bytes))
        wall_time_ns = max(1, int((deadline - now).total_seconds() * 1_000_000_000))
        result = {
            "input_token": input_bound,
            "output_token": requested_output,
            # Providers may report automatic prefix-cache usage even without an
            # explicit directive, so these observable dimensions are always
            # admitted conservatively before transport.
            "cache_read_token": input_bound,
            "cache_write_token": input_bound,
            "request": 1,
            "wall_time_ns": wall_time_ns,
        }
        return result

    def _usage_quote(
        self,
        *,
        ledger: object,
        group: ReservationGroupV1,
        reservations: tuple[BudgetReservationV1, ...],
        decision: ModelRoutingDecision,
    ) -> PriceQuoteV1 | None:
        if not any(
            amount.dimension == "monetary" for member in reservations for amount in member.reserved
        ):
            return None
        descriptor = self._descriptor(ledger=ledger, decision=decision)
        quote = self._price_book.lookup(
            descriptor.provider,
            decision.model_snapshot,
            group.created_at,
        )
        if not isinstance(quote, PriceQuoteV1):
            raise IntegrityViolation("retained monetary reservation lost its exact price quote")
        _validate_price_quote(
            quote,
            provider=descriptor.provider,
            model_snapshot=decision.model_snapshot,
            observed_at=group.created_at,
        )
        return quote


def _step_execution_source(group: ReservationGroupV1) -> str:
    prefix = "agent-step:"
    value = group.idempotency_key
    if not value.startswith(prefix):
        raise IntegrityViolation("agent-step idempotency identity is malformed")
    source, separator, digest = value[len(prefix) :].partition(":")
    if (
        source not in {"online", "full_response_cache", "cassette_replay"}
        or not separator
        or not digest.startswith("identity:sha256:")
    ):
        raise IntegrityViolation("agent-step idempotency identity is malformed")
    return source


def _agent_step_usage(
    *,
    group: ReservationGroupV1,
    reservations: tuple[BudgetReservationV1, ...],
    execution_source: str,
    recorded_at: datetime,
    kind: str,
) -> UsageEntryV1:
    if (
        group.scope != "agent_step"
        or group.attempt_no is None
        or group.fencing_token is None
        or group.transport_attempt is not None
        or execution_source != _step_execution_source(group)
        or kind not in {"actual", "conservative"}
    ):
        raise IntegrityViolation("agent-step usage identity is invalid")
    for reservation in reservations:
        amounts = {amount.dimension: amount for amount in reservation.reserved}
        if set(amounts) != {"agent_step"} or amounts["agent_step"].value != 1:
            raise IntegrityViolation(
                "agent-step reservation must contain exactly one discrete step"
            )
    return UsageEntryV1(
        usage_id=_stable_id(
            "usage",
            {"group": group.reservation_group_id, "kind": kind},
        ),
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=group.budget_reservation_ids,
        scope="agent_step",
        run_id=group.run_id,
        attempt_no=group.attempt_no,
        request_hash=group.request_hash,
        transport_attempt=None,
        execution_source=execution_source,  # type: ignore[arg-type]
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        retry_index=0,
        token_usage=TokenUsageObservationV1(status="unavailable"),
        latency=LatencyObservationV1(status="unavailable"),
        # Provider/cache/replay wall time is accounted by the nested attempt_call;
        # this discrete control token must not double-reserve or double-consume it.
        wall_time_ns=0,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind=None,
        routing_decision_id=None,
        fencing_token_at_reserve=group.fencing_token,
        recorded_at=recorded_at,
    )


def _validate_price_quote(
    quote: PriceQuoteV1,
    *,
    provider: str,
    model_snapshot: str,
    observed_at: datetime,
) -> None:
    if (
        quote.provider != provider
        or quote.model_snapshot != model_snapshot
        or quote.effective_from > observed_at
        or (quote.effective_to is not None and observed_at >= quote.effective_to)
    ):
        raise IntegrityViolation("price-book quote differs from the exact lookup identity")


def _price_quote_for_reservations(
    *,
    ledger: object,
    price_book: PriceBook,
    decision: ModelRoutingDecision,
    group: ReservationGroupV1,
    reservations: tuple[BudgetReservationV1, ...],
) -> PriceQuoteV1 | None:
    if not any(
        amount.dimension == "monetary"
        for reservation in reservations
        for amount in reservation.reserved
    ):
        return None
    descriptor = WorkerCallCostGateway._descriptor(ledger=ledger, decision=decision)
    quote = price_book.lookup(descriptor.provider, decision.model_snapshot, group.created_at)
    if not isinstance(quote, PriceQuoteV1):
        raise IntegrityViolation("monetary reservation lost its exact retained price quote")
    _validate_price_quote(
        quote,
        provider=descriptor.provider,
        model_snapshot=decision.model_snapshot,
        observed_at=group.created_at,
    )
    return quote


def _actual_usage(
    *,
    group: ReservationGroupV1,
    reservations: tuple[BudgetReservationV1, ...],
    decision: ModelRoutingDecision,
    result: M4RouterResultV1,
    wall_time_ns: int,
    recorded_at: datetime,
    price_quote: PriceQuoteV1 | None,
) -> UsageEntryV1:
    expected_kind = "native" if isinstance(decision, RoutingDecisionV1) else "legacy_import"
    if (
        result.routing_decision_id != decision.decision_id
        or result.routing_decision_kind != expected_kind
        or result.execution_source != decision.execution_source
    ):
        raise IntegrityViolation("model result differs from its reserved RoutingDecision")
    token_usage = result.token_usage
    if result.execution_source == "full_response_cache":
        token_usage = TokenUsageObservationV1(
            status="reported",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=0,
        )
    monetary = MonetaryObservationV1(status="unavailable")
    if price_quote is not None:
        fields = {
            "input_tokens": token_usage.input_tokens,
            "output_tokens": token_usage.output_tokens,
            "cache_read_tokens": token_usage.cache_read_tokens,
            "cache_write_tokens": token_usage.cache_write_tokens,
        }
        required = {
            amount.dimension
            for member in reservations
            for amount in member.reserved
            if amount.dimension
            in {
                "input_token",
                "output_token",
                "cache_read_token",
                "cache_write_token",
            }
        }
        field_by_dimension = {
            "input_token": "input_tokens",
            "output_token": "output_tokens",
            "cache_read_token": "cache_read_tokens",
            "cache_write_token": "cache_write_tokens",
        }
        if token_usage.status == "reported" and all(
            fields[field_by_dimension[dimension]] is not None for dimension in required
        ):
            monetary = MonetaryObservationV1(
                status="reported",
                amount=_quoted_amount(
                    price_quote,
                    input_tokens=fields["input_tokens"] or 0,
                    output_tokens=fields["output_tokens"] or 0,
                    cache_read_tokens=fields["cache_read_tokens"] or 0,
                    cache_write_tokens=fields["cache_write_tokens"] or 0,
                ),
                currency=price_quote.currency,
                price_book_version=price_quote.price_book_version,
                quote_effective_at=price_quote.effective_from,
            )
    return UsageEntryV1(
        usage_id=_stable_id("usage", {"group": group.reservation_group_id, "kind": "actual"}),
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=group.budget_reservation_ids,
        scope="attempt_call",
        run_id=group.run_id,
        attempt_no=group.attempt_no,
        request_hash=group.request_hash,
        transport_attempt=group.transport_attempt,
        execution_source=result.execution_source,
        provider_prefix_cache=result.provider_prefix_cache,
        retry_index=(group.transport_attempt or 1) - 1,
        token_usage=token_usage,
        latency=result.latency,
        wall_time_ns=wall_time_ns,
        monetary=monetary,
        routing_decision_kind=result.routing_decision_kind,
        routing_decision_id=result.routing_decision_id,
        fencing_token_at_reserve=group.fencing_token,
        recorded_at=recorded_at,
    )


def _conservative_usage(
    *,
    group: ReservationGroupV1,
    reservations: tuple[BudgetReservationV1, ...],
    decision: ModelRoutingDecision,
    recorded_at: datetime,
    price_quote: PriceQuoteV1 | None,
) -> UsageEntryV1:
    maxima: dict[str, CostAmountV1] = {}
    for reservation in reservations:
        for amount in reservation.reserved:
            retained = maxima.get(amount.dimension)
            if retained is None or amount.value > retained.value:
                maxima[amount.dimension] = amount
    token_values = {
        field: int(maxima[dimension].value)
        for field, dimension in (
            ("input_tokens", "input_token"),
            ("output_tokens", "output_token"),
            ("cache_read_tokens", "cache_read_token"),
            ("cache_write_tokens", "cache_write_token"),
        )
        if dimension in maxima
    }
    token_usage = (
        TokenUsageObservationV1(status="reported", **token_values)
        if token_values
        else TokenUsageObservationV1(status="unavailable")
    )
    monetary_amount = maxima.get("monetary")
    if monetary_amount is not None and price_quote is None:
        raise IntegrityViolation("conservative monetary usage has no exact price quote")
    monetary = (
        MonetaryObservationV1(
            status="reported",
            amount=monetary_amount.value,
            currency=monetary_amount.currency,
            price_book_version=price_quote.price_book_version,
            quote_effective_at=price_quote.effective_from,
        )
        if monetary_amount is not None and price_quote is not None
        else MonetaryObservationV1(status="unavailable")
    )
    return UsageEntryV1(
        usage_id=_stable_id(
            "usage",
            {"group": group.reservation_group_id, "kind": "conservative"},
        ),
        reservation_group_id=group.reservation_group_id,
        budget_reservation_ids=group.budget_reservation_ids,
        scope="attempt_call",
        run_id=group.run_id,
        attempt_no=group.attempt_no,
        request_hash=group.request_hash,
        transport_attempt=group.transport_attempt,
        execution_source=decision.execution_source,
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        retry_index=(group.transport_attempt or 1) - 1,
        token_usage=token_usage,
        latency=LatencyObservationV1(status="unavailable"),
        wall_time_ns=int(maxima.get("wall_time_ns", _zero_wall()).value),
        monetary=monetary,
        routing_decision_kind=(
            "native" if isinstance(decision, RoutingDecisionV1) else "legacy_import"
        ),
        routing_decision_id=decision.decision_id,
        fencing_token_at_reserve=group.fencing_token,
        recorded_at=recorded_at,
    )


def _zero_wall() -> CostAmountV1:
    return CostAmountV1(dimension="wall_time_ns", value=0, unit="ns")


def _quoted_amount(
    quote: PriceQuoteV1,
    *,
    input_tokens: int | Decimal,
    output_tokens: int | Decimal,
    cache_read_tokens: int | Decimal,
    cache_write_tokens: int | Decimal,
) -> Decimal:
    cache_read_rate = quote.input_rate if quote.cache_read_rate is None else quote.cache_read_rate
    cache_write_rate = (
        quote.input_rate if quote.cache_write_rate is None else quote.cache_write_rate
    )
    numerator = (
        Decimal(input_tokens) * quote.input_rate
        + Decimal(output_tokens) * quote.output_rate
        + Decimal(cache_read_tokens) * cache_read_rate
        + Decimal(cache_write_tokens) * cache_write_rate
    )
    return numerator / Decimal(quote.rate_unit)


__all__ = [
    "AgentStepReservationToken",
    "CallReservationToken",
    "WorkerAgentStepCostGateway",
    "WorkerCallCostGateway",
    "WorkerConservativeAttemptUsageProvider",
]
