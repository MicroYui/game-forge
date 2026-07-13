# GameForge — 地基契约文档（Foundational Contracts）v0.3
## 跨里程碑、"决定一次"的接口约定

| 字段 | 值 |
|---|---|
| 状态 | **Frozen v0.3**（v0.2 七项地基 + M4 Artifact/ObjectRef/VersionTuple 增量契约） |
| 日期 | 2026-07-03；v0.3 修订 2026-07-13 |
| 关联 | `2026-07-03-gameforge-prd.md`（PRD） |
| 范围 | 只定"决定一次、所有里程碑都依赖"的契约；子系统**内部**实现留给各里程碑 plan |

**核心原则（v0.3 关键）：不简化，只分期。** 本文把完整字段/类型/接口**现在就定死**；`impl@M0a/M0b/M1/M2/M4` 标注仅表示该元素**首次落地实现**的里程碑，**不代表**契约裁剪。生产级产品不允许"最小子集"式简化接口——接口一次定全，实现分批推进。

**v0.2 相对 v0.1 的补强**：① platform 拆层消除隐性 import 循环；② IR 类型集补全战斗/经济/叙事；③ Relation 加 id/source_ref、canonical 快照序列化；④ DSL 谓词级 oracle；⑤ Env 双层动作 + Observation 补强 + state_hash 作用域；⑥ 版本元组扩展；⑦ Finding/Patch 补 evidence/precondition/run_id 等。

**v0.3 增量（M4 生产化冻结）**：不改 v0.2 的依赖方向与确定性边界；补全跨存储 `ObjectRef`、M4 工件类型、Artifact→ObjectRef 不变量，以及各工件的 `VersionTuple` producer 规则；多调用/多模型以向后兼容的 `ExecutionIdentityV1` 聚合投影，不拿任一调用冒充全体。Model Router/Cassette 只以新 discriminator 增量演进，永久保留 M2/M3 的 `model-router@1` / `cassette@1` reader 与 request hash；历史 loose cassette 只有经 exact request/profile/version 验证导入才可成为 M4 `lineage@2` bundle，证据不足仍保留 legacy direct replay而不伪造。详细工作流、存储 Protocol 与运维设计见 `2026-07-13-m4-production-hardening-design.md`。

---

## 契约 1：仓库布局与依赖方向 `impl@M0a`

Monorepo。**platform 拆成 `runtime`（底层能力）/ `platform`（产品平台）/ `apps`（组合层）**，消除 `platform.api → agents → platform.model_router` 的隐性循环。

```
gameforge/
  contracts/          # ← 唯一 schema 源真相（IR schema、DSL 文法、Finding/Patch、Env 契约的机器可读版）
  runtime/            # 底层能力，无业务逻辑，只依赖 contracts
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
    api/  worker/  solver_worker/  cli/
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
apps/solver_worker → contracts + spine            # allowlisted solver 子进程入口；隔离机制仍在 runtime
bench       → contracts + spine + agents + game/aureus + runtime
web         → 仅经 apps/api（不直接依赖任何业务包）
```

**硬约束（CI 依赖 lint）**：
- `spine/**` 不得 import `agents.*` / `runtime.model_router` / `platform.*`
- `spine/**` 不得 import 任何 LLM/agent SDK：`openai` / `anthropic` / `langchain` / `langgraph` / `llama_index` 等（污染常从 checker/IR 里偷偷 import 进来，必须拦）
- `runtime/**` 只可 import `contracts.*`（不得 import `spine/agents/platform/apps/env/game`）
- `platform/**` 不得 import `agents.*` / `game.*` / `apps.*`
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

## 契约 5：版本 / 血缘 / 审计模型 `impl@M0b(core) / M4(lineage@2 + audit@2)`

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

M4 起，单次 Agent/模型调用仍把 literal `prompt_version/model_snapshot` 写入上述字段；聚合了多节点、多 attempt 或 fallback model 的 Artifact 不能任选一个值冒充全体。新增 wire DTO 只扩展 `lineage@2` metadata，不改变 `VersionTuple` 的既有十字段或 `lineage@1` reader：

```text
InvocationVersionBindingV1 {
  attempt_no, call_ordinal, route_ordinal, transport_attempt?,
  routing_decision_kind:native|legacy_import, routing_decision_id,
  agent_node_id, prompt_version, model_snapshot, tool_version,
  execution_source:online|full_response_cache|cassette_replay,
  response_consumed
}
VersionSetProjectionV1 {
  field:prompt_version|model_snapshot,
  mode:not_applicable|single|set,
  members[], tuple_value?
}
ExecutionIdentityV1 {
  identity_schema_version:execution-identity@1,
  scope:record_shard|attempt|run|artifact,
  agent_graph_version?, bindings:[InvocationVersionBindingV1...],
  prompt_projection:VersionSetProjectionV1,
  model_projection:VersionSetProjectionV1,
  digest
}
```

`bindings` 按 `(attempt_no,call_ordinal,route_ordinal)` 稳定排序且三元组唯一；`route_ordinal` 从 1 单调递增，覆盖同一 logical call 的 fallback/cache/replay routing decisions，online transport attempt 存在时为正整数，cache/replay 时必须 null。`routing_decision_kind=native` 时 ID 必须唯一解析到 M4 `RoutingDecision`；`legacy_import` 只允许 verified `cassette@1` 导入，且必须解析到下文内容寻址的 `LegacyImportRoutingDecisionV1`，不能拿导入时生成的 replay 选择冒充原始在线路由。每个 logical call 恰有一个 `response_consumed=true`，或在调用未取得可消费响应时全 false；routing decision、request 与 cassette/transport record 必须逐字段闭合，不能从当前默认值补齐。run/attempt/failure identity 保留所有 route bindings；领域 Artifact 的 identity 只能包含对其产生因果贡献的 `response_consumed=true` 子集，不能把失败 fallback model 冒充内容生产者，也不能丢掉 run-level 失败历史。

projection 的 `members` 是该 identity bindings 对应字段的 stable-unique canonical 值：空集时 `mode=not_applicable,tuple_value=null`；单成员时 `mode=single,tuple_value=该 literal`；多成员时分别使用 `prompt-set:sha256:<digest>` / `model-set:sha256:<digest>`，其中 digest 是 `sha256(canonical_json({field,members}))`。`ExecutionIdentityV1.digest=sha256(canonical_json(payload excluding digest))`；ArtifactV2 以保留键 `meta.execution_identity` 内联完整对象，且 `VersionTuple.prompt_version/model_snapshot/agent_graph_version` 必须逐字段等于其 projection/identity。这样旧 reader 仍看到字符串，新 reader 可恢复完整调用/route 集合；没有实际模型调用的取消/超时工件诚实使用 null，不能把计划允许的模型冒充已消费模型。`tool_version` 仍表示该 Artifact producer 的版本，每次调用自己的 tool version 保留在 binding 中。

**对象引用（M4 增量）**：
```
ObjectRef {
  object_ref_schema_version,
  key,                     # 内容寻址逻辑 key；不含 bucket/endpoint
  sha256,                  # 对完整明文字节计算的 SHA-256；multipart ETag 不能代替
  size_bytes
}
ObjectLocation {
  location_schema_version, store_id, key, backend_generation, etag?, storage_class?
}
ObjectBinding {
  binding_schema_version, object_ref:ObjectRef, location:ObjectLocation,
  status:active|retired, revision, verified_at
}
```
M4/v0.3 新写入的 SHA-256 摘要字段（`payload_hash`、`ObjectRef.sha256`、下文 subject/target digest）在线上统一表示为 **64 位小写十六进制、无 `0x`/算法前缀**；只有契约明确声明为 namespaced ID 的字段（例如 `cassette_id = sha256:<digest>`）带前缀。新 schema parser 拒绝大小写、前缀或长度不一致的替代表示，避免同一摘要出现多个 canonical 值；`lineage@1`/`audit@1` 历史 reader 保留原存储值，不追溯改写。

`key` 必须由 SHA-256 按版本化布局规则确定性派生；Artifact 只持**跨后端可移植**的 ObjectRef。S3 VersionId/本地 opaque generation 属 ObjectLocation，通过可 revision-CAS 的 ObjectBinding 解析，**不进入** Artifact 或内容寻址 ID。跨桶复制/恢复先复核 hash/size，再原子重建 active binding 并审计，不改 Artifact；同内容迁移后仍是同一工件。相同 key/hash/size 的并发/重试上传在 Artifact 层等价；未绑定或已替代的额外 location 由 GC 按 backend_generation 条件回收。

**工件类型全集（v0.3；新增类型必须 bump lineage schema，不接受任意客户端字符串）**：
```
source_raw | source_rendered |
ir_snapshot | constraint_snapshot | constraint_proposal |
config_export | scenario_spec | task_suite | regression_suite | golden_suite |
bench_dataset | benchmark_spec |
review_report | checker_run | simulation_run | playtest_trace |
patch | validation_evidence | regression_evidence | rollback_request |
run_result | run_failure | cassette_bundle | migration_report | bench_report | operational_evidence
```

**工件（Artifact，按 `lineage_schema_version` 判别）**：
```
ArtifactV1 {
  artifact_id,
  lineage_schema_version: lineage@1,
  kind, version_tuple, lineage,
  payload_hash?, created_at?, meta
  # 历史 schema 无 ObjectRef；reader 不得伪造
}
ArtifactV2 {
  artifact_id,
  lineage_schema_version: lineage@2,
  kind: ArtifactKind,
  version_tuple: VersionTuple,
  lineage: [parent_artifact_id...],
  payload_hash,
  object_ref,
  created_at?,
  meta                      # canonical immutable metadata；禁止塞可变运行态
}
Artifact = ArtifactV1 | ArtifactV2
```
- M4 发布的工件使用 `lineage@2`：`payload_hash` 与 `object_ref` 必填，且 `payload_hash == object_ref.sha256`。`lineage@1` reader 永久保留；SQL 物理列可因 expand/contract nullable，但 domain parser 必须按 discriminator 拒绝 `lineage@2 + object_ref=null`，旧行则不伪造 ObjectRef。同 `artifact_id` 异 canonical 内容、父集合或 payload hash 必须 `IntegrityViolation`，绝不 merge 覆盖。
- `lineage@1` 保留旧 ID 公式；`lineage@2` 的 `artifact_id = sha256(canonical_json({lineage_schema_version, kind, version_tuple, lineage:sorted_unique_parents, payload_hash, meta}))`，使来源边与决定行为的 immutable metadata 同受内容寻址保护，并避免 v1/v2 碰撞。`created_at`、存储后端与 ObjectLocation/Binding 不参与内容哈希；ObjectRef.key 由 payload hash 确定性校验。可变运行态只能进 Run/Approval/Audit 等记录，不能藏在 Artifact.meta。
- `lineage` 只表达内容派生 DAG；回滚是 ref 指针事件，不创建回滚 lineage 边。
- **回滚** = 指针重指历史快照（工件不可变、不删除）。
- **审计**：M4 新记录使用下列 `audit@2`；`actor` 是实际执行者，人与 worker/service 不同时以 `initiated_by` 保留真实发起者。`AuditSubject` 可定位 Artifact 之外的 ref/run/approval/lease/budget/session 等权威资源，审计解释不依赖 best-effort telemetry。
  ```
  AuditActor       { principal_id, principal_kind }
  AuditSubject     { resource_kind, resource_id, artifact_id? }
  AuditCorrelation { request_id?, run_id?, trace_id? }
  AuditRecordV2 {
    audit_schema_version:audit@2, chain_id, seq,
    actor:AuditActor, initiated_by:AuditActor?, action,
    subject:AuditSubject, correlation:AuditCorrelation,
    ts, prev_hash, content_hash
  }
  content_hash = sha256(canonical_json({audit_schema_version,chain_id,seq,actor,initiated_by:null|value,
                                        action,subject,correlation,ts,prev_hash:null|value}))
  ```
  `audit@1` reader 永久保留（legacy actor 是字符串、subject 只有可选 artifact_id）；domain parser 按 discriminator 返回 `AuditRecordV1 | AuditRecordV2`，迁移不伪造旧 actor/resource/correlation。WORM/外部锚定威胁模型见 M4 设计。

### 5.1 VersionTuple producer matrix（M4 起强制）

`None` 只表示该字段对该工件**不适用**，不能拿来表示“生产者忘了写”或“数据未知”。M4 新生产者在发布 Artifact 前按下表校验；缺必填字段 fail-closed。历史工件缺证据时保留原样并报告 `evidence_missing`，不得伪造默认值或宣称可回放。

| Artifact kind | 必填 VersionTuple 字段 | 条件必填 |
|---|---|---|
| `source_raw` | `doc_version` | 对所有 raw source，`doc_version` 表示受信 connector 分配的稳定 source revision；无上游版本时使用完整内容哈希，不能使用抓取时钟或进程默认值。tool output 另继承输入适用字段并填 `tool_version`；connector/provenance 版本进 Artifact meta |
| `source_rendered` | `tool_version` + raw source 的适用字段 | 文档派生时继承 `doc_version`；`tool_version` 固定 renderer/sanitizer 版本；作为实际模型请求的 exact rendered prompt evidence 时，另必填 `prompt_version/agent_graph_version`，并与对应 RunIntermediateArtifactLink/request/cassette identity 闭合 |
| `ir_snapshot` | `ir_snapshot_id`, `tool_version` | 来自文档时 `doc_version`；Agent 抽取时补 `prompt_version/model_snapshot/agent_graph_version`，RECORD/REPLAY 再补 `cassette_id` |
| `constraint_snapshot` | `constraint_snapshot_id`, `tool_version` | 来自文档/IR 时对应 `doc_version/ir_snapshot_id`；由 human-authored proposal 派生时继承其 Agent 字段。validation 可先发布非权威 candidate Artifact，只有另一 human 批准后的 ref CAS 才使其 authoritative；Agent 不得直接产生权威约束 |
| `config_export` | `ir_snapshot_id`, `constraint_snapshot_id`, `tool_version` | 由环境消费时 `env_contract_version` |
| `scenario_spec` | `tool_version` | 从内容/IR/Constraint 派生时继承 `doc_version/ir_snapshot_id/constraint_snapshot_id`；绑定可执行环境时 `env_contract_version` |
| `task_suite` / `regression_suite` / `golden_suite` | `tool_version` | 继承实际消费的 `doc_version/ir_snapshot_id/constraint_snapshot_id/env_contract_version`；多 seed 集合在版本化 payload manifest 中列出，不能拿单个假 seed 代表集合 |
| `bench_dataset` | `tool_version` | 继承样本来源的适用字段；dataset hash、分区、样本 seed/来源 manifest 进 payload，不能从运行时默认数据集补齐 |
| `benchmark_spec` | `tool_version` | 约束/环境相关 benchmark 继承适用 `constraint_snapshot_id/env_contract_version`；指标/分区/采样 policy version 进 immutable payload/meta |
| `checker_run` / `review_report` | `ir_snapshot_id`, `tool_version` | 实际消费 DSL 时 `constraint_snapshot_id`；含 LLM 建议时补 `prompt_version/model_snapshot/agent_graph_version`；RECORD/REPLAY 再补 `cassette_id` |
| `simulation_run` | `ir_snapshot_id`, `tool_version`, `seed` | 实际消费 DSL 时 `constraint_snapshot_id`；消费 Env 时 `env_contract_version` |
| `constraint_proposal` / `patch` | 输入工件对应的 `doc_version/ir_snapshot_id/constraint_snapshot_id`, `tool_version` | `produced_by=agent` 时必填 `prompt_version/model_snapshot/agent_graph_version`；RECORD/REPLAY 再必填 `cassette_id` |
| `playtest_trace` | `ir_snapshot_id`, `constraint_snapshot_id`, `tool_version`, `env_contract_version`, `seed` | Agent 驱动时补 `prompt_version/model_snapshot/agent_graph_version`；RECORD/REPLAY 再补 `cassette_id` |
| `validation_evidence` / `regression_evidence` | exact target binding 的适用 snapshot/constraint 字段 + `tool_version` | 无 candidate target（例如 parse 未形成 candidate）时才继承 subject 输入版本并在 payload 标 target absent；回归含环境/Agent 时按上两行补齐，subject/base 仍作为 typed lineage parent而非强塞进 target tuple |
| `rollback_request` | 目标 Artifact 的适用版本字段 + `tool_version` | 当前/目标 artifact ID、ref name/revision、exact rollback ExecutionProfile binding 与审批 policy 放 payload；两端同时作为 lineage parent，不把两个 tuple 强塞成一个 |
| `run_result` / `run_failure` | 创建 Run 时冻结的完整适用 VersionTuple basis + `tool_version` | terminal tuple 只可按 `RunManifestVersionProjectionV1` 的冻结 transition policy 派生：有实际模型调用时 `prompt_version/model_snapshot/agent_graph_version` 必须投影自 exact terminal `ExecutionIdentityV1`；无调用则二者为 null；RECORD 在终态另填对应 aggregate `cassette_id`，REPLAY 保持创建时 cassette，live/not_applicable 不补；其余字段不得漂移 |
| `cassette_bundle` | producer `tool_version`, `cassette_id` | 仅 RECORD 或下述 verified legacy import 生产；有调用时 `agent_graph_version` + `meta.execution_identity` 必填。record shard 的 prompt/model 为单成员 projection；attempt/run bundle 从全部 child bindings 得到 single/set/null projection，不能任选一个调用值。`cassette_id = sha256:<bundle_payload_hash>`（非 Artifact ID，避免循环 hash）；record-shard/attempt/run 三层规则见契约 7；raw response 为敏感内容，读取受 RBAC/脱敏策略限制 |
| `migration_report` | 源 Artifact 的版本字段 + 执行迁移器的 `tool_version` | 无 |
| `bench_report` / `operational_evidence` | `tool_version` | 数据集/回放相关证据按其输入补齐 |

`run_result` / `run_failure` 是**运行清单工件**，不是把所有父工件 VersionTuple 机械合并成一个 tuple。M4 新写入必须在 payload 中嵌入以下专用受控投影：

```text
VersionTransitionPolicyRefV1 {
  policy_id, policy_version, digest
}
RunManifestParentBindingV1 {
  artifact_id,
  role:input|intermediate|output|evidence,
  publication:existing|run_published,
  attempt_no?, ordinal?,
  cassette_scope?:record_shard|attempt_bundle|run_bundle|replay_input
}
RunManifestVersionProjectionV1 {
  projection_schema_version:run-manifest-version-projection@1,
  manifest_scope:attempt|run, attempt_no?,
  run_kind:{kind,version}, run_payload_hash,
  frozen_input_version_tuple:VersionTuple,
  terminal_version_tuple:VersionTuple,
  version_transition_policy_ref:VersionTransitionPolicyRefV1,
  parents:[RunManifestParentBindingV1...]
}
```

`parents` 按 `(role,attempt_no|null,ordinal|null,cassette_scope|null,artifact_id)` canonical 排序，`artifact_id` 必须唯一；其 ID 稳定去重后与外层 `ArtifactV2.lineage` **精确同集**，不能藏父工件或留下无角色 lineage。`input` 必须精确覆盖 Run 创建时冻结的输入 Artifact；`intermediate`、`output`、`evidence` 分别由已提交的中间链接、OutcomeArtifactPolicy 与失败证据证明，`publication` 说明是既有输入还是本 Run 发布。OutcomeArtifactPolicy 的 `primary` 与 `output` rule 都投影为 manifest `role=output`；唯一主产物只由 `RunResultV1.primary_artifact_id` 标识，不能虚构 manifest `primary` role。`manifest_scope=attempt` 时 projection `attempt_no` 必须是正整数并逐字段匹配外层 `RunFailureV1.attempt_no` 与对应 `RunAttempt.attempt_no`，所有带 `attempt_no` 的 intermediate/evidence parent 必须且只能属于该 attempt，不能混入 prior/future attempt。`manifest_scope=run` 时，若 Run 已分配过 attempt，projection `attempt_no` 必须是正整数、等于外层 RunResult/RunFailure 的 attempt_no 与本清单聚合的最大最终 attempt；仅从未分配 RunAttempt 的 queued 控制面终结可为 null。run scope 才可按冻结 policy 聚合全部 attempts，以及**每个已关闭 attempt 独立发布的 attempt-scope failure manifest**（包含最终 current attempt，若 Run 成功则该 attempt 无 failure manifest）。`RunAttempt.failure_artifact_id` 永远只指 attempt-scope manifest，`RunRecord.failure_artifact_id` 永远只指 run-scope final manifest；run-scope manifest 不得作为自己的 parent。cassette 不是互斥 role：REPLAY bundle 是 `role=input,cassette_scope=replay_input`，RECORD shard/attempt/run bundle 是 `role=intermediate` 并带对应 scope，因而同一 Artifact 不会被迫重复列为 input+cassette。manifest 自身的 `ArtifactV2.version_tuple` 必须逐字段等于 `terminal_version_tuple`；`frozen_input_version_tuple`/`run_payload_hash` 必须等于 Run 创建时的不可变值。

`terminal_version_tuple` 默认逐字段等于 `frozen_input_version_tuple`；只有该 RunKind 冻结的版本化 transition policy 可以解释字段变化。projection 的 `version_transition_policy_ref` 必须逐字段等于所匹配 OutcomeArtifactPolicy 在 Run 创建时冻结的 exact 引用，且 `digest=sha256(canonical_json(VersionTransitionPolicyV1))`；实现必须按 `{policy_id,policy_version,digest}` 从保留历史版本的 registry 唯一解析，不能读取 current alias。M4 初始 policy 只允许：有实际 LLM 调用的 attempt/run manifest 把 `prompt_version/model_snapshot/agent_graph_version` 设置为对应 exact `ExecutionIdentityV1` projection；无调用时 prompt/model 为 null；RECORD 的 attempt-scope RunFailure 在 attempt 关闭时另将 `cassette_id` 从 null 一次性填为 exact attempt bundle，run-scope RunResult/最终 RunFailure 则填 exact run aggregate bundle；projection 必须恰有 scope/attempt_no 相符的 cassette parent。REPLAY 的 execution identity 必须与已绑定 cassette records/import evidence 逐调用相等并保持创建时 cassette ID；live 从持久 RoutingDecision + rendered request 重建 identity；not_applicable 不允许 bindings/cassette。其余字段必须字节相等。内容生成/修复等 output Artifact 按各自 producer matrix 产生新的 snapshot 字段，但**不得反写成 manifest 的输入 tuple**。publisher 必须逐个父工件按其 ArtifactKind producer matrix 与角色校验，不能因 output/preview 与 base 的 `ir_snapshot_id` 不同而把它们强行 merge，也不能从进程“当前默认版本”补值。未知/摘要不符 transition policy、未解释字段变化、identity/角色/集合/cardinality 不符一律 fail-closed。

所有 M4 `lineage@2` 多父 Artifact 都遵循 **typed role projection**，绝不把每个 parent 的完整 VersionTuple 机械 merge：child 的 producer matrix + 版本化 payload binding/policy 必须为每个 child 必填字段声明它是“本 producer 新产生”还是“从哪个 parent role 继承”。只有当**同一 child 语义字段**声明从多个 parent role 共同继承时，父值才必须相等；不一致则 fail-closed，除非显式 `migration_report`/Patch merge/版本化 transition policy 逐字段说明选择。父工件自身的 producer-local `tool_version`、model/prompt/cassette 等若未投影到 child，不要求彼此相等，仍完整保留在 lineage 中；child 的 `tool_version` 始终是 child producer 的版本。`config_export` 的 preview/constraint、candidate constraint 的 proposal/可选 base（source 由 proposal 的 direct lineage 传递）、TaskSuite 的 preview/config/constraint/scenario 等 typed binding 都属于这种显式 projection。某 M4 ArtifactKind 没有冻结 parent roles/projection 时禁止以 `lineage@2` 发布，且永远禁止从进程“当前默认版本”补齐；`lineage@1` 历史 reader/ID 公式仍按前述兼容规则保留，不追溯伪造 role projection。

迁移后的 IR/Constraint 写入新的 snapshot ID，保留源工件为 lineage parent。LLM `live` 工件允许无 `cassette_id`，但必须在 meta 中标 `replayability=online_only`；只有 `record|replay` 工件可声明 `cassette_replay`。`llm_execution_mode=not_applicable` 的纯确定性生产者只有在所有适用输入/工具/约束/环境/seed 均冻结时可标 `deterministic_recompute`；DR 等真实基础设施结果只标 `operational_observation`，不因“未调用 LLM”冒充可复算。

**验证锚点**：任意工件可回溯全部适用来源元组；同 `artifact_id` 异内容被拒；已提交 `object_ref` 可读取且哈希/大小一致；回滚后内容 DAG 不造环、ref transition 可追。

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

### 6.2 Patch（Patch@2 不可变 revision + 派生工作流投影）

M4 新写入使用 `patch@2`；`patch@1` reader 永久保留。Patch 是 immutable Artifact payload，审批/验证状态不写回 Patch：

```
PatchV2 {
  patch_schema_version: patch@2,
  revision, supersedes_artifact_id?,      # 修改必建新 revision；指前一 Patch Artifact.artifact_id
  base_snapshot_id, target_snapshot_id,  # 针对哪个快照生成
  expected_to_fix: [finding_id...],
  preconditions: [...],                  # 应用前必须成立（否则 rebase 或拒绝）
  side_effect_risk,
  ops: [ TypedOp... ],
  produced_by: agent | human, producer_run_id?, rationale
}
PatchView {
  patch: PatchV2,
  validation_status, regression_status, approval_status, workflow_revision
  # 全部由 versioned evidence + ApprovalItem 权威状态派生，不参与 Patch payload/hash
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
Patch 的资源 ID/API `{id}` 一律指外层 `Artifact.artifact_id`；`created_at` 也只在 Artifact envelope，避免 payload 自带身份/时间造成循环 hash 或双字段。`produced_by=agent` 时 producer_run_id 必填且可解析到 Run；`produced_by=human` 时必须为 null，真实 actor 进 Artifact meta/audit，不拿 request_id 冒充 Run。`patch@1` 的 embedded statuses 只作 legacy read-only projection，**不能**作为 M4 apply 授权；历史 Patch 若要应用，必须创建 `patch@2` revision、重跑 validation/regression 并创建新 ApprovalItem，不伪造历史 evidence。解决 PRD §12A.4 并发：Agent 修的是 snapshot A，若当前已是 B，则按 `preconditions`/`old_value` **rebase 或拒绝**，不盲目应用引入新问题。

**验证锚点**：确定性 Finding 与 llm-assisted Finding 存储/统计严格分区；patch 在 `old_value` 失配时被拒绝。

---

## 契约 7：Model Router / Cassette 接口 `impl@M2(core) / M4(typed usage + cache boundary)`

- **版本化 Model Router**：wire union=`ModelRequestV1 | ModelRequestV2`。既有 `model-router@1` 的字段与 parser 永远保留；`cache_key` 继续只是被 request hash 排除的 legacy no-op/routing hint，绝不成为完整响应缓存身份，也不在读取时被悄悄解释为新的 provider cache 指令。M4 新写入使用 `model-router@2`，把该字段替换为类型化、有界的 `prefix_cache_directive?`；它只提示 provider prompt-cache，命中后**仍调用 provider**。本地完整响应缓存只按完整 `request_hash` 命中；禁止用 prefix hash、legacy `cache_key` 或 embedding 相似度复用完整响应。
  ```text
  ModelRequestV1 {
    model_router_schema_version:model-router@1, model_snapshot, messages, params,
    tool_schemas, agent_node_id, prompt_version, cache_key?
  }
  PrefixCacheDirectiveV1 {
    directive_schema_version:prefix-cache-directive@1, prefix_message_count,
    prefix_hash, provider_scope, policy_version
  }
  ModelRequestV2 {
    model_router_schema_version:model-router@2, model_snapshot, messages, params,
    tool_schemas, agent_node_id, prompt_version, prefix_cache_directive?
  }
  ```
  directive 的 prefix 必须是 messages 的前 `prefix_message_count` 项，`prefix_hash=sha256(canonical_json(prefix_messages))`，count/字符串有硬上限；它与 v1 `cache_key` 一样不进下述 semantic `request_hash`，transport 只能在 provider 明确支持且 policy allowlist 命中时使用。
- **request_hash 定义**（v1/v2 的语义字段与 M2 公式保持不变，否则现有 cassette 全部失效）：
  ```
  request_hash = sha256(canonical_json({
    model_snapshot, messages, tool_schema_versions,
    params, agent_node_id, prompt_version
  }))
  ```
- **观测值类型**：`TokenUsageObservation { status:reported|unavailable, input_tokens?, output_tokens?, cache_read_tokens?, cache_write_tokens?, total_tokens? }`；`LatencyObservation { status:reported|unavailable, provider_latency_ms? }`；`CacheHitObservation { status:reported|unavailable, hit? }`。`unavailable` 与真实 0/false 严格区分；reported 时 token/latency 字段满足非负与 total 一致性校验，cache hit 的 `hit` 必填。
- **版本化 Cassette**：`record(request_hash, record)` / `replay(request_hash) -> record | MISS`；wire union=`CassetteRecordV1 | CassetteRecordV2`，按 `cassette_schema_version` 判别，禁止按“字段看起来像新版”猜测。
  ```text
  CassetteRecordV1 {                         # 既有 M2/M3 wire，永久只读兼容
    cassette_schema_version:cassette@1, request_hash, agent_node_id, model_snapshot,
    response:{response_normalized,raw_response,latency_ms,token_usage,finish_reason,tool_calls},
    transport_attempts?, transport_retries?, recorded_at?
  }
  CassetteRecordV2 {                         # M4 新写入
    cassette_schema_version:cassette@2, request_hash, agent_node_id, model_snapshot,
    routing_decision, response_normalized, raw_response,
    latency:LatencyObservation, token_usage:TokenUsageObservation,
    provider_prefix_cache:CacheHitObservation, finish_reason, tool_calls,
    transport_attempt_count, transport_retry_count, recorded_at?
  }
  ```
  `cassette@1` reader 必须先看原始字段是否存在再做映射，不能让旧 Pydantic 默认值把 unknown 伪装为 0：只有存在且 **>0** 的 `response.latency_ms` 才映射 reported；缺失、null、0 或负值均为 unavailable（v1 wire 无法证明真实零，不能声称 reported）。非空 `response.token_usage` 按稳定别名组 `input_tokens|input|prompt_tokens`、`output_tokens|output|completion_tokens`、`cache_read_tokens|cache_read`、`cache_write_tokens|cache_write` 映射，同组多值不等即 `IntegrityViolation`，显式 `total_tokens` 优先，否则只在 input+output 都存在时计算二者之和；空/缺失→unavailable。legacy record 无法证明 provider cache hit 与 routing decision，二者保持 unavailable，绝不由 cache token 大于 0 反推。该映射只产生内存中的 typed observation view，不改写 `cassette@1` bytes/discriminator；response/raw/tool/finish 与 transport attempt 字段原样保留。M4 RECORD 只写 `cassette@2`，REPLAY 同时接受两版。
  ```
  LegacyImportVerificationPolicyRefV1 { policy_id, policy_version, policy_digest }
  LegacyImportVerificationPolicyV1 {
    policy_schema_version:legacy-import-verification-policy@1,
    policy_id, policy_version, source_cassette_schema_version:cassette@1,
    ordinal_mapping:single_attempt_by_source_call_ordinal,
    required_input_binding_keys[], required_profile_field_paths[],
    required_policy_binding_keys[], required_schema_binding_keys[],
    max_wire_bytes_per_call, max_calls_per_import, policy_digest
  }
  LegacyImportVerificationPolicyRegistryV1 {
    registry_schema_version:legacy-import-verification-policy-registry@1,
    registry_version, policies:[LegacyImportVerificationPolicyV1...], registry_digest
  }
  LegacyImportRoutingDecisionV1 {
    decision_schema_version:legacy-import-routing-decision@1, decision_id,
    source_wire_sha256, request_hash, agent_node_id, model_snapshot,
    execution_source:cassette_replay,
    execution_profile_binding_digests[], model_catalog_version, model_catalog_digest,
    verification_policy:LegacyImportVerificationPolicyRefV1
  }
  LegacyCassetteCallImportEvidenceV1 {
    evidence_schema_version:legacy-cassette-call-import@1,
    original_wire_utf8, original_wire_sha256,
    rendered_request_artifact_id?, request_hash?,
    import_routing_decision?:LegacyImportRoutingDecisionV1,
    invocation?:InvocationVersionBindingV1,
    source_suite_id, source_case_id, source_call_ordinal,
    importer_tool_version, verification_status:verified|evidence_missing,
    missing_fields[], evidence_digest
  }
  LegacyCassetteInputBindingV1 {
    binding_key, artifact_id, payload_hash, version_tuple:VersionTuple
  }
  LegacyCassetteProfileBindingV1 {
    field_path, profile_id, profile_version, profile_payload_hash,
    catalog_version, catalog_digest
  }
  LegacyCassettePolicyBindingV1 {
    binding_key, policy_kind, policy_id, policy_version, policy_digest
  }
  LegacyCassetteSchemaBindingV1 {
    binding_key, schema_id
  }
  LegacyCassetteRunImportManifestV1 {
    manifest_schema_version:legacy-cassette-run-import@1,
    import_id, source_suite_id, source_case_id, verification_policy:LegacyImportVerificationPolicyRefV1,
    input_artifact_bindings:[LegacyCassetteInputBindingV1...],
    execution_profile_bindings:[LegacyCassetteProfileBindingV1...],
    frozen_version_tuple?:VersionTuple,
    policy_bindings:[LegacyCassettePolicyBindingV1...],
    schema_bindings:[LegacyCassetteSchemaBindingV1...],
    ordered_call_evidence_digests[], execution_identity?:ExecutionIdentityV1,
    importer_tool_version, status:verified|evidence_missing, digest
  }
  CassetteBundleV1 {
    bundle_schema_version, scope:record_shard|attempt|run, run_id?, attempt_no?, ordinal?, outcome_code?,
    child_bundle_artifact_ids[], records:[(CassetteRecordV1 | CassetteRecordV2)...],
    legacy_call_import_evidence?:LegacyCassetteCallImportEvidenceV1,
    legacy_run_import_manifest?:LegacyCassetteRunImportManifestV1
  }
  ```
  `record_shard` 必须有 attempt/ordinal、恰好一条 record、无 child；Model Router 在把 provider response 交给 Agent 消费前先发布该 immutable `cassette_bundle` Artifact。`attempt` bundle 无内联 record，按 ordinal 引用该 attempt 的 shard Artifact，并以同一集合做 lineage parents，attempt 关闭时即使 0 次已消费调用也发布；`run` bundle 无内联 record，按 attempt_no 引用全部 attempt bundle，Run 任一终态都发布。这样 retry/crash/cancel/timeout 的已消费响应不丢，GC/DR/lineage 共享 Artifact live-set。

  既有 loose `cassette@1` 文件继续由 legacy reader 原地回放且原字节永不改写。要纳入 M4 `lineage@2` bundle，必须走**确定性 verified import**：每个 shard 内联原始 UTF-8 bytes（有界）、验证其 SHA-256 与解析出的原 wire record，加载 exact 历史 rendered ModelRequest 后重算 `request_hash`，并把从该 request、冻结 profile/agent graph/tool 解析出的 invocation 写入 `LegacyCassetteCallImportEvidenceV1`；run manifest 再冻结 exact verification-policy ref、输入/hash/VersionTuple、profile catalog binding、policy/schema、调用顺序和 aggregate execution identity。input/profile bindings 分别按唯一 `binding_key` / `field_path` 排序并逐项验证 hash/digest，input artifact ID 不得重复；`verified` 要求所有 call evidence、child 顺序、request hash 与 manifest digest 闭合；任何字段只能靠当前默认值猜测时必须 `evidence_missing`，该 bundle 不得用于 M4 Run、不得标 `cassette_replay`，但 loose v1 直接回放兼容路径仍保留。verified import 不产生 provider 调用，因此**不要求重录**历史 Opus cassette；它也不伪造不存在的历史 RunRecord。native M4 bundle 的 `run_id` 必填且禁止 legacy 字段；imported 三层 bundle 的 `run_id` 必须为 null，以 run manifest 的 `import_id` 作 source identity，record shard 必有 call evidence，run bundle 恰有 run manifest，attempt bundle按同一 import/status 聚合 child。只有全部 call/attempt/run 都 verified 的 bundle 可进入 M4 REPLAY；evidence-missing bundle 只作不可执行诊断记录。其他字段组合一律拒绝。

  `LegacyImportVerificationPolicyV1.policy_digest=sha256(canonical_json(payload excluding policy_digest))`；policies 按唯一 `(policy_id,policy_version)` 排序，`registry_digest=sha256(canonical_json(payload excluding registry_digest))`，历史版本在任一 import/bundle 保留期内不可删除。四组 required keys 各自 stable unique；limits 为正；M4 初始只允许固定的 `single_attempt_by_source_call_ordinal`，未知 mapping/readers readiness fail-closed。导入器必须按 exact `{policy_id,policy_version,policy_digest}` 从 registry 解析，不能从 current policy 猜 key 集或上限。

  `LegacyImportRoutingDecisionV1` 表示“导入器已验证并选择此 legacy record 用于 replay”，**不表示**旧录制当时存在或执行过 M4 RoutingPolicy。它必须逐字段绑定原 wire SHA、重算的 exact request hash、record 中的 exact agent/model、manifest 中按 `field_path` 排序的 execution-profile binding digests、exact model catalog 与完整 verification-policy ref；`decision_id="legacy-import-route:sha256:" + sha256(canonical_json(payload excluding decision_id))`。对应 invocation 固定 `routing_decision_kind=legacy_import`、引用该 decision ID、`execution_source=cassette_replay`，且 agent/model/request/profile/catalog 必须相等；不得填造 native RoutingDecision、历史 rule/tier/budget/reason。`LegacyCassetteProfileBindingV1` 的 binding digest 是其完整 canonical payload SHA-256；decision 中的 digest 数组按 manifest profile binding 顺序一一对应。`original_wire_sha256=sha256(UTF8(original_wire_utf8))` 且必须等于 decision 的 `source_wire_sha256` 和实际 shard 内联 bytes 的摘要。

  `LegacyCassetteCallImportEvidenceV1.evidence_digest=sha256(canonical_json(payload excluding evidence_digest))`。`LegacyCassetteRunImportManifestV1.digest=sha256(canonical_json(payload excluding digest))`；`import_id="legacy-cassette-import:sha256:" + sha256(canonical_json(payload excluding import_id,digest))`。manifest 的 `ordered_call_evidence_digests` 必须按每份 evidence 的 invocation `(attempt_no,call_ordinal,route_ordinal)` 排序且三元组唯一，与递归展平后的 record-shard Artifact **等长、一一对应、顺序相同**。每个 shard 的 `records[0]` 必须与 exact `original_wire_utf8` 解析出的完整 `CassetteRecordV1` known-field view 逐字段相等，包括 agent/model、normalized/raw response、latency/token usage、finish/tool calls、transport attempts/retries 与 recorded_at；request/model 再与 rendered request、import decision、identity 交叉校验。evidence digest、wire hash 与 manifest identity binding 也必须逐项相等，replay 不在 raw bytes 与结构化 record 之间任选较方便的一份真相。`cassette@1` 没有 route/attempt 语义时，verification policy 固定把 source call 映射为 `attempt_no=1,call_ordinal=source_call_ordinal,route_ordinal=1`；legacy transport retries 仍只是同一 route 的 transport metadata，不能扩写成 fallback route。

  manifest 不再接受任意 policy/schema map：`policy_bindings` 按唯一 `binding_key` 排序，逐项携带 policy kind/id/version/digest；`schema_bindings` 按唯一 `binding_key` 排序并携带 exact schema ID。manifest 的 `verification_policy` 必须从历史 registry 精确解析，且每个 call 的 `import_routing_decision.verification_policy` 与它逐字段相等。所选 import verification policy + exact RunKindDefinition/Profile schema 冻结两组**完整必需 binding-key 集**，导入器只能逐项填满，不能多键、少键或用 current alias补值；input/profile bindings 分别按唯一 `binding_key` / `field_path` 排序，verified 时 key 集必须分别等于 policy 的 required input/profile 集，input artifact ID 也不得重复。0-call import 仍必须用 manifest-level ref 校验四组 required keys 与 limits；所有数组 canonical 后才参与 manifest digest。

  `verification_status=verified` 当且仅当 `missing_fields=[]`，call 的可选 request/decision/invocation 与 manifest 的 version tuple/execution identity 全部非空且闭合；imported invocation 固定 `response_consumed=true,transport_attempt=null`，source call ordinal 为正并与 synthetic tuple `(1,source_call_ordinal,1)` 相等。manifest identity 必须 `scope=run`，bindings 与 ordered call evidence invocation 逐对象相等，projection/digest 全部重算相等。`evidence_missing` 必须有非空、stable-unique 的 JSON Pointer `missing_fields`，无法证明的上述可选字段保持 null，禁止猜值；run manifest status 只要任一 call 不 verified 就必须是 evidence_missing。证据缺失可诚实持久化但不得进入 M4 Run；摘要不符、重复/额外 shard、key 集冲突、raw 与 parsed record 不一致或其他结构矛盾是 `IntegrityViolation`，必须拒绝发布 import bundle，不能伪装成 evidence_missing。

  REPLAY 只以 run bundle 递归展平后的顺序为权威，MISS fail-closed，绝不在线补录；attempt/run child 顺序、ID、execution identity 与 lineage 不一致即 `IntegrityViolation`。REPLAY 成本/bench 使用版本化 reader 产生的 observation；unknown 不补 0/false。raw response/legacy 原始 wire 仅存受控 shard，不进入普通日志/API。

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
| Artifact/ObjectRef + M4 ArtifactKind + VersionTuple producer matrix | M4 | 对象 GC、审批 subject、持久 Run 结果、跨存储恢复与任意产物追溯的共同地基 |

一切**子系统内部**（检查算法、仿真内部、playtest 内部、前端组件、物理存储选型）——不在本文件，各里程碑 plan 现做。
