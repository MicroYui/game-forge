"""Production-shaped deterministic rollback schema/impact ports."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gameforge.apps.worker.rollback_validation import (
    MAX_ROLLBACK_IMPACT_DIFF_ENTRIES_V1,
    DeterministicRollbackImpactAnalyzer,
    ExactRollbackSchemaAnalyzer,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.lineage import ArtifactV2, VersionTuple, build_artifact_v2
from gameforge.contracts.versions import IR_SCHEMA_VERSION
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.rollback_validation import (
    IMPACT_PROFILE_FIELD,
    ROLLBACK_PROFILE_FIELD,
    SCHEMA_PROFILE_FIELD,
    RollbackImpactRequest,
    RollbackSchemaRequest,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.contracts.lineage import object_ref_for_bytes


@dataclass
class _ArtifactReader:
    artifacts: dict[str, ArtifactV2]
    payloads: dict[str, bytes]

    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        return self.artifacts[artifact_id]

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes:
        payload = self.payloads[artifact_id]
        if len(payload) > max_bytes:
            raise ValueError("bounded read exceeded")
        return payload


def _artifact(
    snapshot: Snapshot, *, payload_schema_id: str = IR_SCHEMA_VERSION
) -> tuple[ArtifactV2, bytes]:
    payload = canonical_json(snapshot.content_payload).encode("utf-8")
    object_ref = object_ref_for_bytes(payload)
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot.snapshot_id,
            tool_version="fixture@1",
        ),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": payload_schema_id},
    )
    return artifact, payload


def _constraint_artifact(
    constraints: tuple[Constraint, ...],
) -> tuple[ArtifactV2, bytes]:
    wire = {
        "dsl_grammar_version": "dsl@1",
        "constraints": [item.model_dump(mode="json", by_alias=True) for item in constraints],
    }
    payload = canonical_json(wire).encode("utf-8")
    object_ref = object_ref_for_bytes(payload)
    artifact = build_artifact_v2(
        kind="constraint_snapshot",
        version_tuple=VersionTuple(
            constraint_snapshot_id=object_ref.sha256,
            tool_version="fixture@1",
        ),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": "constraint-snapshot@1"},
    )
    return artifact, payload


def _bindings():
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[-1]

    def resolve(path: str, profile_id: str, kind: str):
        return registry.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path=path,
            profile=ProfileRefV1(profile_id=profile_id, version=1),
            expected_profile_kind=kind,
        )

    return (
        registry,
        resolve(ROLLBACK_PROFILE_FIELD, "builtin.rollback", "rollback"),
        resolve(
            SCHEMA_PROFILE_FIELD,
            "builtin.schema_compatibility",
            "schema_compatibility",
        ),
        resolve(f"{IMPACT_PROFILE_FIELD}/0", "builtin.impact_analysis", "impact_analysis"),
    )


def test_exact_schema_port_proves_current_ir_reader() -> None:
    registry, rollback, schema, _ = _bindings()
    target_snapshot = Snapshot({}, {})
    target, payload = _artifact(target_snapshot)
    reader = _ArtifactReader({target.artifact_id: target}, {target.artifact_id: payload})

    result = ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackSchemaRequest(
            target_artifact_id=target.artifact_id,
            ref_name="ref:main",
            schema_profile_binding=schema,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "passed"
    assert result.target_digest == target.payload_hash
    assert result.target_snapshot_id == target_snapshot.snapshot_id
    assert result.target_version_tuple == target.version_tuple


def test_exact_schema_port_rejects_bytes_that_differ_from_object_ref() -> None:
    registry, rollback, schema, _ = _bindings()
    target_snapshot = Snapshot({}, {})
    target, _ = _artifact(target_snapshot)
    reader = _ArtifactReader(
        {target.artifact_id: target},
        {target.artifact_id: b"{}"},
    )

    with pytest.raises(IntegrityViolation, match="immutable ObjectRef"):
        ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
            RollbackSchemaRequest(
                target_artifact_id=target.artifact_id,
                ref_name="ref:main",
                schema_profile_binding=schema,
                rollback_profile_binding=rollback,
            )
        )


def test_schema_port_cannot_pass_an_unregistered_payload_schema() -> None:
    registry, rollback, schema, _ = _bindings()
    target, payload = _artifact(Snapshot({}, {}), payload_schema_id="ir-core@0")
    reader = _ArtifactReader({target.artifact_id: target}, {target.artifact_id: payload})

    result = ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackSchemaRequest(
            target_artifact_id=target.artifact_id,
            ref_name="ref:main",
            schema_profile_binding=schema,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "unproven"
    assert result.reason_code == "rollback_schema_reader_unavailable"


def test_schema_port_rejects_duplicate_key_snapshot_bytes() -> None:
    registry, rollback, schema, _ = _bindings()
    payload = b'{"meta_schema_version":"meta@1","entities":{},"entities":{},"relations":{}}'
    object_ref = object_ref_for_bytes(payload)
    target = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(
            ir_snapshot_id=Snapshot({}, {}).snapshot_id,
            tool_version="fixture@1",
        ),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": IR_SCHEMA_VERSION},
    )
    reader = _ArtifactReader(
        {target.artifact_id: target},
        {target.artifact_id: payload},
    )

    result = ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackSchemaRequest(
            target_artifact_id=target.artifact_id,
            ref_name="ref:main",
            schema_profile_binding=schema,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "failed"
    assert result.reason_code == "rollback_schema_payload_invalid"


def test_exact_schema_port_accepts_a_canonical_constraint_snapshot() -> None:
    registry, rollback, schema, _ = _bindings()
    target, payload = _constraint_artifact(
        (
            Constraint(
                id="constraint:cap",
                kind="numeric",
                oracle="deterministic",
                **{"assert": "reward_gold <= 80"},
                severity="major",
            ),
        )
    )
    reader = _ArtifactReader({target.artifact_id: target}, {target.artifact_id: payload})

    result = ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackSchemaRequest(
            target_artifact_id=target.artifact_id,
            ref_name="ref:constraints",
            schema_profile_binding=schema,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "passed"
    assert result.target_artifact_kind == "constraint_snapshot"
    assert result.target_snapshot_id == target.version_tuple.constraint_snapshot_id
    assert result.target_digest == target.payload_hash


def test_impact_port_diffs_exact_current_and_target_snapshots_deterministically() -> None:
    registry, rollback, _, impact = _bindings()
    current_snapshot = Snapshot(
        {"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={"level": 1})},
        {},
    )
    target_snapshot = Snapshot(
        {"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={"level": 2})},
        {},
    )
    current, current_payload = _artifact(current_snapshot)
    target, target_payload = _artifact(target_snapshot)
    reader = _ArtifactReader(
        {current.artifact_id: current, target.artifact_id: target},
        {current.artifact_id: current_payload, target.artifact_id: target_payload},
    )
    analyzer = DeterministicRollbackImpactAnalyzer(artifacts=reader, registry=registry)
    request = RollbackImpactRequest(
        current_artifact_id=current.artifact_id,
        current_ref_revision=9,
        target_artifact_id=target.artifact_id,
        ref_name="ref:main",
        impact_profile_binding=impact,
        rollback_profile_binding=rollback,
    )

    first = analyzer.analyze(request)
    second = analyzer.analyze(request)

    assert first == second
    assert first.status == "passed"
    assert first.detail["current_snapshot_id"] == current_snapshot.snapshot_id
    assert first.detail["target_snapshot_id"] == target_snapshot.snapshot_id
    assert first.detail["entry_count"] == 1
    assert isinstance(first.detail["diff_digest"], str)


def test_impact_port_reports_unproven_when_complete_diff_exceeds_v1_cap() -> None:
    registry, rollback, _, impact = _bindings()
    current, current_payload = _artifact(Snapshot({}, {}))
    target_snapshot = Snapshot(
        {
            f"npc:{index}": Entity(id=f"npc:{index}", type=NodeType.NPC, attrs={})
            for index in range(MAX_ROLLBACK_IMPACT_DIFF_ENTRIES_V1 + 1)
        },
        {},
    )
    target, target_payload = _artifact(target_snapshot)
    reader = _ArtifactReader(
        {current.artifact_id: current, target.artifact_id: target},
        {current.artifact_id: current_payload, target.artifact_id: target_payload},
    )

    result = DeterministicRollbackImpactAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackImpactRequest(
            current_artifact_id=current.artifact_id,
            current_ref_revision=9,
            target_artifact_id=target.artifact_id,
            ref_name="ref:main",
            impact_profile_binding=impact,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "unproven"
    assert result.reason_code == "rollback_impact_budget_exhausted"


def test_impact_port_diffs_constraint_snapshots_by_constraint_identity() -> None:
    registry, rollback, _, impact = _bindings()
    current, current_payload = _constraint_artifact(
        (
            Constraint(
                id="constraint:cap",
                kind="numeric",
                oracle="deterministic",
                **{"assert": "reward_gold <= 100"},
                severity="major",
            ),
        )
    )
    target, target_payload = _constraint_artifact(
        (
            Constraint(
                id="constraint:cap",
                kind="numeric",
                oracle="deterministic",
                **{"assert": "reward_gold <= 80"},
                severity="major",
            ),
        )
    )
    reader = _ArtifactReader(
        {current.artifact_id: current, target.artifact_id: target},
        {current.artifact_id: current_payload, target.artifact_id: target_payload},
    )

    result = DeterministicRollbackImpactAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackImpactRequest(
            current_artifact_id=current.artifact_id,
            current_ref_revision=4,
            target_artifact_id=target.artifact_id,
            ref_name="ref:constraints",
            impact_profile_binding=impact,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "passed"
    assert result.detail["current_snapshot_id"] == current.version_tuple.constraint_snapshot_id
    assert result.detail["target_snapshot_id"] == target.version_tuple.constraint_snapshot_id
    assert result.detail["entry_count"] == 1


def test_impact_port_rejects_cross_kind_rollback_history() -> None:
    registry, rollback, _, impact = _bindings()
    current, current_payload = _artifact(Snapshot({}, {}))
    target, target_payload = _constraint_artifact(())
    reader = _ArtifactReader(
        {current.artifact_id: current, target.artifact_id: target},
        {current.artifact_id: current_payload, target.artifact_id: target_payload},
    )

    result = DeterministicRollbackImpactAnalyzer(artifacts=reader, registry=registry).analyze(
        RollbackImpactRequest(
            current_artifact_id=current.artifact_id,
            current_ref_revision=4,
            target_artifact_id=target.artifact_id,
            ref_name="ref:mismatched",
            impact_profile_binding=impact,
            rollback_profile_binding=rollback,
        )
    )

    assert result.status == "failed"
    assert result.reason_code == "rollback_impact_artifact_kind_mismatch"


def test_ports_reject_a_binding_for_the_wrong_exact_field_path() -> None:
    registry, rollback, schema, _ = _bindings()
    target, payload = _artifact(Snapshot({}, {}))
    reader = _ArtifactReader({target.artifact_id: target}, {target.artifact_id: payload})

    with pytest.raises(IntegrityViolation, match="field path"):
        ExactRollbackSchemaAnalyzer(artifacts=reader, registry=registry).analyze(
            RollbackSchemaRequest(
                target_artifact_id=target.artifact_id,
                ref_name="ref:main",
                schema_profile_binding=schema.model_copy(
                    update={"field_path": "/params/impact_profiles/0"}
                ),
                rollback_profile_binding=rollback,
            )
        )
