# M0a — Shortest Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the GameForge monorepo skeleton with enforced dependency boundaries, the `contracts` package (IR core types + canonical snapshot + Env/Finding/Patch schemas), an in-memory Spec-IR store, a minimal deterministic structural checker, and the Aureus minimal kernel (quest state machine + grid navigation), then wire one hand-written scenario config → IR → checker → Aureus and drive a `talk → collect → turn-in` quest chain to completion — deterministically and reproducibly.

**Architecture:** Monorepo with single-direction dependencies (`contracts ← runtime/spine/env/game ← agents/platform ← apps`), enforced by `import-linter`. The deterministic trusted trunk (`spine`, `game/aureus`) never imports LLM SDKs. IR is a typed property graph with content-addressed immutable snapshots (`snapshot_id = sha256(canonical_json(content))`). Aureus is a tick-based deterministic env implementing the engine-agnostic `Environment` ABC; the M0a "planner" is a plain scripted driver in `apps/cli` (the real LLM Playtest Agent is M2). Physical IR storage is in-memory for M0a (full logical query interface implemented; DB backend deferred to M0b).

**Tech Stack:** Python 3.12 (managed by `uv`), pydantic v2 (contracts schemas + JSON-schema export), PyYAML (scenario config), pytest + hypothesis (property/differential tests), import-linter (dependency lint), ruff (lint/format); Vite + React + TypeScript (frontend scaffold, Node 24).

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 and the foundational contracts. Every task's requirements implicitly include this section.

- **不简化，只延后**: interfaces/contracts are defined in full now; only *implementation* may be deferred to a later milestone. No "minimal-subset" field/interface cutting. (memory `no-simplification-principle`)
- **依赖方向单向**: `contracts` depends on nothing; `runtime → contracts`; `spine → contracts` only; `env → contracts`; `game/aureus → contracts + env`; `agents → contracts + spine + env + runtime`; `platform → contracts + spine + runtime`; `apps → all business modules`; `bench → contracts + spine + agents + game + runtime`; `web` only via `apps/api`. **`spine/**` and `runtime/**` must never import `agents.*`; `spine/**` must never import `runtime.model_router`, `platform.*`, or any LLM SDK (`openai`/`anthropic`/`langchain`/`langgraph`/`llama_index`).**
- **确定性优先**: correctness verdicts come from graph/algorithmic oracles; no LLM in the M0a slice at all.
- **可复现只承诺回放**: env is seed-ized; identical `(scenario, seed, action-sequence)` → identical trajectory and per-tick `state_hash`.
- **TDD 全程**: every task is test-first (write failing test → run → implement → run → commit). Checker/serialization use property + differential tests.
- **Git**: commit messages carry NO AI co-author / "Generated with" attribution.
- **Package namespace**: the contract §1 packages live under a single `gameforge/` package root (matching the §1 diagram: `gameforge/` indented root + `web/` sibling). All Python imports are `gameforge.<pkg>` (e.g. `gameforge.contracts.ir`, `gameforge.spine.ir.store`, `gameforge.game.aureus.kernel`). Code snippets below write the short form (`contracts.ir`, `spine.ir.store`, …); read every one with the `gameforge.` prefix. Nesting under `gameforge/` also avoids shadowing stdlib `platform`.
- **schema_version constants (define once, reuse everywhere):** `IR_SCHEMA_VERSION = "ir-core@1"`, `META_SCHEMA_VERSION = "meta@1"`, `ENV_CONTRACT_VERSION = "env@1"`, `FINDING_SCHEMA_VERSION = "finding@1"`, `PATCH_SCHEMA_VERSION = "patch@1"`, `DSL_GRAMMAR_VERSION = "dsl@1"` (DSL grammar itself lands M1; constant reserved now).

## Repo layout produced by this plan (contract §1)

```
game-forge/                     # repo root (checkout dir)
  pyproject.toml                # uv-managed; deps + pytest + ruff + importlinter config
  .python-version               # 3.12
  gameforge/                    # Python package root (contract §1 packages nest here)
    __init__.py
    contracts/                  # schema single-source-of-truth (impl@M0a: ir, env, findings, world)
      __init__.py  ir.py  canonical.py  env_types.py  findings.py  world.py  versions.py
    runtime/                    # low-level capabilities (skeleton in M0a)
      __init__.py  model_router/__init__.py  cassette/__init__.py
      observability/__init__.py  config/__init__.py  secrets/__init__.py
    spine/                      # deterministic trusted trunk (no LLM)
      __init__.py
      ir/  __init__.py  store.py  snapshot.py  loader.py
      checkers/  __init__.py  base.py  structural.py
      dsl/__init__.py  sim/__init__.py  versioning/__init__.py     # skeletons (impl M1/M0b)
    env/                        # Agent-Env interface (no impl)
      __init__.py  base.py
    game/
      __init__.py
      aureus/  __init__.py  grid.py  world.py  kernel.py
    agents/__init__.py          # skeleton (impl M2)
    platform/__init__.py        # skeleton (impl M0b/M4)
    apps/
      __init__.py  cli/__init__.py  cli/ir_to_world.py  cli/driver.py  cli/run_slice.py
    bench/__init__.py           # skeleton (impl M3)
  scenarios/                    # hand-written M0a scenario config
    caravan.yaml
  tests/                        # mirrors package tree; imports gameforge.*
  web/                          # Vite React TS scaffold (repo-root sibling of gameforge/)
docs/superpowers/plans/…        # this file
```

---

## File Structure & responsibilities

- `contracts/*` — pure typed schemas + canonical serialization. No business logic, no I/O. The one place every other package imports names from.
- `spine/ir/*` — in-memory typed-property-graph store, immutable snapshots, diff, YAML→IR loader. Depends only on `contracts`.
- `spine/checkers/*` — deterministic structural oracle producing `Finding`s. Depends only on `contracts` (+ a nav-view provider passed in).
- `env/base.py` — `Environment` ABC. Depends only on `contracts`.
- `game/aureus/*` — deterministic tick kernel implementing `Environment`; grid nav + quest state machine + nav derived view. Depends only on `contracts + env`.
- `apps/cli/*` — the only layer that composes spine + game: IR→WorldConfig, scripted macro-driver, end-to-end slice runner.
- `web/*` — React scaffold (placeholder console shell).

---

## Task 1: Monorepo scaffold, uv env, and dependency lint

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore` (append), `ruff.toml` (or `[tool.ruff]` in pyproject)
- Create: `__init__.py` for every package dir listed in the layout above (empty or one-line docstring)
- Create: `contracts/versions.py`
- Test: `tests/test_dependency_lint.py`

**Interfaces:**
- Produces: importable top-level packages `contracts`, `runtime`, `spine`, `env`, `game`, `agents`, `platform`, `apps`, `bench`; `uv run pytest` and `uv run lint-imports` both green; `contracts.versions` exports the schema-version constants from Global Constraints.

- [ ] **Step 1: Write the failing test** — `tests/test_dependency_lint.py`

```python
import subprocess, sys

def test_import_linter_contracts_pass():
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

def test_version_constants_present():
    from contracts import versions as v
    assert v.IR_SCHEMA_VERSION == "ir-core@1"
    assert v.ENV_CONTRACT_VERSION == "env@1"
    assert v.META_SCHEMA_VERSION == "meta@1"
    assert v.FINDING_SCHEMA_VERSION == "finding@1"
    assert v.PATCH_SCHEMA_VERSION == "patch@1"
    assert v.DSL_GRAMMAR_VERSION == "dsl@1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dependency_lint.py -v`
Expected: FAIL (no pyproject / packages / importlinter yet).

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "gameforge"
version = "0.0.0"
description = "GameForge — game-content correctness compiler + agent workbench"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "pytest>=8.2",
    "hypothesis>=6.100",
    "import-linter>=2.0",
    "ruff>=0.5",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.importlinter]
root_packages = ["contracts", "runtime", "spine", "env", "game", "agents", "platform", "apps", "bench"]

[[tool.importlinter.contracts]]
name = "spine is LLM-free and trunk-pure"
type = "forbidden"
source_modules = ["spine"]
forbidden_modules = [
    "agents", "platform", "runtime.model_router",
    "openai", "anthropic", "langchain", "langgraph", "llama_index",
]

[[tool.importlinter.contracts]]
name = "runtime never imports agents"
type = "forbidden"
source_modules = ["runtime"]
forbidden_modules = ["agents"]

[[tool.importlinter.contracts]]
name = "contracts depends on no business package"
type = "forbidden"
source_modules = ["contracts"]
forbidden_modules = ["runtime", "spine", "env", "game", "agents", "platform", "apps", "bench"]

[[tool.importlinter.contracts]]
name = "env only depends on contracts"
type = "forbidden"
source_modules = ["env"]
forbidden_modules = ["runtime", "spine", "game", "agents", "platform", "apps", "bench"]

[[tool.importlinter.contracts]]
name = "game/aureus only depends on contracts and env"
type = "forbidden"
source_modules = ["game"]
forbidden_modules = ["runtime", "spine", "agents", "platform", "apps", "bench"]

[[tool.importlinter.contracts]]
name = "platform never imports agents"
type = "forbidden"
source_modules = ["platform"]
forbidden_modules = ["agents", "apps"]
```

- [ ] **Step 4: Create `.python-version`** with content `3.12`, and create every package `__init__.py` (all dirs in layout). Each skeleton `__init__.py` may contain a single docstring, e.g. `"""GameForge <pkg> package."""`. Append to `.gitignore`: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `web/node_modules/`, `web/dist/`.

- [ ] **Step 5: Create `contracts/versions.py`**

```python
"""Schema/version constants — single source of truth (Global Constraints)."""
IR_SCHEMA_VERSION = "ir-core@1"
META_SCHEMA_VERSION = "meta@1"
ENV_CONTRACT_VERSION = "env@1"
FINDING_SCHEMA_VERSION = "finding@1"
PATCH_SCHEMA_VERSION = "patch@1"
DSL_GRAMMAR_VERSION = "dsl@1"
```

Re-export from `contracts/__init__.py`:
```python
"""GameForge contracts — schema single source of truth."""
from contracts.versions import (  # noqa: F401
    IR_SCHEMA_VERSION, META_SCHEMA_VERSION, ENV_CONTRACT_VERSION,
    FINDING_SCHEMA_VERSION, PATCH_SCHEMA_VERSION, DSL_GRAMMAR_VERSION,
)
```

- [ ] **Step 6: Provision env & run**

Run: `uv python install 3.12 && uv sync` then `uv run pytest tests/test_dependency_lint.py -v`
Expected: PASS (both tests green; `import-linter` reports all contracts kept).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "chore: monorepo scaffold, uv env, and import-linter dependency gate"
```

---

## Task 2: contracts — IR core types + canonical serialization + snapshot_id

**Files:**
- Create: `contracts/ir.py`, `contracts/canonical.py`
- Test: `tests/contracts/test_canonical.py`, `tests/contracts/test_ir_types.py`

**Interfaces:**
- Consumes: `contracts.versions.*`.
- Produces:
  - `class NodeType(str, Enum)` — core members `FACTION, CHARACTER, NPC, QUEST, QUEST_STEP, DIALOGUE_NODE, REGION, SPAWN_POINT, INTERACTABLE, ITEM, MONSTER, CURRENCY, SHOP, DROP_TABLE, REWARD_TABLE, GACHA_POOL, EVENT, UNLOCK_CONDITION`; combat-economy members reserved now (impl M0b) `EQUIPMENT, SKILL, STATUS_EFFECT, EFFECT, BATTLE_ENCOUNTER, FORMULA`.
  - `class EdgeType(str, Enum)` — `HAS_STEP, PRECEDES, REQUIRES, GATED_BY, UNLOCKS, STARTS_AT, TALKS_TO, TRIGGERED_BY, LOCATED_IN, CONTAINS, SPAWNS, PATH_TO, DROPS_FROM, GRANTS, CONSUMES, REWARDS, SELLS, USES_SKILL, APPLIES_EFFECT, HAS_STAT_CURVE, HOSTILE_TO, ALLY_WITH, BELONGS_TO, REVEALS, REFERENCES`.
  - `class SourceRef(BaseModel)`: `adapter: str; file: str; sheet: str | None; row: int | None; column: str | None`.
  - `class Entity(BaseModel)`: `id: str; type: NodeType; attrs: dict[str, Any] = {}; source_ref: SourceRef | None = None; tags: list[str] | None = None; schema_version: str = IR_SCHEMA_VERSION`.
  - `class Relation(BaseModel)`: `id: str; type: EdgeType; src_id: str; dst_id: str; attrs: dict[str, Any] | None = None; source_ref: SourceRef | None = None; schema_version: str = IR_SCHEMA_VERSION`.
  - `canonical_json(payload: Any) -> str` and `compute_snapshot_id(content_payload: Mapping) -> str` (returns `"sha256:<hex>"`).

- [ ] **Step 1: Write the failing tests** — `tests/contracts/test_canonical.py`

```python
from contracts.canonical import canonical_json, compute_snapshot_id

def test_key_order_independent():
    a = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    b = {"a": 2, "c": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert compute_snapshot_id(a) == compute_snapshot_id(b)

def test_none_optionals_dropped():
    assert canonical_json({"a": 1, "b": None}) == canonical_json({"a": 1})

def test_float_normalized_stably():
    # 1.10 and 1.1 must canonicalize identically; ints stay ints
    assert canonical_json({"v": 1.10}) == canonical_json({"v": 1.1})
    assert canonical_json({"v": 1}) != canonical_json({"v": 1.0})

def test_snapshot_id_prefixed():
    sid = compute_snapshot_id({"x": 1})
    assert sid.startswith("sha256:") and len(sid) == len("sha256:") + 64

def test_ordered_list_preserved():
    assert canonical_json({"steps": [3, 1, 2]}) != canonical_json({"steps": [1, 2, 3]})
```

`tests/contracts/test_ir_types.py`:
```python
from contracts.ir import Entity, Relation, NodeType, EdgeType

def test_entity_defaults_schema_version():
    e = Entity(id="npc:lincheng", type=NodeType.NPC)
    assert e.schema_version == "ir-core@1" and e.attrs == {}

def test_relation_requires_id_and_endpoints():
    r = Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q1", dst_id="s1")
    assert r.type is EdgeType.HAS_STEP

def test_combat_economy_types_reserved():
    assert NodeType.BATTLE_ENCOUNTER.value == "BATTLE_ENCOUNTER"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/contracts -v`
Expected: FAIL (`contracts.canonical` / `contracts.ir` not found).

- [ ] **Step 3: Implement `contracts/canonical.py`**

```python
"""Canonical JSON + content-addressed snapshot id (contract §2.4)."""
from __future__ import annotations
import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping


def _canon(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _canon(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        # Rule 4: stable decimal string; 1.10 == 1.1, no platform/scientific drift.
        return "f:" + format(Decimal(str(obj)).normalize(), "f")
    return obj


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _canon(payload), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def compute_snapshot_id(content_payload: Mapping) -> str:
    digest = hashlib.sha256(canonical_json(content_payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
```

Note: floats are tagged (`"f:"`) so `1` (int) and `1.0` (float) differ per test; ordered lists keep order (rule 3 for ordered arrays); unordered collections are represented as dicts keyed by id upstream (Task 4) so `sort_keys` orders them.

- [ ] **Step 4: Implement `contracts/ir.py`** with the enums and pydantic models exactly as in the Interfaces block above (import `IR_SCHEMA_VERSION` from `contracts.versions`; `from pydantic import BaseModel, Field`; use `model_config = ConfigDict(extra="forbid")` on `SourceRef`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/contracts -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(contracts): IR core types, canonical JSON, and snapshot id"
```

---

## Task 3: contracts — Env types, Finding/Patch, and WorldConfig schemas

**Files:**
- Create: `contracts/env_types.py`, `contracts/findings.py`, `contracts/world.py`
- Test: `tests/contracts/test_env_types.py`, `tests/contracts/test_findings.py`, `tests/contracts/test_world.py`

**Interfaces:**
- Consumes: `contracts.versions.*`, `contracts.ir.SourceRef`.
- Produces (contract §4.1/§4.3, §6, and the WorldConfig Aureus consumes):
  - Actions (pydantic tagged union via `kind` literal): `Observe`, `NavigateTo{target:str}`, `Interact{target:str}`, `Choose{option_id:str}`, `Attack{target_id:str}`, `CastSkill{skill_id:str,target_id:str}`, `Use{item_id:str,target:str|None}`, `Pickup{item_id:str}`, `Equip{item_id:str}`, `Buy{shop_id,item_id,count}`, `Sell{shop_id,item_id,count}`, `Wait{ticks:int}`. `Action = Annotated[Union[...], Field(discriminator="kind")]`.
  - `class Observation(BaseModel)` with **all** §4.3 fields: `tick:int; player_pos:tuple[int,int]; player_stats:dict; equipped_items:list[str]; active_effects:list[str]; active_quests:list[str]; completed_quests:list[str]; known_quests:list[str]; quest_state:dict[str,Any]; inventory:dict[str,int]; hp:int; nearby_entities:list[str]; reachable_targets:list[str]; available_interactions:list[str]; visible_map:dict[str,Any]; dialogue_options:list[str]; last_action_result:str; logs:list[str]`.
  - `class StepResult(BaseModel)`: `observation:Observation; reward:float; done:bool; info:dict[str,Any]`.
  - `HIGH_LEVEL_MACROS = ("accept_quest", "turn_in", "talk")` (planner-layer, not Env).
  - `class TypedOp(BaseModel)` + `class Patch(BaseModel)` + `class Finding(BaseModel)` exactly per contract §6 (all fields; `status`/`source`/`oracle_type` as `Literal[...]`; `finding_schema_version`/`patch_schema_version` default to constants).
  - WorldConfig (`contracts/world.py`): `GridSpec{width:int,height:int,blocked:list[tuple[int,int]]}`, `Placement{entity_id:str,type:NodeType,pos:tuple[int,int],attrs:dict}`, `QuestStepSpec{step_id:str,kind:Literal["talk","collect","turn_in"],target:str|None,item:str|None,count:int}`, `QuestSpec{quest_id:str,giver:str,steps:list[QuestStepSpec],reward:dict}`, `ScenarioConfig{scenario_id:str,start_pos:tuple[int,int]}`, `WorldConfig{scenario:ScenarioConfig,grid:GridSpec,placements:list[Placement],quests:list[QuestSpec],env_contract_version:str=ENV_CONTRACT_VERSION}`.

- [ ] **Step 1: Write the failing tests**

`tests/contracts/test_env_types.py`:
```python
from contracts.env_types import Observation, StepResult, parse_action, HIGH_LEVEL_MACROS

def test_action_discriminated_union():
    a = parse_action({"kind": "navigate_to", "target": "npc:lincheng"})
    assert a.kind == "navigate_to" and a.target == "npc:lincheng"

def test_combat_actions_defined_now():
    a = parse_action({"kind": "attack", "target_id": "mob:1"})
    assert a.kind == "attack"

def test_observation_has_all_fields():
    fields = Observation.model_fields.keys()
    for f in ["reachable_targets", "available_interactions", "last_action_result",
              "quest_state", "active_effects", "equipped_items"]:
        assert f in fields

def test_macros_are_planner_layer():
    assert HIGH_LEVEL_MACROS == ("accept_quest", "turn_in", "talk")
```

`tests/contracts/test_findings.py`:
```python
from contracts.findings import Finding, Patch, TypedOp

def test_finding_schema_version_default():
    f = Finding(id="F1", source="checker", producer_id="structural",
                producer_run_id="run1", oracle_type="deterministic",
                defect_class="missing_drop_source", severity="critical",
                snapshot_id="sha256:x", evidence={}, minimal_repro={},
                status="confirmed", message="m")
    assert f.finding_schema_version == "finding@1"

def test_patch_optimistic_concurrency_fields():
    op = TypedOp(op_id="o1", op="set_relation_attr", target="r1",
                 old_value={"probability": 0.1}, new_value={"probability": 0.2})
    p = Patch(id="P1", base_snapshot_id="sha256:a", target_snapshot_id="sha256:b",
              expected_to_fix=["F1"], preconditions=[], side_effect_risk="low",
              ops=[op], produced_by="agent", producer_run_id="run1", rationale="r")
    assert p.patch_schema_version == "patch@1" and p.ops[0].old_value["probability"] == 0.1
```

`tests/contracts/test_world.py`:
```python
from contracts.world import WorldConfig, QuestSpec, QuestStepSpec, GridSpec, Placement, ScenarioConfig
from contracts.ir import NodeType

def test_worldconfig_roundtrips_via_pydantic():
    wc = WorldConfig(
        scenario=ScenarioConfig(scenario_id="s1", start_pos=(0, 0)),
        grid=GridSpec(width=5, height=5, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 1), attrs={})],
        quests=[QuestSpec(quest_id="q1", giver="npc:a",
                          steps=[QuestStepSpec(step_id="s1", kind="talk", target="npc:a")],
                          reward={"gold": 50})],
    )
    assert wc.env_contract_version == "env@1"
    assert WorldConfig.model_validate(wc.model_dump()).quests[0].steps[0].kind == "talk"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/contracts/test_env_types.py tests/contracts/test_findings.py tests/contracts/test_world.py -v`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement the three modules.** In `contracts/env_types.py` define each action model with `kind: Literal["observe"|...]`, the `Action` discriminated union, and a helper:
```python
def parse_action(data: dict) -> Action:
    from pydantic import TypeAdapter
    return TypeAdapter(Action).validate_python(data)
```
`QuestStepSpec.kind` uses `Literal["talk", "collect", "turn_in"]`. `Placement.pos` / grid coords are `tuple[int, int]`. Implement `Finding`/`Patch`/`TypedOp` per contract §6 with every field present (`confidence: float | None = None`, `entities: list[str] = []`, `relations: list[str] = []`, `constraint_id: str | None = None`, etc.).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/contracts -v`
Expected: PASS (all contracts tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(contracts): Env action/observation, Finding/Patch, and WorldConfig schemas"
```

---

## Task 4: spine/ir — in-memory graph store, immutable snapshot, diff, query interface

**Files:**
- Create: `spine/ir/store.py`, `spine/ir/snapshot.py`
- Test: `tests/spine/ir/test_store.py`, `tests/spine/ir/test_snapshot.py`

**Interfaces:**
- Consumes: `contracts.ir.{Entity,Relation,NodeType,EdgeType}`, `contracts.canonical.compute_snapshot_id`, `contracts.versions.META_SCHEMA_VERSION`.
- Produces:
  - `class IRGraph`: `add_entity(e:Entity)`, `add_relation(r:Relation)`, `get_node(id)->Entity|None`, `get_relation(id)->Relation|None`, `neighbors(id, edge_type:EdgeType|None=None, direction:str="out")->list[Relation]`, `nodes_of_type(t:NodeType)->list[Entity]`, `subgraph(types:set[NodeType])->IRGraph`, `path_exists(src, dst, via:EdgeType|None=None, nav:"NavProvider|None"=None)->bool`, `diff(other:IRGraph)->GraphDiff`.
  - `class NavProvider(Protocol)`: `reachable(src_pos, dst_pos)->bool` and `pos_of(entity_id)->tuple[int,int]|None` (derived spatial view, injected by game/aureus in Task 8; contract §2.3 `path_to` is a derived view).
  - `class GraphDiff(BaseModel)`: `added_entities, removed_entities, changed_entities, added_relations, removed_relations, changed_relations` (lists of ids); `is_empty()->bool`.
  - `class Snapshot` (`spine/ir/snapshot.py`): `snapshot_id:str; parent_id:str|None; meta_schema_version:str; entities:dict[str,Entity]; relations:dict[str,Relation]`; classmethod `from_graph(graph, parent_id=None)->Snapshot`; `to_graph()->IRGraph`; property `content_payload` (excludes created_at/author/snapshot_id/parent_id).

- [ ] **Step 1: Write the failing tests**

`tests/spine/ir/test_store.py`:
```python
from contracts.ir import Entity, Relation, NodeType, EdgeType
from spine.ir.store import IRGraph

def _q():
    g = IRGraph()
    g.add_entity(Entity(id="q1", type=NodeType.QUEST))
    g.add_entity(Entity(id="s1", type=NodeType.QUEST_STEP, attrs={"kind": "talk"}))
    g.add_relation(Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q1", dst_id="s1"))
    return g

def test_get_node_and_relation():
    g = _q()
    assert g.get_node("q1").type is NodeType.QUEST
    assert g.get_relation("r1").dst_id == "s1"

def test_neighbors_by_edge_type_and_direction():
    g = _q()
    assert [r.dst_id for r in g.neighbors("q1", EdgeType.HAS_STEP)] == ["s1"]
    assert [r.src_id for r in g.neighbors("s1", EdgeType.HAS_STEP, direction="in")] == ["q1"]

def test_nodes_of_type_and_subgraph():
    g = _q()
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {"s1"}
    assert set(g.subgraph({NodeType.QUEST})._entities) == {"q1"}

def test_path_exists_via_ir_edges():
    g = _q()
    assert g.path_exists("q1", "s1", via=EdgeType.HAS_STEP) is True
    assert g.path_exists("s1", "q1", via=EdgeType.HAS_STEP) is False
```

`tests/spine/ir/test_snapshot.py`:
```python
from contracts.ir import Entity, Relation, NodeType, EdgeType
from spine.ir.store import IRGraph
from spine.ir.snapshot import Snapshot

def _g():
    g = IRGraph()
    g.add_entity(Entity(id="npc:a", type=NodeType.NPC, attrs={"z": 1, "a": 2}))
    g.add_entity(Entity(id="item:x", type=NodeType.ITEM))
    g.add_relation(Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="item:x", dst_id="npc:a"))
    return g

def test_snapshot_roundtrip_diff_empty():
    g = _g()
    snap = Snapshot.from_graph(g)
    assert snap.to_graph().diff(g).is_empty()          # contract §2.5 anchor

def test_snapshot_id_order_independent():
    snap1 = Snapshot.from_graph(_g())
    # same content, entities added in different order:
    g2 = IRGraph()
    g2.add_entity(Entity(id="item:x", type=NodeType.ITEM))
    g2.add_entity(Entity(id="npc:a", type=NodeType.NPC, attrs={"a": 2, "z": 1}))
    g2.add_relation(Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="item:x", dst_id="npc:a"))
    assert snap1.snapshot_id == Snapshot.from_graph(g2).snapshot_id

def test_diff_detects_change():
    g = _g()
    snap = Snapshot.from_graph(g)
    g.get_node("npc:a").attrs["z"] = 999
    assert not Snapshot.from_graph(g).to_graph().diff(snap.to_graph()).is_empty()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/spine/ir -v`
Expected: FAIL.

- [ ] **Step 3: Implement `spine/ir/store.py`.** Internal `self._entities: dict[str, Entity]`, `self._relations: dict[str, Relation]`, plus adjacency indices `self._out: dict[str, list[str]]`, `self._in: dict[str, list[str]]`. `path_exists`: if `nav` given and both endpoints are spatial (have positions via `nav.pos_of`), delegate to `nav.reachable`; else BFS over IR edges filtered by `via`. `diff`: compare entity/relation dicts using `contracts.canonical.canonical_json` on each entity/relation dump to detect `changed_*`.

- [ ] **Step 4: Implement `spine/ir/snapshot.py`.** `content_payload` builds `{"meta_schema_version": META_SCHEMA_VERSION, "entities": {id: e.model_dump(exclude_none=True) minus id}, "relations": {id: r.model_dump(exclude_none=True) minus id}}`; `snapshot_id = compute_snapshot_id(content_payload)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/spine/ir -v`
Expected: PASS.

- [ ] **Step 6: Property test (differential-style) + commit** — add `tests/spine/ir/test_snapshot_property.py` using hypothesis to generate random small graphs and assert `Snapshot.from_graph(g).to_graph().diff(g).is_empty()` and order-independence of `snapshot_id`. Run `uv run pytest tests/spine/ir -v` (PASS), then:

```bash
git add -A && git commit -m "feat(spine/ir): in-memory graph store, immutable content-addressed snapshots, diff"
```

---

## Task 5: spine/ir — hand-written scenario YAML → IR loader

**Files:**
- Create: `spine/ir/loader.py`, `scenarios/caravan.yaml`
- Test: `tests/spine/ir/test_loader.py`

**Interfaces:**
- Consumes: `contracts.ir.*`, `spine.ir.store.IRGraph`, `spine.ir.snapshot.Snapshot`.
- Produces: `load_scenario(path_or_dict) -> Snapshot`. Loads the M0a scenario schema (regions/grid, npcs, spawn points, interactables, items, one quest with ordered steps) and emits typed Entities + Relations, each carrying a `SourceRef{adapter:"m0a-yaml", file, sheet=<top-key>, row=<index>}`. Quest steps become `QUEST_STEP` entities linked by `HAS_STEP` (Quest→Step) and `PRECEDES` (Step→Step order) — **never** only `Quest.attrs.steps` (contract §2.3).

- [ ] **Step 1: Write `scenarios/caravan.yaml`** — the canonical talk→collect→turn-in scenario (林澈 gives quest; 破损徽记 collected from a spawn/interactable in the newbie zone; turn in to 林澈):

```yaml
scenario_id: caravan_slice
grid: { width: 12, height: 8, blocked: [[4,3],[4,4],[5,3]] }
start_pos: [0, 0]
regions:
  - { id: "region:newbie_zone", name: "新手村" }
npcs:
  - { id: "npc:lincheng", name: "林澈", region: "region:newbie_zone", pos: [2, 1] }
items:
  - { id: "item:broken_emblem", name: "破损徽记" }
spawn_points:
  - { id: "spawn:emblem_pile", region: "region:newbie_zone", pos: [9, 6], spawns: "interact:emblem_pile" }
interactables:
  - { id: "interact:emblem_pile", kind: "gather", pos: [9, 6], yields_item: "item:broken_emblem", yields_count: 3 }
quests:
  - id: "quest:missing_caravan"
    title: "失踪的商队"
    giver: "npc:lincheng"
    region: "region:newbie_zone"
    reward: { gold: 60, item: "item:low_tier_blade" }
    steps:
      - { id: "step:talk_lincheng", kind: "talk", target: "npc:lincheng" }
      - { id: "step:collect_emblem", kind: "collect", item: "item:broken_emblem", count: 3 }
      - { id: "step:turn_in", kind: "turn_in", target: "npc:lincheng" }
```

- [ ] **Step 2: Write the failing test** — `tests/spine/ir/test_loader.py`

```python
from spine.ir.loader import load_scenario
from contracts.ir import NodeType, EdgeType

def test_loads_expected_nodes_and_has_step_edges():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    assert g.get_node("npc:lincheng").type is NodeType.NPC
    assert {e.id for e in g.nodes_of_type(NodeType.QUEST_STEP)} == {
        "step:talk_lincheng", "step:collect_emblem", "step:turn_in"}
    has_step = g.neighbors("quest:missing_caravan", EdgeType.HAS_STEP)
    assert len(has_step) == 3
    # ordering encoded as PRECEDES, not only attrs
    prec = g.neighbors("step:talk_lincheng", EdgeType.PRECEDES)
    assert prec and prec[0].dst_id == "step:collect_emblem"

def test_source_ref_populated():
    snap = load_scenario("scenarios/caravan.yaml")
    assert snap.to_graph().get_node("npc:lincheng").source_ref.adapter == "m0a-yaml"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/spine/ir/test_loader.py -v`
Expected: FAIL.

- [ ] **Step 4: Implement `spine/ir/loader.py`** — parse YAML, build entities for regions/npcs/items/spawn_points/interactables/quest/steps; relations: `LOCATED_IN` (npc→region), `STARTS_AT`/`TALKS_TO` (quest→giver npc), `SPAWNS` (spawn_point→interactable), `CONTAINS`/`GRANTS` (interactable→item), `HAS_STEP` (quest→step), `PRECEDES` (step→next step), `TALKS_TO` (talk step→npc), `REQUIRES`/`DROPS_FROM` (collect step→item source), `REWARDS` (quest→reward item). Populate `attrs` (pos, count, kind, gold) and `source_ref`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/spine/ir/test_loader.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(spine/ir): YAML scenario loader with has_step/precedes edges and source_ref"
```

---

## Task 6: spine/checkers — minimal deterministic structural checker

**Files:**
- Create: `spine/checkers/base.py`, `spine/checkers/structural.py`
- Test: `tests/spine/checkers/test_structural.py`

**Interfaces:**
- Consumes: `contracts.ir.*`, `contracts.findings.Finding`, `spine.ir.snapshot.Snapshot`, `spine.ir.store.{IRGraph,NavProvider}`.
- Produces:
  - `class Checker(Protocol)`: `id: str; check(snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]`.
  - `class StructuralChecker`: implements three deterministic rules, each emitting `Finding(source="checker", oracle_type="deterministic", producer_id="structural", ...)` with `evidence` + `minimal_repro` (source_ref based) and `status="confirmed"`:
    1. **reference integrity** — every `Relation.src_id/dst_id` resolves to an existing entity; every `talk`/`turn_in` step's target NPC exists (`defect_class="dangling_reference"`, severity critical).
    2. **collect-needs-reachable-source** — for every `collect` step, there exists a source (`SPAWNS`/`CONTAINS`/`DROPS_FROM` chain yielding `step.item`) and it is reachable in the quest region (uses `nav.reachable` when provided, else IR-edge reachability) (`defect_class="missing_drop_source"`, critical).
    3. **quest-DAG-acyclic** — `HAS_STEP`+`PRECEDES` subgraph has no cycle (`defect_class="cyclic_dependency"`, critical).
  - `run_all(snapshot, nav=None) -> list[Finding]`.

- [ ] **Step 1: Write the failing tests** — `tests/spine/checkers/test_structural.py`

```python
from spine.ir.loader import load_scenario
from spine.checkers.structural import StructuralChecker
from contracts.ir import Entity, Relation, NodeType, EdgeType

def test_clean_scenario_no_findings():
    snap = load_scenario("scenarios/caravan.yaml")
    assert StructuralChecker().check(snap) == []

def test_dangling_talk_target_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    g.get_node("step:talk_lincheng").attrs["target"] = "npc:ghost"  # nonexistent
    # rebuild a relation to the missing npc to simulate dangling ref
    g.add_relation(Relation(id="r_bad", type=EdgeType.TALKS_TO,
                            src_id="step:talk_lincheng", dst_id="npc:ghost"))
    from spine.ir.snapshot import Snapshot
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert any(f.defect_class == "dangling_reference" for f in findings)

def test_collect_without_source_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    # remove the spawn/interactable source chain
    for rid in [r.id for r in g.neighbors("spawn:emblem_pile", EdgeType.SPAWNS)]:
        g.remove_relation(rid)
    for rid in [r.id for r in g.neighbors("interact:emblem_pile", EdgeType.CONTAINS)]:
        g.remove_relation(rid)
    from spine.ir.snapshot import Snapshot
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert any(f.defect_class == "missing_drop_source" for f in findings)

def test_cycle_flagged():
    snap = load_scenario("scenarios/caravan.yaml")
    g = snap.to_graph()
    g.add_relation(Relation(id="r_cycle", type=EdgeType.PRECEDES,
                            src_id="step:turn_in", dst_id="step:talk_lincheng"))
    from spine.ir.snapshot import Snapshot
    findings = StructuralChecker().check(Snapshot.from_graph(g))
    assert any(f.defect_class == "cyclic_dependency" for f in findings)
```

(This task also adds `IRGraph.remove_relation(id)` if not already present from Task 4 — add it in Task 4's store with a test; if discovered here, add + test here.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/spine/checkers -v`
Expected: FAIL.

- [ ] **Step 3: Implement `spine/checkers/base.py` and `spine/checkers/structural.py`** per Interfaces. Cycle detection via DFS colouring on the `HAS_STEP`+`PRECEDES` subgraph. Each Finding's `minimal_repro` = `{"entity": step_id, "source_ref": <step.source_ref or None>}`; `evidence` = for cycle the node path, for dangling the missing id, for missing-source the item id + region.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/spine/checkers -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(spine/checkers): minimal deterministic structural checker (refs/source/cycle)"
```

---

## Task 7: env — Environment ABC (interface, no implementation)

**Files:**
- Create: `env/base.py`
- Test: `tests/env/test_base.py`

**Interfaces:**
- Consumes: `contracts.env_types.{Action,Observation,StepResult}`, `contracts.versions.ENV_CONTRACT_VERSION`.
- Produces: `class Environment(abc.ABC)` with abstract `reset(self, scenario: str, seed: int) -> Observation`, `step(self, action: Action) -> StepResult`, `state_hash(self) -> str`; class attribute `env_contract_version = ENV_CONTRACT_VERSION`.

- [ ] **Step 1: Write the failing test** — `tests/env/test_base.py`

```python
import pytest
from env.base import Environment

def test_environment_is_abstract():
    with pytest.raises(TypeError):
        Environment()  # abstract methods unimplemented

def test_contract_version_pinned():
    assert Environment.env_contract_version == "env@1"
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/env -v` → FAIL.
- [ ] **Step 3: Implement `env/base.py`** with `abc.ABC` + `@abc.abstractmethod`.
- [ ] **Step 4: Run test to verify it passes** — `uv run pytest tests/env -v` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(env): Agent-Env Environment ABC (engine-agnostic interface)"`

---

## Task 8: game/aureus — deterministic grid navigation + nav derived view

**Files:**
- Create: `game/aureus/grid.py`
- Test: `tests/game/aureus/test_grid.py`

**Interfaces:**
- Consumes: nothing outside stdlib + `contracts.world.GridSpec`.
- Produces:
  - `class Grid`: `__init__(spec: GridSpec)`; `is_walkable(pos)->bool`; `shortest_path(src, dst)->list[tuple[int,int]]|None` — BFS 4-neighbour, deterministic tie-break (neighbour order N,E,S,W), returns the path **including** endpoints or `None` if unreachable.
  - `class AureusNav`: implements `spine.ir.store.NavProvider` shape (`reachable(src_pos,dst_pos)->bool`, `pos_of(entity_id)->tuple|None`) — but defined here in `game` (structural duck-typing; game must not import spine). `reachable` = `shortest_path is not None`.

- [ ] **Step 1: Write the failing test** — `tests/game/aureus/test_grid.py`

```python
from contracts.world import GridSpec
from game.aureus.grid import Grid

def test_shortest_path_length_and_determinism():
    g = Grid(GridSpec(width=5, height=5, blocked=[]))
    p1 = g.shortest_path((0, 0), (2, 0))
    assert p1 == g.shortest_path((0, 0), (2, 0))  # deterministic
    assert p1[0] == (0, 0) and p1[-1] == (2, 0) and len(p1) == 3

def test_blocked_cells_route_around_or_unreachable():
    g = Grid(GridSpec(width=3, height=3, blocked=[[1, 0], [1, 1], [1, 2]]))
    assert g.shortest_path((0, 0), (2, 0)) is None  # wall splits grid
    assert not g.is_walkable((1, 1))
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/game/aureus/test_grid.py -v` → FAIL.
- [ ] **Step 3: Implement `game/aureus/grid.py`** (BFS with a `collections.deque`, visited set, parent map; blocked normalized to `set[tuple[int,int]]`).
- [ ] **Step 4: Run test to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(game/aureus): deterministic grid navigation + nav derived view"`

---

## Task 9: game/aureus — kernel (quest state machine + Environment implementation + state_hash)

**Files:**
- Create: `game/aureus/world.py`, `game/aureus/kernel.py`
- Test: `tests/game/aureus/test_kernel.py`

**Interfaces:**
- Consumes: `contracts.world.WorldConfig`, `contracts.env_types.*`, `env.base.Environment`, `game.aureus.grid.{Grid,AureusNav}`.
- Produces:
  - `class AureusWorld`: built from `WorldConfig`; tracks entity positions, interactable yields, quest definitions/state.
  - `class AureusEnv(Environment)`: `__init__(world_config: WorldConfig)`; `reset(scenario, seed)->Observation`; `step(action)->StepResult`; `state_hash()->str`; `nav_provider()->AureusNav`.
    - Movement: `navigate_to(target)` computes grid path to the target entity's cell and advances **one cell per tick** (each `step` call advances one cell if a path is in progress; issuing `navigate_to` sets the path, subsequent auto-advance until arrival — OR resolves fully with `tick += len(path)-1`). **Chosen semantics for M0a determinism & per-tick hashing: `navigate_to` advances one cell per `step`; returns `done=False, last_action_result="moving"|"arrived"`.**
    - `interact(target)`: if target is the quest giver and current step is `talk` → advance step, `last_action_result="quest_accepted"|"step_talk_done"`; if giver and all objectives complete and current step is `turn_in` → complete quest, grant reward, `done=True`. If target is a gather interactable → yield items into inventory, advance a matching `collect` step when count satisfied.
    - `pickup(item_id)`: adds item if co-located with its source.
    - Combat/economy actions (`attack/cast_skill/use/equip/buy/sell`) → `StepResult` with `last_action_result="unsupported_in_m0a"`, `done=False` (defined now, implemented M0b — no simplification).
    - Observation fully populated (`reachable_targets` = entities with a grid path from player; `available_interactions` = adjacent/co-located interactables & npcs; `quest_state` = per-quest step index + objective counts; combat fields = empty lists).
    - `state_hash()` (contract §4.4): `compute over canonical dict {tick, player_pos, player_stats, inventory, quest_states, world_object_states, monster_states:{}, event_flags, rng:{seed,draws}}` — **excludes** logs/render/wall-clock/debug. Reuse `contracts.canonical.canonical_json` + sha256.

- [ ] **Step 1: Write the failing tests** — `tests/game/aureus/test_kernel.py`

```python
from contracts.world import (WorldConfig, ScenarioConfig, GridSpec, Placement,
                             QuestSpec, QuestStepSpec)
from contracts.ir import NodeType
from contracts.env_types import parse_action
from game.aureus.kernel import AureusEnv

def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=6, height=6, blocked=[]),
        placements=[
            Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={}),
            Placement(entity_id="interact:pile", type=NodeType.INTERACTABLE, pos=(4, 4),
                      attrs={"kind": "gather", "yields_item": "item:x", "yields_count": 2}),
        ],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 60}, steps=[
            QuestStepSpec(step_id="t", kind="talk", target="npc:a"),
            QuestStepSpec(step_id="c", kind="collect", item="item:x", count=2),
            QuestStepSpec(step_id="d", kind="turn_in", target="npc:a"),
        ])],
    )

def test_reset_is_deterministic():
    e1, e2 = AureusEnv(_wc()), AureusEnv(_wc())
    e1.reset("s", seed=7); e2.reset("s", seed=7)
    assert e1.state_hash() == e2.state_hash()

def test_navigate_advances_one_cell_per_tick():
    e = AureusEnv(_wc()); e.reset("s", seed=1)
    r = e.step(parse_action({"kind": "navigate_to", "target": "npc:a"}))
    assert r.observation.player_pos == (1, 0) or r.observation.last_action_result in ("moving", "arrived")

def test_unsupported_combat_action_is_declared_not_crashing():
    e = AureusEnv(_wc()); e.reset("s", seed=1)
    r = e.step(parse_action({"kind": "attack", "target_id": "mob:1"}))
    assert r.observation.last_action_result == "unsupported_in_m0a" and r.done is False

def test_replay_same_seed_same_per_tick_hash():
    actions = [{"kind": "observe"}, {"kind": "navigate_to", "target": "npc:a"},
               {"kind": "wait", "ticks": 1}]
    def run():
        e = AureusEnv(_wc()); e.reset("s", seed=3); hs = [e.state_hash()]
        for a in actions:
            e.step(parse_action(a)); hs.append(e.state_hash())
        return hs
    assert run() == run()   # contract §4.4 anchor: per-tick state_hash equal across replays
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run pytest tests/game/aureus/test_kernel.py -v` → FAIL.
- [ ] **Step 3: Implement `game/aureus/world.py` then `game/aureus/kernel.py`** per Interfaces.
- [ ] **Step 4: Run tests to verify they pass** — PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(game/aureus): deterministic kernel — quest state machine, Env impl, state_hash"`

---

## Task 10: apps/cli — IR→WorldConfig, scripted macro-driver, and the end-to-end slice

**Files:**
- Create: `apps/cli/ir_to_world.py`, `apps/cli/driver.py`, `apps/cli/run_slice.py`
- Test: `tests/apps/test_run_slice.py`

**Interfaces:**
- Consumes: `spine.ir.{loader,snapshot,store}`, `spine.checkers.structural`, `game.aureus.kernel.AureusEnv`, `contracts.world.WorldConfig`, `contracts.env_types.parse_action`.
- Produces:
  - `snapshot_to_world(snapshot: Snapshot) -> WorldConfig` — reads IR (grid from region/scenario attrs, placements from npc/interactable positions, quests from HAS_STEP/PRECEDES ordered steps) and builds the `WorldConfig` Aureus consumes (**this is "Aureus driven by IR-exported config"**).
  - `class ScriptedDriver` — the M0a stand-in planner (LLM Playtest Agent is M2): given the quest's ordered steps, compiles high-level macros to atomic actions using `Observation.reachable_targets`/`available_interactions`: `talk` → navigate_to(giver) until arrived + interact(giver); `collect` → navigate_to(source) + interact/pickup until count met; `turn_in` → navigate_to(giver) + interact(giver). Returns `(completed: bool, trajectory: list[str], final_hash: str)`.
  - `run_slice(scenario_path: str, seed: int = 0) -> dict` — orchestrates: `load_scenario → run_all(checker) → (abort with findings if any critical) → snapshot_to_world → AureusEnv.reset(nav) → ScriptedDriver.run → assert quest completed`; returns `{"completed", "findings", "trajectory", "final_hash", "ticks"}`.

- [ ] **Step 1: Write the failing test** — `tests/apps/test_run_slice.py`

```python
from apps.cli.run_slice import run_slice

def test_vertical_slice_completes_three_step_chain():
    out = run_slice("scenarios/caravan.yaml", seed=0)
    assert out["findings"] == []                 # clean config passes the checker gate
    assert out["completed"] is True              # talk -> collect -> turn_in reached "completed"
    assert out["ticks"] >= 3

def test_slice_is_reproducible():
    a = run_slice("scenarios/caravan.yaml", seed=0)
    b = run_slice("scenarios/caravan.yaml", seed=0)
    assert a["final_hash"] == b["final_hash"] and a["trajectory"] == b["trajectory"]

def test_checker_gate_blocks_broken_scenario(tmp_path):
    import yaml
    data = yaml.safe_load(open("scenarios/caravan.yaml"))
    data["interactables"] = []      # remove the collect source
    data["spawn_points"] = []
    p = tmp_path / "broken.yaml"; p.write_text(yaml.safe_dump(data))
    out = run_slice(str(p), seed=0)
    assert any(f["defect_class"] == "missing_drop_source" for f in out["findings"])
    assert out["completed"] is False
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/apps/test_run_slice.py -v` → FAIL.
- [ ] **Step 3: Implement the three modules.** `run_slice` returns `findings` as `model_dump()` dicts; if any finding has `severity` in `{"critical","major"}`, set `completed=False` and skip execution (checker gate). Inject `AureusEnv.nav_provider()` into `StructuralChecker.check(...)` so reachability uses the real grid.
- [ ] **Step 4: Run test to verify it passes** — `uv run pytest tests/apps/test_run_slice.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(apps/cli): IR->WorldConfig, scripted driver, end-to-end vertical slice"`

---

## Task 11: web — Vite + React + TypeScript scaffold

**Files:**
- Create: `web/package.json`, `web/tsconfig.json`, `web/tsconfig.node.json`, `web/vite.config.ts`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/vite-env.d.ts`, `web/.gitignore`
- Test: build verification (`npm run build`)

**Interfaces:**
- Produces: a valid Vite React-TS project (placeholder console shell — full pages are M4). `App.tsx` renders a titled shell listing the M0-M4 milestone map (static). No dependency on business packages (contract §1: web only via apps/api, which does not exist yet in M0a).

- [ ] **Step 1: Create the scaffold files** — standard Vite `react-ts` template contents; `package.json` with `react`, `react-dom`, `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`; scripts `dev`/`build`/`preview`. `App.tsx`:

```tsx
export default function App() {
  return (
    <main style={{ fontFamily: "system-ui", padding: 24 }}>
      <h1>GameForge Console</h1>
      <p>Correctness compiler + agent workbench — scaffold (M0a).</p>
    </main>
  );
}
```

- [ ] **Step 2: Install & build**

Run: `cd web && npm install && npm run build`
Expected: `dist/` produced, exit 0. (If offline, verify `npx tsc --noEmit` typechecks; note the limitation.)

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "chore(web): Vite + React + TypeScript console scaffold"
```

---

## Task 12: Milestone acceptance & wrap-up

**Files:**
- Modify: `CLAUDE.md` (milestone table M0a → ✅), `docs/superpowers/plans/README.md` (note plan executed)
- Create: `README.md` (repo root: quickstart — `uv sync`, `uv run pytest`, `uv run lint-imports`, `uv run python -m apps.cli.run_slice`)
- Create: `apps/cli/__main__.py` (so `uv run python -m apps.cli.run_slice` / a `python -m apps.cli` prints the slice result)

**Interfaces:**
- Produces: green full test suite + lint gate + the acceptance command demonstrating `config → IR → Aureus 3-step chain`.

- [ ] **Step 1: Full acceptance run**

Run:
```bash
uv run pytest -v
uv run lint-imports
uv run python -c "from apps.cli.run_slice import run_slice; import json; print(json.dumps(run_slice('scenarios/caravan.yaml', seed=0)['completed']))"
```
Expected: all tests PASS; import-linter all contracts kept; slice prints `true`.

- [ ] **Step 2: Update `CLAUDE.md`** milestone row M0a status → ✅完成; add a one-line acceptance evidence note.

- [ ] **Step 3: Write repo `README.md`** with the quickstart + acceptance command + a short "what M0a delivers / what is deferred to M0b" section.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: M0a acceptance — vertical slice green; mark M0a complete"
```

---

## Self-Review

**1. Spec coverage (M0a deliverables from CLAUDE.md roadmap + PRD §14 + contract 分期表):**
- contracts package → Tasks 2, 3 ✔
- IR core 类型 + canonical 快照 (+ Relation.id/source_ref) → Tasks 2, 4, 5 ✔
- 仓库分层 + 依赖 lint (no-LLM-SDK) → Task 1 ✔
- Agent-Env 双层动作 + Observation + state_hash 作用域 → Tasks 3 (types/macros), 7 (ABC), 9 (impl + state_hash §4.4) ✔
- Aureus 最小内核（任务状态机 + 网格导航）→ Tasks 8, 9 ✔
- 最小 checker → Task 6 ✔
- Python/React 脚手架 → Tasks 1, 11 ✔
- 退出准则: 手写配置 → IR → Aureus 跑通 3+ 步任务链 (talk→collect→turn-in) → Tasks 5, 10, 12 ✔
- 可复现（同 seed 同 state_hash）→ Tasks 9, 10 ✔

**2. Placeholder scan:** no "TBD/etc"; every code step shows real code or an exact interface spec. Combat/economy actions are *declared and handled* (`unsupported_in_m0a`), not omitted — honouring 不简化只延后.

**3. Type consistency:** `NodeType`/`EdgeType`/`Entity`/`Relation` defined in Task 2 and reused verbatim in 4/5/6; `Observation`/`Action`/`StepResult`/`WorldConfig` defined in Task 3 and consumed in 7/9/10; `Snapshot.from_graph`/`to_graph`/`diff().is_empty()` consistent across 4/5/6/10; `NavProvider` shape (`reachable`,`pos_of`) defined in 4, duck-implemented by `AureusNav` in 8, injected in 6/10; schema-version constants centralized in Task 1.

**Deferred to later milestones (interfaces defined now, impl later — per 不简化只延后):** combat/economy Env actions & IR combat-economy node/edge impl (M0b); Schema Registry round-trip adapter (M0b); version/lineage/audit + DB migration (M0b); DSL grammar + Graph/ASP/SMT compiler + economy sim (M1); LLM agents/cassette/model-router (M2); full web pages (M4).
