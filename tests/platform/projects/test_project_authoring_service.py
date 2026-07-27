from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select

from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.api import RunAcceptedV1
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    GenerationProposePayloadV2,
    PromptGoalBindingV1,
    RetryPolicyRefV1,
    RunEvent,
    RunQueuedDataV1,
    RunRecord,
    canonical_payload_hash,
)
from gameforge.contracts.projects import (
    ProjectArchiveRequestV1,
    ProjectCreateRequestV1,
    ProjectExtractionCreateRequestV1,
    ProjectExtractionDiscardRequestV1,
    ProjectGraphDraftRequestV1,
    ProjectIdentityAliasDeclareRequestV1,
    ProjectIdentityAliasV1,
    ProjectMaterialRenameRequestV1,
    ProjectMaterialTextRequestV1,
)
from gameforge.platform.projects import ProjectAuthoringService, ProjectCommandContext
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store.local import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import AuditRow, Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.projects import SqlProjectRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4c.handler_support import build_envelope, execution_plan


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
SCOPE = DomainScope(domain_ids=("game-content",))
NOW_TEXT = "2026-07-24T00:00:00Z"


class _ProjectAdmission:
    """Persist a contract-valid queued generation Run for project service tests."""

    def __init__(self, unit_of_work) -> None:
        self._unit_of_work = unit_of_work
        self.plan = execution_plan({"generation": "openai/gpt-5.6-sol/pre-m4@1"})
        self.option_requests = []
        self.admission_requests = []

    def resolve_execution_option(self, *, request, actor):
        self.option_requests.append((request, actor))
        return type("Resolved", (), {"execution_version_plan": self.plan})()

    def admit_generation(self, **kwargs):
        self.admission_requests.append(kwargs)
        actor = kwargs["actor"]
        server = kwargs["server"]
        goal_id = "artifact:project-goal:" + server.request_hash[:16]
        params = GenerationProposePayloadV2(
            base_snapshot_artifact_id=kwargs["base_snapshot_artifact_id"],
            source_artifact_ids=kwargs["source_artifact_ids"],
            constraint_snapshot_artifact_id=kwargs["constraint_snapshot_artifact_id"],
            findings=kwargs["findings"],
            objective_goal=PromptGoalBindingV1(
                source_artifact_id=goal_id,
                expected_payload_hash="a" * 64,
            ),
            domain_scope=kwargs["domain_scope"],
            target=kwargs["target"],
            generation_policy=kwargs["generation_policy"],
            candidate_export_profiles=kwargs["candidate_export_profiles"],
        )
        envelope = build_envelope(
            params=params,
            llm_execution_mode=kwargs["llm_execution_mode"],
            plan=kwargs["execution_version_plan"],
            cassette_artifact_id=kwargs["cassette_artifact_id"],
        )
        run_id = (
            "run:project-extraction:"
            + canonical_sha256(
                {
                    "request_hash": server.request_hash,
                    "idempotency_key": server.idempotency_key,
                }
            )[:24]
        )
        run = RunRecord(
            run_id=run_id,
            kind=RunKindRef(kind="generation.propose", version=2),
            status="queued",
            revision=1,
            idempotency_scope=f"principal:{actor.principal.id}",
            idempotency_key=server.idempotency_key,
            request_hash=server.request_hash,
            payload=envelope,
            payload_hash=canonical_payload_hash(envelope),
            run_kind_definition_digest="b" * 64,
            outcome_policy_set_digest="c" * 64,
            failure_classifier=FailureClassifierRefV1(
                classifier_version=1,
                classifier_digest="d" * 64,
            ),
            initiated_by=AuditActor(
                principal_id=actor.principal.id,
                principal_kind=actor.principal.kind,
            ),
            resource_domain_scope=kwargs["domain_scope"],
            queue_deadline_utc="2026-07-24T00:10:00Z",
            attempt_timeout_ns=30_000_000_000,
            overall_deadline_utc="2026-07-24T01:00:00Z",
            next_attempt_no=1,
            next_fencing_token=1,
            next_event_seq=2,
            budget_set_snapshot_id=envelope.budget_set_snapshot_id,
            run_budget_hold_group_id=f"hold:{run_id}",
            retry_policy=RetryPolicyRefV1(
                retry_policy_id="default",
                retry_policy_version=1,
                retry_policy_digest="e" * 64,
            ),
            max_attempts=3,
            created_at=NOW_TEXT,
            updated_at=NOW_TEXT,
        )
        event = RunEvent(
            run_id=run.run_id,
            seq=1,
            event_type="run.queued",
            occurred_at=NOW_TEXT,
            data_schema_version="run-queued@1",
            data=RunQueuedDataV1(
                run_kind=run.kind,
                queue_deadline_utc=run.queue_deadline_utc,
                overall_deadline_utc=run.overall_deadline_utc,
            ),
        )
        with self._unit_of_work.begin() as transaction:
            transaction.runs.create_queued(run, event)
        return RunAcceptedV1(
            run_id=run.run_id,
            status_url=f"/api/v1/runs/{run.run_id}",
            events_url=f"/api/v1/runs/{run.run_id}/events",
        )


def _governance() -> tuple[DomainRegistryV1, RolePolicy]:
    definitions = (
        DomainDefinitionV1(
            domain_id="game-content",
            display_name="Game Content",
            status="active",
        ),
    )
    registry = DomainRegistryV1(
        registry_version="domains@projects-1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@projects-1", definitions),
    )
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    permissions = tuple(
        Permission(action=action, resource_kind=kind, domain_scope="all")
        for kind, actions in {
            "project": ("create", "read", "update", "archive"),
            "material": ("create", "read", "archive"),
            "extraction": ("create", "read"),
        }.items()
        for action in actions
    )
    grants = {"content_designer": permissions}
    effective_from = "2026-07-24T00:00:00Z"
    policy = RolePolicy(
        policy_version="roles@projects-1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "roles@projects-1",
            ref,
            grants,
            effective_from,
        ),
    )
    return registry, policy


@pytest.fixture
def project_runtime(tmp_path):
    clock = FrozenUtcClock(NOW)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'projects.db'}")
    Base.metadata.create_all(engine)
    objects = LocalObjectStore(
        tmp_path / "objects",
        store_id="projects-test",
        clock=clock,
        cursor_signing_key=b"o" * 32,
    )

    def capabilities(session):
        cursor = CursorSigner(signing_key=b"c" * 32, clock=clock)
        bindings = SqlObjectBindingRepository(session, objects, "projects-test")
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor, clock=clock),
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=bindings,
            runs=SqlRunRepository(session),
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
            idempotency=SqlIdempotencyRepository(session, clock=clock),
            artifacts=SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=cursor,
                clock=clock,
            ),
            projects=SqlProjectRepository(session),
        )

    uow = SqliteUnitOfWork(engine, capabilities)
    registry, policy = _governance()
    bootstrap_actor = AuditActor(principal_id="system:bootstrap", principal_kind="system")
    with uow.begin() as transaction:
        transaction.policies.put_domain_registry(registry)
        transaction.policies.put_role_policy(policy)
        created = transaction.identity.create(
            principal_id="human:maker",
            kind="human",
            display_name="Maker",
        )
        transaction.identity.grant(
            assignment_id="assignment:maker",
            principal_id=created.principal_id,
            role="content_designer",
            scope="all",
            granted_by=bootstrap_actor,
            expected_principal_revision=created.revision,
        )
    with uow.begin() as transaction:
        principal = transaction.identity.project("human:maker")
        assert principal is not None
    actor = ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:maker",
        ),
        session_id="session:maker",
        request_id="request:maker",
    )
    admission = _ProjectAdmission(uow)
    service = ProjectAuthoringService(
        unit_of_work=uow,
        object_store=objects,
        clock=clock,
        role_policy_version=policy.policy_version,
        role_policy_digest=policy.policy_digest,
        audit_chain_id="audit:projects",
        run_admission=admission,
    )
    yield service, actor, uow, objects, engine
    engine.dispose()


def _context(actor: ActorContext, key: str, payload: object) -> ProjectCommandContext:
    return ProjectCommandContext(
        actor=actor,
        idempotency_key=key,
        request_hash=canonical_sha256(payload),
        request_id=f"request:{key}",
    )


def _create(service: ProjectAuthoringService, actor: ActorContext):
    request = ProjectCreateRequestV1(
        project_key="sky-harbor",
        display_name="天空港",
        description="浮空城经营 RPG",
        genre="RPG",
        domain_scope=SCOPE,
    )
    context = _context(actor, "create-project", request.model_dump(mode="json"))
    return service.create_project(request, context=context), request, context


def test_create_project_publishes_bootstrap_without_moving_content_ref(project_runtime) -> None:
    service, actor, uow, _, engine = project_runtime

    project, request, context = _create(service, actor)
    replay = service.create_project(request, context=context)

    assert replay == project
    assert project.status == "draft"
    assert project.current_content_ref is None
    with uow.begin() as transaction:
        assert transaction.refs.get(project.content_ref_name) is None
        artifact = transaction.artifacts.get(project.bootstrap_snapshot_artifact_id)
        assert artifact is not None
        assert artifact.kind == "ir_snapshot"
        assert artifact.version_tuple.doc_version == f"{project.project_id}@bootstrap"
        assert artifact.meta["project_bootstrap"] is True
        assert artifact.meta["project_id"] == project.project_id
    with engine.connect() as connection:
        actions = connection.execute(select(AuditRow.action)).scalars().all()
    assert actions == ["project.created"]


def test_feishu_material_retains_exact_original_and_rendered_provenance(project_runtime) -> None:
    service, actor, uow, objects, _ = project_runtime
    project, _, _ = _create(service, actor)
    source = (
        '{"blocks":[{"heading1":{"elements":[{"text_run":{"content":"天气系统"}}]}},'
        '{"text":{"elements":[{"text_run":{"content":"air.quality 与 air_quality 是同一属性"}}]}}]}'
    )
    request = ProjectMaterialTextRequestV1(
        display_name="飞书天气策划",
        source_format="feishu_blocks_json",
        text=source,
    )
    context = _context(actor, "material-feishu", request.model_dump(mode="json"))

    material = service.add_text_material(project.project_id, request, context=context)
    assert service.add_text_material(project.project_id, request, context=context) == material

    with uow.begin() as transaction:
        raw = transaction.artifacts.get(material.original_source_artifact_id)
        rendered = transaction.artifacts.get(material.rendered_source_artifact_id)
        assert raw is not None and rendered is not None
        assert raw.kind == "source_raw"
        assert rendered.kind == "source_rendered"
        assert rendered.lineage == (raw.artifact_id,)
        assert raw.meta["domain_scope"] == SCOPE.model_dump(mode="json")
        provenance = rendered.meta["provenance"]
        assert provenance["source_kind_id"] == "tool_output"
        assert provenance["trust"] == "reviewed_external"
        assert provenance["parent_source_artifact_ids"] == [raw.artifact_id]
        raw_binding = transaction.object_bindings.resolve(raw.object_ref)
        rendered_binding = transaction.object_bindings.resolve(rendered.object_ref)
        with objects.open(raw_binding.location) as stream:
            assert stream.read() == source.encode("utf-8")
        with objects.open(rendered_binding.location) as stream:
            text = stream.read().decode("utf-8")
            assert "# 天气系统" in text
            assert "air.quality 与 air_quality" in text
    refreshed = service.get_project(project.project_id, actor=actor)
    assert refreshed.revision == 2
    assert service.list_materials(project.project_id, actor=actor).items == (material,)


def test_material_rename_keeps_the_artifacts_and_is_replayable(project_runtime) -> None:
    """Planners rename material; the retained bytes and lineage never move."""

    service, actor, uow, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material = service.add_text_material(
        project.project_id,
        ProjectMaterialTextRequestV1(
            display_name="临时名字",
            source_format="plain_text",
            text="天空港由天气管理员维护。",
        ),
        context=_context(actor, "material-rename-seed", {"seed": True}),
    )

    request = ProjectMaterialRenameRequestV1(
        expected_revision=material.revision,
        display_name="天空港核心创意",
    )
    context = _context(actor, "material-rename", request.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project_material",
            resource_id=material.material_id,
            revision=material.revision,
        ),
    )
    renamed = service.rename_material(
        project.project_id, material.material_id, request, context=context
    )

    assert renamed.display_name == "天空港核心创意"
    assert renamed.revision == material.revision + 1
    assert renamed.original_source_artifact_id == material.original_source_artifact_id
    assert renamed.rendered_source_artifact_id == material.rendered_source_artifact_id
    # A repeated request replays the same result instead of bumping the revision.
    assert (
        service.rename_material(project.project_id, material.material_id, request, context=context)
        == renamed
    )
    with uow.begin() as transaction:
        assert transaction.projects.get_material(material.material_id) == renamed


def test_material_rename_requires_the_current_revision(project_runtime) -> None:
    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material = service.add_text_material(
        project.project_id,
        ProjectMaterialTextRequestV1(
            display_name="初稿",
            source_format="plain_text",
            text="天空港由天气管理员维护。",
        ),
        context=_context(actor, "material-stale-seed", {"seed": True}),
    )
    stale = ProjectMaterialRenameRequestV1(
        expected_revision=material.revision + 5,
        display_name="不该生效",
    )

    stale_context = _context(actor, "material-stale", stale.model_dump(mode="json"))
    object.__setattr__(
        stale_context,
        "if_match",
        compute_resource_etag(
            resource_kind="project_material",
            resource_id=material.material_id,
            revision=material.revision,
        ),
    )

    with pytest.raises(Conflict):
        service.rename_material(
            project.project_id, material.material_id, stale, context=stale_context
        )


def test_material_archive_requires_exact_revision_and_strong_etag(project_runtime) -> None:
    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material_request = ProjectMaterialTextRequestV1(
        display_name="一句创意",
        source_format="plain_text",
        text="一座会移动的城市。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-text", material_request.model_dump(mode="json")),
    )
    archive = ProjectArchiveRequestV1(expected_revision=1, reason="不再使用")
    context = _context(actor, "archive-material", archive.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project_material",
            resource_id=material.material_id,
            revision=material.revision,
        ),
    )

    archived = service.archive_material(
        project.project_id,
        material.material_id,
        archive,
        context=context,
    )

    assert archived.status == "archived"
    assert archived.revision == 2


def test_project_extraction_resolves_execution_and_maps_the_exact_queued_run(
    project_runtime,
) -> None:
    service, actor, uow, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material_request = ProjectMaterialTextRequestV1(
        display_name="空气系统",
        source_format="plain_text",
        text="air.quality 与 air_quality 表示同一个空气质量实体。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-air", material_request.model_dump(mode="json")),
    )
    request = ProjectExtractionCreateRequestV1(
        material_ids=(material.material_id,),
        planning_scope="limited_event",
        objective_goal_text="提取可编辑的实体与关系草案。",
    )
    context = _context(actor, "extract-air", request.model_dump(mode="json"))

    extraction = service.create_extraction(project.project_id, request, context=context)
    replay = service.create_extraction(project.project_id, request, context=context)

    assert replay == extraction
    assert extraction.status == "queued"
    assert extraction.planning_scope == "limited_event"
    assert extraction.material_ids == (material.material_id,)
    assert extraction.source_artifact_ids == (material.rendered_source_artifact_id,)
    assert extraction.base_snapshot_artifact_id == project.bootstrap_snapshot_artifact_id
    with uow.begin() as transaction:
        run = transaction.runs.get(extraction.run_id)
        assert run is not None
        assert run.payload.params.source_artifact_ids == extraction.source_artifact_ids
        assert run.payload.params.target.ref_name == project.content_ref_name
        admission = service._run_admission
        assert isinstance(admission, _ProjectAdmission)
        goal = admission.admission_requests[-1]["objective_goal_text"]
        assert "Planning scope authority: limited_event" in goal
        assert "reward-claim windows" in goal
        mapped = transaction.projects.get_extraction(extraction.extraction_id)
        assert mapped is not None and mapped.run_id == run.run_id
    refreshed = service.get_project(project.project_id, actor=actor)
    assert refreshed.latest_extraction_id == extraction.extraction_id
    assert (
        service.get_extraction(
            project.project_id,
            extraction.extraction_id,
            actor=actor,
        )
        == extraction
    )


def test_project_keeps_multiple_extractions_and_discard_repoints_latest_without_deleting_run(
    project_runtime,
    monkeypatch,
) -> None:
    service, actor, uow, _, engine = project_runtime
    project, _, _ = _create(service, actor)
    material_request = ProjectMaterialTextRequestV1(
        display_name="活动策划合集",
        source_format="plain_text",
        text="活动一与活动二共享世界观，但采用不同玩法方向。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-proposals", material_request.model_dump(mode="json")),
    )
    extraction_request = ProjectExtractionCreateRequestV1(
        material_ids=(material.material_id,),
        planning_scope="limited_event",
        objective_goal_text="提出一份可编辑活动草案。",
    )
    first = service.create_extraction(
        project.project_id,
        extraction_request,
        context=_context(actor, "extract-proposal-1", extraction_request.model_dump(mode="json")),
    )
    second = service.create_extraction(
        project.project_id,
        extraction_request,
        context=_context(actor, "extract-proposal-2", extraction_request.model_dump(mode="json")),
    )

    def terminal_projection(transaction, mapped_project, extraction):
        del transaction, mapped_project
        if extraction.disposition == "discarded":
            return extraction
        return extraction.model_copy(
            update={
                "status": "failed",
                "failure_cause_code": "generation_gate_rejected",
                "failure_message": "已保留本次提案与检查证据。",
                "failure_retryable": False,
            }
        )

    monkeypatch.setattr(service, "_extraction_authority_projection", terminal_projection)

    history = service.list_extractions(project.project_id, actor=actor)
    assert {item.extraction_id for item in history.items} == {
        first.extraction_id,
        second.extraction_id,
    }

    discard = ProjectExtractionDiscardRequestV1(
        expected_revision=second.revision,
        reason="这版玩法方向不合适，保留材料后重新提案。",
    )
    context = _context(actor, "discard-proposal-2", discard.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project_extraction",
            resource_id=second.extraction_id,
            revision=second.revision,
        ),
    )

    discarded = service.discard_extraction(
        project.project_id,
        second.extraction_id,
        discard,
        context=context,
    )
    replayed = service.discard_extraction(
        project.project_id,
        second.extraction_id,
        discard,
        context=context,
    )

    assert replayed == discarded
    assert discarded.status == "failed"
    assert discarded.disposition == "discarded"
    assert discarded.discard_reason == discard.reason
    assert discarded.revision == 2
    assert service.get_project(project.project_id, actor=actor).latest_extraction_id == (
        first.extraction_id
    )
    retained = service.list_extractions(project.project_id, actor=actor).items
    assert (
        next(item for item in retained if item.extraction_id == second.extraction_id) == discarded
    )
    with uow.begin() as transaction:
        assert transaction.runs.get(discarded.run_id) is not None
        assert transaction.projects.get_material(material.material_id) == material
    with engine.connect() as connection:
        actions = connection.execute(select(AuditRow.action)).scalars().all()
    assert actions.count("project.extraction.discarded") == 1


def test_running_project_extraction_cannot_be_discarded(project_runtime) -> None:
    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material_request = ProjectMaterialTextRequestV1(
        display_name="正在提取的策划",
        source_format="plain_text",
        text="先让 AI 完成本次提取。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-running", material_request.model_dump(mode="json")),
    )
    extraction_request = ProjectExtractionCreateRequestV1(
        material_ids=(material.material_id,),
        objective_goal_text="提取实体和关系。",
    )
    extraction = service.create_extraction(
        project.project_id,
        extraction_request,
        context=_context(actor, "extract-running", extraction_request.model_dump(mode="json")),
    )
    discard = ProjectExtractionDiscardRequestV1(
        expected_revision=extraction.revision,
        reason="不再需要这版。",
    )
    context = _context(actor, "discard-running", discard.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project_extraction",
            resource_id=extraction.extraction_id,
            revision=extraction.revision,
        ),
    )

    with pytest.raises(Conflict, match="still running"):
        service.discard_extraction(
            project.project_id,
            extraction.extraction_id,
            discard,
            context=context,
        )


def test_project_graph_editor_prepares_exact_human_patch_with_server_normalization(
    project_runtime,
    monkeypatch,
) -> None:
    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    material_request = ProjectMaterialTextRequestV1(
        display_name="璃月剧情",
        source_format="plain_text",
        text="刻晴与凝光调查岩心异动。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-liyue", material_request.model_dump(mode="json")),
    )
    extraction_request = ProjectExtractionCreateRequestV1(
        material_ids=(material.material_id,),
        planning_scope="permanent_feature",
        objective_goal_text="提取璃月剧情实体与关系。",
    )
    extraction = service.create_extraction(
        project.project_id,
        extraction_request,
        context=_context(actor, "extract-liyue", extraction_request.model_dump(mode="json")),
    )

    def ready_projection(transaction, mapped_project, mapped_extraction):
        del transaction, mapped_project
        return mapped_extraction.model_copy(
            update={
                "status": "ready",
                "patch_artifact_id": "artifact:agent-patch:liyue",
                "preview_snapshot_artifact_id": "artifact:preview:liyue",
            }
        )

    monkeypatch.setattr(service, "_extraction_authority_projection", ready_projection)
    project = service.get_project(project.project_id, actor=actor)
    request = ProjectGraphDraftRequestV1(
        source_extraction_id=extraction.extraction_id,
        expected_source_extraction_revision=extraction.revision,
        expected_project_revision=project.revision,
        entities=(
            Entity(id="air.quality", type=NodeType.ITEM, attrs={"label": "空气质量"}),
            Entity(id="AIR_QUALITY", type=NodeType.ITEM, attrs={"label": "空气质量"}),
        ),
        relations=(),
        rationale="确认首个空气系统内容草案",
    )
    context = _context(actor, "graph-draft-air", request.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project",
            resource_id=project.project_id,
            revision=project.revision,
        ),
    )

    prepared = service.prepare_content_draft(
        project.project_id,
        request,
        context=context,
    )

    assert prepared.workflow_request.base_snapshot_artifact_id == (
        project.bootstrap_snapshot_artifact_id
    )
    assert prepared.workflow_request.ref_name == project.content_ref_name
    assert prepared.workflow_request.expected_ref is None
    assert prepared.source_extraction_id == extraction.extraction_id
    assert prepared.expected_source_extraction_revision == extraction.revision
    assert prepared.normalization_summary.auto_merge_count == 1
    assert prepared.alias_groups[0].canonical_identity == "item:air_quality"
    replace = prepared.workflow_request.ops[0]
    assert replace.op == "replace_subgraph"
    assert replace.new_value["entities"][0]["id"] == "item:air_quality"

    stale = request.model_copy(
        update={"expected_source_extraction_revision": extraction.revision + 1}
    )
    stale_context = _context(actor, "graph-draft-stale-source", stale.model_dump(mode="json"))
    object.__setattr__(stale_context, "if_match", context.if_match)
    with pytest.raises(Conflict, match="source extraction revision differs"):
        service.prepare_content_draft(
            project.project_id,
            stale,
            context=stale_context,
        )


def test_project_extraction_translates_gate_findings_into_planner_language() -> None:
    dead_quest = ProjectAuthoringService._project_extraction_issue(
        {
            "id": "finding:dead-quest",
            "defect_class": "dead_quest",
            "severity": "critical",
            "status": "confirmed",
            "entities": ["quest:未寄之梦"],
            "evidence": {"has_giver": False, "has_steps": True},
        },
        source="structure",
    )
    economy = ProjectAuthoringService._project_extraction_issue(
        {
            "id": "finding:economy",
            "defect_class": "drop_source_existence_and_yield_rate",
            "severity": "major",
            "status": "confirmed",
            "entities": [],
            "evidence": {"currencies_without_source": ["currency:原石", "currency:梦迹书签"]},
        },
        source="economy",
    )
    lifecycle = ProjectAuthoringService._project_extraction_issue(
        {
            "id": "finding:lifecycle",
            "defect_class": "unbound_event_schedule",
            "severity": "major",
            "status": "confirmed",
            "entities": ["event:dream_of_unsent_letter"],
            "evidence": {"duration_days": 14, "reward_claim_grace_days": 3},
        },
        source="structure",
        entity_labels={"event:dream_of_unsent_letter": "梦中未寄出的信"},
    )

    assert dead_quest is not None
    assert dead_quest.title == "任务缺少起点或步骤"
    assert dead_quest.affected_content == ("未寄之梦",)
    assert "发起方" in dead_quest.description
    assert economy is not None
    assert economy.title == "货币产出链不完整"
    assert economy.affected_content == ("原石", "梦迹书签")
    assert "currency:" not in economy.description
    assert lifecycle is not None
    assert lifecycle.title == "限时活动还没有确定档期"
    assert lifecycle.affected_content == ("梦中未寄出的信",)
    assert "dream of unsent letter" not in lifecycle.description
    assert "奖励兑换截止时间" in lifecycle.resolution_hint


def test_a_declared_alias_reaches_the_run_it_was_declared_before(project_runtime) -> None:
    """岩王帝君 and 钟离 share no characters; only a person can say they are one.

    The declaration has to travel into the Run, frozen like every other input, or
    the next extraction invents a second NPC for the same character.
    """

    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)

    # The alias has to name an entity the game already has.
    missing = ProjectIdentityAliasDeclareRequestV1(
        expected_project_revision=project.revision,
        alias="岩王帝君",
        canonical_entity_id="npc:morax",
    )
    context = _context(actor, "alias-missing", missing.model_dump(mode="json"))
    object.__setattr__(
        context,
        "if_match",
        compute_resource_etag(
            resource_kind="project",
            resource_id=project.project_id,
            revision=project.revision,
        ),
    )
    with pytest.raises(Conflict, match="does not have"):
        service.declare_identity_alias(project.project_id, missing, context=context)


def test_every_declared_alias_is_frozen_into_the_extraction_run(project_runtime) -> None:
    """The alias table is read inside the same transaction that reads the base.

    Declaring a new alias after a Run is queued must not change what that Run does,
    so admission freezes the exact set the extraction was started with.
    """

    service, actor, uow, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    with uow.begin() as transaction:
        transaction.projects.create_identity_alias(
            ProjectIdentityAliasV1(
                alias="岩王帝君",
                alias_id="identity-alias:morax",
                canonical_alias="岩王帝君",
                canonical_entity_id="npc:zhongli",
                declared_at=NOW_TEXT,
                declared_by=actor.principal.id,
                project_id=project.project_id,
                revision=1,
                status="active",
            )
        )
    material_request = ProjectMaterialTextRequestV1(
        display_name="璃月篇",
        source_format="plain_text",
        text="岩王帝君坐镇璃月港。",
    )
    material = service.add_text_material(
        project.project_id,
        material_request,
        context=_context(actor, "material-liyue", material_request.model_dump(mode="json")),
    )
    request = ProjectExtractionCreateRequestV1(
        material_ids=(material.material_id,),
        planning_scope="permanent_feature",
        objective_goal_text="提取可编辑的实体与关系草案。",
    )

    service.create_extraction(
        project.project_id,
        request,
        context=_context(actor, "extract-liyue", request.model_dump(mode="json")),
    )

    admission = service._run_admission
    assert isinstance(admission, _ProjectAdmission)
    assert admission.admission_requests[-1]["declared_identity_aliases"] == (
        ("岩王帝君", "npc:zhongli"),
    )


def test_declaring_and_retracting_an_alias_keeps_the_record(project_runtime) -> None:
    service, actor, _, _, _ = project_runtime
    project, _, _ = _create(service, actor)
    page = service.list_identity_aliases(project.project_id, actor=actor, limit=100)

    assert page.items == ()
