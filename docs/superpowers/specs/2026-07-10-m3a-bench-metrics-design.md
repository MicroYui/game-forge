# M3a — GameForge-Bench (Seeded ≥500) + 指标聚合引擎 设计文档（Design Spec）

> M3 拆分为 **M3a**（本文：seeded 语料 ≥500 + 指标聚合，确定性为主）→ **M3b**（外部开源真实语料 Flare + 交叉验证）→ **M3c**（Eval 面板：指标后端 + 结构化报告 + 最小视图，完整 React UI 留 M4）。每块各自 spec→plan→实现 循环。用户已确认（2026-07-10）：M3a→M3b→M3c 顺序；确定性指标跑满 500 + agent 指标跑有界子集；Eval 面板=后端+报告+最小视图；外部语料复用 Flare。

## 0. 定位与验收切分

**M3a 目标**：把 M1 手写的 9 个单缺陷场景，泛化为**程序化、seeded、可复现的 ≥500 样本缺陷语料**，并建一个**指标聚合引擎**，按缺陷类报告 BDR + 误报率 + 修复/agent 指标，全部带置信区间、确定性与 llm-assisted **严格分开**。这是"游戏内容界 SWE-bench"的自建（seeded）半边；外部效度（打破自证循环）由 M3b 的 Flare 真实语料补齐。

**M3a 覆盖的 §14 M3 验收**：
- 分缺陷类 BDR + 误报率报告齐全（seeded 半边）——本 spec 全覆盖。
- 外部语料交叉验证——接口现定（`BenchReport` 有 external 槽），实现落 M3b。
- Eval 面板——接口现定（`BenchReport` JSON 契约 + 最小 CLI/静态视图），完整 UI 落 M3c/M4。

**硬规则复述（不可违背）**：确定性优先——BDR/FP 由检查器/仿真给出，非 LLM 打分；`bench` 属确定性主干，**禁 import 任何 LLM SDK**（依赖 lint 强制，`bench → {contracts, spine}` + agent 指标聚合只读 M2 已录 cassette，不新调网关）；反循环——注入器与检查器**分离代码**，检查器永不读 ground-truth；可复现只承诺回放（seeded 生成确定性；agent 指标走 M2 cassette REPLAY）。

---

## 1. 架构与依赖方向

新增 `gameforge/bench/` 包（确定性，无 LLM），消费 `spine` 的检查器/仿真：

```
gameforge/bench/
  taxonomy.py      # 缺陷分类学枚举（15 类）+ 每类的 oracle/bucket 归属
  inject.py        # 每缺陷类的参数化注入器（IR snapshot 级，seeded）
  corpus.py        # 组装 ≥500 样本 seeded 语料（功效驱动 per-class n）
  metrics.py       # 指标定义 + Wilson-CI 聚合（BDR/FP/... det vs llm-assisted 分离）
  power.py         # 所需 n + 达成 CI 半宽 计算
  agent_metrics.py # 聚合 M2 已录结果（Fix Pass Rate / Playtest / 消融），只读 cassette
  report.py        # BenchReport 结构化契约（JSON 可序列化）+ 最小 CLI/文本视图
  run_bench.py     # 入口：跑 seeded 语料 → BenchReport（确定性）；--agent 附加 agent 指标
```

依赖 lint 新增契约（第 8 项），两条粒度：
- **bench 整体**：`gameforge.bench` 可 import `gameforge.contracts(.*)`、`gameforge.spine(.*)`、`gameforge.game(.*)`、`gameforge.runtime(.*)`、`gameforge.apps.cli.ir_to_world`、以及（仅 `agent_metrics.py`）`gameforge.agents(.*)` 供 REPLAY 聚合；**永不 import 任何 LLM SDK**（openai/anthropic/… 仍由既有第 7 契约锁在 `runtime.model_router.transport`——REPLAY 路径不触发该 import，故 bench 图内无 LLM SDK）。
- **seeded 核心纯度**（更细粒度测试，非 import-linter）：`bench/{inject,corpus,metrics,report,taxonomy,power}.py` **禁 import `gameforge.agents`**——保证 seeded 语料 + 确定性指标管线与 agent 层完全解耦（反循环 + 确定性卖点）。只有 `agent_metrics.py` 这一个桥接模块碰 `gameforge.agents`（且只走 REPLAY，零实调）。

---

## 2. 缺陷分类学（`taxonomy.py`）

`DefectClass` 枚举 + 每类元数据 `{oracle: "graph"|"asp"|"smt"|"sim"|"consistency", bucket: "deterministic"|"simulation"|"llm-assisted"}`。15 类：

| # | class | oracle | bucket |
|---|---|---|---|
| 1 | dangling_reference | graph | deterministic |
| 2 | missing_drop_source | graph/asp | deterministic |
| 3 | unreachable_target | graph(+nav) | deterministic |
| 4 | cyclic_dependency | graph/asp | deterministic |
| 5 | dead_quest | graph | deterministic |
| 6 | unsatisfiable_completion | graph | deterministic |
| 7 | reward_out_of_range | smt | deterministic |
| 8 | prob_sum_ne_1 | smt | deterministic |
| 9 | non_monotonic_curve | smt | deterministic |
| 10 | gacha_expectation_violation | smt | deterministic |
| 11 | economy_collapse | sim | simulation |
| 12 | character_violation | consistency | llm-assisted |
| 13 | spoiler | consistency | llm-assisted |
| 14 | faction_violation | consistency | llm-assisted |
| 15 | uniqueness_violation | consistency | llm-assisted |

`bucket` 决定该类 BDR 从 `ReviewReport` 的哪个分区取（严格分开报告，llm-assisted 永不并入 deterministic 数字）。

---

## 3. 注入器（`inject.py`）——参数化、seeded、单缺陷隔离

**契约**：`inject(base: Snapshot, defect: DefectClass, seed: int) -> InjectedSample`，
`InjectedSample = {snapshot: Snapshot, ground_truth: GroundTruth}`，
`GroundTruth = {defect_class: DefectClass, injected_entities: list[str], note: str}`。

**不变量**（每个注入器都有 property 测试锁）：
- (a) **恰好注入一类缺陷**：注入后该类的独立结构断言成立（不经检查器，直接查图/数值），且 base 上该断言不成立；
- (b) **不误伤他类**：注入不引入其它缺陷类（沿用 M1 教训——cyclic 用自包含子任务避免连带 unsatisfiable）；
- (c) **seeded 确定性**：同 `(base, defect, seed)` → 同 `snapshot_id`。

**每类注入策略**（无占位符，实现即照此）：
1. dangling_reference：选一条关系，dst_id 改指一个全新不存在 id。GT.entities=[relation.id, bad_dst]。
2. missing_drop_source：删掉某 collect-step 目标物品的 GRANTS/DROPS_FROM 源边。GT=[item_id]。
3. unreachable_target：把某目标实体放到网格上与玩家起点无路径的格（注入器同时改 region.grid/positions）。GT=[target_id]。需 nav → 语料样本携带"需建 world"标志。
4. cyclic_dependency：在一段**新增自包含子任务**的 QUEST_STEP 间加 PRECEDES 环。GT=[step ids]。
5. dead_quest：移除某 quest 的 STARTS_AT（无 giver）使其不可启。GT=[quest_id]。
6. unsatisfiable_completion：quest 完成条件引用一个永不可完成的 step。GT=[quest_id]。
7. reward_out_of_range：把某 quest 奖励数值改到约束区间外。GT=[quest_id]。
8. prob_sum_ne_1：把某 drop_table 概率改到和 ≠ 1（精确有理，如 [0.5,0.3]→4/5）。GT=[drop_table_id]。
9. non_monotonic_curve：把某装备功率曲线改为非单调（tier2.power < tier1.power）。GT=[eq tier ids]。
10. gacha_expectation_violation：改 base_rate/pity 使期望违约。GT=[pool_id]。
11. economy_collapse：把某怪 gold_min/gold_max 抬高且无 sink（source≫sink）。GT=[monster_id]（sim 判）。
12–15 narrative：向对话/叙事文本 + 叙事约束注入矛盾（角色设定违反/剧透/阵营/唯一性），产出 `DialogueNarrativeInput` 携样本。GT=[span/entity]，bucket=llm-assisted。

**参数化产多样本**：每类注入器接受 seed → 选不同实体/关系/数值/base，产出**结构上两两不同**的样本（property 测试锁分布多样性，沿用 M2b `scenario_gen` 的 distinctness 手法）。

**base 集**：clean Aureus（`scenarios/defects/clean` + `outpost`）→ 转 IR snapshot 作注入起点；可多 base 增多样性。

---

## 4. 语料组装（`corpus.py`）+ 功效（`power.py`）

- `build_corpus(seed=0, per_class_n: dict[DefectClass,int] | None = None) -> list[InjectedSample]`：对每类调注入器 `n` 次（seed 派生）+ 若干 clean 样本（无注入，供 FP）。默认 `per_class_n` 由 `power.required_n(...)` 按功效目标算。
- **功效目标**（PRD §13.4）：每类 n 使 BDR 95% CI 半宽 ≤ ±5%。`power.required_n(p_hat, half_width=0.05, z=1.96)` 由目标比例反推最小 n。确定性检查器 BDR 期望≈1.0（CI 一侧极窄，n 小即达标）；对 p 未知的类保守用 p=0.5（n≈384）。**总量 ≥500 为 PRD 地板**；报告**如实标注每类达成 CI 半宽**，欠功效类显式标红（把"n=50→±14%"从批评变成一等可见指标）。
- **成本**：seeded 语料全确定性（无 LLM），跑满 ≥500 廉价。narrative 类的 llm-assisted 检出走 M2 consistency（cassette REPLAY，有界，见 §6/§9）。

---

## 5. 指标引擎（`metrics.py`）

对每样本跑 `spine` 检查器/仿真管线（复用 `run_review` 的组装：IR→compile_all(constraints)→checkers+economy sim→`build_review_report`），得分区 `ReviewReport`。**检出判定**：样本"被检出" ⟺ 报告中存在一条 `defect_class` 匹配、且触及 `injected_entities`（注入实体出现在 Finding 的 entities/evidence）的 Finding，且来自该类 `bucket` 对应的分区。

指标（全带 Wilson CI；确定性 vs llm-assisted **分开报告**）：
- **Bug Detection Rate（分缺陷类）**：detections/samples per class。
- **False-Positive（第一 KPI）**，两口径分列（§13.4）：
  - **oracle-FP**：clean 样本上的**确定性** Finding 数（含 unproven，锁 M1 不变量）——目标 **0**。
  - **constraint-FP**：约束误批导致的误报（接口现定；M3a 在 clean 语料上测约束库本身的 FP，实现可含基础版，深化留后）。
- **Fix Pass Rate / Human-Edit-Distance / QA 工时节省 / Cost·Latency**：从 M2 repair harness 聚合（§6）。
- 每指标输出 `Metric{name, class, n, k, rate, ci_low, ci_high, bucket}`。

---

## 6. Agent 指标聚合（`agent_metrics.py`）——有界子集，只读 M2 cassette

用户选定：agent 类指标（Fix Pass Rate、Playtest 完成率、记忆/planner 消融、修复搜索效率、叙事对抗一致性）**在有界 cassette 录制子集上报告**，复用 M2 已录结果，不新调网关。
- `aggregate_agent_metrics() -> list[Metric]`：REPLAY 复算 M2 的 repair harness（Fix Pass Rate 9/10）、M2b playtest 语料（完成率 70%/25%/5% + planner 消融 +45pp + 记忆消融 +5pp + compactor 对比），封成带 `n` + CI 的 `Metric`，`bucket` 标注，**显式标"agent 子集 n=X"**（诚实小样本）。
- 只 import M2 harness 的**纯聚合函数** + `runtime` 的 REPLAY 路由 + 已录 cassette；不触 LLM SDK（lint 锁）。
- 仅当某类严重欠功效才考虑小额新录（LLM，走 M2 的 record 入口），默认不新录。

---

## 7. BenchReport 契约（`report.py`）——M3b/M3c 消费

```python
class BenchReport(BaseModel):
    seeded: list[Metric]              # 分缺陷类 BDR + FP（确定性 + llm-assisted 分区）
    oracle_fp: FPReport               # {n_clean, count, rate, ci}（目标 0）
    constraint_fp: FPReport
    agent: list[Metric]               # 有界 agent 子集
    power: list[PowerRow]             # {class, n, achieved_half_width, target_met}
    external: ExternalReport | None   # M3b 填（Flare 交叉验证）；现定 None
    meta: BenchMeta                   # {seed, model_snapshot, corpus_size, generated_at?}
```
JSON 可序列化（`bench/report.py` 出 `to_json`）；`run_bench.py` 出人读文本视图（最小 CLI 视图，完整面板 M3c/M4）。`external` 槽现定（不简化只延后）。

---

## 8. 反循环（§13.4，卖点）

- 注入器（`bench/inject.py`）与检查器（`spine/checkers/`）**分离代码**，检查器实现独立于分类学、永不读 GT。
- clean 语料上报 oracle-FP=0 证明检查器非"见啥报啥"。
- **真正的循环打破在 M3b**：Flare 真实 commit/补丁历史的**非注入**缺陷，注入器从未编写过它们，检出率在外部语料上独立报告。M3a 的 `BenchReport.external` 槽为此现定。

---

## 9. 测试策略（TDD 全程，确定性、零实网）

- **注入器 property 测试**（每类）：注入后独立结构/数值断言成立（不经检查器）、base 上不成立、seeded 复现、不误伤他类、多 seed 结构两两不同。
- **指标引擎**：手写已知结局 fixture（注入 X → 检出 X；clean → 检出 ∅）验证 BDR/FP 计数与匹配逻辑；oracle-FP=0 在 clean 语料上锁（含 `unproven==[]`）。
- **功效/CI**：Wilson CI 单调性/边界；`required_n` 反推正确性。
- **agent 指标聚合**：REPLAY 复算与 M2 已报数字一致（逐字段）。
- **依赖 lint**：新增第 8 契约（bench 禁 LLM SDK；`import-linter` 层）+ 一条 AST 测试锁 **seeded 核心（inject/corpus/metrics/report/taxonomy/power）禁 import `gameforge.agents`**（只 `agent_metrics.py` 可桥接），并入 `tests/test_dependency_lint.py`。
- **端到端**：`run_bench.py` 跑 seeded 语料出 `BenchReport`，断言分类 BDR 报告齐全、oracle-FP=0、每类带 CI、det/llm 分区不混。

---

## 10. 分期（延后≠简化，接口现定）

| 元素 | 接口落点 | 实现落点 |
|---|---|---|
| 缺陷分类学 15 类 + 注入器 | `bench/taxonomy.py`/`inject.py` | M3a |
| seeded 语料 ≥500 + 功效 | `bench/corpus.py`/`power.py` | M3a |
| 指标引擎 + BDR/FP + CI + 分区 | `bench/metrics.py` | M3a |
| agent 指标（有界子集） | `bench/agent_metrics.py` | M3a |
| `BenchReport` JSON 契约 + 最小视图 | `bench/report.py` | M3a |
| **外部 Flare 语料 + 交叉验证** | `BenchReport.external`（现定） | **M3b** |
| **Eval 完整交互面板** | JSON 契约（现定） | **M3c/M4** |
| constraint-FP 深化 | `metrics.py`（基础版现做） | M3a 基础 / M4 深化 |

**Deferred（接口现定，不简化只延后）**：M3b 外部语料真实检出交叉验证；M3c/M4 React 面板；constraint-FP 与人审批闭环的深度联动（M4 RBAC）。
