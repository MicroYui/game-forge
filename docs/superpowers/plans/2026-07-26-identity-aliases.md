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
人工图谱草案（`platform/projects/graph_draft.py`）已接通。

**AI 提取这条没做到，原因要记下来。** 第一次实现把别名塞进了 `GenerationProposePayloadV1`，结果打穿了**每一条已保留的 Run**：run payload 是内容哈希的不可变记录，加一个带默认值的字段也会改掉它的 canonical 形态。这是硬规则 7 唯一的例外（已持久化的不可变工件与审计记录），已撤回。

正确做法是升 `generation.propose@2`：`_RUN_KIND_PAYLOAD_SCHEMAS` 把 `(kind, version)` 绑死到唯一一个 payload schema，所以新增字段必须是新的 payload schema，而新的 payload schema 必须配新的 run kind 版本，会连带动执行图、执行方案与任务套件。那是一次独立的里程碑级改动。

**这个缺陷是重启真实服务、发现项目列表 500 才发现的，全量门禁没抓住它**——测试全部从新建工作区起步，没有跨版本的历史数据。

**片 D：策划怎么声明**
提取结果复核界面里，看到两个实体是同一个时给一个「这两个是同一个」的动作，落成别名声明。项目页给一份已声明别名的列表，可撤回。

**片 E：关系补进 grounding**
`_snapshot_summary` 补上关系（有界）。这是「同一关系两种表达」的直接原因——模型现在看不见已有关系。

## 片 C 的 AI 那一半：`generation.propose@2` 落地方案

**别名不内联进 payload，铸成工件。** 上限 1000 条别名内联会让每条 Run 记录膨胀到几百 KB，而 payload 是内容哈希的不可变记录。改成 `identity_alias_artifact_id: BoundedId | None`——和 `base_snapshot_artifact_id` 同形，内容寻址、可回放、`_referenced_input_artifact_ids` 自动纳入信封校验。**不新造工件种类**：admission 已有的 `_goal_writer.mint(text=…)` 铸的就是 `source_raw`，别名表用规范 JSON 走同一条路（新造 artifact kind 属于 rule 8 的 overdesign）。

**`GenerationProposePayloadV2` 继承 `GenerationProposePayloadV1`。** 全仓 18 处 `isinstance(..., GenerationProposePayloadV1)`、**0 处精确类型比较**（已核）——继承让这 18 处原样通过，不产生 18 处兼容分支。判别式联合靠 `schema_version` 字面量区分，不受继承影响。

**唯一允许的版本分支**在 `contracts/jobs.py` 的一个访问器里：留存的 `generation-propose@1` payload 早于别名机制，恒为空。这是硬规则 7 的既定例外（已持久化的不可变记录），集中一处并写明理由。

### 改动清单（按依赖序）

| 处 | 改什么 | 为什么非改不可 |
|---|---|---|
| `contracts/jobs.py` | V2 + 进联合 + `_RUN_KIND_PAYLOAD_SCHEMAS[(…,2)]` + 访问器 | `(kind, version)` 绑死唯一 payload schema |
| `registry/model.py` | 三张冻结表加 v2；**`FROZEN_ACTIVE_RUN_KIND_IDENTITIES` 改为按显式 lifecycle 推导**，不再等同于「表里有」 | v1 定义必须留着给留存 Run 解析，但不能仍算 active |
| `registry/defaults.py` | `_profile_compatibility` 按目录版本分叉；**新铸 catalog v4**；run kind v1→`replay_only`；`generation-graph@7`→`replay_only`，新增 `@8` | v1–v3 目录的 `catalog_digest` 写在每条留存 Run 的信封里，**一个字节都不能动** |
| `runs/admission.py` | `_mint_identity_alias_set` + 构造 V2 + `RunKindRef(version=2)` | 别名在 admission 冻结，之后不再变 |
| `projects/service.py` | 事务内读别名表 → 传 admission；`RunKindRef(version=2)` ×2 | 别名表归项目服务所有 |
| `run_handlers/generation.py` | 读别名工件 → `GenerationRunRequest.declared_identity_aliases` | |
| `apps/worker/agent_runners.py:486` | `normalize_typed_ops(…, declared_aliases=…)` | **就是缺陷本身所在的这一行** |

### 落地后的实际形态（已实现）

设计与上表一致，实施中多出三处只有写代码才会发现的连带：

1. **run kind 也需要生命周期。** `FROZEN_ACTIVE_RUN_KIND_IDENTITIES` 原本等于「冻结表里有」，于是 @1 一留下就仍算 active。新增 `FROZEN_DISABLED_RUN_KIND_IDENTITIES`，active 由差集推导；@1 的定义仍在注册表里（留存 Run 与 catalog 1–3 都引用它），但 `status="disabled"`，profile requirement 与 permission resolver 都不再为它注册。
2. **执行方案必须跟着升版。** profile 定义把 `compatible_run_kinds` 写死在自己的内容里，而每份 catalog 都被哈希进 Run，所以服务新 run kind 只能新铸 `generation@2` / `config_export@2`，`@1` 一对按 `builtin.checker@1` 的既有先例置 `disabled`。catalog v1 的 digest 断言（`test_task11_execution_profile_catalog_remains_byte_identical`）全程未动，这就是留存 Run 没被打穿的证明。
3. **删掉了四处 `run.kind.version != 1` 的冗余防御**（`effects.py`、`agent_drafts.py` ×2、`local_reads.py`、`local.py`）。`RunRecord` 契约已经强制 `(kind, version) → payload schema`，再比一次版本号是硬规则 8 说的「防御式对抗代码」，而且正是它们会在版本升级时无声拒绝掉每一条新 Run。

4. **run kind 升版的爆炸半径主要在前端，而且只有 Playwright 看得见。** 前端有四类地方在替服务端断言版本号，四类都被打穿了：
   - `supportsRunKind` 在 5 个文件里各复制一份、全钉 `version === 1` → 方案下拉框**静默变空**。收敛成 `execution-profiles.ts` 一份且**不再钉版本**：哪个版本是当前的由服务端的 `status === "active"` 回答。
   - `GenerationPage` 提交时写死 `run_kind: {..., version: 1}` → 422。改成从已选执行方案的 `compatible_run_kinds` 读回来——服务端已经告诉客户端了，客户端别自己编。
   - `listReplaySourceRuns` 按 `version: 1` 过滤留存 Run → 回放来源下拉框为空。这一处**必须钉版本**且钉的是当前版本：REPLAY 的语义是「重跑这条一模一样的请求」，admission 会比对 `source.payload.params != params`，所以留存的 `@1` Run 在 `@2` 下**结构上不可回放**（它仍是可读的证据，只是不能再执行）。列出来只会让策划点了必然被拒。
   - `candidate.ts::parseRunKind` 解析终态清单时要求 `version === 1`，否则整页显示「生成结果无法安全展示」。清单是**服务端自己写下的不可变记录**，包括这套构建已不再受理的版本；照它写的读，不要再断言一次。

5. **`_profile_compatibility` 需要上下界，不只是下界。** 第一版只做了「@2 从 catalog 4 起才出现」，结果 catalog 4 的 `builtin.generation@2` 同时声明服务 `@1` 和 `@2`——它在替一个 admission 已经拒绝的 run kind 打广告。前端按 `compatible_run_kinds` 读回版本时拿到列表里的第一个（`@1`），resolve 请求就带着 `@1` 发出去，服务端按 prospective 推出 `@2`，422。补上 `_RUN_KIND_LAST_CATALOG_VERSION`（`("generation.propose",1): 3`）之后，catalog 1–3 字节不变、catalog 4 的两个 `@2` profile 只声明 `@2`。

6. **profile 的 `input_schema_ids` 是从 `compatible_run_kinds` 推导的，所以上界一改，worker 的可信组件契约也要跟着改。** `_PROFILE_HANDLER_CONTRACTS` 里 `generation` / `config_export` 两项原本被我写成「`@1` 和 `@2` 的并集」，那是**为了让新旧都过**——正是硬规则 8 禁止的兜底。改成只声明当前形态（`generation-propose@2`）：被 disable 的 profile 不会再被解析，真有一条飞行中的 Run 拿着旧绑定进来，就该 fail closed 报出来，而不是被一个「万一」分支放过去。`builtin.checker@1→@2` 那次没暴露这条，因为 checker 两个版本服务的 run kind 完全一样。

7. **顺手修掉一条与本次无关的既有缺陷。** Journey A 的 Playwright 断言「回归夹具 CLI 的 stderr 必须为空」失败，原因是 `9ddb07e6` 让留存工作区在启动时跑迁移，而 alembic 的 `env.py` 会 `fileConfig(alembic.ini)`——**把整个进程的 root logger 重新接线**。那是人在命令行敲 `alembic upgrade` 时想要的，是 API/worker/机器可读工具**绝不该被强加**的。`migrations_api` 现在显式 `config.attributes["configure_logging"] = False`，`env.py` 据此跳过；命令行走 `alembic -c alembic.ini` 时该属性不存在，日志一如既往。

**回归**：`tests/platform/m4c/test_generation_handler.py` 三个测试锁住行为——声明过的别名会重定向到已有实体、没声明的名字**不会**被猜着合并、留存的 `@1` payload 仍能照常执行；`test_project_authoring_service.py` 锁住「别名表在读 base 快照的同一个事务里读出并冻进 Run」。

### 验收

不是跑测试就算完。**必须重启真实服务打一个有历史数据的工作区**——上次 payload_hash 打穿留存 Run，全量门禁一个都没抓住，因为测试全部从新建工作区起步。

**这一次它又抓到一条。** 重启服务打 `/tmp/gameforge-hands-on-20260726d`（4 条留存 `generation.propose@1`）：留存读路径全部 200，不带别名的新提取端到端跑到确定性门；但**声明了别名之后建提取直接 500 `integrity_violation`**。

根因：`_input_kind_requirements` 是 admission 逐字段声明「这条 Run 的输入是什么、允许什么 kind」的地方，我加了 payload 字段和 `_referenced_input_artifact_ids`，却没加这里。于是 `referenced_input_artifact_ids(params)` 有别名工件、`resolved` 没有，两集合不等 → fail closed。**这是正确的 fail-closed，不是它的 bug——是我漏了一处。** 修法是把别名工件按 `("source_raw",)` 加进 `_input_kind_requirements`，并补了 `test_generation_freezes_the_declared_alias_set_as_its_own_input` 锁住「别名工件被铸出来、绑进 `input_artifact_ids`、kind 是 source_raw」。

**为什么全量门禁抓不到**：没有一条测试走 `admit_generation(declared_identity_aliases=非空)`。别名非空只有在「项目已发布内容 + 有人声明过」时才出现，而单测里的项目都是刚 bootstrap 的空图谱——声明别名会被 fail closed 挡掉。这条路径只有真实工作区才有。

修完之后，在同一个留存工作区上完整走通：声明「璃月七星玉衡星 = 刻晴」→ 建提取 → 别名被铸成内容寻址的 `source_raw` 工件（`[{"alias":"璃月七星玉衡星","canonical_entity_id":"char:keqing"}]`）绑进 Run → worker 读到它并把这条 Run 跑到确定性门。**生产验证到此为止：它证明了「绑定与读取无误」；「别名真的把 op 重定向到已有实体」由 handler 那三个确定性测试证明**——这次的模型输出里没出现被声明的那个名字，所以没有触发重定向，不该拿它当证据。

## 完成定义

片 A、B、C、D、E 全部完成：声明「岩王帝君 = 钟离」之后，人工编辑图谱与**新上传策划案的 AI 提取**都会确定性归一到同一个实体。
