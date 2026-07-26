# 语义别名成为一等数据 · 实现计划

> 日期：2026-07-26
> 起因：产品负责人问「新加一份策划案，会参考已有图谱吗？岩王帝君和钟离是同一个人，会不会出问题？」
> 依赖：PRD、foundations-contracts v0.3、硬规则 1–8

## 现状（读代码得出，非推测）

| 能力 | 现状 | 出处 |
|---|---|---|
| 新提取能看见已有**实体** | ✅ prompt 里带每个实体的 `(id, type, attrs)` | `agents/generation/generator.py::_snapshot_summary` |
| 新提取能看见已有**关系** | ❌ 注释明写 "no narrative/relation dump" | 同上 |
| `air.quality` ≡ `air_quality` | ✅ 确定性词形归一（NFKC + 大小写 + 分隔符/标点折叠） | `spine/identity_normalization.py` |
| 同一 canonical identity 属性打架 | ✅ 产出 `blocking_conflict`，不静默覆盖 | 同上 |
| 岩王帝君 ≡ 钟离 | ❌ 两个 token 无字面交集，词形归一必然分开 | 同上 |
| 重复实体 / 重复关系检查器 | ❌ 不存在 | `spine/checkers/` |

模型**有机会**认出别名（grounding 里能看到 `npc:钟离`），但没有保证；一旦没认出来，没有任何确定性预言机会发现——「同一个人两个名字」是语义判断，确定性主干本来也判不了。

## 设计

**别名不进 IR 实体。** `Entity` 加字段会改掉每一个快照的 canonical 摘要，冻结的 Bench 证据、cassette、审计链全部作废；而且别名是「这个项目怎么称呼这个东西」的**身份事实**，不是游戏内容本身的属性。

**别名是项目级的确定性身份权威**，正好接进已有的那一层：`normalize_typed_ops` 已经在用 `exact_aliases`（base 快照实体 id 的精确/规范拼写）把 op 指向已有实体。声明的别名就是往同一张表里多加几条——**LLM 完全不在判定路径上**，人声明一次之后，之后每次提取都是确定性归一。

### 分片

**片 A：spine 接受声明别名**
`normalize_typed_ops(base, ops, *, declared_aliases)` 增加一个 `Mapping[str, str]`（别名 token → 权威实体 id）。别名先过 `canonical_identity_token` 归一后再入 `exact_aliases`。别名指向 base 快照里不存在的实体 → 冲突，fail closed（不能声明一个指向空气的别名）。

**片 B：别名的存储与治理**
项目级 `identity_alias` 资源：`alias`（策划写的称呼）、`canonical_entity_id`、声明人、声明时间。强 ETag + 幂等 + 审计，和 `rename_material` 同形。撤回是显式动作，不是删除历史。迁移 0017。

**片 C：接进两条归一路径**
AI 提取（`projects/service.py::create_extraction` → generation run）与人工图谱草案（`platform/projects/graph_draft.py`）都要拿到同一份别名表——否则人工编辑会把 AI 刚归一好的东西又拆开。

**片 D：策划怎么声明**
提取结果复核界面里，看到两个实体是同一个时给一个「这两个是同一个」的动作，落成别名声明。项目页给一份已声明别名的列表，可撤回。

**片 E：关系补进 grounding**
`_snapshot_summary` 补上关系（有界）。这是「同一关系两种表达」的直接原因——模型现在看不见已有关系。

## 完成定义

片 A–E 全部完成，且能实跑证明：在「原神」项目里声明「岩王帝君 = 钟离」，再上传一份只写「岩王帝君」的策划案，提取结果指向的是同一个 `npc:钟离`，没有新增重复实体。全量门禁绿。
