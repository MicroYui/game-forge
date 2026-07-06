# GameForge M0b — 地基补全（Foundations Completion）设计文档

| 字段 | 值 |
|---|---|
| 状态 | Draft v1（brainstorming 产出，待用户确认） |
| 日期 | 2026-07-06 |
| 里程碑 | **M0b** |
| 分支 | `m0b-foundations`（自 `m0a-vertical-slice` 切出） |
| 关联 | PRD `2026-07-03-gameforge-prd.md` §7.7/§8/§12A / 地基契约 `2026-07-03-gameforge-foundations-contracts.md` §2/§5 / M0a 计划 |

> **原则遵守**：不简化只延后（接口一次定全）；企业级=生产级工程成熟度；确定性优先；依赖方向单向（CI lint 强制）；可复现只承诺回放（seed 化）；TDD 全程。本文件是 M0b 的**设计（架构/边界/决策）**；task 级 TDD 步骤在随后的 writing-plans 产出。

---

## 0. 目标与验收（锁定，来自 spec）

**主题**：把确定性地基补全——让 Aureus 成为由 IR 导出配置完整驱动的四大系统真实游戏，加上无损往返的 Schema Registry + Aureus 适配器、版本/血缘/审计骨架、DB 迁移框架；全程保持 M0a 垂直链绿色且确定。

**验收标准（PRD §14/§16 + 契约 §2 锚点 + §5 锚点）**：
1. **Aureus 配置 ↔ IR 往返无损**：`diff(import(export(x)), x) == ∅`（字段级；含 `source_ref` sheet/row/column）。
2. **四大系统由配置驱动运行**：任务 / 战斗 / 经济(背包+商店+掉落) / 抽卡，全部从 IR 导出的配置驱动，无硬编码内容。
3. **确定性可复现**：相同 `(scenario, seed, action 序列)` → 相同逐 tick `state_hash`（现覆盖战斗/经济/抽卡的 rng 与 monster 状态）。
4. **版本/血缘/审计**：任意工件可回溯全部来源元组（§5 扩展 10 元组）；回滚后血缘可追；审计 append-only WORM + 内容哈希链。
5. **DB 迁移框架**：Alembic 式迁移，CI 内前向 `upgrade head` + 回滚 `downgrade base` 双测通过。
6. **M0a 衔接**：M0a 垂直链（caravan：talk→collect→turn_in）回归保持绿色且确定。

---

## 1. 本次落地的契约元素（M0a 已声明，M0b 实现）

| 契约 | 元素 | M0b 动作 |
|---|---|---|
| §2.2 节点 | `Equipment, Skill, StatusEffect, Effect, BattleEncounter, Formula`（enum 已声明） | 由 loader/adapter **产出**、Aureus **消费** |
| §2.3 边 | `uses_skill, applies_effect, has_stat_curve` + 经济边 `drops_from/grants/consumes/rewards/sells` | 实际建边 |
| §2 查询/序列化 | 往返锚点 `diff(import(export(x)),x)==∅` | Schema Registry + Aureus 适配器实现 |
| §4.1 原子动作 | `attack, cast_skill, use, equip, buy, sell`（M0a 答 `unsupported_in_m0a`） | Aureus 内核**实现** |
| §5 版本/血缘/审计 | 扩展 10 元组、Artifact+lineage DAG、回滚、WORM 审计 | contracts 定 schema + spine 纯逻辑 + platform 持久化 |
| §12A.3 | DB 迁移框架、元 schema/DSL 文法版本治理 | runtime 持久化 + Alembic |

---

## 2. 关键决策（best-judgment；用户不在时按推荐项推进，可回改）

| # | 决策 | 选择 | 理由 / 可回改性 |
|---|---|---|---|
| D1 | 持久化栈 | **SQLAlchemy 2.0 + Alembic，SQLite 默认，Postgres-ready（`DATABASE_URL`）** | 满足 DB 迁移框架 + §5 持久化；零基础设施、确定性 CI；DB-agnostic schema，PRD §12 的 Postgres 仅换连接串。|
| D2 | Aureus 配置格式 | **规范化 CSV 多表 + 注册 schema**；Schema Registry **格式可插拔**（`Adapter` Protocol） | 最贴近 Luban/Excel 风格表；`source_ref` sheet/row/column 有意义；纯 stdlib、可 diff、确定。**改用 .xlsx/YAML 只是新增一个 Adapter，不返工。** |
| D3 | 分支/衔接 | `m0b-foundations` 自 `m0a-vertical-slice` 切出 | 隔离 M0b，保留 M0a 分支为验收基线；master 合并留给用户。|

> D1–D3 是「怎么做」的选择，不是「是否砍」；无论选哪个，**接口一次定全**。用户回来后若倾向不同（尤其 D2 想要真 .xlsx），只需增/换适配器实现。

---

## 3. 依赖方向的硬约束如何决定包归属（最关键的架构推理）

契约 §1 依赖表里有一条决定性约束：**`spine → contracts`（仅此一项）**。这意味着**任何需要持久化（依赖 `runtime`）的东西都不能放进 `spine`**。据此拆分版本/血缘/审计：

```
contracts/                         # 唯一 schema 真相；不依赖任何业务包
  lineage.py    ← VersionTuple / Artifact / ArtifactKind / AuditRecord（纯 schema）

runtime/                           # 底层能力（无业务逻辑）；runtime → contracts
  persistence/  ← SQLAlchemy engine/session 工厂（读 config/env → DATABASE_URL，默认 sqlite）
                  + Alembic env/config + versions/（迁移脚本）

spine/                             # 确定性可信主干；spine → contracts 仅此一项（不碰 runtime！）
  ingestion/    ← Schema Registry + Aureus 适配器（纯确定性，无 LLM，无 DB）
      schema_registry.py  format_schema.py  csv_format.py  aureus_adapter.py
  versioning/   ← 纯血缘逻辑：内存 ArtifactStore / LineageGraph.ancestors() /
                  RefStore 回滚指针 / VersionTuple 构造（contracts-only，可全内存测试）

platform/                          # 产品平台层；platform → contracts + spine + runtime
  lineage/      ← SQLAlchemy 持久化仓储：ArtifactRepository / LineageRepository
                  （内容寻址 put/get、add_lineage、ancestors、rollback 指针）
  audit/        ← append-only WORM 审计日志（内容哈希链；禁 update/delete）

apps/cli/                          # 组合层：把上述接到 run_slice（衔接 + 演示）
```

- **端口-适配器**：`spine/versioning` 是确定性领域核心（内存、纯 contracts），`platform/lineage`+`platform/audit` 是持久化适配器（SQLAlchemy over `runtime/persistence`），二者**接口同构**（platform 仓储镜像 spine 内存 store 的方法签名）。既满足 §1 依赖方向，又给出真实 DB 落地（企业级）。
- **Schema Registry 归 `spine/ingestion`**：它产出 IR、必须 LLM-free、且只依赖 contracts+spine.ir → 属确定性主干。§6.1 架构图把「Ingestion & Schema Registry」画在基础设施带只是逻辑分层，物理归属按依赖方向落在 spine。
- **审计归 `platform/audit`**：契约 §1 布局显式 `platform/ …/audit/…`。

**import-linter 新增契约**（CI 强制）：`spine/ingestion` 不得 import `runtime.*`/`agents.*`/`platform.*`/LLM SDK；`platform` 新子包遵守 `platform → contracts+spine+runtime`；`runtime/persistence` 不得 import `agents.*`。新增负测（往 `spine.ingestion` 注入 `import anthropic` 应 trip）。

---

## 4. WS1 — Aureus 四大系统（config-driven + 确定性）

### 4.1 IR 扩展（loader/adapter 产出、Aureus 消费）
用 §2.2 combat-economy 节点 + §2.3 边表达四大系统的内容：
- `Monster`：stats（hp/atk/def/spd）、技能列表（`USES_SKILL`）、掉落（`DROPS_FROM`→`DropTable`）。
- `BattleEncounter`：敌人组合/等级/波次/胜负条件/奖励（替代裸 enemy_group；边 `TRIGGERED_BY`/`REWARDS`）。
- `Skill`：cost/power/target/命中，`APPLIES_EFFECT`→`Effect`/`StatusEffect`。
- `StatusEffect`/`Effect`：buff/debuff/dot（duration/magnitude）。
- `Formula`：数值公式/曲线（damage/curve，供 M1 SMT；M0b 由内核求值），边 `HAS_STAT_CURVE`。
- `Equipment`：装备槽 + stat 修饰（`GRANTS` stat）。
- `DropTable`：`{item, probability}` 多重边（Monster/关卡→Item，概率在 relation.attrs，支持 §2.1 多重边）。
- `Shop`：`SELLS`→Item（价格在 attrs）。
- `GachaPool`：条目 + 权重 + 保底（pity）参数；`GATED_BY`→`UnlockCondition`。
- `Currency`：货币定义（gold 等）。

### 4.2 WorldConfig 扩展（**加法式**，M0a 字段不动）
`contracts/world.py` 新增（现有 grid/placements/quests 保持）：
- `MonsterSpec`（stats/skills/drop_table_id）、`SkillSpec`、`EffectSpec`/`StatusEffectSpec`、`BattleEncounterSpec`、`FormulaSpec`、`EquipmentSpec`、`DropTableSpec`（entries: `[{item,probability}]`）、`ShopSpec`（entries: `[{item,price}]`）、`GachaPoolSpec`（entries: `[{item,weight}]` + pity 参数）、`CurrencySpec`。
- `QuestStepKind` 扩展 `Literal["talk","collect","turn_in","fight"]`（加法：新增 `fight` 战斗目标步）。`QuestStepSpec` 增 `encounter: str | None`（fight 步指向 BattleEncounter）。
- `WorldConfig` 增可选列表字段（默认空），M0a 配置仍合法。

### 4.3 Aureus 内核实现（`game/aureus/`）
tick-based、seed 化、headless 权威逻辑；新增 `combat.py`/`economy.py`/`gacha.py` 系统模块，`kernel.py` dispatch：
- **战斗**：`attack(target_id)` → 由 `Formula` 求伤害（如 `max(1, atk*k - def)`）、命中/闪避走 seeded rng；`cast_skill(skill_id,target_id)` → 扣资源、施伤 + 挂 `StatusEffect`。怪物 AI：确定性策略（每 tick 对玩家行动）。StatusEffect 每 tick 结算（dot/buff 衰减）。`BattleEncounter` 胜利 → 结算奖励 + `DropTable` 掷点（seeded）；失败 → 定义化结果。
- **经济/背包**：`buy(shop_id,item_id,count)` 校验货币→扣款加物；`sell` 反向；`use(item_id,target)` 消耗（如药水回血）；`equip(item_id)` 入装备槽并应用 stat 修饰；`pickup` 沿用。掉落表在怪物阵亡时 seeded 掷点。
- **抽卡**：**gacha pull 映射到 `buy` 原子**（契约 §4.2：gacha 由 planner 编译，不改 Env 契约）——内核识别 `shop_id ∈ GachaPool` → 按权重 + 保底计数（pity 计入玩家状态 → 进 state_hash）做 seeded 掷点。
- **Observation 补全**：`equipped_items`/`active_effects`/`player_stats`(atk/def/hp/gold)/附近怪物/`available_interactions` 全部真实填充（M0a 空占位转实值）。
- **state_hash（§4.4）扩展**：新增 `monster_states`(hp/alive/pos)、`combat`(当前 encounter/回合)、`equipped`、`active_effects`、`gacha_pity`、真实 `rng`（seed + draw 计数 / getstate 哈希）。**排除** logs/render/wall-clock。→ 战斗/抽卡的伪随机**逐 tick 可复现**（确定性卖点）。

### 4.4 衔接注意（M0a 行为变化，属预期）
- M0a 测试 `test_unsupported_combat_action_is_declared_not_crashing`：`attack` 不再返回 `unsupported_in_m0a` → **更新该测试**为新战斗语义（无有效目标时返回定义化结果如 `no_target`；有目标则结算）。
- state_hash schema 增大 → M0a 具体哈希值变化；但 M0a 测试断言的是「重放相等」而非具体值，故仍绿。
- Env `Action` union 已含全部原子（M0a 定义），无需改契约。

---

## 5. WS2 — Schema Registry + Aureus 适配器（往返无损）

### 5.1 `spine/ingestion`（确定性、contracts+spine.ir、无 LLM/DB）
- `format_schema.py`：`ColumnSchema{name,type,required,enum?,foreign_key?}`、`SheetSchema{name, columns, primary_key}`、`FormatSchema{format_id, version, sheets}`。
- `schema_registry.py`：`SchemaRegistry.register(FormatSchema)` / `get(format_id,version)`；`validate(rows)` 做类型/外键/枚举**schema 级校验**（把语法层错误挡在 IR 之外，返回结构化 `SchemaError`，不进 IR）。
- `csv_format.py`：规范化 CSV 多表读写（stdlib `csv`，确定性列序/行序）。
- `adapter.py`：`Adapter` Protocol —— `to_ir(source) -> Snapshot` / `from_ir(snapshot) -> workbook`。`aureus_adapter.py`：`AureusCsvAdapter` 具体实现（格式可插拔，D2 可回改）。
  - `to_ir`：每行 → 类型化 Entity/Relation，带 `source_ref{adapter,file,sheet,row,column?}`。
  - `from_ir`：从 IR 反向重建各 sheet 行（用 `source_ref` 复位到原 sheet/row/column，**保留原始字段**）→ 无损。

### 5.2 Aureus CSV 配置（四大系统全表）
一个规范化多表工作簿（目录内 .csv + 一份 `FormatSchema` 定义），sheets 覆盖：`regions, grid, npcs, items, currencies, interactables, spawn_points, quests, quest_steps, monsters, skills, effects, status_effects, battle_encounters, formulas, equipment, drop_tables, shops, gacha_pools`。
- 嵌套/列表规范化为独立 sheet（如 `quest_steps` 带 `quest_id` FK + `order` 列；`drop_table_entries` 带 `drop_table_id` FK）——这正是 Luban/studio 的真实做法，使 round-trip 字段级、`source_ref` 列级有意义。
- 新增 **M0b 验收场景**（如 `outpost`）：`talk → collect → fight → (buy/gacha) → turn_in`，四大系统全触及，由（扩展的）ScriptedDriver 驱动、确定性跑通。

### 5.3 往返锚点（M0b 头号验收）
`to_ir(cfg)=snapA`；`from_ir(snapA)=cfg'`；`to_ir(cfg')=snapB`；断言 `snapA.diff(snapB).is_empty()` 且字段级 `cfg==cfg'`。**property-based（hypothesis）**：随机生成合法 Aureus 配置 → 往返 diff=∅（§12A.1 适配器 round-trip property test）。

### 5.4 衔接
M0a 的 `spine/ir/loader.py`（YAML）+ `caravan.yaml` + `run_slice` 保持不动、回归绿色（M0a 垂直链）；M0b 的 CSV 适配器为新增主路径。两条 ingestion 路径并存。

---

## 6. WS3 — 版本/血缘/审计骨架（契约 §5）

### 6.1 contracts/lineage.py（纯 schema）
- `VersionTuple`：**全 10 元素**——`doc_version, ir_snapshot_id, constraint_snapshot_id, prompt_version, model_snapshot, agent_graph_version, tool_version, env_contract_version, seed, cassette_id?`（M0b 未涉及的元素允许为占位/None，但**字段一次定全**，不裁剪）。
- `ArtifactKind`：`ir_snapshot | config_export | checker_run | playtest_trace | patch`。
- `Artifact`：`artifact_id（内容寻址 hash）, kind, version_tuple, lineage:[parent_artifact_id...], created_at, meta`。
- `AuditRecord`：`actor, action, artifact_id, ts, content_hash, prev_hash`（哈希链）。

### 6.2 spine/versioning（纯逻辑，contracts-only，全内存可测）
- `ArtifactStore`（内存，内容寻址 put/get）、`LineageGraph.ancestors(artifact_id)`（DAG 回溯全部来源）、`RefStore`（name→artifact_id 指针；**回滚=指针重指历史工件**，工件不可变不删除）、`build_version_tuple(...)`（盖当前运行 provenance）。

### 6.3 platform/lineage + platform/audit（SQLAlchemy 持久化）
- `platform/lineage/store.py`：`ArtifactRepository`/`LineageRepository`（内容寻址、add_lineage、ancestors、rollback 指针），接口镜像 spine 内存 store，落 `runtime/persistence`。
- `platform/audit/log.py`：append-only WORM 审计（**禁 update/delete**；`content_hash=hash(record)`、`prev_hash` 链接前一条 → 篡改可检）。

### 6.4 衔接/演示（接入 run_slice）
`run_slice` 运行时**记录工件**：ir_snapshot、config_export、checker_run 各成 Artifact，连 lineage（config_export←ir_snapshot←...），并写审计条目。锚点测试：任意工件回溯完整 VersionTuple；`RefStore` 回滚后 lineage 仍可追。

---

## 7. WS4 — DB 迁移框架（§12A.3）

- `runtime/persistence/engine.py`：`get_engine()`/`session_factory()`，从 `runtime/config` 或 env 读 `DATABASE_URL`（默认 `sqlite:///.../gameforge.db` 或内存 sqlite for 测试）。SQLAlchemy 2.0 `MetaData`/`Table` 定义 artifacts/lineage/audit/refs 表（DB-agnostic 类型）。
- Alembic：`env.py` 绑定该 engine + metadata；`versions/0001_*.py` 首个迁移创建全部表。
- **元 schema / DSL 文法版本治理**：IR 快照已声明 `meta_schema_version`；新增 `MetaSchemaVersion`/`DslGrammarVersion` 常量登记 + 迁移路径文档（M0b combat-economy 类型是**加法**，前向兼容；不可自动迁移的标 "需重抽取/重编译"）。
- **CI 双测**：`alembic upgrade head`（前向）+ `alembic downgrade base`（回滚）均成功（§12A.3）。

---

## 8. 测试策略（TDD 全程，§12A.1 两条独立质量线）

| 面 | 测试 |
|---|---|
| 往返 | hypothesis property：`diff(import(export(x)),x)==∅`（字段级 + snapshot diff）；Schema Registry 类型/FK/枚举校验 |
| Aureus 确定性 | 相同 (scenario,seed,actions) → 逐 tick state_hash 相等（含战斗/经济/抽卡 rng、monster 状态）；战斗/掉落/抽卡公式单测；怪物 AI 确定性 |
| 版本/血缘/审计 | 工件回溯完整 10 元组；回滚指针后 lineage 可追；审计 append-only（update/delete 被拒）+ 哈希链校验 |
| DB 迁移 | `alembic upgrade head` + `downgrade base` 前向/回滚双测 |
| 依赖 lint | 扩展 import-linter：新增包遵守方向；`spine/ingestion` LLM-free & runtime-free；负测注入 `import anthropic` trip |
| M0a 回归 | caravan 垂直链保持绿色 + 确定；更新 `attack` 语义测试 |

---

## 9. 交付物清单（新增/修改文件，供 writing-plans 展开）

**contracts**：`world.py`(扩展)、`lineage.py`(新)。
**runtime**：`persistence/engine.py`、`persistence/models.py`、`alembic.ini`+`persistence/migrations/env.py`+`versions/0001_*.py`。
**spine**：`ingestion/{format_schema,schema_registry,csv_format,adapter,aureus_adapter}.py`、`versioning/{store,lineage,version_tuple}.py`。
**game/aureus**：`combat.py`、`economy.py`、`gacha.py`、`world.py`(扩展)、`kernel.py`(扩展 dispatch + state_hash)。
**platform**：`lineage/store.py`、`audit/log.py`。
**apps/cli**：`ir_to_world.py`(扩展 combat-economy)、`driver.py`(扩展 fight/buy/gacha 宏)、`run_slice.py`(接入工件记录)、新 CSV 场景 runner。
**scenarios**：`outpost/`（CSV 多表四系统场景）+ 其 `FormatSchema`。
**pyproject.toml**：加 `sqlalchemy>=2.0`、`alembic>=1.13`；扩展 import-linter 契约。
**tests**：镜像上述，全部 test-first。
**docs**：更新 `CLAUDE.md` 里程碑表 M0b→🔄/✅；`plans/` 增 M0b 计划。

---

## 10. 风险 / 未决

- **R-M0b-1（确定性 rng）**：战斗/抽卡引入大量 rng draw，必须保证 draw 顺序确定且计入 state_hash，否则回放漂移。缓解：单一 seeded RNG + draw 计数纳入 hash；逐 tick 重放测试为门禁。
- **R-M0b-2（往返无损的边界）**：浮点概率/权重、空值、原始列顺序易破坏字段级相等。缓解：canonical 浮点表示（M0a 已有 `f:` 规则）；`source_ref` 保原列；property test 广覆盖。
- **R-M0b-3（scope 体量）**：四系统 + 适配器 + 持久化 + 迁移量大。缓解：按 WS1→WS2→WS3→WS4 顺序、每步 TDD、M0a 回归为持续护栏。
- **未决（待用户回来确认 D1–D3）**：尤其 D2（配置格式）；已用 `Adapter` Protocol 令其可插拔以降低回改成本。
