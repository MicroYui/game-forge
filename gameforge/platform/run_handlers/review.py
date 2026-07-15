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
from dataclasses import dataclass, field
from typing import Callable, Protocol

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import PreparedArtifact, PreparedRunOutcome, ReviewRunPayloadV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.review import ReviewReport
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.model_routing import (
    ModelBridgeAgentAdapter,
    plan_node_snapshot,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    NavLoader,
    SnapshotLoader,
    load_constraints,
    load_nav,
    load_snapshot,
)
from gameforge.platform.run_handlers.simulation import EconomySimulatorPort

REVIEW_SCHEMA_ID = "review@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"
TRIAGE_AGENT_NODE_ID = "review-triage"
TRIAGE_PROMPT_VERSION = "review-triage@1"


@dataclass(frozen=True, slots=True)
class ReviewSimConfig:
    """Bounded population/horizon for one review simulation profile."""

    n_agents: int
    n_ticks: int


CheckerResolver = Callable[[ProfileRefV1, list[Constraint]], Checker]
SimConfigResolver = Callable[[ProfileRefV1], ReviewSimConfig]


class _CheckerResolverProto(Protocol):
    def __call__(self, profile: ProfileRefV1, constraints: list[Constraint]) -> Checker: ...


@dataclass(frozen=True, slots=True)
class ReviewRunHandler:
    """A ``RunExecutor`` producing the review report + gate checker/sim + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_resolver: CheckerResolver
    sim_config_resolver: SimConfigResolver
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    nav_loader: NavLoader = load_nav
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ReviewRunPayloadV1):
            raise TypeError("review_runner@1 requires a review-run@1 payload")

        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        constraints = self._constraints(payload)
        nav = self.nav_loader(self.blobs, payload.snapshot_artifact_id)
        lineage = _snapshot_lineage(payload)

        checker_findings, checker_artifacts, checkers, checker_evidence = self._run_checkers(
            payload, snapshot, constraints, nav, lineage
        )
        sim_findings, sim_artifacts = self._run_simulations(payload, snapshot, context, lineage)

        # LLM triage is a suggestion-only annotation over the deterministic verdict.
        triage_applied = payload.llm_triage_policy is not None
        triage_findings = (
            self._run_triage(context, snapshot, checker_findings + sim_findings)
            if triage_applied
            else []
        )
        recorded_llm_mode = (
            context.payload.llm_execution_mode if triage_applied else "not_applicable"
        )

        all_findings = checker_findings + sim_findings + triage_findings
        # Wrap the spine partition (build_review_report runs any supplied checkers
        # then appends + partitions); the checkers already ran per profile, so the
        # authoritative report is the same partition applied to the collected set.
        report = build_review_report(snapshot, [], tuple(all_findings), nav)

        primary = _store_review_report(
            self.store,
            snapshot=snapshot,
            report=report,
            lineage=lineage,
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
            *(
                FindingEvidence(finding=finding, evidence_artifact_index=0)
                for finding in sim_findings
            ),
            *(
                FindingEvidence(finding=finding, evidence_artifact_index=0)
                for finding in triage_findings
            ),
        )
        prepared_findings = build_prepared_findings(evidence, run_id=context.run.run_id)
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

    def _run_checkers(
        self,
        payload: ReviewRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        nav: NavProvider | None,
        lineage: tuple[str, ...],
    ) -> tuple[
        list[Finding], tuple[PreparedArtifact, ...], list[Checker], tuple[FindingEvidence, ...]
    ]:
        findings: list[Finding] = []
        artifacts: list[PreparedArtifact] = []
        checkers: list[Checker] = []
        evidence: list[FindingEvidence] = []
        for profile in payload.checker_profiles:
            checker = self.checker_resolver(profile, constraints)
            checkers.append(checker)
            profile_findings = checker.check(snapshot, nav=nav)
            findings.extend(profile_findings)
            for finding in profile_findings:
                evidence.append(
                    FindingEvidence(
                        finding=finding,
                        evidence_artifact_index=0,
                        finding_id=f"{profile.profile_id}@{profile.version}:{finding.id}",
                    )
                )
            artifacts.append(
                store_prepared_artifact(
                    self.store,
                    kind="checker_run",
                    payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
                    version_tuple=VersionTuple(
                        ir_snapshot_id=snapshot.snapshot_id,
                        constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                        tool_version="checker@1",
                    ),
                    lineage=lineage,
                    payload=_profile_checker_payload(
                        profile, snapshot.snapshot_id, profile_findings
                    ),
                )
            )
        return findings, tuple(artifacts), checkers, tuple(evidence)

    def _run_simulations(
        self,
        payload: ReviewRunPayloadV1,
        snapshot: Snapshot,
        context: ExecutorContextLike,
        lineage: tuple[str, ...],
    ) -> tuple[list[Finding], tuple[PreparedArtifact, ...]]:
        seed = int(context.payload.seed) if context.payload.seed is not None else 0
        model = EconomyModel.from_snapshot(snapshot)
        findings: list[Finding] = []
        artifacts: list[PreparedArtifact] = []
        for profile in payload.simulation_profiles:
            config = self.sim_config_resolver(profile)
            result = self.simulator.run(
                model, seed=seed, n_agents=config.n_agents, n_ticks=config.n_ticks
            )
            profile_findings = to_findings(result, snapshot.snapshot_id, model)
            findings.extend(profile_findings)
            artifacts.append(
                store_prepared_artifact(
                    self.store,
                    kind="simulation_run",
                    payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
                    version_tuple=VersionTuple(
                        ir_snapshot_id=snapshot.snapshot_id,
                        constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                        tool_version="economy-sim@1",
                        seed=seed,
                    ),
                    lineage=lineage,
                    payload=_profile_sim_payload(
                        profile, snapshot.snapshot_id, seed, config, result, profile_findings
                    ),
                )
            )
        return findings, tuple(artifacts)

    def _run_triage(
        self,
        context: ExecutorContextLike,
        snapshot: Snapshot,
        deterministic_findings: list[Finding],
    ) -> list[Finding]:
        adapter = ModelBridgeAgentAdapter(
            model_bridge=context.model_bridge,
            idempotency_scope=context.run.idempotency_scope,
            idempotency_prefix=f"{context.run.run_id}:{context.attempt.attempt_no}",
            deadline_utc=context.deadline_utc,
        )
        model_snapshot = plan_node_snapshot(
            context.payload.execution_version_plan, TRIAGE_AGENT_NODE_ID
        )
        prompt = _triage_prompt(snapshot.snapshot_id, deterministic_findings)
        result = adapter.call_model(
            agent_node_id=TRIAGE_AGENT_NODE_ID,
            user_prompt=prompt,
            prompt_version=TRIAGE_PROMPT_VERSION,
            model_snapshot=model_snapshot,
            source_artifact_id=f"{context.run.run_id}:rendered:{TRIAGE_AGENT_NODE_ID}",
        )
        return _parse_triage_suggestions(result.response.response_normalized, snapshot.snapshot_id)


def _snapshot_lineage(payload: ReviewRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    return tuple(lineage)


def _profile_checker_payload(
    profile: ProfileRefV1, snapshot_id: str, findings: list[Finding]
) -> dict[str, object]:
    return {
        "payload_schema_version": CHECKER_REPORT_SCHEMA_ID,
        "profile": profile.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _profile_sim_payload(
    profile: ProfileRefV1,
    snapshot_id: str,
    seed: int,
    config: ReviewSimConfig,
    result,
    findings: list[Finding],
) -> dict[str, object]:
    return {
        "payload_schema_version": SIMULATION_RESULT_SCHEMA_ID,
        "profile": profile.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        "seed": seed,
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
        "sensitivity": result.sensitivity,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _store_review_report(
    store: PreparedArtifactStore,
    *,
    snapshot: Snapshot,
    report: ReviewReport,
    lineage: tuple[str, ...],
    recorded_llm_mode: str,
    triage_applied: bool,
) -> PreparedArtifact:
    return store_prepared_artifact(
        store,
        kind="review_report",
        payload_schema_id=REVIEW_SCHEMA_ID,
        version_tuple=VersionTuple(
            ir_snapshot_id=snapshot.snapshot_id,
            tool_version="review@1",
        ),
        lineage=lineage,
        payload=report.model_dump(mode="json"),
        extra_meta={
            "llm_execution_mode": recorded_llm_mode,
            "llm_triage_applied": triage_applied,
        },
    )


def _triage_prompt(snapshot_id: str, findings: list[Finding]) -> str:
    body = {
        "snapshot_id": snapshot_id,
        "deterministic_findings": [
            {
                "finding_id": finding.id,
                "defect_class": finding.defect_class,
                "severity": finding.severity,
                "message": finding.message,
            }
            for finding in findings
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


_VALID_SEVERITIES = {"critical", "major", "minor"}


def _parse_triage_suggestions(text: str, snapshot_id: str) -> list[Finding]:
    """Parse the model's triage output into llm-assisted SUGGESTION findings.

    Parse failure is a fallback signal (no suggestions), never a crash — matching
    the agent-layer convention. Every suggestion is ``status="unproven"`` so it can
    never be mistaken for a proven deterministic verdict.
    """

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    suggestions = parsed.get("suggestions") if isinstance(parsed, dict) else None
    if not isinstance(suggestions, list):
        return []
    findings: list[Finding] = []
    for index, item in enumerate(suggestions):
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        if severity not in _VALID_SEVERITIES:
            severity = "minor"
        message = item.get("message")
        if not isinstance(message, str) or not message:
            message = "LLM triage suggestion (advisory only, not a proven verdict)"
        entities = item.get("entities")
        findings.append(
            Finding(
                id=f"review-triage@{snapshot_id[:23]}#{index}",
                source="llm",
                producer_id="review_triage",
                producer_run_id=f"review-triage@{snapshot_id[:23]}",
                oracle_type="llm-assisted",
                defect_class=str(item.get("defect_class") or "llm_triage_suggestion"),
                severity=severity,
                snapshot_id=snapshot_id,
                entities=[str(entity) for entity in entities] if isinstance(entities, list) else [],
                status="unproven",
                message=message,
            )
        )
    return findings


__all__ = [
    "CheckerResolver",
    "REVIEW_SCHEMA_ID",
    "ReviewRunHandler",
    "ReviewSimConfig",
    "SimConfigResolver",
]
