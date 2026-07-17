"""Shared support for the three M4c validation Run handlers (Task 13).

``patch_validator@1`` / ``constraint_validator@1`` / ``rollback_validator@1`` each
produce exactly ONE ``PreparedRunOutcome`` that seals an ``EvidenceSet`` primary
plus the exact companion artifacts (regression / compile-evidence / candidate /
auto-apply-proof) the frozen validation outcome policies require. The verdict is
DETERMINISTIC (checkers / simulation / compile engines) — these kinds run NO LLM.

This module holds the pieces the three handlers share:

* :data:`EVIDENCE_SET_SCHEMA_ID` etc. — the frozen payload-schema ids and the
  ``ArtifactKind`` each validation artifact seals under.
* :class:`DimensionResult` / :func:`evidence_requirement` — one
  ``EvidenceRequirement`` per validation dimension (checker / simulation /
  compile / schema / impact / regression …), fail-closed on the frozen
  ``EvidenceRequirement`` disposition rules (a missing execution is NEVER a pass).
* :func:`overall_status_of` — the SAME derivation ``EvidenceSet`` /
  ``ConstraintCompileEvidenceV1`` enforce (any failed → failed; else any unproven
  → unproven; else passed).
* :class:`RegressionRunner` / :class:`RegressionSuiteResultV1` — the injected
  regression-suite port + its per-suite result; the concrete headless-regression
  impl lives in ``apps/worker`` (the platform never hardcodes a game).
* :func:`content_addressed_artifact_id` — the content-addressed id a prepared
  artifact WITHOUT publisher-injected siblings mints (so a requirement can bind
  the exact future evidence-artifact id, reconciled by the terminal publisher).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Mapping, Protocol

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import PreparedArtifact
from gameforge.contracts.lineage import ArtifactKind, VersionTuple, artifact_id_v2_for
from gameforge.contracts.workflow import EvidenceRequirement
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import ArtifactBlobReader

EVIDENCE_SET_SCHEMA_ID = "evidence-set@1"
REGRESSION_EVIDENCE_SCHEMA_ID = "regression-evidence@1"
CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID = "constraint-compile-evidence@1"
CONSTRAINT_SNAPSHOT_SCHEMA_ID = "constraint-snapshot@1"
AUTO_APPLY_PROOF_SCHEMA_ID = "auto-apply-proof@1"

VALIDATION_EVIDENCE_KIND: ArtifactKind = "validation_evidence"
REGRESSION_EVIDENCE_KIND: ArtifactKind = "regression_evidence"
CONSTRAINT_SNAPSHOT_KIND: ArtifactKind = "constraint_snapshot"

RESOLVED_PATCH = "patch-validation"
RESOLVED_CONSTRAINT = "constraint-validation"
RESOLVED_ROLLBACK = "rollback-validation"

VALIDATION_SEED_DERIVATION_VERSION = "subseed@1"
DETERMINISTIC_VALIDATION_EXECUTION_SEED = 0
PATCH_SIMULATION_EXECUTION_MODE_V1 = "single_population@1"

DimensionStatus = Literal["passed", "failed", "unproven", "not_applicable"]
OverallStatus = Literal["passed", "failed", "unproven"]


def content_addressed_artifact_id(prepared: PreparedArtifact) -> str:
    """The content-addressed id a prepared artifact WITHOUT injected siblings mints.

    A validation companion (regression / compile-evidence / candidate) declares a
    fully handler-owned run_input lineage, so its published (content-addressed) id
    is deterministic at seal time and equals what the Task-9 publisher's
    ``build_artifact_v2`` derives. This lets each ``EvidenceRequirement`` /
    auto-apply proof bind the exact future evidence-artifact id (the terminal
    publisher's identity binding reconciles the prepared-sibling parents).
    """

    return artifact_id_v2_for(
        kind=prepared.kind,
        version_tuple=prepared.version_tuple,
        lineage=prepared.lineage,
        payload_hash=prepared.payload_hash,
        meta={**prepared.meta, "replayability": "deterministic_recompute"},
    )


def digest_of(blobs: ArtifactBlobReader, artifact_id: str) -> str:
    """Return the sha256 hex of an input artifact's exact stored bytes.

    Reading the bytes fail-closed re-verifies the input artifact EXISTS; the
    handler binds the digest so the (deferred) completion re-verification can
    cross-check the content it validated against.
    """

    return sha256(blobs.read_bytes(artifact_id)).hexdigest()


def require_exists(blobs: ArtifactBlobReader, artifact_id: str) -> None:
    """Fail-closed re-verify a bound supporting artifact still resolves to bytes."""

    blobs.read_bytes(artifact_id)


def overall_status_of(statuses: tuple[DimensionStatus, ...]) -> OverallStatus:
    """The frozen EvidenceSet/compile-evidence derivation over required statuses."""

    required = [status for status in statuses if status != "not_applicable"]
    if any(status == "failed" for status in required):
        return "failed"
    if any(status == "unproven" for status in required):
        return "unproven"
    return "passed"


def deterministic_finding_status(findings: tuple[Finding, ...]) -> OverallStatus:
    """Derive the only sound verdict from exact Finding payloads."""

    if any(
        finding.status == "confirmed" and finding.oracle_type != "llm-assisted"
        for finding in findings
    ):
        return "failed"
    if any(
        finding.status == "unproven" or finding.oracle_type == "llm-assisted"
        for finding in findings
    ):
        return "unproven"
    return "passed"


def validate_authoritative_regression_findings(
    findings: tuple[Finding, ...],
    *,
    snapshot_id: str,
) -> None:
    """Reject suggestion/advisory output at the deterministic regression port."""

    for finding in findings:
        producer = finding.producer_id.removeprefix("checker:")
        valid_authority = (
            (
                finding.source == "checker"
                and finding.oracle_type == "deterministic"
                and producer in {"graph", "asp", "smt"}
            )
            or (
                finding.source == "sim"
                and finding.oracle_type == "simulation"
                and finding.producer_id == "economy_sim"
            )
            or (
                finding.source == "playtest"
                and finding.oracle_type == "deterministic"
                and finding.producer_id == "agent-env-action-replay@1"
            )
        )
        if (
            not valid_authority
            or finding.snapshot_id != snapshot_id
            or finding.status not in {"confirmed", "unproven"}
        ):
            raise IntegrityViolation(
                "regression runner Finding escaped deterministic oracle authority",
                finding_id=finding.id,
            )


@dataclass(frozen=True, slots=True)
class DimensionResult:
    """One validation dimension's deterministic verdict + its sealed evidence.

    ``evidence_artifact_id`` is the content-addressed id of the companion artifact
    that evidences a ``passed``/``failed`` dimension (required by the frozen
    ``EvidenceRequirement`` rule); ``reason_code`` carries the fail-closed reason
    for an ``unproven`` dimension (a missing execution — timeout / skip — is
    ALWAYS unproven or failed, NEVER passed).
    """

    requirement_id: str
    kind: str
    tool_version: str
    status: DimensionStatus
    evidence_artifact_id: str | None = None
    reason_code: str | None = None


def evidence_requirement(dimension: DimensionResult) -> EvidenceRequirement:
    """Project one dimension onto a frozen ``EvidenceRequirement`` (fail-closed)."""

    applicability: Literal["required", "not_applicable"] = (
        "not_applicable" if dimension.status == "not_applicable" else "required"
    )
    return EvidenceRequirement(
        requirement_id=dimension.requirement_id,
        kind=dimension.kind,
        applicability=applicability,
        status=dimension.status,
        evidence_artifact_id=dimension.evidence_artifact_id,
        reason_code=dimension.reason_code,
        tool_version=dimension.tool_version,
    )


@dataclass(frozen=True, slots=True)
class RegressionSuiteResultV1:
    """One regression suite's deterministic re-run verdict.

    ``status`` is the deterministic headless-regression verdict; ``reason_code`` is
    required for a ``unproven`` / ``not_executed`` suite (fail-closed) and canonical
    null/omitted for ``passed`` / ``failed``. ``payload`` is the
    ``regression-evidence@1`` wire body (a
    hand-built dict — the schema has no strict pydantic class).
    """

    suite_artifact_id: str
    status: Literal["passed", "failed", "unproven", "not_executed"]
    payload: Mapping[str, object]
    reason_code: str | None = None
    # Trusted out-of-band producer fact used to seal the per-suite evidence
    # VersionTuple.  It is intentionally not copied from the Run-wide tuple:
    # one repair can execute suites bound to distinct environment contracts.
    env_contract_version: str | None = None
    # Exact adapter-versioned action work measured from the committed suite before
    # environment construction.  Repair uses this to debit its Run-wide ledger.
    action_work_units: int | None = None
    # Constraint validation uses the same public regression-evidence wire as
    # snapshot validation.  These trusted, internal-only facts prove which exact
    # constraint candidate was compiled against which retained source IR without
    # widening that frozen public wire.  The published Artifact binds the candidate
    # through its candidate prepared-parent + VersionTuple and the source through
    # the suite's unique IR parent + payload ``snapshot_id``.
    constraint_candidate_snapshot_id: str | None = None
    constraint_candidate_digest: str | None = None
    constraint_source_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        bindings = (
            self.constraint_candidate_snapshot_id,
            self.constraint_candidate_digest,
            self.constraint_source_snapshot_id,
        )
        if any(value is not None for value in bindings) and not all(
            isinstance(value, str) and value for value in bindings
        ):
            raise ValueError("constraint regression execution binding must be complete")


@dataclass(frozen=True, slots=True)
class ConstraintRegressionCandidateV1:
    """Exact ephemeral constraint candidate executed by a regression worker.

    The semantic id is derived from the complete canonical candidate wire.  A
    caller therefore cannot pair an old id with changed constraints or grammar,
    and the frozen deep copies prevent mutation after admission to the worker.
    """

    candidate_snapshot_id: str
    dsl_grammar_version: str
    constraints: tuple[Constraint, ...]

    def __post_init__(self) -> None:
        frozen = tuple(constraint.model_copy(deep=True) for constraint in self.constraints)
        object.__setattr__(self, "constraints", frozen)
        if not self.dsl_grammar_version or not frozen:
            raise ValueError("constraint regression candidate is incomplete")
        ids = tuple(constraint.id for constraint in frozen)
        if any(not constraint_id for constraint_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("constraint regression candidate ids must be non-empty and unique")
        if any(constraint.dsl_grammar_version != self.dsl_grammar_version for constraint in frozen):
            raise ValueError("constraint regression candidate grammar is not exact")
        if self.candidate_snapshot_id != f"candidate:{self.candidate_digest[:32]}":
            raise ValueError("constraint regression candidate id differs from its exact payload")

    @property
    def canonical_wire(self) -> dict[str, object]:
        return {
            "dsl_grammar_version": self.dsl_grammar_version,
            "constraints": [
                constraint.model_dump(mode="json", by_alias=True) for constraint in self.constraints
            ],
        }

    @property
    def candidate_digest(self) -> str:
        return canonical_sha256(self.canonical_wire)


def regression_evidence_version_tuple(
    base: VersionTuple,
    outcome: RegressionSuiteResultV1,
) -> VersionTuple:
    """Seal one suite result with its producer-reported environment fact.

    Terminal publication independently re-derives this field from the retained
    regression-suite Artifact.  Binding the prepared tuple to ``outcome`` permits
    distinct suites in one Run while making a missing or forged environment fail
    the terminal projection comparison.
    """

    env_contract_version = outcome.env_contract_version
    if env_contract_version is not None and (
        not isinstance(env_contract_version, str) or not env_contract_version
    ):
        raise IntegrityViolation("regression runner returned an invalid environment contract")
    return VersionTuple.model_validate(
        {
            **base.model_dump(mode="json"),
            "env_contract_version": env_contract_version,
        }
    )


@dataclass(frozen=True, slots=True)
class RegressionRunRequest:
    """Fully-resolved inputs for one regression suite re-run."""

    suite_artifact_id: str
    snapshot_id: str | None
    seed: int
    # Repair candidates are ephemeral until terminal publication.  Supplying only
    # their content id would force a production runner either to guess bytes or to
    # pretend it executed.  Validation handlers that operate on another target kind
    # may leave this absent; a real environment adapter then returns unproven.
    snapshot: Snapshot | None = None
    # Constraint validation is the other mutually-exclusive target form.  The
    # worker resolves the source IR from the already-validated suite lineage and
    # executes this exact candidate on that source; it never pretends the constraint
    # candidate id is an IR snapshot id.
    constraint_candidate: ConstraintRegressionCandidateV1 | None = None
    root_seed: int | None = None
    run_kind: RunKindRef | None = None
    profile: ProfileRefV1 | None = None
    # Remaining authority from the caller's Run-wide regression work ledger.  A
    # production runner must refuse to create an environment when this is absent
    # or smaller than the suite's exact static action work.
    max_action_work_units: int | None = None


class RegressionRunner(Protocol):
    """Re-run one deterministic headless-regression suite (game-specific port)."""

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1: ...


def derive_validation_subseed(
    *,
    root_seed: int,
    run_kind: RunKindRef,
    profile: ProfileRefV1,
    case_id: str,
    replication_index: int,
) -> int:
    """Derive one validation-child seed using the frozen ``subseed@1`` formula.

    The M4 design freezes this exact canonical input closure for every stochastic
    validation child.  In particular, this is deliberately independent of process
    hash randomisation and collection traversal order.
    """

    if not 0 <= root_seed <= (1 << 64) - 1:
        raise ValueError("root seed must be an unsigned 64-bit integer")
    if not case_id:
        raise ValueError("validation child case_id must be non-empty")
    if replication_index < 0:
        raise ValueError("validation child replication_index must be non-negative")
    digest = canonical_sha256(
        {
            "root_seed": root_seed,
            "run_kind": run_kind.model_dump(mode="json"),
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "case_id": case_id,
            "replication_index": replication_index,
        }
    )
    return int(digest[:16], 16)


def validation_child_execution_seed(
    *,
    root_seed: int | None,
    run_kind: RunKindRef,
    profile: ProfileRefV1,
    case_id: str,
    replication_index: int = 0,
) -> int:
    """Return a child subseed, keeping the deterministic no-root fallback internal."""

    if root_seed is None:
        return DETERMINISTIC_VALIDATION_EXECUTION_SEED
    return derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
        replication_index=replication_index,
    )


def regression_suite_execution_coverage_binding(
    *,
    suite_artifact_id: str,
    validation_profile: ProfileRefV1,
    constraint_snapshot_artifact_id: str | None,
    env_contract_version: str,
    root_seed: int,
    run_kind: RunKindRef,
    execution_seed: int,
) -> dict[str, object]:
    """Fingerprint suite execution authority without binding the target snapshot.

    The immutable suite Artifact closes the adapter/environment-profile payload;
    the remaining fields close environment contract, constraint context and exact
    seed derivation.  Excluding the candidate snapshot is intentional: this marker
    proves the same oracle was rerun on a new target, not that the result stayed the
    same.
    """

    expected_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=validation_profile,
        case_id=suite_artifact_id,
        replication_index=0,
    )
    if execution_seed != expected_seed:
        raise IntegrityViolation("regression suite coverage seed binding is invalid")
    return {
        "binding_schema_version": "regression-suite-expected-finding-binding@1",
        "suite_artifact_id": suite_artifact_id,
        "validation_profile": validation_profile.model_dump(mode="json"),
        **(
            {}
            if constraint_snapshot_artifact_id is None
            else {"constraint_snapshot_artifact_id": constraint_snapshot_artifact_id}
        ),
        "env_contract_version": env_contract_version,
        "root_seed": root_seed,
        "run_kind": run_kind.model_dump(mode="json"),
        "case_id": suite_artifact_id,
        "replication_index": 0,
        "execution_seed": execution_seed,
        "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
    }


def regression_suite_execution_coverage_marker(
    binding: Mapping[str, object],
) -> str:
    return "regression-suite-binding:" + canonical_sha256(binding)


def validation_child_seed_evidence(
    *,
    root_seed: int | None,
    execution_seed: int | None,
    run_kind: RunKindRef | None,
    profile: ProfileRefV1 | None,
    case_id: str,
    replication_index: int = 0,
) -> dict[str, object]:
    """Project a stochastic child's complete ``subseed@1`` binding into evidence."""

    if root_seed is None or execution_seed is None or run_kind is None or profile is None:
        return {}
    return {
        "root_seed": root_seed,
        "run_kind": run_kind.model_dump(mode="json"),
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "case_id": case_id,
        "replication_index": replication_index,
        "seed": execution_seed,
        "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
    }


def with_validation_child_seed_evidence(
    payload: Mapping[str, object],
    *,
    root_seed: int | None,
    execution_seed: int,
    run_kind: RunKindRef,
    profile: ProfileRefV1,
    case_id: str,
    replication_index: int = 0,
) -> dict[str, object]:
    """Attach a stochastic child binding without inventing one for deterministic work."""

    return {
        **dict(payload),
        **validation_child_seed_evidence(
            root_seed=root_seed,
            execution_seed=execution_seed,
            run_kind=run_kind,
            profile=profile,
            case_id=case_id,
            replication_index=replication_index,
        ),
    }


class _UnavailableRegressionRunner:
    """Fail-closed default used only when no executable regression port is injected."""

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="unproven",
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "seed": request.seed,
                "status": "unproven",
                "reason_code": "regression_runner_unavailable",
            },
            reason_code="regression_runner_unavailable",
        )


def evidence_version_tuple(
    *,
    ir_snapshot_id: str | None,
    constraint_snapshot_id: str | None,
    tool_version: str,
    seed: int | None,
    env_contract_version: str | None = None,
    doc_version: str | None = None,
) -> VersionTuple:
    """The producer-matrix VersionTuple basis for a validation/regression artifact.

    Per the §3.3 producer matrix, ``validation_evidence`` / ``regression_evidence``
    carry the exact target-binding snapshot/constraint fields + ``tool_version``.
    ``seed`` is the Run's exact frozen root seed when stochastic profiles apply and
    is ``None`` when seed is not applicable; an internal deterministic fallback or
    a derived child subseed must never be substituted into this root lineage field.
    Regression evidence adds ``env_contract_version`` when it consumed an environment.
    """

    return VersionTuple(
        doc_version=doc_version,
        ir_snapshot_id=ir_snapshot_id,
        constraint_snapshot_id=constraint_snapshot_id,
        tool_version=tool_version,
        seed=seed,
        env_contract_version=env_contract_version,
    )


DEFAULT_REGRESSION_RUNNER: RegressionRunner = _UnavailableRegressionRunner()


__all__ = [
    "AUTO_APPLY_PROOF_SCHEMA_ID",
    "CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID",
    "CONSTRAINT_SNAPSHOT_KIND",
    "CONSTRAINT_SNAPSHOT_SCHEMA_ID",
    "DEFAULT_REGRESSION_RUNNER",
    "DETERMINISTIC_VALIDATION_EXECUTION_SEED",
    "EVIDENCE_SET_SCHEMA_ID",
    "PATCH_SIMULATION_EXECUTION_MODE_V1",
    "REGRESSION_EVIDENCE_KIND",
    "REGRESSION_EVIDENCE_SCHEMA_ID",
    "RESOLVED_CONSTRAINT",
    "RESOLVED_PATCH",
    "RESOLVED_ROLLBACK",
    "VALIDATION_SEED_DERIVATION_VERSION",
    "VALIDATION_EVIDENCE_KIND",
    "ConstraintRegressionCandidateV1",
    "DimensionResult",
    "DimensionStatus",
    "OverallStatus",
    "RegressionRunRequest",
    "RegressionRunner",
    "RegressionSuiteResultV1",
    "content_addressed_artifact_id",
    "derive_validation_subseed",
    "deterministic_finding_status",
    "digest_of",
    "evidence_requirement",
    "evidence_version_tuple",
    "overall_status_of",
    "regression_suite_execution_coverage_binding",
    "regression_suite_execution_coverage_marker",
    "require_exists",
    "validation_child_execution_seed",
    "validation_child_seed_evidence",
    "validate_authoritative_regression_findings",
    "with_validation_child_seed_evidence",
]
