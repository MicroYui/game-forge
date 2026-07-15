"""Immutable publication-plan selection and resolution.

The Run lifecycle service already selects the unique outcome policy per scope via
:func:`gameforge.platform.runs.lifecycle.select_outcome_policy` (a pure matcher
over the mutually-exclusive selector tuple that fails closed unless exactly one
policy matches).  This module builds the *resolved* plan on top of that: it
re-derives the RunRecord's frozen definition / outcome-policy-set digests from the
retained registry, resolves every referenced lineage / transition / runtime-parent
/ finding-output policy by exact ``{id,version,digest}`` (never the current alias),
and enforces the scope invariants (attempt-close policies carry no business
artifact rules; per-rule lineage refs follow the fixed ``{policy}/{rule}-lineage``
naming).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ArtifactLineagePolicyV1,
    FindingOutputPolicyV1,
    OutcomeArtifactPolicyV1,
    RunKindDefinition,
    RunRecord,
    RuntimeParentRuleSetV1,
    VersionTransitionPolicyV1,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.platform.publication.validator import PlanRule
from gameforge.platform.runs.lifecycle import select_outcome_policy


class PublicationRegistry(Protocol):
    """The retained-registry lookups the publisher resolves plans through."""

    def get_run_kind(self, kind: object) -> RunKindDefinition | None: ...

    def get_artifact_lineage_policy(self, ref: object) -> ArtifactLineagePolicyV1 | None: ...

    def get_version_transition_policy(self, ref: object) -> VersionTransitionPolicyV1 | None: ...

    def get_runtime_parent_rule_set(self, ref: object) -> RuntimeParentRuleSetV1 | None: ...

    def get_finding_output_policy(self, ref: object) -> FindingOutputPolicyV1 | None: ...


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    """The frozen, fully-resolved plan for one publication scope."""

    definition: RunKindDefinition
    scope: str
    policy: OutcomeArtifactPolicyV1
    transition_policy: VersionTransitionPolicyV1
    runtime_rule_set: RuntimeParentRuleSetV1
    finding_policy: FindingOutputPolicyV1 | None
    lineage_by_rule_id: dict[str, ArtifactLineagePolicyV1]
    plan_rules: tuple[PlanRule, ...]


def resolve_definition(*, registry: PublicationRegistry, run: RunRecord) -> RunKindDefinition:
    """Resolve + field-by-field re-verify the frozen Run kind definition."""

    definition = registry.get_run_kind(run.kind)
    if definition is None:
        raise IntegrityViolation("Run kind definition is not retained exactly", kind=run.kind.kind)
    if run_kind_definition_digest(definition) != run.run_kind_definition_digest:
        raise IntegrityViolation("retained Run kind definition digest differs from the RunRecord")
    if (
        outcome_policy_set_digest(run.kind, definition.outcome_policies)
        != run.outcome_policy_set_digest
    ):
        raise IntegrityViolation("retained outcome-policy-set digest differs from the RunRecord")
    return definition


def build_publication_plan(
    *,
    registry: PublicationRegistry,
    definition: RunKindDefinition,
    policy: OutcomeArtifactPolicyV1,
    scope: str,
) -> PublicationPlan:
    """Resolve every policy the terminal writer needs for one publication scope."""

    if policy.publication_scope != scope:
        raise IntegrityViolation(
            "selected policy scope differs from the requested publication scope",
            expected=scope,
            actual=policy.publication_scope,
        )
    if policy not in definition.outcome_policies:
        raise IntegrityViolation(
            "selected policy is not part of the retained Run kind definition",
            policy_id=policy.policy_id,
        )

    transition_policy = registry.get_version_transition_policy(policy.version_transition_policy_ref)
    if transition_policy is None:
        raise IntegrityViolation(
            "version-transition policy is not retained exactly",
            policy_id=policy.policy_id,
        )
    if transition_policy.manifest_scope != scope:
        raise IntegrityViolation(
            "version-transition policy manifest scope differs from the publication scope",
            policy_id=policy.policy_id,
        )

    runtime_rule_set = registry.get_runtime_parent_rule_set(definition.runtime_parent_rule_set)
    if runtime_rule_set is None:
        raise IntegrityViolation("runtime-parent rule set is not retained exactly")

    finding_policy: FindingOutputPolicyV1 | None = None
    if definition.finding_output_policy_ref is not None:
        finding_policy = registry.get_finding_output_policy(definition.finding_output_policy_ref)
        if finding_policy is None:
            raise IntegrityViolation("finding-output policy is not retained exactly")

    if scope == "attempt" and policy.artifact_rules:
        raise IntegrityViolation(
            "attempt-scope close policy must not consume business artifacts",
            policy_id=policy.policy_id,
        )

    lineage_by_rule_id: dict[str, ArtifactLineagePolicyV1] = {}
    plan_rules: list[PlanRule] = []
    for rule in policy.artifact_rules:
        expected_lineage_id = f"{policy.policy_id}/{rule.rule_id}-lineage"
        if rule.lineage_policy_ref.policy_id != expected_lineage_id:
            raise IntegrityViolation(
                "outcome rule lineage ref does not follow the fixed naming",
                rule_id=rule.rule_id,
                expected=expected_lineage_id,
                actual=rule.lineage_policy_ref.policy_id,
            )
        lineage_policy = registry.get_artifact_lineage_policy(rule.lineage_policy_ref)
        if lineage_policy is None:
            raise IntegrityViolation(
                "artifact lineage policy is not retained exactly",
                rule_id=rule.rule_id,
            )
        if lineage_policy.child_kind != rule.artifact_kind:
            raise IntegrityViolation(
                "lineage policy child kind differs from the outcome rule kind",
                rule_id=rule.rule_id,
            )
        lineage_by_rule_id[rule.rule_id] = lineage_policy
        plan_rules.append(PlanRule(scope=scope, policy_id=policy.policy_id, rule=rule))

    return PublicationPlan(
        definition=definition,
        scope=scope,
        policy=policy,
        transition_policy=transition_policy,
        runtime_rule_set=runtime_rule_set,
        finding_policy=finding_policy,
        lineage_by_rule_id=lineage_by_rule_id,
        plan_rules=tuple(plan_rules),
    )


__all__ = [
    "PublicationPlan",
    "PublicationRegistry",
    "build_publication_plan",
    "resolve_definition",
    "select_outcome_policy",
]
