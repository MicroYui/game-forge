# 确定性接地检索 · 实现记录

> 日期：2026-07-28
> 起因：产品负责人问「RAG 是不是更合适？」——答案是**召回层是，判定层不是**
> 依赖：PRD、foundations-contracts v0.3、硬规则 1–8

## 为什么现在做

`generator.py::_snapshot_summary()` 把**整张图无删减**塞进 prompt。三件已核实的事实让这从「以后会撑不住」变成「现在就在损害产品」：

1. **它按每次模型调用重复一遍。** `run_from_materials` 把材料按 64 KiB 切块，每块一次模型调用，**每次都重塞整张图**；截断重试二分再切，再各塞一遍。图的成本乘以块数。
2. **超限是硬失败。** `model_routing.py::require_agent_prompt_message_bytes` 超过 `max_prompt_message_bytes` 直接 `IntegrityViolation`。图一大，提取不是变差，是**跑不了**。
3. **信噪比现在就在起作用。** 璃月的策划案和蒙德的全部内容抢同一份注意力。

## 为什么不用向量

**RAG 属于召回层，不属于判定层。** 向量相似度不可判定（阈值全靠拍）、不可复现（换一版 embedding 模型同一份材料归到不同实体）、且**错误静默**——合错了图谱被污染而没有任何确定性检查器能发现，因为合并后图上完全自洽。

PRD 早就把「非确定性向量 ANN 检索」列为 cassette 回放的敌人（`specs/2026-07-07-m2-agent-layer-design.md:199`），pgvector 只被允许做辅助语义搜索。`Embedder` 协议存在但**故意不带任何具体实现**。

**别名表就是那个语义桥**：人声明一次「岩王帝君 = 钟离」，之后永久确定性。向量本来要解决的跨字面问题，这里是一条精确、可审计的事实。

## 设计

```
GroundingRetriever      查询 → 排序后的焦点 id        ← 新增，仅 generation
        ↓
project_focus_context   焦点 id → 五键上下文          ← 与 repair 共用
        ↓
GroundingSlice.to_prompt_json()                      字节预算 + 渲染
```

**排序（显式全序）**：`(not declared, -best_length, -occurrences, entity_id)`。`declared` 领先是刻意的——人说过「这两个名字是一个东西」，该压过任何词形信号。`entity_id` 唯一，全序封闭。

**匹配用子串，不用分词**：`canonical_identity_token("老陶在锻造区")` 产出带 `_` 的单一 token，在散文里永不出现；中文也没有空格边界。折叠用 `fold_for_match`（NFKC + casefold + 空白收敛），**不剥标点**——`canonical_identity_token` 才剥，而且会对空结果抛 `ValueError`，所以**显示名绝不能过它**。

**没命中是定义好的行为，不是兜底**：`focus_nodes` 为空，但仍把真实 id 和真实名字给模型看（降级投影）。否则它会另起一套平行分类，那正是别名工作在修的病。

**字节天花板是一条固定顺序的梯子**：邻居 → 关系 → 焦点（按排名倒序）。计数上限管不住字节，因为一个焦点实体的 `attrs` 无界。目录永不丢——它是「这个游戏里有些什么」最后的诚实信号。

**`ensure_ascii=False`**：今天每个中文字符被转义成 `\uXXXX`，中文产品的接地字节数直接三倍。梯子必须量 `.encode("utf-8")`。

## 项目隔离

检索建在**这条 Run 已绑定的 base snapshot** 上——它由 `project.content_ref_name` 解析而来，而 `GameProjectV1` 的校验器（`contracts/projects.py:96-117`）在契约层钉死 `content_ref_name == f"projects/{project_id}/content/head"`。**代码算不出别的项目的 ref 名**，所以不存在可跨越的语料库。

**绝不能**建在内容读模型上：`/api/v1/specs/{id}/graph` 按 artifact id 寻址、只按 domain 授权，而前端建项目时 `domain_ids` 硬编码为 `["builtin"]`，默认角色策略又全是 `domain_scope="all"`——**domain scope 今天对项目零隔离**。

> 既有缺口（非本次引入、不在本次范围）：A 项目的策划今天已能通过 `/api/v1/refs/{name}/history` 读到 B 项目的 ref 历史。

## Prompt 升版 `generation@7 → @8`（正确性要求，不是流程）

系统模板原文：「Use only real existing ids from **the supplied snapshot**」。把切片交给这句话，模型会得出「切片里没有的 id 就不存在」→ `add_entity` 造重复实体——**正是 `ceb8d8b9` 与 `379ccee9` 刚关掉的病**。

`@8` 的模板从 `@7` **派生**（一次 `replace`），所以两者只差那一段、其余逐字节相同，不会有人手抄漏一句。旧模板保留为 `generation.v7.system`：`generation-graph@7/@8` 降为 `replay_only` 后仍指名 `generation@7`，而 worker 从 **active 和 replay_only 两类图**收集必需的 prompt key。

**用户消息构造本身不需要升版**——`ceb8d8b9` 改过同一处而未升版，且写明「Bench replays confirm the prompt change costs no frozen cassette」。`prompt_version` 寻址的是**系统模板字节**；用户消息在留存路径里是从不可变工件逐字读回的。**实测确认 cassette 成本为零。**

## 实施中撞到的陷阱

1. **`_PROFILE_CONTRACT_VERSION` 必须按 `(kind, profile_version)` 对，不能按 kind。** 把 `"generation"` 加进原有的 f-string 例外集，会**追溯改掉 catalog v4 里 `builtin.generation@2` 的 `config_schema_id`**，v4 digest 变化，留存 Run 全部搁浅。这与上一轮 `_profile_compatibility` 只有下界没有上界是同一类错误。
2. **profile 生命周期必须从它首次出现的那份 catalog 的 revision 1 起算。** `test_local_composition` 原本只落盘「第一份 + 合成的最新版」跳过中间，而 `builtin.generation@2` 在 v5 里 revision 已是 2 —— 于是 fail closed。**任何跳过中间 catalog 的落盘方式都会在下一次 profile 升版时炸。**
3. **`_default_generation_policy` 是最容易漏的一处。** 所有测试都显式传 profile，只有走默认值的两个 e2e 测试碰得到；而那正是产品默认路径（前端点「继续生成内容」）。
4. **prompt binding plan 现在合法地同时带 `@7` 和 `@8`。** 直接把断言里的 `@7` 换成 `@8` 是错的。
5. **测试文件基名撞车**：`tests/agents/playtest/test_grounding.py` 已存在，两个目录都没有 `__init__.py`，pytest 按 rootdir 相对路径推导模块名时同名。单独跑永远绿，只有全量收集才炸。

## 回归

- `tests/spine/ir/test_grounding_retrieval.py`：声明别名穿透、**无关材料不拉进无关内容**、一跳邻居降级投影、空图不抛异常、**内容相同插入顺序相反产出逐字节相同**、字节梯子顺序、显示名不过标识符规则、检索不改快照、两份索引互不共享。
- `tests/agents/test_generation.py`：500 实体 + 只提一个 → 用户消息 **< 8 KiB** 且 499 个 id **不出现**。正向断言尺寸与不出现，因为路由那道字节守卫从此基本不再触发，回归会隐形。
- `tests/agents/test_repair_drafter_context.py::test_repair_ir_context_bytes_are_frozen`：共用投影不得让 repair 漂移一个字节（12 份 cassette 的守卫；那些回放测试对空 `cassettes/` 是 skipif 的，没有 cassette 的环境会**假绿**）。
