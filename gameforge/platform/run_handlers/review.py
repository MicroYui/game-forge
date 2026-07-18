"""``review_runner@1`` — the composite review handler.

Review ALWAYS runs the deterministic checkers + economy simulation and treats
their Findings as the *authoritative* verdict. When (and only when) a
``llm_triage_policy`` is bound, it additionally runs an LLM triage pass through
the injected model-routing adapter as a pure SUGGESTION: triage output is
``oracle_type="llm-assisted"``, ``source="llm"``, ``status="unproven"`` and never
promotes, demotes, or overrides a deterministic Finding — it only annotates.

Outputs: the primary ``review_report[review@1]`` plus one
``checker_run[checker-report@1]`` per ``/params/checker_profiles`` entry and one
``simulation_run[simulation-result@1]`` per ``/params/simulation_profiles`` entry
(each carrying its ``/profile`` for the frozen identity binding). Findings — the
deterministic verdict plus any LLM suggestions — are sealed under the
``review-findings`` policy. The recorded LLM mode is ``not_applicable`` when no
triage policy is bound, otherwise the Run's exact ``llm_execution_mode``.
``outcome_code=review_completed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Callable

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    MAX_PREPARED_FINDINGS,
    PreparedArtifact,
    PreparedRunOutcome,
    ReviewRunPayloadV1,
)
from gameforge.contracts.review import ReviewReport
from gameforge.spine.checkers.base import Checker, CheckerExecutionBinding
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    FindingEvidence,
    FindingHeadRevisionResolver,
    PreparedArtifactBatchStore,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    prepared_version_tuple,
    rebind_finding_producers,
    scoped_finding_series_id,
    store_prepared_artifact,
    trust_typed_profile_binding,
    require_exact_profile_bindings,
)
from gameforge.platform.run_handlers.checker import (
    CheckerExecutionPolicyResolver,
    default_checker_execution_policy,
    filter_findings_by_selection,
    navigation_unproven_findings,
    validate_checker_output_policy,
    validate_checker_execution_policy,
)
from gameforge.platform.run_handlers.model_routing import (
    ModelBridgeAgentAdapter,
    plan_node_snapshot,
    prompt_source_artifact_ids,
    require_agent_prompt_message_bytes,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    NavLoader,
    SnapshotLoader,
    load_constraints,
    load_nav,
    load_snapshot,
)
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed
from gameforge.platform.run_handlers.simulation import EconomySimulatorPort
from gameforge.platform.run_handlers.simulation import (
    unproven_input_application_findings,
    validate_economy_simulation_work_budget,
)

REVIEW_SCHEMA_ID = "review@1"
REVIEW_TOOL_VERSION = "review@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"
TRIAGE_AGENT_NODE_ID = "review-triage"
TRIAGE_PROMPT_VERSION = "review-triage@1"
TRIAGE_MAX_INPUT_FINDINGS = 256
TRIAGE_MAX_PROMPT_BYTES = 64 * 1024
TRIAGE_MAX_FINDING_ID_BYTES = 512
TRIAGE_MAX_DEFECT_CLASS_BYTES = 256
TRIAGE_MAX_INPUT_MESSAGE_BYTES = 1_024
TRIAGE_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
TRIAGE_MAX_SUGGESTIONS = 256
TRIAGE_MAX_SUGGESTION_MESSAGE_BYTES = 2_048
TRIAGE_MAX_SUGGESTION_ENTITIES = 64
TRIAGE_MAX_ENTITY_ID_BYTES = 512


@dataclass(frozen=True, slots=True)
class ReviewSimConfig:
    """Bounded population/horizon for one review simulation profile."""

    n_agents: int
    n_ticks: int
    max_work_units: int


@dataclass(frozen=True, slots=True)
class ReviewExecutionConfig:
    max_prompt_message_bytes: int = 16 * 1024 * 1024
    max_checker_profile_count: int = 64
    max_simulation_profile_count: int = 64
    max_total_checker_work_units: int = 2_000_000
    max_total_simulation_work_units: int = 2_000_000
    max_total_prepared_artifact_bytes: int = 128 * 1024 * 1024


CheckerResolver = Callable[[ResolvedExecutionProfileBindingV1, list[Constraint]], Checker]
SimConfigResolver = Callable[[ResolvedExecutionProfileBindingV1], ReviewSimConfig]
ReviewExecutionConfigResolver = Callable[[ResolvedExecutionProfileBindingV1], ReviewExecutionConfig]
TriageProfileAuthorizer = Callable[[ResolvedExecutionProfileBindingV1], None]


def default_review_execution_config(
    _binding: ResolvedExecutionProfileBindingV1,
) -> ReviewExecutionConfig:
    return ReviewExecutionConfig()


def trust_typed_triage_profile(_binding: ResolvedExecutionProfileBindingV1) -> None:
    """Unit/default seam; production validates the built-in triage adapter contract."""


@dataclass(frozen=True, slots=True)
class ReviewRunHandler:
    """A ``RunExecutor`` producing the review report + gate checker/sim + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_resolver: CheckerResolver
    sim_config_resolver: SimConfigResolver
    checker_execution_policy_resolver: CheckerExecutionPolicyResolver = (
        default_checker_execution_policy
    )
    execution_config_resolver: ReviewExecutionConfigResolver = default_review_execution_config
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    nav_loader: NavLoader = load_nav
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)
    finding_head_revision: FindingHeadRevisionResolver | None = None
    triage_profile_authorizer: TriageProfileAuthorizer = trust_typed_triage_profile
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ReviewRunPayloadV1):
            raise TypeError("review_runner@1 requires a review-run@1 payload")
        expected_profiles = {
            "/params/review_profile": (payload.review_profile, "review"),
            **{
                f"/params/checker_profiles/{index}": (profile, "checker")
                for index, profile in enumerate(payload.checker_profiles)
            },
            **{
                f"/params/simulation_profiles/{index}": (profile, "simulation")
                for index, profile in enumerate(payload.simulation_profiles)
            },
        }
        if payload.llm_triage_policy is not None:
            expected_profiles["/params/llm_triage_policy"] = (
                payload.llm_triage_policy,
                "llm_triage",
            )
        profile_bindings = require_exact_profile_bindings(
            context,
            expected=expected_profiles,
            validator=self.profile_binding_validator,
        )
        if payload.llm_triage_policy is not None:
            self.triage_profile_authorizer(profile_bindings["/params/llm_triage_policy"])
        if not payload.checker_profiles and not payload.simulation_profiles:
            raise IntegrityViolation(
                "review.run requires at least one deterministic checker or simulation profile"
            )
        if payload.constraint_snapshot_artifact_id is not None and not payload.checker_profiles:
            raise IntegrityViolation(
                "review constraints require a checker profile that executes canonical DSL routing"
            )
        if payload.simulation_profiles and payload.selection.mode != "full":
            raise IntegrityViolation("review simulation profiles require full graph selection")
        review_binding = profile_bindings["/params/review_profile"]
        execution_config = self.execution_config_resolver(review_binding)
        if (
            len(payload.checker_profiles) > execution_config.max_checker_profile_count
            or len(payload.simulation_profiles) > execution_config.max_simulation_profile_count
        ):
            raise IntegrityViolation("review profile collections exceed the exact count budget")
        if not isinstance(self.store, PreparedArtifactBatchStore):
            batch = PreparedArtifactBatchStore(
                max_bytes=execution_config.max_total_prepared_artifact_bytes
            )
            staged = replace(self, store=batch)(context)
            committed = batch.commit(
                self.store,
                staged.artifacts,
                max_bytes=execution_config.max_total_prepared_artifact_bytes,
            )
            return staged.model_copy(update={"artifacts": committed})

        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        constraints = self._constraints(payload)
        self._validate_checker_budgets(
            profile_bindings,
            payload,
            snapshot,
            constraints,
            execution_config,
        )
        nav = self.nav_loader(self.blobs, payload.snapshot_artifact_id)
        lineage = _snapshot_lineage(payload)

        checker_findings, checker_artifacts, checker_evidence = self._run_checkers(
            context, profile_bindings, payload, snapshot, constraints, nav, lineage
        )
        sim_findings, sim_artifacts, sim_evidence = self._run_simulations(
            payload,
            snapshot,
            constraints,
            context,
            profile_bindings,
            lineage,
            execution_config,
            existing_finding_count=len(checker_findings),
            evidence_artifact_index_offset=1 + len(checker_artifacts),
        )

        # LLM triage is a suggestion-only annotation over the deterministic verdict.
        triage_applied = payload.llm_triage_policy is not None
        triage_input_findings = [
            finding
            for finding in (*checker_findings, *sim_findings)
            if finding.oracle_type != "llm-assisted"
        ]
        triage_findings = (
            self._run_triage(
                context,
                snapshot,
                triage_input_findings,
                execution_config,
            )
            if triage_applied
            else []
        )
        triage_findings = rebind_finding_producers(triage_findings, run_id=context.run.run_id)
        recorded_llm_mode = (
            context.payload.llm_execution_mode if triage_applied else "not_applicable"
        )

        all_findings = checker_findings + sim_findings + triage_findings
        if len(all_findings) > MAX_PREPARED_FINDINGS:
            raise IntegrityViolation("review findings exceed the frozen output bound")
        _require_finding_authority(
            triage_findings,
            snapshot_id=snapshot.snapshot_id,
            source="llm",
            oracle_type="llm-assisted",
            statuses=frozenset(("unproven",)),
            label="review triage",
        )
        # Wrap the spine partition (build_review_report runs any supplied checkers
        # then appends + partitions); the checkers already ran per profile, so the
        # authoritative report is the same partition applied to the collected set.
        report = build_review_report(snapshot, [], tuple(all_findings), nav)

        primary = _store_review_report(
            self.store,
            snapshot=snapshot,
            report=report,
            lineage=lineage,
            context=context,
            recorded_llm_mode=recorded_llm_mode,
            triage_applied=triage_applied,
        )
        artifacts = (primary, *checker_artifacts, *sim_artifacts)
        # Two checker_profiles may resolve to the SAME checker id on the SAME
        # snapshot and emit identical spine ``Finding.id``s; scope every checker
        # finding id by its resolving profile so distinct profiles never collide
        # on one finding-series head (M1 review carry-forward fix). Sim + triage
        # findings keep their own ids (each already unique by profile/order).
        evidence = (
            *checker_evidence,
            *sim_evidence,
            *(
                FindingEvidence(finding=finding, evidence_artifact_index=0)
                for finding in triage_findings
            ),
        )
        prepared_findings = build_prepared_findings(
            evidence,
            run_id=context.run.run_id,
            head_revision_resolver=self.finding_head_revision,
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="review_completed",
            primary_index=0,
            artifacts=artifacts,
            findings=prepared_findings,
        )

    def _constraints(self, payload: ReviewRunPayloadV1) -> list[Constraint]:
        if payload.constraint_snapshot_artifact_id is None:
            return []
        return self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id)

    def _validate_checker_budgets(
        self,
        profile_bindings: dict[str, ResolvedExecutionProfileBindingV1],
        payload: ReviewRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        execution_config: ReviewExecutionConfig,
    ) -> None:
        total_work = 0
        for index, profile in enumerate(payload.checker_profiles):
            binding = profile_bindings[f"/params/checker_profiles/{index}"]
            policy = self.checker_execution_policy_resolver(binding)
            total_work += validate_checker_execution_policy(
                checker_ids=("graph",),
                defect_classes=(),
                constraint_count=len(constraints),
                snapshot=snapshot,
                policy=policy,
            )
        if total_work > execution_config.max_total_checker_work_units:
            raise IntegrityViolation(
                "review checker profiles exceed the aggregate exact work budget"
            )

    def _run_checkers(
        self,
        context: ExecutorContextLike,
        profile_bindings: dict[str, ResolvedExecutionProfileBindingV1],
        payload: ReviewRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        nav: NavProvider | None,
        lineage: tuple[str, ...],
    ) -> tuple[list[Finding], tuple[PreparedArtifact, ...], tuple[FindingEvidence, ...]]:
        findings: list[Finding] = []
        artifacts: list[PreparedArtifact] = []
        evidence: list[FindingEvidence] = []
        for index, profile in enumerate(payload.checker_profiles):
            binding = profile_bindings[f"/params/checker_profiles/{index}"]
            checker = self.checker_resolver(binding, constraints)
            execution_bindings = _trusted_checker_execution_bindings(checker, constraints)
            profile_findings = checker.check(snapshot, nav=nav)
            profile_findings.extend(navigation_unproven_findings(snapshot, nav))
            if len(findings) + len(profile_findings) > MAX_PREPARED_FINDINGS:
                raise IntegrityViolation("review findings exceed the frozen output bound")
            _require_checker_finding_authority(
                profile_findings,
                snapshot_id=snapshot.snapshot_id,
                constraints=constraints,
            )
            validate_checker_output_policy(
                profile_findings,
                policy=self.checker_execution_policy_resolver(binding),
            )
            profile_findings = filter_findings_by_selection(
                profile_findings, payload.selection, snapshot
            )
            profile_findings = [
                _scope_profile_finding(profile, finding) for finding in profile_findings
            ]
            profile_findings = rebind_finding_producers(profile_findings, run_id=context.run.run_id)
            if len(findings) + len(profile_findings) > MAX_PREPARED_FINDINGS:
                raise IntegrityViolation("review findings exceed the frozen output bound")
            findings.extend(profile_findings)
            evidence_artifact_index = 1 + len(artifacts)
            for finding in profile_findings:
                evidence.append(
                    FindingEvidence(
                        finding=finding,
                        evidence_artifact_index=evidence_artifact_index,
                    )
                )
            artifacts.append(
                store_prepared_artifact(
                    self.store,
                    kind="checker_run",
                    payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
                    version_tuple=prepared_version_tuple(
                        context,
                        tool_version="checker@1",
                        projected_fields=("constraint_snapshot_id",),
                        overrides={"ir_snapshot_id": snapshot.snapshot_id},
                    ),
                    lineage=lineage,
                    payload=_profile_checker_payload(
                        profile,
                        snapshot.snapshot_id,
                        profile_findings,
                        execution_bindings=execution_bindings,
                        constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
                    ),
                )
            )
        return findings, tuple(artifacts), tuple(evidence)

    def _run_simulations(
        self,
        payload: ReviewRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        context: ExecutorContextLike,
        profile_bindings: dict[str, ResolvedExecutionProfileBindingV1],
        lineage: tuple[str, ...],
        execution_config: ReviewExecutionConfig,
        *,
        existing_finding_count: int,
        evidence_artifact_index_offset: int,
    ) -> tuple[
        list[Finding],
        tuple[PreparedArtifact, ...],
        tuple[FindingEvidence, ...],
    ]:
        root_seed = context.payload.seed
        if payload.simulation_profiles and root_seed is None:
            raise ValueError("stochastic review simulations require a frozen root seed")
        model = EconomyModel.from_snapshot(snapshot)
        findings: list[Finding] = []
        artifacts: list[PreparedArtifact] = []
        evidence: list[FindingEvidence] = []
        resolved_configs: list[tuple[ProfileRefV1, ReviewSimConfig]] = []
        total_work_units = 0
        for index, profile in enumerate(payload.simulation_profiles):
            binding = profile_bindings[f"/params/simulation_profiles/{index}"]
            config = self.sim_config_resolver(binding)
            total_work_units += validate_economy_simulation_work_budget(
                model,
                n_agents=config.n_agents,
                n_ticks=config.n_ticks,
                replication_count=1,
                max_work_units=config.max_work_units,
            )
            if total_work_units > execution_config.max_total_simulation_work_units:
                raise IntegrityViolation(
                    "review simulation profiles exceed the aggregate exact work budget"
                )
            resolved_configs.append((profile, config))
        for profile, config in resolved_configs:
            assert root_seed is not None
            case_id = f"review:{snapshot.snapshot_id}"
            seed = derive_validation_subseed(
                root_seed=root_seed,
                run_kind=context.run.kind,
                profile=profile,
                case_id=case_id,
                replication_index=0,
            )
            result = self.simulator.run(
                model, seed=seed, n_agents=config.n_agents, n_ticks=config.n_ticks
            )
            profile_findings = to_findings(result, snapshot.snapshot_id, model)
            profile_findings.extend(
                unproven_input_application_findings(
                    snapshot_id=snapshot.snapshot_id,
                    constraints=tuple(constraints),
                    scenario=None,
                )
            )
            _require_finding_authority(
                profile_findings,
                snapshot_id=snapshot.snapshot_id,
                source="sim",
                oracle_type="simulation",
                statuses=frozenset(("confirmed", "unproven")),
                label="review simulation",
            )
            profile_findings = filter_findings_by_selection(
                profile_findings, payload.selection, snapshot
            )
            profile_findings = [
                _scope_profile_finding(profile, finding) for finding in profile_findings
            ]
            profile_findings = rebind_finding_producers(profile_findings, run_id=context.run.run_id)
            if (
                existing_finding_count + len(findings) + len(profile_findings)
                > MAX_PREPARED_FINDINGS
            ):
                raise IntegrityViolation("review findings exceed the frozen output bound")
            findings.extend(profile_findings)
            evidence_artifact_index = evidence_artifact_index_offset + len(artifacts)
            evidence.extend(
                FindingEvidence(
                    finding=finding,
                    evidence_artifact_index=evidence_artifact_index,
                )
                for finding in profile_findings
            )
            artifacts.append(
                store_prepared_artifact(
                    self.store,
                    kind="simulation_run",
                    payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
                    version_tuple=prepared_version_tuple(
                        context,
                        tool_version="economy-sim@1",
                        projected_fields=(
                            "constraint_snapshot_id",
                            "env_contract_version",
                            "seed",
                        ),
                        overrides={"ir_snapshot_id": snapshot.snapshot_id},
                    ),
                    lineage=lineage,
                    payload=_profile_sim_payload(
                        profile,
                        snapshot.snapshot_id,
                        seed,
                        config,
                        result,
                        profile_findings,
                        constraints=constraints,
                        constraint_snapshot_artifact_id=(payload.constraint_snapshot_artifact_id),
                        root_seed=root_seed,
                        run_kind=context.run.kind,
                        case_id=case_id,
                    ),
                )
            )
        return findings, tuple(artifacts), tuple(evidence)

    def _run_triage(
        self,
        context: ExecutorContextLike,
        snapshot: Snapshot,
        deterministic_findings: list[Finding],
        execution_config: ReviewExecutionConfig,
    ) -> list[Finding]:
        payload = context.payload.params
        if not isinstance(payload, ReviewRunPayloadV1):
            raise TypeError("review triage requires a review-run@1 payload")
        adapter = ModelBridgeAgentAdapter(
            model_bridge=context.model_bridge,
            idempotency_scope=(f"run:{context.run.run_id}:attempt:{context.attempt.attempt_no}"),
            deadline_utc=context.deadline_utc,
        )
        model_snapshot = plan_node_snapshot(
            context.payload.execution_version_plan,
            TRIAGE_AGENT_NODE_ID,
            context.model_bridge,
        )
        prompt = _triage_prompt(snapshot.snapshot_id, deterministic_findings)
        require_agent_prompt_message_bytes(
            prompt,
            max_prompt_message_bytes=execution_config.max_prompt_message_bytes,
        )
        result = adapter.call_model(
            agent_node_id=TRIAGE_AGENT_NODE_ID,
            user_prompt=prompt,
            prompt_version=TRIAGE_PROMPT_VERSION,
            model_snapshot=model_snapshot,
            source_artifact_ids=prompt_source_artifact_ids(
                context,
                selected=tuple(
                    sorted(
                        (
                            payload.snapshot_artifact_id,
                            *(
                                (payload.constraint_snapshot_artifact_id,)
                                if payload.constraint_snapshot_artifact_id is not None
                                else ()
                            ),
                        )
                    )
                ),
            ),
            context_kind="review_triage",
        )
        return _parse_triage_suggestions(result.response.response_normalized, snapshot.snapshot_id)


def _snapshot_lineage(payload: ReviewRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    return tuple(lineage)


def _trusted_checker_execution_bindings(
    checker: Checker,
    constraints: list[Constraint],
) -> tuple[CheckerExecutionBinding, ...]:
    """Close a review companion over every deterministic executor actually run."""

    raw = getattr(checker, "executed_checker_bindings", None)
    if raw is None:
        checker_id = getattr(checker, "id", None)
        if constraints:
            raise IntegrityViolation(
                "constrained review checker omitted trusted execution bindings"
            )
        raw = (
            (
                CheckerExecutionBinding(
                    wrapper_id=checker_id,
                    native_id=checker_id,
                    constraint_id=None,
                ),
            )
            if isinstance(checker_id, str) and checker_id
            else ()
        )
    if not isinstance(raw, tuple) or any(
        not isinstance(value, CheckerExecutionBinding) for value in raw
    ):
        raise IntegrityViolation("review checker returned malformed execution bindings")
    ordered = tuple(
        sorted(
            set(raw),
            key=lambda item: (
                item.native_id,
                item.constraint_id or "",
                item.wrapper_id,
            ),
        )
    )
    if not ordered and not constraints:
        raise IntegrityViolation("review checker omitted trusted execution bindings")
    deterministic_constraint_ids = {
        constraint.id for constraint in constraints if not constraint.has_llm_predicate()
    }
    scoped = tuple(item.constraint_id for item in ordered if item.constraint_id is not None)
    if len(scoped) != len(set(scoped)) or set(scoped) != deterministic_constraint_ids:
        raise IntegrityViolation(
            "review checker execution bindings differ from the exact deterministic constraints"
        )
    return ordered


def _scope_profile_finding(profile: ProfileRefV1, finding: Finding) -> Finding:
    """Give each profile execution its own stable Finding series identity."""

    finding_id = finding.id
    if finding.constraint_id is not None:
        finding_id = scoped_finding_series_id(
            namespace="constraint",
            scope_id=finding.constraint_id,
            finding_id=finding_id,
        )
    return finding.model_copy(
        update={
            "id": scoped_finding_series_id(
                namespace="profile",
                scope_id=f"{profile.profile_id}@{profile.version}",
                finding_id=finding_id,
            )
        }
    )


def _require_checker_finding_authority(
    findings: list[Finding],
    *,
    snapshot_id: str,
    constraints: list[Constraint],
) -> None:
    """Accept only deterministic checker verdicts or exact LLM-route placeholders.

    A checker profile owns both the deterministic backends and the DSL compiler.
    The latter deliberately emits :class:`LlmRoutedChecker` placeholders for
    mixed/LLM predicates.  Those placeholders are evidence that the predicate
    was *not* decided by a deterministic oracle; rejecting or relabelling them
    would erase that boundary.  Conversely, a custom checker port may not use
    this allowance to substitute an arbitrary LLM/simulation/human verdict.
    """

    llm_constraint_ids = {
        constraint.id for constraint in constraints if constraint.has_llm_predicate()
    }
    for finding in findings:
        if not isinstance(finding, Finding):
            raise IntegrityViolation("review checker returned a non-Finding value")
        if finding.snapshot_id != snapshot_id:
            raise IntegrityViolation(
                "review checker Finding differs from its exact oracle authority",
                finding_id=finding.id,
            )
        if (
            finding.source == "checker"
            and finding.oracle_type == "deterministic"
            and finding.status in {"confirmed", "unproven"}
        ):
            continue
        if (
            finding.source == "llm"
            and finding.oracle_type == "llm-assisted"
            and finding.status == "unproven"
            and finding.producer_id == "llm-routed"
            and finding.defect_class == "llm_assisted_predicate"
            and finding.constraint_id in llm_constraint_ids
        ):
            continue
        raise IntegrityViolation(
            "review checker Finding differs from its exact oracle authority",
            finding_id=finding.id,
        )


def _require_finding_authority(
    findings: list[Finding],
    *,
    snapshot_id: str,
    source: str,
    oracle_type: str,
    statuses: frozenset[str],
    label: str,
) -> None:
    """Reject a port that tries to relabel another oracle as this review stage."""

    for finding in findings:
        if not isinstance(finding, Finding):
            raise IntegrityViolation(f"{label} returned a non-Finding value")
        if (
            finding.snapshot_id != snapshot_id
            or finding.source != source
            or finding.oracle_type != oracle_type
            or finding.status not in statuses
        ):
            raise IntegrityViolation(
                f"{label} Finding differs from its exact oracle authority",
                finding_id=finding.id,
            )


def _profile_checker_payload(
    profile: ProfileRefV1,
    snapshot_id: str,
    findings: list[Finding],
    *,
    execution_bindings: tuple[CheckerExecutionBinding, ...],
    constraint_snapshot_artifact_id: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "payload_schema_version": CHECKER_REPORT_SCHEMA_ID,
        "profile": profile.model_dump(mode="json"),
        "checker_profile": profile.model_dump(mode="json"),
        "checker_execution_bindings": [
            {
                "wrapper_id": item.wrapper_id,
                "native_id": item.native_id,
                "constraint_id": item.constraint_id,
            }
            for item in execution_bindings
        ],
        "constraint_snapshot_binding_status": (
            "not_applicable" if constraint_snapshot_artifact_id is None else "bound"
        ),
        "snapshot_id": snapshot_id,
        "constraint_application": [
            {
                "constraint_id": item.constraint_id,
                "checker_id": item.native_id,
                "status": "executed",
            }
            for item in execution_bindings
            if item.constraint_id is not None
        ],
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    if constraint_snapshot_artifact_id is not None:
        payload["constraint_snapshot_artifact_id"] = constraint_snapshot_artifact_id
    return payload


def _profile_sim_payload(
    profile: ProfileRefV1,
    snapshot_id: str,
    seed: int,
    config: ReviewSimConfig,
    result,
    findings: list[Finding],
    *,
    constraints: list[Constraint],
    constraint_snapshot_artifact_id: str | None,
    root_seed: int,
    run_kind: RunKindRef,
    case_id: str,
) -> dict[str, object]:
    constraint_ids = sorted(constraint.id for constraint in constraints)
    if len(constraint_ids) != len(set(constraint_ids)):
        raise IntegrityViolation("review constraint snapshot repeats a constraint id")
    return {
        "payload_schema_version": SIMULATION_RESULT_SCHEMA_ID,
        "profile": profile.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        # ``simulation_run.version_tuple.seed`` is the Run's frozen root seed.
        # The per-profile child seed is a deterministic execution detail and is
        # retained below with its complete ``subseed@1`` derivation binding.  A
        # top-level child seed would disagree with the terminal publisher's
        # producer projection (and make the Artifact impossible to publish).
        "seed": root_seed,
        "replication_count": config.n_agents,
        "horizon_steps": config.n_ticks,
        "invariants": [
            {
                "name": check.name,
                "ok": check.ok,
                "observed": check.observed,
                "threshold": check.threshold,
                "evidence": check.evidence,
            }
            for check in result.invariants
        ],
        "sensitivity": {
            **result.sensitivity,
            "execution_binding": {
                "simulation_profile": profile.model_dump(mode="json"),
                "constraint_snapshot_artifact_id": constraint_snapshot_artifact_id,
                "constraint_ids": constraint_ids,
                "constraint_application": {
                    "status": (
                        "not_applicable" if constraint_snapshot_artifact_id is None else "unproven"
                    ),
                    **(
                        {}
                        if constraint_snapshot_artifact_id is None
                        else {"reason_code": "constraint_profile_not_executable"}
                    ),
                },
            },
            "seed_binding": {
                "root_seed": root_seed,
                "run_kind": run_kind.model_dump(mode="json"),
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "case_id": case_id,
                "replication_index": 0,
                "seed": seed,
                "seed_derivation_version": "subseed@1",
            },
        },
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _store_review_report(
    store: PreparedArtifactStore,
    *,
    snapshot: Snapshot,
    report: ReviewReport,
    lineage: tuple[str, ...],
    context: ExecutorContextLike,
    recorded_llm_mode: str,
    triage_applied: bool,
) -> PreparedArtifact:
    return store_prepared_artifact(
        store,
        kind="review_report",
        payload_schema_id=REVIEW_SCHEMA_ID,
        version_tuple=prepared_version_tuple(
            context,
            tool_version=REVIEW_TOOL_VERSION,
            projected_fields=("constraint_snapshot_id",),
            overrides={"ir_snapshot_id": snapshot.snapshot_id},
        ),
        lineage=lineage,
        payload=report.model_dump(mode="json"),
        extra_meta={
            "llm_execution_mode": recorded_llm_mode,
            "llm_triage_applied": triage_applied,
        },
    )


def _triage_prompt(snapshot_id: str, findings: list[Finding]) -> str:
    ordered = sorted(
        findings,
        key=lambda finding: (
            finding.id,
            finding.defect_class,
            finding.severity,
        ),
    )[:TRIAGE_MAX_INPUT_FINDINGS]
    body: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "deterministic_findings": [],
        "projection": {
            "total_count": len(findings),
            "included_count": 0,
            "truncated": bool(findings),
        },
    }
    projected = body["deterministic_findings"]
    assert isinstance(projected, list)
    for finding in ordered:
        projected.append(
            {
                "finding_id": _truncate_utf8(finding.id, TRIAGE_MAX_FINDING_ID_BYTES),
                "defect_class": _truncate_utf8(finding.defect_class, TRIAGE_MAX_DEFECT_CLASS_BYTES),
                "severity": finding.severity,
                "message": _truncate_utf8(finding.message, TRIAGE_MAX_INPUT_MESSAGE_BYTES),
            }
        )
        projection = body["projection"]
        assert isinstance(projection, dict)
        projection["included_count"] = len(projected)
        projection["truncated"] = len(projected) < len(findings)
        candidate = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if len(candidate.encode("utf-8")) > TRIAGE_MAX_PROMPT_BYTES:
            projected.pop()
            projection["included_count"] = len(projected)
            projection["truncated"] = True
            break
    prompt = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if len(prompt.encode("utf-8")) > TRIAGE_MAX_PROMPT_BYTES:
        raise IntegrityViolation("bounded review triage prompt exceeds its byte envelope")
    return prompt


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


_VALID_SEVERITIES = {"critical", "major", "minor"}


def _parse_triage_suggestions(text: str, snapshot_id: str) -> list[Finding]:
    """Parse the model's triage output into llm-assisted SUGGESTION findings.

    Parse failure is a fallback signal (no suggestions), never a crash — matching
    the agent-layer convention. Every suggestion is ``status="unproven"`` so it can
    never be mistaken for a proven deterministic verdict.
    """

    if not isinstance(text, str) or len(text.encode("utf-8")) > TRIAGE_MAX_RESPONSE_BYTES:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    suggestions = parsed.get("suggestions") if isinstance(parsed, dict) else None
    if not isinstance(suggestions, list):
        return []
    findings: list[Finding] = []
    for index, item in enumerate(suggestions[:TRIAGE_MAX_SUGGESTIONS]):
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        if severity not in _VALID_SEVERITIES:
            severity = "minor"
        message = item.get("message")
        if not isinstance(message, str) or not message:
            message = "LLM triage suggestion (advisory only, not a proven verdict)"
        message = _truncate_utf8(message, TRIAGE_MAX_SUGGESTION_MESSAGE_BYTES)
        defect_class = item.get("defect_class")
        if not isinstance(defect_class, str) or not defect_class:
            defect_class = "llm_triage_suggestion"
        defect_class = _truncate_utf8(defect_class, TRIAGE_MAX_DEFECT_CLASS_BYTES)
        entities = item.get("entities")
        bounded_entities: list[str] = []
        if isinstance(entities, list):
            for entity in entities[:TRIAGE_MAX_SUGGESTION_ENTITIES]:
                if not isinstance(entity, str) or not entity:
                    continue
                bounded = _truncate_utf8(entity, TRIAGE_MAX_ENTITY_ID_BYTES)
                if bounded and bounded not in bounded_entities:
                    bounded_entities.append(bounded)
        findings.append(
            Finding(
                id=f"review-triage@{snapshot_id[:23]}#{index}",
                source="llm",
                producer_id="review_triage",
                producer_run_id=f"review-triage@{snapshot_id[:23]}",
                oracle_type="llm-assisted",
                defect_class=defect_class,
                severity=severity,
                snapshot_id=snapshot_id,
                entities=bounded_entities,
                status="unproven",
                message=message,
            )
        )
    return findings


__all__ = [
    "CheckerResolver",
    "REVIEW_SCHEMA_ID",
    "ReviewRunHandler",
    "ReviewExecutionConfig",
    "ReviewExecutionConfigResolver",
    "ReviewSimConfig",
    "SimConfigResolver",
    "default_review_execution_config",
]
