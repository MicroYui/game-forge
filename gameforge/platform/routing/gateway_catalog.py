"""Derive the versioned routing authority from one reading of the gateway.

`ExecutionVersionPlanV1` binds a run to an exact model catalog and an exact routing
policy, and the worker's router always takes a rule's primary model. So "let the
planner choose the model" is expressed here as one policy per selectable model:
picking Opus 5 means picking the policy whose every rule routes to Opus 5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import re

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import (
    GatewayModelV1,
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingPolicyV1,
    RoutingRuleV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
    compute_routing_policy_digest,
)

FAILURE_CLASSIFIER_VERSION = "failure-classifier@1"
# Policy versions are dense per catalog: bumping the catalog moves every policy to a
# fresh band, so a re-read of a changed gateway can never reuse a version whose
# content some run already froze. A deployment manifest caps at 1024 snapshots, so
# one band always holds every model of one catalog.
_POLICIES_PER_CATALOG = 10_000


@dataclass(frozen=True, slots=True)
class GatewayModelAuthoritySeed:
    """Everything a deployment must retain before it can route to these models."""

    catalog: ModelCatalogSnapshotV1
    policies: tuple[RoutingPolicyV1, ...]
    snapshots: tuple[ModelSnapshot, ...]


def gateway_provider_id(vendor: str) -> str:
    """Namespace a gateway vendor into the catalog's provider identifier."""

    provider = re.sub(r"[^a-z0-9._-]+", "-", vendor.strip().lower()).strip("-")
    if not provider:
        raise IntegrityViolation("gateway model vendor has no provider namespace", vendor=vendor)
    return provider


def gateway_model_snapshot(model: GatewayModelV1) -> ModelSnapshot:
    """The structured preimage a provider request is actually built from."""

    return ModelSnapshot(
        provider=gateway_provider_id(model.vendor),
        model=model.model,
        snapshot_tag=f"{model.served_version}@gateway",
    )


def plan_gateway_model_authority(
    models: Sequence[GatewayModelV1],
    *,
    agent_nodes: Mapping[str, Sequence[str]],
    retained_catalogs: Sequence[ModelCatalogSnapshotV1],
    created_at: datetime,
) -> GatewayModelAuthoritySeed:
    """Plan the catalog and per-model policies for what the gateway serves now.

    ``agent_nodes`` maps every Agent node a run can reach to the capabilities that
    node declares. A model the gateway serves but that cannot meet them is not
    catalogued: offering it would let a planner pick a model whose run is rejected
    the moment the plan is validated.
    """

    if not models:
        raise IntegrityViolation("gateway serves no model this deployment can route to")
    if not agent_nodes:
        raise IntegrityViolation("routing authority needs at least one task kind to cover")

    required = {capability for node in agent_nodes.values() for capability in node}
    capable = tuple(model for model in models if required.issubset(model.capabilities))
    if not capable:
        raise IntegrityViolation(
            "no model the gateway serves meets what the Agent graphs require",
            capabilities=sorted(required),
        )
    snapshots = tuple(gateway_model_snapshot(model) for model in capable)
    descriptors = tuple(
        _descriptor(model, snapshot) for model, snapshot in zip(capable, snapshots, strict=True)
    )
    catalog = _catalog(descriptors, retained_catalogs=retained_catalogs, created_at=created_at)
    return GatewayModelAuthoritySeed(
        catalog=catalog,
        policies=_policies(catalog, agent_nodes=agent_nodes),
        snapshots=tuple(sorted(snapshots, key=lambda item: (item.provider, item.model))),
    )


def _descriptor(model: GatewayModelV1, snapshot: ModelSnapshot) -> ModelDescriptorV1:
    return ModelDescriptorV1(
        provider=snapshot.provider,
        model_snapshot=canonical_model_snapshot_id(snapshot),
        tier=model.tier,
        capabilities=model.capabilities,
        context_limit=model.context_limit,
        max_output_tokens=model.max_output_tokens,
        prompt_cache_support=model.prompt_cache_support,
        status="active",
        api_flavor=model.api_flavor,
    )


def _catalog(
    descriptors: Sequence[ModelDescriptorV1],
    *,
    retained_catalogs: Sequence[ModelCatalogSnapshotV1],
    created_at: datetime,
) -> ModelCatalogSnapshotV1:
    wanted = tuple(sorted(descriptors, key=lambda item: (item.provider, item.model_snapshot)))
    for retained in retained_catalogs:
        if retained.models == wanted:
            return retained
    body = {
        "catalog_version": max((item.catalog_version for item in retained_catalogs), default=0) + 1,
        "models": wanted,
        "created_at": created_at,
    }
    return ModelCatalogSnapshotV1(**body, catalog_digest=compute_model_catalog_digest(body))


def _policies(
    catalog: ModelCatalogSnapshotV1,
    *,
    agent_nodes: Mapping[str, Sequence[str]],
) -> tuple[RoutingPolicyV1, ...]:
    base = catalog.catalog_version * _POLICIES_PER_CATALOG
    return tuple(
        _policy(
            catalog,
            policy_version=base + index + 1,
            model_snapshot=descriptor.model_snapshot,
            agent_nodes=agent_nodes,
        )
        for index, descriptor in enumerate(catalog.models)
    )


def _policy(
    catalog: ModelCatalogSnapshotV1,
    *,
    policy_version: int,
    model_snapshot: str,
    agent_nodes: Mapping[str, Sequence[str]],
) -> RoutingPolicyV1:
    body = {
        "policy_version": policy_version,
        "catalog_version": catalog.catalog_version,
        "catalog_digest": catalog.catalog_digest,
        "rules": tuple(
            RoutingRuleV1(
                rule_id=f"route:{task_kind}",
                task_kind=task_kind,
                required_capabilities=tuple(sorted(set(agent_nodes[task_kind]))),
                primary_model_snapshot=model_snapshot,
                allowed_fallback_chain=(),
                budget_predicates=(),
            )
            for task_kind in sorted(agent_nodes)
        ),
        "failure_classifier_version": FAILURE_CLASSIFIER_VERSION,
    }
    return RoutingPolicyV1(**body, routing_policy_digest=compute_routing_policy_digest(body))


__all__ = [
    "FAILURE_CLASSIFIER_VERSION",
    "GatewayModelAuthoritySeed",
    "gateway_model_snapshot",
    "gateway_provider_id",
    "plan_gateway_model_authority",
]
