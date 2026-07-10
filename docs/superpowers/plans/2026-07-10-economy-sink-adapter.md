# Economy Sink Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plumb `price`/`currency`/`buy_prob` from shop config into the derived `SHOP--SELLS-->ITEM` relation so the economy simulator can model gold sinks from real CSV, then honestly re-measure whether the `economy_collapse` repair becomes winnable (Fix Pass Rate 9/10 → possibly 10/10).

**Architecture:** Two phases. **Phase 1** (deterministic, zero-live, TDD): extend the `ShopEntry` contract with an optional `buy_prob`, make `AureusCsvAdapter` pass shop-entry attrs onto the SELLS relation, and lock the adapter→sim sink chain + all regressions. **Phase 2** (live-gated): because plumbing changes the `economy_collapse` finding's `evidence["sinks"]` (empty→populated), that scenario's repair request_hash changes and its cassette MUST be re-recorded; re-record (resume-based, only economy_collapse costs a live call), measure, and only if still 9/10 strengthen the repair prompt + full re-record.

**Tech Stack:** Python 3.12 (uv-managed), pydantic v2, pytest, clingo/z3 (unaffected here), the M2 ModelRouter/Cassette REPLAY/RECORD machinery.

## Global Constraints

- **依赖方向单向:** `agents → spine`, NEVER `spine → agents`; `spine` MUST NOT import any LLM SDK. All 7 import-linter contracts stay green (`uv run lint-imports`).
- **不简化,只延后:** the sink schema is defined in full now (`ShopEntry.buy_prob`); only omitted *values* fall back to a default. No field is cut.
- **确定性优先:** correctness is decided by the economy simulation + verifier, never by the LLM. Sink causality is proven by a differential test (balanced→no-collapse vs faucet≫sink→collapse), not asserted.
- **可复现只承诺回放:** any re-record goes through `model_snapshot` + cassette RECORD/REPLAY. CI is zero-live-network; live calls happen ONLY under `GAMEFORGE_LLM_LIVE=1`.
- **Toolchain:** `uv sync`; `uv run pytest`; `uv run lint-imports`; `uv run ruff check .`. System Python is 3.9 — always use the uv-managed 3.12.
- **Git:** branch `economy-sink-adapter` (already cut off `master`). Commit messages carry NO AI attribution (no `Co-Authored-By`, no "Generated with Claude"). Trunk is `master` (no `main`).
- **Zero-value defaults (verbatim from the code, do not re-derive):** `EconomyModel.from_snapshot` reads sink `buy_prob` as `attrs.get("buy_prob", 0.5)` (`gameforge/spine/sim/economy.py:100`) and `currency` as `attrs.get("currency", default_currency)` where `default_currency = next(iter(currencies), "gold")` (`economy.py:58,99`); a SELLS relation with `price is None` is skipped as a non-sink (`economy.py:91-93`).

---

## Phase 1 — Deterministic plumbing (zero-live, TDD)

### Task 1: `ShopEntry.buy_prob` contract field

**Files:**
- Modify: `gameforge/contracts/world.py:121-124` (the `ShopEntry` model)
- Test: `tests/contracts/test_world.py`

**Interfaces:**
- Produces: `ShopEntry(item: str, price: int, currency: str = "gold", buy_prob: float | None = None)` — an additive optional field. The kernel's `EconomySystem.buy`/`sell` ignore `buy_prob` (they read only `entry.price`); it is carried purely for the economy sim's sink model. `snapshot_to_world` (`gameforge/apps/cli/ir_to_world.py:170`) constructs entries via `ShopEntry(**entry)`, so the model MUST accept a `buy_prob` key without raising.

- [ ] **Step 1: Write the failing test**

Add to `tests/contracts/test_world.py`:

```python
from gameforge.contracts.world import ShopEntry


def test_shop_entry_buy_prob_defaults_to_none():
    e = ShopEntry(item="item:x", price=50)
    assert e.buy_prob is None
    assert e.currency == "gold"


def test_shop_entry_accepts_optional_buy_prob():
    e = ShopEntry(item="item:x", price=50, currency="gold", buy_prob=0.5)
    assert e.buy_prob == 0.5


def test_shop_entry_construction_from_entry_dict_with_buy_prob():
    # This is the EXACT call snapshot_to_world makes (ir_to_world.py:170):
    # ShopEntry(**entry). A buy_prob key in the entries JSON must not raise.
    entry = {"item": "item:x", "price": 50, "currency": "gold", "buy_prob": 0.5}
    e = ShopEntry(**entry)
    assert e.buy_prob == 0.5 and e.price == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/contracts/test_world.py::test_shop_entry_accepts_optional_buy_prob tests/contracts/test_world.py::test_shop_entry_construction_from_entry_dict_with_buy_prob -v`
Expected: FAIL — `ShopEntry` has no `buy_prob` field; pydantic raises `ValidationError` on the extra key (default pydantic v2 forbids/ignores unknown; construction with the kwarg errors as unexpected).

- [ ] **Step 3: Write minimal implementation**

In `gameforge/contracts/world.py`, change the `ShopEntry` model (currently lines 121-124):

```python
class ShopEntry(BaseModel):
    item: str
    price: int
    currency: str = "gold"
    buy_prob: float | None = None  # sim sink purchase-probability; kernel ignores it
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/contracts/test_world.py -v`
Expected: PASS (all three new tests + existing world tests).

- [ ] **Step 5: Confirm the kernel path is unaffected**

Run: `uv run pytest tests/game/aureus/test_economy_gacha.py tests/contracts/test_world_combat_economy.py -v`
Expected: PASS — buy/sell still compute from `entry.price`; the new optional field changes nothing at runtime.

- [ ] **Step 6: Commit**

```bash
git add gameforge/contracts/world.py tests/contracts/test_world.py
git commit -m "feat(contracts): ShopEntry.buy_prob 可选字段(sim sink 携带,内核忽略)"
```

---

### Task 2: `AureusCsvAdapter` plumbs SELLS relation attrs

**Files:**
- Modify: `gameforge/spine/ingestion/aureus_adapter.py:251-258` (the SELLS derivation loop)
- Test: `tests/spine/ingestion/test_aureus_adapter.py`

**Interfaces:**
- Consumes: shop workbook rows shaped `{"shop_id": str, "entries": [{"item": str, "price": int, "currency"?: str, "buy_prob"?: float}, ...]}`.
- Produces: for each entry, a `Relation(type=EdgeType.SELLS, src_id=shop_id, dst_id=entry["item"], attrs=<subset>)` where `attrs` contains only the keys present in the entry among `price`/`currency`/`buy_prob`. Round-trip losslessness is unchanged because `from_ir` rebuilds sheets from entity `attrs`, never from relations.

- [ ] **Step 1: Write the failing test**

Add to `tests/spine/ingestion/test_aureus_adapter.py` (module already imports `AureusCsvAdapter`, `NodeType`, `EdgeType` and defines `_wb()`):

```python
def test_to_ir_plumbs_sells_relation_attrs():
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50, "buy_prob": 0.5}]}]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    sells = g.neighbors("shop:s", EdgeType.SELLS, direction="out")
    assert len(sells) == 1
    assert sells[0].dst_id == "item:x"
    assert sells[0].attrs["price"] == 50
    assert sells[0].attrs["currency"] == "gold"
    assert sells[0].attrs["buy_prob"] == 0.5


def test_to_ir_sells_omits_buy_prob_when_absent():
    # buy_prob absent from config -> key omitted so from_snapshot applies its
    # own default (0.5); price/currency still plumbed.
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50}]}]
    snap = AureusCsvAdapter().to_ir(wb, file_ref="outpost")
    g = snap.to_graph()
    sells = g.neighbors("shop:s", EdgeType.SELLS, direction="out")
    assert sells[0].attrs.get("price") == 50
    assert "buy_prob" not in sells[0].attrs


def test_from_ir_roundtrip_lossless_with_shop_buy_prob():
    # Relations are NOT read back by from_ir (rebuilt from entity attrs), so
    # adding relation attrs must not change from_ir(to_ir(x)) == x.
    wb = _wb()
    wb["shops"] = [{"shop_id": "shop:s", "entries": [
        {"currency": "gold", "item": "item:x", "price": 50, "buy_prob": 0.5}]}]
    adapter = AureusCsvAdapter()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref="outpost"))
    assert back == wb
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/spine/ingestion/test_aureus_adapter.py::test_to_ir_plumbs_sells_relation_attrs -v`
Expected: FAIL — `sells[0].attrs` is empty (`KeyError`/`None`), because the adapter currently builds the SELLS `Relation` without `attrs`.

- [ ] **Step 3: Write minimal implementation**

In `gameforge/spine/ingestion/aureus_adapter.py`, replace the SELLS loop (currently lines 251-258):

```python
        # SELLS (shop -> item) via shops.entries. Plumb the sink attrs the
        # economy sim reads off the relation (price/currency/buy_prob); include
        # only keys the entry actually carries so from_snapshot's price-None
        # skip and buy_prob default (0.5) stay well-defined.
        for i, shop in enumerate(workbook.get("shops", [])):
            for entry in shop.get("entries", []):
                sell_attrs = {
                    k: entry[k] for k in ("price", "currency", "buy_prob") if k in entry
                }
                g.add_relation(Relation(
                    id=rid.next(EdgeType.SELLS, shop["shop_id"], entry["item"]),
                    type=EdgeType.SELLS, src_id=shop["shop_id"], dst_id=entry["item"],
                    attrs=sell_attrs,
                    source_ref=sref("shops", i),
                ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/spine/ingestion/test_aureus_adapter.py -v`
Expected: PASS (three new tests + all existing adapter tests, incl. the existing field-level round-trip).

- [ ] **Step 5: Confirm the outpost round-trip still holds**

Run: `uv run pytest tests/spine/ingestion/test_outpost_scenario.py tests/spine/ingestion/test_roundtrip_property.py -v`
Expected: PASS — the outpost CSV round-trip diff stays ∅.

- [ ] **Step 6: Commit**

```bash
git add gameforge/spine/ingestion/aureus_adapter.py tests/spine/ingestion/test_aureus_adapter.py
git commit -m "feat(spine/ingestion): AureusCsvAdapter plumb SELLS price/currency/buy_prob(经济 sink 落地)"
```

---

### Task 3: adapter→sim sink chain + causality (differential)

**Files:**
- Test: `tests/spine/sim/test_economy.py` (append; module already imports `EconomyModel`, `EconomySimulator`, `detect_collapse`)

**Interfaces:**
- Consumes: `AureusCsvAdapter.to_ir(workbook)` → `Snapshot`; `EconomyModel.from_snapshot(snapshot)`; `EconomySimulator().run(model, seed, n_agents, n_ticks)`; `detect_collapse(result)`.
- Produces: nothing new — this task is a lock proving Task 2's plumbing causally reaches the sim. It should pass immediately after Task 2 (no production code changes here); if it fails, Task 2 was incomplete.

- [ ] **Step 1: Write the test (integration lock — expected GREEN after Task 2)**

Append to `tests/spine/sim/test_economy.py`:

```python
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter


def _econ_workbook(gold_min, gold_max, sink_price, buy_prob):
    # Minimal valid economy workbook: a gold currency, a wolf faucet
    # (gold_min/max + currency => DROPS_FROM(monster->currency)), and a shop
    # sink (SELLS with price/currency/buy_prob). Region+npc keep the snapshot
    # well-formed for to_graph.
    return {
        "regions": [{"region_id": "region:r", "name": "R",
                     "grid": {"width": 4, "height": 4, "blocked": []},
                     "start_pos": [0, 0], "scenario_id": "sc"}],
        "npcs": [{"npc_id": "npc:a", "name": "A", "region": "region:r", "pos": [1, 0]}],
        "currencies": [{"currency_id": "gold", "name": "Gold"}],
        "items": [{"item_id": "item:potion", "name": "Potion"}],
        "monsters": [{
            "monster_id": "m:wolf", "name": "Wolf",
            "stats": {"atk": 1, "def": 1, "hp": 1}, "skills": None,
            "drop_table_id": None, "ai": "aggressive",
            "gold_min": gold_min, "gold_max": gold_max,
            "currency": "gold", "kills_per_tick": 1,
        }],
        "shops": [{"shop_id": "shop:s", "entries": [
            {"currency": "gold", "item": "item:potion",
             "price": sink_price, "buy_prob": buy_prob}]}],
    }


def _model_from_wb(wb):
    return EconomyModel.from_snapshot(AureusCsvAdapter().to_ir(wb, file_ref="econ"))


def test_adapter_derived_model_has_nonempty_sink():
    model = _model_from_wb(_econ_workbook(gold_min=5, gold_max=9,
                                          sink_price=50, buy_prob=0.5))
    assert model.sources, "faucet must be modeled from CSV"
    assert model.sinks, "sink must now be modeled from CSV (the fix)"
    sink = model.sinks[0]
    assert sink["price"] == 50 and sink["buy_prob"] == 0.5 and sink["currency"] == "gold"


def test_adapter_sink_causally_prevents_collapse():
    # Balanced: small faucet (<= sink drain) + an always-buying sink -> net<=0,
    # no collapse. Runaway: same shape but a huge faucet the sink can't absorb
    # -> collapse. The ONLY difference is faucet size, so the sink is proven
    # causally load-bearing (not a measured no-op).
    balanced = _model_from_wb(_econ_workbook(gold_min=5, gold_max=9,
                                             sink_price=50, buy_prob=1.0))
    runaway = _model_from_wb(_econ_workbook(gold_min=500, gold_max=1000,
                                            sink_price=50, buy_prob=1.0))
    rb = EconomySimulator().run(balanced, seed=0, n_agents=50, n_ticks=200)
    rr = EconomySimulator().run(runaway, seed=0, n_agents=50, n_ticks=200)
    assert detect_collapse(rb) is None, "balanced faucet+sink must not collapse"
    assert detect_collapse(rr) is not None, "faucet >> sink must still collapse"
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/spine/sim/test_economy.py -v`
Expected: PASS. (If `test_adapter_derived_model_has_nonempty_sink` fails, Task 2's plumbing did not reach the sim — fix Task 2 before proceeding.)

- [ ] **Step 3: Commit**

```bash
git add tests/spine/sim/test_economy.py
git commit -m "test(spine/sim): 锁 adapter->sim sink 因果链(平衡不崩/faucet>>sink 崩)"
```

---

### Task 4: Deterministic regression locks

**Files:**
- Modify: `tests/apps/test_m1_acceptance.py` (add a clean-no-false-collapse assertion)

**Interfaces:**
- Consumes: `run_review(scenario_dir, constraints_dir[, seed])` → `ReviewReport` with `.deterministic_findings`, `.simulation_findings`, `.unproven_findings`.

- [ ] **Step 1: Write the failing/locking test**

Add to `tests/apps/test_m1_acceptance.py`:

```python
def test_clean_baseline_has_no_false_economy_collapse():
    # After plumbing SELLS sink attrs, the clean baseline gains a sink (price=50
    # potion) but has NO faucet (wolf gold cells empty) -> zero income -> the
    # sink can never fire -> no collapse. Lock that plumbing introduced no false
    # simulation-bucket positive on clean.
    report = run_review(_CLEAN, _CONSTRAINTS, seed=0)
    assert not any(f.defect_class == "economy_collapse"
                   for f in report.simulation_findings)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/apps/test_m1_acceptance.py -v`
Expected: PASS — including the existing `test_clean_baseline_has_zero_oracle_false_positives` (sinks are simulation-bucket, so `deterministic_findings`/`unproven_findings` stay `[]`) and `test_economy_sim_reproduces_collapse_with_early_warning` (economy_collapse faucet 750/tick ≫ its default sink ~25/tick, so it still collapses with an earlier early-warning tick).

- [ ] **Step 3: Run the M2b playtest byte-identical regression**

Run: `uv run pytest tests/agents/test_memory_ablation_acceptance.py tests/agents/test_playtest_harness.py -v`
Expected: PASS or SKIP (skipif-guarded on cassettes). These prove the Aureus kernel is unaffected — confirming §3 of the spec (the kernel builds shop pricing from entity `attrs["entries"]` via `snapshot_to_world`, never from the SELLS relation, so plumbing relation attrs cannot change gameplay or `state_hash`). If any playtest replay raises `CassetteReplayMiss`, STOP — plumbing unexpectedly changed a `state_hash`; investigate before continuing.

- [ ] **Step 4: Run the M3a bench regression**

Run: `uv run pytest tests/bench/ -v`
Expected: PASS — `economy_collapse` seeded BDR stays 1.0 (the bench injector sizes the faucet ≫ any sink by construction, `gameforge/bench/inject.py:451`), oracle-FP stays 0.

- [ ] **Step 5: Run lint + ruff + the full deterministic suite**

Run: `uv run lint-imports && uv run ruff check . && uv run pytest -q`
Expected: 7 import contracts KEPT, ruff clean, and the suite green **except** the three cassette-dependent M2a-part2 anchors that touch `economy_collapse` — see the note below. Confirm that the ONLY failures are `tests/agents/test_part2_acceptance.py::test_fix_pass_rate_ge_70pct` / `::test_repair_search_reproducible` (and any other `economy_collapse`-touching REPLAY) failing/erroring with `CassetteReplayMiss` on the `economy_collapse` scenario. That is the EXPECTED, correct pre-re-record RED state (Phase 2 resolves it). Any OTHER failure is a real regression — fix it before committing.

> **Why the RED is expected and unavoidable:** plumbing flips the `economy_collapse` finding's `evidence["sinks"]` from `[]` to a populated list (`gameforge/spine/sim/economy.py` `to_findings`). The repair drafter serializes `finding.evidence` verbatim into the model request (`gameforge/agents/repair/drafter.py:87-90`), so the `economy_collapse` repair request_hash changes and its existing cassette no longer matches. The other 9 scenarios' findings/evidence are unchanged, so their cassettes still hit. This re-record is FORCED, not a "free replay" — Phase 2 handles it.

- [ ] **Step 6: Commit**

```bash
git add tests/apps/test_m1_acceptance.py
git commit -m "test(regression): clean 无假 economy_collapse + 锁 M2b/M3a/oracle-FP 不受 sink plumb 影响"
```

---

## Phase 2 — economy_collapse winnability (live-gated, run by a human with gateway access)

> These steps make LIVE calls to the local LLM gateway (`http://localhost:4141`, key from `GAMEFORGE_LLM_KEY`, model `opus4.8`). They are NOT subagent tasks. Use `resume=True` recording (already wired via `record_router()` with `max_retries=8, backoff≈3.0`) so a crash/500 mid-run does not repeat completed work.

### Task 5: Re-record `economy_collapse` repair cassette (keep `repair@3`) + measure

**Files:**
- Modify: `cassettes/` (new `economy_collapse` repair cassette(s); the other 9 scenarios' cassettes are reused on-disk via resume)

- [ ] **Step 1: Live re-record (resume; only the changed request costs a call)**

Run:
```bash
GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY="$GAMEFORGE_LLM_KEY" \
  uv run python -m gameforge.agents.harness --record
```
Expected: the 9 unchanged scenarios hit on-disk cassettes (no live call, no quota); only `economy_collapse` (its request_hash changed) makes live opus calls (draft + any refine rounds). New cassette file(s) appear under `cassettes/`.

- [ ] **Step 2: Measure Fix Pass Rate under REPLAY**

Run: `uv run pytest tests/agents/test_part2_acceptance.py::test_fix_pass_rate_ge_70pct tests/agents/test_part2_acceptance.py::test_repair_search_reproducible -v`
Expected: PASS (`attempted == 10`, `fix_pass_rate >= 0.70`, reproducible). Read the actual `fix_pass_rate`:
  - **If `1.0` (10/10):** the existing `repair@3` prompt + the now-populated sink evidence was enough for the model to drive net-flow ≤ 0. Skip Task 6 → go to Task 7.
  - **If still `0.9` (9/10):** the model's patch still does not resolve the collapse (e.g. it lowered `gold_max` but not below the ~25/tick sink drain). Proceed to Task 6.

- [ ] **Step 3: Commit the new cassette(s)**

```bash
git add cassettes/
git commit -m "record(agents): economy_collapse 修复 cassette 重录(sink 就位后 evidence 变化)"
```

---

### Task 6: (CONDITIONAL — only if Task 5 left Fix Pass Rate at 9/10) strengthen the repair prompt + full re-record

**Files:**
- Modify: `gameforge/agents/prompts/library.py:65-71` (the `_REPAIR` economy_collapse guidance) and the version tuple at line 166 (`"repair.system"` → `"repair@4"`)
- Modify: `cassettes/` (changing the shared system prompt re-hashes ALL 10 repair requests → full re-record)

**Interfaces:**
- Produces: `repair.system` prompt version `repair@4`. Bumping the shared system prompt changes every repair request's `request_hash`, so all 10 scenarios re-record.

- [ ] **Step 1: Rewrite the economy_collapse guidance in `_REPAIR`**

Replace the economy_collapse sentence block (currently `gameforge/agents/prompts/library.py:65-71`, from "To fix an economy_collapse:" through "…affect the simulated economy.") with a concrete net-flow anchor that now that a real sink exists is achievable:

```python
    "To fix an economy_collapse: the currency inflates because per-tick faucet income exceeds "
    "per-tick sink drain, so the balance grows without bound. The fix is to make net flow "
    "NON-POSITIVE: expected faucet income per tick (a MONSTER/DROP_TABLE that DROPS_FROM a "
    "currency, roughly kills_per_tick * (gold_min+gold_max)/2, shown in the finding evidence's "
    "'faucets' list) must be <= expected sink drain per tick (a SHOP whose SELLS relation carries "
    "price/buy_prob, roughly price * buy_prob per sink, shown in the evidence's 'sinks' list). Two "
    "legal levers, use either or both: (1) set_entity_attr to lower the runaway faucet's gold_min "
    "and gold_max (named in the finding entities) BELOW the sink drain; (2) set_relation_attr on a "
    "real SELLS sink relation (its id is in the evidence 'sinks') to RAISE its price toward the "
    "faucet income. Do NOT add a brand-new sink or a 'consumes' entity the simulator does not model "
    "— only gold_min/gold_max on real faucets and price/buy_prob on real SELLS sinks move the "
    "simulated economy."
```

- [ ] **Step 2: Bump the prompt version**

In `gameforge/agents/prompts/library.py`, change line 166 from:

```python
    ("repair.system", "repair@3", _REPAIR),
```
to:
```python
    ("repair.system", "repair@4", _REPAIR),
```

- [ ] **Step 3: Verify the prompt renders (no brace/format break)**

Run: `uv run pytest tests/agents/test_agent_base.py -v`
Expected: PASS — the new text has no literal `{`/`}` that would break `str.format` rendering (the M2 known follow-up). If it fails on a brace, escape it and re-run.

- [ ] **Step 4: Full live re-record (all 10 re-hash)**

Run:
```bash
GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY="$GAMEFORGE_LLM_KEY" \
  uv run python -m gameforge.agents.harness --record
```
Expected: because the shared system prompt changed, all 10 scenarios' draft requests miss and re-record live (resume still protects against mid-run crashes).

- [ ] **Step 5: Re-measure**

Run: `uv run pytest tests/agents/test_part2_acceptance.py -v`
Expected: PASS; read `fix_pass_rate`. Report the honest value. **Red line:** do NOT edit `scenarios/defects/economy_collapse/*` to make it easier. If the model still cannot resolve it, keep the honest 9/10 and record why (Task 7 documents whichever outcome landed).

- [ ] **Step 6: Commit**

```bash
git add gameforge/agents/prompts/library.py cassettes/
git commit -m "feat(agents): _REPAIR 经济修复引导升级 repair@4(净流入<=0 锚点)+ 全量重录"
```

---

### Task 7: Finalize — record the honest outcome

**Files:**
- Modify (only if reached 10/10): `README.md`, `CLAUDE.md` (M2 milestone row prose "Fix Pass Rate 90%"), and the memory file `gameforge-milestone-progress.md`
- Optional: `tests/agents/test_part2_acceptance.py` (tighten the assertion to the achieved rate if it is now a stable 10/10)

- [ ] **Step 1: Update docs to the achieved rate**

If Fix Pass Rate reached 10/10 (100%): update the prose "90% (9/10)" references in `README.md` and `CLAUDE.md` to "100% (10/10)", noting the enabler was the economy sink adapter (SELLS attrs plumbed → real sink → net-flow ≤ 0 achievable). Rewrite the `gameforge-milestone-progress.md` economy_collapse "structurally unfixable" gotcha to record that plumbing the sink made it a genuinely-solvable rebalancing the agent now passes. If it stayed 9/10, instead update the gotcha to state the sink is now modeled but the agent still could not drive net-flow ≤ 0 (with the concrete reason observed), keeping the honest denominator.

- [ ] **Step 2: (only if a stable 10/10) tighten the acceptance assertion**

In `tests/agents/test_part2_acceptance.py::test_fix_pass_rate_ge_70pct`, optionally add `assert result.fix_pass_rate == 1.0` alongside the existing `>= 0.70` to lock the new floor. Only do this if the rate is reproducibly 10/10 across two REPLAY runs.

- [ ] **Step 3: Final full verification**

Run: `uv run lint-imports && uv run ruff check . && uv run pytest -q`
Expected: 7 contracts KEPT, ruff clean, full suite green (all cassette-dependent anchors now pass on the re-recorded cassettes).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(roadmap): economy sink 适配器完成 — Fix Pass Rate <achieved> + 记忆/README/CLAUDE 更新"
```

---

## Self-Review

**Spec coverage** (checked against `docs/superpowers/specs/2026-07-10-economy-sink-adapter-design.md`):
- §4.1 sink schema (`ShopEntry.buy_prob`) → Task 1. ✓
- §4.2 adapter plumb + M0a-YAML symmetric audit → Task 2 (audit resolved during exploration: only `aureus_adapter.py` emits SELLS; nothing else to fix — noted here so the executor does not hunt for a second emitter). ✓
- §4.3 repair prompt enablement + `to_findings` sinks non-empty + verifier unchanged → Task 6 (prompt) / Task 3 & 5 (sinks now populated, observed via evidence). ✓
- §5 winnability procedure (re-record-then-measure; prompt-strengthen only if needed) → Tasks 5-6, corrected: economy_collapse ALWAYS re-records (evidence changed) — the cheap-vs-full split is prompt-unchanged (economy_collapse-only via resume) vs prompt-changed (full). ✓
- §6 tests + regression locks (adapter attrs, round-trip, sim differential, clean-no-false-collapse, M2b replay, M3a bench, oracle-FP, contracts/ruff/zero-live) → Tasks 2/3/4. ✓
- §7 acceptance + docs/memory update → Task 7. ✓

**Placeholder scan:** no TBD/TODO; every code step shows real code; the one conditional (Task 6) is explicitly gated on Task 5's measured outcome, not left vague.

**Type consistency:** `ShopEntry.buy_prob: float | None`, `EdgeType.SELLS`, `EconomyModel.from_snapshot`, `EconomySimulator().run(model, seed, n_agents, n_ticks)`, `detect_collapse(result)`, `run_review(dir, constraints[, seed])`, `run_repair_corpus(default_scenario_dirs(), constraints, replay_router())` — all match the code read during planning.

**Correction propagated:** the spec's §5 "先免费重放" wording is superseded here — plumbing changes the economy_collapse repair request, so a re-record is mandatory (documented in Task 4's note and Task 5). The spec file will be footnoted to match.
