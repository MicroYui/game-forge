"""Task 9 producer-fact authority and producer-matrix closure."""

from __future__ import annotations

import pytest

from gameforge.contracts.canonical import canonical_json, canonical_sha256, compute_snapshot_id
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    ConstraintValidationPayloadV1,
    GenerationProposePayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
    RunPayloadEnvelope,
    SimulationRunPayloadV1,
    SolverEngineRefV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import (
    InvocationVersionBindingV1,
    VersionTuple,
    build_artifact_v2,
    build_execution_identity,
    object_ref_for_bytes,
)
from gameforge.platform.publication.producer import (
    BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES,
    BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER,
    DomainProducerFactsResolver,
    validate_domain_artifact_producer,
)
from gameforge.platform.publication.publisher import TerminalPublisher
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    build_envelope,
    build_run_record,
    execution_plan,
)


_HEX = "a" * 64
_MODEL = "anthropic/claude-opus-4-8/m2a@1"


def _binding(kind: str, policy_id: str, rule_id: str):
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind=kind, version=1))
    assert definition is not None
    policy = next(item for item in definition.outcome_policies if item.policy_id == policy_id)
    rule = next(item for item in policy.artifact_rules if item.rule_id == rule_id)
    lineage = registry.get_lineage_policy(rule.lineage_policy_ref)
    assert lineage is not None
    return policy, rule, lineage


def _generation_run():
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash=_HEX
        ),
        domain_scope=DomainScope(domain_ids=("content",)),
        target=RefReadBindingV1(ref_name="ref:content"),
        generation_policy=ProfileRefV1(profile_id="generation", version=1),
        candidate_export_profiles=(),
    )
    plan = execution_plan({"generation": _MODEL})
    envelope = build_envelope(
        params=params,
        llm_execution_mode="replay",
        plan=plan,
        cassette_artifact_id="artifact:cassette",
    )
    return build_run_record(envelope, RunKindRef(kind="generation.propose", version=1))


def _artifact_identity(*, model: str = _MODEL):
    binding = InvocationVersionBindingV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        transport_attempt=None,
        routing_decision_kind="native",
        routing_decision_id="routing:1",
        agent_node_id="generation",
        prompt_version="p@1",
        model_snapshot=model,
        tool_version="t@1",
        execution_source="cassette_replay",
        response_consumed=True,
    )
    return build_execution_identity(
        scope="artifact", bindings=(binding,), agent_graph_version="graph@1"
    )


def test_builtin_producer_facts_exhaust_every_active_outcome_rule() -> None:
    registry = build_builtin_registry()
    assert BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.validate_registry(registry) == 62


def test_producer_fact_registry_rejects_missing_and_duplicate_selectors() -> None:
    registry = build_builtin_registry()
    with pytest.raises(IntegrityViolation):
        DomainProducerFactsResolver(BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES[:-1]).validate_registry(
            registry
        )
    with pytest.raises(IntegrityViolation):
        DomainProducerFactsResolver(
            (*BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES, BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES[0])
        )


def test_ir_snapshot_identity_and_llm_tuple_are_recomputed_from_authorities() -> None:
    run = _generation_run()
    policy, rule, lineage = _binding("generation.propose", "generation-gate-pass", "preview")
    payload = {
        "meta_schema_version": "ir@1",
        "entities": {"npc:a": {"type": "NPC"}},
        "relations": {},
    }
    identity = _artifact_identity()
    cassette_id = f"sha256:{'b' * 64}"

    assert BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.requires_identity(
        run_kind=run.kind,
        policy=policy,
        rule=rule,
        payload_schema_id="ir-core@1",
    )

    facts = BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.resolve(
        run=run,
        policy=policy,
        rule=rule,
        lineage_policy=lineage,
        payload_schema_id="ir-core@1",
        canonical_payload=payload,
        execution_identity=identity,
        cassette_id=cassette_id,
    )

    assert facts.producer_tuple == VersionTuple(
        ir_snapshot_id=compute_snapshot_id(payload),
        prompt_version="p@1",
        model_snapshot=_MODEL,
        agent_graph_version="graph@1",
        tool_version="generation@1",
        cassette_id=cassette_id,
    )
    assert facts.replayability == "cassette_replay"

    projected = facts.producer_tuple.model_copy(update={"doc_version": "doc@1"})
    blob = canonical_json(payload).encode("utf-8")
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=projected,
        lineage=("artifact:base",),
        payload_hash=object_ref_for_bytes(blob).sha256,
        object_ref=object_ref_for_bytes(blob),
        meta=facts.authoritative_meta({"payload_schema_id": "ir-core@1"}),
    )
    assert (
        validate_domain_artifact_producer(
            artifact,
            facts=facts,
            lineage_policy=lineage,
            projected_tuple=projected,
        ).status
        == "valid"
    )


def test_config_export_environment_uses_exact_indexed_frozen_profile_binding() -> None:
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    definition = next(item for item in catalog.definitions if item.profile_kind == "config_export")
    assert isinstance(definition.details, ConfigExportProfileDetailsV1)
    profile = definition.profile
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id="artifact:constraints",
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash=_HEX
        ),
        domain_scope=DomainScope(domain_ids=("content",)),
        target=RefReadBindingV1(ref_name="ref:content"),
        generation_policy=ProfileRefV1(profile_id="builtin.generation", version=1),
        candidate_export_profiles=(profile,),
    )
    resolved = ResolvedExecutionProfileBindingV1(
        field_path="/params/candidate_export_profiles/0",
        profile=profile,
        expected_profile_kind="config_export",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    envelope = build_envelope(params=params, resolved_profiles=(resolved,)).model_copy(
        update={
            "execution_profile_catalog_version": catalog.catalog_version,
            "execution_profile_catalog_digest": catalog.catalog_digest,
        }
    )
    run = build_run_record(envelope, RunKindRef(kind="generation.propose", version=1))
    policy, rule, _ = _binding("generation.propose", "generation-gate-pass", "config-export")
    details = definition.details
    package = {
        "export_profile": profile.model_dump(mode="json"),
        "target_environment_profile": details.target_environment_profile.model_dump(mode="json"),
        "env_contract_version": details.env_contract_version,
        "format_schema_id": details.format_schema_id,
        "package_schema_version": details.package_schema_version,
    }
    publisher = TerminalPublisher(
        registry=registry,
        artifacts=object(),  # type: ignore[arg-type]
        blobs=object(),  # type: ignore[arg-type]
        findings=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        audit=object(),  # type: ignore[arg-type]
    )

    assert (
        publisher._config_export_producer_env(  # noqa: SLF001 - exact authority seam
            run=run,
            rule=rule,
            payload_schema_id="config-export-package@1",
            payload=package,
        )
        == details.env_contract_version
    )

    forged_run = run.model_copy(
        update={
            "payload": envelope.model_copy(
                update={
                    "resolved_profiles": (
                        resolved.model_copy(
                            update={"field_path": "/params/candidate_export_profiles"}
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(IntegrityViolation, match="resolved profile binding"):
        publisher._config_export_producer_env(  # noqa: SLF001
            run=forged_run,
            rule=rule,
            payload_schema_id="config-export-package@1",
            payload=package,
        )


def test_fabricated_artifact_identity_outside_execution_plan_fails_closed() -> None:
    run = _generation_run()
    policy, rule, lineage = _binding("generation.propose", "generation-gate-pass", "preview")
    with pytest.raises(IntegrityViolation):
        BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.resolve(
            run=run,
            policy=policy,
            rule=rule,
            lineage_policy=lineage,
            payload_schema_id="ir-core@1",
            canonical_payload={"entities": {}, "relations": {}},
            execution_identity=_artifact_identity(model="anthropic/not-allowed/snapshot@1"),
            cassette_id=f"sha256:{'b' * 64}",
        )


def test_constraint_candidate_id_and_tool_are_payload_derived_not_run_primary() -> None:
    params = ConstraintValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id="approval:1",
            expected_workflow_revision=1,
            subject_head_revision=1,
            subject_artifact_id="artifact:proposal",
            subject_digest=_HEX,
            active_validation_run_id="run:1",
        ),
        target=RefReadBindingV1(ref_name="ref:constraints"),
        dsl_grammar_version="dsl@1",
        compiler_profile=ProfileRefV1(profile_id="compiler", version=1),
        differential_engines=(
            SolverEngineRefV1(engine_id="clingo", version=1),
            SolverEngineRefV1(engine_id="z3", version=1),
        ),
        regression_suite_artifact_ids=(),
        validation_policy=ProfileRefV1(profile_id="validation", version=1),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="constraint_proposal.validate", version=1),
    )
    policy, rule, lineage = _binding(
        "constraint_proposal.validate", "constraint-validated-with-candidate", "candidate"
    )
    payload = {"dsl_grammar_version": "dsl@1", "constraints": []}
    facts = BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.resolve(
        run=run,
        policy=policy,
        rule=rule,
        lineage_policy=lineage,
        payload_schema_id="constraint-snapshot@1",
        canonical_payload=payload,
    )
    assert facts.producer_tuple.constraint_snapshot_id == (
        f"candidate:{canonical_sha256(payload)[:32]}"
    )
    assert facts.producer_tuple.tool_version == "constraint-compile@1"


def test_root_seed_and_environment_are_the_only_local_simulation_sources() -> None:
    params = SimulationRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        scenario_artifact_id=None,
        simulation_profile=ProfileRefV1(profile_id="simulation", version=1),
        workload_profile=ProfileRefV1(profile_id="workload", version=1),
        replication_count=10,
        horizon_steps=20,
    )
    envelope = build_envelope(params=params, seed=7)
    envelope = RunPayloadEnvelope.model_validate(
        {
            **envelope.model_dump(mode="python"),
            "version_tuple": VersionTuple(
                tool_version="economy-sim@1",
                env_contract_version="agent-env@3",
                seed=7,
            ),
        }
    )
    run = build_run_record(envelope, RunKindRef(kind="simulation.run", version=1))
    policy, rule, lineage = _binding("simulation.run", "simulation-completed", "primary")
    facts = BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.resolve(
        run=run,
        policy=policy,
        rule=rule,
        lineage_policy=lineage,
        payload_schema_id="simulation-result@1",
        canonical_payload={"payload_schema_version": "simulation-result@1"},
    )
    assert facts.producer_tuple.tool_version == "economy-sim@1"
    assert facts.producer_tuple.seed == 7
    assert facts.producer_tuple.env_contract_version == "agent-env@3"


def test_migration_tool_is_resolved_from_the_frozen_payload_profile() -> None:
    params = ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:source",
        target_payload_schema_id="target@2",
        target_meta_schema_version="meta@2",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=4),
        publish_mode="report_only",
    )
    run = build_run_record(
        build_envelope(params=params), RunKindRef(kind="artifact.migrate", version=1)
    )
    policy, rule, lineage = _binding("artifact.migrate", "artifact-migration-reported", "primary")
    facts = BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.resolve(
        run=run,
        policy=policy,
        rule=rule,
        lineage_policy=lineage,
        payload_schema_id="migration-report@1",
        canonical_payload={"report_schema_version": "migration-report@1"},
    )
    assert facts.producer_tuple.tool_version == "builtin.artifact_migrator@4"
