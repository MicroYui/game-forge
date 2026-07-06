# M1 — 确定性可信主干（Deterministic Trusted Trunk）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **实现子 agent 用 sonnet5。**

**Goal:** 把 GameForge 的确定性可信主干补全——约束 DSL（谓词级 oracle）编译成 Graph/ASP(Clingo)/SMT(z3) 检查器套件（含 solver 预算/降级），经济仿真引擎复现并预警一次经济崩坏，接入一个开源数据驱动游戏的真实配置做无损往返（外部效度前置），实现确定性 typed-patch 应用/拒绝引擎，并用 Review Report 聚合 Finding——全程 oracle 误报=0、可复现、TDD。

**Architecture:** 全部落在 `spine`（确定性主干，`spine → contracts` 仅此一项，禁 LLM SDK；clingo/z3 是非-LLM 外部库，允许）。DSL *文法/schema* 归 `contracts/dsl.py`（机器可读真相），DSL *解析/AST/编译* 归 `spine/dsl/`，检查器归 `spine/checkers/`，经济仿真归 `spine/sim/`，patch 引擎归 `spine/patch.py`，开源适配器归 `spine/ingestion/`。M0a 的硬编码 `StructuralChecker` 保留为**差分测试的朴素参照实现**（契约3 锚点：朴素图算法 vs ASP 编码对拍一致）。Finding/Patch schema（契约6）M0a 已定全，M1 只实现**生产者**与**确定性 patch 应用/拒绝**。

**Tech Stack:** Python 3.12 (uv), pydantic v2, PyYAML, stdlib `ast`/`csv`/`json`, **clingo>=5.6**（ASP，含内置 solver）, **z3-solver>=4.12**（SMT）, pytest + hypothesis（差分/属性两条质量线）, import-linter, ruff。**不新增 networkx**——图算法手写（同时充当差分测试的"朴素参照实现"）。

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 + 地基契约。每个 task 的 requirements 隐含包含本节。

- **不简化，只延后**：接口/契约现在就定全；只有*实现*可延后。Finding/Patch schema（契约6）M0a 已定全字段，M1 不裁剪。DSL 含 `llm-assisted` 谓词的路由接口现在定义，但 M1 **不**把 llm-assisted 谓词编译成确定性 checker（路由到 agent 层的实现是 M2）。
- **确定性优先**：对/错判定由 图/ASP(Clingo)/SMT(z3)/仿真 给出；**M1 全程无 LLM 调用**。solver `unknown`/超时/超预算 → Finding `status="unproven"`，**绝不等于 pass**。
- **依赖方向单向（CI 强制）**：`spine → contracts` 仅此一项。spine 禁止 import `runtime`/`env`/`game`/`agents`/`platform`/`apps`/`bench` 及任何 LLM SDK（openai/anthropic/langchain/langgraph/llama_index）。`clingo`/`z3` 是非-LLM 外部库，**允许**。经济仿真需要的 seeded RNG 必须 spine 自带（**不得** import `game.aureus.rng`）。
- **企业级 = 生产级工程成熟度**（production maturity），非商业化。
- **可复现只承诺回放**：仿真 seed 化；相同 `(scenario, seed)` → 相同分布与统计。
- **TDD 全程**：每个 task test-first（写失败测试 → 跑 → 实现 → 跑 → commit）。检查器/编译器用**差分测试**（多引擎/朴素对拍）+ **属性测试**（随机图对拍朴素实现）；适配器用 round-trip property test。这是 §12A.1 的"实现正确性"独立质量线（区别于"检出率"）。
- **误报口径分两类**（§13.4）：`oracle-FP`（检查器算法误报，**目标=0**）与 `constraint-FP`（约束被人误批，M1 不涉及）分别统计。
- **确定性 Finding 与 llm-assisted Finding 严格分区**（契约6 锚点）：ReviewReport 分区存储/统计，不混入一个数字。
- **Git**：commit 信息不带任何 AI 协作者署名 / "Generated with"。主干分支 `master`。
- **Package namespace**：所有 Python 在 `gameforge/` 下；import 为 `gameforge.<pkg>`。下文片段写短形式（`contracts.dsl`/`spine.checkers.*`），一律按 `gameforge.` 前缀读。
- **schema_version 常量**（`contracts/versions.py` 已有）：`DSL_GRAMMAR_VERSION="dsl@1"`、`FINDING_SCHEMA_VERSION="finding@1"`、`PATCH_SCHEMA_VERSION="patch@1"`。M1 新增 `REVIEW_SCHEMA_VERSION="review@1"`。

## 关键决策（best-judgment；可回改，参照 M0b 的 D1–D3 风格）

| # | 决策 | 选择 | 理由 / 可回改性 |
|---|---|---|---|
| M1-D1 | ASP 引擎 | **clingo（Python 包，内置 solver，无需系统装 gringo/clasp）** | 零系统依赖、确定性、CI 友好。换 DLV/其他 = 换 ASPChecker 后端，DSL/编译接口不变。 |
| M1-D2 | SMT 引擎 | **z3-solver（Python 包）** | 事实标准；`z3.Solver.check()` 超时→`unknown`→降级 unproven。 |
| M1-D3 | 图算法库 | **手写 BFS/DFS/SCC(Tarjan)/拓扑，不引 networkx** | 手写实现天然充当差分测试的"朴素参照"（契约3/§12A.1）；少一个依赖。 |
| M1-D4 | DSL assert 表达式 | **受限 mini 表达式语言，用 stdlib `ast` 解析成 typed AST**（复用 `game/aureus/formula.py` 的 fail-closed 白名单思路），非任意 Python `eval` | 确定性、可编译到 Clingo/z3、可静态分析谓词 oracle。 |
| M1-D5 | 开源游戏（外部效度） | **Flare（flare-engine 开源 RPG）的 INI 式 `key=value` 记录 `.txt` 配置**（items/enemies/loot） | 真实数据驱动 RPG，记录式文本 1:1 映射 typed entity+attrs，往返无损直观。vendored 一份真实样本到 `scenarios/flare_sample/`（带上游 URL + 许可署名）。换游戏 = 新增一个 Adapter，不返工。fallback：Endless Sky。 |
| M1-D6 | 经济仿真 RNG | **spine 自带 `spine/sim/rng.py` 的 seeded 确定性 RNG**（不 import game） | 依赖方向：spine 不能碰 game.aureus.rng。 |
| M1-D7 | solver 预算 | **ASP grounding 原子数上限（默认 200k）+ 墙钟超时（默认 10s）；z3 `set("timeout", 5000)`（5s）**；任一触发 → `status="unproven"` | 契约3/§7.3：绝不把 unknown 当 pass。阈值可配。 |

---

## Repo layout delta produced by this plan

```
gameforge/
  contracts/
    dsl.py              # CREATE: Constraint DSL schema (谓词级 oracle) — 机器可读文法
    review.py           # CREATE: ReviewReport schema (确定性/llm-assisted/仿真 严格分区)
    versions.py         # MODIFY: + REVIEW_SCHEMA_VERSION
  spine/
    dsl/
      __init__.py
      ast.py            # CREATE: Selector + AssertExpr typed AST + safe ast-based parser
      compile.py        # CREATE: compile(constraint) -> Checker; 路由 + solver 预算/降级
    checkers/
      base.py           # EXISTING: Checker Protocol (keep)
      structural.py     # EXISTING: M0a 硬编码 checker → 保留为差分"朴素参照"
      graph.py          # CREATE: GraphChecker (手写 BFS/DFS/Tarjan/拓扑) — 7 结构缺陷类
      asp.py            # CREATE: ASPChecker (Clingo) + grounding 预算
      smt.py            # CREATE: SMTChecker (z3) + 超时预算 — 5 数值缺陷类
      report.py         # CREATE: build_review_report(findings) -> ReviewReport
    sim/
      __init__.py
      rng.py            # CREATE: spine-local seeded 确定性 RNG
      economy.py        # CREATE: EconomySimulator (Monte-Carlo + agent-based)
    ingestion/
      flare_adapter.py  # CREATE: FlareTxtAdapter (to_ir/from_ir) — 开源游戏往返
    patch.py            # CREATE: apply_patch(snapshot, patch) 乐观并发/拒绝 (契约6 锚点)
scenarios/
  flare_sample/         # CREATE: vendored 真实 Flare 配置样本 + NOTICE (上游 URL/许可)
  defects/              # CREATE: ≥8 缺陷注入场景 (structural + numeric) + 干净基线
  constraints/          # CREATE: 约束 DSL YAML (对应各缺陷类)
pyproject.toml          # MODIFY: + clingo, z3-solver; import-linter 无需改 (checkers/dsl/sim 已在 spine)
```

---

## Task 1: Setup — 依赖、版本常量、包骨架、solver 冒烟测试

**Files:**
- Modify: `pyproject.toml`, `gameforge/contracts/versions.py`, `gameforge/contracts/__init__.py`
- Create: `gameforge/spine/dsl/__init__.py`, `gameforge/spine/sim/__init__.py`
- Test: `tests/spine/test_m1_setup.py`

**Interfaces:**
- Produces: `clingo`/`z3` importable；`contracts.versions.REVIEW_SCHEMA_VERSION == "review@1"`；`spine.dsl`/`spine.sim` 包可导入；`uv run lint-imports` green（spine 仍只依赖 contracts；clingo/z3 允许）。

- [ ] **Step 1: Write failing test** — `tests/spine/test_m1_setup.py`:
```python
def test_solvers_available():
    import clingo, z3  # noqa: F401
    assert clingo.__version__
    assert z3.get_version_string()

def test_review_schema_version_present():
    from gameforge.contracts import versions as v
    assert v.REVIEW_SCHEMA_VERSION == "review@1"

def test_new_spine_packages_importable():
    import gameforge.spine.dsl  # noqa: F401
    import gameforge.spine.sim  # noqa: F401
```
- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/spine/test_m1_setup.py -v` → FAIL.
- [ ] **Step 3: Add deps** — `pyproject.toml` `[project].dependencies` += `"clingo>=5.6"`, `"z3-solver>=4.12"`.
- [ ] **Step 4: Add version constant** — append `REVIEW_SCHEMA_VERSION = "review@1"` to `contracts/versions.py`; re-export in `contracts/__init__.py`.
- [ ] **Step 5: Create package `__init__.py`** — each a one-line docstring (`"""GameForge spine.dsl — 约束 DSL 解析/编译 (确定性)。"""` 等).
- [ ] **Step 6: Provision + run** — `uv sync && uv run pytest tests/spine/test_m1_setup.py -v && uv run lint-imports` → PASS（clingo/z3 不触发 spine 禁止项；6 契约仍 KEPT）。
- [ ] **Step 7: Commit** — `git commit -m "chore(m1): add clingo/z3-solver, REVIEW_SCHEMA_VERSION, spine.dsl/sim skeletons"`

> 注：若 `clingo`/`z3-solver` 的 wheel 在本机架构缺失导致 `uv sync` 失败，STOP 并报告——这是 M1 的硬前置，不得跳过或用桩替代。

---

## Task 2: contracts — 约束 DSL schema（谓词级 oracle）+ ReviewReport schema

**Files:**
- Create: `gameforge/contracts/dsl.py`, `gameforge/contracts/review.py`
- Test: `tests/contracts/test_dsl.py`, `tests/contracts/test_review.py`

**Interfaces:**
- Consumes: `contracts.versions.{DSL_GRAMMAR_VERSION, REVIEW_SCHEMA_VERSION, FINDING_SCHEMA_VERSION}`, `contracts.findings.{Finding, Severity, OracleType}`.
- Produces（全部 pydantic `BaseModel`，契约3/契约6 字段一次定全）：
  - `ConstraintKind = Literal["structural","numeric","narrative"]`
  - `PredicateOracle = Literal["deterministic","llm-assisted"]`
  - `Predicate{expr:str, oracle:PredicateOracle="deterministic"}`
  - `Selector{var:str, node_type:str, where:dict[str,Any]={}}` —— e.g. `{var:"step", node_type:"QUEST_STEP", where:{kind:"collect"}}`（对应 DSL `forall: {step: QuestStep, type: collect}`）
  - `class Constraint(BaseModel){id:str, dsl_grammar_version:str=DSL_GRAMMAR_VERSION, kind:ConstraintKind, oracle:Literal["deterministic","llm-assisted","mixed"], predicates:list[Predicate]=[], scope:Selector|None=None, forall:Selector|None=None, assert_:str (alias "assert"), severity:Severity, note:str|None=None}`
    - method `has_llm_predicate() -> bool`：`self.oracle in ("llm-assisted","mixed") or any(p.oracle=="llm-assisted" for p in self.predicates)`。
    - `@classmethod from_yaml(text:str) -> list[Constraint]`（PyYAML load；`assert` 是 YAML 保留字，用 `Field(alias="assert")` + `populate_by_name=True`）。
  - `contracts/review.py`：`class DefectClassCount{defect_class:str, severity:Severity, count:int}`；`class ReviewReport(BaseModel){review_schema_version:str=REVIEW_SCHEMA_VERSION, snapshot_id:str, deterministic_findings:list[Finding]=[], llm_assisted_findings:list[Finding]=[], simulation_findings:list[Finding]=[], unproven_findings:list[Finding]=[], by_defect_class:list[DefectClassCount]=[], created_at:str|None=None}` + method `total_deterministic()->int`。
    - **严格分区不变量**（契约6）：一个 Finding 依 `oracle_type` 与 `status` 恰好落一个桶——`oracle_type=="llm-assisted"`→`llm_assisted_findings`；`status=="unproven"`→`unproven_findings`；`oracle_type=="simulation"`→`simulation_findings`；否则 `deterministic_findings`。

- [ ] **Step 1: Write failing tests** — `tests/contracts/test_dsl.py`:
```python
from gameforge.contracts.dsl import Constraint, Predicate, Selector

_YAML = """
- id: C_newbie_gold_cap
  kind: numeric
  oracle: deterministic
  scope: {var: q, node_type: QUEST, where: {region: newbie_zone}}
  assert: "reward_gold <= 80"
  severity: major
- id: C_baiyuan_semantic
  kind: narrative
  oracle: mixed
  predicates:
    - {expr: "chapter >= 3", oracle: deterministic}
    - {expr: "semantically_reveals_identity(dialogue, baiyuan)", oracle: llm-assisted}
  assert: "chapter >= 3"
  severity: critical
"""

def test_parse_constraints_and_alias_assert():
    cs = Constraint.from_yaml(_YAML)
    assert cs[0].id == "C_newbie_gold_cap"
    assert cs[0].assert_ == "reward_gold <= 80"      # `assert` YAML key -> assert_
    assert cs[0].dsl_grammar_version == "dsl@1"

def test_predicate_level_oracle_routing():
    cs = Constraint.from_yaml(_YAML)
    assert cs[0].has_llm_predicate() is False        # pure deterministic
    assert cs[1].has_llm_predicate() is True         # one llm-assisted predicate -> whole constraint routes to LLM
```
`tests/contracts/test_review.py`:
```python
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.findings import Finding

def _f(fid, oracle_type, status="confirmed"):
    return Finding(id=fid, source="checker", producer_id="p", producer_run_id="r",
                   oracle_type=oracle_type, defect_class="x", severity="major",
                   snapshot_id="sha256:s", status=status, message="m")

def test_report_partitions_by_oracle_and_status():
    r = ReviewReport.partition("sha256:s", [
        _f("a", "deterministic"), _f("b", "llm-assisted"),
        _f("c", "deterministic", status="unproven"), _f("d", "simulation"),
    ])
    assert [f.id for f in r.deterministic_findings] == ["a"]
    assert [f.id for f in r.llm_assisted_findings] == ["b"]
    assert [f.id for f in r.unproven_findings] == ["c"]
    assert [f.id for f in r.simulation_findings] == ["d"]
    assert r.total_deterministic() == 1
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `contracts/dsl.py` + `contracts/review.py` per Interfaces（`ReviewReport.partition(snapshot_id, findings)` classmethod 实现分区不变量 + 填 `by_defect_class`）。
- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/contracts/test_dsl.py tests/contracts/test_review.py -v` → PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(contracts): 约束 DSL schema (谓词级 oracle) + ReviewReport 分区 schema"`

---

## Task 3: spine/dsl — Selector + Assert 表达式 typed AST（安全解析）

**Files:**
- Create: `gameforge/spine/dsl/ast.py`
- Test: `tests/spine/dsl/test_ast.py`

**Interfaces:**
- Consumes: `contracts.dsl.{Constraint, Selector, Predicate}`, `contracts.ir.NodeType`, stdlib `ast`.
- Produces:
  - `class DslError(Exception)`。
  - `select(graph:IRGraph, selector:Selector) -> list[Entity]`：`nodes_of_type(NodeType[selector.node_type])` 再按 `where` 逐字段过滤（`e.attrs.get(k)==v`）。`node_type` 非法枚举 → `DslError`。
  - `parse_assert(expr:str) -> AssertNode`：用 `ast.parse(expr, mode="eval")` 解析，白名单 **仅**允许：比较（`== != < <= > >=`）、布尔（`and or not`）、算术（`+ - * / // %`）、整/浮点/字符串常量、`Name`（字段/绑定变量）、属性访问 `a.b`（编译成字段路径）、白名单函数调用 `exists(...)`、`forall(...)`、`reachable_in(item, region)`、`sum(...)`、`prob_sum(...)`、`monotonic(...)`、`in_range(x, lo, hi)`、`count(...)` 以及 llm-assisted 占位 `semantically_reveals_identity(...)`（解析成节点但**不**在 M1 求值）。其它 AST 节点 → `DslError`（fail-closed，复用 `game/aureus/formula.py` 的白名单递归下降风格）。
  - `AssertNode` 是一棵 dataclass/pydantic 树：`Compare/BoolOp/BinOp/Const/Field/Call`，供 Task 4/6/7 的编译器消费（graph/ASP 消费 `exists/reachable_in/count`；SMT 消费 `Compare/BinOp/in_range/prob_sum/monotonic`）。
  - `free_names(node) -> set[str]`：抽取字段名（供编译器知道要绑定哪些图属性）。

- [ ] **Step 1: Write failing tests** — `tests/spine/dsl/test_ast.py`:
```python
import pytest
from gameforge.spine.dsl.ast import parse_assert, select, DslError
from gameforge.spine.ir.store import IRGraph
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.dsl import Selector

def test_parse_numeric_assert_tree():
    node = parse_assert("reward_gold <= 80")
    assert node.__class__.__name__ == "Compare"
    assert "reward_gold" in __import__("gameforge.spine.dsl.ast", fromlist=["free_names"]).free_names(node)

def test_parse_rejects_non_whitelisted():
    with pytest.raises(DslError):
        parse_assert("__import__('os').system('x')")
    with pytest.raises(DslError):
        parse_assert("[x for x in y]")   # comprehension not allowed

def test_selector_filters_by_type_and_where():
    g = IRGraph()
    g.add_entity(Entity(id="q1", type=NodeType.QUEST, attrs={"region": "newbie_zone"}))
    g.add_entity(Entity(id="q2", type=NodeType.QUEST, attrs={"region": "boss_zone"}))
    got = select(g, Selector(var="q", node_type="QUEST", where={"region": "newbie_zone"}))
    assert [e.id for e in got] == ["q1"]

def test_selector_bad_type_raises():
    with pytest.raises(DslError):
        select(IRGraph(), Selector(var="x", node_type="NOT_A_TYPE"))
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `spine/dsl/ast.py`（`ast.parse` + 递归白名单下降构树；`select` 用 `IRGraph.nodes_of_type`）。
- [ ] **Step 4: Run to verify pass** → PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(spine/dsl): safe Selector + assert-expr typed AST (ast-whitelist)"`

---

## Task 4: spine/checkers/graph — GraphChecker（手写图算法，7 结构缺陷类）

**Files:**
- Create: `gameforge/spine/checkers/graph.py`
- Test: `tests/spine/checkers/test_graph.py`, `tests/spine/checkers/test_graph_property.py`

**Interfaces:**
- Consumes: `spine.ir.{store.IRGraph, store.NavProvider, snapshot.Snapshot}`, `contracts.ir.{NodeType, EdgeType}`, `contracts.findings.Finding`.
- Produces: `class GraphChecker`（`id="graph"`，实现 `Checker` Protocol：`check(snapshot, nav=None) -> list[Finding]`），检出 **7 个结构 defect_class**，每个 Finding 带 `evidence`（断裂边/环路径/不可达对）+ `minimal_repro`（`source_ref`）+ `oracle_type="deterministic"` + `status="confirmed"`：
  1. `dangling_reference` — 关系端点不存在（对拍 M0a 规则1）。
  2. `missing_drop_source` — `collect` 步的 item 无 `GRANTS`/`DROPS_FROM` 源（有 nav 时要求源在起点可达）。
  3. `unreachable_target` — quest 的目标区域/交互点从起点不可达（用 `path_exists` + nav）。
  4. `cyclic_dependency` — `HAS_STEP`+`PRECEDES` 子图有环（Tarjan SCC；evidence=环路径）。
  5. `dead_quest` — quest 无 giver/无 `HAS_STEP`/无起点边，永远无法开始。
  6. `unsatisfiable_completion` — `turn_in`/完成步的前置步不可达或缺失（完成条件无法触发）。
  7. `isolated_node` — 关键实体（QUEST/NPC/ITEM/MONSTER）无任何入/出边（孤立）。
- 独立的、可被 property test 对拍的纯函数：`find_cycles(adj)->list[list[str]]`（Tarjan）、`reachable_set(adj, src)->set[str]`（BFS）、`topo_order(adj)->list[str]|None`。

- [ ] **Step 1: Write failing tests** — `tests/spine/checkers/test_graph.py`：对每个缺陷类构造一个最小触发图 + 一个干净图，断言"脏图恰好 1 个对应 defect_class 的 Finding、干净图 0 个"（oracle-FP=0 的单测雏形）。示例：
```python
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.contracts.ir import Entity, Relation, NodeType, EdgeType

def _snap(entities, relations):
    return Snapshot.from_entities_relations(entities, relations)  # 见注

def test_dangling_reference_detected_and_clean_is_silent():
    ents = [Entity(id="q", type=NodeType.QUEST)]
    rels = [Relation(id="r1", type=EdgeType.HAS_STEP, src_id="q", dst_id="missing")]
    fs = [f for f in GraphChecker().check(_snap(ents, rels)) if f.defect_class == "dangling_reference"]
    assert len(fs) == 1 and "missing" in fs[0].evidence["missing"]
    clean = GraphChecker().check(_snap([Entity(id="q", type=NodeType.QUEST)], []))
    assert [f for f in clean if f.defect_class == "dangling_reference"] == []

def test_cycle_detected_with_path_evidence():
    ents = [Entity(id=f"s{i}", type=NodeType.QUEST_STEP) for i in (1, 2, 3)]
    rels = [Relation(id="a", type=EdgeType.PRECEDES, src_id="s1", dst_id="s2"),
            Relation(id="b", type=EdgeType.PRECEDES, src_id="s2", dst_id="s3"),
            Relation(id="c", type=EdgeType.PRECEDES, src_id="s3", dst_id="s1")]
    fs = [f for f in GraphChecker().check(_snap(ents, rels)) if f.defect_class == "cyclic_dependency"]
    assert len(fs) == 1 and set(fs[0].evidence["cycle_path"]) == {"s1", "s2", "s3"}
```
> 注：若 `Snapshot` 无 `from_entities_relations` 便捷构造，本 task 先加一个（`tests` 用）——查 `spine/ir/snapshot.py` 现有构造并复用；不得改变 canonical 语义。
- [ ] **Step 2: property test** — `tests/spine/checkers/test_graph_property.py`（§12A.1 属性线）：hypothesis 随机生成有向图，断言 `find_cycles` 与一个**独立朴素**实现（朴素 DFS 三色回边）判定 acyclic 一致；`reachable_set` 与朴素 BFS 一致。
```python
from hypothesis import given, strategies as st
from gameforge.spine.checkers.graph import find_cycles, reachable_set

def _naive_has_cycle(adj):
    WHITE, GRAY, BLACK = 0, 1, 2; color = {}
    def dfs(u):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY: return True
            if color.get(v, WHITE) == WHITE and dfs(v): return True
        color[u] = BLACK; return False
    return any(color.get(u, WHITE) == WHITE and dfs(u) for u in list(adj))

@given(st.dictionaries(st.integers(0, 8),
                       st.lists(st.integers(0, 8), max_size=4), max_size=9))
def test_cycle_detection_matches_naive(adj):
    assert bool(find_cycles(adj)) == _naive_has_cycle(adj)
```
- [ ] **Step 3: Run to verify failure** → FAIL.
- [ ] **Step 4: Implement** `graph.py`（Tarjan SCC、BFS 可达、拓扑；7 缺陷类；纯函数暴露供对拍）。
- [ ] **Step 5: Run to verify pass** — `uv run pytest tests/spine/checkers -v` → PASS。
- [ ] **Step 6: Commit** — `git commit -m "feat(spine/checkers): GraphChecker — 7 结构缺陷类 + 图算法属性测试对拍朴素"`

---

## Task 5: spine/checkers/asp — ASPChecker（Clingo）+ 差分测试对拍 GraphChecker

**Files:**
- Create: `gameforge/spine/checkers/asp.py`
- Test: `tests/spine/checkers/test_asp.py`, `tests/spine/checkers/test_asp_vs_graph_differential.py`

**Interfaces:**
- Consumes: `clingo`, `spine.ir.*`, `contracts.findings.Finding`, `spine.checkers.graph`（仅测试对拍用）。
- Produces: `class ASPChecker`（`id="asp"`，实现 `Checker` Protocol）：
  - 把 IR 图**编码成 ASP facts**：`node(Id, Type). edge(Rid, Etype, Src, Dst). attr(Id, Key, Value).`（纯函数 `ir_to_asp_facts(graph) -> str`，可单测）。
  - 内置一组 ASP 规则（`.lp` 字符串常量）表达可编码的结构缺陷（任务可解性、依赖满足、"每个 collect 有源且源在可达区"、环）。检出经 `#show violation/3.` 输出 → 转 Finding（`oracle_type="deterministic"`，evidence=反例原子）。
  - `grounding_budget`（默认原子上限 200k / 墙钟 10s，见 M1-D7）：`clingo.Control` 设 `--` 限制 + 监控；超限 → 该约束 Finding `status="unproven"`（**绝不 pass**）。
- **差分锚点（契约3）**：对同一组共享的 structural 约束（环、collect-needs-source、可达性），`ASPChecker` 与 `GraphChecker`（朴素）在随机 IR 上检出**同一集合**的缺陷实体。

- [ ] **Step 1: Write failing tests** — `tests/spine/checkers/test_asp.py`：`ir_to_asp_facts` 生成的 fact 文本可被 `clingo.Control` ground/solve；一个有环图 → ASP 报 `cyclic_dependency`；干净图 → 无 violation。
- [ ] **Step 2: differential test** — `tests/spine/checkers/test_asp_vs_graph_differential.py`（§12A.1 差分线）：hypothesis 生成随机 quest/step/precedes 图，断言
```python
set(f.defect_class + ":" + ",".join(sorted(f.entities))
    for f in ASPChecker().check(snap) if f.defect_class in SHARED)
== set(... same for GraphChecker() ...)
```
两引擎在共享约束上逐缺陷一致。
- [ ] **Step 3: Run to verify failure** → FAIL。
- [ ] **Step 4: Implement** `asp.py`（`ir_to_asp_facts` + `.lp` 规则 + clingo 调用 + 预算/降级）。
- [ ] **Step 5: Run to verify pass** — `uv run pytest tests/spine/checkers/test_asp.py tests/spine/checkers/test_asp_vs_graph_differential.py -v` → PASS。
- [ ] **Step 6: Commit** — `git commit -m "feat(spine/checkers): ASPChecker (Clingo) + grounding 预算/降级 + 差分对拍 GraphChecker"`

---

## Task 6: spine/checkers/smt — SMTChecker（z3）+ 超时预算（5 数值缺陷类）

**Files:**
- Create: `gameforge/spine/checkers/smt.py`
- Test: `tests/spine/checkers/test_smt.py`

**Interfaces:**
- Consumes: `z3`, `spine.ir.*`, `spine.dsl.ast.{parse_assert, AssertNode}`, `contracts.findings.Finding`, `contracts.dsl.Constraint`.
- Produces: `class SMTChecker`（`id="smt"`，实现 `Checker` Protocol；构造时接收要检查的 numeric `Constraint` 列表），检出 **5 个数值 defect_class**，evidence 带 z3 `unsat_core`/违反赋值：
  1. `reward_out_of_range` — 奖励字段越界（`assert reward_gold <= 80`）。
  2. `prob_sum_ne_1` — DropTable/GachaPool 概率/权重和 ≠ 1（或权重归一后 ≠1）。
  3. `non_monotonic_curve` — Formula/曲线应单调但存在反例。
  4. `interval_violation` — 数值区间约束违反（`in_range(x, lo, hi)`）。
  5. `gacha_expectation_violation` — 抽卡期望/保底违规（期望抽数 > 保底阈值等）。
  - `assert` 表达式经 `parse_assert` → 编译成 z3 表达式；对 selector 选中的每个实体建模其数值字段为 z3 常量，求 `Not(assert)` 是否 SAT（SAT=存在违反→Finding + 违反赋值）。
  - **预算（M1-D7）**：`z3.Solver`; `s.set("timeout", 5000)`；`s.check()==z3.unknown` → Finding `status="unproven"`（绝不当 pass）。非线性不可判定片段有界近似并标 unproven。

- [ ] **Step 1: Write failing tests** — `tests/spine/checkers/test_smt.py`：每个数值缺陷类一个"违反→检出 + 违反赋值"与"满足→静默"用例。示例：
```python
def test_reward_out_of_range_detected():
    # QUEST q with reward_gold=120 under constraint reward_gold<=80 -> one finding
    ...
    assert fs[0].defect_class == "reward_out_of_range"
    assert fs[0].evidence["violating_assignment"]["reward_gold"] == 120

def test_prob_sum_ne_1_detected():
    # DropTable entries [0.5, 0.3] -> sum 0.8 != 1 -> finding
    ...
def test_satisfiable_constraint_is_silent():
    # reward_gold=50 under <=80 -> no finding
    ...
def test_unknown_degrades_to_unproven_never_pass():
    # a deliberately hard/nonlinear assert hitting timeout -> status == "unproven"
    ...
```
- [ ] **Step 2: Run to verify failure** → FAIL。
- [ ] **Step 3: Implement** `smt.py`（AST→z3 编译 + 5 缺陷类 + 超时→unproven）。
- [ ] **Step 4: Run to verify pass** → PASS。
- [ ] **Step 5: Commit** — `git commit -m "feat(spine/checkers): SMTChecker (z3) — 5 数值缺陷类 + 超时降级 unproven"`

---

## Task 7: spine/dsl/compile — compile(constraint) -> Checker（路由 + 预算/降级）

**Files:**
- Create: `gameforge/spine/dsl/compile.py`
- Test: `tests/spine/dsl/test_compile.py`

**Interfaces:**
- Consumes: `contracts.dsl.Constraint`, `spine.dsl.ast`, `spine.checkers.{graph.GraphChecker, asp.ASPChecker, smt.SMTChecker, base.Checker}`.
- Produces:
  - `class CompiledChecker`（实现 `Checker` Protocol）：包裹一个后端 + 绑定的 `constraint`；`check(snapshot, nav=None) -> [Finding]`，每个 Finding 的 `constraint_id=constraint.id`。
  - `compile(constraint:Constraint) -> Checker`（契约3）：
    - `constraint.has_llm_predicate()` → 返回一个 `LlmRoutedChecker`（**M1 不求值**：`check()` 返回一个 `oracle_type="llm-assisted"`、`status="unproven"`、`message="routed to agent layer (M2)"` 的占位 Finding，保证严格分区且不进确定性统计）。**接口现定，确定性编译延后**（不简化只延后）。
    - `kind=="structural"` → 路由 ASP（`ASPChecker`，可编码时）否则 Graph；`kind=="numeric"` → `SMTChecker`。
    - solver 预算/降级由后端负责（Task 5/6）；`compile` 只做路由。
  - `compile_all(constraints:list[Constraint]) -> list[Checker]`。
- **验证锚点**：`compile` 出的 structural checker 对同一约束与直接 GraphChecker/ASPChecker 结果一致；llm-assisted 约束**不**产生确定性 Finding。

- [ ] **Step 1: Write failing tests** — `tests/spine/dsl/test_compile.py`：
```python
def test_structural_constraint_compiles_and_binds_constraint_id():
    c = Constraint(id="C_cycle", kind="structural", oracle="deterministic",
                   assert_="acyclic(quest_steps)", severity="critical")
    chk = compile(c)
    fs = chk.check(snap_with_cycle)
    assert fs and all(f.constraint_id == "C_cycle" for f in fs)

def test_numeric_constraint_routes_to_smt():
    c = Constraint(id="C_cap", kind="numeric", oracle="deterministic",
                   scope=Selector(var="q", node_type="QUEST"),
                   assert_="reward_gold <= 80", severity="major")
    assert any(f.defect_class == "reward_out_of_range" for f in compile(c).check(snap_bad_reward))

def test_llm_assisted_constraint_does_not_produce_deterministic_finding():
    c = Constraint(id="C_sem", kind="narrative", oracle="llm-assisted",
                   predicates=[Predicate(expr="semantically_reveals_identity(d, x)", oracle="llm-assisted")],
                   assert_="chapter >= 3", severity="critical")
    fs = compile(c).check(snap_any)
    assert all(f.oracle_type == "llm-assisted" and f.status == "unproven" for f in fs)
```
- [ ] **Step 2–4: fail → implement → pass**。
- [ ] **Step 5: Commit** — `git commit -m "feat(spine/dsl): compile(constraint)->Checker 路由 (structural→ASP/Graph, numeric→SMT, llm→routed)"`

---

## Task 8: spine/sim — 经济仿真引擎（Monte-Carlo + agent-based）+ 复现崩坏预警

**Files:**
- Create: `gameforge/spine/sim/rng.py`, `gameforge/spine/sim/economy.py`
- Test: `tests/spine/sim/test_rng.py`, `tests/spine/sim/test_economy.py`

**Interfaces:**
- Consumes: `spine.ir.*`（读经济实体：CURRENCY/SHOP/DROP_TABLE/GACHA_POOL/MONSTER/ITEM/EQUIPMENT），`contracts.findings.Finding`。**不 import game**。
- Produces:
  - `rng.py`：`class SimRandom`（seeded；`randint/random/weighted_choice/draws` 计数，与 game.aureus.rng 同语义但 spine-local）。
  - `economy.py`：
    - `class EconomyModel`（从 snapshot 抽取：货币、掉落源产出速率、商店价、抽卡成本+保底+期望、装备曲线）。
    - `class EconomySimulator{run(model, seed, n_agents, n_ticks) -> SimResult}`：agent-based 模拟玩家群体行为（打怪掉落→货币 source，商店/抽卡→货币 sink），Monte-Carlo 跑 N 局。
    - `class SimResult{distributions:dict, invariants:list[InvariantCheck], sensitivity:dict}`；`InvariantCheck{name, ok:bool, observed, threshold, evidence}`。
    - **验证的不变量**（§7.4）：货币 sink/source 平衡、通胀率、掉落源存在性与产出速率、装备强度曲线单调、抽卡期望与保底、资源产出速率上限。
    - `to_findings(result, snapshot_id) -> list[Finding]`：违反的不变量 → `oracle_type="simulation"` Finding（source="sim"）；**描述性 what-if，永不给处方数字**。
  - **崩坏 + 预警**：`detect_collapse(result) -> CollapseReport|None`——sink/source 失衡（如通胀率超阈值、货币无界增长）判为"经济崩坏"，并在崩坏发生 tick **之前**基于趋势斜率给出 `early_warning_tick`。

- [ ] **Step 1: Write failing tests** — `tests/spine/sim/test_rng.py`（seed 可复现：同 seed → 同序列 + draws 计数）；`tests/spine/sim/test_economy.py`：
```python
def test_balanced_economy_has_no_collapse_and_is_seed_reproducible():
    model = EconomyModel.from_snapshot(balanced_snap)
    a = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    b = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    assert a.distributions == b.distributions                 # 回放可复现
    assert detect_collapse(a) is None
    assert all(inv.ok for inv in a.invariants)

def test_reproduces_one_collapse_with_early_warning():
    # 注入 source>>sink 的崩坏配置(掉落金过高/无消耗) -> 复现崩坏 + 提前预警
    model = EconomyModel.from_snapshot(collapse_snap)
    res = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    rep = detect_collapse(res)
    assert rep is not None                                    # 复现≥1次崩坏
    assert rep.early_warning_tick < rep.collapse_tick         # 提前预警 (M1 验收锚点)
    fs = to_findings(res, collapse_snap.snapshot_id)
    assert any(f.defect_class == "economy_collapse" and f.oracle_type == "simulation" for f in fs)
```
- [ ] **Step 2–4: fail → implement → pass**。
- [ ] **Step 5: Commit** — `git commit -m "feat(spine/sim): 经济仿真 (Monte-Carlo+ABM) + 复现崩坏并提前预警"`

---

## Task 9: spine/patch — 确定性 typed-patch 应用/拒绝引擎（契约6 锚点）

**Files:**
- Create: `gameforge/spine/patch.py`
- Test: `tests/spine/test_patch.py`

**Interfaces:**
- Consumes: `contracts.findings.{Patch, TypedOp}`, `spine.ir.{store.IRGraph, snapshot.Snapshot}`, `contracts.canonical.compute_snapshot_id`。
- Produces:
  - `class PatchRejected(Exception){reason:str, op_id:str|None}`。
  - `apply_patch(snapshot:Snapshot, patch:Patch) -> Snapshot`：对 `patch.ops` 顺序应用 TypedOp 到图的拷贝；
    - **乐观并发（契约6 锚点）**：若 `op.old_value` 非 None 且目标当前值 ≠ `op.old_value` → `raise PatchRejected`（rebase-or-reject，不盲目应用）。
    - `preconditions` 全部成立才应用（否则 `PatchRejected`）。
    - 支持 7 种 `TypedOpKind`（add/delete entity、set_entity_attr、add/delete relation、set_relation_attr、replace_subgraph）。
    - 返回新的不可变 `Snapshot`（内容寻址新 `snapshot_id`）。
  - `dry_run(snapshot, patch) -> GraphDiff`（可审查 diff，§7.9）。

- [ ] **Step 1: Write failing tests** — `tests/spine/test_patch.py`：
```python
def test_set_attr_applies_and_produces_new_snapshot():
    snap2 = apply_patch(snap, Patch(id="p", base_snapshot_id=snap.snapshot_id,
        target_snapshot_id="", side_effect_risk="low", produced_by="agent",
        producer_run_id="r", rationale="fix",
        ops=[TypedOp(op_id="o1", op="set_entity_attr", target="q:1",
                     old_value=120, new_value=80)]))
    assert snap2.to_graph().get_node("q:1").attrs["reward_gold"] == 80  # (target path 见实现)
    assert snap2.snapshot_id != snap.snapshot_id

def test_patch_rejected_on_old_value_mismatch():
    # 当前值是 120，但 patch 声称 old_value=999 -> 拒绝 (契约6 锚点)
    with pytest.raises(PatchRejected):
        apply_patch(snap, patch_with_wrong_old_value)
```
- [ ] **Step 2–4: fail → implement → pass**。
- [ ] **Step 5: Commit** — `git commit -m "feat(spine/patch): 确定性 typed-patch 应用/拒绝 (old_value 乐观并发)"`

---

## Task 10: spine/ingestion — 开源游戏适配器（Flare）+ 无损往返（外部效度）

**Files:**
- Create: `gameforge/spine/ingestion/flare_adapter.py`, `scenarios/flare_sample/`（vendored 真实样本 + `NOTICE`）
- Test: `tests/spine/ingestion/test_flare_adapter.py`, `tests/spine/ingestion/test_flare_roundtrip_property.py`

**Interfaces:**
- Consumes: `spine.ingestion.adapter.Adapter`（M0b Protocol）, `contracts.ir.{Entity, Relation, NodeType, EdgeType, SourceRef}`, `spine.ir.{store.IRGraph, snapshot.Snapshot}`, stdlib。
- Produces:
  - `class FlareTxtAdapter`（实现 M0b 的 `Adapter` Protocol：`format_id="flare"`, `to_ir(workbook, file_ref)->Snapshot`, `from_ir(snapshot)->dict`）。
    - Flare 的 `.txt` 是 INI 式**记录块**（空行分隔的 `key=value`，同 key 可重复表列表）。`read_flare_dir(dir)->dict[str,list[dict]]` 解析成规范化记录（保留原始 key 顺序与重复语义以便无损）。
    - 映射：`items → ITEM`、`enemies → MONSTER`、`loot/drops → DROP_TABLE`（`DROPS_FROM` 边）、`powers → SKILL` 等（覆盖 Flare 真实字段；每行 → Entity，全字段进 `attrs`，`source_ref{adapter:"flare", file, sheet, row}`）。
    - `from_ir` 纯由 `attrs`+pk+`source_ref.row` 重建原始 `.txt`（losslessness by construction，与 M0b AureusCsvAdapter 同法）。
  - vendored 样本：从 flare-engine 上游取一小组真实记录（items/enemies/loot），`scenarios/flare_sample/NOTICE` 记 URL + 许可（Flare 数据 CC-BY-SA / GPL，注明来源）。
- **验收锚点（M1）**：`from_ir(to_ir(x)) == x` 字段级 + snapshot diff=∅（property test）；开源游戏配置可往返 IR。

- [ ] **Step 1: 取样本** — WebFetch flare-engine 仓库中一小段真实 `items/items.txt` + `enemies/*.txt` + `loot`（只取足够 round-trip 的代表性记录），vendored 到 `scenarios/flare_sample/` 并写 `NOTICE`（上游 URL + 许可署名）。若网络不可用，STOP 报告（外部效度是硬验收，不用编造数据替代）。
- [ ] **Step 2: Write failing tests** — `tests/spine/ingestion/test_flare_adapter.py`（typed entities + DROPS_FROM 边 + source_ref）；`tests/spine/ingestion/test_flare_roundtrip_property.py`（hypothesis 生成合法 Flare 记录 → `from_ir(to_ir(x))==x` + `to_ir(x2).diff(to_ir(x)).is_empty()`）+ 一个基于 vendored 真实样本的 round-trip 用例。
- [ ] **Step 3: Run to verify failure** → FAIL。
- [ ] **Step 4: Implement** `flare_adapter.py`（解析 + to_ir/from_ir 无损）。迭代直到 vendored 真实样本 round-trip diff=∅。
- [ ] **Step 5: Run to verify pass** — `uv run pytest tests/spine/ingestion/test_flare_adapter.py tests/spine/ingestion/test_flare_roundtrip_property.py -v` → PASS。
- [ ] **Step 6: Commit** — `git commit -m "feat(spine/ingestion): Flare 开源游戏适配器 (to_ir/from_ir) + 真实样本无损往返"`

---

## Task 11: 验收 — ≥8 缺陷场景 + Review Report + 全量门禁 + 收尾

**Files:**
- Create: `scenarios/defects/`（≥8 缺陷注入场景 + 干净基线）, `scenarios/constraints/*.yaml`（对应约束）, `gameforge/spine/checkers/report.py`
- Modify: `gameforge/apps/cli/run_slice.py`（或新 `run_review.py`）, `CLAUDE.md`, `README.md`, `docs/superpowers/plans/README.md`, memory `gameforge-milestone-progress.md`
- Test: `tests/spine/checkers/test_report.py`, `tests/apps/test_m1_acceptance.py`

**Interfaces:**
- Consumes: 全部 M1 组件（`compile_all`, `GraphChecker/ASPChecker/SMTChecker`, `EconomySimulator`, `ReviewReport.partition`）。
- Produces:
  - `spine/checkers/report.py`：`build_review_report(snapshot, checkers, sim_findings=()) -> ReviewReport`（跑所有 checker + 合并 sim findings → `ReviewReport.partition`）。
  - `apps/cli`：`run_review(scenario_dir, constraints_path, seed) -> ReviewReport`（load config → IR → compile 约束 → 跑 checker 套件 + 经济仿真 → ReviewReport）。
  - `scenarios/defects/`：**≥8 类缺陷**各一个注入场景（复用 outpost/Aureus 配置注入），覆盖：结构类 `dangling_reference / missing_drop_source / unreachable_target / cyclic_dependency / dead_quest / unsatisfiable_completion`（6）+ 数值类 `reward_out_of_range / prob_sum_ne_1 / non_monotonic_curve / gacha_expectation_violation`（4）——合计 10 类 ≥ 8。外加一个**干净基线**场景。

- [ ] **Step 1: Write failing acceptance test** — `tests/apps/test_m1_acceptance.py`（M1 §16 量化验收全锚点）：
```python
def test_each_defect_class_detected_soundly():
    # 每个注入场景 → 恰好检出对应 defect_class 的 Finding(≥1)，oracle_type=deterministic
    for scenario, defect_class in DEFECT_SCENARIOS:   # ≥8
        r = run_review(scenario, CONSTRAINTS, seed=0)
        assert any(f.defect_class == defect_class for f in r.deterministic_findings)

def test_clean_baseline_has_zero_oracle_false_positives():
    # 干净配置 → 确定性检出为空 (oracle-FP = 0，M1 头号 KPI)
    r = run_review("scenarios/defects/clean", CONSTRAINTS, seed=0)
    assert r.deterministic_findings == []

def test_dsl_compiles_to_clingo_and_z3():
    # 约束 DSL 可编译到 Clingo/z3 (M1 验收)
    ...

def test_economy_sim_reproduces_collapse_with_early_warning():
    r = run_review("scenarios/defects/economy_collapse", CONSTRAINTS, seed=1)
    assert any(f.defect_class == "economy_collapse" for f in r.simulation_findings)

def test_open_source_config_roundtrips_ir():
    # Flare 真实样本 round-trip diff=∅ (M1 外部效度验收)
    ...

def test_deterministic_and_llm_findings_strictly_partitioned():
    r = run_review("scenarios/defects/narrative_semantic", CONSTRAINTS, seed=0)
    assert all(f.oracle_type != "llm-assisted" for f in r.deterministic_findings)
    assert r.llm_assisted_findings  # 语义约束的 Finding 只落 llm 桶
```
- [ ] **Step 2: Run to verify failure** → FAIL。
- [ ] **Step 3: Author scenarios + constraints + report builder**，实现 `run_review`；迭代到全部锚点通过、**oracle-FP=0**。
- [ ] **Step 4: 全量验收 run**：
```bash
uv run pytest -v
uv run lint-imports
uv run ruff check .
uv run python -m gameforge.apps.cli review scenarios/defects/<one> scenarios/constraints  # 演示
```
Expected：全绿；6 契约 KEPT；≥8 缺陷类 sound 检出且干净基线 oracle-FP=0；DSL 编译 Clingo/z3；经济仿真复现崩坏 + 提前预警；Flare round-trip diff=∅。
- [ ] **Step 5: 收尾文档** — `CLAUDE.md` 里程碑表 M1→✅（一行验收证据）；`README.md` 加 M1 段（交付 vs 延后 M2）；`plans/README.md`；memory `gameforge-milestone-progress.md`（M1 ✅ + 非显然决策 M1-D1..D7 + 如何测）。
- [ ] **Step 6: Commit** — `git commit -m "feat(m1): ≥8 缺陷类 sound 检出 (oracle-FP=0) + Review Report + 经济仿真崩坏预警 + Flare round-trip；M1 验收通过"`

---

## Self-Review

**1. Spec coverage**（M1 §14 交付 + §16 验收 + 契约3/6 + §13.2 taxonomy）：
- Graph/ASP/SMT 检查器套件 → Tasks 4,5,6 ✔
- 约束 DSL→检查器编译（含 solver 预算/降级 unproven）→ Tasks 2,3,7 + M1-D7 ✔
- 经济仿真引擎（复现崩坏 + 预警）→ Task 8 ✔
- Review Report → Tasks 2(schema),11(builder) ✔
- 开源游戏适配器（外部效度，round-trip）→ Task 10 ✔
- ≥8 类结构/数值缺陷 sound 检出、oracle-FP=0 → Tasks 4,6,11（10 类 ≥ 8）✔
- 约束 DSL 可编译 Clingo/z3 → Tasks 5,6,7 ✔
- Finding/Patch 全字段（契约6，schema 已定）+ patch old_value 拒绝锚点 → Task 9 ✔
- 确定性 vs llm-assisted Finding 严格分区 → Tasks 2(review),7,11 ✔
- 两条独立质量线（差分：ASP vs Graph；属性：图算法 vs 朴素；round-trip property）→ Tasks 4,5,10（§12A.1/R11）✔
- 谓词级 oracle（含 llm-assisted 路由接口，M1 不编译）→ Tasks 2,7（不简化只延后）✔

**2. Placeholder scan**：每个 code step 有真实测试代码 + 精确接口签名；DSL AST/ASP 编码/z3 编码/仿真崩坏这些"新且难"的部分给了具体锚点测试。llm-assisted 谓词的确定性编译显式延后 M2（接口现定）。solver unproven 降级为一等公民，非占位。

**3. Type consistency**：`Constraint`/`Predicate`/`Selector`（Task 2）被 3/6/7 复用；`AssertNode`/`parse_assert`/`select`（Task 3）被 6/7 消费；`Checker` Protocol（现有 base.py）被 4/5/6/7 实现；`Finding`（现有 findings.py）被 4/5/6/8/9/11 生产；`ReviewReport.partition`（Task 2）被 11 调用；`Patch/TypedOp`（现有）被 9 消费；M0b `Adapter` Protocol 被 10 实现；`Snapshot`/`IRGraph`（现有 spine.ir）贯穿。

**Deferred to later milestones（接口现定 — 不简化只延后）**：llm-assisted 谓词的 agent 层求值 + Extraction/Triage/Repair Drafter + Playtest + cassette/model-router（M2）；GameForge-Bench ≥500 seeded 语料 + 完整指标 + Eval 面板（M3，M1 只做 ≥8 缺陷类的检出正确性与外部效度前置）；RBAC/审批工作流 + 前端全页 + 可观测/成本（M4）；patch 的 verifier-guided 修复**搜索**（M2，M1 只做确定性 apply/reject 引擎）。
