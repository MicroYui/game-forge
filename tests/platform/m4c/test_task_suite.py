"""Task 12a — ``task_suite_deriver@1`` (DETERMINISTIC scenario/suite derivation).

Turns a gated preview Snapshot + its config-export package + a completion-oracle
registry ref into ONE non-empty ``TaskSuiteV1`` and its N sibling
``ScenarioSpecV1`` artifacts (one per completable quest chain). No LLM, no seed,
no findings; same input ⇒ byte-identical outcome. The GAME-SPECIFIC scenario
shaping + the deterministic oracle binding come from the injected apps/worker
ports; the reset-schema is PROFILE-SELECTED from the environment contract.
"""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from gameforge.apps.worker.completion_oracles import ALL_QUESTS_COMPLETED_ORACLE
from gameforge.apps.worker.task_suite import AureusScenarioShaper
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    ProfileRefV1,
    RunKindRef,
    TaskSuiteDerivationProfileConfigV2,
)
from gameforge.contracts.jobs import PreparedRunResult, TaskSuiteDerivePayloadV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import VersionTuple, artifact_id_v2_for
from gameforge.contracts.playtest import (
    MAX_PLAYTEST_COLLECTION_ITEMS,
    CompletionOracleRefV1,
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
    ScenarioSpecV1,
    TaskSuiteV1,
    resolve_completion_oracle,
)
from gameforge.platform.playtest_payload_schemas import (
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.run_handlers.task_suite import (
    ScenarioDraftV1,
    TaskSuiteDeriveHandler,
)
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
DOC_VERSION = "design-doc@7"
CONSTRAINT_SNAPSHOT_ID = "constraint-snapshot@9"

_ENV_PROFILE = ProfileRefV1(profile_id="builtin.environment", version=1)
_DERIVATION_PROFILE = ProfileRefV1(profile_id="builtin.task_suite_derivation", version=2)
_ENV_CONTRACT = EnvironmentContractDescriptorV1(
    env_contract_version="generic-agent-env@1",
    reset_schema_id="generic-env-reset@1",
    action_schema_id="generic-env-action@1",
    observation_schema_id="generic-env-observation@1",
    max_navigation_grid_cells=65_536,
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
    from gameforge.platform.registry import build_builtin_registry

    return build_builtin_registry().completion_oracle_registries[0]


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


def _derivation_config() -> TaskSuiteDerivationProfileConfigV2:
    registry = _oracle_registry()
    return TaskSuiteDerivationProfileConfigV2(
        target_environment_profile=_ENV_PROFILE,
        completion_oracle_registry_version=registry.registry_version,
        completion_oracle_registry_digest=registry.registry_digest,
        max_scenarios=MAX_PLAYTEST_COLLECTION_ITEMS,
        max_total_prepared_artifact_bytes=256 * 1024 * 1024,
    )


def _payload_validator() -> PlaytestPayloadValidationService:
    from gameforge.platform.registry import build_builtin_registry

    registry = build_builtin_registry()
    return PlaytestPayloadValidationService(
        registry=registry,
        validators=build_builtin_playtest_payload_validators(),
    )


def _handler(
    store: FakeArtifactStore,
    *,
    scenario_shaper=None,
    profile_binding_validator=None,
    snapshot_loader=None,
) -> TaskSuiteDeriveHandler:
    kwargs = {}
    if profile_binding_validator is not None:
        kwargs["profile_binding_validator"] = profile_binding_validator
    if snapshot_loader is not None:
        kwargs["snapshot_loader"] = snapshot_loader
    return TaskSuiteDeriveHandler(
        blobs=store,
        store=store,
        scenario_shaper_resolver=lambda ref: scenario_shaper or AureusScenarioShaper(),
        environment_contract_resolver=lambda ref: _ENV_CONTRACT,
        derivation_config_resolver=lambda ref: _derivation_config(),
        completion_oracle_registry_resolver=lambda ref: (
            _oracle_registry() if ref == _registry_ref() else None
        ),
        payload_validator=_payload_validator(),
        **kwargs,
    )


def _context():
    preview = AureusCsvAdapter().to_ir(_workbook(), "preview.csv")
    return build_context(
        params=_payload(),
        kind=TASK_SUITE_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/derivation_profile",
                profile_id="builtin.task_suite_derivation",
                version=2,
                kind="task_suite_derivation",
            ),
            resolved_binding(
                "/params/environment_profile",
                profile_id="builtin.environment",
                version=1,
                kind="environment",
            ),
        ),
        version_tuple=VersionTuple(
            doc_version=DOC_VERSION,
            ir_snapshot_id=preview.snapshot_id,
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version=_ENV_CONTRACT.env_contract_version,
            tool_version="task-suite-admission@1",
        ),
        resource_domain_scope=DomainScope(domain_ids=("builtin",)),
    )


def _run(store: FakeArtifactStore):
    return _handler(store)(_context())


def _replace_profiles(context, bindings):
    envelope = context.payload.model_copy(update={"resolved_profiles": tuple(bindings)})
    return replace(
        context,
        payload=envelope,
        run=context.run.model_copy(update={"payload": envelope}),
    )


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ("missing", "profile set"),
        ("extra", "profile set"),
        ("catalog", "exact Run binding"),
    ),
)
def test_derive_closes_complete_profile_set_before_preview_work(
    mutation: str,
    match: str,
) -> None:
    store = _store()
    context = _context()
    bindings = list(context.payload.resolved_profiles)
    if mutation == "missing":
        bindings = bindings[:1]
    elif mutation == "extra":
        bindings.append(
            resolved_binding(
                "/params/unexpected_profile",
                profile_id="builtin.environment",
                version=1,
                kind="environment",
            )
        )
    else:
        bindings[1] = bindings[1].model_copy(update={"catalog_digest": "b" * 64})

    preview_reads: list[str] = []

    def forbidden_preview_read(*_args):
        preview_reads.append("read")
        raise AssertionError("profile closure must precede preview reads")

    with pytest.raises(IntegrityViolation, match=match):
        _handler(store, snapshot_loader=forbidden_preview_read)(
            _replace_profiles(context, bindings)
        )

    assert preview_reads == []
    assert store.put_count == 0


@pytest.mark.parametrize("reason", ("profile payload hash", "lifecycle"))
def test_derive_revalidates_profile_authority_before_preview_work(reason: str) -> None:
    store = _store()
    context = _context()
    bindings = list(context.payload.resolved_profiles)
    if reason == "profile payload hash":
        bindings[0] = bindings[0].model_copy(update={"profile_payload_hash": "0" * 64})
    context = _replace_profiles(context, bindings)
    validated: list[str] = []
    preview_reads: list[str] = []

    def validate(binding, *, llm_execution_mode, run_kind):
        validated.append(binding.field_path)
        assert llm_execution_mode == "not_applicable"
        assert run_kind == TASK_SUITE_KIND
        if reason == "profile payload hash" and binding.profile_payload_hash == "0" * 64:
            raise IntegrityViolation("profile payload hash differs from retained authority")
        raise IntegrityViolation("execution profile lifecycle forbids this Run mode")

    def forbidden_preview_read(*_args):
        preview_reads.append("read")
        raise AssertionError("retained profile validation must precede preview reads")

    with pytest.raises(IntegrityViolation, match=reason):
        _handler(
            store,
            profile_binding_validator=validate,
            snapshot_loader=forbidden_preview_read,
        )(context)

    assert validated == ["/params/derivation_profile"]
    assert preview_reads == []
    assert store.put_count == 0


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
    assert all(
        episode.domain_scope == DomainScope(domain_ids=("builtin",)) for episode in suite.episodes
    )


def test_derive_inherits_semantic_parent_versions_not_artifact_ids() -> None:
    store = _store()
    outcome = _run(store)
    preview = AureusCsvAdapter().to_ir(_workbook(), "preview.csv")

    expected = VersionTuple(
        doc_version=DOC_VERSION,
        ir_snapshot_id=preview.snapshot_id,
        constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        env_contract_version=_ENV_CONTRACT.env_contract_version,
        tool_version="task-suite@1",
    )
    assert all(artifact.version_tuple == expected for artifact in outcome.artifacts)
    assert CONSTRAINT_ID != CONSTRAINT_SNAPSHOT_ID


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


class _FixedShaper:
    def __init__(self, drafts: tuple[ScenarioDraftV1, ...]) -> None:
        self._drafts = drafts

    def shape(self, request):
        del request
        return self._drafts


def _draft(
    index: int,
    *,
    scenario_id: str | None = None,
    completion_oracle: CompletionOracleRefV1 = ALL_QUESTS_COMPLETED_ORACLE,
    reset_payload: object | None = None,
) -> ScenarioDraftV1:
    return ScenarioDraftV1(
        scenario_id=scenario_id or f"scenario:{index:04d}",
        episode_id=f"episode:{index:04d}",
        domain_scope=DomainScope(domain_ids=("builtin",)),
        reset_payload=(
            {
                "scenario_id": scenario_id or f"scenario:{index:04d}",
                "config_export_artifact_id": CONFIG_ID,
                "quest_ids": [f"quest:{index:04d}"],
                "start_seed": 0,
            }
            if reset_payload is None
            else reset_payload
        ),
        completion_oracle=completion_oracle,
        step_budget=10,
    )


def test_derive_validates_reset_payload_and_oracle_params_before_any_blob_write() -> None:
    store = _store()
    bad_reset = _draft(1, reset_payload={"scenario_id": "scenario:0001"})
    with pytest.raises(IntegrityViolation, match="reset payload"):
        _handler(store, scenario_shaper=_FixedShaper((bad_reset,)))(_context())
    assert store.put_count == 0

    bad_oracle = _draft(
        1,
        completion_oracle=ALL_QUESTS_COMPLETED_ORACLE.model_copy(
            update={"params": {"predicate": "model_says_complete"}}
        ),
    )
    with pytest.raises(IntegrityViolation, match="completion-oracle params"):
        _handler(store, scenario_shaper=_FixedShaper((bad_oracle,)))(_context())
    assert store.put_count == 0


@pytest.mark.parametrize(
    "reset_payload",
    (
        {
            "scenario_id": "scenario:wrong",
            "config_export_artifact_id": CONFIG_ID,
            "quest_ids": ["quest:0001"],
            "start_seed": 0,
        },
        {
            "scenario_id": "scenario:0001",
            "config_export_artifact_id": "artifact:other-config",
            "quest_ids": ["quest:0001"],
            "start_seed": 0,
        },
    ),
)
def test_derive_binds_reset_identity_to_outer_scenario_before_blob_write(
    reset_payload: dict[str, object],
) -> None:
    store = _store()
    draft = _draft(1, reset_payload=reset_payload)

    with pytest.raises(IntegrityViolation, match="reset payload"):
        _handler(store, scenario_shaper=_FixedShaper((draft,)))(_context())

    assert store.put_count == 0


def test_derive_rejects_duplicate_stable_scenario_identity_before_blob_write() -> None:
    store = _store()
    drafts = (
        _draft(1, scenario_id="scenario:duplicate"),
        _draft(2, scenario_id="scenario:duplicate"),
    )
    with pytest.raises(IntegrityViolation, match="scenario_id"):
        _handler(store, scenario_shaper=_FixedShaper(drafts))(_context())
    assert store.put_count == 0


def test_derive_accepts_exact_maximum_episode_closure_and_rejects_cap_plus_one() -> None:
    exact_store = _store()
    exact = tuple(_draft(index) for index in range(MAX_PLAYTEST_COLLECTION_ITEMS))
    outcome = _handler(exact_store, scenario_shaper=_FixedShaper(exact))(_context())
    assert len(outcome.artifacts) == MAX_PLAYTEST_COLLECTION_ITEMS + 1
    assert exact_store.put_count == MAX_PLAYTEST_COLLECTION_ITEMS + 1

    over_store = _store()
    over = tuple(_draft(index) for index in range(MAX_PLAYTEST_COLLECTION_ITEMS + 1))
    with pytest.raises(IntegrityViolation, match="scenario count"):
        _handler(over_store, scenario_shaper=_FixedShaper(over))(_context())
    assert over_store.put_count == 0


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
        canonical_payload_hash,
        outcome_policy_set_digest,
        run_kind_definition_digest,
    )
    from gameforge.contracts.execution_profiles import (
        ResolvedExecutionProfileBindingV1,
        execution_profile_payload_hash,
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
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    exact_bindings = []
    for field_path, profile, expected_kind in (
        ("/params/derivation_profile", _DERIVATION_PROFILE, "task_suite_derivation"),
        ("/params/environment_profile", _ENV_PROFILE, "environment"),
    ):
        profile_definition = next(item for item in catalog.definitions if item.profile == profile)
        exact_bindings.append(
            ResolvedExecutionProfileBindingV1(
                field_path=field_path,
                profile=profile,
                expected_profile_kind=expected_kind,
                profile_payload_hash=execution_profile_payload_hash(profile_definition),
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
            )
        )
    envelope = context.run.payload.model_copy(
        update={
            "execution_profile_catalog_version": catalog.catalog_version,
            "execution_profile_catalog_digest": catalog.catalog_digest,
            "resolved_profiles": tuple(exact_bindings),
        }
    )
    run = context.run.model_copy(
        update={
            "payload": envelope,
            "payload_hash": canonical_payload_hash(envelope),
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
    artifacts.payloads_by_id[PREVIEW_ID] = _preview_bytes()
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

    publisher = _publisher(
        registry,
        artifacts,
        blobs,
        _Findings(),
        _Ledger(),
        _Audit(),
        playtest_payload_validator=PlaytestPayloadValidationService(
            registry=registry,
            validators=build_builtin_playtest_payload_validators(),
        ),
        task_suite_scenario_shaper_resolver=lambda _profile: AureusScenarioShaper(),
    )
    scenario_prepared = next(
        item for item in outcome.artifacts if item.payload_schema_id == "scenario-spec@1"
    )
    forged_scenario = json.loads(blobs.read(scenario_prepared.object_ref))
    forged_scenario["reset_binding"]["reset_schema_id"] = "unknown-reset@1"
    prepared_batch_total_bytes = sum(
        artifact.object_ref.size_bytes for artifact in outcome.artifacts
    )
    with pytest.raises(IntegrityViolation, match="reset schema"):
        publisher._validate_task_suite_payload_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="scenario-spec@1",
            payload=forged_scenario,
            prepared_batch_total_bytes=prepared_batch_total_bytes,
        )

    for field, value in (
        ("scenario_id", "scenario:wrong"),
        ("config_export_artifact_id", "artifact:other-config"),
    ):
        forged_scenario = json.loads(blobs.read(scenario_prepared.object_ref))
        forged_scenario["reset_binding"]["payload"][field] = value
        forged_scenario["reset_binding"]["payload_hash"] = canonical_sha256(
            forged_scenario["reset_binding"]["payload"]
        )
        with pytest.raises(IntegrityViolation, match="contextual binding"):
            publisher._validate_task_suite_payload_authority(  # noqa: SLF001
                run=run,
                payload_schema_id="scenario-spec@1",
                payload=forged_scenario,
                prepared_batch_total_bytes=prepared_batch_total_bytes,
            )

    forged_scenario = json.loads(blobs.read(scenario_prepared.object_ref))
    forged_scenario["domain_scope"] = {"domain_ids": ["forged"]}
    with pytest.raises(IntegrityViolation, match="domain"):
        publisher._validate_task_suite_payload_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="scenario-spec@1",
            payload=forged_scenario,
            prepared_batch_total_bytes=prepared_batch_total_bytes,
        )

    valid_scenario = json.loads(blobs.read(scenario_prepared.object_ref))
    with pytest.raises(IntegrityViolation, match="profile closure"):
        publisher._validate_task_suite_payload_authority(  # noqa: SLF001
            run=run,
            payload_schema_id="scenario-spec@1",
            payload=valid_scenario,
            prepared_batch_total_bytes=_derivation_config().max_total_prepared_artifact_bytes + 1,
        )

    with pytest.raises(IntegrityViolation, match="exact derivation"):
        publisher._validate_task_suite_batch_authority(  # noqa: SLF001
            run=run,
            payloads_by_rule={"scenario": (), "primary": ()},
            artifacts_by_rule={"scenario": (), "primary": ()},
        )

    published = publisher.publish_run_result(
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
