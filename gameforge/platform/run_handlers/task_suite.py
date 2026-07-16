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
from typing import Callable, Protocol

from pydantic import JsonValue

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import (
    EnvironmentContractDescriptorV1,
    ProfileRefV1,
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
    CompletionOracleRegistryRefV1,
    ScenarioResetBindingV1,
    ScenarioSpecV1,
    TaskEpisodeV1,
    TaskSuiteV1,
)
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    resolved_profile,
    store_prepared_artifact,
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


# Resolve an environment profile ref to its frozen contract descriptor (the
# profile-selected reset_schema_id / env_contract_version). Game-agnostic — bound
# by the composition root from the platform profile catalog.
EnvironmentContractResolver = Callable[[ProfileRefV1], EnvironmentContractDescriptorV1]


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
    scenario_shaper: ScenarioShaper
    environment_contract_resolver: EnvironmentContractResolver
    snapshot_loader: SnapshotLoader = load_snapshot

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, TaskSuiteDerivePayloadV1):
            raise TypeError("task_suite_deriver@1 requires a task-suite-derive@1 payload")

        preview = self.snapshot_loader(self.blobs, payload.source_preview_artifact_id)
        environment_profile = resolved_profile(context.payload, ENVIRONMENT_PROFILE_FIELD).profile
        derivation_profile = resolved_profile(context.payload, DERIVATION_PROFILE_FIELD).profile
        contract = self.environment_contract_resolver(environment_profile)

        drafts = self.scenario_shaper.shape(
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
            )
        )
        if not drafts:
            raise ValueError("scenario shaper must derive at least one scenario")

        lineage = self._run_input_lineage(payload)
        version_tuple = VersionTuple(
            ir_snapshot_id=preview.snapshot_id,
            constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
            env_contract_version=contract.env_contract_version,
            tool_version=TASK_SUITE_TOOL_VERSION,
        )

        scenarios: list[PreparedArtifact] = []
        episodes: list[TaskEpisodeV1] = []
        for draft in drafts:
            reset_binding = ScenarioResetBindingV1(
                reset_schema_id=contract.reset_schema_id,
                payload_hash=canonical_sha256(draft.reset_payload),
                payload=draft.reset_payload,
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
                self.store,
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
                    completion_oracle=draft.completion_oracle,
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
            self.store,
            kind="task_suite",
            payload_schema_id=TASK_SUITE_SCHEMA_ID,
            version_tuple=version_tuple,
            lineage=lineage,
            payload=suite.model_dump(mode="json"),
        )

        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code=TASK_SUITE_OUTCOME_CODE,
            primary_index=0,
            artifacts=(suite_artifact, *scenarios),
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
    "TaskSuiteDeriveHandler",
    "content_addressed_artifact_id",
]
