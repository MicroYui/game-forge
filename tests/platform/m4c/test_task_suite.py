"""Task 12a — ``task_suite_deriver@1`` (DETERMINISTIC scenario/suite derivation).

Turns a gated preview Snapshot + its config-export package + a completion-oracle
registry ref into ONE non-empty ``TaskSuiteV1`` and its N sibling
``ScenarioSpecV1`` artifacts (one per completable quest chain). No LLM, no seed,
no findings; same input ⇒ byte-identical outcome. The GAME-SPECIFIC scenario
shaping + the deterministic oracle binding come from the injected apps/worker
ports; the reset-schema is PROFILE-SELECTED from the environment contract.
"""

from __future__ import annotations

import json

from gameforge.apps.worker.completion_oracles import ALL_QUESTS_COMPLETED_ORACLE
from gameforge.apps.worker.task_suite import AureusScenarioShaper
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    ProfileRefV1,
    RunKindRef,
)
from gameforge.contracts.jobs import PreparedRunResult, TaskSuiteDerivePayloadV1
from gameforge.contracts.lineage import artifact_id_v2_for
from gameforge.contracts.playtest import (
    CompletionOracleDefinitionV1,
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    ScenarioSpecV1,
    TaskSuiteV1,
    compute_completion_oracle_registry_digest,
    resolve_completion_oracle,
)
from gameforge.platform.run_handlers.task_suite import TaskSuiteDeriveHandler
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
)

TASK_SUITE_KIND = RunKindRef(kind="task_suite.derive", version=1)
PREVIEW_ID = "artifact:preview"
CONFIG_ID = "artifact:config"
CONSTRAINT_ID = "artifact:constraint"

_ENV_PROFILE = ProfileRefV1(profile_id="builtin.environment", version=1)
_DERIVATION_PROFILE = ProfileRefV1(profile_id="builtin.task_suite_derivation", version=1)
_ENV_CONTRACT = EnvironmentContractDescriptorV1(
    env_contract_version="generic-agent-env@1",
    reset_schema_id="generic-env-reset@1",
    action_schema_id="generic-env-action@1",
    observation_schema_id="generic-env-observation@1",
)


def _workbook() -> dict[str, list[dict]]:
    return {
        "npcs": [{"npc_id": "giver_a", "name": "A"}, {"npc_id": "giver_b", "name": "B"}],
        "quests": [
            {"quest_id": "quest_a", "giver": "giver_a", "region": "town"},
            {"quest_id": "quest_b", "giver": "giver_b", "region": "field"},
        ],
        "quest_steps": [
            {
                "step_id": "qa0",
                "quest_id": "quest_a",
                "kind": "talk",
                "target": "giver_a",
                "order": 0,
            },
            {
                "step_id": "qa1",
                "quest_id": "quest_a",
                "kind": "turn_in",
                "target": "giver_a",
                "order": 1,
            },
            {
                "step_id": "qb0",
                "quest_id": "quest_b",
                "kind": "talk",
                "target": "giver_b",
                "order": 0,
            },
        ],
    }


def _preview_bytes() -> bytes:
    snapshot = AureusCsvAdapter().to_ir(_workbook(), "preview.csv")
    return canonical_json(snapshot.content_payload).encode("utf-8")


def _oracle_registry() -> CompletionOracleRegistryV1:
    definitions = (
        CompletionOracleDefinitionV1(
            oracle_id="state-predicate",
            version=1,
            params_schema_id="state-predicate-params@1",
            result_schema_id="completion-oracle-result@1",
            executor_key="state_predicate_oracle@1",
        ),
    )
    payload = {"registry_version": 1, "definitions": definitions}
    return CompletionOracleRegistryV1(
        **payload,
        registry_digest=compute_completion_oracle_registry_digest(payload),
    )


def _registry_ref() -> CompletionOracleRegistryRefV1:
    registry = _oracle_registry()
    return CompletionOracleRegistryRefV1(
        registry_version=registry.registry_version,
        digest=registry.registry_digest,
    )


def _payload() -> TaskSuiteDerivePayloadV1:
    return TaskSuiteDerivePayloadV1(
        source_preview_artifact_id=PREVIEW_ID,
        config_artifact_id=CONFIG_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        derivation_profile=_DERIVATION_PROFILE,
        environment_profile=_ENV_PROFILE,
        completion_oracle_registry_ref=_registry_ref(),
    )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(PREVIEW_ID, _preview_bytes())
    return store


def _handler(store: FakeArtifactStore) -> TaskSuiteDeriveHandler:
    return TaskSuiteDeriveHandler(
        blobs=store,
        store=store,
        scenario_shaper=AureusScenarioShaper(),
        environment_contract_resolver=lambda ref: _ENV_CONTRACT,
    )


def _context():
    return build_context(
        params=_payload(),
        kind=TASK_SUITE_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/derivation_profile",
                profile_id="builtin.task_suite_derivation",
                version=1,
                kind="task_suite_derivation",
            ),
            resolved_binding(
                "/params/environment_profile",
                profile_id="builtin.environment",
                version=1,
                kind="environment",
            ),
        ),
    )


def _run(store: FakeArtifactStore):
    return _handler(store)(_context())


def _suite(store: FakeArtifactStore, outcome) -> TaskSuiteV1:
    primary = outcome.artifacts[outcome.primary_index]
    return TaskSuiteV1.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def _kinds(outcome) -> list[str]:
    return [artifact.kind for artifact in outcome.artifacts]


def test_derive_publishes_one_suite_plus_n_scenarios() -> None:
    store = _store()
    outcome = _run(store)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "task_suite_derived"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "task_suite"
    assert primary.payload_schema_id == "task-suite@1"

    kinds = _kinds(outcome)
    assert kinds.count("task_suite") == 1
    assert kinds.count("scenario_spec") == 2  # one per quest chain
    assert outcome.summary.prepared_domain_artifact_count == 3
    assert outcome.summary.prepared_finding_count == 0

    suite = _suite(store, outcome)
    assert len(suite.episodes) == 2
    assert suite.suite_profile == _DERIVATION_PROFILE
    assert suite.environment_profile == _ENV_PROFILE
    assert suite.env_contract_version == _ENV_CONTRACT.env_contract_version
    assert suite.completion_oracle_registry_ref == _registry_ref()


def test_derive_binds_episode_scenario_identity() -> None:
    store = _store()
    outcome = _run(store)
    suite = _suite(store, outcome)

    scenario_arts = [a for a in outcome.artifacts if a.kind == "scenario_spec"]
    predicted = {
        artifact_id_v2_for(
            kind=a.kind,
            version_tuple=a.version_tuple,
            lineage=a.lineage,
            payload_hash=a.payload_hash,
            meta={**a.meta, "replayability": "deterministic_recompute"},
        )
        for a in scenario_arts
    }
    episode_refs = {ep.scenario_spec_artifact_id for ep in suite.episodes}
    # exact one-to-one identity binding /episodes → /scenario_spec_artifact_id.
    assert len(episode_refs) == len(suite.episodes)
    assert episode_refs == predicted


def test_derive_reset_binding_uses_profile_selected_schema() -> None:
    store = _store()
    outcome = _run(store)
    scenario_arts = [a for a in outcome.artifacts if a.kind == "scenario_spec"]

    for art in scenario_arts:
        scenario = ScenarioSpecV1.model_validate(json.loads(store.read_prepared(art.object_ref)))
        # reset schema is PROFILE-SELECTED (env contract), not Aureus-hardcoded.
        assert scenario.reset_binding.reset_schema_id == _ENV_CONTRACT.reset_schema_id
        assert scenario.env_contract_version == _ENV_CONTRACT.env_contract_version
        assert scenario.config_export_artifact_id == CONFIG_ID
        assert scenario.source_preview_artifact_id == PREVIEW_ID
        assert scenario.constraint_snapshot_artifact_id == CONSTRAINT_ID


def test_derive_completion_oracle_resolves_in_registry() -> None:
    store = _store()
    outcome = _run(store)
    suite = _suite(store, outcome)
    registry = _oracle_registry()

    for episode in suite.episodes:
        assert episode.completion_oracle == ALL_QUESTS_COMPLETED_ORACLE
        definition = resolve_completion_oracle(
            registry, suite.completion_oracle_registry_ref, episode.completion_oracle
        )
        assert definition.executor_key == "state_predicate_oracle@1"
        assert episode.step_budget >= 1


def test_derive_declares_run_input_lineage_roles() -> None:
    store = _store()
    outcome = _run(store)
    expected = tuple(sorted((PREVIEW_ID, CONFIG_ID, CONSTRAINT_ID)))
    for artifact in outcome.artifacts:
        # the sibling scenario parents on the suite are publisher-injected; the
        # handler declares ONLY preview + config + constraint run_input roles.
        assert tuple(sorted(artifact.lineage)) == expected


def test_derive_is_byte_deterministic() -> None:
    out_a = _run(_store())
    out_b = _run(_store())
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_handler_outcome_publishes_with_exact_scenario_identities() -> None:
    """Exercise handler → generic publisher, including final meta-derived IDs."""

    from gameforge.contracts.jobs import (
        outcome_policy_set_digest,
        run_kind_definition_digest,
    )
    from gameforge.contracts.lineage import ArtifactV1, AuditActor, ObjectLocation, VersionTuple
    from gameforge.platform.registry import build_builtin_registry
    from gameforge.platform.runs.lifecycle import select_outcome_policy
    from tests.platform.m4c.test_terminal_publisher import (
        _Artifacts,
        _Audit,
        _Blobs,
        _Findings,
        _Ledger,
        _publisher,
    )

    store = _store()
    context = _context()
    outcome = _handler(store)(context)
    blobs = _Blobs()
    prepared_artifacts = []
    for prepared in outcome.artifacts:
        blob = store.read_prepared(prepared.object_ref)
        assert blobs.register(blob) == prepared.object_ref
        prepared_artifacts.append(
            prepared.model_copy(
                update={
                    "location": ObjectLocation(
                        store_id="s3",
                        key=prepared.object_ref.key,
                        backend_generation="g1",
                    )
                }
            )
        )
    outcome = outcome.model_copy(update={"artifacts": tuple(prepared_artifacts)})

    registry = build_builtin_registry()
    definition = registry.get_run_kind(TASK_SUITE_KIND)
    assert definition is not None
    retry = registry.get_retry_policy(definition.retry_policy)
    assert retry is not None
    run = context.run.model_copy(
        update={
            "run_kind_definition_digest": run_kind_definition_digest(definition),
            "outcome_policy_set_digest": outcome_policy_set_digest(
                TASK_SUITE_KIND, definition.outcome_policies
            ),
            "failure_classifier": definition.failure_classifier,
            "retry_policy": definition.retry_policy,
            "max_attempts": retry.max_attempts,
        }
    )
    produced_tuple = outcome.artifacts[0].version_tuple
    artifacts = _Artifacts()
    artifacts.add(
        ArtifactV1(
            artifact_id=PREVIEW_ID,
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                doc_version=produced_tuple.doc_version,
                ir_snapshot_id=produced_tuple.ir_snapshot_id,
            ),
            lineage=[],
            payload_hash=None,
            meta={"payload_schema_id": "ir-core@1"},
        )
    )
    artifacts.add(
        ArtifactV1(
            artifact_id=CONFIG_ID,
            kind="config_export",
            version_tuple=VersionTuple(
                doc_version=produced_tuple.doc_version,
                ir_snapshot_id=produced_tuple.ir_snapshot_id,
                constraint_snapshot_id=produced_tuple.constraint_snapshot_id,
                env_contract_version=produced_tuple.env_contract_version,
            ),
            lineage=[],
            payload_hash=None,
            meta={"payload_schema_id": "config-export-package@1"},
        )
    )
    artifacts.add(
        ArtifactV1(
            artifact_id=CONSTRAINT_ID,
            kind="constraint_snapshot",
            version_tuple=VersionTuple(
                constraint_snapshot_id=produced_tuple.constraint_snapshot_id
            ),
            lineage=[],
            payload_hash=None,
            meta={"payload_schema_id": "constraint-snapshot@1"},
        )
    )
    policy = select_outcome_policy(
        definition=definition,
        outcome_code="task_suite_derived",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )

    published = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _Ledger(),
        _Audit(),
    ).publish_run_result(
        run=run,
        attempt=context.attempt,
        prepared=outcome,
        policy=policy,
        occurred_at=context.run.updated_at,
        actor=AuditActor(
            principal_id=context.attempt.worker_principal_id,
            principal_kind="service",
        ),
    )

    manifest = artifacts.by_id[published.result_artifact_id]
    result = json.loads(blobs.read(manifest.object_ref))
    suite_artifact = artifacts.by_id[result["primary_artifact_id"]]
    suite = TaskSuiteV1.model_validate(json.loads(blobs.read(suite_artifact.object_ref)))
    scenario_ids = {episode.scenario_spec_artifact_id for episode in suite.episodes}
    assert scenario_ids == {
        artifact_id
        for artifact_id in result["produced_artifact_ids"]
        if artifacts.by_id[artifact_id].kind == "scenario_spec"
    }


def test_completion_oracle_executors_close_platform_readiness() -> None:
    from dataclasses import replace

    from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
    from gameforge.platform.registry import (
        PlatformReadinessValidator,
        build_builtin_registry,
    )
    from tests.platform.m4c import test_readiness_registry as readiness_mod

    registry = build_builtin_registry()
    executors = build_completion_oracle_executors()

    # the injected Aureus oracle executors cover exactly the frozen registry keys.
    assert set(executors) == readiness_mod._completion_oracle_keys(registry)

    components = replace(readiness_mod._components(registry), completion_oracles=executors)
    report = PlatformReadinessValidator(registry=registry, components=components).validate()
    assert report.ready is True
