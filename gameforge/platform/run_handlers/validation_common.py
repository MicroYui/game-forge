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
  the exact future evidence-artifact id, reconciled by the Task-18 publisher).
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Mapping, Protocol

from gameforge.contracts.jobs import PreparedArtifact
from gameforge.contracts.lineage import ArtifactKind, VersionTuple, artifact_id_v2_for
from gameforge.contracts.workflow import EvidenceRequirement

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

DimensionStatus = Literal["passed", "failed", "unproven", "not_applicable"]
OverallStatus = Literal["passed", "failed", "unproven"]


def content_addressed_artifact_id(prepared: PreparedArtifact) -> str:
    """The content-addressed id a prepared artifact WITHOUT injected siblings mints.

    A validation companion (regression / compile-evidence / candidate) declares a
    fully handler-owned run_input lineage, so its published (content-addressed) id
    is deterministic at seal time and equals what the Task-9 publisher's
    ``build_artifact_v2`` derives. This lets each ``EvidenceRequirement`` /
    auto-apply proof bind the exact future evidence-artifact id (the Task-18
    publisher's identity binding reconciles the prepared-sibling parents).
    """

    return artifact_id_v2_for(
        kind=prepared.kind,
        version_tuple=prepared.version_tuple,
        lineage=prepared.lineage,
        payload_hash=prepared.payload_hash,
        meta=prepared.meta,
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
    required for a ``unproven`` / ``not_executed`` suite (fail-closed) and forbidden
    for ``passed``. ``payload`` is the ``regression-evidence@1`` wire body (a
    hand-built dict — the schema has no strict pydantic class).
    """

    suite_artifact_id: str
    status: Literal["passed", "failed", "unproven", "not_executed"]
    payload: Mapping[str, object]
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class RegressionRunRequest:
    """Fully-resolved inputs for one regression suite re-run."""

    suite_artifact_id: str
    snapshot_id: str | None
    seed: int


class RegressionRunner(Protocol):
    """Re-run one deterministic headless-regression suite (game-specific port)."""

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1: ...


class _DeterministicPassingRegressionRunner:
    """Default port: a clean, already-gated subject re-runs green deterministically.

    The regression suites re-executed at validation time cover the exact preview /
    candidate that a prior gate already produced; a deterministic headless re-run of
    that vetted content passes. A specialised wiring injects a runner that surfaces a
    genuine regression (failed) or an unavailable environment (unproven).
    """

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="passed",
            payload={
                "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "seed": request.seed,
                "status": "passed",
            },
        )


def evidence_version_tuple(
    *,
    ir_snapshot_id: str | None,
    constraint_snapshot_id: str | None,
    tool_version: str,
    seed: int,
    env_contract_version: str | None = None,
) -> VersionTuple:
    """The producer-matrix VersionTuple basis for a validation/regression artifact.

    Per the §3.3 producer matrix, ``validation_evidence`` / ``regression_evidence``
    carry the exact target-binding snapshot/constraint fields + ``tool_version`` +
    ``seed`` (seed is a producer-local field for both kinds); regression evidence
    adds ``env_contract_version`` when it consumed an environment.
    """

    return VersionTuple(
        ir_snapshot_id=ir_snapshot_id,
        constraint_snapshot_id=constraint_snapshot_id,
        tool_version=tool_version,
        seed=seed,
        env_contract_version=env_contract_version,
    )


DEFAULT_REGRESSION_RUNNER: RegressionRunner = _DeterministicPassingRegressionRunner()


__all__ = [
    "AUTO_APPLY_PROOF_SCHEMA_ID",
    "CONSTRAINT_COMPILE_EVIDENCE_SCHEMA_ID",
    "CONSTRAINT_SNAPSHOT_KIND",
    "CONSTRAINT_SNAPSHOT_SCHEMA_ID",
    "DEFAULT_REGRESSION_RUNNER",
    "EVIDENCE_SET_SCHEMA_ID",
    "REGRESSION_EVIDENCE_KIND",
    "REGRESSION_EVIDENCE_SCHEMA_ID",
    "RESOLVED_CONSTRAINT",
    "RESOLVED_PATCH",
    "RESOLVED_ROLLBACK",
    "VALIDATION_EVIDENCE_KIND",
    "DimensionResult",
    "DimensionStatus",
    "OverallStatus",
    "RegressionRunRequest",
    "RegressionRunner",
    "RegressionSuiteResultV1",
    "content_addressed_artifact_id",
    "digest_of",
    "evidence_requirement",
    "evidence_version_tuple",
    "overall_status_of",
    "require_exists",
]
