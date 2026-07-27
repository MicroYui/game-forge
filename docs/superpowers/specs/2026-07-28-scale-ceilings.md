# 原神规模：先撞哪堵墙，以及哪些墙是自己砌的

> 日期：2026-07-28
> 状态：**设计研究，不实现**。产品负责人明确说这一轮不做，但要想清楚并存档。
> 方法：6 个子系统并行实测 → 每条上限派独立代理试图反驳 → 关键结论我逐条亲手复核。
> 下方标 **【实测】** 的是我自己跑出来的数字；标 **【未证实】** 的是代理报告里我没有独立验证的。

## 结论先说

**项目到不了原神规模。它在 1,414 个实体处停下——而在那之前，产品的核心卖点已经在 447 个实体处静默失效了。**

更要紧的是：**这些墙几乎全是自己砌的，不是架构承诺的代价。** 真正为「全图 sound 检查 + 内容寻址不可变快照 + 可回放」付的钱，在 10 万实体处也只是**几秒钟**。

---

## 一、先撞哪堵墙

### 1. ASP 不再判定 —— 447 实体 【实测】

`spine/checkers/asp.py:202`：

```python
estimated_atoms = n_nodes + n_edges + n_scalar_attrs + n_nodes * n_nodes
if estimated_atoms > self.grounding_budget_atoms:   # 默认 200_000
    emit_unproven_all(...)                          # status="unproven"
```

`n_nodes² > 200_000` → **n_nodes ≈ 447**。超过之后 ASP 拒绝判定，发 `status="unproven"` 的 finding。

checker 这一层是诚实的，模块文档明写「NEVER a silent pass」。**问题出在门那一层。**

`agent_runners.py:389` 的 `_gate_finding_key` 只取 `(predicate, status)`，而 `predicate` 里的 `evidence_locator` 会把 `estimated_atoms` / `budget` / `reason` 全部丢掉（不在 `_LOCATOR_KEYS` 里）。我实测构造了 base（450 节点）和 candidate（455 节点）两个 unproven finding：

```
base key : {"predicate":"{...\"defect_class\":\"cyclic_dependency\"...\"evidence_locator\":{}...}","status":"unproven"}
cand key : {"predicate":"{...\"defect_class\":\"cyclic_dependency\"...\"evidence_locator\":{}...}","status":"unproven"}
IDENTICAL: True
```

门做的是 `candidate_keys - base_keys`（找**新引入**的缺陷）。两边都是同一个 unproven key → 差集为空 → **提案通过**。

**所以：447 实体之后，`cyclic_dependency` 这类由 ASP 判定的缺陷永远不再被判定，而门给出的是「通过」。** 门的 base/candidate 差分对「无法判定」是 **fail-open** 的。

GameForge 卖的是 soundness。它在 447 个实体处停止提供 soundness，而没有任何一个界面会说这件事。

**这是本文档里唯一一条我认为不该等到「做原神规模」才修的。**

### 2. Checker / generation 工作量预算 —— 1,414 实体 【实测】

`run_handlers/generation.py:494` 与 `run_handlers/checker.py`：

```python
work_units = (len(entities)² + len(entities) + len(relations)) × (1 + len(constraints))
```

对 `max_checker_work_units = 2_000_000`。零约束时 E² = 2M → **E ≈ 1,414**。

**而真实成本是线性的。** 我实测 `GraphChecker`：

| 实体 | 关系 | 真实耗时 | 公式收费 | 判定 |
|---|---|---|---|---|
| 300 | 200 | **2.0 ms** | 90,500 | 通过 |
| 1,200 | 800 | **7.8 ms** | 1,442,000 | 通过 |
| 4,800 | 3,200 | **31.2 ms** | 23,048,000 | **拒绝** |
| 19,200 | 12,800 | **182.9 ms** | 368,672,000 | **拒绝** |

严格线性，约 9.5 µs/实体。**这条公式在 1,414 实体处拒掉的，是真实耗时 10 毫秒的工作。**

为什么公式是平方的？我读了所有 checker 循环——**没有一个是 E² 的**。`_dangling_reference` 遍历关系；环检测是 Tarjan/Kahn 的 O(V+R)；唯一的 per-start BFS（`_unreachable_target`）在 `nav is None` 时立刻返回，而 generation gate 恰好就是 `nav=None`。

平方项对应的循环不存在。

约束还会乘上去：`(1 + C)`。20 条约束把上限从 1,414 砍到约 307，256 条砍到约 87。而约束编译出来的是**互相独立的线性检查器**。

### 3. 缺陷条数上限 —— 约 200 实体（特定形态） 【实测】

`MAX_PREPARED_FINDINGS = 10_000`（`contracts/jobs.py:103`），作为 pydantic `max_length` 生效。

`_gated_destination` 对每个 quest 独立发射：k 个 quest × s 个共享 step = k×s 条。我实测：

```
quests=20 shared_steps=20  entities=42  findings=420
```

实体数 E = k+s+2，findings ≈ (E/2)²。**打爆 10,000 需要约 200 个实体。**

> **纠正代理报告**：它称这些是「k² 个重复 finding」、绑定在 40 实体、且 `seen` 集合放错位置是 bug。**三条都不对。** 增长是 k×s；每条 (quest, step, region, gate) 是**真正不同的缺陷**（access proof 是 quest 作用域的，A 任务缺证明和 B 任务缺证明是两件事），跨 quest 去重会**藏掉真缺陷**。它引用的 `base.py:466` 也不存在——那个文件只有 41 行。

真正的问题不是重复，是**没有聚合、没有背压**：一万条同类缺陷全量物化后才被拒。

### 4. 之后的墙 【未证实，来自代理报告】

- 经济仿真工作预算：约 276–553 实体（取决于掉落/商店形态）
- snapshot diff 上限 1,000 条：约 333 个变更实体
- `list_graph` 物化上限 1,000：约 333 实体（R=2E 时）
- 前端 cytoscape 渲染

这几条我没有独立复核。它们都排在前三条之后，先修前面的才轮得到它们。

---

## 二、固有 vs 自己砌的

这个区分是全文的重点：**固有上限要改设计，自己砌的墙只是要干活。**

### 固有（架构承诺真的要付这个钱）

**F1. 全图重验。** `run_handlers/checker.py` 把理由写在代码里：子集化会凭空造出 dangling / unreachable 的**假阳性**。这就是 soundness 的定义。成本随实体数**线性**增长且永不消失。

要打破它需要**增量检查 + frame condition 证明**——证明「只触及有界邻域的 op 不可能改变邻域外的判定」。那是新理论，不是新代码。

**F2. 内容寻址快照必须全量 hash。** `snapshot_id = compute_snapshot_id(content_payload)`，身份就是全部字节的哈希，这是 M0a 地基契约。下限是**每个不同快照一次**。

**F3. 两份快照 diff 必须读全两份。** 扁平 canonical payload、零结构共享 ⇒ 跳不过未变子树。要 O(变更量) 需要 Merkle-DAG / 持久化树，**那会改变 `snapshot_id` 的定义，即动地基契约**。

**F4. 可回放把常量焊死。** 任何进 prompt 的东西一改，cassette 全废。增量A 已经吃过一次（SELLS attrs → snapshot_id → drafter prompt → 全部 repair cassette 失效 → 全量重录）。这不是实体上限，但它解释了**为什么上面那些实现问题一直没人动**，也说明修复必须一次做对。

**三条固有成本加起来，在 10 万实体处大约是几秒钟一次 Run。**

### 自己砌的（纯粹是干活）

| # | 问题 | 修法 |
|---|---|---|
| P1 | E² 收费公式，比真实成本高约三个数量级 | 按真实线性成本计费：`E + R`，约束各自线性叠加而非相乘 |
| P2 | `MAX_CHECKER_WORK_UNITS_V1` 既是默认值**又是契约天花板**（`le=` 用的是同一个常量），任何执行方案都抬不动一个单位 | 拆开：默认值降到天花板之下。仿真侧已经是这么做的（默认 2M / 上限 20M），checker 侧没有 |
| P3 | `(1 + C)` 乘一个二次式 | 约束是并行独立检查器，线性叠加 |
| P4 | ASP 预算 shape-blind 且不接执行方案 | 先跑 Tarjan 求强连通分量（`GraphChecker` 已经有），只对分量内部 ground——无环图直接零成本判定 |
| P5 | **门对「无法判定」fail-open** | 判定不了就该拒绝候选，而不是当作「没有新缺陷」。这条是正确性，不是性能 |
| P6 | 缺陷无聚合无背压 | 发射侧限流 + 按 defect_class 聚合 |
| P7 | 错误分类学骗人 | base 超限 → 不透明 `redacted_execution_failure`；candidate 超限 → `generation_gate_rejected` / `business_rule`。**两者都不说「你的图太大」**，策划看到「AI 提案被拒」就去改策划案——改不好，因为问题不在内容 |

---

## 三、如果真要做原神规模，怎么做

按依赖顺序，不是按难度：

**第 0 步：把门的 fail-open 关掉。** 与规模无关，现在就该做。判定不了就拒绝，并且让 `unproven` 的原因进入 `evidence_locator`，这样 base 和 candidate 的 key 不再相同。

**第 1 步：按真实成本计费。** P1+P2+P3 是同一件事的三个面。实测数据在手：线性、约 9.5 µs/实体。这一步单独就把上限从 1,414 推到「几万实体、几百毫秒」。

**第 2 步：ASP 按强连通分量 ground。** 无环图零成本，有环图只付环的钱。这一步把 447 那堵墙从「实体总数」换成「最大环的大小」——后者才是 ASP 真正的复杂度来源。

**第 3 步：缺陷聚合。** 一万条同类缺陷对策划没有价值，一条「这 47 个任务步都缺 access proof」才有。

**第 4 步（真到十万级才需要）：分区内容 ref。** 一个项目一个 `content/head` 是当前设计。原神那种规模真实的组织方式是**按区域/版本分片**，每片一个 ref，跨片引用显式声明。这会改动地基契约里 ref 与快照的关系——**这一步才是真正的设计改动，前三步都不是。**

**永远不做**：为了跑得快而放弃全图 sound 检查。那是产品本身。

---

## 四、我没能确定的

- 经济仿真、diff、read model、前端渲染那几条上限只有代理报告，我没独立复核。
- 十万实体的 canonical hash 成本（代理报告约 1 秒）我没实测。
- 分区内容 ref 只是方向，没有设计。

代理报告全文（含大量未复核的细节和若干已被我证伪的说法）在工作流 transcript 里，**不要直接引用它**——本文档里标了【实测】的部分才是可依赖的。
