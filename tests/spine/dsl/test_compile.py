"""compile(constraint) -> Checker routing (M1 Task 7).

`compile` never runs a structural/narrative `assert_` through `parse_assert`
(that's SMT/numeric-only) — it routes purely by `constraint.kind` /
`has_llm_predicate()` and a keyword inspection of the raw `assert_` text.
Three anchors:
  (a) a structural cycle constraint -> findings tagged with constraint_id,
      defect_class == "cyclic_dependency" (ASP-encodable shared class).
  (b) a numeric reward constraint -> routed to SMTChecker -> reward_out_of_range.
  (c) an llm-assisted constraint -> exactly llm-assisted/unproven Findings,
      zero deterministic Findings ever (contract §6 partition invariant).
"""

from __future__ import annotations

from gameforge.contracts.dsl import Constraint, Predicate, Selector
from gameforge.contracts.ir import Entity, EdgeType, NodeType, Relation
from gameforge.spine.dsl.compile import compile, compile_all
from gameforge.spine.ir.snapshot import Snapshot


def _snap(entities, relations=()):
    return Snapshot.from_entities_relations(list(entities), list(relations))


# --- (a) structural cycle constraint ---------------------------------------

_CYCLE_CONSTRAINT = Constraint(
    id="C_cycle", kind="structural", oracle="deterministic",
    assert_="acyclic(quest_steps)", severity="critical",
)


def _snap_with_cycle():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
        Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
        Relation(id="c", type=EdgeType.PRECEDES, src_id="s3", dst_id="s1"),
    ]
    return _snap(ents, rels)


def test_structural_constraint_compiles_and_binds_constraint_id():
    chk = compile(_CYCLE_CONSTRAINT)
    fs = chk.check(_snap_with_cycle())
    assert fs
    assert all(f.constraint_id == "C_cycle" for f in fs)
    assert all(f.defect_class == "cyclic_dependency" for f in fs)
    assert all(f.oracle_type == "deterministic" for f in fs)


def test_structural_constraint_is_silent_on_acyclic_graph():
    clean = _snap([Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)], [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
    ])
    assert compile(_CYCLE_CONSTRAINT).check(clean) == []


# --- (b) numeric reward constraint -----------------------------------------

_REWARD_CONSTRAINT = Constraint(
    id="C_cap", kind="numeric", oracle="deterministic",
    scope=Selector(var="q", node_type="QUEST"),
    assert_="reward_gold <= 80", severity="major",
)


def test_numeric_constraint_routes_to_smt():
    snap_bad_reward = _snap([Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 120})])
    fs = compile(_REWARD_CONSTRAINT).check(snap_bad_reward)
    assert any(f.defect_class == "reward_out_of_range" for f in fs)
    assert all(f.constraint_id == "C_cap" for f in fs)
    assert all(f.oracle_type == "deterministic" for f in fs)


def test_numeric_constraint_is_silent_when_satisfied():
    snap_ok_reward = _snap([Entity(id="q:1", type=NodeType.QUEST, attrs={"reward_gold": 50})])
    assert compile(_REWARD_CONSTRAINT).check(snap_ok_reward) == []


# --- (c) llm-assisted constraint -------------------------------------------

_LLM_CONSTRAINT = Constraint(
    id="C_sem", kind="narrative", oracle="llm-assisted",
    predicates=[Predicate(expr="semantically_reveals_identity(d, x)", oracle="llm-assisted")],
    assert_="chapter >= 3", severity="critical",
)


def test_llm_assisted_constraint_does_not_produce_deterministic_finding():
    snap_any = _snap([Entity(id="q:1", type=NodeType.QUEST, attrs={"chapter": 1})])
    fs = compile(_LLM_CONSTRAINT).check(snap_any)
    assert fs
    assert all(f.oracle_type == "llm-assisted" and f.status == "unproven" for f in fs)
    assert all(f.source == "llm" for f in fs)
    assert all(f.constraint_id == "C_sem" for f in fs)
    assert not any(f.oracle_type == "deterministic" for f in fs)


def test_llm_assisted_constraint_yields_exactly_one_finding():
    snap_any = _snap([])
    fs = compile(_LLM_CONSTRAINT).check(snap_any)
    assert len(fs) == 1
    assert "M2" in fs[0].message


# --- compile_all -------------------------------------------------------------

def test_compile_all_routes_each_constraint_independently():
    checkers = compile_all([_CYCLE_CONSTRAINT, _REWARD_CONSTRAINT, _LLM_CONSTRAINT])
    assert len(checkers) == 3
    snap = _snap_with_cycle()
    all_fs = [f for chk in checkers for f in chk.check(snap)]
    ids = {f.constraint_id for f in all_fs}
    # cycle constraint fires on this snapshot; reward/llm constraints don't
    # need to find anything on a snapshot with no QUEST reward_gold attr, but
    # the llm-routed one always emits its single placeholder regardless.
    assert "C_cycle" in ids
    assert "C_sem" in ids
