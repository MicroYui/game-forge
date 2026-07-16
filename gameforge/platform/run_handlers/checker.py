"""``checker_runner@1`` — the deterministic structural/numeric checker handler.

Thin adapter over ``gameforge.spine.checkers``: it loads the input IR snapshot,
selects the requested checkers by id (``graph`` / ``asp`` / ``smt``), runs each,
and seals the concatenated ``Finding``s into a single primary
``checker_run[checker-report@1]`` plus one ``PreparedFinding`` per finding under
the frozen ``checker-findings`` policy. ASP/SMT budget degradation stays
``status="unproven"`` (fail-closed) exactly as the spine checkers emit it — this
handler never re-classifies a verdict.

``outcome_code=checker_completed``; LLM execution mode is ``not_applicable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    GraphSelectionV1,
    PreparedRunOutcome,
)
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.checkers.base import Checker
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    FindingHeadRevisionResolver,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    prepared_version_tuple,
    rebind_finding_producers,
    scoped_finding_series_id,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    NavLoader,
    SnapshotLoader,
    load_constraints,
    load_nav,
    load_snapshot,
)

CHECKER_TOOL_VERSION = "checker@1"
CHECKER_REPORT_SCHEMA_ID = "checker-report@1"
_REACHABLE_IN_CALL = re.compile(r"\breachable_in\s*\(", re.IGNORECASE)
_DEFAULT_CHECKER_IDS = ("asp", "graph", "smt")
_DEFAULT_DEFECT_CLASSES = (
    "cyclic_dependency",
    "dangling_reference",
    "dead_quest",
    "gacha_expectation_violation",
    "interval_violation",
    "isolated_node",
    "missing_drop_source",
    "non_monotonic_curve",
    "prob_sum_ne_1",
    "reward_out_of_range",
    "unreachable_target",
    "unsatisfiable_completion",
)


@dataclass(frozen=True, slots=True)
class CheckerExecutionPolicy:
    """Exact profile-owned taxonomy and pre-execution work envelope."""

    allowed_checker_ids: tuple[str, ...]
    allowed_defect_classes: tuple[str, ...]
    max_direct_checker_count: int
    max_constraint_count: int
    max_work_units: int


class CheckerExecutionPolicyResolver(Protocol):
    def __call__(self, profile: ProfileRefV1) -> CheckerExecutionPolicy: ...


def default_checker_execution_policy(_profile: ProfileRefV1) -> CheckerExecutionPolicy:
    """Unit/default wiring matching the frozen built-in checker profile."""

    return CheckerExecutionPolicy(
        allowed_checker_ids=_DEFAULT_CHECKER_IDS,
        allowed_defect_classes=_DEFAULT_DEFECT_CLASSES,
        max_direct_checker_count=3,
        max_constraint_count=256,
        max_work_units=2_000_000,
    )


def validate_checker_execution_policy(
    *,
    checker_ids: tuple[str, ...],
    defect_classes: tuple[str, ...],
    constraint_count: int,
    snapshot: Snapshot,
    policy: CheckerExecutionPolicy,
) -> int:
    """Validate exact taxonomy/count/work and return conservative work units."""

    unknown_checkers = tuple(
        checker_id for checker_id in checker_ids if checker_id not in policy.allowed_checker_ids
    )
    unknown_classes = tuple(
        defect_class
        for defect_class in defect_classes
        if defect_class not in policy.allowed_defect_classes
    )
    if unknown_checkers or unknown_classes:
        raise IntegrityViolation(
            "checker request is outside the exact profile taxonomy",
            checker_ids=unknown_checkers,
            defect_classes=unknown_classes,
        )
    if (
        len(checker_ids) > policy.max_direct_checker_count
        or constraint_count > policy.max_constraint_count
    ):
        raise IntegrityViolation("checker request exceeds the exact profile count budget")

    return validate_checker_work_budget(
        snapshot=snapshot,
        execution_count=len(checker_ids) + constraint_count,
        max_work_units=policy.max_work_units,
    )


def validate_checker_work_budget(
    *,
    snapshot: Snapshot,
    execution_count: int,
    max_work_units: int,
) -> int:
    """Apply the shared conservative checker-work formula before compilation."""

    if execution_count < 0 or max_work_units < 1:
        raise IntegrityViolation("checker work-budget authority is invalid")
    node_count = len(snapshot.entities)
    relation_count = len(snapshot.relations)
    per_execution = max(1, node_count * node_count + node_count + relation_count)
    work_units = per_execution * execution_count
    if work_units > max_work_units:
        raise IntegrityViolation("checker request exceeds its frozen work budget")
    return work_units


class CheckerFactory(Protocol):
    """Build one direct spine backend probe for a checker id."""

    def build(self, checker_id: str, *, constraints: list[Constraint]) -> Checker: ...


class DefaultCheckerFactory:
    """The production checker factory (``graph``/``asp``/``smt``).

    ``clingo`` (ASP) and ``z3`` (SMT) are imported lazily so a checker run that
    only selects ``graph`` never pays for the solver backends.
    """

    def build(self, checker_id: str, *, constraints: list[Constraint]) -> Checker:
        del constraints  # exact DSL constraints are routed once via compile_all
        if checker_id == "graph":
            from gameforge.spine.checkers.graph import GraphChecker

            return GraphChecker()
        if checker_id == "asp":
            from gameforge.spine.checkers.asp import ASPChecker

            return ASPChecker()
        if checker_id == "smt":
            # Unlike graph/ASP, SMT has no schema-independent assertion set.
            # Returning ``SMTChecker([])`` would turn a selected backend into a
            # silent success. The handler treats ``smt`` as an explicit request
            # for exact numeric-constraint work and executes those constraints
            # once through ``compile_all`` instead.
            raise IntegrityViolation("direct SMT has no schema-independent executable assertions")
        raise ValueError(f"unknown checker id {checker_id!r}")


@dataclass(frozen=True, slots=True)
class CheckerRunHandler:
    """A ``RunExecutor`` producing the primary checker report + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    checker_factory: CheckerFactory = field(default_factory=DefaultCheckerFactory)
    execution_policy_resolver: CheckerExecutionPolicyResolver = default_checker_execution_policy
    finding_head_revision: FindingHeadRevisionResolver | None = None
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    nav_loader: NavLoader = load_nav

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, CheckerRunPayloadV1):
            raise TypeError("checker_runner@1 requires a checker-run@1 payload")

        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        constraints = self._constraints(payload)
        if not payload.checker_ids and not constraints:
            raise IntegrityViolation(
                "checker.run has no direct backend or compiled constraint work"
            )
        self._validate_execution_policy(payload, snapshot, constraints)
        self._validate_smt_selection(payload, constraints)
        nav = self.nav_loader(self.blobs, payload.snapshot_artifact_id)

        findings = self._run_checkers(payload, snapshot, constraints, nav)
        compiled_findings, constraint_application = self._run_compiled_constraints(
            snapshot,
            constraints,
            nav,
        )
        findings.extend(compiled_findings)
        if "graph" in payload.checker_ids:
            findings.extend(navigation_unproven_findings(snapshot, nav))
        findings = filter_findings_by_selection(findings, payload.selection, snapshot)
        findings = _filter_defect_classes(findings, payload.defect_classes)
        findings = rebind_finding_producers(findings, run_id=context.run.run_id)

        # Validate the global finding-count bound and resolve every series-head
        # CAS before writing the primary blob. A malicious/custom backend cannot
        # leave an orphaned object merely by returning >10k findings.
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(finding=finding, evidence_artifact_index=0) for finding in findings
            ),
            run_id=context.run.run_id,
            head_revision_resolver=self.finding_head_revision,
        )

        lineage = _snapshot_lineage(payload)
        primary = store_prepared_artifact(
            self.store,
            kind="checker_run",
            payload_schema_id=CHECKER_REPORT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=CHECKER_TOOL_VERSION,
                projected_fields=("constraint_snapshot_id",),
                overrides={"ir_snapshot_id": snapshot.snapshot_id},
            ),
            lineage=lineage,
            payload=_checker_report_payload(
                payload,
                snapshot,
                findings,
                constraint_application=constraint_application,
            ),
        )

        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="checker_completed",
            primary_index=0,
            artifacts=(primary,),
            findings=prepared_findings,
        )

    def _constraints(self, payload: CheckerRunPayloadV1) -> list[Constraint]:
        if payload.constraint_snapshot_artifact_id is None:
            return []
        return self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id)

    def _run_checkers(
        self,
        payload: CheckerRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
        nav: NavProvider | None,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for checker_id in payload.checker_ids:
            if checker_id == "smt":
                # Exact numeric constraints are executed once below by
                # ``_run_compiled_constraints``. Never manufacture a second,
                # empty direct SMT invocation.
                continue
            checker = self.checker_factory.build(checker_id, constraints=constraints)
            findings.extend(checker.check(snapshot, nav=nav))
        return findings

    @staticmethod
    def _validate_smt_selection(
        payload: CheckerRunPayloadV1,
        constraints: list[Constraint],
    ) -> None:
        if "smt" not in payload.checker_ids:
            return
        executable_numeric = tuple(
            constraint.id
            for constraint in constraints
            if constraint.kind == "numeric" and not constraint.has_llm_predicate()
        )
        if not executable_numeric:
            raise IntegrityViolation("SMT selection has no exact executable numeric constraint")

    def _validate_execution_policy(
        self,
        payload: CheckerRunPayloadV1,
        snapshot: Snapshot,
        constraints: list[Constraint],
    ) -> None:
        policy = self.execution_policy_resolver(payload.checker_profile)
        validate_checker_execution_policy(
            checker_ids=payload.checker_ids,
            defect_classes=payload.defect_classes,
            constraint_count=len(constraints),
            snapshot=snapshot,
            policy=policy,
        )

    @staticmethod
    def _run_compiled_constraints(
        snapshot: Snapshot,
        constraints: list[Constraint],
        nav: NavProvider | None,
    ) -> tuple[list[Finding], tuple[dict[str, str], ...]]:
        """Compile and execute the exact constraint snapshot independently.

        ``checker_ids`` selects direct backend probes.  The optional constraint
        snapshot is a second exact input axis whose canonical backend routing is
        owned by M1 ``compile_all``; it may not be silently ignored merely
        because the caller did not guess its backend.  Standalone checker output
        cannot publish llm-assisted Findings under its frozen Finding policy, so
        such constraints fail closed instead of being mislabelled deterministic.
        """

        ordered = sorted(constraints, key=lambda item: item.id)
        ids = tuple(constraint.id for constraint in ordered)
        if len(ids) != len(set(ids)):
            raise IntegrityViolation("constraint snapshot repeats a constraint id")
        llm_assisted = tuple(
            constraint.id for constraint in ordered if constraint.has_llm_predicate()
        )
        if llm_assisted:
            raise IntegrityViolation(
                "checker.run cannot publish llm-assisted constraint verdicts",
                constraint_ids=llm_assisted,
            )

        compiled = compile_all(ordered)
        findings: list[Finding] = []
        applications: list[dict[str, str]] = []
        for constraint, checker in zip(ordered, compiled, strict=True):
            navigation_defect_class = _constraint_navigation_defect_class(constraint)
            if navigation_defect_class is not None and nav is None:
                applications.append(
                    {
                        "constraint_id": constraint.id,
                        "checker_id": "graph",
                        "status": "unproven",
                        "reason_code": "navigation_ground_truth_unavailable",
                    }
                )
                findings.append(_constraint_navigation_unproven_finding(snapshot, constraint))
                continue
            backend = getattr(checker, "backend", None)
            backend_id = getattr(backend, "id", None)
            if navigation_defect_class is not None:
                from gameforge.spine.checkers.graph import GraphChecker

                checker = GraphChecker()
                backend_id = "graph"
            if backend_id not in {"graph", "asp", "smt"}:
                raise IntegrityViolation(
                    "compiled constraint did not resolve a deterministic backend",
                    constraint_id=constraint.id,
                )
            applications.append(
                {
                    "constraint_id": constraint.id,
                    "checker_id": backend_id,
                    "status": "executed",
                }
            )
            executed_findings = checker.check(snapshot, nav=nav)
            if navigation_defect_class is not None:
                executed_findings = [
                    finding
                    for finding in executed_findings
                    if finding.defect_class == navigation_defect_class
                ]
            findings.extend(
                finding.model_copy(
                    update={
                        "id": scoped_finding_series_id(
                            namespace="constraint",
                            scope_id=constraint.id,
                            finding_id=finding.id,
                        ),
                        "constraint_id": constraint.id,
                    }
                )
                for finding in executed_findings
            )
        return findings, tuple(applications)


def _constraint_navigation_defect_class(constraint: Constraint) -> str | None:
    """Return the exact Graph predicate that needs navigation authority.

    Only the frozen DSL function call is navigation-dependent here.  Words such
    as ``graph`` or ``reachable`` in prose are deliberately not enough to turn
    an unrelated structural constraint into an unproven verdict.
    """

    expressions = (constraint.assert_, *(predicate.expr for predicate in constraint.predicates))
    if not any(_REACHABLE_IN_CALL.search(expression) for expression in expressions):
        return None
    joined = " ".join(expressions).lower()
    if any(token in joined for token in ("collect", "drop", "source")):
        return "missing_drop_source"
    return "unreachable_target"


def _constraint_navigation_unproven_finding(
    snapshot: Snapshot,
    constraint: Constraint,
) -> Finding:
    run_id = f"constraint-nav@{snapshot.snapshot_id[:19]}"
    return Finding(
        id=scoped_finding_series_id(
            namespace="constraint",
            scope_id=constraint.id,
            finding_id=f"{run_id}#unproven",
        ),
        source="checker",
        producer_id="graph",
        producer_run_id=run_id,
        oracle_type="deterministic",
        defect_class=_constraint_navigation_defect_class(constraint) or "unreachable_target",
        severity=constraint.severity,
        snapshot_id=snapshot.snapshot_id,
        constraint_id=constraint.id,
        evidence={
            "reason_code": "navigation_ground_truth_unavailable",
            "predicate": constraint.assert_,
        },
        minimal_repro={"constraint_id": constraint.id},
        status="unproven",
        message=(
            f"constraint {constraint.id!r} requires navigation ground truth; "
            "the exact input snapshot carries no navigation authority"
        ),
    )


def _filter_defect_classes(
    findings: list[Finding], defect_classes: tuple[str, ...]
) -> list[Finding]:
    if not defect_classes:
        return findings
    allowed = set(defect_classes)
    return [finding for finding in findings if finding.defect_class in allowed]


def navigation_unproven_findings(
    snapshot: Snapshot,
    nav: NavProvider | None,
) -> list[Finding]:
    """Represent omitted spatial proofs as unproven, never as a silent pass."""

    if nav is not None:
        return []
    graph = snapshot.to_graph()
    obligations: list[tuple[str, list[str], list[str], dict[str, object], dict[str, object]]] = []
    for quest in sorted(graph.nodes_of_type(NodeType.QUEST), key=lambda item: item.id):
        giver_relations = sorted(
            graph.neighbors(quest.id, EdgeType.STARTS_AT, direction="out"),
            key=lambda item: item.id,
        )
        if not giver_relations:
            continue
        giver_relation = giver_relations[0]
        giver = giver_relation.dst_id
        for step_relation in sorted(
            graph.neighbors(quest.id, EdgeType.HAS_STEP, direction="out"),
            key=lambda item: item.id,
        ):
            step = graph.get_node(step_relation.dst_id)
            if step is None or step.attrs.get("kind") not in {"talk", "turn_in"}:
                continue
            target = step.attrs.get("target")
            if not isinstance(target, str) or not target:
                continue
            obligations.append(
                (
                    "unreachable_target",
                    [quest.id, step.id, giver, target],
                    [giver_relation.id, step_relation.id],
                    {
                        "reason_code": "navigation_ground_truth_unavailable",
                        "quest": quest.id,
                        "step": step.id,
                        "giver": giver,
                        "target": target,
                    },
                    {
                        "entity": step.id,
                        "source_ref": (step.source_ref.model_dump() if step.source_ref else None),
                    },
                )
            )
    for step in sorted(graph.nodes_of_type(NodeType.QUEST_STEP), key=lambda item: item.id):
        if step.attrs.get("kind") != "collect":
            continue
        item = step.attrs.get("item")
        if not isinstance(item, str) or not item:
            continue
        source_relations = sorted(
            (
                relation
                for relation in graph.all_relations()
                if relation.type in {EdgeType.GRANTS, EdgeType.DROPS_FROM}
                and relation.dst_id == item
            ),
            key=lambda relation: relation.id,
        )
        if not source_relations:
            continue
        sources = sorted({relation.src_id for relation in source_relations})
        obligations.append(
            (
                "missing_drop_source",
                [step.id, item, *sources],
                [relation.id for relation in source_relations],
                {
                    "reason_code": "navigation_ground_truth_unavailable",
                    "step": step.id,
                    "item": item,
                    "known_sources": sources,
                },
                {
                    "entity": step.id,
                    "source_ref": step.source_ref.model_dump() if step.source_ref else None,
                },
            )
        )
    run_id = f"graph-nav@{snapshot.snapshot_id[:19]}"
    return [
        Finding(
            id=f"{run_id}#{index}",
            source="checker",
            producer_id="graph",
            producer_run_id=run_id,
            oracle_type="deterministic",
            defect_class=defect_class,
            severity="critical",
            snapshot_id=snapshot.snapshot_id,
            entities=entities,
            relations=relations,
            evidence=evidence,
            minimal_repro=minimal_repro,
            status="unproven",
            message=(
                f"{defect_class} requires navigation ground truth; the exact input "
                "snapshot carries no navigation authority"
            ),
        )
        for index, (
            defect_class,
            entities,
            relations,
            evidence,
            minimal_repro,
        ) in enumerate(obligations)
    ]


def filter_findings_by_selection(
    findings: list[Finding],
    selection: GraphSelectionV1,
    snapshot: Snapshot,
) -> list[Finding]:
    """Apply an exact Run graph selection without inventing a partial graph.

    Checkers still evaluate the complete immutable snapshot so omitted neighbours
    cannot create artificial dangling/reachability defects. ``ids`` mode then
    retains only findings that touch an explicitly selected entity/relation (or an
    endpoint of a selected relation). Unknown ids fail closed.
    """

    if selection.mode == "full":
        return findings
    missing_entities = tuple(
        entity_id for entity_id in selection.entity_ids if entity_id not in snapshot.entities
    )
    missing_relations = tuple(
        relation_id
        for relation_id in selection.relation_ids
        if relation_id not in snapshot.relations
    )
    if missing_entities or missing_relations:
        raise IntegrityViolation(
            "graph selection references ids absent from the exact snapshot",
            entity_ids=missing_entities,
            relation_ids=missing_relations,
        )
    selected_entities = set(selection.entity_ids)
    selected_relations = set(selection.relation_ids)
    for relation_id in selected_relations:
        relation = snapshot.relations[relation_id]
        selected_entities.update((relation.src_id, relation.dst_id))
    return [
        finding
        for finding in findings
        if (not finding.entities and not finding.relations)
        or selected_entities.intersection(finding.entities)
        or selected_relations.intersection(finding.relations)
    ]


def _snapshot_lineage(payload: CheckerRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    return tuple(lineage)


def _checker_report_payload(
    payload: CheckerRunPayloadV1,
    snapshot: Snapshot,
    findings: list[Finding],
    *,
    constraint_application: tuple[dict[str, str], ...],
) -> dict[str, object]:
    return {
        "payload_schema_version": CHECKER_REPORT_SCHEMA_ID,
        "snapshot_id": snapshot.snapshot_id,
        "checker_ids": list(payload.checker_ids),
        "defect_classes": list(payload.defect_classes),
        "constraint_application": list(constraint_application),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


__all__ = [
    "CHECKER_REPORT_SCHEMA_ID",
    "CheckerExecutionPolicy",
    "CheckerExecutionPolicyResolver",
    "CheckerFactory",
    "CheckerRunHandler",
    "DefaultCheckerFactory",
    "default_checker_execution_policy",
    "filter_findings_by_selection",
    "navigation_unproven_findings",
    "validate_checker_execution_policy",
    "validate_checker_work_budget",
]
