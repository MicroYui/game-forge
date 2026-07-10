# Economy Sink Adapter — 设计（pre-M4 收尾增量 A）

> 状态：设计已用户确认（2026-07-10）。这是进入 M4 前两个"诚实延后项"中的 **A**。
> **B**（M3b 真实缺陷语料 / Flare 适配器扩展）另立 spec，取决于一次 Flare 历史侦察的 ROI 结论。

## §0 背景与动机

M0b/M1 遗留、并在 M2 复现为一个具体缺陷的**适配器丢数据缺口**：

- `EconomyModel.from_snapshot`（`gameforge/spine/sim/economy.py:82-101`）从 `SHOP --SELLS--> ITEM` 关系的 **relation attrs** 读金币回收口（sink）的 `price`/`currency`/`buy_prob`；`price is None` 的 SELLS 关系一律跳过。
- 但 `AureusCsvAdapter`（`gameforge/spine/ingestion/aureus_adapter.py:251-258`）派生 SELLS 关系时**不传 `attrs=`**，于是 `shops.csv` 的 `entries` JSON 里**本就存在**的 `price`/`currency` 被丢弃。结果 `model.sinks` **恒为空**：仿真只有 faucet、没有 sink。

**下游后果**（M2 已记录、诚实计入分母的硬 case）：`economy_collapse` 缺陷场景**结构性不可修**，卡住 Fix Pass Rate 在 9/10。原因链：collapse 判据是"净流入长期为正、越过 8× 基线"，在**无 sink** 时轨迹对 faucet 是纯线性 → 判据 **scale-invariant**（等比缩 faucet 也等比缩基线，永远越线）；唯一真实解法是一个够大的 sink，而 sink 数据被适配器丢掉了。

**为什么现在做**：这不是"接口已定、延后实现"那类合规延后项，而是**一处实打实的丢数据 bug**，配置里数据都在、修起来极小、且直接解锁一个诚实指标。放到 M4 之后没有任何收益。

## §1 目标与非目标

**目标**
1. 让 `AureusCsvAdapter` 把 `price`/`currency`/`buy_prob` plumb 进派生的 SELLS 关系 attrs，使 `EconomyModel` 能从真实 CSV 建模金币回收口。
2. 把 sink 的**可配置 schema 补全**（现在定全，不砍字段）：shop 条目可带可选 `buy_prob`。
3. 诚实地把 `economy_collapse` 修复推成"真能修"——靠合法的经济再平衡（把净流入压到 ≤0），Fix Pass Rate 落多少报多少。

**非目标（本 spec 明确排除）**
- Flare 适配器扩展（quests/loot-tables/shops/campaign）、M3b 真实缺陷语料、`ExternalReport` 真实缺陷字段填充 —— 全属 **B**，另立 spec，取决于 Flare 历史侦察 ROI。
- 改动 sim 主循环语义（可负担门槛、每 sink 每 tick 单次购买上限）——**不动**。
- 篡改 `economy_collapse` 场景使其人为变易——**红线，绝不**。

## §2 硬规则遵循

- **不简化，只延后**（硬规则 1）：sink schema 现在定全（含 `buy_prob`），只在某条配置缺省时才对**值**兜底默认。B 是延后**实现**、接口不砍。
- **依赖方向单向**（硬规则 4）：改动落在 `spine`（adapter + sim，无变更）与 `agents`（repair prompt + cassette）。无 `spine → agents`；`spine` 不 import 任何 LLM SDK。7 条 import-linter 契约保持。
- **确定性优先**（硬规则 3）：对/错判定仍由经济仿真给出；LLM 只起草修复 patch，由确定性 verifier 兜底。
- **可复现只承诺回放**（硬规则 5）：若需重录,走 `model_snapshot` + cassette RECORD/REPLAY；CI 零实网（实网仅 `GAMEFORGE_LLM_LIVE=1`）。
- **TDD 全程**（硬规则 6）：sink 因果生效用差分测试证明（平衡→不崩 vs faucet≫sink→崩），照搬 M3a"measured no-op"教训。

## §3 已排除的最大风险（设计前核实，非假设）

**plumb SELLS relation attrs 会不会改 Aureus 玩法 / state_hash，从而打破 M2b playtest 逐字节重放？——不会。**

- Aureus 内核的商店定价来自 `WorldConfig.shops[].entries`（`gameforge/game/aureus/economy.py:24` `cost = entry.price`）。
- `WorldConfig` 由 `snapshot_to_world`（`gameforge/apps/cli/ir_to_world.py:168-172`）构建，商店条目取自**商店实体的 `attrs["entries"]`**（`ShopEntry(**entry)`），**完全不读派生的 SELLS 关系**。
- 因此 SELLS relation 的 attrs 对 `snapshot_to_world` / 内核 / 玩法 / `state_hash` **惰性**。SELLS relation 仅被 `EconomyModel.from_snapshot`（sim）与 `GraphChecker` 消费。
- 结论：M2b 的 `memory=None` 逐字节重放（5792/13320 cassettes）**不受影响**——但仍作为强制回归锁验证（§6）。

## §4 组件设计

### §4.1 契约：sink 可配置 schema 补全

- **`gameforge/contracts/world.py` `ShopEntry`**：新增可选字段 `buy_prob: float | None = None`（`item`/`price`/`currency` 不变）。**必须加**——`snapshot_to_world` 用 `ShopEntry(**entry)`，pydantic 默认禁额外字段，若 `entries` JSON 带 `buy_prob` 而模型没有该字段会**抛异常**。内核 buy/sell **忽略** `buy_prob`（它是仿真侧"模拟玩家购买概率"的概念，非内核玩法概念），只是把它随条目携带给 sim。
- **CSV shop 条目 schema**：`shops.csv` 的 `entries` JSON 条目允许可选 `buy_prob`。相应更新 `format_schema.json`（或 shop sheet schema 落点）与文档。
- **无损性**：`entries` JSON 逐字存进 shop 实体 `attrs`（往返真相源，`from_ir` 从 `attrs` 重建），故此扩展纯增量、`from_ir(to_ir(x)) == x` 仍成立。

### §4.2 适配器 plumb（核心修复）

`gameforge/spine/ingestion/aureus_adapter.py:251-258`：派生 SELLS 关系时传入 `attrs`，仅塞入该条目里**出现的**键：

- `price`（存在则塞；缺则不塞 → `from_snapshot` 的 `price is None` 跳过语义保持干净：只陈列不建模的商店不产生 sink）。
- `currency`（存在则塞，默认沿用 `from_snapshot` 的 `default_currency`）。
- `buy_prob`（存在则塞，缺则由 sim 默认 0.5）。

**对称审计 M0a YAML 加载器**：若 caravan.yaml 路径也派生 SELLS 且同样丢 attrs，对称修掉；若不派生 SELLS 则无需动（实现时核实）。

`gameforge/spine/sim/economy.py:82-101` **无需改**——本就读这些 attrs，现在有真数据了。

### §4.3 修复层启用（agents）

- **prompt**（`gameforge/agents/prompts/library.py` `_REPAIR`，bump `prompt_version`）：告知 agent——现在经济里存在真实 sink（在 finding evidence 的 `sinks` 里带 price/buy_prob）；修 runaway faucet = 把**净流入压到 ≤0**,两条合法路径：
  1. `set_entity_attr` 把 faucet 的 `gold_min`/`gold_max` 降到 **sink 排放之下**（现有 prompt 已引导降 gold，只是"多小"现在有了具体锚点）；
  2. `set_relation_attr` 抬高某条真实 SELLS sink 的 `price`/`buy_prob` 去吸收 faucet。
  （`set_relation_attr`/`set_entity_attr` 等**全套 patch op 已存在** `gameforge/spine/patch.py`，无需新增接口。）
- **`to_findings` evidence**（`gameforge/spine/sim/economy.py` `to_findings`）：`sinks` 列表现在是非空真数据（它本就想填，plumb 后就有）。无需改结构,只是数据变实。
- **verifier 无需改**：`gameforge/agents/repair/verify.py` 已扫 `simulation_findings` 判 `target_resolved`；净流入≤0 → 无 collapse finding → 通过。

## §5 可修性流程（经验、诚实、无作弊）

collapse ⟺ 净流入长期为正并越过 8× 基线；**任何**正净流入迟早越线（见 §0），故消解**必须**令 `E[faucet income/tick] ≤ E[sink drain/tick]`。当前场景数字：faucet ≈750/tick（wolf gold 500..1000 × kills 1）vs 默认 sink ≈25/tick（price50 × buy_prob0.5）。合法解法之一：把 wolf gold 降到 ≲sink 排放（注入的 500-1000 本就是缺陷值，合理值就是小值）。

**执行顺序：**
1. §4.1/§4.2 plumb + §6 sim 测试全绿。
2. **先免费重放**已录 `economy_collapse` 修复 cassette 打修后 sim：已录 patch 是否已使净流入≤0？
   - **过** → Fix Pass Rate 9→10 **免费**,不重录。
   - **不过** → 更新 §4.3 prompt + **一次实网 RECORD**（`record_router` 的 `resume=True` + 指数退避 `max_retries=8, backoff≈3.0`，穿越不稳定网关，M2b 已验证）+ 重测。
3. Fix Pass Rate **落多少报多少**。**红线**：`economy_collapse` 场景 CSV 保持原样,faucet 不动;只提升 agent 能力(靠更好引导)。若最终仍不过,诚实维持并记录原因。

## §6 测试与回归（TDD，test-first）

**新增测试**
- **适配器**（`tests/spine/ingestion/`）：SELLS 关系现在带 `price`/`currency`/`buy_prob`;带 `buy_prob` 的条目能 plumb、不带的不塞该键;`from_ir(to_ir(workbook)) == workbook` 仍逐字无损（锁往返，因关系不被读回重建）。
- **CSV→sim**（`tests/spine/sim/test_economy.py`）：CSV 派生模型 `sinks` 非空;**平衡** faucet+sink → **不** collapse;**faucet≫sink** → **仍** collapse（差分,证明 sink 因果生效,反"measured no-op"）。
- **`ShopEntry` 契约**：带 `buy_prob` 的 entries JSON 能被 `snapshot_to_world` 的 `ShopEntry(**entry)` 接受（不抛）;内核 buy/sell 忽略 `buy_prob`、行为不变。
- **修复（REPLAY）**：`economy_collapse` 修复产出净流入≤0 的 patch、过 verifier;重测 Fix Pass Rate。

**回归锁（必须保持绿）**
- `test_clean_baseline_has_zero_oracle_false_positives`：oracle-FP=0 不变（sink 属 simulation 桶,不碰确定性桶）;并断言 clean **不**新增假 `economy_collapse`（clean 有 sink 无 faucet → 无净流入 → 无 collapse）。
- **M2 part2 验收**（`tests/agents/test_part2_acceptance.py`）：`attempted == 10`、`fix_pass_rate ≥ 0.70`（理想更高;若达 10/10 相应更新 README/CLAUDE.md/记忆）。
- **M2b playtest 逐字节重放**：`memory=None` 重放 M2b-1 cassettes 复现原完成率、零 `CassetteReplayMiss`（验证 §3 结论）。
- **M3a bench**：`economy_collapse` seeded BDR=1.0 仍成立（注入器 `inject.py:451` 按构造 faucet≫sink，任何 sink 不可抵消 → 仍崩 → 仍检出）;clean bench 行为不变。
- **契约/风格/零实网**：7 条 import-linter 契约、ruff clean、CI 零实网（实网仅 `GAMEFORGE_LLM_LIVE=1` 下的重录）。

## §7 验收标准

1. `AureusCsvAdapter` 派生的 SELLS 关系携带 `price`/`currency`/`buy_prob`（存在即塞）;`from_ir(to_ir)` 逐字无损回归通过。
2. CSV 派生 `EconomyModel.sinks` 非空;平衡 faucet+sink 不崩、faucet≫sink 仍崩(差分测试锁 sink 因果)。
3. `ShopEntry` 带可选 `buy_prob`;`snapshot_to_world` 接受带 `buy_prob` 的 entries、内核玩法/`state_hash` 不变。
4. `economy_collapse` 可修性**经验落定**并诚实报告:或 9→10(免费重放/重录后),或诚实维持并记录原因;`attempted==10` 不变。
5. 全部 §6 回归锁绿;7 契约/ruff/零实网(除显式重录)保持。
6. 若达 10/10:同步更新 README、CLAUDE.md 里程碑表注、记忆 `gameforge-milestone-progress`(economy_collapse 硬 case 的结论改写)。

## §8 依赖与顺序（供 writing-plans 用）

大致 TDD 任务序（细化留 writing-plans）：
1. `ShopEntry.buy_prob` 契约字段 + 测试(带 buy_prob 的 entries 可构建、内核忽略)。
2. 适配器 plumb SELLS attrs + 往返无损/attrs 存在性测试 + M0a YAML 对称审计。
3. CSV→sim sink 差分测试(平衡不崩/faucet≫sink 崩)。
4. `to_findings` sinks 非空验证 + repair prompt 更新(bump prompt_version)。
5. 免费重放 economy_collapse → 判定是否需重录;需则实网 RECORD + 重测。
6. 全回归锁 + 文档/记忆更新(若 10/10)。
