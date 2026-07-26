"""Turn what the gateway serves into the versioned authority a run can freeze.

A run binds to an exact model catalog and an exact routing policy. The planner who
starts it should be able to say "use Opus 5 this time" — which, in these contracts,
means selecting a routing policy whose rule sends this task to Opus 5. So one
gateway reading yields one catalog and one policy per selectable model.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.routing import (
    GatewayModelV1,
    canonical_model_snapshot_id,
    validate_policy_catalog_closure,
)
from gameforge.platform.routing.gateway_catalog import (
    gateway_model_snapshot,
    plan_gateway_model_authority,
)

_CREATED_AT = datetime(2026, 7, 26, tzinfo=UTC)
_AGENT_NODES = {"extraction": ("reasoning",), "generation": ("reasoning",)}


def _model(
    name: str,
    *,
    vendor: str = "OpenAI",
    flavor: str = "responses",
    context: int = 1_050_000,
    output: int = 128_000,
) -> GatewayModelV1:
    return GatewayModelV1(
        model=name,
        display_name=name.upper(),
        vendor=vendor,
        served_version=name,
        tier="powerful",
        api_flavor=flavor,
        capabilities=("reasoning", "tool_calls"),
        context_limit=context,
        max_output_tokens=output,
        prompt_cache_support=True,
        preview=False,
    )


_SOL = _model("gpt-5.6-sol")
_OPUS = _model(
    "claude-opus-5",
    vendor="Anthropic",
    flavor="anthropic_messages",
    context=1_000_000,
    output=64_000,
)


def test_the_vendor_becomes_a_provider_namespace() -> None:
    assert gateway_model_snapshot(_model("gpt-5-mini", vendor="Azure OpenAI")).provider == (
        "azure-openai"
    )
    assert gateway_model_snapshot(_OPUS).provider == "anthropic"


def test_a_snapshot_pins_the_version_the_gateway_serves() -> None:
    snapshot = gateway_model_snapshot(_OPUS)

    assert snapshot.model == "claude-opus-5"
    assert snapshot.snapshot_tag == "claude-opus-5@gateway"


def test_every_callable_model_is_catalogued_with_the_surface_it_serves() -> None:
    seed = plan_gateway_model_authority(
        (_SOL, _OPUS),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=_CREATED_AT,
    )

    assert seed.catalog.catalog_version == 1
    catalogued = {descriptor.model_snapshot: descriptor for descriptor in seed.catalog.models}
    sol_id = canonical_model_snapshot_id(gateway_model_snapshot(_SOL))
    opus_id = canonical_model_snapshot_id(gateway_model_snapshot(_OPUS))
    assert set(catalogued) == {sol_id, opus_id}
    assert catalogued[sol_id].api_flavor == "responses"
    assert catalogued[opus_id].api_flavor == "anthropic_messages"
    assert catalogued[opus_id].context_limit == 1_000_000
    assert catalogued[opus_id].tier == "powerful"
    assert all(descriptor.status == "active" for descriptor in seed.catalog.models)
    # The deployment needs the structured preimage of every catalogued identity.
    assert {snapshot.model for snapshot in seed.snapshots} == {"gpt-5.6-sol", "claude-opus-5"}


def test_each_model_gets_a_policy_that_sends_every_task_to_it() -> None:
    seed = plan_gateway_model_authority(
        (_SOL, _OPUS),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=_CREATED_AT,
    )

    by_model = {policy: policy.rules[0].primary_model_snapshot for policy in seed.policies}
    assert len(seed.policies) == 2
    for policy in seed.policies:
        validate_policy_catalog_closure(policy, seed.catalog)
        assert {rule.task_kind for rule in policy.rules} == set(_AGENT_NODES)
        # Every rule carries what its Agent node declares; plan validation rejects a
        # rule that requires less than the node it routes.
        assert all(rule.required_capabilities == ("reasoning",) for rule in policy.rules)
        # One model per policy: a planner who picked Opus 5 gets Opus 5 or a failure,
        # never a silent substitution.
        assert {rule.primary_model_snapshot for rule in policy.rules} == {by_model[policy]}
        assert all(rule.allowed_fallback_chain == () for rule in policy.rules)
    assert set(by_model.values()) == {
        canonical_model_snapshot_id(gateway_model_snapshot(_SOL)),
        canonical_model_snapshot_id(gateway_model_snapshot(_OPUS)),
    }


def test_a_model_that_cannot_meet_the_graphs_is_not_offered() -> None:
    """A run planned onto it would be rejected the moment its plan is validated."""

    plain = _model("claude-haiku-4-5", vendor="Anthropic", flavor="anthropic_messages")
    plain = plain.model_copy(update={"capabilities": ("tool_calls",)})

    seed = plan_gateway_model_authority(
        (_SOL, plain),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=_CREATED_AT,
    )

    assert [descriptor.model_snapshot for descriptor in seed.catalog.models] == [
        canonical_model_snapshot_id(gateway_model_snapshot(_SOL))
    ]
    assert len(seed.policies) == 1


def test_a_gateway_serving_nothing_capable_fails_closed() -> None:
    plain = _model("claude-haiku-4-5").model_copy(update={"capabilities": ("tool_calls",)})

    with pytest.raises(IntegrityViolation, match="Agent graphs require"):
        plan_gateway_model_authority(
            (plain,),
            agent_nodes=_AGENT_NODES,
            retained_catalogs=(),
            created_at=_CREATED_AT,
        )


def test_policy_versions_never_collide_across_catalog_versions() -> None:
    first = plan_gateway_model_authority(
        (_SOL,),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=_CREATED_AT,
    )
    second = plan_gateway_model_authority(
        (_SOL, _OPUS),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(first.catalog,),
        created_at=_CREATED_AT,
    )

    assert second.catalog.catalog_version == 2
    assert not set(policy.policy_version for policy in first.policies) & set(
        policy.policy_version for policy in second.policies
    )


def test_an_unchanged_gateway_reuses_the_catalog_a_run_already_froze() -> None:
    first = plan_gateway_model_authority(
        (_SOL, _OPUS),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=_CREATED_AT,
    )
    again = plan_gateway_model_authority(
        (_OPUS, _SOL),
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(first.catalog,),
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    # Restarting the service must not strand every run bound to catalog 1.
    assert again.catalog == first.catalog
    assert again.policies == first.policies


def test_seeding_without_a_task_to_route_fails_closed() -> None:
    with pytest.raises(IntegrityViolation, match="task kind"):
        plan_gateway_model_authority(
            (_SOL,),
            agent_nodes={},
            retained_catalogs=(),
            created_at=_CREATED_AT,
        )


def test_seeding_without_a_callable_model_fails_closed() -> None:
    with pytest.raises(IntegrityViolation, match="model"):
        plan_gateway_model_authority(
            (),
            agent_nodes=_AGENT_NODES,
            retained_catalogs=(),
            created_at=_CREATED_AT,
        )
