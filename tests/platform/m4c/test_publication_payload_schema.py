"""Focused checks for Task 9's exact terminal payload-schema registry."""

from __future__ import annotations

import json
from typing import cast

import pytest

import gameforge.platform.publication.payload_schema as payload_schema_mod
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, PatchV2
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.jobs import (
    AgentPromptArtifactBindingV1,
    AgentPromptContextV1,
    AgentPromptSourceMessageV1,
    MAX_AGENT_PROMPT_CONTEXT_BYTES,
)
from gameforge.contracts.versions import DSL_GRAMMAR_VERSION
from gameforge.platform.publication.payload_schema import (
    ARTIFACT_PAYLOAD_VALIDATORS,
    UNAVAILABLE_ARTIFACT_PAYLOAD_SCHEMAS,
    decode_and_validate_artifact_payload,
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


def test_agent_prompt_context_has_its_exact_128_mib_schema_envelope() -> None:
    context = AgentPromptContextV1(
        context_kind="constraint_extraction",
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=1,
        agent_node_id="extraction",
        prompt_version="extraction@1",
        messages=(
            AgentPromptSourceMessageV1(
                role="user",
                content="\x00" * (16 * 1024 * 1024),
                purpose="context",
            ),
        ),
        upstream_artifacts=(
            AgentPromptArtifactBindingV1(
                binding_key="source:0001",
                artifact_id="artifact:source",
                artifact_kind="source_raw",
                payload_schema_id="source-raw@1",
                payload_hash="a" * 64,
            ),
        ),
    )
    payload = json.loads(canonical_json(context.model_dump(mode="json")))
    blob = canonical_json(payload).encode("utf-8")
    assert len(blob) > payload_schema_mod.MAX_PAYLOAD_JSON_BYTES
    assert len(blob) <= MAX_AGENT_PROMPT_CONTEXT_BYTES

    assert (
        validate_artifact_payload(
            payload_schema_id="agent-prompt-context@1",
            payload=payload,
        )
        == payload
    )
    assert (
        decode_and_validate_artifact_payload(
            payload_schema_id="agent-prompt-context@1",
            blob=blob,
        )
        == payload
    )
    assert (
        payload_schema_mod.encode_validated_artifact_payload(
            payload_schema_id="agent-prompt-context@1",
            payload=payload,
        )
        == blob
    )
    with pytest.raises(IntegrityViolation, match="publication byte bound"):
        validate_artifact_payload(
            payload_schema_id="checker-report@1",
            payload={
                "payload_schema_version": "checker-report@1",
                "oversized": context.messages[0].content,
            },
        )


def test_payload_depth_is_rejected_before_canonical_serialization(monkeypatch) -> None:
    payload: dict[str, object] = {}
    cursor = payload
    for _ in range(payload_schema_mod.MAX_PAYLOAD_JSON_DEPTH + 1):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    monkeypatch.setattr(
        payload_schema_mod,
        "canonical_json",
        lambda _value: (_ for _ in ()).throw(
            AssertionError("deep payload must be rejected before canonicalization")
        ),
    )

    with pytest.raises(IntegrityViolation, match="depth bound"):
        validate_artifact_payload(payload_schema_id="checker-report@1", payload=payload)


def test_ir_snapshot_uses_its_schema_contract_instead_of_a_global_object_count() -> None:
    entities = {
        f"item:{index}": Entity(id=f"item:{index}", type=NodeType.ITEM)
        for index in range(payload_schema_mod.MAX_PAYLOAD_COLLECTION_ITEMS + 1)
    }
    payload = _wire(Snapshot(entities=entities, relations={}).content_payload)
    assert len(canonical_json(payload).encode("utf-8")) < payload_schema_mod.MAX_PAYLOAD_JSON_BYTES

    assert validate_artifact_payload(payload_schema_id="ir-core@1", payload=payload) == payload


def test_decode_reuses_the_verified_canonical_size_for_bounds(monkeypatch) -> None:
    payload = {
        "payload_schema_version": "checker-report@1",
        "snapshot_id": "snapshot:1",
        "findings": [],
    }
    blob = canonical_json(payload).encode("utf-8")
    real_canonical_json = payload_schema_mod.canonical_json
    calls = 0

    def counted_canonical_json(value: object) -> str:
        nonlocal calls
        calls += 1
        return real_canonical_json(value)

    monkeypatch.setattr(payload_schema_mod, "canonical_json", counted_canonical_json)

    assert (
        decode_and_validate_artifact_payload(
            payload_schema_id="checker-report@1",
            blob=blob,
        )
        == payload
    )
    # The byte comparison is the only canonical serialization needed here. The
    # schema parser compares and copies the already-decoded exact wire in memory,
    # while bounds reuse the known byte size.
    assert calls == 1


def test_decode_ir_schema_compares_the_canonical_tree_without_reencoding(monkeypatch) -> None:
    payload = _wire(
        Snapshot(
            entities={"item:1": Entity(id="item:1", type=NodeType.ITEM)},
            relations={},
        ).content_payload
    )
    blob = canonical_json(payload).encode("utf-8")
    real_canonical_json = payload_schema_mod.canonical_json
    calls = 0

    def counted_canonical_json(value: object) -> str:
        nonlocal calls
        calls += 1
        return real_canonical_json(value)

    monkeypatch.setattr(payload_schema_mod, "canonical_json", counted_canonical_json)

    assert decode_and_validate_artifact_payload(payload_schema_id="ir-core@1", blob=blob) == payload
    assert calls == 1


def test_in_memory_exact_wire_comparison_still_rejects_bool_integer_coercion() -> None:
    payload = {
        "payload_schema_version": "checker-report@1",
        "checker_profile": {"profile_id": "checker", "version": True},
        "snapshot_id": "snapshot:1",
        "checker_ids": [],
        "defect_classes": [],
        "constraint_application": [],
        "findings": [],
    }

    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(payload_schema_id="checker-report@1", payload=payload)


def test_unknown_and_non_terminal_schemas_fail_closed() -> None:
    with pytest.raises(IntegrityViolation, match="not registered"):
        validate_artifact_payload(payload_schema_id="checker-report@future", payload={})

    with pytest.raises(IntegrityViolation, match="not valid on the terminal domain"):
        validate_artifact_payload(
            payload_schema_id="bench-report@2",
            payload={"payload_schema_version": "bench-report@2"},
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


def test_checker_report_adds_profiled_standalone_variant_without_breaking_legacy_decode() -> None:
    legacy = {
        "payload_schema_version": "checker-report@1",
        "snapshot_id": "snapshot:1",
        "checker_ids": ["graph"],
        "defect_classes": [],
        "constraint_application": [],
        "findings": [],
    }
    profiled = {
        **legacy,
        "checker_profile": {"profile_id": "checker", "version": 1},
    }
    exact = {
        **profiled,
        "constraint_snapshot_binding_status": "not_applicable",
    }
    assert validate_artifact_payload(payload_schema_id="checker-report@1", payload=legacy) == legacy
    assert (
        validate_artifact_payload(payload_schema_id="checker-report@1", payload=profiled)
        == profiled
    )
    assert validate_artifact_payload(payload_schema_id="checker-report@1", payload=exact) == exact


def test_checker_companion_requires_nonempty_structured_execution_binding() -> None:
    payload = {
        "payload_schema_version": "regression-evidence@1",
        "requirement_id": "checker:checker@1",
        "dimension": "checker",
        "lineage_suite_artifact_ids": [],
        "checker_profile": {"profile_id": "checker", "version": 1},
        "checker_execution_bindings": [{"wrapper_id": "graph", "native_id": "graph"}],
        "constraint_snapshot_binding_status": "not_applicable",
        "snapshot_id": "snapshot:1",
        "status": "passed",
        "findings": [],
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
        == payload
    )

    for field, forged_value in (
        ("wrapper_id", ""),
        ("native_id", ""),
        ("wrapper_id", "   "),
        ("native_id", "   "),
        ("constraint_id", ""),
        ("constraint_id", "   "),
    ):
        forged = {
            **payload,
            "checker_execution_bindings": [
                {
                    "wrapper_id": "graph",
                    "native_id": "graph",
                    field: forged_value,
                }
            ],
        }
        with pytest.raises(IntegrityViolation, match="exact registered schema"):
            validate_artifact_payload(
                payload_schema_id="regression-evidence@1",
                payload=forged,
            )


def test_simulation_execution_binding_is_an_exact_closed_wire_shape() -> None:
    payload = _wire(
        {
            "payload_schema_version": "simulation-result@1",
            "snapshot_id": "snapshot:1",
            "seed": 7,
            "replication_count": 2,
            "horizon_steps": 4,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": {"profile_id": "simulation", "version": 1},
                    "workload_profile": {"profile_id": "workload", "version": 1},
                    "constraint_ids": [],
                    "constraint_application": {"status": "not_applicable"},
                    "scenario_application": {"status": "not_applicable"},
                }
            },
            "findings": [],
        }
    )
    assert (
        validate_artifact_payload(payload_schema_id="simulation-result@1", payload=payload)
        == payload
    )

    sensitivity = payload["sensitivity"]
    assert isinstance(sensitivity, dict)
    execution = sensitivity["execution_binding"]
    assert isinstance(execution, dict)
    execution["worker_claim"] = "trusted"
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(payload_schema_id="simulation-result@1", payload=payload)


def test_simulation_text_may_use_a_float_tag_prefix_without_becoming_numeric() -> None:
    payload = {
        "payload_schema_version": "simulation-result@1",
        "snapshot_id": "snapshot:1",
        "seed": 7,
        "replication_count": 1,
        "horizon_steps": 1,
        "invariants": [],
        "sensitivity": {"operator_note": "f:Infinity"},
        "findings": [],
    }

    assert (
        validate_artifact_payload(payload_schema_id="simulation-result@1", payload=payload)
        == payload
    )


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("sensitivity", "f:Infinity"),
        ("sensitivity", float("inf")),
        ("confidence", "f:Infinity"),
        ("confidence", float("inf")),
    ],
)
def test_simulation_numeric_fields_reject_nonfinite_canonical_values(
    location: str,
    value: object,
) -> None:
    finding = _finding().model_dump(mode="json", exclude_none=True)
    if location == "confidence":
        finding["confidence"] = value
    payload = {
        "payload_schema_version": "simulation-result@1",
        "snapshot_id": "snapshot:1",
        "seed": 7,
        "replication_count": 1,
        "horizon_steps": 1,
        "invariants": [],
        "sensitivity": (
            {"source_total": value, "sink_total": "f:0"} if location == "sensitivity" else {}
        ),
        "findings": [finding] if location == "confidence" else [],
    }

    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(payload_schema_id="simulation-result@1", payload=payload)


def test_patch_simulation_companion_binds_mode_and_complete_subseed() -> None:
    root_seed = 7
    run_kind = RunKindRef(kind="patch.validate", version=1)
    profile = ProfileRefV1(profile_id="sim", version=1)
    case_id = "simulation:sim@1"
    seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
        replication_index=0,
    )
    finding = Finding(
        id="sim:economy-collapse",
        source="sim",
        producer_id="economy_sim",
        producer_run_id="run:1",
        oracle_type="simulation",
        defect_class="economy_collapse",
        severity="major",
        snapshot_id="snapshot:1",
        status="confirmed",
        message="economy collapsed",
    )
    seed_binding = {
        "root_seed": root_seed,
        "run_kind": run_kind.model_dump(mode="json"),
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "case_id": case_id,
        "replication_index": 0,
        "seed": seed,
        "seed_derivation_version": "subseed@1",
    }
    execution_binding = {
        "binding_schema_version": "simulation-expected-finding-binding@1",
        "producer_id": "economy_sim",
        "simulation_profile": profile.model_dump(mode="json"),
        "execution_mode": "single_population@1",
        "seed_binding": seed_binding,
        "constraint_snapshot_binding_status": "not_applicable",
        "constraint_ids": [],
        "constraint_application": {"status": "not_applicable"},
        "n_agents": 6,
        "n_ticks": 12,
    }
    payload = _wire(
        {
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": case_id,
            "dimension": "simulation",
            "lineage_suite_artifact_ids": [],
            "simulation_execution_binding": execution_binding,
            "snapshot_id": finding.snapshot_id,
            "status": "failed",
            "findings": [finding.model_dump(mode="json")],
            **seed_binding,
        }
    )
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
        == payload
    )

    forged_mode = json.loads(json.dumps(payload))
    forged_mode["simulation_execution_binding"]["execution_mode"] = "replication_ensemble@1"
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=forged_mode,
        )

    other_root_seed = root_seed + 1
    other_seed = derive_validation_subseed(
        root_seed=other_root_seed,
        run_kind=run_kind,
        profile=profile,
        case_id=case_id,
        replication_index=0,
    )
    forged_outer_seed = {
        **payload,
        "root_seed": other_root_seed,
        "seed": other_seed,
    }
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=forged_outer_seed,
        )


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
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
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

    internal_field_wire = {
        **constraint_payload,
        "constraints": [constraint.model_dump(mode="json")],
    }
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="constraint-snapshot@1",
            payload=internal_field_wire,
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


def test_regression_lineage_suite_selector_matches_the_semantic_variant() -> None:
    suite_payload = {
        "payload_schema_version": "regression-evidence@1",
        "suite_artifact_id": "artifact:suite",
        "lineage_suite_artifact_ids": ["artifact:suite"],
        "snapshot_id": "snapshot:1",
        "status": "passed",
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=suite_payload,
        )
        == suite_payload
    )
    for forged in ([], ["artifact:other"], ["artifact:suite", "artifact:other"]):
        with pytest.raises(IntegrityViolation, match="exact registered schema"):
            validate_artifact_payload(
                payload_schema_id="regression-evidence@1",
                payload={**suite_payload, "lineage_suite_artifact_ids": forged},
            )

    dimension_payload = {
        "payload_schema_version": "regression-evidence@1",
        "requirement_id": "history",
        "dimension": "history",
        "lineage_suite_artifact_ids": [],
        "status": "passed",
        "detail": {"target_artifact_id": "artifact:target"},
    }
    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=dimension_payload,
        )
        == dimension_payload
    )
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload={
                **dimension_payload,
                "lineage_suite_artifact_ids": ["artifact:suite"],
            },
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


@pytest.mark.parametrize("nested", [False, True])
def test_regression_evidence_rejects_passed_status_with_confirmed_finding(
    nested: bool,
) -> None:
    finding = _finding().model_dump(mode="json")
    if nested:
        payload = {
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "regression:fixture",
            "dimension": "regression",
            "status": "passed",
            "detail": {
                "target_artifact_id": "artifact:target",
                "snapshot_id": "snapshot:1",
                "findings": [finding],
            },
        }
    else:
        payload = {
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "checker:fixture@1",
            "dimension": "checker",
            "snapshot_id": "snapshot:1",
            "status": "passed",
            "findings": [finding],
        }

    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=payload,
        )
