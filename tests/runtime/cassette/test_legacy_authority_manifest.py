from __future__ import annotations

import json

import pytest

from gameforge.contracts.cassette_import import LegacyCassettePolicyBindingV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.runtime.cassette.legacy_authority_manifest import (
    MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES,
    LegacyCallToolVersionBindingV1,
    LegacyFrozenVersionTupleBindingV1,
    LegacyImportAuthorityManifestV1,
    LegacyRenderedRequestBindingV1,
    load_legacy_import_authority,
    load_legacy_import_authority_manifest,
    parse_legacy_import_authority_manifest,
)
from tests.platform.m4c.test_replay_admission import _legacy_verified_fixture


def _manifest() -> LegacyImportAuthorityManifestV1:
    authority = _legacy_verified_fixture().authority
    return LegacyImportAuthorityManifestV1.create(
        authority_version="m2-retained-history@1",
        verification_policy_registry=authority.verification_policy_registry,
        model_catalogs=tuple(authority.model_catalogs.values()),
        input_bindings=tuple(authority.input_bindings.values()),
        profile_bindings=tuple(authority.profile_bindings.values()),
        policy_bindings=(
            LegacyCassettePolicyBindingV1(
                binding_key="review-policy",
                policy_kind="review",
                policy_id="historical-review",
                policy_version=1,
                policy_digest="a" * 64,
            ),
        ),
        schema_bindings=tuple(authority.schema_bindings.values()),
        rendered_requests=tuple(
            LegacyRenderedRequestBindingV1(artifact_id=artifact_id, request=request)
            for artifact_id, request in authority.rendered_requests.items()
        ),
        frozen_version_tuples=tuple(
            LegacyFrozenVersionTupleBindingV1(
                source_suite_id=source_suite_id,
                source_case_id=source_case_id,
                version_tuple=version_tuple,
            )
            for (source_suite_id, source_case_id), version_tuple in (
                authority.frozen_version_tuples.items()
            )
        ),
        call_tool_versions=tuple(
            LegacyCallToolVersionBindingV1(
                source_suite_id=source_suite_id,
                source_case_id=source_case_id,
                source_call_ordinal=source_call_ordinal,
                tool_version=tool_version,
            )
            for (
                source_suite_id,
                source_case_id,
                source_call_ordinal,
            ), tool_version in authority.call_tool_versions.items()
        ),
    )


def _write_manifest(path, manifest: LegacyImportAuthorityManifestV1) -> None:
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )


def test_content_bound_manifest_round_trips_complete_nonempty_authority() -> None:
    manifest = _manifest()

    parsed = parse_legacy_import_authority_manifest(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)
    )
    authority = parsed.build_authority()

    assert not hasattr(authority, "rendered_requests")
    assert not hasattr(authority, "model_catalogs")

    assert authority.verification_policy_registry.policies
    catalog = parsed.model_catalogs[0]
    assert (
        authority.resolve_model_catalog(catalog.catalog_version, catalog.catalog_digest) == catalog
    )
    input_binding = parsed.input_bindings[0]
    assert (
        authority.resolve_input_binding(input_binding.binding_key, input_binding.artifact_id)
        == input_binding
    )
    profile_binding = parsed.profile_bindings[0]
    assert (
        authority.resolve_profile_binding(
            profile_binding.field_path,
            profile_binding.profile_id,
            profile_binding.profile_version,
            profile_binding.catalog_version,
            profile_binding.catalog_digest,
        )
        == profile_binding
    )
    policy_binding = parsed.policy_bindings[0]
    assert (
        authority.resolve_policy_binding(
            policy_binding.binding_key,
            policy_binding.policy_kind,
            policy_binding.policy_id,
            policy_binding.policy_version,
        )
        == policy_binding
    )
    schema_binding = parsed.schema_bindings[0]
    assert (
        authority.resolve_schema_binding(schema_binding.binding_key, schema_binding.schema_id)
        == schema_binding
    )
    rendered = parsed.rendered_requests[0]
    assert authority.resolve_rendered_request(rendered.artifact_id) == rendered.request
    frozen = parsed.frozen_version_tuples[0]
    assert (
        authority.resolve_frozen_version_tuple(frozen.source_suite_id, frozen.source_case_id)
        == frozen.version_tuple
    )
    call = parsed.call_tool_versions[0]
    assert (
        authority.resolve_call_tool_version(
            call.source_suite_id,
            call.source_case_id,
            call.source_call_ordinal,
        )
        == call.tool_version
    )

    original_content = rendered.request.messages[0].content
    parsed.rendered_requests[0].request.messages[0].content = "caller mutation"
    retained = authority.resolve_rendered_request(rendered.artifact_id)
    assert retained is not None
    assert retained.messages[0].content == original_content
    retained.messages[0].content = "resolver mutation"
    retained_again = authority.resolve_rendered_request(rendered.artifact_id)
    assert retained_again is not None
    assert retained_again.messages[0].content == original_content

    returned_input = authority.resolve_input_binding(
        input_binding.binding_key,
        input_binding.artifact_id,
    )
    assert returned_input is not None
    returned_input.version_tuple.tool_version = "resolver mutation"
    input_again = authority.resolve_input_binding(
        input_binding.binding_key,
        input_binding.artifact_id,
    )
    assert input_again is not None
    assert input_again.version_tuple == input_binding.version_tuple


def test_manifest_canonicalizes_semantic_collections_before_hashing() -> None:
    manifest = _manifest()
    first = manifest.call_tool_versions[0]
    second = first.model_copy(update={"source_call_ordinal": 2})
    values = manifest.model_dump(mode="python", exclude={"manifest_digest"})
    values["call_tool_versions"] = (second, first)

    reversed_manifest = LegacyImportAuthorityManifestV1.create(**values)
    values["call_tool_versions"] = (first, second)
    ordered_manifest = LegacyImportAuthorityManifestV1.create(**values)

    assert reversed_manifest == ordered_manifest
    assert tuple(item.source_call_ordinal for item in reversed_manifest.call_tool_versions) == (
        1,
        2,
    )


def test_manifest_digest_and_resolver_identities_fail_closed() -> None:
    manifest = _manifest()
    payload = manifest.model_dump(mode="json")
    payload["authority_version"] = "tampered@1"
    with pytest.raises(IntegrityViolation, match="authority manifest is invalid"):
        parse_legacy_import_authority_manifest(json.dumps(payload))

    duplicate = manifest.model_dump(mode="python")
    duplicate["rendered_requests"] = (
        manifest.rendered_requests[0],
        manifest.rendered_requests[0],
    )
    duplicate.pop("manifest_digest")
    with pytest.raises(ValueError, match="rendered request artifact identities must be unique"):
        LegacyImportAuthorityManifestV1.create(**duplicate)


def test_manifest_loader_enforces_utf8_and_total_byte_bound(tmp_path) -> None:
    manifest_path = tmp_path / "legacy-authority.json"
    manifest_path.write_text(
        json.dumps(_manifest().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    assert load_legacy_import_authority_manifest(manifest_path) == _manifest()

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_BYTES + 1))
    with pytest.raises(IntegrityViolation, match="exceeds its byte bound"):
        load_legacy_import_authority_manifest(oversized)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(IntegrityViolation, match="not UTF-8"):
        load_legacy_import_authority_manifest(invalid_utf8)

    alias = tmp_path / "legacy-authority-link.json"
    alias.symlink_to(manifest_path)
    with pytest.raises(IntegrityViolation, match="regular file"):
        load_legacy_import_authority_manifest(alias)

    for invalid_path in ("legacy\x00authority.json", "x" * 4097):
        with pytest.raises(IntegrityViolation, match="path is invalid"):
            load_legacy_import_authority(invalid_path)


def test_flat_directory_shards_merge_cross_shard_closure_and_ordinals(tmp_path) -> None:
    base = _manifest()
    shard_one_values = base.model_dump(mode="python", exclude={"manifest_digest"})
    shard_one_values.update(
        profile_bindings=(),
    )
    shard_one = LegacyImportAuthorityManifestV1.create(**shard_one_values)

    rendered = base.rendered_requests[0]
    second_artifact_id = f"{rendered.artifact_id}:shard-2"
    source = base.call_tool_versions[0]
    shard_two_values = base.model_dump(mode="python", exclude={"manifest_digest"})
    shard_two_values.update(
        model_catalogs=(),
        input_bindings=(),
        policy_bindings=(),
        schema_bindings=(),
        rendered_requests=(rendered.model_copy(update={"artifact_id": second_artifact_id}),),
        frozen_version_tuples=(),
        call_tool_versions=(source.model_copy(update={"source_call_ordinal": 2}),),
    )
    shard_two = LegacyImportAuthorityManifestV1.create(**shard_two_values)
    shards = tmp_path / "legacy-shards"
    shards.mkdir()
    _write_manifest(shards / "01.json", shard_one)
    _write_manifest(shards / "02.json", shard_two)

    authority = load_legacy_import_authority(shards)

    profile = base.profile_bindings[0]
    assert (
        authority.resolve_profile_binding(
            profile.field_path,
            profile.profile_id,
            profile.profile_version,
            profile.catalog_version,
            profile.catalog_digest,
        )
        == profile
    )
    assert authority.resolve_rendered_request(rendered.artifact_id) == rendered.request
    assert authority.resolve_rendered_request(second_artifact_id) == rendered.request
    assert (
        authority.resolve_call_tool_version(
            source.source_suite_id,
            source.source_case_id,
            2,
        )
        == source.tool_version
    )


def test_shard_merge_dedupes_exact_values_and_rejects_conflicts(tmp_path) -> None:
    base = _manifest()
    shards = tmp_path / "legacy-shards"
    shards.mkdir()
    _write_manifest(shards / "01.json", base)
    _write_manifest(shards / "02.json", base)
    authority = load_legacy_import_authority(shards)
    rendered = base.rendered_requests[0]
    assert authority.resolve_rendered_request(rendered.artifact_id) == rendered.request

    conflicting_request = type(rendered.request).model_validate(
        rendered.request.model_dump(mode="python")
    )
    conflicting_request.messages[0].content = "different retained request"
    conflict_values = base.model_dump(mode="python", exclude={"manifest_digest"})
    conflict_values["rendered_requests"] = (
        rendered.model_copy(update={"request": conflicting_request}),
    )
    conflict = LegacyImportAuthorityManifestV1.create(**conflict_values)
    _write_manifest(shards / "02.json", conflict)

    with pytest.raises(IntegrityViolation, match="conflict on rendered request"):
        load_legacy_import_authority(shards)


def test_shard_merge_requires_exact_version_registry_and_global_ordinals(tmp_path) -> None:
    base = _manifest()
    shards = tmp_path / "legacy-shards"
    shards.mkdir()
    _write_manifest(shards / "01.json", base)

    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    values["authority_version"] = "another-authority@1"
    _write_manifest(
        shards / "02.json",
        LegacyImportAuthorityManifestV1.create(**values),
    )
    with pytest.raises(IntegrityViolation, match="different authority versions"):
        load_legacy_import_authority(shards)

    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    registry = base.verification_policy_registry
    values["verification_policy_registry"] = type(registry).create(
        registry_version=registry.registry_version + 1,
        policies=registry.policies,
    )
    _write_manifest(
        shards / "02.json",
        LegacyImportAuthorityManifestV1.create(**values),
    )
    with pytest.raises(IntegrityViolation, match="different verification registries"):
        load_legacy_import_authority(shards)

    source = base.call_tool_versions[0]
    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    values.update(
        model_catalogs=(),
        input_bindings=(),
        profile_bindings=(),
        policy_bindings=(),
        schema_bindings=(),
        rendered_requests=(),
        frozen_version_tuples=(),
        call_tool_versions=(source.model_copy(update={"source_call_ordinal": 3}),),
    )
    _write_manifest(
        shards / "02.json",
        LegacyImportAuthorityManifestV1.create(**values),
    )
    with pytest.raises(IntegrityViolation, match="ordinals must start at 1"):
        load_legacy_import_authority(shards)


def test_aggregate_rejects_cross_key_global_identity_conflicts() -> None:
    base = _manifest()

    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    input_binding = base.input_bindings[0]
    values["input_bindings"] = (
        input_binding,
        input_binding.model_copy(update={"binding_key": "another-input", "payload_hash": "b" * 64}),
    )
    with pytest.raises(IntegrityViolation, match="cross-key input artifact identity"):
        LegacyImportAuthorityManifestV1.create(**values).build_authority()

    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    profile = base.profile_bindings[0]
    values["profile_bindings"] = (
        profile,
        profile.model_copy(
            update={"field_path": "/params/another-profile", "profile_payload_hash": "b" * 64}
        ),
    )
    with pytest.raises(IntegrityViolation, match="cross-key profile identity"):
        LegacyImportAuthorityManifestV1.create(**values).build_authority()

    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    policy = base.policy_bindings[0]
    values["policy_bindings"] = (
        policy,
        policy.model_copy(update={"binding_key": "another-policy", "policy_digest": "b" * 64}),
    )
    with pytest.raises(IntegrityViolation, match="cross-key policy identity"):
        LegacyImportAuthorityManifestV1.create(**values).build_authority()


def test_aggregate_rejects_call_tool_version_different_from_frozen_tuple() -> None:
    base = _manifest()
    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    call = base.call_tool_versions[0]
    values["call_tool_versions"] = (call.model_copy(update={"tool_version": "another-tool@1"}),)

    with pytest.raises(IntegrityViolation, match="differs from its frozen version tuple"):
        LegacyImportAuthorityManifestV1.create(**values).build_authority()


def test_profile_history_is_keyed_by_exact_execution_profile_catalog() -> None:
    base = _manifest()
    profile = base.profile_bindings[0]
    first = profile.model_copy(update={"catalog_version": 101, "catalog_digest": "d" * 64})
    second = profile.model_copy(update={"catalog_version": 102, "catalog_digest": "e" * 64})
    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    values["profile_bindings"] = (first, second)

    authority = LegacyImportAuthorityManifestV1.create(**values).build_authority()

    assert first.catalog_version != base.model_catalogs[0].catalog_version
    assert (
        authority.resolve_profile_binding(
            first.field_path,
            first.profile_id,
            first.profile_version,
            first.catalog_version,
            first.catalog_digest,
        )
        == first
    )
    assert (
        authority.resolve_profile_binding(
            second.field_path,
            second.profile_id,
            second.profile_version,
            second.catalog_version,
            second.catalog_digest,
        )
        == second
    )


def test_profile_catalog_version_has_one_global_digest() -> None:
    base = _manifest()
    profile = base.profile_bindings[0]
    first = profile.model_copy(update={"catalog_version": 101, "catalog_digest": "d" * 64})
    conflicting = profile.model_copy(
        update={
            "field_path": "/params/another-profile-catalog",
            "profile_id": "another-profile",
            "catalog_version": 101,
            "catalog_digest": "e" * 64,
        }
    )
    values = base.model_dump(mode="python", exclude={"manifest_digest"})
    values["profile_bindings"] = (first, conflicting)

    with pytest.raises(IntegrityViolation, match="execution profile catalog version"):
        LegacyImportAuthorityManifestV1.create(**values).build_authority()
