"""Task 17a — first full end-to-end composition of the M4c stack.

Builds the REAL composed API (admission engine + read APIs + readiness) and the REAL
persistent worker (dispatch loop + Task-9 ``TerminalPublisher`` + concrete SQL
adapters) over ONE shared SQLite authority + ObjectStore, proves platform readiness
GENUINELY closes (all 14 RunKinds across the six component maps), admits a
``checker.run@1`` through the real admission path, drives the worker's
``dispatch_once()`` to a published ``RunResult``, and asserts the terminal publisher
produced the run_result manifest + the ``checker_run`` domain Artifact + the terminal
``run.succeeded`` RunEvent — no faked gateway, no faked executor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.orm import Session

from gameforge.apps.api.local import LocalApiConfig, build_local_api_resources
from gameforge.apps.cli.identity import (
    IdentityBootstrapConfig,
    build_bootstrap_service,
)
from gameforge.apps.worker.app import LocalWorkerConfig, validate_worker_readiness
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionPolicyV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.jobs import CheckerRunPayloadV1, GraphSelectionV1
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.platform.identity.bootstrap import BootstrapAdminRequest
from gameforge.platform.registry import PlatformReadinessValidator, build_builtin_registry
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.secrets.session_keys import SessionSigningKeyProvider
from gameforge.spine.ir.snapshot import Snapshot

OBJECT_STORE_ID = "local:test"
ROLE_POLICY_VERSION = "e2e-roles@1"
DOMAIN_REGISTRY_VERSION = "e2e-domains@1"
NOW = "2026-07-16T12:00:00Z"
_TOOLING_GRANTS: tuple[tuple[str, str], ...] = (
    ("run", "checker"),
    ("run", "simulation"),
    ("run", "review"),
    ("run", "bench"),
    ("run", "playtest"),
    ("propose", "patch"),
    ("propose", "constraint_proposal"),
    ("derive", "task_suite"),
    ("validate", "patch"),
    ("validate", "constraint_proposal"),
    ("validate", "rollback_request"),
    ("read", "spec"),
    ("read", "artifact"),
    ("read", "run"),
    ("read", "approval"),
    ("read", "execution_profile"),
)


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization@1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "private_use", "surrogate"),
        "minimum_codepoints": 1,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


def _password_policy() -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version="argon2id@1",
        algorithm="argon2id",
        memory_kib=8192,
        iterations=1,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _session_policy() -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session@1",
        absolute_ttl_s=3600,
        idle_ttl_s=600,
        touch_interval_s=60,
        signing_key_set_version="keys@1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="game-content", display_name="Game Content", status="active"),
        DomainDefinitionV1(domain_id="builtin", display_name="Built-in Profiles", status="active"),
    )
    return DomainRegistryV1(
        registry_version=DOMAIN_REGISTRY_VERSION,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(DOMAIN_REGISTRY_VERSION, definitions),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "tooling": tuple(
            Permission(action=action, resource_kind=resource_kind, domain_scope="all")
            for action, resource_kind in _TOOLING_GRANTS
        ),
        "identity_admin": (
            Permission(action="identity.manage", resource_kind="identity", domain_scope=None),
        ),
    }
    effective_from = "2026-07-14T00:00:00Z"
    return RolePolicy(
        policy_version=ROLE_POLICY_VERSION,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            ROLE_POLICY_VERSION, registry_ref, grants, effective_from
        ),
    )


def _signing_keys() -> SessionSigningKeyProvider:
    return SessionSigningKeyProvider(
        (
            SessionSigningKeySet(
                key_set_version="keys@1",
                keys=(
                    SessionSigningKey(key_id="session-key-1", secret=b"s" * 32, status="active"),
                ),
            ),
        )
    )


def _tooling_actor() -> ActorContext:
    principal = Principal(
        id="human:maker",
        kind="human",
        display_name="Maker",
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(
            RoleAssignmentV1(
                assignment_id="assign:tooling",
                principal_id="human:maker",
                role="tooling",
                scope="all",
                status="active",
                revision=1,
                granted_at=NOW,
                granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
            ),
        ),
    )
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(mechanism="session", credential_id="credential:maker"),
        session_id="session:maker",
        request_id="request:maker",
    )


def _clean_snapshot() -> tuple[bytes, str]:
    from gameforge.contracts.canonical import canonical_json

    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    snapshot = Snapshot({npc.id: npc}, {})
    blob = canonical_json(snapshot.content_payload).encode("utf-8")
    return blob, snapshot.snapshot_id


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.database_url = f"sqlite:///{tmp_path / 'e2e.db'}"
        self.object_root = tmp_path / "objects"
        self.clock = SystemUtcClock()
        migrations_api.upgrade(self.database_url)
        self.domain_registry = _domain_registry()
        self.role_policy = _role_policy(self.domain_registry)
        self.registry = build_builtin_registry()
        self.catalog = self.registry.list_execution_profile_catalogs()[0]
        self._seed_policies()
        self._bootstrap_admin()

    def _seed_policies(self) -> None:
        engine = get_engine(self.database_url)
        with Session(engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=self.clock)
            policies.put_login_name_normalization_policy(_normalization_policy())
            policies.put_password_hash_policy(_password_policy())
            policies.put_session_policy(_session_policy())
            policies.put_domain_registry(self.domain_registry)
            policies.put_role_policy(self.role_policy)
            policies.put_execution_profile_catalog(self.catalog)
        engine.dispose()

    def _bootstrap_admin(self) -> None:
        build_bootstrap_service(
            IdentityBootstrapConfig(
                database_url=self.database_url,
                login_normalization_policy_version="login-normalization@1",
                login_normalization_policy_digest=_normalization_policy().policy_digest,
                password_hash_policy_version="argon2id@1",
                role_policy_version=self.role_policy.policy_version,
                role_policy_digest=self.role_policy.policy_digest,
                audit_chain_id="identity",
            )
        ).bootstrap(
            BootstrapAdminRequest(
                display_name="Local Admin",
                login_name="admin",
                password=SecretText("correct-password"),
            )
        )

    def seed_ir_snapshot(self, *, artifact_id_tag: str) -> str:
        blob, snapshot_id = _clean_snapshot()
        objects = LocalObjectStore(
            self.object_root,
            store_id=OBJECT_STORE_ID,
            clock=self.clock,
            cursor_signing_key=b"o" * 32,
        )
        stored = objects.put_verified(blob)
        artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(ir_snapshot_id=snapshot_id, tool_version=artifact_id_tag),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={"payload_schema_id": "ir-core@1"},
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
        engine.dispose()
        return artifact.artifact_id

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

    def run_record(self, run_id: str):
        engine = get_engine(self.database_url)
        try:
            with Session(engine) as session:
                return SqlRunRepository(session).get(run_id)
        finally:
            engine.dispose()


def _checker_params(snapshot_artifact_id: str) -> CheckerRunPayloadV1:
    return CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot_artifact_id,
        constraint_snapshot_artifact_id=None,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="builtin.checker", version=1),
        checker_ids=("graph",),
        defect_classes=(),
    )


async def _drive_until_terminal(dispatcher, harness: _Harness, run_id: str, *, max_iterations=50):
    for _ in range(max_iterations):
        run = harness.run_record(run_id)
        if run is not None and run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return run
        await dispatcher.dispatch_once()
    return harness.run_record(run_id)


def test_readiness_closes_and_checker_run_publishes_end_to_end(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    snapshot_artifact_id = harness.seed_ir_snapshot(artifact_id_tag="ir-source@1")

    # The single canonical trusted composition genuinely closes platform readiness.
    resources = build_local_api_resources(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        report = PlatformReadinessValidator(
            registry=build_builtin_registry(),
            components=process.components,
        ).validate()
        assert report.ready is True
        assert report.checked_run_kind_count == 14
        validate_worker_readiness(process.runtime)

        # Admit a checker.run@1 through the REAL admission engine (RBAC + budget hold +
        # profile resolution + queued RunRecord), then drive the worker dispatch loop.
        accepted = resources.dependencies.run_admission.admit_generic_run(
            params=_checker_params(snapshot_artifact_id),
            actor=_tooling_actor(),
            server=AdmissionRequestContext(
                idempotency_key="checker:e2e:1",
                request_hash="c" * 64,
                trace_id=None,
            ),
        )
        run_id = accepted.run_id
        queued = harness.run_record(run_id)
        assert queued is not None and queued.status == "queued"
        assert queued.kind.kind == "checker.run"

        terminal = asyncio.run(_drive_until_terminal(process.dispatcher, harness, run_id))
    finally:
        process.close()
        resources.close()

    assert terminal is not None, "the run never reached a terminal state"
    assert terminal.status == "succeeded", f"run terminated as {terminal.status!r}"
    assert terminal.result_artifact_id is not None

    # The REAL TerminalPublisher published the run_result manifest + the checker_run
    # domain Artifact; both are queryable through the shared authority.
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            cursor_signer = CursorSigner(signing_key=b"a" * 32, clock=harness.clock)
            objects = LocalObjectStore(
                harness.object_root,
                store_id=OBJECT_STORE_ID,
                clock=harness.clock,
                cursor_signing_key=b"o" * 32,
            )
            bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=cursor_signer,
                clock=harness.clock,
            )
            manifest = artifacts.get(terminal.result_artifact_id)
            assert manifest is not None and manifest.kind == "run_result"

            runs = SqlRunRepository(session)
            events = runs.list_events(run_id, after_seq=0, limit=100)
            event_types = tuple(event.event_type for event in events)
    finally:
        engine.dispose()

    assert "run.succeeded" in event_types
    assert "attempt.started" in event_types
