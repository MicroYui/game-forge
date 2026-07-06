"""End-to-end M0a/M0b slice: config → IR → checker gate → Aureus run.

Orchestration lives in apps (the only layer allowed to compose spine + game).
Data flow: load_scenario → StructuralChecker (gate) → snapshot_to_world →
AureusEnv → ScriptedDriver drives talk→collect→turn_in to completion.

`run_slice` (M0a) sources config from hand-written scenario YAML via the
direct loader. `run_slice_workbook` (M0b) sources config from a typed CSV
workbook via the Schema Registry + Aureus adapter round trip, and drives the
richer talk→collect→fight→turn_in (+ economy/gacha) four-system chain.
"""

from __future__ import annotations

import json
import os

from gameforge.apps.cli.driver import ScriptedDriver
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.structural import StructuralChecker
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ingestion.schema_registry import SchemaRegistry
from gameforge.spine.ir.loader import load_scenario

_BLOCKING = {"critical", "major"}


def run_slice(scenario_path: str, seed: int = 0) -> dict:
    snapshot = load_scenario(scenario_path)
    world_config = snapshot_to_world(snapshot)
    env = AureusEnv(world_config)
    nav = env.nav_provider()

    findings = StructuralChecker().check(snapshot, nav=nav)
    findings_dump = [f.model_dump() for f in findings]
    blocking = [f for f in findings if f.severity in _BLOCKING]

    env.reset(world_config.scenario.scenario_id, int(seed))
    if blocking:
        return {
            "completed": False,
            "blocked_by_checker": True,
            "findings": findings_dump,
            "trajectory": [],
            "final_hash": env.state_hash(),
            "ticks": env.observe().tick,
            "snapshot_id": snapshot.snapshot_id,
        }

    result = ScriptedDriver(world_config).run(env)
    return {
        "completed": result["completed"],
        "blocked_by_checker": False,
        "findings": findings_dump,
        "trajectory": result["trajectory"],
        "final_hash": result["final_hash"],
        "ticks": result["ticks"],
        "snapshot_id": snapshot.snapshot_id,
    }


def run_slice_workbook(dir_path: str, seed: int = 0) -> dict:
    with open(os.path.join(dir_path, "format_schema.json"), "r", encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))

    workbook = read_workbook(dir_path, schema)
    schema_errors = SchemaRegistry().validate(schema, workbook)
    if schema_errors:
        raise ValueError(
            f"scenario {dir_path!r} failed schema validation: "
            f"{[e.model_dump() for e in schema_errors]}"
        )

    snapshot = AureusCsvAdapter().to_ir(workbook, file_ref=dir_path)
    world_config = snapshot_to_world(snapshot)
    env = AureusEnv(world_config)
    nav = env.nav_provider()

    findings = StructuralChecker().check(snapshot, nav=nav)
    findings_dump = [f.model_dump() for f in findings]
    blocking = [f for f in findings if f.severity in _BLOCKING]

    env.reset(world_config.scenario.scenario_id, int(seed))
    if blocking:
        return {
            "completed": False,
            "blocked_by_checker": True,
            "findings": findings_dump,
            "trajectory": [],
            "final_hash": env.state_hash(),
            "ticks": env.observe().tick,
            "snapshot_id": snapshot.snapshot_id,
            "systems_exercised": [],
        }

    result = ScriptedDriver(world_config).run(env)
    return {
        "completed": result["completed"],
        "blocked_by_checker": False,
        "findings": findings_dump,
        "trajectory": result["trajectory"],
        "final_hash": result["final_hash"],
        "ticks": result["ticks"],
        "snapshot_id": snapshot.snapshot_id,
        "systems_exercised": result["systems_exercised"],
    }
