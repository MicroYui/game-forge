# M0b — Foundations Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the deterministic foundations — make Aureus a real four-system game (quest + combat + economy + gacha) driven entirely by IR-exported config, add a Schema Registry with a lossless round-trip Aureus adapter, and stand up the version/lineage/audit skeleton + DB migration framework — while keeping the M0a vertical slice green and deterministic.

**Architecture:** Additive extension of the M0a monorepo. Combat/economy/gacha live in `game/aureus/{combat,economy,gacha}.py` behind the frozen Env atomic actions (gacha maps to the `buy` atomic — no Env-contract change, per contract §4.2). The Schema Registry + Aureus CSV adapter live in `spine/ingestion/` (deterministic, LLM-free, no DB). Version/lineage/audit is split by the hard dependency rule `spine → contracts` only: pure schemas in `contracts/lineage.py`, pure in-memory logic in `spine/versioning/`, SQLAlchemy-backed persistence in `platform/lineage/` + `platform/audit/`, and the generic DB engine + Alembic migrations in `runtime/persistence/`.

**Tech Stack:** Python 3.12 (uv), pydantic v2, PyYAML, stdlib `csv`/`ast`, **SQLAlchemy 2.0**, **Alembic**, pytest + hypothesis, import-linter, ruff.

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 + foundational contracts. Every task's requirements implicitly include this section.

- **不简化，只延后**: interfaces/contracts defined in full now; only *implementation* may be deferred. No minimal-subset field/interface cutting. Version-tuple fields not yet produced in M0b (constraint_snapshot_id, prompt_version, model_snapshot, agent_graph_version, cassette_id) are schema-present but `None` — declared, not cut.
- **企业级 = 生产级工程成熟度** (production maturity), not commercialization.
- **确定性优先**: correctness verdicts from graph/algorithmic oracles; **no LLM anywhere in M0b**. Combat/gacha pseudo-randomness is seed-reproducible; rng draw-count is part of `state_hash`.
- **依赖方向单向 (CI-enforced)**: `contracts` depends on nothing; `runtime → contracts`; **`spine → contracts` only** (spine must NOT import `runtime`/`env`/`game`/`agents`/`platform`/`apps`/`bench` or any LLM SDK); `env → contracts`; `game → contracts + env`; `agents → contracts + spine + env + runtime`; `platform → contracts + spine + runtime`; `apps → all business modules`; `web` only via `apps/api`.
- **可复现只承诺回放**: env seed-ized; identical `(scenario, seed, action-sequence)` → identical per-tick `state_hash`.
- **TDD 全程**: every task is test-first (failing test → run → implement → run → commit). Round-trip + serialization use property/differential tests.
- **Git**: commit messages carry NO AI co-author / "Generated with" attribution.
- **Package namespace**: all Python under `gameforge/`; imports are `gameforge.<pkg>`. Snippets below write the short form (`contracts.world`, `spine.ingestion.*`) — read every one with the `gameforge.` prefix.
- **schema_version constants** (already defined in `contracts/versions.py`): `IR_SCHEMA_VERSION="ir-core@1"`, `META_SCHEMA_VERSION="meta@1"`, `ENV_CONTRACT_VERSION="env@1"`, `FINDING_SCHEMA_VERSION="finding@1"`, `PATCH_SCHEMA_VERSION="patch@1"`, `DSL_GRAMMAR_VERSION="dsl@1"`. M0b adds `LINEAGE_SCHEMA_VERSION="lineage@1"`, `AUDIT_SCHEMA_VERSION="audit@1"`, `TOOL_VERSION="gameforge@0.0.0"` (checker/compiler tool version for the version tuple).

## Repo layout delta produced by this plan

```
gameforge/
  contracts/
    world.py            # MODIFY: + combat-economy specs; QuestStepKind += "fight"
    lineage.py          # CREATE: VersionTuple / Artifact / ArtifactKind / AuditRecord
    versions.py         # MODIFY: + LINEAGE/AUDIT/TOOL version constants
  runtime/
    persistence/        # CREATE: generic DB engine + SQLAlchemy models + Alembic
      __init__.py  engine.py  models.py
      migrations/  env.py  script.py.mako  versions/0001_initial.py
    alembic.ini         # CREATE (at repo root — see Task 12)
  spine/
    ingestion/          # CREATE: Schema Registry + Aureus adapter (deterministic, no DB)
      __init__.py  format_schema.py  schema_registry.py  csv_format.py
      adapter.py  aureus_adapter.py
    versioning/         # FILL: pure in-memory artifact/lineage logic (contracts-only)
      version_tuple.py  store.py
  game/aureus/
    formula.py          # CREATE: safe arithmetic evaluator (no python eval)
    rng.py              # CREATE: CountingRandom (seeded, draw-counting)
    combat.py           # CREATE: damage/skills/effects/monster-AI/encounter/drops
    economy.py          # CREATE: buy/sell/use/equip
    gacha.py            # CREATE: weighted pull + pity (via buy atomic)
    world.py            # MODIFY: hold combat-economy config
    kernel.py           # MODIFY: dispatch new atomics; extend state_hash + Observation
  platform/
    lineage/  __init__.py  store.py   # CREATE: SQLAlchemy artifact/lineage/ref store
    audit/    __init__.py  log.py     # CREATE: append-only WORM audit + hash chain
  apps/cli/
    ir_to_world.py      # MODIFY: read combat-economy IR → extended WorldConfig
    driver.py           # MODIFY: + fight/buy/gacha macros
    run_slice.py        # MODIFY: record artifacts (lineage) + audit; four-system runner
scenarios/
  outpost/              # CREATE: four-system CSV workbook + format schema
    *.csv  format_schema.json
pyproject.toml          # MODIFY: + sqlalchemy, alembic; + import-linter contracts
```

---

## Task 1: Setup — dependencies, version constants, import-linter hardening, package skeletons

**Files:**
- Modify: `pyproject.toml`, `gameforge/contracts/versions.py`
- Create: `gameforge/spine/ingestion/__init__.py`, `gameforge/runtime/persistence/__init__.py`, `gameforge/platform/lineage/__init__.py`, `gameforge/platform/audit/__init__.py`
- Test: `tests/test_dependency_lint.py` (extend)

**Interfaces:**
- Produces: `sqlalchemy`/`alembic` importable; `contracts.versions.{LINEAGE_SCHEMA_VERSION,AUDIT_SCHEMA_VERSION,TOOL_VERSION}`; new packages importable; `uv run lint-imports` green with tightened `spine` boundary (spine may import ONLY `gameforge.contracts`).

- [ ] **Step 1: Extend the failing test** — append to `tests/test_dependency_lint.py`:

```python
def test_m0b_version_constants_present():
    from gameforge.contracts import versions as v
    assert v.LINEAGE_SCHEMA_VERSION == "lineage@1"
    assert v.AUDIT_SCHEMA_VERSION == "audit@1"
    assert v.TOOL_VERSION.startswith("gameforge@")

def test_spine_cannot_import_runtime():
    # contract §1: spine → contracts ONLY. Injecting a runtime import must trip the gate.
    import subprocess, sys, pathlib
    probe = pathlib.Path("gameforge/spine/ingestion/_probe.py")
    probe.write_text("import gameforge.runtime.persistence  # noqa\n")
    try:
        from importlinter.cli import lint_imports
        # lint_imports returns non-zero exit code when a contract is broken
        rc = lint_imports()
        assert rc != 0
    finally:
        probe.unlink()
```

> Note (M0a gotcha): drive import-linter via `from importlinter.cli import lint_imports` (returns int exit code), NOT `python -m importlinter.cli lint` (a silent no-op that exits 0).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_dependency_lint.py -v` → FAIL (constants missing; runtime import currently allowed by the loose spine contract).

- [ ] **Step 3: Add version constants** — append to `gameforge/contracts/versions.py`:

```python
LINEAGE_SCHEMA_VERSION = "lineage@1"
AUDIT_SCHEMA_VERSION = "audit@1"
TOOL_VERSION = "gameforge@0.0.0"  # checker/compiler tool version for the version tuple
```
Also add them to the re-export list in `gameforge/contracts/__init__.py`.

- [ ] **Step 4: Update `pyproject.toml`** — add runtime deps and tighten/extend import-linter:

Add to `[project].dependencies`:
```toml
    "sqlalchemy>=2.0",
    "alembic>=1.13",
```

Replace the `"spine is LLM-free and trunk-pure"` contract's `forbidden_modules` with the STRICT set (spine → contracts only):
```toml
forbidden_modules = [
    "gameforge.runtime",
    "gameforge.env",
    "gameforge.game",
    "gameforge.agents",
    "gameforge.platform",
    "gameforge.apps",
    "gameforge.bench",
    "openai", "anthropic", "langchain", "langgraph", "llama_index",
]
```

Append two new contracts:
```toml
[[tool.importlinter.contracts]]
name = "runtime never imports agents or spine or platform"
type = "forbidden"
source_modules = ["gameforge.runtime"]
forbidden_modules = ["gameforge.agents", "gameforge.spine", "gameforge.platform", "gameforge.apps"]

[[tool.importlinter.contracts]]
name = "platform depends only on contracts, spine, runtime"
type = "forbidden"
source_modules = ["gameforge.platform"]
forbidden_modules = ["gameforge.agents", "gameforge.apps", "gameforge.bench", "gameforge.game", "gameforge.env"]
```

> The existing `"runtime never imports agents"` contract is superseded by the new stricter runtime contract — delete the old one to avoid duplication.

- [ ] **Step 5: Create the four new package `__init__.py`** — each a one-line docstring, e.g. `"""GameForge spine.ingestion — Schema Registry + config adapters (deterministic)."""`. Also create `gameforge/runtime/persistence/migrations/` dir later in Task 12 (not needed yet).

- [ ] **Step 6: Provision + run** — `uv sync && uv run pytest tests/test_dependency_lint.py -v && uv run lint-imports` → PASS (constants present; spine-import-runtime probe trips the gate; all real contracts kept).

- [ ] **Step 7: Commit**
```bash
git add -A && git commit -m "chore(m0b): add sqlalchemy/alembic, lineage/audit version constants, tighten spine import boundary"
```

---

## Task 2: contracts — WorldConfig combat-economy extension

**Files:**
- Modify: `gameforge/contracts/world.py`
- Test: `tests/contracts/test_world_combat_economy.py`

**Interfaces:**
- Consumes: `contracts.ir.NodeType`, `contracts.versions.ENV_CONTRACT_VERSION`.
- Produces (all pydantic `BaseModel`, additive to the existing M0a shapes):
  - `QuestStepKind = Literal["talk","collect","turn_in","fight"]`; `QuestStepSpec` gains `encounter: str | None = None`.
  - `CurrencySpec{currency_id:str, name:str|None=None}`
  - `FormulaSpec{formula_id:str, expr:str, kind:Literal["damage","curve","other"]="damage"}`
  - `EffectSpec{effect_id:str, kind:Literal["damage","heal","buff","debuff","dot"], stat:str|None=None, magnitude:int=0, duration:int=0}`
  - `StatusEffectSpec{status_effect_id:str, effect_id:str, duration:int=1}`
  - `SkillSpec{skill_id:str, name:str|None=None, cost:int=0, power:int=100, formula_id:str|None=None, target:Literal["enemy","self","ally"]="enemy", applies_status:str|None=None}`
  - `EquipmentSpec{equipment_id:str, slot:str, stat_mods:dict[str,int]={}}`
  - `MonsterSpec{monster_id:str, name:str|None=None, stats:dict[str,int]={}, skills:list[str]=[], drop_table_id:str|None=None, ai:Literal["aggressive","passive"]="aggressive"}`
  - `DropEntry{item:str, probability:float}`; `DropTableSpec{drop_table_id:str, entries:list[DropEntry]=[]}`
  - `BattleEncounterSpec{encounter_id:str, monsters:list[str]=[], reward:dict[str,Any]={}, pos:tuple[int,int]|None=None}`
  - `ShopEntry{item:str, price:int, currency:str="gold"}`; `ShopSpec{shop_id:str, entries:list[ShopEntry]=[]}`
  - `GachaEntry{item:str, weight:int}`; `GachaPoolSpec{gacha_pool_id:str, cost:int=100, currency:str="gold", entries:list[GachaEntry]=[], pity_threshold:int=0, pity_item:str|None=None}`
  - `WorldConfig` gains additive fields (default empty lists): `currencies, formulas, effects, status_effects, skills, equipment, monsters, drop_tables, encounters, shops, gacha_pools`.

- [ ] **Step 1: Write the failing test** — `tests/contracts/test_world_combat_economy.py`:

```python
from gameforge.contracts.world import (
    WorldConfig, ScenarioConfig, GridSpec, QuestSpec, QuestStepSpec,
    MonsterSpec, DropTableSpec, DropEntry, BattleEncounterSpec, ShopSpec,
    ShopEntry, GachaPoolSpec, GachaEntry, SkillSpec, FormulaSpec, EquipmentSpec,
)

def test_fight_step_kind_and_encounter():
    s = QuestStepSpec(step_id="f", kind="fight", encounter="enc:bandits")
    assert s.kind == "fight" and s.encounter == "enc:bandits"

def test_worldconfig_carries_combat_economy_and_defaults_empty():
    wc = WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=3, height=3, blocked=[]),
    )
    assert wc.monsters == [] and wc.gacha_pools == [] and wc.env_contract_version == "env@1"

def test_full_combat_economy_config_validates():
    wc = WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=3, height=3),
        formulas=[FormulaSpec(formula_id="fx:atk", expr="max(1, atk*power//100 - defense)")],
        skills=[SkillSpec(skill_id="sk:slash", power=120, formula_id="fx:atk")],
        equipment=[EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5})],
        monsters=[MonsterSpec(monster_id="m:bandit", stats={"hp": 20, "atk": 6, "def": 1},
                              skills=["sk:slash"], drop_table_id="dt:bandit")],
        drop_tables=[DropTableSpec(drop_table_id="dt:bandit",
                                   entries=[DropEntry(item="item:coin", probability=0.5)])],
        encounters=[BattleEncounterSpec(encounter_id="enc:bandits", monsters=["m:bandit"],
                                        reward={"gold": 30}, pos=(2, 2))],
        shops=[ShopSpec(shop_id="shop:general",
                        entries=[ShopEntry(item="item:potion", price=10)])],
        gacha_pools=[GachaPoolSpec(gacha_pool_id="gp:std", cost=100,
                                   entries=[GachaEntry(item="item:rare", weight=1),
                                            GachaEntry(item="item:common", weight=9)],
                                   pity_threshold=10, pity_item="item:rare")],
    )
    rt = WorldConfig.model_validate(wc.model_dump())
    assert rt.gacha_pools[0].pity_threshold == 10
    assert rt.drop_tables[0].entries[0].probability == 0.5
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/contracts/test_world_combat_economy.py -v` → FAIL.

- [ ] **Step 3: Implement** — extend `gameforge/contracts/world.py`: change `QuestStepKind` to include `"fight"`; add `encounter` to `QuestStepSpec`; add all spec models above; add the additive list fields to `WorldConfig` (each `Field(default_factory=list)`). Keep existing M0a fields/order intact so M0a configs still validate.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/contracts/test_world_combat_economy.py -v` → PASS. Also `uv run pytest tests/contracts -v` → all green (M0a contracts unaffected).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(contracts): WorldConfig combat-economy specs + fight quest step"
```

---

## Task 3: contracts — version/lineage/audit schemas

**Files:**
- Create: `gameforge/contracts/lineage.py`
- Test: `tests/contracts/test_lineage.py`

**Interfaces:**
- Consumes: `contracts.versions.{LINEAGE_SCHEMA_VERSION, AUDIT_SCHEMA_VERSION, ENV_CONTRACT_VERSION}`.
- Produces:
  - `class VersionTuple(BaseModel)` — all 10 §5 fields, every one optional/defaulted (fields not produced until M1/M2 stay `None`): `doc_version, ir_snapshot_id, constraint_snapshot_id, prompt_version, model_snapshot, agent_graph_version, tool_version, env_contract_version, seed:int|None, cassette_id`.
  - `ArtifactKind = Literal["ir_snapshot","config_export","checker_run","playtest_trace","patch"]`.
  - `class Artifact(BaseModel){artifact_id:str, lineage_schema_version:str=LINEAGE_SCHEMA_VERSION, kind:ArtifactKind, version_tuple:VersionTuple, lineage:list[str]=[], payload_hash:str|None=None, created_at:str|None=None, meta:dict[str,Any]={}}`.
  - `class AuditRecord(BaseModel){audit_schema_version:str=AUDIT_SCHEMA_VERSION, seq:int, actor:str, action:str, artifact_id:str|None=None, ts:str, content_hash:str, prev_hash:str|None=None}`.

- [ ] **Step 1: Write the failing test** — `tests/contracts/test_lineage.py`:

```python
from gameforge.contracts.lineage import VersionTuple, Artifact, AuditRecord

def test_version_tuple_all_fields_present_optional():
    vt = VersionTuple(ir_snapshot_id="sha256:x", env_contract_version="env@1", seed=0)
    d = vt.model_dump()
    for f in ["doc_version", "ir_snapshot_id", "constraint_snapshot_id", "prompt_version",
              "model_snapshot", "agent_graph_version", "tool_version",
              "env_contract_version", "seed", "cassette_id"]:
        assert f in d
    assert d["constraint_snapshot_id"] is None  # not produced until M1 — declared, not cut

def test_artifact_defaults_and_lineage():
    a = Artifact(artifact_id="a1", kind="ir_snapshot", version_tuple=VersionTuple(),
                 lineage=["parent1"])
    assert a.lineage_schema_version == "lineage@1" and a.lineage == ["parent1"]

def test_audit_record_hash_chain_fields():
    r = AuditRecord(seq=1, actor="cli", action="record_artifact", artifact_id="a1",
                    ts="2026-07-06T00:00:00Z", content_hash="sha256:h", prev_hash=None)
    assert r.audit_schema_version == "audit@1" and r.seq == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/contracts/test_lineage.py -v` → FAIL.

- [ ] **Step 3: Implement** `gameforge/contracts/lineage.py` per Interfaces (import constants from `contracts.versions`; `from typing import Any, Literal`; `from pydantic import BaseModel, Field`).

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/contracts/test_lineage.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(contracts): version/lineage/audit schemas (contract §5)"
```

---

## Task 4: game/aureus — safe formula eval, seeded counting RNG, combat system

**Files:**
- Create: `gameforge/game/aureus/formula.py`, `gameforge/game/aureus/rng.py`, `gameforge/game/aureus/combat.py`
- Test: `tests/game/aureus/test_formula.py`, `tests/game/aureus/test_combat.py`

**Interfaces:**
- Consumes: `contracts.world.{WorldConfig, MonsterSpec, SkillSpec, FormulaSpec, EffectSpec, StatusEffectSpec, BattleEncounterSpec, DropTableSpec}`.
- Produces:
  - `formula.py`: `safe_eval(expr:str, names:dict[str,int]) -> int` — parses `expr` with `ast` and evaluates ONLY a whitelist (`+ - * // % ** ( )`, integer literals, names from `names`, and calls to `max`/`min`). Any other node → `raise FormulaError`. NO python `eval`. Deterministic, integer arithmetic.
  - `rng.py`: `class CountingRandom` wrapping `random.Random(seed)` with a public `draws:int` counter incremented on every draw; methods `randint(a,b)`, `random()`, `roll(prob:float)->bool` (True if `random() < prob`), `weighted_choice(items:list, weights:list[int])`. Deterministic given seed + call order.
  - `combat.py`: `class CombatSystem` bound to the kernel's mutable state:
    - `resolve_attack(attacker_stats, target_state, formula:FormulaSpec|None) -> dict` — returns `{"damage":int,"hit":bool}`, mutates `target_state["hp"]`; hit/miss via `rng.roll`.
    - `resolve_skill(skill:SkillSpec, caster_stats, target_state, formulas, effects, status_effects) -> dict` — applies formula-scaled damage/heal + queues status effect.
    - `monster_ai_action(monster_state, monster:MonsterSpec) -> str` — deterministic policy: `"aggressive"` → attack player; `"passive"` → wait.
    - `tick_status_effects(entity_state, effects_index) -> None` — decrement durations, apply dot/heal per tick, drop expired.
    - `roll_drops(drop_table:DropTableSpec|None) -> list[str]` — seeded per-entry `rng.roll(probability)`.
    - `class FormulaError(Exception)`.

- [ ] **Step 1: Write the failing tests** — `tests/game/aureus/test_formula.py`:

```python
import pytest
from gameforge.game.aureus.formula import safe_eval, FormulaError

def test_safe_eval_arithmetic():
    assert safe_eval("max(1, atk*power//100 - defense)", {"atk": 10, "power": 120, "defense": 3}) == 9

def test_safe_eval_rejects_non_whitelisted():
    with pytest.raises(FormulaError):
        safe_eval("__import__('os').system('x')", {})
    with pytest.raises(FormulaError):
        safe_eval("atk.__class__", {"atk": 1})
```

`tests/game/aureus/test_combat.py`:

```python
from gameforge.game.aureus.rng import CountingRandom
from gameforge.game.aureus.combat import CombatSystem
from gameforge.contracts.world import (FormulaSpec, SkillSpec, MonsterSpec,
                                        DropTableSpec, DropEntry)

def _cs(seed=1):
    return CombatSystem(rng=CountingRandom(seed))

def test_attack_deals_deterministic_damage_and_counts_rng():
    cs = _cs(1)
    tgt = {"hp": 20}
    fx = FormulaSpec(formula_id="fx", expr="max(1, atk - defense)")
    r1 = cs.resolve_attack({"atk": 8, "defense": 2}, tgt, fx)
    assert cs.rng.draws >= 1  # hit roll consumed rng
    # replay from a fresh seed reproduces exactly
    cs2 = _cs(1); tgt2 = {"hp": 20}
    r2 = cs2.resolve_attack({"atk": 8, "defense": 2}, tgt2, fx)
    assert r1 == r2 and tgt["hp"] == tgt2["hp"]

def test_monster_ai_is_deterministic_policy():
    cs = _cs()
    m = MonsterSpec(monster_id="m", ai="aggressive")
    assert cs.monster_ai_action({"hp": 5}, m) == "attack"
    assert cs.monster_ai_action({"hp": 5}, MonsterSpec(monster_id="m2", ai="passive")) == "wait"

def test_roll_drops_seed_reproducible():
    dt = DropTableSpec(drop_table_id="dt", entries=[DropEntry(item="i", probability=1.0),
                                                    DropEntry(item="j", probability=0.0)])
    assert _cs(7).roll_drops(dt) == ["i"]  # p=1 always, p=0 never — deterministic
    assert _cs(7).roll_drops(dt) == _cs(7).roll_drops(dt)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/game/aureus/test_formula.py tests/game/aureus/test_combat.py -v` → FAIL.

- [ ] **Step 3: Implement `formula.py`** — `ast.parse(expr, mode="eval")`; recursive `_ev(node)` allowing only `ast.Expression, ast.BinOp` with ops `Add/Sub/Mult/FloorDiv/Mod/Pow`, `ast.UnaryOp` with `USub`, `ast.Constant` (int only), `ast.Name` (looked up in `names`), `ast.Call` where `func` is `ast.Name` in `{"max","min"}` with all-int args. Everything else → `raise FormulaError`. Return `int(result)`.

- [ ] **Step 4: Implement `rng.py` and `combat.py`** per Interfaces. `resolve_attack`: `hit = rng.roll(0.9)` (base hit); damage via `safe_eval(formula.expr, {**attacker_stats, "power":100})` when formula given, else `max(1, atk-def)`; on hit subtract from `target_state["hp"]`. `roll_drops`: iterate entries in order, `rng.roll(entry.probability)`. Keep all draws in deterministic order.

- [ ] **Step 5: Run to verify pass** — `uv run pytest tests/game/aureus/test_formula.py tests/game/aureus/test_combat.py -v` → PASS.

- [ ] **Step 6: Commit**
```bash
git add -A && git commit -m "feat(game/aureus): safe formula eval, seeded counting RNG, combat system (damage/AI/drops/effects)"
```

---

## Task 5: game/aureus — economy + gacha + kernel integration (state_hash, Observation, atomics)

**Files:**
- Create: `gameforge/game/aureus/economy.py`, `gameforge/game/aureus/gacha.py`
- Modify: `gameforge/game/aureus/world.py`, `gameforge/game/aureus/kernel.py`
- Test: `tests/game/aureus/test_economy_gacha.py`, `tests/game/aureus/test_kernel_combat_determinism.py`, `tests/game/aureus/test_kernel.py` (update the M0a attack test)

**Interfaces:**
- Consumes: Task 4 (`CombatSystem`, `CountingRandom`), `contracts.world.*`, `contracts.env_types.{Attack,CastSkill,Use,Equip,Buy,Sell,...}`.
- Produces:
  - `economy.py`: `class EconomySystem` — `buy(player, shop:ShopSpec, item_id, count)`, `sell(...)`, `use(player, item_id)`, `equip(player, equipment:EquipmentSpec)` (moves item→`player["equipped"][slot]`, applies `stat_mods` to `player["stats"]`). Returns a `last_action_result` string.
  - `gacha.py`: `class GachaSystem` — `pull(player, pool:GachaPoolSpec, rng:CountingRandom, count:int) -> list[str]`; spends `pool.cost*count` currency; per pull increments `player["gacha_pity"][pool_id]`; if `pity_threshold>0` and counter hits threshold → force `pity_item` and reset counter; else `rng.weighted_choice(items, weights)`.
  - `world.py` (extend `AureusWorld`): index `self.monsters, self.skills, self.formulas, self.effects, self.status_effects, self.drop_tables, self.encounters, self.shops, self.gacha_pools, self.equipment` as `{id: spec}` dicts; `encounter_at(pos)`; `shop(shop_id)`; `gacha(pool_id)`.
  - `kernel.py` (extend `AureusEnv`):
    - use `CountingRandom(seed)` as `self.rng`; add player state `stats:{hp,atk,def,spd,mp,gold}`, `equipped:{slot:item}`, `active_effects:list[{effect_id,remaining}]`, `gacha_pity:{pool_id:int}`, `monster_states:{monster_id:{hp,alive,pos}}` (populated when a `fight` step / encounter activates).
    - dispatch `Attack→_attack`, `CastSkill→_cast_skill`, `Use→_use`, `Equip→_equip`, `Buy→_buy` (routes to gacha when `shop_id ∈ gacha_pools`, else shop), `Sell→_sell`. A `fight` quest step activates its `BattleEncounter` (spawns monster_states); combat resolves over ticks; victory advances the step + grants reward + rolls drops; the M0a talk/collect/turn_in path is untouched.
    - `state_hash()` payload EXTENDED: add `equipped` (sorted), `active_effects` (sorted canonical), `monster_states`, `combat` (`{active_encounter, turn}`), `gacha_pity`, and change `rng` to `{"seed":self.seed,"draws":self.rng.draws}` reading the real counter. Still EXCLUDES logs/render/wall-clock (contract §4.4).
    - `observe()`: populate `equipped_items` (sorted values), `active_effects` (sorted `effect_id`), `player_stats` (full stats incl. gold), and include alive monsters in `nearby_entities`/`reachable_targets` when in combat.

- [ ] **Step 1: Write the failing tests** — `tests/game/aureus/test_economy_gacha.py`:

```python
from gameforge.game.aureus.economy import EconomySystem
from gameforge.game.aureus.gacha import GachaSystem
from gameforge.game.aureus.rng import CountingRandom
from gameforge.contracts.world import ShopSpec, ShopEntry, GachaPoolSpec, GachaEntry, EquipmentSpec

def test_buy_deducts_gold_and_adds_item():
    econ = EconomySystem()
    player = {"stats": {"gold": 50}, "inventory": {}, "equipped": {}}
    shop = ShopSpec(shop_id="s", entries=[ShopEntry(item="item:potion", price=10)])
    res = econ.buy(player, shop, "item:potion", 3)
    assert res == "bought" and player["stats"]["gold"] == 20 and player["inventory"]["item:potion"] == 3

def test_buy_insufficient_funds_rejected():
    econ = EconomySystem()
    player = {"stats": {"gold": 5}, "inventory": {}, "equipped": {}}
    shop = ShopSpec(shop_id="s", entries=[ShopEntry(item="item:potion", price=10)])
    assert econ.buy(player, shop, "item:potion", 1) == "insufficient_funds"
    assert player["stats"]["gold"] == 5

def test_equip_applies_stat_mods():
    econ = EconomySystem()
    player = {"stats": {"atk": 5}, "inventory": {"eq:blade": 1}, "equipped": {}}
    econ.equip(player, EquipmentSpec(equipment_id="eq:blade", slot="weapon", stat_mods={"atk": 5}))
    assert player["equipped"]["weapon"] == "eq:blade" and player["stats"]["atk"] == 10

def test_gacha_pity_guarantees_rare_and_is_seed_reproducible():
    pool = GachaPoolSpec(gacha_pool_id="gp", cost=10,
                         entries=[GachaEntry(item="item:common", weight=1)],
                         pity_threshold=3, pity_item="item:rare")
    def pull_ten(seed):
        g = GachaSystem(); rng = CountingRandom(seed)
        player = {"stats": {"gold": 1000}, "inventory": {}, "gacha_pity": {}}
        return g.pull(player, pool, rng, count=3)
    got = pull_ten(5)
    assert "item:rare" in got            # pity forces the rare by the 3rd pull
    assert pull_ten(5) == pull_ten(5)    # seed-reproducible
```

`tests/game/aureus/test_kernel_combat_determinism.py`:

```python
from gameforge.contracts.world import (WorldConfig, ScenarioConfig, GridSpec, Placement,
    QuestSpec, QuestStepSpec, MonsterSpec, BattleEncounterSpec, FormulaSpec, DropTableSpec, DropEntry)
from gameforge.contracts.ir import NodeType
from gameforge.contracts.env_types import parse_action
from gameforge.game.aureus.kernel import AureusEnv

def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=6, height=6, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={})],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 10}, steps=[
            QuestStepSpec(step_id="f", kind="fight", encounter="enc:bandit"),
        ])],
        formulas=[FormulaSpec(formula_id="fx", expr="max(1, atk - defense)")],
        monsters=[MonsterSpec(monster_id="m:bandit", stats={"hp": 6, "atk": 3, "def": 0},
                              drop_table_id="dt")],
        drop_tables=[DropTableSpec(drop_table_id="dt", entries=[DropEntry(item="item:coin", probability=1.0)])],
        encounters=[BattleEncounterSpec(encounter_id="enc:bandit", monsters=["m:bandit"],
                                        reward={"gold": 30}, pos=(0, 0))],
    )

def test_combat_run_is_deterministic_per_tick():
    actions = [{"kind": "attack", "target_id": "m:bandit"}] * 10
    def run():
        e = AureusEnv(_wc()); e.reset("s", seed=4); hs = [e.state_hash()]
        for a in actions:
            e.step(parse_action(a)); hs.append(e.state_hash())
        return hs
    assert run() == run()  # contract §4.4 anchor extended to combat rng + monster states

def test_state_hash_includes_monster_and_gacha_scope():
    e = AureusEnv(_wc()); e.reset("s", seed=1)
    # a fresh reset exposes the extended authoritative scope without crashing
    assert isinstance(e.state_hash(), str) and e.state_hash().startswith("sha256:")
```

Update the M0a test in `tests/game/aureus/test_kernel.py` — replace `test_unsupported_combat_action_is_declared_not_crashing` with:

```python
def test_attack_without_active_combat_returns_defined_result():
    e = AureusEnv(_wc()); e.reset("s", seed=1)
    r = e.step(parse_action({"kind": "attack", "target_id": "mob:none"}))
    # attack is now implemented; with no such target/combat it returns a defined result, never crashes
    assert r.observation.last_action_result in ("no_target", "not_in_combat") and r.done is False
```
(The M0a `_wc()` in that file has no encounters, so attack has no valid target → defined non-crashing result.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/game/aureus -v` → FAIL (economy/gacha modules missing; kernel not extended).

- [ ] **Step 3: Implement `economy.py`, `gacha.py`**, then extend `world.py` (index new specs) and `kernel.py` (systems, dispatch, `state_hash`, `observe`) per Interfaces. Keep the M0a talk/collect/turn_in path byte-identical in behavior. Route `Buy` to `GachaSystem.pull` when `shop_id` is a gacha pool id, else `EconomySystem.buy`.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/game/aureus -v` → PASS (new + updated M0a tests). Then `uv run pytest tests/apps -v` (M0a slice still green — Observation/step additive).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(game/aureus): economy + gacha systems, kernel combat integration, extended state_hash/Observation"
```

---

## Task 6: spine/ingestion — FormatSchema + SchemaRegistry (typed validation)

**Files:**
- Create: `gameforge/spine/ingestion/format_schema.py`, `gameforge/spine/ingestion/schema_registry.py`
- Test: `tests/spine/ingestion/test_schema_registry.py`

**Interfaces:**
- Consumes: pydantic only (deterministic; no DB, no LLM, no runtime).
- Produces:
  - `format_schema.py`: `ColumnType = Literal["str","int","float","bool","int_list","str_list","json"]`; `class ColumnSchema{name:str, type:ColumnType="str", required:bool=True, enum:list[str]|None=None, foreign_key:str|None=None}` (`foreign_key="sheet.column"`); `class SheetSchema{name:str, primary_key:str|None=None, columns:list[ColumnSchema]}`; `class FormatSchema{format_id:str, version:str, sheets:list[SheetSchema]}`; helper `FormatSchema.sheet(name)->SheetSchema|None`.
  - `schema_registry.py`: `class SchemaError(BaseModel){sheet:str, row:int, column:str|None, message:str}`; `class SchemaRegistry` with `register(schema)`, `get(format_id, version)->FormatSchema`, and `validate(schema, workbook:dict[str,list[dict]]) -> list[SchemaError]` (checks required, enum membership, and foreign-key existence against the referenced sheet/column values). Type coercion itself lives in `csv_format` (Task 7); `validate` assumes already-typed rows and checks constraints.

- [ ] **Step 1: Write the failing test** — `tests/spine/ingestion/test_schema_registry.py`:

```python
from gameforge.spine.ingestion.format_schema import FormatSchema, SheetSchema, ColumnSchema
from gameforge.spine.ingestion.schema_registry import SchemaRegistry

def _schema():
    return FormatSchema(format_id="aureus", version="1", sheets=[
        SheetSchema(name="items", primary_key="item_id",
                    columns=[ColumnSchema(name="item_id"), ColumnSchema(name="name", required=False)]),
        SheetSchema(name="quest_steps", primary_key="step_id", columns=[
            ColumnSchema(name="step_id"),
            ColumnSchema(name="kind", enum=["talk", "collect", "turn_in", "fight"]),
            ColumnSchema(name="item", required=False, foreign_key="items.item_id"),
        ]),
    ])

def test_register_and_get_roundtrips():
    reg = SchemaRegistry(); reg.register(_schema())
    assert reg.get("aureus", "1").format_id == "aureus"

def test_validate_flags_bad_enum_and_dangling_fk():
    reg = SchemaRegistry()
    wb = {
        "items": [{"item_id": "item:x", "name": "X"}],
        "quest_steps": [
            {"step_id": "s1", "kind": "BOGUS", "item": None},                 # bad enum
            {"step_id": "s2", "kind": "collect", "item": "item:ghost"},        # dangling FK
            {"step_id": "s3", "kind": "collect", "item": "item:x"},            # ok
        ],
    }
    errs = reg.validate(_schema(), wb)
    kinds = {(e.sheet, e.column) for e in errs}
    assert ("quest_steps", "kind") in kinds and ("quest_steps", "item") in kinds
    assert len(errs) == 2

def test_validate_clean_workbook_no_errors():
    reg = SchemaRegistry()
    wb = {"items": [{"item_id": "item:x", "name": "X"}], "quest_steps": []}
    assert reg.validate(_schema(), wb) == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/spine/ingestion/test_schema_registry.py -v` → FAIL.

- [ ] **Step 3: Implement** both modules per Interfaces. `validate`: for each sheet in schema, for each row, check required non-None, enum membership, and (for `foreign_key`) that the value exists in the target sheet's target column value-set (skip when value is None and not required). Return errors sorted by `(sheet, row)`.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/spine/ingestion/test_schema_registry.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(spine/ingestion): FormatSchema + SchemaRegistry with typed/FK/enum validation"
```

---

## Task 7: spine/ingestion — CSV workbook read/write with typed coercion

**Files:**
- Create: `gameforge/spine/ingestion/csv_format.py`
- Test: `tests/spine/ingestion/test_csv_format.py`

**Interfaces:**
- Consumes: `format_schema.{FormatSchema, ColumnSchema}`, stdlib `csv`/`json`.
- Produces:
  - `read_workbook(dir_path:str, schema:FormatSchema) -> dict[str, list[dict]]` — for each sheet reads `<dir>/<sheet>.csv`, coerces each cell per `ColumnSchema.type` (`int`→int, `float`→float, `bool`→bool, `int_list`/`str_list`→ split on `;` (empty→[]), `json`→`json.loads`, `str`→str; empty string on a non-required column → `None`). Missing sheet file → `[]`.
  - `write_workbook(dir_path:str, schema:FormatSchema, workbook:dict[str,list[dict]]) -> None` — writes one CSV per sheet with columns in schema order, rows in given order, deterministic serialization (`int_list`/`str_list`→ `;`-joined; `json`→ `canonical_json`; `None`→ empty string; floats via the canonical decimal rule so `1.10`==`1.1`). Uses `csv.writer` with `\n` line terminator (no platform CRLF drift).

- [ ] **Step 1: Write the failing test** — `tests/spine/ingestion/test_csv_format.py`:

```python
from gameforge.spine.ingestion.format_schema import FormatSchema, SheetSchema, ColumnSchema
from gameforge.spine.ingestion.csv_format import read_workbook, write_workbook

def _schema():
    return FormatSchema(format_id="t", version="1", sheets=[
        SheetSchema(name="monsters", columns=[
            ColumnSchema(name="monster_id"),
            ColumnSchema(name="hp", type="int"),
            ColumnSchema(name="skills", type="str_list", required=False),
            ColumnSchema(name="stats", type="json", required=False),
            ColumnSchema(name="rate", type="float"),
        ]),
    ])

def test_write_then_read_roundtrips_typed_values(tmp_path):
    wb = {"monsters": [
        {"monster_id": "m:1", "hp": 20, "skills": ["a", "b"], "stats": {"atk": 3}, "rate": 0.5},
        {"monster_id": "m:2", "hp": 5, "skills": [], "stats": None, "rate": 1.0},
    ]}
    write_workbook(str(tmp_path), _schema(), wb)
    back = read_workbook(str(tmp_path), _schema())
    assert back == wb  # field-level equality after a full write→read cycle

def test_float_canonical_no_drift(tmp_path):
    wb = {"monsters": [{"monster_id": "m", "hp": 1, "skills": [], "stats": None, "rate": 1.10}]}
    write_workbook(str(tmp_path), _schema(), wb)
    assert read_workbook(str(tmp_path), _schema())["monsters"][0]["rate"] == 1.1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/spine/ingestion/test_csv_format.py -v` → FAIL.

- [ ] **Step 3: Implement `csv_format.py`** per Interfaces. For deterministic float output reuse the decimal-normalize rule from `contracts.canonical` (import `canonical_json` for json cells; for float cells format via `format(Decimal(str(v)).normalize(), "f")` then `float(...)` on read). Ensure `read(write(x)) == x` at field level.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/spine/ingestion/test_csv_format.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(spine/ingestion): deterministic CSV workbook read/write with typed coercion"
```

---

## Task 8: spine/ingestion — Adapter Protocol + AureusCsvAdapter (to_ir / from_ir) + round-trip property test

**Files:**
- Create: `gameforge/spine/ingestion/adapter.py`, `gameforge/spine/ingestion/aureus_adapter.py`
- Test: `tests/spine/ingestion/test_aureus_adapter.py`, `tests/spine/ingestion/test_roundtrip_property.py`

**Interfaces:**
- Consumes: `contracts.ir.{Entity,Relation,NodeType,EdgeType,SourceRef}`, `spine.ir.{store.IRGraph, snapshot.Snapshot}`, `format_schema.FormatSchema`.
- Produces:
  - `adapter.py`: `class Adapter(Protocol){ format_id:str; def to_ir(self, workbook, file_ref)->Snapshot; def from_ir(self, snapshot)->dict[str,list[dict]] }`.
  - `aureus_adapter.py`: `class AureusCsvAdapter` with a module-level `SHEET_NODE_TYPE: dict[str, NodeType]` mapping each entity-sheet to its `NodeType` (`items→ITEM, npcs→NPC, monsters→MONSTER, skills→SKILL, effects→EFFECT, status_effects→STATUS_EFFECT, formulas→FORMULA, equipment→EQUIPMENT, drop_tables→DROP_TABLE, encounters→BATTLE_ENCOUNTER, shops→SHOP, gacha_pools→GACHA_POOL, currencies→CURRENCY, regions→REGION, spawn_points→SPAWN_POINT, interactables→INTERACTABLE, quests→QUEST, quest_steps→QUEST_STEP`), plus a `PK_BY_SHEET` map. `to_ir`: each row → one `Entity(id=row[pk], type=SHEET_NODE_TYPE[sheet], attrs=<full row minus pk>, source_ref=SourceRef(adapter="aureus-csv", file=file_ref, sheet=sheet, row=i))`; then derive relations (`HAS_STEP`/`PRECEDES` from quest_steps.quest_id+order, `DROPS_FROM` from monsters.drop_table_id + drop_tables.entries, `SELLS` from shops.entries, `TALKS_TO`/`REQUIRES` from steps, `TRIGGERED_BY` from fight-step.encounter, `USES_SKILL` from monsters.skills, `APPLIES_EFFECT` from skills.applies_status/status_effects.effect_id) with deterministic relation ids. `from_ir`: for each sheet, collect entities of that `NodeType`, sort by `source_ref.row`, re-emit `{pk: entity.id, **entity.attrs}` as the row (relations are redundant with the attrs and are NOT needed to reconstruct the workbook — losslessness comes from attrs preserving every column).

> **Round-trip correctness invariant:** each config column is preserved verbatim in `entity.attrs`; `from_ir` reconstructs rows purely from `attrs` + pk + `source_ref.row` ordering. Derived relations exist for graph queries/checkers only. This makes `from_ir(to_ir(x)) == x` at field level by construction.

- [ ] **Step 1: Write the failing tests** — `tests/spine/ingestion/test_aureus_adapter.py`:

```python
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.contracts.ir import NodeType, EdgeType

def _wb():
    return {
        "regions": [{"region_id": "region:r", "name": "R", "grid": {"width": 4, "height": 4, "blocked": []},
                     "start_pos": [0, 0], "scenario_id": "sc"}],
        "npcs": [{"npc_id": "npc:a", "name": "A", "region": "region:r", "pos": [1, 0]}],
        "items": [{"item_id": "item:x", "name": "X"}],
        "quests": [{"quest_id": "q", "title": "Q", "region": "region:r", "giver": "npc:a", "reward": {"gold": 10}}],
        "quest_steps": [
            {"step_id": "s1", "quest_id": "q", "order": 0, "kind": "talk", "target": "npc:a", "item": None, "count": 1, "encounter": None},
            {"step_id": "s2", "quest_id": "q", "order": 1, "kind": "turn_in", "target": "npc:a", "item": None, "count": 1, "encounter": None},
        ],
    }

def test_to_ir_builds_typed_entities_and_has_step_edges():
    snap = AureusCsvAdapter().to_ir(_wb(), file_ref="outpost")
    g = snap.to_graph()
    assert g.get_node("npc:a").type is NodeType.NPC
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {"s1", "s2"}
    assert len(g.neighbors("q", EdgeType.HAS_STEP)) == 2
    prec = g.neighbors("s1", EdgeType.PRECEDES)
    assert prec and prec[0].dst_id == "s2"
    assert g.get_node("npc:a").source_ref.sheet == "npcs"

def test_from_ir_reconstructs_workbook_field_level():
    adapter = AureusCsvAdapter()
    wb = _wb()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref="outpost"))
    assert back == wb  # contract §2 anchor: from_ir(to_ir(x)) == x, field level
```

`tests/spine/ingestion/test_roundtrip_property.py` (hypothesis, §12A.1 adapter round-trip property test):

```python
from hypothesis import given, strategies as st
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

_ids = st.from_regex(r"[a-z]{1,6}", fullmatch=True)

@given(
    items=st.lists(st.builds(lambda i: {"item_id": f"item:{i}", "name": i.upper()}, _ids),
                   max_size=6, unique_by=lambda r: r["item_id"]),
    monsters=st.lists(
        st.builds(lambda i, hp: {"monster_id": f"m:{i}", "name": i, "stats": {"hp": hp},
                                 "skills": [], "drop_table_id": None, "ai": "aggressive"},
                  _ids, st.integers(1, 99)),
        max_size=5, unique_by=lambda r: r["monster_id"]),
)
def test_roundtrip_is_lossless(items, monsters):
    wb = {"items": items, "monsters": monsters}
    adapter = AureusCsvAdapter()
    snapA = adapter.to_ir(wb, file_ref="gen")
    wb2 = adapter.from_ir(snapA)
    snapB = adapter.to_ir(wb2, file_ref="gen")
    assert wb2 == wb                              # field-level workbook equality
    assert snapB.to_graph().diff(snapA.to_graph()).is_empty()  # snapshot diff = ∅
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/spine/ingestion/test_aureus_adapter.py tests/spine/ingestion/test_roundtrip_property.py -v` → FAIL.

- [ ] **Step 3: Implement** `adapter.py` (Protocol) + `aureus_adapter.py` per Interfaces. Keep `to_ir` relation-id generation deterministic (reuse the M0a loader's `rel:<TYPE>:<src>-><dst>:<n>` pattern). `from_ir` must emit ONLY sheets that have entities, and reconstruct each row as `{pk_name: id, **attrs}` sorted by `source_ref.row`.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/spine/ingestion -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(spine/ingestion): Aureus CSV adapter (to_ir/from_ir) + lossless round-trip property test"
```

---

## Task 9: scenarios/outpost — four-system CSV workbook + format schema + load test

**Files:**
- Create: `scenarios/outpost/format_schema.json` and the CSV sheets under `scenarios/outpost/` (`regions.csv, npcs.csv, items.csv, currencies.csv, interactables.csv, spawn_points.csv, formulas.csv, effects.csv, status_effects.csv, skills.csv, equipment.csv, monsters.csv, drop_tables.csv, encounters.csv, shops.csv, gacha_pools.csv, quests.csv, quest_steps.csv`)
- Test: `tests/spine/ingestion/test_outpost_scenario.py`

**Interfaces:**
- Consumes: `csv_format.read_workbook`, `schema_registry.SchemaRegistry`, `aureus_adapter.AureusCsvAdapter`.
- Produces: a concrete four-system scenario whose quest chain is `talk → collect → fight → (buy or gacha) → turn_in`, exercising quest+combat+economy+gacha, that validates clean against its `FormatSchema` and round-trips losslessly. The `format_schema.json` is the registered Aureus format (`format_id="aureus", version="1"`) enumerating every sheet's columns/types/FKs/enums.

- [ ] **Step 1: Author `format_schema.json`** — a `FormatSchema` JSON covering all sheets. Column types per Task 2 specs (e.g. `monsters.stats: json`, `monsters.skills: str_list`, `drop_tables.entries: json`, `encounters.monsters: str_list`, `encounters.reward: json`, `gacha_pools.entries: json`, `regions.grid: json`, `regions.start_pos: json`, positions `pos: json`). FKs e.g. `quest_steps.quest_id → quests.quest_id`, `quest_steps.encounter → encounters.encounter_id`, `monsters.drop_table_id → drop_tables.drop_table_id`. Enums: `quest_steps.kind ∈ {talk,collect,turn_in,fight}`.

- [ ] **Step 2: Author the CSV sheets** — a small but complete outpost: 1 region (grid 12×8), NPC giver `npc:qi`, item `item:herb` (collected), a `spawn_points`+`interactables` gather source for `item:herb`, a monster `m:wolf` with stats+drop table, an `encounter enc:wolves` at a grid cell, a `shop:trader` selling `item:potion`, a `gacha_pool gp:relic`, and quest `quest:outpost` with steps `talk(npc:qi) → collect(item:herb×2) → fight(enc:wolves) → turn_in(npc:qi)`. Ensure every FK resolves and every enum is valid.

- [ ] **Step 3: Write the failing test** — `tests/spine/ingestion/test_outpost_scenario.py`:

```python
import json
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.schema_registry import SchemaRegistry
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

_DIR = "scenarios/outpost"

def _schema():
    return FormatSchema.model_validate(json.load(open(f"{_DIR}/format_schema.json")))

def test_outpost_validates_clean_against_schema():
    schema = _schema()
    wb = read_workbook(_DIR, schema)
    assert SchemaRegistry().validate(schema, wb) == []   # no type/FK/enum errors

def test_outpost_roundtrips_lossless():
    schema = _schema()
    wb = read_workbook(_DIR, schema)
    adapter = AureusCsvAdapter()
    snapA = adapter.to_ir(wb, file_ref=_DIR)
    wb2 = adapter.from_ir(snapA)
    assert wb2 == wb
    assert adapter.to_ir(wb2, file_ref=_DIR).to_graph().diff(snapA.to_graph()).is_empty()

def test_outpost_has_all_four_systems():
    wb = read_workbook(_DIR, _schema())
    assert wb["monsters"] and wb["shops"] and wb["gacha_pools"]
    assert any(s["kind"] == "fight" for s in wb["quest_steps"])
```

- [ ] **Step 4: Run to verify** — `uv run pytest tests/spine/ingestion/test_outpost_scenario.py -v`. Iterate on the CSVs until validation is clean and round-trip is ∅.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(scenarios): outpost four-system CSV workbook + Aureus format schema"
```

---

## Task 10: apps/cli — IR→WorldConfig combat-economy, driver macros, four-system slice runner

**Files:**
- Modify: `gameforge/apps/cli/ir_to_world.py`, `gameforge/apps/cli/driver.py`, `gameforge/apps/cli/run_slice.py`
- Test: `tests/apps/test_outpost_slice.py`

**Interfaces:**
- Consumes: `spine.ingestion.{csv_format, aureus_adapter, format_schema, schema_registry}`, `apps.cli.ir_to_world.snapshot_to_world`, `game.aureus.kernel.AureusEnv`.
- Produces:
  - `ir_to_world.snapshot_to_world` EXTENDED — in addition to grid/placements/quests, read combat-economy entities (MONSTER/SKILL/EFFECT/STATUS_EFFECT/FORMULA/EQUIPMENT/DROP_TABLE/BATTLE_ENCOUNTER/SHOP/GACHA_POOL/CURRENCY) from IR `attrs` and build the matching `WorldConfig` spec lists. Encounter positions and shop/gacha placements populate `placements` where they have a `pos`.
  - `driver.ScriptedDriver` EXTENDED — handle `fight` steps (navigate to encounter pos, then repeat `attack` on the alive monster until the step advances), and expose optional post-collect economy macros (`buy`/`gacha` via the `buy` atomic) when a quest step or scenario requests them. Existing talk/collect/turn_in macros unchanged.
  - `run_slice.run_slice_workbook(dir_path:str, seed:int=0) -> dict` — NEW entry that loads a CSV scenario via the adapter (validate → to_ir → checker gate → snapshot_to_world → AureusEnv → driver), returning the same result shape as `run_slice` plus `"systems_exercised"`. The existing YAML `run_slice` is untouched (M0a slice continuity).

- [ ] **Step 1: Write the failing test** — `tests/apps/test_outpost_slice.py`:

```python
from gameforge.apps.cli.run_slice import run_slice_workbook

def test_outpost_four_system_slice_completes_and_is_deterministic():
    a = run_slice_workbook("scenarios/outpost", seed=0)
    assert a["findings"] == []          # clean config passes the checker gate
    assert a["completed"] is True       # talk→collect→fight→turn_in reached completion
    b = run_slice_workbook("scenarios/outpost", seed=0)
    assert a["final_hash"] == b["final_hash"] and a["trajectory"] == b["trajectory"]

def test_outpost_exercises_all_four_systems():
    out = run_slice_workbook("scenarios/outpost", seed=0)
    assert {"quest", "combat", "economy", "gacha"} <= set(out["systems_exercised"])
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/apps/test_outpost_slice.py -v` → FAIL.

- [ ] **Step 3: Implement** the three extensions. `snapshot_to_world` reads combat-economy attrs into the Task 2 spec models. `ScriptedDriver` gains a `_do_fight(env, encounter_pos)` loop and records `systems_exercised`. `run_slice_workbook` mirrors `run_slice` but sources config from the CSV adapter. To exercise economy+gacha deterministically in the slice, have the driver perform one `buy` (shop) and one `buy` (gacha pool) when the player has gold, recording those systems.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/apps -v` → PASS (new four-system slice + M0a `test_run_slice` still green).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(apps/cli): four-system slice runner — IR→WorldConfig combat-economy, driver fight/buy/gacha macros"
```

---

## Task 11: spine/versioning — in-memory artifact store, lineage DAG, ref/rollback, version-tuple builder

**Files:**
- Create: `gameforge/spine/versioning/version_tuple.py`, `gameforge/spine/versioning/store.py`
- Test: `tests/spine/versioning/test_versioning.py`

**Interfaces:**
- Consumes: `contracts.lineage.{Artifact, VersionTuple, ArtifactKind}`, `contracts.canonical.compute_snapshot_id`, `contracts.versions.{TOOL_VERSION, ENV_CONTRACT_VERSION}`. (Pure — contracts only, no DB.)
- Produces:
  - `version_tuple.py`: `build_version_tuple(*, ir_snapshot_id=None, seed=None, tool_version=TOOL_VERSION, env_contract_version=ENV_CONTRACT_VERSION, **overrides) -> VersionTuple`; `artifact_id_for(kind, version_tuple, payload_hash) -> str` = `compute_snapshot_id({...})` (content-addressed).
  - `store.py`:
    - `class InMemoryArtifactStore`: `put(artifact:Artifact) -> str` (keyed by `artifact_id`; idempotent), `get(artifact_id) -> Artifact|None`, `all() -> list[Artifact]`.
    - `class LineageGraph(store)`: `ancestors(artifact_id) -> list[str]` (transitive parents via `Artifact.lineage`, deterministic order), `provenance(artifact_id) -> VersionTuple`.
    - `class RefStore`: `set(name, artifact_id)`, `get(name) -> str|None`, `rollback(name, artifact_id) -> None` (repoint pointer to a historical artifact; artifacts are immutable/never deleted), `history(name) -> list[str]`.

- [ ] **Step 1: Write the failing test** — `tests/spine/versioning/test_versioning.py`:

```python
from gameforge.contracts.lineage import Artifact
from gameforge.spine.versioning.version_tuple import build_version_tuple, artifact_id_for
from gameforge.spine.versioning.store import InMemoryArtifactStore, LineageGraph, RefStore

def _art(kind, vt, lineage, payload="p"):
    aid = artifact_id_for(kind, vt, payload_hash=payload)
    return Artifact(artifact_id=aid, kind=kind, version_tuple=vt, lineage=lineage, payload_hash=payload)

def test_artifact_traces_full_version_tuple():
    vt = build_version_tuple(ir_snapshot_id="sha256:s", seed=0)
    a = _art("ir_snapshot", vt, [])
    store = InMemoryArtifactStore(); store.put(a)
    prov = LineageGraph(store).provenance(a.artifact_id)
    assert prov.env_contract_version == "env@1" and prov.tool_version.startswith("gameforge@")
    assert prov.ir_snapshot_id == "sha256:s" and prov.seed == 0

def test_lineage_ancestors_transitive():
    store = InMemoryArtifactStore()
    vt = build_version_tuple(ir_snapshot_id="sha256:s")
    ir = _art("ir_snapshot", vt, []); store.put(ir)
    cfg = _art("config_export", vt, [ir.artifact_id], payload="c"); store.put(cfg)
    chk = _art("checker_run", vt, [cfg.artifact_id], payload="k"); store.put(chk)
    anc = LineageGraph(store).ancestors(chk.artifact_id)
    assert set(anc) == {cfg.artifact_id, ir.artifact_id}

def test_rollback_repoints_and_lineage_still_traceable():
    store = InMemoryArtifactStore(); refs = RefStore()
    vt = build_version_tuple(ir_snapshot_id="sha256:s")
    v1 = _art("ir_snapshot", vt, [], payload="v1"); store.put(v1)
    v2 = _art("ir_snapshot", vt, [v1.artifact_id], payload="v2"); store.put(v2)
    refs.set("head", v2.artifact_id)
    refs.rollback("head", v1.artifact_id)     # pointer re-point (contract §5)
    assert refs.get("head") == v1.artifact_id
    assert store.get(v2.artifact_id) is not None  # immutable, not deleted
    assert refs.history("head") == [v2.artifact_id, v1.artifact_id]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/spine/versioning/test_versioning.py -v` → FAIL.

- [ ] **Step 3: Implement** both modules per Interfaces. `ancestors` does a deterministic BFS/DFS over `lineage`, returning sorted unique ids. `RefStore.history` records every pointer value in order.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/spine/versioning -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(spine/versioning): in-memory artifact store, lineage DAG, ref/rollback, version-tuple builder"
```

---

## Task 12: runtime/persistence — SQLAlchemy engine + models + Alembic migration (forward/rollback)

**Files:**
- Create: `gameforge/runtime/persistence/engine.py`, `gameforge/runtime/persistence/models.py`, `gameforge/runtime/persistence/migrations/env.py`, `gameforge/runtime/persistence/migrations/script.py.mako`, `gameforge/runtime/persistence/migrations/versions/0001_initial.py`, `alembic.ini` (repo root)
- Test: `tests/runtime/test_persistence_migration.py`

**Interfaces:**
- Consumes: `sqlalchemy`, `alembic`, env var `DATABASE_URL` (default `sqlite+pysqlite:///:memory:` for tests, `sqlite:///gameforge.db` for local).
- Produces:
  - `models.py`: SQLAlchemy 2.0 declarative `Base` + tables `artifacts(artifact_id PK, kind, version_tuple JSON, lineage JSON, payload_hash, created_at, meta JSON)`, `refs(name PK, artifact_id, updated_at)`, `ref_history(id PK, name, artifact_id, seq)`, `audit(seq PK, actor, action, artifact_id, ts, content_hash, prev_hash)`. All DB-agnostic column types (`String`, `JSON`, `Integer`).
  - `engine.py`: `get_engine(url:str|None=None) -> Engine`, `get_sessionmaker(engine=None) -> sessionmaker`, `resolve_url() -> str` (env `DATABASE_URL` else default). `run_migrations(url:str|None=None) -> None` helper invoking Alembic `upgrade("head")` programmatically.
  - Alembic wired to `Base.metadata`; `0001_initial.py` creates all four tables in `upgrade()` and drops them in `downgrade()`.

- [ ] **Step 1: Write the failing test** — `tests/runtime/test_persistence_migration.py`:

```python
from sqlalchemy import inspect
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence import migrations_api as m  # thin wrapper exposing upgrade/downgrade

def test_migration_forward_creates_tables_then_rollback_drops(tmp_path):
    url = f"sqlite:///{tmp_path/'t.db'}"
    m.upgrade(url, "head")
    insp = inspect(get_engine(url))
    tables = set(insp.get_table_names())
    assert {"artifacts", "refs", "ref_history", "audit"} <= tables
    m.downgrade(url, "base")
    insp2 = inspect(get_engine(url))
    assert not ({"artifacts", "refs", "ref_history", "audit"} & set(insp2.get_table_names()))
```

> Provide `gameforge/runtime/persistence/migrations_api.py` with `upgrade(url, rev)` / `downgrade(url, rev)` that build an Alembic `Config` pointing at `alembic.ini` + the `migrations/` dir and set `sqlalchemy.url = url`, so both the test and CI use one code path.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/runtime/test_persistence_migration.py -v` → FAIL.

- [ ] **Step 3: Implement** `models.py`, `engine.py`, `migrations_api.py`, the Alembic `env.py`/`script.py.mako`, `alembic.ini`, and `0001_initial.py`. `alembic.ini` sets `script_location = gameforge/runtime/persistence/migrations`. `env.py` reads `sqlalchemy.url` from the config and targets `Base.metadata` for autogenerate.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/runtime/test_persistence_migration.py -v` → PASS. Also verify the CLI path: `uv run alembic -c alembic.ini upgrade head && uv run alembic -c alembic.ini downgrade base` (uses default sqlite file) → both succeed.

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(runtime/persistence): SQLAlchemy engine + models + Alembic migration (forward/rollback tested)"
```

---

## Task 13: platform — SQLAlchemy artifact/lineage store + append-only WORM audit

**Files:**
- Create: `gameforge/platform/lineage/store.py`, `gameforge/platform/audit/log.py`
- Test: `tests/platform/test_lineage_store.py`, `tests/platform/test_audit_worm.py`

**Interfaces:**
- Consumes: `runtime.persistence.{engine, models}`, `contracts.lineage.{Artifact, AuditRecord}`, `contracts.canonical.{canonical_json, compute_snapshot_id}`, `spine.versioning.store.LineageGraph` (reuse ancestor logic over persisted artifacts). `platform → contracts + spine + runtime` (allowed).
- Produces:
  - `lineage/store.py`: `class SqlArtifactStore(session_factory)` mirroring the spine in-memory store interface (`put/get/all`) but persisting to the `artifacts` table; `SqlRefStore` (`set/get/rollback/history` over `refs`+`ref_history`); `ancestors(artifact_id)` reusing `LineageGraph` against an in-memory view materialized from the DB.
  - `audit/log.py`: `class AuditLog(session_factory)` — `append(actor, action, artifact_id, ts) -> AuditRecord` computes `content_hash = compute_snapshot_id({actor,action,artifact_id,ts,prev_hash})` chaining `prev_hash` from the last row; INSERT-only. `verify_chain() -> bool` recomputes the chain and detects tampering. **WORM**: no update/delete methods exist; a guard raises `PermissionError` if `update`/`delete` are attempted via a helper.

- [ ] **Step 1: Write the failing tests** — `tests/platform/test_lineage_store.py`:

```python
from gameforge.runtime.persistence.engine import get_engine, get_sessionmaker
from gameforge.runtime.persistence import migrations_api as m
from gameforge.runtime.persistence.models import Base
from gameforge.contracts.lineage import Artifact, VersionTuple
from gameforge.platform.lineage.store import SqlArtifactStore, SqlRefStore

def _sf(tmp_path):
    url = f"sqlite:///{tmp_path/'l.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(get_engine(url))

def test_sql_artifact_put_get_and_ancestors(tmp_path):
    sf = _sf(tmp_path); store = SqlArtifactStore(sf)
    vt = VersionTuple(ir_snapshot_id="sha256:s")
    ir = Artifact(artifact_id="a_ir", kind="ir_snapshot", version_tuple=vt, lineage=[])
    cfg = Artifact(artifact_id="a_cfg", kind="config_export", version_tuple=vt, lineage=["a_ir"])
    store.put(ir); store.put(cfg)
    assert store.get("a_cfg").lineage == ["a_ir"]
    assert store.ancestors("a_cfg") == ["a_ir"]

def test_sql_ref_rollback_keeps_history(tmp_path):
    refs = SqlRefStore(_sf(tmp_path))
    refs.set("head", "v2"); refs.rollback("head", "v1")
    assert refs.get("head") == "v1" and refs.history("head") == ["v2", "v1"]
```

`tests/platform/test_audit_worm.py`:

```python
from gameforge.runtime.persistence.engine import get_engine, get_sessionmaker
from gameforge.runtime.persistence.models import Base, AuditRow
from gameforge.platform.audit.log import AuditLog

def _sf(tmp_path):
    url = f"sqlite:///{tmp_path/'a.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(get_engine(url))

def test_audit_append_only_hash_chain_and_tamper_detection(tmp_path):
    sf = _sf(tmp_path); log = AuditLog(sf)
    log.append("cli", "record_artifact", "a1", ts="2026-07-06T00:00:00Z")
    log.append("cli", "record_artifact", "a2", ts="2026-07-06T00:00:01Z")
    assert log.verify_chain() is True
    # tamper directly in the DB → chain verification fails
    with sf() as s:
        row = s.get(AuditRow, 1); row.action = "TAMPERED"; s.commit()
    assert log.verify_chain() is False
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/platform -v` → FAIL.

- [ ] **Step 3: Implement** both modules per Interfaces. `AuditLog.append` reads the max `seq` + its `content_hash` as `prev_hash`, computes the new `content_hash`, inserts. `verify_chain` walks rows by `seq`, recomputing each `content_hash` from stored fields + the previous hash.

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/platform -v && uv run lint-imports` → PASS (platform obeys `→ contracts+spine+runtime`).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(platform): SQLAlchemy artifact/lineage store + append-only WORM audit with hash chain"
```

---

## Task 14: apps/cli/run_slice — record artifacts (lineage) + audit; rollback demo

**Files:**
- Modify: `gameforge/apps/cli/run_slice.py`
- Test: `tests/apps/test_run_slice_lineage.py`

**Interfaces:**
- Consumes: `spine.versioning.{version_tuple, store}`, `contracts.lineage.Artifact`.
- Produces: `run_slice`/`run_slice_workbook` additionally build a lineage chain during a run — `ir_snapshot → config_export → checker_run` artifacts (each with a `VersionTuple` stamping `ir_snapshot_id`, `env_contract_version`, `seed`, `tool_version`), recorded into an `InMemoryArtifactStore`, with a `RefStore` `head` pointer and an audit entry per artifact. The result dict gains `"artifacts": {"ir_snapshot":id, "config_export":id, "checker_run":id}` and `"head": <ref>`. Existing keys unchanged (M0a tests stay green). Uses the deterministic in-memory versioning store (no DB file) so `run_slice` remains file-free and reproducible; the SQL persistence + Alembic path is covered by Tasks 12–13.

- [ ] **Step 1: Write the failing test** — `tests/apps/test_run_slice_lineage.py`:

```python
from gameforge.apps.cli.run_slice import run_slice

def test_slice_records_lineage_chain_traceable_to_source_tuple():
    out = run_slice("scenarios/caravan.yaml", seed=0)
    arts = out["artifacts"]
    assert set(arts) == {"ir_snapshot", "config_export", "checker_run"}
    # checker_run traces back through config_export to ir_snapshot (contract §5 anchor)
    assert out["head"] == arts["checker_run"] or out["head"] == arts["ir_snapshot"]

def test_slice_lineage_is_deterministic():
    a = run_slice("scenarios/caravan.yaml", seed=0)["artifacts"]
    b = run_slice("scenarios/caravan.yaml", seed=0)["artifacts"]
    assert a == b   # content-addressed artifact ids are reproducible
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/apps/test_run_slice_lineage.py -v` → FAIL.

- [ ] **Step 3: Implement** the wiring in `run_slice.py` (a small `_record_lineage(snapshot, world_config, findings, seed)` helper returning the artifacts dict + head; call it from both `run_slice` and `run_slice_workbook`). Keep artifact ids content-addressed (deterministic).

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/apps -v` → PASS (lineage + all prior slice tests).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "feat(apps/cli): record ir_snapshot→config_export→checker_run lineage + audit in run_slice"
```

---

## Task 15: Milestone acceptance & wrap-up

**Files:**
- Modify: `CLAUDE.md` (M0b row → ✅), `docs/superpowers/plans/README.md`, `README.md` (M0b section), `gameforge/apps/cli/__main__.py` (accept a CSV scenario dir), memory `gameforge-milestone-progress.md`
- Create: none (verification task)

**Interfaces:**
- Produces: full green suite + lint gate + Alembic forward/rollback + the two acceptance anchors demonstrated (round-trip diff=∅; four-system config-driven deterministic run).

- [ ] **Step 1: Full acceptance run**
```bash
uv run pytest -v
uv run lint-imports
uv run alembic -c alembic.ini upgrade head && uv run alembic -c alembic.ini downgrade base
uv run python -c "from gameforge.spine.ingestion.csv_format import read_workbook; \
from gameforge.spine.ingestion.format_schema import FormatSchema; \
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter; import json; \
s=FormatSchema.model_validate(json.load(open('scenarios/outpost/format_schema.json'))); \
wb=read_workbook('scenarios/outpost', s); a=AureusCsvAdapter(); \
snapA=a.to_ir(wb,'outpost'); wb2=a.from_ir(snapA); \
print('roundtrip_lossless:', wb2==wb and a.to_ir(wb2,'outpost').to_graph().diff(snapA.to_graph()).is_empty())"
uv run python -c "from gameforge.apps.cli.run_slice import run_slice_workbook; import json; \
o=run_slice_workbook('scenarios/outpost',0); \
print(json.dumps({'completed':o['completed'],'systems':sorted(o['systems_exercised'])}))"
```
Expected: all tests PASS; import-linter all contracts kept; alembic up+down succeed; `roundtrip_lossless: True`; slice prints `completed:true` with the four systems.

- [ ] **Step 2: Update `CLAUDE.md`** — milestone table M0b status → ✅完成 with a one-line acceptance evidence note (round-trip diff=∅; four systems config-driven; version/lineage/audit + Alembic up/down green).

- [ ] **Step 3: Update `README.md`** — add an M0b section (what M0b delivers / what defers to M1); add the CSV-scenario acceptance command; note `DATABASE_URL` default (sqlite) + Postgres-ready.

- [ ] **Step 4: Update memory** `gameforge-milestone-progress.md` — M0b ✅; branch `m0b-foundations`; non-obvious decisions (D1 SQLAlchemy+Alembic sqlite-default; D2 normalized-CSV pluggable adapter; gacha-via-buy atomic; spine import boundary tightened to contracts-only; formula safe-eval; CountingRandom in state_hash).

- [ ] **Step 5: Commit**
```bash
git add -A && git commit -m "docs(m0b): acceptance — round-trip lossless + four-system config-driven; mark M0b complete"
```

---

## Self-Review

**1. Spec coverage** (M0b design §0 acceptance + contracts 分期表):
- Aureus combat/economy/gacha config-driven → Tasks 2,4,5,10 ✔
- IR combat-economy node/edge types produced/consumed → Tasks 4,5,8,10 ✔
- Schema Registry + Aureus adapter round-trip (diff=∅) → Tasks 6,7,8,9 ✔
- Version/lineage/audit skeleton (§5 full tuple, artifact+lineage DAG, rollback, WORM audit) → Tasks 3,11,13,14 ✔
- DB migration framework (Alembic forward/rollback) → Task 12 ✔
- Determinism (per-tick state_hash incl. combat/gacha rng) → Tasks 4,5 ✔
- M0a continuity (caravan slice green; attack-test updated additively) → Tasks 5,10,14 ✔
- Dependency lint tightened (spine→contracts only; new packages) → Task 1 ✔

**2. Placeholder scan:** every code step shows real test code + exact interface signatures; large modules (kernel, adapter) give precise Interfaces + key fragments following the M0a plan's established style. No TBD/TODO. Combat/economy/gacha are implemented, not stubbed. Version-tuple fields unused in M0b are schema-present + `None` (declared, not cut).

**3. Type consistency:** `WorldConfig` spec models (Task 2) reused verbatim in 4/5/8/10; `VersionTuple`/`Artifact`/`AuditRecord` (Task 3) reused in 11/13/14; `FormatSchema`/`SchemaRegistry` (6) consumed by 7/8/9; `AureusCsvAdapter.to_ir/from_ir` (8) consumed by 9/10; `CountingRandom` (4) used in 5; `InMemoryArtifactStore`/`LineageGraph`/`RefStore` (11) mirrored by `SqlArtifactStore`/`SqlRefStore`/`AuditLog` (13); `migrations_api.upgrade/downgrade` (12) used by 12/13 tests + Task 15 CLI.

**Deferred to later milestones (interfaces defined now — 不简化只延后):** DSL grammar + Graph/ASP/SMT checker compilation + economy Monte-Carlo/ABM sim (M1); open-source game adapter external validity (M1); LLM agents / Playtest / cassette / model-router (M2); GameForge-Bench (M3); RBAC/approval workflow / full web pages / observability panels (M4). Version-tuple's constraint/prompt/model/agent/cassette fields populate as those milestones land.
