"""``task_suite_deriver@1`` — DETERMINISTIC task-suite / scenario derivation.

Turns a gated preview ``Snapshot`` + its config-export package + a
completion-oracle registry reference into ONE non-empty :class:`TaskSuiteV1` and
its N sibling :class:`ScenarioSpecV1` artifacts (frozen ``task-suite-derived``
policy: primary ``task_suite[task-suite@1]`` = 1 + N ``scenario_spec[scenario-spec@1]``
bound by the identity ``/episodes → /scenario_spec_artifact_id``). The handler is
fully deterministic — ``seed_policy=forbidden``, ``llm_modes=NA``, no findings; the
same input yields a byte-identical ``PreparedRunOutcome``.

The derivation is a pure function of the preview governed by the resolved
``derivation_profile``. The GAME-SPECIFIC scenario shaping (how many episodes,
what each covers, the deterministic env-reset payload, and the oracle binding) is
an injected :class:`ScenarioShaper` port whose concrete impl lives in
``apps/worker`` — the platform contract never hardcodes Aureus. The reset-schema
and env-contract version are PROFILE-SELECTED through the injected
:data:`EnvironmentContractResolver`, not hardcoded here.

Lineage: both the ``task_suite`` primary and every ``scenario_spec`` sibling
declare the SAME run_input roles — ``preview`` (ir_snapshot) + ``config``
(config_export) + ``constraint`` (constraint_snapshot). The frozen policy also
grants the primary a ``scenarios`` prepared_rule role, but those sibling parents
are content-addressed over the publisher-re-derived tuple and injected by the
Task-18 publisher enhancement — the handler never pre-declares them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from pydantic import JsonValue

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    ProfileRefV1,
    TaskSuiteDerivationProfileConfigV2,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    PreparedArtifact,
    PreparedRunOutcome,
    TaskSuiteDerivePayloadV1,
)
from gameforge.contracts.lineage import VersionTuple, artifact_id_v2_for
from gameforge.contracts.playtest import (
    CompletionOracleRefV1,
    CompletionOracleRegistryV1,
    CompletionOracleRegistryRefV1,
    MAX_PLAYTEST_COLLECTION_ITEMS,
    PlaytestPayloadSchemaPurposeV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
    resolve_completion_oracle,
)
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    PreparedArtifactStore,
    PreparedArtifactBatchStore,
    build_success_result,
    require_exact_profile_bindings,
    store_prepared_artifact,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.readers import SnapshotLoader, load_snapshot

TASK_SUITE_TOOL_VERSION = "task-suite@1"
TASK_SUITE_SCHEMA_ID = "task-suite@1"
SCENARIO_SPEC_SCHEMA_ID = "scenario-spec@1"
TASK_SUITE_OUTCOME_CODE = "task_suite_derived"

ENVIRONMENT_PROFILE_FIELD = "/params/environment_profile"
DERIVATION_PROFILE_FIELD = "/params/derivation_profile"


@dataclass(frozen=True, slots=True)
class ScenarioDerivationRequest:
    """Fully-resolved deterministic inputs for one scenario-shaping pass."""

    preview_snapshot: Snapshot
    source_preview_artifact_id: str
    config_export_artifact_id: str
    constraint_snapshot_artifact_id: str
    environment_profile: ProfileRefV1
    env_contract_version: str
    reset_schema_id: str
    derivation_profile: ProfileRefV1
    completion_oracle_registry_ref: CompletionOracleRegistryRefV1
    domain_scope: DomainScope


@dataclass(frozen=True, slots=True)
class ScenarioDraftV1:
    """One deterministic scenario/episode the shaper derived from the preview.

    ``reset_payload`` is the game-specific deterministic env-reset content; the
    handler wraps it in a :class:`ScenarioResetBindingV1` with the profile-selected
    ``reset_schema_id`` (the shaper never chooses the schema).
    """

    scenario_id: str
    episode_id: str
    domain_scope: DomainScope
    reset_payload: JsonValue
    completion_oracle: CompletionOracleRefV1
    step_budget: int


class ScenarioShaper(Protocol):
    """Derive the ordered scenario drafts from a preview (game-specific port)."""

    def shape(self, request: ScenarioDerivationRequest) -> tuple[ScenarioDraftV1, ...]: ...


ScenarioShaperResolver = Callable[[ProfileRefV1], ScenarioShaper]


# Resolve an environment profile ref to its frozen contract descriptor (the
# profile-selected reset_schema_id / env_contract_version). Game-agnostic — bound
# by the composition root from the platform profile catalog.
EnvironmentContractResolver = Callable[[ProfileRefV1], EnvironmentContractDescriptorV1]
DerivationConfigResolver = Callable[[ProfileRefV1], TaskSuiteDerivationProfileConfigV2]
CompletionOracleRegistryResolver = Callable[
    [CompletionOracleRegistryRefV1], CompletionOracleRegistryV1 | None
]


class PlaytestPayloadValidator(Protocol):
    def validate(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
    ) -> JsonValue: ...

    def validate_exact_contextual(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
        context: Mapping[str, JsonValue],
    ) -> JsonValue: ...


def content_addressed_artifact_id(prepared: PreparedArtifact) -> str:
    """The content-addressed id a prepared artifact WITHOUT injected siblings mints.

    A ``scenario_spec`` child's lineage is fully handler-declared (no
    ``prepared_rule`` parents), so its published (content-addressed) artifact id is
    deterministic at seal time and equals what the Task-9 publisher's
    ``build_artifact_v2`` derives. This lets each episode bind the exact future
    ``scenario_spec_artifact_id`` (the Task-18 identity binding reconciles it).
    """

    return artifact_id_v2_for(
        kind=prepared.kind,
        version_tuple=prepared.version_tuple,
        lineage=prepared.lineage,
        payload_hash=prepared.payload_hash,
        meta={**prepared.meta, "replayability": "deterministic_recompute"},
    )


@dataclass(frozen=True, slots=True)
class TaskSuiteDeriveHandler:
    """A ``RunExecutor`` for ``task_suite_deriver@1`` (deterministic, no LLM)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    scenario_shaper_resolver: ScenarioShaperResolver
    environment_contract_resolver: EnvironmentContractResolver
    derivation_config_resolver: DerivationConfigResolver
    completion_oracle_registry_resolver: CompletionOracleRegistryResolver
    payload_validator: PlaytestPayloadValidator
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding
    snapshot_loader: SnapshotLoader = load_snapshot

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, TaskSuiteDerivePayloadV1):
            raise TypeError("task_suite_deriver@1 requires a task-suite-derive@1 payload")

        profile_bindings = require_exact_profile_bindings(
            context,
            expected={
                DERIVATION_PROFILE_FIELD: (
                    payload.derivation_profile,
                    "task_suite_derivation",
                ),
                ENVIRONMENT_PROFILE_FIELD: (
                    payload.environment_profile,
                    "environment",
                ),
            },
            validator=self.profile_binding_validator,
        )
        derivation_profile = profile_bindings[DERIVATION_PROFILE_FIELD].profile
        environment_profile = profile_bindings[ENVIRONMENT_PROFILE_FIELD].profile

        preview = self.snapshot_loader(self.blobs, payload.source_preview_artifact_id)
        contract = self.environment_contract_resolver(environment_profile)
        config = self.derivation_config_resolver(derivation_profile)
        if config.target_environment_profile != environment_profile:
            raise IntegrityViolation("task-suite derivation profile targets another environment")
        if (
            config.completion_oracle_registry_version
            != payload.completion_oracle_registry_ref.registry_version
            or config.completion_oracle_registry_digest
            != payload.completion_oracle_registry_ref.digest
        ):
            raise IntegrityViolation("task-suite derivation profile binds another oracle registry")
        oracle_registry = self.completion_oracle_registry_resolver(
            payload.completion_oracle_registry_ref
        )
        if oracle_registry is None:
            raise IntegrityViolation("task-suite completion-oracle registry is unavailable")

        shaper = self.scenario_shaper_resolver(derivation_profile)
        drafts = shaper.shape(
            ScenarioDerivationRequest(
                preview_snapshot=preview,
                source_preview_artifact_id=payload.source_preview_artifact_id,
                config_export_artifact_id=payload.config_artifact_id,
                constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
                environment_profile=environment_profile,
                env_contract_version=contract.env_contract_version,
                reset_schema_id=contract.reset_schema_id,
                derivation_profile=derivation_profile,
                completion_oracle_registry_ref=payload.completion_oracle_registry_ref,
                domain_scope=self._resource_domain_scope(context),
            )
        )
        if not drafts:
            raise ValueError("scenario shaper must derive at least one scenario")
        if len(drafts) > config.max_scenarios or len(drafts) > MAX_PLAYTEST_COLLECTION_ITEMS:
            raise IntegrityViolation(
                "derived scenario count exceeds the frozen profile bound",
                scenario_count=len(drafts),
                max_scenarios=config.max_scenarios,
            )

        ordered_drafts = tuple(sorted(drafts, key=lambda item: (item.episode_id, item.scenario_id)))
        episode_ids = tuple(item.episode_id for item in ordered_drafts)
        scenario_ids = tuple(item.scenario_id for item in ordered_drafts)
        if len(episode_ids) != len(set(episode_ids)):
            raise IntegrityViolation("derived episode_id values must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise IntegrityViolation("derived scenario_id values must be unique")

        lineage = self._run_input_lineage(payload)
        frozen = context.payload.version_tuple
        if (
            frozen.ir_snapshot_id != preview.snapshot_id
            or frozen.constraint_snapshot_id is None
            or frozen.env_contract_version != contract.env_contract_version
        ):
            raise IntegrityViolation(
                "task-suite frozen VersionTuple differs from preview/environment authority"
            )
        version_tuple = VersionTuple(
            doc_version=frozen.doc_version,
            ir_snapshot_id=frozen.ir_snapshot_id,
            constraint_snapshot_id=frozen.constraint_snapshot_id,
            env_contract_version=contract.env_contract_version,
            tool_version=TASK_SUITE_TOOL_VERSION,
        )

        batch = PreparedArtifactBatchStore(
            max_bytes=config.max_total_prepared_artifact_bytes,
            max_artifacts=len(ordered_drafts) + 1,
        )
        scenarios: list[PreparedArtifact] = []
        episodes: list[TaskEpisodeV1] = []
        for draft in ordered_drafts:
            try:
                reset_payload = self.payload_validator.validate_exact_contextual(
                    schema_id=contract.reset_schema_id,
                    purpose="scenario_reset",
                    payload=draft.reset_payload,
                    context={
                        "expected_scenario_id": draft.scenario_id,
                        "expected_config_export_artifact_id": payload.config_artifact_id,
                    },
                )
            except IntegrityViolation as exc:
                raise IntegrityViolation(
                    "derived reset payload violates its exact schema",
                    reset_schema_id=contract.reset_schema_id,
                    scenario_id=draft.scenario_id,
                ) from exc
            try:
                oracle_definition = resolve_completion_oracle(
                    oracle_registry,
                    payload.completion_oracle_registry_ref,
                    draft.completion_oracle,
                )
                oracle_params = self.payload_validator.validate(
                    schema_id=oracle_definition.params_schema_id,
                    purpose="completion_oracle_params",
                    payload=draft.completion_oracle.params,
                )
            except (IntegrityViolation, ValueError) as exc:
                raise IntegrityViolation(
                    "derived completion-oracle params violate their exact schema",
                    scenario_id=draft.scenario_id,
                ) from exc
            completion_oracle = draft.completion_oracle.model_copy(update={"params": oracle_params})
            reset_binding = ScenarioResetBindingV1(
                reset_schema_id=contract.reset_schema_id,
                payload_hash=canonical_sha256(reset_payload),
                payload=reset_payload,
            )
            scenario = ScenarioSpecV1(
                scenario_id=draft.scenario_id,
                source_preview_artifact_id=payload.source_preview_artifact_id,
                config_export_artifact_id=payload.config_artifact_id,
                constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
                environment_profile=environment_profile,
                env_contract_version=contract.env_contract_version,
                domain_scope=draft.domain_scope,
                reset_binding=reset_binding,
            )
            prepared = store_prepared_artifact(
                batch,
                kind="scenario_spec",
                payload_schema_id=SCENARIO_SPEC_SCHEMA_ID,
                version_tuple=version_tuple,
                lineage=lineage,
                payload=scenario.model_dump(mode="json"),
            )
            scenarios.append(prepared)
            episodes.append(
                TaskEpisodeV1(
                    episode_id=draft.episode_id,
                    scenario_spec_artifact_id=content_addressed_artifact_id(prepared),
                    completion_oracle=completion_oracle,
                    domain_scope=draft.domain_scope,
                    reset_binding=reset_binding,
                    step_budget=draft.step_budget,
                )
            )

        suite = TaskSuiteV1(
            suite_profile=derivation_profile,
            source_preview_artifact_id=payload.source_preview_artifact_id,
            config_export_artifact_id=payload.config_artifact_id,
            constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
            environment_profile=environment_profile,
            env_contract_version=contract.env_contract_version,
            completion_oracle_registry_ref=payload.completion_oracle_registry_ref,
            episodes=tuple(episodes),
        )
        suite_artifact = store_prepared_artifact(
            batch,
            kind="task_suite",
            payload_schema_id=TASK_SUITE_SCHEMA_ID,
            version_tuple=version_tuple,
            lineage=lineage,
            payload=suite.model_dump(mode="json"),
        )

        committed = batch.commit(
            self.store,
            (suite_artifact, *scenarios),
            max_bytes=config.max_total_prepared_artifact_bytes,
        )

        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=TASK_SUITE_OUTCOME_CODE,
            primary_index=0,
            artifacts=committed,
            findings=(),
        )

    @staticmethod
    def _run_input_lineage(payload: TaskSuiteDerivePayloadV1) -> tuple[str, ...]:
        # preview + config + constraint run_input roles; the scenario_spec siblings
        # on the suite are the publisher-injected prepared_rule parents (Task-18).
        return (
            payload.source_preview_artifact_id,
            payload.config_artifact_id,
            payload.constraint_snapshot_artifact_id,
        )

    @staticmethod
    def _resource_domain_scope(context: ExecutorContextLike) -> DomainScope:
        scope = context.run.resource_domain_scope
        if not isinstance(scope, DomainScope):
            raise IntegrityViolation("task-suite Run lacks an admission-resolved resource domain")
        return scope


__all__ = [
    "DERIVATION_PROFILE_FIELD",
    "ENVIRONMENT_PROFILE_FIELD",
    "SCENARIO_SPEC_SCHEMA_ID",
    "TASK_SUITE_OUTCOME_CODE",
    "TASK_SUITE_SCHEMA_ID",
    "TASK_SUITE_TOOL_VERSION",
    "EnvironmentContractResolver",
    "ScenarioDerivationRequest",
    "ScenarioDraftV1",
    "ScenarioShaper",
    "ScenarioShaperResolver",
    "TaskSuiteDeriveHandler",
    "content_addressed_artifact_id",
]
