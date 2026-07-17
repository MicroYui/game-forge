"""Terminal defense for Task 12 profile-selected Playtest action authority."""

from __future__ import annotations

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.playtest import PlaytestTraceV1, bind_exact_playtest_trace_bytes
from gameforge.platform.playtest_payload_schemas import (
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.run_handlers.playtest import derive_episode_seed
from tests.platform.m4c.handler_support import FakeModelBridge
from tests.platform.m4c.test_playtest_handler import (
    _FakeRunner,
    _context,
    _handler,
    _observe_outcome,
    _store,
    _trace_of,
)
from tests.platform.m4c.test_terminal_publisher import (
    _Artifacts,
    _Audit,
    _Blobs,
    _Findings,
    _Ledger,
    _publisher,
)


def _terminal_authority(*, interaction_mode: str = "autonomous"):
    registry = build_builtin_registry()
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    environment_profile = ProfileRefV1(profile_id="builtin.environment", version=1)
    planner_profile = ProfileRefV1(profile_id="builtin.playtest_planner", version=2)
    environment_binding = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/environment_profile",
        profile=environment_profile,
        expected_profile_kind="environment",
    )
    planner_binding = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/planner_policy",
        profile=planner_profile,
        expected_profile_kind="playtest_planner",
    )
    context = _context(FakeModelBridge(responses=()))
    params = context.payload.params.model_copy(
        update={
            "environment_profile": environment_profile,
            "planner_policy": planner_profile,
            "interaction_mode": interaction_mode,
        }
    )
    envelope = context.payload.model_copy(
        update={
            "params": params,
            "execution_profile_catalog_version": catalog.catalog_version,
            "execution_profile_catalog_digest": catalog.catalog_digest,
            "resolved_profiles": (environment_binding, planner_binding),
        }
    )
    run = context.run.model_copy(update={"payload": envelope})
    return registry, run, environment_binding, planner_binding


def _forged_trace(*, action: dict[str, object], interaction_mode: str) -> dict[str, object]:
    store = _store()
    context = _context(FakeModelBridge(responses=()))
    outcome = _handler(store, _FakeRunner(_observe_outcome()))(context)
    trace = _trace_of(store, outcome).model_dump(mode="json")
    _, _, environment_binding, planner_binding = _terminal_authority()
    trace["environment_profile"] = environment_binding.profile.model_dump(mode="json")
    trace["planner_policy"] = planner_binding.profile.model_dump(mode="json")
    trace["interaction_mode"] = interaction_mode
    trace["execution_envelope"]["planner_profile_payload_hash"] = (
        planner_binding.profile_payload_hash
    )
    trace["episodes"][0]["seed_binding"]["profile"] = environment_binding.profile.model_dump(
        mode="json"
    )
    episode = trace["episodes"][0]
    derived_seed = derive_episode_seed(
        root_seed=trace["seed"],
        run_kind=RunKindRef.model_validate(episode["seed_binding"]["run_kind"]),
        environment_profile=environment_binding.profile,
        task_suite_artifact_id=trace["task_suite_artifact_id"],
        episode_id=episode["episode_id"],
    )
    episode["seed"] = derived_seed
    episode["seed_binding"]["seed"] = derived_seed
    trace["episodes"][0]["action_trace"][0]["action"] = action
    trace["execution_envelope"]["total_action_trace_bytes"] = sum(
        len(canonical_json(episode["action_trace"]).encode("utf-8"))
        for episode in trace["episodes"]
    )
    return bind_exact_playtest_trace_bytes(trace)


@pytest.mark.parametrize(
    ("action", "interaction_mode", "message"),
    (
        ({"kind": "wait", "ticks": -1}, "autonomous", "exact schema"),
        (
            {"kind": "attack", "target_id": "monster:1"},
            "bounded_choice",
            "environment authority",
        ),
    ),
)
def test_terminal_publisher_revalidates_action_schema_and_interaction_mode(
    action: dict[str, object],
    interaction_mode: str,
    message: str,
) -> None:
    registry, run, _, _ = _terminal_authority(interaction_mode=interaction_mode)
    payload = _forged_trace(action=action, interaction_mode=interaction_mode)
    # Prove the forged trace still satisfies the generic trace envelope; the
    # environment action authority is the boundary expected to reject it.
    PlaytestTraceV1.model_validate(payload)
    publisher = _publisher(
        registry,
        _Artifacts(),
        _Blobs(),
        _Findings(),
        _Ledger(),
        _Audit(),
        playtest_payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=build_builtin_playtest_payload_validators(),
        ),
    )

    with pytest.raises(IntegrityViolation, match=message):
        publisher._validate_playtest_profile_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="playtest-trace@1",
            payload=payload,
            producer_identity=None,
        )


def test_terminal_publisher_binds_trace_interaction_mode_to_the_frozen_run() -> None:
    registry, run, _, _ = _terminal_authority(interaction_mode="bounded_choice")
    payload = _forged_trace(
        action={"kind": "attack", "target_id": "monster:1"},
        interaction_mode="autonomous",
    )
    PlaytestTraceV1.model_validate(payload)
    publisher = _publisher(
        registry,
        _Artifacts(),
        _Blobs(),
        _Findings(),
        _Ledger(),
        _Audit(),
        playtest_payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=build_builtin_playtest_payload_validators(),
        ),
    )

    with pytest.raises(IntegrityViolation, match="frozen Run inputs"):
        publisher._validate_playtest_profile_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="playtest-trace@1",
            payload=payload,
            producer_identity=None,
        )
