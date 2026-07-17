"""Anti-masking test for the REAL local workflow-command composition (C1).

This drives ``patch.draft`` and ``constraint.draft`` to success through the exact
production composition — ``build_local_api_resources`` runs the real
``_build_workflow_command_service`` with its DB-resolved ``WorkflowGovernance``
provider and route-policy ``WorkflowScopeResolver``, with NO injected test
governance/scope providers. Governance is provisioned through the trusted
``SqlPolicySnapshotRepository`` path (never a fixture object), exactly as a real
deployment would. Had the shipped composition still passed ``governance=None`` /
``scope_resolver=None``, both drafts would fail closed with a 503
``workflow_governance`` error and this test would catch it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.local import (
    LocalApiConfig,
    build_local_api_resources,
)
from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRouteRule,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.workflow import (
    ApprovalPolicyRegistryV1,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import ArtifactRow, Base
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.secrets.session_keys import SessionSigningKeyProvider
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from tests.platform.m4 import apply_testkit

_SEED_CURSOR_KEY = b"anti-masking-seed-cursor-key-000"
_DOMAIN = DomainScope(domain_ids=("builtin",))


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="builtin", display_name="Built-in", status="active"),
    )
    return DomainRegistryV1(
        registry_version="anti-masking-domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("anti-masking-domains@1", definitions),
    )


def _route_policy(registry: DomainRegistryV1) -> DomainRoutePolicy:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    rules = (
        DomainRouteRule(
            rule_id="route:builtin",
            domain_selector=_DOMAIN,
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role="numeric_designer",
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
        ),
    )
    effective_from = "2026-07-14T00:00:00Z"
    return DomainRoutePolicy(
        route_version="anti-masking-routes@1",
        domain_registry_ref=ref,
        rules=rules,
        effective_from=effective_from,
        route_digest=compute_domain_route_policy_digest(
            "anti-masking-routes@1", ref, rules, effective_from
        ),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "content_designer": (
            Permission(action="validate", resource_kind="patch", domain_scope=_DOMAIN),
        ),
    }
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version="anti-masking-roles@1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "anti-masking-roles@1", ref, grants, effective_from
        ),
    )


def _signing_keys() -> SessionSigningKeyProvider:
    return SessionSigningKeyProvider(
        (
            SessionSigningKeySet(
                key_set_version="keys@1",
                keys=(SessionSigningKey(key_id="k1", secret=b"s" * 32, status="active"),),
            ),
        )
    )


def _config(tmp_path: Path, database_url: str) -> LocalApiConfig:
    registry = _domain_registry()
    route = _route_policy(registry)
    roles = _role_policy(registry)
    approval = apply_testkit._approval_policy()
    # The config declares the exact governance pointers; the real composition resolves
    # the registry/roles/route/approval snapshots from the policy repository at request
    # time. No fixture WorkflowGovernance object is injected anywhere.
    return LocalApiConfig(
        database_url=database_url,
        object_store_root=tmp_path / "objects",
        object_store_id="local:test",
        telemetry_db_path=tmp_path / "telemetry.sqlite3",
        current_password_hash_policy_version="argon2id@1",
        session_policy_version="session@1",
        role_policy_version=roles.policy_version,
        role_policy_digest=roles.policy_digest,
        audit_chain_id="platform-authority",
        root_secret=b"r" * 32,
        session_signing_keys=_signing_keys(),
        workflow_route_policy_version=route.route_version,
        workflow_route_policy_digest=route.route_digest,
        workflow_approval_policy_version=approval.policy_version,
        workflow_approval_policy_digest=approval.policy_digest,
    )


def _maker_actor(engine, clock) -> ActorContext:
    with Session(engine) as session:
        principal = SqlIdentityRepository(session, clock=clock).project("human:maker")
    assert principal is not None
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="credential:human:maker",
        ),
        session_id="session:human:maker",
        request_id="request:human:maker",
    )


def _seed_base(resources, config, clock) -> tuple[str, dict]:
    snapshot = AureusCsvAdapter().to_ir(
        {
            "regions": [
                {
                    "region_id": "region:start",
                    "name": "Start",
                    "grid": {"width": 4, "height": 4, "blocked": []},
                    "start_pos": [0, 0],
                    "scenario_id": "workflow-composition",
                }
            ],
            "npcs": [
                {
                    "npc_id": "npc:guide",
                    "name": "Guide",
                    "region": "region:start",
                    "pos": [1, 0],
                }
            ],
            "quests": [
                {
                    "quest_id": "q:1",
                    "title": "Reward review",
                    "region": "region:start",
                    "giver": "npc:guide",
                    "reward": {"gold": 30},
                    "reward_gold": 120,
                }
            ],
            "quest_steps": [
                {
                    "step_id": "step:turn-in",
                    "quest_id": "q:1",
                    "order": 0,
                    "kind": "turn_in",
                    "target": "npc:guide",
                    "item": None,
                    "count": 1,
                    "encounter": None,
                }
            ],
        },
        file_ref="workflow-composition",
    )
    payload = canonical_json(snapshot.content_payload).encode("utf-8")
    stored = resources.object_store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot.snapshot_id,
            tool_version="local-flow@1",
        ),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "ir-core@1",
            "domain_scope": _DOMAIN.model_dump(mode="json"),
        },
        created_at="2026-07-14T12:00:00Z",
    )
    signer = CursorSigner(signing_key=_SEED_CURSOR_KEY, clock=clock)
    with Session(resources.engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session, resources.object_store, config.object_store_id
        )
        bindings.bind_verified(stored.ref, stored.location, None)
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=signer,
            clock=clock,
        )
        artifacts.put(artifact)
        ref = SqlRefStore(session, cursor_signer=signer, clock=clock).compare_and_set(
            "content/head", None, artifact.artifact_id
        )
    return artifact.artifact_id, ref.model_dump(mode="json")


def _seed_constraint(resources, config, clock) -> str:
    payload = canonical_json({"dsl_grammar_version": "dsl@1", "constraints": []}).encode("utf-8")
    stored = resources.object_store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="constraint_snapshot",
        version_tuple=VersionTuple(
            constraint_snapshot_id=stored.ref.sha256,
            tool_version="constraint-test@1",
        ),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "constraint-snapshot@1",
            "domain_scope": _DOMAIN.model_dump(mode="json"),
        },
        created_at="2026-07-14T12:00:00Z",
    )
    signer = CursorSigner(signing_key=_SEED_CURSOR_KEY, clock=clock)
    with Session(resources.engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session, resources.object_store, config.object_store_id
        )
        bindings.bind_verified(stored.ref, stored.location, None)
        SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=signer,
            clock=clock,
        ).put(artifact)
    return artifact.artifact_id


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": '"etag:1"'}


def _seed_local_governance(engine, clock) -> None:
    registry = _domain_registry()
    route = _route_policy(registry)
    roles = _role_policy(registry)
    approval = apply_testkit._approval_policy()
    approval_registry = ApprovalPolicyRegistryV1(
        policies=(approval,),
        registry_digest=compute_approval_policy_registry_digest((approval,)),
    )
    catalogs = tuple(
        sorted(
            build_builtin_registry().list_execution_profile_catalogs(),
            key=lambda item: item.catalog_version,
        )
    )
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=clock)
        policies.put_domain_registry(registry)
        policies.put_domain_route_policy(route)
        policies.put_role_policy(roles)
        policies.put_approval_policy_registry(approval_registry)
        for catalog in catalogs:
            policies.put_execution_profile_catalog(catalog)
        identities = SqlIdentityRepository(session, clock=clock)
        maker = identities.create(
            principal_id="human:maker",
            kind="human",
            display_name="Maker",
        )
        identities.grant(
            assignment_id="assignment:human:maker:content-designer",
            principal_id=maker.principal_id,
            role="content_designer",
            scope=_DOMAIN,
            granted_by=AuditActor(
                principal_id="system:test-bootstrap",
                principal_kind="system",
            ),
            expected_principal_revision=maker.revision,
        )
        costs = SqlCostLedger(session, clock=clock)
        for budget_id, scope_kind, scope_id in (
            ("budget:principal:human:maker", "principal", "human:maker"),
            ("budget:system:global", "system", "global"),
        ):
            costs.put_budget(
                BudgetV1(
                    budget_id=budget_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    policy_version="anti-masking-budget@1",
                    limits=(
                        CostAmountV1(dimension="request", value=1_000_000, unit="request"),
                        CostAmountV1(dimension="concurrent_run", value=8, unit="count"),
                    ),
                    reserved=(),
                    consumed=(),
                    status="active",
                    revision=1,
                    created_at=clock.now_utc(),
                )
            )


def test_real_local_composition_creates_patch_and_constraint_drafts(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'workflow-composition.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    # Provision the exact governance + identities through the trusted policy repository.
    _seed_local_governance(engine, clock)
    engine.dispose()

    config = _config(tmp_path, database_url)
    resources = build_local_api_resources(config)
    base_artifact_id, base_ref = _seed_base(resources, config, clock)
    constraint_artifact_id = _seed_constraint(resources, config, clock)

    app = create_app(resources.dependencies)
    app.dependency_overrides[require_actor] = lambda: _maker_actor(resources.engine, clock)

    with TestClient(app, base_url="https://gameforge.test") as client:
        patch = client.post(
            "/api/v1/patches",
            json={
                "request_schema_version": "human-patch-draft-request@1",
                "base_snapshot_artifact_id": base_artifact_id,
                "constraint_snapshot_artifact_id": constraint_artifact_id,
                "ref_name": "content/head",
                "expected_ref": base_ref,
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
                "rationale": "Lower the quest reward within the approved envelope.",
                "candidate_export_profiles": [
                    {"profile_id": "builtin.config_export", "version": 1}
                ],
            },
            headers=_headers("patch:draft:real"),
        )

        constraint = client.post(
            "/api/v1/constraint-proposals",
            json={
                "request_schema_version": "human-constraint-draft-request@1",
                "base_constraint_snapshot_artifact_id": None,
                "ref_name": "constraints/head",
                "expected_ref": None,
                "dsl_grammar_version": "dsl@1",
                "domain_scope": _DOMAIN.model_dump(mode="json"),
                "constraints": [
                    {
                        "id": "c:reward-cap",
                        "dsl_grammar_version": "dsl@1",
                        "kind": "numeric",
                        "oracle": "deterministic",
                        "predicates": [],
                        "assert": "reward_gold <= 100",
                        "severity": "major",
                    }
                ],
                "source_artifact_ids": [],
                "rationale": "Cap economy reward payouts.",
            },
            headers=_headers("constraint:draft:real"),
        )

    # Both drafts create real approval subjects through the real composition. A
    # governance=None composition would return 503 workflow_governance here.
    assert patch.status_code == 201, patch.text
    patch_body = patch.json()
    assert patch_body["approval_status"] == "draft"
    assert patch_body["artifact"]["kind"] == "patch"
    # The route-policy scope resolver derived the actual candidate domain (not injected).
    assert patch_body["artifact"]["domain_scope"] == _DOMAIN.model_dump(mode="json")
    patch_artifact_id = patch_body["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{patch_artifact_id}"
    with Session(resources.engine) as session:
        config_ids = tuple(
            session.scalars(
                select(ArtifactRow.artifact_id).where(ArtifactRow.kind == "config_export")
            ).all()
        )
        item = SqlApprovalRepository(session).get(approval_id)
        bindings = SqlObjectBindingRepository(
            session, resources.object_store, config.object_store_id
        )
        patch_artifact = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=_SEED_CURSOR_KEY, clock=clock),
            clock=clock,
        ).get(patch_artifact_id)
    assert len(config_ids) == 1
    assert item is not None
    assert patch_artifact is not None
    assert set(patch_artifact.lineage) == {base_artifact_id, constraint_artifact_id}
    assert patch_artifact.version_tuple.constraint_snapshot_id is not None

    # The real synchronous human draft must be directly admissible by the real
    # patch.validate service with its exact candidate config.  This is the closure
    # that fails when the Patch forgets the authoritative constraint VersionTuple.
    with TestClient(app, base_url="https://gameforge.test") as client:
        validate = client.post(
            f"/api/v1/patches/{patch_artifact_id}:validate",
            json={
                "request_schema_version": "patch-validation-admission-request@1",
                "approval_id": approval_id,
                "expected_subject_head_revision": item.subject_revision,
                "expected_workflow_revision": item.workflow_revision,
                "subject_digest": item.subject_digest,
                "base_snapshot_artifact_id": base_artifact_id,
                "preview_snapshot_artifact_id": item.target_binding.target_artifact_id,
                "candidate_config_export_artifact_ids": list(config_ids),
                "target": {"ref_name": "content/head", "expected_ref": base_ref},
                "validation_policy": {"profile_id": "builtin.validation", "version": 1},
                "checker_profiles": [],
                "simulation_profiles": [],
                "findings": [],
                "review_artifact_ids": [],
                "playtest_trace_artifact_ids": [],
                "regression_suite_artifact_ids": [],
            },
            headers={
                "Idempotency-Key": "patch:validate:real",
                "If-Match": compute_resource_etag(
                    resource_kind="patch",
                    resource_id=patch_artifact_id,
                    revision=item.workflow_revision,
                ),
            },
        )
    assert validate.status_code == 202, validate.text
    assert validate.json()["accepted_schema_version"] == "run-accepted@1"

    assert constraint.status_code == 201, constraint.text
    constraint_body = constraint.json()
    assert constraint_body["proposal"]["revision"] == 1
    assert constraint_body["artifact"]["kind"] == "constraint_proposal"
    resources.close()


def test_real_local_composition_without_governance_fails_closed(tmp_path: Path) -> None:
    # Same real composition, but the deployment did not configure governance pointers:
    # draft ops must fail closed with a typed dependency error, never fabricate authority.
    database_url = f"sqlite:///{tmp_path / 'no-governance.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _seed_local_governance(engine, clock)
    engine.dispose()

    config = _config(tmp_path, database_url)
    ungoverned = LocalApiConfig(
        database_url=config.database_url,
        object_store_root=config.object_store_root,
        object_store_id=config.object_store_id,
        telemetry_db_path=config.telemetry_db_path,
        current_password_hash_policy_version=config.current_password_hash_policy_version,
        session_policy_version=config.session_policy_version,
        role_policy_version=config.role_policy_version,
        role_policy_digest=config.role_policy_digest,
        audit_chain_id=config.audit_chain_id,
        root_secret=config.root_secret,
        session_signing_keys=_signing_keys(),
    )
    resources = build_local_api_resources(ungoverned)
    base_artifact_id, base_ref = _seed_base(resources, ungoverned, clock)
    app = create_app(resources.dependencies)
    app.dependency_overrides[require_actor] = lambda: _maker_actor(resources.engine, clock)

    with TestClient(app, base_url="https://gameforge.test") as client:
        patch = client.post(
            "/api/v1/patches",
            json={
                "request_schema_version": "human-patch-draft-request@1",
                "base_snapshot_artifact_id": base_artifact_id,
                "constraint_snapshot_artifact_id": None,
                "ref_name": "content/head",
                "expected_ref": base_ref,
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
                "rationale": "Lower the quest reward within the approved envelope.",
                "candidate_export_profiles": [],
            },
            headers=_headers("patch:draft:nogov"),
        )
    resources.close()
    assert patch.status_code == 503
    body = patch.json()
    assert body["code"] == "dependency_unavailable"
    assert body["errors"] == [{"component": "workflow_governance"}]
