"""Focused checks for Task 9's exact terminal payload-schema registry."""

from __future__ import annotations

import json
from typing import cast

import pytest

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, PatchV2
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.versions import DSL_GRAMMAR_VERSION
from gameforge.platform.publication.payload_schema import (
    ARTIFACT_PAYLOAD_VALIDATORS,
    UNAVAILABLE_ARTIFACT_PAYLOAD_SCHEMAS,
    validate_artifact_payload,
)
from gameforge.platform.registry.defaults import ARTIFACT_PAYLOAD_SCHEMAS
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed
from gameforge.spine.ir.snapshot import Snapshot


def _wire(payload: object) -> dict[str, object]:
    decoded = json.loads(canonical_json(payload))
    assert isinstance(decoded, dict)
    return decoded


def _finding(*, snapshot_id: str = "snapshot:1") -> Finding:
    return Finding(
        id="finding:1",
        source="checker",
        producer_id="graph@1",
        producer_run_id="run:1",
        oracle_type="deterministic",
        defect_class="dangling_reference",
        severity="major",
        snapshot_id=snapshot_id,
        status="confirmed",
        message="dangling reference",
    )


def test_registry_covers_every_declared_schema_without_wildcards() -> None:
    declared = {
        schema_id for schema_ids in ARTIFACT_PAYLOAD_SCHEMAS.values() for schema_id in schema_ids
    }
    assert set(ARTIFACT_PAYLOAD_VALIDATORS) == declared
    assert set(UNAVAILABLE_ARTIFACT_PAYLOAD_SCHEMAS) == {
        "backup-object-manifest@1",
        "bench-dataset@1",
        "bench-report@2",
        "cassette-bundle@1",
        "cassette-record-shard@1",
        "dr-drill-evidence@1",
        "golden-suite@1",
        "regression-suite@1",
        "source-raw@1",
        "source-rendered@1",
    }
    assert all("*" not in schema_id for schema_id in ARTIFACT_PAYLOAD_VALIDATORS)
    with pytest.raises(TypeError):
        cast(dict[str, object], ARTIFACT_PAYLOAD_VALIDATORS)["fake@1"] = object()


@pytest.mark.parametrize(
    "schema_id",
    [
        schema_id
        for schema_id, validator in ARTIFACT_PAYLOAD_VALIDATORS.items()
        if validator.is_available
    ],
)
def test_every_available_schema_rejects_an_empty_worker_mapping(schema_id: str) -> None:
    with pytest.raises(IntegrityViolation, match="discriminator is missing"):
        validate_artifact_payload(payload_schema_id=schema_id, payload={})


def test_unknown_and_non_terminal_schemas_fail_closed() -> None:
    with pytest.raises(IntegrityViolation, match="not registered"):
        validate_artifact_payload(payload_schema_id="checker-report@future", payload={})

    with pytest.raises(IntegrityViolation, match="not valid on the terminal domain"):
        validate_artifact_payload(
            payload_schema_id="bench-dataset@1",
            payload={"payload_schema_version": "bench-dataset@1", "cases": []},
        )


def test_checker_report_returns_a_canonical_typed_mapping() -> None:
    payload = _wire(
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:1",
            "findings": [_finding().model_dump(mode="json")],
        }
    )
    parsed = validate_artifact_payload(
        payload_schema_id="checker-report@1",
        payload=payload,
    )
    assert parsed == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"payload_schema_version": "simulation-result@1"},
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:1",
            "findings": [],
            "worker_claim": {"trusted": True},
        },
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:1",
            "findings": [_finding(snapshot_id="snapshot:other").model_dump(mode="json")],
        },
    ],
)
def test_checker_report_rejects_wrong_discriminator_extra_fields_and_cross_snapshot_findings(
    payload: dict[str, object],
) -> None:
    with pytest.raises(IntegrityViolation):
        validate_artifact_payload(payload_schema_id="checker-report@1", payload=payload)


def test_ir_and_constraint_snapshots_use_their_real_version_discriminators() -> None:
    snapshot = Snapshot(
        entities={"item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={"name": "token"})},
        relations={},
    )
    ir_payload = _wire(snapshot.content_payload)
    parsed_ir = validate_artifact_payload(
        payload_schema_id="ir-core@1",
        payload=ir_payload,
    )
    assert parsed_ir["meta_schema_version"] == "meta@1"

    constraint = Constraint(
        id="constraint:1",
        kind="structural",
        oracle="deterministic",
        assert_="true",
        severity="major",
    )
    constraint_payload = _wire(
        {
            "dsl_grammar_version": DSL_GRAMMAR_VERSION,
            "constraints": [constraint.model_dump(mode="json")],
        }
    )
    assert (
        validate_artifact_payload(
            payload_schema_id="constraint-snapshot@1",
            payload=constraint_payload,
        )
        == constraint_payload
    )

    wrong_grammar = {**constraint_payload, "dsl_grammar_version": "dsl@future"}
    with pytest.raises(IntegrityViolation, match="discriminator differs"):
        validate_artifact_payload(
            payload_schema_id="constraint-snapshot@1",
            payload=wrong_grammar,
        )


def test_model_backed_payload_requires_exact_discriminator_and_rejects_nested_extra_fields() -> (
    None
):
    patch = _wire(
        PatchV2(
            revision=1,
            base_snapshot_id="snapshot:base",
            target_snapshot_id="snapshot:target",
            side_effect_risk="low",
            ops=[],
            produced_by="human",
            rationale="bounded edit",
        ).model_dump(mode="json")
    )
    assert validate_artifact_payload(payload_schema_id="patch@2", payload=patch) == patch

    wrong = {**patch, "patch_schema_version": "patch@future"}
    with pytest.raises(IntegrityViolation, match="discriminator differs"):
        validate_artifact_payload(payload_schema_id="patch@2", payload=wrong)

    extra = {**patch, "worker_meta": {"claimed_valid": True}}
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(payload_schema_id="patch@2", payload=extra)


def test_regression_evidence_rederives_complete_subseed_binding() -> None:
    run_kind = RunKindRef(kind="patch.validate", version=1)
    profile = ProfileRefV1(profile_id="validation:default", version=1)
    root_seed = 7
    case_id = "suite:smoke"
    seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
        replication_index=0,
    )
    payload = {
        "payload_schema_version": "regression-evidence@1",
        "suite_artifact_id": case_id,
        "snapshot_id": "snapshot:1",
        "status": "passed",
        "root_seed": root_seed,
        "run_kind": run_kind.model_dump(mode="json"),
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "case_id": case_id,
        "replication_index": 0,
        "seed": seed,
        "seed_derivation_version": "subseed@1",
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
        == payload
    )

    fabricated = {**payload, "seed": seed + 1}
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=fabricated,
        )


def test_regression_suite_evidence_requires_the_exact_unproven_reason() -> None:
    payload = {
        "payload_schema_version": "regression-evidence@1",
        "suite_artifact_id": "artifact:suite",
        "snapshot_id": "snapshot:1",
        "status": "unproven",
        "reason_code": "adapter_environment_unavailable",
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
        == payload
    )

    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload={**payload, "reason_code": None},
        )


def test_passed_regression_dimension_uses_the_canonical_omitted_reason() -> None:
    payload = {
        "payload_schema_version": "regression-evidence@1",
        "requirement_id": "history",
        "dimension": "history",
        "status": "passed",
        "detail": {"target_artifact_id": "artifact:target"},
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
        == payload
    )

    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload={**payload, "status": "failed"},
        )
