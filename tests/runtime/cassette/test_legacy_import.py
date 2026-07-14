from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gameforge.contracts.cassette import CassetteRecordV1
from gameforge.contracts.cassette_import import (
    LegacyCassetteInputBindingV1,
    LegacyCassettePolicyBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassetteSchemaBindingV1,
    LegacyImportVerificationPolicyRegistryV1,
    LegacyImportVerificationPolicyV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV1,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)
from gameforge.runtime.cassette.legacy_import import (
    InMemoryLegacyImportAuthority,
    InMemoryLegacyImportDecisionRepository,
    LegacyCassetteRuntimeImporter,
    LegacyImportCallCandidate,
    LegacyImportCandidate,
)
from gameforge.runtime.model_router.m4_router import VerifiedLegacyReplayRouter


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _policy() -> LegacyImportVerificationPolicyV1:
    return LegacyImportVerificationPolicyV1.create(
        policy_id="legacy-v1-import",
        policy_version=1,
        required_input_binding_keys=("source",),
        required_profile_field_paths=("/repair_policy",),
        required_policy_binding_keys=("route",),
        required_schema_binding_keys=("request",),
        max_wire_bytes_per_call=32_768,
        max_calls_per_import=8,
    )


def _snapshot() -> ModelSnapshot:
    return ModelSnapshot(
        provider="anthropic",
        model="claude-opus-4-8",
        snapshot_tag="m2a@1",
    )


def _catalog(*, catalog_version: int = 1) -> ModelCatalogSnapshotV1:
    descriptor = ModelDescriptorV1(
        provider="anthropic",
        model_snapshot=canonical_model_snapshot_id(_snapshot()),
        tier="historical",
        capabilities=("reasoning",),
        context_limit=200_000,
        max_output_tokens=16_000,
        prompt_cache_support=False,
        status="active",
    )
    payload = {
        "catalog_version": catalog_version,
        "models": (descriptor,),
        "created_at": NOW,
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


def _request(ordinal: int = 1) -> ModelRequestV1:
    return ModelRequestV1(
        model_snapshot=_snapshot(),
        messages=[Message(role="user", content=f"repair case {ordinal}")],
        params={"temperature": 0},
        agent_node_id="repair-drafter",
        prompt_version="repair@1",
    )


def _wire(request: ModelRequestV1, ordinal: int = 1) -> str:
    record = CassetteRecordV1(
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=request.model_snapshot,
        response=ModelResponse(
            response_normalized=f"answer-{ordinal}",
            raw_response={"id": f"response-{ordinal}"},
            latency_ms=0,
            token_usage={},
            finish_reason="stop",
        ),
        transport_attempts=1,
        transport_retries=0,
        recorded_at="2026-07-10T00:00:00Z",
    )
    return json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def _bindings() -> tuple[
    LegacyCassetteInputBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassettePolicyBindingV1,
    LegacyCassetteSchemaBindingV1,
]:
    return (
        LegacyCassetteInputBindingV1(
            binding_key="source",
            artifact_id="artifact:source",
            payload_hash="3" * 64,
            version_tuple=VersionTuple(doc_version="source@1"),
        ),
        LegacyCassetteProfileBindingV1(
            field_path="/repair_policy",
            profile_id="repair-profile",
            profile_version=1,
            profile_payload_hash="4" * 64,
            catalog_version=1,
            catalog_digest="5" * 64,
        ),
        LegacyCassettePolicyBindingV1(
            binding_key="route",
            policy_kind="routing",
            policy_id="historical-route",
            policy_version=1,
            policy_digest="6" * 64,
        ),
        LegacyCassetteSchemaBindingV1(
            binding_key="request",
            schema_id="model-router@1",
        ),
    )


def _frozen_tuple() -> VersionTuple:
    return VersionTuple(
        prompt_version="repair@1",
        model_snapshot=canonical_model_snapshot_id(_snapshot()),
        agent_graph_version="agents@2",
        tool_version="gameforge@0.0.0",
    )


def _candidate(
    *, input_binding: LegacyCassetteInputBindingV1 | None = None
) -> LegacyImportCandidate:
    source, profile, policy_binding, schema = _bindings()
    request = _request()
    catalog = _catalog()
    return LegacyImportCandidate(
        source_suite_id="m2-repair",
        source_case_id="repair-case",
        verification_policy=_policy().ref(),
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
        input_artifact_bindings=(input_binding or source,),
        execution_profile_bindings=(profile,),
        policy_bindings=(policy_binding,),
        schema_bindings=(schema,),
        calls=(
            LegacyImportCallCandidate(
                original_wire_utf8=_wire(request),
                rendered_request_artifact_id="artifact:request:1",
                source_call_ordinal=1,
            ),
        ),
        importer_tool_version="gameforge@0.0.0",
    )


def _authority(
    *,
    include_request: bool = True,
    input_binding: LegacyCassetteInputBindingV1 | None = None,
) -> InMemoryLegacyImportAuthority:
    source, profile, policy_binding, schema = _bindings()
    policy = _policy()
    catalog = _catalog()
    return InMemoryLegacyImportAuthority(
        verification_policy_registry=LegacyImportVerificationPolicyRegistryV1.create(
            registry_version=1,
            policies=(policy,),
        ),
        model_catalogs={(catalog.catalog_version, catalog.catalog_digest): catalog},
        input_bindings={(source.binding_key, source.artifact_id): input_binding or source},
        profile_bindings={
            (profile.field_path, profile.profile_id, profile.profile_version): profile
        },
        policy_bindings={
            (
                policy_binding.binding_key,
                policy_binding.policy_kind,
                policy_binding.policy_id,
                policy_binding.policy_version,
            ): policy_binding
        },
        schema_bindings={(schema.binding_key, schema.schema_id): schema},
        rendered_requests=({"artifact:request:1": _request()} if include_request else {}),
        frozen_version_tuples={("m2-repair", "repair-case"): _frozen_tuple()},
        call_tool_versions={("m2-repair", "repair-case", 1): "gameforge@0.0.0"},
    )


def _finalize(importer: LegacyCassetteRuntimeImporter, prepared, repository):
    return importer.finalize(
        prepared,
        record_shard_artifact_ids=("artifact:shard:1",),
        attempt_bundle_artifact_id="artifact:attempt:1",
        decision_repository=repository,
    )


def test_importer_derives_verified_manifest_and_persists_legacy_decision() -> None:
    importer = LegacyCassetteRuntimeImporter(_authority())
    prepared = importer.prepare(_candidate())
    repository = InMemoryLegacyImportDecisionRepository()

    tree = _finalize(importer, prepared, repository)

    assert prepared.status == "verified"
    assert tree.status == "verified"
    assert tree.root.run_id is None
    assert tree.root.legacy_run_import_manifest is not None
    assert tree.root.legacy_run_import_manifest.status == "verified"
    assert len(repository.decisions) == 1
    decision = next(iter(repository.decisions.values()))
    assert decision.decision_id.startswith("legacy-import-route:sha256:")
    assert prepared.evidences[0].verification_status == "verified"
    assert prepared.evidences[0].import_routing_decision == decision

    call = tree.replay_source.replay(_request(), call_ordinal=1)
    assert call.record.response.response_normalized == "answer-1"
    assert call.routing_decision == decision
    assert call.current_transport_attempt_count == 0
    assert call.recorded_transport_attempt_count == 1
    assert call.observation.latency.status == "unavailable"

    manifest = tree.root.legacy_run_import_manifest
    assert manifest is not None and tree.replay_source is not None
    result = VerifiedLegacyReplayRouter(
        source=tree.replay_source,
        expected_import_id=manifest.import_id,
    ).call(_request(), call_ordinal=1)
    assert result.execution_source == "cassette_replay"
    assert result.routing_decision_kind == "legacy_import"
    assert result.routing_decision_id == decision.decision_id
    assert result.invocation == call.invocation
    assert result.transport_attempt_count == 0
    assert result.recorded_transport_attempt_count == 1


def test_importer_rejects_self_consistent_claim_that_differs_from_authority() -> None:
    source, _, _, _ = _bindings()
    claimed = source.model_copy(update={"payload_hash": "9" * 64})
    importer = LegacyCassetteRuntimeImporter(_authority(input_binding=source))

    with pytest.raises(IntegrityViolation, match="input binding differs"):
        importer.prepare(_candidate(input_binding=claimed))


def test_missing_rendered_request_produces_non_executable_evidence_missing_tree() -> None:
    importer = LegacyCassetteRuntimeImporter(_authority(include_request=False))
    prepared = importer.prepare(_candidate())
    repository = InMemoryLegacyImportDecisionRepository()

    tree = _finalize(importer, prepared, repository)

    assert prepared.status == "evidence_missing"
    assert prepared.evidences[0].verification_status == "evidence_missing"
    assert tree.status == "evidence_missing"
    assert tree.replay_source is None
    assert repository.decisions == {}
    with pytest.raises(IntegrityViolation, match="not executable"):
        importer.read_verified(
            root=tree.root,
            child_bundles_by_artifact_id=tree.child_bundles_by_artifact_id,
            model_catalog_version=_catalog().catalog_version,
            model_catalog_digest=_catalog().catalog_digest,
            decision_repository=repository,
        )


def test_reader_revalidates_authorities_and_requires_persisted_decisions() -> None:
    authority = _authority()
    importer = LegacyCassetteRuntimeImporter(authority)
    repository = InMemoryLegacyImportDecisionRepository()
    tree = _finalize(importer, importer.prepare(_candidate()), repository)

    source = importer.read_verified(
        root=tree.root,
        child_bundles_by_artifact_id=tree.child_bundles_by_artifact_id,
        model_catalog_version=_catalog().catalog_version,
        model_catalog_digest=_catalog().catalog_digest,
        decision_repository=repository,
    )
    assert source.replay(_request(), call_ordinal=1).record.request_hash == request_hash(_request())

    empty_repository = InMemoryLegacyImportDecisionRepository()
    with pytest.raises(IntegrityViolation, match="not retained"):
        importer.read_verified(
            root=tree.root,
            child_bundles_by_artifact_id=tree.child_bundles_by_artifact_id,
            model_catalog_version=_catalog().catalog_version,
            model_catalog_digest=_catalog().catalog_digest,
            decision_repository=empty_repository,
        )


def test_missing_authoritative_manifest_binding_never_becomes_verified() -> None:
    authority = _authority()
    authority.input_bindings.clear()
    importer = LegacyCassetteRuntimeImporter(authority)

    prepared = importer.prepare(_candidate())

    assert prepared.status == "evidence_missing"
    assert prepared.manifest.status == "evidence_missing"
    assert prepared.manifest.execution_identity is None
    assert prepared.manifest.frozen_version_tuple is None


def test_missing_retained_policy_or_catalog_is_integrity_failure() -> None:
    authority = _authority()
    authority.verification_policy_registry = LegacyImportVerificationPolicyRegistryV1.create(
        registry_version=2,
        policies=(),
    )
    with pytest.raises(IntegrityViolation, match="not retained"):
        LegacyCassetteRuntimeImporter(authority).prepare(_candidate())

    authority = _authority()
    authority.model_catalogs.clear()
    with pytest.raises(IntegrityViolation, match="model catalog"):
        LegacyCassetteRuntimeImporter(authority).prepare(_candidate())


def test_import_policy_limits_are_enforced_before_wire_parsing() -> None:
    candidate = _candidate()
    oversized_call = replace(
        candidate.calls[0],
        original_wire_utf8="not-json" * 4_097,
    )

    with pytest.raises(IntegrityViolation, match="exceeds verification policy"):
        LegacyCassetteRuntimeImporter(_authority()).prepare(
            replace(candidate, calls=(oversized_call,))
        )

    calls = tuple(
        LegacyImportCallCandidate(
            original_wire_utf8=_wire(_request(ordinal), ordinal),
            rendered_request_artifact_id=f"artifact:request:{ordinal}",
            source_call_ordinal=ordinal,
        )
        for ordinal in range(1, 10)
    )
    with pytest.raises(IntegrityViolation, match="call count"):
        LegacyCassetteRuntimeImporter(_authority()).prepare(replace(candidate, calls=calls))


def test_zero_call_import_is_diagnostic_and_non_executable() -> None:
    importer = LegacyCassetteRuntimeImporter(_authority())
    prepared = importer.prepare(replace(_candidate(), calls=()))
    repository = InMemoryLegacyImportDecisionRepository()

    tree = importer.finalize(
        prepared,
        record_shard_artifact_ids=(),
        attempt_bundle_artifact_id="artifact:attempt:empty",
        decision_repository=repository,
    )

    assert prepared.status == "evidence_missing"
    assert tree.status == "evidence_missing"
    assert tree.replay_source is None
    assert repository.decisions == {}


def test_finalize_revalidates_authority_before_persisting_decisions() -> None:
    authority = _authority()
    importer = LegacyCassetteRuntimeImporter(authority)
    prepared = importer.prepare(_candidate())
    repository = InMemoryLegacyImportDecisionRepository()
    authority.rendered_requests.clear()

    with pytest.raises(IntegrityViolation, match="no longer retained"):
        _finalize(importer, prepared, repository)

    assert repository.decisions == {}


def test_catalog_resolver_must_return_the_exact_requested_snapshot() -> None:
    authority = _authority()
    requested = _catalog()
    authority.model_catalogs[
        (
            requested.catalog_version,
            requested.catalog_digest,
        )
    ] = _catalog(catalog_version=2)

    with pytest.raises(IntegrityViolation, match="different snapshot"):
        LegacyCassetteRuntimeImporter(authority).prepare(_candidate())


def test_frozen_tool_version_must_close_against_call_invocation() -> None:
    authority = _authority()
    authority.frozen_version_tuples[("m2-repair", "repair-case")] = _frozen_tuple().model_copy(
        update={"tool_version": "different-tool@1"}
    )

    with pytest.raises(IntegrityViolation, match="tool version differs"):
        LegacyCassetteRuntimeImporter(authority).prepare(_candidate())
