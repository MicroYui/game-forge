from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.cassette_import import (
    LegacyImportRoutingDecisionV1,
    LegacyImportVerificationPolicyRefV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.cost import SqlCostRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _catalog_and_policy() -> tuple[ModelCatalogSnapshotV1, RoutingPolicyV1]:
    descriptor = ModelDescriptorV1(
        provider="openai",
        model_snapshot=canonical_model_snapshot_id(
            ModelSnapshot(
                provider="openai",
                model="gpt-5.6-sol",
                snapshot_tag="2026-07",
            )
        ),
        tier="best",
        capabilities=("reasoning",),
        context_limit=200_000,
        max_output_tokens=32_000,
        prompt_cache_support=True,
        status="active",
    )
    catalog_payload = {
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": NOW,
    }
    catalog = ModelCatalogSnapshotV1(
        **catalog_payload,
        catalog_digest=compute_model_catalog_digest(catalog_payload),
    )
    rule = RoutingRuleV1(
        rule_id="repair",
        task_kind="patch_repair",
        required_capabilities=("reasoning",),
        primary_model_snapshot=descriptor.model_snapshot,
        allowed_fallback_chain=(),
        budget_predicates=(),
    )
    policy_payload = {
        "policy_version": 1,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": (rule,),
        "failure_classifier_version": "failure-classifier@1",
    }
    return catalog, RoutingPolicyV1(
        **policy_payload,
        routing_policy_digest=compute_routing_policy_digest(policy_payload),
    )


@pytest.fixture
def engine(tmp_path) -> Engine:
    url = f"sqlite:///{tmp_path / 'routing.db'}"
    migrations_api.upgrade(url, "head")
    selected = get_engine(url)
    yield selected
    selected.dispose()


def _capabilities(session: Session) -> TransactionCapabilities:
    repository = SqlCostRepository(session)
    return TransactionCapabilities(
        refs=repository,
        audit=repository,
        approvals=repository,
        lineage=repository,
        object_bindings=repository,
        runs=repository,
        cost=repository,
    )


def _legacy_decision(
    catalog: ModelCatalogSnapshotV1,
    *,
    profile_digest: str,
    verification_policy_version: int,
) -> LegacyImportRoutingDecisionV1:
    return LegacyImportRoutingDecisionV1.create(
        source_wire_sha256="4" * 64,
        request_hash="sha256:" + "5" * 64,
        agent_node_id="repair-drafter",
        model_snapshot=catalog.models[0].model_snapshot,
        execution_profile_binding_digests=(profile_digest,),
        model_catalog_version=catalog.catalog_version,
        model_catalog_digest=catalog.catalog_digest,
        verification_policy=LegacyImportVerificationPolicyRefV1(
            policy_id="legacy-import",
            policy_version=verification_policy_version,
            policy_digest=str(verification_policy_version) * 64,
        ),
    )


def test_policy_requires_and_resolves_the_exact_catalog_history(engine: Engine) -> None:
    catalog, policy = _catalog_and_policy()

    with pytest.raises(IntegrityViolation, match="unavailable catalog history"):
        with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
            transaction.cost.put_routing_policy(policy)

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_model_catalog(catalog)
        transaction.cost.put_routing_policy(policy)

    with Session(engine) as session:
        repository = SqlCostRepository(session)
        assert (
            repository.get_model_catalog(catalog.catalog_version, catalog.catalog_digest) == catalog
        )
        assert (
            repository.get_routing_policy(
                policy.policy_version,
                policy.routing_policy_digest,
            )
            == policy
        )
        with pytest.raises(IntegrityViolation, match="requested exact ref"):
            repository.get_model_catalog(catalog.catalog_version, "0" * 64)
        with pytest.raises(IntegrityViolation, match="requested exact ref"):
            repository.get_routing_policy(policy.policy_version, "0" * 64)


def test_legacy_decisions_use_content_id_and_allow_same_wire_request_contexts(
    engine: Engine,
) -> None:
    catalog, _ = _catalog_and_policy()
    original = _legacy_decision(
        catalog,
        profile_digest="6" * 64,
        verification_policy_version=1,
    )
    different_policy = _legacy_decision(
        catalog,
        profile_digest="6" * 64,
        verification_policy_version=2,
    )
    different_profile = _legacy_decision(
        catalog,
        profile_digest="7" * 64,
        verification_policy_version=1,
    )
    assert (
        len({original.decision_id, different_policy.decision_id, different_profile.decision_id})
        == 3
    )

    with SqliteUnitOfWork(engine, _capabilities).begin() as transaction:
        transaction.cost.put_model_catalog(catalog)
        assert transaction.cost.put_legacy_import_routing_decision(original) == original
        assert (
            transaction.cost.put_legacy_import_routing_decision(different_policy)
            == different_policy
        )
        assert (
            transaction.cost.put_legacy_import_routing_decision(different_profile)
            == different_profile
        )
        assert transaction.cost.put_legacy_import_routing_decision(original) == original

    with Session(engine) as session:
        repository = SqlCostRepository(session)
        assert repository.get_legacy_import_routing_decision(original.decision_id) == original
        assert (
            repository.get_legacy_import_routing_decision(different_policy.decision_id)
            == different_policy
        )
        assert (
            repository.get_legacy_import_routing_decision(different_profile.decision_id)
            == different_profile
        )
