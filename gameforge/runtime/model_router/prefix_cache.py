"""Admission guard for provider prefix-cache directives."""

from __future__ import annotations

from typing import Protocol

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelRequestV2
from gameforge.contracts.routing import ModelCatalogSnapshotV1, RoutingDecisionV1


class ModelCatalogAuthority(Protocol):
    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...


class CatalogPrefixCacheAdmission:
    """Require both retained model capability and an explicit policy allowlist."""

    def __init__(
        self,
        *,
        catalog_authority: ModelCatalogAuthority,
        allowed_policy_versions: frozenset[str],
    ) -> None:
        self._catalogs = catalog_authority
        self._allowed_policies = allowed_policy_versions

    def validate(self, request: ModelRequestV2, decision: RoutingDecisionV1) -> None:
        directive = request.prefix_cache_directive
        if directive is None:
            return
        if directive.policy_version not in self._allowed_policies:
            raise IntegrityViolation(
                "prefix cache policy version is not allowed",
                policy_version=directive.policy_version,
            )
        catalog = self._catalogs.get_model_catalog(
            decision.catalog_version,
            decision.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("prefix cache exact model catalog history is unavailable")
        descriptor = next(
            (item for item in catalog.models if item.model_snapshot == decision.model_snapshot),
            None,
        )
        if descriptor is None or descriptor.tier != decision.tier:
            raise IntegrityViolation("prefix cache routing decision model is absent from catalog")
        if descriptor.status != "active" or not descriptor.prompt_cache_support:
            raise IntegrityViolation("selected model does not support provider prefix caching")
        if descriptor.provider != directive.provider_scope:
            raise IntegrityViolation("prefix cache provider differs from retained model catalog")


__all__ = ["CatalogPrefixCacheAdmission", "ModelCatalogAuthority"]
