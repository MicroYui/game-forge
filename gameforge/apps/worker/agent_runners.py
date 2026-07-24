"""Concrete agent-runner ports that drive the M2 agents for the 11b handlers.

The 11b generation / repair / constraint-proposal handlers live in
``gameforge.platform`` and — by the dependency-direction contract — must NOT import
``gameforge.agents``. Each handler therefore takes an injected agent-runner *port*
(a Protocol declared in ``platform``); this module supplies the concrete
implementations, wired by the worker composition root, that actually invoke the
bounded M2 agents (``ContentGenerator`` + ``gate_proposal`` / ``repair_search`` /
``ExtractionProposer``).

``apps`` is the composition boundary: it may import both ``platform`` and
``agents`` (and ``spine``), so this is the one legitimate place the two sides meet.
The LLM still flows ONLY through the injected ``BridgeModelRouter`` (over the M4b
model bridge); the deterministic gate/verifier verdicts stay in spine
(checkers / economy simulation / headless regression). No LLM SDK is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from gameforge.agents.extraction.proposer import ExtractionProposer
from gameforge.agents.generation.gate import _build_ops
from gameforge.agents.generation.generator import ContentGenerator
from gameforge.agents.repair.search import RepairPromptRoundContext, repair_search
from gameforge.agents.repair.verify import VerifyResult
from gameforge.contracts.agent_io import ConstraintProposal
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, Patch, TypedOp
from gameforge.contracts.jobs import (
    AgentPromptContextDraftV1,
    AgentPromptSemanticBindingV1,
    AgentPromptSourceMessageV1,
    ResolvedArtifactRequirementV1,
)
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.regression import RegressionCaseSeedManifestV1
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch
from gameforge.spine.sim.economy import (
    EconomyModel,
    EconomySimulator,
    SimResult,
    to_findings,
)

from gameforge.platform.run_handlers.constraint_proposal import (
    ConstraintProposalOutcomeV1,
    ConstraintProposalRunRequest,
)
from gameforge.platform.run_handlers.generation import (
    GenerationGateOutcomeV1,
    GenerationRunRequest,
    PreparedEvidenceV1,
)
from gameforge.platform.run_handlers.repair import (
    RepairRunRequest,
    RepairSearchOutcomeV1,
)
from gameforge.platform.run_handlers.checker import navigation_unproven_findings
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.simulation import (
    validate_economy_simulation_work_budget,
)
from gameforge.platform.run_handlers.validation_common import (
    RegressionRunRequest,
    RegressionRunner,
    derive_validation_subseed,
    with_validation_child_seed_evidence,
)

CheckerFactory = Callable[[Snapshot, tuple[Constraint, ...]], list[Checker]]


def _findings_payload(
    schema: str,
    snapshot_id: str,
    findings: tuple[Finding, ...],
    *,
    requirement_id: str,
) -> dict:
    return {
        "payload_schema_version": schema,
        "requirement_id": requirement_id,
        "snapshot_id": snapshot_id,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _generation_simulation_payload(
    snapshot_id: str,
    findings: tuple[Finding, ...],
    *,
    requirement_id: str,
    seed: int,
    population: int,
    horizon_steps: int,
) -> dict[str, object]:
    """Bind generation's deterministic fixed-seed simulation budget exactly."""

    return {
        "payload_schema_version": "simulation-result@1",
        "requirement_id": requirement_id,
        "snapshot_id": snapshot_id,
        "seed": seed,
        "replication_count": population,
        "horizon_steps": horizon_steps,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _profile_sim_payload(
    *,
    profile: ProfileRefV1,
    snapshot_id: str,
    seed: int,
    config: ReviewSimConfig,
    result: SimResult,
    findings: tuple[Finding, ...],
    root_seed: int,
    run_kind: RunKindRef,
    case_id: str,
    requirement_id: str,
) -> dict[str, object]:
    """Exact profile/budget/subseed evidence for one repair simulation gate."""

    return {
        "payload_schema_version": "simulation-result@1",
        "requirement_id": requirement_id,
        "profile": profile.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        # The simulation Artifact binds the Run root seed; the child execution
        # seed remains fully recoverable from ``sensitivity.seed_binding``.
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


def _strict_economy_findings(
    snapshot: Snapshot,
    *,
    seed: int,
    population: int,
    horizon_steps: int,
) -> tuple[Finding, ...]:
    """Run the generation gate simulation; malformed economies fail closed."""

    model = EconomyModel.from_snapshot(snapshot)
    result = EconomySimulator().run(
        model,
        seed=seed,
        n_agents=population,
        n_ticks=horizon_steps,
    )
    return tuple(to_findings(result, snapshot.snapshot_id, model=model))


def _generation_rejection_finding(snapshot_id: str, reason_code: str) -> Finding:
    """Represent a structural gate rejection inside the frozen Finding wire."""

    return Finding(
        id=f"finding:generation-gate:{reason_code}",
        source="checker",
        producer_id="generation-gate@1",
        producer_run_id="generation-gate",
        oracle_type="deterministic",
        defect_class="invalid_generation_proposal",
        severity="major",
        snapshot_id=snapshot_id,
        evidence={"reason_code": reason_code},
        minimal_repro={"proposal_component": "typed_ops"},
        status="confirmed",
        message=f"generation proposal rejected: {reason_code}",
    )


def _generation_unproven_simulation_finding(snapshot_id: str, reason_code: str) -> Finding:
    """Make a skipped candidate simulation explicit in retained gate evidence."""

    return Finding(
        id=f"finding:generation-simulation:{reason_code}",
        source="sim",
        producer_id="generation-gate@1",
        producer_run_id="generation-gate",
        oracle_type="simulation",
        defect_class="generation_gate_simulation_unproven",
        severity="major",
        snapshot_id=snapshot_id,
        evidence={"reason_code": reason_code},
        minimal_repro={"proposal_component": "typed_ops"},
        status="unproven",
        message=f"generation gate simulation was not executed: {reason_code}",
    )


def _deterministic_finding_key(finding: Finding) -> str:
    """Semantic baseline identity for deterministic generation-gate findings.

    Entity/class alone is not an identity: a candidate can introduce a second
    violation on another relation or constraint while the base already has the
    same class on the same entity. Keep the complete replay-stable semantic
    payload, including checker-specific evidence/minimal repro because some
    checkers encode the exact relation only there. Exclude series/producer/run,
    snapshot, timestamp, and presentation text, which change across executions.
    """

    return canonical_json(
        {
            "finding_schema_version": finding.finding_schema_version,
            "source": finding.source,
            "oracle_type": finding.oracle_type,
            "defect_class": finding.defect_class,
            "severity": finding.severity,
            "entities": sorted(finding.entities),
            "relations": sorted(finding.relations),
            "constraint_id": finding.constraint_id,
            "evidence": finding.evidence,
            "minimal_repro": finding.minimal_repro,
            "status": finding.status,
            "confidence": finding.confidence,
        }
    )


def _requirements_for(
    requirements: tuple[ResolvedArtifactRequirementV1, ...],
    outcome_rule_id: str,
    *,
    expected_count: int,
) -> tuple[ResolvedArtifactRequirementV1, ...]:
    selected = tuple(
        requirement
        for requirement in requirements
        if requirement.outcome_rule_id == outcome_rule_id
    )
    if len(selected) != expected_count:
        raise ValueError(
            f"frozen {outcome_rule_id} requirement count differs from handler dimensions"
        )
    return selected


def _exact_finding_key(finding: Finding) -> str:
    """Stable replay identity without requiring stochastic evidence byte equality."""

    return _deterministic_finding_key(finding)


_LOCATOR_KEYS = frozenset(
    {
        "case",
        "case_id",
        "constraint",
        "constraint_id",
        "edge_type",
        "entity",
        "entity_id",
        "invariant",
        "missing",
        "path",
        "relation",
        "relation_id",
        "scenario_id",
        "source_ref",
        "subject",
        "target",
    }
)


def _evidence_locator(value: object) -> object:
    """Keep stable identity/location evidence while dropping observed measurements."""

    if not isinstance(value, dict):
        return value
    return {
        key: _evidence_locator(item)
        for key, item in sorted(value.items())
        if key in _LOCATOR_KEYS or key.endswith("_id") or key.endswith("_ids")
    }


def _target_finding_key(finding: Finding) -> str:
    """Predicate identity used to decide whether one exact target still exists.

    Unlike the full set-diff key, this intentionally omits severity/status and
    observed numeric evidence: those may change while the same violated predicate
    remains. Stable relation/entity/constraint locators remain authoritative.
    """

    return canonical_json(
        {
            "finding_schema_version": finding.finding_schema_version,
            "source": finding.source,
            "oracle_type": finding.oracle_type,
            "defect_class": finding.defect_class,
            "entities": sorted(finding.entities),
            "relations": sorted(finding.relations),
            "constraint_id": finding.constraint_id,
            "minimal_repro": finding.minimal_repro,
            "evidence_locator": _evidence_locator(finding.evidence),
        }
    )


def _gate_finding_key(finding: Finding) -> str:
    """Stable predicate+proof-state identity for base/candidate gate set-diff."""

    return canonical_json({"predicate": _target_finding_key(finding), "status": finding.status})


def _combined_ops(
    original: tuple[TypedOp, ...], corrective: tuple[TypedOp, ...]
) -> tuple[TypedOp, ...]:
    """Retain original ops byte-semantically and only disambiguate colliding new ids."""

    combined = list(original)
    used_ids = {op.op_id for op in original}
    for index, op in enumerate(corrective, start=1):
        candidate = op.op_id
        if candidate in used_ids:
            candidate = f"repair:{index}:{candidate}"
            suffix = 1
            while candidate in used_ids:
                suffix += 1
                candidate = f"repair:{index}:{suffix}:{op.op_id}"
            op = op.model_copy(update={"op_id": candidate})
        used_ids.add(candidate)
        combined.append(op)
    return tuple(combined)


@dataclass(frozen=True, slots=True)
class M2GenerationAgentRunner:
    """Drive ``ContentGenerator`` + the deterministic ``gate_proposal``."""

    checker_factory: CheckerFactory

    def run(self, request: GenerationRunRequest) -> GenerationGateOutcomeV1:
        checkers = self.checker_factory(request.snapshot, request.constraints)
        generator = ContentGenerator(request.snapshot, checkers)
        goal = request.goal
        if request.findings:
            goal = goal.model_copy(
                update={
                    "goal": (
                        f"{goal.goal}\n\nExact admitted findings (JSON):\n"
                        + canonical_json(
                            [finding.model_dump(mode="json") for finding in request.findings]
                        )
                    )
                }
            )
        result = generator.run(
            goal,
            request.router,
            prompt_version=request.prompt_version,
            execute_local_gate=False,
        )

        proposal = result.produced.get("proposal", {})
        ops_raw = proposal.get("proposed_ops", []) if isinstance(proposal, dict) else []
        typed_ops = _build_ops(list(ops_raw))
        rejection_reason: str | None = None
        if result.fallback_taken:
            rejection_reason = "model_response_unparseable"
        elif typed_ops is None:
            rejection_reason = "malformed_ops"
            typed_ops = []
        elif not typed_ops:
            rejection_reason = "empty_ops"
        patch = Patch(
            id=f"generation@{request.snapshot.snapshot_id[:16]}",
            base_snapshot_id=request.snapshot.snapshot_id,
            target_snapshot_id="",
            side_effect_risk="low",
            ops=typed_ops,
            produced_by="agent",
            producer_run_id="generation",
            rationale="generated content proposal",
        )
        try:
            patched = apply_patch(request.snapshot, patch)
        except PatchRejected:
            rejection_reason = "inapplicable_ops"
            patched = request.snapshot

        candidate_gate_executable = rejection_reason is None
        if candidate_gate_executable:
            node_count = len(patched.entities)
            checker_work_units = max(
                1,
                node_count * node_count + node_count + len(patched.relations),
            ) * (1 + len(request.constraints))
            try:
                if checker_work_units > request.max_checker_work_units:
                    raise IntegrityViolation("generated candidate exceeds the checker work budget")
                validate_economy_simulation_work_budget(
                    EconomyModel.from_snapshot(patched),
                    n_agents=request.gate_simulation_population,
                    n_ticks=request.gate_simulation_horizon_steps,
                    replication_count=1,
                    max_work_units=request.max_simulation_work_units,
                )
            except (IntegrityViolation, TypeError, ValueError, OverflowError):
                rejection_reason = "candidate_work_budget_exceeded"
                candidate_gate_executable = False

        base_checker_findings: list[Finding] = []
        checker_findings: list[Finding] = []
        for checker in checkers:
            base_checker_findings.extend(checker.check(request.snapshot))
            if candidate_gate_executable:
                checker_findings.extend(checker.check(patched))
        base_checker_findings.extend(navigation_unproven_findings(request.snapshot, None))
        if candidate_gate_executable:
            checker_findings.extend(navigation_unproven_findings(patched, None))
        simulation_kwargs = {
            "seed": request.gate_simulation_seed,
            "population": request.gate_simulation_population,
            "horizon_steps": request.gate_simulation_horizon_steps,
        }
        base_sim_findings = _strict_economy_findings(request.snapshot, **simulation_kwargs)
        sim_findings = (
            _strict_economy_findings(patched, **simulation_kwargs)
            if candidate_gate_executable
            else (
                _generation_unproven_simulation_finding(
                    patched.snapshot_id,
                    rejection_reason or "candidate_gate_not_executed",
                ),
            )
        )
        rejection_findings = (
            ()
            if rejection_reason is None
            else (_generation_rejection_finding(patched.snapshot_id, rejection_reason),)
        )
        review = ReviewReport.partition(
            patched.snapshot_id,
            [*checker_findings, *sim_findings, *rejection_findings],
        )
        base_gate_findings = (*base_checker_findings, *base_sim_findings)
        base_predicates = {
            _gate_finding_key(finding)
            for finding in base_gate_findings
            if finding.oracle_type != "llm-assisted"
        }
        new_gate_findings = tuple(
            finding
            for finding in (*checker_findings, *sim_findings, *rejection_findings)
            if finding.oracle_type != "llm-assisted"
            and _gate_finding_key(finding) not in base_predicates
        )
        passed = rejection_reason is None and not new_gate_findings
        checker_requirement = _requirements_for(
            request.gate_requirements, "checker", expected_count=1
        )[0]
        simulation_requirement = _requirements_for(
            request.gate_requirements, "simulation", expected_count=1
        )[0]
        review_requirement = _requirements_for(
            request.gate_requirements, "review", expected_count=1
        )[0]

        preview_id = patched.snapshot_id
        return GenerationGateOutcomeV1(
            ops=tuple(typed_ops),
            passed=passed,
            preview_payload=patched.content_payload,
            preview_snapshot_id=preview_id,
            checker_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="checker",
                    requirement_id=checker_requirement.requirement_id,
                    payload=_findings_payload(
                        "checker-report@1",
                        preview_id,
                        tuple((*checker_findings, *rejection_findings)),
                        requirement_id=checker_requirement.requirement_id,
                    ),
                    findings=tuple((*checker_findings, *rejection_findings)),
                ),
            ),
            simulation_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="simulation",
                    requirement_id=simulation_requirement.requirement_id,
                    payload=_generation_simulation_payload(
                        preview_id,
                        sim_findings,
                        requirement_id=simulation_requirement.requirement_id,
                        seed=request.gate_simulation_seed,
                        population=request.gate_simulation_population,
                        horizon_steps=request.gate_simulation_horizon_steps,
                    ),
                    findings=sim_findings,
                ),
            ),
            review_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="review",
                    requirement_id=review_requirement.requirement_id,
                    payload={
                        **review.model_dump(mode="json"),
                        "requirement_id": review_requirement.requirement_id,
                    },
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _ExactRepairEvaluation:
    """One exact-profile verifier pass over a concrete immutable snapshot."""

    complete: bool
    entity_ids: frozenset[str] = frozenset()
    checker_findings: tuple[tuple[str, tuple[Finding, ...]], ...] = ()
    simulation_findings: tuple[tuple[str, tuple[Finding, ...]], ...] = ()
    failed_simulation_invariants: tuple[tuple[str, tuple[str, ...]], ...] = ()
    regression_statuses: tuple[tuple[str, str], ...] = ()
    regression_findings: tuple[tuple[str, tuple[Finding, ...]], ...] = ()
    checker_evidence: tuple[PreparedEvidenceV1, ...] = ()
    simulation_evidence: tuple[PreparedEvidenceV1, ...] = ()
    regression_evidence: tuple[PreparedEvidenceV1, ...] = ()


@dataclass(slots=True)
class _VerifierBudgetLedger:
    """Run-wide deterministic work remaining across every repair candidate."""

    checker_work_units: int
    simulation_work_units: int
    regression_work_units: int


def _repair_verdict_payload(result: VerifyResult) -> dict[str, object]:
    """Canonical semantic preimage for verifier feedback embedded in a refine."""

    return {
        "ok": result.ok,
        "target_resolved": result.target_resolved,
        "new_deterministic": [
            finding.model_dump(mode="json") for finding in result.new_deterministic
        ],
        "regression_ok": result.regression_ok,
        "detail": result.detail,
        "regression_ran": result.regression_ran,
        "economy_ran": result.economy_ran,
    }


def _repair_verifier_closure_payload(request: RepairRunRequest) -> dict[str, object]:
    """Identity of every exact deterministic authority that can shape feedback."""

    return {
        "constraint_snapshot_artifact_id": request.constraint_snapshot_artifact_id,
        "checker_profiles": [
            profile.model_dump(mode="json") for profile, _checker in request.checkers
        ],
        "simulation_profiles": [
            {
                "profile": profile.model_dump(mode="json"),
                "config": {
                    "n_agents": config.n_agents,
                    "n_ticks": config.n_ticks,
                    "max_work_units": config.max_work_units,
                },
            }
            for profile, config in request.simulation_profiles
        ],
        "regression_suite_artifact_ids": list(request.regression_suite_artifact_ids),
        "verifier_requirements": [
            requirement.model_dump(mode="json") for requirement in request.verifier_requirements
        ],
        "root_seed": request.seed,
        "run_kind": request.run_kind.model_dump(mode="json"),
        "regression_profile": request.regression_profile.model_dump(mode="json"),
    }


def _prepare_repair_prompt_context(
    request: RepairRunRequest,
    context: RepairPromptRoundContext,
) -> None:
    """Bind one exact Repair round before the corresponding bridged model call."""

    finding_binding = next(
        (
            binding
            for binding in request.finding_evidence_bindings
            if binding.finding_id == context.finding.id
        ),
        None,
    )
    if finding_binding is None:
        raise IntegrityViolation("Repair prompt finding escapes its frozen evidence binding")

    snapshot_digest = canonical_sha256(context.snapshot.content_payload)
    if context.snapshot.snapshot_id != f"sha256:{snapshot_digest}":
        raise IntegrityViolation("Repair prompt working Snapshot identity is not canonical")

    semantic_bindings = [
        AgentPromptSemanticBindingV1(
            binding_key="repair.finding",
            subject_id=context.finding.id,
            subject_revision=finding_binding.finding_revision,
            subject_digest=finding_binding.finding_digest,
        ),
        AgentPromptSemanticBindingV1(
            binding_key="repair.working_snapshot",
            subject_id=context.snapshot.snapshot_id,
            subject_digest=snapshot_digest,
        ),
    ]
    if context.previous_patch is not None:
        semantic_bindings.append(
            AgentPromptSemanticBindingV1(
                binding_key="repair.previous_patch",
                subject_id=context.previous_patch.id,
                subject_digest=canonical_sha256(context.previous_patch.model_dump(mode="json")),
            )
        )
    if context.previous_verdict is not None:
        semantic_bindings.append(
            AgentPromptSemanticBindingV1(
                binding_key="repair.previous_verdict",
                subject_id="repair:previous-verdict",
                subject_digest=canonical_sha256(_repair_verdict_payload(context.previous_verdict)),
            )
        )

    # A verifier-failed refine consumes the exact constraint/suite closure.  A
    # later Finding's local initial round also consumes it because its working
    # Snapshot is the prior candidate admitted by that same verifier closure.
    uses_verifier_closure = (
        context.previous_verdict is not None
        or context.snapshot.snapshot_id != request.failed_preview_snapshot.snapshot_id
    )
    source_ids = {
        request.failed_preview_artifact_id,
        finding_binding.evidence_artifact_id,
    }
    if uses_verifier_closure:
        source_ids.update(request.regression_suite_artifact_ids)
        if request.constraint_snapshot_artifact_id is not None:
            source_ids.add(request.constraint_snapshot_artifact_id)
        closure = _repair_verifier_closure_payload(request)
        semantic_bindings.append(
            AgentPromptSemanticBindingV1(
                binding_key="repair.verifier_closure",
                subject_id="repair:verifier-closure",
                subject_digest=canonical_sha256(closure),
            )
        )

    request.router.prepare_prompt_context(
        AgentPromptContextDraftV1(
            context_kind=("repair_initial" if context.phase == "initial" else "repair_refine"),
            messages=(
                AgentPromptSourceMessageV1(
                    role="user",
                    content=context.user_prompt,
                    purpose="context",
                ),
            ),
            source_artifact_ids=tuple(sorted(source_ids)),
            semantic_bindings=tuple(semantic_bindings),
            include_previous_consumption=request.router.call_count > 0,
        )
    )


@dataclass(frozen=True, slots=True)
class M2RepairAgentRunner:
    """Drive exact failed-preview search and full combined-patch verification."""

    regression_runner: RegressionRunner | None = None

    def search(self, request: RepairRunRequest) -> RepairSearchOutcomeV1:
        if not request.findings:
            raise ValueError("repair_search requires at least one target finding")

        budget = _VerifierBudgetLedger(
            checker_work_units=request.max_total_checker_work_units,
            simulation_work_units=request.max_total_simulation_work_units,
            regression_work_units=request.max_total_regression_work_units,
        )

        # First execute the frozen verifier dimensions against the immutable
        # failed input preview. These are the only evidence artifacts an
        # unverified outcome may publish: its lineage policy explicitly binds
        # the run-input preview and forbids publishing a new candidate preview.
        baseline = self._evaluate_exact(request, request.failed_preview_snapshot, budget)
        if not baseline.complete:
            return self._unverified(0, "execution_short_circuited", baseline)
        if any(
            not self._target_has_reproduction_authority(baseline, finding)
            for finding in request.findings
        ):
            return self._unverified(0, "search_exhausted", baseline)

        checkers = [checker for _, checker in request.checkers]
        working_preview = request.failed_preview_snapshot
        evaluations = {working_preview.snapshot_id: baseline}
        corrective_ops: list[TypedOp] = []
        search_steps = 0
        for finding in request.findings:
            current_evaluation = evaluations[working_preview.snapshot_id]
            if self._target_resolved(baseline, current_evaluation, finding):
                continue
            remaining_steps = request.max_steps - search_steps
            if remaining_steps <= 0:
                return self._unverified(search_steps, "search_exhausted", baseline)

            def exact_candidate_verifier(
                _base: Snapshot,
                candidate: Snapshot,
                _checkers: list[Checker],
                _target_class: str,
            ) -> VerifyResult:
                del _base, _checkers, _target_class
                evaluation = self._evaluate_exact(request, candidate, budget)
                evaluations[candidate.snapshot_id] = evaluation
                return self._verdict(
                    request.failed_preview_snapshot,
                    baseline,
                    evaluation,
                    finding,
                )

            draft = repair_search(
                finding,
                working_preview,
                checkers,
                request.router,
                max_steps=remaining_steps,
                # M4 repair regression authority is the exact admitted suite set
                # executed below.  The legacy M2 smoke is not profile/suite-bound
                # and emits no corresponding evidence, so it cannot participate.
                run_regression=False,
                # Likewise, M4 simulation authority is the exact admitted profile
                # set executed below, never M2's hidden fixed-budget economy sim.
                run_economy=False,
                # Every draft is judged by the exact selected checker/simulation/
                # regression closure. A failing exact simulation therefore feeds
                # a counterexample into the next bounded LLM draft.
                candidate_verifier=exact_candidate_verifier,
                prompt_context_hook=lambda context: _prepare_repair_prompt_context(
                    request,
                    context,
                ),
            )
            search_steps += draft.search_steps
            if not draft.passed_verification:
                return self._unverified(search_steps, "search_exhausted", baseline)
            try:
                working_preview = apply_patch(working_preview, draft.patch)
            except PatchRejected:
                return self._unverified(search_steps, "search_exhausted", baseline)
            corrective_ops.extend(draft.patch.ops)

        final_evaluation = evaluations.get(working_preview.snapshot_id)
        if final_evaluation is None:
            final_evaluation = self._evaluate_exact(request, working_preview, budget)
        if not final_evaluation.complete:
            return self._unverified(search_steps, "execution_short_circuited", baseline)
        verdicts = tuple(
            self._verdict(
                request.failed_preview_snapshot,
                baseline,
                final_evaluation,
                finding,
            )
            for finding in request.findings
        )
        if not all(verdict.ok for verdict in verdicts):
            reason = (
                "prior_requirement_failed"
                if any(
                    not verdict.regression_ok or verdict.new_deterministic for verdict in verdicts
                )
                else "search_exhausted"
            )
            return self._unverified(search_steps, reason, baseline)

        combined_ops = _combined_ops(tuple(request.current_patch.ops), tuple(corrective_ops))
        combined_patch = Patch(
            id=f"repair-combined@{request.base_snapshot.snapshot_id[:16]}",
            base_snapshot_id=request.base_snapshot.snapshot_id,
            target_snapshot_id=working_preview.snapshot_id,
            expected_to_fix=list(
                dict.fromkeys(
                    (
                        *request.current_patch.expected_to_fix,
                        *(finding.id for finding in request.findings),
                    )
                )
            ),
            preconditions=list(request.current_patch.preconditions),
            side_effect_risk=request.current_patch.side_effect_risk,
            ops=list(combined_ops),
            produced_by="agent",
            producer_run_id="repair-search",
            rationale="full exact-base candidate plus verifier-closed corrective ops",
        )
        try:
            patched = apply_patch(request.base_snapshot, combined_patch)
        except PatchRejected:
            return self._unverified(search_steps, "search_exhausted", baseline)
        if patched.snapshot_id != working_preview.snapshot_id:
            return self._unverified(search_steps, "search_exhausted", baseline)
        preview_id = patched.snapshot_id
        return RepairSearchOutcomeV1(
            passed_verification=True,
            search_steps=search_steps,
            ops=combined_ops,
            preview_payload=patched.content_payload,
            preview_snapshot_id=preview_id,
            expected_to_fix=tuple(finding.id for finding in request.findings),
            side_effect_risk=request.current_patch.side_effect_risk,
            checker_evidence=final_evaluation.checker_evidence,
            simulation_evidence=final_evaluation.simulation_evidence,
            regression_evidence=final_evaluation.regression_evidence,
        )

    def _evaluate_exact(
        self,
        request: RepairRunRequest,
        snapshot: Snapshot,
        budget: _VerifierBudgetLedger,
    ) -> _ExactRepairEvaluation:
        checker_requirements = _requirements_for(
            request.verifier_requirements,
            "checker",
            expected_count=len(request.checkers),
        )
        simulation_requirements = _requirements_for(
            request.verifier_requirements,
            "simulation",
            expected_count=len(request.simulation_profiles),
        )
        regression_requirements = _requirements_for(
            request.verifier_requirements,
            "regression",
            expected_count=len(request.regression_suite_artifact_ids),
        )
        checker_findings: list[tuple[str, tuple[Finding, ...]]] = []
        simulation_findings: list[tuple[str, tuple[Finding, ...]]] = []
        failed_invariants: list[tuple[str, tuple[str, ...]]] = []
        regression_statuses: list[tuple[str, str]] = []
        regression_findings: list[tuple[str, tuple[Finding, ...]]] = []
        checker_evidence: list[PreparedEvidenceV1] = []
        simulation_evidence: list[PreparedEvidenceV1] = []
        regression_evidence: list[PreparedEvidenceV1] = []

        def result(complete: bool) -> _ExactRepairEvaluation:
            return _ExactRepairEvaluation(
                complete=complete,
                entity_ids=frozenset(snapshot.entities),
                checker_findings=tuple(checker_findings),
                simulation_findings=tuple(simulation_findings),
                failed_simulation_invariants=tuple(failed_invariants),
                regression_statuses=tuple(regression_statuses),
                regression_findings=tuple(regression_findings),
                checker_evidence=tuple(checker_evidence),
                simulation_evidence=tuple(simulation_evidence),
                regression_evidence=tuple(regression_evidence),
            )

        node_count = len(snapshot.entities)
        checker_units = (
            max(1, node_count * node_count + node_count + len(snapshot.relations))
            * (1 + len(request.constraints))
            * len(request.checkers)
        )
        if checker_units > budget.checker_work_units:
            return result(False)

        model: EconomyModel | None = None
        simulation_units = 0
        if request.simulation_profiles:
            try:
                model = EconomyModel.from_snapshot(snapshot)
                for _profile, config in request.simulation_profiles:
                    simulation_units += validate_economy_simulation_work_budget(
                        model,
                        n_agents=config.n_agents,
                        n_ticks=config.n_ticks,
                        replication_count=1,
                        max_work_units=config.max_work_units,
                    )
                    if simulation_units > budget.simulation_work_units:
                        return result(False)
            except Exception:  # noqa: BLE001 — invalid work authority proves nothing
                return result(False)
        budget.checker_work_units -= checker_units
        budget.simulation_work_units -= simulation_units

        for requirement, (_profile, checker) in zip(
            checker_requirements, request.checkers, strict=True
        ):
            try:
                findings = tuple(
                    (*checker.check(snapshot), *navigation_unproven_findings(snapshot, None))
                )
            except Exception:  # noqa: BLE001 — an unexecuted checker proves nothing
                return result(False)
            checker_findings.append((requirement.requirement_id, findings))
            checker_evidence.append(
                PreparedEvidenceV1(
                    outcome_rule_id="checker",
                    requirement_id=requirement.requirement_id,
                    payload=_findings_payload(
                        "checker-report@1",
                        snapshot.snapshot_id,
                        findings,
                        requirement_id=requirement.requirement_id,
                    ),
                    findings=findings,
                )
            )

        if request.simulation_profiles and request.seed is None:
            return result(False)
        for requirement, (profile, config) in zip(
            simulation_requirements,
            request.simulation_profiles,
            strict=True,
        ):
            if request.seed is None or config.n_agents < 1 or config.n_ticks < 1:
                return result(False)
            execution_seed = derive_validation_subseed(
                root_seed=request.seed,
                run_kind=request.run_kind,
                profile=profile,
                case_id=requirement.requirement_id,
                replication_index=0,
            )
            try:
                assert model is not None
                simulation_result = EconomySimulator().run(
                    model,
                    seed=execution_seed,
                    n_agents=config.n_agents,
                    n_ticks=config.n_ticks,
                )
                findings = tuple(to_findings(simulation_result, snapshot.snapshot_id, model=model))
            except Exception:  # noqa: BLE001 — an unavailable exact profile proves nothing
                return result(False)
            simulation_findings.append((requirement.requirement_id, findings))
            failed_invariants.append(
                (
                    requirement.requirement_id,
                    tuple(
                        sorted(check.name for check in simulation_result.invariants if not check.ok)
                    ),
                )
            )
            simulation_evidence.append(
                PreparedEvidenceV1(
                    outcome_rule_id="simulation",
                    requirement_id=requirement.requirement_id,
                    payload=_profile_sim_payload(
                        profile=profile,
                        snapshot_id=snapshot.snapshot_id,
                        seed=execution_seed,
                        config=config,
                        result=simulation_result,
                        findings=findings,
                        root_seed=request.seed,
                        run_kind=request.run_kind,
                        case_id=requirement.requirement_id,
                        requirement_id=requirement.requirement_id,
                    ),
                    findings=findings,
                )
            )

        if request.regression_suite_artifact_ids and (
            self.regression_runner is None or request.seed is None
        ):
            return result(False)
        for requirement, suite_id in zip(
            regression_requirements,
            request.regression_suite_artifact_ids,
            strict=True,
        ):
            assert self.regression_runner is not None and request.seed is not None
            execution_seed = derive_validation_subseed(
                root_seed=request.seed,
                run_kind=request.run_kind,
                profile=request.regression_profile,
                case_id=suite_id,
                replication_index=0,
            )
            try:
                suite_result = self.regression_runner.run(
                    RegressionRunRequest(
                        suite_artifact_id=suite_id,
                        snapshot_id=snapshot.snapshot_id,
                        seed=execution_seed,
                        snapshot=snapshot,
                        root_seed=request.seed,
                        run_kind=request.run_kind,
                        profile=request.regression_profile,
                        max_action_work_units=budget.regression_work_units,
                    )
                )
            except IntegrityViolation:
                # Retained Artifact/registry corruption is not an oracle verdict.
                # Preserve the typed terminal integrity failure for worker
                # classification instead of laundering it into repair_unverified.
                raise
            except Exception:  # noqa: BLE001 — an unavailable suite proves nothing
                return result(False)
            action_work_units = suite_result.action_work_units
            if (
                isinstance(action_work_units, bool)
                or not isinstance(action_work_units, int)
                or not 0 <= action_work_units <= budget.regression_work_units
            ):
                return result(False)
            budget.regression_work_units -= action_work_units
            payload = self._regression_payload(
                request=request,
                requirement_id=requirement.requirement_id,
                suite_id=suite_id,
                snapshot_id=snapshot.snapshot_id,
                execution_seed=execution_seed,
                suite_result=suite_result,
            )
            if payload is None:
                return result(False)
            env_contract_version = suite_result.env_contract_version
            if env_contract_version is not None and (
                not isinstance(env_contract_version, str)
                or not 1 <= len(env_contract_version) <= 512
            ):
                return result(False)
            regression_statuses.append((suite_id, suite_result.status))
            regression_findings.append(
                (
                    requirement.requirement_id,
                    tuple(Finding.model_validate(item) for item in payload.get("findings", ())),
                )
            )
            regression_evidence.append(
                PreparedEvidenceV1(
                    outcome_rule_id="regression",
                    requirement_id=requirement.requirement_id,
                    payload=payload,
                    env_contract_version=env_contract_version,
                )
            )
            if suite_result.status in {"unproven", "not_executed"}:
                return result(False)
        return result(True)

    @staticmethod
    def _regression_payload(
        *,
        request: RepairRunRequest,
        requirement_id: str,
        suite_id: str,
        snapshot_id: str,
        execution_seed: int,
        suite_result,
    ) -> dict[str, object] | None:
        payload = dict(suite_result.payload)
        allowed_fields = {
            "payload_schema_version",
            "suite_artifact_id",
            "snapshot_id",
            "status",
            "reason_code",
            "seed",
            "findings",
            "case_seed_manifest",
        }
        unavailable = suite_result.status in {"unproven", "not_executed"}
        raw_findings = payload.get("findings", ())
        try:
            findings = tuple(Finding.model_validate(item) for item in raw_findings)
        except (TypeError, ValueError):
            return None
        raw_case_seed_manifest = payload.get("case_seed_manifest")
        try:
            case_seed_manifest = (
                None
                if raw_case_seed_manifest is None
                else RegressionCaseSeedManifestV1.model_validate(raw_case_seed_manifest)
            )
        except (TypeError, ValueError):
            return None
        if (
            set(payload) - allowed_fields
            or suite_result.suite_artifact_id != suite_id
            or payload.get("payload_schema_version") != "regression-evidence@1"
            or payload.get("suite_artifact_id") != suite_id
            or payload.get("snapshot_id") != snapshot_id
            or payload.get("status") != suite_result.status
            or ("seed" in payload and payload.get("seed") != execution_seed)
            or any(finding.snapshot_id != snapshot_id for finding in findings)
            or (suite_result.status == "passed" and findings)
            or unavailable != bool(suite_result.reason_code)
            or payload.get("reason_code") != suite_result.reason_code
            or (
                case_seed_manifest is not None
                and (
                    request.seed is None
                    or case_seed_manifest.suite_artifact_id != suite_id
                    or case_seed_manifest.root_seed != request.seed
                    or case_seed_manifest.run_kind != request.run_kind
                    or case_seed_manifest.profile != request.regression_profile
                )
            )
        ):
            return None
        body: dict[str, object] = {
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": requirement_id,
            "suite_artifact_id": suite_id,
            "snapshot_id": snapshot_id,
            "status": suite_result.status,
            "reason_code": suite_result.reason_code,
        }
        if "findings" in payload:
            body["findings"] = [finding.model_dump(mode="json") for finding in findings]
        if case_seed_manifest is not None:
            body["case_seed_manifest"] = case_seed_manifest.model_dump(mode="json")
        return with_validation_child_seed_evidence(
            body,
            root_seed=request.seed,
            execution_seed=execution_seed,
            run_kind=request.run_kind,
            profile=request.regression_profile,
            case_id=suite_id,
        )

    @staticmethod
    def _finding_keys(
        groups: tuple[tuple[str, tuple[Finding, ...]], ...],
        *,
        key: Callable[[Finding], str],
    ) -> dict[str, set[str]]:
        return {
            requirement_id: {key(finding) for finding in findings}
            for requirement_id, findings in groups
        }

    @classmethod
    def _target_has_reproduction_authority(
        cls,
        baseline: _ExactRepairEvaluation,
        finding: Finding,
    ) -> bool:
        key = _target_finding_key(finding)
        if finding.source == "checker" and finding.oracle_type != "simulation":
            return any(
                _target_finding_key(item) == key
                for _requirement_id, findings in baseline.checker_findings
                for item in findings
            )
        if finding.source == "sim" or finding.oracle_type == "simulation":
            return any(
                _target_finding_key(item) == key
                for _requirement_id, findings in baseline.simulation_findings
                for item in findings
            )
        # Non-checker/simulation targets need the same predicate reproduced by
        # one exact admitted regression suite. A generic suite failure/status is
        # neither target identity nor authority to spend bounded drafting steps.
        return any(
            _target_finding_key(item) == key
            for _requirement_id, findings in baseline.regression_findings
            for item in findings
        )

    @classmethod
    def _target_resolved(
        cls,
        baseline: _ExactRepairEvaluation,
        candidate: _ExactRepairEvaluation,
        finding: Finding,
    ) -> bool:
        key = _target_finding_key(finding)
        if finding.source == "checker" and finding.oracle_type != "simulation":
            baseline_requirements = {
                requirement_id
                for requirement_id, findings in baseline.checker_findings
                if any(_target_finding_key(item) == key for item in findings)
            }
            return bool(baseline_requirements) and not any(
                _target_finding_key(item) == key
                for requirement_id, findings in candidate.checker_findings
                if requirement_id in baseline_requirements
                for item in findings
            )
        if finding.source == "sim" or finding.oracle_type == "simulation":
            baseline_requirements = {
                requirement_id
                for requirement_id, findings in baseline.simulation_findings
                if any(_target_finding_key(item) == key for item in findings)
            }
            return bool(baseline_requirements) and not any(
                _target_finding_key(item) == key
                for requirement_id, findings in candidate.simulation_findings
                if requirement_id in baseline_requirements
                for item in findings
            )
        baseline_requirements = {
            requirement_id
            for requirement_id, findings in baseline.regression_findings
            if any(_target_finding_key(item) == key for item in findings)
        }
        return (
            bool(baseline_requirements)
            and bool(candidate.regression_statuses)
            and all(status == "passed" for _suite_id, status in candidate.regression_statuses)
            and not any(
                _target_finding_key(item) == key
                for requirement_id, findings in candidate.regression_findings
                if requirement_id in baseline_requirements
                for item in findings
            )
        )

    @classmethod
    def _verdict(
        cls,
        failed_preview: Snapshot,
        baseline: _ExactRepairEvaluation,
        candidate: _ExactRepairEvaluation,
        finding: Finding,
    ) -> VerifyResult:
        if not candidate.complete:
            return VerifyResult(
                ok=False,
                target_resolved=False,
                new_deterministic=[],
                regression_ok=False,
                detail="exact verifier requirement could not execute",
            )
        base_checker = cls._finding_keys(
            baseline.checker_findings,
            key=_exact_finding_key,
        )
        new_deterministic = [
            item
            for requirement_id, findings in candidate.checker_findings
            for item in findings
            if _exact_finding_key(item) not in base_checker.get(requirement_id, set())
        ]
        # Simulation observations are expected to move after a repair.  Their
        # stable violated predicate/locator is authoritative for set-diff; raw
        # observed/threshold evidence is not.  This still distinguishes another
        # invariant, relation, entity, constraint, or case under the same profile.
        base_simulation = cls._finding_keys(
            baseline.simulation_findings,
            key=_target_finding_key,
        )
        introduced_simulation = [
            item
            for requirement_id, findings in candidate.simulation_findings
            for item in findings
            if _target_finding_key(item) not in base_simulation.get(requirement_id, set())
        ]
        base_regression = cls._finding_keys(
            baseline.regression_findings,
            key=_target_finding_key,
        )
        introduced_regression = [
            item
            for requirement_id, findings in candidate.regression_findings
            for item in findings
            if _target_finding_key(item) not in base_regression.get(requirement_id, set())
        ]
        base_invariants = dict(baseline.failed_simulation_invariants)
        introduced_invariants = {
            requirement_id: tuple(sorted(set(names) - set(base_invariants.get(requirement_id, ()))))
            for requirement_id, names in candidate.failed_simulation_invariants
            if set(names) - set(base_invariants.get(requirement_id, ()))
        }
        regression_ok = (
            not introduced_simulation
            and not introduced_regression
            and not introduced_invariants
            and all(status == "passed" for _suite_id, status in candidate.regression_statuses)
        )
        target_resolved = cls._target_resolved(baseline, candidate, finding)
        deleted_subjects = {
            entity_id
            for entity_id in finding.entities
            if entity_id in failed_preview.entities and entity_id not in candidate.entity_ids
        }
        detail_parts: list[str] = []
        if not target_resolved:
            detail_parts.append(f"target {finding.defect_class!r} still present")
        if new_deterministic:
            detail_parts.append("introduced new exact checker findings")
        if introduced_simulation or introduced_regression or introduced_invariants:
            detail_parts.append("introduced exact simulation regression")
        if not all(status == "passed" for _suite_id, status in candidate.regression_statuses):
            detail_parts.append("exact regression suite did not pass")
        if deleted_subjects:
            target_resolved = False
            detail_parts.append("target subject was deleted")
        ok = target_resolved and not new_deterministic and regression_ok
        return VerifyResult(
            ok=ok,
            target_resolved=target_resolved,
            new_deterministic=new_deterministic,
            regression_ok=regression_ok,
            detail="; ".join(detail_parts) or "exact verifier closure passed",
            regression_ran=bool(candidate.regression_statuses),
            economy_ran=bool(candidate.simulation_findings),
        )

    @staticmethod
    def _unverified(
        search_steps: int,
        reason: str,
        evidence: _ExactRepairEvaluation | None = None,
    ) -> RepairSearchOutcomeV1:
        exact = evidence or _ExactRepairEvaluation(complete=False)
        return RepairSearchOutcomeV1(
            passed_verification=False,
            search_steps=search_steps,
            failure_reason=reason,
            checker_evidence=exact.checker_evidence,
            simulation_evidence=exact.simulation_evidence,
            regression_evidence=exact.regression_evidence,
        )


@dataclass(frozen=True, slots=True)
class M2ConstraintProposalAgentRunner:
    """Drive ``ExtractionProposer`` (LLM propose + compile oracle)."""

    def run(self, request: ConstraintProposalRunRequest) -> ConstraintProposalOutcomeV1:
        result = ExtractionProposer().run(request.doc, request.router)
        if result.fallback_taken:
            raise ValueError("constraint extraction did not produce a parseable response")
        raw = result.produced.get("proposals", [])
        proposals = tuple(ConstraintProposal(**item) for item in raw if isinstance(item, dict))
        dropped = int(result.produced.get("dropped", 0))
        if not proposals:
            raise ValueError("constraint extraction produced no compile-valid proposals")
        return ConstraintProposalOutcomeV1(proposals=proposals, dropped=dropped)


__all__ = [
    "M2ConstraintProposalAgentRunner",
    "M2GenerationAgentRunner",
    "M2RepairAgentRunner",
]
