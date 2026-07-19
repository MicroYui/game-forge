"""Bounded, authorized read models for approvals, Runs, Findings, and conflicts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from gameforge.contracts.api import (
    ApprovalRequirementProgressV1,
    ApprovalViewV1,
    RunViewV1,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.diff import ConflictSet, MergeConflict
from gameforge.contracts.errors import IntegrityViolation, NotFound, QueryTooBroad
from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.identity import (
    DomainRegistryV1,
    Permission,
    Principal,
    RolePolicy,
)
from gameforge.contracts.jobs import (
    RunCommandRecordV1,
    RunCommandViewV1,
    RunRecord,
    RunStatus,
)
from gameforge.contracts.storage import MAX_PAGE_ITEMS, PageCursorV1, PageV1
from gameforge.contracts.workflow import ApprovalItem, ApprovalRequirement
from gameforge.platform.rbac import AuthorizationDecision, authorize
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationService,
    ReadPolicyRepository,
)
from gameforge.platform.read_models.paging import (
    MaterializedPageFactory,
    ReadPageBinding,
    ReadPageCandidate,
)

_READ_ACTION = "read"
_T = TypeVar("_T", bound=BaseModel)


class WorkflowReadRepository(Protocol):
    """Narrow authoritative reads; every list implementation must apply max + 1."""

    def get_approval(self, approval_id: str) -> ApprovalItem | None: ...

    def list_approvals(self, *, max_items: int) -> Sequence[ApprovalItem]: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def list_runs(
        self,
        *,
        status: RunStatus | None,
        max_items: int,
    ) -> Sequence[RunRecord]: ...

    def get_finding(
        self,
        finding_id: str,
        revision: int | None = None,
    ) -> FindingRevisionV1 | None: ...

    def list_findings(self, *, max_items: int) -> Sequence[FindingRevisionV1]: ...

    def list_run_findings(
        self,
        run_id: str,
        *,
        max_items: int,
    ) -> Sequence[FindingRevisionV1]: ...

    def list_run_commands(
        self,
        run_id: str,
        *,
        max_items: int,
    ) -> Sequence[RunCommandRecordV1]: ...

    def get_conflict_set(self, conflict_set_id: str) -> ConflictSet | None: ...

    def list_conflicts(
        self,
        conflict_set_id: str,
        *,
        max_items: int,
    ) -> Sequence[MergeConflict]: ...


class WorkflowDomainPermissionResolver(Protocol):
    """Resolve server-proved exact permissions from authoritative resource bindings."""

    def for_run(self, run: RunRecord) -> Permission | None: ...

    def for_finding(self, finding: FindingRevisionV1) -> Permission | None: ...

    def for_conflict_set(self, conflict_set: ConflictSet) -> Permission | None: ...


class PrincipalResolver(Protocol):
    def __call__(self, principal_id: str) -> Principal | None: ...


@dataclass(frozen=True, slots=True)
class WorkflowReadCapabilities:
    """All transaction-bound authorities for one short read operation."""

    repository: WorkflowReadRepository
    authorization: ReadAuthorizationService
    permission_resolver: WorkflowDomainPermissionResolver
    approval_projector: CurrentApprovalProgressProjector
    page_factory: MaterializedPageFactory


WorkflowReadUnitOfWorkFactory = Callable[[], AbstractContextManager[WorkflowReadCapabilities]]


def _read_permission(resource_kind: str, scope: object) -> Permission:
    try:
        return Permission(
            action=_READ_ACTION,
            resource_kind=resource_kind,
            domain_scope=scope,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("resource permission scope is invalid") from exc


def _exact_permission(
    permission: Permission | None,
    *,
    resource_kind: str,
) -> Permission | None:
    if permission is None:
        return None
    if type(permission) is not Permission:
        raise IntegrityViolation("resource permission resolver returned an invalid value")
    if permission.action != _READ_ACTION or permission.resource_kind != resource_kind:
        raise IntegrityViolation("resource permission resolver returned the wrong permission")
    return permission


def _bounded(
    values: Sequence[_T],
    *,
    label: str,
    max_items: int,
) -> tuple[_T, ...]:
    selected = tuple(values)
    if len(selected) > max_items:
        raise QueryTooBroad(
            f"{label} query exceeds the configured bound",
            max_items=max_items,
        )
    return selected


def _page_limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= MAX_PAGE_ITEMS:
        raise QueryTooBroad(
            "page limit is outside the configured bound",
            max_page_items=MAX_PAGE_ITEMS,
        )
    return value


def _query_hash(
    *,
    resource_kind: str,
    filters: dict[str, object],
    sort: tuple[str, ...],
    projection: str,
    page_size: int | None = None,
) -> str:
    return canonical_sha256(
        {
            "query_schema_version": "api-read-query@1",
            "api_version": "v1",
            "resource_kind": resource_kind,
            "filters": filters,
            "sort": sort,
            "projection": projection,
            "page_size": page_size,
        }
    )


def _run_view(run: RunRecord) -> RunViewV1:
    attempt_no = run.current_attempt_no
    if attempt_no is None and run.next_attempt_no > 1:
        attempt_no = run.next_attempt_no - 1
    return RunViewV1(
        run_id=run.run_id,
        status=run.status,
        revision=run.revision,
        attempt_no=attempt_no,
        result_artifact_id=run.result_artifact_id,
        failure_artifact_id=run.failure_artifact_id,
        terminal_cassette_artifact_id=run.terminal_cassette_artifact_id,
        status_url=f"/api/v1/runs/{run.run_id}",
        events_url=f"/api/v1/runs/{run.run_id}/events",
    )


def _command_view(record: RunCommandRecordV1) -> RunCommandViewV1:
    command = record.command
    return RunCommandViewV1(
        run_id=record.run_id,
        command_id=command.command_id,
        client_id=command.client_id,
        client_seq=command.client_seq,
        type=command.type,
        payload_schema_id=command.payload_schema_id,
        status=record.status,
        revision=record.revision,
        created_at=record.created_at,
        applied_at=record.applied_at,
        result_event_seq=record.result_event_seq,
        rejection_code=record.rejection_code,
    )


def _routed_principal(principal: Principal, requirement: ApprovalRequirement) -> Principal | None:
    roles = tuple(role for role in principal.roles if role.role == requirement.route_role)
    if not roles:
        return None
    return Principal.model_validate({**principal.model_dump(mode="python"), "roles": roles})


class CurrentApprovalProgressProjector:
    """Project frozen requirements against current identities and exact current RBAC."""

    def __init__(
        self,
        *,
        policy_repository: ReadPolicyRepository,
        role_policy_version: str,
        role_policy_digest: str,
        principal_resolver: PrincipalResolver,
    ) -> None:
        self._policies = policy_repository
        self._policy_version = role_policy_version
        self._policy_digest = role_policy_digest
        self._principals = principal_resolver

    def project(self, item: ApprovalItem, actor: Principal) -> ApprovalViewV1:
        policy, registry = self._authority()
        requirements = {value.requirement_id: value for value in item.requirements}
        approvals = self._currently_valid_approvals(
            item=item,
            requirements=requirements,
            policy=policy,
            registry=registry,
        )
        distinct = {
            requirement_id: self._distinct_ids(requirement_id, requirements)
            for requirement_id in requirements
        }
        for requirement_id, other_ids in distinct.items():
            overlapping = (
                set().union(*(approvals[requirement_id] & approvals[other] for other in other_ids))
                if other_ids
                else set()
            )
            if overlapping:
                approvals[requirement_id].difference_update(overlapping)
                for other in other_ids:
                    approvals[other].difference_update(overlapping)

        progress: list[ApprovalRequirementProgressV1] = []
        for requirement_id in sorted(requirements):
            requirement = requirements[requirement_id]
            count = len(approvals[requirement_id])
            unmet_distinct = tuple(
                other
                for other in sorted(distinct[requirement_id])
                if len(approvals[other]) < requirements[other].min_approvals
            )
            eligible = self._eligible(
                item=item,
                actor=actor,
                requirement=requirement,
                already_approved=approvals,
                distinct_ids=distinct[requirement_id],
                policy=policy,
                registry=registry,
            )
            progress.append(
                ApprovalRequirementProgressV1(
                    requirement_id=requirement_id,
                    domain_scope=requirement.domain_scope,
                    route_role=requirement.route_role,
                    min_approvals=requirement.min_approvals,
                    valid_approval_count=count,
                    satisfied=count >= requirement.min_approvals,
                    eligible_for_current_actor=eligible,
                    unmet_distinct_from_requirement_ids=unmet_distinct,
                )
            )
        return ApprovalViewV1(
            approval=item,
            requirement_progress=tuple(progress),
            current_actor_allowed_requirement_ids=tuple(
                value.requirement_id for value in progress if value.eligible_for_current_actor
            ),
        )

    def _authority(self) -> tuple[RolePolicy, DomainRegistryV1]:
        policy = self._policies.get_role_policy(self._policy_version, self._policy_digest)
        if type(policy) is not RolePolicy:
            raise IntegrityViolation("current exact role policy is unavailable")
        if (
            policy.policy_version != self._policy_version
            or policy.policy_digest != self._policy_digest
        ):
            raise IntegrityViolation("role policy authority returned another exact policy")
        registry = self._policies.get_domain_registry(policy.domain_registry_ref)
        if type(registry) is not DomainRegistryV1:
            raise IntegrityViolation("current exact domain registry is unavailable")
        if (
            registry.registry_version != policy.domain_registry_ref.registry_version
            or registry.registry_digest != policy.domain_registry_ref.registry_digest
        ):
            raise IntegrityViolation("domain registry authority returned another exact registry")
        return policy, registry

    def _currently_valid_approvals(
        self,
        *,
        item: ApprovalItem,
        requirements: dict[str, ApprovalRequirement],
        policy: RolePolicy,
        registry: DomainRegistryV1,
    ) -> dict[str, set[str]]:
        result = {requirement_id: set() for requirement_id in requirements}
        for decision in item.decisions:
            if decision.decision != "approve":
                continue
            principal = self._principals(decision.actor.principal_id)
            if (
                type(principal) is not Principal
                or principal.id != decision.actor.principal_id
                or principal.status != "active"
                or principal.kind != "human"
                or decision.actor.principal_kind != principal.kind
                or principal.id == item.proposer.principal_id
            ):
                continue
            for requirement_id in decision.requirement_ids:
                requirement = requirements.get(requirement_id)
                if requirement is not None and self._can_decide(
                    principal,
                    requirement,
                    policy=policy,
                    registry=registry,
                ):
                    result[requirement_id].add(principal.id)
        return result

    @staticmethod
    def _distinct_ids(
        requirement_id: str,
        requirements: dict[str, ApprovalRequirement],
    ) -> set[str]:
        result = set(requirements[requirement_id].distinct_from_requirement_ids)
        result.update(
            other.requirement_id
            for other in requirements.values()
            if requirement_id in other.distinct_from_requirement_ids
        )
        return result

    def _eligible(
        self,
        *,
        item: ApprovalItem,
        actor: Principal,
        requirement: ApprovalRequirement,
        already_approved: dict[str, set[str]],
        distinct_ids: set[str],
        policy: RolePolicy,
        registry: DomainRegistryV1,
    ) -> bool:
        if (
            item.status != "pending_approval"
            or actor.kind != "human"
            or actor.status != "active"
            or actor.id == item.proposer.principal_id
            or len(already_approved[requirement.requirement_id]) >= requirement.min_approvals
            or actor.id in already_approved[requirement.requirement_id]
            or any(actor.id in already_approved[other] for other in distinct_ids)
        ):
            return False
        return self._can_decide(actor, requirement, policy=policy, registry=registry)

    @staticmethod
    def _can_decide(
        principal: Principal,
        requirement: ApprovalRequirement,
        *,
        policy: RolePolicy,
        registry: DomainRegistryV1,
    ) -> bool:
        if requirement.assignee_principal_ids and principal.id not in (
            requirement.assignee_principal_ids
        ):
            return False
        routed = _routed_principal(principal, requirement)
        return (
            routed is not None
            and authorize(
                principal=routed,
                role_policy=policy,
                requested_permission=requirement.required_permission,
                domain_registry=registry,
            )
            is AuthorizationDecision.ALLOW
        )


@dataclass(frozen=True, slots=True)
class _ListDefinition:
    resource_kind: str
    stable_sort_schema_id: str
    view_schema_id: str
    projection: str


class _WorkflowReadOperations:
    """One transaction-scoped set of exact read operations."""

    def __init__(
        self,
        *,
        capabilities: WorkflowReadCapabilities,
        max_materialized_items: int,
    ) -> None:
        if (
            isinstance(max_materialized_items, bool)
            or not isinstance(max_materialized_items, int)
            or max_materialized_items < 1
        ):
            raise ValueError("max_materialized_items must be positive")
        self._repository = capabilities.repository
        self._authorization = capabilities.authorization
        self._permissions = capabilities.permission_resolver
        self._approval_projector = capabilities.approval_projector
        self._pages = capabilities.page_factory
        self._max_items = max_materialized_items

    def get_approval(self, principal: Principal, approval_id: str) -> ApprovalViewV1:
        item = self._repository.get_approval(approval_id)
        if item is None:
            raise NotFound("ApprovalItem does not exist", approval_id=approval_id)
        query = _query_hash(
            resource_kind="approval",
            filters={"approval_id": approval_id},
            sort=(),
            projection="approval-view@1",
        )
        self._authorization.require_singular(
            principal=principal,
            permission=_read_permission("approval", item.domain_scope),
            query_hash=query,
        )
        return self._approval_projector.project(item, principal)

    def list_approvals(
        self,
        principal: Principal,
        *,
        assignee: Literal["me"] | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ApprovalViewV1]:
        limit = _page_limit(limit)
        definition = _ListDefinition(
            "approvals", "approval-created-id@1", "approval-view@1", "approval-view@1"
        )
        query = _query_hash(
            resource_kind=definition.resource_kind,
            filters={"assignee": assignee},
            sort=("created_at:asc", "approval_id:asc"),
            projection=definition.projection,
            page_size=limit,
        )
        if cursor is None:
            items = _bounded(
                self._repository.list_approvals(max_items=self._max_items + 1),
                label="approval",
                max_items=self._max_items,
            )
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=items,
                collection_permission=_read_permission("approval", "all"),
                permission_for=lambda item: _read_permission("approval", item.domain_scope),
                query_hash=query,
            )
            views = tuple(
                self._approval_projector.project(item, principal) for item in authorized.items
            )
            if assignee == "me":
                views = tuple(view for view in views if view.current_actor_allowed_requirement_ids)
            binding = authorized.binding
        else:
            binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=_read_permission("approval", "all"),
                query_hash=query,
            )
            views = ()
        return self._materialized_page(
            views,
            principal_binding=binding.principal_binding,
            authz_fingerprint=binding.authz_fingerprint,
            query_hash=query,
            definition=definition,
            cursor=cursor,
            limit=limit,
            model=ApprovalViewV1,
            identity=lambda value: (value.approval.approval_id, value.approval.workflow_revision),
        )

    def get_run(self, principal: Principal, run_id: str) -> RunViewV1:
        run = self._load_run(principal, run_id)
        return _run_view(run)

    def list_runs(
        self,
        principal: Principal,
        *,
        status: RunStatus | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RunViewV1]:
        limit = _page_limit(limit)
        definition = _ListDefinition("runs", "run-created-id@1", "run-view@1", "run-view@1")
        query = _query_hash(
            resource_kind="runs",
            filters={"status": status},
            sort=("created_at:asc", "run_id:asc"),
            projection=definition.projection,
            page_size=limit,
        )
        if cursor is None:
            items = _bounded(
                self._repository.list_runs(
                    status=status,
                    max_items=self._max_items + 1,
                ),
                label="Run",
                max_items=self._max_items,
            )
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=items,
                collection_permission=_read_permission("run", "all"),
                permission_for=lambda value: _exact_permission(
                    self._permissions.for_run(value), resource_kind="run"
                ),
                query_hash=query,
            )
            views = tuple(_run_view(item) for item in authorized.items)
            binding = authorized.binding
        else:
            binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=_read_permission("run", "all"),
                query_hash=query,
            )
            views = ()
        return self._materialized_page(
            views,
            principal_binding=binding.principal_binding,
            authz_fingerprint=binding.authz_fingerprint,
            query_hash=query,
            definition=definition,
            cursor=cursor,
            limit=limit,
            model=RunViewV1,
            identity=lambda value: (value.run_id, value.revision),
        )

    def get_finding(
        self,
        principal: Principal,
        finding_id: str,
        *,
        revision: int | None = None,
    ) -> FindingRevisionV1:
        finding = self._repository.get_finding(finding_id, revision)
        if finding is None:
            raise NotFound("Finding revision does not exist", finding_id=finding_id)
        query = _query_hash(
            resource_kind="finding",
            filters={"finding_id": finding_id, "revision": revision},
            sort=(),
            projection="finding-revision@1",
        )
        self._authorization.require_singular(
            principal=principal,
            permission=_exact_permission(
                self._permissions.for_finding(finding), resource_kind="finding"
            ),
            query_hash=query,
        )
        return finding

    def list_findings(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[FindingRevisionV1]:
        return self._finding_page(
            principal,
            run_id=None,
            cursor=cursor,
            limit=limit,
        )

    def list_run_findings(
        self,
        principal: Principal,
        run_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[FindingRevisionV1]:
        self._load_run(principal, run_id)
        return self._finding_page(
            principal,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )

    def list_run_commands(
        self,
        principal: Principal,
        run_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RunCommandViewV1]:
        limit = _page_limit(limit)
        run = self._load_run(principal, run_id)
        permission = _exact_permission(self._permissions.for_run(run), resource_kind="run")
        definition = _ListDefinition(
            "run_commands", "command-created-id@1", "run-command-view@1", "run-command-view@1"
        )
        query = _query_hash(
            resource_kind=definition.resource_kind,
            filters={"run_id": run_id},
            sort=("created_at:asc", "command_id:asc"),
            projection=definition.projection,
            page_size=limit,
        )
        if cursor is None:
            records = _bounded(
                self._repository.list_run_commands(run_id, max_items=self._max_items + 1),
                label="Run command",
                max_items=self._max_items,
            )
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=records,
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            views = tuple(_command_view(item) for item in authorized.items)
            binding = authorized.binding
        else:
            binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=permission,
                query_hash=query,
            )
            views = ()
        return self._materialized_page(
            views,
            principal_binding=binding.principal_binding,
            authz_fingerprint=binding.authz_fingerprint,
            query_hash=query,
            definition=definition,
            cursor=cursor,
            limit=limit,
            model=RunCommandViewV1,
            identity=lambda value: (value.command_id, value.revision),
        )

    def list_conflicts(
        self,
        principal: Principal,
        conflict_set_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[MergeConflict]:
        limit = _page_limit(limit)
        conflict_set = self._repository.get_conflict_set(conflict_set_id)
        if conflict_set is None:
            raise NotFound("ConflictSet does not exist", conflict_set_id=conflict_set_id)
        permission = _exact_permission(
            self._permissions.for_conflict_set(conflict_set),
            resource_kind="conflict_set",
        )
        definition = _ListDefinition(
            "conflicts", "conflict-path-id@1", "merge-conflict@1", "merge-conflict@1"
        )
        query = _query_hash(
            resource_kind=definition.resource_kind,
            filters={"conflict_set_id": conflict_set_id},
            sort=("path:asc", "id:asc"),
            projection=definition.projection,
            page_size=limit,
        )
        if cursor is None:
            conflicts = _bounded(
                self._repository.list_conflicts(conflict_set_id, max_items=self._max_items + 1),
                label="conflict",
                max_items=self._max_items,
            )
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=conflicts,
                collection_permission=permission,
                permission_for=lambda _: permission,
                query_hash=query,
            )
            values = tuple(authorized.items)
            binding = authorized.binding
        else:
            binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=permission,
                query_hash=query,
            )
            values = ()
        return self._materialized_page(
            values,
            principal_binding=binding.principal_binding,
            authz_fingerprint=binding.authz_fingerprint,
            query_hash=query,
            definition=definition,
            cursor=cursor,
            limit=limit,
            model=MergeConflict,
            identity=lambda value: (value.id, 1),
        )

    def _load_run(self, principal: Principal, run_id: str) -> RunRecord:
        run = self._repository.get_run(run_id)
        if run is None:
            raise NotFound("Run does not exist", run_id=run_id)
        query = _query_hash(
            resource_kind="run",
            filters={"run_id": run_id},
            sort=(),
            projection="run-view@1",
        )
        self._authorization.require_singular(
            principal=principal,
            permission=_exact_permission(self._permissions.for_run(run), resource_kind="run"),
            query_hash=query,
        )
        return run

    def _finding_page(
        self,
        principal: Principal,
        *,
        run_id: str | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[FindingRevisionV1]:
        limit = _page_limit(limit)
        resource_kind = "findings" if run_id is None else "run_findings"
        stable_sort = ("finding_id:asc",) if run_id is None else ("attempt_no:asc", "ordinal:asc")
        definition = _ListDefinition(
            resource_kind,
            ("finding-id@1" if run_id is None else "run-finding-attempt-ordinal@1"),
            "finding-revision@1",
            "finding-revision@1",
        )
        query = _query_hash(
            resource_kind=resource_kind,
            filters={"run_id": run_id},
            sort=stable_sort,
            projection=definition.projection,
            page_size=limit,
        )
        if cursor is None:
            source = (
                self._repository.list_findings(max_items=self._max_items + 1)
                if run_id is None
                else self._repository.list_run_findings(run_id, max_items=self._max_items + 1)
            )
            findings = _bounded(
                source,
                label="Finding",
                max_items=self._max_items,
            )
            authorized = self._authorization.filter_collection(
                principal=principal,
                candidates=findings,
                collection_permission=_read_permission("finding", "all"),
                permission_for=lambda value: _exact_permission(
                    self._permissions.for_finding(value), resource_kind="finding"
                ),
                query_hash=query,
            )
            values = tuple(authorized.items)
            binding = authorized.binding
        else:
            binding = self._authorization.require_collection_continuation(
                principal=principal,
                collection_permission=_read_permission("finding", "all"),
                query_hash=query,
            )
            values = ()
        return self._materialized_page(
            values,
            principal_binding=binding.principal_binding,
            authz_fingerprint=binding.authz_fingerprint,
            query_hash=query,
            definition=definition,
            cursor=cursor,
            limit=limit,
            model=FindingRevisionV1,
            identity=lambda value: (
                f"{value.finding_id}:revision:{value.revision}",
                value.revision,
            ),
        )

    def _materialized_page(
        self,
        values: Sequence[_T],
        *,
        principal_binding: str,
        authz_fingerprint: str,
        query_hash: str,
        definition: _ListDefinition,
        cursor: PageCursorV1 | None,
        limit: int,
        model: type[_T],
        identity: Callable[[_T], tuple[str, int]],
    ) -> PageV1[_T]:
        limit = _page_limit(limit)
        page_repository = self._pages(limit)
        binding = ReadPageBinding(
            resource_kind=definition.resource_kind,
            query_hash=query_hash,
            authz_fingerprint=authz_fingerprint,
            stable_sort_schema_id=definition.stable_sort_schema_id,
            view_schema_id=definition.view_schema_id,
            principal_binding=principal_binding,
        )
        if cursor is None:
            candidates = tuple(
                ReadPageCandidate(
                    resource_id=identity(value)[0],
                    observed_revision=identity(value)[1],
                    canonical_view=value.model_dump(mode="json"),
                )
                for value in values
            )
            internal = page_repository.create(candidates, binding=binding)
        else:
            internal = page_repository.page(cursor, binding=binding)
        parsed: list[_T] = []
        for item in internal.items:
            try:
                value = model.model_validate(item.canonical_view)
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation("materialized workflow read view is invalid") from exc
            resource_id, revision = identity(value)
            if item.resource_id != resource_id or item.observed_revision != revision:
                raise IntegrityViolation("materialized workflow read identity is invalid")
            parsed.append(value)
        return PageV1[_T](
            read_snapshot_id=internal.read_snapshot_id,
            items=tuple(parsed),
            next_cursor=internal.next_cursor,
            expires_at=internal.expires_at,
        )


class WorkflowReadService:
    """Long-lived facade that opens exactly one short UoW per public read."""

    def __init__(
        self,
        *,
        unit_of_work: WorkflowReadUnitOfWorkFactory,
        max_materialized_items: int,
    ) -> None:
        if not callable(unit_of_work):
            raise TypeError("unit_of_work must be callable")
        if (
            isinstance(max_materialized_items, bool)
            or not isinstance(max_materialized_items, int)
            or max_materialized_items < 1
        ):
            raise ValueError("max_materialized_items must be positive")
        self._unit_of_work = unit_of_work
        self._max_items = max_materialized_items

    def _operations(self, capabilities: WorkflowReadCapabilities) -> _WorkflowReadOperations:
        if not isinstance(capabilities, WorkflowReadCapabilities):
            raise TypeError("read UoW returned invalid capabilities")
        return _WorkflowReadOperations(
            capabilities=capabilities,
            max_materialized_items=self._max_items,
        )

    def get_approval(self, principal: Principal, approval_id: str) -> ApprovalViewV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_approval(principal, approval_id)

    def list_approvals(
        self,
        principal: Principal,
        *,
        assignee: Literal["me"] | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[ApprovalViewV1]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_approvals(
                principal,
                assignee=assignee,
                cursor=cursor,
                limit=limit,
            )

    def get_run(self, principal: Principal, run_id: str) -> RunViewV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_run(principal, run_id)

    def list_runs(
        self,
        principal: Principal,
        *,
        status: RunStatus | None,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RunViewV1]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_runs(
                principal,
                status=status,
                cursor=cursor,
                limit=limit,
            )

    def get_finding(
        self,
        principal: Principal,
        finding_id: str,
        *,
        revision: int | None = None,
    ) -> FindingRevisionV1:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).get_finding(
                principal,
                finding_id,
                revision=revision,
            )

    def list_findings(
        self,
        principal: Principal,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[FindingRevisionV1]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_findings(
                principal,
                cursor=cursor,
                limit=limit,
            )

    def list_run_findings(
        self,
        principal: Principal,
        run_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[FindingRevisionV1]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_run_findings(
                principal,
                run_id,
                cursor=cursor,
                limit=limit,
            )

    def list_run_commands(
        self,
        principal: Principal,
        run_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[RunCommandViewV1]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_run_commands(
                principal,
                run_id,
                cursor=cursor,
                limit=limit,
            )

    def list_conflicts(
        self,
        principal: Principal,
        conflict_set_id: str,
        *,
        cursor: PageCursorV1 | None,
        limit: int,
    ) -> PageV1[MergeConflict]:
        with self._unit_of_work() as capabilities:
            return self._operations(capabilities).list_conflicts(
                principal,
                conflict_set_id,
                cursor=cursor,
                limit=limit,
            )


__all__ = [
    "CurrentApprovalProgressProjector",
    "PrincipalResolver",
    "WorkflowDomainPermissionResolver",
    "WorkflowReadCapabilities",
    "WorkflowReadRepository",
    "WorkflowReadService",
    "WorkflowReadUnitOfWorkFactory",
]
