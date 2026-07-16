"""Generic terminal publication engine (M4c Task 9).

The package exports its public surface lazily.  ``runs.commands`` and
``runs.lifecycle`` depend only on the leaf ``platform.terminal_staging`` contracts;
eagerly importing the complete publisher here would pull the registry readiness
validator back through payload binding and make otherwise-valid import order part
of runtime correctness.
"""

from __future__ import annotations

from importlib import import_module


_EXPORT_MODULE = {
    "AgentDraftPreparedAssembler": "effects",
    "AgentDraftWorkflowPort": "effects",
    "AgentDraftWorkflowRequest": "effects",
    "ApprovalCommandAgentDraftWorkflowPort": "effects",
    "AutoApplyValidationPort": "effects",
    "AutoApplyValidationRequest": "effects",
    "WORKFLOW_EFFECTS": "effects",
    "WorkflowEffectContext": "effects",
    "apply_workflow_effect": "effects",
    "resolve_workflow_effect": "effects",
    "PlannedFindingWrite": "findings",
    "plan_finding_write": "findings",
    "LineageParentSources": "lineage",
    "ParentInfo": "lineage",
    "TypedLineage": "lineage",
    "project_typed_lineage": "lineage",
    "PublicationPlan": "planner",
    "PublicationRegistry": "planner",
    "build_publication_plan": "planner",
    "resolve_definition": "planner",
    "ArtifactPort": "publisher",
    "AuditPort": "publisher",
    "BlobStore": "publisher",
    "FindingStore": "publisher",
    "ManifestLedger": "publisher",
    "TerminalPublisher": "publisher",
    "PlanRule": "validator",
    "PreparedArtifactView": "validator",
    "RuleAllocation": "validator",
    "allocate_artifacts": "validator",
    "validate_rule_cardinality": "validator",
    "project_domain_version_tuple": "version",
    "project_manifest_version_tuple": "version",
}

__all__ = sorted(_EXPORT_MODULE)


def __getattr__(name: str) -> object:
    module_name = _EXPORT_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
