"""Source-governance platform composition (design §7.F).

Owns the versioned :class:`SourceKindRegistryV1` and the connector/trust policy that
server-assigns provenance for authenticated goals, plus the blob-first
``source_raw`` writer invoked before Run creation.
"""

from __future__ import annotations

from gameforge.platform.provenance.registry import (
    AUTHENTICATED_HUMAN_GOAL,
    BUILTIN_SOURCE_KIND_REGISTRY_VERSION,
    OPEN_SOURCE_CONTENT,
    PLANNING_DOCUMENT,
    RETRIEVAL_RESULT,
    TOOL_OUTPUT,
    TRUSTED_SERVICE_GOAL,
    build_source_kind_registry,
)
from gameforge.platform.provenance.writer import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    MintedSource,
)

__all__ = [
    "AUTHENTICATED_HUMAN_GOAL",
    "AuthenticatedGoalSourceWriter",
    "BUILTIN_SOURCE_KIND_REGISTRY_VERSION",
    "GoalProvenancePolicy",
    "MintedSource",
    "OPEN_SOURCE_CONTENT",
    "PLANNING_DOCUMENT",
    "RETRIEVAL_RESULT",
    "TOOL_OUTPUT",
    "TRUSTED_SERVICE_GOAL",
    "build_source_kind_registry",
]
