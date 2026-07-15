"""Task 11a — ``checker_runner@1`` handler (deterministic checker adapter)."""

from __future__ import annotations

import json

import pytest

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    GraphSelectionV1,
    PreparedRunResult,
)
from gameforge.platform.run_handlers.checker import (
    CHECKER_REPORT_SCHEMA_ID,
    CheckerRunHandler,
    DefaultCheckerFactory,
)
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
    snapshot_bytes,
)

CHECKER_KIND = RunKindRef(kind="checker.run", version=1)
SNAPSHOT_ID = "artifact:snapshot"


def _dangling_snapshot() -> bytes:
    # A DROPS_FROM relation whose producer entity does not exist -> dangling_reference.
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    dangling = Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="monster:ghost", dst_id="npc:1")
    return snapshot_bytes([npc], [dangling])


def _checker_payload(*, checker_ids=("graph",), defect_classes=()) -> CheckerRunPayloadV1:
    return CheckerRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=checker_ids,
        defect_classes=defect_classes,
    )


def _handler(store: FakeArtifactStore) -> CheckerRunHandler:
    return CheckerRunHandler(blobs=store, store=store, checker_factory=DefaultCheckerFactory())


def _context(store: FakeArtifactStore, payload: CheckerRunPayloadV1):
    store.register(SNAPSHOT_ID, _dangling_snapshot())
    return build_context(
        params=payload,
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
    )


def test_checker_handler_seals_primary_report_and_deterministic_findings() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload())
    outcome = _handler(store)(context)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "checker_completed"
    assert outcome.summary.primary_artifact_kind == "checker_run"
    assert outcome.run_id == "run:1"
    assert len(outcome.artifacts) == 1
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "checker_run"
    assert primary.payload_schema_id == CHECKER_REPORT_SCHEMA_ID
    assert primary.meta["payload_schema_id"] == CHECKER_REPORT_SCHEMA_ID
    assert primary.object_ref.key == primary.location.key

    # count invariants and the finding projection
    assert outcome.summary.prepared_domain_artifact_count == 1
    assert outcome.summary.prepared_finding_count == len(outcome.findings)
    assert outcome.findings, "the dangling reference must be reported"
    for finding in outcome.findings:
        assert finding.evidence_artifact_index == 0
        assert finding.payload.producer_run_id == "run:1"
        assert finding.payload.oracle_type == "deterministic"
        assert finding.payload.source == "checker"
        assert finding.expected_previous_revision is None
    assert any(f.payload.defect_class == "dangling_reference" for f in outcome.findings)


def test_checker_report_payload_carries_findings_and_snapshot_id() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload())
    outcome = _handler(store)(context)
    primary = outcome.artifacts[0]
    payload = json.loads(store.read_prepared(primary.object_ref))
    assert payload["payload_schema_version"] == CHECKER_REPORT_SCHEMA_ID
    assert payload["checker_ids"] == ["graph"]
    assert len(payload["findings"]) == len(outcome.findings)


def test_checker_handler_is_byte_deterministic() -> None:
    store_a, store_b = FakeArtifactStore(), FakeArtifactStore()
    outcome_a = _handler(store_a)(_context(store_a, _checker_payload()))
    outcome_b = _handler(store_b)(_context(store_b, _checker_payload()))
    assert outcome_a.artifacts[0].payload_hash == outcome_b.artifacts[0].payload_hash
    assert [f.finding_id for f in outcome_a.findings] == [f.finding_id for f in outcome_b.findings]


def test_defect_class_filter_drops_unrequested_classes() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload(defect_classes=("no_such_class",)))
    outcome = _handler(store)(context)
    assert outcome.findings == ()
    assert outcome.summary.prepared_finding_count == 0


def test_wrong_payload_type_is_rejected() -> None:
    store = FakeArtifactStore()
    # A simulation payload routed to the checker handler must fail closed.
    from gameforge.contracts.jobs import SimulationRunPayloadV1

    sim = SimulationRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        simulation_profile=ProfileRefV1(profile_id="sim", version=1),
        workload_profile=ProfileRefV1(profile_id="wl", version=1),
        replication_count=1,
        horizon_steps=1,
    )
    context = build_context(params=sim, kind=RunKindRef(kind="simulation.run", version=1), seed=1)
    with pytest.raises(TypeError):
        _handler(store)(context)
