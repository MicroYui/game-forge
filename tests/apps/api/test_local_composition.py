from __future__ import annotations

import base64
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from gameforge.apps.api.local import (
    LOCAL_ROOT_SECRET_ENV,
    SELECTED_BENCH_REPORT_ARTIFACT_ID_ENV,
    SESSION_POLICY_VERSION_ENV,
    LocalApiConfig,
    LocalApiConfigurationError,
    _typed_playtest_payload_validators,
    build_local_api_resources,
    create_local_app,
    create_readiness_closed_local_app,
)
import gameforge.apps.api.local as local_module
from gameforge.apps.cli.identity import (
    PASSWORD_HASH_POLICY_VERSION_ENV,
    ROLE_POLICY_DIGEST_ENV,
    ROLE_POLICY_VERSION_ENV,
    IdentityBootstrapConfig,
    build_bootstrap_service,
)
from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionPolicyV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette_import import LegacyImportVerificationPolicyRegistryV1
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ProfileRefV1,
    execution_profile_catalog_digest,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import (
    CancelRunPayloadV1,
    Problem,
    RunCommandAckV1,
    RunCommandV1,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    EvidenceSet,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.identity.bootstrap import BootstrapAdminRequest
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.cassette.legacy_authority_manifest import (
    LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
)
from gameforge.runtime.cassette.legacy_import import InMemoryLegacyImportAuthority
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import AuditRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.secrets.session_keys import (
    SESSION_SIGNING_KEY_SETS_ENV,
    SessionSigningKeyProvider,
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


def _domain_and_role_policy() -> tuple[DomainRegistryV1, RolePolicy]:
    definitions = (
        DomainDefinitionV1(
            domain_id="game-content",
            display_name="Game Content",
            tags=(),
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="builtin",
            display_name="Built-in Platform Profiles",
            tags=(),
            status="active",
        ),
    )
    registry = DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "identity_admin": (
            Permission(
                action="identity.manage",
                resource_kind="identity",
                domain_scope=None,
            ),
            Permission(action="read", resource_kind="metric", domain_scope=None),
        ),
        "tooling": (
            Permission(
                action="run",
                resource_kind="tooling",
                domain_scope="all",
            ),
            Permission(action="run", resource_kind="checker", domain_scope="all"),
            Permission(action="read", resource_kind="spec", domain_scope="all"),
            Permission(action="read", resource_kind="artifact", domain_scope="all"),
            Permission(action="read", resource_kind="run", domain_scope="all"),
            Permission(action="read", resource_kind="approval", domain_scope="all"),
            Permission(
                action="read",
                resource_kind="execution_profile",
                domain_scope="all",
            ),
        ),
    }
    effective_from = "2026-07-14T00:00:00Z"
    policy = RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "roles@1",
            registry_ref,
            grants,
            effective_from,
        ),
    )
    return registry, policy


def _signing_keys() -> SessionSigningKeyProvider:
    return SessionSigningKeyProvider(
        (
            SessionSigningKeySet(
                key_set_version="keys@1",
                keys=(
                    SessionSigningKey(
                        key_id="session-key-1",
                        secret=b"s" * 32,
                        status="active",
                    ),
                ),
            ),
        )
    )


def _config(tmp_path, database_url: str) -> LocalApiConfig:
    _, role_policy = _domain_and_role_policy()
    return LocalApiConfig(
        database_url=database_url,
        object_store_root=tmp_path / "objects",
        object_store_id="local:test",
        telemetry_db_path=tmp_path / "telemetry.sqlite3",
        current_password_hash_policy_version="argon2id@1",
        session_policy_version="session@1",
        role_policy_version=role_policy.policy_version,
        role_policy_digest=role_policy.policy_digest,
        audit_chain_id="identity",
        root_secret=b"r" * 32,
        session_signing_keys=_signing_keys(),
        allowed_websocket_origins=frozenset({"https://console.gameforge.test"}),
    )


def _legacy_import_authority() -> InMemoryLegacyImportAuthority:
    return InMemoryLegacyImportAuthority(
        verification_policy_registry=LegacyImportVerificationPolicyRegistryV1.create(
            registry_version=1,
            policies=(),
        ),
        model_catalogs={},
        input_bindings={},
        profile_bindings={},
        policy_bindings={},
        schema_bindings={},
        rendered_requests={},
        frozen_version_tuples={},
        call_tool_versions={},
    )


def _seed_and_bootstrap(database_url: str) -> str:
    migrations_api.upgrade(database_url)
    engine = get_engine(database_url)
    normalization = _normalization_policy()
    password = _password_policy()
    session_policy = _session_policy()
    domain_registry, role_policy = _domain_and_role_policy()
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=SystemUtcClock())
        policies.put_login_name_normalization_policy(normalization)
        policies.put_password_hash_policy(password)
        policies.put_session_policy(session_policy)
        policies.put_domain_registry(domain_registry)
        policies.put_role_policy(role_policy)
    engine.dispose()

    service = build_bootstrap_service(
        IdentityBootstrapConfig(
            database_url=database_url,
            login_normalization_policy_version=normalization.policy_version,
            login_normalization_policy_digest=normalization.policy_digest,
            password_hash_policy_version=password.policy_version,
            role_policy_version=role_policy.policy_version,
            role_policy_digest=role_policy.policy_digest,
            audit_chain_id="identity",
        )
    )
    result = service.bootstrap(
        BootstrapAdminRequest(
            display_name="Local Admin",
            login_name="admin",
            password=SecretText("correct-password"),
        )
    )
    return result.principal_id


def _seed_local_command_inputs(app, *, principal_id: str) -> str:
    resources = app.state.local_resources
    clock = SystemUtcClock()
    snapshot_id = "snapshot:local-command-composition"
    payload = canonical_json({"snapshot_id": snapshot_id, "entities": [], "relations": []}).encode(
        "utf-8"
    )
    stored = resources.object_store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot_id,
            tool_version="local-command-composition@1",
        ),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "ir-core@1",
            "domain_scope": {"domain_ids": ["builtin"]},
        },
    )
    with Session(resources.engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session,
            resources.object_store,
            "local:test",
        )
        bindings.bind_verified(stored.ref, stored.location, None)
        SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(
                signing_key=b"local-command-composition-cursor",
                clock=clock,
            ),
            clock=clock,
        ).put(artifact)
        costs = SqlCostLedger(session, clock=clock)
        for budget_id, scope_kind, scope_id in (
            (f"budget:principal:{principal_id}", "principal", principal_id),
            ("budget:system:global", "system", "global"),
        ):
            costs.put_budget(
                BudgetV1(
                    budget_id=budget_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    policy_version="local-command-composition-budget@1",
                    limits=(
                        CostAmountV1(
                            dimension="request",
                            value=10_000_000,
                            unit="request",
                        ),
                        CostAmountV1(
                            dimension="concurrent_run",
                            value=16,
                            unit="count",
                        ),
                    ),
                    reserved=(),
                    consumed=(),
                    status="active",
                    revision=1,
                    created_at=clock.now_utc(),
                )
            )
    return artifact.artifact_id


def _seed_validation_evidence(app) -> tuple[str, EvidenceSet]:
    resources = app.state.local_resources
    subject_payload = canonical_json({"patch_schema_version": "patch@2"}).encode("utf-8")
    stored_subject = resources.object_store.put_verified(subject_payload)
    subject = build_artifact_v2(
        kind="patch",
        version_tuple=VersionTuple(tool_version="local-api-evidence-test@1"),
        lineage=(),
        payload_hash=stored_subject.ref.sha256,
        object_ref=stored_subject.ref,
    )
    domain_registry, role_policy = _domain_and_role_policy()
    domain_ref = DomainRegistryRefV1(
        registry_version=domain_registry.registry_version,
        registry_digest=domain_registry.registry_digest,
    )
    target = PatchTargetBindingV1(
        target_artifact_id="artifact:preview",
        target_snapshot_id="snapshot:preview",
        target_digest="8" * 64,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id="artifact:base", revision=1),
    )
    draft = ApprovalItem(
        approval_id="approval:local-evidence",
        subject_series_id="series:local-evidence",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=AuditActor(principal_id="human:admin", principal_kind="human"),
        domain_scope=DomainScope(domain_ids=("game-content",)),
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="5" * 64,
            domain_registry_ref=domain_ref,
        ),
        role_policy_version=role_policy.policy_version,
        role_policy_digest=role_policy.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval-policy@1",
            policy_digest="7" * 64,
        ),
        requirements=(),
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=target,
        created_at="2026-07-14T12:00:00Z",
    )
    evidence = EvidenceSet(
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        policy_version="validation@1",
        validation_run_id="run:validation:local-evidence",
        target_binding=target,
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(),
        overall_status="passed",
    )
    evidence_payload = canonical_json(evidence.model_dump(mode="json")).encode("utf-8")
    stored_evidence = resources.object_store.put_verified(evidence_payload)
    evidence_artifact = build_artifact_v2(
        kind="validation_evidence",
        version_tuple=VersionTuple(
            producer_run_id=evidence.validation_run_id,
            tool_version="local-api-evidence-test@1",
        ),
        lineage=(subject.artifact_id,),
        payload_hash=stored_evidence.ref.sha256,
        object_ref=stored_evidence.ref,
    )

    clock = SystemUtcClock()
    with Session(resources.engine) as session, session.begin():
        object_bindings = SqlObjectBindingRepository(
            session,
            resources.object_store,
            stored_subject.location.store_id,
        )
        for stored in (stored_subject, stored_evidence):
            object_bindings.bind_verified(stored.ref, stored.location, None)
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=object_bindings,
            cursor_signer=CursorSigner(signing_key=b"l" * 32, clock=clock),
            clock=clock,
        )
        artifacts.put(subject)
        artifacts.put(evidence_artifact)
        approvals = SqlApprovalRepository(session)
        approvals.insert_draft(draft)
        approvals.compare_and_set_subject_head(
            draft.subject_series_id,
            None,
            SubjectHead(
                subject_series_id=draft.subject_series_id,
                current_subject_artifact_id=draft.subject_artifact_id,
                current_approval_id=draft.approval_id,
                revision=1,
            ),
        )
        validating = ApprovalItem.model_validate(
            {
                **draft.model_dump(mode="json"),
                "status": "validating",
                "workflow_revision": 2,
                "active_validation_run_id": evidence.validation_run_id,
            }
        )
        approvals.compare_and_set(draft.approval_id, draft.workflow_revision, validating)
        validated = ApprovalItem.model_validate(
            {
                **validating.model_dump(mode="json"),
                "status": "validated",
                "workflow_revision": 3,
                "active_validation_run_id": None,
                "evidence_set_artifact_id": evidence_artifact.artifact_id,
            }
        )
        approvals.compare_and_set_validation_completion(
            validating.approval_id,
            validating.workflow_revision,
            validated,
        )
    return evidence_artifact.artifact_id, evidence


def test_environment_configuration_requires_stable_secret_without_leaking_it(tmp_path) -> None:
    secret = b"z" * 32
    encoded = base64.b64encode(secret).decode("ascii")
    signing = json.dumps(
        [
            {
                "key_set_version": "keys@1",
                "keys": [
                    {
                        "key_id": "session-key-1",
                        "secret_base64": base64.b64encode(b"s" * 32).decode("ascii"),
                        "status": "active",
                    }
                ],
            }
        ]
    )
    config = LocalApiConfig.from_environment(
        {
            DATABASE_URL_ENV: f"sqlite:///{tmp_path / 'configured.db'}",
            PASSWORD_HASH_POLICY_VERSION_ENV: "argon2id@1",
            SESSION_POLICY_VERSION_ENV: "session@1",
            ROLE_POLICY_VERSION_ENV: "roles@1",
            ROLE_POLICY_DIGEST_ENV: _domain_and_role_policy()[1].policy_digest,
            LOCAL_ROOT_SECRET_ENV: encoded,
            SESSION_SIGNING_KEY_SETS_ENV: signing,
        }
    )

    rendered = repr(config)
    assert config.root_secret == secret
    assert config.selected_bench_report_artifact_id is None
    assert encoded not in rendered
    assert secret.hex() not in rendered

    with pytest.raises(LocalApiConfigurationError, match=LOCAL_ROOT_SECRET_ENV):
        LocalApiConfig.from_environment(
            {
                PASSWORD_HASH_POLICY_VERSION_ENV: "argon2id@1",
                SESSION_POLICY_VERSION_ENV: "session@1",
                ROLE_POLICY_VERSION_ENV: "roles@1",
                ROLE_POLICY_DIGEST_ENV: _domain_and_role_policy()[1].policy_digest,
                LOCAL_ROOT_SECRET_ENV: "not-base64",
                SESSION_SIGNING_KEY_SETS_ENV: signing,
            }
        )


def test_environment_configuration_freezes_selected_bench_report_artifact_id(tmp_path) -> None:
    secret = base64.b64encode(b"z" * 32).decode("ascii")
    signing = json.dumps(
        [
            {
                "key_set_version": "keys@1",
                "keys": [
                    {
                        "key_id": "session-key-1",
                        "secret_base64": base64.b64encode(b"s" * 32).decode("ascii"),
                        "status": "active",
                    }
                ],
            }
        ]
    )
    source = {
        DATABASE_URL_ENV: f"sqlite:///{tmp_path / 'configured.db'}",
        PASSWORD_HASH_POLICY_VERSION_ENV: "argon2id@1",
        SESSION_POLICY_VERSION_ENV: "session@1",
        ROLE_POLICY_VERSION_ENV: "roles@1",
        ROLE_POLICY_DIGEST_ENV: _domain_and_role_policy()[1].policy_digest,
        LOCAL_ROOT_SECRET_ENV: secret,
        SESSION_SIGNING_KEY_SETS_ENV: signing,
        SELECTED_BENCH_REPORT_ARTIFACT_ID_ENV: "artifact:bench:pinned",
    }

    config = LocalApiConfig.from_environment(source)

    assert config.selected_bench_report_artifact_id == "artifact:bench:pinned"
    with pytest.raises(LocalApiConfigurationError, match="selected_bench_report_artifact_id"):
        LocalApiConfig.from_environment({**source, SELECTED_BENCH_REPORT_ARTIFACT_ID_ENV: ""})


def test_real_local_composition_runs_login_me_logout_and_honest_readiness(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'gameforge.db'}"
    _seed_and_bootstrap(database_url)
    config = _config(tmp_path, database_url)
    app = create_local_app(config=config)

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        me = client.get("/api/v1/auth/me")
        readiness = client.get("/readyz")
        logout = client.post(
            "/api/v1/auth/logout",
            headers={
                "Idempotency-Key": "logout:local-admin:1",
                "X-CSRF-Token": login.headers["X-CSRF-Token"],
            },
        )
        after_logout = client.get("/api/v1/auth/me")

    assert login.status_code == 204
    assert me.status_code == 200
    assert me.json()["display_name"] == "Local Admin"
    assert logout.status_code == 204
    assert after_logout.status_code == 401
    assert readiness.status_code == 503
    readiness_problem = Problem.model_validate(readiness.json())
    assert readiness_problem.code == "dependency_unavailable"
    assert readiness_problem.errors == ({"component": "registry"},)

    engine = get_engine(database_url)
    with Session(engine) as session:
        assert SqlAuditSink(session).verify_chain("identity") is True
    engine.dispose()


def test_real_local_command_ports_cancel_queued_run_and_replay_after_restart(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'local-run-command.db'}"
    principal_id = _seed_and_bootstrap(database_url)
    config = _config(tmp_path, database_url)
    app = create_local_app(config=config)
    snapshot_artifact_id = _seed_local_command_inputs(app, principal_id=principal_id)

    dependencies = app.state.local_resources.dependencies
    assert dependencies.run_command_service is not None
    assert dependencies.run_command_authorizer is not None

    run_body = {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "checker-run@1",
            "snapshot_artifact_id": snapshot_artifact_id,
            "constraint_snapshot_artifact_id": None,
            "selection": {
                "mode": "full",
                "entity_ids": [],
                "relation_ids": [],
            },
            "checker_profile": ProfileRefV1(
                profile_id="builtin.checker",
                version=1,
            ).model_dump(mode="json"),
            "checker_ids": [],
            "defect_classes": [],
        },
        "llm_execution_mode": "not_applicable",
        "seed": None,
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }
    command = RunCommandV1(
        command_id="command:local-cancel",
        client_id="browser:local",
        client_seq=1,
        idempotency_key="local-cancel:1",
        expected_run_revision=1,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )

    terminal_sql: list[str] = []

    def capture_terminal_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        terminal_sql.append(statement)

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        csrf = login.headers["X-CSRF-Token"]
        admitted = client.post(
            "/api/v1/runs",
            json=run_body,
            headers={"Idempotency-Key": "local-checker:1", "X-CSRF-Token": csrf},
        )
        assert admitted.status_code == 202, admitted.text
        run_id = admitted.json()["run_id"]
        event.listen(
            app.state.local_resources.engine,
            "before_cursor_execute",
            capture_terminal_statement,
        )
        try:
            cancelled = client.post(
                f"/api/v1/runs/{run_id}:cancel",
                json=command.model_dump(mode="json"),
                headers={"X-CSRF-Token": csrf},
            )
        finally:
            event.remove(
                app.state.local_resources.engine,
                "before_cursor_execute",
                capture_terminal_statement,
            )

    assert cancelled.status_code == 200, cancelled.text
    request_id = cancelled.headers["X-Request-ID"]
    first_ack = RunCommandAckV1.model_validate(cancelled.json())
    assert first_ack.status == "accepted"
    assert first_ack.persisted_status == "applied"
    operations = tuple(statement.lstrip().split(None, 1)[0].upper() for statement in terminal_sql)
    first_dml = next(
        index
        for index, operation in enumerate(operations)
        if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    )
    audit_reads = tuple(
        index
        for index, (operation, statement) in enumerate(zip(operations, terminal_sql, strict=True))
        if operation in {"SELECT", "WITH"}
        and ("audit_heads" in statement.lower() or "from audit" in statement.lower())
    )
    assert len(audit_reads) == 2
    assert max(audit_reads) < first_dml
    assert not any(operation in {"SELECT", "WITH"} for operation in operations[first_dml + 1 :])

    restarted = create_local_app(config=config)
    restarted_dependencies = restarted.state.local_resources.dependencies
    assert restarted_dependencies.run_command_service is not None
    assert restarted_dependencies.run_command_authorizer is not None
    with TestClient(restarted, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        duplicate = client.post(
            f"/api/v1/runs/{run_id}:cancel",
            json=command.model_dump(mode="json"),
            headers={"X-CSRF-Token": login.headers["X-CSRF-Token"]},
        )

    assert duplicate.status_code == 200, duplicate.text
    duplicate_ack = RunCommandAckV1.model_validate(duplicate.json())
    assert duplicate_ack.status == "duplicate"
    assert duplicate_ack.persisted_status == first_ack.persisted_status
    assert duplicate_ack.command_revision == first_ack.command_revision
    assert duplicate_ack.run_revision == first_ack.run_revision

    engine = get_engine(database_url)
    with Session(engine) as session:
        runs = SqlRunRepository(session)
        run = runs.get(run_id)
        record = runs.get_command(run_id, command.command_id)
        events = runs.list_events(run_id, after_seq=0, limit=16)
        assert run is not None and run.status == "cancelled"
        assert record is not None and record.status == "applied"
        assert tuple(event.event_type for event in events) == (
            "run.queued",
            "run.cancel_requested",
            "run.cancelled",
        )
        terminal_audits = session.scalars(
            select(AuditRow)
            .where(
                AuditRow.action.in_(("run.failure", "run.command_submitted", "run.terminal")),
                AuditRow.chain_id == "identity",
            )
            .order_by(AuditRow.seq)
        ).all()
        assert tuple(row.action for row in terminal_audits) == (
            "run.failure",
            "run.command_submitted",
            "run.terminal",
        )
        assert tuple(
            row.correlation.get("request_id") if isinstance(row.correlation, dict) else None
            for row in terminal_audits
        ) == (request_id, request_id, request_id)
        assert SqlAuditSink(session).verify_chain("identity") is True
    engine.dispose()


def test_real_local_command_reauthorizes_inside_write_uow_after_outer_grant_revoked(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'local-command-auth-race.db'}"
    principal_id = _seed_and_bootstrap(database_url)
    config = _config(tmp_path, database_url)
    app = create_local_app(config=config)
    snapshot_artifact_id = _seed_local_command_inputs(app, principal_id=principal_id)
    dependencies = app.state.local_resources.dependencies
    outer = dependencies.run_command_authorizer
    assert outer is not None

    class _RevokeAfterOuterAuthorization:
        def authorize(self, *, run_id: str, actor) -> None:  # type: ignore[no-untyped-def]
            outer.authorize(run_id=run_id, actor=actor)
            with Session(app.state.local_resources.engine) as session, session.begin():
                identities = SqlIdentityRepository(session, clock=SystemUtcClock())
                principal = identities.get(principal_id)
                projected = identities.project(principal_id)
                assert principal is not None and projected is not None
                assert projected.roles
                for assignment in projected.roles:
                    current = identities.get(principal_id)
                    assert current is not None
                    identities.revoke(
                        assignment_id=assignment.assignment_id,
                        revoked_by=AuditActor(
                            principal_id=principal_id,
                            principal_kind="human",
                        ),
                        revoke_reason="task15_authority_race",
                        expected_principal_revision=current.revision,
                        expected_assignment_revision=assignment.revision,
                    )

    app.state.dependencies = replace(
        dependencies,
        run_command_authorizer=_RevokeAfterOuterAuthorization(),
    )
    run_body = {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "checker-run@1",
            "snapshot_artifact_id": snapshot_artifact_id,
            "constraint_snapshot_artifact_id": None,
            "selection": {"mode": "full", "entity_ids": [], "relation_ids": []},
            "checker_profile": {"profile_id": "builtin.checker", "version": 1},
            "checker_ids": [],
            "defect_classes": [],
        },
        "llm_execution_mode": "not_applicable",
        "seed": None,
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }
    command = RunCommandV1(
        command_id="command:revoked-between-guards",
        client_id="browser:local",
        client_seq=1,
        idempotency_key="command:revoked-between-guards",
        expected_run_revision=1,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        csrf = login.headers["X-CSRF-Token"]
        admitted = client.post(
            "/api/v1/runs",
            json=run_body,
            headers={"Idempotency-Key": "local-auth-race:1", "X-CSRF-Token": csrf},
        )
        assert admitted.status_code == 202, admitted.text
        run_id = admitted.json()["run_id"]
        with Session(app.state.local_resources.engine) as session:
            before_events = SqlRunRepository(session).list_events(
                run_id,
                after_seq=0,
                limit=16,
            )
        response = client.post(
            f"/api/v1/runs/{run_id}:cancel",
            json=command.model_dump(mode="json"),
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 403, response.text
    engine = get_engine(database_url)
    with Session(engine) as session:
        runs = SqlRunRepository(session)
        run = runs.get(run_id)
        assert run is not None and run.status == "queued" and run.revision == 1
        assert runs.get_command(run_id, command.command_id) is None
        assert runs.list_events(run_id, after_seq=0, limit=16) == before_events
        actions = session.scalars(
            select(AuditRow.action).where(
                AuditRow.chain_id == "identity",
                AuditRow.action.in_(("run.command_submitted", "run.terminal")),
            )
        ).all()
        assert actions == []
    engine.dispose()


def test_real_local_composition_mounts_request_scoped_bounded_reads(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'bounded-reads.db'}"
    _seed_and_bootstrap(database_url)
    app = create_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        first_specs = client.get("/api/v1/specs", params={"limit": 10})
        second_specs = client.get("/api/v1/specs", params={"limit": 10})
        runs = client.get("/api/v1/runs", params={"limit": 10})
        profiles = client.get("/api/v1/execution-profiles", params={"limit": 100})
        validation_profile = client.get("/api/v1/execution-profiles/builtin.validation/versions/1")
        missing_registry = client.get("/api/v1/schema-registry/not-published")
        missing_metrics = client.get("/api/v1/metrics/descriptors")

    assert login.status_code == 204
    assert first_specs.status_code == second_specs.status_code == 200
    assert first_specs.json()["items"] == second_specs.json()["items"] == []
    assert first_specs.json()["read_snapshot_id"] != second_specs.json()["read_snapshot_id"]
    assert runs.status_code == 200
    assert runs.json()["items"] == []
    assert profiles.status_code == 200
    assert len(profiles.json()["items"]) > 0
    assert validation_profile.status_code == 200
    assert validation_profile.json()["profile"]["profile_id"] == "builtin.validation"
    assert "catalog_version" not in validation_profile.json()

    for response, component in (
        (missing_registry, "content_producer_binding"),
        (missing_metrics, "metric_descriptor_registry"),
    ):
        assert response.status_code == 503
        problem = Problem.model_validate(response.json())
        assert problem.code == "dependency_unavailable"
        assert problem.errors == ({"component": component},)


def test_local_composition_retains_profile_history_and_reads_latest_catalog(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'profile-history.db'}"
    _seed_and_bootstrap(database_url)
    builtin_catalogs = build_builtin_registry().list_execution_profile_catalogs()
    base = builtin_catalogs[0]
    current = builtin_catalogs[-1]
    lifecycle = tuple(
        item.model_copy(
            update={
                "state": "replay_only",
                "revision": item.revision + 1,
                "reason_code": "superseded",
                "changed_at": "2026-07-16T00:00:00Z",
            }
        )
        if item.profile.profile_id == "builtin.validation"
        else item
        for item in current.lifecycle
    )
    payload = {
        "catalog_schema_version": current.catalog_schema_version,
        "catalog_version": current.catalog_version + 1,
        "definitions": current.definitions,
        "lifecycle": lifecycle,
    }
    latest = ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )
    engine = get_engine(database_url)
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=SystemUtcClock())
        policies.put_execution_profile_catalog(base)
        policies.put_execution_profile_catalog(latest)
    engine.dispose()

    app = create_local_app(config=_config(tmp_path, database_url))
    with TestClient(app, base_url="https://gameforge.test") as client:
        client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        profile = client.get("/api/v1/execution-profiles/builtin.validation/versions/1")

    assert profile.status_code == 200
    assert profile.json()["status"] == "replay_only"
    admission = app.state.local_resources.dependencies.run_admission
    assert tuple(
        item.catalog_version
        for item in admission._registry.list_execution_profile_catalogs()  # noqa: SLF001
    ) == tuple(
        sorted(
            {
                *(item.catalog_version for item in builtin_catalogs),
                latest.catalog_version,
            }
        )
    )


def test_fresh_local_composition_resolves_process_builtin_catalog_for_admission(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'builtin-profile-authority.db'}"
    _seed_and_bootstrap(database_url)

    app = create_local_app(config=_config(tmp_path, database_url))
    admission = app.state.local_resources.dependencies.run_admission
    catalog = admission._catalog  # noqa: SLF001
    with admission._read_scope() as read:  # noqa: SLF001
        binding = read.policies.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path="/params/validation_policy",
            profile=ProfileRefV1(profile_id="builtin.validation", version=1),
            expected_profile_kind="validation",
        )

    assert binding.catalog_version == catalog.catalog_version
    assert binding.catalog_digest == catalog.catalog_digest
    assert binding.profile == ProfileRefV1(profile_id="builtin.validation", version=1)


def test_local_composition_wires_trusted_legacy_import_authority(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-authority.db'}"
    _seed_and_bootstrap(database_url)
    authority = _legacy_import_authority()

    app = create_local_app(
        config=_config(tmp_path, database_url),
        legacy_import_authority=authority,
    )
    with TestClient(app, base_url="https://gameforge.test"):
        admission = app.state.local_resources.dependencies.run_admission
        assert admission._legacy_import_authority is authority  # noqa: SLF001


def test_uvicorn_factory_loads_shared_legacy_authority_from_environment(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-authority-environment.db'}"
    _seed_and_bootstrap(database_url)
    config = _config(tmp_path, database_url)
    manifest_path = tmp_path / "legacy-authority"
    authority = _legacy_import_authority()
    observed: list[object] = []

    def load(path: object) -> InMemoryLegacyImportAuthority:
        observed.append(path)
        return authority

    monkeypatch.setenv(
        LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
        str(manifest_path),
    )
    monkeypatch.setattr(local_module, "load_legacy_import_authority", load)
    monkeypatch.setattr(
        LocalApiConfig,
        "from_environment",
        classmethod(lambda cls, environment=None: config),
    )

    app = create_local_app()
    with TestClient(app, base_url="https://gameforge.test"):
        admission = app.state.local_resources.dependencies.run_admission
        assert admission._legacy_import_authority is authority  # noqa: SLF001

    assert observed == [str(manifest_path)]


def test_uvicorn_factory_rejects_explicit_and_environment_legacy_authorities(
    tmp_path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "must-not-be-loaded"
    monkeypatch.setenv(
        LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
        str(manifest_path),
    )
    monkeypatch.setattr(
        local_module,
        "load_legacy_import_authority",
        lambda path: pytest.fail(f"unexpected authority load: {path!r}"),
    )

    with pytest.raises(LocalApiConfigurationError, match="legacy import authority"):
        create_local_app(
            config=_config(tmp_path, f"sqlite:///{tmp_path / 'unused.db'}"),
            legacy_import_authority=_legacy_import_authority(),
        )


def test_uvicorn_factory_redacts_legacy_authority_load_failures(
    tmp_path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "private-customer-authority.json"
    sensitive_detail = "rendered prompt contains private customer text"
    monkeypatch.setenv(
        LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
        str(manifest_path),
    )

    def reject(path: object) -> object:
        assert path == str(manifest_path)
        raise IntegrityViolation(sensitive_detail)

    monkeypatch.setattr(local_module, "load_legacy_import_authority", reject)

    with pytest.raises(LocalApiConfigurationError) as raised:
        create_local_app(config=_config(tmp_path, f"sqlite:///{tmp_path / 'unused.db'}"))

    assert sensitive_detail not in str(raised.value)
    assert str(manifest_path) not in str(raised.value)


def test_direct_resource_composition_does_not_read_legacy_authority_environment(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'direct-legacy-authority-di.db'}"
    _seed_and_bootstrap(database_url)
    monkeypatch.setenv(
        LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
        str(tmp_path / "environment-authority"),
    )
    monkeypatch.setattr(
        local_module,
        "load_legacy_import_authority",
        lambda path: pytest.fail(f"unexpected authority load: {path!r}"),
    )
    authority = _legacy_import_authority()

    resources = build_local_api_resources(
        _config(tmp_path, database_url),
        legacy_import_authority=authority,
    )
    try:
        admission = resources.dependencies.run_admission
        assert admission._legacy_import_authority is authority  # noqa: SLF001
    finally:
        resources.close()


def test_real_local_api_reads_approval_bound_validation_evidence(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'validation-evidence.db'}"
    _seed_and_bootstrap(database_url)
    app = create_local_app(config=_config(tmp_path, database_url))
    evidence_artifact_id, evidence = _seed_validation_evidence(app)

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        response = client.get(f"/api/v1/artifacts/{evidence_artifact_id}")

    assert login.status_code == 204
    assert response.status_code == 200
    body = response.json()
    assert body["artifact"]["artifact_id"] == evidence_artifact_id
    assert body["artifact"]["kind"] == "validation_evidence"
    assert body["artifact"]["payload_schema_id"] == "evidence-set@1"
    assert body["artifact"]["domain_scope"] == {"domain_ids": ["game-content"]}
    assert body["payload"] == evidence.model_dump(mode="json", exclude_none=True)


def test_local_composition_never_auto_migrates_authoritative_database(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'unmigrated.db'}"
    app = create_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    problem = Problem.model_validate(response.json())
    assert problem.errors == ({"component": "migration_head"},)
    engine = get_engine(database_url)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()


def test_local_api_readyz_runs_persistent_auto_apply_history_closure(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'auto-apply-readyz.db'}"
    _seed_and_bootstrap(database_url)
    seen: list[object] = []

    def reject(registry, **resolvers) -> None:
        seen.append(registry)
        assert set(resolvers) == {
            "policy_registries",
            "domain_registries",
            "oracle_registries",
        }
        raise IntegrityViolation("retained auto-apply history is unavailable")

    monkeypatch.setattr(
        local_module,
        "ensure_worker_auto_apply_catalog_supported",
        reject,
    )
    app = create_readiness_closed_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    problem = Problem.model_validate(response.json())
    assert problem.errors == ({"component": "registry"},)
    assert len(seen) == 1


def test_local_api_rejects_readiness_only_playtest_validator_sentinels() -> None:
    with pytest.raises(LocalApiConfigurationError, match="executable components"):
        _typed_playtest_payload_validators({"generic_env_reset_payload@1": "sentinel"})
