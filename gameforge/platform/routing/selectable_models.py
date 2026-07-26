"""Offer only the models a planner can actually start a run on.

Two authorities have to agree. The gateway has to be serving the model right now —
that is where its name, limits and tier come from, read when the panel opens rather
than frozen at deploy time. And this deployment has to have retained a routing
policy whose rules send the run to it, because the worker's router always takes a
rule's primary model; without such a policy the choice cannot be expressed at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from gameforge.contracts.api import SelectableModelPageV1, SelectableModelV1
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import DependencyUnavailable
from gameforge.contracts.identity import Permission, Principal
from gameforge.contracts.routing import (
    GatewayModelV1,
    ModelCatalogSnapshotV1,
    RoutingPolicyV1,
    canonical_model_snapshot_id,
)
from gameforge.platform.routing.gateway_catalog import gateway_model_snapshot

# The models a deployment can route to are the same execution authority as the
# profiles that decide how a run executes; reading them takes the same permission.
SELECTABLE_MODEL_PERMISSION = Permission(
    action="read",
    resource_kind="execution_profile",
    domain_scope="all",
)
# The read is unfiltered and unpaged: what this deployment can route to is one
# bounded set, so its authorization binding is one constant query.
SELECTABLE_MODEL_QUERY_HASH = canonical_sha256(
    {
        "query_schema_version": "api-read-query@1",
        "api_version": "v1",
        "resource_kind": "selectable_models",
        "filters": {},
        "sort": ("model:asc",),
        "projection": "selectable-model@1",
        "page_size": None,
    }
)


class RoutingHistory(Protocol):
    """Retained routing history addressed by exact version and digest."""

    def get_routing_policy(
        self,
        policy_version: int,
        routing_policy_digest: str,
    ) -> RoutingPolicyV1 | None: ...

    def list_routing_policies(self) -> tuple[RoutingPolicyV1, ...]: ...

    def get_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...


class ReadAuthorization(Protocol):
    """Exact read authorization over the current role policy."""

    def require_singular(
        self,
        *,
        principal: Principal,
        permission: Permission | None,
        query_hash: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class SelectableModelRead:
    """One read snapshot covering both the role policy and the routing history."""

    routing: RoutingHistory
    authorization: ReadAuthorization


class SelectableModelService:
    def __init__(
        self,
        *,
        read_scope: Callable[[], AbstractContextManager[SelectableModelRead]],
        gateway_models: Callable[[], Sequence[GatewayModelV1]],
        default_policy_version: int | None,
        default_policy_digest: str | None,
    ) -> None:
        if not callable(read_scope) or not callable(gateway_models):
            raise TypeError("selectable models need a routing authority and a gateway reading")
        if (default_policy_version is None) != (default_policy_digest is None):
            raise ValueError("default routing-policy version and digest are configured together")
        self._read_scope = read_scope
        self._gateway_models = gateway_models
        self._default_policy_version = default_policy_version
        self._default_policy_digest = default_policy_digest

    def list_selectable_models(self, *, principal: Principal) -> SelectableModelPageV1:
        if self._default_policy_version is None or self._default_policy_digest is None:
            raise DependencyUnavailable(
                "this deployment has no configured routing policy to start runs on",
                component="execution_routing_policy",
            )
        with self._read_scope() as read:
            read.authorization.require_singular(
                principal=principal,
                permission=SELECTABLE_MODEL_PERMISSION,
                query_hash=SELECTABLE_MODEL_QUERY_HASH,
            )
            default = read.routing.get_routing_policy(
                self._default_policy_version,
                self._default_policy_digest,
            )
            if not isinstance(default, RoutingPolicyV1):
                raise DependencyUnavailable(
                    "the configured routing policy is not retained",
                    component="execution_routing_policy",
                )
            # Every alternative comes from the same catalog the default is bound to,
            # so a choice never silently moves the run to a different catalog.
            policies = tuple(
                policy
                for policy in read.routing.list_routing_policies()
                if (policy.catalog_version, policy.catalog_digest)
                == (default.catalog_version, default.catalog_digest)
            )
        live = {
            canonical_model_snapshot_id(gateway_model_snapshot(model)): model
            for model in self._gateway_models()
        }
        items = tuple(
            sorted(
                (
                    _offer(
                        live[model_snapshot_id],
                        policy=policy,
                        model_snapshot_id=model_snapshot_id,
                        is_default=policy.policy_version == default.policy_version,
                    )
                    for policy, model_snapshot_id in (
                        (policy, _primary_model(policy)) for policy in policies
                    )
                    if model_snapshot_id in live
                ),
                key=lambda item: item.model,
            )
        )
        return SelectableModelPageV1(items=items)


def _primary_model(policy: RoutingPolicyV1) -> str | None:
    """The one model this policy routes every task to, or None if it splits."""

    primaries = {rule.primary_model_snapshot for rule in policy.rules}
    return primaries.pop() if len(primaries) == 1 else None


def _offer(
    model: GatewayModelV1,
    *,
    policy: RoutingPolicyV1,
    model_snapshot_id: str,
    is_default: bool,
) -> SelectableModelV1:
    return SelectableModelV1(
        model=model.model,
        display_name=model.display_name,
        vendor=model.vendor,
        tier=model.tier,
        context_limit=model.context_limit,
        max_output_tokens=model.max_output_tokens,
        preview=model.preview,
        is_default=is_default,
        model_snapshot_id=model_snapshot_id,
        routing_policy_version=policy.policy_version,
        routing_policy_digest=policy.routing_policy_digest,
        model_catalog_version=policy.catalog_version,
        model_catalog_digest=policy.catalog_digest,
    )


__all__ = [
    "SELECTABLE_MODEL_PERMISSION",
    "SELECTABLE_MODEL_QUERY_HASH",
    "ReadAuthorization",
    "RoutingHistory",
    "SelectableModelRead",
    "SelectableModelService",
]
