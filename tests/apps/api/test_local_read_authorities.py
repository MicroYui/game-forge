"""Focused fail-closed tests for the local production read-domain adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gameforge.apps.api.local_reads import _ArtifactDomainAuthority, _ContentPermissionAuthority
from gameforge.apps.api.run_read_domain import resolve_run_read_domain
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation, QueryTooBroad
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    compute_domain_registry_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    ArtifactV2,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)


BUILTIN = DomainScope(domain_ids=("builtin",))
OTHER = DomainScope(domain_ids=("other",))


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="builtin", display_name="Built-in", status="active"),
        DomainDefinitionV1(domain_id="other", display_name="Other", status="active"),
        DomainDefinitionV1(domain_id="retired", display_name="Retired", status="deprecated"),
    )
    version = "local-read-authority@1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _artifact(
    artifact_id: str,
    *,
    scope: DomainScope | None,
    lineage: tuple[str, ...] = (),
    kind: str = "ir_snapshot",
    schema_id: str = "ir-core@1",
) -> ArtifactV1:
    meta = {"payload_schema_id": schema_id}
    if scope is not None:
        meta["domain_scope"] = scope.model_dump(mode="json")
    return ArtifactV1(
        artifact_id=artifact_id,
        kind=kind,
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=list(lineage),
        meta=meta,
    )


class _Artifacts:
    def __init__(self, *artifacts: ArtifactV1 | ArtifactV2) -> None:
        self._values = {artifact.artifact_id: artifact for artifact in artifacts}

    def get(self, artifact_id: str):
        return self._values.get(artifact_id)


class _Payloads:
    def __init__(self, scope: DomainScope = BUILTIN) -> None:
        self._scope = scope

    def load(self, *args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace(domain_scope=self._scope)


class _PayloadBindings:
    def __init__(self, schemas: dict[str, str] | None = None) -> None:
        self._schemas = schemas or {}

    def resolve(self, artifact_id: str):
        schema_id = self._schemas.get(artifact_id)
        return None if schema_id is None else SimpleNamespace(payload_schema_id=schema_id)


def _authority(
    *artifacts: ArtifactV1 | ArtifactV2,
    payload_scope: DomainScope = BUILTIN,
    trusted_schemas: dict[str, str] | None = None,
):
    return _ArtifactDomainAuthority(
        artifacts=_Artifacts(*artifacts),  # type: ignore[arg-type]
        registry=_registry(),
        payloads=_Payloads(payload_scope),  # type: ignore[arg-type]
        payload_bindings=_PayloadBindings(trusted_schemas),  # type: ignore[arg-type]
    )


def test_artifact_domain_inherits_exact_parent_scope() -> None:
    parent = _artifact("artifact:parent", scope=BUILTIN)
    child = _artifact("artifact:child", scope=None, lineage=(parent.artifact_id,))

    assert _authority(parent, child).resolve(child) == BUILTIN


def test_modern_explicit_artifact_domain_does_not_scan_historical_lineage() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=("artifact:retained-but-not-needed",),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    assert _authority(artifact).resolve(artifact) == BUILTIN


def test_typed_payload_and_metadata_domain_mismatch_fails_closed() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    with pytest.raises(IntegrityViolation, match="typed payload domains disagree"):
        _authority(
            proposal,
            payload_scope=OTHER,
            trusted_schemas={proposal.artifact_id: "constraint-proposal@1"},
        ).resolve(proposal)


def test_workflow_schema_binding_closes_missing_metadata_domain_seam() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    with pytest.raises(IntegrityViolation, match="typed payload domains disagree"):
        _authority(
            proposal,
            payload_scope=OTHER,
            trusted_schemas={proposal.artifact_id: "constraint-proposal@1"},
        ).resolve(proposal)


def test_unbound_constraint_metadata_cannot_select_typed_domain_authority() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": "constraint-proposal@1"},
    )

    with pytest.raises(DependencyUnavailable, match="no authoritative"):
        _authority(proposal).resolve(proposal)


def test_artifact_domain_cannot_exceed_lineage_authority() -> None:
    parent = _artifact("artifact:parent", scope=BUILTIN)
    child = _artifact("artifact:child", scope=OTHER, lineage=(parent.artifact_id,))

    with pytest.raises(IntegrityViolation, match="exceeds its lineage authority"):
        _authority(parent, child).resolve(child)


def test_artifact_domain_resolves_deep_lineage_without_recursion() -> None:
    artifacts = [_artifact("artifact:depth:0", scope=BUILTIN)]
    for index in range(1, 900):
        artifacts.append(
            _artifact(
                f"artifact:depth:{index}",
                scope=None,
                lineage=(artifacts[-1].artifact_id,),
            )
        )

    assert _authority(*artifacts).resolve(artifacts[-1]) == BUILTIN


def test_artifact_domain_rejects_lineage_over_public_traversal_bound() -> None:
    artifact = _artifact(
        "artifact:wide",
        scope=BUILTIN,
        lineage=tuple(f"artifact:parent:{index}" for index in range(1_001)),
    )

    with pytest.raises(QueryTooBroad, match="traversal bound"):
        _authority(artifact).resolve(artifact)


def test_artifact_domain_rejects_lineage_cycle() -> None:
    first = _artifact("artifact:cycle:a", scope=None, lineage=("artifact:cycle:b",))
    second = _artifact("artifact:cycle:b", scope=None, lineage=("artifact:cycle:a",))

    with pytest.raises(IntegrityViolation, match="cycle"):
        _authority(first, second).resolve(first)


def test_artifact_domain_rejects_duplicate_legacy_edges() -> None:
    parent = _artifact("artifact:duplicate:parent", scope=BUILTIN)
    root = _artifact(
        "artifact:duplicate:root",
        scope=None,
        lineage=(parent.artifact_id, parent.artifact_id),
    )

    with pytest.raises(IntegrityViolation, match="repeats a parent"):
        _authority(parent, root).resolve(root)


def test_artifact_domain_rejects_over_cap_edges_before_duplicate_projection() -> None:
    parent = _artifact("artifact:over-cap:parent", scope=BUILTIN)
    root = _artifact(
        "artifact:over-cap:root",
        scope=None,
        lineage=(parent.artifact_id,) * 10_001,
    )

    with pytest.raises(QueryTooBroad, match="edge bound"):
        _authority(parent, root).resolve(root)


def test_artifact_domain_rejects_dense_lineage_over_edge_bound() -> None:
    artifacts = [_artifact("artifact:dense:0", scope=BUILTIN)]
    for index in range(1, 150):
        artifacts.append(
            _artifact(
                f"artifact:dense:{index}",
                scope=None,
                lineage=tuple(item.artifact_id for item in artifacts),
            )
        )

    with pytest.raises(QueryTooBroad, match="edge bound"):
        _authority(*artifacts).resolve(artifacts[-1])


def test_artifact_domain_resolves_shared_diamond_once_semantically() -> None:
    base = _artifact("artifact:diamond:base", scope=BUILTIN)
    left = _artifact("artifact:diamond:left", scope=None, lineage=(base.artifact_id,))
    right = _artifact("artifact:diamond:right", scope=None, lineage=(base.artifact_id,))
    root = _artifact(
        "artifact:diamond:root",
        scope=None,
        lineage=(left.artifact_id, right.artifact_id),
    )

    assert _authority(base, left, right, root).resolve(root) == BUILTIN


@pytest.mark.parametrize(
    ("artifact", "message"),
    (
        (_artifact("artifact:missing", scope=None, lineage=("artifact:gone",)), "unavailable"),
        (
            _artifact(
                "artifact:unknown",
                scope=DomainScope(domain_ids=("unknown",)),
            ),
            "unknown domain",
        ),
    ),
)
def test_missing_or_unknown_artifact_domain_authority_fails_closed(
    artifact: ArtifactV1,
    message: str,
) -> None:
    with pytest.raises(IntegrityViolation, match=message):
        _authority(artifact).resolve(artifact)


def test_deprecated_artifact_domain_remains_readable() -> None:
    scope = DomainScope(domain_ids=("retired",))
    artifact = _artifact("artifact:historical", scope=scope)

    assert _authority(artifact).resolve(artifact) == scope


def test_legacy_run_fallback_covers_all_retained_domains() -> None:
    run = SimpleNamespace(
        resource_domain_scope=None,
        payload=SimpleNamespace(params=object()),
    )

    assert resolve_run_read_domain(run, _registry(), None) == DomainScope(
        domain_ids=("builtin", "other", "retired")
    )


class _ApprovalPermissions:
    def __init__(self, permission: Permission | None) -> None:
        self._permission = permission

    def for_artifact(self, *args: object, **kwargs: object) -> Permission:
        del args, kwargs
        if self._permission is None:
            raise DependencyUnavailable("not workflow-bound", component="test")
        return self._permission


class _Domains:
    def __init__(
        self,
        scope: DomainScope | None,
        *,
        failure: Exception | None = None,
        legacy_fallback: bool = False,
    ) -> None:
        self._scope = scope
        self._failure = failure
        self._legacy_fallback = legacy_fallback

    def resolve(self, artifact: ArtifactV1) -> DomainScope:
        del artifact
        if self._failure is not None:
            raise self._failure
        assert self._scope is not None
        return self._scope

    def legacy_workflow_fallback_allowed(self, artifact: ArtifactV1) -> bool:
        del artifact
        return self._legacy_fallback


def _read_permission(scope: DomainScope) -> Permission:
    return Permission(action="read", resource_kind="artifact", domain_scope=scope)


def test_legacy_workflow_artifact_uses_retained_approval_domain() -> None:
    artifact = _artifact("artifact:legacy-evidence", scope=None)
    permission = _read_permission(BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(permission),  # type: ignore[arg-type]
        domains=_Domains(  # type: ignore[arg-type]
            None,
            failure=DependencyUnavailable("legacy lineage", component="test"),
            legacy_fallback=True,
        ),
    )

    assert authority.for_artifact(artifact, resource_kind="artifact") == permission


def test_legacy_typed_metadata_does_not_block_exact_approval_fallback() -> None:
    proposal = _artifact(
        "artifact:legacy-proposal",
        scope=None,
        kind="constraint_proposal",
        schema_id="constraint-proposal@1",
    )
    permission = _read_permission(BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(permission),  # type: ignore[arg-type]
        domains=_authority(proposal),
    )

    assert authority.for_artifact(proposal, resource_kind="artifact") == permission


def test_workflow_and_immutable_artifact_domain_mismatch_fails_closed() -> None:
    artifact = _artifact("artifact:mismatch", scope=OTHER)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(_read_permission(BUILTIN)),  # type: ignore[arg-type]
        domains=_Domains(OTHER),  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrityViolation, match="authorities disagree"):
        authority.for_artifact(artifact, resource_kind="artifact")


def test_non_workflow_artifact_requires_immutable_domain_authority() -> None:
    artifact = _artifact("artifact:standalone", scope=BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(None),  # type: ignore[arg-type]
        domains=_Domains(BUILTIN),  # type: ignore[arg-type]
    )

    assert authority.for_artifact(artifact, resource_kind="artifact") == _read_permission(BUILTIN)


def test_workflow_fallback_rejects_partial_modern_domain_lineage() -> None:
    legacy_parent = _artifact("artifact:legacy-parent", scope=None)
    root = _artifact(
        "artifact:modern-root",
        scope=OTHER,
        lineage=(legacy_parent.artifact_id,),
    )
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(_read_permission(BUILTIN)),  # type: ignore[arg-type]
        domains=_authority(legacy_parent, root),
    )

    with pytest.raises(DependencyUnavailable, match="no authoritative"):
        authority.for_artifact(root, resource_kind="artifact")
