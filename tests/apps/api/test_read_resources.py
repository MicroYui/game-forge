from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.routers.content import content_read_router
from gameforge.bench.report_contracts import BenchReport
from gameforge.contracts.api import (
    ArtifactSummaryV1,
    ConstraintValidationCompilerBindingViewV1,
    Problem,
    ReviewProducerBindingViewV1,
    SubjectApprovalBindingViewV1,
    TaskSuiteDerivationBindingViewV1,
)
from gameforge.contracts.canonical import canonical_json, canonical_sha256, compute_snapshot_id
from gameforge.contracts.diff import SnapshotDiff
from gameforge.contracts.errors import (
    Conflict,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    NotFound,
    QueryTooBroad,
)
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    ProfileRefV1,
    RunKindRef,
    TaskSuiteDerivationProfileConfigV1,
    canonical_config_hash,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Permission,
    Principal,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    ObjectBinding,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.playtest import TaskSuiteV1
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue
from gameforge.platform.read_models.artifacts import (
    TrustedArtifactPayloadBinding,
    VerifiedArtifactPayload,
)
from gameforge.platform.read_models.authorization import (
    AuthorizedReadCollection,
    ReadAuthorizationBinding,
)
from gameforge.platform.read_models.content import (
    ConstraintValidationCompilerBindingRead,
    ContentReadCapabilities,
    ContentReadService,
    LineageSourceEntry,
    PatchWorkflowReadBinding,
    SnapshotDiffRead,
    SpecReadBinding,
    TaskSuiteDerivationBindingRead,
    _profile_view,
)
from gameforge.platform.read_models.paging import RetainedReadPageItem
from gameforge.platform.registry import build_builtin_registry


DOMAIN = DomainScope(domain_ids=("content",))
OTHER_DOMAIN = DomainScope(domain_ids=("other",))
MULTI_DOMAIN = DomainScope(domain_ids=("content", "other"))
AUTHZ_BINDING = ReadAuthorizationBinding(
    principal_binding="1" * 64,
    authz_fingerprint="2" * 64,
)


def _principal() -> Principal:
    return Principal(
        id="human:reader",
        kind="human",
        display_name="Reader",
        status="active",
        revision=1,
        credential_epoch=0,
        authz_revision=1,
        roles=(),
    )


def _artifact(
    *,
    kind: str,
    schema_id: str,
    payload: dict[str, Any],
    versions: VersionTuple,
    meta: dict[str, Any] | None = None,
):
    payload_bytes = canonical_json(payload).encode("utf-8")
    object_ref = object_ref_for_bytes(payload_bytes)
    artifact = build_artifact_v2(
        kind=kind,
        version_tuple=versions,
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={} if meta is None else meta,
        created_at="2026-07-14T00:00:00Z",
    )
    location = ObjectLocation(
        store_id="local",
        key=object_ref.key,
        backend_generation="generation:1",
    )
    object_binding = ObjectBinding(
        object_ref=object_ref,
        location=location,
        status="active",
        revision=1,
        verified_at="2026-07-14T00:00:00Z",
    )
    verified = VerifiedArtifactPayload(
        artifact=artifact,
        object_binding=object_binding,
        payload_schema_id=schema_id,
        kind=kind,
        metadata={},
        payload_bytes=payload_bytes,
        payload=payload,
    )
    return (
        artifact,
        verified,
        TrustedArtifactPayloadBinding.for_artifact(
            artifact,
            payload_schema_id=schema_id,
        ),
    )


def _spec_artifact(entity_id: str = "npc:guide"):
    payload = {
        "meta_schema_version": "meta@1",
        "entities": {
            entity_id: {
                "type": "NPC",
                "attrs": {"name": "Guide"},
                "schema_version": "ir-core@1",
            }
        },
        "relations": {},
    }
    snapshot_id = compute_snapshot_id(payload)
    artifact, verified, binding = _artifact(
        kind="ir_snapshot",
        schema_id="ir-core@1",
        payload=payload,
        versions=VersionTuple(ir_snapshot_id=snapshot_id, tool_version="ingest@1"),
    )
    return artifact, verified, binding, snapshot_id


def _task_suite_artifact(
    label: str,
    *,
    config_artifact_id: str,
    constraint_artifact_id: str,
    environment_profile: ProfileRefV1,
):
    reset_payload = {
        "scenario_id": f"scenario:{label}",
        "config_export_artifact_id": config_artifact_id,
        "quest_ids": [f"quest:{label}"],
        "start_seed": 0,
    }
    suite = TaskSuiteV1.model_validate(
        {
            "suite_profile": {
                "profile_id": "builtin.task_suite_derivation",
                "version": 2,
            },
            "source_preview_artifact_id": f"preview:{label}",
            "config_export_artifact_id": config_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "environment_profile": environment_profile.model_dump(mode="json"),
            "env_contract_version": "generic-agent-env@1",
            "completion_oracle_registry_ref": {
                "registry_version": 1,
                "digest": "5" * 64,
            },
            "episodes": [
                {
                    "episode_id": f"episode:{label}",
                    "scenario_spec_artifact_id": f"scenario-artifact:{label}",
                    "completion_oracle": {
                        "oracle_id": "all-quests-completed",
                        "version": 1,
                        "params_schema_id": "all-quests-completed-params@1",
                        "params": {"quest_ids": [f"quest:{label}"]},
                    },
                    "domain_scope": DOMAIN.model_dump(mode="json"),
                    "reset_binding": {
                        "reset_schema_id": "generic-env-reset@1",
                        "payload_hash": canonical_sha256(reset_payload),
                        "payload": reset_payload,
                    },
                    "step_budget": 200,
                }
            ],
        }
    )
    return _artifact(
        kind="task_suite",
        schema_id="task-suite@1",
        payload=suite.model_dump(mode="json"),
        versions=VersionTuple(
            ir_snapshot_id=f"snapshot:{label}",
            constraint_snapshot_id=f"constraint-snapshot:{label}",
            tool_version="task-suite-deriver@1",
            env_contract_version="generic-agent-env@1",
        ),
        meta={"domain_scope": DOMAIN.model_dump(mode="json")},
    )


class _Authorization:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []

    def require_singular(self, *, permission, **_: Any):
        self.events.append("authorize")
        if permission is None:
            raise IntegrityViolation("resource domain is not proved")
        return AUTHZ_BINDING

    def filter_collection(self, *, candidates, collection_permission, permission_for, **_: Any):
        self.events.append("authorize-list")
        assert collection_permission is not None
        values = tuple(candidates)
        for value in values:
            assert permission_for(value) is not None
        return AuthorizedReadCollection(items=values, binding=AUTHZ_BINDING)

    def require_collection_continuation(self, **_: Any):
        self.events.append("authorize-list-continuation")
        return AUTHZ_BINDING


class _Repository:
    def __init__(self, artifacts, events: list[str] | None = None) -> None:
        self.artifacts = {item.artifact_id: item for item in artifacts}
        self.events = events if events is not None else []
        self.requested_ids: list[str] = []

    def get_artifact(self, artifact_id: str):
        self.events.append("load-envelope")
        self.requested_ids.append(artifact_id)
        return self.artifacts.get(artifact_id)


class _Reader:
    def __init__(self, values, events: list[str] | None = None) -> None:
        self.values = values
        self.events = events if events is not None else []
        self.calls: list[str] = []

    def read(self, artifact_id: str):
        self.events.append("read-payload")
        self.calls.append(artifact_id)
        return self.values[artifact_id]


class _Bindings:
    def __init__(self, values) -> None:
        self.values = values

    def resolve(self, artifact_id: str):
        return self.values.get(artifact_id)


class _Permissions:
    def for_artifact(self, artifact, *, resource_kind: str):
        return Permission(action="read", resource_kind=resource_kind, domain_scope=DOMAIN)

    def for_ref(self, ref_name, value, artifact):
        del ref_name, value, artifact
        return Permission(action="read", resource_kind="ref", domain_scope=DOMAIN)


class _MappedPermissions:
    def __init__(self, artifact_scopes, ref_scopes=None) -> None:
        self.artifact_scopes = artifact_scopes
        self.ref_scopes = ref_scopes or {}

    def for_artifact(self, artifact, *, resource_kind: str):
        return Permission(
            action="read",
            resource_kind=resource_kind,
            domain_scope=self.artifact_scopes[artifact.artifact_id],
        )

    def for_ref(self, ref_name, value, artifact):
        del ref_name, artifact
        return Permission(
            action="read",
            resource_kind="ref",
            domain_scope=self.ref_scopes[value.revision],
        )


class _ScopeAuthorization:
    def __init__(self, allowed_domain_ids: set[str]) -> None:
        self.allowed_domain_ids = allowed_domain_ids

    def require_singular(self, *, permission, **_: Any):
        if permission is None or permission.domain_scope == "all":
            raise Forbidden("singular permission is not covered")
        if (
            isinstance(permission.domain_scope, DomainScope)
            and set(permission.domain_scope.domain_ids) <= self.allowed_domain_ids
        ):
            return AUTHZ_BINDING
        raise Forbidden("singular permission is not covered")

    def filter_collection(
        self,
        *,
        candidates,
        collection_permission,
        permission_for,
        **_: Any,
    ):
        assert collection_permission.domain_scope == "all"
        selected = []
        for candidate in candidates:
            permission = permission_for(candidate)
            scope = permission.domain_scope
            if isinstance(scope, DomainScope) and set(scope.domain_ids) <= self.allowed_domain_ids:
                selected.append(candidate)
        return AuthorizedReadCollection(items=tuple(selected), binding=AUTHZ_BINDING)


class _Specs:
    def __init__(self, values: dict[str, SpecReadBinding]) -> None:
        self.by_artifact = values
        self.by_snapshot = {value.snapshot_id: value for value in values.values()}
        self.snapshot_lookups: list[str] = []

    def resolve(self, artifact_id: str):
        return self.by_artifact.get(artifact_id)

    def resolve_snapshot_id(self, snapshot_id: str):
        self.snapshot_lookups.append(snapshot_id)
        return self.by_snapshot.get(snapshot_id)


@dataclass
class _ImmutablePages:
    by_index: dict[str, tuple[Any, ...]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def page(self, **kwargs):
        self.calls.append(kwargs)
        return PageV1(
            read_snapshot_id=f"snapshot:{kwargs['index_kind']}",
            items=self.by_index.get(kwargs["index_kind"], ()),
            expires_at="2026-07-14T00:05:00Z",
        )

    def page_lineage(self, **kwargs):
        self.calls.append(kwargs)
        return PageV1(
            read_snapshot_id="snapshot:lineage",
            items=self.by_index.get("artifact_lineage", ()),
            expires_at="2026-07-14T00:05:00Z",
        )


class _MaterializedPages:
    def __init__(self) -> None:
        self.items: tuple[RetainedReadPageItem, ...] = ()
        self.query_hash: str | None = None

    def adapter(self, page_size: int):
        return _MaterializedPage(page_size, self)


class _MaterializedPage:
    def __init__(self, page_size: int, store: _MaterializedPages) -> None:
        self.page_size = page_size
        self.store = store

    def create(self, candidates, *, binding):
        self.store.items = tuple(
            RetainedReadPageItem(
                resource_id=value.resource_id,
                observed_revision=value.observed_revision,
                canonical_view=value.canonical_view,
            )
            for value in candidates
        )
        self.store.query_hash = binding.query_hash
        return self._page(0)

    def page(self, cursor, *, binding):
        assert cursor.query_hash == binding.query_hash == self.store.query_hash
        return self._page(int(cursor.position))

    def _page(self, position: int):
        selected = self.store.items[position : position + self.page_size]
        next_position = position + len(selected)
        next_cursor = None
        if next_position < len(self.store.items):
            assert self.store.query_hash is not None
            next_cursor = PageCursorV1(
                snapshot_id="snapshot:materialized",
                position=str(next_position),
                page_size=self.page_size,
                query_hash=self.store.query_hash,
                opaque_signature="test-signature",
            )
        return PageV1(
            read_snapshot_id="snapshot:materialized",
            items=selected,
            next_cursor=next_cursor,
            expires_at="2026-07-14T00:05:00Z",
        )


class _SchemaRegistry:
    def __init__(self, value=None) -> None:
        self.value = value

    def get(self, version: str):
        del version
        return self.value


class _ProposalWorkflows:
    def resolve(self, artifact_id: str):
        del artifact_id
        return None


class _SubjectWorkflows:
    def __init__(self, patches=None, approval_bindings=None) -> None:
        self.patches = patches or {}
        self.approval_bindings = approval_bindings or {}
        self.approval_binding_calls: list[str] = []

    def resolve_patch(self, artifact_id: str):
        return self.patches.get(artifact_id)

    def resolve_rollback(self, artifact_id: str):
        del artifact_id
        return None

    def resolve_approval_binding(self, artifact_id: str):
        self.approval_binding_calls.append(artifact_id)
        return self.approval_bindings.get(artifact_id)


class _Refs:
    def get_current(self, ref_name: str):
        del ref_name
        return None

    def page_history(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("ref history not expected")


class _HistoryRefs:
    def __init__(self, current: RefValue, history: tuple[RefValue, ...]) -> None:
        self.current = current
        self.history = history

    def get_current(self, ref_name: str):
        del ref_name
        return self.current

    def page_history(self, ref_name, *, cursor, binding, page_size):
        del ref_name, cursor, binding
        assert len(self.history) <= page_size
        return PageV1(
            read_snapshot_id="snapshot:ref-history",
            items=self.history,
            expires_at="2026-07-14T00:05:00Z",
        )


class _Diffs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def read(self, base: str, target: str, *, max_items: int):
        del max_items
        self.calls.append((base, target))
        return SnapshotDiffRead(
            diff=SnapshotDiff(
                base_snapshot_id=base,
                target_snapshot_id=target,
                entry_count=0,
            ),
            entries=(),
        )


class _Bench:
    def __init__(self, artifact_id: str | None = None) -> None:
        self.artifact_id = artifact_id

    def selected_artifact_id(self):
        return self.artifact_id


def _bench_report_payload() -> dict[str, Any]:
    path = Path(__file__).parents[3] / "scenarios" / "bench" / "bench-report.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _Catalog:
    def __init__(self, value=None) -> None:
        self.value = value

    def current_catalog(self):
        return self.value


class _PlaytestResults:
    def result_artifact_id(self, run_id: str):
        del run_id
        return None


class _ReviewProducers:
    def __init__(self, values=None, run_scopes=None) -> None:
        self.values = values or {}
        self.run_scopes = run_scopes or {}
        self.calls: list[tuple[str, str]] = []
        self.run_permission_calls: list[str] = []

    def permission_for_run(self, run_id: str):
        self.run_permission_calls.append(run_id)
        scope = self.run_scopes.get(run_id, DOMAIN)
        return (
            None
            if scope is None
            else Permission(action="read", resource_kind="run", domain_scope=scope)
        )

    def resolve(self, *, artifact, report, run_id: str):
        del report
        self.calls.append((artifact.artifact_id, run_id))
        return self.values.get((artifact.artifact_id, run_id))


def _service(
    *,
    artifacts,
    verified,
    bindings,
    specs,
    immutable_pages=None,
    subject_workflows=None,
    authorization=None,
    permission_resolver=None,
    refs=None,
    events=None,
    max_items: int = 1,
    execution_profiles=None,
    review_producers=None,
    materialized_pages=None,
    bench_reports=None,
):
    repository = _Repository(artifacts, events)
    reader = _Reader(verified, events)
    pages = immutable_pages or _ImmutablePages({})
    diffs = _Diffs()
    materialized_pages = materialized_pages or _MaterializedPages()
    capabilities = ContentReadCapabilities(
        repository=repository,
        immutable_artifact_pages=pages,
        payload_reader=reader,
        payload_bindings=_Bindings(bindings),
        authorization=authorization or _Authorization(events),
        permission_resolver=permission_resolver or _Permissions(),
        specs=_Specs(specs),
        schema_registry=_SchemaRegistry(),
        proposal_workflows=_ProposalWorkflows(),
        subject_workflows=subject_workflows or _SubjectWorkflows(),
        review_producers=review_producers or _ReviewProducers(),
        playtest_results=_PlaytestResults(),
        refs=refs or _Refs(),
        diffs=diffs,
        bench_reports=bench_reports or _Bench(),
        execution_profiles=execution_profiles or _Catalog(),
        page_factory=materialized_pages.adapter,
    )

    @contextmanager
    def uow():
        yield capabilities

    return (
        ContentReadService(uow_factory=uow, max_materialized_items=max_items),
        repository,
        reader,
        pages,
        diffs,
        capabilities.specs,
    )


def test_artifact_summary_accepts_honest_legacy_hashes_but_keeps_v2_strict() -> None:
    for payload_hash in (None, "sha256:legacy-payload"):
        artifact = ArtifactV1(
            artifact_id=f"legacy:{payload_hash or 'missing'}",
            kind="ir_snapshot",
            version_tuple=VersionTuple(tool_version="legacy-reader@1"),
            lineage=[],
            payload_hash=payload_hash,
        )
        summary = ArtifactSummaryV1(
            artifact_id=artifact.artifact_id,
            lineage_schema_version=artifact.lineage_schema_version,
            kind=artifact.kind,
            version_tuple=artifact.version_tuple,
            parent_artifact_ids=tuple(artifact.lineage),
            payload_hash=artifact.payload_hash,
            payload_schema_id=None,
            domain_scope=DOMAIN,
        )

        assert summary.lineage_schema_version == "lineage@1"
        assert summary.payload_hash == payload_hash

    with pytest.raises(ValueError, match="lineage@2 payload_hash"):
        ArtifactSummaryV1(
            artifact_id="current:invalid-hash",
            lineage_schema_version="lineage@2",
            kind="ir_snapshot",
            version_tuple=VersionTuple(tool_version="current-reader@1"),
            parent_artifact_ids=(),
            payload_hash="sha256:not-lowerhex",
            payload_schema_id="ir-core@1",
            domain_scope=DOMAIN,
        )


def test_lineage_summary_keeps_unbound_payload_schema_optional() -> None:
    parent_ref = object_ref_for_bytes(b"parent")
    parent = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="lineage-test@1"),
        lineage=(),
        payload_hash=parent_ref.sha256,
        object_ref=parent_ref,
    )
    root_ref = object_ref_for_bytes(b"root")
    root = build_artifact_v2(
        kind="validation_evidence",
        version_tuple=VersionTuple(tool_version="lineage-test@1"),
        lineage=(parent.artifact_id,),
        payload_hash=root_ref.sha256,
        object_ref=root_ref,
    )
    pages = _ImmutablePages(
        {"artifact_lineage": (LineageSourceEntry(artifact_id=parent.artifact_id, depth=1),)}
    )
    service, *_ = _service(
        artifacts=(root, parent),
        verified={},
        bindings={},
        specs={},
        immutable_pages=pages,
        max_items=10,
    )

    page = service.lineage(
        _principal(),
        root.artifact_id,
        cursor=None,
        limit=10,
    )

    assert len(page.items) == 1
    assert page.items[0].artifact.artifact_id == parent.artifact_id
    assert page.items[0].artifact.payload_schema_id is None


@pytest.mark.parametrize(
    ("allowed_domains", "parent_visible"),
    [
        ({"content"}, False),
        ({"content", "other"}, True),
    ],
)
def test_lineage_uses_all_domain_collection_then_filters_exact_parent_scopes(
    allowed_domains: set[str],
    parent_visible: bool,
) -> None:
    parent_ref = object_ref_for_bytes(b"parent")
    parent = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="lineage-test@1"),
        lineage=(),
        payload_hash=parent_ref.sha256,
        object_ref=parent_ref,
    )
    root_ref = object_ref_for_bytes(b"root")
    root = build_artifact_v2(
        kind="validation_evidence",
        version_tuple=VersionTuple(tool_version="lineage-test@1"),
        lineage=(parent.artifact_id,),
        payload_hash=root_ref.sha256,
        object_ref=root_ref,
    )
    pages = _ImmutablePages(
        {"artifact_lineage": (LineageSourceEntry(artifact_id=parent.artifact_id, depth=1),)}
    )
    service, *_ = _service(
        artifacts=(root, parent),
        verified={},
        bindings={},
        specs={},
        immutable_pages=pages,
        authorization=_ScopeAuthorization(allowed_domains),
        permission_resolver=_MappedPermissions(
            {root.artifact_id: DOMAIN, parent.artifact_id: MULTI_DOMAIN}
        ),
        max_items=10,
    )

    page = service.lineage(_principal(), root.artifact_id, cursor=None, limit=10)

    expected_ids = (parent.artifact_id,) if parent_visible else ()
    assert tuple(item.artifact.artifact_id for item in page.items) == expected_ids


@pytest.mark.parametrize(
    ("allowed_domains", "expected_revisions"),
    [
        ({"content"}, (2,)),
        ({"content", "other"}, (1, 2)),
    ],
)
def test_ref_history_filters_historical_domains_without_binding_to_current_scope(
    allowed_domains: set[str],
    expected_revisions: tuple[int, ...],
) -> None:
    old_ref = object_ref_for_bytes(b"old")
    old = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="ref-test@1"),
        lineage=(),
        payload_hash=old_ref.sha256,
        object_ref=old_ref,
    )
    current_ref = object_ref_for_bytes(b"current")
    current = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="ref-test@1"),
        lineage=(),
        payload_hash=current_ref.sha256,
        object_ref=current_ref,
    )
    history = (
        RefValue(artifact_id=old.artifact_id, revision=1),
        RefValue(artifact_id=current.artifact_id, revision=2),
    )
    service, *_ = _service(
        artifacts=(old, current),
        verified={},
        bindings={},
        specs={},
        authorization=_ScopeAuthorization(allowed_domains),
        permission_resolver=_MappedPermissions(
            {old.artifact_id: OTHER_DOMAIN, current.artifact_id: DOMAIN},
            {1: OTHER_DOMAIN, 2: DOMAIN},
        ),
        refs=_HistoryRefs(history[-1], history),
        max_items=10,
    )

    page = service.ref_history(_principal(), "refs/live", cursor=None, limit=10)

    assert tuple(item.value.revision for item in page.items) == expected_revisions


def test_spec_list_uses_retained_immutable_page_without_full_list_cap() -> None:
    artifact, verified, trusted, snapshot_id = _spec_artifact()
    spec_binding = SpecReadBinding(
        artifact_id=artifact.artifact_id,
        snapshot_id=snapshot_id,
        schema_registry_version="registry@1",
    )
    immutable = _ImmutablePages({"specs": (artifact,)})
    service, _, _, pages, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={artifact.artifact_id: spec_binding},
        immutable_pages=immutable,
        max_items=1,
    )

    page = service.list_specs(_principal(), cursor=None, limit=1)

    assert page.items[0].artifact.artifact_id == artifact.artifact_id
    assert page.items[0].snapshot_id == snapshot_id
    assert len(pages.calls) == 1
    assert pages.calls[0]["index_kind"] == "specs"
    assert pages.calls[0]["expected_artifact_kind"] == "ir_snapshot"
    assert pages.calls[0]["binding"].resource_kind == "specs"


def test_singular_loads_envelope_then_authorizes_before_reading_payload() -> None:
    events: list[str] = []
    artifact, verified, trusted, snapshot_id = _spec_artifact()
    service, _, _, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={
            artifact.artifact_id: SpecReadBinding(
                artifact_id=artifact.artifact_id,
                snapshot_id=snapshot_id,
                schema_registry_version="registry@1",
            )
        },
        events=events,
    )

    service.get_spec(_principal(), artifact.artifact_id)

    assert events[:3] == ["load-envelope", "authorize", "read-payload"]


def test_diff_resolves_snapshot_ids_to_distinct_artifact_ids_before_authorizing() -> None:
    first, first_verified, first_trusted, first_snapshot = _spec_artifact("npc:first")
    second, second_verified, second_trusted, second_snapshot = _spec_artifact("npc:second")
    bindings = {
        first.artifact_id: SpecReadBinding(
            artifact_id=first.artifact_id,
            snapshot_id=first_snapshot,
            schema_registry_version="registry@1",
        ),
        second.artifact_id: SpecReadBinding(
            artifact_id=second.artifact_id,
            snapshot_id=second_snapshot,
            schema_registry_version="registry@1",
        ),
    }
    service, repository, _, _, diffs, specs = _service(
        artifacts=(first, second),
        verified={first.artifact_id: first_verified, second.artifact_id: second_verified},
        bindings={first.artifact_id: first_trusted, second.artifact_id: second_trusted},
        specs=bindings,
    )

    metadata, page = service.diff(
        _principal(),
        base_snapshot_id=first_snapshot,
        target_snapshot_id=second_snapshot,
        cursor=None,
        limit=10,
    )

    assert metadata.base_snapshot_id == first_snapshot
    assert page.items == ()
    assert specs.snapshot_lookups == [first_snapshot, second_snapshot]
    assert repository.requested_ids[:2] == [first.artifact_id, second.artifact_id]
    assert first_snapshot not in repository.requested_ids
    assert diffs.calls == [(first_snapshot, second_snapshot)]


def test_patch_list_binds_artifact_schema_and_workflow_revision_identity() -> None:
    payload = PatchV2(
        revision=1,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:target",
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="typed patch",
    ).model_dump(mode="json")
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@2",
        payload=payload,
        versions=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="patch@2"),
    )
    workflows = _SubjectWorkflows(
        {
            artifact.artifact_id: PatchWorkflowReadBinding(
                workflow_revision=7,
                validation_status="passed",
                regression_status="passed",
                approval_status="pending_approval",
            )
        }
    )
    service, _, _, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        immutable_pages=_ImmutablePages({"patches": (artifact,)}),
        subject_workflows=workflows,
    )

    page = service.list_patches(_principal(), cursor=None, limit=10)

    value = page.items[0]
    assert value.artifact.artifact_id == artifact.artifact_id
    assert value.artifact.payload_schema_id == "patch@2"
    assert value.workflow_revision == 7
    assert value.approval_status == "pending_approval"


def test_patch_list_continuation_uses_the_first_materialized_workflow_projection() -> None:
    def patch_artifact(rationale: str):
        payload = PatchV2(
            revision=1,
            base_snapshot_id="snapshot:base",
            target_snapshot_id=f"snapshot:{rationale}",
            expected_to_fix=[],
            preconditions=[],
            side_effect_risk="low",
            ops=[],
            produced_by="human",
            rationale=rationale,
        ).model_dump(mode="json")
        return _artifact(
            kind="patch",
            schema_id="patch@2",
            payload=payload,
            versions=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="patch@2"),
        )

    first_bundle = patch_artifact("first")
    second_bundle = patch_artifact("second")
    ordered = tuple(
        sorted(
            (first_bundle, second_bundle),
            key=lambda value: value[0].artifact_id,
        )
    )
    workflows = _SubjectWorkflows(
        {
            artifact.artifact_id: PatchWorkflowReadBinding(
                workflow_revision=7,
                validation_status="passed",
                regression_status="passed",
                approval_status="pending_approval",
            )
            for artifact, _, _ in ordered
        }
    )
    service, _, reader, _, _, _ = _service(
        artifacts=tuple(value[0] for value in ordered),
        verified={value[0].artifact_id: value[1] for value in ordered},
        bindings={value[0].artifact_id: value[2] for value in ordered},
        specs={},
        immutable_pages=_ImmutablePages({"patches": tuple(value[0] for value in ordered)}),
        subject_workflows=workflows,
        max_items=2,
    )

    first_page = service.list_patches(_principal(), cursor=None, limit=1)

    assert first_page.next_cursor is not None
    assert first_page.items[0].workflow_revision == 7
    second_artifact_id = ordered[1][0].artifact_id
    workflows.patches[second_artifact_id] = PatchWorkflowReadBinding(
        workflow_revision=8,
        validation_status="failed",
        regression_status="failed",
        approval_status="changes_requested",
    )

    second_page = service.list_patches(
        _principal(),
        cursor=first_page.next_cursor,
        limit=1,
    )

    assert second_page.read_snapshot_id == first_page.read_snapshot_id
    assert second_page.items[0].artifact.artifact_id == second_artifact_id
    assert second_page.items[0].workflow_revision == 7
    assert second_page.items[0].validation_status == "passed"
    assert second_page.items[0].regression_status == "passed"
    assert second_page.items[0].approval_status == "pending_approval"
    assert len(reader.calls) == 2


def test_patch_wrong_payload_schema_fails_closed() -> None:
    payload = PatchV2(
        revision=1,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:target",
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="typed patch",
    ).model_dump(mode="json")
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@1-wrong",
        payload=payload,
        versions=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="patch@2"),
    )
    service, _, _, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        subject_workflows=_SubjectWorkflows(
            {
                artifact.artifact_id: PatchWorkflowReadBinding(
                    workflow_revision=1,
                    validation_status="not_started",
                    regression_status="not_started",
                    approval_status="draft",
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="payload schema"):
        service.get_patch(_principal(), artifact.artifact_id)


@pytest.mark.parametrize("kind", ["source_raw", "source_rendered", "cassette_bundle"])
def test_generic_artifact_endpoint_never_reads_sensitive_payload(kind: str) -> None:
    artifact, verified, trusted = _artifact(
        kind=kind,
        schema_id=f"{kind.replace('_', '-')}@1",
        payload={"prompt": "secret prompt", "raw_response": "secret response"},
        versions=VersionTuple(tool_version="sensitive-artifact@1"),
    )
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
    )

    with pytest.raises(Forbidden, match="not exposed"):
        service.get_artifact(_principal(), artifact.artifact_id)

    assert reader.calls == []


def test_evidence_only_patch_is_hidden_from_workflow_patch_reads() -> None:
    def patch_artifact(rationale: str):
        payload = PatchV2(
            revision=1,
            base_snapshot_id="snapshot:base",
            target_snapshot_id=f"snapshot:{rationale}",
            expected_to_fix=[],
            preconditions=[],
            side_effect_risk="low",
            ops=[],
            produced_by="human",
            rationale=rationale,
        ).model_dump(mode="json")
        return _artifact(
            kind="patch",
            schema_id="patch@2",
            payload=payload,
            versions=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="patch@2"),
        )

    workflow_bundle = patch_artifact("workflow")
    evidence_bundle = patch_artifact("generation-gate-rejected")
    ordered = tuple(
        sorted((workflow_bundle, evidence_bundle), key=lambda value: value[0].artifact_id)
    )
    workflow_artifact = workflow_bundle[0]
    evidence_artifact = evidence_bundle[0]
    service, _, reader, _, _, _ = _service(
        artifacts=tuple(value[0] for value in ordered),
        verified={value[0].artifact_id: value[1] for value in ordered},
        bindings={value[0].artifact_id: value[2] for value in ordered},
        specs={},
        immutable_pages=_ImmutablePages({"patches": tuple(value[0] for value in ordered)}),
        subject_workflows=_SubjectWorkflows(
            {
                workflow_artifact.artifact_id: PatchWorkflowReadBinding(
                    workflow_revision=1,
                    validation_status="not_started",
                    regression_status="not_started",
                    approval_status="draft",
                )
            }
        ),
        max_items=2,
    )

    page = service.list_patches(_principal(), cursor=None, limit=10)

    assert [item.artifact.artifact_id for item in page.items] == [workflow_artifact.artifact_id]
    with pytest.raises(NotFound):
        service.get_patch(_principal(), evidence_artifact.artifact_id)
    generic = service.get_artifact(_principal(), evidence_artifact.artifact_id)
    assert generic.artifact.artifact_id == evidence_artifact.artifact_id
    assert reader.calls.count(evidence_artifact.artifact_id) == 1


def test_subject_approval_binding_authorizes_the_subject_without_reading_payload() -> None:
    payload = PatchV2(
        revision=1,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:target",
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="typed patch",
    ).model_dump(mode="json")
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@2",
        payload=payload,
        versions=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="patch@2"),
    )
    binding = SubjectApprovalBindingViewV1(
        subject_artifact_id=artifact.artifact_id,
        subject_digest=artifact.payload_hash,
        subject_kind="patch",
        subject_series_id="series:patch",
        subject_revision=1,
        subject_head_revision=1,
        is_current_head=True,
        approval_id="approval:patch",
        workflow_revision=4,
        approval_status="pending_approval",
    )

    class _RecordingPermissions(_Permissions):
        def __init__(self) -> None:
            self.resource_kinds: list[str] = []

        def for_artifact(self, artifact, *, resource_kind: str):
            self.resource_kinds.append(resource_kind)
            return super().for_artifact(artifact, resource_kind=resource_kind)

    permissions = _RecordingPermissions()
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        subject_workflows=_SubjectWorkflows(approval_bindings={artifact.artifact_id: binding}),
        permission_resolver=permissions,
    )

    resolved = service.get_subject_approval_binding(_principal(), artifact.artifact_id)

    assert resolved == binding
    assert permissions.resource_kinds == ["patch"]
    assert reader.calls == []


def test_subject_approval_binding_etag_changes_when_the_subject_head_advances() -> None:
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@2",
        payload={"patch_schema_version": "patch@2"},
        versions=VersionTuple(tool_version="patch@2"),
    )
    bindings = {
        artifact.artifact_id: SubjectApprovalBindingViewV1(
            subject_artifact_id=artifact.artifact_id,
            subject_digest=artifact.payload_hash,
            subject_kind="patch",
            subject_series_id="series:patch",
            subject_revision=1,
            subject_head_revision=1,
            is_current_head=True,
            approval_id="approval:patch",
            workflow_revision=4,
            approval_status="pending_approval",
        )
    }
    service, _, _, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        subject_workflows=_SubjectWorkflows(approval_bindings=bindings),
    )
    app = FastAPI()
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:approval-binding",
    )
    client = TestClient(app)

    current = client.get(f"/api/v1/workflow-subjects/{artifact.artifact_id}/approval-binding")
    bindings[artifact.artifact_id] = bindings[artifact.artifact_id].model_copy(
        update={"subject_head_revision": 2, "is_current_head": False}
    )
    historical = client.get(f"/api/v1/workflow-subjects/{artifact.artifact_id}/approval-binding")

    assert current.status_code == historical.status_code == 200
    assert current.headers["x-resource-revision"] == "4"
    assert historical.headers["x-resource-revision"] == "4"
    assert current.json()["is_current_head"] is True
    assert historical.json()["is_current_head"] is False
    assert current.headers["etag"] != historical.headers["etag"]


def test_subject_approval_binding_does_not_invent_an_evidence_only_patch_binding() -> None:
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@2",
        payload={"patch_schema_version": "patch@2"},
        versions=VersionTuple(tool_version="generation-gate@1"),
    )
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
    )

    with pytest.raises(NotFound):
        service.get_subject_approval_binding(_principal(), artifact.artifact_id)

    assert reader.calls == []


def test_subject_approval_binding_checks_domain_rbac_before_disclosing_the_binding() -> None:
    artifact, verified, trusted = _artifact(
        kind="patch",
        schema_id="patch@2",
        payload={"patch_schema_version": "patch@2"},
        versions=VersionTuple(tool_version="patch@2"),
    )
    workflows = _SubjectWorkflows(
        approval_bindings={
            artifact.artifact_id: SubjectApprovalBindingViewV1(
                subject_artifact_id=artifact.artifact_id,
                subject_digest=artifact.payload_hash,
                subject_kind="patch",
                subject_series_id="series:other",
                subject_revision=1,
                subject_head_revision=1,
                is_current_head=True,
                approval_id="approval:other",
                workflow_revision=1,
                approval_status="draft",
            )
        }
    )
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        subject_workflows=workflows,
        authorization=_ScopeAuthorization({"content"}),
        permission_resolver=_MappedPermissions({artifact.artifact_id: OTHER_DOMAIN}),
    )

    with pytest.raises(Forbidden):
        service.get_subject_approval_binding(_principal(), artifact.artifact_id)

    assert workflows.approval_binding_calls == []
    assert reader.calls == []


def test_missing_schema_registry_authority_is_explicit_dependency_failure() -> None:
    service, _, _, _, _, _ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
    )

    with pytest.raises(DependencyUnavailable) as error:
        service.get_schema_registry(_principal(), "registry@missing")

    assert error.value.context["component"] == "schema_registry"


def test_selected_bench_report_read_retains_exact_authorized_artifact_identity() -> None:
    payload = _bench_report_payload()
    artifact, verified, trusted = _artifact(
        kind="bench_report",
        schema_id="bench-report@2",
        payload=payload,
        versions=VersionTuple(
            doc_version="bench-report-fixture@1",
            tool_version="bench-report@2",
        ),
    )
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        bench_reports=_Bench(artifact.artifact_id),
    )

    selected = service.get_bench_report(_principal())

    assert selected.artifact.artifact_id == artifact.artifact_id
    assert selected.artifact.payload_schema_id == "bench-report@2"
    assert selected.payload == payload
    assert reader.calls == [artifact.artifact_id]


def test_selected_bench_report_read_accepts_valid_reports_above_generic_json_limit() -> None:
    payload = _bench_report_payload()
    source = payload["false_positives"][0]
    payload["false_positives"].extend(
        {
            **source,
            "name": f"future_fp_{index}",
            "bucket": f"future_bucket_{index}",
        }
        for index in range(400)
    )
    BenchReport.model_validate(payload)
    assert len(canonical_json(payload).encode("utf-8")) > 64 * 1024
    artifact, verified, trusted = _artifact(
        kind="bench_report",
        schema_id="bench-report@2",
        payload=payload,
        versions=VersionTuple(tool_version="bench-report@2"),
    )
    service, _, reader, _, _, _ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        bench_reports=_Bench(artifact.artifact_id),
    )

    selected = service.get_bench_report(_principal())

    assert selected.artifact.artifact_id == artifact.artifact_id
    assert selected.payload == payload
    assert reader.calls == [artifact.artifact_id]


def test_bench_report_router_exposes_exact_artifact_id_without_wrapping_body() -> None:
    payload = _bench_report_payload()
    artifact, verified, trusted = _artifact(
        kind="bench_report",
        schema_id="bench-report@2",
        payload=payload,
        versions=VersionTuple(
            doc_version="bench-report-fixture@1",
            tool_version="bench-report@2",
        ),
    )
    service, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        bench_reports=_Bench(artifact.artifact_id),
    )
    app = FastAPI()
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:bench-report",
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/bench/report")

    assert response.status_code == 200, response.text
    assert response.headers["X-Artifact-ID"] == artifact.artifact_id
    assert response.json() == BenchReport.model_validate(payload).model_dump(mode="json")
    assert "artifact" not in response.json()


def _review_occurrence_fixture():
    report = ReviewReport(snapshot_id="snapshot:review")
    artifact, verified, trusted = _artifact(
        kind="review_report",
        schema_id="review@1",
        payload=report.model_dump(mode="json"),
        versions=VersionTuple(
            ir_snapshot_id=report.snapshot_id,
            tool_version="review@1",
        ),
    )
    binding = ReviewProducerBindingViewV1(
        review_artifact_id=artifact.artifact_id,
        run_id="run:review",
        attempt_no=1,
        run_kind=RunKindRef(kind="review.run", version=1),
        terminal_status="succeeded",
        terminal_manifest_id="artifact:run-result",
        terminal_manifest_kind="run_result",
        outcome_code="review_completed",
        outcome_policy_id="review-completed",
        outcome_policy_version=1,
        outcome_rule_id="primary",
        manifest_role="output",
        finding_authority="not-applicable",
    )
    return artifact, verified, trusted, binding


def test_review_producer_binding_reads_one_explicit_occurrence() -> None:
    artifact, verified, trusted, binding = _review_occurrence_fixture()
    producers = _ReviewProducers({(artifact.artifact_id, binding.run_id): binding})
    service, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        review_producers=producers,
    )

    result = service.get_review_producer_binding(
        _principal(),
        artifact.artifact_id,
        run_id=binding.run_id,
    )

    assert result == binding
    assert producers.run_permission_calls == [binding.run_id]
    assert producers.calls == [(artifact.artifact_id, binding.run_id)]


def test_review_producer_binding_rejects_a_non_occurrence_without_guessing() -> None:
    artifact, verified, trusted, _binding = _review_occurrence_fixture()
    producers = _ReviewProducers()
    service, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        review_producers=producers,
    )

    with pytest.raises(NotFound, match="occurrence"):
        service.get_review_producer_binding(
            _principal(),
            artifact.artifact_id,
            run_id="run:other",
        )

    assert producers.calls == [(artifact.artifact_id, "run:other")]


def test_review_producer_binding_authorizes_before_payload_or_run_disclosure() -> None:
    artifact, verified, trusted, binding = _review_occurrence_fixture()
    producers = _ReviewProducers({(artifact.artifact_id, binding.run_id): binding})
    service, _repository, reader, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        review_producers=producers,
        authorization=_ScopeAuthorization({"content"}),
        permission_resolver=_MappedPermissions({artifact.artifact_id: OTHER_DOMAIN}),
    )

    with pytest.raises(Forbidden):
        service.get_review_producer_binding(
            _principal(),
            artifact.artifact_id,
            run_id=binding.run_id,
        )

    assert reader.calls == []
    assert producers.run_permission_calls == []
    assert producers.calls == []


def test_review_producer_binding_requires_candidate_run_read_permission() -> None:
    artifact, verified, trusted, binding = _review_occurrence_fixture()
    producers = _ReviewProducers(
        {(artifact.artifact_id, binding.run_id): binding},
        run_scopes={binding.run_id: OTHER_DOMAIN},
    )
    service, _repository, reader, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
        review_producers=producers,
        authorization=_ScopeAuthorization({"content"}),
    )

    with pytest.raises(Forbidden):
        service.get_review_producer_binding(
            _principal(),
            artifact.artifact_id,
            run_id=binding.run_id,
        )

    assert producers.run_permission_calls == [binding.run_id]
    assert reader.calls == []
    assert producers.calls == []


def test_generation_review_read_accepts_only_the_exact_requirement_occurrence() -> None:
    report = ReviewReport(snapshot_id="snapshot:generation-review")
    payload = {**report.model_dump(mode="json"), "requirement_id": "generation-gate:review"}
    artifact, verified, trusted = _artifact(
        kind="review_report",
        schema_id="review@1",
        payload=payload,
        versions=VersionTuple(
            ir_snapshot_id=report.snapshot_id,
            tool_version="generation-gate@1",
        ),
        meta={"requirement_id": "generation-gate:review"},
    )
    service, *_ = _service(
        artifacts=(artifact,),
        verified={artifact.artifact_id: verified},
        bindings={artifact.artifact_id: trusted},
        specs={},
    )

    assert service.get_review(_principal(), artifact.artifact_id).report == report

    corrupt, corrupt_verified, corrupt_trusted = _artifact(
        kind="review_report",
        schema_id="review@1",
        payload=payload,
        versions=VersionTuple(
            ir_snapshot_id=report.snapshot_id,
            tool_version="generation-gate@1",
        ),
        meta={"requirement_id": "generation-gate:other"},
    )
    corrupt_service, *_ = _service(
        artifacts=(corrupt,),
        verified={corrupt.artifact_id: corrupt_verified},
        bindings={corrupt.artifact_id: corrupt_trusted},
        specs={},
    )
    with pytest.raises(IntegrityViolation, match="requirement binding differs"):
        corrupt_service.get_review(_principal(), corrupt.artifact_id)


def _builtin_profile_catalog():
    return build_builtin_registry().list_execution_profile_catalogs()[-1]


def test_active_task_suite_derivation_profile_projects_exact_target_environment() -> None:
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(_builtin_profile_catalog()),
    )

    page = service.list_execution_profiles(
        _principal(),
        profile_kind="task_suite_derivation",
        run_kind=None,
        domain_id=None,
        status="active",
        cursor=None,
        limit=10,
    )

    assert len(page.items) == 1
    assert page.items[0].profile.profile == ProfileRefV1(
        profile_id="builtin.task_suite_derivation",
        version=2,
    )
    assert page.items[0].profile.target_environment_profile == ProfileRefV1(
        profile_id="builtin.environment",
        version=1,
    )


def test_task_suite_derivation_projection_keeps_profile_v2_with_v1_config_compatible() -> None:
    catalog = _builtin_profile_catalog()
    definition = next(
        item
        for item in catalog.definitions
        if item.profile
        == ProfileRefV1(
            profile_id="builtin.task_suite_derivation",
            version=2,
        )
    )
    lifecycle = next(item for item in catalog.lifecycle if item.profile == definition.profile)
    config = TaskSuiteDerivationProfileConfigV1().model_dump(mode="json")
    historical_config_definition = ExecutionProfileDefinitionV1.model_validate(
        {
            **definition.model_dump(mode="json"),
            "config_schema_id": "task_suite_derivation-profile-config@1",
            "config": config,
            "config_hash": canonical_config_hash(config),
        }
    )

    view = _profile_view(historical_config_definition, lifecycle)

    assert view.target_environment_profile is None


def test_task_suite_derivation_projection_uses_v2_config_for_later_profile_ref() -> None:
    catalog = _builtin_profile_catalog()
    definition = next(
        item
        for item in catalog.definitions
        if item.profile
        == ProfileRefV1(
            profile_id="builtin.task_suite_derivation",
            version=2,
        )
    )
    lifecycle = next(item for item in catalog.lifecycle if item.profile == definition.profile)
    later_ref = ProfileRefV1(profile_id="custom.task_suite_derivation", version=3)
    later_definition = ExecutionProfileDefinitionV1.model_validate(
        {
            **definition.model_dump(mode="json"),
            "profile": later_ref.model_dump(mode="json"),
        }
    )
    later_lifecycle = ExecutionProfileLifecycleV1.model_validate(
        {
            **lifecycle.model_dump(mode="json"),
            "profile": later_ref.model_dump(mode="json"),
        }
    )

    view = _profile_view(later_definition, later_lifecycle)

    assert view.target_environment_profile == ProfileRefV1(
        profile_id="builtin.environment",
        version=1,
    )


def test_task_suite_filters_materialize_exact_verified_payloads_with_stable_pagination() -> None:
    environment = ProfileRefV1(profile_id="builtin.environment", version=1)
    first = _task_suite_artifact(
        "first",
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile=environment,
    )
    second = _task_suite_artifact(
        "second",
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile=environment,
    )
    unrelated = _task_suite_artifact(
        "unrelated",
        config_artifact_id="config:other",
        constraint_artifact_id="constraint:other",
        environment_profile=ProfileRefV1(profile_id="builtin.environment", version=2),
    )
    triples = (first, second, unrelated)
    artifacts = tuple(sorted((item[0] for item in triples), key=lambda item: item.artifact_id))
    verified = {item[0].artifact_id: item[1] for item in triples}
    bindings = {item[0].artifact_id: item[2] for item in triples}
    pages = _ImmutablePages({"task_suites": artifacts})
    retained = _MaterializedPages()
    service, *_ = _service(
        artifacts=artifacts,
        verified=verified,
        bindings=bindings,
        specs={},
        immutable_pages=pages,
        materialized_pages=retained,
        max_items=10,
    )

    page = service.list_task_suites(
        _principal(),
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile_id=environment.profile_id,
        environment_profile_version=environment.version,
        cursor=None,
        limit=1,
    )
    assert page.next_cursor is not None
    continued = service.list_task_suites(
        _principal(),
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile_id=environment.profile_id,
        environment_profile_version=environment.version,
        cursor=page.next_cursor,
        limit=1,
    )

    expected_ids = tuple(sorted((first[0].artifact_id, second[0].artifact_id)))
    assert tuple(item.artifact.artifact_id for item in (*page.items, *continued.items)) == (
        expected_ids
    )
    assert continued.read_snapshot_id == page.read_snapshot_id
    assert continued.next_cursor is None
    assert pages.calls[0]["filters"] == {}
    assert retained.query_hash == page.next_cursor.query_hash


def test_task_suite_materialized_filter_authorizes_before_reading_payload() -> None:
    environment = ProfileRefV1(profile_id="builtin.environment", version=1)
    allowed = _task_suite_artifact(
        "allowed",
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile=environment,
    )
    denied = _task_suite_artifact(
        "denied",
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile=environment,
    )
    artifacts = tuple(sorted((allowed[0], denied[0]), key=lambda item: item.artifact_id))
    pages = _ImmutablePages({"task_suites": artifacts})
    service, _repository, reader, *_ = _service(
        artifacts=artifacts,
        verified={allowed[0].artifact_id: allowed[1], denied[0].artifact_id: denied[1]},
        bindings={allowed[0].artifact_id: allowed[2], denied[0].artifact_id: denied[2]},
        specs={},
        immutable_pages=pages,
        authorization=_ScopeAuthorization({"content"}),
        permission_resolver=_MappedPermissions(
            {allowed[0].artifact_id: DOMAIN, denied[0].artifact_id: OTHER_DOMAIN}
        ),
        max_items=10,
    )

    page = service.list_task_suites(
        _principal(),
        config_artifact_id="config:target",
        constraint_artifact_id=None,
        environment_profile_id=None,
        environment_profile_version=None,
        cursor=None,
        limit=10,
    )

    assert tuple(item.artifact.artifact_id for item in page.items) == (allowed[0].artifact_id,)
    assert reader.calls == [allowed[0].artifact_id]


def test_task_suite_materialized_filter_fails_typed_when_source_set_exceeds_bound() -> None:
    environment = ProfileRefV1(profile_id="builtin.environment", version=1)
    first = _task_suite_artifact(
        "bounded-first",
        config_artifact_id="config:target",
        constraint_artifact_id="constraint:target",
        environment_profile=environment,
    )
    second = _task_suite_artifact(
        "bounded-second",
        config_artifact_id="config:other",
        constraint_artifact_id="constraint:other",
        environment_profile=environment,
    )
    artifacts = tuple(sorted((first[0], second[0]), key=lambda item: item.artifact_id))
    service, _repository, reader, *_ = _service(
        artifacts=artifacts,
        verified={first[0].artifact_id: first[1], second[0].artifact_id: second[1]},
        bindings={first[0].artifact_id: first[2], second[0].artifact_id: second[2]},
        specs={},
        immutable_pages=_ImmutablePages({"task_suites": artifacts}),
        max_items=1,
    )

    with pytest.raises(QueryTooBroad, match="materialization bound"):
        service.list_task_suites(
            _principal(),
            config_artifact_id="config:target",
            constraint_artifact_id=None,
            environment_profile_id=None,
            environment_profile_version=None,
            cursor=None,
            limit=1,
        )
    assert reader.calls == []


def test_task_suite_derivation_binding_reads_complete_active_authority() -> None:
    catalog = _builtin_profile_catalog()
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )

    result = service.get_task_suite_derivation_binding(
        _principal(),
        profile_id="builtin.task_suite_derivation",
        version=2,
    )

    assert isinstance(result, TaskSuiteDerivationBindingRead)
    assert isinstance(result.binding, TaskSuiteDerivationBindingViewV1)
    assert result.catalog_version == catalog.catalog_version
    assert result.binding.derivation_profile.version == 2
    assert result.binding.target_environment_profile.profile_id == "builtin.environment"
    assert result.binding.completion_oracle_registry_ref.registry_version == 1
    assert result.binding.max_scenarios == 1024
    assert result.binding.max_total_prepared_artifact_bytes == 256 * 1024 * 1024


def test_task_suite_derivation_binding_reuses_exact_execution_profile_rbac() -> None:
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(_builtin_profile_catalog()),
        authorization=_ScopeAuthorization({"content"}),
    )

    with pytest.raises(Forbidden):
        service.get_task_suite_derivation_binding(
            _principal(),
            profile_id="builtin.task_suite_derivation",
            version=2,
        )


def test_task_suite_derivation_binding_rejects_non_active_target_environment() -> None:
    catalog = _builtin_profile_catalog()
    replay_only_environment = catalog.model_copy(
        update={
            "lifecycle": tuple(
                item.model_copy(update={"state": "replay_only"})
                if item.profile.profile_id == "builtin.environment"
                else item
                for item in catalog.lifecycle
            )
        }
    )
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(replay_only_environment),
    )

    with pytest.raises(Conflict, match="target environment.*active"):
        service.get_task_suite_derivation_binding(
            _principal(),
            profile_id="builtin.task_suite_derivation",
            version=2,
        )


def test_constraint_validation_compiler_binding_reads_exact_active_authority() -> None:
    catalog = _builtin_profile_catalog()
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )

    result = service.get_constraint_validation_compiler_binding(
        _principal(),
        profile_id="builtin.constraint_compiler",
        version=1,
    )

    assert isinstance(result, ConstraintValidationCompilerBindingRead)
    assert isinstance(result.binding, ConstraintValidationCompilerBindingViewV1)
    assert result.catalog_version == catalog.catalog_version
    assert result.binding.compiler_profile.profile_id == "builtin.constraint_compiler"
    assert tuple(item.engine_id for item in result.binding.differential_engines) == (
        "clingo",
        "graph-reference",
        "numeric-reference",
        "z3",
    )


@pytest.mark.parametrize(
    "update",
    [
        {"config_schema_id": "constraint_compiler-profile-config@999"},
        {"compatible_run_kinds": (RunKindRef(kind="checker.run", version=1),)},
        {"input_schema_ids": ("checker-run@1",)},
        {"output_schema_ids": ("checker-report@1",)},
        {"stochastic": True},
        {"required_capabilities": ("reasoning",)},
    ],
    ids=(
        "config-schema",
        "compatible-run-kinds",
        "input-schemas",
        "output-schemas",
        "stochastic",
        "required-capabilities",
    ),
)
def test_constraint_validation_compiler_binding_rejects_worker_adapter_drift(
    update: dict[str, object],
) -> None:
    catalog = _builtin_profile_catalog()
    mutated = catalog.model_copy(
        update={
            "definitions": tuple(
                item.model_copy(update=update)
                if item.profile.profile_id == "builtin.constraint_compiler"
                else item
                for item in catalog.definitions
            )
        }
    )
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(mutated),
    )

    with pytest.raises(Conflict, match="adapter contract"):
        service.get_constraint_validation_compiler_binding(
            _principal(),
            profile_id="builtin.constraint_compiler",
            version=1,
        )


def test_constraint_validation_compiler_binding_has_typed_missing_kind_and_dependency_errors() -> (
    None
):
    missing_service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
    )
    with pytest.raises(DependencyUnavailable) as unavailable:
        missing_service.get_constraint_validation_compiler_binding(
            _principal(),
            profile_id="builtin.constraint_compiler",
            version=1,
        )
    assert unavailable.value.context["component"] == "execution_profile_catalog"

    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(_builtin_profile_catalog()),
    )
    with pytest.raises(NotFound):
        service.get_constraint_validation_compiler_binding(
            _principal(),
            profile_id="missing.compiler",
            version=1,
        )
    with pytest.raises(Conflict, match="constraint_compiler"):
        service.get_constraint_validation_compiler_binding(
            _principal(),
            profile_id="builtin.validation",
            version=1,
        )


def test_constraint_validation_compiler_binding_router_etag_covers_complete_binding() -> None:
    catalog = _builtin_profile_catalog()
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )
    app = FastAPI()
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:compiler-binding",
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/execution-profiles/builtin.constraint_compiler/versions/1/"
            "constraint-validation-binding"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    digest = canonical_sha256(
        {
            "etag_schema_version": "constraint-validation-compiler-binding-etag@1",
            "binding": body,
        }
    )
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["x-resource-revision"] == str(catalog.catalog_version)
    assert response.headers["cache-control"] == "private, no-cache"


def _compiler_binding_http_response(catalog, profile_id: str):
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:compiler-binding-error",
    )
    with TestClient(app) as client:
        return client.get(
            f"/api/v1/execution-profiles/{profile_id}/versions/1/constraint-validation-binding"
        )


def test_constraint_validation_compiler_binding_maps_exact_read_failures() -> None:
    catalog = _builtin_profile_catalog()

    missing = _compiler_binding_http_response(catalog, "missing.compiler")
    wrong_kind = _compiler_binding_http_response(catalog, "builtin.validation")
    unavailable = _compiler_binding_http_response(None, "builtin.constraint_compiler")

    inactive_catalog = catalog.model_copy(
        update={
            "lifecycle": tuple(
                item.model_copy(update={"state": "disabled"})
                if item.profile.profile_id == "builtin.constraint_compiler"
                else item
                for item in catalog.lifecycle
            )
        }
    )
    inactive = _compiler_binding_http_response(
        inactive_catalog,
        "builtin.constraint_compiler",
    )

    without_compiler_lifecycle = catalog.model_copy(
        update={
            "lifecycle": tuple(
                item
                for item in catalog.lifecycle
                if item.profile.profile_id != "builtin.constraint_compiler"
            )
        }
    )
    corrupt = _compiler_binding_http_response(
        without_compiler_lifecycle,
        "builtin.constraint_compiler",
    )

    assert (missing.status_code, Problem.model_validate(missing.json()).code) == (
        404,
        "not_found",
    )
    assert (wrong_kind.status_code, Problem.model_validate(wrong_kind.json()).code) == (
        409,
        "revision_conflict",
    )
    assert (inactive.status_code, Problem.model_validate(inactive.json()).code) == (
        409,
        "revision_conflict",
    )
    assert (unavailable.status_code, Problem.model_validate(unavailable.json()).code) == (
        503,
        "dependency_unavailable",
    )
    assert (corrupt.status_code, Problem.model_validate(corrupt.json()).code) == (
        500,
        "integrity_violation",
    )


def test_task_suite_derivation_binding_router_etag_covers_complete_binding() -> None:
    catalog = _builtin_profile_catalog()
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )
    app = FastAPI()
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:task-suite-binding",
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/execution-profiles/builtin.task_suite_derivation/versions/2/"
            "task-suite-derivation-binding"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    digest = canonical_sha256(
        {
            "etag_schema_version": "task-suite-derivation-binding-etag@1",
            "binding": body,
        }
    )
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["x-resource-revision"] == str(catalog.catalog_version)
    assert response.headers["cache-control"] == "private, no-cache"


def _task_suite_binding_http_response(catalog, profile_id: str, version: int = 2):
    service, *_ = _service(
        artifacts=(),
        verified={},
        bindings={},
        specs={},
        execution_profiles=_Catalog(catalog),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:task-suite-binding-error",
    )
    with TestClient(app) as client:
        return client.get(
            f"/api/v1/execution-profiles/{profile_id}/versions/{version}/"
            "task-suite-derivation-binding"
        )


def test_task_suite_derivation_binding_maps_exact_read_failures() -> None:
    catalog = _builtin_profile_catalog()
    missing = _task_suite_binding_http_response(catalog, "missing.derivation")
    wrong_kind = _task_suite_binding_http_response(catalog, "builtin.environment", 1)
    unavailable = _task_suite_binding_http_response(
        None,
        "builtin.task_suite_derivation",
    )
    replay_only_catalog = catalog.model_copy(
        update={
            "lifecycle": tuple(
                item.model_copy(update={"state": "replay_only"})
                if item.profile.profile_id == "builtin.task_suite_derivation"
                and item.profile.version == 2
                else item
                for item in catalog.lifecycle
            )
        }
    )
    replay_only = _task_suite_binding_http_response(
        replay_only_catalog,
        "builtin.task_suite_derivation",
    )
    without_lifecycle = catalog.model_copy(
        update={
            "lifecycle": tuple(
                item
                for item in catalog.lifecycle
                if not (
                    item.profile.profile_id == "builtin.task_suite_derivation"
                    and item.profile.version == 2
                )
            )
        }
    )
    corrupt = _task_suite_binding_http_response(
        without_lifecycle,
        "builtin.task_suite_derivation",
    )

    assert (missing.status_code, Problem.model_validate(missing.json()).code) == (
        404,
        "not_found",
    )
    assert (wrong_kind.status_code, Problem.model_validate(wrong_kind.json()).code) == (
        409,
        "revision_conflict",
    )
    assert (replay_only.status_code, Problem.model_validate(replay_only.json()).code) == (
        409,
        "revision_conflict",
    )
    assert (unavailable.status_code, Problem.model_validate(unavailable.json()).code) == (
        503,
        "dependency_unavailable",
    )
    assert (corrupt.status_code, Problem.model_validate(corrupt.json()).code) == (
        500,
        "integrity_violation",
    )


def test_content_router_exports_every_frozen_content_read_path() -> None:
    @contextmanager
    def unused_uow():
        raise AssertionError("OpenAPI construction must not execute a read")
        yield  # pragma: no cover

    service = ContentReadService(uow_factory=unused_uow, max_materialized_items=10)
    app = FastAPI()
    app.include_router(content_read_router(service))
    openapi = app.openapi()
    paths = set(openapi["paths"])

    assert {
        "/api/v1/artifacts/{artifact_id}",
        "/api/v1/artifacts/{artifact_id}/lineage",
        "/api/v1/specs",
        "/api/v1/specs/{artifact_id}",
        "/api/v1/specs/{artifact_id}/graph",
        "/api/v1/schema-registry/{version}",
        "/api/v1/constraints",
        "/api/v1/constraints/{artifact_id}",
        "/api/v1/constraint-proposals",
        "/api/v1/constraint-proposals/{artifact_id}",
        "/api/v1/patches",
        "/api/v1/patches/{artifact_id}",
        "/api/v1/rollback-requests",
        "/api/v1/rollback-requests/{artifact_id}",
        "/api/v1/workflow-subjects/{artifact_id}/approval-binding",
        "/api/v1/reviews",
        "/api/v1/reviews/{artifact_id}",
        "/api/v1/reviews/{artifact_id}/producer-binding",
        "/api/v1/task-suites",
        "/api/v1/task-suites/{artifact_id}",
        "/api/v1/playtest/{run_id}/result",
        "/api/v1/diff",
        "/api/v1/refs/{ref_name}/history",
        "/api/v1/bench/report",
        "/api/v1/execution-profiles",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
    } <= paths

    profile_response = openapi["paths"][
        "/api/v1/execution-profiles/{profile_id}/versions/{version}"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert profile_response["$ref"].endswith("/ExecutionProfileViewV1")

    profile_page_response = openapi["paths"]["/api/v1/execution-profiles"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    page_schema_name = profile_page_response["$ref"].rsplit("/", 1)[-1]
    page_schema = openapi["components"]["schemas"][page_schema_name]
    item_ref = page_schema["properties"]["items"]["items"]["$ref"]
    assert item_ref.endswith("/ExecutionProfileViewV1")

    compiler_binding_response = openapi["paths"][
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert compiler_binding_response["$ref"].endswith("/ConstraintValidationCompilerBindingViewV1")

    task_suite_binding_response = openapi["paths"][
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert task_suite_binding_response["$ref"].endswith("/TaskSuiteDerivationBindingViewV1")


def test_content_router_maps_invalid_cross_field_profile_queries_to_typed_422() -> None:
    @contextmanager
    def unused_uow():
        raise AssertionError("invalid query must fail before opening a read UoW")
        yield  # pragma: no cover

    service = ContentReadService(uow_factory=unused_uow, max_materialized_items=10)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(content_read_router(service))
    app.dependency_overrides[require_actor] = lambda: ActorContext(
        principal=_principal(),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:reader",
        ),
        session_id="session:reader",
        request_id="request:profile-query",
    )
    client = TestClient(app)

    responses = (
        client.get(
            "/api/v1/task-suites",
            params={"environment_profile_id": "env:local", "environment_profile_version": 0},
        ),
        client.get(
            "/api/v1/execution-profiles",
            params={"run_kind": "checker.run", "run_kind_version": 0},
        ),
        client.get("/api/v1/execution-profiles/env:local/versions/0"),
        client.get(
            "/api/v1/execution-profiles/builtin.task_suite_derivation/versions/0/"
            "task-suite-derivation-binding"
        ),
    )

    for response in responses:
        assert response.status_code == 422
        assert Problem.model_validate(response.json()).code == "request_schema_invalid"
