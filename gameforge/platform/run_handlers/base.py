"""Shared support for the M4c Run handlers (Task 11).

Every stage-specific handler under this package is a ``RunExecutor`` (Task 10
seam): it receives a fully-resolved executor context and returns exactly one
sealed ``PreparedRunOutcome``. A handler NEVER touches persistence, cost,
publication, refs, or audit — it composes deterministic spine work (and, for the
composite review/bench handlers, bounded LLM *suggestions* routed through the
injected model bridge) into a ``PreparedRunResult`` that the Task-9 terminal
publisher will later admit against the frozen outcome policy.

This module holds the pieces every handler shares:

* :class:`ExecutorContextLike` / :class:`ModelBridgePort` — structural views of
  the Task-10 ``ExecutorContext`` / ``WorkerModelBridgePort`` so ``platform``
  never imports ``gameforge.apps`` (dependency lint: platform → contracts /
  spine / runtime only).
* :class:`ArtifactBlobReader` / :class:`PreparedArtifactStore` — the injected
  read/write object-store ports; the concrete backends are composed by the
  worker/composition root exactly as the terminal publisher's ``_Blobs`` double.
* :func:`store_prepared_artifact` — canonical-serialize a domain payload and
  seal it into a :class:`PreparedArtifact` (``object_ref.key == location.key``).
* :func:`finding_to_payload` — map a spine ``Finding`` field-by-field onto the
  immutable ``FindingPayloadV1`` with ``producer_run_id`` re-bound to the Run.
* :func:`build_success_result` — assemble the ``PreparedRunResult`` + summary so
  the count/primary invariants are computed once, in one place.
* :func:`resolved_profile` — resolve one ``ResolvedExecutionProfileBindingV1`` by
  its exact ``field_path`` (fail-closed on a missing required binding).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import ResolvedExecutionProfileBindingV1
from gameforge.contracts.findings import Finding, FindingPayloadV1
from gameforge.contracts.jobs import (
    PreparedArtifact,
    PreparedFindingV1,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RequirementDispositionV1,
    RunAttempt,
    RunPayloadEnvelope,
    RunRecord,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
)
from gameforge.contracts.model_router import ModelSnapshot


class ModelBridgePort(Protocol):
    """Structural view of the Task-10 ``WorkerModelBridgePort``.

    The bridge takes one rendered model call and returns one result; both are
    treated as opaque objects here so ``platform`` need not import the concrete
    ``gameforge.apps.worker.model_bridge`` types (dependency direction).
    """

    def call_model(self, request: object) -> object: ...

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot: ...


@runtime_checkable
class ExecutorContextLike(Protocol):
    """Structural view of the Task-10 ``ExecutorContext`` (no ``apps`` import)."""

    run: RunRecord
    attempt: RunAttempt
    payload: RunPayloadEnvelope
    deadline_utc: datetime
    model_bridge: ModelBridgePort


class ArtifactBlobReader(Protocol):
    """Read the exact stored bytes of an already-committed input Artifact."""

    def read_bytes(self, artifact_id: str) -> bytes: ...


class PreparedArtifactStore(Protocol):
    """Content-address a prepared domain payload into the object store.

    Returns the verified ``ObjectRef`` and the co-located ``ObjectLocation`` (same
    key) so the sealed :class:`PreparedArtifact` satisfies its object binding.
    """

    def put_prepared(self, payload: bytes) -> tuple[ObjectRef, ObjectLocation]: ...


def canonical_payload_bytes(payload: Mapping[str, object]) -> bytes:
    """Deterministic UTF-8 canonical JSON bytes for a domain-artifact payload."""

    return canonical_json(payload).encode("utf-8")


def store_prepared_artifact(
    store: PreparedArtifactStore,
    *,
    kind: ArtifactKind,
    payload_schema_id: str,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...],
    payload: Mapping[str, object],
    extra_meta: Mapping[str, object] | None = None,
) -> PreparedArtifact:
    """Seal a domain payload into a :class:`PreparedArtifact`.

    The payload is canonical-serialized once; ``meta`` always carries the exact
    ``payload_schema_id`` (the publisher re-verifies ``meta`` matches the field).
    """

    return store_prepared_blob(
        store,
        kind=kind,
        payload_schema_id=payload_schema_id,
        version_tuple=version_tuple,
        lineage=lineage,
        blob=canonical_payload_bytes(payload),
        extra_meta=extra_meta,
    )


def store_prepared_blob(
    store: PreparedArtifactStore,
    *,
    kind: ArtifactKind,
    payload_schema_id: str,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...],
    blob: bytes,
    extra_meta: Mapping[str, object] | None = None,
) -> PreparedArtifact:
    """Seal already-serialized payload bytes into a :class:`PreparedArtifact`."""

    object_ref, location = store.put_prepared(blob)
    meta: dict[str, object] = {"payload_schema_id": payload_schema_id}
    if extra_meta:
        for key, value in extra_meta.items():
            if key == "payload_schema_id":
                raise ValueError("extra_meta cannot override payload_schema_id")
            meta[key] = value
    return PreparedArtifact(
        kind=kind,
        payload_schema_id=payload_schema_id,
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=object_ref.sha256,
        meta=meta,
        object_ref=object_ref,
        location=location,
    )


def finding_to_payload(finding: Finding, *, producer_run_id: str) -> FindingPayloadV1:
    """Map a spine ``Finding`` field-by-field onto the immutable payload.

    ``producer_run_id`` is re-bound to the current Run (a spine ``Finding`` carries
    its own producer-local run id like ``graph@<snapshot>``; the sealed payload's
    ``producer_run_id`` MUST equal the platform Run so the publisher's finding CAS
    accepts it). Every other semantic field is copied verbatim.
    """

    return FindingPayloadV1(
        source=finding.source,
        producer_id=finding.producer_id,
        producer_run_id=producer_run_id,
        oracle_type=finding.oracle_type,
        defect_class=finding.defect_class,
        severity=finding.severity,
        snapshot_id=finding.snapshot_id,
        entities=list(finding.entities),
        relations=list(finding.relations),
        constraint_id=finding.constraint_id,
        evidence=dict(finding.evidence),
        minimal_repro=dict(finding.minimal_repro),
        status=finding.status,
        confidence=finding.confidence,
        message=finding.message,
    )


@dataclass(frozen=True, slots=True)
class FindingEvidence:
    """One spine finding paired with the prepared-artifact index that evidences it.

    ``finding_id`` overrides the projected finding-series id; leave it ``None`` to
    use the spine ``Finding.id``. A composite handler that runs the SAME oracle
    under several distinct profiles MUST scope the id by profile so two profiles
    resolving to one oracle id do not collide on a single series head (the
    finding-series CAS would otherwise reject the duplicate at publish).
    """

    finding: Finding
    evidence_artifact_index: int
    finding_id: str | None = None


def build_prepared_findings(
    evidence: tuple[FindingEvidence, ...],
    *,
    run_id: str,
) -> tuple[PreparedFindingV1, ...]:
    """Project spine findings onto ``PreparedFindingV1`` (fresh series heads)."""

    prepared: list[PreparedFindingV1] = []
    for item in evidence:
        prepared.append(
            PreparedFindingV1(
                finding_id=item.finding_id or item.finding.id,
                expected_previous_revision=None,
                evidence_artifact_index=item.evidence_artifact_index,
                payload=finding_to_payload(item.finding, producer_run_id=run_id),
            )
        )
    return tuple(prepared)


def build_success_result(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    outcome_code: str,
    primary_index: int,
    artifacts: tuple[PreparedArtifact, ...],
    findings: tuple[PreparedFindingV1, ...] = (),
    requirement_dispositions: tuple[RequirementDispositionV1, ...] = (),
) -> PreparedRunResult:
    """Assemble the sealed success outcome with a self-consistent summary.

    ``requirement_dispositions`` carry the full produced/not_executed manifest a
    ``subset(...)`` count binding reconciles against (used by the validation
    handlers whose failed-outcome policies bind regression by allowed
    not-executed reason codes); it defaults to empty for the plain policies.
    """

    if not artifacts:
        raise ValueError("a success outcome requires at least one prepared artifact")
    if primary_index >= len(artifacts):
        raise ValueError("primary_index is outside the prepared artifacts")
    summary = PreparedRunResultSummaryV1(
        outcome_code=outcome_code,
        primary_artifact_kind=artifacts[primary_index].kind,
        prepared_domain_artifact_count=len(artifacts),
        prepared_finding_count=len(findings),
    )
    return PreparedRunResult(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        primary_index=primary_index,
        artifacts=artifacts,
        findings=findings,
        requirement_dispositions=requirement_dispositions,
        summary=summary,
    )


def resolved_profile(
    payload: RunPayloadEnvelope,
    field_path: str,
    *,
    required: bool = True,
) -> ResolvedExecutionProfileBindingV1 | None:
    """Resolve the single profile binding at ``field_path`` (fail-closed)."""

    for binding in payload.resolved_profiles:
        if binding.field_path == field_path:
            return binding
    if required:
        raise ValueError(f"required profile binding {field_path!r} is not resolved")
    return None


def load_json_blob(reader: ArtifactBlobReader, artifact_id: str) -> object:
    """Read + JSON-decode an input artifact blob (fail-closed on malformed bytes)."""

    raw = reader.read_bytes(artifact_id)
    return json.loads(raw)


__all__ = [
    "ArtifactBlobReader",
    "ExecutorContextLike",
    "FindingEvidence",
    "ModelBridgePort",
    "PreparedArtifactStore",
    "build_prepared_findings",
    "build_success_result",
    "canonical_payload_bytes",
    "finding_to_payload",
    "load_json_blob",
    "resolved_profile",
    "store_prepared_artifact",
    "store_prepared_blob",
]
