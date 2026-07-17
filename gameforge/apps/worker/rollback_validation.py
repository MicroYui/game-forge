"""Exact deterministic production ports for rollback schema and impact analysis.

The platform handler owns orchestration and evidence sealing.  These worker-side
adapters own the resource-shaped work: resolving the Run's frozen profile binding
from its retained catalog, re-reading exact Artifact envelopes/objects, parsing the
canonical IR snapshots, and performing a bounded complete field diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ResolvedExecutionProfileBindingV1,
)
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.versions import IR_SCHEMA_VERSION, META_SCHEMA_VERSION
from gameforge.platform.diff.engine import CollectionIdentity, iter_snapshot_diff_entries
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.rollback_validation import (
    IMPACT_PROFILE_FIELD,
    ROLLBACK_PROFILE_FIELD,
    SCHEMA_PROFILE_FIELD,
    DimensionCheckV1,
    RollbackImpactRequest,
    RollbackSchemaRequest,
    RollbackTargetInspectionV1,
)
from gameforge.spine.ir.snapshot import Snapshot

MAX_ROLLBACK_SNAPSHOT_BYTES_V1 = 16 * 1024 * 1024
MAX_ROLLBACK_IMPACT_DIFF_ENTRIES_V1 = 4_096

_SUPPORTED_TARGET_SCHEMAS = {
    "ir_snapshot": IR_SCHEMA_VERSION,
    "constraint_snapshot": "constraint-snapshot@1",
}

_ROLLBACK_KIND = "rollback.validate"
_ROLLBACK_VERSION = 1
_ROLLBACK_PAYLOAD_SCHEMA = "rollback-validation@1"


class ExactRollbackArtifactReader(Protocol):
    """Identity-aware, bounded Artifact reads supplied by worker composition."""

    def load_artifact(self, artifact_id: str) -> ArtifactV2: ...

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes: ...


class _SnapshotPayloadInvalid(ValueError):
    pass


def _resolve_profile(
    registry: ImmutablePlatformRegistry,
    binding: ResolvedExecutionProfileBindingV1,
    *,
    expected_field_path: str,
    expected_kind: str,
    expected_handler_key: str,
) -> ExecutionProfileDefinitionV1:
    if binding.field_path != expected_field_path:
        raise IntegrityViolation(
            "rollback execution profile has the wrong exact field path",
            expected_field_path=expected_field_path,
            actual_field_path=binding.field_path,
        )
    if binding.expected_profile_kind != expected_kind:
        raise IntegrityViolation(
            "rollback execution profile has the wrong kind",
            field_path=expected_field_path,
        )
    definition, lifecycle = registry.resolve_execution_profile_binding(binding)
    if lifecycle.state != "active":
        raise IntegrityViolation(
            "rollback execution profile lifecycle does not permit execution",
            field_path=expected_field_path,
            lifecycle_state=lifecycle.state,
        )
    if definition.handler_key != expected_handler_key:
        raise IntegrityViolation(
            "rollback execution profile handler is unavailable",
            field_path=expected_field_path,
            handler_key=definition.handler_key,
        )
    if definition.config:
        raise IntegrityViolation(
            "rollback built-in profile v1 has unsupported non-empty config",
            field_path=expected_field_path,
        )
    if not any(
        item.kind == _ROLLBACK_KIND and item.version == _ROLLBACK_VERSION
        for item in definition.compatible_run_kinds
    ):
        raise IntegrityViolation(
            "rollback execution profile is incompatible with rollback.validate@1",
            field_path=expected_field_path,
        )
    if _ROLLBACK_PAYLOAD_SCHEMA not in definition.input_schema_ids:
        raise IntegrityViolation(
            "rollback execution profile does not accept rollback-validation@1",
            field_path=expected_field_path,
        )
    return definition


def _resolve_rollback_authority(
    registry: ImmutablePlatformRegistry,
    binding: ResolvedExecutionProfileBindingV1,
) -> ExecutionProfileDefinitionV1:
    return _resolve_profile(
        registry,
        binding,
        expected_field_path=ROLLBACK_PROFILE_FIELD,
        expected_kind="rollback",
        expected_handler_key="builtin_rollback_profile@1",
    )


def _load_artifact(
    artifacts: ExactRollbackArtifactReader,
    artifact_id: str,
) -> ArtifactV2:
    artifact = artifacts.load_artifact(artifact_id)
    if not isinstance(artifact, ArtifactV2) or artifact.artifact_id != artifact_id:
        raise IntegrityViolation(
            "rollback Artifact authority returned another envelope",
            artifact_id=artifact_id,
        )
    return artifact


def _read_verified_bytes(
    artifacts: ExactRollbackArtifactReader,
    artifact: ArtifactV2,
    *,
    max_bytes: int,
) -> bytes:
    payload = artifacts.read_bytes_bounded(artifact.artifact_id, max_bytes=max_bytes)
    if (
        len(payload) != artifact.object_ref.size_bytes
        or sha256(payload).hexdigest() != artifact.payload_hash
    ):
        raise IntegrityViolation(
            "rollback Artifact bytes differ from their immutable ObjectRef",
            artifact_id=artifact.artifact_id,
        )
    return payload


def _parse_snapshot(payload: bytes) -> Snapshot:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if not isinstance(raw, dict):
            raise ValueError("snapshot payload is not an object")
        if canonical_json(raw).encode("utf-8") != payload:
            raise ValueError("snapshot payload is not canonical JSON")
        return snapshot_from_canonical_view(raw)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        IntegrityViolation,
    ) as exc:
        raise _SnapshotPayloadInvalid from exc


def _snapshot_id(artifact: ArtifactV2) -> str | None:
    if artifact.kind == "ir_snapshot":
        return artifact.version_tuple.ir_snapshot_id
    if artifact.kind == "constraint_snapshot":
        return artifact.version_tuple.constraint_snapshot_id
    return None


def _target_inspection(
    artifact: ArtifactV2,
    *,
    status: str,
    reason_code: str | None,
    schema_profile_binding: ResolvedExecutionProfileBindingV1,
    rollback_profile_binding: ResolvedExecutionProfileBindingV1,
) -> RollbackTargetInspectionV1:
    return RollbackTargetInspectionV1(
        status=status,  # type: ignore[arg-type]
        target_artifact_kind=artifact.kind,
        target_digest=artifact.payload_hash,
        target_snapshot_id=_snapshot_id(artifact),
        target_version_tuple=artifact.version_tuple,
        reason_code=reason_code,
        schema_profile_binding=schema_profile_binding,
        rollback_profile_binding=rollback_profile_binding,
    )


@dataclass(frozen=True, slots=True)
class ExactRollbackSchemaAnalyzer:
    """Prove that the target is readable by the exact built-in schema policy."""

    artifacts: ExactRollbackArtifactReader
    registry: ImmutablePlatformRegistry
    max_snapshot_bytes: int = MAX_ROLLBACK_SNAPSHOT_BYTES_V1

    def analyze(self, request: RollbackSchemaRequest) -> RollbackTargetInspectionV1:
        _resolve_rollback_authority(self.registry, request.rollback_profile_binding)
        _resolve_profile(
            self.registry,
            request.schema_profile_binding,
            expected_field_path=SCHEMA_PROFILE_FIELD,
            expected_kind="schema_compatibility",
            expected_handler_key="builtin_schema_compatibility_profile@1",
        )
        target = _load_artifact(self.artifacts, request.target_artifact_id)
        schema_id = target.meta.get("payload_schema_id")
        expected_schema = _SUPPORTED_TARGET_SCHEMAS.get(target.kind)
        if (
            expected_schema is None
            or schema_id != expected_schema
            or target.object_ref.size_bytes > self.max_snapshot_bytes
        ):
            return _target_inspection(
                target,
                status="unproven",
                reason_code="rollback_schema_reader_unavailable",
                schema_profile_binding=request.schema_profile_binding,
                rollback_profile_binding=request.rollback_profile_binding,
            )
        payload = _read_verified_bytes(
            self.artifacts,
            target,
            max_bytes=self.max_snapshot_bytes,
        )
        try:
            if target.kind == "ir_snapshot":
                snapshot = _parse_snapshot(payload)
                semantic_id = snapshot.snapshot_id
                semantic_binding_matches = (
                    snapshot.meta_schema_version == META_SCHEMA_VERSION
                    and target.version_tuple.ir_snapshot_id == semantic_id
                )
            else:
                decoded = decode_and_validate_artifact_payload(
                    payload_schema_id="constraint-snapshot@1",
                    blob=payload,
                )
                semantic_id = target.version_tuple.constraint_snapshot_id
                semantic_binding_matches = isinstance(semantic_id, str) and bool(semantic_id)
                if decoded.get("dsl_grammar_version") is None:
                    semantic_binding_matches = False
        except (IntegrityViolation, _SnapshotPayloadInvalid):
            return _target_inspection(
                target,
                status="failed",
                reason_code="rollback_schema_payload_invalid",
                schema_profile_binding=request.schema_profile_binding,
                rollback_profile_binding=request.rollback_profile_binding,
            )
        if not semantic_binding_matches:
            return _target_inspection(
                target,
                status="failed",
                reason_code="rollback_schema_snapshot_binding_mismatch",
                schema_profile_binding=request.schema_profile_binding,
                rollback_profile_binding=request.rollback_profile_binding,
            )
        return _target_inspection(
            target,
            status="passed",
            reason_code=None,
            schema_profile_binding=request.schema_profile_binding,
            rollback_profile_binding=request.rollback_profile_binding,
        )


@dataclass(frozen=True, slots=True)
class DeterministicRollbackImpactAnalyzer:
    """Compute one complete, bounded canonical current→target Snapshot diff."""

    artifacts: ExactRollbackArtifactReader
    registry: ImmutablePlatformRegistry
    max_snapshot_bytes: int = MAX_ROLLBACK_SNAPSHOT_BYTES_V1
    max_diff_entries: int = MAX_ROLLBACK_IMPACT_DIFF_ENTRIES_V1

    def analyze(self, request: RollbackImpactRequest) -> DimensionCheckV1:
        _resolve_rollback_authority(self.registry, request.rollback_profile_binding)
        _resolve_profile(
            self.registry,
            request.impact_profile_binding,
            expected_field_path=request.impact_profile_binding.field_path,
            expected_kind="impact_analysis",
            expected_handler_key="builtin_impact_analysis_profile@1",
        )
        if not request.impact_profile_binding.field_path.startswith(f"{IMPACT_PROFILE_FIELD}/"):
            raise IntegrityViolation("rollback impact profile has the wrong exact field path")

        current = _load_artifact(self.artifacts, request.current_artifact_id)
        target = _load_artifact(self.artifacts, request.target_artifact_id)
        detail: dict[str, object] = {
            "current_artifact_id": current.artifact_id,
            "current_ref_revision": request.current_ref_revision,
            "target_artifact_id": target.artifact_id,
            "current_artifact_kind": current.kind,
            "target_artifact_kind": target.kind,
        }
        if current.kind != target.kind:
            return self._result(
                request,
                status="failed",
                reason_code="rollback_impact_artifact_kind_mismatch",
                detail=detail,
            )
        schema_id = current.meta.get("payload_schema_id")
        expected_schema = _SUPPORTED_TARGET_SCHEMAS.get(current.kind)
        if (
            expected_schema is None
            or schema_id != expected_schema
            or target.meta.get("payload_schema_id") != expected_schema
        ):
            return self._result(
                request,
                status="unproven",
                reason_code="rollback_impact_snapshot_reader_unavailable",
                detail=detail,
            )
        if (
            current.object_ref.size_bytes > self.max_snapshot_bytes
            or target.object_ref.size_bytes > self.max_snapshot_bytes
        ):
            return self._result(
                request,
                status="unproven",
                reason_code="rollback_impact_budget_exhausted",
                detail=detail,
            )
        current_payload = _read_verified_bytes(
            self.artifacts,
            current,
            max_bytes=self.max_snapshot_bytes,
        )
        target_payload = _read_verified_bytes(
            self.artifacts,
            target,
            max_bytes=self.max_snapshot_bytes,
        )
        try:
            if current.kind == "ir_snapshot":
                current_snapshot = _parse_snapshot(current_payload)
                target_snapshot = _parse_snapshot(target_payload)
                current_view = current_snapshot.content_payload
                target_view = target_snapshot.content_payload
                current_semantic_id = current_snapshot.snapshot_id
                target_semantic_id = target_snapshot.snapshot_id
                collection_identities = ()
            else:
                current_view = decode_and_validate_artifact_payload(
                    payload_schema_id="constraint-snapshot@1",
                    blob=current_payload,
                )
                target_view = decode_and_validate_artifact_payload(
                    payload_schema_id="constraint-snapshot@1",
                    blob=target_payload,
                )
                current_semantic_id = current.version_tuple.constraint_snapshot_id
                target_semantic_id = target.version_tuple.constraint_snapshot_id
                collection_identities = (
                    CollectionIdentity(path="/constraints", identity_key="id"),
                )
        except (_SnapshotPayloadInvalid, IntegrityViolation):
            return self._result(
                request,
                status="unproven",
                reason_code="rollback_impact_snapshot_reader_unavailable",
                detail=detail,
            )
        if current.kind == "ir_snapshot" and (
            current.version_tuple.ir_snapshot_id != current_semantic_id
            or target.version_tuple.ir_snapshot_id != target_semantic_id
        ):
            raise IntegrityViolation("rollback impact Snapshot bytes differ from Artifact tuple")
        if current.kind == "constraint_snapshot" and (
            not current_semantic_id or not target_semantic_id
        ):
            raise IntegrityViolation(
                "rollback impact constraint bytes lack Artifact tuple identity"
            )
        entries = []
        for entry in iter_snapshot_diff_entries(
            current_view,
            target_view,
            collection_identities=collection_identities,
        ):
            if len(entries) == self.max_diff_entries:
                return self._result(
                    request,
                    status="unproven",
                    reason_code="rollback_impact_budget_exhausted",
                    detail={
                        **detail,
                        "current_snapshot_id": current_semantic_id,
                        "target_snapshot_id": target_semantic_id,
                        "entry_count_lower_bound": self.max_diff_entries + 1,
                    },
                )
            entries.append(entry)
        entry_payloads = tuple(entry.model_dump(mode="json") for entry in entries)
        return self._result(
            request,
            status="passed",
            reason_code=None,
            detail={
                **detail,
                "current_snapshot_id": current_semantic_id,
                "target_snapshot_id": target_semantic_id,
                "entry_count": len(entries),
                "diff_digest": canonical_sha256(
                    {
                        "impact_schema_version": "rollback-impact@1",
                        "current_snapshot_id": current_semantic_id,
                        "target_snapshot_id": target_semantic_id,
                        "entries": entry_payloads,
                    }
                ),
            },
        )

    @staticmethod
    def _result(
        request: RollbackImpactRequest,
        *,
        status: str,
        reason_code: str | None,
        detail: dict[str, object],
    ) -> DimensionCheckV1:
        return DimensionCheckV1(
            status=status,  # type: ignore[arg-type]
            reason_code=reason_code,
            detail=detail,
            profile_binding=request.impact_profile_binding,
            rollback_profile_binding=request.rollback_profile_binding,
        )


__all__ = [
    "MAX_ROLLBACK_IMPACT_DIFF_ENTRIES_V1",
    "MAX_ROLLBACK_SNAPSHOT_BYTES_V1",
    "DeterministicRollbackImpactAnalyzer",
    "ExactRollbackArtifactReader",
    "ExactRollbackSchemaAnalyzer",
]
