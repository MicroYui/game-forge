from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV2,
    ModelSnapshot,
    PrefixCacheDirectiveV1,
    compute_prefix_hash,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)
from gameforge.runtime.model_router.prefix_cache import CatalogPrefixCacheAdmission


NOW = datetime(2026, 7, 14, tzinfo=UTC)
SNAPSHOT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="2026-07-14",
)
MODEL_ID = canonical_model_snapshot_id(SNAPSHOT)


def _catalog(*, supported: bool = True) -> ModelCatalogSnapshotV1:
    descriptor = ModelDescriptorV1(
        provider="openai",
        model_snapshot=MODEL_ID,
        tier="best",
        capabilities=("reasoning",),
        context_limit=100_000,
        max_output_tokens=8_000,
        prompt_cache_support=supported,
        status="active",
    )
    payload = {
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": NOW,
    }
    return ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )


def _request(*, policy_version: str = "prefix-policy@1") -> ModelRequestV2:
    messages = (
        Message(role="system", content="stable prefix"),
        Message(role="user", content="case suffix"),
    )
    return ModelRequestV2(
        model_snapshot=SNAPSHOT,
        messages=messages,
        agent_node_id="repair",
        prompt_version="repair@2",
        prefix_cache_directive=PrefixCacheDirectiveV1(
            prefix_message_count=1,
            prefix_hash=compute_prefix_hash(messages[:1]),
            provider_scope="openai",
            policy_version=policy_version,
        ),
    )


def _decision(catalog: ModelCatalogSnapshotV1) -> RoutingDecisionV1:
    from gameforge.contracts.model_router import request_hash

    request = _request()
    return RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=request_hash(request),
        rule_id="repair",
        model_snapshot=MODEL_ID,
        tier="best",
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set-1",
        fallback_from=None,
        fallback_index=0,
        policy_version=1,
        routing_policy_digest="1" * 64,
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        execution_source="online",
        decided_at=NOW,
    )


class _Catalogs:
    def __init__(self, *catalogs: ModelCatalogSnapshotV1) -> None:
        self.items = {(item.catalog_version, item.catalog_digest): item for item in catalogs}

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        return self.items.get((catalog_version, catalog_digest))


def test_prefix_cache_requires_exact_catalog_capability_and_allowed_policy() -> None:
    catalog = _catalog()
    admission = CatalogPrefixCacheAdmission(
        catalog_authority=_Catalogs(catalog),
        allowed_policy_versions=frozenset({"prefix-policy@1"}),
    )

    admission.validate(_request(), _decision(catalog))


def test_prefix_cache_rejects_unsupported_model_or_unapproved_policy() -> None:
    unsupported = _catalog(supported=False)
    admission = CatalogPrefixCacheAdmission(
        catalog_authority=_Catalogs(unsupported),
        allowed_policy_versions=frozenset({"prefix-policy@1"}),
    )
    with pytest.raises(IntegrityViolation, match="does not support"):
        admission.validate(_request(), _decision(unsupported))

    supported = _catalog()
    admission = CatalogPrefixCacheAdmission(
        catalog_authority=_Catalogs(supported),
        allowed_policy_versions=frozenset({"other-policy@1"}),
    )
    with pytest.raises(IntegrityViolation, match="not allowed"):
        admission.validate(_request(policy_version="prefix-policy@1"), _decision(supported))


def test_prefix_cache_rejects_missing_exact_catalog_history() -> None:
    catalog = _catalog()
    admission = CatalogPrefixCacheAdmission(
        catalog_authority=_Catalogs(),
        allowed_policy_versions=frozenset({"prefix-policy@1"}),
    )

    with pytest.raises(IntegrityViolation, match="catalog history"):
        admission.validate(_request(), _decision(catalog))
