"""`python -m gameforge.apps.cli` — run the M0a vertical slice and print the result."""

from __future__ import annotations

import json
import sys

from gameforge.apps.cli.run_slice import run_slice


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else "scenarios/caravan.yaml"
    seed = int(argv[1]) if len(argv) > 1 else 0
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
