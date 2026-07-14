from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cassette import CassetteRecordV1
from gameforge.contracts.cassette_import import (
    CassetteBundleV1,
    LegacyCassetteCallImportEvidenceV1,
    LegacyCassetteInputBindingV1,
    LegacyCassettePolicyBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassetteRunImportManifestV1,
    LegacyCassetteSchemaBindingV1,
    LegacyImportRoutingDecisionV1,
    LegacyImportVerificationPolicyRefV1,
    LegacyImportVerificationPolicyRegistryV1,
    LegacyImportVerificationPolicyV1,
    build_legacy_import_manifest,
    compute_legacy_profile_binding_digest,
    original_wire_sha256,
    require_verified_legacy_import_bundle_tree,
    resolve_legacy_import_verification_policy,
    validate_legacy_import_bundle_tree,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import (
    InvocationVersionBindingV1,
    VersionTuple,
    build_execution_identity,
)
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


def _registry(policy: LegacyImportVerificationPolicyV1) -> LegacyImportVerificationPolicyRegistryV1:
    return LegacyImportVerificationPolicyRegistryV1.create(
        registry_version=1,
        policies=(policy,),
    )


def _model_snapshot() -> ModelSnapshot:
    return ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag="m2a@1")


def _catalog() -> ModelCatalogSnapshotV1:
    descriptor = ModelDescriptorV1(
        provider="anthropic",
        model_snapshot=canonical_model_snapshot_id(_model_snapshot()),
        tier="historical",
        capabilities=("text",),
        context_limit=200_000,
        max_output_tokens=8_192,
        prompt_cache_support=False,
        status="active",
    )
    payload = {
        "catalog_schema_version": "model-catalog@1",
        "catalog_version": 1,
        "models": [descriptor.model_dump(mode="json")],
        "created_at": "2026-07-14T00:00:00Z",
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


def _request(call_ordinal: int) -> ModelRequestV1:
    return ModelRequestV1(
        model_snapshot=_model_snapshot(),
        messages=[Message(role="user", content=f"repair case {call_ordinal}")],
        params={"temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@4",
    )


def _record(request: ModelRequestV1, call_ordinal: int) -> CassetteRecordV1:
    return CassetteRecordV1(
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=request.model_snapshot,
        response=ModelResponse(
            response_normalized=f"answer-{call_ordinal}",
            raw_response={"id": f"response-{call_ordinal}"},
            latency_ms=100 + call_ordinal,
            token_usage={"input_tokens": 10, "output_tokens": 2},
            finish_reason="stop",
        ),
        transport_attempts=1,
        transport_retries=0,
        recorded_at="2026-07-10T00:00:00Z",
    )


def _wire(record: CassetteRecordV1) -> str:
    return json.dumps(record.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def _profile_binding() -> LegacyCassetteProfileBindingV1:
    return LegacyCassetteProfileBindingV1(
        field_path="/repair_policy",
        profile_id="repair-profile",
        profile_version=4,
        profile_payload_hash="1" * 64,
        catalog_version=3,
        catalog_digest="2" * 64,
    )


def _invocation(
    request: ModelRequestV1,
    decision: LegacyImportRoutingDecisionV1,
    call_ordinal: int,
) -> InvocationVersionBindingV1:
    return InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=call_ordinal,
        route_ordinal=1,
        transport_attempt=None,
        routing_decision_kind="legacy_import",
        routing_decision_id=decision.decision_id,
        agent_node_id=request.agent_node_id,
        prompt_version=request.prompt_version,
        model_snapshot=canonical_model_snapshot_id(request.model_snapshot),
        tool_version="gameforge@0.0.0",
        execution_source="cassette_replay",
        response_consumed=True,
    )


def _verified_evidence(
    *,
    policy: LegacyImportVerificationPolicyV1,
    catalog: ModelCatalogSnapshotV1,
    profile: LegacyCassetteProfileBindingV1,
    request: ModelRequestV1,
    record: CassetteRecordV1,
    call_ordinal: int,
) -> LegacyCassetteCallImportEvidenceV1:
    wire = _wire(record)
    decision = LegacyImportRoutingDecisionV1.create(
        source_wire_sha256=original_wire_sha256(wire),
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=canonical_model_snapshot_id(request.model_snapshot),
        execution_profile_binding_digests=(compute_legacy_profile_binding_digest(profile),),
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
        verification_policy=policy.ref(),
    )
    return LegacyCassetteCallImportEvidenceV1.create(
        original_wire_utf8=wire,
        rendered_request_artifact_id=f"artifact:request:{call_ordinal}",
        request_hash=request_hash(request),
        import_routing_decision=decision,
        invocation=_invocation(request, decision, call_ordinal),
        source_suite_id="m2-repair",
        source_case_id="repair-case",
        source_call_ordinal=call_ordinal,
        importer_tool_version="gameforge@0.0.0",
        verification_status="verified",
        missing_fields=(),
    )


def _bindings() -> tuple[
    tuple[LegacyCassetteInputBindingV1, ...],
    tuple[LegacyCassettePolicyBindingV1, ...],
    tuple[LegacyCassetteSchemaBindingV1, ...],
]:
    return (
        (
            LegacyCassetteInputBindingV1(
                binding_key="source",
                artifact_id="artifact:source",
                payload_hash="3" * 64,
                version_tuple=VersionTuple(doc_version="source@1"),
            ),
        ),
        (
            LegacyCassettePolicyBindingV1(
                binding_key="route",
                policy_kind="routing",
                policy_id="historical-route",
                policy_version=1,
                policy_digest="4" * 64,
            ),
        ),
        (LegacyCassetteSchemaBindingV1(binding_key="request", schema_id="model-router@1"),),
    )


def _manifest(
    policy: LegacyImportVerificationPolicyV1,
    evidences: tuple[LegacyCassetteCallImportEvidenceV1, ...],
    profile: LegacyCassetteProfileBindingV1,
) -> LegacyCassetteRunImportManifestV1:
    input_bindings, policy_bindings, schema_bindings = _bindings()
    identity = build_execution_identity(
        scope="run",
        bindings=tuple(evidence.invocation for evidence in evidences if evidence.invocation),
        agent_graph_version="agents@2",
    )
    return build_legacy_import_manifest(
        source_suite_id="m2-repair",
        source_case_id="repair-case",
        verification_policy=policy.ref(),
        input_artifact_bindings=input_bindings,
        execution_profile_bindings=(profile,),
        frozen_version_tuple=VersionTuple(
            prompt_version=identity.prompt_projection.tuple_value,
            model_snapshot=identity.model_projection.tuple_value,
            agent_graph_version=identity.agent_graph_version,
            tool_version="gameforge@0.0.0",
        ),
        policy_bindings=policy_bindings,
        schema_bindings=schema_bindings,
        ordered_call_evidence_digests=tuple(item.evidence_digest for item in evidences),
        execution_identity=identity,
        importer_tool_version="gameforge@0.0.0",
        status="verified",
    )


def _tree(
    policy: LegacyImportVerificationPolicyV1,
    catalog: ModelCatalogSnapshotV1,
    call_count: int = 2,
) -> tuple[
    CassetteBundleV1,
    dict[str, CassetteBundleV1],
    tuple[ModelRequestV1, ...],
    tuple[InvocationVersionBindingV1, ...],
]:
    profile = _profile_binding()
    requests = tuple(_request(ordinal) for ordinal in range(1, call_count + 1))
    records = tuple(_record(request, ordinal) for ordinal, request in enumerate(requests, 1))
    evidences = tuple(
        _verified_evidence(
            policy=policy,
            catalog=catalog,
            profile=profile,
            request=request,
            record=record,
            call_ordinal=ordinal,
        )
        for ordinal, (request, record) in enumerate(zip(requests, records, strict=True), 1)
    )
    shard_ids = tuple(f"artifact:shard:{ordinal}" for ordinal in range(1, call_count + 1))
    children: dict[str, CassetteBundleV1] = {
        shard_id: CassetteBundleV1(
            scope="record_shard",
            attempt_no=1,
            ordinal=ordinal,
            records=(record,),
            legacy_call_import_evidence=evidence,
        )
        for ordinal, (shard_id, record, evidence) in enumerate(
            zip(shard_ids, records, evidences, strict=True),
            1,
        )
    }
    children["artifact:attempt:1"] = CassetteBundleV1(
        scope="attempt",
        attempt_no=1,
        child_bundle_artifact_ids=shard_ids,
        outcome_code="succeeded",
    )
    root = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=("artifact:attempt:1",),
        outcome_code="succeeded",
        legacy_run_import_manifest=_manifest(policy, evidences, profile),
    )
    return (
        root,
        children,
        requests,
        tuple(evidence.invocation for evidence in evidences if evidence.invocation),
    )


def test_policy_registry_is_content_addressed_and_resolves_exact_ref() -> None:
    policy = _policy()
    registry = _registry(policy)

    assert policy.required_input_binding_keys == ("source",)
    assert policy.policy_digest == canonical_sha256(
        policy.model_dump(mode="json", exclude={"policy_digest"})
    )
    assert registry.registry_digest == canonical_sha256(
        registry.model_dump(mode="json", exclude={"registry_digest"})
    )
    assert resolve_legacy_import_verification_policy(registry, policy.ref()) == policy

    with pytest.raises(IntegrityViolation, match="not retained"):
        resolve_legacy_import_verification_policy(
            registry,
            LegacyImportVerificationPolicyRefV1(
                policy_id=policy.policy_id,
                policy_version=2,
                policy_digest="0" * 64,
            ),
        )
    with pytest.raises(ValidationError, match="policy_digest"):
        LegacyImportVerificationPolicyV1(
            **policy.model_dump(exclude={"policy_digest"}),
            policy_digest="0" * 64,
        )


def test_verified_call_evidence_closes_wire_route_request_and_invocation() -> None:
    policy = _policy()
    catalog = _catalog()
    profile = _profile_binding()
    request = _request(1)
    record = _record(request, 1)
    evidence = _verified_evidence(
        policy=policy,
        catalog=catalog,
        profile=profile,
        request=request,
        record=record,
        call_ordinal=1,
    )

    assert evidence.original_wire_sha256 == original_wire_sha256(evidence.original_wire_utf8)
    assert evidence.evidence_digest == canonical_sha256(
        evidence.model_dump(mode="json", exclude={"evidence_digest"})
    )
    assert evidence.import_routing_decision is not None
    expected_decision_id = "legacy-import-route:sha256:" + canonical_sha256(
        evidence.import_routing_decision.model_dump(mode="json", exclude={"decision_id"})
    )
    assert evidence.import_routing_decision.decision_id == expected_decision_id

    with pytest.raises(ValidationError, match="verified evidence"):
        LegacyCassetteCallImportEvidenceV1.create(
            original_wire_utf8=evidence.original_wire_utf8,
            rendered_request_artifact_id=None,
            request_hash=None,
            import_routing_decision=None,
            invocation=None,
            source_suite_id="m2-repair",
            source_case_id="repair-case",
            source_call_ordinal=1,
            importer_tool_version="gameforge@0.0.0",
            verification_status="verified",
            missing_fields=("/rendered_request_artifact_id",),
        )


def test_evidence_missing_is_diagnostic_and_never_executable() -> None:
    record = _record(_request(1), 1)
    evidence = LegacyCassetteCallImportEvidenceV1.create(
        original_wire_utf8=_wire(record),
        rendered_request_artifact_id=None,
        request_hash=None,
        import_routing_decision=None,
        invocation=None,
        source_suite_id="m2-repair",
        source_case_id="repair-case",
        source_call_ordinal=1,
        importer_tool_version="gameforge@0.0.0",
        verification_status="evidence_missing",
        missing_fields=("/request_hash", "/rendered_request_artifact_id"),
    )
    policy = _policy()
    profile = _profile_binding()
    input_bindings, policy_bindings, schema_bindings = _bindings()
    manifest = build_legacy_import_manifest(
        source_suite_id="m2-repair",
        source_case_id="repair-case",
        verification_policy=policy.ref(),
        input_artifact_bindings=input_bindings,
        execution_profile_bindings=(profile,),
        frozen_version_tuple=None,
        policy_bindings=policy_bindings,
        schema_bindings=schema_bindings,
        ordered_call_evidence_digests=(evidence.evidence_digest,),
        execution_identity=None,
        importer_tool_version="gameforge@0.0.0",
        status="evidence_missing",
    )
    shard = CassetteBundleV1(
        scope="record_shard",
        attempt_no=1,
        ordinal=1,
        records=(record,),
        legacy_call_import_evidence=evidence,
    )
    attempt = CassetteBundleV1(
        scope="attempt",
        attempt_no=1,
        child_bundle_artifact_ids=("artifact:shard:1",),
    )
    root = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=("artifact:attempt:1",),
        legacy_run_import_manifest=manifest,
    )
    children = {"artifact:attempt:1": attempt, "artifact:shard:1": shard}

    assert validate_legacy_import_bundle_tree(
        root,
        children,
        policy_registry=_registry(policy),
        model_catalog=_catalog(),
        rendered_requests_by_artifact_id={},
        expected_invocations_by_artifact_id={},
    ) == (record,)
    with pytest.raises(IntegrityViolation, match="not executable"):
        require_verified_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=_registry(policy),
            model_catalog=_catalog(),
            rendered_requests_by_artifact_id={},
            expected_invocations_by_artifact_id={},
        )


def test_verified_manifest_has_stable_import_id_digest_and_binding_sets() -> None:
    policy = _policy()
    root, _, _, _ = _tree(policy, _catalog(), call_count=1)
    manifest = root.legacy_run_import_manifest
    assert manifest is not None

    payload_without_ids = manifest.model_dump(mode="json", exclude={"import_id", "digest"})
    assert manifest.import_id == (
        "legacy-cassette-import:sha256:" + canonical_sha256(payload_without_ids)
    )
    assert manifest.digest == canonical_sha256(manifest.model_dump(mode="json", exclude={"digest"}))
    assert tuple(item.binding_key for item in manifest.input_artifact_bindings) == ("source",)
    assert tuple(item.field_path for item in manifest.execution_profile_bindings) == (
        "/repair_policy",
    )


def test_three_level_tree_validates_exact_order_and_returns_authoritative_records() -> None:
    policy = _policy()
    catalog = _catalog()
    root, children, requests, invocations = _tree(policy, catalog)
    rendered = {
        f"artifact:request:{ordinal}": request for ordinal, request in enumerate(requests, 1)
    }
    expected = {
        f"artifact:request:{ordinal}": invocation
        for ordinal, invocation in enumerate(invocations, 1)
    }

    records = require_verified_legacy_import_bundle_tree(
        root,
        children,
        policy_registry=_registry(policy),
        model_catalog=catalog,
        rendered_requests_by_artifact_id=rendered,
        expected_invocations_by_artifact_id=expected,
    )

    assert tuple(record.response.response_normalized for record in records) == (
        "answer-1",
        "answer-2",
    )


def test_zero_call_three_level_import_still_checks_manifest_bindings() -> None:
    policy = _policy()
    profile = _profile_binding()
    input_bindings, policy_bindings, schema_bindings = _bindings()
    identity = build_execution_identity(
        scope="run",
        bindings=(),
        agent_graph_version="agents@2",
    )
    manifest = build_legacy_import_manifest(
        source_suite_id="m2-repair",
        source_case_id="empty-case",
        verification_policy=policy.ref(),
        input_artifact_bindings=input_bindings,
        execution_profile_bindings=(profile,),
        frozen_version_tuple=VersionTuple(agent_graph_version="agents@2"),
        policy_bindings=policy_bindings,
        schema_bindings=schema_bindings,
        ordered_call_evidence_digests=(),
        execution_identity=identity,
        importer_tool_version="gameforge@0.0.0",
        status="verified",
    )
    root = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=("artifact:attempt:1",),
        legacy_run_import_manifest=manifest,
    )
    children = {
        "artifact:attempt:1": CassetteBundleV1(scope="attempt", attempt_no=1),
    }

    assert (
        require_verified_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=_registry(policy),
            model_catalog=_catalog(),
            rendered_requests_by_artifact_id={},
            expected_invocations_by_artifact_id={},
        )
        == ()
    )

    bad_policy = LegacyImportVerificationPolicyV1.create(
        policy_id="legacy-v1-import",
        policy_version=2,
        required_input_binding_keys=("different",),
        required_profile_field_paths=("/repair_policy",),
        required_policy_binding_keys=("route",),
        required_schema_binding_keys=("request",),
        max_wire_bytes_per_call=32_768,
        max_calls_per_import=8,
    )
    bad_manifest = build_legacy_import_manifest(
        **manifest.model_dump(
            exclude={"manifest_schema_version", "import_id", "digest", "verification_policy"}
        ),
        verification_policy=bad_policy.ref(),
    )
    bad_root = root.model_copy(update={"legacy_run_import_manifest": bad_manifest})
    with pytest.raises(IntegrityViolation, match="input binding keys"):
        require_verified_legacy_import_bundle_tree(
            bad_root,
            children,
            policy_registry=LegacyImportVerificationPolicyRegistryV1.create(
                registry_version=1,
                policies=(bad_policy,),
            ),
            model_catalog=_catalog(),
            rendered_requests_by_artifact_id={},
            expected_invocations_by_artifact_id={},
        )


def test_zero_call_evidence_missing_manifest_is_structural_but_not_executable() -> None:
    policy = _policy()
    profile = _profile_binding()
    input_bindings, policy_bindings, schema_bindings = _bindings()
    manifest = build_legacy_import_manifest(
        source_suite_id="m2-repair",
        source_case_id="empty-case",
        verification_policy=policy.ref(),
        input_artifact_bindings=input_bindings,
        execution_profile_bindings=(profile,),
        frozen_version_tuple=None,
        policy_bindings=policy_bindings,
        schema_bindings=schema_bindings,
        ordered_call_evidence_digests=(),
        execution_identity=None,
        importer_tool_version="gameforge@0.0.0",
        status="evidence_missing",
    )
    root = CassetteBundleV1(
        scope="run",
        child_bundle_artifact_ids=("artifact:attempt:1",),
        legacy_run_import_manifest=manifest,
    )
    children = {
        "artifact:attempt:1": CassetteBundleV1(scope="attempt", attempt_no=1),
    }

    assert (
        validate_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=_registry(policy),
            model_catalog=_catalog(),
            rendered_requests_by_artifact_id={},
            expected_invocations_by_artifact_id={},
        )
        == ()
    )
    with pytest.raises(IntegrityViolation, match="not executable"):
        require_verified_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=_registry(policy),
            model_catalog=_catalog(),
            rendered_requests_by_artifact_id={},
            expected_invocations_by_artifact_id={},
        )


def test_bundle_tree_rejects_reordering_extra_children_and_wire_disagreement() -> None:
    policy = _policy()
    catalog = _catalog()
    root, children, requests, invocations = _tree(policy, catalog)
    rendered = {
        f"artifact:request:{ordinal}": request for ordinal, request in enumerate(requests, 1)
    }
    expected = {
        f"artifact:request:{ordinal}": invocation
        for ordinal, invocation in enumerate(invocations, 1)
    }

    attempt = children["artifact:attempt:1"]
    reordered = attempt.model_copy(
        update={"child_bundle_artifact_ids": tuple(reversed(attempt.child_bundle_artifact_ids))}
    )
    with pytest.raises(IntegrityViolation, match="canonical call order"):
        require_verified_legacy_import_bundle_tree(
            root,
            {**children, "artifact:attempt:1": reordered},
            policy_registry=_registry(policy),
            model_catalog=catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=expected,
        )

    with pytest.raises(IntegrityViolation, match="unreachable"):
        require_verified_legacy_import_bundle_tree(
            root,
            {**children, "artifact:unreachable": children["artifact:shard:1"]},
            policy_registry=_registry(policy),
            model_catalog=catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=expected,
        )

    first_shard = children["artifact:shard:1"]
    different_record = _record(_request(1), 99)
    conflicting_shard = first_shard.model_copy(update={"records": (different_record,)})
    with pytest.raises(IntegrityViolation, match="original wire"):
        require_verified_legacy_import_bundle_tree(
            root,
            {**children, "artifact:shard:1": conflicting_shard},
            policy_registry=_registry(policy),
            model_catalog=catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=expected,
        )


def test_bundle_shape_rejects_native_v1_and_imported_v2_records() -> None:
    request = _request(1)
    v1 = _record(request, 1)
    with pytest.raises(ValidationError, match="native record shard"):
        CassetteBundleV1(
            scope="record_shard",
            run_id="run-1",
            attempt_no=1,
            ordinal=1,
            records=(v1,),
        )

    policy = _policy()
    catalog = _catalog()
    profile = _profile_binding()
    evidence = _verified_evidence(
        policy=policy,
        catalog=catalog,
        profile=profile,
        request=request,
        record=v1,
        call_ordinal=1,
    )
    from gameforge.contracts.cassette import CassetteRecordV2
    from gameforge.contracts.cost import (
        CacheHitObservationV1,
        LatencyObservationV1,
        TokenUsageObservationV1,
    )
    from gameforge.contracts.routing import RoutingDecisionV1

    routing_payload = {
        "run_id": "run-1",
        "attempt_no": 1,
        "request_hash": request_hash(request),
        "rule_id": "rule-1",
        "model_snapshot": canonical_model_snapshot_id(request.model_snapshot),
        "tier": "historical",
        "reason_code": "primary",
        "budget_set_snapshot_id": "budget-set-1",
        "fallback_index": 0,
        "policy_version": 1,
        "routing_policy_digest": "5" * 64,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "execution_source": "cassette_replay",
        "decided_at": datetime(2026, 7, 14, tzinfo=UTC),
    }
    v2 = CassetteRecordV2(
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=request.model_snapshot,
        routing_decision=RoutingDecisionV1.create(**routing_payload),
        response_normalized="answer",
        raw_response={},
        latency=LatencyObservationV1(status="unavailable"),
        token_usage=TokenUsageObservationV1(status="unavailable"),
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        finish_reason="stop",
        tool_calls=(),
        transport_attempt_count=1,
        transport_retry_count=0,
    )
    with pytest.raises(ValidationError, match="imported record shard"):
        CassetteBundleV1(
            scope="record_shard",
            attempt_no=1,
            ordinal=1,
            records=(v2,),
            legacy_call_import_evidence=evidence,
        )
