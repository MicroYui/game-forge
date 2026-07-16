"""Task 11a — ``review_runner@1`` (composite) + ``bench_runner@1`` (two-phase)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import gameforge.platform.run_handlers.review as review_mod
from gameforge.contracts.dsl import Constraint, Predicate
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    BenchRunPayloadV1,
    GraphSelectionV1,
    MAX_PREPARED_FINDINGS,
    PreparedRunResult,
    ReviewRunPayloadV1,
)
from gameforge.contracts.model_router import request_hash
from gameforge.contracts.lineage import VersionTuple
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.dsl.compile import compile_all
from gameforge.platform.run_handlers.bench import (
    BENCH_REPORT_SCHEMA_ID,
    BENCH_SEED_DERIVATION_VERSION,
    BenchAggregateInputExpectationV1,
    BenchAggregateInputIdentityV1,
    BenchCaseSpecV1,
    BenchCaseVerdictV1,
    BenchRunHandler,
    BenchVerifiedAggregateInputV1,
)
from gameforge.platform.run_handlers.review import (
    ReviewExecutionConfig,
    ReviewRunHandler,
    ReviewSimConfig,
)
from gameforge.platform.run_handlers.checker import CheckerExecutionPolicy
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed
from gameforge.platform.publication.payload_schema import validate_artifact_payload
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
    snapshot_bytes,
)

REVIEW_KIND = RunKindRef(kind="review.run", version=1)
BENCH_KIND = RunKindRef(kind="bench.run", version=1)
SNAPSHOT_ID = "artifact:snapshot"
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"


# ------------------------------------------------------------------- review
def _combined_snapshot() -> bytes:
    # dangling reference (for the graph checker) + runaway faucet (for the sim).
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    mob = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 60, "gold_max": 140, "kills_per_tick": 10},
    )
    drop = Relation(id="drop", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    dangling = Relation(id="bad", type=EdgeType.SELLS, src_id="shop:ghost", dst_id="gold")
    return snapshot_bytes([gold, mob], [drop, dangling])


def _checker_resolver(profile, constraints):
    return GraphChecker()


class _CompiledCheckerGroup:
    def __init__(self, constraints: list[Constraint], *, include_graph: bool = False) -> None:
        self.id = "compiled-test-group"
        self._checkers = (
            *((GraphChecker(),) if include_graph else ()),
            *compile_all(constraints),
        )

    def check(self, snapshot, nav=None):
        return [
            finding for checker in self._checkers for finding in checker.check(snapshot, nav=nav)
        ]


def _constraint_payload(*constraints: Constraint) -> dict[str, object]:
    return {
        "dsl_grammar_version": "dsl@1",
        "constraints": [
            constraint.model_dump(mode="json", by_alias=True) for constraint in constraints
        ],
    }


def _sim_config_resolver(profile):
    return ReviewSimConfig(n_agents=12, n_ticks=40, max_work_units=2_000_000)


def _review_payload(*, triage: bool) -> ReviewRunPayloadV1:
    return ReviewRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(ProfileRefV1(profile_id="graph", version=1),),
        simulation_profiles=(ProfileRefV1(profile_id="econ", version=1),),
        llm_triage_policy=ProfileRefV1(profile_id="triage", version=1) if triage else None,
    )


def _review_handler(
    store: FakeArtifactStore,
    *,
    execution_config: ReviewExecutionConfig | None = None,
) -> ReviewRunHandler:
    return ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=_checker_resolver,
        sim_config_resolver=_sim_config_resolver,
        execution_config_resolver=lambda _profile: execution_config or ReviewExecutionConfig(),
    )


def _review_profiles():
    return (
        resolved_binding("/params/review_profile", profile_id="review", version=1, kind="review"),
        resolved_binding("/params/checker_profiles", profile_id="graph", version=1, kind="checker"),
        resolved_binding(
            "/params/simulation_profiles", profile_id="econ", version=1, kind="simulation"
        ),
    )


def test_review_without_triage_is_deterministic_only_and_not_applicable() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge()
    context = build_context(
        params=_review_payload(triage=False),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
        model_bridge=bridge,
    )
    outcome = _review_handler(store)(context)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "review_completed"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "review_report"
    assert primary.payload_schema_id == "review@1"
    assert primary.meta["llm_execution_mode"] == "not_applicable"
    assert primary.meta["llm_triage_applied"] is False
    # No triage policy => the bridge is never touched.
    assert bridge.requests == []
    # output checker_run + simulation_run artifacts, each carrying its /profile.
    kinds = [artifact.kind for artifact in outcome.artifacts]
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    checker_artifact = next(a for a in outcome.artifacts if a.kind == "checker_run")
    payload = json.loads(store.read_prepared(checker_artifact.object_ref))
    assert payload["profile"] == {"profile_id": "graph", "version": 1}
    simulation_artifact = next(a for a in outcome.artifacts if a.kind == "simulation_run")
    simulation_payload = json.loads(store.read_prepared(simulation_artifact.object_ref))
    expected_child_seed = derive_validation_subseed(
        root_seed=5,
        run_kind=REVIEW_KIND,
        profile=ProfileRefV1(profile_id="econ", version=1),
        case_id=f"review:{simulation_payload['snapshot_id']}",
        replication_index=0,
    )
    # Artifact/payload seed is the Run root. The exact child execution seed is
    # retained in the bounded subseed derivation evidence.
    assert simulation_payload["seed"] == 5
    assert simulation_payload["sensitivity"]["seed_binding"]["root_seed"] == 5
    assert simulation_payload["sensitivity"]["seed_binding"]["seed"] == expected_child_seed
    assert simulation_payload["sensitivity"]["execution_binding"] == {
        "simulation_profile": {"profile_id": "econ", "version": 1},
        "constraint_ids": [],
        "constraint_application": {"status": "not_applicable"},
    }
    validate_artifact_payload(payload_schema_id="simulation-result@1", payload=simulation_payload)
    assert simulation_artifact.version_tuple.seed == 5
    assert checker_artifact.version_tuple.seed is None
    assert primary.version_tuple.seed is None
    # authoritative deterministic verdict present (checker + simulation only).
    oracle_types = {f.payload.oracle_type for f in outcome.findings}
    assert oracle_types == {"deterministic", "simulation"}
    assert all(f.payload.producer_run_id == "run:1" for f in outcome.findings)
    checker_index = outcome.artifacts.index(checker_artifact)
    simulation_index = outcome.artifacts.index(simulation_artifact)
    assert all(
        finding.evidence_artifact_index == checker_index
        for finding in outcome.findings
        if finding.payload.oracle_type == "deterministic"
    )
    assert all(
        finding.evidence_artifact_index == simulation_index
        for finding in outcome.findings
        if finding.payload.oracle_type == "simulation"
    )


def test_review_rejects_noop_without_deterministic_profiles() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    payload = _review_payload(triage=False).model_copy(
        update={"checker_profiles": (), "simulation_profiles": ()}
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        resolved_profiles=(_review_profiles()[0],),
    )

    with pytest.raises(IntegrityViolation, match="at least one deterministic"):
        _review_handler(store)(context)


def test_review_rejects_constraint_inputs_without_a_checker_profile() -> None:
    store = FakeArtifactStore()
    payload = _review_payload(triage=False).model_copy(
        update={
            "constraint_snapshot_artifact_id": "artifact:constraints",
            "checker_profiles": (),
        }
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=(_review_profiles()[0], _review_profiles()[2]),
    )

    with pytest.raises(IntegrityViolation, match="constraints require a checker"):
        _review_handler(store)(context)


def test_review_rejects_partial_selection_for_full_graph_simulation_profile() -> None:
    store = FakeArtifactStore()
    payload = _review_payload(triage=False).model_copy(
        update={
            "selection": GraphSelectionV1(
                mode="ids",
                entity_ids=("gold",),
                relation_ids=(),
            )
        }
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
    )

    with pytest.raises(IntegrityViolation, match="require full graph selection"):
        _review_handler(store)(context)


def test_review_without_triage_preserves_mixed_predicate_as_profile_advisory() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    constraint_artifact_id = "artifact:mixed-constraints"
    constraint = Constraint(
        id="C_semantic_price",
        kind="numeric",
        oracle="mixed",
        predicates=(Predicate(expr="semantic_price(item)", oracle="llm-assisted"),),
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store.register(constraint_artifact_id, _constraint_payload(constraint))
    payload = _review_payload(triage=False).model_copy(
        update={
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "simulation_profiles": (),
        }
    )
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=lambda _profile, constraints: _CompiledCheckerGroup(constraints),
        sim_config_resolver=_sim_config_resolver,
    )
    outcome = handler(
        build_context(
            params=payload,
            kind=REVIEW_KIND,
            resolved_profiles=_review_profiles()[:2],
            version_tuple=VersionTuple(
                constraint_snapshot_id="constraint:semantic:1",
                tool_version="handler@1",
            ),
        )
    )

    assert len(outcome.findings) == 1
    advisory = outcome.findings[0]
    assert advisory.payload.source == "llm"
    assert advisory.payload.oracle_type == "llm-assisted"
    assert advisory.payload.status == "unproven"
    assert advisory.payload.constraint_id == constraint.id
    assert advisory.finding_id.startswith("profile:graph@1:constraint:C_semantic_price:")
    # The Finding is evidenced by the exact per-profile checker Artifact, not
    # merely by the aggregate ReviewReport.
    assert advisory.evidence_artifact_index == 1
    assert outcome.artifacts[1].kind == "checker_run"
    checker_payload = json.loads(store.read_prepared(outcome.artifacts[1].object_ref))
    assert checker_payload["profile"] == {"profile_id": "graph", "version": 1}
    assert checker_payload["findings"][0]["id"] == advisory.finding_id

    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    assert report["deterministic_findings"] == []
    assert report["unproven_findings"] == []
    assert [item["id"] for item in report["llm_assisted_findings"]] == [advisory.finding_id]


def test_review_triage_excludes_mixed_predicate_placeholder_from_authoritative_input() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    constraint_artifact_id = "artifact:mixed-constraints"
    constraint = Constraint(
        id="C_semantic_price",
        kind="numeric",
        oracle="mixed",
        predicates=(Predicate(expr="semantic_price(item)", oracle="llm-assisted"),),
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store.register(constraint_artifact_id, _constraint_payload(constraint))
    payload = _review_payload(triage=True).model_copy(
        update={
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "simulation_profiles": (),
        }
    )
    bridge = FakeModelBridge(responses=("{}",))
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=lambda _profile, constraints: _CompiledCheckerGroup(
            constraints, include_graph=True
        ),
        sim_config_resolver=_sim_config_resolver,
    )
    outcome = handler(
        build_context(
            params=payload,
            kind=REVIEW_KIND,
            resolved_profiles=_review_profiles()[:2],
            llm_execution_mode="replay",
            plan=execution_plan({"review-triage": MODEL_REF}),
            cassette_artifact_id="artifact:cassette",
            model_bridge=bridge,
            version_tuple=VersionTuple(
                constraint_snapshot_id="constraint:semantic:1",
                tool_version="handler@1",
            ),
        )
    )

    assert len(bridge.requests) == 1
    prompt = bridge.requests[0].model_request.messages[-1].content
    triage_input = json.loads(prompt)
    assert triage_input["deterministic_findings"]
    assert all(
        item["defect_class"] != "llm_assisted_predicate"
        for item in triage_input["deterministic_findings"]
    )
    assert any(
        item.payload.oracle_type == "llm-assisted" and item.payload.constraint_id == constraint.id
        for item in outcome.findings
    )


def test_review_constrained_simulation_is_explicitly_unproven_and_profile_evidenced() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    constraint_artifact_id = "artifact:simulation-constraints"
    constraint = Constraint(
        id="C_gold_cap",
        kind="numeric",
        oracle="deterministic",
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    store.register(constraint_artifact_id, _constraint_payload(constraint))
    payload = _review_payload(triage=False).model_copy(
        update={"constraint_snapshot_artifact_id": constraint_artifact_id}
    )
    outcome = _review_handler(store)(
        build_context(
            params=payload,
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
            version_tuple=VersionTuple(
                constraint_snapshot_id="constraint:semantic:1",
                seed=5,
                tool_version="handler@1",
            ),
        )
    )

    simulation_index = next(
        index
        for index, artifact in enumerate(outcome.artifacts)
        if artifact.kind == "simulation_run"
    )
    simulation_payload = json.loads(
        store.read_prepared(outcome.artifacts[simulation_index].object_ref)
    )
    execution = simulation_payload["sensitivity"]["execution_binding"]
    assert execution == {
        "simulation_profile": {"profile_id": "econ", "version": 1},
        "constraint_snapshot_artifact_id": constraint_artifact_id,
        "constraint_ids": [constraint.id],
        "constraint_application": {
            "status": "unproven",
            "reason_code": "constraint_profile_not_executable",
        },
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="simulation-result@1", payload=simulation_payload
        )["sensitivity"]["execution_binding"]
        == execution
    )

    unproven = [
        item
        for item in outcome.findings
        if item.payload.defect_class == "simulation_constraint_unproven"
    ]
    assert len(unproven) == 1
    assert unproven[0].payload.status == "unproven"
    assert unproven[0].finding_id.startswith("profile:econ@1:")
    assert unproven[0].evidence_artifact_index == simulation_index
    assert all(
        item.evidence_artifact_index == simulation_index
        for item in outcome.findings
        if item.payload.oracle_type == "simulation"
    )

    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    assert any(item["id"] == unproven[0].finding_id for item in report["unproven_findings"])


@pytest.mark.parametrize(
    "updates",
    (
        {"snapshot_id": "snapshot:substituted"},
        {"source": "llm"},
        {"oracle_type": "llm-assisted"},
        {"status": "dismissed"},
        {"source": "llm", "oracle_type": "llm-assisted", "status": "unproven"},
    ),
)
def test_review_rejects_checker_finding_outside_exact_oracle_authority(
    updates: dict[str, str],
) -> None:
    class SpoofingChecker:
        id = "spoofing"

        def check(self, snapshot, nav=None):
            del nav
            finding = Finding(
                id="finding:spoofed",
                source="checker",
                producer_id=self.id,
                producer_run_id="run:spoofed",
                oracle_type="deterministic",
                defect_class="spoofed",
                severity="major",
                snapshot_id=snapshot.snapshot_id,
                status="confirmed",
                message="spoofed authority",
            )
            return [finding.model_copy(update=updates)]

    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    payload = _review_payload(triage=False).model_copy(update={"simulation_profiles": ()})
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=lambda _profile, _constraints: SpoofingChecker(),
        sim_config_resolver=_sim_config_resolver,
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        resolved_profiles=_review_profiles()[:2],
    )

    with pytest.raises(IntegrityViolation, match="exact oracle authority"):
        handler(context)

    assert store.put_count == 0


def test_review_rejects_excess_findings_before_staging_any_artifact() -> None:
    class AmplifyingChecker:
        id = "amplifying"

        def check(self, snapshot, nav=None):
            del nav
            finding = Finding(
                id="finding:amplified",
                source="checker",
                producer_id=self.id,
                producer_run_id="run:amplified",
                oracle_type="deterministic",
                defect_class="amplified",
                severity="major",
                snapshot_id=snapshot.snapshot_id,
                status="confirmed",
                message="amplified output",
            )
            return [finding] * (MAX_PREPARED_FINDINGS + 1)

    inputs = FakeArtifactStore()
    inputs.register(SNAPSHOT_ID, _combined_snapshot())
    staged = review_mod.PreparedArtifactBatchStore()
    payload = _review_payload(triage=False).model_copy(update={"simulation_profiles": ()})
    handler = ReviewRunHandler(
        blobs=inputs,
        store=staged,
        checker_resolver=lambda _profile, _constraints: AmplifyingChecker(),
        sim_config_resolver=_sim_config_resolver,
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        resolved_profiles=_review_profiles()[:2],
    )

    with pytest.raises(IntegrityViolation, match="frozen output bound"):
        handler(context)

    assert staged.staged_artifact_count == 0
    assert staged.staged_bytes == 0


def test_review_triage_adds_llm_suggestions_without_changing_verdict() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge(
        responses=(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "defect_class": "balance_smell",
                            "severity": "minor",
                            "message": "hot faucet",
                        }
                    ]
                }
            ),
        )
    )
    context = build_context(
        params=_review_payload(triage=True),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
        llm_execution_mode="replay",
        plan=execution_plan({"review-triage": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )
    outcome = _review_handler(store)(context)

    primary = outcome.artifacts[outcome.primary_index]
    assert primary.meta["llm_execution_mode"] == "replay"
    assert primary.meta["llm_triage_applied"] is True
    # exactly one ordered triage call went through the bridge.
    assert len(bridge.requests) == 1
    assert bridge.requests[0].source_artifact_ids == (SNAPSHOT_ID,)

    by_oracle = {}
    for finding in outcome.findings:
        by_oracle.setdefault(finding.payload.oracle_type, []).append(finding)
    assert "deterministic" in by_oracle and "simulation" in by_oracle
    suggestions = by_oracle.get("llm-assisted", [])
    assert suggestions, "triage suggestion must be recorded as llm-assisted"
    for suggestion in suggestions:
        assert suggestion.payload.source == "llm"
        assert suggestion.payload.status == "unproven"  # advisory, never a proven verdict
    # the deterministic findings are unchanged (still confirmed).
    assert all(f.payload.status == "confirmed" for f in by_oracle["deterministic"])


def test_review_prompt_profile_cap_rejects_before_bridge_or_object_write() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge(responses=("{}",))
    context = build_context(
        params=_review_payload(triage=True),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
        llm_execution_mode="replay",
        plan=execution_plan({"review-triage": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )

    with pytest.raises(IntegrityViolation, match="profile byte limit"):
        _review_handler(
            store,
            execution_config=ReviewExecutionConfig(max_prompt_message_bytes=1),
        )(context)

    assert bridge.requests == []
    assert store.put_count == 0


def test_review_triage_prompt_projection_is_stable_and_byte_bounded() -> None:
    findings = [
        Finding(
            id=f"finding:{index:04d}",
            source="checker",
            producer_id="graph",
            producer_run_id="run:graph",
            oracle_type="deterministic",
            defect_class="oversized_message",
            severity="major",
            snapshot_id="snapshot:bounded",
            status="confirmed",
            message="界" * 5_000,
        )
        for index in reversed(range(review_mod.TRIAGE_MAX_INPUT_FINDINGS + 100))
    ]

    prompt = review_mod._triage_prompt("snapshot:bounded", findings)
    reversed_prompt = review_mod._triage_prompt("snapshot:bounded", list(reversed(findings)))
    body = json.loads(prompt)

    assert prompt == reversed_prompt
    assert len(prompt.encode("utf-8")) <= review_mod.TRIAGE_MAX_PROMPT_BYTES
    assert len(body["deterministic_findings"]) <= review_mod.TRIAGE_MAX_INPUT_FINDINGS
    assert body["projection"]["truncated"] is True


def test_review_triage_output_parser_bounds_suggestions_and_fields() -> None:
    suggestions = [
        {
            "defect_class": "d" * 1_000,
            "severity": "major",
            "message": "m" * 5_000,
            "entities": [f"entity:{index}:" + "e" * 600 for index in range(100)],
        },
        *(
            {"defect_class": f"suggestion:{index}", "message": "advisory"}
            for index in range(review_mod.TRIAGE_MAX_SUGGESTIONS + 50)
        ),
    ]

    parsed = review_mod._parse_triage_suggestions(
        json.dumps({"suggestions": suggestions}),
        "snapshot:bounded",
    )

    assert len(parsed) == review_mod.TRIAGE_MAX_SUGGESTIONS
    assert len(parsed[0].defect_class.encode("utf-8")) <= (review_mod.TRIAGE_MAX_DEFECT_CLASS_BYTES)
    assert len(parsed[0].message.encode("utf-8")) <= (
        review_mod.TRIAGE_MAX_SUGGESTION_MESSAGE_BYTES
    )
    assert len(parsed[0].entities) == review_mod.TRIAGE_MAX_SUGGESTION_ENTITIES
    assert all(
        len(entity.encode("utf-8")) <= review_mod.TRIAGE_MAX_ENTITY_ID_BYTES
        for entity in parsed[0].entities
    )


def test_oversized_triage_response_adds_no_advisory_and_preserves_oracles() -> None:
    baseline_store = FakeArtifactStore()
    baseline_store.register(SNAPSHOT_ID, _combined_snapshot())
    baseline = _review_handler(baseline_store)(
        build_context(
            params=_review_payload(triage=False),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
        )
    )
    triage_store = FakeArtifactStore()
    triage_store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge(responses=("x" * (review_mod.TRIAGE_MAX_RESPONSE_BYTES + 1),))
    triaged = _review_handler(triage_store)(
        build_context(
            params=_review_payload(triage=True),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
            llm_execution_mode="replay",
            plan=execution_plan({"review-triage": MODEL_REF}),
            cassette_artifact_id="artifact:cassette",
            model_bridge=bridge,
        )
    )

    baseline_oracles = [
        (item.payload.oracle_type, item.payload.defect_class, item.payload.status)
        for item in baseline.findings
    ]
    triaged_oracles = [
        (item.payload.oracle_type, item.payload.defect_class, item.payload.status)
        for item in triaged.findings
    ]
    assert triaged_oracles == baseline_oracles
    assert bridge.requests


def test_review_without_triage_is_byte_deterministic() -> None:
    store_a, store_b = FakeArtifactStore(), FakeArtifactStore()
    store_a.register(SNAPSHOT_ID, _combined_snapshot())
    store_b.register(SNAPSHOT_ID, _combined_snapshot())
    out_a = _review_handler(store_a)(
        build_context(
            params=_review_payload(triage=False),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
        )
    )
    out_b = _review_handler(store_b)(
        build_context(
            params=_review_payload(triage=False),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
        )
    )
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_review_two_profiles_same_checker_id_emit_distinct_finding_ids() -> None:
    # M1 carry-forward fix: two checker_profiles resolving to the SAME checker id on
    # the SAME snapshot must not collide on one finding-series head (the finding CAS
    # would otherwise reject the duplicate at publish).
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    payload = ReviewRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(
            ProfileRefV1(profile_id="graph-a", version=1),
            ProfileRefV1(profile_id="graph-b", version=1),
        ),
        simulation_profiles=(),
        llm_triage_policy=None,
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/review_profile", profile_id="review", version=1, kind="review"
            ),
            resolved_binding(
                "/params/checker_profiles", profile_id="graph-a", version=1, kind="checker"
            ),
        ),
    )
    outcome = _review_handler(store)(context)

    checker_findings = [f for f in outcome.findings if f.payload.oracle_type == "deterministic"]
    assert checker_findings, "the dangling reference must surface a deterministic finding"
    finding_ids = [f.finding_id for f in checker_findings]
    # each deterministic finding is present once per resolving profile, but the ids
    # are profile-scoped so no two collide on one series head.
    assert len(finding_ids) == len(set(finding_ids))
    assert all(len(finding_id) <= 4096 for finding_id in finding_ids)
    assert any(fid.startswith("profile:graph-a@1:") for fid in finding_ids)
    assert any(fid.startswith("profile:graph-b@1:") for fid in finding_ids)


def test_review_checker_budget_rejects_before_profile_backend_execution() -> None:
    calls = 0

    def never_resolve(profile, constraints):
        nonlocal calls
        calls += 1
        raise AssertionError("checker resolver must not execute")

    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    payload = _review_payload(triage=False).model_copy(update={"simulation_profiles": ()})
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=never_resolve,
        sim_config_resolver=_sim_config_resolver,
        checker_execution_policy_resolver=lambda _profile: CheckerExecutionPolicy(
            allowed_checker_ids=("graph",),
            allowed_defect_classes=("dangling_reference",),
            max_direct_checker_count=1,
            max_constraint_count=1,
            max_work_units=1,
        ),
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        resolved_profiles=(_review_profiles()[0], _review_profiles()[1]),
    )

    with pytest.raises(IntegrityViolation, match="work budget"):
        handler(context)

    assert calls == 0


def test_review_simulation_profiles_share_one_run_level_work_budget() -> None:
    class NeverCalledSimulator:
        calls = 0

        def run(self, model, seed, n_agents, n_ticks):
            del model, seed, n_agents, n_ticks
            self.calls += 1
            raise AssertionError("simulator must not run")

    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    simulation_profiles = (
        ProfileRefV1(profile_id="econ-a", version=1),
        ProfileRefV1(profile_id="econ-b", version=1),
    )
    payload = _review_payload(triage=False).model_copy(
        update={"checker_profiles": (), "simulation_profiles": simulation_profiles}
    )
    simulator = NeverCalledSimulator()
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=_checker_resolver,
        sim_config_resolver=lambda _profile: ReviewSimConfig(
            n_agents=12,
            n_ticks=40,
            # Each profile's ~6.7k work is legal; their sum is not.
            max_work_units=10_000,
        ),
        simulator=simulator,
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=(
            resolved_binding(
                "/params/review_profile", profile_id="review", version=1, kind="review"
            ),
            *(
                resolved_binding(
                    f"/params/simulation_profiles/{index}",
                    profile_id=profile.profile_id,
                    version=profile.version,
                    kind="simulation",
                )
                for index, profile in enumerate(simulation_profiles)
            ),
        ),
    )

    with pytest.raises(IntegrityViolation, match="aggregate exact work budget"):
        handler(context)

    assert simulator.calls == 0


def test_review_aggregate_output_budget_rejects_before_any_object_write() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=_checker_resolver,
        sim_config_resolver=_sim_config_resolver,
        execution_config_resolver=lambda _profile: ReviewExecutionConfig(
            max_total_prepared_artifact_bytes=1
        ),
    )
    context = build_context(
        params=_review_payload(triage=False),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
    )

    with pytest.raises(IntegrityViolation, match="aggregate byte bound"):
        handler(context)

    assert store.put_count == 0


def test_review_profile_count_rejects_before_checker_or_simulation_execution() -> None:
    checker_calls = 0

    def never_resolve(_profile, _constraints):
        nonlocal checker_calls
        checker_calls += 1
        raise AssertionError("checker must not resolve")

    store = FakeArtifactStore()
    handler = ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=never_resolve,
        sim_config_resolver=_sim_config_resolver,
        execution_config_resolver=lambda _profile: ReviewExecutionConfig(
            max_checker_profile_count=0,
            max_simulation_profile_count=0,
        ),
    )
    context = build_context(
        params=_review_payload(triage=False),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
    )

    with pytest.raises(IntegrityViolation, match="count budget"):
        handler(context)

    assert checker_calls == 0
    assert store.put_count == 0


# -------------------------------------------------------------------- bench
class _FakeCaseLoader:
    def __init__(self, cases):
        self._cases = cases
        self.seen = None

    def load_cases(
        self,
        *,
        dataset_artifact_id,
        benchmark_spec_artifact_id,
        partition_ids,
        root_seed,
        seed_derivation_version,
        evaluator_profile,
        execution_profile_catalog_version,
        execution_profile_catalog_digest,
        repetition_count,
        execution_scope,
    ):
        self.seen = (
            dataset_artifact_id,
            benchmark_spec_artifact_id,
            partition_ids,
            root_seed,
            seed_derivation_version,
            evaluator_profile,
            execution_profile_catalog_version,
            execution_profile_catalog_digest,
            repetition_count,
            execution_scope,
        )
        return self._cases


class _FakeEvaluator:
    def __init__(self):
        self.deterministic_calls = 0
        self.agent_calls = 0
        self.requests = []

    def evaluate(self, request, *, agent_invoker):
        self.requests.append(request)
        if request.case.mode == "agent":
            self.agent_calls += 1
            assert agent_invoker is not None
            text = agent_invoker(
                request.case.prompt,
                source_suffix=request.agent_source_suffix,
            )
            return BenchCaseVerdictV1(status=text.strip() or "pass")
        self.deterministic_calls += 1
        assert agent_invoker is None
        return BenchCaseVerdictV1(status="pass")


class _FakeAggregateVerifier:
    def __init__(self, identities):
        self._identities = identities
        self.seen = []

    def load_verified(self, *, expectation, request):
        blob = b"x"
        self.seen.append((expectation, request, blob))
        return BenchVerifiedAggregateInputV1(
            identity=self._identities[expectation.artifact_id],
            blob=blob,
        )


class _FakeComposer:
    def __init__(self):
        self.execute_calls = []
        self.aggregate_calls = []

    def compose_execute(self, *, case_results, seed, partition_ids, **_):
        self.execute_calls.append((case_results, seed, partition_ids))
        return json.dumps(
            {
                "schema_version": BENCH_REPORT_SCHEMA_ID,
                "phase": "execute_cases",
                "seed": seed,
                "cases": [
                    {"case_id": r.case_id, "partition_id": r.partition_id, "status": r.status}
                    for r in case_results
                ],
            },
            sort_keys=True,
        ).encode("utf-8")

    def compose_aggregate(self, *, case_result_blobs, seed, **_):
        self.aggregate_calls.append((case_result_blobs, seed))
        return json.dumps(
            {
                "schema_version": BENCH_REPORT_SCHEMA_ID,
                "phase": "aggregate_results",
                "seed": seed,
                "case_result_ids": [item.identity.artifact_id for item in case_result_blobs],
            },
            sort_keys=True,
        ).encode("utf-8")


def _bench_execute_payload(*, repetitions: int = 1) -> BenchRunPayloadV1:
    return BenchRunPayloadV1(
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        partition_ids=("p1",),
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        repetition_count=repetitions,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )


def _bench_aggregate_payload() -> BenchRunPayloadV1:
    return BenchRunPayloadV1(
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        partition_ids=("p1",),
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        repetition_count=1,
        execution_scope="aggregate_results",
        case_result_artifact_ids=("case:1", "case:2"),
    )


def _aggregate_identity(
    *, artifact_id: str, case: BenchCaseSpecV1, replication_index: int = 0
) -> BenchAggregateInputIdentityV1:
    return BenchAggregateInputIdentityV1(
        artifact_id=artifact_id,
        case_id=case.case_id,
        partition_id=case.partition_id,
        mode=case.mode,
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        run_kind=BENCH_KIND,
        root_seed=None,
        replication_index=replication_index,
        execution_seed=None,
        seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
    )


def _aggregate_case(
    *, case_id: str, artifact_id: str, mode: str = "deterministic", prompt: str = ""
) -> BenchCaseSpecV1:
    return BenchCaseSpecV1(
        case_id=case_id,
        partition_id="p1",
        mode=mode,
        prompt=prompt,
        aggregate_inputs=(
            BenchAggregateInputExpectationV1(
                artifact_id=artifact_id,
                payload_hash="0" * 64,
                payload_size_bytes=1,
                artifact_kind="checker_run",
                payload_schema_id="checker-report@1",
            ),
        ),
    )


def test_bench_execute_cases_runs_ordered_agent_cassette_and_seals_report() -> None:
    store = FakeArtifactStore()
    cases = (
        BenchCaseSpecV1(case_id="c0", partition_id="p1", mode="deterministic"),
        BenchCaseSpecV1(case_id="c1", partition_id="p1", mode="agent", prompt="score c1"),
        BenchCaseSpecV1(case_id="c2", partition_id="p1", mode="agent", prompt="score c2"),
    )
    loader, evaluator, composer = _FakeCaseLoader(cases), _FakeEvaluator(), _FakeComposer()
    bridge = FakeModelBridge(responses=("pass", "pass", "pass", "pass"))
    handler = BenchRunHandler(
        blobs=store, store=store, case_loader=loader, evaluator=evaluator, composer=composer
    )
    context = build_context(
        params=_bench_execute_payload(repetitions=2),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )
    outcome = handler(context)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "bench_completed"
    assert outcome.findings == ()  # bench finding policy is null
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "bench_report"
    assert primary.payload_schema_id == BENCH_REPORT_SCHEMA_ID
    assert primary.lineage == ("artifact:dataset", "artifact:spec")

    assert evaluator.deterministic_calls == 2 and evaluator.agent_calls == 4
    requests = evaluator.requests
    assert [
        (request.case.case_id, request.case_ordinal, request.replication_index)
        for request in requests
    ] == [
        ("c0", 0, 0),
        ("c0", 0, 1),
        ("c1", 1, 0),
        ("c1", 1, 1),
        ("c2", 2, 0),
        ("c2", 2, 1),
    ]
    assert all(request.evaluator_profile.profile_id == "eval" for request in requests)
    assert all(request.dataset_artifact_id == "artifact:dataset" for request in requests)
    assert all(request.benchmark_spec_artifact_id == "artifact:spec" for request in requests)
    assert all(request.root_seed == 3 for request in requests)
    assert [request.result_ordinal for request in requests] == list(range(6))
    assert loader.seen == (
        "artifact:dataset",
        "artifact:spec",
        ("p1",),
        3,
        BENCH_SEED_DERIVATION_VERSION,
        ProfileRefV1(profile_id="eval", version=1),
        1,
        "a" * 64,
        2,
        "execute_cases",
    )
    assert [request.execution_seed for request in requests] == [
        derive_validation_subseed(
            root_seed=3,
            run_kind=BENCH_KIND,
            profile=ProfileRefV1(profile_id="eval", version=1),
            case_id=request.case.case_id,
            replication_index=request.replication_index,
        )
        for request in requests
    ]
    results, report_seed, partition_ids = composer.execute_calls[0]
    assert report_seed == 3
    assert partition_ids == ("p1",)
    assert [result.result_ordinal for result in results] == list(range(6))
    assert [result.model_call_ordinals for result in results] == [
        (),
        (),
        (1,),
        (2,),
        (3,),
        (4,),
    ]
    assert [result.replication_index for result in results] == [0, 1, 0, 1, 0, 1]
    suffixes = [result.agent_source_suffix for result in results]
    assert suffixes[:2] == [None, None]
    assert all(value is not None and value.startswith("sha256:") for value in suffixes[2:])
    assert len(set(suffixes[2:])) == 4
    # ONE ordered run-scoped cassette: all Agent replications share one monotonic sequence.
    assert len(bridge.requests) == 4
    scopes = [request.idempotency_scope for request in bridge.requests]
    assert scopes == ["run:run:1:attempt:1"] * 4
    keys = [request.idempotency_key for request in bridge.requests]
    assert keys == ["model:1", "model:2", "model:3", "model:4"]
    # Each scoped case/replication starts a fresh causal prompt context even though
    # all calls share one monotonic run cassette.
    assert [request.prompt_context.include_previous_consumption for request in bridge.requests] == [
        False,
        False,
        False,
        False,
    ]


def test_bench_deterministic_execute_preserves_absent_root_seed() -> None:
    store = FakeArtifactStore()
    cases = (BenchCaseSpecV1(case_id="c0", partition_id="p1", mode="deterministic"),)
    evaluator, composer = _FakeEvaluator(), _FakeComposer()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=evaluator,
        composer=composer,
    )
    context = build_context(
        params=_bench_execute_payload(repetitions=2),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )

    outcome = handler(context)

    assert [request.root_seed for request in evaluator.requests] == [None, None]
    assert [request.execution_seed for request in evaluator.requests] == [None, None]
    results, report_seed, _ = composer.execute_calls[0]
    assert report_seed is None
    assert [result.root_seed for result in results] == [None, None]
    assert [result.execution_seed for result in results] == [None, None]
    assert outcome.artifacts[outcome.primary_index].version_tuple.seed is None


def test_bench_rejects_non_json_evaluator_metric_signal_at_handler_boundary() -> None:
    class _InvalidMetricsEvaluator:
        def evaluate(self, request, *, agent_invoker):
            assert request.case.mode == "deterministic"
            assert agent_invoker is None
            return BenchCaseVerdictV1(status="pass", metrics={"bad": object()})

    store = FakeArtifactStore()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(
            (BenchCaseSpecV1(case_id="c0", partition_id="p1", mode="deterministic"),)
        ),
        evaluator=_InvalidMetricsEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )

    with pytest.raises(ValueError, match="metrics are not canonical JSON"):
        handler(context)


def test_bench_rejects_non_binary_case_status_before_composition() -> None:
    class _InvalidStatusEvaluator:
        def evaluate(self, request, *, agent_invoker):
            assert request.case.mode == "deterministic"
            assert agent_invoker is None
            return BenchCaseVerdictV1(status="unknown")

    store = FakeArtifactStore()
    composer = _FakeComposer()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(
            (BenchCaseSpecV1(case_id="c0", partition_id="p1", mode="deterministic"),)
        ),
        evaluator=_InvalidStatusEvaluator(),
        composer=composer,
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile",
                profile_id="eval",
                version=1,
                kind="bench_evaluator",
            ),
        ),
    )

    with pytest.raises(ValueError, match="non-binary status"):
        handler(context)
    assert composer.execute_calls == []


@pytest.mark.parametrize(("limit", "accepted"), ((2, True), (1, False)))
def test_bench_enforces_the_stricter_spec_run_total_metrics_limit(
    limit: int,
    accepted: bool,
) -> None:
    store = FakeArtifactStore()
    composer = _FakeComposer()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(
            (
                BenchCaseSpecV1(
                    case_id="c0",
                    partition_id="p1",
                    mode="deterministic",
                    result_metrics_bytes_total_limit=limit,
                ),
            )
        ),
        evaluator=_FakeEvaluator(),
        composer=composer,
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile",
                profile_id="eval",
                version=1,
                kind="bench_evaluator",
            ),
        ),
    )

    if accepted:
        handler(context)
        assert len(composer.execute_calls) == 1
    else:
        with pytest.raises(ValueError, match="Run-total bound"):
            handler(context)
        assert composer.execute_calls == []


def test_bench_agent_replication_fails_closed_without_any_model_call() -> None:
    class _SkippingAgentEvaluator:
        def evaluate(self, request, *, agent_invoker):
            assert request.case.mode == "agent"
            assert agent_invoker is not None
            return BenchCaseVerdictV1(status="pass")

    store = FakeArtifactStore()
    cases = (BenchCaseSpecV1(case_id="c1", partition_id="p1", mode="agent", prompt="c1"),)
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_SkippingAgentEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=FakeModelBridge(),
    )

    with pytest.raises(ValueError, match="exact model-call count"):
        handler(context)


def test_bench_rejects_agent_call_product_before_first_model_call() -> None:
    store = FakeArtifactStore()
    bridge = FakeModelBridge()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(
            (
                BenchCaseSpecV1(
                    case_id="c1",
                    partition_id="p1",
                    mode="agent",
                    prompt="c1",
                    agent_prompt_count=32,
                    agent_model_calls_total_limit=31,
                ),
            )
        ),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile",
                profile_id="eval",
                version=1,
                kind="bench_evaluator",
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )

    with pytest.raises(ValueError, match="Agent calls exceed"):
        handler(context)
    assert bridge.requests == []


def test_bench_agent_replication_preserves_a_multi_call_ordered_cassette() -> None:
    class _MultiCallAgentEvaluator:
        def evaluate(self, request, *, agent_invoker):
            assert request.case.mode == "agent"
            assert agent_invoker is not None
            first = agent_invoker(
                request.case.prompt,
                source_suffix=request.agent_source_suffix,
            )
            second = agent_invoker(
                f"refine:{first}",
                source_suffix=request.agent_source_suffix,
            )
            return BenchCaseVerdictV1(status=second)

    store = FakeArtifactStore()
    cases = (
        BenchCaseSpecV1(
            case_id="c1",
            partition_id="p1",
            mode="agent",
            prompt="c1",
            agent_prompt_count=2,
        ),
    )
    composer = _FakeComposer()
    bridge = FakeModelBridge(responses=("draft", "pass"))
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_MultiCallAgentEvaluator(),
        composer=composer,
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )

    handler(context)

    results, _, _ = composer.execute_calls[0]
    assert results[0].model_call_ordinals == (1, 2)
    assert [request.idempotency_key for request in bridge.requests] == ["model:1", "model:2"]
    assert [request.prompt_context.include_previous_consumption for request in bridge.requests] == [
        False,
        True,
    ]


def test_bench_same_prompt_is_bound_to_distinct_opaque_case_request_hashes() -> None:
    store = FakeArtifactStore()
    cases = (
        BenchCaseSpecV1(case_id="case:secret-a", partition_id="p1", mode="agent", prompt="same"),
        BenchCaseSpecV1(case_id="case:secret-b", partition_id="p1", mode="agent", prompt="same"),
    )
    bridge = FakeModelBridge(responses=("pass", "pass"))
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )

    handler(context)

    prompts = [item.model_request.messages[-1].content for item in bridge.requests]
    assert prompts[0] != prompts[1]
    assert all("<gameforge-bench-binding>sha256:" in prompt for prompt in prompts)
    assert all("case:secret" not in prompt for prompt in prompts)
    assert len({request_hash(item.model_request) for item in bridge.requests}) == 2


def test_bench_agent_rejects_a_bridge_that_changes_the_logical_call_head() -> None:
    class _ShiftedOrdinalBridge(FakeModelBridge):
        def call_model(self, request):
            result = super().call_model(request)
            result.link.call_ordinal += 1
            return result

    store = FakeArtifactStore()
    cases = (BenchCaseSpecV1(case_id="c1", partition_id="p1", mode="agent", prompt="c1"),)
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=_ShiftedOrdinalBridge(responses=("pass",)),
    )

    with pytest.raises(ValueError, match="one ordered cassette sequence"):
        handler(context)


def test_bench_aggregate_consumes_case_results_and_binds_lineage() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    store.register("case:2", {"case": 2})
    # Artifact ids are intentionally the reverse of semantic case order.  The handler
    # must verify every input identity and feed the composer canonical case order.
    cases = (
        _aggregate_case(case_id="c2", artifact_id="case:2"),
        _aggregate_case(case_id="c1", artifact_id="case:1"),
    )
    verifier = _FakeAggregateVerifier(
        {
            "case:1": _aggregate_identity(artifact_id="case:1", case=cases[1]),
            "case:2": _aggregate_identity(artifact_id="case:2", case=cases[0]),
        }
    )
    loader, evaluator, composer = _FakeCaseLoader(cases), _FakeEvaluator(), _FakeComposer()
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=loader,
        evaluator=evaluator,
        composer=composer,
        aggregate_input_verifier=verifier,
    )
    context = build_context(
        params=_bench_aggregate_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )
    outcome = handler(context)

    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "bench_report"
    assert primary.lineage == ("artifact:dataset", "artifact:spec", "case:1", "case:2")
    assert primary.version_tuple.seed is None
    assert outcome.findings == ()
    assert evaluator.agent_calls == 0  # aggregation never evaluates cases
    assert len(composer.aggregate_calls) == 1
    assert composer.aggregate_calls[0][1] is None
    consumed_ids = [item.identity.artifact_id for item in composer.aggregate_calls[0][0]]
    assert consumed_ids == ["case:2", "case:1"]


def test_bench_aggregate_fails_closed_without_case_result_identity_verifier() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    store.register("case:2", {"case": 2})
    cases = (
        _aggregate_case(case_id="c1", artifact_id="case:1"),
        _aggregate_case(case_id="c2", artifact_id="case:2"),
    )
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
    )
    context = build_context(
        params=_bench_aggregate_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )

    with pytest.raises(ValueError, match="identity verifier is unavailable"):
        handler(context)


def test_bench_aggregate_requires_not_applicable_model_execution() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    cases = (BenchCaseSpecV1(case_id="c1", partition_id="p1", mode="agent"),)
    verifier = _FakeAggregateVerifier(
        {"case:1": _aggregate_identity(artifact_id="case:1", case=cases[0])}
    )
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
        aggregate_input_verifier=verifier,
    )
    context = build_context(
        params=BenchRunPayloadV1(
            dataset_artifact_id="artifact:dataset",
            benchmark_spec_artifact_id="artifact:spec",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="aggregate_results",
            case_result_artifact_ids=("case:1",),
        ),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=FakeModelBridge(),
    )

    with pytest.raises(ValueError, match="requires not_applicable"):
        handler(context)


def test_bench_aggregate_rejects_result_identity_outside_exact_case_replication_set() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    store.register("case:2", {"case": 2})
    cases = (
        _aggregate_case(case_id="c1", artifact_id="case:1"),
        _aggregate_case(case_id="c2", artifact_id="case:2"),
    )
    verifier = _FakeAggregateVerifier(
        {
            "case:1": _aggregate_identity(artifact_id="case:1", case=cases[0]),
            # replication 1 was not requested by repetition_count=1.
            "case:2": _aggregate_identity(artifact_id="case:2", case=cases[1], replication_index=1),
        }
    )
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader(cases),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
        aggregate_input_verifier=verifier,
    )
    context = build_context(
        params=_bench_aggregate_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )

    with pytest.raises(ValueError, match="exact spec binding"):
        handler(context)


def test_bench_aggregate_rejects_boolean_replication_identity() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    case = _aggregate_case(case_id="c1", artifact_id="case:1")
    malformed = _aggregate_identity(artifact_id="case:1", case=case, replication_index=False)
    handler = BenchRunHandler(
        blobs=store,
        store=store,
        case_loader=_FakeCaseLoader((case,)),
        evaluator=_FakeEvaluator(),
        composer=_FakeComposer(),
        aggregate_input_verifier=_FakeAggregateVerifier({"case:1": malformed}),
    )
    context = build_context(
        params=BenchRunPayloadV1(
            dataset_artifact_id="artifact:dataset",
            benchmark_spec_artifact_id="artifact:spec",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="aggregate_results",
            case_result_artifact_ids=("case:1",),
        ),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )

    with pytest.raises(ValueError, match="replication index"):
        handler(context)


def test_bench_payload_enforces_execution_scope_input_rules() -> None:
    with pytest.raises(ValidationError):
        BenchRunPayloadV1(
            dataset_artifact_id="d",
            benchmark_spec_artifact_id="s",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="execute_cases",
            case_result_artifact_ids=("case:1",),
        )
    with pytest.raises(ValidationError):
        BenchRunPayloadV1(
            dataset_artifact_id="d",
            benchmark_spec_artifact_id="s",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="aggregate_results",
            case_result_artifact_ids=(),
        )
