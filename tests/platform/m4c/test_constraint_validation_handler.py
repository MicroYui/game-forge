"""Task 13 — ``constraint_validator@1`` (≥2-engine differential compile validation).

Compiles a subject ``constraint_proposal`` through
``parse → typecheck → compile → differential(×≥2) → golden``, driving two
independent engine pairs (z3/reference + Clingo/Graph), records every stage in a
``constraint-compile-evidence@1``, conditionally publishes ONE candidate
``constraint-snapshot@1``, and NEVER treats a missing execution as a pass.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gameforge.apps.worker import validation as worker_validation
from gameforge.apps.worker.validation import (
    ClingoDifferentialEngine,
    GraphReferenceDifferentialEngine,
    build_differential_engines,
)
from gameforge.contracts.dsl import Constraint, Predicate, Selector
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    ProfileRefV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PreparedRunResult,
    RefReadBindingV1,
    SolverEngineRefV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import VersionTuple, artifact_id_v2_for
from gameforge.contracts.regression import (
    RegressionCaseSeedManifestV1,
    RegressionCaseSeedV1,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ConstraintCompileEvidenceV1,
    ConstraintProposalV1,
    ConstraintTargetBindingV1,
    EvidenceSet,
)
from gameforge.platform.run_handlers.constraint_validation import (
    BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
    ConstraintValidationProfileAuthorityV1,
    ConstraintValidationHandler,
    DifferentialEngineResultV1,
    DifferentialEvalRequest,
    GoldenSuiteResultV1,
)
from gameforge.platform.run_handlers import constraint_validation as constraint_validation_handler
from gameforge.platform.run_handlers.readers import load_constraints
from gameforge.platform.run_handlers.validation_common import (
    RegressionSuiteResultV1,
    content_addressed_artifact_id,
    derive_validation_subseed,
)
from gameforge.platform.publication.payload_binding import (
    FinalSiblingFact,
    bind_final_payload_references,
    final_sibling_fact_for,
)
from gameforge.platform.publication.payload_schema import (
    decode_and_validate_artifact_payload,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
)

CONSTRAINT_VALIDATE_KIND = RunKindRef(kind="constraint_proposal.validate", version=1)
SUBJECT_ID = "artifact:constraint-proposal"
BASE_ID = "artifact:base-constraints"
REGRESSION_SUITE_ID = "artifact:regression-suite"
SECOND_REGRESSION_SUITE_ID = "artifact:regression-suite:2"
GOLDEN_SUITE_ID = "artifact:golden-suite"
SOURCE_SNAPSHOT_ID = "ir:source"
_HEX = "a" * 64
_ENGINES = BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1
_PARTITIONED_ENGINES = (
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
        scope=Selector(var="q", node_type="QUEST"),
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


# A mixed candidate exercises both independent pairs: z3 + numeric-reference and
# Clingo + graph-reference.
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


def _payload(
    *,
    base=None,
    regression=(),
    golden=None,
    engines=_ENGINES,
) -> ConstraintValidationPayloadV1:
    return ConstraintValidationPayloadV1(
        subject=_subject(),
        base_constraint_snapshot_artifact_id=base,
        target=RefReadBindingV1(
            ref_name="ref:constraints", expected_ref=RefValue(artifact_id=BASE_ID, revision=1)
        ),
        dsl_grammar_version="dsl@1",
        compiler_profile=ProfileRefV1(profile_id="compiler", version=1),
        differential_engines=engines,
        golden_suite_artifact_id=golden,
        regression_suite_artifact_ids=regression,
        validation_policy=ProfileRefV1(profile_id="validation", version=1),
    )


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        assert request.constraint_candidate is not None
        finding = _regression_finding(SOURCE_SNAPSHOT_ID)
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": SOURCE_SNAPSHOT_ID,
                "seed": request.seed,
                "status": "failed",
                "reason_code": None,
                "findings": [finding.model_dump(mode="json")],
                "case_seed_manifest": _case_seed_manifest(request),
            },
            action_work_units=1,
            **_constraint_execution_binding(request, SOURCE_SNAPSHOT_ID),
        )


class _PassingRegressionRunner:
    def __init__(self, source_snapshot_id: str = SOURCE_SNAPSHOT_ID) -> None:
        self.source_snapshot_id = source_snapshot_id

    def run(self, request) -> RegressionSuiteResultV1:
        assert request.constraint_candidate is not None
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="passed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": self.source_snapshot_id,
                "seed": request.seed,
                "status": "passed",
                "reason_code": None,
            },
            action_work_units=1,
            **_constraint_execution_binding(request, self.source_snapshot_id),
        )


class _UnprovenRegressionRunner:
    reason = "adapter_environment_unavailable"

    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="unproven",
            reason_code=self.reason,
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": SOURCE_SNAPSHOT_ID,
                "seed": request.seed,
                "status": "unproven",
                "reason_code": self.reason,
            },
        )


class _PassingGoldenRunner:
    def run(self, *, golden_suite_artifact_id, constraints) -> GoldenSuiteResultV1:
        return GoldenSuiteResultV1(status="passed")


def _constraint_execution_binding(request, source_snapshot_id: str) -> dict[str, str]:
    candidate = request.constraint_candidate
    assert candidate is not None
    return {
        "constraint_candidate_snapshot_id": candidate.candidate_snapshot_id,
        "constraint_candidate_digest": candidate.candidate_digest,
        "constraint_source_snapshot_id": source_snapshot_id,
    }


def _regression_finding(source_snapshot_id: str) -> Finding:
    return Finding(
        id="regression:fresh",
        source="playtest",
        producer_id="agent-env-action-replay@1",
        producer_run_id="regression-runner",
        oracle_type="deterministic",
        defect_class="regression_expectation_mismatch",
        severity="major",
        snapshot_id=source_snapshot_id,
        evidence={"case": "case:fresh"},
        minimal_repro={"case_id": "case:fresh"},
        status="confirmed",
        message="fresh constraint regression mismatch",
    )


def _case_seed_manifest(request) -> dict[str, object]:
    assert request.root_seed is not None
    assert request.run_kind is not None
    assert request.profile is not None
    case_id = "case:fresh"
    derivation_case_id = f"{request.suite_artifact_id}:{case_id}"
    manifest = RegressionCaseSeedManifestV1(
        suite_artifact_id=request.suite_artifact_id,
        root_seed=request.root_seed,
        run_kind=request.run_kind,
        profile=request.profile,
        cases=(
            RegressionCaseSeedV1(
                case_id=case_id,
                derivation_case_id=derivation_case_id,
                seed=derive_validation_subseed(
                    root_seed=request.root_seed,
                    run_kind=request.run_kind,
                    profile=request.profile,
                    case_id=derivation_case_id,
                    replication_index=0,
                ),
            ),
        ),
    )
    return manifest.model_dump(mode="json")


class _MislabeledEngine:
    engine_id = "z3"
    engine_version = 1

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, request) -> DifferentialEngineResultV1:
        self.calls += 1
        return DifferentialEngineResultV1(
            status="evaluated",
            consistency="consistent",
            decided_constraint_ids=tuple(item.id for item in request.constraints),
        )


class _NoCoverageEngine:
    engine_id = "z3"
    engine_version = 1

    def evaluate(self, request) -> DifferentialEngineResultV1:
        return DifferentialEngineResultV1(
            status="evaluated",
            consistency="consistent",
            decided_constraint_ids=(),
        )


class _InconsistentNumericReference:
    engine_id = "numeric-reference"
    engine_version = 1

    def evaluate(self, request) -> DifferentialEngineResultV1:
        return DifferentialEngineResultV1(
            status="evaluated",
            consistency="inconsistent",
        )


def _store(constraints: tuple[Constraint, ...]) -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(SUBJECT_ID, _proposal(constraints).model_dump(mode="json"))
    store.register(BASE_ID, {"dsl_grammar_version": "dsl@1", "constraints": []})
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    store.register(SECOND_REGRESSION_SUITE_ID, {"suite": "s2"})
    store.register(GOLDEN_SUITE_ID, {"suite": "golden"})
    return store


class _TestProfileResolver:
    def resolve(
        self,
        *,
        run_kind,
        validation_binding,
        compiler_binding,
    ) -> ConstraintValidationProfileAuthorityV1:
        assert run_kind == CONSTRAINT_VALIDATE_KIND
        return ConstraintValidationProfileAuthorityV1(
            validation_binding=validation_binding,
            compiler_binding=compiler_binding,
            validation_handler_key="builtin_validation_profile@1",
            compiler_handler_key="builtin_constraint_compiler_profile@1",
        )


def _handler(store: FakeArtifactStore, **kwargs) -> ConstraintValidationHandler:
    engines = kwargs.pop("differential_engines", build_differential_engines())
    profile_resolver = kwargs.pop("profile_resolver", _TestProfileResolver())
    return ConstraintValidationHandler(
        blobs=store,
        store=store,
        differential_engines=engines,
        profile_resolver=profile_resolver,
        **kwargs,
    )


def _context(
    store: FakeArtifactStore,
    payload: ConstraintValidationPayloadV1,
    *,
    seed: int | None = None,
    version_tuple: VersionTuple | None = None,
):
    if version_tuple is None:
        version_tuple = VersionTuple(
            ir_snapshot_id=SOURCE_SNAPSHOT_ID,
            tool_version="constraint-proposal@1",
        )
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
        seed=seed,
        version_tuple=version_tuple,
    )


@pytest.mark.parametrize(
    "values",
    (
        {
            "status": "evaluated",
            "consistency": None,
        },
        {
            "status": "evaluated",
            "consistency": "consistent",
            "reason_code": "contradictory_reason",
        },
        {
            "status": "not_applicable",
            "consistency": "inconsistent",
            "reason_code": "engine_domain_not_applicable",
        },
        {
            "status": "not_applicable",
            "reason_code": "forged_reason",
        },
        {
            "status": "undecided",
            "reason_code": None,
        },
    ),
)
def test_differential_result_rejects_contradictory_shape(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DifferentialEngineResultV1(**values)  # type: ignore[arg-type]


def test_compile_integrity_fault_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_compile(_constraints):
        raise IntegrityViolation("compiler authority corrupt")

    monkeypatch.setattr(constraint_validation_handler, "compile_all", fail_compile)
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))

    with pytest.raises(IntegrityViolation, match="compiler authority corrupt"):
        _handler(store)(_context(store, _payload()))


@pytest.mark.parametrize("fault", ("worker_compile", "parse", "checker"))
def test_differential_internal_fault_propagates(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    if fault == "worker_compile":

        def fail_compile(_constraints):
            raise IntegrityViolation("differential compiler corrupt")

        monkeypatch.setattr(worker_validation, "compile_all", fail_compile)
    elif fault == "parse":

        def fail_parse(_expression):
            raise RuntimeError("differential parser crashed")

        monkeypatch.setattr(worker_validation, "parse_assert", fail_parse)
    else:

        class ExplodingChecker:
            id = "compiled:smt:C_cap"
            execution_binding = worker_validation.CheckerExecutionBinding(
                wrapper_id=id,
                native_id="smt",
                constraint_id="C_cap",
            )

            def check(self, _snapshot):
                raise IntegrityViolation("compiled checker corrupt")

        monkeypatch.setattr(
            worker_validation,
            "compile_all",
            lambda _constraints: [ExplodingChecker()],
        )

    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    with pytest.raises((IntegrityViolation, RuntimeError)):
        _handler(store)(_context(store, _payload()))


def test_invalid_structural_selector_fails_typecheck_without_candidate() -> None:
    constraint = _structural("C_bad_selector", "acyclic(quest_steps)").model_copy(
        update={"scope": Selector(var="q", node_type="NOT_A_NODE")}
    )
    store = _store((constraint,))

    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validation_failed_without_candidate"
    evidence = _compile_evidence(store, outcome)
    typecheck = next(stage for stage in evidence.stages if stage.stage == "typecheck")
    assert typecheck.status == "failed"
    assert typecheck.reason_code == "selector_node_type_invalid"


def _production_profile_bindings(registry, *, catalog_version: int | None = None):
    catalogs = registry.list_execution_profile_catalogs()
    catalog = next(
        item
        for item in catalogs
        if item.catalog_version
        == (
            catalog_version
            if catalog_version is not None
            else max(c.catalog_version for c in catalogs)
        )
    )
    validation = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/validation_policy",
        profile=ProfileRefV1(profile_id="builtin.validation", version=1),
        expected_profile_kind="validation",
    )
    compiler = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/compiler_profile",
        profile=ProfileRefV1(profile_id="builtin.constraint_compiler", version=1),
        expected_profile_kind="constraint_compiler",
    )
    return validation, compiler


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


def test_mixed_candidate_validated_when_both_independent_pairs_decide() -> None:
    store = _store(_MIXED)
    outcome = _handler(store)(_context(store, _payload()))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "constraint_validated"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.payload_schema_id == "evidence-set@1"
    # None means seed is not applicable for this all-deterministic profile closure;
    # the handler must not fabricate the old seed=0 on primary/compile/candidate.
    assert all(artifact.version_tuple.seed is None for artifact in outcome.artifacts)

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
    # Both exact pairs genuinely evaluated their domains and passed.
    differential = [s for s in evidence.stages if s.stage == "differential"]
    assert {s.engine_id for s in differential} == {
        "z3",
        "numeric-reference",
        "clingo",
        "graph-reference",
    }
    assert all(s.status == "passed" for s in differential)
    # golden absent -> the only permitted not_applicable stage.
    golden = next(s for s in evidence.stages if s.stage == "golden")
    assert golden.status == "not_applicable"

    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "passed"
    assert isinstance(ev_set.target_binding, ConstraintTargetBindingV1)
    compile_requirement = next(
        item for item in ev_set.requirements if item.requirement_id == "compile"
    )
    assert compile_requirement.kind == "constraint_compile"


def test_production_profile_resolver_authorizes_only_one_exact_catalog() -> None:
    registry = build_builtin_registry()
    resolver = worker_validation.RegistryConstraintValidationProfileResolver(registry)
    validation, compiler = _production_profile_bindings(registry)

    authority = resolver.resolve(
        run_kind=CONSTRAINT_VALIDATE_KIND,
        validation_binding=validation,
        compiler_binding=compiler,
    )

    assert authority.validation_binding == validation
    assert authority.compiler_binding == compiler

    old_validation, _old_compiler = _production_profile_bindings(registry, catalog_version=1)
    with pytest.raises(IntegrityViolation, match="different exact catalogs"):
        resolver.resolve(
            run_kind=CONSTRAINT_VALIDATE_KIND,
            validation_binding=old_validation,
            compiler_binding=compiler,
        )

    with pytest.raises(IntegrityViolation, match="payload hash"):
        resolver.resolve(
            run_kind=CONSTRAINT_VALIDATE_KIND,
            validation_binding=validation,
            compiler_binding=compiler.model_copy(update={"profile_payload_hash": "b" * 64}),
        )


def test_production_profile_resolver_accepts_configured_patch_auto_apply_policy() -> None:
    """A shared validation profile may configure patch-only auto-apply authority.

    Constraint validation consumes the same exact profile for its subject-kind and
    execution closure, but must neither require nor reject the optional patch-only
    policy reference.
    """

    registry = build_builtin_registry()
    validation, compiler = _production_profile_bindings(registry)
    policy_ref = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-apply@1",
            registry_digest="a" * 64,
        ),
        policy_id="builtin.patch-low-risk",
        policy_version="1",
        policy_digest="b" * 64,
    )

    class _ConfiguredRegistry:
        def resolve_execution_profile_binding(self, binding):
            definition, lifecycle = registry.resolve_execution_profile_binding(binding)
            if definition.profile_kind == "validation":
                definition = definition.model_copy(
                    update={
                        "details": definition.details.model_copy(
                            update={"auto_apply_policy": policy_ref}
                        )
                    }
                )
            return definition, lifecycle

    authority = worker_validation.RegistryConstraintValidationProfileResolver(
        _ConfiguredRegistry()
    ).resolve(
        run_kind=CONSTRAINT_VALIDATE_KIND,
        validation_binding=validation,
        compiler_binding=compiler,
    )

    assert authority.validation_binding == validation
    assert authority.compiler_binding == compiler


@pytest.mark.parametrize(
    ("target_kind", "definition_update", "disable"),
    (
        ("constraint_compiler", {"handler_key": "forged@1"}, False),
        ("constraint_compiler", {"config_schema_id": "forged-config@1"}, False),
        ("constraint_compiler", {"config": {"forged": True}}, False),
        ("constraint_compiler", {"input_schema_ids": ("patch-validation@1",)}, False),
        ("constraint_compiler", {"output_schema_ids": ("evidence-set@1",)}, False),
        (
            "constraint_compiler",
            {
                "compatible_run_kinds": (
                    CONSTRAINT_VALIDATE_KIND,
                    RunKindRef(kind="patch.validate", version=1),
                )
            },
            False,
        ),
        ("validation", {"handler_key": "forged@1"}, False),
        (
            "validation",
            {"compatible_run_kinds": (CONSTRAINT_VALIDATE_KIND,)},
            False,
        ),
        ("validation", {"__subject_kinds__": ("constraint_proposal",)}, False),
        ("validation", {}, True),
    ),
)
def test_production_profile_resolver_rejects_forged_handler_config_and_lifecycle(
    target_kind: str,
    definition_update: dict[str, object],
    disable: bool,
) -> None:
    registry = build_builtin_registry()
    validation, compiler = _production_profile_bindings(registry)

    class _ForgedRegistry:
        def resolve_execution_profile_binding(self, binding):
            definition, lifecycle = registry.resolve_execution_profile_binding(binding)
            if definition.profile_kind == target_kind:
                updates = dict(definition_update)
                subject_kinds = updates.pop("__subject_kinds__", None)
                if subject_kinds is not None:
                    updates["details"] = definition.details.model_copy(
                        update={"subject_kinds": subject_kinds}
                    )
                definition = definition.model_copy(update=updates)
                if disable:
                    lifecycle = lifecycle.model_copy(update={"state": "disabled"})
            return definition, lifecycle

    resolver = worker_validation.RegistryConstraintValidationProfileResolver(_ForgedRegistry())
    with pytest.raises(IntegrityViolation, match="does not authorize"):
        resolver.resolve(
            run_kind=CONSTRAINT_VALIDATE_KIND,
            validation_binding=validation,
            compiler_binding=compiler,
        )


@pytest.mark.parametrize("mutation", ("extra_path", "wrong_profile", "wrong_catalog"))
def test_handler_rejects_forged_exact_profile_binding_set(mutation: str) -> None:
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    context = _context(store, _payload())
    bindings = list(context.payload.resolved_profiles)
    if mutation == "extra_path":
        bindings.append(bindings[0].model_copy(update={"field_path": "/params/extra"}))
    elif mutation == "wrong_profile":
        bindings[0] = bindings[0].model_copy(
            update={"profile": ProfileRefV1(profile_id="forged", version=1)}
        )
    else:
        bindings[1] = bindings[1].model_copy(update={"catalog_version": 2})
    envelope = context.payload.model_copy(update={"resolved_profiles": tuple(bindings)})
    forged_context = replace(
        context,
        payload=envelope,
        run=context.run.model_copy(update={"payload": envelope}),
    )

    with pytest.raises(IntegrityViolation, match="exact profile|Run authority"):
        _handler(store)(forged_context)


def test_evidence_set_reseals_candidate_and_compile_sibling_references() -> None:
    store = _store(_MIXED)
    context = _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    outcome = _handler(store, regression_runner=_PassingRegressionRunner())(context)
    primary = outcome.artifacts[outcome.primary_index]
    candidate = next(
        artifact for artifact in outcome.artifacts if artifact.kind == "constraint_snapshot"
    )
    compile_evidence = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.payload_schema_id == "constraint-compile-evidence@1"
    )
    regression_evidence = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.payload_schema_id == "regression-evidence@1"
    )
    candidate_id = content_addressed_artifact_id(candidate)
    prepared_compile_id = content_addressed_artifact_id(compile_evidence)
    final_compile_id = artifact_id_v2_for(
        kind=compile_evidence.kind,
        version_tuple=compile_evidence.version_tuple,
        lineage=(*compile_evidence.lineage, candidate_id),
        payload_hash=compile_evidence.payload_hash,
        meta={**compile_evidence.meta, "replayability": "deterministic_recompute"},
    )
    assert final_compile_id != prepared_compile_id
    regression_id = content_addressed_artifact_id(regression_evidence)

    registry = build_builtin_registry()
    definition = registry.get_run_kind(CONSTRAINT_VALIDATE_KIND)
    assert definition is not None
    policy = next(
        item
        for item in definition.outcome_policies
        if item.policy_id == "constraint-validated-with-candidate"
    )
    rule = next(item for item in policy.artifact_rules if item.rule_id == "primary")
    compile_rule = next(
        item for item in policy.artifact_rules if item.rule_id == "compile-evidence"
    )
    regression_rule = next(item for item in policy.artifact_rules if item.rule_id == "regression")
    payload = json.loads(store.read_prepared(primary.object_ref))
    binding_kwargs = dict(
        run=context.run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="evidence-set@1",
        projected_tuple=primary.version_tuple,
        final_artifact_ids_by_rule={
            "candidate": (candidate_id,),
            "compile-evidence": (final_compile_id,),
            "regression": (regression_id,),
        },
        final_sibling_facts_by_id={
            candidate_id: FinalSiblingFact(
                artifact_id=candidate_id,
                outcome_rule_id="candidate",
                artifact_kind=candidate.kind,
                payload_schema_id=candidate.payload_schema_id,
                payload_hash=candidate.payload_hash,
                requirement_id=None,
                requirement_kind=None,
            ),
            final_compile_id: final_sibling_fact_for(
                run=context.run,
                artifact_id=final_compile_id,
                outcome_rule=compile_rule,
                payload_schema_id=compile_evidence.payload_schema_id,
                canonical_payload=json.loads(store.read_prepared(compile_evidence.object_ref)),
                payload_hash=compile_evidence.payload_hash,
                authoritative_meta=compile_evidence.meta,
            ),
            regression_id: final_sibling_fact_for(
                run=context.run,
                artifact_id=regression_id,
                outcome_rule=regression_rule,
                payload_schema_id=regression_evidence.payload_schema_id,
                canonical_payload=json.loads(store.read_prepared(regression_evidence.object_ref)),
                payload_hash=regression_evidence.payload_hash,
                authoritative_meta=regression_evidence.meta,
            ),
        },
        prepared_to_final_artifact_ids_by_rule={
            "candidate": {candidate_id: candidate_id},
            "compile-evidence": {prepared_compile_id: final_compile_id},
            "regression": {regression_id: regression_id},
        },
    )
    bound = bind_final_payload_references(canonical_payload=payload, **binding_kwargs)
    assert bound["target_binding"]["target_artifact_id"] == candidate_id
    assert {
        requirement["evidence_artifact_id"]
        for requirement in bound["requirements"]
        if requirement["evidence_artifact_id"] is not None
    } == {final_compile_id, regression_id}
    assert final_compile_id in bound["supporting_artifact_ids"]
    assert prepared_compile_id not in bound["supporting_artifact_ids"]

    forged = {**payload, "target_binding": {**payload["target_binding"], "target_digest": _HEX}}
    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        bind_final_payload_references(canonical_payload=forged, **binding_kwargs)

    swapped = [dict(requirement) for requirement in payload["requirements"]]
    compile_row = next(item for item in swapped if item["requirement_id"] == "compile")
    regression_row = next(
        item for item in swapped if item["requirement_id"] == f"regression:{REGRESSION_SUITE_ID}"
    )
    compile_row["evidence_artifact_id"], regression_row["evidence_artifact_id"] = (
        regression_row["evidence_artifact_id"],
        compile_row["evidence_artifact_id"],
    )
    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        bind_final_payload_references(
            canonical_payload={**payload, "requirements": swapped}, **binding_kwargs
        )


def test_purely_numeric_candidate_validated_via_z3() -> None:
    # z3 and the independent numeric reference agree; structural engines are N/A.
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validated"
    evidence = _compile_evidence(store, outcome)
    assert evidence.overall_status == "passed"
    by_engine = {s.engine_id: s for s in evidence.stages if s.stage == "differential"}
    assert by_engine["z3"].status == "passed"
    assert by_engine["numeric-reference"].status == "passed"
    assert by_engine["clingo"].status == "not_applicable"
    assert by_engine["graph-reference"].status == "not_applicable"
    assert by_engine["clingo"].reason_code == "engine_domain_not_applicable"
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "passed"


def test_numeric_differential_rejects_worker_compiler_wrongly_routed_to_graph(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        worker_validation,
        "compile_all",
        lambda constraints: [worker_validation.GraphChecker() for _constraint in constraints],
    )
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))

    with pytest.raises(IntegrityViolation, match="wrong checker route"):
        _handler(store)(_context(store, _payload()))


def test_numeric_compile_stage_rejects_handler_compiler_wrongly_routed_to_graph(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        constraint_validation_handler,
        "compile_all",
        lambda constraints: [worker_validation.GraphChecker() for _constraint in constraints],
    )
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))

    with pytest.raises(IntegrityViolation, match="wrong numeric route"):
        _handler(store)(_context(store, _payload()))


def test_purely_structural_candidate_validated_via_clingo() -> None:
    # Clingo and the independent Graph reference agree; numeric engines are N/A.
    store = _store((_structural("C_acyclic", "acyclic(quest_steps)"),))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validated"
    by_engine = {
        s.engine_id: s
        for s in _compile_evidence(store, outcome).stages
        if s.stage == "differential"
    }
    assert by_engine["clingo"].status == "passed"
    assert by_engine["graph-reference"].status == "passed"
    assert by_engine["z3"].status == "not_applicable"
    assert by_engine["numeric-reference"].status == "not_applicable"


@pytest.mark.parametrize(
    "assert_expr",
    (
        "quest_step_dependency_graph_is_acyclic",
        "every_collect_step_has_a_drop_source",
    ),
)
def test_exact_supported_structural_predicates_receive_two_engine_coverage(
    assert_expr: str,
) -> None:
    store = _store((_structural("C_structural", assert_expr),))
    outcome = _handler(store)(_context(store, _payload()))
    by_engine = {
        stage.engine_id: stage
        for stage in _compile_evidence(store, outcome).stages
        if stage.stage == "differential"
    }

    assert outcome.summary.outcome_code == "constraint_validated"
    assert by_engine["clingo"].status == "passed"
    assert by_engine["graph-reference"].status == "passed"


@pytest.mark.parametrize(
    ("engine_type", "backend_name", "assert_expr", "defect_class"),
    (
        (
            ClingoDifferentialEngine,
            "ASPChecker",
            "acyclic(quest_steps)",
            "cyclic_dependency",
        ),
        (
            GraphReferenceDifferentialEngine,
            "GraphChecker",
            "acyclic(quest_steps)",
            "cyclic_dependency",
        ),
        (
            ClingoDifferentialEngine,
            "ASPChecker",
            "every_collect_step_has_a_drop_source",
            "missing_drop_source",
        ),
        (
            GraphReferenceDifferentialEngine,
            "GraphChecker",
            "every_collect_step_has_a_drop_source",
            "missing_drop_source",
        ),
    ),
)
def test_structural_engines_execute_dirty_and_clean_witnesses_for_each_predicate(
    monkeypatch,
    engine_type,
    backend_name: str,
    assert_expr: str,
    defect_class: str,
) -> None:
    real_backend = getattr(worker_validation, backend_name)
    observed = []

    class RecordingChecker:
        id = f"recording:{backend_name}"

        def check(self, snapshot, nav=None):
            findings = real_backend().check(snapshot, nav=nav)
            observed.append((snapshot, findings))
            return findings

    monkeypatch.setattr(worker_validation, backend_name, RecordingChecker)
    constraint = _structural("C_witness", assert_expr)
    result = engine_type().evaluate(
        DifferentialEvalRequest(constraints=(constraint,), dsl_grammar_version="dsl@1")
    )

    assert result.status == "evaluated"
    assert result.consistency == "consistent"
    assert result.decided_constraint_ids == (constraint.id,)
    assert len(observed) == 2
    dirty_findings = observed[0][1]
    clean_findings = observed[1][1]
    assert any(
        finding.defect_class == defect_class and finding.status == "confirmed"
        for finding in dirty_findings
    )
    assert not any(
        finding.defect_class == defect_class and finding.status == "confirmed"
        for finding in clean_findings
    )


def test_structural_engine_cannot_claim_coverage_when_witness_is_not_detected(
    monkeypatch,
) -> None:
    class BrokenChecker:
        id = "broken"

        def check(self, snapshot, nav=None):
            return []

    monkeypatch.setattr(worker_validation, "ASPChecker", BrokenChecker)
    constraint = _structural("C_cycle", "acyclic(quest_steps)")
    result = ClingoDifferentialEngine().evaluate(
        DifferentialEvalRequest(constraints=(constraint,), dsl_grammar_version="dsl@1")
    )

    assert result.status == "evaluated"
    assert result.consistency == "inconsistent"
    assert result.decided_constraint_ids == ()


@pytest.mark.parametrize(
    "engine_type",
    (ClingoDifferentialEngine, GraphReferenceDifferentialEngine),
)
def test_structural_engine_rejects_compiler_that_misses_the_dirty_witness(
    monkeypatch,
    engine_type,
) -> None:
    class MissingCompiledChecker:
        id = "compiled:missing"

        def check(self, snapshot, nav=None):
            return []

    monkeypatch.setattr(
        worker_validation,
        "compile_all",
        lambda constraints: [MissingCompiledChecker()],
    )
    constraint = _structural("C_compiler_miss", "acyclic(quest_steps)")

    result = engine_type().evaluate(
        DifferentialEvalRequest(constraints=(constraint,), dsl_grammar_version="dsl@1")
    )

    assert result.status == "evaluated"
    assert result.consistency == "inconsistent"
    assert result.decided_constraint_ids == ()


@pytest.mark.parametrize(
    "engine_type",
    (ClingoDifferentialEngine, GraphReferenceDifferentialEngine),
)
def test_structural_engine_rejects_compiler_wrong_route_with_extra_findings(
    monkeypatch,
    engine_type,
) -> None:
    class UnfilteredGraphCompiledChecker:
        id = "compiled:graph:unfiltered"

        def __init__(self, constraint: Constraint) -> None:
            self.constraint = constraint

        def check(self, snapshot, nav=None):
            return [
                finding.model_copy(update={"constraint_id": self.constraint.id})
                for finding in worker_validation.GraphChecker().check(snapshot, nav=nav)
            ]

    monkeypatch.setattr(
        worker_validation,
        "compile_all",
        lambda constraints: [UnfilteredGraphCompiledChecker(constraints[0])],
    )
    constraint = _structural(
        "C_compiler_wrong_route",
        "every_collect_step_has_a_drop_source",
    )

    result = engine_type().evaluate(
        DifferentialEvalRequest(constraints=(constraint,), dsl_grammar_version="dsl@1")
    )

    assert result.status == "evaluated"
    assert result.consistency == "inconsistent"
    assert result.decided_constraint_ids == ()


@pytest.mark.parametrize(
    "constraint",
    (
        _structural("C_reachable", "reachable_in(target, giver)"),
        _structural(
            "C_reachable_source",
            "every_collect_step_has_a_reachable_drop_source",
        ),
        _structural("C_general", "quest_content_graph_is_well_formed"),
        Constraint(
            id="C_narrative",
            dsl_grammar_version="dsl@1",
            kind="narrative",
            oracle="deterministic",
            **{"assert": "acyclic(quest_steps)"},
            severity="major",
        ),
        _structural("C_partial", "acyclic(quest_steps)").model_copy(
            update={
                "predicates": [
                    Predicate(expr="reachable_in(target, giver)", oracle="deterministic")
                ]
            }
        ),
    ),
)
def test_unsupported_general_and_narrative_predicates_are_never_covered(
    constraint: Constraint,
) -> None:
    store = _store((constraint,))
    outcome = _handler(store)(_context(store, _payload()))
    by_engine = {
        stage.engine_id: stage
        for stage in _compile_evidence(store, outcome).stages
        if stage.stage == "differential"
    }

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert by_engine["clingo"].status == "unproven"
    assert by_engine["clingo"].reason_code == "clingo_predicate_unsupported"
    assert by_engine["graph-reference"].status == "unproven"
    assert by_engine["graph-reference"].reason_code == ("graph_reference_predicate_unsupported")


@pytest.mark.parametrize(
    "constraints",
    (
        (_constraint("C_cap", "reward_gold <= 80"),),
        (_structural("C_acyclic", "acyclic(quest_steps)"),),
    ),
)
def test_domain_partitioned_z3_and_clingo_alone_never_form_a_differential_quorum(
    constraints: tuple[Constraint, ...],
) -> None:
    store = _store(constraints)
    outcome = _handler(store)(_context(store, _payload(engines=_PARTITIONED_ENGINES)))
    compile_evidence = _compile_evidence(store, outcome)
    compile_requirement = next(
        item
        for item in _evidence_set(store, outcome).requirements
        if item.requirement_id == "compile"
    )

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert compile_evidence.overall_status == "unproven"
    assert compile_requirement.status == "unproven"
    assert any(
        stage.reason_code == "candidate_independent_coverage_incomplete"
        for stage in compile_evidence.stages
        if stage.stage == "differential"
    )


def test_empty_candidate_is_unproven_never_validated() -> None:
    # An empty proposal is not a compilable candidate. It must fail before the
    # differential rather than deriving a vacuous all-not-applicable pass.
    store = _store(())
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validation_failed_without_candidate"
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "failed"
    evidence = _compile_evidence(store, outcome)
    typecheck = next(stage for stage in evidence.stages if stage.stage == "typecheck")
    assert typecheck.status == "failed"
    assert typecheck.reason_code == "empty_constraint_candidate"
    differential = [s for s in evidence.stages if s.stage == "differential"]
    assert all(s.status == "unproven" for s in differential)


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
    assert by_engine["numeric-reference"].status == "failed"
    assert by_engine["numeric-reference"].reason_code == "candidate_inconsistent"
    assert by_engine["clingo"].status == "not_applicable"
    assert by_engine["graph-reference"].status == "not_applicable"


def test_numeric_constraints_are_checked_jointly_within_one_scope() -> None:
    store = _store(
        (
            _constraint("C_upper", "reward_gold <= 80"),
            _constraint("C_lower", "reward_gold >= 100"),
        )
    )
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    z3_stage = next(
        stage
        for stage in _compile_evidence(store, outcome).stages
        if stage.stage == "differential" and stage.engine_id == "z3"
    )
    assert z3_stage.status == "failed"
    assert z3_stage.reason_code == "candidate_inconsistent"


def test_numeric_constraints_in_distinct_scopes_do_not_share_symbols() -> None:
    upper = _constraint("C_upper", "reward_gold <= 80").model_copy(
        update={"scope": Selector(var="item", node_type="ITEM", where={"tier": "starter"})}
    )
    lower = _constraint("C_lower", "reward_gold >= 100").model_copy(
        update={"scope": Selector(var="item", node_type="ITEM", where={"tier": "endgame"})}
    )
    store = _store((upper, lower))
    outcome = _handler(store)(_context(store, _payload()))

    assert outcome.summary.outcome_code == "constraint_validated"


def test_numeric_differential_uses_real_not_integer_feasibility() -> None:
    store = _store((_constraint("C_fraction", "0 < reward_ratio < 1"),))
    outcome = _handler(store)(_context(store, _payload()))
    by_engine = {
        stage.engine_id: stage
        for stage in _compile_evidence(store, outcome).stages
        if stage.stage == "differential"
    }

    assert outcome.summary.outcome_code == "constraint_validated"
    assert by_engine["z3"].status == "passed"
    assert by_engine["numeric-reference"].status == "passed"


def test_unknown_engine_version_is_an_execution_failure_and_never_relabels_v1() -> None:
    requested = (
        SolverEngineRefV1(engine_id="clingo", version=1),
        SolverEngineRefV1(engine_id="z3", version=999),
    )
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    with pytest.raises(IntegrityViolation, match="engine is not registered"):
        _handler(store)(_context(store, _payload(engines=requested)))

    assert store.put_count == 0


def test_registry_key_cannot_relabel_an_implementation_with_another_version() -> None:
    requested = (
        SolverEngineRefV1(engine_id="clingo", version=1),
        SolverEngineRefV1(engine_id="z3", version=999),
    )
    registry = build_differential_engines()
    mislabeled = _MislabeledEngine()
    registry[("z3", 999)] = mislabeled
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    with pytest.raises(IntegrityViolation, match="engine identity"):
        _handler(store, differential_engines=registry)(_context(store, _payload(engines=requested)))

    assert mislabeled.calls == 0
    assert store.put_count == 0


def test_engine_cannot_claim_pass_without_positive_candidate_coverage() -> None:
    registry = build_differential_engines()
    registry[("z3", 1)] = _NoCoverageEngine()
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    outcome = _handler(store, differential_engines=registry)(_context(store, _payload()))
    compile_evidence = _compile_evidence(store, outcome)
    z3_stage = next(
        stage
        for stage in compile_evidence.stages
        if stage.stage == "differential" and stage.engine_id == "z3"
    )
    compile_requirement = next(
        item
        for item in _evidence_set(store, outcome).requirements
        if item.requirement_id == "compile"
    )

    assert z3_stage.status == "unproven"
    assert z3_stage.reason_code == "engine_reported_no_coverage"
    assert compile_evidence.overall_status == "unproven"
    assert compile_requirement.status == compile_evidence.overall_status


def test_independent_numeric_engine_disagreement_fails_closed() -> None:
    registry = build_differential_engines()
    registry[("numeric-reference", 1)] = _InconsistentNumericReference()
    store = _store((_constraint("C_cap", "reward_gold <= 80"),))
    outcome = _handler(store, differential_engines=registry)(_context(store, _payload()))
    by_engine = {
        stage.engine_id: stage
        for stage in _compile_evidence(store, outcome).stages
        if stage.stage == "differential"
    }

    assert by_engine["z3"].status == "passed"
    assert by_engine["numeric-reference"].status == "failed"
    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"


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
    assert by_engine["numeric-reference"].status == "unproven"
    assert by_engine["numeric-reference"].reason_code == "numeric_reference_unsupported_predicate"
    assert by_engine["clingo"].status == "not_applicable"
    assert by_engine["graph-reference"].status == "not_applicable"
    assert all(s.status != "passed" for s in evidence.stages if s.stage == "differential")


def test_failing_regression_fails_with_candidate() -> None:
    # a MIXED candidate makes the differential genuinely pass so regression runs;
    # a failing regression then drives the failed-with-candidate outcome.
    store = _store(_MIXED)
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
    )
    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    # compile passed -> candidate published; the regression dimension failed.
    assert len([a for a in outcome.artifacts if a.kind == "constraint_snapshot"]) == 1
    ev_set = _evidence_set(store, outcome)
    assert ev_set.overall_status == "failed"
    assert len(outcome.findings) == 1
    prepared_finding = outcome.findings[0]
    assert prepared_finding.evidence_artifact_index == 3
    assert prepared_finding.payload.producer_run_id == "run:1"
    assert prepared_finding.payload.snapshot_id == SOURCE_SNAPSHOT_ID
    regression_artifact = outcome.artifacts[prepared_finding.evidence_artifact_index]
    sealed_regression = decode_and_validate_artifact_payload(
        payload_schema_id="regression-evidence@1",
        blob=store.read_prepared(regression_artifact.object_ref),
    )
    assert sealed_regression["snapshot_id"] == SOURCE_SNAPSHOT_ID
    assert sealed_regression["findings"][0]["id"] == "regression:fresh"
    produced = [d for d in outcome.requirement_dispositions if d.status == "produced"]
    assert produced and all(d.outcome_rule_id == "regression" for d in produced)


def test_constraint_regression_rejects_llm_finding_authority() -> None:
    class LlmRegressionRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            finding = Finding(
                id="regression:llm",
                source="llm",
                producer_id="llm-routed",
                producer_run_id="regression-runner",
                oracle_type="llm-assisted",
                defect_class="llm_assisted_predicate",
                severity="major",
                snapshot_id=SOURCE_SNAPSHOT_ID,
                status="unproven",
                message="suggestion-only output",
            )
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="unproven",
                reason_code="llm_only",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": SOURCE_SNAPSHOT_ID,
                    "seed": request.seed,
                    "status": "unproven",
                    "reason_code": "llm_only",
                    "findings": [finding.model_dump(mode="json")],
                },
                **_constraint_execution_binding(request, SOURCE_SNAPSHOT_ID),
            )

    store = _store(_MIXED)
    with pytest.raises(IntegrityViolation, match="deterministic oracle authority"):
        _handler(store, regression_runner=LlmRegressionRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
        )


def test_unproven_regression_seals_the_adapter_reason_on_both_wires() -> None:
    store = _store(_MIXED)
    runner = _UnprovenRegressionRunner()
    outcome = _handler(store, regression_runner=runner)(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    evidence = _evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )
    artifact = next(
        item for item in outcome.artifacts if item.payload_schema_id == "regression-evidence@1"
    )
    sealed = json.loads(store.read_prepared(artifact.object_ref))

    assert requirement.reason_code == runner.reason
    assert sealed["reason_code"] == runner.reason


def test_default_regression_runner_never_manufactures_a_pass() -> None:
    store = _store(_MIXED)
    outcome = _handler(store)(_context(store, _payload(regression=(REGRESSION_SUITE_ID,))))
    evidence = _evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "constraint_regression_runner_unavailable"


def test_regression_evidence_rejects_a_runner_wire_for_another_source() -> None:
    class MisreportingWireRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": SECOND_REGRESSION_SUITE_ID,
                    "snapshot_id": request.snapshot_id,
                    "seed": request.seed + 1,
                    "status": "failed",
                },
                action_work_units=1,
                **_constraint_execution_binding(request, SOURCE_SNAPSHOT_ID),
            )

    store = _store(_MIXED)
    with pytest.raises(IntegrityViolation, match="exact execution binding"):
        _handler(store, regression_runner=MisreportingWireRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=None)
        )


def test_regression_requires_measured_work_for_an_executed_verdict() -> None:
    class MissingWorkRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": SOURCE_SNAPSHOT_ID,
                    "seed": request.seed,
                    "status": "passed",
                },
                **_constraint_execution_binding(request, SOURCE_SNAPSHOT_ID),
            )

    store = _store(_MIXED)
    with pytest.raises(IntegrityViolation, match="omitted measured action work"):
        _handler(store, regression_runner=MissingWorkRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
        )


def test_regression_suites_share_one_aggregate_work_ledger() -> None:
    class BudgetRunner:
        def __init__(self) -> None:
            self.requests = []
            self.works = iter((MAX_REPAIR_REGRESSION_WORK_UNITS_V1 - 1, 2))

        def run(self, request) -> RegressionSuiteResultV1:
            self.requests.append(request)
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": SOURCE_SNAPSHOT_ID,
                    "seed": request.seed,
                    "status": "passed",
                },
                action_work_units=next(self.works),
                **_constraint_execution_binding(request, SOURCE_SNAPSHOT_ID),
            )

    store = _store(_MIXED)
    runner = BudgetRunner()
    with pytest.raises(IntegrityViolation, match="aggregate work budget"):
        _handler(store, regression_runner=runner)(
            _context(
                store,
                _payload(regression=(REGRESSION_SUITE_ID, SECOND_REGRESSION_SUITE_ID)),
                seed=17,
            )
        )

    assert [request.max_action_work_units for request in runner.requests] == [
        MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
        1,
    ]


def test_default_golden_runner_never_manufactures_a_pass() -> None:
    store = _store(_MIXED)
    outcome = _handler(store)(_context(store, _payload(golden=GOLDEN_SUITE_ID)))
    golden = next(
        stage for stage in _compile_evidence(store, outcome).stages if stage.stage == "golden"
    )

    assert outcome.summary.outcome_code == "constraint_validation_failed_with_candidate"
    assert golden.status == "unproven"
    assert golden.reason_code == "golden_runner_unavailable"


def test_injected_golden_runner_must_explicitly_attest_pass() -> None:
    store = _store(_MIXED)
    outcome = _handler(store, golden_runner=_PassingGoldenRunner())(
        _context(store, _payload(golden=GOLDEN_SUITE_ID))
    )

    assert outcome.summary.outcome_code == "constraint_validated"


def test_semantic_candidate_identity_closes_every_local_version_tuple() -> None:
    frozen = VersionTuple(
        doc_version="doc:proposal",
        ir_snapshot_id="ir:proposal",
        constraint_snapshot_id="constraint:base",
        tool_version="constraint-validation@1",
        seed=17,
    )
    store = _store(_MIXED)
    outcome = _handler(
        store,
        regression_runner=_PassingRegressionRunner(source_snapshot_id="ir:proposal"),
    )(
        _context(
            store,
            _payload(regression=(REGRESSION_SUITE_ID,)),
            seed=17,
            version_tuple=frozen,
        )
    )
    candidate = next(item for item in outcome.artifacts if item.kind == "constraint_snapshot")
    candidate_artifact_id = content_addressed_artifact_id(candidate)
    target = _evidence_set(store, outcome).target_binding
    assert isinstance(target, ConstraintTargetBindingV1)
    semantic_id = target.target_snapshot_id

    assert semantic_id.startswith("candidate:")
    assert semantic_id != candidate_artifact_id
    for artifact in outcome.artifacts:
        assert artifact.version_tuple.doc_version == frozen.doc_version
        assert artifact.version_tuple.ir_snapshot_id == frozen.ir_snapshot_id
        assert artifact.version_tuple.constraint_snapshot_id == semantic_id
        assert artifact.version_tuple.seed == (
            None if artifact.kind == "constraint_snapshot" else frozen.seed
        )

    compile_evidence = _compile_evidence(store, outcome)
    assert compile_evidence.candidate_constraint_snapshot_artifact_id == candidate_artifact_id


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
