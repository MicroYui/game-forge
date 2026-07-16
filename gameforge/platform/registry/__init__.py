"""Exact, immutable platform registries and readiness validation."""

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
from gameforge.platform.registry.readiness import PlatformReadinessValidator
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
