"""M4 identity, domain-registry, RBAC, and approval-routing wire contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.lineage import AuditActor


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]

SubjectKind = Literal["patch", "constraint_proposal", "rollback_request"]
SubjectKindOrder: tuple[SubjectKind, ...] = (
    "patch",
    "constraint_proposal",
    "rollback_request",
)
SubjectKindRank = {value: index for index, value in enumerate(SubjectKindOrder)}

PrincipalKind = Literal["human", "service", "system"]
Role = Literal[
    "content_designer",
    "numeric_designer",
    "qa",
    "tooling",
    "constraint_admin",
    "gacha_compliance_reviewer",
    "identity_admin",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


def _stable_unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _canonical_model_key(value: BaseModel) -> str:
    return canonical_json(value.model_dump(mode="json"))


def _stable_unique_models[T: BaseModel](values: Sequence[T]) -> tuple[T, ...]:
    by_payload = {_canonical_model_key(value): value for value in values}
    return tuple(by_payload[key] for key in sorted(by_payload))


def _canonical_subject_kinds(values: Sequence[SubjectKind]) -> tuple[SubjectKind, ...]:
    return tuple(sorted(set(values), key=SubjectKindRank.__getitem__))


class DomainScope(_FrozenModel):
    domain_ids: tuple[NonEmptyStr, ...]

    @field_validator("domain_ids")
    @classmethod
    def _canonical_domain_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _stable_unique_strings(value)
        if not canonical:
            raise ValueError("domain_ids must be non-empty")
        return canonical


DomainScopeValue: TypeAlias = DomainScope | Literal["all"] | None
RoutedDomainScope: TypeAlias = DomainScope | Literal["all"]


def _scope_key(value: DomainScopeValue) -> str:
    if value is None:
        return "0:null"
    if value == "all":
        return "1:all"
    return "2:" + canonical_json(value.model_dump(mode="json"))


class DomainDefinitionV1(_FrozenModel):
    domain_id: NonEmptyStr
    display_name: NonEmptyStr
    description: NonEmptyStr | None = None
    parent_domain_id: NonEmptyStr | None = None
    tags: tuple[NonEmptyStr, ...] = ()
    status: Literal["active", "deprecated"]

    @field_validator("tags")
    @classmethod
    def _canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)


class DomainRegistryRefV1(_FrozenModel):
    registry_version: NonEmptyStr
    registry_digest: LowerHexSha256


def compute_domain_registry_digest(
    registry_version: str,
    definitions: Sequence[DomainDefinitionV1],
) -> str:
    ordered = sorted(definitions, key=lambda item: item.domain_id)
    return canonical_sha256(
        {
            "registry_schema_version": "domain-registry@1",
            "registry_version": registry_version,
            "definitions": [item.model_dump(mode="json") for item in ordered],
        }
    )


class DomainRegistryV1(_FrozenModel):
    registry_schema_version: Literal["domain-registry@1"] = "domain-registry@1"
    registry_version: NonEmptyStr
    definitions: tuple[DomainDefinitionV1, ...]
    registry_digest: LowerHexSha256

    @field_validator("definitions")
    @classmethod
    def _canonical_definitions(
        cls, value: tuple[DomainDefinitionV1, ...]
    ) -> tuple[DomainDefinitionV1, ...]:
        seen: set[str] = set()
        for definition in value:
            if definition.domain_id in seen:
                raise ValueError(f"duplicate domain_id: {definition.domain_id}")
            seen.add(definition.domain_id)
        return tuple(sorted(value, key=lambda item: item.domain_id))

    @model_validator(mode="after")
    def _validate_graph_and_digest(self) -> DomainRegistryV1:
        definitions = {item.domain_id: item for item in self.definitions}
        for definition in self.definitions:
            parent = definition.parent_domain_id
            if parent is not None and parent not in definitions:
                raise ValueError(f"parent_domain_id {parent!r} does not exist in registry")

        states: dict[str, Literal["visiting", "visited"]] = {}

        def visit(domain_id: str) -> None:
            state = states.get(domain_id)
            if state == "visiting":
                raise ValueError("domain registry contains a parent cycle")
            if state == "visited":
                return
            states[domain_id] = "visiting"
            parent = definitions[domain_id].parent_domain_id
            if parent is not None:
                visit(parent)
            states[domain_id] = "visited"

        for domain_id in definitions:
            visit(domain_id)

        expected = compute_domain_registry_digest(self.registry_version, self.definitions)
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match canonical registry payload")
        return self


class Permission(_FrozenModel):
    action: NonEmptyStr
    resource_kind: NonEmptyStr
    domain_scope: DomainScopeValue


def _canonical_permissions(
    values: Sequence[Permission],
) -> tuple[Permission, ...]:
    return _stable_unique_models(values)


def _canonical_grants(
    grants: Mapping[str, Sequence[Permission]],
) -> dict[str, tuple[Permission, ...]]:
    return {role: _canonical_permissions(tuple(grants[role])) for role in sorted(grants)}


def compute_role_policy_digest(
    policy_version: str,
    domain_registry_ref: DomainRegistryRefV1,
    grants: Mapping[str, Sequence[Permission]],
    effective_from: str,
) -> str:
    canonical_grants = _canonical_grants(grants)
    return canonical_sha256(
        {
            "policy_version": policy_version,
            "domain_registry_ref": domain_registry_ref.model_dump(mode="json"),
            "grants": {
                role: [permission.model_dump(mode="json") for permission in permissions]
                for role, permissions in canonical_grants.items()
            },
            "effective_from": effective_from,
        }
    )


class RolePolicy(_FrozenModel):
    policy_version: NonEmptyStr
    domain_registry_ref: DomainRegistryRefV1
    grants: dict[Role, tuple[Permission, ...]]
    effective_from: NonEmptyStr
    policy_digest: LowerHexSha256

    @field_validator("grants")
    @classmethod
    def _canonicalize_grants(
        cls, value: dict[Role, tuple[Permission, ...]]
    ) -> dict[Role, tuple[Permission, ...]]:
        return {role: _canonical_permissions(value[role]) for role in sorted(value)}

    @model_validator(mode="after")
    def _validate_digest(self) -> RolePolicy:
        expected = compute_role_policy_digest(
            self.policy_version,
            self.domain_registry_ref,
            self.grants,
            self.effective_from,
        )
        if self.policy_digest != expected:
            raise ValueError("policy_digest does not match canonical role policy payload")
        return self


class RoleAssignmentV1(_FrozenModel):
    assignment_schema_version: Literal["role-assignment@1"] = "role-assignment@1"
    assignment_id: NonEmptyStr
    principal_id: NonEmptyStr
    role: Role
    scope: DomainScopeValue
    status: Literal["active", "revoked"]
    revision: PositiveInt
    granted_at: NonEmptyStr
    granted_by: AuditActor
    revoked_at: NonEmptyStr | None = None
    revoked_by: AuditActor | None = None
    revoke_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_revocation_projection(self) -> RoleAssignmentV1:
        revoked = (self.revoked_at, self.revoked_by, self.revoke_reason)
        if self.status == "active" and any(value is not None for value in revoked):
            raise ValueError("active role assignment cannot contain revocation fields")
        return self


class PrincipalRecordV1(_FrozenModel):
    principal_schema_version: Literal["principal@1"] = "principal@1"
    principal_id: NonEmptyStr
    kind: PrincipalKind
    display_name: NonEmptyStr
    status: Literal["active", "disabled"]
    credential_epoch: NonNegativeInt
    authz_revision: NonNegativeInt
    revision: PositiveInt
    created_at: NonEmptyStr
    updated_at: NonEmptyStr
    disabled_at: NonEmptyStr | None = None
    disabled_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_disabled_projection(self) -> PrincipalRecordV1:
        disabled = (self.disabled_at, self.disabled_reason)
        if self.status == "active" and any(value is not None for value in disabled):
            raise ValueError("active principal cannot contain disabled fields")
        return self


class Principal(_FrozenModel):
    id: NonEmptyStr
    kind: PrincipalKind
    display_name: NonEmptyStr
    status: Literal["active", "disabled"]
    revision: PositiveInt
    credential_epoch: NonNegativeInt
    authz_revision: NonNegativeInt
    roles: tuple[RoleAssignmentV1, ...]

    @field_validator("roles")
    @classmethod
    def _canonical_roles(cls, value: tuple[RoleAssignmentV1, ...]) -> tuple[RoleAssignmentV1, ...]:
        return tuple(
            sorted(
                value,
                key=lambda item: (item.role, _scope_key(item.scope), item.assignment_id),
            )
        )

    @model_validator(mode="after")
    def _validate_assignment_projection(self) -> Principal:
        identities: set[tuple[str, str]] = set()
        assignment_ids: set[str] = set()
        for assignment in self.roles:
            if assignment.principal_id != self.id:
                raise ValueError("role assignment principal_id does not match principal")
            if assignment.status != "active":
                raise ValueError("Principal projection accepts active role assignments only")
            identity = (assignment.role, _scope_key(assignment.scope))
            if identity in identities:
                raise ValueError("duplicate active role assignment identity")
            if assignment.assignment_id in assignment_ids:
                raise ValueError("duplicate assignment_id")
            identities.add(identity)
            assignment_ids.add(assignment.assignment_id)
        return self


class AuthenticationContext(_FrozenModel):
    mechanism: Literal["session", "api_key", "trusted_internal"]
    credential_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_credential_binding(self) -> AuthenticationContext:
        if self.mechanism in {"session", "api_key"} and self.credential_id is None:
            raise ValueError(f"credential_id is required for {self.mechanism}")
        if self.mechanism == "trusted_internal" and self.credential_id is not None:
            raise ValueError("trusted_internal cannot carry credential_id")
        return self


class ActorContext(_FrozenModel):
    principal: Principal
    authentication: AuthenticationContext
    session_id: NonEmptyStr | None = None
    request_id: NonEmptyStr

    @model_validator(mode="after")
    def _validate_authentication_kind(self) -> ActorContext:
        expected_mechanism = {
            "human": "session",
            "service": "api_key",
            "system": "trusted_internal",
        }[self.principal.kind]
        if self.authentication.mechanism != expected_mechanism:
            raise ValueError(
                f"{self.principal.kind} principal requires {expected_mechanism} authentication"
            )
        if self.principal.status != "active":
            raise ValueError("disabled principal cannot form an ActorContext")
        if self.principal.kind in {"service", "system"} and self.session_id is not None:
            raise ValueError("service/system ActorContext cannot carry browser session_id")
        return self


class DomainRouteRule(_FrozenModel):
    rule_id: NonEmptyStr
    domain_selector: RoutedDomainScope
    subject_kinds: tuple[SubjectKind, ...]
    route_role: Role
    required_action: NonEmptyStr
    resource_kind: NonEmptyStr
    min_approvals: PositiveInt
    distinct_from_rule_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("subject_kinds")
    @classmethod
    def _canonical_kinds(cls, value: tuple[SubjectKind, ...]) -> tuple[SubjectKind, ...]:
        canonical = _canonical_subject_kinds(value)
        if not canonical:
            raise ValueError("subject_kinds must be non-empty")
        return canonical

    @field_validator("distinct_from_rule_ids")
    @classmethod
    def _canonical_distinct_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique_strings(value)


class DomainRoutePolicyRefV1(_FrozenModel):
    route_version: NonEmptyStr
    route_digest: LowerHexSha256
    domain_registry_ref: DomainRegistryRefV1


def compute_domain_route_policy_digest(
    route_version: str,
    domain_registry_ref: DomainRegistryRefV1,
    rules: Sequence[DomainRouteRule],
    effective_from: str,
) -> str:
    ordered = sorted(rules, key=lambda item: item.rule_id)
    return canonical_sha256(
        {
            "route_version": route_version,
            "domain_registry_ref": domain_registry_ref.model_dump(mode="json"),
            "rules": [item.model_dump(mode="json") for item in ordered],
            "effective_from": effective_from,
        }
    )


class DomainRoutePolicy(_FrozenModel):
    route_version: NonEmptyStr
    domain_registry_ref: DomainRegistryRefV1
    rules: tuple[DomainRouteRule, ...]
    effective_from: NonEmptyStr
    route_digest: LowerHexSha256

    @field_validator("rules")
    @classmethod
    def _canonical_rules(cls, value: tuple[DomainRouteRule, ...]) -> tuple[DomainRouteRule, ...]:
        seen: set[str] = set()
        for rule in value:
            if rule.rule_id in seen:
                raise ValueError(f"duplicate rule_id: {rule.rule_id}")
            seen.add(rule.rule_id)
        return tuple(sorted(value, key=lambda item: item.rule_id))

    @model_validator(mode="after")
    def _validate_distinct_refs_and_digest(self) -> DomainRoutePolicy:
        rule_ids = {rule.rule_id for rule in self.rules}
        for rule in self.rules:
            for distinct_id in rule.distinct_from_rule_ids:
                if distinct_id not in rule_ids:
                    raise ValueError(f"unknown distinct_from_rule_id: {distinct_id}")
                if distinct_id == rule.rule_id:
                    raise ValueError("route rule cannot be distinct from itself")
        expected = compute_domain_route_policy_digest(
            self.route_version,
            self.domain_registry_ref,
            self.rules,
            self.effective_from,
        )
        if self.route_digest != expected:
            raise ValueError("route_digest does not match canonical route policy payload")
        return self


__all__ = [
    "ActorContext",
    "AuthenticationContext",
    "DomainDefinitionV1",
    "DomainRegistryRefV1",
    "DomainRegistryV1",
    "DomainRoutePolicy",
    "DomainRoutePolicyRefV1",
    "DomainRouteRule",
    "DomainScope",
    "DomainScopeValue",
    "Permission",
    "Principal",
    "PrincipalKind",
    "PrincipalRecordV1",
    "Role",
    "RoleAssignmentV1",
    "RolePolicy",
    "SubjectKind",
    "compute_domain_registry_digest",
    "compute_domain_route_policy_digest",
    "compute_role_policy_digest",
]
