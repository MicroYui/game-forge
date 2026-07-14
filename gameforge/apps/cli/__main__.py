"""`python -m gameforge.apps.cli` — run a vertical slice and print the result.

Accepts either:
- a YAML scenario file (M0a acceptance demo, e.g. `scenarios/caravan.yaml`) —
  runs `run_slice` (direct IR loader, talk->collect->turn_in).
- a CSV scenario directory (M0b acceptance demo, e.g. `scenarios/outpost`) —
  runs `run_slice_workbook` (Schema Registry + Aureus adapter round trip,
  talk->collect->fight->turn_in across combat/economy/gacha/quest).
- `review <scenario_dir> <constraints_dir> [seed]` (M1 acceptance demo) —
  runs `run_review` (Graph/ASP/SMT checkers + economy sim -> ReviewReport)
  and prints the deterministic/llm-assisted/simulation/unproven bucket
  counts. Exits non-zero iff any deterministic (oracle-proven) defect was
  found — the only bucket that gates soundly, per contract §6.
- `identity bootstrap --display-name ... --login-name ...` (M4c) — calls the
  trusted identity bootstrap platform service through one UnitOfWork.

Dispatch is by path kind: a directory is treated as a CSV workbook, anything
else as a YAML scenario file.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

from gameforge.apps.cli.run_review import run_review
from gameforge.apps.cli.run_slice import run_slice, run_slice_workbook


def main(
    argv: list[str] | None = None,
    *,
    identity_password_reader: Callable[[str], str] | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "identity":
        from gameforge.apps.identity_cli import main as identity_main

        if identity_password_reader is None:
            return identity_main(argv[1:])
        return identity_main(argv[1:], password_reader=identity_password_reader)

    if argv and argv[0] == "review":
        scenario_dir, constraints_dir = argv[1], argv[2]
        seed = int(argv[3]) if len(argv) > 3 else 0
        report = run_review(scenario_dir, constraints_dir, seed=seed)
        summary = {
            "scenario": scenario_dir,
            "constraints": constraints_dir,
            "seed": seed,
            "snapshot_id": report.snapshot_id,
            "deterministic_findings": len(report.deterministic_findings),
            "llm_assisted_findings": len(report.llm_assisted_findings),
            "simulation_findings": len(report.simulation_findings),
            "unproven_findings": len(report.unproven_findings),
            "by_defect_class": [d.model_dump() for d in report.by_defect_class],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if not report.deterministic_findings else 1

    path = argv[0] if argv else "scenarios/caravan.yaml"
    seed = int(argv[1]) if len(argv) > 1 else 0

    if os.path.isdir(path):
        out = run_slice_workbook(path, seed=seed)
        summary = {
            "scenario": path,
            "seed": seed,
            "completed": out["completed"],
            "blocked_by_checker": out["blocked_by_checker"],
            "ticks": out["ticks"],
            "num_findings": len(out["findings"]),
            "snapshot_id": out["snapshot_id"],
            "final_hash": out["final_hash"],
            "systems_exercised": sorted(out["systems_exercised"]),
        }
    else:
        out = run_slice(path, seed=seed)
        summary = {
            "scenario": path,
            "seed": seed,
            "completed": out["completed"],
            "blocked_by_checker": out["blocked_by_checker"],
            "ticks": out["ticks"],
            "num_findings": len(out["findings"]),
            "snapshot_id": out["snapshot_id"],
            "final_hash": out["final_hash"],
        }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if out["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
