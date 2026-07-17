"""Task 11a — ``checker_runner@1`` handler (deterministic checker adapter)."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from gameforge.contracts.dsl import Constraint, Predicate, Selector
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    GraphSelectionV1,
    PreparedRunResult,
)
from gameforge.contracts.lineage import ObjectLocation, VersionTuple, object_ref_for_bytes
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload
from gameforge.platform.run_handlers import base as handler_base
from gameforge.platform.run_handlers.base import (
    PreparedArtifactBatchStore,
    scoped_finding_series_id,
    store_prepared_blob,
)
from gameforge.platform.run_handlers.checker import (
    CHECKER_REPORT_SCHEMA_ID,
    CheckerExecutionPolicy,
    CheckerRunHandler,
    DefaultCheckerFactory,
)
from gameforge.spine.checkers.graph import GraphChecker
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
    snapshot_bytes,
)

CHECKER_KIND = RunKindRef(kind="checker.run", version=1)
SNAPSHOT_ID = "artifact:snapshot"


def test_checker_rejects_mismatched_exact_profile_binding_before_execution() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload(checker_ids=("graph",)))
    context = replace(
        context,
        payload=context.payload.model_copy(
            update={
                "resolved_profiles": (
                    resolved_binding(
                        "/params/checker_profile",
                        profile_id="other",
                        version=9,
                        kind="simulation",
                    ),
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        _handler(store)(context)

    assert store.put_count == 0


def test_checker_rejects_an_extra_profile_binding_before_execution() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload(checker_ids=("graph",)))
    context = replace(
        context,
        payload=context.payload.model_copy(
            update={
                "resolved_profiles": (
                    *context.payload.resolved_profiles,
                    resolved_binding(
                        "/params/unconsumed_profile",
                        profile_id="injected",
                        version=1,
                        kind="workload",
                    ),
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        _handler(store)(context)

    assert store.put_count == 0


def test_prepared_blob_bound_rejects_before_object_store_write(monkeypatch) -> None:
    store = FakeArtifactStore()
    monkeypatch.setattr(handler_base, "MAX_PREPARED_ARTIFACT_BYTES", 3)

    with pytest.raises(IntegrityViolation, match="pre-write byte bound"):
        store_prepared_blob(
            store,
            kind="checker_run",
            payload_schema_id="checker-report@1",
            version_tuple=VersionTuple(tool_version="checker@1"),
            lineage=(),
            blob=b"four",
        )

    assert store.put_count == 0


def test_prepared_batch_rejects_cumulative_overflow_without_retaining_new_blob() -> None:
    batch = PreparedArtifactBatchStore(max_bytes=3)
    batch.put_prepared(b"ab")

    with pytest.raises(IntegrityViolation, match="aggregate byte bound"):
        batch.put_prepared(b"cd")

    assert batch.staged_bytes == 2


def test_checker_finding_cap_rejects_before_primary_blob_write(monkeypatch) -> None:
    class TwoFindingChecker:
        def check(self, snapshot, nav=None):
            del nav
            finding = GraphChecker().check(snapshot)[0]
            return [
                finding.model_copy(update={"id": "finding:one"}),
                finding.model_copy(update={"id": "finding:two"}),
            ]

    class Factory:
        def build(self, checker_id, *, constraints):
            del constraints
            assert checker_id == "asp"
            return TwoFindingChecker()

    monkeypatch.setattr(handler_base, "MAX_PREPARED_FINDINGS", 1)
    store = FakeArtifactStore()
    handler = CheckerRunHandler(blobs=store, store=store, checker_factory=Factory())

    with pytest.raises(ValueError, match="finding count"):
        handler(_context(store, _checker_payload(checker_ids=("asp",))))

    assert store.put_count == 0


def test_prepared_batch_rejects_artifact_count_before_retaining_new_blob() -> None:
    batch = PreparedArtifactBatchStore(max_bytes=10, max_artifacts=1)
    batch.put_prepared(b"a")

    with pytest.raises(IntegrityViolation, match="aggregate artifact bound"):
        batch.put_prepared(b"b")

    assert batch.staged_artifact_count == 1
    assert batch.staged_bytes == 1


def test_prepared_batch_rejects_substituted_commit_binding_before_target_write() -> None:
    batch = PreparedArtifactBatchStore(max_bytes=10)
    first = store_prepared_blob(
        batch,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(tool_version="checker@1"),
        lineage=(),
        blob=b"a",
    )
    store_prepared_blob(
        batch,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(tool_version="checker@1"),
        lineage=(),
        blob=b"b",
    )
    target = FakeArtifactStore()

    with pytest.raises(IntegrityViolation, match="exact staged binding"):
        batch.commit(target, (first, first), max_bytes=10)

    assert target.put_count == 0


def test_prepared_batch_rejects_forged_staged_hash_before_target_write() -> None:
    batch = PreparedArtifactBatchStore(max_bytes=10)
    artifact = store_prepared_blob(
        batch,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(tool_version="checker@1"),
        lineage=(),
        blob=b"a",
    ).model_copy(update={"payload_hash": "0" * 64})
    target = FakeArtifactStore()

    with pytest.raises(IntegrityViolation, match="exact staged binding"):
        batch.commit(target, (artifact,), max_bytes=10)

    assert target.put_count == 0


def test_prepared_batch_rejects_target_location_outside_exact_object_key() -> None:
    class WrongLocationStore:
        def __init__(self) -> None:
            self.put_count = 0

        def put_prepared(self, payload: bytes):
            self.put_count += 1
            object_ref = object_ref_for_bytes(payload)
            return object_ref, ObjectLocation(
                store_id="malicious",
                key="objects/forged-location",
                backend_generation="malicious@1",
            )

    batch = PreparedArtifactBatchStore(max_bytes=10)
    artifact = store_prepared_blob(
        batch,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(tool_version="checker@1"),
        lineage=(),
        blob=b"a",
    )
    target = WrongLocationStore()

    with pytest.raises(IntegrityViolation, match="changed a staged object binding"):
        batch.commit(target, (artifact,), max_bytes=10)

    assert target.put_count == 1


def test_scoped_finding_series_id_is_bounded_and_commits_full_inputs() -> None:
    shared_prefix = "x" * 5000
    first = scoped_finding_series_id(
        namespace="profile",
        scope_id=shared_prefix + "a",
        finding_id=shared_prefix + "first",
    )
    second = scoped_finding_series_id(
        namespace="profile",
        scope_id=shared_prefix + "b",
        finding_id=shared_prefix + "second",
    )

    assert len(first) <= 4096
    assert len(second) <= 4096
    assert first != second


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
    assert payload["checker_profile"] == {"profile_id": "checker", "version": 1}
    assert payload["constraint_snapshot_binding_status"] == "not_applicable"
    assert "constraint_snapshot_artifact_id" not in payload
    assert payload["checker_ids"] == ["graph"]
    assert payload["constraint_application"] == []
    assert len(payload["findings"]) == len(outcome.findings)
    assert {item["producer_run_id"] for item in payload["findings"]} == {"run:1"}


def test_checker_handler_is_byte_deterministic() -> None:
    store_a, store_b = FakeArtifactStore(), FakeArtifactStore()
    outcome_a = _handler(store_a)(_context(store_a, _checker_payload()))
    outcome_b = _handler(store_b)(_context(store_b, _checker_payload()))
    assert outcome_a.artifacts[0].payload_hash == outcome_b.artifacts[0].payload_hash
    assert [f.finding_id for f in outcome_a.findings] == [f.finding_id for f in outcome_b.findings]


def test_defect_class_filter_drops_unrequested_classes() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload(defect_classes=("dead_quest",)))
    outcome = _handler(store)(context)
    assert outcome.findings == ()
    assert outcome.summary.prepared_finding_count == 0


@pytest.mark.parametrize(
    "payload",
    (
        _checker_payload(checker_ids=()),
        _checker_payload(defect_classes=("typo_unknown_class",)),
        _checker_payload(checker_ids=("typo_unknown_checker",)),
    ),
)
def test_noop_or_unknown_checker_requests_fail_closed(payload) -> None:
    store = FakeArtifactStore()
    match = "no direct backend" if not payload.checker_ids else "profile taxonomy"
    with pytest.raises(IntegrityViolation, match=match):
        _handler(store)(_context(store, payload))


def test_standalone_smt_selection_cannot_succeed_with_zero_assertions() -> None:
    store = FakeArtifactStore()

    with pytest.raises(IntegrityViolation, match="no exact executable numeric constraint"):
        _handler(store)(_context(store, _checker_payload(checker_ids=("smt",))))

    assert store.put_count == 0


def test_smt_selection_executes_each_exact_numeric_constraint_once() -> None:
    class NoDirectSmtFactory:
        calls: list[str] = []

        def build(self, checker_id, *, constraints):
            del constraints
            self.calls.append(checker_id)
            raise AssertionError("numeric constraints must not invoke an empty direct SMT probe")

    store = FakeArtifactStore()
    constraint_id = "artifact:numeric-constraints"
    constraint = Constraint(
        id="C_cap",
        kind="numeric",
        oracle="deterministic",
        scope=Selector(var="q", node_type="QUEST"),
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store.register(
        constraint_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    payload = _checker_payload(checker_ids=("smt",)).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_id}
    )
    context = _context(store, payload)
    store.register(
        SNAPSHOT_ID,
        snapshot_bytes(
            [Entity(id="quest:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
            [],
        ),
    )
    factory = NoDirectSmtFactory()

    outcome = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=factory,
    )(context)

    assert factory.calls == []
    matching = [
        item
        for item in outcome.findings
        if item.payload.constraint_id == "C_cap"
        and item.payload.defect_class == "reward_out_of_range"
    ]
    assert len(matching) == 1
    report = decode_and_validate_artifact_payload(
        payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
        blob=store.read_prepared(outcome.artifacts[0].object_ref),
    )
    assert report["checker_ids"] == ["smt"]
    assert report["constraint_application"] == [
        {"constraint_id": "C_cap", "checker_id": "smt", "status": "executed"}
    ]
    assert report["constraint_snapshot_binding_status"] == "bound"
    assert report["constraint_snapshot_artifact_id"] == constraint_id


def test_checker_profile_rejects_compiled_native_outside_exact_allowlist() -> None:
    store = FakeArtifactStore()
    constraint_id = "artifact:numeric-constraints"
    constraint = Constraint(
        id="C_cap",
        kind="numeric",
        oracle="deterministic",
        scope=Selector(var="q", node_type="QUEST"),
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store.register(
        constraint_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    payload = _checker_payload(checker_ids=("graph",)).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_id}
    )
    context = _context(store, payload)
    store.register(
        SNAPSHOT_ID,
        snapshot_bytes(
            [Entity(id="quest:1", type=NodeType.QUEST, attrs={"reward_gold": 120})],
            [],
        ),
    )
    restricted = CheckerExecutionPolicy(
        allowed_checker_ids=("graph",),
        allowed_defect_classes=("dangling_reference",),
        max_direct_checker_count=1,
        max_constraint_count=1,
        max_work_units=2_000_000,
    )
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=DefaultCheckerFactory(),
        execution_policy_resolver=lambda _profile: restricted,
    )

    with pytest.raises(IntegrityViolation, match="compiled checker route"):
        handler(context)

    assert store.put_count == 0


def test_checker_profile_rejects_actual_finding_outside_exact_defect_allowlist() -> None:
    store = FakeArtifactStore()
    context = _context(store, _checker_payload())
    store.register(
        SNAPSHOT_ID,
        snapshot_bytes([Entity(id="quest:1", type=NodeType.QUEST, attrs={})], []),
    )
    restricted = CheckerExecutionPolicy(
        allowed_checker_ids=("graph",),
        allowed_defect_classes=("dangling_reference",),
        max_direct_checker_count=1,
        max_constraint_count=0,
        max_work_units=2_000_000,
    )
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=DefaultCheckerFactory(),
        execution_policy_resolver=lambda _profile: restricted,
    )

    with pytest.raises(IntegrityViolation, match="output is outside"):
        handler(context)

    assert store.put_count == 0


def test_restricted_checker_profile_accepts_matching_native_and_finding_taxonomy() -> None:
    store = FakeArtifactStore()
    restricted = CheckerExecutionPolicy(
        allowed_checker_ids=("graph",),
        allowed_defect_classes=("dangling_reference",),
        max_direct_checker_count=1,
        max_constraint_count=0,
        max_work_units=2_000_000,
    )
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=DefaultCheckerFactory(),
        execution_policy_resolver=lambda _profile: restricted,
    )

    outcome = handler(_context(store, _checker_payload()))

    assert [finding.payload.defect_class for finding in outcome.findings] == ["dangling_reference"]


def test_checker_work_budget_rejects_before_backend_execution() -> None:
    class RecordingFactory:
        calls = 0

        def build(self, checker_id, *, constraints):
            self.calls += 1
            raise AssertionError("backend must not run")

    factory = RecordingFactory()
    store = FakeArtifactStore()
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=factory,
        execution_policy_resolver=lambda _profile: CheckerExecutionPolicy(
            allowed_checker_ids=("graph",),
            allowed_defect_classes=("dangling_reference",),
            max_direct_checker_count=1,
            max_constraint_count=1,
            max_work_units=1,
        ),
    )

    with pytest.raises(IntegrityViolation, match="work budget"):
        handler(_context(store, _checker_payload()))

    assert factory.calls == 0


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


def test_checker_run_rejects_llm_assisted_constraint_in_deterministic_execution() -> None:
    constraint = Constraint(
        id="C_llm",
        kind="numeric",
        oracle="mixed",
        predicates=(Predicate(expr="semantic_price(item)", oracle="llm-assisted"),),
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store = FakeArtifactStore()
    constraint_id = "artifact:constraints"
    store.register(
        constraint_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    payload = _checker_payload().model_copy(
        update={"constraint_snapshot_artifact_id": constraint_id}
    )
    with pytest.raises(IntegrityViolation, match="llm-assisted"):
        _handler(store)(_context(store, payload))


def test_exact_constraint_snapshot_is_canonically_compiled_and_reported() -> None:
    store = FakeArtifactStore()
    constraint_id = "artifact:constraints"
    quest = Entity(
        id="quest:1",
        type=NodeType.QUEST,
        attrs={"reward_gold": 120},
    )
    steps = [Entity(id=f"step:{index}", type=NodeType.QUEST_STEP) for index in (1, 2)]
    relations = [
        Relation(
            id="cycle:1",
            type=EdgeType.PRECEDES,
            src_id="step:1",
            dst_id="step:2",
        ),
        Relation(
            id="cycle:2",
            type=EdgeType.PRECEDES,
            src_id="step:2",
            dst_id="step:1",
        ),
        Relation(
            id="dangling:1",
            type=EdgeType.DROPS_FROM,
            src_id="monster:missing",
            dst_id="quest:1",
        ),
    ]
    store.register(SNAPSHOT_ID, snapshot_bytes([quest, *steps], relations))
    constraints = [
        Constraint(
            id="C_cap",
            kind="numeric",
            oracle="deterministic",
            scope=Selector(var="q", node_type="QUEST"),
            **{"assert": "reward_gold <= 80"},
            severity="major",
        ),
        Constraint(
            id="C_cycle",
            kind="structural",
            oracle="deterministic",
            **{"assert": "acyclic(quest_steps)"},
            severity="critical",
        ),
        Constraint(
            id="C_story",
            kind="narrative",
            oracle="deterministic",
            **{"assert": "continuity_consistent"},
            severity="major",
        ),
    ]
    store.register(
        constraint_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [
                constraint.model_dump(mode="json", by_alias=True)
                for constraint in reversed(constraints)
            ],
        },
    )
    payload = _checker_payload(checker_ids=("graph",)).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_id}
    )
    context = build_context(
        params=payload,
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
        version_tuple=VersionTuple(
            constraint_snapshot_id="constraint:semantic:1",
            tool_version="handler@1",
        ),
    )

    outcome = _handler(store)(context)

    by_constraint = {
        (item.payload.constraint_id, item.payload.defect_class) for item in outcome.findings
    }
    assert ("C_cap", "reward_out_of_range") in by_constraint
    assert ("C_cycle", "cyclic_dependency") in by_constraint
    assert ("C_story", "dangling_reference") in by_constraint
    report = decode_and_validate_artifact_payload(
        payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
        blob=store.read_prepared(outcome.artifacts[0].object_ref),
    )
    assert report["constraint_application"] == [
        {"constraint_id": "C_cap", "checker_id": "smt", "status": "executed"},
        {"constraint_id": "C_cycle", "checker_id": "asp", "status": "executed"},
        {"constraint_id": "C_story", "checker_id": "graph", "status": "executed"},
    ]
    assert outcome.artifacts[0].version_tuple.constraint_snapshot_id == "constraint:semantic:1"


def test_reachable_constraint_without_navigation_is_unproven_not_executed() -> None:
    store = FakeArtifactStore()
    constraint_artifact_id = "artifact:reachable-constraint"
    constraint = Constraint(
        id="C_reachable",
        kind="structural",
        oracle="deterministic",
        **{"assert": "reachable_in(target, giver)"},
        severity="critical",
    )
    store.register(
        constraint_artifact_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    payload = _checker_payload(checker_ids=()).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_artifact_id}
    )

    outcome = _handler(store)(_context(store, payload))

    assert [(item.payload.constraint_id, item.payload.status) for item in outcome.findings] == [
        ("C_reachable", "unproven")
    ]
    report = decode_and_validate_artifact_payload(
        payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
        blob=store.read_prepared(outcome.artifacts[0].object_ref),
    )
    assert report["constraint_application"] == [
        {
            "constraint_id": "C_reachable",
            "checker_id": "graph",
            "status": "unproven",
            "reason_code": "navigation_ground_truth_unavailable",
        }
    ]


def test_reachable_constraint_with_navigation_executes_real_graph_decision() -> None:
    class DisconnectedNav:
        def pos_of(self, entity_id: str):
            return {
                "npc:giver": (0, 0),
                "npc:target": (2, 0),
            }.get(entity_id)

        def reachable(self, src, dst):
            return src == dst

    store = FakeArtifactStore()
    constraint_artifact_id = "artifact:reachable-constraint"
    constraint = Constraint(
        id="C_reachable",
        kind="structural",
        oracle="deterministic",
        **{"assert": "reachable_in(target, giver)"},
        severity="critical",
    )
    quest = Entity(id="quest:1", type=NodeType.QUEST, attrs={})
    giver = Entity(id="npc:giver", type=NodeType.NPC, attrs={})
    target = Entity(id="npc:target", type=NodeType.NPC, attrs={})
    step = Entity(
        id="step:talk",
        type=NodeType.QUEST_STEP,
        attrs={"kind": "talk", "target": target.id},
    )
    store.register(
        SNAPSHOT_ID,
        snapshot_bytes(
            [quest, giver, target, step],
            [
                Relation(
                    id="quest:giver",
                    type=EdgeType.STARTS_AT,
                    src_id=quest.id,
                    dst_id=giver.id,
                ),
                Relation(
                    id="quest:step",
                    type=EdgeType.HAS_STEP,
                    src_id=quest.id,
                    dst_id=step.id,
                ),
            ],
        ),
    )
    store.register(
        constraint_artifact_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    payload = _checker_payload(checker_ids=()).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_artifact_id}
    )
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=DefaultCheckerFactory(),
        nav_loader=lambda _blobs, _artifact_id: DisconnectedNav(),
    )

    context = build_context(
        params=payload,
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
    )
    outcome = handler(context)

    assert [(item.payload.constraint_id, item.payload.status) for item in outcome.findings] == [
        ("C_reachable", "confirmed")
    ]
    report = decode_and_validate_artifact_payload(
        payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
        blob=store.read_prepared(outcome.artifacts[0].object_ref),
    )
    assert report["constraint_application"] == [
        {"constraint_id": "C_reachable", "checker_id": "graph", "status": "executed"}
    ]


def test_graph_selection_filters_to_selected_resources_and_rejects_unknown_ids() -> None:
    store = FakeArtifactStore()
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    other = Entity(id="item:other", type=NodeType.ITEM, attrs={})
    dangling = Relation(
        id="r1",
        type=EdgeType.DROPS_FROM,
        src_id="monster:ghost",
        dst_id="npc:1",
    )
    store.register(SNAPSHOT_ID, snapshot_bytes([npc, other], [dangling]))
    selected = _checker_payload().model_copy(
        update={
            "selection": GraphSelectionV1(mode="ids", entity_ids=("item:other",), relation_ids=())
        }
    )
    context = build_context(
        params=selected,
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
    )
    outcome = _handler(store)(context)
    assert "dangling_reference" not in {item.payload.defect_class for item in outcome.findings}

    unknown = selected.model_copy(
        update={"selection": GraphSelectionV1(mode="ids", entity_ids=("missing",), relation_ids=())}
    )
    with pytest.raises(IntegrityViolation, match="absent from the exact snapshot"):
        _handler(store)(
            build_context(
                params=unknown,
                kind=CHECKER_KIND,
                resolved_profiles=context.payload.resolved_profiles,
            )
        )


def test_id_selection_preserves_unlocated_global_unproven_findings() -> None:
    class BudgetExceededAspFactory:
        def build(self, checker_id, *, constraints):
            del constraints
            assert checker_id == "asp"
            from gameforge.spine.checkers.asp import ASPChecker

            return ASPChecker(grounding_budget_atoms=0)

    store = FakeArtifactStore()
    entity = Entity(id="npc:selected", type=NodeType.NPC, attrs={})
    store.register(SNAPSHOT_ID, snapshot_bytes([entity], []))
    payload = _checker_payload(checker_ids=("asp",)).model_copy(
        update={
            "selection": GraphSelectionV1(
                mode="ids",
                entity_ids=("npc:selected",),
                relation_ids=(),
            )
        }
    )
    context = build_context(
        params=payload,
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
    )
    handler = CheckerRunHandler(
        blobs=store,
        store=store,
        checker_factory=BudgetExceededAspFactory(),
    )

    outcome = handler(context)

    assert {(item.payload.defect_class, item.payload.status) for item in outcome.findings} == {
        ("cyclic_dependency", "unproven"),
        ("missing_drop_source", "unproven"),
    }
    assert all(
        not item.payload.entities and not item.payload.relations for item in outcome.findings
    )


def test_missing_navigation_ground_truth_is_explicitly_unproven() -> None:
    store = FakeArtifactStore()
    quest = Entity(id="quest:talk", type=NodeType.QUEST, attrs={})
    giver = Entity(id="npc:giver", type=NodeType.NPC, attrs={})
    step = Entity(
        id="step:talk",
        type=NodeType.QUEST_STEP,
        attrs={"kind": "talk", "target": "npc:target"},
    )
    target = Entity(id="npc:target", type=NodeType.NPC, attrs={})
    relations = (
        Relation(
            id="quest:giver",
            type=EdgeType.STARTS_AT,
            src_id=quest.id,
            dst_id=giver.id,
        ),
        Relation(
            id="quest:step",
            type=EdgeType.HAS_STEP,
            src_id=quest.id,
            dst_id=step.id,
        ),
    )
    store.register(SNAPSHOT_ID, snapshot_bytes([quest, giver, step, target], relations))
    context = build_context(
        params=_checker_payload(defect_classes=("unreachable_target",)),
        kind=CHECKER_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/checker_profile", profile_id="checker", version=1, kind="checker"
            ),
        ),
    )
    outcome = _handler(store)(context)
    assert [(item.payload.defect_class, item.payload.status) for item in outcome.findings] == [
        ("unreachable_target", "unproven")
    ]
