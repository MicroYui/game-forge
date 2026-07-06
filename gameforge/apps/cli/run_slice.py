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
from datetime import datetime, timezone

from gameforge.apps.cli.driver import ScriptedDriver
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.lineage import Artifact, AuditRecord
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.structural import StructuralChecker
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ingestion.schema_registry import SchemaRegistry
from gameforge.spine.ir.loader import load_scenario
from gameforge.spine.versioning.store import InMemoryArtifactStore, RefStore
from gameforge.spine.versioning.version_tuple import artifact_id_for, build_version_tuple

_BLOCKING = {"critical", "major"}


def _record_lineage(snapshot, world_config, findings, seed: int) -> tuple[dict, str]:
    """Build the ir_snapshot -> config_export -> checker_run lineage chain for
    one run and record it into a call-scoped `InMemoryArtifactStore` (contract
    §5). Artifact ids are content-addressed (`artifact_id_for`), so the chain
    is identical across two runs with the same scenario + seed regardless of
    the fact that the store/refs/audit log themselves are not persisted
    beyond this call. An audit entry is appended per artifact, hash-chained
    the same way as the WORM `platform.audit.log.AuditLog` (`prev_hash`
    linking), but kept in-memory here so `run_slice` stays file-free.
    """
    store = InMemoryArtifactStore()
    refs = RefStore()
    audit: list[AuditRecord] = []

    def _put(kind: str, lineage: list[str], payload_hash: str) -> str:
        version_tuple = build_version_tuple(ir_snapshot_id=snapshot.snapshot_id, seed=seed)
        artifact_id = artifact_id_for(kind, version_tuple, payload_hash)
        store.put(
            Artifact(
                artifact_id=artifact_id,
                kind=kind,
                version_tuple=version_tuple,
                lineage=lineage,
                payload_hash=payload_hash,
                created_at=None,  # kept out of the content-addressed id (determinism)
            )
        )
        prev_hash = audit[-1].content_hash if audit else None
        seq = len(audit) + 1
        ts = datetime.now(timezone.utc).isoformat()
        content_hash = compute_snapshot_id(
            {
                "actor": "run_slice",
                "action": f"record_{kind}",
                "artifact_id": artifact_id,
                "ts": ts,
                "prev_hash": prev_hash,
            }
        )
        audit.append(
            AuditRecord(
                seq=seq,
                actor="run_slice",
                action=f"record_{kind}",
                artifact_id=artifact_id,
                ts=ts,
                content_hash=content_hash,
                prev_hash=prev_hash,
            )
        )
        refs.set("head", artifact_id)
        return artifact_id

    ir_id = _put("ir_snapshot", [], snapshot.snapshot_id)
    config_hash = compute_snapshot_id(world_config.model_dump())
    config_id = _put("config_export", [ir_id], config_hash)
    findings_hash = compute_snapshot_id({"findings": findings})
    checker_id = _put("checker_run", [config_id], findings_hash)

    artifacts = {"ir_snapshot": ir_id, "config_export": config_id, "checker_run": checker_id}
    return artifacts, refs.get("head")


def run_slice(scenario_path: str, seed: int = 0) -> dict:
    snapshot = load_scenario(scenario_path)
    world_config = snapshot_to_world(snapshot)
    env = AureusEnv(world_config)
    nav = env.nav_provider()

    findings = StructuralChecker().check(snapshot, nav=nav)
    findings_dump = [f.model_dump() for f in findings]
    blocking = [f for f in findings if f.severity in _BLOCKING]
    artifacts, head = _record_lineage(snapshot, world_config, findings_dump, seed)

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
            "artifacts": artifacts,
            "head": head,
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
        "artifacts": artifacts,
        "head": head,
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
    artifacts, head = _record_lineage(snapshot, world_config, findings_dump, seed)

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
            "artifacts": artifacts,
            "head": head,
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
        "artifacts": artifacts,
        "head": head,
    }
