"""SMTChecker (M1 Task 6): 5 numeric defect classes + timeout/undecidable
degradation to `status="unproven"` (M1-D7). Each defect class gets a
violation->detected(+violating assignment) case and a satisfied->silent case
(oracle-FP=0 anchor); plus a real (non-mocked) hard-nonlinear case proving the
solver-budget degrade path never treats `z3.unknown` as a pass.
"""

from __future__ import annotations

import z3

from gameforge.contracts.dsl import Constraint, Selector
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.checkers.smt import SMTChecker
from gameforge.spine.ir.snapshot import Snapshot


def _snap(*entities):
    return Snapshot.from_entities_relations(list(entities), [])


def _by_class(findings, defect_class):
    return [f for f in findings if f.defect_class == defect_class]


# --- 1. reward_out_of_range ------------------------------------------------

_CAP_CONSTRAINT = Constraint(
    id="C_reward_cap", kind="numeric", oracle="deterministic",
    scope=Selector(var="q", node_type="QUEST"),
    assert_="reward_gold <= 80",
    severity="major",
)


def test_reward_out_of_range_detected_with_violating_assignment():
    ent = Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})
    fs = _by_class(SMTChecker([_CAP_CONSTRAINT]).check(_snap(ent)), "reward_out_of_range")
    assert len(fs) == 1
    f = fs[0]
    assert f.constraint_id == "C_reward_cap"
    assert f.oracle_type == "deterministic"
    assert f.status == "confirmed"
    assert f.entities == ["q:1"]
    assert f.evidence["violating_assignment"]["reward_gold"] == 120


def test_reward_within_range_is_silent():
    ent = Entity(id="q:2", type=NodeType.QUEST, attrs={"reward_gold": 50})
    fs = SMTChecker([_CAP_CONSTRAINT]).check(_snap(ent))
    assert fs == []


# --- 2. prob_sum_ne_1 -------------------------------------------------------

_PROB_SUM_CONSTRAINT = Constraint(
    id="C_prob_sum", kind="numeric", oracle="deterministic",
    scope=Selector(var="d", node_type="DROP_TABLE"),
    assert_="prob_sum(entries) == 1",
    severity="critical",
)


def test_prob_sum_ne_1_detected_with_violating_assignment():
    ent = Entity(
        id="dt:1", type=NodeType.DROP_TABLE,
        attrs={"entries": [{"probability": 0.5}, {"probability": 0.3}]},
    )
    fs = _by_class(SMTChecker([_PROB_SUM_CONSTRAINT]).check(_snap(ent)), "prob_sum_ne_1")
    assert len(fs) == 1
    f = fs[0]
    assert f.constraint_id == "C_prob_sum"
    assert f.oracle_type == "deterministic"
    assert f.status == "confirmed"
    assert f.evidence["violating_assignment"]["entries"] == [
        {"probability": 0.5}, {"probability": 0.3},
    ]


def test_prob_sum_eq_1_is_silent():
    ent = Entity(
        id="dt:2", type=NodeType.DROP_TABLE,
        attrs={"entries": [{"probability": 0.6}, {"probability": 0.4}]},
    )
    fs = SMTChecker([_PROB_SUM_CONSTRAINT]).check(_snap(ent))
    assert fs == []


def test_prob_sum_valid_multi_entry_no_false_positive():
    # Regression (M1 final review, CRITICAL): `_call_prob_sum` used to
    # accumulate entry probabilities in a Python float, so a legitimately
    # correct table like [0.7, 0.2, 0.1] float-sums to 0.9999999999999999 (!=
    # 1) and spuriously tripped `Not(prob_sum(entries) == 1)` into SAT --
    # emitting a FALSE-POSITIVE prob_sum_ne_1 Finding for a table that is
    # actually correct. This directly violates oracle-FP=0: a satisfied
    # constraint must yield ZERO findings. Fixed by summing as exact
    # `fractions.Fraction`s (via `str(v)`, never `Fraction(float)`) and
    # handing z3 an exact rational (`z3.Q`) instead of a lossy float literal.
    non_power_of_two = Entity(
        id="dt:3", type=NodeType.DROP_TABLE,
        attrs={"entries": [
            {"probability": 0.7}, {"probability": 0.2}, {"probability": 0.1},
        ]},
    )
    ten_tenths = Entity(
        id="dt:4", type=NodeType.DROP_TABLE,
        attrs={"entries": [{"probability": 0.1} for _ in range(10)]},
    )
    fs = SMTChecker([_PROB_SUM_CONSTRAINT]).check(_snap(non_power_of_two, ten_tenths))
    assert fs == []


# --- 3. non_monotonic_curve --------------------------------------------------

_MONOTONIC_CONSTRAINT = Constraint(
    id="C_monotonic_curve", kind="numeric", oracle="deterministic",
    scope=Selector(var="f", node_type="FORMULA"),
    assert_="monotonic(curve)",
    severity="major",
)


def test_non_monotonic_curve_detected_with_violating_assignment():
    ent = Entity(id="f:1", type=NodeType.FORMULA, attrs={"curve": [1, 5, 3, 10]})
    fs = _by_class(SMTChecker([_MONOTONIC_CONSTRAINT]).check(_snap(ent)), "non_monotonic_curve")
    assert len(fs) == 1
    f = fs[0]
    assert f.constraint_id == "C_monotonic_curve"
    assert f.status == "confirmed"
    assert f.evidence["violating_assignment"]["curve"] == [1, 5, 3, 10]


def test_monotonic_curve_is_silent():
    ent = Entity(id="f:2", type=NodeType.FORMULA, attrs={"curve": [1, 3, 5, 10]})
    fs = SMTChecker([_MONOTONIC_CONSTRAINT]).check(_snap(ent))
    assert fs == []


# --- 4. interval_violation ---------------------------------------------------

_IN_RANGE_CONSTRAINT = Constraint(
    id="C_difficulty_range", kind="numeric", oracle="deterministic",
    scope=Selector(var="q", node_type="QUEST"),
    assert_="in_range(difficulty, 1, 10)",
    severity="minor",
)


def test_interval_violation_detected_with_violating_assignment():
    ent = Entity(id="q:3", type=NodeType.QUEST, attrs={"difficulty": 15})
    fs = _by_class(SMTChecker([_IN_RANGE_CONSTRAINT]).check(_snap(ent)), "interval_violation")
    assert len(fs) == 1
    f = fs[0]
    assert f.constraint_id == "C_difficulty_range"
    assert f.status == "confirmed"
    assert f.evidence["violating_assignment"]["difficulty"] == 15


def test_interval_satisfied_is_silent():
    ent = Entity(id="q:4", type=NodeType.QUEST, attrs={"difficulty": 5})
    fs = SMTChecker([_IN_RANGE_CONSTRAINT]).check(_snap(ent))
    assert fs == []


# --- 5. gacha_expectation_violation ------------------------------------------

_GACHA_CONSTRAINT = Constraint(
    id="C_gacha_expectation", kind="numeric", oracle="deterministic",
    scope=Selector(var="g", node_type="GACHA_POOL"),
    assert_="gacha_expectation(base_rate, pity_threshold) <= max_expected_pulls",
    severity="major",
)


def test_gacha_expectation_violation_detected_with_violating_assignment():
    # p=0.005, N=90 -> E[min(X,N)] = (1-(1-p)**N)/p ~= 72.6, which exceeds the
    # designer's expected-pulls budget of 50 (well under the hard pity of 90).
    ent = Entity(
        id="g:1", type=NodeType.GACHA_POOL,
        attrs={"base_rate": 0.005, "pity_threshold": 90, "max_expected_pulls": 50},
    )
    fs = _by_class(
        SMTChecker([_GACHA_CONSTRAINT]).check(_snap(ent)), "gacha_expectation_violation"
    )
    assert len(fs) == 1
    f = fs[0]
    assert f.constraint_id == "C_gacha_expectation"
    assert f.status == "confirmed"
    assignment = f.evidence["violating_assignment"]
    assert assignment["base_rate"] == 0.005
    assert assignment["pity_threshold"] == 90
    assert assignment["max_expected_pulls"] == 50


def test_gacha_expectation_within_budget_is_silent():
    ent = Entity(
        id="g:2", type=NodeType.GACHA_POOL,
        attrs={"base_rate": 0.005, "pity_threshold": 90, "max_expected_pulls": 100},
    )
    fs = SMTChecker([_GACHA_CONSTRAINT]).check(_snap(ent))
    assert fs == []


# --- M1-D7 budget: unknown/undecidable degrades to unproven, NEVER a pass ---

def test_unknown_degrades_to_unproven_never_pass():
    # Deliberately hard, genuinely undecidable-in-practice fragment: nonlinear
    # *integer* arithmetic (a^3 + b^3 == c^3, i.e. Fermat-cubes) over three
    # free bounded ("ranged") int fields. This equation has NO integer
    # solutions in the box (Fermat's Last Theorem, n=3) but z3's incomplete
    # NIA decision procedure cannot conclude that within any small timeout —
    # it must never be silently treated as "satisfied" (a pass).
    hard = Constraint(
        id="C_hard_nonlinear", kind="numeric", oracle="deterministic",
        scope=Selector(var="f", node_type="FORMULA"),
        assert_="a*a*a + b*b*b != c*c*c",
        severity="minor",
    )
    ent = Entity(
        id="f:hard", type=NodeType.FORMULA,
        attrs={
            "a": {"min": 2, "max": 99999},
            "b": {"min": 2, "max": 99999},
            "c": {"min": 2, "max": 99999},
        },
    )
    fs = SMTChecker([hard], timeout_ms=200).check(_snap(ent))
    assert len(fs) == 1
    f = fs[0]
    assert f.status == "unproven"
    assert f.status != "confirmed"  # never silently treated as a pass
    assert f.constraint_id == "C_hard_nonlinear"


def test_missing_field_degrades_to_unproven_never_pass():
    # A field the assert-expression references is entirely absent from the
    # entity's attrs: fail closed (unproven), never silently dropped/passed.
    ent = Entity(id="q:5", type=NodeType.QUEST, attrs={})
    fs = SMTChecker([_CAP_CONSTRAINT]).check(_snap(ent))
    assert len(fs) == 1
    assert fs[0].status == "unproven"


def test_z3_smoke_solver_available():
    assert z3.get_version_string()
