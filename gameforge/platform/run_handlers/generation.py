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

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from gameforge.contracts.agent_io import DesignGoalInput
from gameforge.contracts.config_export import (
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import Finding, PatchV2, TypedOp
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunOutcome,
)
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    load_json_blob,
    store_prepared_artifact,
    store_prepared_blob,
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

GENERATION_AGENT_NODE_ID = "generation"
PATCH_SCHEMA_ID = "patch@2"
IR_SNAPSHOT_SCHEMA_ID = "ir-core@1"
CONFIG_EXPORT_SCHEMA_ID = "config-export-package@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"
REVIEW_SCHEMA_ID = "review@1"

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


@dataclass(frozen=True, slots=True)
class GenerationRunRequest:
    """Fully-resolved inputs for one generation-gate agent invocation."""

    snapshot: Snapshot
    constraints: tuple[Constraint, ...]
    goal: DesignGoalInput
    findings: tuple[Finding, ...]
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
        preview_snapshot_id: str,
        preview_payload: Mapping[str, object],
        constraint_snapshot_artifact_id: str,
        constraints: tuple[Constraint, ...],
    ) -> ConfigExportPackageV1: ...


GoalLoader = Callable[[ArtifactBlobReader, str], DesignGoalInput]
FindingLoader = Callable[[ArtifactBlobReader, GenerationProposePayloadV1], tuple[Finding, ...]]


def load_goal(blobs: ArtifactBlobReader, artifact_id: str) -> DesignGoalInput:
    """Read the ``objective_goal`` source artifact into a ``DesignGoalInput``.

    The bound source artifact is canonical JSON ``{"goal": <text>,
    "grounding_snapshot_id": <id>}``; ``grounding_snapshot_id`` is advisory (the
    handler re-binds the goal to the exact loaded base snapshot).
    """

    payload = load_json_blob(blobs, artifact_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("goal"), str):
        raise ValueError("objective_goal source artifact must carry a goal string")
    return DesignGoalInput(
        goal=payload["goal"],
        grounding_snapshot_id=str(payload.get("grounding_snapshot_id", "")),
    )


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
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    goal_loader: GoalLoader = load_goal
    finding_loader: FindingLoader = field(default=_no_findings)

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, GenerationProposePayloadV1):
            raise TypeError("generation_proposer@1 requires a generation-propose@1 payload")

        snapshot = self.snapshot_loader(self.blobs, payload.base_snapshot_artifact_id)
        constraints = self._constraints(payload)
        goal = self.goal_loader(self.blobs, payload.objective_goal.source_artifact_id)
        findings = self.finding_loader(self.blobs, payload)
        router = build_bridge_router(context=context, agent_node_id=GENERATION_AGENT_NODE_ID)

        outcome = self.agent_runner.run(
            GenerationRunRequest(
                snapshot=snapshot,
                constraints=tuple(constraints),
                goal=goal,
                findings=findings,
                router=router,
            )
        )

        run_id = context.run.run_id
        patch = self._seal_patch(payload, snapshot, outcome, run_id, terminal=not outcome.passed)
        preview = self._seal_preview(payload, outcome)
        evidence = self._seal_gate_evidence(payload, snapshot, outcome)

        if outcome.passed:
            configs = self._seal_configs(payload, outcome, constraints)
            artifacts = (patch, preview, *configs, *evidence)
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
        return PreparedRunFailure(
            run_id=run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=(patch, preview, *evidence),
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

    # ----------------------------------------------------------------- sealing
    def _seal_patch(
        self,
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
            version_tuple=VersionTuple(
                ir_snapshot_id=snapshot.snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                tool_version="generation@1",
            ),
            lineage=self._patch_lineage(payload),
            payload=patch.model_dump(mode="json"),
        )

    def _seal_preview(
        self, payload: GenerationProposePayloadV1, outcome: GenerationGateOutcomeV1
    ) -> PreparedArtifact:
        return store_prepared_artifact(
            self.store,
            kind="ir_snapshot",
            payload_schema_id=IR_SNAPSHOT_SCHEMA_ID,
            version_tuple=VersionTuple(
                ir_snapshot_id=outcome.preview_snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                tool_version="generation@1",
            ),
            lineage=self._preview_lineage(payload),
            payload=outcome.preview_payload,
        )

    def _seal_configs(
        self,
        payload: GenerationProposePayloadV1,
        outcome: GenerationGateOutcomeV1,
        constraints: list[Constraint],
    ) -> tuple[PreparedArtifact, ...]:
        # constraint_snapshot_artifact_id is guaranteed non-null when export
        # profiles exist (payload validator), so config lineage is well-formed.
        constraint_id = payload.constraint_snapshot_artifact_id
        assert constraint_id is not None
        artifacts: list[PreparedArtifact] = []
        for profile in payload.candidate_export_profiles:
            package = self.config_exporter.export(
                export_profile=profile,
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
                    version_tuple=VersionTuple(
                        ir_snapshot_id=outcome.preview_snapshot_id,
                        constraint_snapshot_id=constraint_id,
                        tool_version="config-export@1",
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
                artifacts.append(
                    store_prepared_artifact(
                        self.store,
                        kind=kind,
                        payload_schema_id=schema,
                        version_tuple=VersionTuple(
                            ir_snapshot_id=outcome.preview_snapshot_id,
                            constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                            tool_version="generation-gate@1",
                        ),
                        lineage=lineage,
                        payload=item.payload,
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
    "IR_SNAPSHOT_SCHEMA_ID",
    "PATCH_SCHEMA_ID",
    "ConfigExporter",
    "GenerationAgentRunner",
    "GenerationGateOutcomeV1",
    "GenerationProposalHandler",
    "GenerationRunRequest",
    "PreparedEvidenceV1",
    "load_goal",
]
