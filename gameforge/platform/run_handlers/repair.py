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

from dataclasses import dataclass, replace
from typing import Callable, Protocol

from gameforge.contracts.config_export import canonical_config_export_bytes
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, PatchV2, TypedOp, parse_patch
from gameforge.contracts.jobs import (
    PatchRepairPayloadV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunOutcome,
    RequirementDispositionV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicySnapshotV1,
)
from gameforge.contracts.lineage import ArtifactKind
from gameforge.contracts.workflow import (
    EvidenceSet,
    FindingEvidenceBindingV1,
    PatchTargetBindingV1,
)
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactBatchStore,
    PreparedArtifactStore,
    build_success_result,
    load_json_blob,
    prepared_version_tuple,
    rebind_embedded_finding_payload,
    store_prepared_artifact,
    store_prepared_blob,
)
from gameforge.platform.run_handlers.generation import (
    CONFIG_EXPORT_SCHEMA_ID,
    IR_SNAPSHOT_SCHEMA_ID,
    PATCH_SCHEMA_ID,
    ConfigExporter,
    PreparedEvidenceV1,
    config_export_profile_binding,
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
_UNVERIFIED_REASON = "search_exhausted"
_UNVERIFIED_REASONS = frozenset(
    {"prior_requirement_failed", "search_exhausted", "execution_short_circuited"}
)

_EVIDENCE_KIND: dict[str, tuple[ArtifactKind, str]] = {
    "checker": ("checker_run", CHECKER_REPORT_SCHEMA_ID),
    "simulation": ("simulation_run", SIMULATION_RESULT_SCHEMA_ID),
    "regression": ("regression_evidence", REGRESSION_EVIDENCE_SCHEMA_ID),
}

CheckerResolver = Callable[[ProfileRefV1, list[Constraint]], Checker]
SimConfigResolver = Callable[[ProfileRefV1], ReviewSimConfig]
FindingLoader = Callable[[ArtifactBlobReader, PatchRepairPayloadV1], tuple[Finding, ...]]


@dataclass(frozen=True, slots=True)
class RepairRunRequest:
    """Fully-resolved inputs for one verifier-guided repair search."""

    failed_preview_artifact_id: str
    constraint_snapshot_artifact_id: str | None
    finding_evidence_bindings: tuple[FindingEvidenceBindingV1, ...]
    base_snapshot: Snapshot
    failed_preview_snapshot: Snapshot
    current_patch: PatchV2
    validation_evidence: EvidenceSet
    constraints: tuple[Constraint, ...]
    findings: tuple[Finding, ...]
    checkers: tuple[tuple[ProfileRefV1, Checker], ...]
    simulation_profiles: tuple[tuple[ProfileRefV1, ReviewSimConfig], ...]
    regression_suite_artifact_ids: tuple[str, ...]
    verifier_requirements: tuple[ResolvedArtifactRequirementV1, ...]
    router: BridgeModelRouter
    seed: int | None
    run_kind: RunKindRef
    regression_profile: ProfileRefV1
    max_steps: int = 4
    max_total_checker_work_units: int = 2_000_000
    max_total_simulation_work_units: int = 2_000_000
    max_total_regression_work_units: int = 10_000_000


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
    failure_reason: str = _UNVERIFIED_REASON


class RepairAgentRunner(Protocol):
    """Drive the M2 verifier-guided repair search for one target defect."""

    def search(self, request: RepairRunRequest) -> RepairSearchOutcomeV1: ...


@dataclass(frozen=True, slots=True)
class RepairExecutionConfig:
    max_search_steps: int
    max_prompt_message_bytes: int = 16 * 1024 * 1024
    max_total_checker_work_units: int = 2_000_000
    max_total_simulation_work_units: int = 2_000_000
    max_checker_profile_count: int = 64
    max_simulation_profile_count: int = 64
    max_regression_suite_count: int = 64
    max_total_regression_work_units: int = 10_000_000
    max_regression_suite_bytes: int = 17 * 1024 * 1024
    max_total_regression_suite_bytes: int = 64 * 1024 * 1024
    max_candidate_export_profiles: int = 16
    max_total_prepared_artifact_bytes: int = 128 * 1024 * 1024


class RepairExecutionConfigResolver(Protocol):
    def __call__(self, profile: ProfileRefV1) -> RepairExecutionConfig: ...


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
    execution_config_resolver: RepairExecutionConfigResolver
    finding_loader: FindingLoader = _no_finding_loader
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, PatchRepairPayloadV1):
            raise TypeError("repair_search@1 requires a patch-repair@1 payload")

        execution_config = self.execution_config_resolver(payload.repair_policy)
        if (
            len(payload.checker_profiles) > execution_config.max_checker_profile_count
            or len(payload.simulation_profiles) > execution_config.max_simulation_profile_count
            or len(payload.regression_suite_artifact_ids)
            > execution_config.max_regression_suite_count
            or len(payload.candidate_export_profiles)
            > execution_config.max_candidate_export_profiles
        ):
            raise ValueError("repair verifier/profile outputs exceed the profile count budget")
        self._validate_regression_suite_bytes(payload, execution_config)

        base_snapshot = self.snapshot_loader(self.blobs, payload.base_snapshot_artifact_id)
        failed_preview = self.snapshot_loader(self.blobs, payload.preview_snapshot_artifact_id)
        constraints = self._constraints(payload)
        current_patch = self._load_current_patch(payload)
        self._validate_current_candidate(base_snapshot, failed_preview, current_patch)
        findings = self._exact_findings(payload, failed_preview)
        validation_evidence = self._load_validation_evidence(
            payload,
            current_patch,
            failed_preview,
        )
        checkers = tuple(
            (profile, self.checker_resolver(profile, constraints))
            for profile in payload.checker_profiles
        )
        sim_profiles = tuple(
            (profile, self.sim_config_resolver(profile)) for profile in payload.simulation_profiles
        )
        if not 1 <= execution_config.max_search_steps <= 1_000:
            raise ValueError("repair search budget is outside the exact profile bounds")
        prompt_sources = tuple(
            sorted(
                {
                    payload.preview_snapshot_artifact_id,
                    *(binding.evidence_artifact_id for binding in payload.findings),
                    *payload.regression_suite_artifact_ids,
                    *(
                        ()
                        if payload.constraint_snapshot_artifact_id is None
                        else (payload.constraint_snapshot_artifact_id,)
                    ),
                }
            )
        )
        router = build_bridge_router(
            context=context,
            agent_node_id=REPAIR_AGENT_NODE_ID,
            max_prompt_message_bytes=execution_config.max_prompt_message_bytes,
            source_artifact_ids=prompt_sources,
        )
        policy_snapshot = self._verifier_policy_snapshot(context)

        outcome = self.agent_runner.search(
            RepairRunRequest(
                failed_preview_artifact_id=payload.preview_snapshot_artifact_id,
                constraint_snapshot_artifact_id=payload.constraint_snapshot_artifact_id,
                finding_evidence_bindings=tuple(payload.findings),
                base_snapshot=base_snapshot,
                failed_preview_snapshot=failed_preview,
                current_patch=current_patch,
                validation_evidence=validation_evidence,
                constraints=tuple(constraints),
                findings=findings,
                checkers=checkers,
                simulation_profiles=sim_profiles,
                regression_suite_artifact_ids=tuple(payload.regression_suite_artifact_ids),
                verifier_requirements=policy_snapshot.requirements,
                router=router,
                seed=context.payload.seed,
                run_kind=context.run.kind,
                regression_profile=payload.repair_policy,
                max_steps=execution_config.max_search_steps,
                max_total_checker_work_units=(execution_config.max_total_checker_work_units),
                max_total_simulation_work_units=(execution_config.max_total_simulation_work_units),
                max_total_regression_work_units=(execution_config.max_total_regression_work_units),
            )
        )

        if outcome.passed_verification:
            return self._verified(
                context,
                payload,
                base_snapshot,
                current_patch,
                findings,
                constraints,
                outcome,
                execution_config,
            )
        return self._unverified(
            context,
            payload,
            failed_preview.snapshot_id,
            policy_snapshot,
            outcome,
            execution_config,
        )

    def _validate_regression_suite_bytes(
        self,
        payload: PatchRepairPayloadV1,
        execution_config: RepairExecutionConfig,
    ) -> None:
        total = 0
        for artifact_id in payload.regression_suite_artifact_ids:
            loader = getattr(self.blobs, "load_artifact", None)
            if callable(loader):
                artifact = loader(artifact_id)
                object_ref = getattr(artifact, "object_ref", None)
                size = getattr(object_ref, "size_bytes", None)
            else:
                bounded = getattr(self.blobs, "read_bytes_bounded", None)
                raw = (
                    bounded(
                        artifact_id,
                        max_bytes=execution_config.max_regression_suite_bytes,
                    )
                    if callable(bounded)
                    else self.blobs.read_bytes(artifact_id)
                )
                size = len(raw) if isinstance(raw, bytes) else None
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise IntegrityViolation("regression suite has no exact byte authority")
            if size > execution_config.max_regression_suite_bytes:
                raise IntegrityViolation("regression suite exceeds its profile byte budget")
            if total > execution_config.max_total_regression_suite_bytes - size:
                raise IntegrityViolation(
                    "repair regression suites exceed their total profile byte budget"
                )
            total += size

    @staticmethod
    def _verifier_policy_snapshot(
        context: ExecutorContextLike,
    ) -> ResolvedPolicySnapshotV1:
        snapshots = context.payload.resolved_policy_snapshots
        if len(snapshots) != 1:
            raise ValueError("repair run requires exactly one frozen verifier policy snapshot")
        return snapshots[0]

    # --------------------------------------------------------------- verified
    def _verified(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        current_patch: PatchV2,
        findings: tuple[Finding, ...],
        constraints: list[Constraint],
        outcome: RepairSearchOutcomeV1,
        execution_config: RepairExecutionConfig,
    ) -> PreparedRunOutcome:
        assert outcome.preview_payload is not None and outcome.preview_snapshot_id is not None
        self._validate_verified_outcome(base_snapshot, current_patch, findings, outcome)
        run_id = context.run.run_id
        batch = PreparedArtifactBatchStore(
            max_bytes=execution_config.max_total_prepared_artifact_bytes
        )
        staged_handler = replace(self, store=batch)
        patch = staged_handler._seal_superseding_patch(
            context, payload, base_snapshot, current_patch, outcome, run_id
        )
        preview = staged_handler._seal_preview(context, payload, base_snapshot, outcome)
        configs = staged_handler._seal_configs(context, payload, outcome, constraints)
        evidence = staged_handler._seal_evidence(
            context,
            payload,
            outcome,
            evidence_snapshot_id=outcome.preview_snapshot_id,
            verified=True,
        )
        artifacts = batch.commit(
            self.store,
            (patch, preview, *configs, *evidence),
            max_bytes=execution_config.max_total_prepared_artifact_bytes,
        )
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
        context: ExecutorContextLike,
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
            expected_to_fix=list(
                dict.fromkeys((*current_patch.expected_to_fix, *outcome.expected_to_fix))
            ),
            preconditions=list(current_patch.preconditions),
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
            version_tuple=prepared_version_tuple(
                context,
                tool_version=REPAIR_TOOL_VERSION,
                projected_fields=("doc_version", "constraint_snapshot_id"),
                overrides={"ir_snapshot_id": base_snapshot.snapshot_id},
            ),
            lineage=self._patch_lineage(payload),
            payload=patch.model_dump(mode="json"),
        )

    @staticmethod
    def _validate_verified_outcome(
        base_snapshot: Snapshot,
        current_patch: PatchV2,
        findings: tuple[Finding, ...],
        outcome: RepairSearchOutcomeV1,
    ) -> None:
        """Defend the sealing boundary against a non-combined or fabricated pass."""

        if outcome.preview_payload is None or outcome.preview_snapshot_id is None:
            raise ValueError("verified repair outcome has no preview")
        original_count = len(current_patch.ops)
        if tuple(outcome.ops[:original_count]) != tuple(current_patch.ops):
            raise ValueError("verified repair outcome dropped or rewrote original patch ops")
        if not {finding.id for finding in findings}.issubset(outcome.expected_to_fix):
            raise ValueError("verified repair outcome omitted an exact target finding")
        candidate = PatchV2(
            revision=current_patch.revision + 1,
            supersedes_artifact_id="validation:subject-patch",
            base_snapshot_id=base_snapshot.snapshot_id,
            target_snapshot_id=outcome.preview_snapshot_id,
            expected_to_fix=list(outcome.expected_to_fix),
            preconditions=list(current_patch.preconditions),
            side_effect_risk=outcome.side_effect_risk,
            ops=list(outcome.ops),
            produced_by="agent",
            producer_run_id="validation:repair-runner",
            rationale="validate full combined repair outcome",
        )
        try:
            replayed = apply_patch(base_snapshot, candidate)
        except PatchRejected as exc:
            raise ValueError("verified repair outcome cannot replay on the exact base") from exc
        if (
            replayed.snapshot_id != outcome.preview_snapshot_id
            or replayed.content_payload != outcome.preview_payload
        ):
            raise ValueError("verified repair outcome preview differs from exact-base replay")

    def _seal_preview(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        base_snapshot: Snapshot,
        outcome: RepairSearchOutcomeV1,
    ) -> PreparedArtifact:
        assert outcome.preview_snapshot_id is not None
        return store_prepared_artifact(
            self.store,
            kind="ir_snapshot",
            payload_schema_id=IR_SNAPSHOT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=REPAIR_TOOL_VERSION,
                projected_fields=("doc_version",),
                overrides={"ir_snapshot_id": outcome.preview_snapshot_id},
            ),
            lineage=(payload.base_snapshot_artifact_id,),
            payload=outcome.preview_payload,  # type: ignore[arg-type]
        )

    def _seal_configs(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        outcome: RepairSearchOutcomeV1,
        constraints: list[Constraint],
    ) -> tuple[PreparedArtifact, ...]:
        constraint_id = payload.constraint_snapshot_artifact_id
        assert outcome.preview_snapshot_id is not None
        artifacts: list[PreparedArtifact] = []
        for index, profile in enumerate(payload.candidate_export_profiles):
            assert constraint_id is not None  # payload validator guarantees this
            binding = config_export_profile_binding(context, index=index, profile=profile)
            package = self.config_exporter.export(
                export_profile=profile,
                export_profile_binding=binding,
                run_kind=context.run.kind,
                llm_execution_mode=context.payload.llm_execution_mode,
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
                    extra_meta={"export_profile": profile.model_dump(mode="json")},
                )
            )
        return tuple(artifacts)

    def _seal_evidence(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        outcome: RepairSearchOutcomeV1,
        *,
        evidence_snapshot_id: str,
        verified: bool,
    ) -> tuple[PreparedArtifact, ...]:
        # Verified evidence binds the newly prepared preview (publisher-injected
        # sibling). Unverified evidence may bind only the immutable failed input
        # preview; the frozen failure policy publishes no candidate preview.
        lineage_items = [] if verified else [payload.preview_snapshot_artifact_id]
        if payload.constraint_snapshot_artifact_id is not None:
            lineage_items.append(payload.constraint_snapshot_artifact_id)
        lineage = tuple(lineage_items)
        artifacts: list[PreparedArtifact] = []
        regression_items = tuple(outcome.regression_evidence)
        if verified and len(regression_items) != len(payload.regression_suite_artifact_ids):
            raise ValueError("repair regression evidence count differs from exact suites")
        seen_suites: set[str] = set()
        for item in regression_items:
            suite_id = item.payload.get("suite_artifact_id")
            if (
                not isinstance(suite_id, str)
                or suite_id not in payload.regression_suite_artifact_ids
                or suite_id in seen_suites
            ):
                raise ValueError("repair regression evidence is bound to another suite")
            seen_suites.add(suite_id)
        if verified and seen_suites != set(payload.regression_suite_artifact_ids):
            raise ValueError("repair regression evidence differs from exact suites")
        for group in (
            outcome.checker_evidence,
            outcome.simulation_evidence,
            regression_items,
        ):
            for item in group:
                kind, schema = _EVIDENCE_KIND[item.outcome_rule_id]
                if item.payload.get("snapshot_id") != evidence_snapshot_id:
                    raise ValueError("repair evidence is grounded on another preview")
                item_lineage = lineage
                if item.outcome_rule_id == "regression":
                    suite_id = item.payload.get("suite_artifact_id")
                    if not isinstance(suite_id, str):  # guarded above; defensive for typing
                        raise ValueError("repair regression evidence has no exact suite")
                    item_lineage = (*lineage, suite_id)
                version_overrides: dict[str, object | None] = {
                    "ir_snapshot_id": evidence_snapshot_id
                }
                if item.env_contract_version is not None:
                    if (
                        item.outcome_rule_id != "regression"
                        or not isinstance(item.env_contract_version, str)
                        or not 1 <= len(item.env_contract_version) <= 512
                    ):
                        raise ValueError(
                            "repair evidence has an invalid environment contract binding"
                        )
                    version_overrides["env_contract_version"] = item.env_contract_version
                artifacts.append(
                    store_prepared_artifact(
                        self.store,
                        kind=kind,
                        payload_schema_id=schema,
                        version_tuple=prepared_version_tuple(
                            context,
                            tool_version="repair-verifier@1",
                            projected_fields=(
                                *(("doc_version",) if item.outcome_rule_id == "regression" else ()),
                                "constraint_snapshot_id",
                                *(
                                    ("seed",)
                                    if item.outcome_rule_id in {"simulation", "regression"}
                                    else ()
                                ),
                            ),
                            overrides=version_overrides,
                        ),
                        lineage=item_lineage,
                        payload=rebind_embedded_finding_payload(
                            item.payload, run_id=context.run.run_id
                        ),
                        extra_meta={"requirement_id": item.requirement_id},
                    )
                )
        return tuple(artifacts)

    # ------------------------------------------------------------- unverified
    def _unverified(
        self,
        context: ExecutorContextLike,
        payload: PatchRepairPayloadV1,
        failed_preview_snapshot_id: str,
        policy_snapshot: ResolvedPolicySnapshotV1,
        outcome: RepairSearchOutcomeV1,
        execution_config: RepairExecutionConfig,
    ) -> PreparedRunFailure:
        reason = outcome.failure_reason
        if reason not in _UNVERIFIED_REASONS:
            raise ValueError("repair unverified reason is not allowed by the frozen policy")
        items = tuple(
            (
                *outcome.checker_evidence,
                *outcome.simulation_evidence,
                *outcome.regression_evidence,
            )
        )
        requirement_by_key = {
            (requirement.outcome_rule_id, requirement.requirement_id): requirement
            for requirement in policy_snapshot.requirements
        }
        produced_keys: set[tuple[str, str]] = set()
        for item in items:
            key = (item.outcome_rule_id, item.requirement_id)
            requirement = requirement_by_key.get(key)
            if requirement is None or key in produced_keys:
                raise ValueError("repair failure evidence differs from frozen requirements")
            expected_kind, expected_schema = _EVIDENCE_KIND[item.outcome_rule_id]
            if (
                requirement.artifact_kind != expected_kind
                or requirement.payload_schema_id != expected_schema
            ):
                raise ValueError("repair failure evidence kind differs from frozen requirement")
            produced_keys.add(key)
        batch = PreparedArtifactBatchStore(
            max_bytes=execution_config.max_total_prepared_artifact_bytes
        )
        staged_handler = replace(self, store=batch)
        staged_artifacts = staged_handler._seal_evidence(
            context,
            payload,
            outcome,
            evidence_snapshot_id=failed_preview_snapshot_id,
            verified=False,
        )
        artifacts = batch.commit(
            self.store,
            staged_artifacts,
            max_bytes=execution_config.max_total_prepared_artifact_bytes,
        )
        # Every frozen requirement gets exactly one disposition. Executed input-
        # preview evidence is `produced`; only dimensions that genuinely did not
        # execute are marked `not_executed` with the frozen reason code.
        dispositions = tuple(
            RequirementDispositionV1(
                resolved_policy_id=policy_snapshot.resolved_policy_id,
                outcome_rule_id=requirement.outcome_rule_id,
                requirement_id=requirement.requirement_id,
                status=(
                    "produced"
                    if (requirement.outcome_rule_id, requirement.requirement_id) in produced_keys
                    else "not_executed"
                ),
                reason_code=(
                    None
                    if (requirement.outcome_rule_id, requirement.requirement_id) in produced_keys
                    else reason
                ),
            )
            for requirement in policy_snapshot.requirements
        )
        return PreparedRunFailure(
            run_id=context.run.run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=artifacts,
            requirement_dispositions=dispositions,
            cause_code="repair_unverified",
            failure_class="validation",
            intrinsic_retry_eligible=False,
            classifier=context.run.failure_classifier,
            redacted_message="verifier-guided repair search exhausted without a verified patch",
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

    @staticmethod
    def _validate_current_candidate(
        base_snapshot: Snapshot,
        failed_preview: Snapshot,
        current_patch: PatchV2,
    ) -> None:
        """Prove the SubjectHead patch is the exact base→failed-preview candidate."""

        if current_patch.base_snapshot_id != base_snapshot.snapshot_id:
            raise ValueError("repair subject patch does not target the exact CURRENT-ref base")
        if current_patch.target_snapshot_id != failed_preview.snapshot_id:
            raise ValueError("repair subject patch target differs from the failed preview")
        try:
            replayed = apply_patch(base_snapshot, current_patch)
        except PatchRejected as exc:
            raise ValueError("repair subject patch cannot replay on its exact base") from exc
        if replayed.snapshot_id != failed_preview.snapshot_id:
            raise ValueError("repair subject patch does not reproduce the failed preview")

    def _exact_findings(
        self,
        payload: PatchRepairPayloadV1,
        failed_preview: Snapshot,
    ) -> tuple[Finding, ...]:
        """Load and order exactly the frozen finding revisions bound by admission."""

        loaded = self.finding_loader(self.blobs, payload)
        by_id = {finding.id: finding for finding in loaded}
        if len(by_id) != len(loaded):
            raise ValueError("repair finding loader returned duplicate finding ids")
        expected_ids = tuple(binding.finding_id for binding in payload.findings)
        if set(by_id) != set(expected_ids):
            raise ValueError("repair finding loader differs from the frozen finding bindings")
        ordered = tuple(by_id[finding_id] for finding_id in expected_ids)
        if not ordered:
            raise ValueError("repair_search requires at least one exact finding")
        if any(finding.snapshot_id != failed_preview.snapshot_id for finding in ordered):
            raise ValueError("repair finding is not grounded on the failed preview")
        return ordered

    def _load_validation_evidence(
        self,
        payload: PatchRepairPayloadV1,
        current_patch: PatchV2,
        failed_preview: Snapshot,
    ) -> EvidenceSet:
        """Consume and re-verify the exact failed EvidenceSet frozen at admission."""

        evidence = EvidenceSet.model_validate(
            load_json_blob(self.blobs, payload.validation_evidence_artifact_id)
        )
        binding = evidence.target_binding
        subject_digest = canonical_sha256(current_patch.model_dump(mode="json"))
        target_digest = canonical_sha256(
            load_json_blob(self.blobs, payload.preview_snapshot_artifact_id)
        )
        if (
            evidence.overall_status not in {"failed", "unproven"}
            or evidence.subject_artifact_id != payload.subject_patch_artifact_id
            or evidence.subject_digest != subject_digest
            or evidence.finding_bindings != payload.findings
            or not isinstance(binding, PatchTargetBindingV1)
            or binding.target_artifact_id != payload.preview_snapshot_artifact_id
            or binding.target_snapshot_id != failed_preview.snapshot_id
            or binding.target_digest != target_digest
            or binding.ref_name != payload.target.ref_name
            or binding.expected_ref != payload.target.expected_ref
        ):
            raise ValueError("repair validation evidence differs from the exact failed subject")
        return evidence

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
    "CheckerResolver",
    "RepairAgentRunner",
    "RepairExecutionConfig",
    "RepairExecutionConfigResolver",
    "RepairRunRequest",
    "RepairSearchHandler",
    "RepairSearchOutcomeV1",
    "SimConfigResolver",
]
