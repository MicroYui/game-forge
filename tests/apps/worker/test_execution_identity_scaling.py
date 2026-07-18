"""Regression locks for bounded terminal execution-identity reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

import pytest

from gameforge.apps.worker.publication import WorkerManifestLedger
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import ReservationGroupV1
from gameforge.contracts.jobs import RunModelRouteLinkV1
from gameforge.contracts.lineage import (
    ObjectBinding,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.model_router import Message, ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.runtime.persistence.runs import TerminalRunAuthorityProjection
from tests.platform.m4c.test_terminal_publisher import (
    NOW,
    _attempt,
    _registry_and_definition,
)
from tests.platform.m4c.test_terminal_runtime_identity import (
    GRAPH,
    MODEL,
    MODEL_DESCRIPTOR,
    NODE,
    PROMPT,
    TOOL,
    _mode_run,
)


_RECORDED_AT = datetime(2026, 7, 16, tzinfo=UTC)


class _ProjectionRuns:
    def __init__(self, projection: TerminalRunAuthorityProjection) -> None:
        self.projection = projection
        self.projection_calls = 0
        self.attempt_projection_calls = 0

    def terminal_authority_projection(self, run_id: str, *, limit: int):
        assert run_id == self.projection.run.run_id
        assert limit >= len(self.projection.model_routes)
        self.projection_calls += 1
        return self.projection

    def terminal_attempt_authority_projection(
        self,
        run_id: str,
        *,
        limit: int,
    ) -> TerminalRunAuthorityProjection:
        assert run_id == self.projection.run.run_id
        current_attempt_no = self.projection.run.current_attempt_no
        self.attempt_projection_calls += 1
        return TerminalRunAuthorityProjection(
            run=self.projection.run,
            attempts=tuple(
                attempt
                for attempt in self.projection.attempts
                if attempt.attempt_no == current_attempt_no
            ),
            prompt_links=tuple(
                link
                for link in self.projection.prompt_links
                if link.attempt_no == current_attempt_no
            ),
            tool_links=tuple(
                link for link in self.projection.tool_links if link.attempt_no == current_attempt_no
            ),
            model_routes=tuple(
                route
                for route in self.projection.model_routes
                if route.attempt_no == current_attempt_no
            ),
            model_consumptions=tuple(
                item
                for item in self.projection.model_consumptions
                if item.attempt_no == current_attempt_no
            ),
            closed_attempt_failures=(),
        )


class _RoutingAuthority:
    def __init__(
        self,
        *,
        decisions: dict[str, RoutingDecisionV1],
        groups: dict[int, tuple[ReservationGroupV1, ...]],
    ) -> None:
        self.decisions = decisions
        self.groups = groups
        self.native_batch_calls = 0
        self.group_batch_calls = 0
        self.group_batch_attempt_nos: list[tuple[int, ...]] = []
        self.scalar_decision_calls = 0
        self.scalar_group_calls = 0

    def get_routing_decisions_many(
        self, decision_ids: tuple[str, ...]
    ) -> dict[str, RoutingDecisionV1]:
        self.native_batch_calls += 1
        return {decision_id: self.decisions[decision_id] for decision_id in decision_ids}

    def terminal_attempt_reservation_groups(
        self,
        *,
        run_id: str,
        attempt_nos: tuple[int, ...],
        limit: int,
    ) -> tuple[tuple[int, tuple[ReservationGroupV1, ...]], ...]:
        assert run_id == "run:1"
        assert limit >= sum(len(items) for items in self.groups.values())
        self.group_batch_calls += 1
        self.group_batch_attempt_nos.append(attempt_nos)
        return tuple((attempt_no, self.groups[attempt_no]) for attempt_no in attempt_nos)

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1:
        self.scalar_decision_calls += 1
        return self.decisions[decision_id]

    def list_attempt_reservation_groups(
        self, *, run_id: str, attempt_no: int
    ) -> tuple[ReservationGroupV1, ...]:
        assert run_id == "run:1"
        self.scalar_group_calls += 1
        return self.groups[attempt_no]


class _Artifacts:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.get_calls = 0

    def get(self, artifact_id: str) -> object | None:
        self.get_calls += 1
        return self.values.get(artifact_id)


class _Bindings:
    def __init__(self, values: dict[str, ObjectBinding]) -> None:
        self.values = values
        self.resolve_calls = 0

    def resolve(self, object_ref) -> ObjectBinding:
        self.resolve_calls += 1
        return self.values[object_ref.key]


class _ObjectStore:
    def __init__(self, values: dict[ObjectLocation, bytes]) -> None:
        self.values = values
        self.open_calls = 0

    def open(self, location: ObjectLocation) -> BytesIO:
        self.open_calls += 1
        return BytesIO(self.values[location])


class _CountingManifestLedger(WorkerManifestLedger):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reservation_cache_lookups = 0

    def list_attempt_reservation_groups(
        self, *, run_id: str, attempt_no: int
    ) -> tuple[object, ...]:
        self.reservation_cache_lookups += 1
        return super().list_attempt_reservation_groups(
            run_id=run_id,
            attempt_no=attempt_no,
        )


@dataclass(frozen=True)
class _Fixture:
    ledger: _CountingManifestLedger
    runs: _ProjectionRuns
    routing: _RoutingAuthority
    artifacts: _Artifacts
    bindings: _Bindings
    objects: _ObjectStore
    attempt_count: int
    route_count: int


def _fixture(*, route_count: int, attempt_count: int) -> _Fixture:
    _, definition = _registry_and_definition()
    run = _mode_run(definition, mode="live").model_copy(
        update={
            "current_attempt_no": attempt_count,
            "next_attempt_no": attempt_count + 1,
            "next_fencing_token": attempt_count + 1,
        }
    )
    plan = run.payload.execution_version_plan
    assert plan is not None

    routes: list[RunModelRouteLinkV1] = []
    decisions: dict[str, RoutingDecisionV1] = {}
    groups: dict[int, list[ReservationGroupV1]] = {
        attempt_no: [] for attempt_no in range(1, attempt_count + 1)
    }
    artifact_values: dict[str, object] = {}
    binding_values: dict[str, ObjectBinding] = {}
    object_values: dict[ObjectLocation, bytes] = {}
    per_attempt_call: dict[int, int] = {}

    for index in range(route_count):
        attempt_no = index % attempt_count + 1
        call_ordinal = per_attempt_call.get(attempt_no, 0) + 1
        per_attempt_call[attempt_no] = call_ordinal
        rendered = ModelRequestV2(
            model_snapshot=MODEL_DESCRIPTOR,
            messages=[Message(role="user", content=f"route {index}")],
            params={},
            tool_schemas=[],
            agent_node_id=NODE,
            prompt_version=PROMPT,
        )
        rendered_bytes = canonical_json(rendered.model_dump(mode="json")).encode("utf-8")
        object_ref = object_ref_for_bytes(rendered_bytes)
        location = ObjectLocation(
            store_id="local:test",
            key=object_ref.key,
            backend_generation=f"generation:{index}",
        )
        artifact = build_artifact_v2(
            kind="source_rendered",
            version_tuple=VersionTuple(
                prompt_version=PROMPT,
                agent_graph_version=GRAPH,
                tool_version="renderer@1",
            ),
            lineage=(),
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta={
                "payload_schema_id": "source-rendered@1",
                "renderer_version": "renderer@1",
                "agent_tool_version": TOOL,
                "producer_run_id": run.run_id,
                "producer_attempt_no": attempt_no,
                "logical_call_ordinal": call_ordinal,
                "route_ordinal": 1,
            },
            created_at=NOW,
        )
        rendered_hash = request_hash(rendered)
        decision = RoutingDecisionV1.create(
            run_id=run.run_id,
            attempt_no=attempt_no,
            request_hash=rendered_hash,
            rule_id="checker-default",
            model_snapshot=MODEL,
            tier="best",
            reason_code="primary_rule",
            budget_set_snapshot_id=run.payload.budget_set_snapshot_id,
            fallback_from=None,
            fallback_index=0,
            policy_version=plan.routing_policy_version,
            routing_policy_digest=plan.routing_policy_digest,
            catalog_version=plan.model_catalog_version,
            catalog_digest=plan.model_catalog_digest,
            execution_source="online",
            decided_at=_RECORDED_AT,
        )
        route = RunModelRouteLinkV1(
            run_id=run.run_id,
            attempt_no=attempt_no,
            call_ordinal=call_ordinal,
            route_ordinal=1,
            prompt_artifact_id=artifact.artifact_id,
            request_hash=rendered_hash.removeprefix("sha256:"),
            routing_decision_kind="native",
            routing_decision_id=decision.decision_id,
            fencing_token=attempt_no,
            published_at=NOW,
        )
        group = ReservationGroupV1(
            reservation_group_id=f"reservation-group:{index}",
            scope="attempt_call",
            run_id=run.run_id,
            budget_set_snapshot_id=run.payload.budget_set_snapshot_id,
            parent_hold_group_id=run.run_budget_hold_group_id,
            attempt_no=attempt_no,
            request_hash=rendered_hash,
            transport_attempt=1,
            fencing_token=attempt_no,
            idempotency_key=(
                f"model-route:{decision.decision_id}:call:{call_ordinal}:route:1:transport:1"
            ),
            budget_reservation_ids=(f"reservation:{index}",),
            status="held_unknown",
            revision=1,
            created_at=_RECORDED_AT,
        )
        routes.append(route)
        decisions[decision.decision_id] = decision
        groups[attempt_no].append(group)
        artifact_values[artifact.artifact_id] = artifact
        binding_values[object_ref.key] = ObjectBinding(
            object_ref=object_ref,
            location=location,
            status="active",
            revision=1,
            verified_at=NOW,
        )
        object_values[location] = rendered_bytes

    attempts = tuple(
        _attempt().model_copy(
            update={
                "attempt_no": attempt_no,
                "fencing_token": attempt_no,
            }
        )
        for attempt_no in range(1, attempt_count + 1)
    )
    projection = TerminalRunAuthorityProjection(
        run=run,
        attempts=attempts,
        prompt_links=(),
        tool_links=(),
        model_routes=tuple(routes),
        model_consumptions=(),
        closed_attempt_failures=(),
    )
    run_authority = _ProjectionRuns(projection)
    routing_authority = _RoutingAuthority(
        decisions=decisions,
        groups={attempt_no: tuple(items) for attempt_no, items in groups.items()},
    )
    artifacts = _Artifacts(artifact_values)
    bindings = _Bindings(binding_values)
    objects = _ObjectStore(object_values)
    ledger = _CountingManifestLedger(
        run_authority,
        routing_authority,
        artifacts=artifacts,
        object_bindings=bindings,
        object_store=objects,
    )
    return _Fixture(
        ledger=ledger,
        runs=run_authority,
        routing=routing_authority,
        artifacts=artifacts,
        bindings=bindings,
        objects=objects,
        attempt_count=attempt_count,
        route_count=route_count,
    )


@pytest.mark.parametrize(
    ("route_count", "attempt_count"),
    ((1, 1), (8, 2), (64, 4)),
)
def test_terminal_execution_identity_batches_authority_and_scales_linearly(
    route_count: int,
    attempt_count: int,
) -> None:
    fixture = _fixture(route_count=route_count, attempt_count=attempt_count)

    digest = fixture.ledger.terminal_authority_digest("run:1")
    identity = fixture.ledger.execution_identity("run:1", attempt_no=None)

    assert len(digest) == 64
    assert len(identity.bindings) == route_count
    assert fixture.runs.projection_calls == 1
    assert fixture.routing.native_batch_calls == 1
    assert fixture.routing.group_batch_calls == 1
    assert fixture.routing.scalar_decision_calls == 0
    assert fixture.routing.scalar_group_calls == 0
    assert fixture.ledger.reservation_cache_lookups == attempt_count
    assert fixture.artifacts.get_calls == route_count
    assert fixture.bindings.resolve_calls == route_count
    assert fixture.objects.open_calls == route_count


def test_retry_authority_digest_reads_only_current_attempt_and_fresh_bypasses_cache() -> None:
    fixture = _fixture(route_count=64, attempt_count=4)

    retained = fixture.ledger.terminal_attempt_authority_digest("run:1")
    assert fixture.ledger.terminal_attempt_authority_digest("run:1") == retained
    fresh = fixture.ledger.fresh_terminal_attempt_authority_digest("run:1")

    assert fresh == retained
    assert fixture.runs.projection_calls == 0
    assert fixture.runs.attempt_projection_calls == 2
    assert fixture.routing.group_batch_attempt_nos == [(4,), (4,)]
    assert fixture.routing.native_batch_calls == 2
