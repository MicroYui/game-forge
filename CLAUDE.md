# CLAUDE.md — GameForge 项目操作手册

> 本文件在每个新会话启动时**自动加载**。它是新会话的唯一入口：**动任何代码前，先按下面"必读"清单读 spec。**

## 这是什么项目

**GameForge** = 面向游戏内容的**正确性编译器 + 生产级 Agent 工作台**。
从策划案/配置表构建可版本化的 Design-Spec IR（知识图谱+约束）→ 用**确定性检查器 + 经济仿真**做可判定验证（**不是** LLM 打分）→ 用**有边界的 LLM Agent** 做抽取提议/缺陷分诊/修复起草 → 在**真实可运行的参考游戏 Aureus** 里由 Playtest Agent 闭环回归 → 全程可观测、可复现、可审计、人工审批。

## 🔴 必读（动代码前）

按顺序读，这三份是**单一真相源**：

1. `docs/superpowers/specs/2026-07-03-gameforge-prd.md` — 产品需求文档（定位/子系统/里程碑/验收）
2. `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md` — **地基契约 v0.2**（7 项跨里程碑接口，改起来代价最高，必须严格遵守）
3. `docs/superpowers/plans/` — 当前里程碑的实现计划（存在则读；执行中以它为准）

## 🔴 不可违背的硬规则

1. **不简化，只延后**：任何"最小子集/砍字段/缩接口"的建议一律拒绝；最多把*实现*延后到后续里程碑，接口/契约**现在就定全**。（见记忆 `no-simplification-principle`）
2. **企业级 = 生产级工程成熟度**，非商业化/上市/对外 IP。不为售卖、私有化、SSO 计费、SOC2 设计。（见记忆 `enterprise-grade-definition`）
3. **确定性优先**：对/错判定由图/ASP(Clingo)/SMT(z3)/仿真给出；LLM 只做提议/起草/提示，每个 LLM 输出必有确定性预言机或人工兜底。
4. **依赖方向单向**：`agents → spine`，**永不** `spine → agents`；`spine` 禁止 import 任何 LLM SDK（openai/anthropic/langchain/langgraph…）。CI 加依赖 lint。
5. **可复现只承诺回放**：固定 model_snapshot + cassette 回放 + seed 化环境；不承诺在线模型的 bit 级复现。
6. **TDD 全程**：尤其检查器/DSL 编译器用差分测试（多引擎对拍）+ 属性测试（对拍朴素实现）——"soundness"是卖点，必须 test-first。

## Git 约定

- **所有 git 提交不带任何 AI 协作者署名**：commit message **不加** `Co-Authored-By: Claude ...`，也**不加** `Generated with Claude Code` 之类的 AI 归属尾注。PR 描述同理。提交信息就写正常的工程内容。

## 里程碑路线图与工作流

**每个里程碑走**：`writing-plans（详细计划）→ goal 模式实现（executing-plans / subagent-driven-development）→ TDD → requesting-code-review → 进下一个`。

| 里程碑 | 主题 | 状态 |
|---|---|---|
| M0a | contracts 包 + IR core 类型 + canonical 快照 + Aureus 最小内核（任务+网格导航）+ 最小 checker + 跑通一条 3+ 步任务链 | ⬜ 未开始 |
| M0b | Aureus 补齐（战斗/经济/抽卡）+ Schema Registry round-trip + 版本/血缘/审计骨架 + DB 迁移框架 | ⬜ |
| M1 | Graph/ASP/SMT 检查器套件 + DSL→检查器编译 + 经济仿真 + 开源游戏适配器 + Finding/Patch | ⬜ |
| M2 | 有边界 Agent 层 + Agent-Env + Playtest Agent + 回归框架（cassette）+ Model Router/Cassette | ⬜ |
| M3 | GameForge-Bench（≥500 seeded + 开源真实语料）+ 完整指标 + Eval 面板 | ⬜ |
| M4 | 生产化硬化：可观测/成本治理 + 版本血缘/回滚/审计 + RBAC/审批 + 前端全页面 | ⬜ |

> **推进本项目时，把本表的状态列更新为 🔄进行中 / ✅完成，作为跨会话的进度锚点。**

## 技术栈（已锁定）

Python 核心（Agent 编排、仿真、检查器、Clingo/z3）+ React/TS 前端；参考游戏 Aureus 内核也用 Python（确定性 headless + 薄渲染）；形式化全量（图 + ASP/Clingo + SMT/z3 + Monte-Carlo/ABM 经济仿真）。

## 记忆文件（`~/.claude/projects/-Users-liyifan-Documents-code-self-game-forge/memory/`）

自动加载，承载跨会话的**决策与原则**：`gameforge-positioning`（定位+锁定决策+关键评估发现）、`enterprise-grade-definition`、`no-simplification-principle`。
