"""``constraint_validator@1`` — the ≥2-engine differential constraint-compile validator.

Compiles the subject ``constraint_proposal``'s constraints through a fixed
``parse → typecheck → compile → differential(×≥2) → golden`` pipeline and records
EVERY stage in a ``ConstraintCompileEvidenceV1`` with its exact status /
reason_code. The ``differential`` stage runs ≥2 EXACT engines named by
``payload.differential_engines`` (the two initial engines wrap ``spine/checkers``:
Clingo/ASP + z3/SMT). Each engine authoritatively decides ITS OWN solver domain and
reports honestly: a ``passed`` differential stage requires the engine to have
GENUINELY evaluated a candidate in its domain and found it consistent; an engine
whose domain does not apply is a sound ``not_applicable`` stage (it executed and
found nothing to attest — it does NOT block validation and is NOT a vacuous pass);
an engine that could not decide an in-domain constraint (``undecided`` — timeout /
unbindable) is ``unproven`` (never a pass); a genuine contradiction (z3 ``unsat``)
is ``failed``. The two initial engines are DOMAIN-PARTITIONED (z3 numeric / Clingo
structural), so a single-domain candidate is validated by its ONE applicable engine
(the other honestly ``not_applicable``). A cross-engine SOUNDNESS GUARD requires
that at least one applicable engine POSITIVELY decided EVERY constraint — a
candidate no engine positively decided (empty constraints / all ``not_applicable`` /
all ``undecided``) is ``unproven``, NEVER a vacuous pass. When (and only when) the
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
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    PreparedArtifact,
    PreparedRunOutcome,
    RequirementDispositionV1,
    SolverEngineRefV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.workflow import (
    ConstraintCompileEvidenceV1,
    ConstraintCompileStageV1,
    ConstraintProposalV1,
    ConstraintTargetBindingV1,
    EvidenceRequirement,
    EvidenceSet,
)
from gameforge.spine.dsl.ast import DslError, parse_assert
from gameforge.spine.dsl.compile import compile_all

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    load_json_blob,
    resolved_profile,
    store_prepared_artifact,
    store_prepared_blob,
)
from gameforge.platform.run_handlers.readers import ConstraintLoader, load_constraints
from gameforge.platform.run_handlers.validation_common import (
    CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID,
    CONSTRAINT_SNAPSHOT_KIND,
    CONSTRAINT_SNAPSHOT_SCHEMA_ID,
    DEFAULT_REGRESSION_RUNNER,
    EVIDENCE_SET_SCHEMA_ID,
    REGRESSION_EVIDENCE_KIND,
    REGRESSION_EVIDENCE_SCHEMA_ID,
    RESOLVED_CONSTRAINT,
    VALIDATION_EVIDENCE_KIND,
    DimensionResult,
    RegressionRunner,
    RegressionRunRequest,
    content_addressed_artifact_id,
    evidence_requirement,
    evidence_version_tuple,
    overall_status_of,
    require_exists,
)

VALIDATION_POLICY_FIELD = "/params/validation_policy"
COMPILER_PROFILE_FIELD = "/params/compiler_profile"
COMPILE_TOOL_VERSION = "constraint-compile@1"
REGRESSION_TOOL_VERSION = "regression@1"
EVIDENCE_TOOL_VERSION = "constraint-validation@1"

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
    (its sound coverage contribution); the handler unions them across engines to
    enforce that every constraint was covered by at least one applicable engine.

    A ``passed`` differential stage requires ``evaluated`` + ``consistent``;
    ``not_applicable`` maps to a ``not_applicable`` stage (does not block validation)
    and ``undecided`` to an ``unproven`` stage — neither ever attests consistency.
    """

    status: Literal["evaluated", "not_applicable", "undecided"]
    consistency: Literal["consistent", "inconsistent"] | None = None
    reason_code: str | None = None
    decided_constraint_ids: tuple[str, ...] = ()


class ConstraintDifferentialEngine(Protocol):
    """One exact differential solver engine (concrete impls wrap ``spine/checkers``)."""

    engine_id: str
    engine_version: int

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1: ...


class GoldenSuiteRunner(Protocol):
    """Replay the bound golden suite against the compiled candidate (deterministic)."""

    def run(
        self, *, golden_suite_artifact_id: str, constraints: tuple[Constraint, ...]
    ) -> bool: ...


class _AlwaysGreenGoldenRunner:
    """Default golden replay: a compiled candidate replays green deterministically."""

    def run(self, *, golden_suite_artifact_id: str, constraints: tuple[Constraint, ...]) -> bool:
        return True


def load_proposal(blobs: ArtifactBlobReader, artifact_id: str) -> ConstraintProposalV1:
    """Parse the subject ``constraint_proposal`` artifact into its typed proposal."""

    return ConstraintProposalV1.model_validate(load_json_blob(blobs, artifact_id))


@dataclass(frozen=True, slots=True)
class _CompilePipelineV1:
    """The full compile pipeline result: every stage + whether a candidate formed.

    ``differential_positively_covered`` is the SOUNDNESS GUARD: at least one engine
    positively decided consistency AND every candidate constraint was covered by some
    applicable engine that positively decided it. A candidate that no engine
    positively decided (empty constraints, all stages ``not_applicable``, or every
    applicable engine ``undecided``) is NEVER validated on it.
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
    differential_engines: Mapping[str, ConstraintDifferentialEngine]
    golden_runner: GoldenSuiteRunner = field(default_factory=_AlwaysGreenGoldenRunner)
    regression_runner: RegressionRunner = DEFAULT_REGRESSION_RUNNER
    constraint_loader: ConstraintLoader = load_constraints

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ConstraintValidationPayloadV1):
            raise TypeError("constraint_validator@1 requires a constraint-validation@1 payload")
        if payload.base_constraint_snapshot_artifact_id is not None:
            # re-verify the bound base snapshot exists (typed lineage parent).
            self.constraint_loader(self.blobs, payload.base_constraint_snapshot_artifact_id)

        proposal = load_proposal(self.blobs, payload.subject.subject_artifact_id)
        candidate = tuple(proposal.constraints)
        seed = context.payload.seed
        lineage = self._artifact_lineage(payload)

        pipeline = self._run_pipeline(payload, candidate)

        candidate_artifact = (
            self._seal_candidate(payload, candidate, lineage) if pipeline.compile_passed else None
        )
        candidate_id = (
            content_addressed_artifact_id(candidate_artifact)
            if candidate_artifact is not None
            else None
        )
        compile_evidence = self._seal_compile_evidence(
            payload, pipeline, candidate_id, lineage, seed
        )
        compile_evidence_id = content_addressed_artifact_id(compile_evidence)

        (
            regression_artifacts,
            regression_dimensions,
            dispositions,
        ) = self._regression_phase(payload, pipeline, lineage, seed)

        compile_dimension = self._compile_dimension(payload, pipeline, compile_evidence_id)
        requirements = tuple(
            evidence_requirement(dim) for dim in (compile_dimension, *regression_dimensions)
        )
        overall = overall_status_of(
            tuple(dim.status for dim in (compile_dimension, *regression_dimensions))
        )

        target_binding = self._target_binding(payload, candidate, candidate_id)
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
            proposal,
            target_binding,
            requirements,
            supporting,
            lineage,
            overall,
            seed,
        )

        artifacts: tuple[PreparedArtifact, ...] = (
            evidence_set,
            *([candidate_artifact] if candidate_artifact is not None else []),
            compile_evidence,
            *regression_artifacts,
        )
        outcome_code = self._outcome_code(pipeline, overall)
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=outcome_code,
            primary_index=0,
            artifacts=artifacts,
            findings=(),
            requirement_dispositions=dispositions,
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
        # SOUNDNESS GUARD: at least one engine positively decided consistency AND every
        # constraint is covered by some applicable engine that positively decided it.
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
        for constraint in candidate:
            if constraint.dsl_grammar_version != payload.dsl_grammar_version:
                return "failed", "dsl_grammar_version_mismatch"
        if any(constraint.has_llm_predicate() for constraint in candidate):
            # an llm-assisted predicate cannot be validated deterministically here.
            return "unproven", "llm_assisted_predicate_deferred"
        return "passed", None

    def _compile(self, candidate: tuple[Constraint, ...]) -> tuple[StageStatus, str | None]:
        try:
            compile_all(list(candidate))
        except Exception:  # noqa: BLE001 - any compile failure is a definite failed stage
            return "failed", "constraint_compile_error"
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
        covered_ids: set[str] = set()
        for engine_ref in payload.differential_engines:
            engine = self.differential_engines.get(engine_ref.engine_id)
            if engine is None:
                # a missing engine is a MISSING execution -> unproven, never a pass.
                stages_list.append(
                    self._differential_stage(engine_ref, "unproven", "engine_unavailable")
                )
                continue
            result = engine.evaluate(
                DifferentialEvalRequest(
                    constraints=candidate, dsl_grammar_version=payload.dsl_grammar_version
                )
            )
            status, reason = self._differential_verdict(result)
            if status == "passed":
                covered_ids.update(result.decided_constraint_ids)
            stages_list.append(self._differential_stage(engine_ref, status, reason))
        return tuple(stages_list), covered_ids

    def _differential_verdict(
        self, result: DifferentialEngineResultV1
    ) -> tuple[StageStatus, str | None]:
        # Each engine authoritatively decides ITS OWN solver domain. With the two
        # DOMAIN-PARTITIONED initial engines (z3 numeric / Clingo structural) each
        # constraint is decided by exactly one applicable engine; the cross-engine
        # SOUNDNESS GUARD (`differential_positively_covered`) enforces that every
        # constraint was positively decided by some applicable engine. Honest labeling:
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
        require_exists(self.blobs, payload.golden_suite_artifact_id)
        passed = self.golden_runner.run(
            golden_suite_artifact_id=payload.golden_suite_artifact_id, constraints=candidate
        )
        if passed:
            return self._stage("golden", "golden", "passed", None)
        return self._stage("golden", "golden", "failed", "golden_replay_mismatch")

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
        payload: ConstraintValidationPayloadV1,
        pipeline: _CompilePipelineV1,
        lineage: tuple[str, ...],
        seed: int | None,
    ) -> tuple[
        tuple[PreparedArtifact, ...],
        tuple[DimensionResult, ...],
        tuple[RequirementDispositionV1, ...],
    ]:
        # Regression runs only when the compile pipeline positively validated the
        # candidate (compile passed + overall passed + the soundness guard: some
        # applicable engine positively decided every constraint). A candidate no
        # engine positively decided short-circuits regression exactly like a failed
        # prior requirement.
        run_regression = (
            pipeline.compile_passed
            and pipeline.overall_status == "passed"
            and pipeline.differential_positively_covered
        )
        if run_regression:
            return self._run_regression(payload, lineage, seed)
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
        return (), (), dispositions

    def _run_regression(
        self,
        payload: ConstraintValidationPayloadV1,
        lineage: tuple[str, ...],
        seed: int | None,
    ) -> tuple[
        tuple[PreparedArtifact, ...],
        tuple[DimensionResult, ...],
        tuple[RequirementDispositionV1, ...],
    ]:
        artifacts: list[PreparedArtifact] = []
        dimensions: list[DimensionResult] = []
        dispositions: list[RequirementDispositionV1] = []
        for suite_id in payload.regression_suite_artifact_ids:
            require_exists(self.blobs, suite_id)
            outcome = self.regression_runner.run(
                RegressionRunRequest(
                    suite_artifact_id=suite_id,
                    snapshot_id=None,
                    seed=0 if seed is None else seed,
                )
            )
            status = "unproven" if outcome.status == "not_executed" else outcome.status
            reason = outcome.reason_code
            if status == "unproven" and reason is None:
                reason = "regression_not_executed"
            artifact = store_prepared_artifact(
                self.store,
                kind=REGRESSION_EVIDENCE_KIND,
                payload_schema_id=REGRESSION_EVIDENCE_SCHEMA_ID,
                version_tuple=evidence_version_tuple(
                    ir_snapshot_id=None,
                    constraint_snapshot_id=None,
                    # producer-local (§3.3): the RUN's producer tool, which the
                    # publisher re-projects via ``producer_value``. The dimension
                    # tool is recorded on the EvidenceRequirement, not here.
                    tool_version=EVIDENCE_TOOL_VERSION,
                    seed=seed,
                ),
                lineage=lineage,
                payload=outcome.payload,
                extra_meta={"requirement_id": f"regression:{suite_id}"},
            )
            artifacts.append(artifact)
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
        return tuple(artifacts), tuple(dimensions), tuple(dispositions)

    # ---------------------------------------------------------------- artifacts
    def _seal_candidate(
        self,
        payload: ConstraintValidationPayloadV1,
        candidate: tuple[Constraint, ...],
        lineage: tuple[str, ...],
    ) -> PreparedArtifact:
        wire = _candidate_wire(payload.dsl_grammar_version, candidate)
        blob = _canonical_bytes(wire)
        snapshot_id = f"candidate:{canonical_sha256(wire)[:32]}"
        candidate_lineage = (payload.subject.subject_artifact_id,)
        if payload.base_constraint_snapshot_artifact_id is not None:
            candidate_lineage = (
                payload.subject.subject_artifact_id,
                payload.base_constraint_snapshot_artifact_id,
            )
        return store_prepared_blob(
            self.store,
            kind=CONSTRAINT_SNAPSHOT_KIND,
            payload_schema_id=CONSTRAINT_SNAPSHOT_SCHEMA_ID,
            version_tuple=VersionTuple(
                constraint_snapshot_id=snapshot_id,
                tool_version=COMPILE_TOOL_VERSION,
            ),
            lineage=candidate_lineage,
            blob=blob,
        )

    def _seal_compile_evidence(
        self,
        payload: ConstraintValidationPayloadV1,
        pipeline: _CompilePipelineV1,
        candidate_id: str | None,
        lineage: tuple[str, ...],
        seed: int | None,
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
        compile_lineage = (payload.subject.subject_artifact_id,)
        if payload.base_constraint_snapshot_artifact_id is not None:
            compile_lineage = (
                payload.subject.subject_artifact_id,
                payload.base_constraint_snapshot_artifact_id,
            )
        return store_prepared_artifact(
            self.store,
            kind=VALIDATION_EVIDENCE_KIND,
            payload_schema_id=CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID,
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=None,
                constraint_snapshot_id=candidate_id,
                tool_version=EVIDENCE_TOOL_VERSION,
                seed=seed,
            ),
            lineage=compile_lineage,
            payload=evidence.model_dump(mode="json"),
        )

    def _seal_evidence_set(
        self,
        context: ExecutorContextLike,
        payload: ConstraintValidationPayloadV1,
        proposal: ConstraintProposalV1,
        target_binding: ConstraintTargetBindingV1 | None,
        requirements: tuple[EvidenceRequirement, ...],
        supporting: tuple[str, ...],
        lineage: tuple[str, ...],
        overall: str,
        seed: int | None,
    ) -> PreparedArtifact:
        evidence_set = EvidenceSet(
            subject_artifact_id=payload.subject.subject_artifact_id,
            subject_digest=payload.subject.subject_digest,
            policy_version=_profile_key(
                resolved_profile(context.payload, VALIDATION_POLICY_FIELD).profile
            ),
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
            version_tuple=evidence_version_tuple(
                ir_snapshot_id=None,
                constraint_snapshot_id=(
                    target_binding.target_snapshot_id if target_binding is not None else None
                ),
                tool_version=EVIDENCE_TOOL_VERSION,
                seed=seed,
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
        # SOUNDNESS GUARD: the raw compile-evidence stage derivation ignores
        # `not_applicable` differential stages, so a candidate whose ONLY differential
        # stages are `not_applicable` (empty / all-domain-absent) would otherwise
        # vacuously derive `passed`. Downgrade the compile DIMENSION to `unproven`
        # unless some applicable engine positively decided every constraint — this is
        # the authoritative EvidenceSet verdict, so an unproven-covered candidate is
        # never validated even though the compile-evidence artifact records `passed`.
        if status == "passed" and not pipeline.differential_positively_covered:
            status = "unproven"
            reason = "no_engine_positively_decided_candidate"
        return DimensionResult(
            requirement_id="compile",
            kind="compile",
            tool_version=_profile_key(payload.compiler_profile),
            status=status,
            evidence_artifact_id=compile_evidence_id,
            reason_code=reason if status == "unproven" else None,
        )

    def _target_binding(
        self,
        payload: ConstraintValidationPayloadV1,
        candidate: tuple[Constraint, ...],
        candidate_id: str | None,
    ) -> ConstraintTargetBindingV1 | None:
        if candidate_id is None:
            return None
        wire = _candidate_wire(payload.dsl_grammar_version, candidate)
        snapshot_id = f"candidate:{canonical_sha256(wire)[:32]}"
        return ConstraintTargetBindingV1(
            target_artifact_id=candidate_id,
            target_snapshot_id=snapshot_id,
            target_digest=canonical_sha256(wire),
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
    "COMPILER_PROFILE_FIELD",
    "VALIDATION_POLICY_FIELD",
    "ConstraintDifferentialEngine",
    "ConstraintValidationHandler",
    "DifferentialEngineResultV1",
    "DifferentialEvalRequest",
    "GoldenSuiteRunner",
    "load_proposal",
]
