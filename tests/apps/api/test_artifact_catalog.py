from __future__ import annotations

from contextlib import contextmanager
from typing import Any, get_args

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.routers.artifacts import artifact_catalog_router
from gameforge.contracts.api import ArtifactSummaryV1, Problem
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Permission,
    Principal,
)
from gameforge.contracts.lineage import (
    ArtifactKind,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import PageV1
from gameforge.platform.read_models.artifacts import TrustedArtifactPayloadBinding
from gameforge.platform.read_models.authorization import (
    AuthorizedReadCollection,
    ReadAuthorizationBinding,
)
from gameforge.platform.read_models.content import ContentReadCapabilities, ContentReadService


CONTENT = DomainScope(domain_ids=("content",))
OTHER = DomainScope(domain_ids=("other",))
AUTHORIZATION_BINDING = ReadAuthorizationBinding(
    principal_binding="1" * 64,
    authz_fingerprint="2" * 64,
)


def _principal() -> Principal:
    return Principal(
        id="human:artifact-reader",
        kind="human",
        display_name="Artifact reader",
        status="active",
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=(),
    )


def _actor() -> ActorContext:
    return ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="credential:artifact-reader",
        ),
        session_id="session:artifact-reader",
        request_id="request:artifact-catalog",
    )


def _artifact(kind: ArtifactKind, suffix: str = "only"):
    payload = f"catalog:{kind}:{suffix}".encode()
    object_ref = object_ref_for_bytes(payload)
    return build_artifact_v2(
        kind=kind,
        version_tuple=VersionTuple(tool_version="artifact-catalog-test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": f"{kind}-payload@1"},
        created_at="2026-07-23T00:00:00Z",
    )


class _ArtifactPages:
    def __init__(self, values: dict[ArtifactKind, tuple[Any, ...]]) -> None:
        self.values = values
        self.calls: list[dict[str, Any]] = []

    def page(self, **kwargs: Any) -> PageV1[Any]:
        self.calls.append(kwargs)
        kind = kwargs["expected_artifact_kind"]
        assert kwargs["index_kind"] == "artifacts"
        assert kwargs["filters"] == {"kind": kind}
        return PageV1(
            read_snapshot_id=f"snapshot:artifacts:{kind}",
            items=self.values.get(kind, ()),
            expires_at="2026-07-23T00:05:00Z",
        )

    def page_lineage(self, **kwargs: Any) -> PageV1[Any]:
        raise AssertionError(f"lineage paging is not expected: {kwargs}")


class _Bindings:
    def __init__(self, values: dict[str, TrustedArtifactPayloadBinding]) -> None:
        self.values = values

    def resolve(self, artifact_id: str) -> TrustedArtifactPayloadBinding | None:
        return self.values.get(artifact_id)


class _NoPayloadReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read(self, artifact_id: str) -> Any:
        self.calls.append(artifact_id)
        raise AssertionError("Artifact catalog must not read or expose payload bytes")


class _Permissions:
    def __init__(self, scopes: dict[str, DomainScope]) -> None:
        self.scopes = scopes

    def for_artifact(self, artifact: Any, *, resource_kind: str) -> Permission:
        return Permission(
            action="read",
            resource_kind=resource_kind,
            domain_scope=self.scopes[artifact.artifact_id],
        )

    def for_ref(self, *_: Any, **__: Any) -> Permission:
        raise AssertionError("ref permission is not expected")


class _Authorization:
    def __init__(self, allowed_domains: set[str]) -> None:
        self.allowed_domains = allowed_domains

    def filter_collection(
        self,
        *,
        candidates: Any,
        collection_permission: Permission,
        permission_for: Any,
        **_: Any,
    ) -> AuthorizedReadCollection[Any]:
        assert collection_permission == Permission(
            action="read",
            resource_kind="artifact",
            domain_scope="all",
        )
        selected = []
        for candidate in tuple(candidates):
            permission = permission_for(candidate)
            scope = permission.domain_scope
            if isinstance(scope, DomainScope) and set(scope.domain_ids) <= self.allowed_domains:
                selected.append(candidate)
        return AuthorizedReadCollection(
            items=tuple(selected),
            binding=AUTHORIZATION_BINDING,
        )

    def require_collection_continuation(self, **_: Any) -> ReadAuthorizationBinding:
        return AUTHORIZATION_BINDING


def _service(
    artifacts: tuple[Any, ...],
    *,
    scopes: dict[str, DomainScope] | None = None,
) -> tuple[ContentReadService, _ArtifactPages, _NoPayloadReader]:
    by_kind: dict[ArtifactKind, list[Any]] = {}
    bindings: dict[str, TrustedArtifactPayloadBinding] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact.kind, []).append(artifact)
        bindings[artifact.artifact_id] = TrustedArtifactPayloadBinding.for_artifact(
            artifact,
            payload_schema_id=artifact.meta["payload_schema_id"],
        )
    pages = _ArtifactPages(
        {
            kind: tuple(sorted(values, key=lambda value: value.artifact_id))
            for kind, values in by_kind.items()
        }
    )
    reader = _NoPayloadReader()
    selected_scopes = scopes or {artifact.artifact_id: CONTENT for artifact in artifacts}
    capabilities = ContentReadCapabilities(
        repository=object(),
        immutable_artifact_pages=pages,
        payload_reader=reader,
        payload_bindings=_Bindings(bindings),
        authorization=_Authorization({"content"}),
        permission_resolver=_Permissions(selected_scopes),
        specs=object(),
        schema_registry=object(),
        proposal_workflows=object(),
        subject_workflows=object(),
        review_producers=object(),
        playtest_results=object(),
        refs=object(),
        diffs=object(),
        bench_reports=object(),
        execution_profiles=object(),
        page_factory=object(),
    )

    @contextmanager
    def uow():
        yield capabilities

    return (
        ContentReadService(uow_factory=uow, max_materialized_items=100),
        pages,
        reader,
    )


def _client(service: ContentReadService) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(artifact_catalog_router(service))
    app.dependency_overrides[require_actor] = _actor
    return TestClient(app)


def test_artifact_catalog_requires_a_valid_kind_and_never_reads_payload() -> None:
    artifact = _artifact("config_export")
    service, pages, reader = _service((artifact,))

    with _client(service) as client:
        missing = client.get("/api/v1/artifacts")
        invalid = client.get("/api/v1/artifacts", params={"kind": "not-an-artifact-kind"})
        response = client.get("/api/v1/artifacts", params={"kind": "config_export"})

    assert (missing.status_code, Problem.model_validate(missing.json()).code) == (
        422,
        "request_schema_invalid",
    )
    assert (invalid.status_code, Problem.model_validate(invalid.json()).code) == (
        422,
        "request_schema_invalid",
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"].startswith('"')
    item = response.json()["items"][0]
    assert ArtifactSummaryV1.model_validate(item).artifact_id == artifact.artifact_id
    assert item["payload_schema_id"] == "config_export-payload@1"
    assert {"payload", "object_ref", "meta", "location"}.isdisjoint(item)
    assert reader.calls == []
    assert pages.calls[0]["expected_artifact_kind"] == "config_export"


def test_artifact_catalog_supports_every_frozen_artifact_kind() -> None:
    kinds = tuple(get_args(ArtifactKind))
    artifacts = tuple(_artifact(kind) for kind in kinds)
    service, _pages, reader = _service(artifacts)

    with _client(service) as client:
        responses = {kind: client.get("/api/v1/artifacts", params={"kind": kind}) for kind in kinds}

    assert all(response.status_code == 200 for response in responses.values())
    assert {kind: response.json()["items"][0]["kind"] for kind, response in responses.items()} == {
        kind: kind for kind in kinds
    }
    assert reader.calls == []


def test_artifact_catalog_filters_each_item_through_current_rbac() -> None:
    allowed = _artifact("review_report", "allowed")
    forbidden = _artifact("review_report", "forbidden")
    service, _pages, _reader = _service(
        (forbidden, allowed),
        scopes={
            allowed.artifact_id: CONTENT,
            forbidden.artifact_id: OTHER,
        },
    )

    with _client(service) as client:
        response = client.get("/api/v1/artifacts", params={"kind": "review_report"})

    assert response.status_code == 200
    assert [item["artifact_id"] for item in response.json()["items"]] == [allowed.artifact_id]
