from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation, InvalidStateTransition
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainRouteRule,
    DomainScope,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_domain_route_policy_digest,
    compute_role_policy_digest,
)
from gameforge.platform.routing import resolve_domain_routes
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, PolicySnapshotRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'domain-routing.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _registry(*, deprecated: tuple[str, ...] = ()) -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(
            domain_id=domain_id,
            display_name=domain_id.replace("_", " ").title(),
            tags=(category,),
            status="deprecated" if domain_id in deprecated else "active",
        )
        for domain_id, category in (
            ("economy", "numeric"),
            ("narrative", "narrative"),
            ("gacha", "gacha"),
            ("quest_graph", "structural"),
            ("crafting", "custom"),
        )
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _registry_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _route_policy(
    registry: DomainRegistryV1,
    *,
    include_custom: bool = True,
    version: str = "routes@1",
) -> DomainRoutePolicy:
    role_by_domain = {
        "economy": "numeric_designer",
        "narrative": "content_designer",
        "gacha": "gacha_compliance_reviewer",
        "quest_graph": "tooling",
    }
    if include_custom:
        role_by_domain["crafting"] = "content_designer"
    rules = tuple(
        DomainRouteRule(
            rule_id=f"route:{domain_id}",
            domain_selector=DomainScope(domain_ids=(domain_id,)),
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
            route_role=role,  # type: ignore[arg-type]
            required_action="approval.decide",
            resource_kind="approval",
            min_approvals=1,
        )
        for domain_id, role in role_by_domain.items()
    )
    ref = _registry_ref(registry)
    return DomainRoutePolicy(
        route_version=version,
        domain_registry_ref=ref,
        rules=rules,
        effective_from="2026-07-14T00:00:00Z",
        route_digest=compute_domain_route_policy_digest(
            version,
            ref,
            rules,
            "2026-07-14T00:00:00Z",
        ),
    )


def _role_policy(registry: DomainRegistryV1, *, version: str = "roles@1") -> RolePolicy:
    ref = _registry_ref(registry)
    permission = Permission(
        action="approval.decide",
        resource_kind="approval",
        domain_scope=DomainScope(domain_ids=("economy",)),
    )
    grants = {"numeric_designer": (permission,)}
    return RolePolicy(
        policy_version=version,
        domain_registry_ref=ref,
        grants=grants,
        effective_from="2026-07-14T00:00:00Z",
        policy_digest=compute_role_policy_digest(
            version,
            ref,
            grants,
            "2026-07-14T00:00:00Z",
        ),
    )


def _repository(session: Session) -> SqlPolicySnapshotRepository:
    return SqlPolicySnapshotRepository(session, clock=FrozenUtcClock(NOW))


def test_domain_registry_is_immutable_idempotent_and_resolved_by_exact_ref(
    engine: Engine,
) -> None:
    registry = _registry()
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        assert repository.put_domain_registry(registry) == registry
        assert repository.put_domain_registry(registry) == registry

    with Session(engine) as session:
        loaded = _repository(session).get_domain_registry(_registry_ref(registry))
        rows = session.scalars(select(PolicySnapshotRow)).all()
    assert loaded == registry
    assert len(rows) == 1

    changed_definitions = registry.definitions + (
        DomainDefinitionV1(
            domain_id="combat",
            display_name="Combat",
            tags=("numeric",),
            status="active",
        ),
    )
    changed = DomainRegistryV1(
        registry_version=registry.registry_version,
        definitions=changed_definitions,
        registry_digest=compute_domain_registry_digest(
            registry.registry_version,
            changed_definitions,
        ),
    )
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="immutable"):
            _repository(session).put_domain_registry(changed)

    wrong_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest="0" * 64,
    )
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="digest"):
            _repository(session).get_domain_registry(wrong_ref)


def test_role_and_route_policies_require_the_exact_retained_registry(
    engine: Engine,
) -> None:
    registry = _registry()
    ref = _registry_ref(registry)
    role_policy = _role_policy(registry)
    route_policy = _route_policy(registry)

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="registry"):
            repository.put_role_policy(role_policy)
        with pytest.raises(IntegrityViolation, match="registry"):
            repository.put_domain_route_policy(route_policy)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_domain_registry(registry)
        repository.put_role_policy(role_policy)
        repository.put_domain_route_policy(route_policy)

    with Session(engine) as session:
        repository = _repository(session)
        assert (
            repository.get_role_policy(role_policy.policy_version, role_policy.policy_digest)
            == role_policy
        )
        assert (
            repository.get_domain_route_policy(
                DomainRoutePolicyRefV1(
                    route_version=route_policy.route_version,
                    route_digest=route_policy.route_digest,
                    domain_registry_ref=ref,
                )
            )
            == route_policy
        )


@pytest.mark.parametrize(
    "document_kind",
    ["domain_registry", "role_policy", "domain_route_policy"],
)
def test_exact_history_rejects_payload_version_that_differs_from_row_key(
    engine: Engine,
    document_kind: str,
) -> None:
    registry = _registry()
    role_policy = _role_policy(registry)
    route_policy = _route_policy(registry)
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_domain_registry(registry)
        repository.put_role_policy(role_policy)
        repository.put_domain_route_policy(route_policy)

    if document_kind == "domain_registry":
        replacement = DomainRegistryV1(
            registry_version="domains@2",
            definitions=registry.definitions,
            registry_digest=compute_domain_registry_digest(
                "domains@2",
                registry.definitions,
            ),
        )
        replacement_digest = replacement.registry_digest
    elif document_kind == "role_policy":
        replacement = _role_policy(registry, version="roles@2")
        replacement_digest = replacement.policy_digest
    else:
        replacement = _route_policy(registry, version="routes@2")
        replacement_digest = replacement.route_digest

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(PolicySnapshotRow).where(PolicySnapshotRow.document_kind == document_kind)
        )
        assert row is not None
        row.document_digest = replacement_digest
        row.payload = replacement.model_dump(mode="json")

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="noncanonical"):
            if document_kind == "domain_registry":
                repository.get_domain_registry(
                    DomainRegistryRefV1(
                        registry_version=registry.registry_version,
                        registry_digest=replacement_digest,
                    )
                )
            elif document_kind == "role_policy":
                repository.get_role_policy(
                    role_policy.policy_version,
                    replacement_digest,
                )
            else:
                repository.get_domain_route_policy(
                    DomainRoutePolicyRefV1(
                        route_version=route_policy.route_version,
                        route_digest=replacement_digest,
                        domain_registry_ref=_registry_ref(registry),
                    )
                )


def test_policy_publication_rejects_unknown_domain_ids(engine: Engine) -> None:
    registry = _registry()
    ref = _registry_ref(registry)
    unknown = Permission(
        action="approval.decide",
        resource_kind="approval",
        domain_scope=DomainScope(domain_ids=("missing-domain",)),
    )
    grants = {"tooling": (unknown,)}
    policy = RolePolicy(
        policy_version="roles@unknown",
        domain_registry_ref=ref,
        grants=grants,
        effective_from="2026-07-14T00:00:00Z",
        policy_digest=compute_role_policy_digest(
            "roles@unknown", ref, grants, "2026-07-14T00:00:00Z"
        ),
    )
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_domain_registry(registry)
        with pytest.raises(IntegrityViolation, match="unknown domain"):
            repository.put_role_policy(policy)


def test_configured_routes_cover_all_requested_domains_and_support_new_games() -> None:
    registry = _registry()
    policy = _route_policy(registry)

    rules = resolve_domain_routes(
        registry=registry,
        policy=policy,
        subject_kind="patch",
        domain_scope=DomainScope(
            domain_ids=("economy", "narrative", "gacha", "quest_graph", "crafting")
        ),
    )

    assert {rule.rule_id: rule.route_role for rule in rules} == {
        "route:crafting": "content_designer",
        "route:economy": "numeric_designer",
        "route:gacha": "gacha_compliance_reviewer",
        "route:narrative": "content_designer",
        "route:quest_graph": "tooling",
    }


def test_route_resolution_fails_closed_for_missing_or_deprecated_domains() -> None:
    registry = _registry()
    with pytest.raises(IntegrityViolation, match="complete domain scope"):
        resolve_domain_routes(
            registry=registry,
            policy=_route_policy(registry, include_custom=False),
            subject_kind="patch",
            domain_scope=DomainScope(domain_ids=("economy", "crafting")),
        )

    deprecated = _registry(deprecated=("gacha",))
    with pytest.raises(InvalidStateTransition, match="deprecated"):
        resolve_domain_routes(
            registry=deprecated,
            policy=_route_policy(deprecated),
            subject_kind="patch",
            domain_scope=DomainScope(domain_ids=("gacha",)),
        )

    # Historical exact snapshots remain readable; only new resource routing is blocked.
    assert (
        next(item for item in deprecated.definitions if item.domain_id == "gacha").status
        == "deprecated"
    )
