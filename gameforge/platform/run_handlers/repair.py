"""``repair_search@1`` — the verifier-guided patch-repair handler.

The LLM only DRAFTS patches; the DETERMINISTIC verifier (spine checkers + economy
simulation + headless game regression) decides whether a draft genuinely resolves
the target defect without regressing anything. That agent+verifier work lives in
``gameforge.agents.repair`` (``search.py`` / ``verify.py`` / ``drafter.py``);
``platform`` cannot import it, so it is driven through an injected
:class:`RepairAgentRunner` port with the LLM routed through the 11a
``ModelBridgeAgentAdapter``.

VERIFIED (``repair_verified``, run/succeeded): ONE full combined exact-base
superseding ``patch[patch@2]`` produced only AFTER verifier closure, plus the new
preview ``ir_snapshot[ir-core@1]``, one ``config_export`` per
``/params/candidate_export_profiles``, and ``checker_run`` / ``simulation_run`` /
``regression_evidence`` evidence. The superseding patch's ``doc_version`` /
``ir_snapshot_id`` inherit from the exact CURRENT-ref base, NOT the unapproved
preview.

UNVERIFIED (``repair_unverified``, run/failed, class ``validation``, terminal): a
``PreparedRunFailure`` with NO new patch/preview/config; every frozen requirement
(each checker profile, simulation profile, regression suite) carries a produced /
``not_executed`` ``RequirementDispositionV1`` — no silent omission. The SubjectHead
is NOT advanced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from gameforge.contracts.config_export import canonical_config_export_bytes
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import Finding, PatchV2, TypedOp, parse_patch
from gameforge.contracts.jobs import (
    PatchRepairPayloadV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunOutcome,
    RequirementDispositionV1,
)
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.spine.checkers.base import Checker
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
from gameforge.platform.run_handlers.generation import (
    CONFIG_EXPORT_SCHEMA_ID,
    IR_SNAPSHOT_SCHEMA_ID,
    PATCH_SCHEMA_ID,
    ConfigExporter,
    PreparedEvidenceV1,
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
from gameforge.platform.run_handlers.review import ReviewSimConfig

REPAIR_AGENT_NODE_ID = "repair"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"
REGRESSION_EVIDENCE_SCHEMA_ID = "regression-evidence@1"
REPAIR_TOOL_VERSION = "repair@1"
RESOLVED_POLICY_ID = "repair-verifier"
_UNVERIFIED_REASON = "search_exhausted"

_EVIDENCE_KIND: dict[str, tuple[ArtifactKind, str]] = {
    "checker": ("checker_run", CHECKER_REPORT_SCHEMA_ID),
    "simulation": ("simulation_run", SIMULATION_RESULT_SCHEMA_ID),
    "regression": ("regression_evidence", REGRESSION_EVIDENCE_SCHEMA_ID),
}

CheckerResolver = Callable[[ProfileRefV1, list[Constraint]], Checker]
SimConfigResolver = Callable[[ProfileRefV1], ReviewSimConfig]
FindingLoader = Callable[[ArtifactBlobReader, PatchRepairPayloadV1], tuple[Finding, ...]]


def requirement_id_for_profile(profile: ProfileRefV1) -> str:
    """The frozen resolved-policy requirement id projected from a profile ref."""

    return f"{profile.profile_id}@{profile.version}"


@dataclass(frozen=True, slots=True)
class RepairRunRequest:
    """Fully-resolved inputs for one verifier-guided repair search."""

    base_snapshot: Snapshot
    constraints: tuple[Constraint, ...]
    findings: tuple[Finding, ...]
    checkers: tuple[tuple[ProfileRefV1, Checker], ...]
    simulation_profiles: tuple[tuple[ProfileRefV1, ReviewSimConfig], ...]
    regression_suite_artifact_ids: tuple[str, ...]
    router: BridgeModelRouter
    max_steps: int = 4
    run_regression: bool = True


@dataclass(frozen=True, slots=True)
class RepairSearchOutcomeV1:
    """The deterministic result of one repair search (LLM draft + verifier).

    ``passed_verification`` is the DETERMINISTIC verifier verdict; the LLM only
    produced ``ops``. Preview + evidence are present ONLY on closure; an
    unverified search advances nothing.
    """

    passed_verification: bool
    search_steps: int
    ops: tuple[TypedOp, ...] = ()
    preview_payload: object | None = None
    preview_snapshot_id: str | None = None
    expected_to_fix: tuple[str, ...] = ()
    side_effect_risk: str = "low"
    checker_evidence: tuple[PreparedEvidenceV1, ...] = ()
    simulation_evidence: tuple[PreparedEvidenceV1, ...] = ()
    regression_evidence: tuple[PreparedEvidenceV1, ...] = ()


class RepairAgentRunner(Protocol):
    """Drive the M2 verifier-guided repair search for one target defect."""

    def search(self, request: RepairRunRequest) -> RepairSearchOutcomeV1: ...


def _no_finding_loader(
    blobs: ArtifactBlobReader, payload: PatchRepairPayloadV1
) -> tuple[Finding, ...]:
    raise ValueError(
        "repair_search@1 requires a finding_loader that materialises the target findings"
    )


@dataclass(frozen=True, slots=True)
class RepairSearchHandler:
    """A ``RunExecutor`` for ``repair_search@1``."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    agent_runner: RepairAgentRunner
    config_exporter: ConfigExporter
    checker_resolver: CheckerResolver
    sim_config_resolver: SimConfigResolver
    finding_loader: FindingLoader = _no_finding_loader
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    max_steps: int = 4

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PatchRepairPayloadV1):
            raise TypeError("repair_search@1 requires a patch-repair@1 payload")

        base_snapshot = self.snapshot_loader(self.blobs, payload.base_snapshot_artifact_id)
        constraints = self._constraints(payload)
        current_patch = self._load_current_patch(payload)
        findings = self.finding_loader(self.blobs, payload)
        checkers = tuple(
            (profile, self.checker_resolver(profile, constraints))
            for profile in payload.checker_profiles
        )
        sim_profiles = tuple(
            (profile, self.sim_config_resolver(profile)) for profile in payload.simulation_profiles
        )
        router = build_bridge_router(context=context, agent_node_id=REPAIR_AGENT_NODE_ID)

        outcome = self.agent_runner.search(
            RepairRunRequest(
                base_snapshot=base_snapshot,
                constraints=tuple(constraints),
                findings=findings,
                checkers=checkers,
                simulation_profiles=sim_profiles,
                regression_suite_artifact_ids=tuple(payload.regression_suite_artifact_ids),
                router=router,
                max_steps=self.max_steps,
                run_regression=bool(payload.regression_suite_artifact_ids),
            )
        )

        if outcome.passed_verification:
            return self._verified(
                context, payload, base_snapshot, current_patch, constraints, outcome
            )
        return self._unverified(context, payload)

    # --------------------------------------------------------------- verified
    def _verified(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        current_patch: PatchV2,
        constraints: list[Constraint],
        outcome: RepairSearchOutcomeV1,
    ) -> PreparedRunOutcome:
        assert outcome.preview_payload is not None and outcome.preview_snapshot_id is not None
        run_id = context.run.run_id
        patch = self._seal_superseding_patch(payload, base_snapshot, current_patch, outcome, run_id)
        preview = self._seal_preview(payload, base_snapshot, outcome)
        configs = self._seal_configs(payload, outcome, constraints)
        evidence = self._seal_evidence(payload, base_snapshot, outcome)
        artifacts = (patch, preview, *configs, *evidence)
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="repair_verified",
            primary_index=0,
            artifacts=artifacts,
            findings=(),
        )

    def _seal_superseding_patch(
        self,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        current_patch: PatchV2,
        outcome: RepairSearchOutcomeV1,
        run_id: str,
    ) -> PreparedArtifact:
        assert outcome.preview_snapshot_id is not None
        patch = PatchV2(
            revision=current_patch.revision + 1,
            supersedes_artifact_id=payload.subject_patch_artifact_id,
            base_snapshot_id=base_snapshot.snapshot_id,
            target_snapshot_id=outcome.preview_snapshot_id,
            expected_to_fix=list(outcome.expected_to_fix),
            side_effect_risk=outcome.side_effect_risk,
            ops=list(outcome.ops),
            produced_by="agent",
            producer_run_id=run_id,
            rationale="verifier-closed repair (superseding patch revision)",
        )
        return store_prepared_artifact(
            self.store,
            kind="patch",
            payload_schema_id=PATCH_SCHEMA_ID,
            # doc_version / ir_snapshot_id inherit from the exact CURRENT-ref base,
            # NOT the unapproved preview (spec §5.5 repair-patch lineage).
            version_tuple=VersionTuple(
                ir_snapshot_id=base_snapshot.snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                tool_version=REPAIR_TOOL_VERSION,
            ),
            lineage=self._patch_lineage(payload),
            payload=patch.model_dump(mode="json"),
        )

    def _seal_preview(
        self,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        outcome: RepairSearchOutcomeV1,
    ) -> PreparedArtifact:
        assert outcome.preview_snapshot_id is not None
        return store_prepared_artifact(
            self.store,
            kind="ir_snapshot",
            payload_schema_id=IR_SNAPSHOT_SCHEMA_ID,
            version_tuple=VersionTuple(
                ir_snapshot_id=outcome.preview_snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                tool_version=REPAIR_TOOL_VERSION,
            ),
            lineage=(payload.base_snapshot_artifact_id,),
            payload=outcome.preview_payload,  # type: ignore[arg-type]
        )

    def _seal_configs(
        self,
        payload: PatchRepairPayloadV1,
        outcome: RepairSearchOutcomeV1,
        constraints: list[Constraint],
    ) -> tuple[PreparedArtifact, ...]:
        constraint_id = payload.constraint_snapshot_artifact_id
        assert outcome.preview_snapshot_id is not None
        artifacts: list[PreparedArtifact] = []
        for profile in payload.candidate_export_profiles:
            assert constraint_id is not None  # payload validator guarantees this
            package = self.config_exporter.export(
                export_profile=profile,
                preview_snapshot_id=outcome.preview_snapshot_id,
                preview_payload=outcome.preview_payload,  # type: ignore[arg-type]
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
                    extra_meta={"export_profile": profile.model_dump(mode="json")},
                )
            )
        return tuple(artifacts)

    def _seal_evidence(
        self,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        outcome: RepairSearchOutcomeV1,
    ) -> tuple[PreparedArtifact, ...]:
        assert outcome.preview_snapshot_id is not None
        # verifier checker/sim/regression = preview(prepared sibling, publisher-
        # injected) + optional constraint; the base snapshot is NOT a role here.
        lineage: tuple[str, ...] = (
            (payload.constraint_snapshot_artifact_id,)
            if payload.constraint_snapshot_artifact_id is not None
            else ()
        )
        artifacts: list[PreparedArtifact] = []
        for group in (
            outcome.checker_evidence,
            outcome.simulation_evidence,
            outcome.regression_evidence,
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
                            tool_version="repair-verifier@1",
                        ),
                        lineage=lineage,
                        payload=item.payload,
                        extra_meta={"requirement_id": item.requirement_id},
                    )
                )
        return tuple(artifacts)

    # ------------------------------------------------------------- unverified
    def _unverified(
        self, context: ExecutorContextLike, payload: PatchRepairPayloadV1
    ) -> PreparedRunFailure:
        # Every frozen requirement gets a produced / not_executed disposition; a
        # fully-exhausted search produced no verified evidence, so all are
        # not_executed with the `search_exhausted` reason (no silent omission).
        dispositions = self._unverified_dispositions(payload)
        return PreparedRunFailure(
            run_id=context.run.run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=(),
            requirement_dispositions=dispositions,
            cause_code="repair_unverified",
            failure_class="validation",
            intrinsic_retry_eligible=False,
            classifier=context.run.failure_classifier,
            redacted_message="verifier-guided repair search exhausted without a verified patch",
        )

    def _unverified_dispositions(
        self, payload: PatchRepairPayloadV1
    ) -> tuple[RequirementDispositionV1, ...]:
        dispositions: list[RequirementDispositionV1] = []
        for profile in payload.checker_profiles:
            dispositions.append(self._not_executed("checker", requirement_id_for_profile(profile)))
        for profile in payload.simulation_profiles:
            dispositions.append(
                self._not_executed("simulation", requirement_id_for_profile(profile))
            )
        for suite_id in payload.regression_suite_artifact_ids:
            dispositions.append(self._not_executed("regression", suite_id))
        return tuple(dispositions)

    def _not_executed(self, outcome_rule_id: str, requirement_id: str) -> RequirementDispositionV1:
        return RequirementDispositionV1(
            resolved_policy_id=RESOLVED_POLICY_ID,
            outcome_rule_id=outcome_rule_id,
            requirement_id=requirement_id,
            status="not_executed",
            reason_code=_UNVERIFIED_REASON,
        )

    # ------------------------------------------------------------------ inputs
    def _constraints(self, payload: PatchRepairPayloadV1) -> list[Constraint]:
        if payload.constraint_snapshot_artifact_id is None:
            return []
        return self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id)

    def _load_current_patch(self, payload: PatchRepairPayloadV1) -> PatchV2:
        parsed = parse_patch(load_json_blob(self.blobs, payload.subject_patch_artifact_id))
        if not isinstance(parsed, PatchV2):
            raise ValueError("repair subject head must be an immutable patch@2 artifact")
        return parsed

    def _patch_lineage(self, payload: PatchRepairPayloadV1) -> tuple[str, ...]:
        lineage = [
            payload.base_snapshot_artifact_id,
            payload.preview_snapshot_artifact_id,
            payload.subject_patch_artifact_id,
            payload.validation_evidence_artifact_id,
        ]
        if payload.constraint_snapshot_artifact_id is not None:
            lineage.append(payload.constraint_snapshot_artifact_id)
        lineage.extend(binding.evidence_artifact_id for binding in payload.findings)
        return tuple(lineage)


__all__ = [
    "REGRESSION_EVIDENCE_SCHEMA_ID",
    "REPAIR_AGENT_NODE_ID",
    "RESOLVED_POLICY_ID",
    "CheckerResolver",
    "RepairAgentRunner",
    "RepairRunRequest",
    "RepairSearchHandler",
    "RepairSearchOutcomeV1",
    "SimConfigResolver",
    "requirement_id_for_profile",
]
