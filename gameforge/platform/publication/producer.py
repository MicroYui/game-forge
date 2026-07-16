"""Authoritative producer-local facts for Task 9 domain Artifact publication.

Typed lineage decides which VersionTuple fields come from parents and which are
created by the current producer.  The latter cannot be copied from a worker's
``PreparedArtifact`` or from the Run's primary-output placeholder: auxiliary
outputs often have a different tool (for example ``config-export@1``), and IR /
constraint snapshots carry content-derived identities.

This module freezes that remaining authority by the complete
``RunKind/policy/rule/kind/schema`` selector.  It also derives producer-local
snapshot identities from the canonical payload, binds the Run root seed and
environment, and admits only an exact artifact-scoped execution identity.  The
terminal publisher can therefore project the final tuple without trusting any
worker-authored producer fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from gameforge.contracts.canonical import canonical_sha256, compute_snapshot_id
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.jobs import (
    ArtifactLineagePolicyV1,
    ArtifactMigrationPayloadV1,
    OutcomeArtifactPolicyV1,
    OutcomeArtifactRuleV1,
    RunKindDefinition,
    RunRecord,
    artifact_lineage_policy_digest,
)
from gameforge.contracts.lineage import ArtifactV2, ExecutionIdentityV1, VersionTuple
from gameforge.platform.lineage.validation import (
    PRODUCER_RULES,
    ProducerValidationContext,
    ProducerValidationReport,
    validate_artifact_producer,
)


SnapshotDerivation = Literal["none", "ir_content", "constraint_candidate"]
IdentityPolicy = Literal["forbidden", "required_for_llm_mode"]
ToolSource = Literal["fixed", "migration_profile"]
SeedSource = Literal["run_root", "fixed"]


@dataclass(frozen=True, order=True, slots=True)
class DomainProducerRuleKey:
    """The complete immutable selector for one domain outcome Artifact."""

    run_kind: str
    run_kind_version: int
    policy_id: str
    policy_version: int
    outcome_rule_id: str
    artifact_kind: str
    payload_schema_id: str


@dataclass(frozen=True, slots=True)
class DomainProducerRuleFacts:
    """Facts that cannot be inherited from an Artifact parent."""

    key: DomainProducerRuleKey
    tool_source: ToolSource
    fixed_tool_version: str | None = None
    seed_source: SeedSource = "run_root"
    fixed_seed: int | None = None
    snapshot_derivation: SnapshotDerivation = "none"
    identity_policy: IdentityPolicy = "forbidden"
    operational_observation: bool = False

    def __post_init__(self) -> None:
        if self.tool_source == "fixed":
            if not self.fixed_tool_version:
                raise ValueError("fixed producer tool source requires a version")
        elif self.fixed_tool_version is not None:
            raise ValueError("profile-derived producer tool forbids a fixed version")
        if self.seed_source == "fixed":
            if isinstance(self.fixed_seed, bool) or not isinstance(self.fixed_seed, int):
                raise ValueError("fixed producer seed requires an integer value")
        elif self.fixed_seed is not None:
            raise ValueError("Run-root producer seed forbids a fixed value")


@dataclass(frozen=True, slots=True)
class ResolvedDomainProducerFacts:
    """Trusted facts ready for typed lineage and producer-matrix validation."""

    rule: DomainProducerRuleFacts
    producer_tuple: VersionTuple
    execution_identity: ExecutionIdentityV1 | None
    llm_execution_mode: Literal["not_applicable", "live", "record", "replay"]
    replayability: Literal[
        "online_only",
        "cassette_replay",
        "deterministic_recompute",
        "operational_observation",
    ]

    def authoritative_meta(self, prepared_meta: Mapping[str, object]) -> dict[str, object]:
        """Bind reserved metadata without silently overwriting worker claims."""

        meta = dict(prepared_meta)
        expected: dict[str, object] = {"replayability": self.replayability}
        if self.execution_identity is not None:
            expected["execution_identity"] = self.execution_identity
        elif "execution_identity" in meta:
            raise IntegrityViolation(
                "deterministic domain Artifact cannot carry a worker execution identity",
                outcome_rule_id=self.rule.key.outcome_rule_id,
            )
        for name, value in expected.items():
            if name in meta and meta[name] != value:
                raise IntegrityViolation(
                    "worker domain Artifact metadata differs from authoritative producer facts",
                    field=name,
                    outcome_rule_id=self.rule.key.outcome_rule_id,
                )
            meta[name] = value
        return meta


class DomainProducerRegistry(Protocol):
    def list_run_kinds(self) -> tuple[RunKindDefinition, ...]: ...

    def get_lineage_policy(self, ref: object) -> ArtifactLineagePolicyV1 | None: ...


def _key(
    run_kind: str,
    policy_id: str,
    rule_id: str,
    artifact_kind: str,
    payload_schema_id: str,
) -> DomainProducerRuleKey:
    return DomainProducerRuleKey(
        run_kind=run_kind,
        run_kind_version=1,
        policy_id=policy_id,
        policy_version=1,
        outcome_rule_id=rule_id,
        artifact_kind=artifact_kind,
        payload_schema_id=payload_schema_id,
    )


def _fixed(
    run_kind: str,
    policies: Iterable[str],
    rule_id: str,
    artifact_kind: str,
    schema: str,
    tool: str,
    *,
    fixed_seed: int | None = None,
    snapshot: SnapshotDerivation = "none",
    identity: IdentityPolicy = "forbidden",
    operational: bool = False,
) -> tuple[DomainProducerRuleFacts, ...]:
    return tuple(
        DomainProducerRuleFacts(
            key=_key(run_kind, policy, rule_id, artifact_kind, schema),
            tool_source="fixed",
            fixed_tool_version=tool,
            seed_source="fixed" if fixed_seed is not None else "run_root",
            fixed_seed=fixed_seed,
            snapshot_derivation=snapshot,
            identity_policy=identity,
            operational_observation=operational,
        )
        for policy in policies
    )


_GENERATION_POLICIES = ("generation-gate-pass", "generation-gate-rejected")
_CONSTRAINT_VALIDATION_POLICIES = (
    "constraint-validated-with-candidate",
    "constraint-validation-failed-with-candidate",
    "constraint-validation-failed-without-candidate",
)
_PATCH_VALIDATION_POLICIES = (
    "patch-validation-auto-eligible",
    "patch-validation-failed",
    "patch-validation-passed",
    "patch-validation-unproven",
)
_ROLLBACK_VALIDATION_POLICIES = (
    "rollback-validation-failed",
    "rollback-validation-passed",
    "rollback-validation-unproven",
)
_MIGRATION_POLICIES = (
    "artifact-migration-compatible",
    "artifact-migration-needs-action",
    "artifact-migration-reported",
)


# This is intentionally an entry sequence, not a dict literal: resolver construction
# detects duplicate selectors (including identical duplicates) rather than allowing
# Python's last-write-wins behaviour to hide a readiness conflict.
BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES: tuple[DomainProducerRuleFacts, ...] = (
    # Generation proposal and deterministic gate.
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "primary",
        "patch",
        "patch@2",
        "generation@1",
        identity="required_for_llm_mode",
    ),
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "preview",
        "ir_snapshot",
        "ir-core@1",
        "generation@1",
        snapshot="ir_content",
        identity="required_for_llm_mode",
    ),
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "config-export",
        "config_export",
        "config-export-package@1",
        "config-export@1",
    ),
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "checker",
        "checker_run",
        "checker-report@1",
        "generation-gate@1",
    ),
    # generation.propose forbids a Run root seed, while its deterministic gate
    # executes the economy simulator with the frozen tool-local seed 0.  Keep
    # that seed as producer authority instead of trusting a worker payload or
    # projecting the enclosing (necessarily null) Run seed.
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "simulation",
        "simulation_run",
        "simulation-result@1",
        "generation-gate@1",
        fixed_seed=0,
    ),
    *_fixed(
        "generation.propose",
        _GENERATION_POLICIES,
        "review",
        "review_report",
        "review@1",
        "generation-gate@1",
    ),
    # Repair proposal and deterministic verifier.
    *_fixed(
        "patch.repair",
        ("repair-verified",),
        "primary",
        "patch",
        "patch@2",
        "repair@1",
        identity="required_for_llm_mode",
    ),
    *_fixed(
        "patch.repair",
        ("repair-verified",),
        "preview",
        "ir_snapshot",
        "ir-core@1",
        "repair@1",
        snapshot="ir_content",
        identity="required_for_llm_mode",
    ),
    *_fixed(
        "patch.repair",
        ("repair-verified",),
        "config-export",
        "config_export",
        "config-export-package@1",
        "config-export@1",
    ),
    *(
        facts
        for policy in ("repair-verified", "repair-unverified")
        for rule_id, kind, schema in (
            ("checker", "checker_run", "checker-report@1"),
            ("simulation", "simulation_run", "simulation-result@1"),
            ("regression", "regression_evidence", "regression-evidence@1"),
        )
        for facts in _fixed(
            "patch.repair",
            (policy,),
            rule_id,
            kind,
            schema,
            "repair-verifier@1",
        )
    ),
    *_fixed(
        "constraint_proposal.propose",
        ("constraint-proposal-drafted",),
        "primary",
        "constraint_proposal",
        "constraint-proposal@1",
        "extraction@1",
        identity="required_for_llm_mode",
    ),
    # Standalone review/check/sim and derived suites.
    *_fixed(
        "review.run",
        ("review-completed",),
        "primary",
        "review_report",
        "review@1",
        "review@1",
        identity="required_for_llm_mode",
    ),
    *_fixed(
        "review.run",
        ("review-completed",),
        "checker",
        "checker_run",
        "checker-report@1",
        "checker@1",
    ),
    *_fixed(
        "review.run",
        ("review-completed",),
        "simulation",
        "simulation_run",
        "simulation-result@1",
        "economy-sim@1",
    ),
    *_fixed(
        "checker.run",
        ("checker-completed",),
        "primary",
        "checker_run",
        "checker-report@1",
        "checker@1",
    ),
    *_fixed(
        "simulation.run",
        ("simulation-completed",),
        "primary",
        "simulation_run",
        "simulation-result@1",
        "economy-sim@1",
    ),
    *_fixed(
        "task_suite.derive",
        ("task-suite-derived",),
        "primary",
        "task_suite",
        "task-suite@1",
        "task-suite@1",
    ),
    *_fixed(
        "task_suite.derive",
        ("task-suite-derived",),
        "scenario",
        "scenario_spec",
        "scenario-spec@1",
        "task-suite@1",
    ),
    *_fixed(
        "playtest.run",
        ("playtest-completed",),
        "primary",
        "playtest_trace",
        "playtest-trace@1",
        "playtest@1",
        identity="required_for_llm_mode",
    ),
    # Patch validation: every published Artifact is sealed by this Run producer;
    # the checker/simulation/regression implementation tool remains in EvidenceRequirement.
    *(
        facts
        for rule_id, kind, schema in (
            ("primary", "validation_evidence", "evidence-set@1"),
            ("regression", "regression_evidence", "regression-evidence@1"),
        )
        for facts in _fixed(
            "patch.validate",
            _PATCH_VALIDATION_POLICIES,
            rule_id,
            kind,
            schema,
            "patch-validation@1",
        )
    ),
    *_fixed(
        "patch.validate",
        ("patch-validation-auto-eligible",),
        "auto-apply-proof",
        "validation_evidence",
        "auto-apply-proof@1",
        "patch-validation@1",
    ),
    # Constraint candidate identity is content-derived, not the Run primary tool.
    *_fixed(
        "constraint_proposal.validate",
        _CONSTRAINT_VALIDATION_POLICIES,
        "candidate",
        "constraint_snapshot",
        "constraint-snapshot@1",
        "constraint-compile@1",
        snapshot="constraint_candidate",
    ),
    *(
        facts
        for rule_id, kind, schema in (
            ("compile-evidence", "validation_evidence", "constraint-compile-evidence@1"),
            ("primary", "validation_evidence", "evidence-set@1"),
            ("regression", "regression_evidence", "regression-evidence@1"),
        )
        for facts in _fixed(
            "constraint_proposal.validate",
            _CONSTRAINT_VALIDATION_POLICIES,
            rule_id,
            kind,
            schema,
            "constraint-validation@1",
        )
    ),
    *(
        facts
        for rule_id, kind, schema in (
            ("primary", "validation_evidence", "evidence-set@1"),
            ("regression", "regression_evidence", "regression-evidence@1"),
        )
        for facts in _fixed(
            "rollback.validate",
            _ROLLBACK_VALIDATION_POLICIES,
            rule_id,
            kind,
            schema,
            "rollback-validation@1",
        )
    ),
    *_fixed(
        "bench.run",
        ("bench-completed",),
        "primary",
        "bench_report",
        "bench-report@2",
        "bench@1",
        identity="required_for_llm_mode",
    ),
    # M4c executors are fail-closed, but the retained success interface still has
    # to be closed so enabling the reviewed M4e implementation cannot fall back to
    # a process default.
    *(
        DomainProducerRuleFacts(
            key=_key(
                "artifact.migrate",
                policy,
                "primary",
                "migration_report",
                "migration-report@1",
            ),
            tool_source="migration_profile",
        )
        for policy in _MIGRATION_POLICIES
    ),
    *_fixed(
        "dr.drill",
        ("dr-drill-completed",),
        "primary",
        "operational_evidence",
        "dr-drill-evidence@1",
        "dr-drill@1",
        operational=True,
    ),
)


def _index_entries(
    entries: Iterable[DomainProducerRuleFacts],
) -> Mapping[DomainProducerRuleKey, DomainProducerRuleFacts]:
    indexed: dict[DomainProducerRuleKey, DomainProducerRuleFacts] = {}
    for entry in entries:
        if entry.key in indexed:
            raise IntegrityViolation(
                "domain producer facts repeat an exact outcome selector",
                selector=entry.key,
            )
        indexed[entry.key] = entry
    return MappingProxyType(indexed)


class DomainProducerFactsResolver:
    """Resolve and validate producer-local facts without mutable defaults."""

    def __init__(
        self,
        entries: Iterable[DomainProducerRuleFacts] = BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES,
    ) -> None:
        self._facts = _index_entries(entries)

    @property
    def selectors(self) -> frozenset[DomainProducerRuleKey]:
        return frozenset(self._facts)

    def rule_facts(
        self,
        *,
        run_kind: RunKindRef,
        policy: OutcomeArtifactPolicyV1,
        rule: OutcomeArtifactRuleV1,
        payload_schema_id: str,
    ) -> DomainProducerRuleFacts:
        """Return facts only after closing the complete exact selector."""

        retained_rule = next(
            (candidate for candidate in policy.artifact_rules if candidate.rule_id == rule.rule_id),
            None,
        )
        if retained_rule != rule or payload_schema_id not in rule.payload_schema_ids:
            raise IntegrityViolation(
                "domain producer query differs from the exact selected outcome rule"
            )
        key = DomainProducerRuleKey(
            run_kind=run_kind.kind,
            run_kind_version=run_kind.version,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            outcome_rule_id=rule.rule_id,
            artifact_kind=rule.artifact_kind,
            payload_schema_id=payload_schema_id,
        )
        facts = self._facts.get(key)
        if facts is None:
            raise IntegrityViolation(
                "domain producer facts are not frozen for the exact outcome selector",
                selector=key,
            )
        return facts

    def requires_identity(
        self,
        *,
        run_kind: RunKindRef,
        policy: OutcomeArtifactPolicyV1,
        rule: OutcomeArtifactRuleV1,
        payload_schema_id: str,
    ) -> bool:
        """Tell the publisher whether to resolve an artifact-scoped identity."""

        return (
            self.rule_facts(
                run_kind=run_kind,
                policy=policy,
                rule=rule,
                payload_schema_id=payload_schema_id,
            ).identity_policy
            == "required_for_llm_mode"
        )

    def resolve(
        self,
        *,
        run: RunRecord,
        policy: OutcomeArtifactPolicyV1,
        rule: OutcomeArtifactRuleV1,
        lineage_policy: ArtifactLineagePolicyV1,
        payload_schema_id: str,
        canonical_payload: Mapping[str, object],
        execution_identity: ExecutionIdentityV1 | None = None,
        cassette_id: str | None = None,
        producer_env_contract_version: str | None = None,
    ) -> ResolvedDomainProducerFacts:
        """Resolve the exact producer tuple for one allocated domain Artifact."""

        retained_rule = next(
            (candidate for candidate in policy.artifact_rules if candidate.rule_id == rule.rule_id),
            None,
        )
        if retained_rule != rule:
            raise IntegrityViolation("outcome Artifact rule is not exact in its selected policy")
        facts = self.rule_facts(
            run_kind=run.kind,
            policy=policy,
            rule=rule,
            payload_schema_id=payload_schema_id,
        )
        self._validate_lineage_binding(facts=facts, rule=rule, lineage=lineage_policy)

        if run.payload.seed != run.payload.version_tuple.seed:
            raise IntegrityViolation("Run root seed differs from its frozen VersionTuple")
        producer_values: dict[str, object | None] = {
            field: None for field in VersionTuple.model_fields
        }
        producer_values["tool_version"] = self._tool_version(facts, run)
        producer_values["seed"] = (
            facts.fixed_seed if facts.seed_source == "fixed" else run.payload.seed
        )
        if producer_env_contract_version is not None and (
            rule.artifact_kind != "config_export" or payload_schema_id != "config-export-package@1"
        ):
            raise IntegrityViolation(
                "producer environment override belongs only to config export Artifacts"
            )
        producer_values["env_contract_version"] = (
            producer_env_contract_version
            if producer_env_contract_version is not None
            else run.payload.version_tuple.env_contract_version
        )

        if facts.snapshot_derivation == "ir_content":
            producer_values["ir_snapshot_id"] = compute_snapshot_id(canonical_payload)
        elif facts.snapshot_derivation == "constraint_candidate":
            producer_values["constraint_snapshot_id"] = (
                f"candidate:{canonical_sha256(canonical_payload)[:32]}"
            )

        local_mode: Literal["not_applicable", "live", "record", "replay"] = "not_applicable"
        if facts.identity_policy == "required_for_llm_mode":
            if run.payload.llm_execution_mode == "not_applicable":
                if execution_identity is not None or cassette_id is not None:
                    raise IntegrityViolation(
                        "not-applicable domain producer cannot bind an execution identity"
                    )
            else:
                if execution_identity is None:
                    raise IntegrityViolation(
                        "LLM domain producer requires an artifact execution identity",
                        outcome_rule_id=rule.rule_id,
                    )
                local_mode = run.payload.llm_execution_mode
        elif execution_identity is not None or cassette_id is not None:
            raise IntegrityViolation(
                "deterministic outcome rule cannot bind LLM execution facts",
                outcome_rule_id=rule.rule_id,
            )

        if execution_identity is not None:
            self._validate_identity(run=run, identity=execution_identity)
            producer_values.update(
                prompt_version=execution_identity.prompt_projection.tuple_value,
                model_snapshot=execution_identity.model_projection.tuple_value,
                agent_graph_version=execution_identity.agent_graph_version,
            )
            if local_mode in {"record", "replay"}:
                _require_cassette_id(cassette_id)
                producer_values["cassette_id"] = cassette_id
            elif cassette_id is not None:
                raise IntegrityViolation("LIVE domain Artifact cannot bind a cassette")
        elif cassette_id is not None:
            raise IntegrityViolation("cassette identity requires an artifact execution identity")

        replayability: Literal[
            "online_only",
            "cassette_replay",
            "deterministic_recompute",
            "operational_observation",
        ]
        if facts.operational_observation:
            replayability = "operational_observation"
        elif local_mode == "live":
            replayability = "online_only"
        elif local_mode in {"record", "replay"}:
            replayability = "cassette_replay"
        else:
            replayability = "deterministic_recompute"

        return ResolvedDomainProducerFacts(
            rule=facts,
            producer_tuple=VersionTuple.model_validate(producer_values),
            execution_identity=execution_identity,
            llm_execution_mode=local_mode,
            replayability=replayability,
        )

    def validate_registry(self, registry: DomainProducerRegistry) -> int:
        """Readiness-close every active outcome Artifact selector exactly once."""

        expected: dict[DomainProducerRuleKey, tuple[OutcomeArtifactRuleV1, object]] = {}
        for definition in registry.list_run_kinds():
            if definition.status != "active":
                continue
            for policy in definition.outcome_policies:
                for rule in policy.artifact_rules:
                    lineage = registry.get_lineage_policy(rule.lineage_policy_ref)
                    if lineage is None:
                        raise IntegrityViolation(
                            "domain producer readiness cannot resolve exact lineage policy"
                        )
                    for schema in rule.payload_schema_ids:
                        key = DomainProducerRuleKey(
                            run_kind=definition.kind,
                            run_kind_version=definition.version,
                            policy_id=policy.policy_id,
                            policy_version=policy.policy_version,
                            outcome_rule_id=rule.rule_id,
                            artifact_kind=rule.artifact_kind,
                            payload_schema_id=schema,
                        )
                        if key in expected:
                            raise IntegrityViolation(
                                "active registry repeats a domain producer selector",
                                selector=key,
                            )
                        expected[key] = (rule, lineage)

        actual_keys = set(self._facts)
        expected_keys = set(expected)
        if actual_keys != expected_keys:
            raise IntegrityViolation(
                "domain producer facts do not close the active outcome registry exactly",
                missing=tuple(sorted(expected_keys - actual_keys)),
                extra=tuple(sorted(actual_keys - expected_keys)),
            )
        for key, (rule, lineage) in expected.items():
            self._validate_lineage_binding(
                facts=self._facts[key],
                rule=rule,
                lineage=lineage,  # type: ignore[arg-type]
            )
        return len(expected)

    @staticmethod
    def _tool_version(facts: DomainProducerRuleFacts, run: RunRecord) -> str:
        if facts.tool_source == "fixed":
            assert facts.fixed_tool_version is not None
            return facts.fixed_tool_version
        params = run.payload.params
        if not isinstance(params, ArtifactMigrationPayloadV1):
            raise IntegrityViolation(
                "migration-profile tool source is bound to another Run payload"
            )
        return _profile_tool_version(params.migrator)

    @staticmethod
    def _validate_lineage_binding(
        *,
        facts: DomainProducerRuleFacts,
        rule: OutcomeArtifactRuleV1,
        lineage: ArtifactLineagePolicyV1,
    ) -> None:
        ref = rule.lineage_policy_ref
        if (
            lineage.policy_id != ref.policy_id
            or lineage.policy_version != ref.policy_version
            or artifact_lineage_policy_digest(lineage) != ref.digest
            or lineage.child_kind != rule.artifact_kind
            or tuple(lineage.child_payload_schema_ids) != tuple(rule.payload_schema_ids)
        ):
            raise IntegrityViolation(
                "domain producer lineage policy differs from the exact outcome rule"
            )

        producer_fields = {
            projection.field
            for projection in lineage.version_projection
            if projection.source == "producer_value"
        }
        # Every producer tuple has explicit null slots for the LLM fields.  A
        # deterministic sibling inside an LLM-capable Run therefore may retain a
        # frozen ``producer_value`` rule while resolving it to null; the identity
        # policy below controls whether those fields may become non-null.
        supported = {
            "tool_version",
            "seed",
            "env_contract_version",
            "prompt_version",
            "model_snapshot",
            "agent_graph_version",
            "cassette_id",
        }
        if facts.snapshot_derivation == "ir_content":
            supported.add("ir_snapshot_id")
        if facts.snapshot_derivation == "constraint_candidate":
            supported.add("constraint_snapshot_id")
        if "tool_version" not in producer_fields or not producer_fields.issubset(supported):
            raise IntegrityViolation(
                "lineage policy requests an unavailable producer-local fact",
                selector=facts.key,
                producer_fields=tuple(sorted(producer_fields)),
            )
        if facts.snapshot_derivation == "ir_content" and "ir_snapshot_id" not in producer_fields:
            raise IntegrityViolation("IR snapshot lineage does not project its content identity")
        if (
            facts.snapshot_derivation == "constraint_candidate"
            and "constraint_snapshot_id" not in producer_fields
        ):
            raise IntegrityViolation(
                "constraint candidate lineage does not project its content identity"
            )

    @staticmethod
    def _validate_identity(*, run: RunRecord, identity: ExecutionIdentityV1) -> None:
        if identity.scope != "artifact" or not identity.bindings:
            raise IntegrityViolation(
                "domain Artifact requires a non-empty artifact-scoped execution identity"
            )
        plan = run.payload.execution_version_plan
        if plan is None or identity.agent_graph_version != plan.agent_graph_version:
            raise IntegrityViolation(
                "domain Artifact execution identity differs from the frozen execution plan"
            )
        nodes = {node.agent_node_id: node for node in plan.nodes}
        for binding in identity.bindings:
            node = nodes.get(binding.agent_node_id)
            if (
                node is None
                or binding.prompt_version != node.prompt_version
                or binding.tool_version != node.tool_version
                or binding.model_snapshot not in node.allowed_model_snapshots
                or not binding.response_consumed
            ):
                raise IntegrityViolation(
                    "domain Artifact invocation is outside the frozen execution plan"
                )
            if run.current_attempt_no is not None and binding.attempt_no != run.current_attempt_no:
                raise IntegrityViolation(
                    "domain Artifact identity includes another Run attempt",
                    attempt_no=binding.attempt_no,
                )


def _profile_tool_version(profile: ProfileRefV1) -> str:
    return f"{profile.profile_id}@{profile.version}"


def _require_cassette_id(value: str | None) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise IntegrityViolation(
            "record/replay domain Artifact requires an exact content-bound cassette id"
        )


def producer_validation_context(
    *,
    facts: ResolvedDomainProducerFacts,
    lineage_policy: ArtifactLineagePolicyV1,
    projected_tuple: VersionTuple,
) -> ProducerValidationContext:
    """Build the narrow trusted context for an already projected domain tuple."""

    matrix_rule = PRODUCER_RULES[lineage_policy.child_kind]
    expected = {
        projection.field: getattr(projected_tuple, projection.field)
        for projection in lineage_policy.version_projection
        if projection.source != "constant_null" and projection.field in matrix_rule.projected_fields
    }
    conditions = matrix_rule.condition_names
    return ProducerValidationContext(
        expected_versions=expected,
        llm_execution_mode=facts.llm_execution_mode,
        has_llm_invocations=facts.execution_identity is not None,
        produced_by_agent=(
            facts.rule.identity_policy == "required_for_llm_mode"
            and facts.llm_execution_mode != "not_applicable"
        ),
        uses_dsl=("uses_dsl" in conditions and projected_tuple.constraint_snapshot_id is not None),
        uses_environment=(
            "uses_environment" in conditions and projected_tuple.env_contract_version is not None
        ),
        operational_observation=facts.rule.operational_observation,
    )


def validate_domain_artifact_producer(
    artifact: ArtifactV2,
    *,
    facts: ResolvedDomainProducerFacts,
    lineage_policy: ArtifactLineagePolicyV1,
    projected_tuple: VersionTuple,
) -> ProducerValidationReport:
    """Validate one authoritative domain Artifact against both frozen matrices."""

    if artifact.version_tuple != projected_tuple:
        raise IntegrityViolation(
            "domain Artifact VersionTuple differs from its typed lineage projection"
        )
    if artifact.meta.get("replayability") != facts.replayability:
        raise IntegrityViolation(
            "domain Artifact replayability differs from authoritative producer facts"
        )
    if artifact.meta.get("execution_identity") != facts.execution_identity:
        raise IntegrityViolation(
            "domain Artifact execution identity differs from authoritative producer facts"
        )
    return validate_artifact_producer(
        artifact,
        producer_validation_context(
            facts=facts,
            lineage_policy=lineage_policy,
            projected_tuple=projected_tuple,
        ),
    )


BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER = DomainProducerFactsResolver()


__all__ = [
    "BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES",
    "BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER",
    "DomainProducerFactsResolver",
    "DomainProducerRuleFacts",
    "DomainProducerRuleKey",
    "ResolvedDomainProducerFacts",
    "producer_validation_context",
    "validate_domain_artifact_producer",
]
