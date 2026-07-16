"""Exact, immutable platform registries and readiness validation.

The readiness validator imports terminal payload validators.  Keep that one
export lazy so importing the leaf registry defaults from payload decoding does
not recurse through readiness back into a partially initialized publisher.
"""

from __future__ import annotations

from importlib import import_module

from gameforge.contracts.execution_graphs import (
    AgentExecutionGraphV1,
    AgentExecutionNodeV1,
    AgentExecutionProfileSelectorV1,
    agent_execution_graph_digest,
)
from gameforge.platform.registry.components import build_readiness_component_maps
from gameforge.platform.registry.defaults import (
    ARTIFACT_PAYLOAD_SCHEMAS,
    build_builtin_registry,
)
from gameforge.platform.registry.model import (
    FROZEN_ACTIVE_RUN_KIND_IDENTITIES,
    FROZEN_PROFILE_REQUIREMENT_SHAPES,
    FROZEN_RUN_KIND_DEFINITION_DIGESTS,
    FROZEN_RUN_KIND_SHAPES,
    PlatformReadinessReport,
    ProfileRequirement,
    TrustedComponentMaps,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry

__all__ = [
    "AgentExecutionGraphV1",
    "AgentExecutionNodeV1",
    "AgentExecutionProfileSelectorV1",
    "ARTIFACT_PAYLOAD_SCHEMAS",
    "FROZEN_ACTIVE_RUN_KIND_IDENTITIES",
    "FROZEN_PROFILE_REQUIREMENT_SHAPES",
    "FROZEN_RUN_KIND_DEFINITION_DIGESTS",
    "FROZEN_RUN_KIND_SHAPES",
    "ImmutablePlatformRegistry",
    "PlatformReadinessReport",
    "PlatformReadinessValidator",
    "ProfileRequirement",
    "TrustedComponentMaps",
    "agent_execution_graph_digest",
    "build_builtin_registry",
    "build_readiness_component_maps",
]


def __getattr__(name: str) -> object:
    if name != "PlatformReadinessValidator":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(
        import_module("gameforge.platform.registry.readiness"),
        name,
    )
    globals()[name] = value
    return value
