"""Task 18 — constraint publication through the real public API and worker.

The agent entry uses a native RECORD run only to bootstrap retained replay
authority.  The product journey then starts from a distinct REPLAY-produced draft;
the RECORD proposal is deliberately never revised, validated, or published.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import socket

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ProfileRefV1,
    execution_profile_catalog_digest,
)
from gameforge.contracts.identity import (
    Permission,
    RolePolicy,
    compute_role_policy_digest,
)
from gameforge.contracts.workflow import ApprovalItem
from gameforge.platform.registry.constraint_compilers import (
    resolve_constraint_validation_compiler_authority,
)
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ApprovalItemRow,
    ArtifactRow,
    AuditRow,
    RefHistoryRow,
    RefRow,
    RunRow,
    SubjectHeadRow,
)
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository

from tests.e2e.m4c.test_agent_draft_terminal_audit import (
    _ConstraintProposalTransport,
    _execution_plan,
    _model_authorities,
    _seed_source,
)
from tests.e2e.m4c.test_journey_b import (
    APPROVER_LOGIN,
    APPROVER_PASSWORD,
    DOMAIN,
    MAKER_LOGIN,
    MAKER_PASSWORD,
    _Harness,
    _Session,
    _DOMAIN,
    _approval,
    _drive,
    _headers,
    _login,
    _run,
    _start_api,
    _stop_api,
)


_DOMAIN_JSON = {"domain_ids": [DOMAIN]}
_AGENT_REF = "constraint-agent-head"
_HUMAN_REF = "constraint-human-head"
_UNPROVEN_REF = "constraint-unproven-head"


@pytest.fixture(autouse=True)
def _deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic model transport and cassette replay must be the only I/O."""

    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("constraint publication journey attempted external network access")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)
    monkeypatch.setattr(socket.socket, "sendmsg", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _install_constraint_role_policy(harness: _Harness) -> RolePolicy:
    base = harness.role_policy
    shared = (
        Permission(action="read", resource_kind="constraint_proposal", domain_scope="all"),
        Permission(action="read", resource_kind="constraint_snapshot", domain_scope="all"),
        Permission(action="read", resource_kind="validation_evidence", domain_scope="all"),
    )
    grants = dict(base.grants)
    grants["content_designer"] = (
        *grants["content_designer"],
        Permission(action="propose", resource_kind="constraint_proposal", domain_scope=_DOMAIN),
        Permission(action="validate", resource_kind="constraint_proposal", domain_scope=_DOMAIN),
        Permission(action="replay", resource_kind="run", domain_scope=_DOMAIN),
        *shared,
    )
    grants["numeric_designer"] = (*grants["numeric_designer"], *shared)
    version = "constraint-publication-roles@1"
    policy = RolePolicy(
        policy_version=version,
        domain_registry_ref=base.domain_registry_ref,
        grants=grants,
        effective_from=base.effective_from,
        policy_digest=compute_role_policy_digest(
            version,
            base.domain_registry_ref,
            grants,
            base.effective_from,
        ),
    )
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            SqlPolicySnapshotRepository(session, clock=harness.clock).put_role_policy(policy)
    finally:
        engine.dispose()
    harness.role_policy = policy
    return policy


def _seed_model_authorities(harness: _Harness):
    authorities, catalog, routing = _model_authorities()
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            ledger = SqlCostLedger(session, clock=harness.clock)
            ledger.put_model_catalog(catalog)
            ledger.put_routing_policy(routing)
    finally:
        engine.dispose()
    return authorities, catalog, routing


def _install_drifted_constraint_compiler_catalog(
    harness: _Harness,
    update: dict[str, object],
):
    base = harness.catalog
    definition = next(
        item
        for item in base.definitions
        if item.profile.profile_id == "builtin.constraint_compiler"
    )
    lifecycle = next(item for item in base.lifecycle if item.profile == definition.profile)
    baseline_authority = resolve_constraint_validation_compiler_authority(
        definition,
        lifecycle,
    )
    drifted_profile = ProfileRefV1(
        profile_id=definition.profile.profile_id,
        version=definition.profile.version + 1,
    )
    drifted_definition = definition.model_copy(update={"profile": drifted_profile, **update})
    drifted_lifecycle = lifecycle.model_copy(update={"profile": drifted_profile})
    definitions = (*base.definitions, drifted_definition)
    lifecycles = (*base.lifecycle, drifted_lifecycle)
    payload = {
        "catalog_schema_version": "execution-profile-catalog@1",
        "catalog_version": base.catalog_version + 1_000,
        "definitions": [item.model_dump(mode="json") for item in definitions],
        "lifecycle": [item.model_dump(mode="json") for item in lifecycles],
    }
    catalog = ExecutionProfileCatalogSnapshotV1.model_validate(
        {
            **payload,
            "catalog_digest": execution_profile_catalog_digest(payload),
        }
    )
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            SqlPolicySnapshotRepository(
                session,
                clock=harness.clock,
            ).put_execution_profile_catalog(catalog)
    finally:
        engine.dispose()
    return drifted_profile, baseline_authority.differential_engines


def _worker_config(harness: _Harness):
    return replace(
        harness.worker_config(),
        role_policy_version=harness.role_policy.policy_version,
        role_policy_digest=harness.role_policy.policy_digest,
        workflow_route_policy_version=harness.route.route_version,
        workflow_route_policy_digest=harness.route.route_digest,
        workflow_approval_policy_version=harness.approval_policy.policy_version,
        workflow_approval_policy_digest=harness.approval_policy.policy_digest,
    )


def _constraint(constraint_id: str, assert_expr: str, *, oracle: str = "deterministic") -> dict:
    return {
        "id": constraint_id,
        "dsl_grammar_version": "dsl@1",
        "kind": "numeric",
        "oracle": oracle,
        "predicates": [],
        "scope": {"var": "q", "node_type": "QUEST", "where": {}},
        "assert": assert_expr,
        "severity": "major",
    }


def _draft_body(
    *,
    ref_name: str,
    constraint: dict,
    source_id: str,
    rationale: str,
) -> dict:
    return {
        "request_schema_version": "human-constraint-draft-request@1",
        "base_constraint_snapshot_artifact_id": None,
        "ref_name": ref_name,
        "expected_ref": None,
        "dsl_grammar_version": "dsl@1",
        "domain_scope": _DOMAIN_JSON,
        "constraints": [constraint],
        "source_artifact_ids": [source_id],
        "rationale": rationale,
    }


def _revision_body(
    *,
    prior: ApprovalItem,
    ref_name: str,
    constraint: dict,
    source_id: str,
    rationale: str,
) -> dict:
    return {
        **_draft_body(
            ref_name=ref_name,
            constraint=constraint,
            source_id=source_id,
            rationale=rationale,
        ),
        "request_schema_version": "human-constraint-revision-request@1",
        "approval_id": prior.approval_id,
        "expected_subject_head_revision": prior.subject_revision,
        "expected_workflow_revision": prior.workflow_revision,
    }


def _compiler_binding(reader: _Session) -> dict:
    response = reader.client.get(
        "/api/v1/execution-profiles/builtin.constraint_compiler/versions/1/"
        "constraint-validation-binding"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _validation_body(reader: _Session, item: ApprovalItem, *, ref_name: str) -> dict:
    compiler = _compiler_binding(reader)
    return {
        "request_schema_version": "constraint-validation-admission-request@1",
        "approval_id": item.approval_id,
        "expected_subject_head_revision": item.subject_revision,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "base_constraint_snapshot_artifact_id": None,
        "target": {"ref_name": ref_name, "expected_ref": None},
        "dsl_grammar_version": "dsl@1",
        "compiler_profile": compiler["compiler_profile"],
        "differential_engines": compiler["differential_engines"],
        "golden_suite_artifact_id": None,
        "regression_suite_artifact_ids": [],
        "validation_policy": {"profile_id": "builtin.validation", "version": 1},
        "seed": None,
    }


def _publish_body(item: ApprovalItem) -> dict:
    binding = item.target_binding
    assert binding is not None
    return {
        "request_schema_version": "workflow-apply-request@1",
        "approval_id": item.approval_id,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "target_artifact_id": binding.target_artifact_id,
        "target_digest": binding.target_digest,
        "ref_name": binding.ref_name,
        "expected_ref": (
            None if binding.expected_ref is None else binding.expected_ref.model_dump(mode="json")
        ),
    }


def _proposal_for_run(reader: _Session, run_id: str) -> dict:
    response = reader.client.get("/api/v1/constraint-proposals", params={"limit": 100})
    assert response.status_code == 200, response.text
    matches = [
        item for item in response.json()["items"] if item["proposal"]["producer_run_id"] == run_id
    ]
    assert len(matches) == 1
    return matches[0]


def _row_signature(row: object | None) -> tuple | None:
    if row is None:
        return None
    values = []
    for column in row.__table__.columns:  # type: ignore[attr-defined]
        value = getattr(row, column.name)
        if isinstance(value, (dict, list)):
            value = canonical_json(value)
        values.append((column.name, value))
    return tuple(values)


@dataclass(frozen=True)
class _AuthoritySnapshot:
    item: tuple | None
    head: tuple | None
    ref: tuple | None
    history: tuple[tuple, ...]
    row_counts: tuple[int, ...]


def _authority_snapshot(
    harness: _Harness,
    *,
    approval_id: str,
    ref_name: str,
) -> _AuthoritySnapshot:
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            item = session.get(ApprovalItemRow, approval_id)
            head = None if item is None else session.get(SubjectHeadRow, item.subject_series_id)
            ref = session.get(RefRow, ref_name)
            history = tuple(
                _row_signature(row)
                for row in session.scalars(
                    select(RefHistoryRow)
                    .where(RefHistoryRow.name == ref_name)
                    .order_by(RefHistoryRow.seq)
                )
            )
            counts = tuple(
                int(session.scalar(select(func.count()).select_from(model)) or 0)
                for model in (
                    ArtifactRow,
                    ApprovalItemRow,
                    SubjectHeadRow,
                    RunRow,
                    RefRow,
                    RefHistoryRow,
                    AuditRow,
                )
            )
            return _AuthoritySnapshot(
                item=_row_signature(item),
                head=_row_signature(head),
                ref=_row_signature(ref),
                history=history,  # type: ignore[arg-type]
                row_counts=counts,
            )
    finally:
        engine.dispose()


def _assert_rejected_without_authority_change(
    harness: _Harness,
    *,
    approval_id: str,
    ref_name: str,
    request,
    status_code: int,
) -> None:
    before = _authority_snapshot(harness, approval_id=approval_id, ref_name=ref_name)
    response = request()
    assert response.status_code == status_code, response.text
    assert _authority_snapshot(harness, approval_id=approval_id, ref_name=ref_name) == before


def _revise(
    maker: _Session,
    *,
    prior: ApprovalItem,
    ref_name: str,
    constraint: dict,
    source_id: str,
    key: str,
) -> ApprovalItem:
    response = maker.client.post(
        f"/api/v1/constraint-proposals/{prior.subject_artifact_id}:revise",
        json=_revision_body(
            prior=prior,
            ref_name=ref_name,
            constraint=constraint,
            source_id=source_id,
            rationale=f"Human-owned revision for {ref_name}.",
        ),
        headers=_headers(
            maker,
            idempotency_key=f"{key}:revise",
            resource_kind="constraint_proposal",
            resource_id=prior.subject_artifact_id,
            revision=prior.workflow_revision,
        ),
    )
    assert response.status_code == 201, response.text
    artifact_id = response.json()["artifact"]["artifact_id"]
    item = _approval(maker, f"approval:constraint_proposal:{artifact_id}")
    assert item.subject_revision == 2
    assert item.proposer.principal_id == "human:maker"
    assert item.supersedes_approval_id == prior.approval_id
    return item


def _assert_exact_compile(
    reader: _Session,
    *,
    item: ApprovalItem,
    expected_status: str,
) -> None:
    evidence_id = item.evidence_set_artifact_id
    assert evidence_id is not None
    evidence_response = reader.client.get(f"/api/v1/artifacts/{evidence_id}")
    assert evidence_response.status_code == 200, evidence_response.text
    evidence = evidence_response.json()["payload"]
    assert evidence["overall_status"] == expected_status
    compile_id = next(
        requirement["evidence_artifact_id"]
        for requirement in evidence["requirements"]
        if requirement["kind"] == "constraint_compile"
    )
    compile_response = reader.client.get(f"/api/v1/artifacts/{compile_id}")
    assert compile_response.status_code == 200, compile_response.text
    compile_evidence = compile_response.json()["payload"]
    assert compile_evidence["proposal_artifact_id"] == item.subject_artifact_id
    assert compile_evidence["overall_status"] == expected_status
    differential = [
        stage for stage in compile_evidence["stages"] if stage["stage"] == "differential"
    ]
    compiler = _compiler_binding(reader)
    assert {(stage["engine_id"], stage["engine_version"]) for stage in differential} == {
        (ref["engine_id"], str(ref["version"])) for ref in compiler["differential_engines"]
    }


def _validate(
    harness: _Harness,
    process,
    maker: _Session,
    *,
    item: ApprovalItem,
    ref_name: str,
    key: str,
    expected_status: str,
) -> tuple[ApprovalItem, str]:
    response = maker.client.post(
        f"/api/v1/constraint-proposals/{item.subject_artifact_id}:validate",
        json=_validation_body(maker, item, ref_name=ref_name),
        headers=_headers(
            maker,
            idempotency_key=f"{key}:validate",
            resource_kind="constraint_proposal",
            resource_id=item.subject_artifact_id,
            revision=item.workflow_revision,
        ),
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert _approval(maker, item.approval_id).status == "validating"
    terminal = asyncio.run(_drive(process.dispatcher, maker, run_id))
    assert terminal.status == "succeeded"
    retained = _approval(maker, item.approval_id)
    assert retained.status == ("validated" if expected_status == "passed" else "validation_failed")
    _assert_exact_compile(maker, item=retained, expected_status=expected_status)
    if expected_status == "passed":
        binding = retained.target_binding
        assert binding is not None and binding.target_artifact_kind == "constraint_snapshot"
        candidate = maker.client.get(f"/api/v1/artifacts/{binding.target_artifact_id}")
        assert candidate.status_code == 200, candidate.text
        assert candidate.json()["artifact"]["payload_hash"] == binding.target_digest
        assert item.subject_artifact_id in candidate.json()["artifact"]["parent_artifact_ids"]
    return retained, run_id


def _submit(maker: _Session, item: ApprovalItem, *, key: str) -> ApprovalItem:
    response = maker.client.post(
        f"/api/v1/constraint-proposals/{item.subject_artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": item.approval_id,
            "expected_workflow_revision": item.workflow_revision,
        },
        headers=_headers(
            maker,
            idempotency_key=f"{key}:submit",
            resource_kind="constraint_proposal",
            resource_id=item.subject_artifact_id,
            revision=item.workflow_revision,
        ),
    )
    assert response.status_code == 200, response.text
    pending = _approval(maker, item.approval_id)
    assert pending.status == "pending_approval"
    return pending


def _approve(approver: _Session, item: ApprovalItem, *, key: str) -> ApprovalItem:
    response = approver.client.post(
        f"/api/v1/approvals/{item.approval_id}:approve",
        json={
            "request_schema_version": "approval-decision-request@1",
            "decision": "approve",
            "requirement_ids": [value.requirement_id for value in item.requirements],
            "expected_workflow_revision": item.workflow_revision,
            "reason_code": "independent_constraint_review_passed",
        },
        headers=_headers(
            approver,
            idempotency_key=f"{key}:approve",
            resource_kind="approval",
            resource_id=item.approval_id,
            revision=item.workflow_revision,
        ),
    )
    assert response.status_code == 200, response.text
    approved = _approval(approver, item.approval_id)
    assert approved.status == "approved"
    return approved


def _publish(approver: _Session, item: ApprovalItem, *, key: str) -> dict:
    response = approver.client.post(
        f"/api/v1/constraint-proposals/{item.subject_artifact_id}:publish",
        json=_publish_body(item),
        headers=_headers(
            approver,
            idempotency_key=f"{key}:publish",
            resource_kind="constraint_proposal",
            resource_id=item.subject_artifact_id,
            revision=item.workflow_revision,
        ),
    )
    assert response.status_code == 200, response.text
    assert _approval(approver, item.approval_id).status == "applied"
    assert response.json()["ref_value"]["artifact_id"] == item.target_binding.target_artifact_id
    return response.json()


def _assert_ref_and_lineage(reader: _Session, *, item: ApprovalItem, ref_name: str) -> None:
    history = reader.client.get(f"/api/v1/refs/{ref_name}/history", params={"limit": 100})
    assert history.status_code == 200, history.text
    assert [entry["value"]["artifact_id"] for entry in history.json()["items"]] == [
        item.target_binding.target_artifact_id
    ]
    lineage = reader.client.get(
        f"/api/v1/artifacts/{item.target_binding.target_artifact_id}/lineage",
        params={"limit": 100},
    )
    assert lineage.status_code == 200, lineage.text
    assert item.subject_artifact_id in {
        entry["artifact"]["artifact_id"] for entry in lineage.json()["items"]
    }


def _assert_audit_chains(harness: _Harness) -> None:
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            audit = SqlAuditSink(session)
            assert audit.verify_chain("identity") is True
            assert audit.verify_chain("runs") is True
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "update",
    [
        {"config_schema_id": "constraint_compiler-profile-config@999"},
        {"input_schema_ids": ("checker-run@1",)},
        {"output_schema_ids": ("checker-report@1",)},
        {"stochastic": True},
        {"required_capabilities": ("reasoning",)},
    ],
    ids=(
        "config-schema",
        "input-schemas",
        "output-schemas",
        "stochastic",
        "required-capabilities",
    ),
)
def test_constraint_compiler_adapter_drift_blocks_binding_read_and_admission_before_202(
    tmp_path: Path,
    update: dict[str, object],
) -> None:
    harness = _Harness(tmp_path)
    _install_constraint_role_policy(harness)
    source_id = _seed_source(harness)
    compiler_profile, differential_engines = _install_drifted_constraint_compiler_catalog(
        harness,
        update,
    )
    ref_name = "constraint-adapter-drift"
    api = _start_api(harness.api_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        binding = maker.client.get(
            "/api/v1/execution-profiles/builtin.constraint_compiler/versions/2/"
            "constraint-validation-binding"
        )
        assert binding.status_code == 409, binding.text

        draft = maker.client.post(
            "/api/v1/constraint-proposals",
            json=_draft_body(
                ref_name=ref_name,
                constraint=_constraint("c:adapter-drift", "reward_gold <= 100"),
                source_id=source_id,
                rationale="Create a subject for adapter-drift admission.",
            ),
            headers=_headers(maker, idempotency_key="adapter-drift:draft"),
        )
        assert draft.status_code == 201, draft.text
        draft_id = draft.json()["artifact"]["artifact_id"]
        v1 = _approval(maker, f"approval:constraint_proposal:{draft_id}")
        v2 = _revise(
            maker,
            prior=v1,
            ref_name=ref_name,
            constraint=_constraint("c:adapter-drift", "reward_gold <= 90"),
            source_id=source_id,
            key="adapter-drift",
        )
        request_body = {
            "request_schema_version": "constraint-validation-admission-request@1",
            "approval_id": v2.approval_id,
            "expected_subject_head_revision": v2.subject_revision,
            "expected_workflow_revision": v2.workflow_revision,
            "subject_digest": v2.subject_digest,
            "base_constraint_snapshot_artifact_id": None,
            "target": {"ref_name": ref_name, "expected_ref": None},
            "dsl_grammar_version": "dsl@1",
            "compiler_profile": compiler_profile.model_dump(mode="json"),
            "differential_engines": [item.model_dump(mode="json") for item in differential_engines],
            "golden_suite_artifact_id": None,
            "regression_suite_artifact_ids": [],
            "validation_policy": {"profile_id": "builtin.validation", "version": 1},
            "seed": 1 if update.get("stochastic") is True else None,
        }
        _assert_rejected_without_authority_change(
            harness,
            approval_id=v2.approval_id,
            ref_name=ref_name,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{v2.subject_artifact_id}:validate",
                json=request_body,
                headers=_headers(
                    maker,
                    idempotency_key="adapter-drift:validate",
                    resource_kind="constraint_proposal",
                    resource_id=v2.subject_artifact_id,
                    revision=v2.workflow_revision,
                ),
            ),
        )
    finally:
        _stop_api(api)


def test_constraint_publication_agent_replay_and_human_entry_paths(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    _install_constraint_role_policy(harness)
    source_id = _seed_source(harness)
    authorities, catalog, routing = _seed_model_authorities(harness)
    plan = _execution_plan(catalog, routing)
    config = _worker_config(harness)

    api = _start_api(harness.api_config())
    process = build_worker_process(config, model_execution_authorities=authorities)
    try:
        maker_before_restart = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        record_body = {
            "request_schema_version": "constraint-propose-request@1",
            "source_artifact_ids": [source_id],
            "base_constraint_snapshot_artifact_id": None,
            "authoring_goal_text": "Extract a deterministic gold reward cap.",
            "domain_scope": _DOMAIN_JSON,
            "dsl_grammar_version": "dsl@1",
            "extraction_policy": {
                "profile_id": "builtin.constraint_extraction",
                "version": 1,
            },
            "llm_execution_mode": "record",
            "execution_version_plan": plan.model_dump(mode="json"),
            "cassette_artifact_id": None,
        }
        record = maker_before_restart.client.post(
            "/api/v1/constraint-proposals:propose",
            json=record_body,
            headers=_headers(maker_before_restart, idempotency_key="constraint:record-bootstrap"),
        )
        assert record.status_code == 202, record.text
        record_run_id = record.json()["run_id"]
        record_terminal = asyncio.run(
            _drive(process.dispatcher, maker_before_restart, record_run_id)
        )
        assert record_terminal.status == "succeeded"
        assert record_terminal.terminal_cassette_artifact_id is not None
        record_proposal = _proposal_for_run(maker_before_restart, record_run_id)
        record_proposal_id = record_proposal["artifact"]["artifact_id"]
        record_approval_id = f"approval:constraint_proposal:{record_proposal_id}"

        replay_body = {
            **record_body,
            "llm_execution_mode": "replay",
            "cassette_artifact_id": record_terminal.terminal_cassette_artifact_id,
        }
        replay = maker_before_restart.client.post(
            "/api/v1/constraint-proposals:propose",
            json=replay_body,
            headers=_headers(maker_before_restart, idempotency_key="constraint:replay-entry"),
        )
        assert replay.status_code == 202, replay.text
        replay_run_id = replay.json()["run_id"]
        replay_terminal = asyncio.run(
            _drive(process.dispatcher, maker_before_restart, replay_run_id)
        )
        assert replay_terminal.status == "succeeded"
        replay_proposal = _proposal_for_run(maker_before_restart, replay_run_id)
        replay_proposal_id = replay_proposal["artifact"]["artifact_id"]
        replay_approval_id = f"approval:constraint_proposal:{replay_proposal_id}"

        # producer_run_id is immutable proposal content.  RECORD and REPLAY must
        # therefore create distinct workflow subjects; only REPLAY continues below.
        assert replay_run_id != record_run_id
        assert replay_proposal_id != record_proposal_id
        assert replay_approval_id != record_approval_id
        assert replay_proposal["proposal"]["producer_run_id"] == replay_run_id
        assert record_proposal["proposal"]["producer_run_id"] == record_run_id
        assert isinstance(authorities.transport, _ConstraintProposalTransport)
        assert authorities.transport.calls == 1
    finally:
        process.close()
        _stop_api(api)

    # Rebuild both long-lived entries over the same DB/ObjectStore.  The sessions
    # below are fresh and independent; retained REPLAY workflow authority survives.
    api = _start_api(harness.api_config())
    process = build_worker_process(config)
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)
        assert maker.client is not approver.client

        replay_v1 = _approval(maker, replay_approval_id)
        assert replay_v1.status == "draft"
        assert replay_v1.proposer.principal_id == "human:maker"
        _assert_rejected_without_authority_change(
            harness,
            approval_id=replay_v1.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{replay_v1.subject_artifact_id}:validate",
                json=_validation_body(maker, replay_v1, ref_name=_AGENT_REF),
                headers=_headers(
                    maker,
                    idempotency_key="agent:missing-human-revision",
                    resource_kind="constraint_proposal",
                    resource_id=replay_v1.subject_artifact_id,
                    revision=replay_v1.workflow_revision,
                ),
            ),
        )
        agent_v2 = _revise(
            maker,
            prior=replay_v1,
            ref_name=_AGENT_REF,
            constraint=_constraint("c:agent-cap", "reward_gold <= 75"),
            source_id=source_id,
            key="agent",
        )

        stale_validation = _validation_body(maker, agent_v2, ref_name=_AGENT_REF)
        stale_validation["expected_workflow_revision"] = 99
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_v2.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{agent_v2.subject_artifact_id}:validate",
                json=stale_validation,
                headers=_headers(
                    maker,
                    idempotency_key="agent:stale-validation",
                    resource_kind="constraint_proposal",
                    resource_id=agent_v2.subject_artifact_id,
                    revision=99,
                ),
            ),
        )
        tampered_compiler_binding = _validation_body(
            maker,
            agent_v2,
            ref_name=_AGENT_REF,
        )
        tampered_compiler_binding["differential_engines"] = tampered_compiler_binding[
            "differential_engines"
        ][:-1]
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_v2.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{agent_v2.subject_artifact_id}:validate",
                json=tampered_compiler_binding,
                headers=_headers(
                    maker,
                    idempotency_key="agent:tampered-compiler-binding",
                    resource_kind="constraint_proposal",
                    resource_id=agent_v2.subject_artifact_id,
                    revision=agent_v2.workflow_revision,
                ),
            ),
        )
        agent_validated, _ = _validate(
            harness,
            process,
            maker,
            item=agent_v2,
            ref_name=_AGENT_REF,
            key="agent",
            expected_status="passed",
        )

        # Constraint proposals have no qualified deterministic auto-apply path.
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_validated.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{agent_validated.subject_artifact_id}:publish",
                json=_publish_body(agent_validated),
                headers=_headers(
                    maker,
                    idempotency_key="agent:auto-apply-bypass",
                    resource_kind="constraint_proposal",
                    resource_id=agent_validated.subject_artifact_id,
                    revision=agent_validated.workflow_revision,
                ),
            ),
        )
        agent_pending = _submit(maker, agent_validated, key="agent")
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_pending.approval_id,
            ref_name=_AGENT_REF,
            status_code=403,
            request=lambda: maker.client.post(
                f"/api/v1/approvals/{agent_pending.approval_id}:approve",
                json={
                    "request_schema_version": "approval-decision-request@1",
                    "decision": "approve",
                    "requirement_ids": [
                        requirement.requirement_id for requirement in agent_pending.requirements
                    ],
                    "expected_workflow_revision": agent_pending.workflow_revision,
                    "reason_code": "self_approval_forbidden",
                },
                headers=_headers(
                    maker,
                    idempotency_key="agent:maker-self-approval",
                    resource_kind="approval",
                    resource_id=agent_pending.approval_id,
                    revision=agent_pending.workflow_revision,
                ),
            ),
        )
        agent_approved = _approve(approver, agent_pending, key="agent")

        stale_publish = _publish_body(agent_approved)
        stale_publish["expected_workflow_revision"] -= 1
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_approved.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: approver.client.post(
                f"/api/v1/constraint-proposals/{agent_approved.subject_artifact_id}:publish",
                json=stale_publish,
                headers=_headers(
                    approver,
                    idempotency_key="agent:stale-publish",
                    resource_kind="constraint_proposal",
                    resource_id=agent_approved.subject_artifact_id,
                    revision=stale_publish["expected_workflow_revision"],
                ),
            ),
        )
        digest_mismatch = _publish_body(agent_approved)
        digest_mismatch["target_digest"] = "0" * 64
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_approved.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: approver.client.post(
                f"/api/v1/constraint-proposals/{agent_approved.subject_artifact_id}:publish",
                json=digest_mismatch,
                headers=_headers(
                    approver,
                    idempotency_key="agent:digest-mismatch",
                    resource_kind="constraint_proposal",
                    resource_id=agent_approved.subject_artifact_id,
                    revision=agent_approved.workflow_revision,
                ),
            ),
        )
        ref_mismatch = _publish_body(agent_approved)
        ref_mismatch["expected_ref"] = {
            "artifact_id": agent_approved.target_binding.target_artifact_id,
            "revision": 999,
        }
        _assert_rejected_without_authority_change(
            harness,
            approval_id=agent_approved.approval_id,
            ref_name=_AGENT_REF,
            status_code=409,
            request=lambda: approver.client.post(
                f"/api/v1/constraint-proposals/{agent_approved.subject_artifact_id}:publish",
                json=ref_mismatch,
                headers=_headers(
                    approver,
                    idempotency_key="agent:ref-mismatch",
                    resource_kind="constraint_proposal",
                    resource_id=agent_approved.subject_artifact_id,
                    revision=agent_approved.workflow_revision,
                ),
            ),
        )
        _publish(approver, agent_approved, key="agent")
        _assert_ref_and_lineage(maker, item=agent_approved, ref_name=_AGENT_REF)

        # The human-typed entry is a separate series and repeats the entire guarded
        # revision/validation/approval/ref-CAS path.
        human_draft = maker.client.post(
            "/api/v1/constraint-proposals",
            json=_draft_body(
                ref_name=_HUMAN_REF,
                constraint=_constraint("c:human-cap", "reward_gold <= 100"),
                source_id=source_id,
                rationale="Initial human-typed proposal.",
            ),
            headers=_headers(maker, idempotency_key="human:draft"),
        )
        assert human_draft.status_code == 201, human_draft.text
        human_v1_id = human_draft.json()["artifact"]["artifact_id"]
        human_v1 = _approval(maker, f"approval:constraint_proposal:{human_v1_id}")
        assert human_v1.subject_series_id != replay_v1.subject_series_id
        _assert_rejected_without_authority_change(
            harness,
            approval_id=human_v1.approval_id,
            ref_name=_HUMAN_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{human_v1.subject_artifact_id}:validate",
                json=_validation_body(maker, human_v1, ref_name=_HUMAN_REF),
                headers=_headers(
                    maker,
                    idempotency_key="human:missing-human-revision",
                    resource_kind="constraint_proposal",
                    resource_id=human_v1.subject_artifact_id,
                    revision=human_v1.workflow_revision,
                ),
            ),
        )
        human_v2 = _revise(
            maker,
            prior=human_v1,
            ref_name=_HUMAN_REF,
            constraint=_constraint("c:human-cap", "reward_gold <= 90"),
            source_id=source_id,
            key="human",
        )
        human_validated, _ = _validate(
            harness,
            process,
            maker,
            item=human_v2,
            ref_name=_HUMAN_REF,
            key="human",
            expected_status="passed",
        )
        human_approved = _approve(
            approver,
            _submit(maker, human_validated, key="human"),
            key="human",
        )
        _publish(approver, human_approved, key="human")
        _assert_ref_and_lineage(maker, item=human_approved, ref_name=_HUMAN_REF)

        # An LLM-assisted predicate is honestly unproven by the deterministic
        # compiler.  Its evidence may persist, but submit/publish cannot change the
        # workflow or create the target ref/history.
        unproven_draft = maker.client.post(
            "/api/v1/constraint-proposals",
            json=_draft_body(
                ref_name=_UNPROVEN_REF,
                constraint=_constraint(
                    "c:unproven-cap", "reward_gold <= 60", oracle="llm-assisted"
                ),
                source_id=source_id,
                rationale="Exercise an explicitly unproven oracle.",
            ),
            headers=_headers(maker, idempotency_key="unproven:draft"),
        )
        assert unproven_draft.status_code == 201, unproven_draft.text
        unproven_v1_id = unproven_draft.json()["artifact"]["artifact_id"]
        unproven_v1 = _approval(maker, f"approval:constraint_proposal:{unproven_v1_id}")
        unproven_v2 = _revise(
            maker,
            prior=unproven_v1,
            ref_name=_UNPROVEN_REF,
            constraint=_constraint("c:unproven-cap", "reward_gold <= 55", oracle="llm-assisted"),
            source_id=source_id,
            key="unproven",
        )
        unproven_failed, _ = _validate(
            harness,
            process,
            maker,
            item=unproven_v2,
            ref_name=_UNPROVEN_REF,
            key="unproven",
            expected_status="unproven",
        )
        _assert_rejected_without_authority_change(
            harness,
            approval_id=unproven_failed.approval_id,
            ref_name=_UNPROVEN_REF,
            status_code=409,
            request=lambda: maker.client.post(
                f"/api/v1/constraint-proposals/{unproven_failed.subject_artifact_id}:submit-for-approval",
                json={
                    "request_schema_version": "submit-for-approval-request@1",
                    "approval_id": unproven_failed.approval_id,
                    "expected_workflow_revision": unproven_failed.workflow_revision,
                },
                headers=_headers(
                    maker,
                    idempotency_key="unproven:submit-rejected",
                    resource_kind="constraint_proposal",
                    resource_id=unproven_failed.subject_artifact_id,
                    revision=unproven_failed.workflow_revision,
                ),
            ),
        )
        assert (
            _authority_snapshot(
                harness,
                approval_id=unproven_failed.approval_id,
                ref_name=_UNPROVEN_REF,
            ).ref
            is None
        )
        _assert_audit_chains(harness)

        # The bootstrap RECORD draft remains untouched; no accidental continuation
        # through the wrong subject series was possible.
        record_item = _approval(maker, record_approval_id)
        assert record_item.status == "draft"
        assert record_item.subject_revision == 1
        assert _run(maker, record_run_id).status == "succeeded"
        assert _run(maker, replay_run_id).status == "succeeded"
    finally:
        process.close()
        _stop_api(api)
