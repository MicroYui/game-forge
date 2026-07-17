"""Concrete composition for the three M4c validation handlers (Task 13).

The platform validation handlers are game-agnostic and drive their solver /
analysis work through injected ports; this module (the ``apps`` composition
boundary, which may import ``spine``) supplies the concrete implementations wired
by the worker composition root.

The worker exposes two independent pairs: z3 + a small exact-rational reference
solver for numeric constraints, and Clingo/ASP + the hand-written GraphChecker for
structural constraints. Structural engines prove only frozen, explicitly supported
predicate semantics by running deterministic dirty/clean witnesses through their
own backend and comparing those results with the real ``compile_all`` output on the
same snapshots; prose and unsupported predicates never acquire coverage by keyword.
An engine whose domain does not apply reports
``not_applicable``; unsupported or budget-exhausted work is ``undecided``. Every
engine reports the exact constraint ids it positively decided so the handler can
require a two-engine quorum per applicable constraint. No LLM SDK is imported here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product

import z3

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    ValidationProfileDetailsV1,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.base import Checker, CheckerExecutionBinding
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.smt import (
    SmtCompileError,
    _BindCtx,
    _compile as _smt_compile,
)
from gameforge.spine.dsl.ast import (
    AssertNode,
    BinOp,
    BoolOp,
    Call,
    Compare,
    Const,
    DslError,
    Field,
    UnaryOp,
    parse_assert,
)
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot

from gameforge.platform.run_handlers.constraint_validation import (
    BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1,
    ConstraintDifferentialEngine,
    ConstraintValidationProfileAuthorityV1,
    DifferentialEngineResultV1,
    DifferentialEvalRequest,
)
from gameforge.platform.registry import ImmutablePlatformRegistry


@dataclass(frozen=True, slots=True)
class RegistryConstraintValidationProfileResolver:
    """Authorize compiler/differential adapters from exact retained bindings."""

    registry: ImmutablePlatformRegistry

    def resolve(
        self,
        *,
        run_kind: RunKindRef,
        validation_binding: ResolvedExecutionProfileBindingV1,
        compiler_binding: ResolvedExecutionProfileBindingV1,
    ) -> ConstraintValidationProfileAuthorityV1:
        if (
            validation_binding.catalog_version != compiler_binding.catalog_version
            or validation_binding.catalog_digest != compiler_binding.catalog_digest
        ):
            raise IntegrityViolation(
                "constraint validation profiles come from different exact catalogs"
            )
        expected = (
            (
                validation_binding,
                "validation",
                "builtin_validation_profile@1",
                "validation-profile-config@1",
                {
                    "constraint-validation@1",
                    "patch-validation@1",
                },
                {"auto-apply-proof@1", "evidence-set@1"},
            ),
            (
                compiler_binding,
                "constraint_compiler",
                "builtin_constraint_compiler_profile@1",
                "constraint_compiler-profile-config@1",
                {"constraint-validation@1"},
                {"constraint-compile-evidence@1", "constraint-snapshot@1"},
            ),
        )
        for (
            binding,
            profile_kind,
            handler_key,
            config_schema_id,
            input_schema_ids,
            output_schema_ids,
        ) in expected:
            definition, lifecycle = self.registry.resolve_execution_profile_binding(binding)
            expected_run_kinds = (
                {
                    RunKindRef(kind="constraint_proposal.validate", version=1),
                    RunKindRef(kind="patch.validate", version=1),
                }
                if profile_kind == "validation"
                else {RunKindRef(kind="constraint_proposal.validate", version=1)}
            )
            if (
                lifecycle.state != "active"
                or definition.profile != binding.profile
                or definition.profile_kind != profile_kind
                or run_kind not in definition.compatible_run_kinds
                or set(definition.compatible_run_kinds) != expected_run_kinds
                or set(definition.input_schema_ids) != input_schema_ids
                or set(definition.output_schema_ids) != output_schema_ids
                or definition.handler_key != handler_key
                or definition.config_schema_id != config_schema_id
                or definition.config != {}
                or definition.stochastic
                or definition.required_capabilities
                or (
                    profile_kind == "validation"
                    and (
                        not isinstance(definition.details, ValidationProfileDetailsV1)
                        or set(definition.details.subject_kinds)
                        != {"patch", "constraint_proposal", "rollback_request"}
                    )
                )
            ):
                raise IntegrityViolation(
                    "constraint validation profile does not authorize the built-in adapter"
                )
        return ConstraintValidationProfileAuthorityV1(
            validation_binding=validation_binding,
            compiler_binding=compiler_binding,
            validation_handler_key="builtin_validation_profile@1",
            compiler_handler_key="builtin_constraint_compiler_profile@1",
        )


_Z3_TIMEOUT_MS = 5000

# Structural ``assert`` is intentionally a free-form string in DSL v1.  The
# differential boundary therefore needs a *closed* semantic vocabulary: an
# unknown sentence is not executable merely because it happens to contain a
# word such as "reachable".  These are the exact spellings shipped by the repo
# (plus whitespace-insensitive function formatting for the canonical call).
_STRUCTURAL_PREDICATE_SEMANTICS = {
    "acyclic(quest_steps)": "cyclic_dependency",
    "quest_step_dependency_graph_is_acyclic": "cyclic_dependency",
    # ASPChecker proves source existence but has no navigation authority.  It may
    # therefore cross-check only the weaker exact predicate; the shipped
    # ``...reachable_drop_source`` spelling remains unproven until both engines
    # consume the same navigation contract.
    "every_collect_step_has_a_drop_source": "missing_drop_source",
}


def _field_paths(node: AssertNode) -> set[str]:
    """Collect every ``Field`` path referenced by a parsed assert-expression."""

    paths: set[str] = set()

    def walk(current: AssertNode) -> None:
        if isinstance(current, Field):
            paths.add(current.path)
        elif isinstance(current, Compare):
            walk(current.left)
            walk(current.right)
        elif isinstance(current, BinOp):
            walk(current.left)
            walk(current.right)
        elif isinstance(current, BoolOp):
            for value in current.values:
                walk(value)
        elif isinstance(current, UnaryOp):
            walk(current.operand)
        elif isinstance(current, Call):
            for arg in current.args:
                walk(arg)

    walk(node)
    return paths


def _numeric_groups(constraints: list[Constraint]) -> tuple[tuple[Constraint, ...], ...]:
    groups: dict[tuple[str, str], list[Constraint]] = {}
    for constraint in constraints:
        selector_kind, selector = (
            ("scope", constraint.scope)
            if constraint.scope is not None
            else ("forall", constraint.forall)
            if constraint.forall is not None
            else ("unscoped", None)
        )
        selector_wire = None if selector is None else selector.model_dump(mode="json")
        groups.setdefault(
            (selector_kind, canonical_json(selector_wire)),
            [],
        ).append(constraint)
    return tuple(tuple(group) for group in groups.values())


def _structural_domain(constraints: tuple[Constraint, ...]) -> tuple[Constraint, ...]:
    return tuple(
        constraint for constraint in constraints if constraint.kind in ("structural", "narrative")
    )


def _normalize_structural_predicate(expression: str) -> str:
    """Normalize insignificant whitespace, never words or substrings."""

    return "".join(expression.lower().split())


def _structural_predicate_semantic(constraint: Constraint) -> str | None:
    """Resolve one constraint only when every exact predicate is understood.

    Narrative and LLM-assisted constraints are outside this deterministic pair.
    When ``predicates`` are present, each expression must name the same frozen
    semantic as ``assert``; partially-understood candidates remain unproven.
    """

    if constraint.kind != "structural" or constraint.has_llm_predicate():
        return None
    expressions = (
        constraint.assert_,
        *(predicate.expr for predicate in constraint.predicates),
    )
    semantics = {
        _STRUCTURAL_PREDICATE_SEMANTICS.get(_normalize_structural_predicate(expression))
        for expression in expressions
    }
    if None in semantics or len(semantics) != 1:
        return None
    return next(iter(semantics))


def _cycle_witnesses() -> tuple[Snapshot, Snapshot]:
    steps = [
        Entity(id="witness:step:a", type=NodeType.QUEST_STEP),
        Entity(id="witness:step:b", type=NodeType.QUEST_STEP),
    ]
    forward = Relation(
        id="witness:precedes:a-b",
        type=EdgeType.PRECEDES,
        src_id=steps[0].id,
        dst_id=steps[1].id,
    )
    backward = Relation(
        id="witness:precedes:b-a",
        type=EdgeType.PRECEDES,
        src_id=steps[1].id,
        dst_id=steps[0].id,
    )
    return (
        Snapshot.from_entities_relations(steps, [forward, backward]),
        Snapshot.from_entities_relations(steps, [forward]),
    )


def _drop_source_witnesses() -> tuple[Snapshot, Snapshot]:
    item = Entity(id="witness:item", type=NodeType.ITEM)
    step = Entity(
        id="witness:collect-step",
        type=NodeType.QUEST_STEP,
        attrs={"kind": "collect", "item": item.id},
    )
    source = Entity(id="witness:monster", type=NodeType.MONSTER)
    drop = Relation(
        id="witness:drop",
        type=EdgeType.DROPS_FROM,
        src_id=source.id,
        dst_id=item.id,
    )
    return (
        Snapshot.from_entities_relations([item, step], []),
        Snapshot.from_entities_relations([item, step, source], [drop]),
    )


_STRUCTURAL_WITNESSES = {
    "cyclic_dependency": _cycle_witnesses,
    "missing_drop_source": _drop_source_witnesses,
}

_NUMERIC_WITNESS_ENTITY_ID = "witness:numeric"


def _numeric_snapshot(
    constraint: Constraint,
    values: dict[str, Fraction],
) -> Snapshot | None:
    """Materialize one selector-matching concrete numeric witness."""

    selector = constraint.scope or constraint.forall
    if selector is None:
        return None
    try:
        node_type = NodeType[selector.node_type]
    except KeyError:
        return None
    attrs: dict[str, object] = dict(selector.where)
    for path, value in values.items():
        parts = path.split(".")
        target = attrs
        for part in parts[:-1]:
            current = target.get(part)
            if current is None:
                nested: dict[str, object] = {}
                target[part] = nested
                target = nested
            elif isinstance(current, dict):
                target = current
            else:
                return None
        concrete: int | float = value.numerator if value.denominator == 1 else float(value)
        existing = target.get(parts[-1])
        if existing is not None and existing != concrete:
            # A selector-fixed value cannot yield both sides of a discriminating
            # witness without leaving the exact selector domain.
            return None
        target[parts[-1]] = concrete
    entity = Entity(
        id=f"{_NUMERIC_WITNESS_ENTITY_ID}:{constraint.id}",
        type=node_type,
        attrs=attrs,
    )
    return Snapshot.from_entities_relations([entity], [])


def _normalize_numeric_findings(
    findings: Sequence[Finding],
) -> tuple[tuple[object, ...], ...]:
    normalized = [
        (
            finding.source,
            finding.producer_id,
            finding.oracle_type,
            finding.defect_class,
            finding.severity,
            tuple(sorted(finding.entities)),
            tuple(sorted(finding.relations)),
            finding.constraint_id,
            finding.status,
        )
        for finding in findings
    ]
    return tuple(sorted(normalized, key=repr))


def _run_numeric_compiled_witness(
    constraint: Constraint,
    *,
    satisfying_values: dict[str, Fraction],
    violating_values: dict[str, Fraction],
    expected_defect_class: str,
) -> tuple[str, str | None]:
    """Execute the real compiler result against independently-derived witnesses."""

    compiled = compile_all([constraint])
    if len(compiled) != 1:
        raise IntegrityViolation("numeric witness compiler returned the wrong checker count")
    checker = compiled[0]
    binding = getattr(checker, "execution_binding", None)
    if (
        not isinstance(binding, CheckerExecutionBinding)
        or binding.wrapper_id != getattr(checker, "id", None)
        or binding.native_id != "smt"
        or binding.constraint_id != constraint.id
    ):
        raise IntegrityViolation("numeric witness compiler returned the wrong checker route")
    satisfying = _numeric_snapshot(constraint, satisfying_values)
    violating = _numeric_snapshot(constraint, violating_values)
    if satisfying is None or violating is None:
        return "undecided", "witness_selector_unsupported"
    satisfied_findings = checker.check(satisfying)
    violated_findings = checker.check(violating)
    if any(finding.status == "unproven" for finding in (*satisfied_findings, *violated_findings)):
        return "undecided", "compiled_checker_unproven"
    entity_id = f"{_NUMERIC_WITNESS_ENTITY_ID}:{constraint.id}"
    expected_violation = (
        (
            "checker",
            "smt",
            "deterministic",
            expected_defect_class,
            constraint.severity,
            (entity_id,),
            (),
            constraint.id,
            "confirmed",
        ),
    )
    try:
        actual = (
            _normalize_numeric_findings(satisfied_findings),
            _normalize_numeric_findings(violated_findings),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("compiled numeric checker returned invalid findings") from exc
    if actual != ((), expected_violation):
        return "inconsistent", None
    return "consistent", None


def _z3_model_values(
    model: z3.ModelRef,
    variables: dict[str, z3.ArithRef],
) -> dict[str, Fraction] | None:
    values: dict[str, Fraction] = {}
    for path, variable in variables.items():
        value = model.eval(variable, model_completion=True)
        if z3.is_int_value(value):
            values[path] = Fraction(value.as_long())
        elif z3.is_rational_value(value):
            values[path] = Fraction(
                value.numerator_as_long(),
                value.denominator_as_long(),
            )
        else:
            return None
    return values


def _z3_numeric_witness_values(
    constraint: Constraint,
) -> tuple[dict[str, Fraction], dict[str, Fraction]] | None:
    """Derive both sides directly from z3, independently of the reference engine."""

    try:
        node = parse_assert(constraint.assert_)
        paths = _field_paths(node)
        if not paths:
            return None
        entity = Entity(id="z3-witness-reference", type=NodeType.ITEM)
        ctx = _BindCtx(entity=entity)
        ctx._vars.update({path: z3.Real(f"z3-witness::{constraint.id}::{path}") for path in paths})
        expression = _smt_compile(node, ctx)
        if not z3.is_bool(expression):
            return None
    except (SmtCompileError, TypeError, ValueError):
        return None

    assignments: list[dict[str, Fraction]] = []
    for predicate in (expression, z3.Not(expression)):
        solver = z3.Solver()
        solver.set("timeout", _Z3_TIMEOUT_MS)
        solver.add(*ctx.extra, predicate)
        if solver.check() != z3.sat:
            return None
        values = _z3_model_values(solver.model(), ctx._vars)
        if values is None:
            return None
        assignments.append(values)
    return assignments[0], assignments[1]


def _run_structural_witness(
    reference_checker: Checker,
    compiled_checker: Checker,
    semantic: str,
    constraint_id: str,
) -> tuple[str, str | None]:
    """Cross-check the real compiler output against one independent backend.

    The reference backend first has to prove the fixture itself is discriminating:
    it must confirm the selected semantic on the dirty witness and not on the clean
    witness.  The checker returned by ``compile_all([constraint])`` then runs on the
    *same* two snapshots.  Its complete normalized output (not only findings matching
    ``semantic``) must equal the selected reference output, so an unfiltered/wrong
    compiler route cannot acquire coverage merely because it also finds the expected
    defect.
    """

    dirty, clean = _STRUCTURAL_WITNESSES[semantic]()
    reference_dirty = reference_checker.check(dirty)
    reference_clean = reference_checker.check(clean)

    relevant_dirty = [item for item in reference_dirty if item.defect_class == semantic]
    relevant_clean = [item for item in reference_clean if item.defect_class == semantic]
    if any(item.status == "unproven" for item in (*relevant_dirty, *relevant_clean)):
        return "undecided", "reference_budget_exhausted"
    dirty_confirmed = any(item.status == "confirmed" for item in relevant_dirty)
    clean_confirmed = any(item.status == "confirmed" for item in relevant_clean)
    if not dirty_confirmed or clean_confirmed:
        return "inconsistent", None

    compiled_dirty = compiled_checker.check(dirty)
    compiled_clean = compiled_checker.check(clean)
    if any(item.status == "unproven" for item in (*compiled_dirty, *compiled_clean)):
        return "undecided", "compiled_checker_unproven"

    try:
        expected = (
            _normalize_structural_findings(
                relevant_dirty,
                constraint_id=constraint_id,
                reference=True,
            ),
            _normalize_structural_findings(
                relevant_clean,
                constraint_id=constraint_id,
                reference=True,
            ),
        )
        actual = (
            _normalize_structural_findings(
                compiled_dirty,
                constraint_id=constraint_id,
                reference=False,
            ),
            _normalize_structural_findings(
                compiled_clean,
                constraint_id=constraint_id,
                reference=False,
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("structural checker returned invalid findings") from exc
    if actual != expected:
        return "inconsistent", None
    return "consistent", None


def _normalize_structural_findings(
    findings: Sequence[Finding],
    *,
    constraint_id: str,
    reference: bool,
) -> tuple[tuple[object, ...], ...]:
    """Project backend-specific Findings onto their shared deterministic facts."""

    normalized: list[tuple[object, ...]] = []
    for finding in findings:
        bound_constraint_id = constraint_id if reference else finding.constraint_id
        normalized.append(
            (
                finding.source,
                finding.oracle_type,
                finding.defect_class,
                finding.severity,
                tuple(sorted(finding.entities)),
                tuple(sorted(finding.relations)),
                bound_constraint_id,
                finding.status,
            )
        )
    return tuple(sorted(normalized, key=repr))


def _evaluate_structural_candidates(
    constraints: tuple[Constraint, ...],
    *,
    checker_factory: Callable[[], Checker],
    reason_prefix: str,
) -> DifferentialEngineResultV1:
    structural = _structural_domain(constraints)
    if not structural:
        return DifferentialEngineResultV1(
            status="not_applicable", reason_code="engine_domain_not_applicable"
        )

    decided: list[str] = []
    undecided_reason: str | None = None
    for constraint in structural:
        semantic = _structural_predicate_semantic(constraint)
        if semantic is None:
            undecided_reason = f"{reason_prefix}_predicate_unsupported"
            continue
        compiled = compile_all([constraint])
        if len(compiled) != 1:
            raise IntegrityViolation("structural witness compiler returned the wrong checker count")
        verdict, reason = _run_structural_witness(
            checker_factory(),
            compiled[0],
            semantic,
            constraint.id,
        )
        if verdict == "inconsistent":
            return DifferentialEngineResultV1(
                status="evaluated",
                consistency="inconsistent",
                decided_constraint_ids=tuple(decided),
            )
        if verdict == "undecided":
            undecided_reason = f"{reason_prefix}_{reason}"
            continue
        decided.append(constraint.id)

    if undecided_reason is not None:
        return DifferentialEngineResultV1(
            status="undecided",
            reason_code=undecided_reason,
            decided_constraint_ids=tuple(decided),
        )
    return DifferentialEngineResultV1(
        status="evaluated",
        consistency="consistent",
        decided_constraint_ids=tuple(decided),
    )


@dataclass(frozen=True, slots=True)
class Z3DifferentialEngine:
    """Cross-check every NUMERIC constraint's satisfiability with REAL z3.

    z3's domain is the numeric constraints. When the candidate has NONE, this
    engine reports ``not_applicable``. Otherwise predicates with the same exact
    selector scope are compiled over one shared FREE bounded symbol table and
    asserted together. This catches contradictions split across separate constraints,
    rather than proving each row satisfiable in isolation. Any joint ``unsat`` is a
    genuine contradiction; ``unknown`` or an in-domain predicate the probe cannot
    bind is ``undecided`` (never a pass). Only when every selector group is jointly
    satisfiable does it report ``evaluated`` / ``consistent``.
    """

    engine_id: str = "z3"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        numeric = [
            constraint
            for constraint in request.constraints
            if constraint.kind == "numeric" and not constraint.has_llm_predicate()
        ]
        if not numeric:
            return DifferentialEngineResultV1(
                status="not_applicable", reason_code="engine_domain_not_applicable"
            )
        decided: list[str] = []
        undecided_reason: str | None = None
        for group in _numeric_groups(numeric):
            verdict, reason = self._check_group(group)
            if verdict == "inconsistent":
                return DifferentialEngineResultV1(
                    status="evaluated",
                    consistency="inconsistent",
                    decided_constraint_ids=tuple(decided),
                )
            if verdict == "undecided":
                undecided_reason = reason
                continue
            for constraint in group:
                witnesses = _z3_numeric_witness_values(constraint)
                if witnesses is None:
                    undecided_reason = "z3_numeric_witness_unsupported"
                    continue
                witness_verdict, witness_reason = _run_numeric_compiled_witness(
                    constraint,
                    satisfying_values=witnesses[0],
                    violating_values=witnesses[1],
                    expected_defect_class="reward_out_of_range",
                )
                if witness_verdict == "inconsistent":
                    return DifferentialEngineResultV1(
                        status="evaluated",
                        consistency="inconsistent",
                        decided_constraint_ids=tuple(decided),
                    )
                if witness_verdict == "undecided":
                    undecided_reason = f"z3_numeric_{witness_reason}"
                    continue
                decided.append(constraint.id)
        if undecided_reason is not None:
            return DifferentialEngineResultV1(
                status="undecided",
                reason_code=undecided_reason,
                decided_constraint_ids=tuple(decided),
            )
        return DifferentialEngineResultV1(
            status="evaluated", consistency="consistent", decided_constraint_ids=tuple(decided)
        )

    def _check_group(
        self,
        constraints: tuple[Constraint, ...],
    ) -> tuple[str, str | None]:
        """Check one selector scope as a conjunction over a shared symbol table."""

        nodes: list[AssertNode] = []
        try:
            for constraint in constraints:
                nodes.append(parse_assert(constraint.assert_))
        except DslError:
            return "undecided", "z3_parse_error"
        paths = set().union(*(_field_paths(node) for node in nodes))
        entity = Entity(id="differential-probe", type="ITEM")
        ctx = _BindCtx(entity=entity)
        # Pre-bind every scalar field as an unbounded REAL.  Supplying integer
        # min/max placeholder attrs would silently change the DSL domain to Int;
        # supplying finite bounds would reject otherwise-satisfiable candidates.
        ctx._vars.update({path: z3.Real(f"{entity.id}::{path}") for path in paths})
        expressions: list[z3.BoolRef] = []
        try:
            for node in nodes:
                expr = _smt_compile(node, ctx)
                if not z3.is_bool(expr):
                    # A numeric constraint in z3's domain that is not a boolean
                    # predicate is unproven, never silently skipped-as-passed.
                    return "undecided", "z3_non_boolean_predicate"
                expressions.append(expr)
        except SmtCompileError:
            # an in-domain numeric predicate this free-var probe cannot bind (e.g.
            # prob_sum over a list attr) -> undecided, NEVER a pass.
            return "undecided", "z3_cannot_bind_predicate"
        solver = z3.Solver()
        solver.set("timeout", _Z3_TIMEOUT_MS)
        for extra in ctx.extra:
            solver.add(extra)
        solver.add(*expressions)
        result = solver.check()
        if result == z3.unsat:
            return "inconsistent", None
        if result == z3.unknown:
            return "undecided", "z3_budget_exhausted"
        return "consistent", None


@dataclass(slots=True)
class _RationalDomain:
    """Independent exact-rational feasibility bounds for one numeric field."""

    lower: Fraction | None = None
    lower_inclusive: bool = True
    upper: Fraction | None = None
    upper_inclusive: bool = True
    excluded: set[Fraction] = field(default_factory=set)

    def _tighten_lower(self, value: Fraction, *, inclusive: bool) -> None:
        if self.lower is None or value > self.lower:
            self.lower = value
            self.lower_inclusive = inclusive
        elif value == self.lower:
            self.lower_inclusive = self.lower_inclusive and inclusive

    def _tighten_upper(self, value: Fraction, *, inclusive: bool) -> None:
        if self.upper is None or value < self.upper:
            self.upper = value
            self.upper_inclusive = inclusive
        elif value == self.upper:
            self.upper_inclusive = self.upper_inclusive and inclusive

    def apply(self, operator: str, value: Fraction) -> bool:
        if operator == "<=":
            self._tighten_upper(value, inclusive=True)
        elif operator == "<":
            self._tighten_upper(value, inclusive=False)
        elif operator == ">=":
            self._tighten_lower(value, inclusive=True)
        elif operator == ">":
            self._tighten_lower(value, inclusive=False)
        elif operator == "==":
            self._tighten_lower(value, inclusive=True)
            self._tighten_upper(value, inclusive=True)
        elif operator == "!=":
            self.excluded.add(value)
        else:  # pragma: no cover - parser owns the comparison operator enum
            return False
        return self.feasible()

    def feasible(self) -> bool:
        if self.lower is None or self.upper is None:
            return True
        if self.lower > self.upper:
            return False
        if self.lower < self.upper:
            # A non-empty rational interval contains infinitely many points;
            # finitely many ``!=`` exclusions cannot exhaust it.
            return True
        return self.lower_inclusive and self.upper_inclusive and self.lower not in self.excluded


def _numeric_constant(node: AssertNode) -> Fraction | None:
    if not isinstance(node, Const):
        return None
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return Fraction(str(value))


_REVERSED_COMPARISON = {
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
    "==": "==",
    "!=": "!=",
}


def _apply_reference_predicate(
    node: AssertNode,
    domains: dict[str, _RationalDomain],
) -> str:
    """Return ``supported``, ``inconsistent``, or ``unsupported`` without z3."""

    if isinstance(node, BoolOp):
        if node.op != "and":
            return "unsupported"
        saw_unsupported = False
        for value in node.values:
            result = _apply_reference_predicate(value, domains)
            if result == "inconsistent":
                return result
            saw_unsupported = saw_unsupported or result == "unsupported"
        return "unsupported" if saw_unsupported else "supported"
    if not isinstance(node, Compare):
        return "unsupported"

    field_node: Field | None = None
    constant: Fraction | None = None
    operator = node.op
    if isinstance(node.left, Field):
        field_node = node.left
        constant = _numeric_constant(node.right)
    elif isinstance(node.right, Field):
        field_node = node.right
        constant = _numeric_constant(node.left)
        operator = _REVERSED_COMPARISON[operator]
    else:
        left = _numeric_constant(node.left)
        right = _numeric_constant(node.right)
        if left is None or right is None:
            return "unsupported"
        verdict = {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
            "==": left == right,
            "!=": left != right,
        }[operator]
        return "supported" if verdict else "inconsistent"
    if field_node is None or constant is None:
        return "unsupported"
    domain = domains.setdefault(field_node.path, _RationalDomain())
    return "supported" if domain.apply(operator, constant) else "inconsistent"


def _reference_thresholds(
    node: AssertNode,
) -> dict[str, set[Fraction]] | None:
    """Extract the exact simple-comparison domain accepted by the reference engine."""

    if isinstance(node, BoolOp):
        if node.op != "and":
            return None
        merged: dict[str, set[Fraction]] = {}
        for child in node.values:
            child_thresholds = _reference_thresholds(child)
            if child_thresholds is None:
                return None
            for path, values in child_thresholds.items():
                merged.setdefault(path, set()).update(values)
        return merged
    if not isinstance(node, Compare):
        return None
    if isinstance(node.left, Field):
        constant = _numeric_constant(node.right)
        path = node.left.path
    elif isinstance(node.right, Field):
        constant = _numeric_constant(node.left)
        path = node.right.path
    else:
        return None
    if constant is None:
        return None
    return {path: {constant}}


def _reference_eval(node: AssertNode, values: dict[str, Fraction]) -> bool:
    if isinstance(node, Const):
        if isinstance(node.value, str):
            raise ValueError("string is outside numeric reference semantics")
        return bool(Fraction(str(node.value)))
    if isinstance(node, Field):
        return bool(values[node.path])
    if isinstance(node, Compare):
        left = _reference_value(node.left, values)
        right = _reference_value(node.right, values)
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
            "==": left == right,
            "!=": left != right,
        }[node.op]
    if isinstance(node, BoolOp) and node.op == "and":
        return all(_reference_eval(child, values) for child in node.values)
    raise ValueError("unsupported reference predicate")


def _reference_value(node: AssertNode, values: dict[str, Fraction]) -> Fraction:
    if isinstance(node, Field):
        return values[node.path]
    if isinstance(node, Const) and not isinstance(node.value, str):
        return Fraction(str(node.value))
    raise ValueError("unsupported reference numeric value")


def _reference_numeric_witness_values(
    constraint: Constraint,
) -> tuple[dict[str, Fraction], dict[str, Fraction]] | None:
    """Search exact rationals without consuming z3 or its model."""

    try:
        node = parse_assert(constraint.assert_)
    except DslError:
        return None
    thresholds = _reference_thresholds(node)
    if not thresholds:
        return None
    paths = tuple(sorted(thresholds))
    candidates: list[tuple[Fraction, ...]] = []
    combinations = 1
    for path in paths:
        ordered = sorted(thresholds[path])
        values = {Fraction(0)}
        for threshold in ordered:
            values.update((threshold - 1, threshold, threshold + 1))
        values.update((left + right) / 2 for left, right in zip(ordered, ordered[1:]))
        options = tuple(sorted(values))
        combinations *= len(options)
        if combinations > 4096:
            return None
        candidates.append(options)

    satisfying: dict[str, Fraction] | None = None
    violating: dict[str, Fraction] | None = None
    try:
        for combination in product(*candidates):
            assignment = dict(zip(paths, combination, strict=True))
            if _reference_eval(node, assignment):
                satisfying = satisfying or assignment
            else:
                violating = violating or assignment
            if satisfying is not None and violating is not None:
                return satisfying, violating
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return None


@dataclass(frozen=True, slots=True)
class NumericReferenceDifferentialEngine:
    """Independent exact-rational reference for simple numeric predicates."""

    engine_id: str = "numeric-reference"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        numeric = [
            constraint
            for constraint in request.constraints
            if constraint.kind == "numeric" and not constraint.has_llm_predicate()
        ]
        if not numeric:
            return DifferentialEngineResultV1(
                status="not_applicable",
                reason_code="engine_domain_not_applicable",
            )
        decided: list[str] = []
        unsupported = False
        for group in _numeric_groups(numeric):
            domains: dict[str, _RationalDomain] = {}
            group_unsupported = False
            for constraint in group:
                try:
                    node = parse_assert(constraint.assert_)
                except DslError:
                    return DifferentialEngineResultV1(
                        status="undecided",
                        reason_code="numeric_reference_parse_error",
                        decided_constraint_ids=tuple(decided),
                    )
                verdict = _apply_reference_predicate(node, domains)
                if verdict == "inconsistent":
                    return DifferentialEngineResultV1(
                        status="evaluated",
                        consistency="inconsistent",
                        decided_constraint_ids=tuple(decided),
                    )
                group_unsupported = group_unsupported or verdict == "unsupported"
            if group_unsupported:
                unsupported = True
                continue
            for constraint in group:
                witnesses = _reference_numeric_witness_values(constraint)
                if witnesses is None:
                    unsupported = True
                    continue
                witness_verdict, witness_reason = _run_numeric_compiled_witness(
                    constraint,
                    satisfying_values=witnesses[0],
                    violating_values=witnesses[1],
                    expected_defect_class="reward_out_of_range",
                )
                if witness_verdict == "inconsistent":
                    return DifferentialEngineResultV1(
                        status="evaluated",
                        consistency="inconsistent",
                        decided_constraint_ids=tuple(decided),
                    )
                if witness_verdict == "undecided":
                    return DifferentialEngineResultV1(
                        status="undecided",
                        reason_code=f"numeric_reference_{witness_reason}",
                        decided_constraint_ids=tuple(decided),
                    )
                decided.append(constraint.id)
        if unsupported:
            return DifferentialEngineResultV1(
                status="undecided",
                reason_code="numeric_reference_unsupported_predicate",
                decided_constraint_ids=tuple(decided),
            )
        return DifferentialEngineResultV1(
            status="evaluated",
            consistency="consistent",
            decided_constraint_ids=tuple(decided),
        )


@dataclass(frozen=True, slots=True)
class ClingoDifferentialEngine:
    """Execute supported structural predicates through REAL Clingo witnesses."""

    engine_id: str = "clingo"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        return _evaluate_structural_candidates(
            request.constraints,
            checker_factory=ASPChecker,
            reason_prefix="clingo",
        )


@dataclass(frozen=True, slots=True)
class GraphReferenceDifferentialEngine:
    """Execute the same exact semantics through the independent GraphChecker."""

    engine_id: str = "graph-reference"
    engine_version: int = 1

    def evaluate(self, request: DifferentialEvalRequest) -> DifferentialEngineResultV1:
        return _evaluate_structural_candidates(
            request.constraints,
            checker_factory=GraphChecker,
            reason_prefix="graph_reference",
        )


def build_differential_engines() -> dict[tuple[str, int], ConstraintDifferentialEngine]:
    """The exact ``(engine_id, version) -> implementation`` worker registry."""

    engines: tuple[ConstraintDifferentialEngine, ...] = (
        Z3DifferentialEngine(),
        NumericReferenceDifferentialEngine(),
        ClingoDifferentialEngine(),
        GraphReferenceDifferentialEngine(),
    )
    registry = {(engine.engine_id, engine.engine_version): engine for engine in engines}
    expected = {
        (engine.engine_id, engine.version)
        for engine in BUILTIN_CONSTRAINT_DIFFERENTIAL_ENGINE_REFS_V1
    }
    if set(registry) != expected:  # pragma: no cover - composition invariant
        raise RuntimeError("builtin constraint differential engine registry drifted")
    return registry


__all__ = [
    "ClingoDifferentialEngine",
    "GraphReferenceDifferentialEngine",
    "NumericReferenceDifferentialEngine",
    "RegistryConstraintValidationProfileResolver",
    "Z3DifferentialEngine",
    "build_differential_engines",
]
