"""Immutable exact-history persistence for M4 governance policy snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SessionPolicyV1,
)
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileKindV1,
    ExecutionProfileLifecycleV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainScope,
    RolePolicy,
)
from gameforge.contracts.storage import UtcClock
from gameforge.contracts.workflow import (
    ApprovalPolicyRefV1,
    ApprovalPolicyRegistryV1,
    ApprovalPolicyV1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    compute_auto_apply_policy_digest,
)
from gameforge.runtime.persistence.models import PolicySnapshotRow


_DOMAIN_REGISTRY_KIND = "domain_registry"
_DOMAIN_REGISTRY_ID = "platform_domains"
_ROLE_POLICY_KIND = "role_policy"
_ROLE_POLICY_ID = "platform_roles"
_ROUTE_POLICY_KIND = "domain_route_policy"
_ROUTE_POLICY_ID = "platform_routes"
_APPROVAL_POLICY_KIND = "approval_policy"
_APPROVAL_POLICY_ID = "platform_approvals"
_APPROVAL_POLICY_REGISTRY_KIND = "approval_policy_registry"
_APPROVAL_POLICY_REGISTRY_ID = "platform_approval_policies"
_DETERMINISTIC_ORACLE_REGISTRY_KIND = "deterministic_oracle_registry"
_DETERMINISTIC_ORACLE_REGISTRY_ID = "platform_deterministic_oracles"
_AUTO_APPLY_POLICY_KIND = "auto_apply_policy"
_AUTO_APPLY_POLICY_REGISTRY_KIND = "auto_apply_policy_registry"
_AUTO_APPLY_POLICY_REGISTRY_ID = "platform_auto_apply_policies"
_EXECUTION_PROFILE_CATALOG_KIND = "execution_profile_catalog"
_EXECUTION_PROFILE_CATALOG_ID = "platform_execution_profiles"
_EXECUTION_PROFILE_DEFINITION_KIND = "execution_profile_definition"
_LOGIN_NAME_NORMALIZATION_POLICY_KIND = "login_name_normalization_policy"
_LOGIN_NAME_NORMALIZATION_POLICY_ID = "platform_login_name_normalization"
_PASSWORD_HASH_POLICY_KIND = "password_hash_policy"
_PASSWORD_HASH_POLICY_ID = "platform_password_hash"
_SESSION_POLICY_KIND = "session_policy"
_SESSION_POLICY_ID = "platform_session"

PolicyModel = TypeVar("PolicyModel", bound=BaseModel)


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if not isinstance(now, datetime):
        raise IntegrityViolation("policy repository clock did not return a datetime")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise IntegrityViolation("policy repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_lifecycle_changed_at(
    value: str,
    *,
    profile: ProfileRefV1,
    catalog_version: int,
) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(
            "execution profile lifecycle changed_at must be a UTC timestamp",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            catalog_version=catalog_version,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise IntegrityViolation(
            "execution profile lifecycle changed_at must be a UTC timestamp",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            catalog_version=catalog_version,
        )
    return parsed.astimezone(timezone.utc)


def _canonical_model(value: PolicyModel, model_type: type[PolicyModel]) -> PolicyModel:
    if type(value) is not model_type or set(value.__dict__) != set(model_type.model_fields):
        raise IntegrityViolation("policy snapshot must be a canonical contract model")
    wire = value.model_dump(mode="json")
    try:
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation("policy snapshot is invalid") from exc
    if parsed != value or canonical_json(parsed.model_dump(mode="json")) != canonical_json(wire):
        raise IntegrityViolation("policy snapshot is noncanonical")
    return parsed


def _registry_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _validate_scope_ids(scope: DomainScope | str | None, known_ids: set[str]) -> None:
    if isinstance(scope, DomainScope):
        unknown = set(scope.domain_ids) - known_ids
        if unknown:
            raise IntegrityViolation(
                "policy references an unknown domain",
                unknown_domain_ids=sorted(unknown),
            )


def _validate_role_policy_domains(
    policy: RolePolicy,
    registry: DomainRegistryV1,
) -> None:
    known_ids = {definition.domain_id for definition in registry.definitions}
    for permissions in policy.grants.values():
        for permission in permissions:
            _validate_scope_ids(permission.domain_scope, known_ids)


def _validate_route_policy_domains(
    policy: DomainRoutePolicy,
    registry: DomainRegistryV1,
) -> None:
    known_ids = {definition.domain_id for definition in registry.definitions}
    for rule in policy.rules:
        _validate_scope_ids(rule.domain_selector, known_ids)


class SqlPolicySnapshotRepository:
    """Transaction-bound immutable registry and policy snapshot repository."""

    def __init__(self, session: Session, *, clock: UtcClock) -> None:
        self._session = session
        self._clock = clock

    def put_login_name_normalization_policy(
        self,
        policy: LoginNameNormalizationPolicyV1,
    ) -> LoginNameNormalizationPolicyV1:
        canonical = _canonical_model(policy, LoginNameNormalizationPolicyV1)
        self._put(
            document_kind=_LOGIN_NAME_NORMALIZATION_POLICY_KIND,
            document_id=_LOGIN_NAME_NORMALIZATION_POLICY_ID,
            document_version=canonical.policy_version,
            document_digest=canonical.policy_digest,
            payload_schema_version=canonical.policy_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_login_name_normalization_policy(
        self,
        *,
        policy_version: str,
        policy_digest: str,
    ) -> LoginNameNormalizationPolicyV1 | None:
        row = self._session.get(
            PolicySnapshotRow,
            (
                _LOGIN_NAME_NORMALIZATION_POLICY_KIND,
                _LOGIN_NAME_NORMALIZATION_POLICY_ID,
                policy_version,
            ),
        )
        if row is None:
            return None
        if row.document_digest != policy_digest:
            raise IntegrityViolation(
                "retained login-name normalization policy digest differs from requested exact ref",
                policy_version=policy_version,
            )
        return self._parse_row(
            row,
            model_type=LoginNameNormalizationPolicyV1,
            expected_kind=_LOGIN_NAME_NORMALIZATION_POLICY_KIND,
            expected_id=_LOGIN_NAME_NORMALIZATION_POLICY_ID,
            expected_version=policy_version,
            version_field="policy_version",
            expected_schema="login-name-normalization@1",
            digest_field="policy_digest",
        )

    def put_password_hash_policy(
        self,
        policy: PasswordHashPolicyV1,
    ) -> PasswordHashPolicyV1:
        canonical = _canonical_model(policy, PasswordHashPolicyV1)
        self._put(
            document_kind=_PASSWORD_HASH_POLICY_KIND,
            document_id=_PASSWORD_HASH_POLICY_ID,
            document_version=canonical.policy_version,
            document_digest=canonical_sha256(canonical.model_dump(mode="json")),
            payload_schema_version=canonical.policy_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_password_hash_policy(
        self,
        policy_version: str,
    ) -> PasswordHashPolicyV1 | None:
        row = self._session.get(
            PolicySnapshotRow,
            (_PASSWORD_HASH_POLICY_KIND, _PASSWORD_HASH_POLICY_ID, policy_version),
        )
        if row is None:
            return None
        policy = self._parse_row(
            row,
            model_type=PasswordHashPolicyV1,
            expected_kind=_PASSWORD_HASH_POLICY_KIND,
            expected_id=_PASSWORD_HASH_POLICY_ID,
            expected_version=policy_version,
            version_field="policy_version",
            expected_schema="password-hash-policy@1",
            digest_field=None,
        )
        if row.document_digest != canonical_sha256(policy.model_dump(mode="json")):
            raise IntegrityViolation("stored password-hash policy digest is inconsistent")
        return policy

    def put_session_policy(self, policy: SessionPolicyV1) -> SessionPolicyV1:
        canonical = _canonical_model(policy, SessionPolicyV1)
        self._put(
            document_kind=_SESSION_POLICY_KIND,
            document_id=_SESSION_POLICY_ID,
            document_version=canonical.policy_version,
            document_digest=canonical_sha256(canonical.model_dump(mode="json")),
            payload_schema_version=canonical.policy_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_session_policy(self, policy_version: str) -> SessionPolicyV1 | None:
        row = self._session.get(
            PolicySnapshotRow,
            (_SESSION_POLICY_KIND, _SESSION_POLICY_ID, policy_version),
        )
        if row is None:
            return None
        policy = self._parse_row(
            row,
            model_type=SessionPolicyV1,
            expected_kind=_SESSION_POLICY_KIND,
            expected_id=_SESSION_POLICY_ID,
            expected_version=policy_version,
            version_field="policy_version",
            expected_schema="session-policy@1",
            digest_field=None,
        )
        if row.document_digest != canonical_sha256(policy.model_dump(mode="json")):
            raise IntegrityViolation("stored session policy digest is inconsistent")
        return policy

    def put_execution_profile_catalog(
        self,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> ExecutionProfileCatalogSnapshotV1:
        canonical = _canonical_model(catalog, ExecutionProfileCatalogSnapshotV1)
        self._validate_execution_profile_lifecycle_history(canonical)
        for definition in canonical.definitions:
            self._put(
                document_kind=_EXECUTION_PROFILE_DEFINITION_KIND,
                document_id=definition.profile.profile_id,
                document_version=str(definition.profile.version),
                document_digest=execution_profile_payload_hash(definition),
                payload_schema_version=definition.definition_schema_version,
                payload=definition.model_dump(mode="json"),
            )
        self._put(
            document_kind=_EXECUTION_PROFILE_CATALOG_KIND,
            document_id=_EXECUTION_PROFILE_CATALOG_ID,
            document_version=str(canonical.catalog_version),
            document_digest=canonical.catalog_digest,
            payload_schema_version=canonical.catalog_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_execution_profile_catalog(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
    ) -> ExecutionProfileCatalogSnapshotV1 | None:
        self._validate_catalog_version(catalog_version)
        row = self._session.get(
            PolicySnapshotRow,
            (
                _EXECUTION_PROFILE_CATALOG_KIND,
                _EXECUTION_PROFILE_CATALOG_ID,
                str(catalog_version),
            ),
        )
        if row is None:
            return None
        if row.document_digest != catalog_digest:
            raise IntegrityViolation(
                "retained execution profile catalog digest differs from requested exact ref",
                catalog_version=catalog_version,
            )
        catalog = self._parse_execution_profile_catalog_row(row)
        for definition in catalog.definitions:
            retained = self._get_execution_profile_definition(definition)
            if retained != definition:
                raise IntegrityViolation(
                    "execution profile catalog definition history is inconsistent",
                    catalog_version=catalog.catalog_version,
                    profile_id=definition.profile.profile_id,
                    profile_version=definition.profile.version,
                )
        return catalog

    def list_execution_profile_catalogs(
        self,
    ) -> tuple[ExecutionProfileCatalogSnapshotV1, ...]:
        """Return every retained catalog in canonical version order.

        This is an exact-history read, not a mutable ``current`` alias.  Each row is
        revalidated through the same catalog/definition closure as point lookup, and
        the lifecycle transition chain is checked before any caller may compose a
        current-plus-history registry for replay admission.
        """

        rows = self._session.scalars(
            select(PolicySnapshotRow)
            .where(PolicySnapshotRow.document_kind == _EXECUTION_PROFILE_CATALOG_KIND)
            .order_by(PolicySnapshotRow.document_version)
        ).all()
        catalogs: list[ExecutionProfileCatalogSnapshotV1] = []
        for row in rows:
            parsed = self._parse_execution_profile_catalog_row(row)
            catalog = self.get_execution_profile_catalog(
                catalog_version=parsed.catalog_version,
                catalog_digest=parsed.catalog_digest,
            )
            if catalog is None:  # pragma: no cover - row was selected in this transaction
                raise IntegrityViolation("execution profile catalog disappeared during read")
            catalogs.append(catalog)
        catalogs.sort(key=lambda item: item.catalog_version)
        if catalogs:
            self._validate_execution_profile_lifecycle_history(catalogs[-1])
        return tuple(catalogs)

    def resolve_execution_profile(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        field_path: str,
        profile: ProfileRefV1,
        expected_profile_kind: ExecutionProfileKindV1,
    ) -> ResolvedExecutionProfileBindingV1:
        if type(profile) is not ProfileRefV1:
            raise IntegrityViolation("execution profile resolution requires an exact ProfileRef")
        try:
            canonical_profile = ProfileRefV1.model_validate(profile.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("execution profile ref is invalid") from exc
        if canonical_profile != profile:
            raise IntegrityViolation("execution profile ref is noncanonical")
        catalog = self.get_execution_profile_catalog(
            catalog_version=catalog_version,
            catalog_digest=catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation(
                "execution profile catalog history is unavailable",
                catalog_version=catalog_version,
            )
        definition, _ = self._resolve_catalog_profile(catalog, canonical_profile)
        if definition.profile_kind != expected_profile_kind:
            raise IntegrityViolation(
                "execution profile kind differs from the requested binding",
                profile_id=canonical_profile.profile_id,
                profile_version=canonical_profile.version,
            )
        try:
            binding = ResolvedExecutionProfileBindingV1(
                field_path=field_path,
                profile=canonical_profile,
                expected_profile_kind=expected_profile_kind,
                profile_payload_hash=execution_profile_payload_hash(definition),
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("execution profile binding is invalid") from exc
        self._validate_execution_profile_binding_in_catalog(binding, catalog)
        return binding

    def resolve_execution_profile_binding(
        self,
        binding: ResolvedExecutionProfileBindingV1,
    ) -> tuple[ExecutionProfileDefinitionV1, ExecutionProfileLifecycleV1]:
        if type(binding) is not ResolvedExecutionProfileBindingV1:
            raise IntegrityViolation(
                "execution profile resolution requires an exact resolved binding"
            )
        try:
            canonical_binding = ResolvedExecutionProfileBindingV1.model_validate(
                binding.model_dump(mode="json")
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("execution profile binding is invalid") from exc
        if canonical_binding != binding:
            raise IntegrityViolation("execution profile binding is noncanonical")
        catalog = self.get_execution_profile_catalog(
            catalog_version=canonical_binding.catalog_version,
            catalog_digest=canonical_binding.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation(
                "execution profile catalog history is unavailable",
                catalog_version=canonical_binding.catalog_version,
            )
        return self._validate_execution_profile_binding_in_catalog(
            canonical_binding,
            catalog,
        )

    @staticmethod
    def _validate_catalog_version(catalog_version: int) -> None:
        if (
            isinstance(catalog_version, bool)
            or not isinstance(catalog_version, int)
            or catalog_version < 1
        ):
            raise IntegrityViolation("execution profile catalog version is invalid")

    def _parse_execution_profile_catalog_row(
        self,
        row: PolicySnapshotRow,
    ) -> ExecutionProfileCatalogSnapshotV1:
        try:
            catalog_version = int(row.document_version)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored execution profile catalog metadata is invalid"
            ) from exc
        if str(catalog_version) != row.document_version or catalog_version < 1:
            raise IntegrityViolation("stored execution profile catalog metadata is invalid")
        catalog = self._parse_row(
            row,
            model_type=ExecutionProfileCatalogSnapshotV1,
            expected_kind=_EXECUTION_PROFILE_CATALOG_KIND,
            expected_id=_EXECUTION_PROFILE_CATALOG_ID,
            expected_version=row.document_version,
            version_field=None,
            expected_schema="execution-profile-catalog@1",
            digest_field="catalog_digest",
        )
        if catalog.catalog_version != catalog_version:
            raise IntegrityViolation("stored policy snapshot payload is noncanonical")
        return catalog

    def _validate_execution_profile_lifecycle_history(
        self,
        candidate: ExecutionProfileCatalogSnapshotV1,
    ) -> None:
        catalogs: dict[int, ExecutionProfileCatalogSnapshotV1] = {}
        rows = self._session.scalars(
            select(PolicySnapshotRow).where(
                PolicySnapshotRow.document_kind == _EXECUTION_PROFILE_CATALOG_KIND
            )
        ).all()
        for row in rows:
            retained = self._parse_execution_profile_catalog_row(row)
            catalogs[retained.catalog_version] = retained

        retained_candidate = catalogs.get(candidate.catalog_version)
        if retained_candidate is not None and retained_candidate != candidate:
            raise IntegrityViolation(
                "execution profile catalog version has different immutable content",
                catalog_version=candidate.catalog_version,
            )
        catalogs[candidate.catalog_version] = candidate

        history: dict[
            ProfileRefV1,
            list[tuple[int, ExecutionProfileLifecycleV1]],
        ] = {}
        for catalog_version in sorted(catalogs):
            for lifecycle in catalogs[catalog_version].lifecycle:
                history.setdefault(lifecycle.profile, []).append((catalog_version, lifecycle))

        for profile, entries in history.items():
            first = entries[0][1]
            previous_changed_at = _parse_lifecycle_changed_at(
                first.changed_at,
                profile=profile,
                catalog_version=entries[0][0],
            )
            if first.revision != 1:
                raise IntegrityViolation(
                    "execution profile lifecycle must start at revision 1",
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                    catalog_version=entries[0][0],
                )
            previous = first
            for catalog_version, current in entries[1:]:
                current_changed_at = _parse_lifecycle_changed_at(
                    current.changed_at,
                    profile=profile,
                    catalog_version=catalog_version,
                )
                changed = (current.state, current.reason_code) != (
                    previous.state,
                    previous.reason_code,
                )
                if not changed:
                    if (
                        current.revision != previous.revision
                        or current.changed_at != previous.changed_at
                    ):
                        raise IntegrityViolation(
                            "unchanged lifecycle must copy revision and changed_at exactly",
                            profile_id=profile.profile_id,
                            profile_version=profile.version,
                            catalog_version=catalog_version,
                        )
                else:
                    if current.revision != previous.revision + 1:
                        raise IntegrityViolation(
                            "lifecycle state or reason change must increment revision exactly one",
                            profile_id=profile.profile_id,
                            profile_version=profile.version,
                            catalog_version=catalog_version,
                        )
                    if current_changed_at <= previous_changed_at:
                        raise IntegrityViolation(
                            "lifecycle state or reason change must refresh changed_at "
                            "to a strictly later UTC timestamp",
                            profile_id=profile.profile_id,
                            profile_version=profile.version,
                            catalog_version=catalog_version,
                        )
                previous = current
                previous_changed_at = current_changed_at

    def _get_execution_profile_definition(
        self,
        catalog_definition: ExecutionProfileDefinitionV1,
    ) -> ExecutionProfileDefinitionV1:
        profile = catalog_definition.profile
        expected_hash = execution_profile_payload_hash(catalog_definition)
        row = self._session.get(
            PolicySnapshotRow,
            (
                _EXECUTION_PROFILE_DEFINITION_KIND,
                profile.profile_id,
                str(profile.version),
            ),
        )
        if row is None:
            raise IntegrityViolation(
                "execution profile definition history is unavailable",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            )
        if row.document_digest != expected_hash:
            raise IntegrityViolation(
                "retained execution profile definition digest differs from catalog",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            )
        retained = self._parse_row(
            row,
            model_type=ExecutionProfileDefinitionV1,
            expected_kind=_EXECUTION_PROFILE_DEFINITION_KIND,
            expected_id=profile.profile_id,
            expected_version=str(profile.version),
            version_field=None,
            expected_schema="execution-profile@1",
            digest_field=None,
        )
        if retained.profile != profile or execution_profile_payload_hash(retained) != expected_hash:
            raise IntegrityViolation(
                "stored execution profile definition payload is noncanonical",
                profile_id=profile.profile_id,
                profile_version=profile.version,
            )
        return retained

    @staticmethod
    def _resolve_catalog_profile(
        catalog: ExecutionProfileCatalogSnapshotV1,
        profile: ProfileRefV1,
    ) -> tuple[ExecutionProfileDefinitionV1, ExecutionProfileLifecycleV1]:
        definitions = [item for item in catalog.definitions if item.profile == profile]
        lifecycle = [item for item in catalog.lifecycle if item.profile == profile]
        if len(definitions) != 1 or len(lifecycle) != 1:
            raise IntegrityViolation(
                "execution profile ref is not a member or is duplicated in the exact catalog",
                catalog_version=catalog.catalog_version,
                profile_id=profile.profile_id,
                profile_version=profile.version,
            )
        return definitions[0], lifecycle[0]

    @classmethod
    def _validate_execution_profile_binding_in_catalog(
        cls,
        binding: ResolvedExecutionProfileBindingV1,
        catalog: ExecutionProfileCatalogSnapshotV1,
    ) -> tuple[ExecutionProfileDefinitionV1, ExecutionProfileLifecycleV1]:
        if (
            binding.catalog_version != catalog.catalog_version
            or binding.catalog_digest != catalog.catalog_digest
        ):
            raise IntegrityViolation(
                "execution profile binding differs from the exact catalog identity"
            )
        definition, lifecycle = cls._resolve_catalog_profile(catalog, binding.profile)
        if definition.profile_kind != binding.expected_profile_kind:
            raise IntegrityViolation(
                "execution profile kind differs from the resolved binding",
                profile_id=binding.profile.profile_id,
                profile_version=binding.profile.version,
            )
        if execution_profile_payload_hash(definition) != binding.profile_payload_hash:
            raise IntegrityViolation(
                "execution profile payload hash differs from the resolved binding",
                profile_id=binding.profile.profile_id,
                profile_version=binding.profile.version,
            )
        return definition, lifecycle

    def put_domain_registry(self, registry: DomainRegistryV1) -> DomainRegistryV1:
        canonical = _canonical_model(registry, DomainRegistryV1)
        self._put(
            document_kind=_DOMAIN_REGISTRY_KIND,
            document_id=_DOMAIN_REGISTRY_ID,
            document_version=canonical.registry_version,
            document_digest=canonical.registry_digest,
            payload_schema_version=canonical.registry_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_domain_registry(
        self,
        ref: DomainRegistryRefV1,
    ) -> DomainRegistryV1 | None:
        if not isinstance(ref, DomainRegistryRefV1):
            raise IntegrityViolation("domain registry lookup requires an exact ref")
        row = self._session.get(
            PolicySnapshotRow,
            (_DOMAIN_REGISTRY_KIND, _DOMAIN_REGISTRY_ID, ref.registry_version),
        )
        if row is None:
            return None
        if row.document_digest != ref.registry_digest:
            raise IntegrityViolation(
                "retained domain registry digest differs from requested exact ref",
                registry_version=ref.registry_version,
            )
        return self._parse_row(
            row,
            model_type=DomainRegistryV1,
            expected_kind=_DOMAIN_REGISTRY_KIND,
            expected_id=_DOMAIN_REGISTRY_ID,
            expected_version=ref.registry_version,
            version_field="registry_version",
            expected_schema="domain-registry@1",
            digest_field="registry_digest",
        )

    def put_role_policy(self, policy: RolePolicy) -> RolePolicy:
        canonical = _canonical_model(policy, RolePolicy)
        registry = self.get_domain_registry(canonical.domain_registry_ref)
        if registry is None:
            raise IntegrityViolation("role policy references an unretained domain registry")
        _validate_role_policy_domains(canonical, registry)
        self._put(
            document_kind=_ROLE_POLICY_KIND,
            document_id=_ROLE_POLICY_ID,
            document_version=canonical.policy_version,
            document_digest=canonical.policy_digest,
            payload_schema_version="role-policy@1",
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_role_policy(
        self,
        policy_version: str,
        policy_digest: str,
    ) -> RolePolicy | None:
        row = self._session.get(
            PolicySnapshotRow,
            (_ROLE_POLICY_KIND, _ROLE_POLICY_ID, policy_version),
        )
        if row is None:
            return None
        if row.document_digest != policy_digest:
            raise IntegrityViolation(
                "retained role policy digest differs from requested exact ref",
                policy_version=policy_version,
            )
        policy = self._parse_row(
            row,
            model_type=RolePolicy,
            expected_kind=_ROLE_POLICY_KIND,
            expected_id=_ROLE_POLICY_ID,
            expected_version=policy_version,
            version_field="policy_version",
            expected_schema="role-policy@1",
            digest_field="policy_digest",
        )
        registry = self.get_domain_registry(policy.domain_registry_ref)
        if registry is None:
            raise IntegrityViolation("role policy registry history is unavailable")
        _validate_role_policy_domains(policy, registry)
        return policy

    def put_domain_route_policy(self, policy: DomainRoutePolicy) -> DomainRoutePolicy:
        canonical = _canonical_model(policy, DomainRoutePolicy)
        registry = self.get_domain_registry(canonical.domain_registry_ref)
        if registry is None:
            raise IntegrityViolation("route policy references an unretained domain registry")
        _validate_route_policy_domains(canonical, registry)
        self._put(
            document_kind=_ROUTE_POLICY_KIND,
            document_id=_ROUTE_POLICY_ID,
            document_version=canonical.route_version,
            document_digest=canonical.route_digest,
            payload_schema_version="domain-route-policy@1",
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_domain_route_policy(
        self,
        ref: DomainRoutePolicyRefV1,
    ) -> DomainRoutePolicy | None:
        if not isinstance(ref, DomainRoutePolicyRefV1):
            raise IntegrityViolation("domain route policy lookup requires an exact ref")
        row = self._session.get(
            PolicySnapshotRow,
            (_ROUTE_POLICY_KIND, _ROUTE_POLICY_ID, ref.route_version),
        )
        if row is None:
            return None
        if row.document_digest != ref.route_digest:
            raise IntegrityViolation(
                "retained route policy digest differs from requested exact ref",
                route_version=ref.route_version,
            )
        policy = self._parse_row(
            row,
            model_type=DomainRoutePolicy,
            expected_kind=_ROUTE_POLICY_KIND,
            expected_id=_ROUTE_POLICY_ID,
            expected_version=ref.route_version,
            version_field="route_version",
            expected_schema="domain-route-policy@1",
            digest_field="route_digest",
        )
        if policy.domain_registry_ref != ref.domain_registry_ref:
            raise IntegrityViolation("route policy registry ref differs from requested exact ref")
        registry = self.get_domain_registry(policy.domain_registry_ref)
        if registry is None:
            raise IntegrityViolation("route policy registry history is unavailable")
        _validate_route_policy_domains(policy, registry)
        return policy

    def put_approval_policy_registry(
        self,
        registry: ApprovalPolicyRegistryV1,
    ) -> ApprovalPolicyRegistryV1:
        canonical = _canonical_model(registry, ApprovalPolicyRegistryV1)
        for policy in canonical.policies:
            self._put(
                document_kind=_APPROVAL_POLICY_KIND,
                document_id=_APPROVAL_POLICY_ID,
                document_version=policy.policy_version,
                document_digest=policy.policy_digest,
                payload_schema_version=policy.policy_schema_version,
                payload=policy.model_dump(mode="json"),
            )
        self._put(
            document_kind=_APPROVAL_POLICY_REGISTRY_KIND,
            document_id=_APPROVAL_POLICY_REGISTRY_ID,
            document_version=canonical.registry_digest,
            document_digest=canonical.registry_digest,
            payload_schema_version=canonical.registry_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_approval_policy_registry(
        self,
        registry_digest: str,
    ) -> ApprovalPolicyRegistryV1 | None:
        row = self._session.get(
            PolicySnapshotRow,
            (
                _APPROVAL_POLICY_REGISTRY_KIND,
                _APPROVAL_POLICY_REGISTRY_ID,
                registry_digest,
            ),
        )
        if row is None:
            return None
        if row.document_digest != registry_digest:
            raise IntegrityViolation("retained approval policy registry digest is inconsistent")
        registry = self._parse_row(
            row,
            model_type=ApprovalPolicyRegistryV1,
            expected_kind=_APPROVAL_POLICY_REGISTRY_KIND,
            expected_id=_APPROVAL_POLICY_REGISTRY_ID,
            expected_version=registry_digest,
            version_field=None,
            expected_schema="approval-policy-registry@1",
            digest_field="registry_digest",
        )
        for policy in registry.policies:
            retained = self.get_approval_policy(
                ApprovalPolicyRefV1(
                    policy_version=policy.policy_version,
                    policy_digest=policy.policy_digest,
                )
            )
            if retained is None or retained != policy:
                raise IntegrityViolation(
                    "approval policy registry history is incomplete",
                    registry_digest=registry.registry_digest,
                    policy_version=policy.policy_version,
                )
        return registry

    def get_approval_policy(
        self,
        ref: ApprovalPolicyRefV1,
    ) -> ApprovalPolicyV1 | None:
        if not isinstance(ref, ApprovalPolicyRefV1):
            raise IntegrityViolation("approval policy lookup requires an exact ref")
        row = self._session.get(
            PolicySnapshotRow,
            (_APPROVAL_POLICY_KIND, _APPROVAL_POLICY_ID, ref.policy_version),
        )
        if row is None:
            return None
        if row.document_digest != ref.policy_digest:
            raise IntegrityViolation(
                "retained approval policy digest differs from requested exact ref",
                policy_version=ref.policy_version,
            )
        return self._parse_row(
            row,
            model_type=ApprovalPolicyV1,
            expected_kind=_APPROVAL_POLICY_KIND,
            expected_id=_APPROVAL_POLICY_ID,
            expected_version=ref.policy_version,
            version_field="policy_version",
            expected_schema="approval-policy@1",
            digest_field="policy_digest",
        )

    def put_deterministic_oracle_registry(
        self,
        registry: DeterministicOracleRegistryV1,
    ) -> DeterministicOracleRegistryV1:
        canonical = _canonical_model(registry, DeterministicOracleRegistryV1)
        self._validate_deterministic_oracle_registry_history(canonical)
        self._put(
            document_kind=_DETERMINISTIC_ORACLE_REGISTRY_KIND,
            document_id=_DETERMINISTIC_ORACLE_REGISTRY_ID,
            document_version=canonical.registry_version,
            document_digest=canonical.registry_digest,
            payload_schema_version=canonical.registry_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_deterministic_oracle_registry(
        self,
        ref: DeterministicOracleRegistryRefV1,
    ) -> DeterministicOracleRegistryV1 | None:
        if not isinstance(ref, DeterministicOracleRegistryRefV1):
            raise IntegrityViolation("deterministic oracle registry lookup requires an exact ref")
        row = self._session.get(
            PolicySnapshotRow,
            (
                _DETERMINISTIC_ORACLE_REGISTRY_KIND,
                _DETERMINISTIC_ORACLE_REGISTRY_ID,
                ref.registry_version,
            ),
        )
        if row is None:
            return None
        if row.document_digest != ref.registry_digest:
            raise IntegrityViolation(
                "retained deterministic oracle registry digest differs from requested exact ref",
                registry_version=ref.registry_version,
            )
        registry = self._parse_row(
            row,
            model_type=DeterministicOracleRegistryV1,
            expected_kind=_DETERMINISTIC_ORACLE_REGISTRY_KIND,
            expected_id=_DETERMINISTIC_ORACLE_REGISTRY_ID,
            expected_version=ref.registry_version,
            version_field="registry_version",
            expected_schema="deterministic-oracle-registry@1",
            digest_field="registry_digest",
        )
        self._validate_deterministic_oracle_registry_history(registry)
        return registry

    def put_auto_apply_policy_registry(
        self,
        registry: AutoApplyPolicyRegistryV1,
    ) -> AutoApplyPolicyRegistryV1:
        canonical = _canonical_model(registry, AutoApplyPolicyRegistryV1)
        for policy in canonical.policies:
            self._validate_auto_apply_policy_history(policy)
            self._put(
                document_kind=_AUTO_APPLY_POLICY_KIND,
                document_id=policy.policy_id,
                document_version=policy.policy_version,
                document_digest=compute_auto_apply_policy_digest(policy),
                payload_schema_version=policy.policy_schema_version,
                payload=policy.model_dump(mode="json"),
            )
        self._put(
            document_kind=_AUTO_APPLY_POLICY_REGISTRY_KIND,
            document_id=_AUTO_APPLY_POLICY_REGISTRY_ID,
            document_version=canonical.registry_version,
            document_digest=canonical.registry_digest,
            payload_schema_version=canonical.registry_schema_version,
            payload=canonical.model_dump(mode="json"),
        )
        return canonical

    def get_auto_apply_policy_registry(
        self,
        ref: AutoApplyPolicyRegistryRefV1,
    ) -> AutoApplyPolicyRegistryV1 | None:
        if not isinstance(ref, AutoApplyPolicyRegistryRefV1):
            raise IntegrityViolation("auto-apply policy registry lookup requires an exact ref")
        row = self._session.get(
            PolicySnapshotRow,
            (
                _AUTO_APPLY_POLICY_REGISTRY_KIND,
                _AUTO_APPLY_POLICY_REGISTRY_ID,
                ref.registry_version,
            ),
        )
        if row is None:
            return None
        if row.document_digest != ref.registry_digest:
            raise IntegrityViolation(
                "retained auto-apply policy registry digest differs from requested exact ref",
                registry_version=ref.registry_version,
            )
        registry = self._parse_row(
            row,
            model_type=AutoApplyPolicyRegistryV1,
            expected_kind=_AUTO_APPLY_POLICY_REGISTRY_KIND,
            expected_id=_AUTO_APPLY_POLICY_REGISTRY_ID,
            expected_version=ref.registry_version,
            version_field="registry_version",
            expected_schema="auto-apply-policy-registry@1",
            digest_field="registry_digest",
        )
        for policy in registry.policies:
            digest = compute_auto_apply_policy_digest(policy)
            retained = self._get_auto_apply_policy_payload(
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                policy_digest=digest,
            )
            if retained is None or retained != policy:
                raise IntegrityViolation(
                    "auto-apply policy registry history is incomplete",
                    registry_version=registry.registry_version,
                    policy_id=policy.policy_id,
                    policy_version=policy.policy_version,
                )
            self._validate_auto_apply_policy_history(retained)
        return registry

    def get_auto_apply_policy(
        self,
        ref: AutoApplyPolicyRefV1,
    ) -> AutoApplyPolicyV1 | None:
        if not isinstance(ref, AutoApplyPolicyRefV1):
            raise IntegrityViolation("auto-apply policy lookup requires an exact ref")
        registry = self.get_auto_apply_policy_registry(ref.registry)
        if registry is None:
            return None
        matches = [
            policy
            for policy in registry.policies
            if (policy.policy_id, policy.policy_version) == (ref.policy_id, ref.policy_version)
        ]
        if len(matches) != 1:
            raise IntegrityViolation(
                "auto-apply policy exact ref is not a registry member",
                registry_version=ref.registry.registry_version,
                policy_id=ref.policy_id,
                policy_version=ref.policy_version,
            )
        policy = matches[0]
        if compute_auto_apply_policy_digest(policy) != ref.policy_digest:
            raise IntegrityViolation(
                "retained auto-apply policy digest differs from requested exact ref",
                policy_id=ref.policy_id,
                policy_version=ref.policy_version,
            )
        retained = self._get_auto_apply_policy_payload(
            policy_id=ref.policy_id,
            policy_version=ref.policy_version,
            policy_digest=ref.policy_digest,
        )
        if retained is None or retained != policy:
            raise IntegrityViolation(
                "auto-apply policy registry history is incomplete",
                registry_version=registry.registry_version,
                policy_id=ref.policy_id,
                policy_version=ref.policy_version,
            )
        return retained

    def _get_auto_apply_policy_payload(
        self,
        *,
        policy_id: str,
        policy_version: str,
        policy_digest: str,
    ) -> AutoApplyPolicyV1 | None:
        row = self._session.get(
            PolicySnapshotRow,
            (_AUTO_APPLY_POLICY_KIND, policy_id, policy_version),
        )
        if row is None:
            return None
        if row.document_digest != policy_digest:
            raise IntegrityViolation(
                "retained auto-apply policy digest differs from requested exact ref",
                policy_id=policy_id,
                policy_version=policy_version,
            )
        return self._parse_row(
            row,
            model_type=AutoApplyPolicyV1,
            expected_kind=_AUTO_APPLY_POLICY_KIND,
            expected_id=policy_id,
            expected_version=policy_version,
            version_field="policy_version",
            expected_schema="auto-apply-policy@1",
            digest_field=None,
        )

    def _validate_deterministic_oracle_registry_history(
        self,
        registry: DeterministicOracleRegistryV1,
    ) -> None:
        for definition in registry.definitions:
            domain_registry = self.get_domain_registry(definition.domain_registry)
            if domain_registry is None:
                raise IntegrityViolation(
                    "deterministic oracle domain registry history is unavailable",
                    oracle_id=definition.oracle_id,
                    oracle_version=definition.oracle_version,
                )
            known_ids = {item.domain_id for item in domain_registry.definitions}
            _validate_scope_ids(definition.supported_domain_scope, known_ids)

    def _validate_auto_apply_policy_history(self, policy: AutoApplyPolicyV1) -> None:
        domain_registry = self.get_domain_registry(policy.domain_registry)
        if domain_registry is None:
            raise IntegrityViolation("auto-apply policy domain registry history is unavailable")
        known_ids = {item.domain_id for item in domain_registry.definitions}
        for scope in (*policy.allowed_domain_scopes, *policy.forbidden_domain_scopes):
            _validate_scope_ids(scope, known_ids)

        oracle_registry = self.get_deterministic_oracle_registry(
            policy.deterministic_oracle_registry
        )
        if oracle_registry is None:
            raise IntegrityViolation(
                "auto-apply policy oracle registry history is unavailable",
                registry_version=policy.deterministic_oracle_registry.registry_version,
            )
        definitions = {
            (definition.oracle_id, definition.oracle_version): definition
            for definition in oracle_registry.definitions
        }
        for oracle_ref in policy.required_deterministic_oracles:
            definition = definitions.get((oracle_ref.oracle_id, oracle_ref.oracle_version))
            if definition is None:
                raise IntegrityViolation(
                    "auto-apply policy requires an oracle absent from its exact registry",
                    oracle_id=oracle_ref.oracle_id,
                    oracle_version=oracle_ref.oracle_version,
                )
            if definition.oracle_digest != oracle_ref.oracle_digest:
                raise IntegrityViolation(
                    "auto-apply policy oracle digest differs from exact registry history",
                    oracle_id=oracle_ref.oracle_id,
                    oracle_version=oracle_ref.oracle_version,
                )
            if definition.domain_registry != policy.domain_registry:
                raise IntegrityViolation(
                    "auto-apply policy and required oracle domain registries differ",
                    oracle_id=oracle_ref.oracle_id,
                    oracle_version=oracle_ref.oracle_version,
                )

    def _put(
        self,
        *,
        document_kind: str,
        document_id: str,
        document_version: str,
        document_digest: str,
        payload_schema_version: str,
        payload: dict[str, object],
    ) -> None:
        key = (document_kind, document_id, document_version)
        existing = self._session.get(PolicySnapshotRow, key)
        expected = {
            "document_digest": document_digest,
            "payload_schema_version": payload_schema_version,
            "payload": payload,
        }
        if existing is not None:
            actual = {name: getattr(existing, name) for name in expected}
            if canonical_json(actual) != canonical_json(expected):
                raise IntegrityViolation(
                    "policy snapshot identity has different immutable content",
                    document_kind=document_kind,
                    document_version=document_version,
                )
            return

        self._session.add(
            PolicySnapshotRow(
                document_kind=document_kind,
                document_id=document_id,
                document_version=document_version,
                document_digest=document_digest,
                payload_schema_version=payload_schema_version,
                payload=payload,
                created_at=_utc_text(self._clock),
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(
                "policy snapshot could not be inserted",
                document_kind=document_kind,
                document_version=document_version,
            ) from exc

    @staticmethod
    def _parse_row(
        row: PolicySnapshotRow,
        *,
        model_type: type[PolicyModel],
        expected_kind: str,
        expected_id: str,
        expected_version: str,
        version_field: str | None,
        expected_schema: str,
        digest_field: str | None,
    ) -> PolicyModel:
        if (
            row.document_kind != expected_kind
            or row.document_id != expected_id
            or row.document_version != expected_version
            or row.payload_schema_version != expected_schema
            or not isinstance(row.payload, dict)
        ):
            raise IntegrityViolation("stored policy snapshot metadata is invalid")
        try:
            parsed = model_type.model_validate(row.payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("stored policy snapshot payload is invalid") from exc
        if (
            (version_field is not None and getattr(parsed, version_field) != expected_version)
            or (digest_field is not None and getattr(parsed, digest_field) != row.document_digest)
            or canonical_json(parsed.model_dump(mode="json")) != canonical_json(row.payload)
        ):
            raise IntegrityViolation("stored policy snapshot payload is noncanonical")
        return parsed


__all__ = ["SqlPolicySnapshotRepository"]
