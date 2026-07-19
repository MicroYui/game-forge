"""``constraint_validator@1`` — the ≥2-engine differential constraint-compile validator.

Compiles the subject ``constraint_proposal``'s constraints through a fixed
``parse → typecheck → compile → differential(×≥2) → golden`` pipeline and records
EVERY stage in a ``ConstraintCompileEvidenceV1`` with its exact status /
reason_code. The ``differential`` stage runs ≥2 EXACT engines named by
``payload.differential_engines`` (the two initial engines wrap ``spine/checkers``:
Clingo/ASP + z3/SMT). Each engine authoritatively decides ITS OWN solver domain and
reports honestly: a ``passed`` differential stage requires the engine to have
GENUINELY evaluated a candidate in its domain and found it consistent; an engine
whose domain does not apply is a sound ``not_applicable`` stage; an engine that
could not decide is ``unproven``; and a genuine contradiction is ``failed``. The
soundness guard requires every constraint to receive positive decisions from two
distinct exact engines. Domain-partitioned z3 + Clingo alone therefore cannot claim
a differential pass; the worker supplies independent numeric-reference and
graph-reference peers. Missing quorum is ``unproven``, never a vacuous pass. When
(and only when) the
``compile`` stage passes, ONE candidate ``constraint_snapshot[constraint-snapshot@1]``
is published in the exact ``readers.load_constraints`` wire shape; otherwise the
candidate id is null.

Three frozen outcomes (all run,succeeded — a business result, not a RunFailure):

* ``constraint_validated`` — candidate 1/1, every stage + every regression passed;
* ``constraint_validation_failed_with_candidate`` — compile passed (candidate 1/1)
  but a later stage / regression did not; regression short-circuits carry
  ``prior_requirement_failed`` dispositions;
* ``constraint_validation_failed_without_candidate`` — compile failed (candidate id
  null); regression short-circuits carry ``candidate_unavailable`` dispositions and
  produce ZERO regression evidence.

The verdict is DETERMINISTIC — no LLM (``llm_modes=NA``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    ProfileRefV1,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import NodeType
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PreparedArtifact,
    PreparedRunOutcome,
    RequirementDispositionV1,
    SolverEngineRefV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.workflow import (
    CONSTRAINT_COMPILE_REQUIREMENT_KIND,
    ConstraintCompileEvidenceV1,
    ConstraintCompileStageV1,
    ConstraintProposalV1,
    ConstraintTargetBindingV1,
    EvidenceRequirement,
    EvidenceSet,
)
from gameforge.spine.dsl.ast import DslError, parse_assert
from gameforge.spine.checkers.base import CheckerExecutionBinding
from gameforge.spine.dsl.compile import compile_all

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    prepared_version_tuple,
    rebind_finding_producers,
    require_exact_profile_bindings,
    scoped_finding_series_id,
    store_prepared_artifact,
    store_prepared_blob,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.validation_common import (
    CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID,
    CONSTRAINT_SNAPSHOT_KIND,
    CONSTRAINT_SNAPSHOT_SCHEMA_ID,
    EVIDENCE_SET_SCHEMA_ID,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    RESOLVED_CONSTRAINT,
    VALIDATION_EVIDENCE_KIND,
    ConstraintRegressionCandidateV1,
    DimensionResult,
    RegressionRunner,
    RegressionRunRequest,
    RegressionSuiteResultV1,
    content_addressed_artifact_id,
    deterministic_finding_status,
    evidence_requirement,
    overall_status_of,
    regression_evidence_version_tuple,
    validation_child_execution_seed,
    validate_authoritative_regression_findings,
    with_validation_child_seed_evidence,
)

VALIDATION_POLICY_FIELD = "/params/validation_policy"
COMPILER_PROFILE_FIELD = "/params/compiler_profile"
COMPILE_TOOL_VERSION = "constraint-compile@1"
REGRESSION_TOOL_VERSION = "regression@1"
EVIDENCE_TOOL_VERSION = "constraint-validation@1"
BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1 = (
    SolverEngineRefV1(engine_id="clingo", version=1),
    SolverEngineRefV1(engine_id="graph-reference", version=1),
    SolverEngineRefV1(engine_id="numeric-reference", version=1),
    SolverEngineRefV1(engine_id="z3", version=1),
)

_VALIDATED_CODE = "constraint_validated"
_FAILED_WITH_CANDIDATE_CODE = "constraint_validation_failed_with_candidate"
_FAILED_WITHOUT_CANDIDATE_CODE = "constraint_validation_failed_without_candidate"

_SHORT_CIRCUIT = "execution_short_circuited"

StageStatus = Literal["passed", "failed", "unproven", "not_applicable"]


@dataclass(frozen=True, slots=True)
class DifferentialEvalRequest:
    """The compiled candidate an engine cross-checks (deterministic, seeded)."""

    constraints: tuple[Constraint, ...]
    dsl_grammar_version: str


@dataclass(frozen=True, slots=True)
class DifferentialEngineResultV1:
    """One engine's deterministic differential verdict for the candidate.

    An engine reports whether ITS solver domain applies to the candidate and, if
    so, its consistency verdict:

    * ``evaluated`` + ``consistency`` — the engine's domain applied and it decided;
    * ``not_applicable`` — the candidate has no constraint in this engine's domain
      (it executed and honestly found nothing to decide — a sound, non-attesting
      outcome, NOT a missing execution);
    * ``undecided`` — the engine's domain applied but it could not decide (timeout /
      ``unknown`` / an in-domain constraint it cannot bind).

    ``decided_constraint_ids`` are the ids this engine POSITIVELY decided consistent
    (its sound coverage contribution); the handler requires two distinct exact
    engine identities for every candidate constraint.

    A ``passed`` differential stage requires ``evaluated`` + ``consistent``;
    ``not_applicable`` maps to a ``not_applicable`` stage (does not block validation)
    and ``undecided`` to an ``unproven`` stage — neither ever attests consistency.
    """

    status: Literal["evaluated", "not_applicable", "undecided"]
    consistency: Literal["consistent", "inconsistent"] | None = None
    reason_code: str | None = None
    decided_constraint_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        decided = self.decided_constraint_ids
        if any(not isinstance(value, str) or not value for value in decided):
            raise ValueError("differential result contains an invalid constraint id")
        if len(decided) != len(set(decided)):
            raise ValueError("differential result repeats a decided constraint id")
        if len(decided) > 4_096:
            raise ValueError("differential result exceeds the decided-id bound")
        object.__setattr__(self, "decided_constraint_ids", tuple(sorted(decided)))
        if self.status == "evaluated":
            if self.consistency is None or self.reason_code is not None:
                raise ValueError("evaluated differential result requires consistency and no reason")
            return
        if self.consistency is not None:
            raise ValueError("non-evaluated differential result cannot claim consistency")
        if not self.reason_code:
            raise ValueError("non-evaluated differential result requires a reason")
        if self.status == "not_applicable":
            if self.reason_code != "engine_domain_not_applicable" or decided:
                raise ValueError(
                    "not-applicable differential result must carry the exact empty-domain reason"
                )


class ConstraintDifferentialEngine(Protocol):
    """One exact differential solver engine (concrete impls wrap ``spine/checkers``)."""

    engine_id: str
    engine_version: int

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1: ...


class GoldenSuiteRunner(Protocol):
    """Replay the bound golden suite against the compiled candidate (deterministic)."""

    def run(
        self, *, golden_suite_artifact_id: str, constraints: tuple[Constraint, ...]
    ) -> GoldenSuiteResultV1: ...


@dataclass(frozen=True, slots=True)
class GoldenSuiteResultV1:
    """One exact golden replay verdict; unavailable execution is never a pass."""

    status: Literal["passed", "failed", "unproven"]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "passed":
            if self.reason_code is not None:
                raise ValueError("passed golden replay cannot carry a reason")
        elif self.status == "failed":
            if self.reason_code != "golden_suite_failed":
                raise ValueError("failed golden replay requires golden_suite_failed")
        elif self.reason_code not in {"golden_runner_unavailable", "golden_suite_unproven"}:
            raise ValueError("unproven golden replay requires an allowlisted reason")


class _UnavailableGoldenSuiteRunner:
    """Fail-closed process default until an exact golden adapter is injected."""

    def run(
        self, *, golden_suite_artifact_id: str, constraints: tuple[Constraint, ...]
    ) -> GoldenSuiteResultV1:
        return GoldenSuiteResultV1(
            status="unproven",
            reason_code="golden_runner_unavailable",
        )


class _UnavailableConstraintRegressionRunner:
    """Fail-closed default: no adapter execution can manufacture green evidence."""

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        reason = "constraint_regression_runner_unavailable"
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="unproven",
            reason_code=reason,
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": None,
                "seed": request.seed,
                "status": "unproven",
                "reason_code": reason,
            },
        )


def load_proposal(blobs: ArtifactBlobReader, artifact_id: str) -> ConstraintProposalV1:
    """Parse the subject ``constraint_proposal`` artifact into its typed proposal."""

    return ConstraintProposalV1.model_validate(load_json_blob(blobs, artifact_id))


@dataclass(frozen=True, slots=True)
class _CompilePipelineV1:
    """The full compile pipeline result: every stage + whether a candidate formed.

    ``differential_positively_covered`` is the SOUNDNESS GUARD: every candidate
    constraint was positively decided by at least two distinct exact engines. Domain
    partitioning alone (for example z3 for numeric + Clingo for structural) is not a
    differential cross-check and can never satisfy this quorum.
    """

    stages: tuple[ConstraintCompileStageV1, ...]
    overall_status: Literal["passed", "failed", "unproven"]
    compile_passed: bool
    differential_positively_covered: bool


@dataclass(frozen=True, slots=True)
class ConstraintValidationHandler:
    """A ``RunExecutor`` for ``constraint_validator@1`` (deterministic, no LLM)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    differential_engines: Mapping[tuple[str, int], ConstraintDifferentialEngine]
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding
    golden_runner: GoldenSuiteRunner = field(default_factory=_UnavailableGoldenSuiteRunner)
    regression_runner: RegressionRunner = field(
        default_factory=_UnavailableConstraintRegressionRunner
    )

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ConstraintValidationPayloadV1):
            raise TypeError("constraint_validator@1 requires a constraint-validation@1 payload")
        require_exact_profile_bindings(
            context,
            expected={
                VALIDATION_POLICY_FIELD: (payload.validation_policy, "validation"),
                COMPILER_PROFILE_FIELD: (payload.compiler_profile, "constraint_compiler"),
            },
            validator=self.profile_binding_validator,
        )

        proposal = load_proposal(self.blobs, payload.subject.subject_artifact_id)
        candidate = tuple(proposal.constraints)
        candidate_wire = _candidate_wire(payload.dsl_grammar_version, candidate)
        candidate_digest = canonical_sha256(candidate_wire)
        candidate_snapshot_id = f"candidate:{candidate_digest[:32]}"
        lineage = self._artifact_lineage(payload)

        pipeline = self._run_pipeline(payload, candidate)

        candidate_artifact = (
            self._seal_candidate(
                context,
                candidate_wire,
                candidate_snapshot_id,
                lineage,
            )
            if pipeline.compile_passed
            else None
        )
        candidate_id = (
            content_addressed_artifact_id(candidate_artifact)
            if candidate_artifact is not None
            else None
        )
        compile_evidence = self._seal_compile_evidence(
            context,
            payload,
            pipeline,
            candidate_id,
            candidate_snapshot_id if candidate_id is not None else None,
            lineage,
        )
        compile_evidence_id = content_addressed_artifact_id(compile_evidence)

        (
            regression_artifacts,
            regression_dimensions,
            dispositions,
            regression_finding_batches,
        ) = self._regression_phase(
            context,
            payload,
            pipeline,
            candidate_snapshot_id if candidate_id is not None else None,
            candidate_digest if candidate_id is not None else None,
            candidate,
            lineage,
        )

        compile_dimension = self._compile_dimension(payload, pipeline, compile_evidence_id)
        requirements = tuple(
            evidence_requirement(dim) for dim in (compile_dimension, *regression_dimensions)
        )
        overall = overall_status_of(
            tuple(dim.status for dim in (compile_dimension, *regression_dimensions))
        )

        target_binding = self._target_binding(
            payload,
            candidate_id,
            candidate_snapshot_id,
            candidate_digest,
        )
        supporting = (
            *([candidate_id] if candidate_id is not None else []),
            compile_evidence_id,
            *(content_addressed_artifact_id(a) for a in regression_artifacts),
            *(
                [payload.base_constraint_snapshot_artifact_id]
                if payload.base_constraint_snapshot_artifact_id is not None
                else []
            ),
            *payload.regression_suite_artifact_ids,
            *(
                [payload.golden_suite_artifact_id]
                if payload.golden_suite_artifact_id is not None
                else []
            ),
        )
        evidence_set = self._seal_evidence_set(
            context,
            payload,
            target_binding,
            requirements,
            supporting,
            lineage,
            overall,
        )

        artifacts: tuple[PreparedArtifact, ...] = (
            evidence_set,
            *([candidate_artifact] if candidate_artifact is not None else []),
            compile_evidence,
            *regression_artifacts,
        )
        regression_artifact_offset = 2 + (1 if candidate_artifact is not None else 0)
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(
                    finding=finding,
                    evidence_artifact_index=regression_artifact_offset + index,
                    finding_id=scoped_finding_series_id(
                        namespace="constraint-regression",
                        scope_id=suite_id,
                        finding_id=finding.id,
                    ),
                )
                for index, (suite_id, findings) in enumerate(regression_finding_batches)
                for finding in findings
            ),
            run_id=context.run.run_id,
        )
        outcome_code = self._outcome_code(pipeline, overall)
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=outcome_code,
            primary_index=0,
            artifacts=artifacts,
            findings=prepared_findings,
            # A positively validated result publishes the complete resolved
            # regression set, so there is no unexecuted subset for terminal
            # completion to consume.  Produced dispositions are meaningful on
            # the failed/unproven variants only.
            requirement_dispositions=(() if outcome_code == _VALIDATED_CODE else dispositions),
        )

    # ------------------------------------------------------------------ pipeline
    def _run_pipeline(
        self, payload: ConstraintValidationPayloadV1, candidate: tuple[Constraint, ...]
    ) -> _CompilePipelineV1:
        stages: list[ConstraintCompileStageV1] = []

        parse_status, parse_reason = self._parse(candidate)
        stages.append(self._stage("parse", "parse", parse_status, parse_reason))

        typecheck_status, typecheck_reason = (
            self._typecheck(payload, candidate)
            if parse_status == "passed"
            else ("unproven", _SHORT_CIRCUIT)
        )
        stages.append(self._stage("typecheck", "typecheck", typecheck_status, typecheck_reason))

        compile_ok = parse_status == "passed" and typecheck_status == "passed"
        if compile_ok:
            compile_status, compile_reason = self._compile(candidate)
        else:
            compile_status, compile_reason = "unproven", _SHORT_CIRCUIT
        stages.append(self._stage("compile", "compile", compile_status, compile_reason))
        compile_passed = compile_status == "passed"

        differential_stages, covered_ids = self._differential(
            payload, candidate, run=compile_passed
        )
        stages.extend(differential_stages)
        stages.append(self._golden_stage(payload, candidate, run=compile_passed))

        overall = overall_status_of(tuple(stage.status for stage in stages))
        candidate_ids = {constraint.id for constraint in candidate}
        # SOUNDNESS GUARD: every constraint has two independent positive decisions.
        positively_covered = bool(covered_ids) and candidate_ids <= covered_ids
        return _CompilePipelineV1(
            stages=tuple(stages),
            overall_status=overall,
            compile_passed=compile_passed,
            differential_positively_covered=positively_covered,
        )

    def _parse(self, candidate: tuple[Constraint, ...]) -> tuple[StageStatus, str | None]:
        for constraint in candidate:
            if not constraint.assert_:
                return "failed", "empty_assert_expression"
            if constraint.kind == "numeric" and not constraint.has_llm_predicate():
                try:
                    parse_assert(constraint.assert_)
                except DslError:
                    return "failed", "assert_parse_error"
        return "passed", None

    def _typecheck(
        self, payload: ConstraintValidationPayloadV1, candidate: tuple[Constraint, ...]
    ) -> tuple[StageStatus, str | None]:
        if not candidate:
            return "failed", "empty_constraint_candidate"
        for constraint in candidate:
            if constraint.dsl_grammar_version != payload.dsl_grammar_version:
                return "failed", "dsl_grammar_version_mismatch"
            if constraint.scope is not None and constraint.forall is not None:
                return "failed", "selector_scope_ambiguous"
            for selector in (constraint.scope, constraint.forall):
                if selector is None:
                    continue
                try:
                    NodeType[selector.node_type]
                except KeyError:
                    return "failed", "selector_node_type_invalid"
        if any(constraint.has_llm_predicate() for constraint in candidate):
            # an llm-assisted predicate cannot be validated deterministically here.
            return "unproven", "llm_assisted_predicate_deferred"
        return "passed", None

    def _compile(self, candidate: tuple[Constraint, ...]) -> tuple[StageStatus, str | None]:
        # Parse/typecheck already convert deterministic proposal defects into stage
        # verdicts. Any exception here is an execution/integrity fault and must reach
        # the worker failure classifier so the current draft is restored.
        compiled = compile_all(list(candidate))
        if len(compiled) != len(candidate):
            raise IntegrityViolation("constraint compiler returned the wrong checker count")
        for constraint, checker in zip(candidate, compiled, strict=True):
            if constraint.kind != "numeric":
                continue
            binding = getattr(checker, "execution_binding", None)
            if (
                not isinstance(binding, CheckerExecutionBinding)
                or binding.wrapper_id != getattr(checker, "id", None)
                or binding.native_id != "smt"
                or binding.constraint_id != constraint.id
            ):
                raise IntegrityViolation("constraint compiler returned the wrong numeric route")
        return "passed", None

    def _differential(
        self,
        payload: ConstraintValidationPayloadV1,
        candidate: tuple[Constraint, ...],
        *,
        run: bool,
    ) -> tuple[tuple[ConstraintCompileStageV1, ...], set[str]]:
        """Run every engine; return its stages + the union of positively-decided ids."""

        if not run:
            stages = tuple(
                self._differential_stage(engine_ref, "unproven", _SHORT_CIRCUIT)
                for engine_ref in payload.differential_engines
            )
            return stages, set()
        stages_list: list[ConstraintCompileStageV1] = []
        coverage_by_constraint: dict[str, set[tuple[str, int]]] = {}
        candidate_ids = {constraint.id for constraint in candidate}
        resolved_engines: list[tuple[SolverEngineRefV1, ConstraintDifferentialEngine]] = []
        for engine_ref in payload.differential_engines:
            engine = self.differential_engines.get((engine_ref.engine_id, engine_ref.version))
            if engine is None:
                raise IntegrityViolation(
                    "constraint differential engine is not registered",
                    engine_id=engine_ref.engine_id,
                    engine_version=engine_ref.version,
                )
            if (
                engine.engine_id != engine_ref.engine_id
                or engine.engine_version != engine_ref.version
            ):
                raise IntegrityViolation(
                    "constraint differential engine identity differs from its registry key",
                    engine_id=engine_ref.engine_id,
                    engine_version=engine_ref.version,
                )
            resolved_engines.append((engine_ref, engine))
        for engine_ref, engine in resolved_engines:
            # Engines express bounded undecidable/unavailable outcomes in their
            # result contract. Exceptions are execution/integrity faults, not
            # authoritative evidence that a candidate is merely unproven.
            result = engine.evaluate(
                DifferentialEvalRequest(
                    constraints=candidate,
                    dsl_grammar_version=payload.dsl_grammar_version,
                )
            )
            status, reason = self._differential_verdict(result)
            if status == "passed":
                decided_ids = set(result.decided_constraint_ids)
                if not decided_ids:
                    status = "unproven"
                    reason = "engine_reported_no_coverage"
                elif len(decided_ids) != len(result.decided_constraint_ids):
                    status = "unproven"
                    reason = "engine_reported_invalid_coverage"
                elif not decided_ids <= candidate_ids:
                    status = "unproven"
                    reason = "engine_reported_invalid_coverage"
                else:
                    engine_key = (engine_ref.engine_id, engine_ref.version)
                    for constraint_id in decided_ids:
                        coverage_by_constraint.setdefault(constraint_id, set()).add(engine_key)
            stages_list.append(
                self._differential_stage(
                    engine_ref,
                    status,
                    reason,
                )
            )
        independently_covered_ids = {
            constraint_id
            for constraint_id, engines in coverage_by_constraint.items()
            if len(engines) >= 2
        }
        if candidate_ids - independently_covered_ids and not any(
            stage.status in {"failed", "unproven"} for stage in stages_list
        ):
            # All exact engines may have executed while still leaving some candidate
            # constraint unattested (for example, a faulty engine reports only a
            # subset of its domain). Encode that missing execution in the compile
            # evidence itself so EvidenceSet and completion see the same verdict.
            index = next(
                (i for i, stage in enumerate(stages_list) if stage.status == "passed"),
                0,
            )
            stages_list[index] = stages_list[index].model_copy(
                update={
                    "status": "unproven",
                    "reason_code": "candidate_independent_coverage_incomplete",
                }
            )
        return tuple(stages_list), independently_covered_ids

    def _differential_verdict(
        self, result: DifferentialEngineResultV1
    ) -> tuple[StageStatus, str | None]:
        # Each engine authoritatively decides its own solver domain; the caller later
        # enforces the independent two-engine quorum. Honest labeling:
        #   not_applicable (domain absent, sound non-attesting) -> not_applicable stage
        #     (does NOT block validation),
        #   undecided (in-domain but couldn't decide)           -> unproven (NEVER pass),
        #   evaluated + inconsistent                            -> failed,
        #   evaluated + consistent                              -> passed,
        #   evaluated + no verdict (defensive, fail-closed)     -> unproven.
        if result.status == "not_applicable":
            return "not_applicable", result.reason_code or "engine_domain_not_applicable"
        if result.status == "undecided":
            return "unproven", result.reason_code or "engine_could_not_decide"
        if result.consistency == "inconsistent":
            return "failed", "candidate_inconsistent"
        if result.consistency == "consistent":
            return "passed", None
        return "unproven", "engine_reported_no_verdict"

    def _golden_stage(
        self,
        payload: ConstraintValidationPayloadV1,
        candidate: tuple[Constraint, ...],
        *,
        run: bool,
    ) -> ConstraintCompileStageV1:
        if payload.golden_suite_artifact_id is None:
            return self._stage("golden", "golden", "not_applicable", "golden_suite_absent")
        if not run:
            return self._stage("golden", "golden", "unproven", _SHORT_CIRCUIT)
        result = self.golden_runner.run(
            golden_suite_artifact_id=payload.golden_suite_artifact_id, constraints=candidate
        )
        if result.status == "passed":
            return self._stage("golden", "golden", "passed", None)
        return self._stage(
            "golden",
            "golden",
            result.status,
            result.reason_code,
        )

    @staticmethod
    def _stage(
        stage_id: str,
        stage: str,
        status: StageStatus,
        reason_code: str | None,
    ) -> ConstraintCompileStageV1:
        return ConstraintCompileStageV1(
            stage_id=stage_id,
            stage=stage,  # type: ignore[arg-type]
            status=status,
            reason_code=reason_code if status != "passed" else None,
        )

    @staticmethod
    def _differential_stage(
        engine_ref: SolverEngineRefV1,
        status: StageStatus,
        reason_code: str | None,
    ) -> ConstraintCompileStageV1:
        return ConstraintCompileStageV1(
            stage_id=f"differential:{engine_ref.engine_id}@{engine_ref.version}",
            stage="differential",
            status=status,
            engine_id=engine_ref.engine_id,
            engine_version=str(engine_ref.version),
            reason_code=reason_code if status != "passed" else None,
        )

    # --------------------------------------------------------------- regression
    def _regression_phase(
        self,
        context: ExecutorContextLike,
        payload: ConstraintValidationPayloadV1,
        pipeline: _CompilePipelineV1,
        candidate_snapshot_id: str | None,
        candidate_digest: str | None,
        candidate: tuple[Constraint, ...],
        lineage: tuple[str, ...],
    ) -> tuple[
        tuple[PreparedArtifact, ...],
        tuple[DimensionResult, ...],
        tuple[RequirementDispositionV1, ...],
        tuple[tuple[str, tuple[Finding, ...]], ...],
    ]:
        if not payload.regression_suite_artifact_ids:
            return (), (), (), ()

        # Regression runs only when the compile pipeline positively validated the
        # candidate (compile passed + overall passed + the two-engine soundness
        # quorum). Missing independent coverage short-circuits regression exactly
        # like a failed prior requirement.
        run_regression = (
            pipeline.compile_passed
            and pipeline.overall_status == "passed"
            and pipeline.differential_positively_covered
        )
        if run_regression:
            if candidate_snapshot_id is None or candidate_digest is None:
                raise ValueError("passing compile pipeline omitted its semantic candidate id")
            regression_candidate = ConstraintRegressionCandidateV1(
                candidate_snapshot_id=candidate_snapshot_id,
                dsl_grammar_version=payload.dsl_grammar_version,
                constraints=candidate,
            )
            if regression_candidate.candidate_digest != candidate_digest:
                raise IntegrityViolation("constraint candidate digest changed before regression")
            return self._run_regression(
                context,
                payload,
                regression_candidate,
                lineage,
            )
        reason = (
            "candidate_unavailable" if not pipeline.compile_passed else "prior_requirement_failed"
        )
        dispositions = tuple(
            RequirementDispositionV1(
                resolved_policy_id=RESOLVED_CONSTRAINT,
                outcome_rule_id="regression",
                requirement_id=f"regression:{suite_id}",
                status="not_executed",
                reason_code=reason,
            )
            for suite_id in payload.regression_suite_artifact_ids
        )
        dimensions = tuple(
            DimensionResult(
                requirement_id=f"regression:{suite_id}",
                kind="regression",
                tool_version=REGRESSION_TOOL_VERSION,
                status="unproven",
                evidence_artifact_id=None,
                reason_code=reason,
            )
            for suite_id in payload.regression_suite_artifact_ids
        )
        return (), dimensions, dispositions, ()

    def _run_regression(
        self,
        context: ExecutorContextLike,
        payload: ConstraintValidationPayloadV1,
        candidate: ConstraintRegressionCandidateV1,
        lineage: tuple[str, ...],
    ) -> tuple[
        tuple[PreparedArtifact, ...],
        tuple[DimensionResult, ...],
        tuple[RequirementDispositionV1, ...],
        tuple[tuple[str, tuple[Finding, ...]], ...],
    ]:
        candidate_snapshot_id = candidate.candidate_snapshot_id
        artifacts: list[PreparedArtifact] = []
        dimensions: list[DimensionResult] = []
        dispositions: list[RequirementDispositionV1] = []
        finding_batches: list[tuple[str, tuple[Finding, ...]]] = []
        root_seed = context.payload.seed
        run_kind = context.run.kind
        remaining_work_units = MAX_REPAIR_REGRESSION_WORK_UNITS_V1
        candidate_digest = candidate.candidate_digest
        target_tuple = _target_version_tuple(
            context,
            target_snapshot_id=candidate_snapshot_id,
            tool_version=EVIDENCE_TOOL_VERSION,
        )
        expected_source_snapshot_id = target_tuple.ir_snapshot_id
        if not expected_source_snapshot_id:
            raise IntegrityViolation("constraint regression source IR identity is unavailable")
        for suite_id in payload.regression_suite_artifact_ids:
            execution_seed = validation_child_execution_seed(
                root_seed=root_seed,
                run_kind=run_kind,
                profile=payload.validation_policy,
                case_id=suite_id,
            )
            outcome = self.regression_runner.run(
                RegressionRunRequest(
                    suite_artifact_id=suite_id,
                    snapshot_id=candidate_snapshot_id,
                    seed=execution_seed,
                    constraint_candidate=candidate,
                    root_seed=root_seed,
                    run_kind=run_kind,
                    profile=payload.validation_policy,
                    max_action_work_units=remaining_work_units,
                )
            )
            if outcome.suite_artifact_id != suite_id:
                raise IntegrityViolation("regression runner returned another suite Artifact")
            candidate_binding = (
                outcome.constraint_candidate_snapshot_id,
                outcome.constraint_candidate_digest,
                outcome.constraint_source_snapshot_id,
            )
            has_candidate_binding = all(candidate_binding)
            if outcome.status in {"passed", "failed"} and not has_candidate_binding:
                raise IntegrityViolation(
                    "executed constraint regression omitted its exact candidate binding"
                )
            if has_candidate_binding and candidate_binding != (
                candidate_snapshot_id,
                candidate_digest,
                expected_source_snapshot_id,
            ):
                raise IntegrityViolation(
                    "regression runner returned another constraint execution binding"
                )
            if (
                outcome.payload.get("payload_schema_version") != REGRESSION_EVIDENCE_SCHEMA_ID
                or outcome.payload.get("suite_artifact_id") != suite_id
                or outcome.payload.get("seed") != execution_seed
                or outcome.payload.get("status") != outcome.status
                or outcome.payload.get("reason_code") != outcome.reason_code
            ):
                raise IntegrityViolation(
                    "regression runner wire escaped its exact execution binding"
                )
            returned_snapshot_id = outcome.payload.get("snapshot_id")
            if returned_snapshot_id != expected_source_snapshot_id and not (
                not has_candidate_binding
                and outcome.status in {"unproven", "not_executed"}
                and returned_snapshot_id is None
            ):
                raise IntegrityViolation("regression runner returned another source snapshot")
            findings_raw = outcome.payload.get("findings", ())
            if not isinstance(findings_raw, (tuple, list)):
                raise IntegrityViolation("regression runner returned invalid Finding evidence")
            suite_findings = tuple(
                rebind_finding_producers(
                    [Finding.model_validate(item) for item in findings_raw],
                    run_id=context.run.run_id,
                )
            )
            if any(
                finding.snapshot_id != expected_source_snapshot_id for finding in suite_findings
            ):
                raise IntegrityViolation("regression Finding escaped its source snapshot")
            validate_authoritative_regression_findings(
                suite_findings,
                snapshot_id=expected_source_snapshot_id,
            )
            measured_work = outcome.action_work_units
            if measured_work is not None and (
                isinstance(measured_work, bool)
                or not isinstance(measured_work, int)
                or measured_work < 0
            ):
                raise IntegrityViolation("regression runner returned invalid measured work")
            if outcome.status in {"passed", "failed"} and measured_work is None:
                raise IntegrityViolation(
                    "executed regression omitted measured action work",
                    suite_artifact_id=suite_id,
                )
            if measured_work is not None:
                if measured_work > remaining_work_units:
                    raise IntegrityViolation(
                        "constraint validation regressions exceed the aggregate work budget",
                        suite_artifact_id=suite_id,
                        remaining_work_units=remaining_work_units,
                        measured_work_units=measured_work,
                    )
                remaining_work_units -= measured_work
            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason = outcome.reason_code
            if suite_findings and status != deterministic_finding_status(suite_findings):
                raise IntegrityViolation(
                    "constraint regression status contradicts its exact Findings"
                )
            if status == "passed" and suite_findings:
                raise IntegrityViolation("passed constraint regression returned Findings")
            if status == "failed" and not suite_findings:
                raise IntegrityViolation("failed constraint regression omitted Findings")
            if status == "failed" and outcome.env_contract_version is None:
                raise IntegrityViolation(
                    "failed constraint regression omitted its environment binding"
                )
            if status == "passed" and outcome.env_contract_version is None:
                status = "unproven"
                reason = "regression_environment_binding_unavailable"
            if status == "unproven" and reason is None:
                reason = "regression_not_executed"
            regression_tuple = regression_evidence_version_tuple(
                target_tuple,
                outcome,
            )
            artifact = store_prepared_artifact(
                self.store,
                kind=REGRESSION_EVIDENCE_KIND,
                payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
                version_tuple=regression_tuple,
                lineage=(*lineage, suite_id),
                payload=with_validation_child_seed_evidence(
                    {
                        **outcome.payload,
                        "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                        "requirement_id": f"regression:{suite_id}",
                        "suite_artifact_id": suite_id,
                        "snapshot_id": expected_source_snapshot_id,
                        "seed": execution_seed,
                        "status": status,
                        "reason_code": reason if status == "unproven" else None,
                        "findings": [finding.model_dump(mode="json") for finding in suite_findings],
                    },
                    root_seed=root_seed,
                    execution_seed=execution_seed,
                    run_kind=run_kind,
                    profile=payload.validation_policy,
                    case_id=suite_id,
                ),
                extra_meta={"requirement_id": f"regression:{suite_id}"},
            )
            artifacts.append(artifact)
            finding_batches.append((suite_id, suite_findings))
            dimensions.append(
                DimensionResult(
                    requirement_id=f"regression:{suite_id}",
                    kind="regression",
                    tool_version=REGRESSION_TOOL_VERSION,
                    status=status,  # type: ignore[arg-type]
                    evidence_artifact_id=content_addressed_artifact_id(artifact),
                    reason_code=reason if status == "unproven" else None,
                )
            )
            dispositions.append(
                RequirementDispositionV1(
                    resolved_policy_id=RESOLVED_CONSTRAINT,
                    outcome_rule_id="regression",
                    requirement_id=f"regression:{suite_id}",
                    status="produced",
                )
            )
        return (
            tuple(artifacts),
            tuple(dimensions),
            tuple(dispositions),
            tuple(finding_batches),
        )

    # ---------------------------------------------------------------- artifacts
    def _seal_candidate(
        self,
        context: ExecutorContextLike,
        wire: Mapping[str, object],
        snapshot_id: str,
        lineage: tuple[str, ...],
    ) -> PreparedArtifact:
        blob = _canonical_bytes(wire)
        return store_prepared_blob(
            self.store,
            kind=CONSTRAINT_SNAPSHOT_KIND,
            payload_schema_id=CONSTRAINT_SNAPSHOT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=COMPILE_TOOL_VERSION,
                # The candidate is semantic content derived solely from proposal
                # + base constraints.  Regression's root seed must not leak into
                # its immutable VersionTuple.
                projected_fields=("doc_version", "ir_snapshot_id"),
                overrides={"constraint_snapshot_id": snapshot_id},
            ),
            lineage=lineage,
            blob=blob,
        )

    def _seal_compile_evidence(
        self,
        context: ExecutorContextLike,
        payload: ConstraintValidationPayloadV1,
        pipeline: _CompilePipelineV1,
        candidate_id: str | None,
        candidate_snapshot_id: str | None,
        lineage: tuple[str, ...],
    ) -> PreparedArtifact:
        evidence = ConstraintCompileEvidenceV1(
            proposal_artifact_id=payload.subject.subject_artifact_id,
            base_constraint_snapshot_artifact_id=payload.base_constraint_snapshot_artifact_id,
            candidate_constraint_snapshot_artifact_id=candidate_id,
            dsl_grammar_version=payload.dsl_grammar_version,
            compiler_profile=payload.compiler_profile,
            stages=pipeline.stages,
            overall_status=pipeline.overall_status,
        )
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID,
            version_tuple=_target_version_tuple(
                context,
                target_snapshot_id=candidate_snapshot_id,
                tool_version=EVIDENCE_TOOL_VERSION,
            ),
            lineage=lineage,
            payload=evidence.model_dump(mode="json"),
        )

    def _seal_evidence_set(
        self,
        context: ExecutorContextLike,
        payload: ConstraintValidationPayloadV1,
        target_binding: ConstraintTargetBindingV1 | None,
        requirements: tuple[EvidenceRequirement, ...],
        supporting: tuple[str, ...],
        lineage: tuple[str, ...],
        overall: str,
    ) -> PreparedArtifact:
        evidence_set = EvidenceSet(
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            policy_version=_profile_key(payload.validation_policy),
            validation_run_id=context.run.run_id,
            target_binding=target_binding,
            supporting_artifact_ids=supporting,
            finding_bindings=(),
            requirements=requirements,
            overall_status=overall,  # type: ignore[arg-type]
        )
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=EVIDENCE_SET_SCHEMA_ID,
            version_tuple=_target_version_tuple(
                context,
                target_snapshot_id=(
                    target_binding.target_snapshot_id if target_binding is not None else None
                ),
                tool_version=EVIDENCE_TOOL_VERSION,
            ),
            lineage=lineage,
            payload=evidence_set.model_dump(mode="json"),
        )

    # -------------------------------------------------------------------- glue
    def _compile_dimension(
        self,
        payload: ConstraintValidationPayloadV1,
        pipeline: _CompilePipelineV1,
        compile_evidence_id: str,
    ) -> DimensionResult:
        status = pipeline.overall_status
        reason = None if status == "passed" else "compile_evidence_not_passed"
        return DimensionResult(
            requirement_id="compile",
            kind=CONSTRAINT_COMPILE_REQUIREMENT_KIND,
            tool_version=_profile_key(payload.compiler_profile),
            status=status,
            evidence_artifact_id=compile_evidence_id,
            reason_code=reason if status == "unproven" else None,
        )

    def _target_binding(
        self,
        payload: ConstraintValidationPayloadV1,
        candidate_id: str | None,
        candidate_snapshot_id: str,
        candidate_digest: str,
    ) -> ConstraintTargetBindingV1 | None:
        if candidate_id is None:
            return None
        return ConstraintTargetBindingV1(
            target_artifact_id=candidate_id,
            target_snapshot_id=candidate_snapshot_id,
            target_digest=candidate_digest,
            ref_name=payload.target.ref_name,
            expected_ref=payload.target.expected_ref,
        )

    def _outcome_code(self, pipeline: _CompilePipelineV1, overall: str) -> str:
        if not pipeline.compile_passed:
            return _FAILED_WITHOUT_CANDIDATE_CODE
        if overall == "passed":
            return _VALIDATED_CODE
        return _FAILED_WITH_CANDIDATE_CODE

    def _artifact_lineage(self, payload: ConstraintValidationPayloadV1) -> tuple[str, ...]:
        # proposal + optional base_constraint run_input roles; the candidate /
        # compile-evidence / regression prepared siblings on the EvidenceSet are the
        # Task-18 publisher injection.
        lineage = [payload.subject.subject_artifact_id]
        if payload.base_constraint_snapshot_artifact_id is not None:
            lineage.append(payload.base_constraint_snapshot_artifact_id)
        return tuple(lineage)


def _profile_key(profile: ProfileRefV1) -> str:
    return f"{profile.profile_id}@{profile.version}"


def _target_version_tuple(
    context: ExecutorContextLike,
    *,
    target_snapshot_id: str | None,
    tool_version: str,
) -> VersionTuple:
    if target_snapshot_id is None:
        return prepared_version_tuple(
            context,
            tool_version=tool_version,
            projected_fields=(
                "doc_version",
                "ir_snapshot_id",
                "constraint_snapshot_id",
                "seed",
            ),
        )
    return prepared_version_tuple(
        context,
        tool_version=tool_version,
        projected_fields=("doc_version", "ir_snapshot_id", "seed"),
        overrides={"constraint_snapshot_id": target_snapshot_id},
    )


def _candidate_wire(
    dsl_grammar_version: str, candidate: tuple[Constraint, ...]
) -> dict[str, object]:
    return {
        "dsl_grammar_version": dsl_grammar_version,
        "constraints": [
            constraint.model_dump(mode="json", by_alias=True) for constraint in candidate
        ],
    }


def _canonical_bytes(wire: Mapping[str, object]) -> bytes:
    from gameforge.contracts.canonical import canonical_json

    return canonical_json(wire).encode("utf-8")


__all__ = [
    "BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1",
    "COMPILER_PROFILE_FIELD",
    "VALIDATION_POLICY_FIELD",
    "ConstraintDifferentialEngine",
    "ConstraintValidationHandler",
    "DifferentialEngineResultV1",
    "DifferentialEvalRequest",
    "GoldenSuiteResultV1",
    "GoldenSuiteRunner",
    "load_proposal",
]
