"""run_review (M1 Task 11): scenario dir + constraints dir -> ReviewReport.

Orchestration only (composes spine + game per the apps-layer convention —
see `run_slice.py`): read the CSV workbook, ingest it straight to Spec-IR via
`AureusCsvAdapter.to_ir`, compile every `Constraint` found under
`constraints_path` into a Checker, run the M1 economy simulator, and fan
everything into one `ReviewReport` via `build_review_report`.

Deliberately bypasses `SchemaRegistry.validate` (unlike
`run_slice_workbook`): several M1 defect scenarios are intentionally
schema-invalid on purpose (e.g. `dead_quest`'s blank required `giver` cell),
and this review path exists precisely to let the deterministic checkers
(Graph/ASP/SMT) — not `SchemaRegistry` — be the oracle that decides such a
scenario is broken.

`unreachable_target` needs a `NavProvider` (`GraphChecker._unreachable_target`
is a silent no-op without one); this CLI path never builds an Aureus world,
so `nav` stays `None` and that one defect class is out of scope here — the
other 9 IR-only classes (5 GraphChecker/ASPChecker structural + 4 SMT
numeric) don't need spatial ground truth at all.
"""

from __future__ import annotations

import glob
import json
import os

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.review import ReviewReport
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

_N_AGENTS = 50
_N_TICKS = 200


def run_review(scenario_dir: str, constraints_path: str, seed: int = 0) -> ReviewReport:
    with open(os.path.join(scenario_dir, "format_schema.json"), encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(scenario_dir, schema)
    snapshot = AureusCsvAdapter().to_ir(workbook, file_ref=scenario_dir)

    constraints: list[Constraint] = []
    for yaml_path in sorted(glob.glob(os.path.join(constraints_path, "*.yaml"))):
        with open(yaml_path, encoding="utf-8") as fh:
            constraints.extend(Constraint.from_yaml(fh.read()))
    checkers = compile_all(constraints)

    economy_model = EconomyModel.from_snapshot(snapshot)
    sim_result = EconomySimulator().run(
        economy_model, seed=seed, n_agents=_N_AGENTS, n_ticks=_N_TICKS
    )
    sim_findings = to_findings(sim_result, snapshot.snapshot_id, model=economy_model)

    return build_review_report(snapshot, checkers, sim_findings=sim_findings)
