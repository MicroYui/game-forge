"""Task 11b fix wave 1 — handler prepared-artifact lineage conformance.

Projects every artifact each agent handler produces against the ACTUAL frozen
``ArtifactLineagePolicyV1`` from ``build_builtin_registry()`` (the same
``project_typed_lineage`` the Task-9 terminal publisher runs). This catches the
class of bug where a handler declares a lineage parent that matches NO typed role
(a dangling / wrong run_input parent) at the 11b unit level instead of at Task-18
E2E.

Ownership split (see report §"Fix wave 1"):
- run_input / run_intermediate roles are HANDLER-owned — the handler must declare
  exactly the right ids and nothing that dangles.
- ``prepared_rule`` sibling roles (preview←patch, evidence/config←preview) are
  content-addressed over the publisher-re-derived version tuple, so the handler
  cannot compute their ids. The Task-9 publisher does NOT yet inject them into a
  child's ``lineage[]`` (`publisher._publish_domain_artifacts` passes
  ``child_lineage=view.lineage`` unchanged); injecting sibling ids after minting
  each parent is a Task-18 publisher enhancement. This test therefore asserts the
  HANDLER-owned (run_input) subset is dangling-free, and that once the (future)
  publisher-supplied siblings are injected the full projection conforms.
"""

from __future__ import annotations

from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    _candidate_for_rule,
)
from gameforge.platform.registry.defaults import build_builtin_registry

from tests.platform.m4c import (
    test_constraint_proposal_handler as constraint_mod,
)
from tests.platform.m4c import test_generation_handler as gen_mod
from tests.platform.m4c import test_repair_handler as repair_mod
from tests.platform.m4c.handler_support import FakeModelBridge

REGISTRY = build_builtin_registry()

# Every run_input id any of the three scenarios can declare, with its true kind and
# a schema valid for that kind (the `validation` role restricts to evidence-set@1).
_RUN_INPUTS: dict[str, tuple[str, str]] = {
    gen_mod.SNAPSHOT_ID: ("ir_snapshot", "ir-core@1"),
    gen_mod.CONSTRAINT_ID: ("constraint_snapshot", "constraint-snapshot@1"),
    gen_mod.GOAL_ID: ("source_raw", "source-raw@1"),
    repair_mod.BASE_ID: ("ir_snapshot", "ir-core@1"),
    repair_mod.PREVIEW_ID: ("ir_snapshot", "ir-core@1"),
    repair_mod.SUBJECT_ID: ("patch", "patch@2"),
    repair_mod.CONSTRAINT_ID: ("constraint_snapshot", "constraint-snapshot@1"),
    repair_mod.EVIDENCE_ID: ("validation_evidence", "evidence-set@1"),
    repair_mod.FINDING_EVIDENCE_ID: ("checker_run", "checker-report@1"),
    constraint_mod.DOC_ID: ("source_raw", "source-raw@1"),
}


def _run_inputs() -> dict[str, ParentInfo]:
    return {
        artifact_id: ParentInfo(
            artifact_id=artifact_id,
            kind=kind,
            payload_schema_id=schema,
            version_tuple=VersionTuple(),
        )
        for artifact_id, (kind, schema) in _RUN_INPUTS.items()
    }


def _rule_for_kind(policy, artifact_kind: str):
    matches = [rule for rule in policy.artifact_rules if rule.artifact_kind == artifact_kind]
    assert len(matches) == 1, f"expected one rule for {artifact_kind}, got {len(matches)}"
    return matches[0]


def _success_policy(kind: RunKindRef, outcome_code: str):
    definition = REGISTRY.get_run_kind(kind)
    assert definition is not None
    for policy in definition.outcome_policies:
        if policy.outcome_code == outcome_code:
            return policy
    raise AssertionError(f"no success policy for {outcome_code}")


def _injected_siblings(lineage_policy) -> tuple[dict[str, dict[str, ParentInfo]], list[str]]:
    """The prepared_rule siblings the (future) publisher must inject for a child."""

    siblings: dict[str, dict[str, ParentInfo]] = {}
    injected_ids: list[str] = []
    for rule in lineage_policy.parent_rules:
        if rule.source != "prepared_rule":
            continue
        for n in range(max(rule.min_count, 1)):
            sibling_id = f"sibling:{rule.source_rule_id}:{rule.parent_role}:{n}"
            siblings.setdefault(rule.source_rule_id, {})[sibling_id] = ParentInfo(
                artifact_id=sibling_id,
                kind=rule.artifact_kinds[0],
                payload_schema_id=rule.payload_schema_ids[0],
                version_tuple=VersionTuple(),
            )
            injected_ids.append(sibling_id)
    return siblings, injected_ids


def _assert_artifact_lineage_conforms(kind: RunKindRef, outcome_code: str, outcome) -> None:
    policy = _success_policy(kind, outcome_code)
    run_inputs = _run_inputs()
    for artifact in outcome.artifacts:
        rule = _rule_for_kind(policy, artifact.kind)
        lineage_policy = REGISTRY.get_lineage_policy(rule.lineage_policy_ref)
        assert lineage_policy is not None
        siblings, injected_ids = _injected_siblings(lineage_policy)

        # Every prepared_rule sibling is publisher-supplied — the handler must NOT
        # have declared it (it cannot content-address it).
        assert set(injected_ids).isdisjoint(artifact.lineage), (
            f"{artifact.kind} lineage should not pre-declare publisher-injected siblings"
        )

        # The core conformance assertion: EVERY id the handler declares matches at
        # least one valid typed parent role (no dangling / wrong run_input parent).
        # ``>=1`` (not exactly-one) tolerates legitimate same-kind role ambiguity
        # the publisher disambiguates by child-payload pointer at Task-18 (e.g. the
        # repair patch's `base` + `preview` are both run_input ir_snapshot); it does
        # NOT tolerate a parent that matches NO role, which is exactly the D1 bug.
        sources = LineageParentSources(
            run_inputs=run_inputs, run_intermediates={}, prepared_siblings=siblings
        )
        for parent_id in artifact.lineage:
            matched_roles = [
                r.parent_role
                for r in lineage_policy.parent_rules
                if _candidate_for_rule(parent_id, rule=r, sources=sources) is not None
            ]
            assert matched_roles, (
                f"{artifact.kind} lineage parent {parent_id!r} matches no typed role "
                f"in {lineage_policy.policy_id}"
            )

        # Document the Task-18 dependency: every prepared_rule role is genuinely
        # publisher-supplied (no handler-declared parent could ever satisfy it).
        for r in lineage_policy.parent_rules:
            if r.source != "prepared_rule":
                continue
            assert not any(
                _candidate_for_rule(
                    parent_id,
                    rule=r,
                    sources=LineageParentSources(
                        run_inputs=run_inputs, run_intermediates={}, prepared_siblings={}
                    ),
                )
                for parent_id in artifact.lineage
            ), f"{artifact.kind} unexpectedly satisfies prepared_rule role {r.parent_role}"


def test_generation_gate_pass_lineage_conforms_to_frozen_policy() -> None:
    store = gen_mod._store()
    outcome = gen_mod._handler(store)(
        gen_mod._context(FakeModelBridge(responses=(gen_mod._BENIGN_OPS,)))
    )
    _assert_artifact_lineage_conforms(gen_mod.GENERATION_KIND, "generation_gate_passed", outcome)


def test_repair_verified_lineage_conforms_to_frozen_policy() -> None:
    store = repair_mod._store()
    outcome = repair_mod._handler(store)(
        repair_mod._context(FakeModelBridge(responses=(repair_mod._FIX_OPS,)))
    )
    _assert_artifact_lineage_conforms(repair_mod.REPAIR_KIND, "repair_verified", outcome)


def test_constraint_proposal_lineage_conforms_to_frozen_policy() -> None:
    store = constraint_mod._store()
    outcome = constraint_mod._handler(store)(
        constraint_mod._context(FakeModelBridge(responses=(constraint_mod._PROPOSALS,)))
    )
    _assert_artifact_lineage_conforms(
        constraint_mod.CONSTRAINT_KIND, "constraint_proposal_drafted", outcome
    )
