# AGENTS.md — GameForge 项目操作手册

> 本文件在每个新会话启动时**自动加载**。它是新会话的唯一入口：**动任何代码前，先按下面"必读"清单读 spec。**

## 这是什么项目

**GameForge** = 面向游戏内容的**正确性编译器 + 生产级 Agent 工作台**。
从策划案/配置表构建可版本化的 Design-Spec IR（知识图谱+约束）→ 用**确定性检查器 + 经济仿真**做可判定验证（**不是** LLM 打分）→ 用**有边界的 LLM Agent** 做抽取提议/缺陷分诊/修复起草 → 在**真实可运行的参考游戏 Aureus** 里由 Playtest Agent 闭环回归 → 全程可观测、可复现、可审计、人工审批。

## 🔴 必读（动代码前）

按顺序读，以下文档共同构成**单一真相源**：

1. `docs/superpowers/specs/2026-07-03-gameforge-prd.md` — 产品需求文档（定位/子系统/里程碑/验收）
2. `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md` — **地基契约 v0.3**（7 项地基 + M4 Artifact/ObjectRef/VersionTuple 增量契约，改起来代价最高，必须严格遵守）
3. `docs/superpowers/specs/2026-07-13-m4-production-hardening-design.md` — **M4 最终设计稿**（做任何 M4 代码前必读；五片边界、跨模块契约、API/UI/运维验收）
4. `docs/superpowers/plans/` — 当前里程碑的实现计划（存在则读；执行中以它为准，但不得偏离上述 PRD/地基/M4 设计）

## 🔴 不可违背的硬规则

1. **不简化，只延后**：任何"最小子集/砍字段/缩接口"的建议一律拒绝；最多把*实现*延后到后续里程碑，接口/契约**现在就定全**。（见记忆 `no-simplification-principle`）
2. **企业级 = 生产级工程成熟度**，非商业化/上市/对外 IP。不为售卖、私有化、SSO 计费、SOC2 设计。（见记忆 `enterprise-grade-definition`）
3. **确定性优先**：对/错判定由图/ASP(Clingo)/SMT(z3)/仿真给出；LLM 只做提议/起草/提示，每个 LLM 输出必有确定性预言机或人工兜底。
4. **依赖方向单向**：`agents → spine`，**永不** `spine → agents`；`spine` 禁止 import 任何 LLM SDK（openai/anthropic/langchain/langgraph…）。CI 加依赖 lint。
5. **可复现只承诺回放**：固定 model_snapshot + cassette 回放 + seed 化环境；不承诺在线模型的 bit 级复现。
6. **TDD 全程**：尤其检查器/DSL 编译器用差分测试（多引擎对拍）+ 属性测试（对拍朴素实现）——"soundness"是卖点，必须 test-first。

## Git 约定

- **所有 git 提交不带任何 AI 协作者署名**：commit message **不加** `Co-Authored-By: Codex ...`，也**不加** `Generated with Codex` 之类的 AI 归属尾注。PR 描述同理。提交信息就写正常的工程内容。
- **主干分支是 `master`（本仓库无 `main`）**：M0a、M0b 已线性合入 `master`；PR/合并/新里程碑切分支都以 `master` 为基线（harness 可能默认显示 `main`，忽略之）。

## 里程碑路线图与工作流

**每个里程碑走**：`writing-plans（详细计划）→ goal 模式实现（executing-plans / subagent-driven-development）→ TDD → requesting-code-review → 进下一个`。

| 里程碑 | 主题 | 状态 |
|---|---|---|
| M0a | contracts 包 + IR core 类型 + canonical 快照 + Aureus 最小内核（任务+网格导航）+ 最小 checker + 跑通一条 3+ 步任务链 | ✅ 完成（vertical slice 验收通过：config→IR→Aureus talk→collect→turn-in） |
| M0b | Aureus 补齐（战斗/经济/抽卡）+ Schema Registry round-trip + 版本/血缘/审计骨架 + DB 迁移框架 | ✅ 完成（验收：outpost CSV round-trip diff=∅；combat/economy/gacha/quest 四系统配置驱动且确定性跑通；version/lineage/audit + Alembic upgrade/downgrade 全绿） |
| M1 | Graph/ASP/SMT 检查器套件 + DSL→检查器编译 + 经济仿真 + 开源游戏适配器 + Finding/Patch | ✅ 完成（验收：9 类 IR 缺陷场景 + 1 类经济崩坏场景 sound 检出，`scenarios/defects/clean` 基线 oracle-FP=0；Clingo/z3 双后端 + Flare 开源适配器无损往返全绿） |
| M2 | 有边界 Agent 层 + Agent-Env + Playtest Agent + 回归框架（cassette）+ Model Router/Cassette | ✅ 完成（并入 `master` 2026-07-10, tip `0dfa0c4`）。**M2a**：part1 地基 + part2 6 agent + verifier-guided 修复搜索 Fix Pass Rate 90%。**M2b-1**：Playtest 核心（状态抽象+planner/executor+verifier-grounding+反思+主循环 完成=env.done）+ ≥20 链生成器 + 真实 opus 录制 → **完成率 70%(layered)/25%(flat)/5%(baseline)，planner/executor 消融 +45pp**（发现并修复"agent 打不了战斗步"缺陷=开战需先 navigate 到怪物格）。**M2b-2**：`MemTrace` 记忆层（episodic+转移图+技能层+确定性4项召回，`memory=None` 逐字节等价回归锁）+ **记忆消融 mem-on 75% vs off 70% =+5pp（memory 有效）** + compactor 对比（Deterministic 75%>LLM 70%）+ Consistency perspective-diverse quorum。462 测试绿/7 契约/ruff/零实网；RECORD resume+指数退避（穿越不稳定网关）。§16 M2 验收 anchor 1–6 全满足 |
| M3 | GameForge-Bench（≥500 seeded + 开源真实语料）+ 完整指标 + Eval 面板 | ✅ 工程实现完成；人工证据延后且不阻塞 M4。M3a/M3b/M3c 切片已落地：982 seeded 样本、11 个确定性/仿真类 BDR=1.0、oracle-FP=0、BenchReport 契约与静态 Eval 视图均保留；Flare sample round-trip/真实拓扑注入只证明 adapter/跨域探针，不冒充 PRD §13.3 的真实非注入缺陷外部验证。**Flare B0A 已按预注册 harness 和两次书面审批完整执行**：expanded universe 526 candidates，得到 7/8 independent proposed groups、10 proposed cases、4/4 proposed classes、`qualified_candidate=0`、`accepted=0`，终态 `insufficient_evidence` / `stop_flare_heavy_investment`，因此不进入 B0B（Flare），也不扩 Flare quest/loot reader。**通用 external evidence harness 已实现并保持 Flare 冻结兼容；两轮代码审查共发现并修复了 generic direct-match replay、递归外部 revert lineage，以及 selected revert 与等价 lineage 组件 disposition 冲突三个缺陷。Endless Sky 已从包含全部修复的干净 registration anchor `687f36fb6ab499d3667fe43429fec4a25132c97a` 重建，并在两个独立临时目录逐字节回放**：610 matched / 562 config-only，机械选择 80 条（75 config-only），candidate universe SHA-256 `f22981b17b43e02caaa494193e6a4b8cd92bbc0c312f9d5f1db249da7365793f`。当前工件派生状态为 `awaiting_human_evidence`；review package 不含 disposition/人类身份，且没有 `adjudication-evidence.json`、最终 ledger 或 B0A decision，因此不授权 B0B/Adapter。narrative BDR、Human-Edit-Distance、QA-hours 与 BenchReport v2 所需真人数据继续标记 `qa.evidence_missing`，由产品负责人后补；不改写为通过，也不构成 M4 前置阻塞。 |
| 增量A (pre-M4) | economy sink 适配器：`AureusCsvAdapter` plumb SELLS price/currency/buy_prob → `EconomyModel` 从真实 CSV 建模金币回收口 | ✅ 完成（branch `economy-sink-adapter`）。sink 就位后 **economy_collapse 首次真能修 → Fix Pass Rate 9/10→10/10**（agent 把 gold 降到 sink 排放之下，净流入≤0）。根因链（systematic-debugging）：SELLS attrs 改了每场景 `snapshot_id` → drafter prompt 的 `base_snapshot_id` 让**全部** repair cassette 失效 → 全量非确定性重录 → unsatisfiable_completion 退化（模型给 `delete_relation` 带 summarized old_value → `apply_patch` 乐观并发误拒）→ 修：`drafter._build_ops` 对 delete op 丢 old_value（不改请求、不重录）。532 测试绿/7 契约/ruff/零实网；request identity follow-up 已由增量B完成。 |
| 增量B (pre-M4) | 核心契约修正：Patch exact-base/fail-closed、`DROPS_FROM` producer→product、稳定 repair request/Patch identity、当前与历史模型证据分流 | ✅ 完成（`5fdfb32`、`f403a5c`、`35330e8`、`5adaab0`、`586b579`、`cc0fbc4`）。当前 repair/generation 使用 `openai/gpt-5.6-sol/pre-m4@1`，历史 extraction/consistency/playtest 保持 `anthropic/claude-opus-4-8/m2a@1` 且字节未改；bench clean base 使用跨 checkout 稳定的逻辑来源路径；两次零实网 REPLAY 完整输出一致，Fix Pass Rate **10/10**、first-pass **10/10**、runtime-vetted **10/10**。全仓 **962 passed, 1 skipped** / 7 契约 kept / Ruff / `git diff --check` 全绿；Flare 与 Endless Sky 冻结证据未变。 |
| M4 | 生产化硬化：可观测/成本治理 + 版本血缘/回滚/审计 + RBAC/审批 + 前端全页面 | 🔄 实现中：M4a 平台核心与持久化地基 ✅；M4b 可观测、成本治理与可靠性 ✅；M4c Task 1–14 ✅（Task 7–13 的同步命令、原子 admission、终态发布、worker/handler/validation authority 已逐项复审修复；Task 14 的 M4e-deferred executors 已统一为完整 `ExecutorContext` seam，重验 typed payload/profile/matrix/source-or-manifest/system projection，同 key M4e replacement 不再被错误包装，readiness 按实际 callable 报告 deferred 状态；扩展回归 **281 passed**，Ruff、format、lock、diff-check 全绿）；Task 15 待复审。 |

> **推进本项目时，把本表的状态列更新为 🔄进行中 / ✅完成，作为跨会话的进度锚点。**

> **M3 治理决定**：M3 工程实现已完成；真人 QA/人工证据由产品负责人延后，`qa.evidence_missing` 继续如实保留，但不阻塞 M4 的设计与实现，绝不把缺失证据改写成通过。

## 技术栈（已锁定）

Python 核心（Agent 编排、仿真、检查器、Clingo/z3）+ React/TS 前端；参考游戏 Aureus 内核也用 Python（确定性 headless + 薄渲染）；形式化全量（图 + ASP/Clingo + SMT/z3 + Monte-Carlo/ABM 经济仿真）。

## LLM 网关（有 LLM 调用需求时用）

本地反代网关，供 **M2+ 有边界 Agent 层**（抽取提议 / 缺陷分诊 / 修复起草）使用：

- **Base URL**：`http://localhost:4141`（OpenAI 兼容反代）
- **API Key**：从环境变量 `GAMEFORGE_LLM_KEY` 读取（**绝不入库**——密钥进版本库会永久留在 git 历史）。本地实际值见私密记忆 `llm-gateway-access`（在 `~/.claude/…/memory/`，不在仓库内）或本地 gitignored `.env`。
- **模型**：新录制默认使用 `gpt-5.6-sol`（或后续经验证的更优模型）；既有 `opus4.8` cassette 保留原始 `model_snapshot`，通过显式历史快照继续回放，不批量改写。

**硬约束（复述硬规则 3/4，不可违背）**：
- **只有 `agents` 层能调 LLM**；确定性主干 `spine` **永不**触碰网关（`spine → contracts` 仅此一项，禁 import 任何 LLM SDK，CI lint 强制）。
- 每个 LLM 输出**必有确定性预言机或人工兜底**；LLM 只做提议/起草/提示，对错判定仍由 图/ASP/SMT/仿真 给出。
- 可复现只承诺回放：走 model_snapshot + cassette 回放（M2 落地）；不承诺在线模型 bit 级复现。

## 记忆文件（`~/.claude/projects/-Users-liyifan-Documents-code-self-game-forge/memory/`）

自动加载，承载跨会话的**决策与原则**：`gameforge-positioning`（定位+锁定决策+关键评估发现）、`enterprise-grade-definition`、`no-simplification-principle`。
