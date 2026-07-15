"""Deterministic VersionTuple projection for the terminal publication engine.

Two distinct projections live here, both from the M4 design (§3.3 / foundations
v0.3 §5.1):

* :func:`project_domain_version_tuple` applies an ``ArtifactLineagePolicyV1``'s
  ``version_projection`` to re-derive a domain Artifact's ten-field VersionTuple
  from its *typed* parent roles plus the producer (Run frozen) tuple.  The
  publisher compares the result field-by-field with the worker's PreparedArtifact
  tuple; any divergence is fail-closed.
* :func:`project_manifest_version_tuple` applies a ``VersionTransitionPolicyV1``
  (``attempt-manifest-transition`` / ``run-manifest-transition``) across the four
  execution modes to produce a manifest Artifact's terminal tuple.

Neither function reads any ``current`` registry alias; both operate only on the
exact policy objects the caller resolved from the retained registry.
"""

from __future__ import annotations

from collections.abc import Mapping

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ArtifactLineagePolicyV1,
    VersionTransitionPolicyV1,
)
from gameforge.contracts.lineage import ExecutionIdentityV1, VersionTuple


_VERSION_FIELDS: tuple[str, ...] = tuple(VersionTuple.model_fields)
_EXECUTION_IDENTITY_FIELDS = frozenset({"prompt_version", "model_snapshot", "agent_graph_version"})


def project_domain_version_tuple(
    *,
    policy: ArtifactLineagePolicyV1,
    parent_tuples: Mapping[str, tuple[VersionTuple, ...]],
    producer_tuple: VersionTuple,
) -> VersionTuple:
    """Re-derive a child Artifact's VersionTuple from typed parents + producer.

    ``parent_tuples`` maps each ``parent_role`` to the ordered tuple of parent
    VersionTuples matched for that role.  The ``parent_role`` projection follows
    the fixed optional-single rule (``min_count=0, max_count=1``): zero parents
    means the field is null, one parent inherits its same-named field, and any
    ``equality_parent_roles`` must agree on that value.
    """

    values: dict[str, object | None] = {}
    for rule in policy.version_projection:
        field = rule.field
        if rule.source == "constant_null":
            values[field] = None
            continue
        if rule.source == "producer_value":
            values[field] = getattr(producer_tuple, field)
            continue

        # source == "parent_role": optional single-value inheritance.
        parent_role = rule.parent_role
        if parent_role is None:  # pragma: no cover - contract guarantees non-null
            raise IntegrityViolation("parent_role projection is missing its parent role")
        matched = parent_tuples.get(parent_role, ())
        if len(matched) > 1:
            raise IntegrityViolation(
                "single-value parent_role projection matched multiple parents",
                field=field,
                parent_role=parent_role,
            )
        inherited = getattr(matched[0], field) if matched else None
        for equality_role in rule.equality_parent_roles:
            for equal_parent in parent_tuples.get(equality_role, ()):
                if getattr(equal_parent, field) != inherited:
                    raise IntegrityViolation(
                        "equality parent role disagrees with the inherited field",
                        field=field,
                        parent_role=parent_role,
                        equality_parent_role=equality_role,
                    )
        values[field] = inherited

    return VersionTuple.model_validate(values)


def project_manifest_version_tuple(
    *,
    policy: VersionTransitionPolicyV1,
    manifest_scope: str,
    llm_execution_mode: str,
    frozen_tuple: VersionTuple,
    execution_identity: ExecutionIdentityV1 | None,
    cassette_ids_by_scope: Mapping[str, str],
) -> VersionTuple:
    """Apply a manifest VersionTransitionPolicy for one execution mode."""

    if policy.manifest_scope != manifest_scope:
        raise IntegrityViolation(
            "version-transition policy scope differs from the manifest scope",
            expected=manifest_scope,
            actual=policy.manifest_scope,
        )
    mode_rule = next(
        (rule for rule in policy.mode_rules if rule.llm_execution_mode == llm_execution_mode),
        None,
    )
    if mode_rule is None:  # pragma: no cover - policy covers every mode
        raise IntegrityViolation(
            "version-transition policy does not cover the execution mode",
            mode=llm_execution_mode,
        )

    values: dict[str, object | None] = {}
    for rule in mode_rule.field_rules:
        field = rule.field
        if rule.operation == "copy_frozen":
            values[field] = getattr(frozen_tuple, field)
        elif rule.operation == "set_null_no_invocation":
            values[field] = None
        elif rule.operation == "set_from_execution_identity":
            if field not in _EXECUTION_IDENTITY_FIELDS:
                raise IntegrityViolation(
                    "execution-identity projection targets an unsupported field",
                    field=field,
                )
            values[field] = _execution_identity_value(field, execution_identity)
        elif rule.operation == "set_from_exact_cassette_parent":
            scope = rule.cassette_scope
            if scope is None:  # pragma: no cover - contract guarantees the scope
                raise IntegrityViolation("cassette transition is missing its scope")
            cassette_id = cassette_ids_by_scope.get(scope)
            if cassette_id is None:
                raise IntegrityViolation(
                    "manifest transition has no exact cassette parent for its scope",
                    cassette_scope=scope,
                )
            values[field] = cassette_id
        else:  # pragma: no cover - exhaustive over TransitionOperation
            raise IntegrityViolation(
                "unknown version-transition operation", operation=rule.operation
            )

    return VersionTuple.model_validate(values)


def _execution_identity_value(field: str, identity: ExecutionIdentityV1 | None) -> str | None:
    if identity is None:
        raise IntegrityViolation(
            "execution-identity projection requires a bound execution identity",
            field=field,
        )
    if field == "prompt_version":
        return identity.prompt_projection.tuple_value
    if field == "model_snapshot":
        return identity.model_projection.tuple_value
    return identity.agent_graph_version


__all__ = [
    "project_domain_version_tuple",
    "project_manifest_version_tuple",
]
