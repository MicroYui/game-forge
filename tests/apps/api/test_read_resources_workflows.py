from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.errors import install_error_handlers
from gameforge.apps.api.routers.workflows import workflow_read_router
from gameforge.contracts.diff import ConflictSet, JsonValueState, MergeConflict
from gameforge.contracts.errors import Forbidden, IntegrityViolation, NotFound, QueryTooBroad
from gameforge.contracts.api import RunFindingLinkViewV1
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import RunCommandRecordV1, RunCommandViewV1, RunRecord
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    PatchTargetBindingV1,
)
from gameforge.platform.read_models.authorization import ReadAuthorizationService
from gameforge.platform.read_models.paging import (
    MaterializedPagePort,
    ReadPageBinding,
    ReadPageCandidate,
    RetainedReadPageItem,
)
from gameforge.platform.read_models.workflows import (
    CurrentApprovalProgressProjector,
    WorkflowReadCapabilities,
    WorkflowReadService,
    _run_view,
)


NOW = "2026-07-14T10:00:00Z"


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="narrative",
            display_name="Narrative",
            status="active",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _policy(registry: DomainRegistryV1) -> RolePolicy:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    narrative = DomainScope(domain_ids=("narrative",))
    grants = {
        "content_designer": tuple(
            Permission(action="read", resource_kind=kind, domain_scope=narrative)
            for kind in ("approval", "run", "finding", "conflict_set")
        )
        + (
            Permission(
                action="approval.decide",
                resource_kind="approval",
                domain_scope=narrative,
            ),
        )
    }
    return RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=ref,
        grants=grants,
        effective_from=NOW,
        policy_digest=compute_role_policy_digest("roles@1", ref, grants, NOW),
    )


def _principal(principal_id: str, *, role: bool = True) -> Principal:
    assignment = RoleAssignmentV1(
        assignment_id=f"assignment:{principal_id}",
        principal_id=principal_id,
        role="content_designer",
        scope=DomainScope(domain_ids=("narrative",)),
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="system:bootstrap", principal_kind="system"),
    )
    return Principal(
        id=principal_id,
        kind="human",
        display_name=principal_id,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(assignment,) if role else (),
    )


def _approval(registry: DomainRegistryV1) -> ApprovalItem:
    ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    scope = DomainScope(domain_ids=("narrative",))
    role_policy = _policy(registry)
    return ApprovalItem(
        approval_id="approval:1",
        subject_series_id="patch-series:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id="artifact:patch:1",
        subject_digest="a" * 64,
        status="pending_approval",
        workflow_revision=4,
        proposer=AuditActor(principal_id="human:alice", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="b" * 64,
            domain_registry_ref=ref,
        ),
        role_policy_version=role_policy.policy_version,
        role_policy_digest=role_policy.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval@1",
            policy_digest="d" * 64,
        ),
        requirements=(
            ApprovalRequirement(
                requirement_id="requirement:narrative",
                domain_scope=scope,
                required_permission=Permission(
                    action="approval.decide",
                    resource_kind="approval",
                    domain_scope=scope,
                ),
                route_role="content_designer",
                min_approvals=1,
                assignee_principal_ids=("human:bob",),
                distinct_from_requirement_ids=(),
            ),
        ),
        decisions=(),
        evidence_set_artifact_id="artifact:evidence:1",
        regression_evidence_artifact_ids=(),
        target_binding=PatchTargetBindingV1(
            target_artifact_id="artifact:preview:1",
            target_snapshot_id="snapshot:preview:1",
            target_digest="e" * 64,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=2),
        ),
        created_at=NOW,
        submitted_at=NOW,
    )


def _run(
    run_id: str,
    *,
    revision: int = 1,
    status: str = "queued",
    current_attempt_no: int | None = None,
    next_attempt_no: int = 1,
) -> RunRecord:
    return RunRecord.model_construct(
        run_id=run_id,
        status=status,
        revision=revision,
        current_attempt_no=current_attempt_no,
        next_attempt_no=next_attempt_no,
        result_artifact_id=("artifact:result" if status == "succeeded" else None),
        failure_artifact_id=None,
        terminal_cassette_artifact_id=None,
        payload={"secret_prompt": "must-not-leak"},
        request_hash="f" * 64,
        next_fencing_token=99,
        created_at=NOW,
    )


def _finding() -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id="finding:1",
        revision=1,
        created_at=NOW,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker:graph",
            producer_run_id="run:narrative",
            oracle_type="deterministic",
            defect_class="dangling_ref",
            severity="major",
            snapshot_id="snapshot:1",
            status="confirmed",
            message="dangling reference",
        ),
    )


def _command() -> RunCommandRecordV1:
    command = type("CommandProjection", (), {})()
    command.command_id = "command:1"
    command.client_id = "client:1"
    command.client_seq = 1
    command.type = "cancel"
    command.payload_schema_id = "run-cancel@1"
    return RunCommandRecordV1.model_construct(
        run_id="run:narrative",
        command=command,
        status="applied",
        revision=2,
        created_at=NOW,
        applied_at=NOW,
        result_event_seq=2,
        rejection_code=None,
        actor={"principal_id": "human:bob"},
        claimed_fencing_token=123,
    )


def _finding_link() -> RunFindingLinkViewV1:
    finding = _finding()
    return RunFindingLinkViewV1(
        run_id="run:narrative",
        attempt_no=1,
        ordinal=1,
        finding=finding,
        finding_digest=finding_revision_digest(finding),
        evidence_artifact_id="artifact:checker-run:1",
    )


def _conflict_set() -> ConflictSet:
    return ConflictSet(
        id="conflict-set:1",
        base_snapshot_id="snapshot:base",
        current_snapshot_id="snapshot:current",
        proposed_patch_artifact_id="artifact:patch:1",
        expected_ref_revision=2,
        conflict_count=1,
        non_conflicting_ops_digest="1" * 64,
        created_at=NOW,
    )


def _conflict() -> MergeConflict:
    return MergeConflict(
        id="conflict:1",
        path="/entities/e1/name",
        kind="both_modified",
        base=JsonValueState.model_validate({"presence": "present", "value": "old"}),
        current=JsonValueState.model_validate({"presence": "present", "value": "current"}),
        proposed=JsonValueState.model_validate({"presence": "present", "value": "proposed"}),
        allowed_resolutions=("keep_current", "take_proposed"),
    )


@dataclass
class _State:
    approvals: tuple[ApprovalItem, ...]
    runs: tuple[RunRecord, ...]
    findings: tuple[FindingRevisionV1, ...]
    run_finding_links: tuple[RunFindingLinkViewV1, ...]
    commands: tuple[RunCommandRecordV1, ...]
    conflict_set: ConflictSet
    conflicts: tuple[MergeConflict, ...]
    operations: list[tuple[str, int, Any]] = field(default_factory=list)
    next_token: int = 0


class _Policies:
    def __init__(self, state: _State, token: int, policy: RolePolicy, registry: DomainRegistryV1):
        self._state = state
        self._token = token
        self._policy = policy
        self._registry = registry

    def get_role_policy(self, version: str, digest: str):
        self._state.operations.append(("role_policy", self._token, (version, digest)))
        if version == self._policy.policy_version and digest == self._policy.policy_digest:
            return self._policy
        return None

    def get_domain_registry(self, ref: DomainRegistryRefV1):
        self._state.operations.append(("domain_registry", self._token, ref.registry_version))
        return self._registry if ref == self._policy.domain_registry_ref else None


class _Repository:
    def __init__(self, state: _State, token: int):
        self._state = state
        self._token = token

    def _record(self, operation: str, value: Any = None) -> None:
        self._state.operations.append((operation, self._token, value))

    def get_approval(self, approval_id: str):
        self._record("get_approval", approval_id)
        return next(
            (value for value in self._state.approvals if value.approval_id == approval_id), None
        )

    def list_approvals(self, *, max_items: int):
        self._record("list_approvals", max_items)
        return self._state.approvals[:max_items]

    def get_run(self, run_id: str):
        self._record("get_run", run_id)
        return next((value for value in self._state.runs if value.run_id == run_id), None)

    def list_runs(self, *, status: str | None, max_items: int):
        self._record("list_runs", max_items)
        values = self._state.runs
        return tuple(value for value in values if status is None or value.status == status)[
            :max_items
        ]

    def get_finding(self, finding_id: str, revision: int | None = None):
        self._record("get_finding", (finding_id, revision))
        candidates = [value for value in self._state.findings if value.finding_id == finding_id]
        if revision is not None:
            return next((value for value in candidates if value.revision == revision), None)
        return max(candidates, key=lambda value: value.revision, default=None)

    def list_findings(self, *, max_items: int):
        self._record("list_findings", max_items)
        return self._state.findings[:max_items]

    def list_run_findings(self, run_id: str, *, max_items: int):
        self._record("list_run_findings", (run_id, max_items))
        return self._state.findings[:max_items]

    def list_run_finding_links(self, run_id: str, *, max_items: int):
        self._record("list_run_finding_links", (run_id, max_items))
        return tuple(value for value in self._state.run_finding_links if value.run_id == run_id)[
            :max_items
        ]

    def list_run_commands(self, run_id: str, *, max_items: int):
        self._record("list_run_commands", (run_id, max_items))
        return self._state.commands[:max_items]

    def get_conflict_set(self, conflict_set_id: str):
        self._record("get_conflict_set", conflict_set_id)
        return self._state.conflict_set if conflict_set_id == self._state.conflict_set.id else None

    def list_conflicts(self, conflict_set_id: str, *, max_items: int):
        self._record("list_conflicts", (conflict_set_id, max_items))
        return self._state.conflicts[:max_items]


class _Permissions:
    def __init__(self, state: _State, token: int):
        self._state = state
        self._token = token

    def _permission(self, resource_kind: str, domain: str = "narrative") -> Permission:
        self._state.operations.append(("permission", self._token, resource_kind))
        return Permission(
            action="read",
            resource_kind=resource_kind,
            domain_scope=DomainScope(domain_ids=(domain,)),
        )

    def for_run(self, run: RunRecord):
        domain = "economy" if run.run_id.endswith("economy") else "narrative"
        return self._permission("run", domain)

    def for_finding(self, finding: FindingRevisionV1):
        return self._permission("finding")

    def for_conflict_set(self, conflict_set: ConflictSet):
        return self._permission("conflict_set")


class _Pages(MaterializedPagePort):
    def __init__(self, state: _State, token: int, limit: int):
        self._state = state
        self._token = token
        self._limit = limit

    def create(self, candidates: tuple[ReadPageCandidate, ...], *, binding: ReadPageBinding):
        self._state.operations.append(("materialize", self._token, binding.query_hash))
        items = tuple(
            RetainedReadPageItem(
                resource_id=value.resource_id,
                observed_revision=value.observed_revision,
                canonical_view=value.canonical_view,
            )
            for value in candidates[: self._limit]
        )
        return PageV1[RetainedReadPageItem](
            read_snapshot_id=f"snapshot:{self._token}",
            items=items,
            expires_at="2026-07-14T10:05:00Z",
        )

    def page(self, cursor: PageCursorV1, *, binding: ReadPageBinding):
        raise AssertionError(f"unexpected retained-page call: {cursor} {binding}")


def _service(state: _State, principals: dict[str, Principal], *, max_items: int = 10):
    registry = _registry()
    policy = _policy(registry)

    @contextmanager
    def unit_of_work():
        state.next_token += 1
        token = state.next_token
        state.operations.append(("uow_open", token, None))
        policies = _Policies(state, token, policy, registry)

        def resolve(principal_id: str):
            state.operations.append(("principal", token, principal_id))
            return principals.get(principal_id)

        try:
            yield WorkflowReadCapabilities(
                repository=_Repository(state, token),
                authorization=ReadAuthorizationService(
                    policy_repository=policies,
                    role_policy_version=policy.policy_version,
                    role_policy_digest=policy.policy_digest,
                ),
                permission_resolver=_Permissions(state, token),
                approval_projector=CurrentApprovalProgressProjector(
                    policy_repository=policies,
                    principal_resolver=resolve,
                ),
                page_factory=lambda limit: _Pages(state, token, limit),
            )
        finally:
            state.operations.append(("uow_close", token, None))

    return WorkflowReadService(
        unit_of_work=unit_of_work,
        max_materialized_items=max_items,
    )


@pytest.fixture
def read_fixture():
    registry = _registry()
    state = _State(
        approvals=(_approval(registry),),
        runs=(_run("run:narrative"), _run("run:economy")),
        findings=(_finding(),),
        run_finding_links=(_finding_link(),),
        commands=(_command(),),
        conflict_set=_conflict_set(),
        conflicts=(_conflict(),),
    )
    principals = {
        "human:alice": _principal("human:alice"),
        "human:bob": _principal("human:bob"),
    }
    return state, principals, _service(state, principals)


def test_list_run_uses_one_short_uow_max_plus_one_and_redacted_projection(read_fixture) -> None:
    state, principals, service = read_fixture

    page = service.list_runs(
        principals["human:bob"],
        status=None,
        cursor=None,
        limit=100,
    )

    assert [value.run_id for value in page.items] == ["run:narrative"]
    wire = page.items[0].model_dump(mode="json")
    assert "payload" not in wire
    assert "request_hash" not in wire
    assert "fencing" not in str(wire)
    assert next(value for name, _, value in state.operations if name == "list_runs") == 11
    operation_tokens = {token for name, token, _ in state.operations if name != "uow_close"}
    assert operation_tokens == {1}
    assert state.operations[0][0] == "uow_open"
    assert state.operations[-1][0] == "uow_close"


@pytest.mark.parametrize(
    ("status", "current_attempt_no", "next_attempt_no", "expected_attempt_no"),
    (
        ("queued", None, 1, None),
        ("retry_wait", None, 2, 1),
        ("succeeded", 2, 3, 2),
    ),
)
def test_run_view_projects_current_or_last_allocated_attempt(
    status: str,
    current_attempt_no: int | None,
    next_attempt_no: int,
    expected_attempt_no: int | None,
) -> None:
    run = _run(
        "run:narrative",
        status=status,
        current_attempt_no=current_attempt_no,
        next_attempt_no=next_attempt_no,
    )

    assert _run_view(run).attempt_no == expected_attempt_no


def test_approval_assignee_projection_uses_current_role_and_maker_checker(read_fixture) -> None:
    _, principals, service = read_fixture

    bob = service.list_approvals(
        principals["human:bob"],
        assignee="me",
        cursor=None,
        limit=100,
    )
    alice = service.list_approvals(
        principals["human:alice"],
        assignee="me",
        cursor=None,
        limit=100,
    )

    assert bob.items[0].current_actor_allowed_requirement_ids == ("requirement:narrative",)
    assert bob.items[0].requirement_progress[0].valid_approval_count == 0
    assert alice.items == ()


def test_approval_projection_resolves_the_item_frozen_policy_after_deployment_upgrade() -> None:
    registry = _registry()
    frozen = _policy(registry)
    current_grants = {
        "content_designer": tuple(
            permission
            for permission in frozen.grants["content_designer"]
            if permission.action == "read"
        )
    }
    current = RolePolicy(
        policy_version="roles@2",
        domain_registry_ref=frozen.domain_registry_ref,
        grants=current_grants,
        effective_from=NOW,
        policy_digest=compute_role_policy_digest(
            "roles@2",
            frozen.domain_registry_ref,
            current_grants,
            NOW,
        ),
    )
    item = _approval(registry).model_copy(
        update={
            "role_policy_version": frozen.policy_version,
            "role_policy_digest": frozen.policy_digest,
        }
    )
    bob = _principal("human:bob")

    class _RetainedPolicies:
        def get_role_policy(self, version: str, digest: str):
            return {
                (frozen.policy_version, frozen.policy_digest): frozen,
                (current.policy_version, current.policy_digest): current,
            }.get((version, digest))

        def get_domain_registry(self, ref: DomainRegistryRefV1):
            return registry if ref == frozen.domain_registry_ref else None

    view = CurrentApprovalProgressProjector(
        policy_repository=_RetainedPolicies(),
        principal_resolver=lambda principal_id: bob if principal_id == bob.id else None,
    ).project(item, bob)

    approve = next(
        value
        for value in view.requirement_progress[0].decision_eligibility
        if value.decision == "approve"
    )
    assert approve.eligible is True
    assert approve.reason_codes == ()


def test_reject_and_request_changes_remain_eligible_for_a_satisfied_requirement() -> None:
    registry = _registry()
    policy = _policy(registry)
    first = (
        _approval(registry)
        .requirements[0]
        .model_copy(update={"assignee_principal_ids": ("human:bob", "human:charlie")})
    )
    second = first.model_copy(
        update={
            "requirement_id": "requirement:secondary",
            "assignee_principal_ids": ("human:bob",),
        }
    )
    prior = ApprovalDecision(
        decision_id="decision:charlie",
        requirement_ids=(first.requirement_id,),
        decision="approve",
        actor=AuditActor(principal_id="human:charlie", principal_kind="human"),
        expected_workflow_revision=3,
        reason_code="reviewed",
        occurred_at=NOW,
    )
    item = _approval(registry).model_copy(
        update={
            "role_policy_version": policy.policy_version,
            "role_policy_digest": policy.policy_digest,
            "requirements": (first, second),
            "decisions": (prior,),
            "workflow_revision": 5,
        }
    )
    bob = _principal("human:bob")
    charlie = _principal("human:charlie")

    class _FrozenPolicies:
        def get_role_policy(self, version: str, digest: str):
            return (
                policy
                if (version, digest) == (policy.policy_version, policy.policy_digest)
                else None
            )

        def get_domain_registry(self, ref: DomainRegistryRefV1):
            return registry if ref == policy.domain_registry_ref else None

    principals = {bob.id: bob, charlie.id: charlie}
    view = CurrentApprovalProgressProjector(
        policy_repository=_FrozenPolicies(),
        principal_resolver=principals.get,
    ).project(item, bob)
    progress = next(
        value for value in view.requirement_progress if value.requirement_id == first.requirement_id
    )
    by_action = {value.decision: value for value in progress.decision_eligibility}

    assert progress.satisfied is True
    assert by_action["approve"].eligible is False
    assert by_action["approve"].reason_codes == ("requirement_already_satisfied",)
    assert by_action["reject"].eligible is True
    assert by_action["request_changes"].eligible is True


def test_pending_all_satisfied_projects_explicit_reconfirmation_for_an_effective_voter() -> None:
    registry = _registry()
    policy = _policy(registry)
    requirement = (
        _approval(registry)
        .requirements[0]
        .model_copy(
            update={
                "min_approvals": 2,
                "assignee_principal_ids": (
                    "human:bob",
                    "human:charlie",
                    "human:dave",
                ),
            }
        )
    )
    bob_vote = ApprovalDecision(
        decision_id="decision:bob",
        requirement_ids=(requirement.requirement_id,),
        decision="approve",
        actor=AuditActor(principal_id="human:bob", principal_kind="human"),
        expected_workflow_revision=4,
        reason_code="reviewed",
        occurred_at=NOW,
    )
    charlie_vote = bob_vote.model_copy(
        update={
            "decision_id": "decision:charlie",
            "actor": AuditActor(
                principal_id="human:charlie",
                principal_kind="human",
            ),
            "expected_workflow_revision": 5,
        }
    )
    item = _approval(registry).model_copy(
        update={
            "requirements": (requirement,),
            "decisions": (bob_vote, charlie_vote),
            "workflow_revision": 6,
        }
    )
    bob = _principal("human:bob")
    charlie = _principal("human:charlie")
    dave = _principal("human:dave")

    class _FrozenPolicies:
        def get_role_policy(self, version: str, digest: str):
            return (
                policy
                if (version, digest) == (policy.policy_version, policy.policy_digest)
                else None
            )

        def get_domain_registry(self, ref: DomainRegistryRefV1):
            return registry if ref == policy.domain_registry_ref else None

    principals = {bob.id: bob, charlie.id: charlie, dave.id: dave}
    projector = CurrentApprovalProgressProjector(
        policy_repository=_FrozenPolicies(),
        principal_resolver=principals.get,
    )

    bob_view = projector.project(item, bob)
    progress = bob_view.requirement_progress[0]
    by_action = {value.decision: value for value in progress.decision_eligibility}
    assert progress.satisfied is True
    assert by_action["approve"].eligible is True
    assert by_action["approve"].reason_codes == ()
    assert by_action["reject"].reason_codes == ("actor_already_decided_requirement",)
    assert by_action["request_changes"].reason_codes == ("actor_already_decided_requirement",)
    assert bob_view.current_actor_allowed_requirement_ids == (requirement.requirement_id,)

    dave_approve = next(
        value
        for value in projector.project(item, dave).requirement_progress[0].decision_eligibility
        if value.decision == "approve"
    )
    assert dave_approve.eligible is False
    assert dave_approve.reason_codes == ("requirement_already_satisfied",)


def test_approval_projection_treats_one_way_distinct_requirement_as_symmetric(
    read_fixture,
) -> None:
    state, principals, _ = read_fixture
    original = state.approvals[0]
    first = original.requirements[0].model_copy(
        update={"distinct_from_requirement_ids": ("requirement:secondary",)}
    )
    second = ApprovalRequirement(
        requirement_id="requirement:secondary",
        domain_scope=first.domain_scope,
        required_permission=first.required_permission,
        route_role=first.route_role,
        min_approvals=1,
        assignee_principal_ids=("human:bob",),
        distinct_from_requirement_ids=(),
    )
    state.approvals = (original.model_copy(update={"requirements": (first, second)}),)
    service = _service(state, principals)

    view = service.get_approval(principals["human:bob"], original.approval_id)

    unmet = {
        item.requirement_id: item.unmet_distinct_from_requirement_ids
        for item in view.requirement_progress
    }
    assert unmet == {
        "requirement:narrative": ("requirement:secondary",),
        "requirement:secondary": ("requirement:narrative",),
    }


def test_missing_singular_is_loaded_before_domain_resolution(read_fixture) -> None:
    state, principals, service = read_fixture

    with pytest.raises(NotFound):
        service.get_run(principals["human:bob"], "run:missing")

    names = [name for name, _, _ in state.operations]
    assert names == ["uow_open", "get_run", "uow_close"]


def test_injected_materialization_bound_and_page_limit_fail_closed(read_fixture) -> None:
    state, principals, _ = read_fixture
    state.runs = tuple(_run(f"run:narrative:{index}") for index in range(3))
    service = _service(state, principals, max_items=2)

    with pytest.raises(QueryTooBroad):
        service.list_runs(principals["human:bob"], status=None, cursor=None, limit=100)
    assert next(value for name, _, value in state.operations if name == "list_runs") == 3
    assert not any(name == "materialize" for name, _, _ in state.operations)

    state.operations.clear()
    with pytest.raises(QueryTooBroad):
        service.list_runs(principals["human:bob"], status=None, cursor=None, limit=0)
    assert not any(name == "list_runs" for name, _, _ in state.operations)


def test_collection_continuation_rejects_principal_after_all_read_access_is_revoked(
    read_fixture,
) -> None:
    _, principals, service = read_fixture
    revoked = principals["human:bob"].model_copy(
        update={"roles": (), "authz_revision": 2, "revision": 2}
    )
    cursor = PageCursorV1.model_construct(
        snapshot_id="snapshot:retained",
        position="retained-position",
        page_size=100,
        query_hash="1" * 64,
        opaque_signature="2" * 64,
    )

    with pytest.raises(Forbidden, match="no longer has collection read permission"):
        service.list_runs(revoked, status=None, cursor=cursor, limit=100)


def test_router_exposes_exact_workflow_reads_etags_and_no_command_payload(read_fixture) -> None:
    _, principals, service = read_fixture
    actor = ActorContext(
        principal=principals["human:bob"],
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:bob",
        ),
        session_id="session:bob",
        request_id="request:1",
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(workflow_read_router(service))
    app.dependency_overrides[require_actor] = lambda: actor

    with TestClient(app) as client:
        approval = client.get("/api/v1/approvals/approval:1")
        run = client.get("/api/v1/runs/run:narrative")
        finding = client.get("/api/v1/findings/finding:1/revisions/1")
        finding_links = client.get("/api/v1/runs/run:narrative/finding-links")
        commands = client.get("/api/v1/runs/run:narrative/commands")
        conflicts = client.get("/api/v1/conflict-sets/conflict-set:1/conflicts")

    assert approval.status_code == run.status_code == finding.status_code == 200
    for response in (approval, run, finding):
        assert response.headers["ETag"].startswith('"')
        assert int(response.headers["X-Resource-Revision"]) >= 1
    approval_actions = {
        value["decision"]: value
        for value in approval.json()["requirement_progress"][0]["decision_eligibility"]
    }
    assert approval_actions == {
        "approve": {"decision": "approve", "eligible": True, "reason_codes": []},
        "reject": {"decision": "reject", "eligible": True, "reason_codes": []},
        "request_changes": {
            "decision": "request_changes",
            "eligible": True,
            "reason_codes": [],
        },
    }
    assert finding_links.status_code == commands.status_code == conflicts.status_code == 200
    link_wire = finding_links.json()["items"][0]
    assert RunFindingLinkViewV1.model_validate(link_wire).finding.finding_id == "finding:1"
    assert link_wire["finding_digest"] == finding_revision_digest(_finding())
    assert link_wire["evidence_artifact_id"] == "artifact:checker-run:1"
    command_wire = commands.json()["items"][0]
    assert RunCommandViewV1.model_validate(command_wire).command_id == "command:1"
    assert "payload" not in command_wire
    assert "actor" not in command_wire
    assert "claimed_fencing_token" not in command_wire
    assert conflicts.json()["items"][0]["path"] == "/entities/e1/name"


def test_exact_finding_and_run_finding_return_immutable_revision(read_fixture) -> None:
    _, principals, service = read_fixture

    exact = service.get_finding(principals["human:bob"], "finding:1", revision=1)
    linked = service.list_run_findings(
        principals["human:bob"],
        "run:narrative",
        cursor=None,
        limit=100,
    )
    linked_authority = service.list_run_finding_links(
        principals["human:bob"],
        "run:narrative",
        cursor=None,
        limit=100,
    )

    assert exact == linked.items[0]
    assert linked.items[0].revision == 1
    assert linked_authority.items[0].finding == exact
    assert linked_authority.items[0].finding_digest == finding_revision_digest(exact)
    assert linked_authority.items[0].evidence_artifact_id == "artifact:checker-run:1"


def test_unproved_resource_domain_fails_closed(read_fixture) -> None:
    state, principals, _ = read_fixture
    registry = _registry()
    policy = _policy(registry)

    class _MissingPermissions(_Permissions):
        def for_run(self, run: RunRecord):
            return None

    @contextmanager
    def unit_of_work():
        policies = _Policies(state, 1, policy, registry)
        yield WorkflowReadCapabilities(
            repository=_Repository(state, 1),
            authorization=ReadAuthorizationService(
                policy_repository=policies,
                role_policy_version=policy.policy_version,
                role_policy_digest=policy.policy_digest,
            ),
            permission_resolver=_MissingPermissions(state, 1),
            approval_projector=CurrentApprovalProgressProjector(
                policy_repository=policies,
                principal_resolver=lambda principal_id: principals.get(principal_id),
            ),
            page_factory=lambda limit: _Pages(state, 1, limit),
        )

    service = WorkflowReadService(unit_of_work=unit_of_work, max_materialized_items=10)
    with pytest.raises(IntegrityViolation, match="domain scope"):
        service.get_run(principals["human:bob"], "run:narrative")
