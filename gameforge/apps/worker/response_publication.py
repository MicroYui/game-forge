"""Atomic model-response consumption and native RECORD shard publication."""

from __future__ import annotations

from datetime import UTC, datetime

from gameforge.apps.worker.cost_bridge import (
    AgentStepReservationToken,
    CallReservationToken,
    WorkerAgentStepCostGateway,
    WorkerCallCostGateway,
)
from gameforge.apps.worker.execution_identity import (
    PendingResponseConsumption,
    build_authoritative_execution_identity,
    prepare_rendered_request_authority,
)
from gameforge.apps.worker.publication import WorkerArtifactPort
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.cassette_import import (
    CassetteBundleV1,
    LegacyImportRoutingDecisionV1,
)
from gameforge.contracts.errors import AttemptFenceStateRejected, IntegrityViolation
from gameforge.contracts.jobs import (
    RunIntermediateArtifactLinkV1,
    RunModelResponseConsumptionV1,
    RunRecord,
)
from gameforge.contracts.lineage import (
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    ExecutionIdentityV1,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.storage import UtcClock
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.runs.lifecycle import (
    AttemptWriteFence,
    validate_attempt_write_fence,
)
from gameforge.platform.terminal_staging import StagedReceipt
from gameforge.runtime.model_router.m4_router import M4RouterResultV1


_CASSETTE_TOOL_VERSION = "cassette@1"
ModelRoutingDecision = RoutingDecisionV1 | LegacyImportRoutingDecisionV1


def _now(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("response publication clock must return UTC")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class WorkerResponseConsumptionPublisher:
    """Commit shard/usage/consumption/audit before a response reaches an Agent."""

    def __init__(
        self,
        *,
        unit_of_work: object,
        run: RunRecord,
        cost: WorkerCallCostGateway,
        object_store: object,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._run = run
        self._cost = cost
        self._object_store = object_store
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def publish_response_consumption(
        self,
        *,
        fence: AttemptWriteFence,
        link: RunIntermediateArtifactLinkV1,
        decision: ModelRoutingDecision,
        result: M4RouterResultV1,
        record: CassetteRecordV2 | None,
        reservation: object,
        step_cost: WorkerAgentStepCostGateway,
        step_reservation: object,
        wall_time_ns: int,
        actor: AuditActor,
    ) -> RunModelResponseConsumptionV1:
        token, step_token = self._validate_inputs(
            fence=fence,
            link=link,
            decision=decision,
            result=result,
            record=record,
            reservation=reservation,
            step_reservation=step_reservation,
        )
        pending = PendingResponseConsumption(
            call_ordinal=link.call_ordinal,
            route_ordinal=link.route_ordinal,
            execution_source=result.execution_source,
            transport_attempt=(
                result.transport_attempt_count if result.execution_source == "online" else None
            ),
        )
        identity: ExecutionIdentityV1 | None = None
        record_lineage: tuple[str, ...] = ()
        if record is not None:
            # Read/plan, close the complete logical-call route identity, and release
            # the DB transaction before the potentially slow ObjectStore write.
            with self._unit_of_work.begin_read() as transaction:  # type: ignore[attr-defined]
                call_authority = transaction.runs.get_model_call_write_authority(
                    fence,
                    call_ordinal=link.call_ordinal,
                    route_ordinal=link.route_ordinal,
                )
                if call_authority is None:
                    raise IntegrityViolation("response publication Run disappeared")
                retained_run = call_authority.run
                prepared_route = call_authority.route_links[-1]
                rendered_authority = prepare_rendered_request_authority(
                    transaction=transaction,
                    object_store=self._object_store,
                    route=prepared_route,
                )
                identity = build_authoritative_execution_identity(
                    transaction=transaction,
                    object_store=self._object_store,
                    run=retained_run,
                    attempt_no=link.attempt_no,
                    scope="record_shard",
                    pending=pending,
                    call_authority=call_authority,
                    rendered_request_authority=rendered_authority,
                )
                record_lineage = (prepared_route.prompt_artifact_id,)
                if record_lineage != (link.artifact_id,) or len(identity.bindings) != 1:
                    raise IntegrityViolation("RECORD consumed prompt does not close shard identity")
        staged = self._stage_record_shard(
            link=link,
            decision=decision,
            result=result,
            record=record,
            identity=identity,
            lineage=record_lineage,
        )
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            now = _now(self._clock)
            runs = transaction.runs
            call_authority = runs.get_model_call_write_authority(
                fence,
                call_ordinal=link.call_ordinal,
                route_ordinal=link.route_ordinal,
            )
            if call_authority is None:
                raise IntegrityViolation("response publication authority disappeared")
            run = call_authority.run
            attempt = call_authority.attempt
            lease = call_authority.lease
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=actor,
                now=now,
                allowed_statuses=frozenset({"running"}),
            )
            if run.cancel_requested_at is not None:
                raise AttemptFenceStateRejected(
                    "cancel-requested Run cannot consume a model response"
                )
            route = call_authority.route_links[-1]
            if (
                route.prompt_artifact_id != link.artifact_id
                or route.request_hash != link.request_hash
                or route.routing_decision_kind != result.routing_decision_kind
                or route.routing_decision_id != decision.decision_id
                or route.fencing_token != fence.fencing_token
            ):
                raise IntegrityViolation("response differs from its committed model route")
            first_route = call_authority.route_links[0]
            if step_token.request_hash != f"sha256:{first_route.request_hash}":
                raise IntegrityViolation(
                    "agent-step reservation differs from the logical call's first route"
                )

            if identity is not None:
                if call_authority.consumption is not None:
                    raise IntegrityViolation("RECORD logical call was consumed during staging")

            shard_id: str | None = None
            if staged is not None:
                artifact, receipt = staged
                artifact_port = WorkerArtifactPort(
                    artifacts=transaction.artifacts,
                    object_bindings=transaction.object_bindings,
                    object_store=self._object_store,
                )
                retained = artifact_port.put_staged(artifact, receipt)
                if canonical_json(
                    retained.model_dump(mode="json", exclude={"created_at"})
                ) != canonical_json(artifact.model_dump(mode="json", exclude={"created_at"})):
                    raise IntegrityViolation("response shard publisher retained another Artifact")
                shard_id = artifact.artifact_id

            settled = self._cost.reconcile_in_transaction(
                transaction=transaction,
                reservation=token,
                decision=decision,
                result=result,
                wall_time_ns=wall_time_ns,
            )
            if settled.status not in {
                "reconciled",
                "conservatively_settled",
                "late_reconciled",
            }:
                raise IntegrityViolation("response usage did not reach a terminal settlement")
            settled_step = step_cost.reconcile_step_in_transaction(
                transaction=transaction,
                reservation=step_token,
            )
            if settled_step.status != "reconciled":
                raise IntegrityViolation("response agent step did not reconcile exactly")
            consumption = RunModelResponseConsumptionV1(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                call_ordinal=link.call_ordinal,
                route_ordinal=link.route_ordinal,
                execution_source=result.execution_source,
                reservation_group_id=token.reservation_group_id,
                transport_attempt=(
                    result.transport_attempt_count if result.execution_source == "online" else None
                ),
                cassette_shard_artifact_id=shard_id,
                response_digest=sha256_lowerhex(result.response_normalized.encode("utf-8")),
                consumed_at=_utc_text(now),
            )
            retained_consumption = runs.put_model_response_consumption(consumption)
            if retained_consumption != consumption:
                raise IntegrityViolation("RunStore retained another response consumption")
            AuditGate(sink=transaction.audit, clock=self._clock).append(
                chain_id=self._audit_chain_id,
                actor=actor,
                initiated_by=run.initiated_by,
                action="run.model_response_consumed",
                subject=AuditSubject(
                    resource_kind="run",
                    resource_id=run.run_id,
                    artifact_id=shard_id,
                ),
                correlation=AuditCorrelation(
                    request_id=None,
                    run_id=run.run_id,
                    trace_id=attempt.trace_id,
                ),
            )
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=fence,
                actor=actor,
                now=_now(self._clock),
                allowed_statuses=frozenset({"running"}),
            )
            return retained_consumption

    def _stage_record_shard(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        decision: ModelRoutingDecision,
        result: M4RouterResultV1,
        record: CassetteRecordV2 | None,
        identity: ExecutionIdentityV1 | None,
        lineage: tuple[str, ...],
    ):
        if record is None:
            if self._run.payload.llm_execution_mode == "record":
                raise IntegrityViolation("RECORD response omitted its cassette record")
            if identity is not None:
                raise IntegrityViolation("non-RECORD response supplied a shard identity")
            return None
        if self._run.payload.llm_execution_mode != "record":
            raise IntegrityViolation("non-RECORD response supplied a cassette record")
        if not isinstance(decision, RoutingDecisionV1):
            raise IntegrityViolation("RECORD shard requires a native routing decision")
        if identity is None:
            raise IntegrityViolation("RECORD response has no authoritative route identity")
        plan = self._run.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("RECORD response has no execution plan")
        node = next(
            (item for item in plan.nodes if item.agent_node_id == record.agent_node_id),
            None,
        )
        if node is None:
            raise IntegrityViolation("RECORD response node escapes its execution plan")
        consumed = tuple(item for item in identity.bindings if item.response_consumed)
        if (
            len(consumed) != 1
            or consumed[0].route_ordinal != link.route_ordinal
            or consumed[0].routing_decision_id != decision.decision_id
            or consumed[0].agent_node_id != record.agent_node_id
            or consumed[0].prompt_version != node.prompt_version
            or consumed[0].model_snapshot != decision.model_snapshot
            or consumed[0].execution_source != result.execution_source
        ):
            raise IntegrityViolation("RECORD identity differs from its consumed response route")
        bundle = CassetteBundleV1(
            scope="record_shard",
            run_id=self._run.run_id,
            attempt_no=link.attempt_no,
            ordinal=link.call_ordinal,
            records=(record,),
        )
        payload = canonical_json(bundle.model_dump(mode="json")).encode("utf-8")
        stored = self._object_store.put_verified(payload)  # type: ignore[attr-defined]
        stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
        if stat.ref != stored.ref or stat.location != stored.location:
            raise IntegrityViolation("staged response shard failed exact ObjectStore stat")
        artifact = build_artifact_v2(
            kind="cassette_bundle",
            version_tuple=VersionTuple(
                prompt_version=identity.prompt_projection.tuple_value,
                model_snapshot=identity.model_projection.tuple_value,
                agent_graph_version=identity.agent_graph_version,
                tool_version=_CASSETTE_TOOL_VERSION,
                cassette_id=f"sha256:{stored.ref.sha256}",
            ),
            lineage=lineage,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "cassette-record-shard@1",
                "execution_identity": identity,
                "replayability": "cassette_replay",
            },
            created_at=_utc_text(_now(self._clock)),
        )
        return (
            artifact,
            StagedReceipt(
                slot=(f"record-shard:{link.attempt_no}:{link.call_ordinal}:{link.route_ordinal}"),
                ref=stored.ref,
                location=stored.location,
                verified_at=stat.verified_at,
                generation_verification_token=stat.generation_verification_token,
            ),
        )

    def _validate_inputs(
        self,
        *,
        fence: AttemptWriteFence,
        link: RunIntermediateArtifactLinkV1,
        decision: ModelRoutingDecision,
        result: M4RouterResultV1,
        record: CassetteRecordV2 | None,
        reservation: object,
        step_reservation: object,
    ) -> tuple[CallReservationToken, AgentStepReservationToken]:
        if not isinstance(reservation, CallReservationToken):
            raise IntegrityViolation("response publisher received an unknown reservation")
        if not isinstance(step_reservation, AgentStepReservationToken):
            raise IntegrityViolation(
                "response publisher received an unknown agent-step reservation"
            )
        if (
            fence.run_id != self._run.run_id
            or link.run_id != fence.run_id
            or link.attempt_no != fence.attempt_no
            or decision.request_hash != f"sha256:{link.request_hash}"
            or result.routing_decision_id != decision.decision_id
            or reservation.decision_id != decision.decision_id
            or reservation.call_ordinal != link.call_ordinal
            or reservation.route_ordinal != link.route_ordinal
            or (
                result.execution_source == "online"
                and reservation.transport_attempt != result.transport_attempt_count
            )
            or step_reservation.call_ordinal != link.call_ordinal
            or not (
                step_reservation.execution_source == result.execution_source
                or (
                    # One logical Agent step is admitted before its first route.
                    # A typed transport failure may then fall back to a retained
                    # full-response cache entry without minting a second step.
                    step_reservation.execution_source == "online"
                    and result.execution_source == "full_response_cache"
                )
            )
        ):
            raise IntegrityViolation("response publisher inputs do not close one model route")
        if isinstance(decision, RoutingDecisionV1) and (
            decision.run_id != fence.run_id or decision.attempt_no != fence.attempt_no
        ):
            raise IntegrityViolation("native response decision belongs to another attempt")
        if record is not None and record.routing_decision != decision:
            raise IntegrityViolation("response cassette record differs from its route")
        return reservation, step_reservation


__all__ = ["WorkerResponseConsumptionPublisher"]
