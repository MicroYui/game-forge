"""Exact, immutable platform registries and readiness validation."""

from gameforge.platform.registry.defaults import build_builtin_registry
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
    "FROZEN_ACTIVE_RUN_KIND_IDENTITIES",
    "FROZEN_PROFILE_REQUIREMENT_SHAPES",
    "FROZEN_RUN_KIND_DEFINITION_DIGESTS",
    "FROZEN_RUN_KIND_SHAPES",
    "ImmutablePlatformRegistry",
    "PlatformReadinessReport",
    "PlatformReadinessValidator",
    "ProfileRequirement",
    "TrustedComponentMaps",
    "build_builtin_registry",
]
