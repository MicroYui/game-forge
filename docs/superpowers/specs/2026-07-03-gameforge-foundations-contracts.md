# GameForge — 地基契约文档（Foundational Contracts）v0.2
## 跨里程碑、"决定一次"的接口约定

| 字段 | 值 |
|---|---|
| 状态 | Draft v0.2（含外部评审补强；待最终确认） |
| 日期 | 2026-07-03 |
| 关联 | `2026-07-03-gameforge-prd.md`（PRD） |
| 范围 | 只定"决定一次、所有里程碑都依赖"的契约；子系统**内部**实现留给各里程碑 plan |

**核心原则（v0.2 关键）：不简化，只分期。** 本文把完整字段/类型/接口**现在就定死**；`impl@M0a/M0b/M1/M2` 标注仅表示该元素**首次落地实现**的里程碑，**不代表**契约裁剪。生产级产品不允许"最小子集"式简化接口——接口一次定全，实现分批推进。

**v0.2 相对 v0.1 的补强**：① platform 拆层消除隐性 import 循环；② IR 类型集补全战斗/经济/叙事；③ Relation 加 id/source_ref、canonical 快照序列化；④ DSL 谓词级 oracle；⑤ Env 双层动作 + Observation 补强 + state_hash 作用域；⑥ 版本元组扩展；⑦ Finding/Patch 补 evidence/precondition/run_id 等。

---

## 契约 1：仓库布局与依赖方向 `impl@M0a`

Monorepo。**platform 拆成 `runtime`（底层能力）/ `platform`（产品平台）/ `apps`（组合层）**，消除 `platform.api → agents → platform.model_router` 的隐性循环。

```
gameforge/
  contracts/          # ← 唯一 schema 源真相（IR schema、DSL 文法、Finding/Patch、Env 契约的机器可读版）
  runtime/            # 底层能力，无业务逻辑，不依赖 agents/spine
    model_router/
    cassette/
    observability/
    config/
    secrets/
  spine/              # 确定性可信主干（无 LLM 依赖）
    ir/  dsl/  checkers/  sim/  versioning/
  env/                # Agent-Env 契约（interface，无实现）
  game/aureus/        # 参考游戏：确定性内核（实现 env）+ 薄渲染
  agents/             # 有边界 LLM 层：extraction/triage/repair/consistency/generator/playtest
  platform/           # 产品平台层：approval/rbac/audit/workflow
  apps/               # 组合层
    api/  worker/  cli/
  bench/              # GameForge-Bench
web/                  # React/TS 前端
```

**依赖方向（单向，CI 强制）**：
```
contracts   ← 所有包可依赖；contracts 自身不依赖任何业务包
runtime     → contracts
spine       → contracts                       # 仅此一项
env         → contracts
game/aureus → contracts + env
agents      → contracts + spine + env + runtime
platform    → contracts + spine + runtime
apps/api    → contracts + spine + agents + platform + game/aureus + runtime   # 组合层，允许依赖全部业务模块
apps/worker → 同 apps/api
bench       → contracts + spine + agents + game/aureus + runtime
web         → 仅经 apps/api（不直接依赖任何业务包）
```

**硬约束（CI 依赖 lint）**：
- `spine/**` 不得 import `agents.*` / `runtime.model_router` / `platform.*`
- `spine/**` 不得 import 任何 LLM/agent SDK：`openai` / `anthropic` / `langchain` / `langgraph` / `llama_index` 等（污染常从 checker/IR 里偷偷 import 进来，必须拦）
- `runtime/**` 不得 import `agents.*`
- `web` 不得直接 import 业务包

> 这条 lint 是"确定性可信主干"从"口号"变"可信"的关键机制。

---

## 契约 2：Spec-IR schema `impl@M0a(core) / M0b(combat-economy)`

**逻辑模型 = 类型化属性图**。定**逻辑模型 + 查询接口 + 序列化规则**；物理存储（PG+JSONB+adjacency vs 图库 vs RDF）留 M0a 决定，但**必须满足下列全部查询接口**（含 path/subgraph），不得因物理选型阉割接口。

### 2.1 Entity / Relation（补 id / source_ref / schema_version）
```
Entity   { id, type, attrs, source_ref?, tags?, schema_version }
Relation { id, type, src_id, dst_id, attrs?, source_ref?, schema_version }
```
- **Relation 必须有 `id`**：支持多重边（同一 Monster 在不同 DropTable 以不同概率掉同一 Item），且 Finding/Patch 可精确指向一条边（`set_relation_attr(relation_id, probability=0.2)`）。
- **`source_ref`**（round-trip + minimal repro 依赖）：`{adapter, file, sheet, row, column?}`，使 Finding 能显示"`quest.xlsx / Quest / row 17 / reward_gold = 120 > 80`"。

### 2.2 节点类型全集（分期实现，不裁剪）
```
# core        impl@M0a
Faction, Character, NPC, Quest, QuestStep, DialogueNode,
Region, SpawnPoint, Interactable,
Item, Monster, Currency, Shop, DropTable, RewardTable, GachaPool,
Event, UnlockCondition

# combat-economy   impl@M0b
Equipment, Skill, StatusEffect, Effect, BattleEncounter, Formula
```
- `DropTable`(怪物/关卡/采集掉落) / `RewardTable`(任务/活动/成就/邮件奖励) / `GachaPool`(抽卡) **三者分开**，避免 `rewards` 边混乱。
- `SpawnPoint`（比 `Region` 细，表达刷怪/采集/初始位置/阶段生成）、`Interactable`（宝箱/机关/采集点/门/传送点/调查点，对应 action `interact`）、`UnlockCondition`（把 `chapter>=3 / questA done / level>=10 / rep>=50 / item owned / event active` 抽成节点，供 Quest/Dialogue/Shop/GachaPool/Region 引用——叙事剧透检查本质就是 unlock 检查）。
- `BattleEncounter`（敌人组合/等级/波次/地形/胜负条件/奖励，替代裸 `enemy_group`）、`Skill`/`StatusEffect`/`Effect`（战斗引用完整性）、`Formula`（数值公式/曲线，供 SMT 检查）。

### 2.3 边类型全集（分期实现，不裁剪）
```
# 结构/任务
has_step, precedes, requires, gated_by, unlocks, starts_at, talks_to, triggered_by
# 空间/可达
located_in, contains, spawns, path_to
# 经济/产消
drops_from, grants, consumes, rewards, sells
# 战斗/效果
uses_skill, applies_effect, has_stat_curve
# 叙事/阵营
hostile_to, ally_with, belongs_to, reveals, references
```
- **`has_step`（Quest→QuestStep）必须有**：任务 DAG/步骤顺序/引用/minimal-repro 都依赖它，**不得**只靠 `Quest.attrs.steps`。
- `contains`（容器/包含，区别于 `located_in` 位置）、`spawns`（SpawnPoint→Monster/Interactable，collect source/fight target/可达性关键）、`grants`/`consumes`（source/sink 经济检查）、`gated_by`（比 `requires` 更高层的"可用性门控"）、`talks_to`（talk step 的目标 NPC，不一定是 start npc）。
- **`path_to` 是派生视图**：`path_exists(src,dst,via)` **不只查 IR 边**，可调用 navigation-graph derived view（Region 层太粗，无法验证真实可达性）。契约明确此点。

### 2.4 canonical 序列化与快照 ID（关键补强）
```
snapshot_id = sha256(canonical_json(content_payload))
```
`canonical_json` 规则（否则 hash 不稳定，殃及 cassette/diff/lineage）：
1. object key 按字典序排序；
2. 丢弃 null 的 optional 字段；
3. 有序语义的数组保序，无序集合按 `id` 排序；
4. 浮点用固定 decimal/string 表示，避免平台差异；
5. `content_payload` **不含** `created_at`/`author`/`snapshot_id` 等非内容字段。

### 2.5 快照与查询接口
- **快照**：不可变、内容寻址，`{snapshot_id, parent_id, meta_schema_version, created_at, author}`。
- **查询接口**（逻辑，物理无关，物理存储必须全部支持）：`get_node(id)` / `neighbors(id, edge_type?)` / `nodes_of_type(type)` / `get_relation(id)` / `path_exists(src,dst,via)`（可走 derived nav view）/ `subgraph(types)` / `diff(a,b)`。

**验证锚点**：`diff(import(export(x)), x) == ∅`；同内容不同字段序 → 相同 `snapshot_id`。

---

## 契约 3：约束 DSL 文法 + 谓词级 oracle + 编译器接口 `impl@M1（文法现定）`

- **文法**（YAML，`dsl_grammar_version`），支持**谓词级 oracle**（顶层二分类太粗——"白鸢身份"天然混合：章节门控=确定性，语义泄露=llm-assisted）：
  ```yaml
  id: <str>
  kind: structural | numeric | narrative
  oracle: deterministic | llm-assisted | mixed
  predicates:                         # 谓词级标注
    - expr: "chapter >= 3"
      oracle: deterministic
    - expr: "semantically_reveals_identity(dialogue, 白鸢)"
      oracle: llm-assisted
  scope | forall: <selector>
  severity: critical | major | minor
  note?: <str>
  ```
  规则：**只要一条约束含 `llm-assisted` 谓词，其 Finding 归"LLM 建议（需人确认）"，不进确定性检出统计**（PRD §5.2/§7.3）。
- **编译器接口**：`compile(constraint) -> Checker`，`Checker.check(ir_snapshot) -> [Finding]`。
  - `structural` → graph/ASP(Clingo)；`numeric` → SMT(z3)；`llm-assisted` 谓词 → 路由 agent 层，不编译成确定性 checker。
- **solver 预算**：ASP grounding 上限 + z3 超时；`unknown`/超时/超预算 → Finding `status=unproven`（**绝不等于 pass**）；非线性不可判定片段用有界近似并标 `unproven`/不完备。

**验证锚点**：同一 structural 约束用"朴素图算法"与"ASP 编码"两条路对拍一致（差分测试）。

---

## 契约 4：Agent-Env 契约 `impl@M0a`（Aureus + Playtest + 回归三家共用）

Gym/dm_env 风格，引擎无关，`env_contract_version`。**动作分两层**，防止 Action 爆炸。

```
reset(scenario, seed) -> Observation
step(action) -> (Observation, reward, done, info)
state_hash() -> str
```

### 4.1 Low-level 原子动作（进 Env）
```
observe | navigate_to(target) | interact(target) | choose(option_id) |
attack(target_id) | cast_skill(skill_id, target_id) |
use(item_id, target?) | pickup(item_id) | equip(item_id) |
buy(shop_id, item_id, count) | sell(shop_id, item_id, count) | wait(ticks)
```
### 4.2 High-level 语义宏动作（Playtest Agent planner 层，编译成原子序列，**不进 Env**）
```
accept_quest | turn_in | talk        # e.g. talk = navigate_to + interact + choose
```
> 契约明确：Env 原子动作少而通用；escort/deliver/shop/gacha/equip 等扩展由 planner 编译，**不改 Env 契约**。

### 4.3 Observation（补强，Playtest 依赖）
```
{ tick, player_pos, player_stats, equipped_items, active_effects,
  active_quests, completed_quests, known_quests, quest_state,
  inventory, hp,
  nearby_entities[], reachable_targets[], available_interactions[], visible_map,
  dialogue_options[], last_action_result, logs[] }
```
`reachable_targets` / `available_interactions` / `last_action_result` 尤其关键——否则 Agent 只能靠猜动作试错，轨迹很乱。

### 4.4 确定性与 state_hash 作用域
- env 必须 seed 化；相同 `(scenario, seed, action 序列)` → 相同轨迹与 `state_hash`。
- **`state_hash = hash(canonical_env_state)` 包含**：tick、player state、quest states、inventory、world-object states、monster states、event flags、rng state。**不包含**：logs、render-only state、wall-clock、debug metadata（否则 hash 因日志/调试信息波动）。

**验证锚点**：同 seed 重放下逐 tick `state_hash` 相等。

---

## 契约 5：版本 / 血缘 / 审计模型 `impl@M0b`

**版本即函数（扩展元组——5 元组不足以完整复现）**：
```
config = f(
  doc_version,
  ir_snapshot_id,
  constraint_snapshot_id,      # 约束版本 ≠ kg 版本
  prompt_version,
  model_snapshot,
  agent_graph_version,
  tool_version,                # checker/编译器版本影响结果
  env_contract_version,        # game/env 版本影响 playtest
  seed,                        # 仿真/游戏
  cassette_id?                 # 决定回放复现
)
```
- **工件（Artifact）**：IR 快照、config 导出、checker run、playtest trace、patch —— 每个有 `artifact_id` + `lineage:[parent_artifact_id...]`，连成 lineage DAG。
- **回滚** = 指针重指历史快照（工件不可变、不删除）。
- **审计**：append-only `(actor, action, artifact_id, ts)`，WORM/内容哈希。

**验证锚点**：任意工件可回溯全部来源元组；回滚后血缘可追。

---

## 契约 6：Finding / Patch 标准数据格式 `impl@M1`（最易膨胀的接口，字段一次定全）

### 6.1 Finding
```
Finding {
  id, finding_schema_version,
  source: checker | sim | playtest | llm,
  producer_id, producer_run_id,          # 可追到哪次运行产生
  oracle_type: deterministic | llm-assisted | simulation,
  defect_class, severity,
  snapshot_id,                           # 针对哪个 IR 快照
  entities: [id...], relations: [id...], constraint_id?,
  evidence,                              # 按来源不同：cycle path / unsat core / 分布+seed+CI / action trace+failure tick / quoted span+rationale
  minimal_repro,                         # 结构化：哪条任务哪一步 / 哪张表哪一行（用 source_ref）
  status: confirmed | unproven | dismissed | fixed | accepted_risk,
  confidence?, message, created_at
}
```
- `evidence` 必须有，否则 Dashboard/Bench 会不断加临时字段。
- **status 生命周期全定**（`unproven`=solver 未证明；`dismissed`=人工/复验驳回；`fixed`=patch 后复验通过；`accepted_risk`=人工接受）；实现可分期，契约不裁剪。

### 6.2 Patch（补 precondition / base_snapshot，支持 rebase-or-reject 乐观并发）
```
Patch {
  id, patch_schema_version,
  base_snapshot_id, target_snapshot_id,  # 针对哪个快照生成
  expected_to_fix: [finding_id...],
  preconditions: [...],                  # 应用前必须成立（否则 rebase 或拒绝）
  side_effect_risk,
  ops: [ TypedOp... ],
  produced_by: agent | human, producer_run_id, rationale,
  validation_status, regression_status, approval_status, created_at
}
TypedOp {
  op_id,
  op: add_entity | delete_entity | set_entity_attr
    | add_relation | delete_relation | set_relation_attr | replace_subgraph,
  target,                                # entity_id / relation_id / path
  old_value?, new_value?,                # old_value 支持乐观并发：仅当仍等于 old_value 才应用
  source_ref?
}
```
> 解决 PRD §12A.4 并发：Agent 修的是 snapshot A，若当前已是 B，则按 `preconditions`/`old_value` **rebase 或拒绝**，不盲目应用引入新问题。

**验证锚点**：确定性 Finding 与 llm-assisted Finding 存储/统计严格分区；patch 在 `old_value` 失配时被拒绝。

---

## 契约 7：Model Router / Cassette 接口 `impl@M2（接口现定，M0/M1 打桩）`

- **Model Router**：`call(messages, model_snapshot, params, cache_key?) -> response`；固定 `model_snapshot`、配额、超时/重试/降级、稳定前缀语义缓存。
- **request_hash 定义**（否则回放不可控）：
  ```
  request_hash = sha256(canonical_json({
    model_snapshot, messages, tool_schema_versions,
    params, agent_node_id, prompt_version
  }))
  ```
- **Cassette**：`record(request_hash, record)` / `replay(request_hash) -> record | MISS`；record 落盘、replay 隔离非确定性、CI/回归强制 replay。**record 内容**（供成本面板/debug 复用）：`{response_normalized, raw_response, latency, token_usage, finish_reason, tool_calls}`。

**验证锚点**：同 `model_snapshot` + replay 下 agent 运行逐步复现（PRD §5.5：只承诺回放复现）。

---

## 实现分期表（分期 ≠ 简化：接口全定，实现分批）

| 契约元素 | 首次落地 | 现在就定全的原因 |
|---|---|---|
| 仓库分层 + 依赖 lint（含 no-LLM-SDK） | M0a | 定错全程返工 |
| IR core 节点/边 + canonical 快照 + Relation.id/source_ref | M0a | 所有子系统共同语言，hash 稳定性地基 |
| Agent-Env 双层动作 + Observation + state_hash 作用域 | M0a | 三家共用，事后改最痛 |
| IR combat-economy 节点/边（Skill/Effect/BattleEncounter/Formula...） | M0b | 现在预留 schema，M0b 实现 |
| 版本/血缘/审计（扩展元组） | M0b | 贯穿全系统 |
| DSL 文法（谓词级 oracle）+ 编译器接口 | M1 | checker/生成/修复都消费 |
| Finding/Patch 全字段（evidence/precondition/run_id/status 生命周期） | M1 | 生产者↔消费者接口，最易膨胀 |
| Model Router/Cassette（request_hash/record 全字段） | M2 | 可复现地基 |

一切**子系统内部**（检查算法、仿真内部、playtest 内部、前端组件、物理存储选型）——不在本文件，各里程碑 plan 现做。
