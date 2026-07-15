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
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.local import (
    LocalApiConfig,
    build_local_api_resources,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.secrets.session_keys import SessionSigningKeyProvider
from gameforge.spine.ir.snapshot import Snapshot
from tests.platform.m4 import apply_testkit
from tests.platform.m4.test_local_service_flow_integration import (
    _profile_catalog,
    _seed_governance,
)

_SEED_CURSOR_KEY = b"anti-masking-seed-cursor-key-000"


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
    registry = apply_testkit._registry()
    route = apply_testkit._route(registry)
    roles = apply_testkit._roles(registry)
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
    snapshot = Snapshot.from_entities_relations(
        [Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})], []
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


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": '"etag:1"'}


def test_real_local_composition_creates_patch_and_constraint_drafts(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'workflow-composition.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    # Provision the exact governance + identities through the trusted policy repository.
    _seed_governance(engine, clock=clock, catalog=_profile_catalog())
    engine.dispose()

    config = _config(tmp_path, database_url)
    resources = build_local_api_resources(config)
    base_artifact_id, base_ref = _seed_base(resources, config, clock)

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
                "domain_scope": {"domain_ids": ["economy"]},
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

    resources.close()

    # Both drafts create real approval subjects through the real composition. A
    # governance=None composition would return 503 workflow_governance here.
    assert patch.status_code == 201, patch.text
    patch_body = patch.json()
    assert patch_body["approval_status"] == "draft"
    assert patch_body["artifact"]["kind"] == "patch"
    # the route-policy scope resolver derived the affected economy domain (not injected)
    assert patch_body["artifact"]["domain_scope"] == {"domain_ids": ["economy"]}

    assert constraint.status_code == 201, constraint.text
    constraint_body = constraint.json()
    assert constraint_body["proposal"]["revision"] == 1
    assert constraint_body["artifact"]["kind"] == "constraint_proposal"


def test_real_local_composition_without_governance_fails_closed(tmp_path: Path) -> None:
    # Same real composition, but the deployment did not configure governance pointers:
    # draft ops must fail closed with a typed dependency error, never fabricate authority.
    database_url = f"sqlite:///{tmp_path / 'no-governance.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _seed_governance(engine, clock=clock, catalog=_profile_catalog())
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
