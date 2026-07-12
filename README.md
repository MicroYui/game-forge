# GameForge

**Game-content correctness compiler + production-grade agent workbench.** Build a
versionable **Design-Spec IR** (knowledge graph + typed constraints) from design
docs / config tables → validate with **deterministic checkers + economy simulation**
(decidable, *not* LLM-judging) → use **bounded LLM agents** for extraction proposals,
defect triage, and repair drafting → close the loop with a **Playtest Agent** inside a
real runnable reference game **Aureus** — observable, reproducible, auditable, human-approved.

See `docs/superpowers/specs/` for the PRD and foundational contracts (single source of truth).

## Status

| Milestone | Theme | Status |
|---|---|---|
| **M0a** | Shortest vertical slice: contracts + IR core + canonical snapshot + Aureus minimal kernel (quest + grid nav) + minimal checker + a 3-step quest chain | ✅ acceptance passing |
| **M0b** | Aureus combat/economy/gacha; Schema Registry + Aureus CSV adapter round-trip; version/lineage/audit skeleton; Alembic DB migrations | ✅ acceptance passing |
| **M1** | Graph/ASP/SMT checker suite; DSL→checker compiler; economy simulation; open-source game adapter; Finding/Patch | ✅ acceptance passing |
| **M2a-part1** | Model Router (RECORD/REPLAY/PASSTHROUGH) + Cassette store + deterministic agent orchestration — foundations only | ✅ acceptance passing |
| **M2a-part2** | 6 bounded LLM agent roles (extraction/triage/repair/consistency/generation) + verifier-guided repair search; Fix Pass Rate 90% | ✅ acceptance passing |
| **M2b-1** | Playtest agent core (state abstraction + planner/executor + verifier-grounding + reflection + main loop) + regression harness (completion rate + Wilson CI + random baseline) + planner/executor ablation | ✅ acceptance passing (REPLAY/scripted smoke) |
| **M2b-2** | MemTrace episodic/transition/skill memory + deterministic recall, compactor comparison, and consistency quorum | ✅ acceptance passing |
| Pre-M4: economy sink adapter | Plumb SELLS price/currency/buy_prob so the economy sim models real gold sinks from CSV; `economy_collapse` becomes economically fixable → **repair Fix Pass Rate 9/10 → 10/10** | ✅ acceptance passing |
| Pre-M4: core contract corrections | Exact-base Patch rejection, producer-to-product `DROPS_FROM`, stable repair request identity, active `gpt-5.6-sol` repair/generation evidence, and checkout-stable benchmark provenance | ✅ `5fdfb32..cc0fbc4`; repair double-REPLAY **10/10**, full gate **962 passed, 1 skipped**, 7 import contracts kept |
| **M3** | GameForge-Bench seeded corpus, complete metrics, real non-injected open-source defect corpus, and Eval view | 🔄 incomplete: Flare B0A returned terminal `insufficient_evidence`; Endless Sky remains `awaiting_human_evidence`; PRD §13.3/§16 remain unmet |
| **M4** | Production hardening: observability/cost, lineage/rollback/audit, RBAC/approval, and full React console | ⬜ not started; blocked by the pre-M4 gates |

## M3 external-validity status

The Flare B0A mining harness and both hash-bound human reviews completed successfully.
The frozen expanded universe contains 526 candidates, but adjudication found only 7 of
the required 8 independent proposed fix groups: 10 proposed cases across all 4 required
classes, with `qualified_candidate=0` and `accepted=0`. The terminal decision is
`insufficient_evidence` with `next_action=stop_flare_heavy_investment`.

This is a valid negative investment-gate result, not M3 acceptance. B0B, Corpus Freeze,
and M3d-1 through M3d-4 were not entered; no Flare quest/loot reader expansion is
authorized.

The source-neutral replacement harness preserves the frozen Flare surface while binding
new discovery to the exact registered tool commit and profile bytes. Two code-review
passes found and fixed three generic replay defects: selected candidates were not
contractually limited to registered direct matches; a valid recursive external revert
lineage could crash adjudication; and a selected revert inside an equivalence lineage
component had mutually exclusive disposition requirements. Endless Sky discovery was
rebuilt from clean registration anchor
`687f36fb6ab499d3667fe43429fec4a25132c97a` and replayed byte-for-byte in two
independent temporary directories. The registered range produces 610 matched and 562
config-only candidates; the mechanical first-80 contains 75 config-only commits and is
bound by candidate-universe SHA-256
`f22981b17b43e02caaa494193e6a4b8cd92bbc0c312f9d5f1db249da7365793f`.

The complete non-approving review package remains `awaiting_human_evidence`: it contains
no dispositions, reviewer identity, or attestation, and no final candidate ledger or B0A
decision exists. B0B and Adapter work are not authorized.

The separate pre-M4 core-corrections slice is complete on commits `5fdfb32`,
`f403a5c`, `35330e8`, `5adaab0`, `586b579`, and `cc0fbc4`: Patch application now fails closed
on stale bases and malformed preconditions; Aureus and Flare emit producer-to-product
`DROPS_FROM`; repair request identity is stable without weakening base-bound Patch
identity; active repair/generation recordings use `openai/gpt-5.6-sol/pre-m4@1`; and
the seeded benchmark clean base uses checkout-independent logical source provenance.
Two zero-live repair replays were byte-identical at 10/10. Historical M2 cassettes and
frozen external evidence remain unchanged. The full repository gate is 962 passed,
1 skipped, with all 7 import contracts kept. Narrative BDR, Human-Edit-Distance,
QA-hours, and BenchReport v2 remain separate pre-M4 debts.

## Layout (contract §1)

All Python packages live under `gameforge/` (dependency direction enforced by
`import-linter`); `web/` (React/TS) is a repo-root sibling.

```
gameforge/
  contracts/   # schema single source of truth (IR, Env, Finding/Patch, WorldConfig)
  runtime/     # low-level capabilities (skeleton until M2)
  spine/       # deterministic trusted trunk — NO LLM (ir, checkers, dsl, sim, versioning)
  env/         # Agent-Env interface (ABC, no impl)
  game/aureus/ # reference game: deterministic kernel implementing env
  agents/      # bounded LLM layer (skeleton until M2)
  platform/    # product platform (skeleton)
  apps/cli/    # composition layer: end-to-end slice runner
  bench/       # GameForge-Bench (skeleton until M3)
scenarios/     # hand-written scenario configs
web/           # Vite + React + TS console scaffold
```

## Quickstart

```bash
uv python install 3.12 && uv sync     # provision env + deps
uv run pytest                         # full test suite (unit + property + e2e)
uv run lint-imports                   # dependency-direction gate (spine is LLM-free)
```

## M0a acceptance

One hand-written config → IR → Aureus runs a 3+ step quest chain (talk → collect → turn-in),
deterministically and reproducibly:

```bash
uv run python -m gameforge.apps.cli scenarios/caravan.yaml 0
# -> {"completed": true, "ticks": 30, "num_findings": 0, ...}
```

The frontend scaffold builds with:

```bash
cd web && npm install && npm run build
```

## M0b acceptance

A typed CSV scenario workbook (`scenarios/outpost/`) round-trips losslessly through the
Schema Registry + Aureus adapter (`workbook -> IR -> workbook` diff is empty), and the
same scenario drives all four Aureus systems — combat, economy, gacha, quest —
config-driven and deterministically to completion:

```bash
uv run python -m gameforge.apps.cli scenarios/outpost 0
# -> {"completed": true, "ticks": 29, ..., "systems_exercised": ["combat", "economy", "gacha", "quest"]}
```

Version/lineage/audit (contract §5) and the DB migration framework are exercised by:

```bash
uv run alembic -c alembic.ini upgrade head && uv run alembic -c alembic.ini downgrade base
```

`DATABASE_URL` defaults to a local sqlite file (`sqlite:///gameforge.db`, gitignored) when
unset; the schema is Postgres-ready (SQLAlchemy Core + Alembic, no sqlite-only types).

## M1 acceptance

A constraint DSL (`scenarios/constraints/*.yaml`) compiles to three deterministic
backends — GraphChecker (graph algorithms), ASPChecker (Clingo, differential-tested
against GraphChecker on the two defect classes they share), SMTChecker (z3) — plus an
economy Monte-Carlo/ABM simulator, fanned into one `ReviewReport` with a strict
deterministic / llm-assisted / simulation / unproven partition:

```bash
uv run python -m gameforge.apps.cli review scenarios/defects/clean scenarios/constraints
# -> {"deterministic_findings": 0, "llm_assisted_findings": 1, "simulation_findings": 1, ...}
```

9 injected-defect scenarios under `scenarios/defects/<class>/` (one CSV mutation each,
otherwise identical to the pristine `clean/` baseline) are each soundly detected as
*exactly* their own defect class — `dangling_reference`, `missing_drop_source`,
`cyclic_dependency`, `dead_quest`, `unsatisfiable_completion` (structural, Graph/ASP);
`reward_out_of_range`, `prob_sum_ne_1`, `non_monotonic_curve`,
`gacha_expectation_violation` (numeric, SMT) — while the `clean` baseline yields
**zero deterministic findings** (oracle-FP=0, the headline KPI). A tenth scenario,
`economy_collapse`, reproduces a Monte-Carlo economy collapse with an early-warning
tick strictly ahead of the collapse tick. The open-source Flare adapter
(`gameforge/spine/ingestion/flare_adapter.py`) round-trips its vendored sample
losslessly (`from_ir(to_ir(x)) == x`); this is an adapter-integrity fixture, not a
real-defect external-validity anchor. See
`tests/apps/test_m1_acceptance.py` for the full acceptance suite.

## M2a-part1 acceptance

A toy two-call agent node, run through the deterministic orchestration harness
(`gameforge.agents.orchestrator.run_graph`) under `ModelRouter(mode=RECORD)` against a
canned `StubTransport`, writes one cassette file per LLM call; re-running the identical
graph under `ModelRouter(mode=REPLAY)` — with a transport stub that raises if it is ever
invoked — reproduces the resulting `AgentNodeResult` byte-for-byte (`model_dump()`
equality), with zero live network calls:

```bash
uv run pytest tests/agents/test_foundations_acceptance.py -v
# -> test_foundations_record_then_replay_reproduces_byte_identical PASSED
```

This is the foundations slice of contract §7 (Model Router / Cassette / `request_hash`)
plus the 6-role `agent_io` contract and the "LLM SDK only in `runtime.model_router`"
import-linter contract (7th contract, `uv run lint-imports` → 7 kept, 0 broken).

## M2b-1 acceptance

A layered Playtest agent (a Planner PROPOSES a high-level subgoal, an Executor
PROPOSES the next atomic action, and `AureusEnv` is the SOLE authority on `done`)
closes a real 3-step quest chain (`caravan`: talk → collect → turn-in) end-to-end
under a scripted, network-free REPLAY-mode router — and beats a no-LLM
random-action floor on the same scenario (the floor never completes within the
step budget), proving the agent is doing real work, not coasting on scenario
triviality:

```bash
uv run pytest tests/agents/playtest/test_playtest_smoke.py -v
# -> scripted agent completion_rate == 1.0  vs.  random floor completion_rate < 1.0
```

The planner/executor ablation (`use_planner=True`/`False`) and the regression
harness (`run_playtest_corpus` completion rate + per-length-bucket Wilson CI,
`random_baseline`) both run end-to-end through
`gameforge/agents/playtest_harness.py`; verifier-grounding (a BFS reachability
oracle cross-checking the engine's own `unreachable` verdict before a quest is
aborted) is exercised by the walled-env test in
`tests/agents/playtest/test_agent_loop.py`. See those plus
`tests/agents/test_playtest_harness.py` for the full suite.

## What M0a delivers vs. deferred (不简化，只延后)

**Delivered:** monorepo + dependency lint; `contracts` (IR core types, canonical
snapshot, Env action/observation, Finding/Patch, WorldConfig — full contract field
sets); in-memory IR store + immutable content-addressed snapshots + diff; YAML
scenario → IR loader; minimal deterministic structural checker (reference integrity,
collect-needs-reachable-source, quest-DAG-acyclic); Aureus minimal kernel (quest state
machine + grid navigation) implementing the Agent-Env contract with per-tick
`state_hash` determinism; end-to-end vertical slice; React scaffold.

**Interfaces defined now, implementation deferred:** combat/economy Env atomics + IR
combat-economy node/edge impl (M0b); Schema Registry round-trip adapter, version/
lineage/audit, DB migrations (M0b); DSL grammar + Graph/ASP/SMT compiler + economy sim
(M1); bounded LLM agents + cassette/model-router (M2); GameForge-Bench (M3); full web
pages + observability/RBAC (M4).

## What M0b delivers vs. deferred (不简化，只延后)

**Delivered:** Aureus combat (formula-driven damage/hit/crit + seeded `CountingRandom`),
economy (currencies/shops/atomic buy), and gacha (drop tables, atomic buy-and-roll) —
all config-driven via `WorldConfig`, integrated into the kernel alongside quest/nav, with
per-tick `state_hash` covering combat/gacha rng; IR combat-economy node/edge types
produced by the loader and consumed by the checker/kernel; a typed CSV `FormatSchema` +
`SchemaRegistry` (structural + referential validation) and a pluggable `AureusCsvAdapter`
(`to_ir`/`from_ir`) giving a lossless workbook<->IR round trip (property-tested); the
`scenarios/outpost/` CSV scenario exercising all four systems end-to-end; version/lineage/
audit skeleton (contract §5 full `VersionTuple`, content-addressed `Artifact` + lineage
DAG, ref/rollback, append-only WORM `AuditRecord` with hash chain) with both an in-memory
store (used by `run_slice`) and a SQLAlchemy-backed store; Alembic migration framework
with a tested forward/rollback migration; the dependency lint tightened so `spine` only
depends on `contracts`.

**Interfaces defined now, implementation deferred:** DSL grammar + Graph/ASP/SMT checker
compilation + economy Monte-Carlo/ABM simulation + open-source game adapter external
validity (M1); bounded LLM agents + Agent-Env + Playtest Agent + cassette/model-router
(M2); GameForge-Bench (M3); RBAC/approval workflow + full web pages + observability
panels (M4). `VersionTuple` fields for constraint/prompt/model/agent/cassette are
schema-present and `None` until those milestones populate them.

## What M1 delivers vs. deferred (不简化，只延后)

**Delivered:** constraint DSL (`Predicate`/`Selector`/`Constraint`, deterministic and
llm-assisted oracle kinds); `parse_assert` whitelist-only typed-AST expression
evaluator (never `eval`/`exec`); `GraphChecker` (7 structural defect classes: dangling
reference, missing drop source, unreachable target, cyclic dependency, dead quest,
unsatisfiable completion, isolated node); `ASPChecker` (Clingo encoding of the two
defect classes shared with GraphChecker, differential-tested against it, with a
grounding-budget/wall-clock degrade-to-`unproven` — never a silent pass); `SMTChecker`
(z3 encoding of 5 numeric defect classes: reward-out-of-range, prob-sum≠1,
non-monotonic curve, gacha-expectation-vs-pity, interval violation); `compile(constraint)
-> Checker` routing (llm-assisted predicate check first, then kind-based dispatch) with
`LlmRoutedChecker` as M1's complete-but-unevaluated routing target for the agent layer;
a deterministic typed-patch apply/reject engine (contract §6 `old_value` optimistic-
concurrency anchor); the economy Monte-Carlo/ABM simulator (6 named invariants +
collapse/early-warning detection) and its `to_findings` projection; `ReviewReport`'s
strict deterministic/llm-assisted/simulation/unproven partition (`build_review_report`)
and the `run_review` CLI orchestration; the open-source Flare adapter (`to_ir`/`from_ir`,
lossless line-level round trip); 9 injected-defect scenarios + a pristine `clean`
baseline (oracle-FP=0) + an economy-collapse scenario, all real CSV-derived via the
Aureus adapter, not hand-built IR fixtures.

**Interfaces defined now, implementation deferred:** `unreachable_target` (needs a
`NavProvider`; `GraphChecker._unreachable_target` is a complete, silently-no-op-safe
implementation, but `run_review`'s CLI path never builds an Aureus world/nav, so this
class isn't exercised by the M1 scenario suite — it's implementation-complete, just
untriggered here); llm-assisted predicate *evaluation* (the routing/Finding-shape
contract is complete now; actually judging a narrative predicate is M2's bounded agent
layer); bounded LLM agents + Agent-Env + Playtest Agent + cassette/model-router (M2);
GameForge-Bench (M3); RBAC/approval workflow + full web pages + observability panels
(M4).

## What M2a-part1 delivers vs. deferred (不简化，只延后)

**Delivered:** contract §7 (`ModelSnapshot`/`Message`/`ModelRequest`/`ModelResponse`,
`request_hash` excluding `cache_key`/schema_version so it hashes exactly the fields that
determine the model's output) and the 6-role `agent_io` contract (`AgentNodeResult` plus
each role's typed input/output — extraction, triage, repair, consistency, generation,
playtest — fields defined in full now, only extraction/triage/repair/consistency/
generation implemented in part2, playtest in M2b); the "LLM SDK only in
`runtime.model_router`" import-linter contract (`LlmTransport`/`OpenAITransport`/
`StubTransport` are the sole `openai` import site); `ModelRouter` with
RECORD/REPLAY/PASSTHROUGH modes, retry-with-quota (every live transport attempt counted,
including retries) and in-session exact-match de-duplication (stable-prefix semantic
caching deferred to part2); `CassetteStore` flat-file record/replay keyed by
`request_hash`; a hand-rolled deterministic orchestration harness
(`gameforge.agents.orchestrator.run_graph` — ordered nodes, no concurrency, no hidden
state) plus a `prompt_version` registry; and the record→replay reproducibility
acceptance test proving byte-identical `AgentNodeResult` reproduction with zero live
network calls.

**Interfaces defined now, implementation deferred to M2a-part2:** Extraction
Proposer/Defect Triager/Repair Drafter/Consistency Assistant/Content Generator real LLM
semantics + per-role fallback, verifier-guided repair search, generation gate, Fix Pass
Rate ≥70% acceptance against real recorded gateway cassettes, stable-prefix semantic
cache. **Deferred to M2b:** Playtest Agent, mem-trace, ablation studies.

## What M2b-1 delivers vs. deferred (不简化，只延后)

**Delivered:** deterministic state abstraction (`abstract_state`) turning an Env
observation into the shared input for both planner and executor; registered
`playtest@1` planner/executor/reflect prompts; a Planner (high-level subgoal
proposal, falls back to "advance") and an Executor (atomic-action proposal with an
action-priority fallback to `observe`); verifier-grounding (`ground_target` /
`make_unreachable_finding` — a static BFS reachability oracle cross-checks the
engine's own `unreachable` verdict; only when BOTH agree is a quest aborted and a
confirmed `unreachable_target` Finding recorded, so an LLM's mere suspicion can
never abort a quest on its own); the main loop (`PlaytestAgent.run`) driving the
REAL `AureusEnv` — `done`/`completed` is always read back from the env, never
claimed by a model — with a stagnation-triggered Reflector hint, plus a flat
(no-planner) ablation that skips the Planner call entirely; the regression harness
(`gameforge/agents/playtest_harness.py`: `run_playtest_corpus` — completion rate +
per-length-bucket 95% Wilson CI + `RECORD`/`REPLAY` entrypoints; `random_baseline`
— a no-LLM uniform-random-action floor); and a REPLAY/scripted smoke test proving
the planner/executor ablation switch runs both positions end-to-end through the
harness, with the scripted agent (`completion_rate == 1.0`) genuinely beating the
random floor on the same scenario (`completion_rate < 1.0`).

**Interfaces defined now, implementation deferred:** the ≥20-chain deterministic
scenario generator and the real live-opus RECORD pass that would produce actual
corpus-wide completion-rate numbers (M2b-1b — today's numbers are scripted-smoke
proof-of-life on one scenario, not a completion-rate claim over a real corpus);
mem-trace + memory ablation (the `memory` slot is already wired into
`PlaytestAgent.run` / `run_playtest_corpus`, guarded by a `None` check, and runs
as `None` this milestone) and the adversarial-quorum narrative-defect advance
(M2b-2).
