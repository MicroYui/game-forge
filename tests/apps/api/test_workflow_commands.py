from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.errors import Conflict
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    DomainScope,
    compute_domain_registry_digest,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.run_handlers.constraint_validation import (
    BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
)
from gameforge.platform.workflow.service import _validate_spec_domain_scope
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import ArtifactRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.spine.ir.snapshot import Snapshot
from tests.apps.api.workflow_command_testkit import (
    CURSOR_KEY,
    actor_context,
    build_harness,
    headers,
    maker_actor,
    publish_base,
    publish_constraint_snapshot,
    resource_etag,
)


def _client(harness) -> TestClient:
    return TestClient(harness.app, base_url="https://gameforge.test")


def _spec_payload(**overrides) -> dict:
    payload = {
        "request_schema_version": "human-spec-upload-request@1",
        "ref_name": "spec/head",
        "expected_ref": None,
        "schema_registry_version": "registry@1",
        "meta_schema_version": "meta@1",
        "domain_scope": {"domain_ids": ["economy"]},
        "content_payload": _spec_content(120),
    }
    payload.update(overrides)
    return payload


def _spec_content(reward_gold: int) -> dict:
    return Snapshot.from_entities_relations(
        [Entity(id="q:spec", type=NodeType.QUEST, attrs={"reward_gold": reward_gold})],
        [],
    ).content_payload


def _base_entities() -> list[Entity]:
    return [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})]


def test_spec_scope_cannot_narrow_registry_owned_ir_domains() -> None:
    definitions = (
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            status="active",
            tags=("auto-apply:ir-all@1",),
        ),
        DomainDefinitionV1(
            domain_id="narrative",
            display_name="Narrative",
            status="active",
            tags=("auto-apply:entity-type:QUEST@1",),
        ),
    )
    registry = DomainRegistryV1(
        registry_version="spec-domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("spec-domains@1", definitions),
    )
    snapshot = Snapshot.from_entities_relations(
        [Entity(id="q:spec", type=NodeType.QUEST)],
        [],
    )

    with pytest.raises(Conflict, match="does not cover"):
        _validate_spec_domain_scope(
            snapshot=snapshot,
            registry=registry,
            declared=DomainScope(domain_ids=("economy",)),
        )


def test_spec_upload_requires_current_domain_permission(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    with Session(harness.engine) as session, session.begin():
        SqlIdentityRepository(session, clock=harness.clock).create(
            principal_id="human:no-role",
            kind="human",
            display_name="No Role",
        )
    harness.use_actor(actor_context(harness, "human:no-role"))
    with Session(harness.engine) as session:
        before = len(session.scalars(select(ArtifactRow)).all())

    with _client(harness) as client:
        forbidden = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers(key="spec:no-role"),
        )
        harness.use_actor(maker_actor(harness))
        allowed = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers(key="spec:authorized"),
        )

    assert forbidden.status_code == 403, forbidden.text
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["ref_value"]["revision"] == 1
    with Session(harness.engine) as session:
        assert len(session.scalars(select(ArtifactRow)).all()) == before + 1


def _patch_payload(harness) -> dict:
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": "content/head",
        "expected_ref": harness.base_ref.model_dump(mode="json"),
        "expected_to_fix": [],
        "preconditions": [],
        "side_effect_risk": "low",
        "ops": [
            {
                "op_id": "set-reward-gold",
                "op": "set_entity_attr",
                "target": "q:1.reward_gold",
                "old_value": 120,
                "new_value": 80,
            }
        ],
        "rationale": "Keep quest rewards within the approved economy envelope.",
        "candidate_export_profiles": [],
    }


def test_human_spec_upload_publishes_ref_and_returns_spec_view(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers(key="spec:1"),
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["view_schema_version"] == "spec-view@1"
    assert body["ref_name"] == "spec/head"
    assert body["ref_value"]["revision"] == 1
    assert body["artifact"]["kind"] == "ir_snapshot"
    assert body["artifact"]["payload_schema_id"] == "ir-core@1"
    assert response.headers["X-Resource-Revision"] == "1"
    assert response.headers["ETag"].startswith('"')
    assert response.headers["Cache-Control"] == "private, no-cache"
    with Session(harness.engine) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=harness.objects,
            default_store_id="local",
        )
        artifact = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(body["artifact"]["artifact_id"])
    assert artifact is not None
    assert artifact.meta["schema_registry_version"] == "registry@1"
    assert artifact.meta["meta_schema_version"] == "meta@1"
    assert artifact.meta["domain_scope"] == {"domain_ids": ["economy"]}


def test_spec_upload_duplicate_exact_request_replays_committed_result(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:2"))
        second = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:2"))
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["ETag"] == second.headers["ETag"]


def test_spec_upload_same_key_different_payload_is_idempotency_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:3"))
        second = client.post(
            "/api/v1/specs",
            json=_spec_payload(content_payload=_spec_content(99)),
            headers=headers(key="spec:3"),
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"


def test_spec_upload_stale_expected_ref_is_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="spec:4"))
        stale = client.post(
            "/api/v1/specs",
            json=_spec_payload(
                content_payload=_spec_content(50),
                expected_ref=None,
            ),
            headers=headers(key="spec:5"),
        )
    assert first.status_code == 201
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"


def test_spec_upload_rejects_noncanonical_ir_without_moving_ref(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        invalid = client.post(
            "/api/v1/specs",
            json=_spec_payload(content_payload={"kind": "spec", "reward_gold": 120}),
            headers=headers(key="spec:invalid-shape"),
        )
        valid = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers(key="spec:after-invalid"),
        )
    assert invalid.status_code == 422
    assert valid.status_code == 201, valid.text
    assert valid.json()["ref_value"]["revision"] == 1


def test_spec_upload_rejects_declared_meta_schema_mismatch(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/specs",
            json=_spec_payload(meta_schema_version="meta@future"),
            headers=headers(key="spec:wrong-meta"),
        )
    assert response.status_code == 409


def test_spec_upload_rejects_unsupported_meta_schema_even_when_declared(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    content = _spec_content(120)
    content["meta_schema_version"] = "meta@future"
    with _client(harness) as client:
        response = client.post(
            "/api/v1/specs",
            json=_spec_payload(
                meta_schema_version="meta@future",
                content_payload=content,
            ),
            headers=headers(key="spec:unsupported-meta"),
        )
    assert response.status_code == 409


def test_human_patch_draft_creates_draft_approval(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches",
            json=_patch_payload(harness),
            headers=headers(key="patch:draft:1"),
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["view_schema_version"] == "patch-artifact-read-view@1"
    assert body["approval_status"] == "draft"
    assert body["workflow_revision"] == 1
    assert body["artifact"]["kind"] == "patch"
    assert response.headers["ETag"] == resource_etag(
        resource_kind="patch",
        resource_id=body["artifact"]["artifact_id"],
        revision=1,
    )


class _ConfigExporter:
    def __init__(self) -> None:
        self.packages: list[ConfigExportPackageV1] = []
        self.bindings: list[tuple[object, object, object]] = []

    def export(
        self,
        *,
        export_profile,
        export_profile_binding,
        run_kind,
        llm_execution_mode,
        preview_snapshot_id,
        preview_payload,
        constraint_snapshot_artifact_id,
        constraints,
    ) -> ConfigExportPackageV1:
        self.bindings.append((export_profile_binding, run_kind, llm_execution_mode))
        del preview_payload, constraints
        content = b"[]"
        package = ConfigExportPackageV1(
            export_profile=export_profile,
            target_environment_profile=ProfileRefV1(profile_id="builtin.environment", version=1),
            env_contract_version="generic-agent-env@1",
            source_preview_artifact_id=preview_snapshot_id,
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            format_schema_id="config-export-files@1",
            files=(
                ConfigExportFileV1(
                    relative_path="quests.json",
                    media_type="application/json",
                    content_sha256=sha256_lowerhex(content),
                    size_bytes=len(content),
                    content_bytes=content,
                ),
            ),
        )
        self.packages.append(package)
        return package


def test_patch_draft_publishes_exact_requested_config_export_candidate(
    tmp_path: Path,
) -> None:
    catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
    exporter = _ConfigExporter()
    harness = build_harness(
        tmp_path,
        execution_profile_catalog=catalog,
        config_exporter=exporter,
    )
    publish_base(harness, entities=_base_entities(), doc_version="design-doc@7")
    constraint = publish_constraint_snapshot(harness, constraints=[])
    profile = ProfileRefV1(profile_id="builtin.config_export", version=1)
    request = _patch_payload(harness)
    request.update(
        {
            "constraint_snapshot_artifact_id": constraint.artifact_id,
            "candidate_export_profiles": [profile.model_dump(mode="json")],
        }
    )
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches",
            json=request,
            headers=headers(key="patch:draft:config-export"),
        )
    assert response.status_code == 201, response.text
    assert len(exporter.packages) == 1

    approval_id = f"approval:patch:{response.json()['artifact']['artifact_id']}"
    preview_id = harness.load_item(approval_id).target_binding.target_artifact_id
    with Session(harness.engine) as session:
        config_id = session.scalars(
            select(ArtifactRow.artifact_id).where(ArtifactRow.kind == "config_export")
        ).one()
        bindings = SqlObjectBindingRepository(
            session,
            object_store=harness.objects,
            default_store_id="local",
        )
        repository = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        )
        config = repository.get(config_id)
        patch = repository.get(response.json()["artifact"]["artifact_id"])
        preview = repository.get(preview_id)
        assert config is not None
        assert patch is not None
        assert preview is not None
        location = bindings.resolve(config.object_ref).location
    with harness.objects.open(location) as source:
        payload = source.read()

    package = exporter.packages[0]
    assert package.export_profile == profile
    exact_binding, run_kind, execution_mode = exporter.bindings[0]
    assert exact_binding.field_path == "/candidate_export_profiles/0"
    assert exact_binding.profile == profile
    assert exact_binding.catalog_version == catalog.catalog_version
    assert exact_binding.catalog_digest == catalog.catalog_digest
    assert run_kind is None
    assert execution_mode == "not_applicable"
    assert package.source_preview_artifact_id == preview_id
    assert package.constraint_snapshot_artifact_id == constraint.artifact_id
    assert payload == canonical_config_export_bytes(package)
    assert set(config.lineage) == {preview_id, constraint.artifact_id}
    assert set(patch.lineage) == {harness.base_artifact_id, constraint.artifact_id}
    assert patch.version_tuple.doc_version == "design-doc@7"
    assert (
        patch.version_tuple.constraint_snapshot_id
        == constraint.version_tuple.constraint_snapshot_id
    )
    assert preview.version_tuple.doc_version == "design-doc@7"
    assert config.version_tuple.doc_version == "design-doc@7"
    assert (
        config.version_tuple.constraint_snapshot_id
        == constraint.version_tuple.constraint_snapshot_id
    )


def test_patch_draft_binds_constraint_without_requesting_config_export(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    constraint = publish_constraint_snapshot(harness, constraints=[])
    request = _patch_payload(harness)
    request["constraint_snapshot_artifact_id"] = constraint.artifact_id

    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches",
            json=request,
            headers=headers(key="patch:draft:constraint-without-export"),
        )

    assert response.status_code == 201, response.text
    patch_id = response.json()["artifact"]["artifact_id"]
    with Session(harness.engine) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=harness.objects,
            default_store_id="local",
        )
        patch = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(patch_id)
        config_count = session.scalars(
            select(ArtifactRow.artifact_id).where(ArtifactRow.kind == "config_export")
        ).all()

    assert patch is not None
    assert set(patch.lineage) == {harness.base_artifact_id, constraint.artifact_id}
    assert (
        patch.version_tuple.constraint_snapshot_id
        == constraint.version_tuple.constraint_snapshot_id
    )
    assert config_count == []


def test_patch_draft_rejects_unknown_export_profile_before_publication(
    tmp_path: Path,
) -> None:
    catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
    harness = build_harness(
        tmp_path,
        execution_profile_catalog=catalog,
        config_exporter=_ConfigExporter(),
    )
    publish_base(harness, entities=_base_entities())
    constraint = publish_constraint_snapshot(harness, constraints=[])
    request = _patch_payload(harness)
    request.update(
        {
            "constraint_snapshot_artifact_id": constraint.artifact_id,
            "candidate_export_profiles": [{"profile_id": "missing", "version": 1}],
        }
    )
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        invalid = client.post(
            "/api/v1/patches",
            json=request,
            headers=headers(key="patch:draft:unknown-export"),
        )
        request["candidate_export_profiles"] = [
            {"profile_id": "builtin.config_export", "version": 1}
        ]
        valid = client.post(
            "/api/v1/patches",
            json=request,
            headers=headers(key="patch:draft:after-unknown-export"),
        )
    assert invalid.status_code == 409
    assert valid.status_code == 201, valid.text


def test_patch_draft_rejects_expected_ref_that_does_not_bind_exact_base(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    payload = _patch_payload(harness)
    payload["expected_ref"] = {
        "artifact_id": "artifact:another-base",
        "revision": harness.base_ref.revision,
    }
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches",
            json=payload,
            headers=headers(key="patch:draft:wrong-base"),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"


def test_patch_draft_duplicate_request_replays(tmp_path: Path) -> None:
    # Advance the clock between the two identical requests: the second request
    # re-assembles a draft whose fresh created_at differs from the committed one, so
    # a broken replay path would raise IntegrityViolation (500) instead of replaying.
    from datetime import timedelta

    from tests.apps.api.workflow_command_testkit import NOW_DT, AdvancingUtcClock

    harness = build_harness(tmp_path, clock=AdvancingUtcClock(NOW_DT, step=timedelta(seconds=1)))
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:2")
        )
        second = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:2")
        )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # Duplicate exact request replays the committed result byte-for-byte.
    assert first.json() == second.json()
    assert first.headers["ETag"] == second.headers["ETag"]
    assert first.json()["artifact"]["created_at"] == second.json()["artifact"]["created_at"]


def test_patch_draft_same_key_different_payload_is_idempotency_conflict(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.apps.api.workflow_command_testkit import NOW_DT, AdvancingUtcClock

    harness = build_harness(tmp_path, clock=AdvancingUtcClock(NOW_DT, step=timedelta(seconds=1)))
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        first = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft:3")
        )
        conflict = client.post(
            "/api/v1/patches",
            json={**_patch_payload(harness), "rationale": "A different rationale entirely."},
            headers=headers(key="patch:draft:3"),
        )
    assert first.status_code == 201, first.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_patch_validate_without_admission_fails_closed(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, admission=False)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    payload = {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": "approval:patch:x",
        "expected_subject_head_revision": 1,
        "expected_workflow_revision": 1,
        "subject_digest": "1" * 64,
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "preview_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content/head",
            "expected_ref": harness.base_ref.model_dump(mode="json"),
        },
        "validation_policy": {"profile_id": "validation.patch", "version": 1},
        "checker_profiles": [],
        "simulation_profiles": [],
        "expected_findings": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=payload,
            headers=headers(key="patch:validate:1"),
        )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "dependency_unavailable"
    assert body["errors"] == [{"component": "run_admission"}]


def test_patch_validate_with_admission_returns_accepted_run(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    payload = {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": "approval:patch:x",
        "expected_subject_head_revision": 1,
        "expected_workflow_revision": 1,
        "subject_digest": "1" * 64,
        "base_snapshot_artifact_id": harness.base_artifact_id,
        "preview_snapshot_artifact_id": harness.base_artifact_id,
        "constraint_snapshot_artifact_id": "artifact:constraint:exact",
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content/head",
            "expected_ref": harness.base_ref.model_dump(mode="json"),
        },
        "validation_policy": {"profile_id": "validation.patch", "version": 1},
        "checker_profiles": [],
        "simulation_profiles": [],
        "expected_findings": [
            {
                "finding_id": "finding:expected",
                "finding_revision": 1,
                "evidence_artifact_id": "artifact:evidence:expected",
                "finding_digest": "a" * 64,
            }
        ],
        "findings": [
            {
                "finding_id": "finding:target",
                "finding_revision": 2,
                "evidence_artifact_id": "artifact:evidence:target",
                "finding_digest": "b" * 64,
            }
        ],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }
    with _client(harness) as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=payload,
            headers=headers(
                key="patch:validate:2",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id="artifact-patch",
                    revision=1,
                ),
            ),
        )
    assert response.status_code == 202, response.text
    assert response.json()["run_id"].startswith("run:patch.validate:")
    call = harness.admission.calls[-1]
    assert call["idempotency_key"] == "patch:validate:2"
    assert call["request"].constraint_snapshot_artifact_id == "artifact:constraint:exact"
    assert tuple(item.finding_id for item in call["request"].expected_findings) == (
        "finding:expected",
    )
    assert tuple(item.finding_id for item in call["request"].findings) == ("finding:target",)


def test_all_validation_routes_reject_etag_for_another_subject(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    common = {
        "expected_subject_head_revision": 1,
        "expected_workflow_revision": 1,
        "subject_digest": "1" * 64,
    }
    cases = (
        (
            "/api/v1/patches/artifact-patch:validate",
            "patch",
            "artifact-patch",
            {
                "request_schema_version": "patch-validation-admission-request@1",
                "approval_id": "approval:patch:x",
                **common,
                "base_snapshot_artifact_id": harness.base_artifact_id,
                "preview_snapshot_artifact_id": harness.base_artifact_id,
                "constraint_snapshot_artifact_id": None,
                "candidate_config_export_artifact_ids": [],
                "target": {
                    "ref_name": "content/head",
                    "expected_ref": harness.base_ref.model_dump(mode="json"),
                },
                "validation_policy": {"profile_id": "validation.patch", "version": 1},
                "checker_profiles": [],
                "simulation_profiles": [],
                "expected_findings": [],
                "findings": [],
                "review_artifact_ids": [],
                "playtest_trace_artifact_ids": [],
                "regression_suite_artifact_ids": [],
            },
        ),
        (
            "/api/v1/constraint-proposals/artifact-constraint:validate",
            "constraint_proposal",
            "artifact-constraint",
            {
                "request_schema_version": "constraint-validation-admission-request@1",
                "approval_id": "approval:constraint:x",
                **common,
                "base_constraint_snapshot_artifact_id": None,
                "target": {
                    "ref_name": "content/head",
                    "expected_ref": harness.base_ref.model_dump(mode="json"),
                },
                "dsl_grammar_version": "dsl@1",
                "compiler_profile": {"profile_id": "compiler", "version": 1},
                "differential_engines": [
                    item.model_dump(mode="json")
                    for item in BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1
                ],
                "golden_suite_artifact_id": None,
                "regression_suite_artifact_ids": [],
                "validation_policy": {
                    "profile_id": "validation.constraint",
                    "version": 1,
                },
            },
        ),
        (
            "/api/v1/rollback-requests/artifact-rollback:validate",
            "rollback_request",
            "artifact-rollback",
            {
                "request_schema_version": "rollback-validation-admission-request@1",
                "approval_id": "approval:rollback:x",
                **common,
                "ref_name": "content/head",
                "expected_current_ref": harness.base_ref.model_dump(mode="json"),
                "target_artifact_id": harness.base_artifact_id,
                "target_history_revision": 1,
                "rollback_profile": {"profile_id": "rollback", "version": 1},
                "schema_compatibility_policy": {
                    "profile_id": "schema-compatibility",
                    "version": 1,
                },
                "impact_profiles": [],
                "regression_suite_artifact_ids": [],
            },
        ),
    )

    with _client(harness) as client:
        for index, (path, resource_kind, resource_id, payload) in enumerate(cases):
            response = client.post(
                path,
                json=payload,
                headers=headers(
                    key=f"validate:wrong-etag:{index}",
                    if_match=resource_etag(
                        resource_kind=resource_kind,
                        resource_id=f"{resource_id}:another",
                        revision=1,
                    ),
                ),
            )
            assert response.status_code == 409, response.text

    assert harness.admission.calls == []


def _draft_and_validate(harness, client) -> tuple[str, str]:
    draft = client.post(
        "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="patch:draft")
    )
    assert draft.status_code == 201, draft.text
    artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{artifact_id}"
    from tests.apps.api.workflow_command_testkit import drive_to_validated

    drive_to_validated(harness, approval_id, run_id="run:patch-validation:1")
    return artifact_id, approval_id


def test_patch_full_lifecycle_submit_approve_apply(tmp_path: Path) -> None:
    from tests.apps.api.workflow_command_testkit import operator_actor, reviewer_actor

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)

        validated = harness.load_item(approval_id)
        submit = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(
                key="patch:submit",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=artifact_id,
                    revision=validated.workflow_revision,
                ),
            ),
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["approval"]["status"] == "pending_approval"

        pending = harness.load_item(approval_id)
        requirement_ids = [r.requirement_id for r in pending.requirements]
        harness.use_actor(reviewer_actor(harness))
        approve = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": requirement_ids,
                "expected_workflow_revision": pending.workflow_revision,
                "reason_code": "independent_review_passed",
            },
            headers=headers(
                key="patch:approve",
                if_match=resource_etag(
                    resource_kind="approval",
                    resource_id=approval_id,
                    revision=pending.workflow_revision,
                ),
            ),
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["approval"]["status"] == "approved"

        approved = harness.load_item(approval_id)
        binding = approved.target_binding
        harness.use_actor(operator_actor(harness))
        apply = client.post(
            f"/api/v1/patches/{approved.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": approved.workflow_revision,
                "subject_digest": approved.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": binding.ref_name,
                "expected_ref": binding.expected_ref.model_dump(mode="json"),
            },
            headers=headers(
                key="patch:apply",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=approved.subject_artifact_id,
                    revision=approved.workflow_revision,
                ),
            ),
        )
    assert apply.status_code == 200, apply.text
    result = apply.json()
    assert result["result_schema_version"] == "workflow-apply-result@1"
    assert result["approval"]["approval"]["status"] == "applied"
    assert result["ref_value"]["artifact_id"] == binding.target_artifact_id
    assert result["ref_transition_id"] is None


def test_submit_stale_workflow_revision_is_conflict(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)
        submit = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": 1,
            },
            headers=headers(
                key="patch:submit:stale",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=artifact_id,
                    revision=harness.load_item(approval_id).workflow_revision,
                ),
            ),
        )
    assert submit.status_code == 409
    assert submit.json()["code"] == "revision_conflict"


def test_submit_path_must_bind_the_exact_subject_artifact(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        _, approval_id = _draft_and_validate(harness, client)
        validated = harness.load_item(approval_id)
        submit = client.post(
            "/api/v1/patches/artifact:another-subject:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(
                key="patch:submit:wrong-path",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=validated.subject_artifact_id,
                    revision=validated.workflow_revision,
                ),
            ),
        )
    assert submit.status_code == 409
    assert submit.json()["code"] == "revision_conflict"


def test_submit_rejects_strong_if_match_for_another_resource_revision(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)
        validated = harness.load_item(approval_id)
        submit = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=headers(
                key="patch:submit:wrong-etag",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=artifact_id,
                    revision=validated.workflow_revision + 1,
                ),
            ),
        )
    assert submit.status_code == 409
    assert submit.json()["code"] == "revision_conflict"


def test_submit_exact_idempotent_replay_precedes_fresh_etag_check(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        artifact_id, approval_id = _draft_and_validate(harness, client)
        validated = harness.load_item(approval_id)
        body = {
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": validated.workflow_revision,
        }
        request_headers = headers(
            key="patch:submit:replay",
            if_match=resource_etag(
                resource_kind="patch",
                resource_id=artifact_id,
                revision=validated.workflow_revision,
            ),
        )
        first = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json=body,
            headers=request_headers,
        )
        replay = client.post(
            f"/api/v1/patches/{artifact_id}:submit-for-approval",
            json=body,
            headers=request_headers,
        )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


def _constraint(id_: str, assert_expr: str) -> dict:
    return {
        "id": id_,
        "dsl_grammar_version": "dsl@1",
        "kind": "numeric",
        "oracle": "deterministic",
        "predicates": [],
        "assert": assert_expr,
        "severity": "major",
    }


def _constraint_payload(**overrides) -> dict:
    payload = {
        "request_schema_version": "human-constraint-draft-request@1",
        "base_constraint_snapshot_artifact_id": None,
        "ref_name": "constraints/head",
        "expected_ref": None,
        "dsl_grammar_version": "dsl@1",
        "domain_scope": {"domain_ids": ["economy"]},
        "constraints": [_constraint("c:reward-cap", "reward_gold <= 100")],
        "source_artifact_ids": [],
        "rationale": "Cap economy reward payouts.",
    }
    payload.update(overrides)
    return payload


def test_human_constraint_draft_and_revision(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        draft = client.post(
            "/api/v1/constraint-proposals",
            json=_constraint_payload(),
            headers=headers(key="constraint:draft"),
        )
        assert draft.status_code == 201, draft.text
        body = draft.json()
        assert body["view_schema_version"] == "constraint-proposal-read-view@1"
        assert body["proposal"]["revision"] == 1
        assert body["workflow_revision"] == 1
        artifact_id = body["artifact"]["artifact_id"]
        approval_id = f"approval:constraint_proposal:{artifact_id}"

        revise = client.post(
            f"/api/v1/constraint-proposals/{artifact_id}:revise",
            json=_constraint_payload(
                request_schema_version="human-constraint-revision-request@1",
                constraints=[_constraint("c:reward-cap", "reward_gold <= 90")],
                approval_id=approval_id,
                expected_subject_head_revision=1,
                expected_workflow_revision=1,
            ),
            headers=headers(
                key="constraint:revise",
                if_match=resource_etag(
                    resource_kind="constraint_proposal",
                    resource_id=artifact_id,
                    revision=1,
                ),
            ),
        )
    assert revise.status_code == 201, revise.text
    revised = revise.json()
    assert revised["proposal"]["revision"] == 2
    assert revised["proposal"]["supersedes_artifact_id"] == artifact_id


def test_constraint_revision_rejects_stale_workflow_revision(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        draft = client.post(
            "/api/v1/constraint-proposals",
            json=_constraint_payload(),
            headers=headers(key="constraint:draft:stale-revision"),
        )
        assert draft.status_code == 201, draft.text
        artifact_id = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:constraint_proposal:{artifact_id}"
        revise = client.post(
            f"/api/v1/constraint-proposals/{artifact_id}:revise",
            json=_constraint_payload(
                request_schema_version="human-constraint-revision-request@1",
                approval_id=approval_id,
                expected_subject_head_revision=1,
                expected_workflow_revision=99,
            ),
            headers=headers(
                key="constraint:revise:stale-revision",
                if_match=resource_etag(
                    resource_kind="constraint_proposal",
                    resource_id=artifact_id,
                    revision=1,
                ),
            ),
        )
    assert revise.status_code == 409
    assert revise.json()["code"] == "revision_conflict"


def test_constraint_revision_path_must_bind_current_subject_artifact(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        draft = client.post(
            "/api/v1/constraint-proposals",
            json=_constraint_payload(),
            headers=headers(key="constraint:draft:wrong-path"),
        )
        assert draft.status_code == 201, draft.text
        artifact_id = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:constraint_proposal:{artifact_id}"
        revise = client.post(
            "/api/v1/constraint-proposals/artifact:another-subject:revise",
            json=_constraint_payload(
                request_schema_version="human-constraint-revision-request@1",
                approval_id=approval_id,
                expected_subject_head_revision=1,
                expected_workflow_revision=1,
            ),
            headers=headers(
                key="constraint:revise:wrong-path",
                if_match=resource_etag(
                    resource_kind="constraint_proposal",
                    resource_id=artifact_id,
                    revision=1,
                ),
            ),
        )
    assert revise.status_code == 409
    assert revise.json()["code"] == "revision_conflict"


def test_constraint_base_must_be_a_constraint_snapshot(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        response = client.post(
            "/api/v1/constraint-proposals",
            json=_constraint_payload(
                base_constraint_snapshot_artifact_id=harness.base_artifact_id,
            ),
            headers=headers(key="constraint:draft:wrong-base-kind"),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "revision_conflict"


def test_patch_rebase_conflict_persists_conflict_set(tmp_path: Path) -> None:
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    with _client(harness) as client:
        # First bind the draft to the live base, then let an intervening approved
        # apply advance the ref.  Creating an already-stale draft is now rejected at
        # publication; rebase handles drift that occurs after a valid draft exists.
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="rebase:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:patch:{patch_artifact}"
        apply_full_patch(
            harness,
            client,
            ref_name="content/head",
            new_value=100,
            key="intervening",
        )
        live_ref = _live_ref(harness, "content/head")
        wrong_etag = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="rebase:wrong-etag",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=2,
                ),
            ),
        )
        assert wrong_etag.status_code == 409
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="rebase:1",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
    assert rebase.status_code == 200, rebase.text
    body = rebase.json()
    assert body["status"] == "conflicted"
    assert body["conflict_set_id"].startswith("conflict-set:")


def test_patch_rebase_rejects_client_stale_head_and_workflow_revisions(
    tmp_path: Path,
) -> None:
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    with _client(harness) as client:
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches",
            json=_patch_payload(harness),
            headers=headers(key="occ:source-draft"),
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:patch:{patch_artifact}"
        apply_full_patch(harness, client, ref_name="content/head", new_value=100, key="occ")
        live_ref = _live_ref(harness, "content/head")
        stale_workflow = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 99,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="occ:stale-workflow",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
        stale_head = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": 99,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="occ:stale-head",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
    assert stale_workflow.status_code == 409
    assert stale_workflow.json()["code"] == "revision_conflict"
    assert stale_head.status_code == 409
    assert stale_head.json()["code"] == "revision_conflict"


def test_patch_rebase_clean_compiles_and_publishes_rebased_draft(tmp_path: Path) -> None:
    from gameforge.contracts.storage import RefValue
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    # a base with two independent fields so the intervening change and the draft touch
    # disjoint JSON paths -> a conflict-free three-way merge (the clean rebase branch).
    publish_base(
        harness,
        entities=[
            Entity(
                id="q:1",
                type=NodeType.QUEST,
                attrs={"reward_gold": 120, "difficulty": "normal"},
            )
        ],
    )
    base_artifact_id = harness.base_artifact_id
    constraint = publish_constraint_snapshot(harness, constraints=[])
    with _client(harness) as client:
        # Create the source draft while its exact base ref is current.
        harness.use_actor(maker_actor(harness))
        source_request = _patch_payload(harness)
        source_request["constraint_snapshot_artifact_id"] = constraint.artifact_id
        draft = client.post(
            "/api/v1/patches", json=source_request, headers=headers(key="clean:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        source_approval_id = f"approval:patch:{patch_artifact}"
        # intervening approved apply advances the ref by changing difficulty only
        apply_full_patch(
            harness,
            client,
            ref_name="content/head",
            key="intervening",
            ops=[
                {
                    "op_id": "set-difficulty",
                    "op": "set_entity_attr",
                    "target": "q:1.difficulty",
                    "old_value": "normal",
                    "new_value": "hard",
                }
            ],
        )
        live_ref = _live_ref(harness, "content/head")
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="clean:rebase",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
        replay = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="clean:rebase",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
    assert rebase.status_code == 200, rebase.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == rebase.json()
    body = rebase.json()
    assert body["status"] == "clean"
    assert body["conflict_set_id"] is None
    new_patch_artifact_id = body["new_patch_artifact_id"]
    assert new_patch_artifact_id is not None

    # the byte-exact rebased draft supersedes the source on the same subject series,
    # carrying its exact preview companion pinned to the live ref (supersession CAS).
    rebased = harness.load_item(f"approval:patch:{new_patch_artifact_id}")
    source = harness.load_item(source_approval_id)
    assert rebased.supersedes_approval_id == source_approval_id
    assert rebased.subject_series_id == source.subject_series_id
    assert rebased.subject_revision == source.subject_revision + 1
    assert rebased.status == "draft"
    assert rebased.target_binding.expected_ref == RefValue.model_validate(live_ref)
    with Session(harness.engine) as session:
        bindings = SqlObjectBindingRepository(
            session,
            object_store=harness.objects,
            default_store_id="local",
        )
        repository = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        )
        source_artifact = repository.get(patch_artifact)
        rebased_artifact = repository.get(new_patch_artifact_id)
    assert source_artifact is not None
    assert rebased_artifact is not None
    assert set(source_artifact.lineage) == {base_artifact_id, constraint.artifact_id}
    assert set(rebased_artifact.lineage) == {patch_artifact, live_ref["artifact_id"]}
    assert (
        rebased_artifact.version_tuple.constraint_snapshot_id
        == constraint.version_tuple.constraint_snapshot_id
    )


def test_patch_resolve_conflicts_publishes_resolved_draft(tmp_path: Path) -> None:
    from gameforge.contracts.diff import (
        ThreeWayMergePolicyV1,
        compute_merge_policy_digest,
    )
    from gameforge.contracts.storage import RefValue
    from gameforge.platform.diff.three_way import compute_three_way_merge
    from gameforge.spine.ir.snapshot import Snapshot
    from tests.apps.api.workflow_command_testkit import apply_full_patch

    harness = build_harness(tmp_path)
    publish_base(harness, entities=_base_entities())
    with _client(harness) as client:
        harness.use_actor(maker_actor(harness))
        draft = client.post(
            "/api/v1/patches", json=_patch_payload(harness), headers=headers(key="resolve:draft")
        )
        assert draft.status_code == 201, draft.text
        patch_artifact = draft.json()["artifact"]["artifact_id"]
        source_approval_id = f"approval:patch:{patch_artifact}"
        # intervening apply changes the SAME field the draft changes -> conflict
        apply_full_patch(harness, client, ref_name="content/head", new_value=100, key="intervening")
        live_ref = _live_ref(harness, "content/head")
        rebase = client.post(
            f"/api/v1/patches/{patch_artifact}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
            },
            headers=headers(
                key="resolve:rebase",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
        assert rebase.status_code == 200, rebase.text
        conflict_set_id = rebase.json()["conflict_set_id"]
        assert conflict_set_id is not None

        # Recompute the exact conflicts the service saw so resolutions cover them.
        policy = ThreeWayMergePolicyV1(
            policy_version="workflow-three-way@1",
            collection_identities=(),
            policy_digest=compute_merge_policy_digest("workflow-three-way@1", ()),
        )
        base_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})], []
        ).content_payload
        current_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 100})], []
        ).content_payload
        proposed_payload = Snapshot.from_entities_relations(
            [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 80})], []
        ).content_payload
        plan = compute_three_way_merge(base_payload, current_payload, proposed_payload, policy)
        assert plan.conflicts, "expected the intervening apply to force a conflict"
        resolutions = [
            {"conflict_id": conflict.id, "choice": "take_proposed"} for conflict in plan.conflicts
        ]

        resolve = client.post(
            f"/api/v1/patches/{patch_artifact}:resolve-conflicts",
            json={
                "request_schema_version": "resolve-conflicts-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": 1,
                "expected_workflow_revision": 1,
                "ref_name": "content/head",
                "expected_ref": live_ref,
                "conflict_set_id": conflict_set_id,
                "resolutions": resolutions,
            },
            headers=headers(
                key="resolve:resolve",
                if_match=resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact,
                    revision=1,
                ),
            ),
        )
    assert resolve.status_code == 200, resolve.text
    body = resolve.json()
    assert body["status"] == "clean"
    new_patch_artifact_id = body["new_patch_artifact_id"]
    assert new_patch_artifact_id is not None

    resolved = harness.load_item(f"approval:patch:{new_patch_artifact_id}")
    source = harness.load_item(source_approval_id)
    assert resolved.supersedes_approval_id == source_approval_id
    assert resolved.subject_series_id == source.subject_series_id
    assert resolved.subject_revision == source.subject_revision + 1
    assert resolved.status == "draft"
    assert resolved.target_binding.expected_ref == RefValue.model_validate(live_ref)


def _live_ref(harness, ref_name: str) -> dict:
    from sqlalchemy.orm import Session

    from gameforge.runtime.persistence.cursor import CursorSigner
    from gameforge.runtime.persistence.refs import SqlRefStore
    from tests.apps.api.workflow_command_testkit import CURSOR_KEY

    with Session(harness.engine) as session:
        value = SqlRefStore(
            session,
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(ref_name)
    return value.model_dump(mode="json")


def _object_generation_count(harness) -> int:
    total = 0
    cursor = None
    while True:
        page = harness.objects.list_versions(cursor)
        total += len(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return total


def _artifact_row_count(harness) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from gameforge.runtime.persistence.models import ArtifactRow

    with Session(harness.engine) as session:
        return session.execute(select(func.count(ArtifactRow.artifact_id))).scalar_one()


def test_failed_publication_leaves_only_a_verified_gc_eligible_orphan(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.use_actor(maker_actor(harness))
    with _client(harness) as client:
        ok = client.post("/api/v1/specs", json=_spec_payload(), headers=headers(key="orphan:1"))
        assert ok.status_code == 201
        committed_generations = _object_generation_count(harness)
        committed_artifacts = _artifact_row_count(harness)
        # a second upload whose ref CAS is stale: the blob is put_verified BEFORE the
        # failing write transaction, so it becomes a referenced-by-nothing orphan.
        failed = client.post(
            "/api/v1/specs",
            json=_spec_payload(content_payload=_spec_content(7)),
            headers=headers(key="orphan:2"),
        )
    assert failed.status_code == 409
    # the DB authority is unchanged: still exactly one committed spec Artifact and ref@1
    assert _artifact_row_count(harness) == committed_artifacts
    # the object store gained exactly one verified orphan generation with no Artifact row
    assert _object_generation_count(harness) == committed_generations + 1
