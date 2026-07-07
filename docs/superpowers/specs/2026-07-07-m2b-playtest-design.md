# M2b — 长程 Playtest Agent + 回归框架 + 消融 设计文档（Design Spec）

> 状态：**DRAFT，等待用户确认 §0 的 5 项决策 + 过审本设计后进 writing-plans**。用户离开期间按"推荐默认值"起草；受决策影响的段落已隔离标注 `[D1]..[D5]`，用户答复只需改这些段落。本文是**设计**，非实现——未经用户确认决策 + 过审，不进 writing-plans、不写代码、不调 LLM 网关。

关联单一真相源：PRD `…2026-07-03-gameforge-prd.md`（§7.7 Agent-Env / §7.8 Playtest+回归 / §13.4 agent 硬核指标 / §16 M2 验收）、M2 设计 `…2026-07-07-m2-agent-layer-design.md` §8（含 §8.4 mem-trace 从零结论）、地基契约 契约4（Agent-Env）。CLAUDE.md 硬规则。记忆 `gameforge-milestone-progress`（M2a 全部 ✅ 已并入 master `9e8c621`）。

---

## 0. 待确认的 5 项决策（IMPLEMENTATION GATE）

每项给出**推荐默认**（本草案据此展开）。请确认/改写；未确认前不开工。

| # | 决策 | 推荐默认（本草案采用） | 若改选的影响面 |
|---|---|---|---|
| **D1** | ≥20 链 + 60+ 动作长链内容从哪来（今仅 ~11 条几乎重复的 4 步链） | **确定性场景生成器**（`game/aureus/scenario_gen.py`，seed 化）：把 Aureus 真实系统（talk/collect/fight/turn_in + 多 quest + 导航 + 商店/抽卡）组合成 ≥20 条**真正不同**的链（不同 NPC/道具/怪/区域/步序），含若干 6–12 quest 的 60+ 动作长链。可复现、非重复、不简化 | 选"手写 20+"→ §3 换成人工 authoring 清单；选"Content Generator 生成"→ §3 复用 part2 generator（但需过门禁且未必造出长链） |
| **D2** | "≥20 链可靠闭环"的验收标准（PRD 未定死百分比） | **诚实报告 + 消融正向 + 胜过基线**：分链长完成率 + 95% CI；"通过"= harness 跑完 ≥20 链、诚实出数、带-agent 完成率**显著高于**无-agent/随机基线、且记忆消融与 planner 消融均为**正 delta**。契合 §7.8"不用玩具高分掩饰真难度" | 选"固定百分比阈"→ §9 换成硬阈（如短链 ≥70%、长链诚实更低）；风险：固定高阈易诱导 gaming |
| **D3** | mem-trace 的 Recall 相关性项用真实 embedding 还是纯确定性 | **纯确定性 recall 为主**（state-hash 精确键 + recency×verifier-verdict×结构相似，无 embedding）；embedding（网关 `text-embedding-3-small`，经 Router+cassette）作**可选**相关性项、默认关。契合 §8.4"state-hash 键是头号确定性决定" | 选"用 embedding"→ §6 recall 增一个经 cassette 录制的 embedding 相似项（+录制成本） |
| **D4** | M2b 拆分 | **拆 M2b-1 / M2b-2**：M2b-1 = 场景内容(≥20 链) + Playtest agent 核心(planner/executor + 状态抽象 + verifier-grounding，记忆 OFF) + 回归 harness + 录制 → 完成率基线 + **planner/executor 消融**；M2b-2 = mem-trace(Recall/Compaction/skill/reflection) + **记忆消融** + 叙事对抗 quorum 进阶。各自 spec→plan→impl，里程碑碑间停下过审 | 选"不拆"→ 合成一份大 plan，端到端做完 |
| **D5** | Playtest 录制 pass 的模型（本项目最大 token 开销：≥20链×60+动作×消融维度） | **opus4.8 全程**（决策 D，质量优先、不设硬预算）；但把每 agent-node 的 `ModelSnapshot` 设为**可配置**，若录制体量过大再切分层不需返工 | 选"分层"→ executor 大批量用 `claude-sonnet-5`(现有 OpenAI transport)、planner/反思用 opus4.8；§4/§5 的 node→snapshot 映射按分层填 |

**共同不变量（无论选择，均成立）**：
- 依赖方向：`agents → {contracts, spine, env, game, runtime}`；LLM 仅经 `runtime.model_router`；`spine` 永不碰 LLM（7 契约不破）。
- **verifier-grounding**：Playtest 对"不可达/死任务"的判断**必与确定性可达性交叉验证**（`nav.reachable()` / `reachable_targets` / spine GraphChecker），LLM 单独判定不可信（PRD §7.8）。
- **完成率是确定性 ground truth**：`AureusEnv` 的 `done=_all_quests_completed()`（`kernel.py:242`），**非** LLM 自评。
- **可复现只承诺回放**：env seed 化（seed→逐 tick `state_hash`，已验证）+ LLM 经 Router cassette REPLAY；CI/测试**零实网**（实调仅 `GAMEFORGE_LLM_LIVE=1` 门控录制）。
- **不简化只延后**：≥20 链是**真正不同**的链（不是 4 步 outpost 复制 20 遍）；mem-trace/planner 全实现；Playtest I/O（`PlaytestInput/Report`）part2 已定。
- Git 无 AI 署名；分支 `m2b-*`；主干 `master`。

---

## 1. M2b 分解与 §16 验收映射 `[D4]`

M2 §16 剩余验收（M2a 已交 Fix Pass Rate 90%/复现/修复搜索效率）：**Playtest ≥20 链可靠闭环（完成率+CI）** + **记忆消融** + **planner/executor 消融**。

### M2b-1 — 场景内容 + Playtest 核心 + 基线
产出：确定性场景生成器(≥20 链，含长链) + Playtest Agent 核心（planner/executor 分层 + 状态抽象 + 动作优先级 + verifier-grounding + 反思，**记忆 OFF**）+ 回归 harness（完成率+CI）+ 录制 pass。
锁定验收：**≥20 链完成率基线（带 CI，胜过无-agent 基线）** + **planner/executor 消融**（flat vs planner 的完成率 delta）。

### M2b-2 — mem-trace + 记忆消融 + 叙事对抗进阶
产出：`MemTrace`（Recall/Compaction/skill 层/reflection，从零）插入 Playtest + **记忆消融**（recall→∅ 的完成率 delta）+ Consistency 对抗性 perspective-diverse 辩论（part2 只基础 quorum）。
锁定验收：**记忆消融报告**（TITAN 式有/无记忆材料级差）+ 完成率随记忆提升。

### §16 M2b 验收 → 交付映射
| §16 锚点 | 归属 | 交付物 |
|---|---|---|
| Playtest ≥20 链可靠闭环（完成率+CI，不掩饰） | M2b-1 | 场景生成器 + Playtest 核心 + harness |
| planner/executor 消融 | M2b-1 | flat-agent 对照 + 完成率 delta 报告 |
| 记忆消融 | M2b-2 | mem-trace on/off + 完成率 delta 报告 |
| （§7.5 硬核面4）叙事对抗验证一致性 | M2b-2 | Consistency 进阶 quorum/辩论 |

---

## 2. 现状盘点与关键约束（来自 M2b 接口勘查，file:line）

- **Agent-Env 已实现，M2b 只驱动**：`AureusEnv(world_config)`（`kernel.py:49`），`reset(scenario,seed)`（`kernel.py:78`，`scenario` 参数**惰性**——世界来自构造时的 `WorldConfig`），`step(action)→StepResult(obs,reward=0.0,done,info={})`（`kernel.py:115`），`state_hash()`（`kernel.py:581`），`nav_provider()→AureusNav`（`kernel.py:604`）。加载路径：`load_scenario → snapshot_to_world(snapshot)（ir_to_world.py:51） → AureusEnv → reset(world.scenario.scenario_id, seed)`。
- **无 reward 信号**（恒 0.0）→ verifier-grounding 只能取 `Observation`（`quest_state`/`completed_quests`/`logs`/`reachable_targets`，`env_types.py:110-128`）+ `nav.reachable(src,dst)`（`grid.py:69`）。
- **`ScriptedDriver`（`driver.py:23`）作弊**（用全 `WorldConfig` ground truth 规划，非部分可观测）→ M2b 的 planner/executor **从零写**，不复用其作弊逻辑（但其 macro→atomic 的 kind 分派模式可参考）。
- **12 原子动作**（`env_types.py:22-96`）；`Choose`/对话分支**未实现**（`dialogue_options` 恒 `[]`）；`QuestStepKind` 仅 `talk/collect/turn_in/fight`（`world.py:17`，无 escort/deliver）→ 长链靠**多 quest + 更多导航/战斗**，不靠对话树。
- **内容缺口（M2b 头号前置）**：现 `scenarios/` 仅 ~11 链、每 3–4 步、10 条是同一 4 步 outpost 的近重复（仅 `cyclic_dependency` 有 2 quest，且是缺陷 fixture）。远不够 ≥20 链/60+ 动作 → **§3 场景生成器**。
- **可复现已证**：`reset(seed)`+定动作序列 → 逐 tick `state_hash` 相等（`tests/game/aureus/test_kernel.py`，含战斗 RNG）。
- **可复用 infra**（part2 已建）：`agents/base.{call_model,parse_json_block}`、`agents/orchestrator.{AgentNode,run_graph}`、`runtime.model_router.router.ModelRouter(RECORD/REPLAY)`、`AnthropicMessagesTransport`、`agents/harness.py`（record/replay CLI 范式）、`contracts.agent_io.{PlaytestInput,PlaytestReport}`。

---

## 3. 场景内容：确定性场景生成器 `[D1]` — M2b-1

**`game/aureus/scenario_gen.py`**：`generate_chains(seed:int, n:int, length_mix:dict) -> list[Snapshot]`——用 spine-local seeded RNG（不碰 game.rng；同 M1 sim/rng 思路）确定性地组合 Aureus 真实系统成**真正不同**的 quest 链 IR：
- **每条链**：k 个 quest（k 按 length_mix：短 1–2、中 3–5、长 6–12），每 quest 3–5 步（talk→collect[→fight]→turn_in），`PRECEDES` 串成序，`HAS_STEP` 挂步，giver `STARTS_AT` NPC，collect 步的 item 有 `GRANTS/DROPS_FROM` 源，fight 步有 encounter+monster，区域网格布点使导航非平凡（≥若干 tick/步）。**多样性种子**：NPC/item/monster/region 名与坐标、步序、combat 难度、collect 数量都随 `seed+index` 变化 → 20 条链无一重复。
- **长链 = 60+ 原子动作**：一条 8-quest×(导航~5tick + interact + collect + 一场 fight ~10 攻击) 链天然 60–120 原子动作，命中 §7.8 长程门槛。
- **输出 IR `Snapshot`**（entities/relations）→ 走同一 `snapshot_to_world` → `AureusEnv`，且**可过 spine 检查器**（生成的链本身应无缺陷；另可故意注入缺陷链供 Playtest 的缺陷检出验证）。
- **落盘**：`scenarios/playtest/<seed>/chain_<i>/`（CSV，可 diff、可提交），生成器可复跑得同内容（seed 化）。
- 验收锚点：`generate_chains(seed=0, n=20)` → 20 个 snapshot，两两 `IRGraph.diff` 非空（真不同），且 ≥3 条经 `AureusEnv` 由 `ScriptedDriver`（全信息参照）可 100% 跑通（证明链**可完成**——Playtest 的完成率分母是"本可完成"的链）。

> 说明：`ScriptedDriver` 作为**全信息参照**证明每条生成链"可完成"（分母合法性）；Playtest Agent 则在**部分可观测**下尝试完成——两者差就是 agent 的真实难度。

---

## 4. Playtest Agent 核心（planner/executor + 状态抽象 + verifier-grounding）`[D5]` — M2b-1

`agents/playtest/`（新包，`AgentNode` 风格，经 `ModelRouter` 调 LLM）：

### 4.1 状态抽象 `agents/playtest/state.py`
`abstract_state(obs:Observation) -> str`：把 `Observation` 压成紧凑可推理文本——当前/已知/已完成 quest 及各 `quest_state`（status/current_step/step_kind）、`reachable_targets`、`available_interactions`、`inventory`、`hp`、`nearby_entities`、`last_action_result`、近期 `logs` 尾部。**确定性**（纯函数），压缩决策空间。

### 4.2 Planner `agents/playtest/planner.py`
`class Planner`（`node_id="playtest.planner"`）：`plan(state:str, router) -> Subgoal`——LLM 从抽象状态选**下一个高层子目标**（如 `{"goal":"complete","quest":"q3","step":"collect","need_item":"item:herb"}`）。prompt 给 quest 图 + 当前进度 + 可达目标；输出 JSON 子目标；解析失败/无进展→兜底选"推进第一个未完成 quest 的当前步"。

### 4.3 Executor `agents/playtest/executor.py`
`class Executor`（`node_id="playtest.executor"`）：`act(subgoal, state, router) -> Action`——LLM 从子目标 + 抽象状态产**下一个原子动作**（`navigate_to/interact/attack/...`，经 `parse_action`）。**动作优先级**：prompt 明确"优先与当前子目标相关、且在 `reachable_targets` 里的目标"。fight 子目标下循环 `attack`。解析失败→兜底 `observe`。

### 4.4 verifier-grounding `agents/playtest/grounding.py`
`ground_belief(belief, obs, nav) -> GroundedVerdict`：当 agent（planner/反思）判"目标不可达/quest 死"时，**交叉验证** `nav.reachable(player_pos, target_pos)` / `target in obs.reachable_targets` / spine `GraphChecker` 的 `dead_quest`/`unreachable_target`。确定性预言机与 LLM 判定不一致时**以预言机为准**（LLM 判死但可达→继续尝试；LLM 判可达但预言机判死→产 `defect_finding` 并停止该 quest）。这是 §7.8 的外部 ground-truth，使自纠**收敛而非漂移**。

### 4.5 反思自纠 `agents/playtest/reflect.py`
连续 K 步（默认 6）`last_action_result` 无进展（quest_state 未变）→ `reflect(trace, router)` 基于失败轨迹产一条修正提示，注入下一轮 planner。

### 4.6 主循环 `agents/playtest/agent.py`
`class PlaytestAgent`：`run(input:PlaytestInput, env, router, *, use_planner=True, memory=None) -> PlaytestReport`——loop 到 `done` 或超 `max_steps`（默认 200）：抽象状态 →(planner→子目标)→ executor→原子动作 → `env.step` → 记 `action_trace` →（verifier-grounding 检查）→（无进展→反思）。`use_planner=False` = **flat 消融**（单 LLM 环：抽象状态→直接下个原子动作，无子目标分层）。`memory` 插槽 = M2b-2 的 `MemTrace`（默认 None）。产 `PlaytestReport(action_trace, defect_findings, completed=env.done)`。**完成 = env.done（确定性）**。

---

## 5. 回归 harness + 完成率报告(CI) + 录制 pass — M2b-1

`agents/playtest_harness.py`（仿 part2 `harness.py` 的 RECORD/REPLAY）：
- `run_playtest_corpus(chain_snapshots, router, *, use_planner=True, memory_factory=None, seed=0) -> PlaytestCorpusResult`——对每条链：`snapshot_to_world→AureusEnv→reset(seed)`，`PlaytestAgent.run`，收 `completed`(env.done)/`action_count`/`defect_findings`。
- `@dataclass PlaytestCorpusResult{n_chains, completed, completion_rate, per_chain:[{chain, length_bucket, completed, steps}], by_length:{bucket:{rate, ci_low, ci_high}}, ...}`——分链长完成率 + **95% CI（Wilson）**。
- **基线对照**：`random_baseline`（随机合法动作 agent，无 LLM）跑同语料 → 完成率对照，证明 agent 显著更高。
- `__main__` `--record`/`--replay`（同 part2：`GAMEFORGE_LLM_LIVE=1`+key 门控 RECORD，CI 只 REPLAY）。

---

## 6. mem-trace（Recall/Compaction/skill/reflection，从零）`[D3]` — M2b-2

`agents/playtest/memory.py`——`MemTrace` Protocol（M2a 设计 §8.4 已定）：`record(step)` / `recall(state, task, k) -> items` / `compact(trace, verdicts) -> compacted` / `reflect(failed_trace, verdict) -> note`。实现（借设计不借依赖）：
- **结构**（TITAN+Voyager）：episodic `(状态抽象哈希, 动作, 结果)` 轨迹 + 持久**状态-动作转移图**（键=Aureus `state_hash`，精确匹配、零模型调用）+ 技能层（复现验证过的可复用子轨迹，如"从 X 导航到 giver Y"）。
- **Recall**（Generative-Agents，为 verifier 改权重）：`recency × 结构相似 × verifier-verdict`（确定性算术）；`[D3]` embedding 相关项默认关。TITAN 式"多次无进展则降权"抑已验证死路。
- **Compaction**（HiAgent+AWM+MemGPT，在 quest-step 完成边界触发，经 Router 可回放）。
- **Reflection**：验证器判负 → 判决条件化反思写 episodic。
- **消融接口**：`recall→∅` 一开关（§7 消融）。

---

## 7. 消融 — M2b-1(planner/executor) / M2b-2(memory)

- **planner/executor 消融**（M2b-1）：`use_planner=True`（分层）vs `False`（flat 单环）跑同语料 → 完成率 delta + CI。证明分层的贡献。
- **记忆消融**（M2b-2）：`memory=MemTrace()` vs `memory=None`（或 recall→∅）跑同语料 → 完成率 delta + CI（TITAN 式材料级差）。
- 报告：`agents/playtest_harness.py` 出 `AblationReport{variant, completion_rate, ci, delta_vs_base}`，全 REPLAY 可复现。

---

## 8. 叙事对抗 quorum 进阶（Consistency）— M2b-2
part2 的基础 3-采样 quorum → **perspective-diverse 辩论**：多个"视角"（时序/身份/剧透）各判，分歧时再一轮相互反驳，quorum 通过阈可调。仍 llm-assisted 严格分区、人确认。

---

## 9. 验收标准（§16 M2b）+ 测试策略 `[D2]`

全 REPLAY、零实网。锚点：
1. `test_generate_20_distinct_chains`：≥20 链两两 diff 非空 + ≥3 条 ScriptedDriver 可 100% 跑通（可完成性）。
2. `test_playtest_completes_chains_replay`：REPLAY 下 `run_playtest_corpus` 完成率可复现（两跑逐字段相等）+ **显著高于 random_baseline**。
3. `test_planner_executor_ablation`：flat vs planner 完成率 delta 报告存在且 planner 不劣。
4.（M2b-2）`test_memory_ablation`：mem-trace on vs off 完成率 delta 报告存在、on 不劣。
5. `test_verifier_grounding_overrides_llm`：构造 LLM 误判"死任务"但确定性可达 → agent 继续（不被 LLM 漂移带偏）。
6. **完成率标准 `[D2]`**：诚实报告分链长完成率 + 95% CI；"通过"= 全 ≥20 链跑完、带-agent 显著 > 基线、两项消融正向。**不设固定高百分比阈**（避免 gaming；§7.8）。

**录制 pass**（人工、`GAMEFORGE_LLM_LIVE=1`）：对 ≥20 链 ×{planner on/off}×{memory on/off} 跑 RECORD → cassette 入库；REPLAY 复算完成率/消融。**最大 token 开销**（`[D5]`）——opus4.8 全程或分层，node→snapshot 可配置。

---

## 10. 分期表（延后 ≠ 简化：接口现定，实现分批）

| 元素 | 接口落点 | 实现落点 |
|---|---|---|
| 场景生成器（≥20 链，含长链） | `game/aureus/scenario_gen.py` | M2b-1 |
| Playtest 核心（planner/executor/状态抽象/grounding/反思） | `agents/playtest/` | M2b-1 |
| 回归 harness + 完成率(CI) + 基线 + 录制 | `agents/playtest_harness.py` | M2b-1 |
| planner/executor 消融 | harness variant | M2b-1 |
| `MemTrace`（Recall/Compaction/skill/reflection） | `agents/playtest/memory.py`（Protocol 现定） | M2b-2 |
| 记忆消融 | harness variant | M2b-2 |
| 叙事对抗 quorum 进阶 | Consistency（part2 接口在） | M2b-2 |
| escort/deliver step kind、对话分支 Choose | `world.py`/`kernel.py`（现声明） | v-next（不在 M2b 关键路径） |

**Deferred（接口现定 — 不简化只延后）**：escort/deliver 语义步 + Choose 对话分支（v-next，非 §16 必需）；embedding 相关性项（`[D3]` 默认关）；真实引擎 headless（v-next）。
