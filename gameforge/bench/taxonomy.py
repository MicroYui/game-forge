"""GameForge-Bench defect taxonomy (M3a Task 1 / design §2).

15 defect classes spanning the deterministic checker suite (Graph/ASP/SMT),
the economy simulator, and LLM-assisted narrative-consistency checks. Every
class carries its `oracle` (which backend is authoritative for that class)
and its `bucket` (which partition of `ReviewReport` its Bug-Detection-Rate is
reported under — deterministic and llm-assisted numbers are NEVER merged,
hard rule 3 / design §5).

Pure stdlib + `Bucket`/`DefectClass` values only — no `gameforge.agents`, no
LLM SDK (this module is part of the bench "seeded core": contract §I file
structure, plan Task 10 AST guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DefectClass(str, Enum):
    # --- structural (graph, impl@M3a Task 2) ---
    dangling_reference = "dangling_reference"
    missing_drop_source = "missing_drop_source"
    unreachable_target = "unreachable_target"
    cyclic_dependency = "cyclic_dependency"
    dead_quest = "dead_quest"
    unsatisfiable_completion = "unsatisfiable_completion"
    # --- numeric / economy (smt + sim, impl@M3a Task 3) ---
    reward_out_of_range = "reward_out_of_range"
    prob_sum_ne_1 = "prob_sum_ne_1"
    non_monotonic_curve = "non_monotonic_curve"
    gacha_expectation_violation = "gacha_expectation_violation"
    economy_collapse = "economy_collapse"
    # --- narrative (consistency / llm-assisted, impl@M3a Task 4) ---
    character_violation = "character_violation"
    spoiler = "spoiler"
    faction_violation = "faction_violation"
    uniqueness_violation = "uniqueness_violation"


class Bucket(str, Enum):
    """Which `ReviewReport` partition a class's Bug-Detection-Rate is scored
    against (design §5 — det vs llm-assisted strictly separated)."""

    deterministic = "deterministic"
    simulation = "simulation"
    llm_assisted = "llm_assisted"


@dataclass(frozen=True)
class DefectMeta:
    oracle: str  # "graph" | "asp" | "smt" | "sim" | "consistency"
    bucket: Bucket


# Design §2 table, verbatim (# | class | oracle | bucket).
CLASS_META: dict[DefectClass, DefectMeta] = {
    DefectClass.dangling_reference: DefectMeta(oracle="graph", bucket=Bucket.deterministic),
    DefectClass.missing_drop_source: DefectMeta(oracle="graph/asp", bucket=Bucket.deterministic),
    DefectClass.unreachable_target: DefectMeta(oracle="graph(+nav)", bucket=Bucket.deterministic),
    DefectClass.cyclic_dependency: DefectMeta(oracle="graph/asp", bucket=Bucket.deterministic),
    DefectClass.dead_quest: DefectMeta(oracle="graph", bucket=Bucket.deterministic),
    DefectClass.unsatisfiable_completion: DefectMeta(oracle="graph", bucket=Bucket.deterministic),
    DefectClass.reward_out_of_range: DefectMeta(oracle="smt", bucket=Bucket.deterministic),
    DefectClass.prob_sum_ne_1: DefectMeta(oracle="smt", bucket=Bucket.deterministic),
    DefectClass.non_monotonic_curve: DefectMeta(oracle="smt", bucket=Bucket.deterministic),
    DefectClass.gacha_expectation_violation: DefectMeta(oracle="smt", bucket=Bucket.deterministic),
    DefectClass.economy_collapse: DefectMeta(oracle="sim", bucket=Bucket.simulation),
    DefectClass.character_violation: DefectMeta(oracle="consistency", bucket=Bucket.llm_assisted),
    DefectClass.spoiler: DefectMeta(oracle="consistency", bucket=Bucket.llm_assisted),
    DefectClass.faction_violation: DefectMeta(oracle="consistency", bucket=Bucket.llm_assisted),
    DefectClass.uniqueness_violation: DefectMeta(oracle="consistency", bucket=Bucket.llm_assisted),
}
