# M2 — 有边界 Agent 层 + Playtest 设计文档（Design Spec）

> 状态：**决策已确认（2026-07-07）；等待用户过审本设计后进 writing-plans**。A/B/D 已定；C（mem-trace）用户在手无现成实现，要求"按**质量**比较开源项目 vs 从零实现（不计工作量，只看质量）"——见 §8.4 的质量对比与结论。本文是**设计**，非实现——未经用户过审，不进 writing-plans、不写代码、不调 LLM 网关。

关联单一真相源：PRD `docs/superpowers/specs/2026-07-03-gameforge-prd.md`（§7.5–7.9 / §13.4 / §14 / §16）、地基契约 `…foundations-contracts.md`（契约 4/5/6/**7**）、CLAUDE.md 硬规则、记忆 `no-simplification-principle` / `gameforge-positioning` / `llm-gateway-access`。

---

## 0. 待确认的 4 项决策（IMPLEMENTATION GATE）

下面每项为**已确认结果**（2026-07-07 用户拍板）。

| # | 决策 | ✅ 确认结果 | 备注 |
|---|---|---|---|
| **A** | M2 拆分 | **拆成 M2a（Agent 地基 + 修复闭环）/ M2b（长程 Playtest + 消融）**，各自 spec→plan→impl，里程碑之间停下过审 | 本文覆盖 M2 总览 + M2a 详设 + M2b 概要 |
| **B** | 编排框架 | **自研确定性状态机**（`agents/orchestrator.py`），不引 LangGraph | request_hash/replay 口径最紧、无重依赖 |
| **C** | 长程记忆 mem-trace | 用户手上**无现成实现**；要求**按质量比较开源 vs 从零**（不计工作量）→ 结论见 **§8.4**，接口一次定全、实现可换 | 影响 M2b `agents/memory/`，不阻塞 M2a |
| **D** | 模型/成本 | **opus4.8 全程录制、不设硬预算、质量优先**；cassette 入库、CI 只回放；录制前先验证 `localhost:4141` 连通 | 见 §4 |

**共同不变量（无论上述如何选，均成立）**：
- 依赖方向：`agents → {contracts, spine, env, game, runtime}`；`spine` 永不碰 LLM（已 allowlist 强制，见 M1 硬化 commit `c5f4c05`）。
- 每个 LLM 输出必有**确定性预言机或人工兜底**；LLM 只提议/起草/提示。
- 可复现只承诺**回放**：`model_snapshot` + cassette replay + seed 化 env。
- 契约先定全、实现分期（`no-simplification-principle`）。

---

## 1. M2 分解与验收映射

M2 交付面（PRD §14/§7.5）= 6 个 agent 角色 + Agent-Env（**已实现**，见 §2）+ 长程 Playtest + 回归/cassette + Model Router + 生成门禁 + verifier-guided 修复搜索 + 3 份消融报告。这是多子系统集合，按 brainstorming 最佳实践先分解：

### M2a — Agent 地基与修复闭环
产出：契约 §7 backfill（contracts + runtime 实现）、Model Router + Cassette、Agent 编排 harness、Extraction Proposer、Defect Triager、Repair Drafter + **verifier-guided 修复搜索**、生成门禁、Consistency Assistant（接口 + 基础 quorum）。
锁定验收：**Fix Pass Rate ≥ 70%**（修复经复验+回归+审批闭环）、**同输入同快照可复现**（cassette replay）、修复搜索效率报告。

### M2b — 长程 Playtest 与消融
产出：Playtest Agent（planner/executor + 状态抽象 + 动作优先级 + mem-trace + 反思自纠 + verifier-grounding）、回归框架跑 **≥20 条任务链**、完成率带 CI 的诚实报告、**记忆消融 + planner/executor 消融**报告、Consistency Assistant 的对抗性 quorum 进阶。
锁定验收：**Playtest ≥20 链可靠闭环**（完成率含置信区间）、记忆/planner 消融报告齐全。

### §16 M2 验收 → 交付映射
| §16 验收锚点 | 归属 | 交付物 |
|---|---|---|
| Playtest ≥20 链可靠闭环（完成率+CI） | M2b | Playtest Agent + 回归框架 + ≥20 场景 |
| 修复 diff 复验+回归+审批 → Fix Pass Rate ≥70% | M2a | Repair Drafter + 修复搜索 + maker-checker |
| 同输入同快照可复现 | M2a | Model Router `model_snapshot` + Cassette replay |
| 记忆消融 + planner/executor 消融 + 修复搜索效率 报告齐全 | M2b(前二)/M2a(后一) | 消融 harness + 指标报告 |

---

## 2. 现状盘点（M2 不重建的部分）

- **Agent-Env 契约已实现**：`contracts/env_types.py`（12 原子动作判别联合 `Action` + 全字段 `Observation` + `StepResult`）、`env/base.py` 的 `Environment` ABC（`reset/step/state_hash`）、`game/aureus/kernel.py:49 AureusEnv(Environment)` 完整实现（`reset`/`step`/`state_hash`）。**M2 只驱动它，不改契约 §4。** 高层宏动作（`accept_quest/turn_in/talk`）在 planner 层编译成原子序列（`HIGH_LEVEL_MACROS`），不进 Env。
- **Finding/Patch 全 schema 已定**：`contracts/findings.py`（`Finding.source` 含 `playtest`/`llm`；`Patch`/`TypedOp` 含 `old_value` 乐观并发、`preconditions`、`produced_by`）。M2 的 agent 直接**生产**这些类型。
- **确定性 patch 应用/拒绝引擎已实现**：`spine/patch.py`（`apply_patch`/`dry_run`，old_value 失配拒绝）。修复搜索的"apply→回验"复用它。
- **LlmRoutedChecker 插桩点已在**：`spine/dsl/compile.py` 对 llm-assisted 约束产出 `oracle_type="llm-assisted", status="unproven"` 占位 Finding；M2 的 Consistency Assistant 是它的"真实评估"落点（M1 只留接口）。
- **§7 是空壳**：`runtime/cassette/__init__.py`、`runtime/model_router/__init__.py`、`agents/__init__.py` 只有 docstring。**这是 M2a 第一刀（§3）。**
- **网关**：`localhost:4141`（OpenAI 兼容反代，key 从 `GAMEFORGE_LLM_KEY`/`.env` 读，绝不入库），模型 `opus4.8`。

---

## 3. 契约 §7 backfill —— M2a 的**第一个 commit**（就绪度审计的头号建议）

理由：M2 的复现验收压在 cassette replay 上；`request_hash` 口径、record 字段集若事后改最痛（`no-simplification`）。故先把机器可读类型落进 `contracts/`（单一真相源，契约 1），再落实现。

### 3.1 `contracts/model_router.py`（新增，schema 一次定全）
```
ModelSnapshot   { provider:str, model:str, snapshot_tag:str }   # pin，避免"模型被悄悄升级"
ToolSchemaRef   { name:str, version:str }
ModelRequest    { model_snapshot:ModelSnapshot, messages:list[Message],
                  params:dict, tool_schemas:list[ToolSchemaRef],
                  agent_node_id:str, prompt_version:str, cache_key:str|None }
Message         { role:Literal["system","user","assistant","tool"], content:str, tool_calls?:... }
ModelResponse   { response_normalized:str, raw_response:dict, latency_ms:int,
                  token_usage:dict, finish_reason:str, tool_calls:list }
def request_hash(req: ModelRequest) -> str    # = sha256(canonical_json({model_snapshot, messages,
                                              #   tool_schema_versions, params, agent_node_id, prompt_version}))
```
- **复用 `contracts/canonical.py`** 做 canonical_json（与快照 ID 同一套确定性序列化），保证 `request_hash` 稳定。
- 新增版本常量 `MODEL_ROUTER_SCHEMA_VERSION="model-router@1"`、`CASSETTE_SCHEMA_VERSION="cassette@1"`（`contracts/versions.py`）。

### 3.2 `contracts/cassette.py`（新增）
```
CassetteRecord { cassette_schema_version:str, request_hash:str, response:ModelResponse,
                 model_snapshot:ModelSnapshot, recorded_at:str|None }
CassetteMiss   (sentinel)
```

### 3.3 `contracts/agent_io.py`（新增）—— 6 个 agent 角色的 I/O 契约（PRD §7.5，一次定全）
每个角色一对 `*Input`/`*Output` pydantic 模型 + 一个 `fallback` 语义标注。**接口现定，实现分期**（M2a 落 Extraction/Triager/Repair/Consistency；Playtest 的 I/O 也现定，实现在 M2b）：
```
AgentRole = Literal["extraction","triage","repair","consistency","generation","playtest"]
AgentNodeResult { role:AgentRole, produced:..., fallback_taken:bool, model_run_id:str,
                  request_hashes:list[str] }   # 可追到哪几次 LLM 调用
# 各角色（输入→输出，兜底）：
Extraction  : DesignDocInput      -> EntityConstraintProposals   (进人审批队列)
Triage      : FindingsInput        -> TriagedFindings            (聚类/优先级/疑似根因；不改判确定性结论)
Repair      : FindingContextInput  -> PatchDraft                 (检查器复验+回归+人审批)
Consistency : DialogueNarrativeInput -> ConsistencyHints         (标注为建议，人确认)
Generation  : DesignGoalInput      -> ContentProposal            (永过检查器+仿真门禁)
Playtest    : (Agent-Env 观测流)    -> ActionTrace + DefectReport (Aureus 真实环境验证)
```

**依赖 lint 强化（本 commit 一并加，契合硬规则 4 字面）**：新增 import-linter 契约——LLM SDK（openai/litellm/…）**仅允许出现在 `runtime.model_router`**；`agents` 通过 router 调用、自身不直连 SDK；`spine` 已 allowlist 墙死。这样"只有 agent 层（经 model-router 基建）调 LLM"成为结构约束，而非口头约定。

---

## 4. Model Router + Cassette（`runtime/`）—— M2a

### 4.1 `runtime/model_router/router.py`
`ModelRouter.call(req: ModelRequest) -> ModelResponse`：
- **模式**：`RECORD`（打真网关 + 写 cassette）/ `REPLAY`（只读 cassette，MISS→报错）/ `PASSTHROUGH`（打网关不写，本地探索用）。CI/测试**强制 REPLAY**。
- 打网关：OpenAI 兼容 → 用 `openai` SDK 指向 `base_url=localhost:4141`，key 从 `runtime/secrets/`（读 `GAMEFORGE_LLM_KEY`）。**SDK 只在此文件出现**（依赖 lint 强制）。
- 护栏（PRD §7.5）：配额、超时/重试/降级、稳定前缀语义缓存（复用稳定 KG+约束前缀降本，`cache_key`）。
- 固定 `model_snapshot`（`[决策D]`：默认 `{anthropic, opus4.8, <snapshot_tag>}`）。

### 4.2 `runtime/cassette/store.py`
`CassetteStore.record(request_hash, CassetteRecord)` / `.replay(request_hash) -> CassetteRecord | CassetteMiss`：
- 落盘布局：`cassettes/<agent_node_id>/<request_hash>.json`（可读、可 diff、可提交入库）。
- **复现验收**：同 `model_snapshot` + REPLAY → agent 逐步复现（PRD §5.5 只承诺回放）。测试：录一遍 → 全量 REPLAY → 逐 `request_hash` 命中且 `response_normalized` 一致。

### 4.3 模型分配（决策 D：质量优先 ✅）
- **全角色 `opus4.8`，不设硬预算**（用户确认）。仍在报告出成本/延迟（§13.4 Cost/Latency）供观测。
- 备选（未采用）：按 `agent_node_id` 分层控本——若日后要压成本，`ModelSnapshot` 可按角色映射到便宜模型；架构不变，仅改映射。

---

## 5. Agent 编排 harness（`agents/`）—— M2a

### 5.1 `agents/orchestrator.py`（自研确定性状态机 ✅B）
- `AgentNode` Protocol：`run(input) -> AgentNodeResult`；每节点 = 明确 typed I/O（§3.3）+ 失败兜底 + 经 `ModelRouter` 调 LLM。
- 确定性：节点执行顺序、`request_hash`（含 `agent_node_id` + `prompt_version`）、cassette replay 三者叠加 → 回放可复现。**不引入 LangGraph 的隐式并发/非确定性。**
- `agents/prompts/`：prompt 注册表，每 prompt 带 `prompt_version`（进 `request_hash` + 版本元组契约 5 的 `prompt_version` 槽）。
- `agent_graph_version` 常量（契约 5 版本元组槽），图结构变更即 bump。

### 5.2 备选（未采用）：LangGraph
> 决策 B = 自研，本节仅存档。若日后改用 LangGraph：编排层换成 LangGraph 图；节点 I/O 契约、Model Router、cassette 不变；须把所有调用点收敛到 `ModelRouter`（禁止节点直连 SDK）并 pin langgraph 版本进 `tool_version`。

---

## 6. 修复闭环：Repair Drafter + verifier-guided 修复搜索 —— M2a（Fix Pass Rate ≥70% 的核心）

PRD §7.9 + §7.5 硬核面 2：修复不是一次性 diff，而是 **propose → verify → refine** 多轮搜索。

### 6.1 搜索循环（`agents/repair/search.py`）
```
输入: Finding + IR 快照上下文
loop (直到通过或超搜索预算):
  1. Repair Drafter (LLM, 经 router) 产出 typed PatchDraft（TypedOp 序列，old_value 乐观并发）
  2. apply: spine/patch.apply_patch(snapshot, patch)  —— old_value 失配→拒绝→带原因回灌重试
  3. verify (确定性验证器):
       - 结构/数值: 重跑 spine 检查器套件（Graph/ASP/SMT via compile_all）→ 目标 Finding 消解且不引入新 deterministic Finding
       - 回归: 在 Aureus 跑相关任务链（Agent-Env）确认无回归（state_hash 对照）
  4. 未过 → 带反例（unsat_core / 新 Finding / 回归失败 tick）refine，重试
输出: 通过的 Patch（validation_status/regression_status 置位）→ maker-checker 审批队列
```
- **验证器是确定性的**（Clingo/z3 + Aureus 回归），不是 LLM 自评——这是 verifier-grounding，让搜索**收敛而非漂移**。
- **自动应用策略**（PRD §7.9）：仅**可证明结构性修复**（如补缺失掉落源引用）允许自动应用；数值/叙事一律人工审批。无静默一键应用。
- **maker-checker**：按域路由（经济→数值主策 / 叙事→主叙事 / 抽卡→合规），`Patch.approval_status`。
- **指标**：修复搜索效率（平均收敛步数、一次通过率）、Fix Pass Rate（复验+回归通过率）—— §13.4。

### 6.2 生成门禁（§7.6，`agents/generation/`）
Content Generator 输出**永远是提议**，进 §7.3 检查器 + §7.4 仿真门禁，未过不得进候选。generated grounded 在 Spec-IR（可用实体/区间）降幻觉。测试：故意生成越界内容 → 被门禁拦。

### 6.3 Extraction Proposer / Defect Triager / Consistency Assistant（M2a）
- **Extraction**：策划文档 → 候选实体/约束**提议** → 人审批队列（人撰写为权威）。兜底=人。测试：给定文档片段 → 提议可被人接受为 `Constraint`（契约 3 类型）。
- **Triage**：检查器/仿真 Findings → 分诊/聚类/优先级/疑似根因；**不改判确定性结论**。测试：一组 Findings → 稳定聚类，确定性结论字段不被篡改。
- **Consistency**：对话/剧情 + 叙事约束 → 疑似不一致/剧透**提示**（`oracle_type="llm-assisted"`，严格分区，绝不进确定性桶）。M2a 落基础 quorum；对抗性 perspective-diverse 辩论进阶留 M2b。这是 M1 `LlmRoutedChecker` 占位的真实评估落点。

---

## 7. 复现与测试策略（贯穿 M2）

- **CI/测试只回放**：所有 agent 测试走 `REPLAY` 模式读入库 cassette，**零实网调用**、确定性、可在 CI 跑。
- **录制是独立的、带预算的人工触发**：`RECORD` 模式打真网关生成 cassette，提交入库。录制前**先验证 `localhost:4141` 连通**（`/models` 探活，不烧 token）。
- **TDD 全程**：router/cassette/patch-search/各 agent 节点 test-first；LLM 依赖用录制好的 cassette fixture（非 mock 出的假语义——用真实录制回放，符合"可复现只承诺回放"）。
- **两条质量线延续**（§12A.1）：确定性验证器的正确性（M1 已立）+ agent 面的可验证信号（修复搜索/Playtest 以确定性预言机为奖励）。

---

## 8. M2b 概要 —— 长程 Playtest 与消融（待 M2a 完成后细化成独立 spec）

- **planner/executor 分层**：planner 从任务目标做子目标分解（高层宏动作），executor 落 Agent-Env 原子动作；两层可分别消融。
- **状态抽象**：连续 `Observation` → 可推理离散状态，压缩决策空间。
- **动作优先级**：优先与当前子目标相关动作（用 `reachable_targets`/`available_interactions`）。
- **mem-trace（action-trace 长程记忆）**：**从零自研** `agents/memory/`（决策 C 结论，质量对比见 §8.4）；`MemTrace` Protocol 接口 M2a 定形、实现 M2b；做**有/无记忆消融**。
- **反思自纠**：卡住时基于失败轨迹反思重试。
- **verifier-grounding（关键）**：LLM 对"不可达目标/死任务"的判断**必须与确定性可达性检查（spine GraphChecker + Aureus nav）交叉验证**——确定性检查=外部 ground-truth，使自纠收敛。
- **回归框架**：cassette 录/放 + 固定 model_snapshot + seed 化 Aureus；≥20 条任务链；完成率**分缺陷类 + 置信区间**诚实报告（不掩饰真实难度；§13.4 功效目标 95% CI 半宽 ≤ ±5% 反推最小 n）。
- **消融报告**：记忆消融、planner/executor 消融（完成率差）。

---

### 8.4 mem-trace 质量抉择（决策 C 结论：**从零自研**，借设计不借依赖）

**结论：从零自研 `agents/memory/`，按质量胜过任何开源库。** 两路独立研究（TITAN/学术谱系 + 开源库落地性）收敛到同一判断：对"确定性可复现 + 可干净消融 + verifier-grounded + 面向 60+ 动作 embodied playtest 的 action-trace 记忆"这一**具体**场景，开源 agent-记忆库在**质量**上不占优——这不是省不省事，是数据模型与确定性上的硬错配。

**为什么开源库在质量上不占优（4/5 轴劣势，license 轴平手）：**
- **数据模型错配（决定性轴）**：Mem0 / Zep-Graphiti / LangMem / Letta(MemGPT) / cognee / A-MEM 全是**对话/用户事实/知识图谱**记忆——记忆单元是"从文本抽取的 NL 事实/实体/笔记"（多在 LoCoMo 这类**对话** benchmark 上评测）；而我们的记忆单元是**子轨迹 `(观测, 动作, 结果)` 序列**。把轨迹塞进"文本事实抽取"管线有损且语义错，且**不可配置绕过**（是它们的核心数据模型）。
- **确定性对抗**：它们各自发起**自己的 LLM + embedding 调用**、异步/后台摘要（LangMem `ReflectionExecutor`、Graphiti 并发摄取、Letta 心跳循环）、非确定性向量 ANN 检索——正是 bit-级 cassette 回放的三大天敌。自研则：Recall=你掌控的确定性检索（零隐藏调用）、Compaction 的唯一 LLM 调用**由你写的调用点**经 Model Router + cassette 落盘，天然可回放。
- **消融不干净**：自研=一个开关翻成 `∅`；开源库"停止调用 recall"会让 A/B **被混淆**（测的是它们在错配数据模型上的抽取质量，非"轨迹记忆"的纯架构开关）；Letta 记忆嵌在 agent 循环里**无法干净消融**。
- **RL/embodied 侧无可复用件**：2025–2026 轨迹记忆工作（Trajectory-Informed Memory、Memory-R1、AgentGym-RL、SkillRL）是**研究方法/训练框架，非可 pip 安装模块**。
- **License**：全 permissive（Apache-2.0 / MIT），不构成差异。

**TITAN 校正（研究发现，给实现者）**：TITAN（arXiv 2509.22170）原文**并未**使用 "Recall/Compaction" 二词，也未给打分/压缩算法——那是 PRD 的解读标签。TITAN 真正给的是**结构**：两层——(1) episode 内 `(抽象状态 s, 动作 a*, 结果 o)` 轨迹缓冲；(2) 跨 episode 融合成**状态-动作转移图（"coverage map"）**，转移按结果打标（成功/bug/无进展）。其消融（TITAN-R）是"反思+记忆"**捆绑**移除（成功率 −6pts、**状态覆盖 −17.3pts**、bug −2）。所以具体 Recall/Compaction 操作由我们从更广文献**设计**。

**质量最优的自研设计（借设计，不借依赖）：**
- **结构**（借 TITAN + Voyager）：episodic `(状态抽象, 动作, 结果)` 轨迹 + 持久**状态-动作转移图**，键用 **state hash**（对拍 Aureus `state_hash`，精确匹配、零模型调用——确定性头号决定）；再加 Voyager 式**技能/程序化层**存"复现验证过的可复用子轨迹"。
- **Recall**（借 Generative Agents，为 verifier 改权重）：`recency × relevance × importance` 显式加权和（纯可复现算术），但把 "importance"（LLM poignancy）**换成确定性验证器判决**（可达性/进展信号作 ground-truth 权重）；embedding 模型在 Model Router pin 住 → relevance 项从 cassette 逐位回放；TITAN 式"多次无进展则降权"抑制已验证死路。
- **Compaction**（借 HiAgent + AWM + MemGPT，在验证边界触发）：三个可组合算子，全经 Router 可回放——(1) HiAgent 子目标分块；(2) AWM 工作流归纳（复现成功轨迹→技能层例程）；(3) MemGPT 递归摘要（冷跨度按上下文压力阈值）。分段边界=**验证器确认的转移**，**绝不丢弃未验证/异常分支**（那可能正是 bug——预言机的意义）。
- **反思**（借 Reflexion）：验证器判负时写"判决条件化"言语反思进 episodic 层（TITAN"反思自纠"的具体化，天然轨迹+判决 grounded）。
- **消融接口**：`recall(state, task) -> K 条注入项` 单一接口，A/B 翻 `∅`；记忆严格不进策略/prompt 权重（排除 A-MEM 式原地 evolution——它破坏回放确定性）。

**接口一次定全（`no-simplification`）**：`agents/memory/` 的 `MemTrace` Protocol（`record(step)` / `recall(state, task, k) -> items` / `compact(trace, verdicts) -> compacted` / `reflect(failed_trace, verdict) -> note`）在 **M2a 契约期定形**、实现落 **M2b**；日后若要换某库只换实现不换接口。

**参考**：TITAN 2509.22170 · Generative Agents 2304.03442 · MemGPT/Letta 2310.08560 · Reflexion 2303.11366 · Voyager 2305.16291 · Agent Workflow Memory 2409.07429 · HiAgent(ACL'25) · A-MEM 2502.12110。（开源库落地性：Mem0/Zep-Graphiti/LangMem/Letta/cognee/A-MEM 均对话/KG 记忆，非轨迹记忆。）

---

## 9. 分期表（延后 ≠ 简化：接口现定，实现分批）

| 元素 | 接口落点（现定） | 实现落点 |
|---|---|---|
| 契约 §7（ModelRouter/Cassette/request_hash） | `contracts/model_router.py`+`cassette.py`（M2a 首刀） | `runtime/` M2a |
| 6 agent 角色 I/O 契约 | `contracts/agent_io.py`（M2a 首刀，6 个全定） | Extraction/Triage/Repair/Consistency/Generation @M2a；Playtest @M2b |
| verifier-guided 修复搜索 | `agents/repair/` | M2a |
| 长程 Playtest（planner/executor/mem-trace/反思） | `agents/playtest/` + `agents/memory/` 接口 | M2b |
| 对抗性叙事 quorum | Consistency Assistant 接口 @M2a | 进阶 quorum @M2b |
| GameForge-Bench 聚合（§7.10/§13） | Finding 标准格式已定 | M3 |
| RBAC/审批工作流前端、可观测/成本面板 | maker-checker 字段已在 `Patch` | M4（M2 出后端闭环 + 指标，前端全页 M4） |

---

## 10. 开放问题（非阻塞，可在 plan 阶段定）
- `≥20 条任务链`场景来源：复用 outpost + 新作若干 + 可选用 Content Generator 生成（经门禁）——在 M2b plan 锁定具体 20+ 场景清单。
- cassette 体积治理：若入库过大，用 `response_normalized` 精简 + gzip；M2a plan 定阈值。
- 语义缓存 key 的稳定前缀边界：M2a plan 定"KG+约束前缀"具体切分。
