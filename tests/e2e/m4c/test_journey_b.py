"""Task 17c — Journey B end-to-end through the composed M4c stack.

Two INDEPENDENT human sessions (maker A, approver B) drive the hand-written-Patch
maker-checker journey over the REAL composed platform: the FastAPI app
(``build_local_api_resources`` — real admission + workflow-command service + the
17c validation-starting composition + read APIs) and the REAL persistent worker
(``build_worker_process`` — real dispatch loop + Task-9 ``TerminalPublisher`` +
validation-completion effect), sharing ONE SQLite authority + ObjectStore. The API
runs as a ``TestClient``; the worker is driven deterministically by ``dispatch_once``.

Every workflow transition goes through the PUBLIC HTTP API (``POST /patches``,
``:validate``, ``:submit-for-approval``, ``/approvals/{id}:approve``, ``:apply``) and
the REAL worker + terminal publisher. Identity provisioning + governance seeding are
out-of-band fixture bootstrap (allowed); the JOURNEY itself is never bypassed. The
Patch journey is hand-written + deterministic ``patch.validate`` (``not_applicable``
LLM mode) — NO cassette, NO external network.

Proven here:

* Happy path: A drafts a Patch+preview → ``:validate`` (worker runs ``patch.validate``
  against REAL graph-invariant evidence → the 17c single-UoW validation-start CAS +
  terminal effect move the ApprovalItem ``draft→validating→validated``) → A ``:submit``
  → B ``:approve`` → B ``:apply`` — the ref/history MOVE, apply binds the exact preview.
* Repeat spanning an API+worker RESTART: a second cycle whose validation Run is queued
  before the stack is rebuilt over the SAME DB; state persists and the queued Run still
  completes on the rebuilt worker; the second apply moves the ref/history again.
* Coverage: A self-approval → 403 (maker-checker); a stale ``expected_workflow_revision``
  → 409; a same-Idempotency-Key/different-payload draft → 409 idempotency conflict;
  audit/lineage/run/cost/log correlation on ``run_id``/subject; API+worker restart.
* Failure Patch: a dangling-preview Patch → confirmed Finding + FAILED EvidenceSet →
  ApprovalItem ``validation_failed`` → ``:submit`` and ``:apply`` BLOCKED (409) and the
  ref/history UNCHANGED.
* Rollback HAPPY path: a ``rollback_request`` repeats validate → submit → B approve →
  apply against the REAL deterministic ``rollback_validator@1`` history + schema ports,
  and the ref/history MOVE BACK to the prior revision (with a ref transition).
* Rollback fail-closed: a rollback whose ref advances out from under it fails the
  deterministic history dimension → ``validation_failed`` → ``:submit`` BLOCKED, ref
  unmoved.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.apps.api.local import LocalApiConfig, create_readiness_closed_local_app
from gameforge.apps.worker.app import LocalWorkerConfig
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.auth import (
    PasswordCredentialRecordV1,
    SecretText,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.identity import (
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
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime, normalize_login_name
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.spine.ir.snapshot import Snapshot

from tests.e2e.m4c.test_composition import (
    OBJECT_STORE_ID,
    _normalization_policy,
    _password_policy,
    _session_policy,
    _shared_budget,
    _signing_keys,
)
from tests.platform.m4 import apply_testkit

# Journey B runs in the "builtin" domain — the exact domain the composed admission's
# built-in execution-profile catalog covers (every builtin profile is domain_scope
# ("builtin",)). A content-domain rollback cannot apply against a builtin-scoped
# rollback profile (apply.py's ExactRollbackExecutionVerifier requires the item domain
# ⊆ the profile domain), so the whole maker-checker journey — patch AND rollback — is
# routed here, keeping every profile binding domain-consistent end-to-end.
DOMAIN = "builtin"
REF_NAME = "content-head"
NOW = "2026-07-16T12:00:00Z"
REGISTRY_VERSION = "journey-b-domains@1"
ROUTE_VERSION = "journey-b-routes@1"
ROLE_POLICY_VERSION = "journey-b-roles@1"
_DOMAIN = DomainScope(domain_ids=(DOMAIN,))

MAKER_LOGIN = "maker"
MAKER_PASSWORD = "maker-password-1"
APPROVER_LOGIN = "approver"
APPROVER_PASSWORD = "approver-password-1"

_READS = (
    Permission(action="read", resource_kind="run", domain_scope="all"),
    Permission(action="read", resource_kind="approval", domain_scope="all"),
    Permission(action="read", resource_kind="artifact", domain_scope="all"),
    Permission(action="read", resource_kind="spec", domain_scope="all"),
    Permission(action="read", resource_kind="execution_profile", domain_scope="all"),
)


def _registry() -> DomainRegistryV1:
    definitions = (DomainDefinitionV1(domain_id=DOMAIN, display_name="Built-in", status="active"),)
    return DomainRegistryV1(
        registry_version=REGISTRY_VERSION,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(REGISTRY_VERSION, definitions),
    )


def _route(registry: DomainRegistryV1) -> DomainRoutePolicy:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version, registry_digest=registry.registry_digest
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
    return DomainRoutePolicy(
        route_version=ROUTE_VERSION,
        domain_registry_ref=ref,
        rules=rules,
        effective_from="2026-07-14T00:00:00Z",
        route_digest=compute_domain_route_policy_digest(
            ROUTE_VERSION, ref, rules, "2026-07-14T00:00:00Z"
        ),
    )


def _role_policy(registry) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        # Maker A: draft needs no grant; :validate needs validate/patch (+ rollback);
        # submit needs no grant.
        "content_designer": (
            Permission(action="validate", resource_kind="patch", domain_scope=_DOMAIN),
            Permission(action="validate", resource_kind="rollback_request", domain_scope=_DOMAIN),
            *_READS,
        ),
        # Approver B == the route_role: approval.decide + apply/rollback (applies too).
        "numeric_designer": (
            Permission(action="approval.decide", resource_kind="approval", domain_scope=_DOMAIN),
            Permission(action="apply", resource_kind="patch", domain_scope=_DOMAIN),
            Permission(action="rollback", resource_kind="ref", domain_scope=_DOMAIN),
            Permission(action="publish", resource_kind="constraint_proposal", domain_scope=_DOMAIN),
            *_READS,
        ),
    }
    return RolePolicy(
        policy_version=ROLE_POLICY_VERSION,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from="2026-07-14T00:00:00Z",
        policy_digest=compute_role_policy_digest(
            ROLE_POLICY_VERSION, registry_ref, grants, "2026-07-14T00:00:00Z"
        ),
    )


@dataclass
class _Session:
    client: TestClient
    csrf: str


class _Harness:
    """Composed API + worker + governance + two provisioned humans over one DB."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.database_url = f"sqlite:///{tmp_path / 'journey-b.db'}"
        self.object_root = tmp_path / "objects"
        self.clock = SystemUtcClock()
        migrations_api.upgrade(self.database_url)

        self.registry = _registry()
        self.route = _route(self.registry)
        self.approval_policy = apply_testkit._approval_policy()
        self.role_policy = _role_policy(self.registry)
        # The composed admission resolves profiles against the BUILTIN catalog
        # (build_builtin_registry), so the authoritative DB snapshot must be that exact
        # catalog — not a test-local one — or the exact-ref catalog lookup fails closed.
        self.catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
        self._seed_policies()
        self._provision_humans()

    # ── governance + identity seeding (out-of-band fixture bootstrap) ─────────
    def _seed_policies(self) -> None:
        from gameforge.contracts.workflow import (
            ApprovalPolicyRegistryV1,
            compute_approval_policy_registry_digest,
        )

        engine = get_engine(self.database_url)
        approval_registry = ApprovalPolicyRegistryV1(
            policies=(self.approval_policy,),
            registry_digest=compute_approval_policy_registry_digest((self.approval_policy,)),
        )
        with Session(engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=self.clock)
            policies.put_login_name_normalization_policy(_normalization_policy())
            policies.put_password_hash_policy(_password_policy())
            policies.put_session_policy(_session_policy())
            policies.put_domain_registry(self.registry)
            policies.put_domain_route_policy(self.route)
            policies.put_role_policy(self.role_policy)
            policies.put_approval_policy_registry(approval_registry)
            policies.put_execution_profile_catalog(self.catalog)
            costs = SqlCostLedger(session, clock=self.clock)
            costs.put_budget(
                _shared_budget(
                    budget_id="budget:principal:human:maker",
                    scope_kind="principal",
                    scope_id="human:maker",
                )
            )
            costs.put_budget(
                _shared_budget(
                    budget_id="budget:system:global",
                    scope_kind="system",
                    scope_id="global",
                )
            )
        engine.dispose()

    def _provision_humans(self) -> None:
        self._provision_human(
            principal_id="human:maker",
            login=MAKER_LOGIN,
            password=MAKER_PASSWORD,
            display_name="Maker A",
            role="content_designer",
        )
        self._provision_human(
            principal_id="human:approver",
            login=APPROVER_LOGIN,
            password=APPROVER_PASSWORD,
            display_name="Approver B",
            role="numeric_designer",
        )

    def _provision_human(
        self, *, principal_id: str, login: str, password: str, display_name: str, role: str
    ) -> None:
        normalization = _normalization_policy()
        hash_policy = _password_policy()
        runtime = Argon2PasswordRuntime()
        engine = get_engine(self.database_url)
        with Session(engine) as session, session.begin():
            identities = SqlIdentityRepository(session, clock=self.clock)
            auth = SqlAuthRepository(session, clock=self.clock)
            created = identities.create(
                principal_id=principal_id, kind="human", display_name=display_name
            )
            auth.create_password(
                PasswordCredentialRecordV1(
                    credential_id=f"password:{principal_id}",
                    principal_id=principal_id,
                    normalized_login_name=normalize_login_name(login, normalization),
                    normalization_policy_version=normalization.policy_version,
                    normalization_policy_digest=normalization.policy_digest,
                    password_hash=runtime.hash_password(SecretText(password), hash_policy),
                    hash_policy_version=hash_policy.policy_version,
                    credential_version=1,
                    status="active",
                    changed_at=NOW,
                    revision=1,
                )
            )
            bumped = identities.bump_credential_epoch(
                principal_id, expected_revision=created.revision
            )
            identities.grant(
                assignment_id=f"assignment:{principal_id}:{role}",
                principal_id=principal_id,
                role=role,
                scope=_DOMAIN,
                granted_by=AuditActor(principal_id="system:test", principal_kind="system"),
                expected_principal_revision=bumped.revision,
            )
        engine.dispose()

    # ── base ref (a quest snapshot bound to content/head @ rev 1) ────────────
    def seed_base_snapshot(self) -> tuple[str, dict]:
        # A referentially-complete quest graph that the production GraphChecker passes
        # with ZERO findings (quest has a giver + a step; no isolated/dangling nodes), so
        # a clean Patch's preview validates against real invariant evidence.
        snapshot = Snapshot.from_entities_relations(
            [
                Entity(id="npc:giver", type=NodeType.NPC, attrs={}),
                Entity(id="qs:1", type=NodeType.QUEST_STEP, attrs={}),
                Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120}),
            ],
            [
                Relation(id="r:starts", type=EdgeType.STARTS_AT, src_id="q:1", dst_id="npc:giver"),
                Relation(id="r:step", type=EdgeType.HAS_STEP, src_id="q:1", dst_id="qs:1"),
            ],
        )
        blob = canonical_json(snapshot.content_payload).encode("utf-8")
        objects = self._object_store()
        stored = objects.put_verified(blob)
        artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(ir_snapshot_id=snapshot.snapshot_id, tool_version="base@1"),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "ir-core@1",
                "domain_scope": _DOMAIN.model_dump(mode="json"),
            },
            created_at=NOW,
        )
        engine = get_engine(self.database_url)
        with Session(engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                clock=self.clock,
            ).put(artifact)
            ref = SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                clock=self.clock,
            ).compare_and_set(REF_NAME, None, artifact.artifact_id)
        engine.dispose()
        return artifact.artifact_id, ref.model_dump(mode="json")

    # ── composition configs ──────────────────────────────────────────────────
    def _object_store(self) -> LocalObjectStore:
        return LocalObjectStore(
            self.object_root,
            store_id=OBJECT_STORE_ID,
            clock=self.clock,
            cursor_signing_key=b"o" * 32,
        )

    def api_config(self) -> LocalApiConfig:
        return LocalApiConfig(
            database_url=self.database_url,
            object_store_root=self.object_root,
            object_store_id=OBJECT_STORE_ID,
            telemetry_db_path=self.tmp_path / "api-telemetry.sqlite3",
            current_password_hash_policy_version="argon2id@1",
            session_policy_version="session@1",
            role_policy_version=self.role_policy.policy_version,
            role_policy_digest=self.role_policy.policy_digest,
            audit_chain_id="identity",
            root_secret=b"r" * 32,
            session_signing_keys=_signing_keys(),
            workflow_route_policy_version=self.route.route_version,
            workflow_route_policy_digest=self.route.route_digest,
            workflow_approval_policy_version=self.approval_policy.policy_version,
            workflow_approval_policy_digest=self.approval_policy.policy_digest,
        )

    def worker_config(self) -> LocalWorkerConfig:
        return LocalWorkerConfig(
            database_url=self.database_url,
            object_store_root=self.object_root,
            object_store_id=OBJECT_STORE_ID,
            telemetry_db_path=self.tmp_path / "worker-telemetry.sqlite3",
            worker_principal_id="service:worker:1",
            reaper_principal_id="system:lease-reaper",
            root_secret=b"w" * 32,
        )

    # ── read-back helpers (authoritative store reads for test scaffolding) ────
    def load_item(self, approval_id: str):
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session:
                return SqlApprovalRepository(session).get(approval_id)
        finally:
            engine.dispose()

    def run_record(self, run_id: str):
        from gameforge.runtime.persistence.runs import SqlRunRepository

        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session:
                return SqlRunRepository(session).get(run_id)
        finally:
            engine.dispose()

    def current_ref(self) -> dict:
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session:
                value = SqlRefStore(
                    session,
                    cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                    clock=self.clock,
                ).get(REF_NAME)
                return value.model_dump(mode="json")
        finally:
            engine.dispose()

    def ref_history(self, revision: int) -> dict | None:
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session:
                value = SqlRefStore(
                    session,
                    cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                    clock=self.clock,
                ).get_history_entry(REF_NAME, revision)
                return None if value is None else value.model_dump(mode="json")
        finally:
            engine.dispose()


# ── HTTP session helpers ─────────────────────────────────────────────────────
def _start_api(config: LocalApiConfig):
    """Build the composed API and run its startup effect (audit-cache refresh).

    The tests use several independent ``TestClient`` cookie jars over one app, so
    they drive the lifespan startup manually (a single ``with TestClient`` would own
    the shared lifespan and close the resources for every sibling client). Call
    ``api.state.local_resources.close()`` when done.
    """

    api = create_readiness_closed_local_app(config)
    api.state.local_resources.refresh_audit_cache()
    return api


def _login(app, login_name: str, password: str) -> _Session:
    client = TestClient(app, base_url="https://gameforge.test")
    response = client.post(
        "/api/v1/auth/login", json={"login_name": login_name, "password": password}
    )
    assert response.status_code == 204, response.text
    return _Session(client=client, csrf=response.headers["X-CSRF-Token"])


def _headers(
    session: _Session,
    *,
    idempotency_key: str,
    resource_kind: str | None = None,
    resource_id: str | None = None,
    revision: int | None = None,
) -> dict[str, str]:
    etag = '"etag:journey-b"'
    if resource_kind is not None and resource_id is not None and revision is not None:
        etag = compute_resource_etag(
            resource_kind=resource_kind,
            resource_id=resource_id,
            revision=revision,
        )
    return {
        "Idempotency-Key": idempotency_key,
        "If-Match": etag,
        "X-CSRF-Token": session.csrf,
    }


def _patch_body(
    *,
    base_artifact_id: str,
    expected_ref: dict,
    new_value: int,
    rationale: str,
    old_value: int = 120,
):
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": REF_NAME,
        "expected_ref": expected_ref,
        "expected_to_fix": [],
        "preconditions": [],
        "side_effect_risk": "low",
        "ops": [
            {
                "op_id": "set-reward-gold",
                "op": "set_entity_attr",
                "target": "q:1.reward_gold",
                "old_value": old_value,
                "new_value": new_value,
            }
        ],
        "rationale": rationale,
        "candidate_export_profiles": [],
    }


def _dangling_patch_body(*, base_artifact_id: str, expected_ref: dict):
    return {
        "request_schema_version": "human-patch-draft-request@1",
        "base_snapshot_artifact_id": base_artifact_id,
        "constraint_snapshot_artifact_id": None,
        "ref_name": REF_NAME,
        "expected_ref": expected_ref,
        "expected_to_fix": [],
        "preconditions": [],
        "side_effect_risk": "high",
        "ops": [
            {
                "op_id": "add-dangling-drop",
                "op": "add_relation",
                "target": "r:dangling",
                "old_value": None,
                "new_value": {
                    "id": "r:dangling",
                    "type": "DROPS_FROM",
                    "src_id": "monster:ghost",
                    "dst_id": "q:1",
                },
            }
        ],
        "rationale": "Introduce a drop from a monster that does not exist (regression).",
        "candidate_export_profiles": [],
    }


def _validation_body(item, *, base_artifact_id: str, expected_ref: dict, checker_graph: bool):
    binding = item.target_binding
    checker_profiles = [{"profile_id": "builtin.checker", "version": 1}] if checker_graph else []
    return {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": item.approval_id,
        "expected_subject_head_revision": item.subject_revision,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "base_snapshot_artifact_id": base_artifact_id,
        "preview_snapshot_artifact_id": binding.target_artifact_id,
        "candidate_config_export_artifact_ids": [],
        "target": {"ref_name": REF_NAME, "expected_ref": expected_ref},
        "validation_policy": {"profile_id": "builtin.validation", "version": 1},
        "checker_profiles": checker_profiles,
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }


async def _drive(dispatcher, harness: _Harness, run_id: str, *, max_iterations: int = 80):
    for _ in range(max_iterations):
        run = harness.run_record(run_id)
        if run is not None and run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return run
        await dispatcher.dispatch_once()
    return harness.run_record(run_id)


# ── the composed happy-path cycle (draft→validate→submit→approve→apply) ──────
@dataclass
class _CycleResult:
    approval_id: str
    patch_artifact_id: str
    validation_run_id: str
    evidence_set_artifact_id: str
    new_ref: dict


def _run_patch_cycle(
    harness: _Harness,
    process,
    maker: _Session,
    approver: _Session,
    *,
    base_artifact_id: str,
    expected_ref: dict,
    new_value: int,
    key: str,
    old_value: int = 120,
) -> _CycleResult:
    # A drafts the Patch (+preview) through the public API.
    draft = maker.client.post(
        "/api/v1/patches",
        json=_patch_body(
            base_artifact_id=base_artifact_id,
            expected_ref=expected_ref,
            new_value=new_value,
            old_value=old_value,
            rationale=f"Set reward to {new_value}.",
        ),
        headers=_headers(maker, idempotency_key=f"{key}:draft"),
    )
    assert draft.status_code == 201, draft.text
    patch_artifact_id = draft.json()["artifact"]["artifact_id"]
    approval_id = f"approval:patch:{patch_artifact_id}"
    item = harness.load_item(approval_id)
    assert item is not None and item.status == "draft"

    # A validates: the composed :validate admits the Run AND (17c) CASes draft→validating.
    validate = maker.client.post(
        f"/api/v1/patches/{patch_artifact_id}:validate",
        json=_validation_body(
            item, base_artifact_id=base_artifact_id, expected_ref=expected_ref, checker_graph=True
        ),
        headers=_headers(
            maker,
            idempotency_key=f"{key}:validate",
            resource_kind="patch",
            resource_id=patch_artifact_id,
            revision=item.workflow_revision,
        ),
    )
    assert validate.status_code == 202, validate.text
    run_id = validate.json()["run_id"]
    validating = harness.load_item(approval_id)
    assert validating.status == "validating"
    assert validating.active_validation_run_id == run_id

    # Worker runs patch.validate → terminal effect CASes validating→validated.
    terminal = asyncio.run(_drive(process.dispatcher, harness, run_id))
    assert terminal is not None and terminal.status == "succeeded", (
        f"validation run terminated as {None if terminal is None else terminal.status!r}"
    )
    validated = harness.load_item(approval_id)
    assert validated.status == "validated", validated.status
    assert validated.evidence_set_artifact_id is not None

    # A submits for approval.
    submit = maker.client.post(
        f"/api/v1/patches/{patch_artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": validated.workflow_revision,
        },
        headers=_headers(
            maker,
            idempotency_key=f"{key}:submit",
            resource_kind="patch",
            resource_id=patch_artifact_id,
            revision=validated.workflow_revision,
        ),
    )
    assert submit.status_code == 200, submit.text
    pending = harness.load_item(approval_id)
    assert pending.status == "pending_approval"

    # B (≠A) approves.
    approve = approver.client.post(
        f"/api/v1/approvals/{approval_id}:approve",
        json={
            "request_schema_version": "approval-decision-request@1",
            "decision": "approve",
            "requirement_ids": [r.requirement_id for r in pending.requirements],
            "expected_workflow_revision": pending.workflow_revision,
            "reason_code": "independent_review_passed",
        },
        headers=_headers(
            approver,
            idempotency_key=f"{key}:approve",
            resource_kind="approval",
            resource_id=approval_id,
            revision=pending.workflow_revision,
        ),
    )
    assert approve.status_code == 200, approve.text
    approved = harness.load_item(approval_id)
    assert approved.status == "approved"
    binding = approved.target_binding

    # B applies — the ref MOVES to the exact preview Artifact.
    apply = approver.client.post(
        f"/api/v1/patches/{approved.subject_artifact_id}:apply",
        json={
            "request_schema_version": "workflow-apply-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": approved.workflow_revision,
            "subject_digest": approved.subject_digest,
            "target_artifact_id": binding.target_artifact_id,
            "target_digest": binding.target_digest,
            "ref_name": REF_NAME,
            "expected_ref": binding.expected_ref.model_dump(mode="json"),
        },
        headers=_headers(
            approver,
            idempotency_key=f"{key}:apply",
            resource_kind="patch",
            resource_id=approved.subject_artifact_id,
            revision=approved.workflow_revision,
        ),
    )
    assert apply.status_code == 200, apply.text
    new_ref = apply.json()["ref_value"]
    assert new_ref["artifact_id"] == binding.target_artifact_id
    return _CycleResult(
        approval_id=approval_id,
        patch_artifact_id=patch_artifact_id,
        validation_run_id=run_id,
        evidence_set_artifact_id=validated.evidence_set_artifact_id,
        new_ref=new_ref,
    )


# ═══════════════════════════ tests ══════════════════════════════════════════
def test_journey_b_maker_checker_happy_path_repeat_restart_and_coverage(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()

    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        readyz = TestClient(api, base_url="https://gameforge.test").get("/readyz")
        assert readyz.status_code == 200, readyz.text

        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)

        # ── Cycle 1: full happy path; the ref moves rev 1 → rev 2 ──────────
        cycle1 = _run_patch_cycle(
            harness,
            process,
            maker,
            approver,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            new_value=80,
            key="c1",
        )
        assert cycle1.new_ref["revision"] == 2
        assert harness.current_ref()["revision"] == 2
        assert harness.ref_history(1)["artifact_id"] == base_artifact_id
        assert harness.ref_history(2)["artifact_id"] == cycle1.new_ref["artifact_id"]

        # ── Coverage: idempotency conflict — same key, different payload ───
        first = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=cycle1.new_ref["artifact_id"],
                expected_ref=cycle1.new_ref,
                new_value=70,
                old_value=80,
                rationale="Idempotent draft.",
            ),
            headers=_headers(maker, idempotency_key="idem:dup"),
        )
        assert first.status_code == 201, first.text
        conflict = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=cycle1.new_ref["artifact_id"],
                expected_ref=cycle1.new_ref,
                new_value=71,
                old_value=80,
                rationale="Different payload, same idempotency key.",
            ),
            headers=_headers(maker, idempotency_key="idem:dup"),
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "idempotency_conflict", conflict.text

        # ── correlation read-back on the cycle-1 validation run ───────────
        _assert_run_correlation(
            harness,
            maker,
            run_id=cycle1.validation_run_id,
            approval_id=cycle1.approval_id,
            evidence_set_artifact_id=cycle1.evidence_set_artifact_id,
            patch_artifact_id=cycle1.patch_artifact_id,
        )
    finally:
        process.close()
        api.state.local_resources.close()

    # ── Cycle 2 spanning an API + worker RESTART over the SAME DB ────────────
    head_ref = harness.current_ref()
    head_artifact_id = head_ref["artifact_id"]

    api2 = _start_api(harness.api_config())
    process2 = build_worker_process(harness.worker_config())
    approval_id2 = ""
    run_id2 = ""
    try:
        maker2 = _login(api2, MAKER_LOGIN, MAKER_PASSWORD)
        draft = maker2.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=head_artifact_id,
                expected_ref=head_ref,
                new_value=60,
                old_value=80,
                rationale="Second cycle across a restart.",
            ),
            headers=_headers(maker2, idempotency_key="c2:draft"),
        )
        assert draft.status_code == 201, draft.text
        patch2 = draft.json()["artifact"]["artifact_id"]
        approval_id2 = f"approval:patch:{patch2}"
        item2 = harness.load_item(approval_id2)
        validate = maker2.client.post(
            f"/api/v1/patches/{patch2}:validate",
            json=_validation_body(
                item2, base_artifact_id=head_artifact_id, expected_ref=head_ref, checker_graph=True
            ),
            headers=_headers(
                maker2,
                idempotency_key="c2:validate",
                resource_kind="patch",
                resource_id=patch2,
                revision=item2.workflow_revision,
            ),
        )
        assert validate.status_code == 202, validate.text
        run_id2 = validate.json()["run_id"]
        # The validation Run is QUEUED but NOT yet driven — leave it for the rebuilt worker.
        assert harness.run_record(run_id2).status == "queued"
        assert harness.load_item(approval_id2).status == "validating"
    finally:
        process2.close()
        api2.state.local_resources.close()
        # api2 TestClient contexts are per-request; nothing to close beyond the process.

    # State persists across the restart: the applied ref is unchanged on the shared DB.
    assert harness.current_ref()["revision"] == 2

    # Rebuild BOTH the API and the worker over the SAME DB/object store.
    api3 = _start_api(harness.api_config())
    process3 = build_worker_process(harness.worker_config())
    try:
        # The queued Run still completes on the rebuilt worker.
        terminal = asyncio.run(_drive(process3.dispatcher, harness, run_id2))
        assert terminal is not None and terminal.status == "succeeded", (
            f"post-restart run terminated as {None if terminal is None else terminal.status!r}"
        )
        validated2 = harness.load_item(approval_id2)
        assert validated2.status == "validated"

        # Re-login on the rebuilt API (sessions are DB-backed; workflow continues).
        maker3 = _login(api3, MAKER_LOGIN, MAKER_PASSWORD)
        approver3 = _login(api3, APPROVER_LOGIN, APPROVER_PASSWORD)

        submit = maker3.client.post(
            f"/api/v1/patches/{validated2.subject_artifact_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id2,
                "expected_workflow_revision": validated2.workflow_revision,
            },
            headers=_headers(
                maker3,
                idempotency_key="c2:submit",
                resource_kind="patch",
                resource_id=validated2.subject_artifact_id,
                revision=validated2.workflow_revision,
            ),
        )
        assert submit.status_code == 200, submit.text
        pending2 = harness.load_item(approval_id2)

        # ── Coverage: A self-approval is rejected (maker-checker, 403) ────────
        self_approve = maker3.client.post(
            f"/api/v1/approvals/{approval_id2}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [r.requirement_id for r in pending2.requirements],
                "expected_workflow_revision": pending2.workflow_revision,
                "reason_code": "self_approval_attempt",
            },
            headers=_headers(
                maker3,
                idempotency_key="c2:self-approve",
                resource_kind="approval",
                resource_id=approval_id2,
                revision=pending2.workflow_revision,
            ),
        )
        assert self_approve.status_code == 403, self_approve.text

        # ── Coverage: a stale expected_workflow_revision → 409 ────────────────
        stale = approver3.client.post(
            f"/api/v1/approvals/{approval_id2}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [r.requirement_id for r in pending2.requirements],
                "expected_workflow_revision": pending2.workflow_revision + 99,
                "reason_code": "independent_review_passed",
            },
            headers=_headers(
                approver3,
                idempotency_key="c2:stale-approve",
                resource_kind="approval",
                resource_id=approval_id2,
                revision=pending2.workflow_revision,
            ),
        )
        assert stale.status_code == 409, stale.text

        # B approves with the exact revision, then applies — ref moves rev 2 → rev 3.
        approve = approver3.client.post(
            f"/api/v1/approvals/{approval_id2}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [r.requirement_id for r in pending2.requirements],
                "expected_workflow_revision": pending2.workflow_revision,
                "reason_code": "independent_review_passed",
            },
            headers=_headers(
                approver3,
                idempotency_key="c2:approve",
                resource_kind="approval",
                resource_id=approval_id2,
                revision=pending2.workflow_revision,
            ),
        )
        assert approve.status_code == 200, approve.text
        approved2 = harness.load_item(approval_id2)
        binding2 = approved2.target_binding
        apply = approver3.client.post(
            f"/api/v1/patches/{approved2.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": approval_id2,
                "expected_workflow_revision": approved2.workflow_revision,
                "subject_digest": approved2.subject_digest,
                "target_artifact_id": binding2.target_artifact_id,
                "target_digest": binding2.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": binding2.expected_ref.model_dump(mode="json"),
            },
            headers=_headers(
                approver3,
                idempotency_key="c2:apply",
                resource_kind="patch",
                resource_id=approved2.subject_artifact_id,
                revision=approved2.workflow_revision,
            ),
        )
        assert apply.status_code == 200, apply.text
        assert apply.json()["ref_value"]["revision"] == 3
        assert harness.current_ref()["revision"] == 3
        assert harness.ref_history(3)["artifact_id"] == binding2.target_artifact_id
    finally:
        process3.close()
        api3.state.local_resources.close()


def _assert_run_correlation(
    harness: _Harness,
    reader: _Session,
    *,
    run_id: str,
    approval_id: str,
    evidence_set_artifact_id: str,
    patch_artifact_id: str,
) -> None:
    from gameforge.runtime.persistence.runs import SqlRunRepository

    # lineage (read API): the published EvidenceSet is bound to this run + subject.
    artifact = reader.client.get(f"/api/v1/artifacts/{evidence_set_artifact_id}")
    assert artifact.status_code == 200, artifact.text
    body = artifact.json()
    assert body["artifact"]["kind"] == "validation_evidence"
    assert body["payload"]["validation_run_id"] == run_id
    assert patch_artifact_id in body["artifact"]["parent_artifact_ids"]

    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            runs = SqlRunRepository(session)
            # run: the validation Run reached a succeeded terminal on the shared authority.
            run = runs.get(run_id)
            assert run is not None and run.status == "succeeded"
            assert run.kind.kind == "patch.validate"
            # log/trace: the run's authoritative execution event stream, keyed on run_id.
            events = runs.list_events(run_id, after_seq=0, limit=100)
            event_types = {event.event_type for event in events}
            assert "run.succeeded" in event_types
            assert "attempt.started" in event_types

            # cost: the run's budget-set snapshot + reserved run-budget hold group are
            # keyed on run_id (a deterministic not_applicable run books no per-call
            # reservations, so cost accounting is the admission-time run budget hold).
            ledger = SqlCostLedger(session, clock=harness.clock)
            assert run.budget_set_snapshot_id == f"budget-set:{run_id}"
            assert run.run_budget_hold_group_id is not None
            assert ledger.get_reservation_group(run.run_budget_hold_group_id) is not None

            # audit: the chain is intact and carries a run-correlated record; the
            # validation-start record correlates on BOTH run_id and the approval subject.
            audit = SqlAuditSink(session)
            assert audit.verify_chain("identity") is True
            assert _find_audit(audit, chain_id="identity", run_id=run_id) is not None, (
                "no audit record correlated on the validation run_id"
            )
            started = _find_audit(
                audit, chain_id="identity", run_id=run_id, subject_resource_id=approval_id
            )
            assert started is not None, "no validation-start audit correlated on run_id + subject"
            assert started.action == "approval.validation_started"
    finally:
        engine.dispose()


def _find_audit(audit: SqlAuditSink, *, chain_id: str, run_id: str, subject_resource_id=None):
    seq = 1
    while True:
        record = audit.get(chain_id, seq)
        if record is None:
            return None
        if record.correlation.run_id == run_id and (
            subject_resource_id is None or record.subject.resource_id == subject_resource_id
        ):
            return record
        seq += 1


def test_journey_b_failure_patch_blocks_submit_and_apply(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()

    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)

        draft = maker.client.post(
            "/api/v1/patches",
            json=_dangling_patch_body(base_artifact_id=base_artifact_id, expected_ref=base_ref),
            headers=_headers(maker, idempotency_key="fail:draft"),
        )
        assert draft.status_code == 201, draft.text
        patch_id = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:patch:{patch_id}"
        item = harness.load_item(approval_id)

        # Validate WITH the graph checker so the dangling preview is CONFIRMED a Finding.
        validate = maker.client.post(
            f"/api/v1/patches/{patch_id}:validate",
            json=_validation_body(
                item, base_artifact_id=base_artifact_id, expected_ref=base_ref, checker_graph=True
            ),
            headers=_headers(
                maker,
                idempotency_key="fail:validate",
                resource_kind="patch",
                resource_id=patch_id,
                revision=item.workflow_revision,
            ),
        )
        assert validate.status_code == 202, validate.text
        run_id = validate.json()["run_id"]
        terminal = asyncio.run(_drive(process.dispatcher, harness, run_id))
        assert terminal is not None and terminal.status == "succeeded", (
            f"validation run terminated as {None if terminal is None else terminal.status!r}"
        )
        failed = harness.load_item(approval_id)
        assert failed.status == "validation_failed", failed.status
        assert failed.evidence_set_artifact_id is not None

        # A failed EvidenceSet blocks submit (409 workflow guard) …
        submit = maker.client.post(
            f"/api/v1/patches/{patch_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": failed.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="fail:submit",
                resource_kind="patch",
                resource_id=patch_id,
                revision=failed.workflow_revision,
            ),
        )
        assert submit.status_code == 409, submit.text

        # … and apply (409 workflow guard); the ref/history are UNCHANGED.
        binding = failed.target_binding
        apply = approver.client.post(
            f"/api/v1/patches/{failed.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": failed.workflow_revision,
                "subject_digest": failed.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": base_ref,
            },
            headers=_headers(
                approver,
                idempotency_key="fail:apply",
                resource_kind="patch",
                resource_id=failed.subject_artifact_id,
                revision=failed.workflow_revision,
            ),
        )
        assert apply.status_code == 409, apply.text

        assert harness.current_ref() == base_ref
        assert harness.ref_history(2) is None

        # The failed EvidenceSet + a confirmed Finding are readable through the API.
        evidence = maker.client.get(f"/api/v1/artifacts/{failed.evidence_set_artifact_id}")
        assert evidence.status_code == 200, evidence.text
        assert evidence.json()["payload"]["overall_status"] == "failed"
    finally:
        process.close()
        api.state.local_resources.close()


def _rollback_validation_body(
    item, *, head_ref: dict, target_artifact_id: str, target_revision: int
):
    return {
        "request_schema_version": "rollback-validation-admission-request@1",
        "approval_id": item.approval_id,
        "expected_subject_head_revision": item.subject_revision,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "ref_name": REF_NAME,
        "expected_current_ref": head_ref,
        "target_artifact_id": target_artifact_id,
        "target_history_revision": target_revision,
        "rollback_profile": {"profile_id": "builtin.rollback", "version": 1},
        "schema_compatibility_policy": {
            "profile_id": "builtin.schema_compatibility",
            "version": 1,
        },
        "impact_profiles": [],
        "regression_suite_artifact_ids": [],
    }


def _draft_rollback(
    maker: _Session, *, head_ref: dict, target_artifact_id: str, target_revision: int, key: str
):
    draft = maker.client.post(
        f"/api/v1/refs/{REF_NAME}/rollback-requests",
        json={
            "request_schema_version": "rollback-draft-request@1",
            "expected_current_ref": head_ref,
            "target_artifact_id": target_artifact_id,
            "target_history_revision": target_revision,
            "rollback_profile": {"profile_id": "builtin.rollback", "version": 1},
            "reason": "Revert the reward change.",
            "reverses_approval_id": None,
        },
        headers=_headers(maker, idempotency_key=f"{key}:draft"),
    )
    assert draft.status_code == 201, draft.text
    rollback_artifact_id = draft.json()["artifact"]["artifact_id"]
    return rollback_artifact_id, f"approval:rollback_request:{rollback_artifact_id}"


def test_journey_b_rollback_happy_path_moves_ref_back(tmp_path: Path) -> None:
    # Journey B line 5: the rollback request repeats validate → submit → B approve → apply,
    # and the ref/history MOVE BACK. The rollback_validator@1 history + schema ports are
    # real deterministic platform reads (apps/worker/components.py); the impact analyzer
    # stays deferred and is never invoked (no impact profiles on the happy path).
    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()

    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)
        # Move the ref to rev 2 (reward 120 → 80) so a prior revision exists to restore.
        _run_patch_cycle(
            harness,
            process,
            maker,
            approver,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            new_value=80,
            key="rb-setup",
        )
        head_ref = harness.current_ref()
        assert head_ref["revision"] == 2 and head_ref["artifact_id"] != base_artifact_id

        # A drafts a rollback_request back to the base (history revision 1).
        rollback_id, approval_id = _draft_rollback(
            maker,
            head_ref=head_ref,
            target_artifact_id=base_artifact_id,
            target_revision=1,
            key="rb",
        )
        item = harness.load_item(approval_id)
        assert item is not None and item.status == "draft"

        # :validate → the real rollback ports pass → passed EvidenceSet → validated.
        validate = maker.client.post(
            f"/api/v1/rollback-requests/{rollback_id}:validate",
            json=_rollback_validation_body(
                item, head_ref=head_ref, target_artifact_id=base_artifact_id, target_revision=1
            ),
            headers=_headers(
                maker,
                idempotency_key="rb:validate",
                resource_kind="rollback_request",
                resource_id=rollback_id,
                revision=item.workflow_revision,
            ),
        )
        assert validate.status_code == 202, validate.text
        run_id = validate.json()["run_id"]
        assert harness.load_item(approval_id).status == "validating"
        terminal = asyncio.run(_drive(process.dispatcher, harness, run_id))
        assert terminal is not None and terminal.status == "succeeded", (
            f"rollback validation terminated as {None if terminal is None else terminal.status!r}"
        )
        validated = harness.load_item(approval_id)
        assert validated.status == "validated", validated.status
        assert validated.evidence_set_artifact_id is not None
        evidence_response = maker.client.get(
            f"/api/v1/artifacts/{validated.evidence_set_artifact_id}"
        )
        assert evidence_response.status_code == 200, evidence_response.text
        evidence_payload = evidence_response.json()["payload"]
        expected_evidence_ids = tuple(
            sorted(
                requirement["evidence_artifact_id"]
                for requirement in evidence_payload["requirements"]
            )
        )
        assert len(expected_evidence_ids) == 4
        assert validated.regression_evidence_artifact_ids == expected_evidence_ids

        # A submits; B approves; B applies — the ref MOVES BACK to the base at a new revision.
        submit = maker.client.post(
            f"/api/v1/rollback-requests/{rollback_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": validated.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="rb:submit",
                resource_kind="rollback_request",
                resource_id=rollback_id,
                revision=validated.workflow_revision,
            ),
        )
        assert submit.status_code == 200, submit.text
        pending = harness.load_item(approval_id)
        approve = approver.client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "approve",
                "requirement_ids": [r.requirement_id for r in pending.requirements],
                "expected_workflow_revision": pending.workflow_revision,
                "reason_code": "independent_review_passed",
            },
            headers=_headers(
                approver,
                idempotency_key="rb:approve",
                resource_kind="approval",
                resource_id=approval_id,
                revision=pending.workflow_revision,
            ),
        )
        assert approve.status_code == 200, approve.text
        approved = harness.load_item(approval_id)
        binding = approved.target_binding
        apply = approver.client.post(
            f"/api/v1/rollback-requests/{approved.subject_artifact_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": approved.workflow_revision,
                "subject_digest": approved.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": binding.expected_ref.model_dump(mode="json"),
            },
            headers=_headers(
                approver,
                idempotency_key="rb:apply",
                resource_kind="rollback_request",
                resource_id=approved.subject_artifact_id,
                revision=approved.workflow_revision,
            ),
        )
        assert apply.status_code == 200, apply.text
        result = apply.json()
        # The rollback moved the ref BACK to the base Artifact, at a NEW revision, and
        # recorded a ref transition (rollback apply, unlike a patch apply, is a transition).
        assert result["ref_value"]["artifact_id"] == base_artifact_id
        assert result["ref_value"]["revision"] == 3
        assert result["ref_transition_id"] is not None
        assert harness.current_ref() == {"artifact_id": base_artifact_id, "revision": 3}
        assert harness.ref_history(3)["artifact_id"] == base_artifact_id
    finally:
        process.close()
        api.state.local_resources.close()


def test_journey_b_rollback_stale_ref_fails_closed(tmp_path: Path) -> None:
    # Fail-closed rollback surface: a rollback drafted against rev 2 whose ref then MOVES
    # to rev 3 (out from under it) fails the deterministic history dimension at validation
    # → failed EvidenceSet → validation_failed → submit BLOCKED (409); the ref never moves
    # back. (Soundness: the real history port confirms the exact current-ref binding; it
    # never passes a rollback whose base has advanced.)
    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()

    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)
        cycle1 = _run_patch_cycle(
            harness,
            process,
            maker,
            approver,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            new_value=80,
            key="rbf-1",
        )
        head_ref = harness.current_ref()
        assert head_ref["revision"] == 2

        # A drafts a valid rollback to the base (rev 1), binding the current head (rev 2).
        rollback_id, approval_id = _draft_rollback(
            maker,
            head_ref=head_ref,
            target_artifact_id=base_artifact_id,
            target_revision=1,
            key="rbf",
        )
        item = harness.load_item(approval_id)
        assert item.status == "draft"

        # The ref advances to rev 3 (a second, independent patch cycle) — the rollback's
        # bound base is now stale.
        _run_patch_cycle(
            harness,
            process,
            maker,
            approver,
            base_artifact_id=cycle1.new_ref["artifact_id"],
            expected_ref=head_ref,
            new_value=60,
            old_value=80,
            key="rbf-2",
        )
        assert harness.current_ref()["revision"] == 3

        # Exact admission re-reads the bound ref and rejects the now-stale rollback before
        # creating a Run (current rev 3 != the retained expected rev 2).
        validate = maker.client.post(
            f"/api/v1/rollback-requests/{rollback_id}:validate",
            json=_rollback_validation_body(
                item, head_ref=head_ref, target_artifact_id=base_artifact_id, target_revision=1
            ),
            headers=_headers(
                maker,
                idempotency_key="rbf:validate",
                resource_kind="rollback_request",
                resource_id=rollback_id,
                revision=item.workflow_revision,
            ),
        )
        assert validate.status_code == 409, validate.text
        rejected = harness.load_item(approval_id)
        assert rejected.status == "draft"
        assert rejected.active_validation_run_id is None

        submit = maker.client.post(
            f"/api/v1/rollback-requests/{rollback_id}:submit-for-approval",
            json={
                "request_schema_version": "submit-for-approval-request@1",
                "approval_id": approval_id,
                "expected_workflow_revision": rejected.workflow_revision,
            },
            headers=_headers(
                maker,
                idempotency_key="rbf:submit",
                resource_kind="rollback_request",
                resource_id=rollback_id,
                revision=rejected.workflow_revision,
            ),
        )
        assert submit.status_code == 409, submit.text
        assert harness.current_ref()["revision"] == 3
    finally:
        process.close()
        api.state.local_resources.close()
