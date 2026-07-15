"""Task 13 — ``patch_validator@1`` (deterministic preview re-verification).

Re-runs the selected checkers + economy simulations + regression suites against a
subject patch's PREVIEW snapshot and seals ONE ``evidence-set@1`` primary plus one
``regression-evidence@1`` per dimension. ``EvidenceSet.overall_status`` IS the
outcome code; the auto-apply proof is sealed ONLY for the exact deterministic
eligible policy.
"""

from __future__ import annotations

import json

import pytest

from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    ProfileRefV1,
    RunKindRef,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    FindingEvidenceBindingV1,
    PatchValidationPayloadV1,
    PreparedRunResult,
    RefReadBindingV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    AutoApplyOracleEvidenceBindingV1,
    AutoApplyOutcomeEvidenceBindingV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    DeterministicOracleRefV1,
    EvidenceSet,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
)
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.sim.economy import SimResult
from gameforge.platform.run_handlers.patch_validation import (
    AutoApplyEvaluationRequest,
    PatchValidationHandler,
)
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.validation_common import RegressionSuiteResultV1
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
    snapshot_bytes,
)

PATCH_VALIDATE_KIND = RunKindRef(kind="patch.validate", version=1)
SUBJECT_ID = "artifact:subject-patch"
BASE_ID = "artifact:base-snapshot"
PREVIEW_ID = "artifact:preview-snapshot"
REVIEW_ID = "artifact:review"
FINDING_EVIDENCE_ID = "artifact:finding-evidence"
REGRESSION_SUITE_ID = "artifact:regression-suite"
_HEX = "a" * 64
_CHECKER = ProfileRefV1(profile_id="checker", version=1)
_SIM = ProfileRefV1(profile_id="sim", version=1)
_VALIDATION = ProfileRefV1(profile_id="validation", version=1)


def _clean_snapshot() -> bytes:
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    return snapshot_bytes([npc], [])


def _dangling_snapshot() -> bytes:
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    dangling = Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="monster:ghost", dst_id="npc:1")
    return snapshot_bytes([npc], [dangling])


def _subject() -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id=SUBJECT_ID,
        subject_digest=_HEX,
        active_validation_run_id="run:1",
    )


def _payload(
    *,
    checker_profiles=(_CHECKER,),
    simulation_profiles=(),
    findings=(),
    review_artifact_ids=(),
    regression_suite_artifact_ids=(),
) -> PatchValidationPayloadV1:
    return PatchValidationPayloadV1(
        subject=_subject(),
        base_snapshot_artifact_id=BASE_ID,
        preview_snapshot_artifact_id=PREVIEW_ID,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(
            ref_name="ref:main", expected_ref=RefValue(artifact_id=BASE_ID, revision=1)
        ),
        validation_policy=_VALIDATION,
        checker_profiles=checker_profiles,
        simulation_profiles=simulation_profiles,
        findings=findings,
        review_artifact_ids=review_artifact_ids,
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=regression_suite_artifact_ids,
    )


class _FakeAutoApplyEvaluator:
    """Builds a self-consistent auto-apply proof for the eligible scenario."""

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None:
        scope = DomainScope(domain_ids=("economy",))
        registry = AutoApplyPolicyRegistryRefV1(registry_version="reg@1", registry_digest=_HEX)
        policy = AutoApplyPolicyRefV1(
            registry=registry, policy_id="auto", policy_version="1", policy_digest=_HEX
        )
        oracle = DeterministicOracleRefV1(
            oracle_id="checker", oracle_version="1", oracle_digest=_HEX
        )
        first_requirement = request.requirements[0]
        return AutoApplyProofV1(
            subject_artifact_id=request.subject_artifact_id,
            subject_digest=request.subject_digest,
            target_binding=request.target_binding,
            affected_domain_scope=scope,
            validation_evidence_artifact_id=request.validation_evidence_artifact_id,
            regression_evidence_artifact_ids=request.regression_evidence_artifact_ids,
            validation_profile_binding=AutoApplyValidationProfileBindingV1(
                validation_profile=request.validation_profile,
                validation_profile_payload_hash=request.validation_profile_payload_hash,
                policy=policy,
            ),
            deterministic_oracle_evidence=(
                AutoApplyOracleEvidenceBindingV1(
                    oracle=oracle,
                    evaluated_domain_scope=scope,
                    evidence_artifact_id=first_requirement.evidence_artifact_id,
                    evidence_payload_hash=_HEX,
                ),
            ),
            required_outcome_evidence=(
                AutoApplyOutcomeEvidenceBindingV1(
                    rule=QualifiedOutcomeRuleRefV1(
                        resolved_policy_id="patch-validation", outcome_rule_id="regression"
                    ),
                    requirement_id=first_requirement.requirement_id,
                    evidence_artifact_id=first_requirement.evidence_artifact_id,
                    evidence_payload_hash=_HEX,
                ),
            ),
            policy=policy,
        )


class _CleanChecker:
    """A deterministic checker that reports no defect (a vetted preview)."""

    id = "graph"

    def check(self, snapshot, nav=None):
        return []


class _CleanSimulator:
    """An economy simulator whose horizon shows no invariant violation."""

    def run(self, model, *, seed, n_agents, n_ticks) -> SimResult:
        return SimResult(distributions={}, invariants=[], sensitivity={})


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "status": "failed",
            },
        )


def _store(*, snapshot: bytes | None = None) -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(BASE_ID, _clean_snapshot())
    store.register(PREVIEW_ID, snapshot if snapshot is not None else _clean_snapshot())
    store.register(REVIEW_ID, {"payload_schema_version": "review@1"})
    store.register(FINDING_EVIDENCE_ID, {"payload_schema_version": "checker-report@1"})
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    return store


def _handler(
    store: FakeArtifactStore,
    *,
    checker_resolver=lambda profile, constraints: _CleanChecker(),
    simulator=None,
    **kwargs,
) -> PatchValidationHandler:
    return PatchValidationHandler(
        blobs=store,
        store=store,
        checker_resolver=checker_resolver,
        sim_config_resolver=lambda profile: ReviewSimConfig(n_agents=6, n_ticks=12),
        simulator=simulator if simulator is not None else _CleanSimulator(),
        **kwargs,
    )


def _context(store: FakeArtifactStore, payload: PatchValidationPayloadV1):
    return build_context(
        params=payload,
        kind=PATCH_VALIDATE_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/validation_policy", profile_id="validation", version=1, kind="validation"
            ),
        ),
        seed=7,
    )


def _read_evidence_set(store: FakeArtifactStore, outcome: PreparedRunResult) -> EvidenceSet:
    primary = outcome.artifacts[outcome.primary_index]
    return EvidenceSet.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def test_clean_preview_passes_and_seals_evidence_set() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload(simulation_profiles=(_SIM,))))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "patch_validation_passed"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "validation_evidence"
    assert primary.payload_schema_id == "evidence-set@1"

    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "passed"
    assert evidence.subject_artifact_id == SUBJECT_ID
    assert isinstance(evidence.target_binding, PatchTargetBindingV1)
    assert evidence.target_binding.target_artifact_id == PREVIEW_ID
    # one regression-evidence per checker + simulation dimension.
    regression = [a for a in outcome.artifacts if a.kind == "regression_evidence"]
    assert len(regression) == 2
    kinds = {req.kind for req in evidence.requirements}
    assert kinds == {"regression"}
    assert all(req.status == "passed" for req in evidence.requirements)
    # no auto-apply proof without an eligible evaluator.
    assert all(a.payload_schema_id != "auto-apply-proof@1" for a in outcome.artifacts)


def test_defect_preview_fails_validation() -> None:
    store = _store(snapshot=_dangling_snapshot())
    outcome = _handler(store, checker_resolver=lambda profile, constraints: GraphChecker())(
        _context(store, _payload())
    )

    assert outcome.summary.outcome_code == "patch_validation_failed"
    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "failed"
    assert any(req.status == "failed" for req in evidence.requirements)
    # discovered violations are emitted as validation findings.
    assert outcome.findings, "the dangling reference must be reported as a validation finding"


def test_failing_regression_suite_fails_validation() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(store, _payload(regression_suite_artifact_ids=(REGRESSION_SUITE_ID,)))
    )
    assert outcome.summary.outcome_code == "patch_validation_failed"
    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "failed"
    assert any(
        req.requirement_id == f"regression:{REGRESSION_SUITE_ID}" and req.status == "failed"
        for req in evidence.requirements
    )


def test_finding_bindings_and_supporting_are_bound_exactly() -> None:
    store = _store()
    binding = FindingEvidenceBindingV1(
        finding_id="f1",
        finding_revision=1,
        evidence_artifact_id=FINDING_EVIDENCE_ID,
        finding_digest=_HEX,
    )
    outcome = _handler(store)(
        _context(store, _payload(findings=(binding,), review_artifact_ids=(REVIEW_ID,)))
    )
    evidence = _read_evidence_set(store, outcome)
    assert evidence.finding_bindings == (binding,)
    assert REVIEW_ID in evidence.supporting_artifact_ids
    assert FINDING_EVIDENCE_ID in evidence.supporting_artifact_ids
    # the bound review/finding evidence become typed lineage parents.
    primary = outcome.artifacts[outcome.primary_index]
    assert REVIEW_ID in primary.lineage
    assert FINDING_EVIDENCE_ID in primary.lineage


def test_auto_apply_eligible_seals_proof() -> None:
    store = _store()
    outcome = _handler(store, auto_apply_evaluator=_FakeAutoApplyEvaluator())(
        _context(store, _payload())
    )
    assert outcome.summary.outcome_code == "patch_validation_auto_eligible"
    proofs = [a for a in outcome.artifacts if a.payload_schema_id == "auto-apply-proof@1"]
    assert len(proofs) == 1
    proof = AutoApplyProofV1.model_validate(json.loads(store.read_prepared(proofs[0].object_ref)))
    primary = outcome.artifacts[outcome.primary_index]
    from gameforge.platform.run_handlers.validation_common import content_addressed_artifact_id

    assert proof.validation_evidence_artifact_id == content_addressed_artifact_id(primary)


def test_passed_scenario_has_no_proof_without_eligibility() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload()))
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert all(a.payload_schema_id != "auto-apply-proof@1" for a in outcome.artifacts)


def test_patch_validation_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(store_a, _payload(simulation_profiles=(_SIM,))))
    out_b = _handler(store_b)(_context(store_b, _payload(simulation_profiles=(_SIM,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_wrong_payload_type_is_rejected() -> None:
    from gameforge.contracts.jobs import SimulationRunPayloadV1

    store = _store()
    sim = SimulationRunPayloadV1(
        snapshot_artifact_id=PREVIEW_ID,
        simulation_profile=ProfileRefV1(profile_id="sim", version=1),
        workload_profile=ProfileRefV1(profile_id="wl", version=1),
        replication_count=1,
        horizon_steps=1,
    )
    context = build_context(params=sim, kind=RunKindRef(kind="simulation.run", version=1), seed=1)
    with pytest.raises(TypeError):
        _handler(store)(context)
