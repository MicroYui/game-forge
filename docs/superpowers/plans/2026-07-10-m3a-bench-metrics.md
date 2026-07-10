# M3a — GameForge-Bench (Seeded ≥500) + Metrics Aggregation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic `gameforge/bench/` package that programmatically injects the 15-class defect taxonomy into Aureus configs (≥500 seeded, reproducible samples), runs the spine checkers/sim over them, and aggregates per-class Bug-Detection-Rate + False-Positive + agent metrics with Wilson CIs — deterministic vs llm-assisted strictly separated — into a JSON `BenchReport`.

**Architecture:** Injectors mutate the **IR `Snapshot`** in-memory (no 500 committed dirs), each producing `(mutated_snapshot, GroundTruth)`. The metrics engine reuses the M1 review pipeline (`build_review_report`) and scores detections against ground-truth. Agent metrics are a single bridge module that REPLAY-aggregates the existing M2 cassettes (no live calls). Anti-circularity: injectors and checkers are separate code; the seeded core never imports `gameforge.agents`.

**Tech Stack:** Python 3.12 via `uv`; pydantic v2; pytest + TDD; import-linter; ruff.

## Global Constraints

- **不简化只延后**: all 15 taxonomy classes get injectors this milestone; `BenchReport.external` (M3b) + full panel (M3c/M4) + deep constraint-FP are interface-defined now, implemented later.
- **Determinism-first**: BDR/FP decided by checkers/sim, never LLM. Injectors are seeded/reproducible (same `(base,defect,seed)` → same `snapshot_id`). Agent metrics run REPLAY-only over committed M2 cassettes.
- **Dependency (new 8th contract)**: `gameforge.bench` may import `contracts`/`spine`/`game`/`runtime`/`apps.cli.ir_to_world` and (only `agent_metrics.py`) `gameforge.agents`; **never an LLM SDK** (still isolated to `runtime.model_router.transport` by the 7th contract). The seeded core (`inject/corpus/metrics/report/taxonomy/power`) must **not** import `gameforge.agents` — locked by an AST test.
- **Anti-circularity**: injector code is independent of checker code; the checker never consults `GroundTruth`.
- **Test hygiene**: unique test basenames under `tests/` (no `__init__.py`). TDD: RED before GREEN. Commits carry NO AI attribution. Trunk is `master`.

## File Structure

- Create `gameforge/spine/stats.py` — `wilson_ci` (moved from `agents/playtest_harness.py`), shared by agents + bench.
- Create `gameforge/bench/__init__.py`, `taxonomy.py`, `inject.py`, `corpus.py`, `power.py`, `metrics.py`, `agent_metrics.py`, `report.py`, `run_bench.py`.
- Modify `gameforge/agents/playtest_harness.py` — re-import `wilson_ci` from `spine.stats` (no behavior change).
- Modify `tests/test_dependency_lint.py` — add the bench contract + seeded-core AST guard.
- Tests under `tests/bench/` (unique basenames): `test_taxonomy.py`, `test_inject_structural.py`, `test_inject_numeric.py`, `test_inject_narrative.py`, `test_power.py`, `test_corpus.py`, `test_metrics.py`, `test_agent_metrics.py`, `test_report.py`, `test_run_bench.py`.

---

### Task 1: `spine/stats.py` (shared Wilson CI) + `bench/taxonomy.py`

**Files:** Create `gameforge/spine/stats.py`, `gameforge/bench/__init__.py`, `gameforge/bench/taxonomy.py`; Modify `gameforge/agents/playtest_harness.py`; Test `tests/bench/test_taxonomy.py`, `tests/spine/test_stats.py`.

**Interfaces — Produces:**
- `wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]` in `spine/stats.py`.
- `DefectClass(str, Enum)` — 15 members (names verbatim): `dangling_reference, missing_drop_source, unreachable_target, cyclic_dependency, dead_quest, unsatisfiable_completion, reward_out_of_range, prob_sum_ne_1, non_monotonic_curve, gacha_expectation_violation, economy_collapse, character_violation, spoiler, faction_violation, uniqueness_violation`.
- `Bucket(str, Enum)` = `deterministic | simulation | llm_assisted`.
- `CLASS_META: dict[DefectClass, DefectMeta]` where `DefectMeta = dataclass(oracle: str, bucket: Bucket)` per the spec §2 table.

- [ ] **Step 1: Write failing tests**
```python
# tests/spine/test_stats.py
from gameforge.spine.stats import wilson_ci
def test_wilson_ci_bounds_and_monotonic():
    lo0, hi0 = wilson_ci(0, 20)
    assert 0.0 <= lo0 <= hi0 <= 1.0
    prev = (lo0, hi0)
    for k in range(1, 21):
        lo, hi = wilson_ci(k, 20)
        assert lo >= prev[0] and hi >= prev[1] and 0.0 <= lo <= hi <= 1.0
        prev = (lo, hi)
    assert wilson_ci(0, 0) == (0.0, 1.0)  # n=0 → full interval, no div-by-zero
```
```python
# tests/bench/test_taxonomy.py
from gameforge.bench.taxonomy import DefectClass, Bucket, CLASS_META
def test_15_classes_each_have_meta_and_bucket():
    assert len(DefectClass) == 15
    for dc in DefectClass:
        assert dc in CLASS_META
        assert CLASS_META[dc].bucket in Bucket
    # narrative classes are llm-assisted; economy_collapse is simulation; rest deterministic
    assert CLASS_META[DefectClass.economy_collapse].bucket is Bucket.simulation
    assert CLASS_META[DefectClass.character_violation].bucket is Bucket.llm_assisted
    assert CLASS_META[DefectClass.dangling_reference].bucket is Bucket.deterministic
```
- [ ] **Step 2: Verify RED** — `uv run pytest tests/spine/test_stats.py tests/bench/test_taxonomy.py -v` → FAIL (modules missing).
- [ ] **Step 3: Implement** — move `wilson_ci` body from `playtest_harness.py` into `spine/stats.py` (guard `n==0 → (0.0, 1.0)`); in `playtest_harness.py` replace the def with `from gameforge.spine.stats import wilson_ci`. Write `taxonomy.py` per the interface + spec §2 table.
- [ ] **Step 4: Verify GREEN** — those tests pass; `uv run pytest tests/agents/test_playtest_acceptance.py -q` still green (wilson_ci move is behavior-preserving).
- [ ] **Step 5: Commit** — `feat(bench): 缺陷分类学 15 类 + 共享 wilson_ci(spine/stats)`

---

### Task 2: `inject.py` — types + 6 structural injectors

**Files:** Create `gameforge/bench/inject.py`; Test `tests/bench/test_inject_structural.py`.

**Interfaces — Produces:**
- `@dataclass GroundTruth: defect_class: DefectClass; injected_entities: list[str]; note: str`
- `@dataclass InjectedSample: snapshot: Snapshot; ground_truth: GroundTruth; needs_nav: bool = False`
- `inject(base: Snapshot, defect: DefectClass, seed: int) -> InjectedSample` — dispatches to a per-class injector; deterministic in `(base, defect, seed)`.
- Helper `_clone_lists(base) -> tuple[list[Entity], list[Relation]]` (from `base.entities`/`base.relations` dict values) so injectors mutate copies and rebuild via `Snapshot.from_entities_relations`.

**Consumes:** `Snapshot.from_entities_relations`, `Entity`/`Relation`/`EdgeType`/`NodeType` from `contracts.ir`, `DefectClass` from Task 1.

- [ ] **Step 1: Write failing tests** (property tests — verify the defect independently of any checker; use a clean base built inline or from `scenarios/defects/clean`):
```python
# tests/bench/test_inject_structural.py
from gameforge.bench.inject import inject
from gameforge.bench.taxonomy import DefectClass
from gameforge.bench.testbases import clean_base  # small helper returning a clean Snapshot

def test_dangling_reference_points_dst_to_nonexistent_id():
    s = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    g = s.snapshot.to_graph()
    ids = {n.id for n in g.nodes()}
    # the injected relation's dst is NOT a known entity id (structural check, no checker)
    bad = s.ground_truth.injected_entities[-1]
    assert bad not in ids
    assert clean_base().to_graph()  # base itself has no dangling ref (sanity)

def test_cyclic_dependency_adds_a_precedes_cycle():
    s = inject(clean_base(), DefectClass.cyclic_dependency, seed=1)
    # independent cycle detection over PRECEDES edges (not via ASPChecker)
    assert _has_precedes_cycle(s.snapshot)  # helper in the test
    assert not _has_precedes_cycle(clean_base())

def test_injectors_are_seeded_reproducible_and_distinct():
    a = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    b = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    c = inject(clean_base(), DefectClass.dangling_reference, seed=2)
    assert a.snapshot.snapshot_id == b.snapshot.snapshot_id  # reproducible
    assert a.snapshot.snapshot_id != c.snapshot.snapshot_id   # different seed → different sample

def test_each_structural_injector_sets_correct_ground_truth():
    for dc in [DefectClass.dangling_reference, DefectClass.missing_drop_source,
               DefectClass.unreachable_target, DefectClass.cyclic_dependency,
               DefectClass.dead_quest, DefectClass.unsatisfiable_completion]:
        s = inject(clean_base(), dc, seed=3)
        assert s.ground_truth.defect_class is dc
        assert s.ground_truth.injected_entities  # non-empty
```
Also create `tests/bench/testbases.py` (NOT a test file — a shared helper; give it a `clean_base()` returning a small but complete Aureus `Snapshot` with quests/steps/items/monsters/regions so every class is injectable). Reuse `AureusCsvAdapter().to_ir(read_workbook("scenarios/defects/clean", schema))` if simpler.
- [ ] **Step 2: Verify RED** — FAIL (module missing).
- [ ] **Step 3: Implement** the 6 structural injectors per spec §3 strategies 1–6 (each: clone lists → mutate → `from_entities_relations` → `InjectedSample` with `needs_nav=True` for `unreachable_target`). Seed via `random.Random(hash((base.snapshot_id, defect, seed)) & 0xFFFFFFFF)` — deterministic entity/relation selection.
- [ ] **Step 4: Verify GREEN**.
- [ ] **Step 5: Commit** — `feat(bench): 6 结构类缺陷注入器 + GroundTruth/InjectedSample(property 测试锁单缺陷隔离)`

---

### Task 3: `inject.py` — 5 numeric/economy injectors

**Files:** Modify `gameforge/bench/inject.py`; Test `tests/bench/test_inject_numeric.py`.

**Interfaces — Produces:** extends `inject(...)` dispatch to `reward_out_of_range, prob_sum_ne_1, non_monotonic_curve, gacha_expectation_violation, economy_collapse`.

- [ ] **Step 1: Write failing tests** — per class, an independent numeric assertion (e.g. `prob_sum_ne_1`: the injected drop-table's `Fraction(str(p))` sum ≠ 1; `non_monotonic_curve`: some tier power decreases; `economy_collapse`: injected monster gold_max ≫ any sink). Include the seeded-reproducible + correct-ground-truth checks as in Task 2.
```python
# tests/bench/test_inject_numeric.py
from fractions import Fraction
def test_prob_sum_ne_1_breaks_the_sum():
    s = inject(clean_base(), DefectClass.prob_sum_ne_1, seed=1)
    tbl = s.ground_truth.injected_entities[0]
    probs = _drop_probs(s.snapshot, tbl)  # helper reads the table's entry probs
    assert sum(Fraction(str(p)) for p in probs) != Fraction(1)
def test_non_monotonic_curve_has_a_decrease():
    s = inject(clean_base(), DefectClass.non_monotonic_curve, seed=1)
    powers = _tier_powers(s.snapshot, s.ground_truth.injected_entities)
    assert any(powers[i+1] < powers[i] for i in range(len(powers)-1))
```
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement** per spec §3 strategies 7–11. - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): 5 数值/经济类缺陷注入器`

---

### Task 4: `inject.py` — 4 narrative injectors (llm-assisted bucket)

**Files:** Modify `gameforge/bench/inject.py`; Test `tests/bench/test_inject_narrative.py`.

**Interfaces — Produces:** `inject(...)` handles `character_violation, spoiler, faction_violation, uniqueness_violation`. These attach a `DialogueNarrativeInput` (contracts.agent_io) + narrative-constraint ids to the sample; `InjectedSample` gains `dialogue: DialogueNarrativeInput | None = None` (the seeded dialogue carrying the injected contradiction). `GroundTruth.injected_entities` = the offending span/entity.

- [ ] **Step 1: Write failing tests** — e.g. `character_violation` produces a `DialogueNarrativeInput` whose dialogue text contradicts a named character trait constraint (independent string assertion: the dialogue contains the contradiction token + the constraint id is present). Seeded-reproducible + ground-truth correct.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement** per spec §3 strategies 12–15 (deterministic templated dialogue with a seeded contradiction). - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): 4 叙事类缺陷注入器(DialogueNarrativeInput, llm-assisted bucket)`

---

### Task 5: `power.py`

**Files:** Create `gameforge/bench/power.py`; Test `tests/bench/test_power.py`.

**Interfaces — Produces:**
- `required_n(p_hat: float, half_width: float = 0.05, z: float = 1.96) -> int` — smallest n with Wilson (or normal-approx) CI half-width ≤ `half_width` at `p_hat`; conservative `p_hat=0.5` when unknown.
- `achieved_half_width(k: int, n: int) -> float` — `(hi-lo)/2` from `wilson_ci`.
- `@dataclass PowerRow: defect_class: DefectClass; n: int; achieved_half_width: float; target_met: bool`.

- [ ] **Step 1: Write failing tests** — `required_n(0.5)` ≈ 384 (±a few); `required_n(0.95)` < `required_n(0.5)`; `achieved_half_width(k,n)` shrinks as n grows; `target_met` true iff `achieved_half_width ≤ 0.05`.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement** (binary-search n or closed form). - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): 功效计算 required_n + achieved CI 半宽`

---

### Task 6: `corpus.py`

**Files:** Create `gameforge/bench/corpus.py`; Test `tests/bench/test_corpus.py`.

**Interfaces — Produces:**
- `build_corpus(seed: int = 0, per_class_n: dict[DefectClass, int] | None = None, n_clean: int = 40) -> Corpus` where `Corpus = dataclass(samples: list[InjectedSample], clean: list[Snapshot], per_class_n: dict[...])`.
- Default `per_class_n`: for deterministic/simulation classes use `required_n(0.95)`; for llm-assisted use a bounded value (e.g. 20, honestly under-powered by design — narrative detection is LLM/cassette-bound); TOTAL samples ≥ 500 (assert in test).

- [ ] **Step 1: Write failing tests** — `build_corpus()` yields ≥500 samples, every `DefectClass` represented, clean list length `n_clean`, seeded-reproducible `snapshot_id` sequence, per-class samples pairwise-distinct `snapshot_id`.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement** (loop classes × `required_n`, seed-derive per sample). - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): ≥500 seeded 语料组装(功效驱动 per-class n)`

---

### Task 7: `metrics.py`

**Files:** Create `gameforge/bench/metrics.py`; Test `tests/bench/test_metrics.py`.

**Interfaces — Produces:**
- `@dataclass Metric: name: str; defect_class: str | None; n: int; k: int; rate: float; ci_low: float; ci_high: float; bucket: str`.
- `detects(report: ReviewReport, gt: GroundTruth) -> bool` — a Finding in `gt`'s bucket partition with `defect_class == gt.defect_class` and an injected entity in the Finding's `entities`/`evidence`.
- `score_seeded(corpus: Corpus, constraints: list[Constraint]) -> tuple[list[Metric], FPReport, FPReport]` — returns per-class BDR Metrics + `oracle_fp` (deterministic+unproven Findings on clean, target 0) + `constraint_fp` (basic: deterministic Findings on clean that no ground-truth explains). Runs the M1 pipeline (`compile_all` → `build_review_report` with economy sim + nav for `needs_nav` samples) per sample.
- `@dataclass FPReport: n_clean: int; count: int; rate: float; ci_low: float; ci_high: float`.

**Consumes:** `build_review_report`, `compile_all`, `EconomyModel/EconomySimulator/to_findings`, `snapshot_to_world`+nav for `needs_nav`, `wilson_ci`.

- [ ] **Step 1: Write failing tests** — hand-built known-outcome cases: an injected `dangling_reference` sample → `detects(...) is True`; a clean snapshot → `detects` False for every class AND `oracle_fp.count == 0`; a Finding of the right class but wrong entity → `detects` False (locks the entity-overlap requirement, not just class match). BDR/FP `Metric` fields correct (k/n/rate/CI).
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement**. - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): 指标引擎 — 分类 BDR + oracle-FP(=0) + constraint-FP + Wilson CI + 分区匹配`

---

### Task 8: `agent_metrics.py` (REPLAY-only bridge)

**Files:** Create `gameforge/bench/agent_metrics.py`; Test `tests/bench/test_agent_metrics.py`.

**Interfaces — Produces:** `aggregate_agent_metrics() -> list[Metric]` — REPLAY-recompute the committed M2 results and wrap as `Metric`s with `n`+CI+`bucket="agent"`: Playtest completion (layered/flat) + planner/executor ablation + memory ablation + compactor comparison (from `agents.playtest_harness` REPLAY) and repair Fix Pass Rate (from `agents.harness` REPLAY). Skip-guard each on its cassette dir presence (mirror the acceptance tests) so it degrades gracefully if a cassette set is absent.

**Consumes (the ONLY bench module allowed to import `gameforge.agents`):** `agents.playtest_harness.{run_playtest_corpus, default_chain_snapshots, replay_router, random_baseline}`, `agents.harness` repair aggregation. Zero live calls (REPLAY).

- [ ] **Step 1: Write failing tests** — `aggregate_agent_metrics()` returns Metrics including a `playtest_completion_layered` whose `rate == 0.7` (matches the committed record) and a `memory_ablation` delta, each with a valid CI; all `bucket == "agent"`. Reproducible across two calls.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement**. - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): agent 指标聚合(REPLAY-only 复用 M2 录制,有界子集)`

---

### Task 9: `report.py`

**Files:** Create `gameforge/bench/report.py`; Test `tests/bench/test_report.py`.

**Interfaces — Produces:** pydantic `BenchReport` (fields per spec §7: `seeded, oracle_fp, constraint_fp, agent, power, external: ExternalReport | None = None, meta`), `to_json() -> str`, `format_text(report) -> str` (minimal human view). `ExternalReport`/`BenchMeta`/`PowerRow` models.

- [ ] **Step 1: Write failing tests** — a `BenchReport` round-trips through `to_json`/`model_validate_json`; `external` defaults `None`; `format_text` includes each class's BDR line + the `oracle-FP` line + separates deterministic/llm-assisted/agent sections; det and llm-assisted never merged into one number.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement**. - [ ] **Step 4: Verify GREEN**. - [ ] **Step 5: Commit** — `feat(bench): BenchReport JSON 契约 + 最小文本视图(external 槽现定)`

---

### Task 10: `run_bench.py` + dependency contract + end-to-end

**Files:** Create `gameforge/bench/run_bench.py`; Modify `tests/test_dependency_lint.py`, `pyproject.toml`/import-linter config; Test `tests/bench/test_run_bench.py`.

**Interfaces — Produces:** `build_bench_report(seed=0, with_agent=True) -> BenchReport` (assemble corpus → `score_seeded` → `aggregate_agent_metrics` if `with_agent` → `power` rows → `BenchReport`); `main()` prints `format_text`; `--json` prints `to_json`.

- [ ] **Step 1: Write failing tests**:
```python
# tests/bench/test_run_bench.py
def test_end_to_end_bench_report_has_per_class_bdr_and_zero_oracle_fp():
    from gameforge.bench.run_bench import build_bench_report
    r = build_bench_report(seed=0, with_agent=False)  # fast: seeded only
    assert r.oracle_fp.count == 0                       # headline KPI
    classes = {m.defect_class for m in r.seeded}
    assert len(classes) >= 11                           # all deterministic+sim classes reported
    for m in r.seeded:
        assert 0.0 <= m.ci_low <= m.ci_high <= 1.0
    det = [m for m in r.seeded if m.bucket == "deterministic"]
    llm = [m for m in r.seeded if m.bucket == "llm_assisted"]
    assert det and llm  # both buckets present, separately
```
And in `tests/test_dependency_lint.py`: assert the seeded-core modules import no `gameforge.agents`; assert `gameforge.bench` imports no LLM SDK.
- [ ] **Step 2: Verify RED**. - [ ] **Step 3: Implement** `run_bench.py` + add the 8th import-linter contract (forbid LLM SDKs from `gameforge.bench`) + the AST guard.
- [ ] **Step 4: Verify GREEN** — end-to-end test passes; `uv run lint-imports` → 8 kept; full suite green; `uv run ruff check .` clean.
- [ ] **Step 5: Commit** — `feat(bench): run_bench 入口 + 依赖契约(bench 禁 LLM SDK / seeded 核心禁 agents) + 端到端(oracle-FP=0)`

---

## Self-Review

- **Spec coverage**: taxonomy(T1) / injectors 15 类(T2–4) / power(T5) / corpus ≥500(T6) / metrics BDR+FP+CI+分区(T7) / agent 有界子集(T8) / BenchReport(T9) / run_bench+反循环 lint+端到端(T10). External(M3b) + panel(M3c) interface-defined via `BenchReport.external` + JSON contract. Covered.
- **Placeholder scan**: injector strategies come from spec §3 (concrete); test code is concrete. The narrative injectors' detection is llm-assisted (cassette-bound) — their BDR is reported honestly as under-powered (n bounded), not faked.
- **Type consistency**: `DefectClass`/`Bucket`/`GroundTruth`/`InjectedSample`/`Metric`/`FPReport`/`BenchReport`/`PowerRow` used consistently across tasks; `wilson_ci` single source in `spine/stats`.
- **Highest risks**: (a) detection entity-overlap matching (T7) — locked by the wrong-entity test; (b) the seeded-core-agents-free boundary (T10 AST guard); (c) narrative BDR needs consistency cassettes — T8/T4 keep it REPLAY/skip-guarded, honest under-power.
