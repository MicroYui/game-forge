"""What a planner may actually pick when starting an AI run.

Two authorities have to agree before a model can be offered: the gateway has to
be serving it right now, and this deployment has to have retained a routing policy
that sends the run to it. Offering anything else means the planner picks a model
and the run is rejected — or worse, silently executes on a different one.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import re

import pytest

from gameforge.contracts.errors import DependencyUnavailable, Forbidden
from gameforge.contracts.identity import Principal
from gameforge.contracts.routing import GatewayModelV1, canonical_model_snapshot_id
from gameforge.platform.routing.gateway_catalog import (
    gateway_model_snapshot,
    plan_gateway_model_authority,
)
from gameforge.platform.routing.selectable_models import (
    SelectableModelRead,
    SelectableModelService,
)

_AGENT_NODES = {"extraction": ("reasoning",), "generation": ("reasoning",)}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _model(name: str, *, vendor: str, flavor: str, tier: str = "powerful") -> GatewayModelV1:
    return GatewayModelV1(
        model=name,
        display_name=name.upper(),
        vendor=vendor,
        served_version=name,
        tier=tier,
        api_flavor=flavor,
        capabilities=("reasoning", "tool_calls"),
        context_limit=1_000_000,
        max_output_tokens=64_000,
        prompt_cache_support=True,
        preview=name.endswith("-preview"),
    )


_SOL = _model("gpt-5.6-sol", vendor="OpenAI", flavor="responses")
_OPUS = _model("claude-opus-5", vendor="Anthropic", flavor="anthropic_messages")
_GEMINI = _model("gemini-3.6-flash", vendor="Google", flavor="chat_completions", tier="versatile")


def _seed(*models: GatewayModelV1):
    return plan_gateway_model_authority(
        models,
        agent_nodes=_AGENT_NODES,
        retained_catalogs=(),
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )


class _Authority:
    def __init__(self, seed) -> None:
        self._seed = seed

    def get_routing_policy(self, policy_version: int, routing_policy_digest: str):
        for policy in self._seed.policies:
            if (
                policy.policy_version == policy_version
                and policy.routing_policy_digest == routing_policy_digest
            ):
                return policy
        return None

    def list_routing_policies(self):
        return self._seed.policies

    def get_model_catalog(self, catalog_version: int, catalog_digest: str):
        catalog = self._seed.catalog
        if (catalog.catalog_version, catalog.catalog_digest) == (catalog_version, catalog_digest):
            return catalog
        return None


class _Reads:
    """As strict as the real read authorization about what it is handed."""

    def __init__(self) -> None:
        self.denied = False

    def require_singular(self, *, principal, permission, query_hash):
        del principal
        if _SHA256.fullmatch(query_hash) is None:
            raise ValueError("query_hash must be a lowercase SHA-256 digest")
        if self.denied:
            raise Forbidden(
                "current principal lacks exact read permission",
                action=permission.action,
                resource_kind=permission.resource_kind,
            )
        return None


def _read_scope(seed, reads: _Reads):
    read = SelectableModelRead(routing=_Authority(seed), authorization=reads)

    @contextmanager
    def scope():
        yield read

    return scope


def _service(seed, *, live, reads: _Reads | None = None) -> SelectableModelService:
    default = seed.policies[0]
    return SelectableModelService(
        read_scope=_read_scope(seed, reads or _Reads()),
        gateway_models=lambda: live,
        default_policy_version=default.policy_version,
        default_policy_digest=default.routing_policy_digest,
    )


def _principal() -> Principal:
    return Principal(
        id="human:planner",
        kind="human",
        display_name="Planner",
        status="active",
        revision=1,
        credential_epoch=0,
        authz_revision=0,
        roles=(),
    )


def test_a_model_is_offered_with_the_exact_policy_that_routes_to_it() -> None:
    seed = _seed(_SOL, _OPUS)
    page = _service(seed, live=(_SOL, _OPUS)).list_selectable_models(principal=_principal())

    by_model = {item.model: item for item in page.items}
    assert set(by_model) == {"gpt-5.6-sol", "claude-opus-5"}
    sol = by_model["gpt-5.6-sol"]
    assert sol.display_name == "GPT-5.6-SOL"
    assert sol.vendor == "OpenAI"
    assert sol.tier == "powerful"
    assert sol.context_limit == 1_000_000
    assert sol.model_snapshot_id == canonical_model_snapshot_id(gateway_model_snapshot(_SOL))
    policy = next(
        item
        for item in seed.policies
        if item.rules[0].primary_model_snapshot == sol.model_snapshot_id
    )
    assert sol.routing_policy_version == policy.policy_version
    assert sol.routing_policy_digest == policy.routing_policy_digest
    assert sol.model_catalog_version == seed.catalog.catalog_version


def test_exactly_one_model_is_the_one_a_run_starts_on() -> None:
    seed = _seed(_SOL, _OPUS)
    page = _service(seed, live=(_SOL, _OPUS)).list_selectable_models(principal=_principal())

    defaults = [item.model for item in page.items if item.is_default]
    assert len(defaults) == 1
    default_policy = seed.policies[0]
    assert defaults == [
        next(
            model.model
            for model in (_SOL, _OPUS)
            if canonical_model_snapshot_id(gateway_model_snapshot(model))
            == default_policy.rules[0].primary_model_snapshot
        )
    ]


def test_a_model_the_gateway_stopped_serving_is_not_offered() -> None:
    seed = _seed(_SOL, _OPUS)

    # The default is the one the gateway dropped: the alternative is still
    # offerable, but nothing is preselected, so the planner has to choose.
    page = _service(seed, live=(_SOL,)).list_selectable_models(principal=_principal())

    assert [item.model for item in page.items] == ["gpt-5.6-sol"]
    assert [item.is_default for item in page.items] == [False]


def test_a_model_this_deployment_cannot_route_to_is_not_offered() -> None:
    # The gateway grew a model after the catalog was retained; picking it would
    # bind a run to a policy that does not exist.
    seed = _seed(_SOL, _OPUS)

    page = _service(seed, live=(_SOL, _OPUS, _GEMINI)).list_selectable_models(
        principal=_principal()
    )

    assert [item.model for item in page.items] == ["claude-opus-5", "gpt-5.6-sol"]


def test_models_are_offered_in_a_stable_order() -> None:
    seed = _seed(_SOL, _OPUS, _GEMINI)

    page = _service(seed, live=(_GEMINI, _SOL, _OPUS)).list_selectable_models(
        principal=_principal()
    )

    assert [item.model for item in page.items] == [
        "claude-opus-5",
        "gemini-3.6-flash",
        "gpt-5.6-sol",
    ]


def test_reading_requires_the_same_permission_as_the_rest_of_execution_authority() -> None:
    seed = _seed(_SOL)
    reads = _Reads()
    reads.denied = True

    with pytest.raises(Forbidden):
        _service(seed, live=(_SOL,), reads=reads).list_selectable_models(principal=_principal())


def test_a_deployment_without_a_configured_default_fails_closed() -> None:
    seed = _seed(_SOL)
    service = SelectableModelService(
        read_scope=_read_scope(seed, _Reads()),
        gateway_models=lambda: (_SOL,),
        default_policy_version=None,
        default_policy_digest=None,
    )

    with pytest.raises(DependencyUnavailable):
        service.list_selectable_models(principal=_principal())


def test_a_configured_default_that_is_not_retained_fails_closed() -> None:
    seed = _seed(_SOL)
    service = SelectableModelService(
        read_scope=_read_scope(seed, _Reads()),
        gateway_models=lambda: (_SOL,),
        default_policy_version=seed.policies[0].policy_version,
        default_policy_digest="f" * 64,
    )

    with pytest.raises(DependencyUnavailable):
        service.list_selectable_models(principal=_principal())
