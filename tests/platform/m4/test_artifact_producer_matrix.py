from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

import pytest

from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import (
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.lineage import (
    Artifact,
    ArtifactKind,
    ArtifactV2,
    InvocationVersionBindingV1,
    VersionTuple,
    build_artifact_v2,
    build_execution_identity,
    object_ref_for_bytes,
)
from gameforge.platform.lineage.validation import (
    PRODUCER_RULES,
    ProducerValidationContext,
    validate_artifact_producer,
)


_PAYLOAD = b'{"fixture":"producer-matrix"}'
_CASSETTE_ID = f"sha256:{sha256_lowerhex(_PAYLOAD)}"


@dataclass(frozen=True)
class _Case:
    versions: dict[str, str | int | None]
    expected_versions: dict[str, str | int | None] | None = None
    llm_execution_mode: str = "not_applicable"
    meta: dict[str, Any] | None = None


_MINIMAL_CASES: dict[str, _Case] = {
    "source_raw": _Case({"doc_version": "doc@1"}),
    "source_rendered": _Case(
        {"doc_version": "doc@1", "tool_version": "renderer@1"},
        {"doc_version": "doc@1"},
    ),
    "ir_snapshot": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "extractor@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "constraint_snapshot": _Case(
        {"constraint_snapshot_id": "sha256:constraint", "tool_version": "compiler@1"},
        {"constraint_snapshot_id": "sha256:constraint"},
    ),
    "constraint_proposal": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "author@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "config_export": _Case(
        {
            "ir_snapshot_id": "sha256:ir",
            "constraint_snapshot_id": "sha256:constraint",
            "tool_version": "adapter@1",
        },
        {
            "ir_snapshot_id": "sha256:ir",
            "constraint_snapshot_id": "sha256:constraint",
        },
    ),
    "scenario_spec": _Case({"tool_version": "scenario@1"}, {}),
    "task_suite": _Case({"tool_version": "suite@1"}, {}),
    "regression_suite": _Case({"tool_version": "suite@1"}, {}),
    "golden_suite": _Case({"tool_version": "suite@1"}, {}),
    "bench_dataset": _Case({"tool_version": "bench-data@1"}, {}),
    "benchmark_spec": _Case({"tool_version": "bench-spec@1"}),
    "review_report": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "review@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "checker_run": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "checker@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "simulation_run": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "sim@1", "seed": 7},
        {"ir_snapshot_id": "sha256:ir", "seed": 7},
    ),
    "playtest_trace": _Case(
        {
            "ir_snapshot_id": "sha256:ir",
            "constraint_snapshot_id": "sha256:constraint",
            "tool_version": "playtest@1",
            "env_contract_version": "env@1",
            "seed": 7,
        },
        {
            "ir_snapshot_id": "sha256:ir",
            "constraint_snapshot_id": "sha256:constraint",
            "env_contract_version": "env@1",
            "seed": 7,
        },
    ),
    "patch": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "patch@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "validation_evidence": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "validate@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "regression_evidence": _Case(
        {"ir_snapshot_id": "sha256:ir", "tool_version": "regress@1"},
        {"ir_snapshot_id": "sha256:ir"},
    ),
    "rollback_request": _Case({"tool_version": "rollback@1"}, {}),
    "run_result": _Case({"tool_version": "runner@1"}),
    "run_failure": _Case({"tool_version": "runner@1"}),
    "cassette_bundle": _Case(
        {"tool_version": "cassette@2", "cassette_id": _CASSETTE_ID},
        llm_execution_mode="record",
        meta={"replayability": "cassette_replay"},
    ),
    "migration_report": _Case({"tool_version": "migrator@1"}, {}),
    "bench_report": _Case({"tool_version": "bench-report@1"}, {}),
    "operational_evidence": _Case({"tool_version": "ops@1"}, {}),
}

_BASE_REQUIRED: dict[str, frozenset[str]] = {
    "source_raw": frozenset({"doc_version"}),
    "source_rendered": frozenset({"doc_version", "tool_version"}),
    "ir_snapshot": frozenset({"ir_snapshot_id", "tool_version"}),
    "constraint_snapshot": frozenset({"constraint_snapshot_id", "tool_version"}),
    "constraint_proposal": frozenset({"tool_version"}),
    "config_export": frozenset({"ir_snapshot_id", "constraint_snapshot_id", "tool_version"}),
    "scenario_spec": frozenset({"tool_version"}),
    "task_suite": frozenset({"tool_version"}),
    "regression_suite": frozenset({"tool_version"}),
    "golden_suite": frozenset({"tool_version"}),
    "bench_dataset": frozenset({"tool_version"}),
    "benchmark_spec": frozenset({"tool_version"}),
    "review_report": frozenset({"ir_snapshot_id", "tool_version"}),
    "checker_run": frozenset({"ir_snapshot_id", "tool_version"}),
    "simulation_run": frozenset({"ir_snapshot_id", "tool_version", "seed"}),
    "playtest_trace": frozenset(
        {
            "ir_snapshot_id",
            "constraint_snapshot_id",
            "tool_version",
            "env_contract_version",
            "seed",
        }
    ),
    "patch": frozenset({"ir_snapshot_id", "tool_version"}),
    "validation_evidence": frozenset({"tool_version"}),
    "regression_evidence": frozenset({"tool_version"}),
    "rollback_request": frozenset({"tool_version"}),
    "run_result": frozenset({"tool_version"}),
    "run_failure": frozenset({"tool_version"}),
    "cassette_bundle": frozenset({"tool_version", "cassette_id"}),
    "migration_report": frozenset({"tool_version"}),
    "bench_report": frozenset({"tool_version"}),
    "operational_evidence": frozenset({"tool_version"}),
}


def _artifact(
    kind: str,
    versions: dict[str, str | int | None],
    *,
    meta: dict[str, Any] | None = None,
    payload: bytes = _PAYLOAD,
    lineage: list[str] | None = None,
) -> ArtifactV2:
    ref = object_ref_for_bytes(payload)
    return build_artifact_v2(
        kind=kind,
        version_tuple=VersionTuple(**versions),
        lineage=lineage or [],
        payload_hash=ref.sha256,
        object_ref=ref,
        meta=meta or {},
    )


def _context(case: _Case, **overrides: Any) -> ProducerValidationContext:
    values: dict[str, Any] = {
        "expected_versions": case.expected_versions,
        "llm_execution_mode": case.llm_execution_mode,
    }
    values.update(overrides)
    return ProducerValidationContext(**values)


def _run_projection(
    artifact: ArtifactV2,
    *,
    manifest_scope: str = "run",
    frozen_input: VersionTuple | None = None,
    parents: tuple[RunManifestParentBindingV1, ...] | None = None,
    transition_digest: str = "2" * 64,
) -> RunManifestVersionProjectionV1:
    parent_bindings = parents
    if parent_bindings is None:
        parent_bindings = tuple(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role="input",
                publication="existing",
            )
            for artifact_id in artifact.lineage
        )
    return RunManifestVersionProjectionV1(
        manifest_scope=manifest_scope,
        attempt_no=1,
        run_kind=RunKindRef(kind="fixture.run", version=1),
        run_payload_hash="1" * 64,
        frozen_input_version_tuple=frozen_input or artifact.version_tuple,
        terminal_version_tuple=artifact.version_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="fixture-transition",
            policy_version=1,
            digest=transition_digest,
        ),
        parents=parent_bindings,
    )


def _context_for_artifact(
    kind: str,
    artifact: ArtifactV2,
    case: _Case,
) -> ProducerValidationContext:
    if kind not in {"run_result", "run_failure"}:
        return _context(case)
    projection = _run_projection(artifact)
    return _context(
        case,
        run_manifest_projection=projection,
        expected_run_manifest_projection=projection,
    )


def _violations(error: pytest.ExceptionInfo[IntegrityViolation]) -> tuple[str, ...]:
    return tuple(error.value.context["violations"])


def _binding(
    *,
    source: str = "online",
    consumed: bool = True,
) -> InvocationVersionBindingV1:
    return InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=1 if source == "online" else None,
        routing_decision_kind="native",
        routing_decision_id="route:1",
        agent_node_id="producer.node",
        prompt_version="prompt@1",
        model_snapshot="provider/model@1",
        tool_version="agent-tool@1",
        execution_source=source,
        response_consumed=consumed,
    )


def _llm_artifact(
    *,
    mode: str,
    scope: str = "artifact",
    source: str = "online",
    consumed: bool = True,
    replayability: str | None = None,
) -> ArtifactV2:
    identity = build_execution_identity(
        scope=scope,
        agent_graph_version="graph@1",
        bindings=[_binding(source=source, consumed=consumed)],
    )
    versions: dict[str, str | int | None] = {
        "ir_snapshot_id": "sha256:ir",
        "tool_version": "extractor@1",
        "prompt_version": identity.prompt_projection.tuple_value,
        "model_snapshot": identity.model_projection.tuple_value,
        "agent_graph_version": identity.agent_graph_version,
    }
    if mode in {"record", "replay"}:
        versions["cassette_id"] = "sha256:" + "1" * 64
    return _artifact(
        "ir_snapshot",
        versions,
        meta={
            "execution_identity": identity,
            "replayability": replayability
            or ("online_only" if mode == "live" else "cassette_replay"),
        },
    )


def test_matrix_covers_the_closed_artifact_kind_set_exactly() -> None:
    expected = set(get_args(ArtifactKind))
    assert set(_MINIMAL_CASES) == expected
    assert set(_BASE_REQUIRED) == expected
    assert set(PRODUCER_RULES) == expected
    for kind, required_fields in _BASE_REQUIRED.items():
        assert frozenset(PRODUCER_RULES[kind].required_fields) == required_fields


@pytest.mark.parametrize("kind", sorted(_MINIMAL_CASES))
def test_every_kind_accepts_its_minimal_complete_tuple(kind: str) -> None:
    case = _MINIMAL_CASES[kind]
    artifact = _artifact(kind, case.versions, meta=case.meta)
    report = validate_artifact_producer(
        artifact,
        _context_for_artifact(kind, artifact, case),
    )
    assert report.status == "valid"
    assert report.artifact_kind == kind
    assert report.missing_evidence == ()


@pytest.mark.parametrize(
    ("kind", "field"),
    [(kind, field) for kind, fields in sorted(_BASE_REQUIRED.items()) for field in sorted(fields)],
)
def test_every_base_required_field_fails_closed_when_missing(kind: str, field: str) -> None:
    case = _MINIMAL_CASES[kind]
    versions = {**case.versions, field: None}
    artifact = _artifact(kind, versions, meta=case.meta)
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context_for_artifact(kind, artifact, case),
        )
    assert any(field in violation for violation in _violations(error))


@pytest.mark.parametrize("kind", ["checker_run", "review_report", "simulation_run"])
def test_dsl_consumption_requires_an_exact_constraint_projection(kind: str) -> None:
    case = _MINIMAL_CASES[kind]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact(kind, case.versions, meta=case.meta),
            _context(case, uses_dsl=True),
        )
    assert any("constraint_snapshot_id" in item for item in _violations(error))

    versions = {**case.versions, "constraint_snapshot_id": "sha256:constraint"}
    expected = {**(case.expected_versions or {}), "constraint_snapshot_id": "sha256:constraint"}
    report = validate_artifact_producer(
        _artifact(kind, versions, meta=case.meta),
        _context(case, uses_dsl=True, expected_versions=expected),
    )
    assert report.status == "valid"


def test_conditional_requirement_cannot_be_satisfied_by_explicit_null() -> None:
    case = _MINIMAL_CASES["checker_run"]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact("checker_run", case.versions),
            _context(
                case,
                uses_dsl=True,
                expected_versions={
                    "ir_snapshot_id": "sha256:ir",
                    "constraint_snapshot_id": None,
                },
            ),
        )
    assert any("constraint_snapshot_id" in item for item in _violations(error))


@pytest.mark.parametrize(
    "kind",
    [
        "config_export",
        "scenario_spec",
        "task_suite",
        "regression_suite",
        "golden_suite",
        "benchmark_spec",
        "simulation_run",
    ],
)
def test_environment_consumption_requires_an_exact_environment_projection(kind: str) -> None:
    case = _MINIMAL_CASES[kind]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact(kind, case.versions, meta=case.meta),
            _context(case, uses_environment=True),
        )
    assert any("env_contract_version" in item for item in _violations(error))

    versions = {**case.versions, "env_contract_version": "env@1"}
    expected = {**(case.expected_versions or {}), "env_contract_version": "env@1"}
    assert (
        validate_artifact_producer(
            _artifact(kind, versions, meta=case.meta),
            _context(case, uses_environment=True, expected_versions=expected),
        ).status
        == "valid"
    )


def test_tool_generated_raw_source_requires_the_tool_version_conditionally() -> None:
    case = _MINIMAL_CASES["source_raw"]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact("source_raw", case.versions),
            _context(case, tool_output=True),
        )
    assert any("tool_version" in item for item in _violations(error))

    versions = {**case.versions, "tool_version": "connector-tool@1"}
    assert (
        validate_artifact_producer(
            _artifact("source_raw", versions),
            _context(case, tool_output=True),
        ).status
        == "valid"
    )


def test_rendered_prompt_evidence_binds_prompt_and_graph_without_fabricating_a_response() -> None:
    versions = {
        "doc_version": "doc@1",
        "tool_version": "renderer@1",
        "prompt_version": "prompt@1",
        "agent_graph_version": "graph@1",
    }
    artifact = _artifact(
        "source_rendered",
        versions,
        meta={"replayability": "online_only"},
    )
    context = ProducerValidationContext(
        llm_execution_mode="live",
        rendered_prompt_evidence=True,
        expected_versions={
            "doc_version": "doc@1",
            "prompt_version": "prompt@1",
            "agent_graph_version": "graph@1",
        },
    )
    assert validate_artifact_producer(artifact, context).status == "valid"

    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact(
                "source_rendered",
                {**versions, "agent_graph_version": None},
                meta={"replayability": "online_only"},
            ),
            context,
        )
    assert any("agent_graph_version" in item for item in _violations(error))


def test_live_agent_artifact_requires_causal_artifact_identity_and_online_only_marker() -> None:
    artifact = _llm_artifact(mode="live")
    context = ProducerValidationContext(
        llm_execution_mode="live",
        has_llm_invocations=True,
        produced_by_agent=True,
        expected_versions={"ir_snapshot_id": "sha256:ir"},
    )
    assert validate_artifact_producer(artifact, context).status == "valid"

    missing_identity = _artifact(
        "ir_snapshot",
        artifact.version_tuple.model_dump(),
        meta={"replayability": "online_only"},
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(missing_identity, context)
    assert any("execution_identity" in item for item in _violations(error))

    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _llm_artifact(mode="live", replayability="cassette_replay"),
            context,
        )
    assert any("replayability" in item for item in _violations(error))


def test_domain_artifact_identity_rejects_run_scope_and_unconsumed_routes() -> None:
    context = ProducerValidationContext(
        llm_execution_mode="live",
        has_llm_invocations=True,
        expected_versions={"ir_snapshot_id": "sha256:ir"},
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(_llm_artifact(mode="live", scope="run"), context)
    assert any("scope" in item for item in _violations(error))

    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _llm_artifact(mode="live", consumed=False),
            context,
        )
    assert any("response_consumed" in item for item in _violations(error))


@pytest.mark.parametrize(
    ("mode", "source"),
    [("record", "online"), ("replay", "cassette_replay")],
)
def test_record_and_replay_close_cassette_identity_and_replayability(
    mode: str, source: str
) -> None:
    artifact = _llm_artifact(mode=mode, source=source)
    context = ProducerValidationContext(
        llm_execution_mode=mode,
        has_llm_invocations=True,
        produced_by_agent=True,
        expected_versions={
            "ir_snapshot_id": "sha256:ir",
            "cassette_id": "sha256:" + "1" * 64,
        },
    )
    assert validate_artifact_producer(artifact, context).status == "valid"

    versions = artifact.version_tuple.model_dump()
    versions["cassette_id"] = None
    without_cassette = _artifact("ir_snapshot", versions, meta=dict(artifact.meta))
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(without_cassette, context)
    assert any("cassette_id" in item for item in _violations(error))


def test_replay_identity_cannot_claim_an_online_execution_source() -> None:
    artifact = _llm_artifact(mode="replay", source="online")
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            ProducerValidationContext(
                llm_execution_mode="replay",
                has_llm_invocations=True,
                expected_versions={
                    "ir_snapshot_id": "sha256:ir",
                    "cassette_id": "sha256:" + "1" * 64,
                },
            ),
        )
    assert any("execution_source" in item for item in _violations(error))


def test_agent_patch_cannot_hide_behind_not_applicable_execution_mode() -> None:
    case = _MINIMAL_CASES["patch"]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact("patch", case.versions),
            _context(case, produced_by_agent=True),
        )
    assert any("produced_by_agent" in item for item in _violations(error))


def test_cassette_bundle_binds_payload_digest_and_verified_import_mode() -> None:
    case = _MINIMAL_CASES["cassette_bundle"]
    artifact = _artifact("cassette_bundle", case.versions, meta=case.meta)
    assert validate_artifact_producer(artifact, _context(case)).status == "valid"

    wrong = _artifact(
        "cassette_bundle",
        {**case.versions, "cassette_id": "sha256:" + "0" * 64},
        meta=case.meta,
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(wrong, _context(case))
    assert any("bundle_payload_hash" in item for item in _violations(error))

    replay_context = _context(case, llm_execution_mode="replay")
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(artifact, replay_context)
    assert any("verified_legacy_import" in item for item in _violations(error))

    assert (
        validate_artifact_producer(
            artifact,
            _context(
                case,
                llm_execution_mode="replay",
                verified_legacy_import=True,
            ),
        ).status
        == "valid"
    )


def test_replayability_claims_are_mode_and_observation_bound() -> None:
    case = _MINIMAL_CASES["config_export"]
    deterministic = _artifact(
        "config_export",
        case.versions,
        meta={"replayability": "deterministic_recompute"},
    )
    assert validate_artifact_producer(deterministic, _context(case)).status == "valid"

    false_replay = _artifact(
        "config_export",
        case.versions,
        meta={"replayability": "cassette_replay"},
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(false_replay, _context(case))
    assert any("replayability" in item for item in _violations(error))

    ops_case = _MINIMAL_CASES["operational_evidence"]
    observation = _artifact(
        "operational_evidence",
        ops_case.versions,
        meta={"replayability": "operational_observation"},
    )
    with pytest.raises(IntegrityViolation):
        validate_artifact_producer(observation, _context(ops_case))
    assert (
        validate_artifact_producer(
            observation,
            _context(ops_case, operational_observation=True),
        ).status
        == "valid"
    )


@pytest.mark.parametrize("marker", [[], {"mode": "online_only"}])
def test_malformed_replayability_metadata_is_a_typed_integrity_failure(
    marker: object,
) -> None:
    case = _MINIMAL_CASES["config_export"]
    artifact = _artifact(
        "config_export",
        case.versions,
        meta={"replayability": marker},
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(artifact, _context(case))
    assert any("replayability" in item for item in _violations(error))


def test_projection_is_exact_and_never_mechanically_merges_unsupported_fields() -> None:
    case = _MINIMAL_CASES["config_export"]
    artifact = _artifact("config_export", case.versions)
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                expected_versions={
                    "ir_snapshot_id": "sha256:different",
                    "constraint_snapshot_id": "sha256:constraint",
                },
            ),
        )
    assert any("expected_versions.ir_snapshot_id" in item for item in _violations(error))

    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                expected_versions={
                    **(case.expected_versions or {}),
                    "model_snapshot": "unrelated-parent-model",
                },
            ),
        )
    assert any("unsupported projection" in item for item in _violations(error))


def test_config_export_accepts_and_checks_exact_preview_document_projection() -> None:
    case = _MINIMAL_CASES["config_export"]
    artifact = _artifact(
        "config_export",
        {**case.versions, "doc_version": "design-doc@7"},
    )
    expected = {
        **(case.expected_versions or {}),
        "doc_version": "design-doc@7",
    }

    assert (
        validate_artifact_producer(
            artifact,
            _context(case, expected_versions=expected),
        ).status
        == "valid"
    )

    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                expected_versions={**expected, "doc_version": "another-doc@1"},
            ),
        )
    assert any("expected_versions.doc_version" in item for item in _violations(error))


def test_inherited_base_fields_cannot_be_bypassed_by_an_empty_projection() -> None:
    case = _MINIMAL_CASES["config_export"]
    artifact = _artifact("config_export", case.versions)
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(case, expected_versions={}),
        )
    violations = _violations(error)
    assert any("ir_snapshot_id" in item for item in violations)
    assert any("constraint_snapshot_id" in item for item in violations)


def test_patch_requires_its_exact_ir_base_projection() -> None:
    case = _MINIMAL_CASES["patch"]
    artifact = _artifact("patch", case.versions)
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                expected_versions={"doc_version": None},
            ),
        )
    assert any("ir_snapshot_id" in item for item in _violations(error))


def test_projection_required_kinds_reject_missing_projection_evidence() -> None:
    case = _MINIMAL_CASES["migration_report"]
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact("migration_report", case.versions),
            ProducerValidationContext(),
        )
    assert any("version projection evidence" in item for item in _violations(error))


def test_run_manifest_requires_exact_typed_projection_evidence() -> None:
    case = _MINIMAL_CASES["run_result"]
    artifact = _artifact("run_result", case.versions)
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(artifact, _context(case))
    assert any("RunManifestVersionProjectionV1" in item for item in _violations(error))


def test_run_manifest_closes_terminal_tuple_lineage_scope_and_transition_ref() -> None:
    case = _MINIMAL_CASES["run_result"]
    artifact = _artifact(
        "run_result",
        case.versions,
        lineage=["artifact:input"],
    )
    projection = _run_projection(artifact)

    wrong_terminal = projection.model_copy(
        update={"terminal_version_tuple": VersionTuple(tool_version="different-tool@1")}
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                run_manifest_projection=wrong_terminal,
                expected_run_manifest_projection=wrong_terminal,
            ),
        )
    assert any("terminal_version_tuple" in item for item in _violations(error))

    no_parents = projection.model_copy(update={"parents": ()})
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                run_manifest_projection=no_parents,
                expected_run_manifest_projection=no_parents,
            ),
        )
    assert any("lineage" in item for item in _violations(error))

    attempt_projection = _run_projection(artifact, manifest_scope="attempt")
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                run_manifest_projection=attempt_projection,
                expected_run_manifest_projection=attempt_projection,
            ),
        )
    assert any("run_result" in item and "scope" in item for item in _violations(error))

    expected_projection = projection.model_copy(
        update={
            "version_transition_policy_ref": VersionTransitionPolicyRefV1(
                policy_id="fixture-transition",
                policy_version=1,
                digest="3" * 64,
            )
        }
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                run_manifest_projection=projection,
                expected_run_manifest_projection=expected_projection,
            ),
        )
    assert any("transition policy ref" in item for item in _violations(error))


def test_attempt_run_failure_closes_parent_attempt_scope() -> None:
    case = _MINIMAL_CASES["run_failure"]
    parent_id = "artifact:attempt-evidence"
    artifact = _artifact(
        "run_failure",
        case.versions,
        lineage=[parent_id],
    )
    valid_parent = RunManifestParentBindingV1(
        artifact_id=parent_id,
        role="evidence",
        publication="run_published",
        attempt_no=1,
    )
    projection = _run_projection(
        artifact,
        manifest_scope="attempt",
        parents=(valid_parent,),
    )
    context = _context(
        case,
        run_manifest_projection=projection,
        expected_run_manifest_projection=projection,
    )
    assert validate_artifact_producer(artifact, context).status == "valid"

    wrong_parent = valid_parent.model_copy(update={"attempt_no": 2})
    wrong_projection = _run_projection(
        artifact,
        manifest_scope="attempt",
        parents=(wrong_parent,),
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            artifact,
            _context(
                case,
                run_manifest_projection=wrong_projection,
                expected_run_manifest_projection=wrong_projection,
            ),
        )
    assert any("parent attempt_no" in item for item in _violations(error))


def _zero_call_record_manifest(
    *,
    prompt_version: str | None,
    model_snapshot: str | None,
    frozen_agent_graph: str | None,
    terminal_agent_graph: str | None,
) -> tuple[ArtifactV2, ProducerValidationContext]:
    case = _MINIMAL_CASES["run_result"]
    cassette_id = "sha256:" + "4" * 64
    terminal = VersionTuple(
        tool_version="runner@1",
        prompt_version=prompt_version,
        model_snapshot=model_snapshot,
        agent_graph_version=terminal_agent_graph,
        cassette_id=cassette_id,
    )
    frozen = VersionTuple(
        tool_version="runner@1",
        agent_graph_version=frozen_agent_graph,
    )
    cassette_artifact_id = "artifact:cassette-run-bundle"
    artifact = _artifact(
        "run_result",
        terminal.model_dump(),
        meta={"replayability": "cassette_replay"},
        lineage=[cassette_artifact_id],
    )
    projection = _run_projection(
        artifact,
        frozen_input=frozen,
        parents=(
            RunManifestParentBindingV1(
                artifact_id=cassette_artifact_id,
                role="intermediate",
                publication="run_published",
                cassette_scope="run_bundle",
            ),
        ),
    )
    context = _context(
        case,
        llm_execution_mode="record",
        expected_versions=None,
        run_manifest_projection=projection,
        expected_run_manifest_projection=projection,
    )
    return artifact, context


def test_zero_call_run_manifest_rejects_prompt_and_model_without_identity() -> None:
    artifact, context = _zero_call_record_manifest(
        prompt_version="fabricated-prompt",
        model_snapshot="fabricated-model",
        frozen_agent_graph="graph@1",
        terminal_agent_graph="graph@1",
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(artifact, context)
    violations = _violations(error)
    assert any("prompt_version" in item for item in violations)
    assert any("model_snapshot" in item for item in violations)


def test_zero_call_agent_graph_must_copy_the_exact_frozen_plan_projection() -> None:
    artifact, context = _zero_call_record_manifest(
        prompt_version=None,
        model_snapshot=None,
        frozen_agent_graph="graph@1",
        terminal_agent_graph="graph@1",
    )
    assert validate_artifact_producer(artifact, context).status == "valid"

    changed, changed_context = _zero_call_record_manifest(
        prompt_version=None,
        model_snapshot=None,
        frozen_agent_graph="graph@1",
        terminal_agent_graph="fabricated-graph",
    )
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(changed, changed_context)
    assert any("agent_graph_version" in item for item in _violations(error))


@pytest.mark.parametrize(
    "kind",
    ["constraint_proposal", "validation_evidence", "regression_evidence"],
)
def test_subject_bound_artifacts_require_a_real_snapshot_projection(kind: str) -> None:
    case = _MINIMAL_CASES[kind]
    versions = {
        **case.versions,
        "doc_version": None,
        "ir_snapshot_id": None,
        "constraint_snapshot_id": None,
    }
    with pytest.raises(IntegrityViolation) as error:
        validate_artifact_producer(
            _artifact(kind, versions),
            _context(case, expected_versions={}),
        )
    assert any("at least one" in item for item in _violations(error))


def test_legacy_artifact_reports_evidence_missing_without_mutation_or_inference() -> None:
    legacy = Artifact(
        artifact_id="legacy-artifact",
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id="sha256:legacy"),
        payload_hash="sha256:legacy-wire-value",
        meta={"historical": True},
    )
    before = legacy.model_dump(mode="json")

    report = validate_artifact_producer(legacy, ProducerValidationContext())

    assert report.status == "evidence_missing"
    assert report.artifact_kind == "ir_snapshot"
    assert report.missing_evidence == (
        "/object_ref",
        "/producer_version_projection",
        "/meta/execution_identity",
    )
    assert legacy.model_dump(mode="json") == before
    assert "object_ref" not in legacy.model_dump(mode="json")
