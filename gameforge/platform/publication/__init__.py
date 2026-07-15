"""Generic terminal publication engine (M4c Task 9).

Turns a worker-submitted, non-authoritative ``PreparedRunOutcome`` into
authoritative Artifacts, Finding revisions/links, workflow effects,
RunResult/RunFailure manifests and audit inside the one transaction the Run
lifecycle service owns.  See :mod:`gameforge.platform.publication.publisher`.
"""

from __future__ import annotations

from gameforge.platform.publication.effects import (
    WORKFLOW_EFFECTS,
    WorkflowEffectContext,
    apply_workflow_effect,
    resolve_workflow_effect,
)
from gameforge.platform.publication.findings import PlannedFindingWrite, plan_finding_write
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    TypedLineage,
    project_typed_lineage,
)
from gameforge.platform.publication.planner import (
    PublicationPlan,
    PublicationRegistry,
    build_publication_plan,
    resolve_definition,
)
from gameforge.platform.publication.publisher import (
    ArtifactPort,
    AuditPort,
    BlobStore,
    FindingStore,
    ManifestLedger,
    TerminalPublisher,
)
from gameforge.platform.publication.validator import (
    PlanRule,
    PreparedArtifactView,
    RuleAllocation,
    allocate_artifacts,
    validate_rule_cardinality,
)
from gameforge.platform.publication.version import (
    project_domain_version_tuple,
    project_manifest_version_tuple,
)


__all__ = [
    "WORKFLOW_EFFECTS",
    "ArtifactPort",
    "AuditPort",
    "BlobStore",
    "FindingStore",
    "LineageParentSources",
    "ManifestLedger",
    "ParentInfo",
    "PlanRule",
    "PlannedFindingWrite",
    "PreparedArtifactView",
    "PublicationPlan",
    "PublicationRegistry",
    "RuleAllocation",
    "TerminalPublisher",
    "TypedLineage",
    "WorkflowEffectContext",
    "allocate_artifacts",
    "apply_workflow_effect",
    "build_publication_plan",
    "plan_finding_write",
    "project_domain_version_tuple",
    "project_manifest_version_tuple",
    "project_typed_lineage",
    "resolve_definition",
    "resolve_workflow_effect",
    "validate_rule_cardinality",
]
