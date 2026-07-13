from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from gameforge.contracts.canonical import (
    canonical_sha256,
    compute_snapshot_id,
    sha256_lowerhex,
)
from gameforge.contracts.lineage import (
    Artifact,
    ArtifactKind,
    ArtifactV1,
    ArtifactV2,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    artifact_id_v2_for,
    build_artifact_v2,
    build_execution_identity,
    object_key_for_sha256,
    object_ref_for_bytes,
    parse_artifact,
)
from gameforge.contracts import versions


_ALL_ARTIFACT_KINDS = {
    "source_raw",
    "source_rendered",
    "ir_snapshot",
    "constraint_snapshot",
    "constraint_proposal",
    "config_export",
    "scenario_spec",
    "task_suite",
    "regression_suite",
    "golden_suite",
    "bench_dataset",
    "benchmark_spec",
    "review_report",
    "checker_run",
    "simulation_run",
    "playtest_trace",
    "patch",
    "validation_evidence",
    "regression_evidence",
    "rollback_request",
    "run_result",
    "run_failure",
    "cassette_bundle",
    "migration_report",
    "bench_report",
    "operational_evidence",
}


def test_legacy_constants_constructor_and_snapshot_hash_are_unchanged() -> None:
    assert versions.LINEAGE_SCHEMA_VERSION == "lineage@1"
    assert versions.AUDIT_SCHEMA_VERSION == "audit@1"
    assert versions.PATCH_SCHEMA_VERSION == "patch@1"

    artifact = Artifact(
        artifact_id="legacy-id",
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id="sha256:legacy"),
    )
    assert isinstance(artifact, ArtifactV1)
    assert artifact.lineage_schema_version == "lineage@1"
    assert artifact.payload_hash is None
    assert artifact.lineage == []

    assert compute_snapshot_id({"b": 1, "a": 2, "n": None}) == (
        "sha256:d3626ac30a87e6f7a6428233b3c68299976865fa5508e4267c5415c76af7a772"
    )


def test_m4_constants_are_additive() -> None:
    assert versions.LINEAGE_SCHEMA_VERSION_V2 == "lineage@2"
    assert versions.AUDIT_SCHEMA_VERSION_V2 == "audit@2"
    assert versions.PATCH_SCHEMA_VERSION_V2 == "patch@2"
    assert versions.FINDING_REVISION_SCHEMA_VERSION == "finding-revision@1"
    assert versions.FINDING_PAYLOAD_SCHEMA_VERSION == "finding-payload@1"
    assert versions.OBJECT_REF_SCHEMA_VERSION == "object-ref@1"
    assert versions.OBJECT_LOCATION_SCHEMA_VERSION == "object-location@1"
    assert versions.OBJECT_BINDING_SCHEMA_VERSION == "object-binding@1"
    assert versions.EXECUTION_IDENTITY_SCHEMA_VERSION == "execution-identity@1"


def test_lowerhex_helpers_do_not_change_legacy_namespaced_hashes() -> None:
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_lowerhex(b"abc") == expected
    assert canonical_sha256({"value": "abc"}) == sha256_lowerhex(b'{"value":"abc"}')
    assert not sha256_lowerhex(b"abc").startswith("sha256:")


def test_artifact_kind_is_the_closed_foundations_v03_set() -> None:
    assert set(get_args(ArtifactKind)) == _ALL_ARTIFACT_KINDS


def test_object_ref_is_content_addressed_and_strict() -> None:
    ref = object_ref_for_bytes(b"payload")
    assert ref.object_ref_schema_version == "object-ref@1"
    assert ref.sha256 == sha256_lowerhex(b"payload")
    assert ref.size_bytes == 7
    assert ref.key == object_key_for_sha256(ref.sha256)
    assert ref.key == f"objects/v1/sha256/{ref.sha256[:2]}/{ref.sha256}"

    for bad_digest in (f"sha256:{ref.sha256}", ref.sha256.upper(), ref.sha256[:-1]):
        with pytest.raises(ValidationError):
            ObjectRef(key=object_key_for_sha256(ref.sha256), sha256=bad_digest, size_bytes=7)

    with pytest.raises(ValidationError, match="content-addressed key"):
        ObjectRef(key="bucket/path", sha256=ref.sha256, size_bytes=7)
    with pytest.raises(ValidationError):
        ObjectRef(key=ref.key, sha256=ref.sha256, size_bytes=-1)


def test_object_binding_closes_ref_location_and_revision() -> None:
    ref = object_ref_for_bytes(b"payload")
    location = ObjectLocation(
        store_id="local-primary",
        key=ref.key,
        backend_generation="generation-1",
    )
    binding = ObjectBinding(
        object_ref=ref,
        location=location,
        status="active",
        revision=1,
        verified_at="2026-07-13T00:00:00Z",
    )
    assert binding.binding_schema_version == "object-binding@1"

    with pytest.raises(ValidationError, match="location key"):
        ObjectBinding(
            object_ref=ref,
            location=location.model_copy(update={"key": "objects/v1/sha256/00/wrong"}),
            status="active",
            revision=1,
            verified_at="2026-07-13T00:00:00Z",
        )
    with pytest.raises(ValidationError):
        ObjectBinding(
            object_ref=ref,
            location=location,
            status="active",
            revision=0,
            verified_at="2026-07-13T00:00:00Z",
        )


def _artifact_v2(
    payload: bytes = b"payload",
    *,
    lineage: list[str] | None = None,
    meta: dict | None = None,
    version_tuple: VersionTuple | None = None,
    created_at: str | None = None,
) -> ArtifactV2:
    ref = object_ref_for_bytes(payload)
    return build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=version_tuple or VersionTuple(ir_snapshot_id="sha256:ir", tool_version="tool@1"),
        lineage=lineage or [],
        payload_hash=ref.sha256,
        object_ref=ref,
        meta=meta or {},
        created_at=created_at,
    )


def test_artifact_v2_id_binds_schema_kind_tuple_parents_payload_and_meta() -> None:
    first = _artifact_v2(lineage=["parent-b", "parent-a"], meta={"purpose": "test"})
    reordered = _artifact_v2(lineage=["parent-a", "parent-b"], meta={"purpose": "test"})
    later = _artifact_v2(
        lineage=["parent-a", "parent-b"],
        meta={"purpose": "test"},
        created_at="2026-07-13T00:00:00Z",
    )
    changed_meta = _artifact_v2(lineage=["parent-a", "parent-b"], meta={"purpose": "other"})

    assert first.lineage_schema_version == "lineage@2"
    assert first.lineage == ("parent-a", "parent-b")
    assert first.artifact_id == reordered.artifact_id == later.artifact_id
    assert first.artifact_id != changed_meta.artifact_id
    assert first.payload_hash == first.object_ref.sha256
    assert first.artifact_id == artifact_id_v2_for(
        kind=first.kind,
        version_tuple=first.version_tuple,
        lineage=first.lineage,
        payload_hash=first.payload_hash,
        meta=first.meta,
    )


def test_artifact_v2_rejects_integrity_mismatches_and_duplicate_parents() -> None:
    artifact = _artifact_v2(lineage=["parent-a"])
    raw = artifact.model_dump(mode="json")

    with pytest.raises(ValidationError, match="artifact_id"):
        ArtifactV2.model_validate({**raw, "artifact_id": "sha256:" + "0" * 64})
    with pytest.raises(ValidationError, match="payload_hash"):
        ArtifactV2.model_validate({**raw, "payload_hash": "0" * 64})
    with pytest.raises(ValidationError, match="duplicate"):
        build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=artifact.version_tuple,
            lineage=["parent-a", "parent-a"],
            payload_hash=artifact.payload_hash,
            object_ref=artifact.object_ref,
        )


def test_artifact_parser_is_discriminator_driven_and_never_fabricates_object_ref() -> None:
    legacy = Artifact(
        artifact_id="legacy-id",
        kind="patch",
        version_tuple=VersionTuple(),
        payload_hash="sha256:legacy-wire-value",
    )
    assert parse_artifact(legacy.model_dump(mode="json")) == legacy

    current = _artifact_v2()
    parsed = parse_artifact(current.model_dump(mode="json"))
    assert isinstance(parsed, ArtifactV2)
    assert parsed == current

    missing_ref = current.model_dump(mode="json")
    del missing_ref["object_ref"]
    with pytest.raises(ValidationError):
        parse_artifact(missing_ref)
    with pytest.raises(ValidationError):
        parse_artifact({**legacy.model_dump(mode="json"), "lineage_schema_version": "lineage@999"})


def _binding(
    *,
    attempt_no: int,
    call_ordinal: int,
    route_ordinal: int,
    model: str,
    prompt: str = "prompt@1",
    source: str = "online",
    consumed: bool = False,
) -> InvocationVersionBindingV1:
    return InvocationVersionBindingV1(
        attempt_no=attempt_no,
        call_ordinal=call_ordinal,
        route_ordinal=route_ordinal,
        transport_attempt=1 if source == "online" else None,
        routing_decision_kind="native",
        routing_decision_id=(
            f"route:{attempt_no}:{call_ordinal}:{route_ordinal}"
        ),
        agent_node_id="repair.draft",
        prompt_version=prompt,
        model_snapshot=model,
        tool_version="repair@1",
        execution_source=source,
        response_consumed=consumed,
    )


def test_execution_identity_sorts_bindings_and_builds_single_and_set_projections() -> None:
    identity = build_execution_identity(
        scope="run",
        agent_graph_version="repair-graph@1",
        bindings=[
            _binding(attempt_no=2, call_ordinal=1, route_ordinal=1, model="model-b", consumed=True),
            _binding(attempt_no=1, call_ordinal=1, route_ordinal=2, model="model-a", consumed=True),
            _binding(attempt_no=1, call_ordinal=1, route_ordinal=1, model="model-b"),
        ],
    )

    assert isinstance(identity, ExecutionIdentityV1)
    assert [(b.attempt_no, b.call_ordinal, b.route_ordinal) for b in identity.bindings] == [
        (1, 1, 1),
        (1, 1, 2),
        (2, 1, 1),
    ]
    assert identity.prompt_projection.mode == "single"
    assert identity.prompt_projection.members == ("prompt@1",)
    assert identity.prompt_projection.tuple_value == "prompt@1"
    assert identity.model_projection.mode == "set"
    assert identity.model_projection.members == ("model-b", "model-a")
    expected_set_digest = canonical_sha256(
        {"field": "model_snapshot", "members": ["model-b", "model-a"]}
    )
    assert identity.model_projection.tuple_value == f"model-set:sha256:{expected_set_digest}"
    assert len(identity.digest) == 64 and identity.digest == identity.digest.lower()


def test_execution_identity_empty_projection_is_explicitly_not_applicable() -> None:
    identity = build_execution_identity(scope="run", bindings=[])
    assert identity.prompt_projection.mode == "not_applicable"
    assert identity.prompt_projection.members == ()
    assert identity.prompt_projection.tuple_value is None
    assert identity.model_projection.mode == "not_applicable"


def test_execution_identity_rejects_route_and_digest_integrity_failures() -> None:
    with pytest.raises(ValidationError, match="route_ordinal"):
        build_execution_identity(
            scope="attempt",
            bindings=[_binding(attempt_no=1, call_ordinal=1, route_ordinal=2, model="model")],
        )

    with pytest.raises(ValidationError, match="response_consumed"):
        build_execution_identity(
            scope="attempt",
            bindings=[
                _binding(
                    attempt_no=1,
                    call_ordinal=1,
                    route_ordinal=1,
                    model="model-a",
                    consumed=True,
                ),
                _binding(
                    attempt_no=1,
                    call_ordinal=1,
                    route_ordinal=2,
                    model="model-b",
                    consumed=True,
                ),
            ],
        )

    identity = build_execution_identity(
        scope="attempt",
        bindings=[
            _binding(
                attempt_no=1,
                call_ordinal=1,
                route_ordinal=1,
                model="model-a",
                consumed=True,
            )
        ],
    )
    with pytest.raises(ValidationError, match="digest"):
        ExecutionIdentityV1.model_validate(
            {**identity.model_dump(mode="json"), "digest": "0" * 64}
        )

    with pytest.raises(ValidationError, match="transport_attempt"):
        InvocationVersionBindingV1(
            **{
                **_binding(
                    attempt_no=1,
                    call_ordinal=1,
                    route_ordinal=1,
                    model="model",
                    source="cassette_replay",
                ).model_dump(),
                "transport_attempt": 1,
            }
        )


def test_artifact_execution_identity_must_match_version_tuple_projections() -> None:
    identity = build_execution_identity(
        scope="artifact",
        agent_graph_version="repair-graph@1",
        bindings=[
            _binding(
                attempt_no=1,
                call_ordinal=1,
                route_ordinal=1,
                model="model-a",
                consumed=True,
            )
        ],
    )
    tuple_ = VersionTuple(
        ir_snapshot_id="sha256:ir",
        tool_version="repair@1",
        prompt_version=identity.prompt_projection.tuple_value,
        model_snapshot=identity.model_projection.tuple_value,
        agent_graph_version=identity.agent_graph_version,
    )
    artifact = _artifact_v2(meta={"execution_identity": identity}, version_tuple=tuple_)
    assert artifact.meta["execution_identity"].digest == identity.digest

    mismatched = tuple_.model_copy(update={"model_snapshot": "another-model"})
    with pytest.raises(ValidationError, match="model_snapshot"):
        _artifact_v2(meta={"execution_identity": identity}, version_tuple=mismatched)
