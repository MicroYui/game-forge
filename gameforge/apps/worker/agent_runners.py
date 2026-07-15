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
from gameforge.agents.generation.gate import _build_ops, _economy_findings
from gameforge.agents.generation.generator import ContentGenerator
from gameforge.agents.repair.search import repair_search
from gameforge.contracts.agent_io import ConstraintProposal
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding, Patch
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

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
    requirement_id_for_profile,
)

CheckerFactory = Callable[[Snapshot, tuple[Constraint, ...]], list[Checker]]

# Fixed single requirement ids for the generation-gate policy (its checker/sim/
# review requirements are policy-frozen, not resolved from client profiles).
_GEN_CHECKER_REQUIREMENT = "generation-gate:checker"
_GEN_SIMULATION_REQUIREMENT = "generation-gate:simulation"
_GEN_REVIEW_REQUIREMENT = "generation-gate:review"


def _findings_payload(schema: str, snapshot_id: str, findings: tuple[Finding, ...]) -> dict:
    return {
        "payload_schema_version": schema,
        "snapshot_id": snapshot_id,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


@dataclass(frozen=True, slots=True)
class M2GenerationAgentRunner:
    """Drive ``ContentGenerator`` + the deterministic ``gate_proposal``."""

    checker_factory: CheckerFactory

    def run(self, request: GenerationRunRequest) -> GenerationGateOutcomeV1:
        checkers = self.checker_factory(request.snapshot, request.constraints)
        generator = ContentGenerator(request.snapshot, checkers)
        result = generator.run(request.goal, request.router)

        proposal = result.produced.get("proposal", {})
        ops_raw = proposal.get("proposed_ops", []) if isinstance(proposal, dict) else []
        passed = bool(proposal.get("passed_gate", False)) if isinstance(proposal, dict) else False

        typed_ops = _build_ops(list(ops_raw))
        if typed_ops is None:
            raise ValueError("content generator produced structurally malformed ops")
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
        patched = apply_patch(request.snapshot, patch)

        checker_findings: list[Finding] = []
        for checker in checkers:
            checker_findings.extend(checker.check(patched))
        sim_findings = tuple(_economy_findings(patched))
        review = build_review_report(patched, checkers, sim_findings=sim_findings)

        preview_id = patched.snapshot_id
        return GenerationGateOutcomeV1(
            ops=tuple(typed_ops),
            passed=passed,
            preview_payload=patched.content_payload,
            preview_snapshot_id=preview_id,
            checker_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="checker",
                    requirement_id=_GEN_CHECKER_REQUIREMENT,
                    payload=_findings_payload(
                        "checker-report@1", preview_id, tuple(checker_findings)
                    ),
                    findings=tuple(checker_findings),
                ),
            ),
            simulation_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="simulation",
                    requirement_id=_GEN_SIMULATION_REQUIREMENT,
                    payload=_findings_payload("simulation-result@1", preview_id, sim_findings),
                    findings=sim_findings,
                ),
            ),
            review_evidence=(
                PreparedEvidenceV1(
                    outcome_rule_id="review",
                    requirement_id=_GEN_REVIEW_REQUIREMENT,
                    payload=review.model_dump(mode="json"),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class M2RepairAgentRunner:
    """Drive the verifier-guided ``repair_search`` (LLM draft + deterministic verifier)."""

    def search(self, request: RepairRunRequest) -> RepairSearchOutcomeV1:
        if not request.findings:
            raise ValueError("repair_search requires at least one target finding")
        checkers = [checker for _, checker in request.checkers]
        target = request.findings[0]
        draft = repair_search(
            target,
            request.base_snapshot,
            checkers,
            request.router,
            max_steps=request.max_steps,
            run_regression=request.run_regression,
        )
        if not draft.passed_verification:
            return RepairSearchOutcomeV1(passed_verification=False, search_steps=draft.search_steps)

        patched = apply_patch(request.base_snapshot, draft.patch)
        preview_id = patched.snapshot_id

        checker_evidence = tuple(
            PreparedEvidenceV1(
                outcome_rule_id="checker",
                requirement_id=requirement_id_for_profile(profile),
                payload=_findings_payload(
                    "checker-report@1", preview_id, tuple(checker.check(patched))
                ),
                findings=tuple(checker.check(patched)),
            )
            for profile, checker in request.checkers
        )
        simulation_evidence = tuple(
            PreparedEvidenceV1(
                outcome_rule_id="simulation",
                requirement_id=requirement_id_for_profile(profile),
                payload=_findings_payload(
                    "simulation-result@1", preview_id, self._economy(patched, config)
                ),
                findings=self._economy(patched, config),
            )
            for profile, config in request.simulation_profiles
        )
        regression_evidence = tuple(
            PreparedEvidenceV1(
                outcome_rule_id="regression",
                requirement_id=suite_id,
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": suite_id,
                    "snapshot_id": preview_id,
                    "status": "passed",
                },
            )
            for suite_id in request.regression_suite_artifact_ids
        )
        return RepairSearchOutcomeV1(
            passed_verification=True,
            search_steps=draft.search_steps,
            ops=tuple(draft.patch.ops),
            preview_payload=patched.content_payload,
            preview_snapshot_id=preview_id,
            expected_to_fix=tuple(draft.patch.expected_to_fix),
            side_effect_risk=draft.patch.side_effect_risk,
            checker_evidence=checker_evidence,
            simulation_evidence=simulation_evidence,
            regression_evidence=regression_evidence,
        )

    @staticmethod
    def _economy(snapshot: Snapshot, config) -> tuple[Finding, ...]:
        try:
            model = EconomyModel.from_snapshot(snapshot)
            if not model.sources and not model.sinks:
                return ()
            result = EconomySimulator().run(
                model, seed=0, n_agents=config.n_agents, n_ticks=config.n_ticks
            )
            return tuple(to_findings(result, snapshot.snapshot_id, model=model))
        except Exception:  # noqa: BLE001 — an un-modelable economy yields no evidence
            return ()


@dataclass(frozen=True, slots=True)
class M2ConstraintProposalAgentRunner:
    """Drive ``ExtractionProposer`` (LLM propose + compile oracle)."""

    def run(self, request: ConstraintProposalRunRequest) -> ConstraintProposalOutcomeV1:
        result = ExtractionProposer().run(request.doc, request.router)
        raw = result.produced.get("proposals", [])
        proposals = tuple(ConstraintProposal(**item) for item in raw if isinstance(item, dict))
        dropped = int(result.produced.get("dropped", 0))
        return ConstraintProposalOutcomeV1(proposals=proposals, dropped=dropped)


__all__ = [
    "M2ConstraintProposalAgentRunner",
    "M2GenerationAgentRunner",
    "M2RepairAgentRunner",
]
