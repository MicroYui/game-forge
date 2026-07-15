"""Task 13 — ``constraint_validator@1`` (≥2-engine differential compile validation).

Compiles a subject ``constraint_proposal`` through
``parse → typecheck → compile → differential(×≥2) → golden``, driving the TWO REAL
spine differential engines (z3 + Clingo), records every stage in a
``constraint-compile-evidence@1``, conditionally publishes ONE candidate
``constraint-snapshot@1``, and NEVER treats a missing execution as a pass.
"""

from __future__ import annotations

import json

from gameforge.apps.worker.validation import build_differential_engines
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PreparedRunResult,
    RefReadBindingV1,
    SolverEngineRefV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ConstraintCompileEvidenceV1,
    ConstraintProposalV1,
    ConstraintTargetBindingV1,
    EvidenceSet,
)
from gameforge.platform.run_handlers.constraint_validation import ConstraintValidationHandler
from gameforge.platform.run_handlers.readers import load_constraints
from gameforge.platform.run_handlers.validation_common import RegressionSuiteResultV1
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
)

CONSTRAINT_VALIDATE_KIND = RunKindRef(kind="constraint_proposal.validate", version=1)
SUBJECT_ID = "artifact:constraint-proposal"
BASE_ID = "artifact:base-constraints"
REGRESSION_SUITE_ID = "artifact:regression-suite"
_HEX = "a" * 64
_ENGINES = (
    SolverEngineRefV1(engine_id="clingo", version=1),
    SolverEngineRefV1(engine_id="z3", version=1),
)


def _proposal(constraints: tuple[Constraint, ...]) -> ConstraintProposalV1:
    return ConstraintProposalV1(
        revision=1,
        dsl_grammar_version="dsl@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
        constraints=constraints,
        source_bindings=(),
        produced_by="human",
        rationale="human-authored constraint subject under validation",
    )


def _constraint(constraint_id: str, assert_expr: str) -> Constraint:
    return Constraint(
        id=constraint_id,
        dsl_grammar_version="dsl@1",
        kind="numeric",
        oracle="deterministic",
        **{"assert": assert_expr},
        severity="major",
    )


def _structural(constraint_id: str, assert_expr: str) -> Constraint:
    return Constraint(
        id=constraint_id,
        dsl_grammar_version="dsl@1",
        kind="structural",
        oracle="deterministic",
        **{"assert": assert_expr},
        severity="major",
    )


# A MIXED candidate exercises BOTH engine domains (z3 numeric + Clingo structural),
# the only shape for which the two domain-partitioned engines can BOTH positively
# decide and thus reach `constraint_validated` (see the report's co-apply finding).
_MIXED = (
    _constraint("C_cap", "reward_gold <= 80"),
    _structural("C_acyclic", "acyclic(quest_steps)"),
)


def _subject() -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id=SUBJECT_ID,
        subject_digest=_HEX,
        active_validation_run_id="run:1",
    )


def _payload(*, base=None, regression=(), golden=None) -> ConstraintValidationPayloadV1:
    return ConstraintValidationPayloadV1(
        subject=_subject(),
        base_constraint_snapshot_artifact_id=base,
        target=RefReadBindingV1(
            ref_name="ref:constraints", expected_ref=RefValue(artifact_id=BASE_ID, revision=1)
        ),
        dsl_grammar_version="dsl@1",
        compiler_profile=ProfileRefV1(profile_id="compiler", version=1),
        differential_engines=_ENGINES,
        golden_suite_artifact_id=golden,
        regression_suite_artifact_ids=regression,
        validation_policy=ProfileRefV1(profile_id="validation", version=1),
    )


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            payload={"payload_schema_version": "regression-evidence@1", "status": "failed"},
        )


def _store(constraints: tuple[Constraint, ...]) -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(SUBJECT_ID, _proposal(constraints).model_dump(mode="json"))
    store.register(BASE_ID, {"dsl_grammar_version": "dsl@1", "constraints": []})
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    return store


def _handler(store: FakeArtifactStore, **kwargs) -> ConstraintValidationHandler:
    return ConstraintValidationHandler(
        blobs=store,
        store=store,
        differential_engines=build_differential_engines(),
        **kwargs,
    )


def _context(store: FakeArtifactStore, payload: ConstraintValidationPayloadV1):
    return build_context(
        params=payload,
        kind=CONSTRAINT_VALIDATE_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/validation_policy", profile_id="validation", version=1, kind="validation"
            ),
            resolved_binding(
                "/params/compiler_profile",
                profile_id="compiler",
                version=1,
                kind="constraint_compiler",
            ),
        ),
        seed=3,
    )


def _compile_evidence(
    store: FakeArtifactStore, outcome: PreparedRunResult
) -> ConstraintCompileEvidenceV1:
    for artifact in outcome.artifacts:
        if artifact.payload_schema_id == "constraint-compile-evidence@1":
            return ConstraintCompileEvidenceV1.model_validate(
                json.loads(store.read_prepared(artifact.object_ref))
            )
    raise AssertionError("no compile evidence sealed")


def _evidence_set(store: FakeArtifactStore, outcome: PreparedRunResult) -> EvidenceSet:
    primary = outcome.artifacts[outcome.primary_index]
    return EvidenceSet.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def test_mixed_candidate_validated_when_both_engines_positively_decide() -> None:
    # ONLY a MIXED candidate exercises BOTH engine domains, so both engines can
    # positively decide consistency and the differential can genuinely pass.
    store = _store(_MIXED)
    outcome = _handler(store)(_context(store, _payload()))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "constraint_validated"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.payload_schema_id == "evidence-set@1"

    # exactly one candidate constraint_snapshot, re-loadable via load_constraints.
    candidates = [a for a in outcome.artifacts if a.kind == "constraint_snapshot"]
    assert len(candidates) == 1
    store._by_artifact_id["artifact:candidate"] = store.read_prepared(candidates[0].object_ref)
    loaded = load_constraints(store, "artifact:candidate")
    assert [c.id for c in loaded] == ["C_acyclic", "C_cap"]

    evidence = _compile_evidence(store, outcome)
    assert evidence.overall_status == "passed"
    assert evidence.candidate_constraint_snapshot_artifact_id is not None
    stages = {stage.stage for stage in evidence.stages}
    assert stages == {"parse", "typecheck", "compile", "differential", "golden"}
    # BOTH real engines GENUINELY evaluated their domain and passed — no vacuous pass.
    differential = [s for s in evidence.stages if s.stage == "differential"]
    assert {s.engine_id for s in differential} == {"z3", "clingo"}
    assert all(s.status == "passed" for s in differential)
    # golden absent -> the only permitted not_applicable stage.
    golden = next(s for s in evidence.stages if s.stage == "golden")
    assert golden.status == "not_applicable"

    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "passed"
    assert isinstance(ev_set.target_binding, ConstraintTargetBindingV1)


def test_purely_numeric_candidate_validated_via_z3() -> None:
    # z3's numeric domain applies and it soundly decides `sat`; Clingo's STRUCTURAL
    # domain is honestly not_applicable (it executed and found nothing in its domain)
    # -> that stage does NOT block validation. z3's positive decision covers the only
    # constraint -> `constraint_validated`.
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validated"
    evidence = _compile_evidence(store, outcome)
    assert evidence.overall_status == "passed"
    by_engine = {s.engine_id: s for s in evidence.stages if s.stage == "differential"}
    assert by_engine["z3"].status == "passed"
    assert by_engine["clingo"].status == "not_applicable"
    assert by_engine["clingo"].reason_code == "engine_domain_not_applicable"
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "passed"


def test_purely_structural_candidate_validated_via_clingo() -> None:
    # symmetric: Clingo's structural domain applies and grounds cleanly; z3's numeric
    # domain is honestly not_applicable -> `constraint_validated`.
    store = _store((_structural("C_acyclic", "acyclic(quest_steps)"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validated"
    by_engine = {
        s.engine_id: s
        for s in _compile_evidence(store, outcome).stages
        if s.stage == "differential"
    }
    assert by_engine["clingo"].status == "passed"
    assert by_engine["z3"].status == "not_applicable"


def test_empty_candidate_is_unproven_never_validated() -> None:
    # SOUNDNESS GUARD: no constraint -> no engine positively decides anything ->
    # every differential stage is not_applicable -> must NOT vacuously validate.
    store = _store(())
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code != "constraint_validated"
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "unproven"
    differential = [
        s for s in _compile_evidence(store, outcome).stages if s.stage == "differential"
    ]
    assert all(s.status == "not_applicable" for s in differential)


def test_uncompilable_proposal_fails_without_candidate() -> None:
    store = _store((_constraint("C_bad", "__import__('os').system('x')"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validation_failed_without_candidate"
    assert all(a.kind != "constraint_snapshot" for a in outcome.artifacts)
    evidence = _compile_evidence(store, outcome)
    assert evidence.candidate_constraint_snapshot_artifact_id is None
    parse = next(s for s in evidence.stages if s.stage == "parse")
    assert parse.status == "failed"
    # differential never treated as passed on missing execution.
    assert all(s.status != "passed" for s in evidence.stages if s.stage == "differential")
    ev_set = _evidence_set(store, outcome)
    assert ev_set.target_binding is None


def test_numeric_contradiction_fails_with_candidate() -> None:
    # a numeric contradiction compiles; z3 GENUINELY derives unsat (inconsistent) ->
    # its differential stage FAILS. Clingo's structural domain does not apply -> its
    # stage is unproven (NOT a vacuous consistent). candidate still published.
    store = _store((_constraint("C_contra", "reward_gold <= 80 and reward_gold >= 100"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert len([a for a in outcome.artifacts if a.kind == "constraint_snapshot"]) == 1
    evidence = _compile_evidence(store, outcome)
    assert evidence.candidate_constraint_snapshot_artifact_id is not None
    assert evidence.overall_status == "failed"
    by_engine = {s.engine_id: s for s in evidence.stages if s.stage == "differential"}
    assert by_engine["z3"].status == "failed"
    assert by_engine["z3"].reason_code == "candidate_inconsistent"
    assert by_engine["clingo"].status == "not_applicable"


def test_engine_unbindable_candidate_is_never_validated() -> None:
    # a prob_sum aggregate compiles (SMTChecker constructs), but z3's free-var probe
    # cannot bind the list attr -> z3 is UNDECIDED (unproven, NOT skipped-as-passed);
    # Clingo's structural domain does not apply -> unproven. NO engine positively
    # decided consistency -> the candidate is NEVER `constraint_validated`.
    store = _store((_constraint("C_prob", "prob_sum(entries) == 1"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code != "constraint_validated"
    evidence = _compile_evidence(store, outcome)
    by_engine = {s.engine_id: s for s in evidence.stages if s.stage == "differential"}
    # z3's numeric domain APPLIES but it could not bind -> undecided (unproven);
    # Clingo's structural domain does not apply -> not_applicable. NO engine passed.
    assert by_engine["z3"].status == "unproven"
    assert by_engine["z3"].reason_code == "z3_cannot_bind_predicate"
    assert by_engine["clingo"].status == "not_applicable"
    assert all(s.status != "passed" for s in evidence.stages if s.stage == "differential")


def test_failing_regression_fails_with_candidate() -> None:
    # a MIXED candidate makes the differential genuinely pass so regression runs;
    # a failing regression then drives the failed-with-candidate outcome.
    store = _store(_MIXED)
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    # compile passed -> candidate published; the regression dimension failed.
    assert len([a for a in outcome.artifacts if a.kind == "constraint_snapshot"]) == 1
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "failed"
    produced = [d for d in outcome.requirement_dispositions if d.status == "produced"]
    assert produced and all(d.outcome_rule_id == "regression" for d in produced)


def test_without_candidate_short_circuits_regression() -> None:
    store = _store((_constraint("C_bad", "__import__('x')"),))
    outcome = _handler(store)(_context(store, _payload(regression=(REGRESSION_SUITE_ID,))))
    assert outcome.summary.outcome_code == "constraint_validation_failed_without_candidate"
    # regression produces NOTHING; every suite is a not_executed/candidate_unavailable row.
    assert all(a.kind != "regression_evidence" for a in outcome.artifacts)
    dispositions = outcome.requirement_dispositions
    assert dispositions and all(
        d.status == "not_executed" and d.reason_code == "candidate_unavailable"
        for d in dispositions
    )


def test_constraint_validation_is_byte_deterministic() -> None:
    store_a, store_b = _store(_MIXED), _store(_MIXED)
    out_a = _handler(store_a)(_context(store_a, _payload(base=BASE_ID)))
    out_b = _handler(store_b)(_context(store_b, _payload(base=BASE_ID)))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
