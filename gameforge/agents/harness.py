"""M2a-part2 Task 8 harness: repair-corpus Fix Pass Rate + search efficiency +
runtime coverage, plus the RECORD/REPLAY entrypoint.

Each defect scenario is loaded EXACTLY the way `apps.cli.run_review` loads one
(read the CSV workbook -> `AureusCsvAdapter.to_ir` -> `Snapshot`;
`Constraint.from_yaml` over the constraints dir -> `compile_all`). We then find
the deterministic/simulation `Finding` whose `defect_class == basename(dir)`,
run verifier-guided `repair_search` over it, and aggregate an honest
`RepairCorpusResult`.

Pass/fail is the DETERMINISTIC verifier's `PatchDraft.passed_verification`
(spine checkers + M1 economy sim + real Aureus regression) — never a model
claim. `runtime_vetted` counts only passes whose Aureus regression gate actually
built + stepped a world (`VerifyResult.regression_ran`): a skipped gate is
honestly NOT counted as vetted.

RECORD hits the live gateway (gated on `GAMEFORGE_LLM_LIVE=1` AND
`GAMEFORGE_LLM_KEY` present) and writes `cassettes/`; REPLAY reads them back with
zero live calls. The deterministic trunk (`spine`) is never touched — only
`agents.repair.*` reach the router (hard rule 4 / import-linter).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass

from gameforge.agents.repair.search import repair_search
from gameforge.agents.repair.verify import verify_patch
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.review import ReviewReport
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

# --- corpus layout ---------------------------------------------------------
# Dir basename IS the target defect_class to repair (the 10 injected-defect
# scenarios; `clean` is the FP baseline and is deliberately NOT in the corpus).
_DEFECT_CLASSES = [
    "cyclic_dependency",
    "dangling_reference",
    "dead_quest",
    "economy_collapse",
    "gacha_expectation_violation",
    "missing_drop_source",
    "non_monotonic_curve",
    "prob_sum_ne_1",
    "reward_out_of_range",
    "unsatisfiable_completion",
]
_DEFECTS_ROOT = "scenarios/defects"
_CONSTRAINTS_PATH = "scenarios/constraints"
_CASSETTES_ROOT = "cassettes"
_AGENTS_SCENARIOS = "scenarios/agents"

# Economy-sim budget for the target-finding search — matches `run_review`
# (50 agents / 200 ticks) so an `economy_collapse` scenario surfaces its
# simulation Finding here exactly as the canonical review path does.
_SIM_SEED = 0
_SIM_N_AGENTS = 50
_SIM_N_TICKS = 200


def default_scenario_dirs() -> list[str]:
    """The 10 injected-defect scenario dirs (the repair corpus)."""
    return [os.path.join(_DEFECTS_ROOT, c) for c in _DEFECT_CLASSES]


# --- scenario loading (replicates apps.cli.run_review's intermediates) ------
def load_scenario(scenario_dir: str, constraints_path: str) -> tuple[Snapshot, list[Checker]]:
    """Load one scenario dir into its `(snapshot, checkers)` intermediates.

    Same pipeline as `run_review` (which returns only the final `ReviewReport`);
    the harness needs the snapshot + compiled checkers to drive `repair_search`,
    so the loading is replicated here over the same spine helpers.
    """
    with open(os.path.join(scenario_dir, "format_schema.json"), encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(scenario_dir, schema)
    snapshot = AureusCsvAdapter().to_ir(workbook, file_ref=scenario_dir)

    constraints: list[Constraint] = []
    for yaml_path in sorted(glob.glob(os.path.join(constraints_path, "*.yaml"))):
        with open(yaml_path, encoding="utf-8") as fh:
            constraints.extend(Constraint.from_yaml(fh.read()))
    checkers = compile_all(constraints)
    return snapshot, checkers


def _sim_findings(snapshot: Snapshot) -> list[Finding]:
    """Economy-sim Findings for the review (so a `simulation`-oracle target such
    as `economy_collapse` is discoverable). An un-modelable economy yields `[]`
    — never a spurious target."""
    try:
        model = EconomyModel.from_snapshot(snapshot)
        if not model.sources and not model.sinks:
            return []
        result = EconomySimulator().run(
            model, seed=_SIM_SEED, n_agents=_SIM_N_AGENTS, n_ticks=_SIM_N_TICKS
        )
        return to_findings(result, snapshot.snapshot_id)
    except Exception:  # noqa: BLE001 — an un-modelable economy is not a target
        return []


def _review(snapshot: Snapshot, checkers: list[Checker]) -> ReviewReport:
    return build_review_report(
        snapshot, checkers, sim_findings=tuple(_sim_findings(snapshot))
    )


def _find_target_finding(report: ReviewReport, defect_class: str) -> Finding | None:
    """The first PROVEN Finding (deterministic + simulation + unproven) whose
    `defect_class` matches the scenario's injected defect. llm-assisted findings
    are never a repair target (they aren't proven defects)."""
    for f in (
        report.deterministic_findings
        + report.simulation_findings
        + report.unproven_findings
    ):
        if f.defect_class == defect_class:
            return f
    return None


# --- result aggregate ------------------------------------------------------
@dataclass
class RepairCorpusResult:
    attempted: int
    passed: int
    fix_pass_rate: float
    per_scenario: list[dict]
    avg_steps: float
    first_pass_rate: float
    runtime_vetted: int


def run_repair_corpus(
    scenario_dirs: list[str],
    constraints_path: str,
    router: ModelRouter,
    *,
    max_steps: int = 4,
) -> RepairCorpusResult:
    """Run verifier-guided repair over every scenario dir and aggregate.

    A scenario PASSES iff `repair_search(...).passed_verification is True` — the
    verifier's sound `ok` (target genuinely resolved across det+sim+unproven,
    no new deterministic finding, content preserved, no regression). Nothing here
    re-decides pass/fail; the harness only measures and reports.
    """
    per_scenario: list[dict] = []
    attempted = 0
    passed = 0
    passed_steps: list[int] = []
    first_pass = 0
    runtime_vetted = 0

    for scenario_dir in scenario_dirs:
        defect_class = os.path.basename(os.path.normpath(scenario_dir))
        attempted += 1

        snapshot, checkers = load_scenario(scenario_dir, constraints_path)
        report = _review(snapshot, checkers)
        target = _find_target_finding(report, defect_class)

        if target is None:
            # The review never produced the injected defect: there is nothing to
            # repair. Honestly recorded as attempted + failed (not skipped from
            # the denominator) — a corpus that can't even reproduce its own
            # defect should drag the Fix Pass Rate down, not vanish.
            per_scenario.append(
                {
                    "scenario": scenario_dir,
                    "defect_class": defect_class,
                    "passed": False,
                    "search_steps": 0,
                    "regression_vetted": False,
                    "note": "target defect_class not produced by review — cannot repair",
                }
            )
            continue

        draft = repair_search(target, snapshot, checkers, router, max_steps=max_steps)
        ok = draft.passed_verification is True

        regression_vetted = False
        note = ""
        if ok:
            passed += 1
            passed_steps.append(draft.search_steps)
            if draft.search_steps == 1:
                first_pass += 1
            # Re-verify the returned patch ONCE to read regression coverage
            # honestly: `regression_ran` is True only when the Aureus gate
            # actually built + reset/stepped a world. A skipped gate leaves it
            # False and is NOT counted as vetted (no runtime-coverage inflation).
            patched = apply_patch(snapshot, draft.patch)
            vr = verify_patch(snapshot, patched, checkers, defect_class)
            regression_vetted = vr.regression_ran
            if regression_vetted:
                runtime_vetted += 1
        else:
            note = "no verifier-passing patch found within max_steps"

        per_scenario.append(
            {
                "scenario": scenario_dir,
                "defect_class": defect_class,
                "passed": ok,
                "search_steps": draft.search_steps,
                "regression_vetted": regression_vetted,
                "note": note,
            }
        )

    fix_pass_rate = passed / attempted if attempted else 0.0
    first_pass_rate = first_pass / attempted if attempted else 0.0
    avg_steps = (sum(passed_steps) / len(passed_steps)) if passed_steps else 0.0

    return RepairCorpusResult(
        attempted=attempted,
        passed=passed,
        fix_pass_rate=fix_pass_rate,
        per_scenario=per_scenario,
        avg_steps=avg_steps,
        first_pass_rate=first_pass_rate,
        runtime_vetted=runtime_vetted,
    )


# --- router construction ---------------------------------------------------
class _NoLiveTransport:
    """REPLAY must never touch the network: a cassette miss raises
    `CassetteReplayMiss` inside the router BEFORE any transport call, so this
    `complete` should never run. If it ever does, fail loud rather than silently
    reaching the gateway."""

    def complete(self, req):  # noqa: ANN001, ANN201 — Protocol shape only
        raise RuntimeError(
            "REPLAY router attempted a live transport call — this is a bug "
            "(a cassette miss must surface as CassetteReplayMiss, never a live call)"
        )


def replay_router(cassettes_root: str = _CASSETTES_ROOT) -> ModelRouter:
    """REPLAY router over `cassettes/` — zero live calls (CI / acceptance)."""
    return ModelRouter(
        _NoLiveTransport(), CassetteStore(cassettes_root), mode=RouterMode.REPLAY
    )


def record_router(cassettes_root: str = _CASSETTES_ROOT) -> ModelRouter:
    """RECORD router over the live gateway — the ONLY place a live call happens.

    Imported lazily so importing this module (e.g. in REPLAY tests) never pulls
    in the HTTP transport or requires a key.
    """
    from gameforge.runtime.model_router.anthropic_transport import (
        AnthropicMessagesTransport,
    )
    from gameforge.runtime.secrets.env import get_llm_key

    return ModelRouter(
        AnthropicMessagesTransport(base_url="http://localhost:4141", api_key=get_llm_key()),
        CassetteStore(cassettes_root),
        mode=RouterMode.RECORD,
    )


# --- reporting -------------------------------------------------------------
def format_result(result: RepairCorpusResult) -> str:
    lines = [
        "=== Repair Corpus Result ===",
        f"attempted:       {result.attempted}",
        f"passed:          {result.passed}",
        f"fix_pass_rate:   {result.fix_pass_rate:.1%}",
        f"first_pass_rate: {result.first_pass_rate:.1%}",
        f"avg_steps:       {result.avg_steps:.2f}",
        f"runtime_vetted:  {result.runtime_vetted}",
        "per-scenario:",
    ]
    for row in result.per_scenario:
        mark = "PASS" if row["passed"] else "FAIL"
        note = f"  ({row['note']})" if row.get("note") else ""
        lines.append(
            f"  [{mark}] {row['defect_class']:<28} "
            f"steps={row['search_steps']} "
            f"regression_vetted={row['regression_vetted']}{note}"
        )
    return "\n".join(lines)


# --- agent-scenario samples (recorded alongside the repair corpus) ----------
def _record_agent_samples(router: ModelRouter) -> None:
    """Exercise the extraction / consistency / generation agents on the
    `scenarios/agents/` samples so their cassettes are recorded too. Best-effort:
    a sample failing here prints a warning but never aborts the corpus recording.
    """
    # Extraction: design-doc snippet -> proposed compilable constraints.
    try:
        from gameforge.agents.extraction.proposer import ExtractionProposer
        from gameforge.contracts.agent_io import DesignDocInput

        doc_path = os.path.join(_AGENTS_SCENARIOS, "extraction_doc.md")
        with open(doc_path, encoding="utf-8") as fh:
            doc_text = fh.read()
        res = ExtractionProposer().run(
            DesignDocInput(doc_text=doc_text, doc_version="v3"), router
        )
        print(f"[record] extraction: {len(res.produced.get('proposals', []))} proposals")
    except Exception as exc:  # noqa: BLE001 — sample recording is best-effort
        print(f"[record] extraction sample failed: {exc}", file=sys.stderr)

    # Consistency: dialogue + narrative constraint ids -> quorum-voted hints.
    try:
        from gameforge.agents.consistency.assistant import ConsistencyAssistant
        from gameforge.contracts.agent_io import DialogueNarrativeInput

        dialogue_path = os.path.join(_AGENTS_SCENARIOS, "dialogue.txt")
        narrative_path = os.path.join(_AGENTS_SCENARIOS, "narrative.yaml")
        with open(dialogue_path, encoding="utf-8") as fh:
            dialogue = fh.read()
        with open(narrative_path, encoding="utf-8") as fh:
            narrative_ids = [c.id for c in Constraint.from_yaml(fh.read())]
        res = ConsistencyAssistant().run(
            DialogueNarrativeInput(dialogue=dialogue, narrative_constraint_ids=narrative_ids),
            router,
        )
        print(f"[record] consistency: {len(res.produced.get('hints', []))} quorum hints")
    except Exception as exc:  # noqa: BLE001 — sample recording is best-effort
        print(f"[record] consistency sample failed: {exc}", file=sys.stderr)

    # Generation: benign design goal grounded on the clean baseline -> gated proposal.
    try:
        from gameforge.agents.generation.generator import ContentGenerator
        from gameforge.contracts.agent_io import DesignGoalInput

        snapshot, checkers = load_scenario(
            os.path.join(_DEFECTS_ROOT, "clean"), _CONSTRAINTS_PATH
        )
        res = ContentGenerator(snapshot, checkers).run(
            DesignGoalInput(
                goal="Add a small new side quest that awards 40 gold, staying within all caps.",
                grounding_snapshot_id=snapshot.snapshot_id,
            ),
            router,
        )
        print(f"[record] generation: passed_gate={res.produced['proposal']['passed_gate']}")
    except Exception as exc:  # noqa: BLE001 — sample recording is best-effort
        print(f"[record] generation sample failed: {exc}", file=sys.stderr)


# --- CLI -------------------------------------------------------------------
def _run_record() -> int:
    if os.environ.get("GAMEFORGE_LLM_LIVE") != "1":
        print(
            "RECORD refused: live LLM calls are gated. Re-run with "
            "GAMEFORGE_LLM_LIVE=1 (and GAMEFORGE_LLM_KEY set) to record cassettes.",
            file=sys.stderr,
        )
        return 2
    try:
        from gameforge.runtime.secrets.env import get_llm_key

        get_llm_key()  # presence check only — never printed, never written to disk
    except RuntimeError as exc:
        print(f"RECORD refused: {exc}", file=sys.stderr)
        return 2

    router = record_router()
    print("Recording repair corpus (live gateway)…")
    result = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS_PATH, router)
    print(format_result(result))
    _record_agent_samples(router)
    if result.fix_pass_rate < 0.70:
        print(
            f"\nWARNING: fix_pass_rate {result.fix_pass_rate:.1%} < 70% — iterate the "
            "repair prompts / max_steps and re-record (adjust the agent, never the "
            "threshold).",
            file=sys.stderr,
        )
    return 0


def _run_replay() -> int:
    router = replay_router()
    result = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS_PATH, router)
    print(format_result(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gameforge.agents.harness",
        description="GameForge M2a-part2 repair-corpus harness (record / replay).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--record",
        action="store_true",
        help="RECORD mode: live gateway calls (gated on GAMEFORGE_LLM_LIVE=1 + key).",
    )
    group.add_argument(
        "--replay",
        action="store_true",
        help="REPLAY mode (default): read cassettes/, zero live calls.",
    )
    args = parser.parse_args(argv)
    if args.record:
        return _run_record()
    return _run_replay()


if __name__ == "__main__":
    raise SystemExit(main())
