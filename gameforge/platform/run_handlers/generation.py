"""``generation_proposer@1`` — the bounded content-generation gate handler.

The LLM only PROPOSES ops; the DETERMINISTIC gate (spine checkers + economy
simulation over the applied preview) decides pass/reject. The gate never trusts
the model's own claim about what its ops do — it builds the ``Patch``, applies it
deterministically, and compares base vs. patched deterministic + economy findings.
That agent+gate work lives in ``gameforge.agents`` (``generator.py`` /
``gate.py``); ``platform`` cannot import it, so it is driven through an injected
:class:`GenerationAgentRunner` port whose concrete impl the composition root binds
(mirroring how 11a injected the checker/sim factories and the bench composer). The
LLM is routed ONLY through the 11a ``ModelBridgeAgentAdapter`` over
``ExecutorContext.model_bridge``.

Gate PASS (``generation_gate_passed``, run/succeeded): a ``PreparedRunResult`` with
the primary ``patch[patch@2]`` + the non-authoritative preview ``ir_snapshot[ir-core@1]``
+ one ``config_export[config-export-package@1]`` per ``/params/candidate_export_profiles``
+ gate ``checker_run`` / ``simulation_run`` / ``review_report`` evidence.

Gate REJECT (``generation_gate_rejected``, run/failed, class ``business_rule``,
terminal): a ``PreparedRunFailure`` carrying an evidence-only ``patch`` + preview +
gate evidence — NO ``config_export``, NO workflow subject. The rejected Patch keeps
``producer_run_id == run_id`` (the frozen failure-code producer exception).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Literal, Mapping, Protocol

from gameforge.contracts.agent_io import DesignGoalInput
from gameforge.contracts.config_export import (
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding, PatchV2, TypedOp
from gameforge.contracts.jobs import (
    AttemptProgressDataV1,
    GenerationProposePayloadV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunOutcome,
    ResolvedArtifactRequirementV1,
)
from gameforge.contracts.lineage import ArtifactKind
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch
from gameforge.spine.sim.economy import EconomyModel

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    PreparedArtifactBatchStore,
    PreparedArtifactStore,
    build_success_result,
    canonical_payload_bytes,
    prepared_version_tuple,
    rebind_embedded_finding_payload,
    require_exact_profile_binding,
    require_exact_profile_bindings,
    store_prepared_artifact,
    store_prepared_blob,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.model_routing import (
    BridgeModelRouter,
    build_bridge_router,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    SnapshotLoader,
    load_constraints,
    load_snapshot,
)
from gameforge.platform.run_handlers.simulation import (
    validate_economy_simulation_work_budget,
)

GENERATION_AGENT_NODE_ID = "generation"
PATCH_SCHEMA_ID = "patch@2"
GENERATION_TOOL_VERSION = "generation@1"
IR_SNAPSHOT_SCHEMA_ID = "ir-core@1"
CONFIG_EXPORT_SCHEMA_ID = "config-export-package@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"
REVIEW_SCHEMA_ID = "review@1"
# ``generation.propose`` forbids a Run root seed.  Its deterministic economy
# gate nevertheless uses this frozen producer-local seed, which is mirrored by
# the exact generation/simulation producer-fact selector at publication.
GENERATION_GATE_SIMULATION_SEED = 0

# Evidence rule -> ArtifactKind + payload schema for gate evidence artifacts.
_EVIDENCE_KIND: dict[str, tuple[ArtifactKind, str]] = {
    "checker": ("checker_run", CHECKER_REPORT_SCHEMA_ID),
    "simulation": ("simulation_run", SIMULATION_RESULT_SCHEMA_ID),
    "review": ("review_report", REVIEW_SCHEMA_ID),
}


@dataclass(frozen=True, slots=True)
class PreparedEvidenceV1:
    """One deterministic-oracle evidence payload the gate/verifier produced.

    ``outcome_rule_id`` is the frozen policy rule (``checker`` / ``simulation`` /
    ``review`` / ``regression``); ``requirement_id`` is the resolved-policy
    requirement the artifact maps one-to-one onto (``resolved(<id>,<rule>)``).
    """

    outcome_rule_id: str
    requirement_id: str
    payload: Mapping[str, object]
    findings: tuple[Finding, ...] = ()
    # Conditional producer fact for evidence that actually consumed an Env.
    # Regression suites may each bind a different contract, so this cannot be
    # projected safely from the Run-wide VersionTuple.
    env_contract_version: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationRunRequest:
    """Fully-resolved inputs for one generation-gate agent invocation."""

    snapshot: Snapshot
    constraints: tuple[Constraint, ...]
    goal: DesignGoalInput
    prompt_version: str
    findings: tuple[Finding, ...]
    gate_requirements: tuple[ResolvedArtifactRequirementV1, ...]
    gate_simulation_seed: int
    gate_simulation_population: int
    gate_simulation_horizon_steps: int
    max_checker_work_units: int
    max_simulation_work_units: int
    router: BridgeModelRouter


@dataclass(frozen=True, slots=True)
class GenerationGateOutcomeV1:
    """The deterministic result of one generation-gate run (LLM propose + gate).

    ``passed`` is the DETERMINISTIC gate verdict (``gate_proposal``); the LLM only
    produced ``ops``. ``preview_payload`` is the applied preview content (always
    present — even a rejected proposal is re-checkable), and the evidence tuples
    carry the checker/sim/review the gate judged.
    """

    ops: tuple[TypedOp, ...]
    passed: bool
    preview_payload: Mapping[str, object]
    preview_snapshot_id: str
    checker_evidence: tuple[PreparedEvidenceV1, ...]
    simulation_evidence: tuple[PreparedEvidenceV1, ...]
    review_evidence: tuple[PreparedEvidenceV1, ...]
    expected_to_fix: tuple[str, ...] = ()
    side_effect_risk: str = "low"


class GenerationAgentRunner(Protocol):
    """Drive the M2 content generator + deterministic gate for one proposal."""

    def run(self, request: GenerationRunRequest) -> GenerationGateOutcomeV1: ...


@dataclass(frozen=True, slots=True)
class GenerationExecutionConfig:
    max_constraint_count: int
    max_work_units: int
    gate_simulation_seed: int
    gate_simulation_population: int
    gate_simulation_horizon_steps: int
    max_simulation_work_units: int
    max_prompt_message_bytes: int = 16 * 1024 * 1024
    max_candidate_export_profiles: int = 16
    max_total_prepared_artifact_bytes: int = 128 * 1024 * 1024


class GenerationExecutionConfigResolver(Protocol):
    def __call__(self, binding: ResolvedExecutionProfileBindingV1) -> GenerationExecutionConfig: ...


class ConfigExporter(Protocol):
    """Export one preview+constraint pair into a versioned config package.

    Game-specific adapters live outside ``platform`` (the platform contract never
    hardcodes Aureus/Flare), so the exporter is injected; the composition root
    binds the real versioned adapter and tests bind a double.
    """

    def export(
        self,
        *,
        export_profile: ProfileRefV1,
        export_profile_binding: ResolvedExecutionProfileBindingV1,
        run_kind: RunKindRef | None,
        llm_execution_mode: Literal["not_applicable", "live", "record", "replay"],
        preview_snapshot_id: str,
        preview_payload: Mapping[str, object],
        constraint_snapshot_artifact_id: str,
        constraints: tuple[Constraint, ...],
    ) -> ConfigExportPackageV1: ...


def config_export_profile_binding(
    context: ExecutorContextLike,
    *,
    index: int,
    profile: ProfileRefV1,
) -> ResolvedExecutionProfileBindingV1:
    """Rebind one collection member to its exact catalog/profile authority."""

    field_path = f"/params/candidate_export_profiles/{index}"
    return require_exact_profile_binding(
        context,
        field_path=field_path,
        profile=profile,
        profile_kind="config_export",
    )


GoalLoader = Callable[[ArtifactBlobReader, str], DesignGoalInput]
FindingLoader = Callable[[ArtifactBlobReader, GenerationProposePayloadV1], tuple[Finding, ...]]


def load_goal(blobs: ArtifactBlobReader, artifact_id: str) -> DesignGoalInput:
    """Read the ``objective_goal`` source artifact into a ``DesignGoalInput``.

    Authenticated goal sources are stored as exact UTF-8 text bytes. The snapshot
    identity is producer authority, not user content, and is rebound after the
    exact base snapshot is loaded.
    """

    try:
        goal = blobs.read_bytes(artifact_id).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("objective_goal source artifact must be UTF-8 text") from exc
    if not goal:
        raise ValueError("objective_goal source artifact must be non-empty")
    return DesignGoalInput(goal=goal, grounding_snapshot_id="pending-exact-base")


def _no_findings(
    blobs: ArtifactBlobReader, payload: GenerationProposePayloadV1
) -> tuple[Finding, ...]:
    """Generation grounds on the goal, not the finding set; default to empty.

    The finding evidence artifact ids are still folded into the Patch lineage; a
    specialised wiring may inject a loader that materialises the spine findings for
    a richer generation prompt.
    """

    return ()


@dataclass(frozen=True, slots=True)
class GenerationProposalHandler:
    """A ``RunExecutor`` for ``generation_proposer@1``."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    agent_runner: GenerationAgentRunner
    config_exporter: ConfigExporter
    execution_config_resolver: GenerationExecutionConfigResolver
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    goal_loader: GoalLoader = load_goal
    finding_loader: FindingLoader = field(default=_no_findings)
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, GenerationProposePayloadV1):
            raise TypeError("generation_proposer@1 requires a generation-propose@1 payload")

        profile_bindings = require_exact_profile_bindings(
            context,
            expected={
                "/params/generation_policy": (payload.generation_policy, "generation"),
                **{
                    f"/params/candidate_export_profiles/{index}": (
                        profile,
                        "config_export",
                    )
                    for index, profile in enumerate(payload.candidate_export_profiles)
                },
            },
            validator=self.profile_binding_validator,
        )

        execution_config = self.execution_config_resolver(
            profile_bindings["/params/generation_policy"]
        )
        if len(payload.candidate_export_profiles) > execution_config.max_candidate_export_profiles:
            raise ValueError("generation candidate export profiles exceed the profile count budget")

        snapshot = self.snapshot_loader(self.blobs, payload.base_snapshot_artifact_id)
        constraints = self._constraints(payload)
        self._validate_execution_config(execution_config, snapshot, constraints)
        goal = self.goal_loader(self.blobs, payload.objective_goal.source_artifact_id).model_copy(
            update={"grounding_snapshot_id": snapshot.snapshot_id}
        )
        findings = self.finding_loader(self.blobs, payload)
        router = build_bridge_router(
            context=context,
            agent_node_id=GENERATION_AGENT_NODE_ID,
            max_prompt_message_bytes=execution_config.max_prompt_message_bytes,
            source_artifact_ids=tuple(
                sorted(
                    (
                        payload.base_snapshot_artifact_id,
                        payload.objective_goal.source_artifact_id,
                        *(binding.evidence_artifact_id for binding in payload.findings),
                    )
                )
            ),
        )

        outcome = self.agent_runner.run(
            GenerationRunRequest(
                snapshot=snapshot,
                constraints=tuple(constraints),
                goal=goal,
                prompt_version=self._generation_prompt_version(context),
                findings=findings,
                gate_requirements=self._gate_requirements(context),
                gate_simulation_seed=execution_config.gate_simulation_seed,
                gate_simulation_population=(execution_config.gate_simulation_population),
                gate_simulation_horizon_steps=(execution_config.gate_simulation_horizon_steps),
                max_checker_work_units=execution_config.max_work_units,
                max_simulation_work_units=execution_config.max_simulation_work_units,
                router=router,
            )
        )
        self._validate_preview_replay(snapshot, outcome)
        if context.progress_publisher is not None:
            context.progress_publisher(
                AttemptProgressDataV1(
                    attempt_no=context.attempt.attempt_no,
                    phase_code="generation.preliminary_gate",
                    completed_units=1,
                    total_units=1,
                )
            )

        run_id = context.run.run_id
        batch = PreparedArtifactBatchStore(
            max_bytes=execution_config.max_total_prepared_artifact_bytes
        )
        staged_handler = replace(self, store=batch)
        patch = staged_handler._seal_patch(
            context, payload, snapshot, outcome, run_id, terminal=not outcome.passed
        )
        preview = staged_handler._seal_preview(context, payload, outcome)
        evidence = staged_handler._seal_gate_evidence(context, payload, snapshot, outcome)

        if outcome.passed:
            configs = staged_handler._seal_configs(context, payload, outcome, constraints)
            staged_artifacts = (patch, preview, *configs, *evidence)
            artifacts = batch.commit(
                self.store,
                staged_artifacts,
                max_bytes=execution_config.max_total_prepared_artifact_bytes,
            )
            # generation-gate-pass@1 has no finding-output policy: the gate findings
            # live inside the checker/sim/review evidence payloads, not as a
            # PreparedFinding series.
            return build_success_result(
                run=context.run,
                attempt=context.attempt,
                outcome_code="generation_gate_passed",
                primary_index=0,
                artifacts=artifacts,
                findings=(),
            )

        # Gate REJECT: evidence-only patch + preview + gate evidence, NO config_export,
        # NO workflow subject; terminal business-rule failure.
        artifacts = batch.commit(
            self.store,
            (patch, preview, *evidence),
            max_bytes=execution_config.max_total_prepared_artifact_bytes,
        )
        return PreparedRunFailure(
            run_id=run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=artifacts,
            requirement_dispositions=(),
            cause_code="generation_gate_rejected",
            failure_class="business_rule",
            intrinsic_retry_eligible=False,
            classifier=context.run.failure_classifier,
            redacted_message="content generation proposal rejected by the deterministic gate",
        )

    # ------------------------------------------------------------------ inputs
    def _constraints(self, payload: GenerationProposePayloadV1) -> list[Constraint]:
        if payload.constraint_snapshot_artifact_id is None:
            return []
        return self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id)

    @staticmethod
    def _generation_prompt_version(context: ExecutorContextLike) -> str:
        plan = context.payload.execution_version_plan
        if plan is None:
            raise ValueError("generation Run lacks an exact execution version plan")
        matches = tuple(
            node for node in plan.nodes if node.agent_node_id == GENERATION_AGENT_NODE_ID
        )
        if len(matches) != 1:
            raise ValueError("generation execution plan lacks one exact generation node")
        return matches[0].prompt_version

    @staticmethod
    def _validate_execution_config(
        config: GenerationExecutionConfig,
        snapshot: Snapshot,
        constraints: list[Constraint],
    ) -> None:
        work_units = max(
            1,
            len(snapshot.entities) * len(snapshot.entities)
            + len(snapshot.entities)
            + len(snapshot.relations),
        ) * (1 + len(constraints))
        if len(constraints) > config.max_constraint_count or work_units > config.max_work_units:
            raise ValueError("generation checker gate exceeds the exact profile work budget")
        if config.gate_simulation_seed != GENERATION_GATE_SIMULATION_SEED:
            raise ValueError("generation gate seed differs from frozen producer facts")
        if config.gate_simulation_population < 1 or config.gate_simulation_horizon_steps < 1:
            raise ValueError("generation simulation budget is outside exact profile bounds")
        validate_economy_simulation_work_budget(
            EconomyModel.from_snapshot(snapshot),
            n_agents=config.gate_simulation_population,
            n_ticks=config.gate_simulation_horizon_steps,
            replication_count=1,
            max_work_units=config.max_simulation_work_units,
        )

    @staticmethod
    def _gate_requirements(
        context: ExecutorContextLike,
    ) -> tuple[ResolvedArtifactRequirementV1, ...]:
        snapshots = context.payload.resolved_policy_snapshots
        if len(snapshots) != 1:
            raise ValueError("generation run requires exactly one frozen gate policy snapshot")
        return snapshots[0].requirements

    @staticmethod
    def _validate_preview_replay(
        snapshot: Snapshot,
        outcome: GenerationGateOutcomeV1,
    ) -> None:
        """Bind a claimed gate preview to the exact proposed ops at the handler seam."""

        if outcome.passed and not outcome.ops:
            raise ValueError("generation gate pass contains no proposed operations")
        candidate = PatchV2(
            revision=1,
            base_snapshot_id=snapshot.snapshot_id,
            target_snapshot_id=outcome.preview_snapshot_id,
            expected_to_fix=list(outcome.expected_to_fix),
            side_effect_risk=outcome.side_effect_risk,
            ops=list(outcome.ops),
            produced_by="agent",
            producer_run_id="validation:generation-runner",
            rationale="validate generation runner preview",
        )
        try:
            replayed = apply_patch(snapshot, candidate)
        except PatchRejected as exc:
            if outcome.passed:
                raise ValueError("generation gate pass cannot replay on the exact base") from exc
            replayed = snapshot
        try:
            exact_payload = canonical_payload_bytes(outcome.preview_payload)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("generation gate preview is not canonical JSON") from exc
        if (
            replayed.snapshot_id != outcome.preview_snapshot_id
            or canonical_payload_bytes(replayed.content_payload) != exact_payload
        ):
            raise ValueError("generation gate preview differs from exact-base replay")

    # ----------------------------------------------------------------- sealing
    def _seal_patch(
        self,
        context: ExecutorContextLike,
        payload: GenerationProposePayloadV1,
        snapshot: Snapshot,
        outcome: GenerationGateOutcomeV1,
        run_id: str,
        *,
        terminal: bool,
    ) -> PreparedArtifact:
        patch = PatchV2(
            revision=1,
            base_snapshot_id=snapshot.snapshot_id,
            target_snapshot_id=outcome.preview_snapshot_id,
            expected_to_fix=list(outcome.expected_to_fix),
            side_effect_risk=outcome.side_effect_risk,
            ops=list(outcome.ops),
            produced_by="agent",
            producer_run_id=run_id,
            rationale=(
                "generation gate rejected proposal (retained for review)"
                if terminal
                else "gated content generation proposal"
            ),
        )
        return store_prepared_artifact(
            self.store,
            kind="patch",
            payload_schema_id=PATCH_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=GENERATION_TOOL_VERSION,
                projected_fields=("doc_version", "constraint_snapshot_id"),
                overrides={"ir_snapshot_id": snapshot.snapshot_id},
            ),
            lineage=self._patch_lineage(payload),
            payload=patch.model_dump(mode="json"),
        )

    def _seal_preview(
        self,
        context: ExecutorContextLike,
        payload: GenerationProposePayloadV1,
        outcome: GenerationGateOutcomeV1,
    ) -> PreparedArtifact:
        return store_prepared_artifact(
            self.store,
            kind="ir_snapshot",
            payload_schema_id=IR_SNAPSHOT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=GENERATION_TOOL_VERSION,
                projected_fields=("doc_version",),
                overrides={"ir_snapshot_id": outcome.preview_snapshot_id},
            ),
            lineage=self._preview_lineage(payload),
            payload=outcome.preview_payload,
        )

    def _seal_configs(
        self,
        context: ExecutorContextLike,
        payload: GenerationProposePayloadV1,
        outcome: GenerationGateOutcomeV1,
        constraints: list[Constraint],
    ) -> tuple[PreparedArtifact, ...]:
        # constraint_snapshot_artifact_id is guaranteed non-null when export
        # profiles exist (payload validator), so config lineage is well-formed.
        constraint_id = payload.constraint_snapshot_artifact_id
        assert constraint_id is not None
        artifacts: list[PreparedArtifact] = []
        for index, profile in enumerate(payload.candidate_export_profiles):
            binding = config_export_profile_binding(context, index=index, profile=profile)
            package = self.config_exporter.export(
                export_profile=profile,
                export_profile_binding=binding,
                run_kind=context.run.kind,
                llm_execution_mode=context.payload.llm_execution_mode,
                preview_snapshot_id=outcome.preview_snapshot_id,
                preview_payload=outcome.preview_payload,
                constraint_snapshot_artifact_id=constraint_id,
                constraints=tuple(constraints),
            )
            artifacts.append(
                store_prepared_blob(
                    self.store,
                    kind="config_export",
                    payload_schema_id=CONFIG_EXPORT_SCHEMA_ID,
                    version_tuple=prepared_version_tuple(
                        context,
                        tool_version="config-export@1",
                        projected_fields=("doc_version", "constraint_snapshot_id"),
                        overrides={
                            "ir_snapshot_id": outcome.preview_snapshot_id,
                            "env_contract_version": package.env_contract_version,
                        },
                    ),
                    lineage=(constraint_id,),
                    blob=canonical_config_export_bytes(package),
                    extra_meta={
                        "export_profile": profile.model_dump(mode="json"),
                    },
                )
            )
        return tuple(artifacts)

    def _seal_gate_evidence(
        self,
        context: ExecutorContextLike,
        payload: GenerationProposePayloadV1,
        snapshot: Snapshot,
        outcome: GenerationGateOutcomeV1,
    ) -> tuple[PreparedArtifact, ...]:
        # Gate checker/sim/review evidence is grounded on the PREVIEW (a prepared
        # sibling the publisher injects) + optional constraint — NOT the base
        # snapshot (frozen generation-gate checker/sim/review lineage roles).
        lineage = self._evidence_lineage(payload)
        artifacts: list[PreparedArtifact] = []
        for group in (
            outcome.checker_evidence,
            outcome.simulation_evidence,
            outcome.review_evidence,
        ):
            for item in group:
                kind, schema = _EVIDENCE_KIND[item.outcome_rule_id]
                producer_overrides: dict[str, object] = {
                    "ir_snapshot_id": outcome.preview_snapshot_id
                }
                if kind == "simulation_run":
                    producer_overrides["seed"] = GENERATION_GATE_SIMULATION_SEED
                artifacts.append(
                    store_prepared_artifact(
                        self.store,
                        kind=kind,
                        payload_schema_id=schema,
                        version_tuple=prepared_version_tuple(
                            context,
                            tool_version="generation-gate@1",
                            projected_fields=("constraint_snapshot_id",),
                            overrides=producer_overrides,
                        ),
                        lineage=lineage,
                        payload=rebind_embedded_finding_payload(
                            item.payload, run_id=context.run.run_id
                        ),
                        extra_meta={"requirement_id": item.requirement_id},
                    )
                )
        return tuple(artifacts)

    # ------------------------------------------------------------------ lineage
    def _patch_lineage(self, payload: GenerationProposePayloadV1) -> tuple[str, ...]:
        lineage = [payload.base_snapshot_artifact_id]
        if payload.constraint_snapshot_artifact_id is not None:
            lineage.append(payload.constraint_snapshot_artifact_id)
        lineage.append(payload.objective_goal.source_artifact_id)
        lineage.extend(binding.evidence_artifact_id for binding in payload.findings)
        return tuple(lineage)

    def _preview_lineage(self, payload: GenerationProposePayloadV1) -> tuple[str, ...]:
        # new preview = base + patch(prepared sibling, publisher-injected); the
        # frozen preview lineage has NO constraint role.
        return (payload.base_snapshot_artifact_id,)

    def _evidence_lineage(self, payload: GenerationProposePayloadV1) -> tuple[str, ...]:
        # gate checker/sim/review = preview(prepared sibling) + optional constraint;
        # the base snapshot is NOT a role on the gate evidence.
        if payload.constraint_snapshot_artifact_id is not None:
            return (payload.constraint_snapshot_artifact_id,)
        return ()


__all__ = [
    "CONFIG_EXPORT_SCHEMA_ID",
    "GENERATION_AGENT_NODE_ID",
    "GENERATION_GATE_SIMULATION_SEED",
    "IR_SNAPSHOT_SCHEMA_ID",
    "PATCH_SCHEMA_ID",
    "ConfigExporter",
    "config_export_profile_binding",
    "GenerationAgentRunner",
    "GenerationExecutionConfig",
    "GenerationExecutionConfigResolver",
    "GenerationGateOutcomeV1",
    "GenerationProposalHandler",
    "GenerationRunRequest",
    "PreparedEvidenceV1",
    "load_goal",
]
