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
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Mapping, Protocol, runtime_checkable

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileKindV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding, FindingPayloadV1
from gameforge.contracts.jobs import (
    MAX_PREPARED_DOMAIN_ARTIFACTS,
    MAX_PREPARED_ARTIFACT_BYTES,
    MAX_PREPARED_FINDINGS,
    MAX_PREPARED_OUTCOME_BYTES,
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
    object_ref_for_bytes,
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


LlmExecutionMode = Literal["not_applicable", "live", "record", "replay"]


class ExactProfileBindingValidator(Protocol):
    """Production seam that re-resolves one frozen binding and its lifecycle."""

    def __call__(
        self,
        binding: ResolvedExecutionProfileBindingV1,
        *,
        llm_execution_mode: LlmExecutionMode,
        run_kind: RunKindRef,
    ) -> None: ...


def trust_typed_profile_binding(
    _binding: ResolvedExecutionProfileBindingV1,
    *,
    llm_execution_mode: LlmExecutionMode,
    run_kind: RunKindRef,
) -> None:
    """Test/default validator; production composition injects retained authority."""

    del llm_execution_mode, run_kind


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


class PreparedArtifactBatchStore:
    """Stage an entire handler outcome in memory, then commit after preflight."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_PREPARED_OUTCOME_BYTES,
        max_artifacts: int = MAX_PREPARED_DOMAIN_ARTIFACTS,
    ) -> None:
        if not 1 <= max_bytes <= MAX_PREPARED_OUTCOME_BYTES:
            raise IntegrityViolation("prepared batch byte authority is outside the hard bound")
        if not 1 <= max_artifacts <= MAX_PREPARED_DOMAIN_ARTIFACTS:
            raise IntegrityViolation("prepared batch artifact authority is outside the hard bound")
        self._max_bytes = max_bytes
        self._max_artifacts = max_artifacts
        self._staged_bytes = 0
        self._blobs: dict[str, bytes] = {}
        self._staged_bindings: list[tuple[ObjectRef, ObjectLocation]] = []

    @property
    def staged_bytes(self) -> int:
        return self._staged_bytes

    @property
    def staged_artifact_count(self) -> int:
        return len(self._staged_bindings)

    def put_prepared(self, payload: bytes) -> tuple[ObjectRef, ObjectLocation]:
        if len(self._staged_bindings) >= self._max_artifacts:
            raise IntegrityViolation("prepared outcome exceeds the aggregate artifact bound")
        if len(payload) > self._max_bytes - self._staged_bytes:
            raise IntegrityViolation("prepared outcome exceeds the aggregate byte bound")
        object_ref = object_ref_for_bytes(payload)
        location = ObjectLocation(
            store_id="prepared-preflight",
            key=object_ref.key,
            backend_generation="preflight@1",
        )
        self._staged_bytes += len(payload)
        self._blobs[object_ref.key] = bytes(payload)
        self._staged_bindings.append((object_ref, location))
        return object_ref, location

    def commit(
        self,
        target: PreparedArtifactStore,
        artifacts: tuple[PreparedArtifact, ...],
        *,
        max_bytes: int,
    ) -> tuple[PreparedArtifact, ...]:
        if max_bytes != self._max_bytes:
            raise IntegrityViolation("prepared batch commit authority changed after staging")
        if len(artifacts) != len(self._staged_bindings):
            raise IntegrityViolation("prepared batch artifacts differ from the staged call count")
        validate_prepared_artifact_total(artifacts, max_bytes=max_bytes)
        staged_bindings = Counter(
            _prepared_binding_identity(object_ref, location)
            for object_ref, location in self._staged_bindings
        )
        artifact_bindings = Counter(
            _prepared_binding_identity(artifact.object_ref, artifact.location)
            for artifact in artifacts
        )
        if artifact_bindings != staged_bindings:
            raise IntegrityViolation(
                "prepared batch Artifact differs from its exact staged binding"
            )
        for artifact in artifacts:
            if artifact.payload_hash != artifact.object_ref.sha256:
                raise IntegrityViolation(
                    "prepared batch Artifact differs from its exact staged binding"
                )
            blob = self._blobs.get(artifact.object_ref.key)
            if blob is None or object_ref_for_bytes(blob) != artifact.object_ref:
                raise IntegrityViolation("prepared batch staged bytes differ from ObjectRef")
        committed: list[PreparedArtifact] = []
        for artifact in artifacts:
            blob = self._blobs[artifact.object_ref.key]
            object_ref, location = target.put_prepared(blob)
            if object_ref != artifact.object_ref or location.key != object_ref.key:
                raise IntegrityViolation("prepared store changed a staged object binding")
            committed.append(
                PreparedArtifact.model_validate(
                    {
                        **artifact.model_dump(mode="python"),
                        "payload_hash": object_ref.sha256,
                        "object_ref": object_ref,
                        "location": location,
                    }
                )
            )
        return tuple(committed)


def _prepared_binding_identity(
    object_ref: ObjectRef,
    location: ObjectLocation,
) -> tuple[object, ...]:
    return (
        object_ref.object_ref_schema_version,
        object_ref.key,
        object_ref.sha256,
        object_ref.size_bytes,
        location.location_schema_version,
        location.store_id,
        location.key,
        location.backend_generation,
        location.etag,
        location.storage_class,
    )


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

    if len(blob) > MAX_PREPARED_ARTIFACT_BYTES:
        raise IntegrityViolation(
            "prepared artifact exceeds the pre-write byte bound",
            payload_schema_id=payload_schema_id,
            max_bytes=MAX_PREPARED_ARTIFACT_BYTES,
        )
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


def validate_prepared_artifact_total(
    artifacts: tuple[PreparedArtifact, ...],
    *,
    max_bytes: int = MAX_PREPARED_OUTCOME_BYTES,
) -> int:
    """Validate one handler outcome's aggregate object bytes."""

    if not 1 <= max_bytes <= MAX_PREPARED_OUTCOME_BYTES:
        raise IntegrityViolation("prepared outcome byte authority is outside the hard bound")
    total = sum(artifact.object_ref.size_bytes for artifact in artifacts)
    if total > max_bytes:
        raise IntegrityViolation(
            "prepared outcome exceeds the aggregate byte bound",
            total_bytes=total,
            max_bytes=max_bytes,
        )
    return total


def scoped_finding_series_id(
    *,
    namespace: str,
    scope_id: str,
    finding_id: str,
) -> str:
    """Build a bounded readable series id with collision-resistant full binding."""

    if not namespace or not scope_id or not finding_id:
        raise ValueError("scoped finding identity inputs must be non-empty")
    digest = canonical_sha256(
        {
            "namespace": namespace,
            "scope_id": scope_id,
            "finding_id": finding_id,
        }
    )
    # Truncation affects readability only; the full values remain committed by
    # the digest, so equal prefixes cannot collapse distinct series identities.
    return f"{namespace[:64]}:{scope_id[:128]}:{finding_id[:128]}:sha256:{digest}"


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


def rebind_finding_producers(
    findings: list[Finding] | tuple[Finding, ...], *, run_id: str
) -> list[Finding]:
    """Make embedded report Findings agree with their authoritative revisions."""

    return [
        finding
        if finding.producer_run_id == run_id
        else finding.model_copy(update={"producer_run_id": run_id})
        for finding in findings
    ]


_EMBEDDED_FINDING_FIELDS = (
    "findings",
    "deterministic_findings",
    "llm_assisted_findings",
    "simulation_findings",
    "unproven_findings",
)


def rebind_embedded_finding_payload(
    payload: Mapping[str, object], *, run_id: str
) -> dict[str, object]:
    """Rebind every registered report Finding to the authoritative producer Run."""

    rebound = dict(payload)
    for field_name in _EMBEDDED_FINDING_FIELDS:
        raw = rebound.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"embedded {field_name} must be a finding collection")
        rebound[field_name] = [
            finding.model_dump(mode="json")
            for finding in rebind_finding_producers(
                [Finding.model_validate(item) for item in raw], run_id=run_id
            )
        ]
    return rebound


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


FindingHeadRevisionResolver = Callable[[tuple[str, ...]], Mapping[str, int | None]]


def build_prepared_findings(
    evidence: tuple[FindingEvidence, ...],
    *,
    run_id: str,
    head_revision_resolver: FindingHeadRevisionResolver | None = None,
) -> tuple[PreparedFindingV1, ...]:
    """Project findings with the exact retained series-head CAS precondition."""

    if len(evidence) > MAX_PREPARED_FINDINGS:
        raise ValueError("prepared finding count exceeds the frozen output bound")
    finding_ids = tuple(item.finding_id or item.finding.id for item in evidence)
    unique_finding_ids = tuple(dict.fromkeys(finding_ids))
    head_revisions = (
        {}
        if head_revision_resolver is None or not unique_finding_ids
        else head_revision_resolver(unique_finding_ids)
    )
    prepared: list[PreparedFindingV1] = []
    for item, finding_id in zip(evidence, finding_ids, strict=True):
        prepared.append(
            PreparedFindingV1(
                finding_id=finding_id,
                expected_previous_revision=head_revisions.get(finding_id),
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
    validate_prepared_artifact_total(artifacts)
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


def require_exact_profile_binding(
    context: ExecutorContextLike,
    *,
    field_path: str,
    profile: ProfileRefV1,
    profile_kind: ExecutionProfileKindV1,
    validator: ExactProfileBindingValidator = trust_typed_profile_binding,
) -> ResolvedExecutionProfileBindingV1:
    """Close one params ProfileRef over the exact binding frozen on this Run.

    The structural comparison belongs in ``platform`` so every handler fails before
    domain or model work. Production additionally injects ``validator`` to resolve
    the binding's payload hash and lifecycle against retained catalog history.
    """

    binding = resolved_profile(context.payload, field_path, required=False)
    return _validate_exact_profile_binding(
        context,
        binding,
        field_path=field_path,
        profile=profile,
        profile_kind=profile_kind,
        validator=validator,
    )


def _validate_exact_profile_binding(
    context: ExecutorContextLike,
    binding: ResolvedExecutionProfileBindingV1 | None,
    *,
    field_path: str,
    profile: ProfileRefV1,
    profile_kind: ExecutionProfileKindV1,
    validator: ExactProfileBindingValidator,
) -> ResolvedExecutionProfileBindingV1:
    if (
        binding is None
        or binding.profile != profile
        or binding.expected_profile_kind != profile_kind
        or binding.catalog_version != context.payload.execution_profile_catalog_version
        or binding.catalog_digest != context.payload.execution_profile_catalog_digest
    ):
        raise IntegrityViolation(
            "execution profile differs from its exact Run binding",
            field_path=field_path,
        )
    validator(
        binding,
        llm_execution_mode=context.payload.llm_execution_mode,
        run_kind=context.run.kind,
    )
    return binding


def require_exact_profile_bindings(
    context: ExecutorContextLike,
    *,
    expected: Mapping[str, tuple[ProfileRefV1, ExecutionProfileKindV1]],
    validator: ExactProfileBindingValidator = trust_typed_profile_binding,
) -> dict[str, ResolvedExecutionProfileBindingV1]:
    """Close the complete ordered-agnostic profile set frozen on this Run.

    A per-field lookup alone accepts stale or injected bindings that no handler
    consumes.  Task-11 handlers instead declare their complete expected path set
    and fail before any domain/model work when a binding is missing, duplicated,
    or extra.  Each surviving binding is then checked structurally and, in
    production, re-resolved through the retained catalog authority by ``validator``.
    """

    actual_bindings = {binding.field_path: binding for binding in context.payload.resolved_profiles}
    actual_paths = tuple(binding.field_path for binding in context.payload.resolved_profiles)
    expected_paths = tuple(expected)
    if len(actual_paths) != len(expected_paths) or set(actual_paths) != set(expected_paths):
        raise IntegrityViolation(
            "execution profile set differs from its exact Run bindings",
            expected_field_paths=tuple(sorted(expected_paths)),
            actual_field_paths=tuple(sorted(actual_paths)),
        )
    return {
        field_path: _validate_exact_profile_binding(
            context,
            actual_bindings[field_path],
            field_path=field_path,
            profile=profile,
            profile_kind=profile_kind,
            validator=validator,
        )
        for field_path, (profile, profile_kind) in expected.items()
    }


_PREPARED_VERSION_FIELDS = frozenset(
    {
        "doc_version",
        "ir_snapshot_id",
        "constraint_snapshot_id",
        "env_contract_version",
        "seed",
    }
)


def prepared_version_tuple(
    context: ExecutorContextLike,
    *,
    tool_version: str,
    projected_fields: tuple[str, ...] = (),
    overrides: Mapping[str, object | None] | None = None,
) -> VersionTuple:
    """Build the worker-checkable tuple for one prepared domain Artifact.

    Admission has already frozen the exact semantic input projection on the Run.
    Handlers must copy semantic snapshot identities from that tuple, never from an
    Artifact id that merely addresses the parent. Content-derived child identities
    (for example a newly applied IR preview) are supplied as explicit overrides.

    Current-run model/prompt/cassette identity is intentionally absent here. The
    terminal publisher derives those fields from the committed call graph and only
    then adds them to the authoritative Artifact.
    """

    if not tool_version:
        raise ValueError("prepared Artifact tool_version must be non-empty")
    selected = tuple(projected_fields)
    if len(selected) != len(set(selected)) or set(selected) - _PREPARED_VERSION_FIELDS:
        raise ValueError("prepared VersionTuple projection fields are invalid")
    updates = dict(overrides or {})
    if set(updates) - _PREPARED_VERSION_FIELDS:
        raise ValueError("prepared VersionTuple overrides contain unsupported fields")
    if set(selected) & set(updates):
        raise ValueError("prepared VersionTuple field cannot be projected and overridden")

    frozen = context.payload.version_tuple
    values: dict[str, object | None] = {field: getattr(frozen, field) for field in selected}
    values.update(updates)
    values["tool_version"] = tool_version
    return VersionTuple.model_validate(values)


def load_json_blob(reader: ArtifactBlobReader, artifact_id: str) -> object:
    """Read + JSON-decode an input artifact blob (fail-closed on malformed bytes)."""

    raw = reader.read_bytes(artifact_id)
    return json.loads(raw)


__all__ = [
    "ArtifactBlobReader",
    "ExactProfileBindingValidator",
    "ExecutorContextLike",
    "FindingEvidence",
    "FindingHeadRevisionResolver",
    "LlmExecutionMode",
    "ModelBridgePort",
    "PreparedArtifactStore",
    "PreparedArtifactBatchStore",
    "build_prepared_findings",
    "build_success_result",
    "canonical_payload_bytes",
    "finding_to_payload",
    "load_json_blob",
    "prepared_version_tuple",
    "rebind_embedded_finding_payload",
    "rebind_finding_producers",
    "require_exact_profile_binding",
    "require_exact_profile_bindings",
    "scoped_finding_series_id",
    "resolved_profile",
    "store_prepared_artifact",
    "store_prepared_blob",
    "trust_typed_profile_binding",
    "validate_prepared_artifact_total",
]
