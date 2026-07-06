"""`python -m gameforge.apps.cli` — run a vertical slice and print the result.

Accepts either:
- a YAML scenario file (M0a acceptance demo, e.g. `scenarios/caravan.yaml`) —
  runs `run_slice` (direct IR loader, talk->collect->turn_in).
- a CSV scenario directory (M0b acceptance demo, e.g. `scenarios/outpost`) —
  runs `run_slice_workbook` (Schema Registry + Aureus adapter round trip,
  talk->collect->fight->turn_in across combat/economy/gacha/quest).

Dispatch is by path kind: a directory is treated as a CSV workbook, anything
else as a YAML scenario file.
"""

from __future__ import annotations

import json
import os
import sys

from gameforge.apps.cli.run_slice import run_slice, run_slice_workbook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
