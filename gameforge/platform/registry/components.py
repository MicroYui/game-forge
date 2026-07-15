"""Key-only trusted-component maps for readiness closure of non-executing processes.

``PlatformReadinessValidator`` requires ``TrustedComponentMaps`` to close EXACTLY against
the 14 active RunKind definitions across all six component maps — but it only inspects the
KEY SET (``_require_exact_keys``); it never invokes a component. A process that must pass
the readiness probe yet never EXECUTES a Run (the API process only admits + reads; the
worker is the sole executor) therefore needs only the exact keys, not executable authority.

:func:`build_readiness_component_maps` derives every component key purely from the exact
registry and maps each to itself (a sentinel that satisfies closure). It imports no game /
agent / spine handler, so the API can thread it without pulling the worker's executor
graph into its process. A process that EXECUTES Runs must instead supply the REAL executor
instances (see ``gameforge.apps.worker.components.build_trusted_components``), which reuses
this exact key derivation as its single source of truth.
"""

from __future__ import annotations

from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.platform.registry.model import TrustedComponentMaps
from gameforge.platform.registry.repository import ImmutablePlatformRegistry


def build_readiness_component_maps(
    registry: ImmutablePlatformRegistry,
) -> TrustedComponentMaps:
    """Derive the exact 6-map component KEY-SET from the registry (each key -> itself)."""

    active = tuple(item for item in registry.list_run_kinds() if item.status == "active")
    executors: dict[str, object] = {}
    terminal_hooks: dict[str, object] = {}
    workflow_effects: dict[str, object] = {}
    permission_resolvers: dict[str, object] = {}
    for definition in active:
        executors[definition.executor_key] = definition.executor_key
        hooks = definition.terminal_hooks
        for hook in (hooks.on_success, hooks.on_failure, hooks.on_cancel, hooks.on_timeout):
            terminal_hooks[hook] = hook
        for policy in definition.outcome_policies:
            workflow_effects[policy.workflow_effect_key] = policy.workflow_effect_key
        if definition.required_permission.domain_scope == "all":
            resolver_key = registry.get_permission_resolver_key(
                RunKindRef(kind=definition.kind, version=definition.version)
            )
            if resolver_key is not None:
                permission_resolvers[resolver_key] = resolver_key

    completion_oracles: dict[str, object] = {}
    for oracle_registry in registry.completion_oracle_registries:
        for oracle in oracle_registry.definitions:
            completion_oracles[oracle.executor_key] = oracle.executor_key

    profile_handlers: dict[str, object] = {}
    for catalog in registry.list_execution_profile_catalogs():
        states = {
            (item.profile.profile_id, item.profile.version): item.state
            for item in catalog.lifecycle
        }
        for definition in catalog.definitions:
            ref = (definition.profile.profile_id, definition.profile.version)
            if states[ref] in {"active", "replay_only"}:
                profile_handlers[definition.handler_key] = definition.handler_key

    return TrustedComponentMaps(
        executors=executors,
        terminal_hooks=terminal_hooks,
        workflow_effects=workflow_effects,
        completion_oracles=completion_oracles,
        profile_handlers=profile_handlers,
        permission_domain_resolvers=permission_resolvers,
    )


__all__ = ["build_readiness_component_maps"]
