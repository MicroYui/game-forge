# M2b-1 — 长程 Playtest Agent 核心 + 回归 harness + planner/executor 消融 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **实现子 agent 用 sonnet5；难任务(主循环/harness)用 opus4.8。**

**Goal:** 在 part2 的确定性 LLM 地基上，从零建 Playtest Agent 的**核心**——状态抽象 + planner/executor 分层 + verifier-grounding + 反思自纠 + 主循环，驱动**已实现的 `AureusEnv`** 在现有 quest 链上闭环，配一个 RECORD/REPLAY 回归 harness（完成率 + 95% CI + 随机基线）与 **planner/executor 消融**——全程 TDD、**零实网 LLM**（stub transport + 真实确定性 env）。

**Architecture:** `agents/playtest/` 新包，`AgentNode` 风格，只经 `ModelRouter`（opus via `AnthropicMessagesTransport`）调 LLM。**完成 = `AureusEnv.done`（`_all_quests_completed`，确定性）**，非 LLM 自评。**verifier-grounding**：agent 对"不可达/死任务"的判断必与 `nav.reachable()`/`reachable_targets` 交叉验证，预言机为准。env seed 化（seed→逐 tick `state_hash` 已验证）+ LLM cassette REPLAY → 全程可复现。测试用 stub transport 返回脚本化动作驱动真实 env 至 `done`。

**Tech Stack:** Python 3.12 (uv), pydantic v2, stdlib `json`/`math`, part2 的 `agents/base`(`call_model`/`parse_json_block`)、`ModelRouter`(RECORD/REPLAY)、`AnthropicMessagesTransport`、`CassetteStore`；`game/aureus/kernel.AureusEnv`、`apps/cli/ir_to_world.snapshot_to_world`、`game/aureus/grid.AureusNav`、`contracts/env_types`(`Action`/`Observation`/`parse_action`)、`contracts/agent_io`(`PlaytestInput`/`PlaytestReport`)、pytest。

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 + 地基契约4 + M2b 设计（`docs/superpowers/specs/2026-07-07-m2b-playtest-design.md`）。每 task 隐含含本节。

- **不简化只延后**：planner/executor/状态抽象/grounding/反思全实现；mem-trace 插槽现定（`memory=None` 默认）实现留 M2b-2；≥20 链生成器 + 真实完成率数字留下一步（D1/D5 待用户确认 + 录制 pass）。
- **verifier-grounding（§7.8）**：LLM 判"不可达/死任务"**必与** `nav.reachable(src,dst)`/`obs.reachable_targets`/spine GraphChecker 交叉验证，**预言机为准**；LLM 单独判定不可信。
- **完成率是确定性 ground truth**：`AureusEnv.step().done == env._all_quests_completed()`（`kernel.py:242`）；**非** LLM 自报。
- **依赖方向（7 契约不破）**：`agents → {contracts, spine, env, game, runtime}`；LLM 仅经 `runtime.model_router`；`spine` 不碰 LLM。`agents/playtest` 不直连 SDK。
- **可复现只承诺回放**：env `reset(seed)`+定序列 → 逐 tick `state_hash` 相等（已证）；LLM 经 Router REPLAY。**CI/测试零实网**（实调仅 `GAMEFORGE_LLM_LIVE=1` 门控录制/测试）。
- **模型**：`ModelSnapshot(provider="anthropic", model="claude-opus-4-8", ...)`（决策 D）；每 agent-node 的 snapshot **可配置**（便于后续分层）。
- Git 无 AI 署名 / "Generated with"；分支 **`m2b-1-playtest-core`**（自 `master` c2d7e74 起）；完成后 review + 并入 master。
- Package `gameforge.<pkg>`。

## 关键接口（勘查确认，file:line — 消费方按此写，零猜测）

- `gameforge/game/aureus/kernel.py`：`AureusEnv(world_config)`（:49）；`reset(scenario:str, seed:int)->Observation`（:78，`scenario` 惰性）；`step(action:Action)->StepResult`（:84，`StepResult(observation,reward=0.0,done,info={})`，`done=_all_quests_completed()` :242）；`observe()->Observation`（:520）；`state_hash()->str`（:581）；`nav_provider()->AureusNav`（:604）。
- `gameforge/apps/cli/ir_to_world.py`：`snapshot_to_world(snapshot:Snapshot)->WorldConfig`（:51）；`world_config.scenario.scenario_id` 传给 `reset`。
- `gameforge/game/aureus/grid.py`：`AureusNav.pos_of(id)->Pos|None`（:66）、`reachable(src:Pos,dst:Pos)->bool`（:69）。
- `gameforge/contracts/env_types.py`：`Action` 判别联合（`observe/navigate_to/interact/choose/attack/cast_skill/use/pickup/equip/buy/sell/wait`）；`parse_action(dict|BaseModel)->Action`（:99）；`Observation`（:110，字段 `tick,player_pos,player_stats,equipped_items,active_effects,active_quests,completed_quests,known_quests,quest_state,inventory,hp,nearby_entities,reachable_targets,available_interactions,visible_map,dialogue_options,last_action_result,logs`）。
- `gameforge/contracts/agent_io.py`：`PlaytestInput(scenario:str, seed:int)`、`PlaytestReport(action_trace:list[dict],defect_findings:list[Finding],completed:bool)`；`AgentRole` 含 `"playtest"`。
- part2：`agents/base.{call_model(router,node_id,user,version,*,system=None,params=None,snapshot=DEFAULT_SNAPSHOT)->(resp,hash), parse_json_block, AgentParseError, DEFAULT_SNAPSHOT}`；`agents/prompts/registry.{register_prompt,get_prompt,render}`；`runtime.model_router.router.{ModelRouter,RouterMode}`；`runtime.cassette.store.CassetteStore`；`runtime.model_router.transport.StubTransport`（按 request_hash 键）。
- 加载现有场景（供测试驱动）：`from gameforge.agents.harness import load_scenario`（part2 建，`load_scenario(dir, constraints)->(Snapshot,checkers)`）——但 Playtest 只需 snapshot → `snapshot_to_world` → `AureusEnv`。现成可完成链：`scenarios/outpost`（1 quest 4 步 talk/collect/fight/turn_in）、`scenarios/caravan.yaml`（1 quest 3 步）。

---

## Repo layout delta

```
gameforge/agents/playtest/
  __init__.py
  state.py        # CREATE: abstract_state(obs)->str (确定性状态抽象)
  prompts.py      # CREATE: register playtest.planner/executor/reflect prompts (prompt_version)
  planner.py      # CREATE: Planner.plan(state,router)->Subgoal
  executor.py     # CREATE: Executor.act(subgoal,state,router)->Action
  grounding.py    # CREATE: ground_belief(...) verifier-grounding (nav/reachable 交叉验证)
  reflect.py      # CREATE: reflect(trace,router)->str
  agent.py        # CREATE: PlaytestAgent.run(input,env,router,*,use_planner,memory=None)->PlaytestReport
gameforge/agents/
  playtest_harness.py  # CREATE: run_playtest_corpus + random_baseline + CI + record/replay 入口
tests/agents/playtest/  # 各 task 测试 (unique basenames, 无 __init__.py)
```

---

## Task 1: state — 确定性状态抽象

**Files:** Create `gameforge/agents/playtest/__init__.py`, `gameforge/agents/playtest/state.py`; Test `tests/agents/playtest/test_state.py`.

**Interfaces:**
- Consumes: `contracts.env_types.Observation`.
- Produces: `abstract_state(obs:Observation) -> str`——纯函数，把 `Observation` 压成紧凑多行文本：tick；active/known/completed quests + 各 `quest_state`（status/current_step/step_kind）；`reachable_targets`；`available_interactions`；`inventory`；hp；`nearby_entities`；`last_action_result`；`logs` 尾 5 条。确定性、可复现。

- [ ] **Step 1: failing test** `tests/agents/playtest/test_state.py`:
```python
from gameforge.agents.playtest.state import abstract_state
from gameforge.contracts.env_types import Observation


def test_abstract_state_is_compact_deterministic_and_covers_progress():
    obs = Observation(
        tick=3, player_pos=(1, 2), active_quests=["q1"], known_quests=["q1"],
        completed_quests=[], quest_state={"q1": {"status": "active", "step_kind": "collect", "step_id": "s2"}},
        reachable_targets=["npc:qi", "src:herb"], available_interactions=["npc:qi"],
        inventory={"item:herb": 1}, hp=30, nearby_entities=["npc:qi"],
        last_action_result="arrived", logs=["a", "b", "c", "d", "e", "f"],
    )
    s = abstract_state(obs)
    assert abstract_state(obs) == s                 # deterministic
    assert "q1" in s and "collect" in s and "npc:qi" in s and "src:herb" in s
    assert "tick=3" in s
    assert "f" in s and "a" not in s.split("logs")[-1]  # only last 5 logs (a dropped)
```
- [ ] **Step 2: run→FAIL.** `uv run pytest tests/agents/playtest/test_state.py -v`.
- [ ] **Step 3: implement** `state.py`:
```python
"""Deterministic state abstraction: Observation -> compact reasoning text.

Pure function (no LLM, no RNG) so a playtest run stays byte-reproducible under
cassette REPLAY. Compresses the decision-relevant slice of the Observation.
"""
from __future__ import annotations

from gameforge.contracts.env_types import Observation


def abstract_state(obs: Observation) -> str:
    lines = [f"tick={obs.tick} pos={obs.player_pos} hp={obs.hp}"]
    lines.append(f"active_quests={obs.active_quests} known={obs.known_quests} done={obs.completed_quests}")
    for qid, st in sorted(obs.quest_state.items()):
        lines.append(f"quest {qid}: status={st.get('status')} step_kind={st.get('step_kind')} step_id={st.get('step_id')}")
    lines.append(f"reachable_targets={obs.reachable_targets}")
    lines.append(f"available_interactions={obs.available_interactions}")
    lines.append(f"inventory={dict(sorted(obs.inventory.items()))}")
    lines.append(f"nearby={obs.nearby_entities}")
    lines.append(f"last_action_result={obs.last_action_result}")
    lines.append(f"logs(last5)={obs.logs[-5:]}")
    return "\n".join(lines)
```
`__init__.py`: `"""GameForge agents.playtest — 长程 Playtest Agent (M2b)。"""`
- [ ] **Step 4: run→PASS. Step 5: commit** `feat(agents/playtest): 确定性状态抽象 abstract_state`

---

## Task 2: prompts — planner/executor/reflect prompt 注册

**Files:** Create `gameforge/agents/playtest/prompts.py`; Test `tests/agents/playtest/test_prompts.py`.

**Interfaces:** Produces `register_playtest_prompts()`（幂等，import 时调用一次）；名/版：`playtest.planner`=`playtest@1`、`playtest.executor`=`playtest@1`、`playtest.reflect`=`playtest@1`。每个 prompt 要求**只输出 JSON**、给 schema、声明"你只提议动作，完成与否由确定性游戏引擎判定；对不可达/死任务的判断会被确定性可达性检查复核"。brace-safe（用 `get_prompt` 取，不经 `render` 除非 reflect 需 `{...}` 占位——本 task 无占位，纯 `get_prompt`）。

- [ ] **Step 1: failing test**:
```python
from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt


def test_playtest_prompts_registered():
    register_playtest_prompts()
    for name in ("playtest.planner", "playtest.executor", "playtest.reflect"):
        v, tmpl = get_prompt(name)
        assert v == "playtest@1" and "JSON" in tmpl
```
- [ ] **Step 2: FAIL → 3: implement** `prompts.py`（写实英文 prompt，非占位；planner: 输出 `{"quest","step_kind","need_item?","target?"}` 子目标；executor: 输出一个 atomic action JSON `{"kind",...}`，优先 `reachable_targets` 内、与子目标相关的目标；reflect: 输出一条修正提示字符串的 JSON `{"hint": "..."}`）；末尾调用一次 `register_playtest_prompts()`。→ **4: PASS → 5: commit** `feat(agents/playtest): planner/executor/reflect prompt 注册 (playtest@1)`

---

## Task 3: planner — 高层子目标

**Files:** Create `gameforge/agents/playtest/planner.py`; Test `tests/agents/playtest/test_planner.py`.

**Interfaces:**
- Consumes: `agents.base.{call_model,parse_json_block,AgentParseError}`, `agents.prompts.registry.get_prompt`, `runtime.model_router.router.ModelRouter`, `contracts.model_router.ModelSnapshot`.
- Produces:
  - `Subgoal = dict[str, Any]`（键：`quest,step_kind,need_item?,target?`）。
  - `class Planner(node_id="playtest.planner", snapshot=DEFAULT_SNAPSHOT)`：`plan(state:str, router, *, extra:str|None=None) -> tuple[Subgoal, str]`——render user=state(+extra 反思提示)，`call_model`，`parse_json_block`；返回 `(subgoal, request_hash)`。解析失败→兜底 `Subgoal={"quest":None,"step_kind":"advance"}` + `fallback` 标记（返回 dict 里带 `"_fallback":True`）。

- [ ] **Step 1: failing test**（用 `_FixedTransport` 返回一段 planner JSON；断言解析出 subgoal + request_hash；非 JSON→兜底 `_fallback`）。参照 part2 `tests/agents/test_extraction.py` 的 `_FixedTransport`+`RouterMode.PASSTHROUGH` 范式。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/playtest): Planner (高层子目标, 兜底 advance)`

---

## Task 4: executor — 原子动作 + 动作优先级

**Files:** Create `gameforge/agents/playtest/executor.py`; Test `tests/agents/playtest/test_executor.py`.

**Interfaces:**
- Consumes: 同 Task 3 + `contracts.env_types.{Action,parse_action}`。
- Produces: `class Executor(node_id="playtest.executor", snapshot=DEFAULT_SNAPSHOT)`：`act(subgoal:Subgoal, state:str, router) -> tuple[Action, str]`——prompt 给子目标+抽象状态，要求输出 atomic action JSON，**优先 `reachable_targets` 内、与子目标相关的目标**；`parse_json_block`→`parse_action`→`Action`；返回 `(action, request_hash)`。解析/校验失败→兜底 `parse_action({"kind":"observe"})`。

- [ ] **Step 1: failing test**（`_FixedTransport` 返回 `{"kind":"navigate_to","target":"npc:qi"}` → `act` 返回 `NavigateTo(target="npc:qi")`；非法 JSON→兜底 `Observe`）。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/playtest): Executor (原子动作 + 动作优先级, 兜底 observe)`

---

## Task 5: grounding — verifier-grounding（可达性交叉验证）

**Files:** Create `gameforge/agents/playtest/grounding.py`; Test `tests/agents/playtest/test_grounding.py`.

**Interfaces:**
- Consumes: `contracts.env_types.Observation`, `game.aureus.grid.AureusNav`, `contracts.findings.Finding`.
- Produces:
  - `@dataclass GroundedVerdict{target:str, llm_says_reachable:bool, oracle_says_reachable:bool, agree:bool, action:Literal["continue","abort_quest"]}`。
  - `ground_target(target:str, obs:Observation, nav:AureusNav) -> GroundedVerdict`：oracle = `target in obs.reachable_targets` **或** `nav.reachable(nav.pos_of("__player_start__") or obs.player_pos, nav.pos_of(target))`（`pos_of` None→不可达）。`action="abort_quest"` 仅当 oracle 判**不可达**（LLM 无论怎么想，可达性以 oracle 为准）；否则 `continue`。
  - `make_unreachable_finding(target, snapshot_id) -> Finding`（`source="playtest"`, `oracle_type="deterministic"`, `defect_class="unreachable_target"`, `status="confirmed"`）——当 oracle 判死。

- [ ] **Step 1: failing test**：构造一个 `AureusNav`（用 `Grid`+positions）——一个可达 target、一个不可达 target（无 pos 或无路径）；断言 `ground_target(reachable,...).action=="continue"`、`ground_target(unreachable,...).action=="abort_quest"`，且**不看 LLM 输入**（纯确定性）。读 `gameforge/game/aureus/grid.py` 看 `Grid`/`AureusNav` 构造。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/playtest): verifier-grounding (可达性 oracle 覆盖 LLM 判定, 产 unreachable_target Finding)`

---

## Task 6: agent — 主循环 + planner/executor 消融

**Files:** Create `gameforge/agents/playtest/reflect.py`, `gameforge/agents/playtest/agent.py`; Test `tests/agents/playtest/test_agent_loop.py`.

**Interfaces:**
- Consumes: Task 1–5 + `contracts.agent_io.{PlaytestInput,PlaytestReport}`, `game.aureus.kernel.AureusEnv`, `apps.cli.ir_to_world.snapshot_to_world`.
- Produces:
  - `reflect.py`：`reflect(trace:list[dict], router) -> tuple[str,str]`（LLM 产修正 hint + request_hash；失败→`("", hash?)`）。
  - `agent.py`：`class PlaytestAgent`：`run(input:PlaytestInput, env:AureusEnv, router, *, use_planner:bool=True, memory=None, max_steps:int=200) -> PlaytestReport`。循环：`obs=env.observe()`→`state=abstract_state(obs)`→ if `use_planner`: `subgoal=Planner().plan(state,router)` else `subgoal={"step_kind":"advance"}`（flat）→`action=Executor().act(subgoal,state,router)`→`res=env.step(action)`→记 `action_trace.append({...})`→ **verifier-grounding**（agent 判某 target 不可达时 `ground_target`；oracle 判死→产 `defect_finding`+跳过该 quest）→ 连续 6 步 `quest_state` 未变→`reflect`→注入下轮 planner `extra`。到 `res.done` 或 `max_steps` 停。产 `PlaytestReport(action_trace, defect_findings, completed=res.done)`。`memory` 插槽：`memory is not None` 时 `memory.record(step)` + `Planner.plan(..., extra=memory.recall(...))`（M2b-2 实现 MemTrace；本 task 只留插槽，`memory=None` 走通）。

- [ ] **Step 1: failing test** `test_agent_loop.py`：加载 `scenarios/outpost` snapshot→`snapshot_to_world`→`AureusEnv`；用一个 **`_ScriptedTransport`**（按 executor 的 request_hash 或按调用序返回完成 outpost 所需的 atomic action JSON 序列——navigate_to giver→interact→navigate_to source→interact→（fight：navigate+attack×N）→navigate_to giver→interact）驱动 `PlaytestAgent(use_planner=True).run(...)` → 断言 `report.completed is True`（env.done）。第二个用例 `use_planner=False`（flat）同样脚本化→completed（证明两模式都跑通、消融机制在）。**关键：完成判定来自 `env.done`，非 stub**。（实现者：先跑 `ScriptedDriver` 打印 outpost 的原子动作序列作为 stub 脚本来源。）
- [ ] **Step 2–4: FAIL→implement→PASS**（迭代 stub 脚本直到 outpost 闭环）。**5: commit** `feat(agents/playtest): 主循环 (planner/executor + grounding + 反思) + flat 消融, 驱动 AureusEnv 至 done`

---

## Task 7: harness — 回归语料 + 完成率(CI) + 随机基线 + 消融 + record/replay

**Files:** Create `gameforge/agents/playtest_harness.py`; Test `tests/agents/test_playtest_harness.py`.

**Interfaces:**
- Consumes: Task 6 + `runtime.model_router.{router,transport}`, `runtime.cassette.store.CassetteStore`, `game.aureus.kernel.AureusEnv`, `apps.cli.ir_to_world.snapshot_to_world`.
- Produces:
  - `@dataclass PlaytestCorpusResult{n_chains,completed,completion_rate,per_chain:list[dict],by_length:dict,mean_steps:float}`；`wilson_ci(k,n)->tuple[float,float]`（95% Wilson 区间，纯 stdlib `math`）。
  - `run_playtest_corpus(chain_snapshots:list[Snapshot], router, *, use_planner=True, memory_factory=None, seed=0, max_steps=200) -> PlaytestCorpusResult`——每链 `snapshot_to_world→AureusEnv→PlaytestAgent.run`，收 `completed`(env.done)/steps；`completion_rate=completed/n`；`by_length` 分桶（短/中/长按 step 数）各带 `wilson_ci`。
  - `random_baseline(chain_snapshots, seed) -> PlaytestCorpusResult`——无 LLM 的随机合法动作 agent（从 `available_interactions`/`reachable_targets` 随机 navigate/interact，seed 化），作对照分母。
  - `replay_router(cassettes_root="cassettes/playtest")` / `record_router(...)`（同 part2：REPLAY 用哑 transport 防实调；RECORD 门控 `GAMEFORGE_LLM_LIVE=1`+key，`AnthropicMessagesTransport`）。
  - `__main__` `--record`/`--replay`。

- [ ] **Step 1: failing test**：用 `_ScriptedTransport` + `RouterMode.PASSTHROUGH`（或 REPLAY+手写 cassette）跑一个 2-链小语料→断言 `PlaytestCorpusResult.completion_rate` 计算正确、`wilson_ci` 单调合理、`random_baseline` 可跑且完成率 ≤ scripted。`wilson_ci(0,10)`/`(10,10)`/`(5,10)` 数值单测。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents): Playtest 回归 harness (完成率 + Wilson CI + 随机基线 + 消融 + record/replay 入口)`

---

## Task 8: 收尾 — 主循环冒烟 + 文档（不含 ≥20 链录制，待 D1/D5）

**Files:** Test `tests/agents/playtest/test_playtest_smoke.py`; Modify `README.md`, `docs/superpowers/plans/README.md`。

**Interfaces:** Consumes 全部 Task 1–7。
- Produces：一个 REPLAY/scripted 冒烟——`run_playtest_corpus([outpost_snapshot], scripted_router)` → `completion_rate==1.0`（outpost 可闭环）+ `random_baseline([outpost])` 完成率 < 1.0（证明 agent 非平凡）+ planner on/off 都产 `PlaytestReport`（消融机制在）。

- [ ] **Step 1: failing test** `test_playtest_smoke.py`（如上）。
- [ ] **Step 2–4: FAIL→implement→PASS.**
- [ ] **Step 3b: 全量门禁**：`uv run pytest -q`（零实网全绿）、`uv run lint-imports`（7 契约 KEPT）、`uv run ruff check .`。
- [ ] **Step 4: 文档** `README.md` 加 M2b-1 段（Playtest 核心 + harness + 消融机制**交付** vs **待 D1/D5**：≥20 链生成器、真实完成率录制、记忆消融 = M2b-1b/M2b-2）。
- [ ] **Step 5: commit** `feat(m2b-1): Playtest agent 核心 + 回归 harness + planner/executor 消融机制 (驱动 AureusEnv, REPLAY 零实网; ≥20 链生成器与录制待 D1/D5)`

---

## Self-Review

**1. Spec coverage**（M2b 设计 §4/§5/§7/§9 的 M2b-1 份额）：
- 状态抽象 → Task 1 ✔；prompts → Task 2 ✔；planner → Task 3 ✔；executor(动作优先级) → Task 4 ✔；verifier-grounding → Task 5 ✔；主循环 + planner/executor 消融 → Task 6 ✔；回归 harness(完成率+CI+基线)+消融 → Task 7 ✔;冒烟+文档 → Task 8 ✔。
- **本 plan 明确延后（待 D1/D5 确认 + 录制 pass）**：≥20 链**生成器**（§3，D1 待确认；`game/aureus/scenario_gen.py`）；真实完成率**录制 pass**（D5，最大 token 开销）；**记忆消融** + mem-trace（M2b-2）；叙事对抗 quorum 进阶（M2b-2）。接口现定（`memory` 插槽、harness `--record`），实现分批——不简化只延后。

**2. Placeholder scan**：确定性部分（state/grounding/CI）给全代码；LLM 部分（planner/executor/reflect/主循环）给精确接口 + `_FixedTransport`/`_ScriptedTransport` 测试范式 + 兜底具体；主循环测试明说"实现者先用 ScriptedDriver 打印 outpost 原子序列作 stub 脚本"——具体机制非"add loop"。

**3. Type consistency**：`abstract_state`(T1) 被 T3/T4/T6 消费；`Subgoal`(T3) 被 T4/T6；`Action`/`parse_action` 贯穿 T4/T6；`GroundedVerdict`/`ground_target`(T5) 被 T6；`PlaytestReport`(agent_io) 被 T6/T7 产；`PlaytestCorpusResult`/`wilson_ci`(T7) 被 T8；`ModelRouter`/`StubTransport`/`AureusEnv`/`snapshot_to_world` 贯穿。`memory` 插槽签名（`record`/`recall`）与 M2a 设计 §8.4 `MemTrace` Protocol 一致（M2b-2 实现）。
