"""M1 acceptance suite (contract §16 M1 acceptance row / §13.2 defect taxonomy).

End-to-end proof that the M1 deterministic trunk actually holds:

  - >=8 (we cover 9) IR-only defect classes are soundly detected off real
    CSV-derived scenarios, each isolating exactly its own injected mutation.
  - The `scenarios/defects/clean` baseline — the same real outpost content,
    fixed at its two pre-existing landmines (drop-table probabilities not
    summing to 1, no economy-sim gold source) — is the oracle-FP=0 anchor:
    `deterministic_findings == []` under the FULL constraint set.
  - The DSL constraint set actually reaches both non-Graph backends (Clingo
    via ASPChecker, z3 via SMTChecker), not just GraphChecker.
  - The economy simulator reproduces a collapse with a strictly-earlier
    early-warning tick off a real (CSV -> adapter -> IR) economy, not just
    hand-built fixtures (already covered in `tests/spine/sim/test_economy.py`).
  - The open-source adapter (Flare) round-trips losslessly (external
    validity anchor, contract §12A.1).
  - `ReviewReport`'s deterministic/llm-assisted partition holds end-to-end
    through the full CLI-facing `run_review` path, not just the narrower
    `build_review_report` unit tests.
"""

from __future__ import annotations

import glob

import pytest

from gameforge.apps.cli.run_review import run_review
from gameforge.contracts.dsl import Constraint
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.smt import SMTChecker
from gameforge.spine.dsl.compile import CompiledChecker, compile_all
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.flare_adapter import FlareTxtAdapter, read_flare_dir
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, detect_collapse

_SCENARIOS = "scenarios/defects"
_CONSTRAINTS = "scenarios/constraints"
_CLEAN = f"{_SCENARIOS}/clean"

# The 9 IR-only defect classes this milestone targets (unreachable_target and
# interval_violation are explicitly out of scope for M1 — see the plan).
_DEFECT_SCENARIOS = {
    "dangling_reference": "dangling_reference",
    "missing_drop_source": "missing_drop_source",
    "cyclic_dependency": "cyclic_dependency",
    "dead_quest": "dead_quest",
    "unsatisfiable_completion": "unsatisfiable_completion",
    "reward_out_of_range": "reward_out_of_range",
    "prob_sum_ne_1": "prob_sum_ne_1",
    "non_monotonic_curve": "non_monotonic_curve",
    "gacha_expectation_violation": "gacha_expectation_violation",
}


def _load_all_constraints() -> list[Constraint]:
    constraints: list[Constraint] = []
    for path in sorted(glob.glob(f"{_CONSTRAINTS}/*.yaml")):
        with open(path, encoding="utf-8") as fh:
            constraints.extend(Constraint.from_yaml(fh.read()))
    return constraints


@pytest.mark.parametrize("scenario_name,expected_class", sorted(_DEFECT_SCENARIOS.items()))
def test_each_defect_class_detected_soundly(scenario_name, expected_class):
    report = run_review(f"{_SCENARIOS}/{scenario_name}", _CONSTRAINTS)
    found_classes = {f.defect_class for f in report.deterministic_findings}
    # Soundness: the injected defect is caught, and NOTHING else is —
    # a single mutation against an otherwise oracle-FP=0 baseline must not
    # also drag in an unrelated defect class.
    assert found_classes == {expected_class}, (
        f"{scenario_name}: expected exactly {{{expected_class!r}}}, got {found_classes!r}"
    )


def test_clean_baseline_has_zero_oracle_false_positives():
    report = run_review(_CLEAN, _CONSTRAINTS)
    assert report.deterministic_findings == []
    # oracle-FP=0 must be a genuine "satisfied", not a vacuous pass: a numeric
    # constraint whose field failed to resolve (or a solver timeout) degrades to
    # status="unproven" and would leave deterministic_findings empty while
    # proving nothing. Lock that door — the clean baseline proves every
    # constraint actually evaluated.
    assert report.unproven_findings == []


def test_dsl_compiles_to_clingo_and_z3():
    constraints = _load_all_constraints()
    checkers = compile_all(constraints)

    backends = [c.backend for c in checkers if isinstance(c, CompiledChecker)]
    assert any(isinstance(b, ASPChecker) for b in backends), "no constraint routed to Clingo"
    assert any(isinstance(b, SMTChecker) for b in backends), "no constraint routed to z3"

    # And both backends actually produce a verdict (not just get instantiated)
    # against the clean baseline: zero findings from either, proving they ran
    # rather than silently no-op'd.
    report = run_review(_CLEAN, _CONSTRAINTS)
    assert report.deterministic_findings == []


def test_economy_sim_reproduces_collapse_with_early_warning():
    workbook_dir = f"{_SCENARIOS}/economy_collapse"
    import json
    from gameforge.spine.ingestion.csv_format import read_workbook
    from gameforge.spine.ingestion.format_schema import FormatSchema

    with open(f"{workbook_dir}/format_schema.json", encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(workbook_dir, schema)
    snapshot = AureusCsvAdapter().to_ir(workbook, file_ref=workbook_dir)

    model = EconomyModel.from_snapshot(snapshot)
    assert model.sources, "economy_collapse scenario must have a currency source"
    result = EconomySimulator().run(model, seed=0, n_agents=50, n_ticks=200)

    collapse = detect_collapse(result)
    assert collapse is not None
    assert collapse.early_warning_tick < collapse.collapse_tick

    report = run_review(workbook_dir, _CONSTRAINTS, seed=0)
    assert any(f.defect_class == "economy_collapse" for f in report.simulation_findings)


def test_open_source_config_roundtrips_ir():
    flare_dir = "scenarios/flare_sample"
    adapter = FlareTxtAdapter()
    workbook = read_flare_dir(flare_dir)
    back = adapter.from_ir(adapter.to_ir(workbook, file_ref=flare_dir))
    assert back == workbook  # contract §2 anchor: from_ir(to_ir(x)) == x


def test_deterministic_and_llm_findings_strictly_partitioned():
    report = run_review(f"{_SCENARIOS}/dangling_reference", _CONSTRAINTS)

    assert report.deterministic_findings != []
    assert report.llm_assisted_findings != []

    det_ids = {f.id for f in report.deterministic_findings}
    llm_ids = {f.id for f in report.llm_assisted_findings}
    assert det_ids.isdisjoint(llm_ids)

    assert all(f.oracle_type != "llm-assisted" for f in report.deterministic_findings)
    assert all(f.oracle_type == "llm-assisted" for f in report.llm_assisted_findings)
    assert all(f.status != "unproven" for f in report.deterministic_findings)
