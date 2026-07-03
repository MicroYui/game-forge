# GameForge — 游戏内容正确性编译器与生产级 Agent 工作台
## 产品需求文档（PRD）

| 字段 | 值 |
|---|---|
| 文档状态 | Draft v0.1（待评审） |
| 日期 | 2026-07-03 |
| 产品工作代号 | **GameForge**（注：与 GameForge GmbH 商标冲突，若未来外部发布需改名；当前定位非商业化，暂用此代号） |
| 参考游戏代号 | **Aureus**（自研 live-service 风格 2D RPG，作主测试床） |
| 定位 | 生产级工程完整度（**非**商业化/上市/对外售卖）——衡量标准是工程深度与产品完整度 |
| 关联文档 | `idea.md`（原始构想）、多视角评估结论（见附录 A） |

---

## 0. 一句话定义

> **GameForge 是一个面向游戏内容的"正确性编译器 + 生产级 Agent 工作台"**：把策划案 / 配置表 / 内容补丁作为输入，构建可版本化、可查询的 **Design-Spec IR（知识图谱 + 类型化约束存储）**，用**确定性检查器 + 经济仿真**做**可判定的**验证（而非"一个 LLM 给另一个 LLM 打分"），用**有边界的 LLM Agent 层**做约束抽取*提议*、缺陷分诊、修复*起草*，并在一个**真实可运行的参考游戏 Aureus** 里由 **Playtest Agent** 执行任务链做闭环回归——全程可观测、可复现、可审计、人工审批。

---

## 1. 执行摘要（TL;DR）

GameForge 解决的是游戏研发管线里一个**被现有产品跳过的缝**：**作者时刻（authoring-time）与 live-ops 补丁时刻**的内容正确性。

- 现有"AI 进游戏做测试"的产品（网易伏羲 **TITAN**、腾讯 WeTest **Acorn**）都在 **QA 时刻**测**已经做好的游戏**，靠像素/UI/状态驱动，缺陷发现得**晚**。
- **没有人**在配置层、在内容尚未进引擎前，就 lint 任务 DAG、掉落表、经济配置、剧情一致性。这就是 GameForge 的位置——**shift-left**。

GameForge 的可辩护内核由三根支柱构成，每一根都刻意规避了"LLM 自评"的信任陷阱：

1. **双向 Design-Spec IR**：同一份类型化约束存储，既**约束生成**，又**编译成可执行的测试预言机**。现有工作只用约束在单边。
2. **确定性预言机**：可达性/引用完整性/环依赖用图算法 + ASP(Clingo)，数值不变量用 SMT(z3)（限可判定理论片段；非线性整数/期望/幂律曲线等给有界近似或标注"未证明"），经济平衡用 Monte-Carlo/ABM 仿真（**描述性证据**，不主张 precision）——判定部分**给定正确约束下 sound 且回放可复现**，LLM 不参与"判对错"。
3. **开放的 seeded-defect Benchmark**（"游戏内容界的 SWE-bench"）+ **开源游戏外部效度校验**：公开不存在的、带缺陷分类学的可复现内容缺陷基准，堵死"在自己写的内容上自评"的批评。

LLM Agent 层是**真实但有边界**的：负责约束抽取*提议*（人审批）、缺陷*分诊*、修复 diff *起草*（检查器复验 + 人审批）、跨版本一致性*提示*、以及在 Aureus 里做**扩展 TITAN 思路**的 Playtest。没有任何 LLM 输出在没有确定性预言机或人工兜底的情况下被采信。这一层不是"多 agent 聊天"，而是 **verifier-grounded** 的硬核面——长程 Playtest、验证器引导的修复搜索、agentic 约束抽取、对抗性叙事验证——把 agent 建在可验证地基上（删掉 LLM-as-judge 不是减少 agent，而是给 agent 一个让自纠循环收敛的外部奖励信号）。

本 PRD 覆盖一个完整的、多子系统的生产级平台，规划为 M0–M4 五个里程碑，每个里程碑有明确的进入/退出准则与量化验收标准。

---

## 2. 背景与问题陈述

### 2.1 现状与痛点

现代 live-service RPG / 抽卡游戏的内容是**数据驱动**的：任务、对话、掉落表、数值曲线、经济参数、活动/卡池，绝大部分以外部配置表（Excel/Luban/protobuf/ScriptableObject/DataTable）形式存在，由策划/数值手写或工具生成。这些配置之间有大量**隐式约束**：

- **结构约束**：每个 `collect` 任务的道具必须有掉落源；每个 `talk` 任务必须有对应 NPC；任务前置图不能有环；每个任务目标必须可达。
- **数值/经济约束**：新手区金币奖励 ≤ 上限；货币产出/回收（sink/source）需平衡；掉落概率之和 = 1；装备强度曲线单调；抽卡期望符合披露。
- **叙事/世界观约束**：角色 A 在第 N 章前不能暴露身份；阵营 X 与 Y 不可合作；唯一神器不能作为普通掉落；对话不能泄露未解锁剧情。

**痛点**：这些约束今天靠**人肉审查 + 零散的构建期脚本 + 上线后玩家反馈**来兜底。一个漏掉的死任务或崩坏的经济，在 live-service 抽卡游戏里会直接导致**玩家补偿事件**（发放代币/退款）——有硬性、公开的失败代价。

### 2.2 现有方案为何不覆盖

| 方案 | 覆盖点 | 缺口 |
|---|---|---|
| TITAN（网易伏羲）/ Acorn（腾讯 WeTest） | QA 时刻，LLM Agent 驱动**已构建**的游戏找 bug，95% 任务完成率 | 缺陷发现**晚**；不 lint 作者时刻的配置；不做经济不变量的形式化验证 |
| 工作室内建构建期校验脚本 | 部分结构校验（引用完整性等） | 零散、无统一 IR、无经济仿真、无叙事一致性、无修复闭环、无版本血缘、无 benchmark |
| Machinations.io | 经济仿真 | 无 LLM、无叙事、无可达性、不接内容管线 |
| articy:draft / Ink / Yarn Spinner | 叙事创作 | 无可达性/经济验证、无检查闭环 |
| Self-Refine / RepairAgent / SWE-bench 范式 | 通用"生成→验证→修复"闭环 | 通用软件，非游戏内容领域；无游戏专用预言机 |

**结论**：GameForge 占据的是"**作者时刻/补丁时刻 × 配置层 × 生成-验证-修复闭环 × 游戏专用确定性预言机**"这个没人占的格子。

### 2.3 我们如何避免"自评作业"陷阱

评估阶段最尖锐的批评是：*"LLM 生成配置，兄弟 LLM 祝福它，Phaser 玩具'测试'它，缺陷是作者自己埋的——零外部效度。"* 本产品从架构上系统性地回应：

- **判对错的是确定性预言机**，不是 LLM。
- **测试床是真实可运行的参考游戏 Aureus**（真实任务/战斗/导航/经济系统），不是玩具。
- **外部效度**：接入一个开源数据驱动游戏的真实配置与真实 commit/补丁历史做交叉验证，指标不只在自建内容上报告。
- **false-positive rate 是第一 KPI**（误报会让资深策划两次误判后就关掉工具）。

---

## 3. 定位、目标、非目标

### 3.1 产品目标

| # | 目标 | 成功判据（概览，详见 §16） |
|---|---|---|
| G1 | 构建可版本化、可查询、类型化的 Design-Spec IR，并能与真实风格 schema 双向往返 | Aureus + 1 个开源游戏的配置可无损导入/导出 IR |
| G2 | 确定性检查器对**结构/数值**缺陷做 sound 验证（限可判定理论片段） | 给定正确约束下结构类 **spurious-violation = 0**（algorithmic soundness，见 §16 注） |
| G3 | 经济仿真引擎对数值/经济不变量给出 what-if 报告 | 能复现一次真实经济崩坏并提前预警 |
| G4 | 有边界的 LLM Agent 层做抽取提议/分诊/修复起草，每步有预言机或人兜底 | 修复 diff 经检查器复验 + 人审批后回归通过率达标 |
| G5 | Aureus 参考游戏 + Agent-Env 契约 + Playtest Agent 闭环回归 | Playtest Agent 可靠跑通多步任务链，记录卡死/失败/循环/异常 |
| G6 | GameForge-Bench：开放 seeded-defect 语料 + 开源外部校验 + 完整指标体系 | 分缺陷类报告 BDR、false-positive、human-edit-distance、QA 工时节省、成本/延迟 |
| G7 | 生产级工程：版本血缘、审计、可观测、可复现、人工审批工作流 | 每个内容产物可追溯到 (doc, KG, prompt, model, agent-graph) 版本并可回滚 |

### 3.2 非目标（明确排除）

- ❌ 商业化 / 定价 / GTM / 护城河 / 上市
- ❌ 对外多租户计费、SSO 商业集成、SOC2/等保合规认证
- ❌ 外部客户 IP 安全、私有化/VPC/气隙部署、跨境数据合规
- ❌ 迁就外部第三方工作室的差异化管线集成（只需支撑自建 Aureus + 1 个开源游戏）
- ❌ 真实商用引擎（Unreal/Unity）的 headless playtest SDK（作为 v-next 远期，不在本 PRD 主范围）
- ❌ "有趣/节奏/玩家体验/Balance Score" 这类**主观质量评分**（评估判定为过度承诺——平衡是从遥测/A/B 涌现的，不可由静态配置计算）
- ❌ 静默一键自动应用到活配置（只对**可证明可校验的结构性修复**允许自动应用，其余必须人工审批）

### 3.3 与 idea.md 的差异（重心迁移）

| idea.md 押注 | 本 PRD 的处理 |
|---|---|
| LLM 生成内容为旗舰 | 降为**提议**，永远经检查器门禁；不是价值主张头条 |
| 5 个评审 Agent | 结构/数值"评审"改由确定性检查器、经济由仿真承担（**均非 LLM**）；**LLM 评审仅保留叙事一致性 1 个**；删掉 fun/pacing/player-experience/Balance-Score |
| LLM Playtest 为最大差异点 | 重新定位为**扩展 TITAN** 的一环；价值主干在作者时刻的确定性验证 |
| Phaser 玩具 demo | 升级为真实可运行的 Aureus（确定性内核 + 薄渲染） |
| pgvector 作约束存储 | 约束存储用图/三元组 + Datalog/ASP + SMT；pgvector 仅用于语义检索等辅助 |
| 静默一键修复 | maker-checker 人工审批；自动应用仅限可证明结构性修复 |

---

## 4. 目标用户与核心旅程

### 4.1 用户角色（Persona）

| 角色 | 关心 | 在 GameForge 里做什么 |
|---|---|---|
| **内容策划**（任务/剧情） | 任务能不能跑通、有没有死任务、对话会不会剧透 | 上传/编辑任务与对话；看 Review Report；审批修复 diff |
| **数值/经济策划** | 经济会不会崩、掉落/抽卡期望对不对 | 看经济仿真 what-if 报告；审批数值相关修复；定义经济不变量 |
| **QA / 内容质量** | 回归覆盖、缺陷分类、复现 | 跑 Playtest 回归；看轨迹与失败点；维护缺陷语料 |
| **工具/主程** | 管线接入、schema、可复现、成本 | 配 Schema Registry 适配器；管版本血缘/审计；看可观测面板 |
| **约束/知识管理员** | IR 正确性、约束库质量 | 审批 LLM 抽取的约束提议；维护约束库 |

### 4.2 核心用户旅程

**旅程 A — 作者时刻（greenfield 生成-验证-修复）**
1. 策划输入目标（"给新手村一个 3–5 分钟支线，含 1 对话 1 探索 1 轻战斗，奖励不破坏早期经济，不暴露主线角色白鸢身份"）。
2. 生成 Agent 产出任务配置/对话/怪物/掉落**提议**（grounded 在 Spec-IR）。
3. 确定性检查器（结构/数值）+ 经济仿真 + **LLM 叙事一致性助手**（唯一的 LLM 评审，输出一律标注为"建议·需人确认"）→ 统一 Review Report（分严重度，附建议修复）。
4. Playtest Agent 在 Aureus 跑任务链 → 记录卡死/失败/循环/异常。
5. 修复 Agent 起草 typed patch → 检查器复验 + 回归 → 人工审批 → 应用。
6. Dashboard 展示通过率、缺陷分类、工时节省、成本/延迟。

**旅程 B — Live-Ops 补丁回归门（推荐主循环，信任风险最低）**
1. 策划提交一次人写的内容补丁（新卡池/活动/数值调整）。
2. 系统对**版本化不变量基线 + 上一版经济仿真**做**diff 驱动**的回归校验。
3. 检测到回归（经济崩坏/死任务/引用断裂/概率异常）→ 阻断 changelist，附最小复现与建议修复。
4. 人工审批修复后放行；历史热修/回滚/补偿事件自动沉淀为**真实缺陷语料**。

> 注：旅程 B 的配置**已存在**（几乎不需要 LLM 生成），信任与复现风险最低，且提供免费的真实标注语料；旅程 A 展示完整"生成-验证-修复"能力。两者共享同一套 IR / 检查器 / 仿真 / 修复工作流。

---

## 5. 产品原则（贯穿全系统的架构约束）

1. **确定性优先（Determinism-first）**：任何"对/错"判定尽量由可判定算法给出；LLM 只做"提议/起草/提示"。
2. **预言机背书（Oracle-backed claims）**：每一条被报告的缺陷，要么来自确定性检查器/仿真，要么标注为"LLM 建议（需人确认）"，两类在 UI 与指标里**严格分开**。
3. **有边界的 LLM（Bounded LLM）**：LLM 的每个角色都有明确的输入/输出契约与失败兜底（检查器复验或人审批）。
4. **人在环（Human-in-the-loop）**：maker-checker 审批；经济→数值主策，叙事→主叙事，抽卡→合规角色。自动应用仅限可证明结构性修复。
5. **可复现（Reproducibility）**：**回放可复现**（固定模型快照 + cassette 录制/回放 + seed 化环境 → 同快照同结果）；全新运行打在线模型**不保证 bit 级复现**，故一切回归/评测走录制-回放。
6. **外部效度（External validity）**：指标不只在自建内容上报告，必接开源游戏真实语料交叉验证。
7. **false-positive 是第一 KPI**：宁可漏报不可乱报——误报摧毁信任。
8. **版本即函数**：`config = f(doc, IR快照, 约束快照, prompt, model, agent图, tool, env契约, seed, cassette)`（完整元组见地基契约 §契约5），全链路版本化、可血缘、可回滚、可审计。

---

## 6. 系统总体架构

### 6.1 分层与组件

```
┌─────────────────────────────────────────────────────────────────────┐
│  Web Console (React + TS)  — Spec/KG · Generation · Review · Playtest │
│  · Patch/Diff · Eval/Bench · Observability · Approvals               │
└───────────────┬─────────────────────────────────────────────────────┘
                │  REST / WebSocket / SSE
┌───────────────▼─────────────────────────────────────────────────────┐
│  API Gateway & Orchestration (FastAPI + LangGraph/自研状态机)         │
├──────────────────────────────────────────────────────────────────────┤
│  ┌── 可信主干（确定性）─────────────┐  ┌── 有边界 Agent 层（LLM）──┐   │
│  │ Spec-IR Store (KG + Constraints) │  │ Extraction Proposer      │   │
│  │ Checker Suite (Graph/ASP/SMT)    │  │ Defect Triager           │   │
│  │ Economy Simulator (MC/ABM)       │  │ Repair Drafter           │   │
│  │ Regression Harness (cassette)    │  │ Consistency Assistant    │   │
│  └──────────────────────────────────┘  │ Content Generator        │   │
│  ┌── 参考游戏 Aureus ───────────────┐  │ Playtest Agent (扩展TITAN)│   │
│  │ Deterministic Core + Env Contract│  └──────────────────────────┘   │
│  │ Thin 2D Render (demo)            │                                 │
│  └──────────────────────────────────┘                                │
├──────────────────────────────────────────────────────────────────────┤
│  Ingestion & Schema Registry (Luban/Excel-style adapters, round-trip) │
│  Version & Lineage Store · Audit · Approval Workflow · RBAC           │
│  Observability (tracing/metrics/logs · cost/latency) · Model Router   │
├──────────────────────────────────────────────────────────────────────┤
│  存储：Postgres(关系) · 图/三元组存储(约束/KG) · 对象存储(cassette/工件)│
│         pgvector(语义检索辅助) · 时序(指标)                            │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.2 服务边界（每个单元：做什么 / 怎么用 / 依赖什么）

- **Spec-IR Store**：唯一权威的内容语义源。对外提供类型化实体/关系/约束的 CRUD + 查询 API。依赖图/三元组存储 + 版本存储。
- **Checker Suite**：无状态计算服务，输入 IR 快照 → 输出结构化 Finding（确定性）。依赖 Spec-IR。
- **Economy Simulator**：无状态计算服务，输入经济配置 + 场景 → 输出 what-if 报告。依赖 IR 数值子图。
- **Agent Layer**：编排的 LLM 工作流，输出永远经检查器/人门禁。依赖 Spec-IR、Checker、Model Router。
- **Reference Game (Aureus)**：确定性内核，暴露 Agent-Env 契约。被 Playtest Agent 与 Regression Harness 驱动。依赖配置（由 IR 导出）。
- **Regression Harness**：跑回归、录/放 cassette、统计显著性。依赖 Aureus、Agent Layer、Version Store。
- **Patch Workflow**：typed patch 的复验/审批/应用/回滚。依赖 Checker、Regression、Approval、Version Store。

**数据流主线**：`Doc/Config → Ingestion → Spec-IR → {Checkers, Simulator, Agents} → Findings → Patch(draft) → 复验+回归 → Approval → Apply → Version/Lineage → Dashboard/Bench`。

---

## 7. 子系统详述

### 7.1 Design-Spec IR（知识图谱 + 约束存储）

**目标**：一个类型化、可版本化、可查询的中间表示，作为"内容语义"的唯一权威源，且**双向**——既约束生成，又编译为预言机。

**组成**：
- **实体图（Knowledge Graph）**：类型化属性图。节点/边**全集以地基契约 §契约2 为准**（core 类型 M0a 实现，combat-economy 类型如 `Skill/StatusEffect/Effect/BattleEncounter/Formula/Equipment` M0b 实现）；关键补充：`Quest --has_step--> QuestStep` 显式建边（不塞 attrs）、`SpawnPoint`/`Interactable`/`UnlockCondition` 支撑可达性与叙事门控、`DropTable`/`RewardTable`/`GachaPool` 三类奖励源分开。Relation 带 `id`（多重边 + 可被 Finding/Patch 精确指向）与 `source_ref`（round-trip + minimal repro）。
- **约束存储（Constraint Store）**：类型化、机器可校验的约束，分三族：
  - 结构约束（可达性/引用/环）→ 编译到 Graph/ASP 检查器
  - 数值约束（不变量/区间/单调）→ 编译到 SMT 检查器
  - 叙事约束（一致性/剧透）→ 部分可形式化（时序/解锁关系），其余交 LLM 辅助 + 人确认

**约束来源**（关键信任设计）：约束**由人撰写为权威**；LLM 只**提议**候选约束进入审查队列（见 §7.6）。Live-ops 场景下，约束可由"上一版已知良好配置"**diff 驱动**地半自动生成（基线不变量），大幅降低人工撰写成本。

**约束 DSL 示例**（YAML，编译到对应检查器；每个谓词标 `oracle` 类型）：
```yaml
- id: C_newbie_gold_cap
  kind: numeric        # -> SMT
  oracle: deterministic
  scope: {region: newbie_zone, field: quest.reward.gold}
  assert: "value <= 80"
  severity: major
- id: C_collect_needs_source
  kind: structural     # -> Graph/ASP
  oracle: deterministic
  forall: {step: QuestStep, type: collect}
  assert: "exists DropTable d where d.item == step.item and d.reachable_in(step.quest.region)"
  severity: critical
# 叙事约束拆成两半：可形式化的时序门控（确定性）+ 语义判定（LLM 助手，需人确认）
- id: C_baiyuan_gate_deterministic
  kind: narrative
  oracle: deterministic          # 纯时序/解锁关系，可判定
  assert: "no DialogueNode tagged(reveals_identity=白鸢) is unlocked before chapter>=3"
  severity: critical
- id: C_baiyuan_semantic_llm
  kind: narrative
  oracle: llm-assisted           # '这段对话是否在语义上泄露身份' 需 LLM 判断
  assert: "no DialogueNode semantically reveals 白鸢 true_identity before chapter>=3"
  severity: critical
  note: "Finding 归 'LLM 建议·需人确认'，不进确定性检出"
```

**IR 快照示例**（节选）：见附录 C。

**版本化**：IR 每次变更产生不可变快照；实体/约束带版本；支持时间旅行查询与两快照 diff。

### 7.2 内容摄取与 Schema Registry

**目标**：把真实风格的工作室配置格式导入/导出 IR，无损往返。

- **Schema Registry**：注册每种配置格式的 schema（字段、类型、外键、枚举）。首发支持：① Aureus 的 Luban/Excel 风格表；② 1 个开源游戏的真实配置。
- **Adapter**：`format ↔ IR` 双向映射；保留原始字段以便导出回原格式（round-trip 无损）。
- **校验**：导入时做 schema 级校验（类型/外键/枚举），把语法层错误挡在 IR 之外。
- **增量摄取**：支持 diff 摄取（只处理变更的表/行），服务 live-ops 补丁场景。

### 7.3 确定性检查器套件（Checker Suite）

**原则**：可判定属性由算法判定，**给定正确约束下 sound**（报告的结构缺陷必为真——algorithmic soundness；**不含**"约束本身被人误批"的情形，那类归 constraint-FP，见 §15-R3）；输出结构化 Finding（含最小复现路径）。

| 检查器 | 技术 | 检查内容 | 输出 |
|---|---|---|---|
| **Graph Checker** | 图算法（BFS/DFS/SCC/拓扑） | 引用完整性（悬挂引用）、环依赖（任务前置成环）、可达性（目标区域/道具源可达）、孤立节点 | Finding + 具体断裂边/环路径 |
| **ASP Checker** | Datalog/ASP（Clingo） | 任务可解性、依赖满足、复杂组合约束（"每个 collect 有源且源在可达区"） | 反例 / 满足性证明 |
| **SMT Checker** | z3 | 数值不变量（奖励上限、概率和=1、曲线单调、区间约束）、跨字段一致性 | UNSAT core / 违反赋值 |

**设计要点**：
- 检查器从 §7.1 的约束**编译**而来（约束 DSL → Clingo/z3 程序），不是硬编码 if-else。
- 每个 Finding 带**最小复现**（具体是哪条任务的哪一步、哪张表哪一行、违反哪条约束）。
- 反循环性防护：检查器**不**依赖 benchmark 的 seeded 分类学定义自身能力，而是独立于约束库实现；见 §13.4 反 gaming。
- **Solver 预算与降级**：ASP grounding 设规模上限、z3 设超时预算；返回 `unknown`/超时/grounding 超限一律降级为"**未证明**"（绝不当作"通过"）；非线性整数等不可判定片段用有界近似并显式标注"不完备结论"。
- **谓词 oracle 类型标注**：约束的每个谓词标 `deterministic` 或 `llm-assisted`；只要一条约束含 `llm-assisted` 谓词，其 Finding 即归"LLM 建议（需人确认）"，**不进确定性检出统计**。

### 7.4 经济仿真引擎（Economy Simulator）

**目标**：对数值/经济属性给出**what-if 报告**（描述性，永不擅自给出"应改成 X"的处方数字）。

- **方法**：Monte-Carlo + agent-based（模拟玩家群体行为分布，跑 N 局）。
- **验证的不变量**：货币 sink/source 平衡、通胀率、掉落源存在性与产出速率、装备强度曲线、抽卡期望与保底、资源产出速率上限。
- **输出**：分布图 + 违反的不变量 + 敏感度（改哪个参数影响多大）。作为**证据**供数值策划决策，不作处方。
- **Live-ops 用法**：对新补丁跑仿真，与上一版基线对比，报告经济偏移。

### 7.5 LLM Agent 层（有边界）

**编排**：LangGraph / 自研状态机；每个 Agent 是明确输入/输出契约的节点；失败有兜底。

| Agent 角色 | 输入 | 输出 | 兜底 |
|---|---|---|---|
| **Extraction Proposer** | 策划文档 | 候选实体/约束**提议** | 进人审批队列，人撰写为权威 |
| **Defect Triager** | 检查器/仿真 Findings | 分诊/聚类/优先级/疑似根因 | 不改判确定性结论，仅整理 |
| **Repair Drafter** | Finding + IR 上下文 | typed patch **草案** | 检查器复验 + 回归 + 人审批 |
| **Consistency Assistant** | 对话/剧情 + 叙事约束 | 疑似不一致/剧透**提示** | 标注为"建议"，人确认 |
| **Content Generator** | 策划目标 + IR | 内容**提议**（配置/对话） | 永远经检查器门禁（§7.6） |
| **Playtest Agent** | Agent-Env 观测 | 动作序列 + 缺陷报告 | 在 Aureus 真实环境验证（§7.7-7.8） |

**护栏**：所有 LLM 调用经 Model Router（可切换 Claude/Qwen/DeepSeek/GLM 等），带 prompt/语义缓存（复用稳定的 KG+约束前缀降本）、成本配额、超时/重试/降级。

**硬核 agent 面（verifier-grounded，非浅封装）**：本层刻意规避"多 agent 聊天"式 theater，把深度放在四个真正困难、且当前前沿（RLVR / verifier-grounded agents）的面上，每个都以确定性预言机为**可验证信号**——这也是回应"agent 不够硬核"质疑的地方：
1. **长程 Playtest Agent**（§7.8）：60+ 动作、部分可观测，需 planner/executor 分层 + 状态抽象 + 长程记忆 + 反思自纠；确定性可达性检查充当其"对/错"外部奖励信号。
2. **verifier-guided 修复搜索**（§7.9）：在补丁空间里由 Clingo/z3 + 回归引导的多轮 propose→verify→refine 搜索（SWE-agent/AlphaCode 式），非一次性 diff。
3. **agentic 约束抽取**（Extraction Proposer）：从非正式策划案 spec-mining 出类型化约束，人审为权威。
4. **对抗性叙事验证**（Consistency Assistant 的进阶形态）：叙事一致性用 **perspective-diverse 辩论 / quorum 投票**（有机制才上多 agent，不为多而多）。

> 核心论点：**删掉 LLM-as-judge 不是减少 agent，而是把 agent 建在可验证地基上**——LLM 自纠在缺乏外部 ground-truth 时会退化（self-correction 综述结论），确定性预言机正是让 agent 自纠循环收敛的那个信号。

### 7.6 内容生成（Generation-as-proposal）

- 生成 Agent 的输出**永远是提议**，进入 §7.3 检查器 + §7.4 仿真门禁，未过门禁不得进入候选。
- 生成 grounded 在 Spec-IR（可用实体/区域/道具/数值区间），减少幻觉。
- NPC 对话被降级为"一致性检查过的工件"，不是价值头条。

### 7.7 参考游戏 Aureus 与 Agent-Env 契约

**Aureus 定位**：自研、数据驱动、live-service 风格 2D 俯视 RPG，作为**真实（非玩具）测试床**。

**确定性内核（Python，权威逻辑，tick-based）**，包含真实系统：
- **任务系统**：talk/collect/fight/escort/deliver，前置图、分支、状态机。
- **战斗系统**：属性/技能/伤害公式、怪物 AI、命中/闪避（伪随机可 seed）。
- **导航**：网格/navmesh 寻路，可达性真实（不是"瞬移到 NPC"）。
- **背包/经济**：货币、商店、掉落表、抽卡式奖励、库存约束。
- **配置加载**：全部由 IR 导出的配置表驱动。
- **日志**：结构化事件流（quest step completed、combat resolved、item acquired）。

**薄渲染层**：一层 2D 渲染仅用于演示/审查（回放 Playtest 轨迹、人工审查失败点），**不参与权威逻辑**——保证 headless 可复现。

**Agent-Env 契约**（Gym / dm_env 风格，引擎无关）：
```
reset(scenario) -> Observation
step(action) -> (Observation, reward, done, info)
Observation: {player_pos, current_quest, quest_state, inventory, hp, nearby_entities, dialogue_options, logs}
Action: move_to(target) | talk(npc) | choose(option) | attack(enemy_group) | use(item) | pickup(item) | accept_quest | turn_in
```
这层契约让 Playtest Agent 通过**结构化观测/动作**驱动，而非视觉控制；同时为未来接真实引擎（v-next）预留同一契约。

### 7.8 自动化 Playtest 与回归框架

**Playtest Agent（长程 agent，扩展 TITAN 思路；本项目最硬核的 agent 面）**：
一个 60+ 动作、部分可观测的长程任务——单动作 97% 成功率在 60 动作上仅约 16% 完成率（WebArena/OSWorld 天花板才 14–40%），可靠跑通是真 agent 研究，非壳。设计：
- **规划/执行分层（planner/executor）**：planner 从任务目标做高层子目标分解，executor 落到 Agent-Env 原子动作；两层可分别消融评估。
- **状态抽象（state abstraction）**：把连续观测抽象成可推理的离散状态，压缩决策空间。
- **动作优先级（action prioritization）**：优先探索与当前子目标相关的动作。
- **长程记忆（action-trace memory）——一等公民**：直接复用作者的 mem-trace（Recall / Compaction）。TITAN 消融证明该模块对结果材料级重要，是本项目独有护城河；做**有/无记忆的消融对比**。
- **反思自纠（reflective self-correction）**：卡住时基于失败轨迹反思重试。
- **verifier-grounding（关键）**：LLM 预言机对"不可达目标/死任务"的判断**必须与确定性可达性检查交叉验证**——确定性检查充当外部 ground-truth，使自纠**收敛而非漂移**。
- **诚实报告**：完成率分缺陷类报告并带置信区间，绝不用玩具世界高分掩饰真实难度。

**回归框架（Regression Harness）——复现性核心**：
- **cassette 录制/回放**：录下 LLM 交互与环境轨迹；回归时回放，隔离非确定性。
- **固定模型快照**：pin 模型版本，避免"模型被悄悄弃用/升级"导致不可复现。
- **确定性环境**：Aureus 内核 seed 化，同 seed 同结果。
- **统计严谨**：报告置信区间；n 要足够（评估指出 n=50 的 ±14% 半宽是欠功效噪声）——用更大样本或按缺陷类分层报告。

### 7.9 修复与补丁工作流（Patch Workflow）

- **Typed domain patch**：修复表达为对 IR 的类型化补丁（不是自由文本 diff），可被检查器复验。
- **verifier-guided 修复搜索（agentic program synthesis）**：修复不是一次性 LLM diff，而是 **propose → verify → refine** 的多轮搜索——Repair Drafter 提议 typed patch，Clingo/z3 检查器 + Aureus 回归充当**验证器**给出反馈（反例/未过项），未过则带反例重试，直至通过或超搜索预算。报告**搜索效率**（收敛步数、一次通过率）——这是 SWE-agent/AlphaCode 式的可验证 agentic synthesis，而非壳。
- **maker-checker 审批**：按域路由（经济→数值主策，叙事→主叙事，抽卡→合规）。
- **自动应用策略**：仅**可证明可校验的结构性修复**（如补一个缺失掉落源引用）允许自动应用；数值/叙事修复一律人工审批。**无静默一键应用**。
- **可审查 diff 视图**：原配置 ↔ 修复后配置的结构化 diff；一键应用/回滚。

### 7.10 关于 §13 Benchmark 的接口

Playtest/检查器/仿真的所有 Finding 与修复结果，都以标准化格式落库，供 §13 GameForge-Bench 聚合与评测。

---

## 8. 数据模型与版本治理

- **版本即函数**：每个内容产物记录其生成的完整来源（**完整元组以地基契约 §契约5 为准**）：`config = f(doc_version, ir_snapshot_id, constraint_snapshot_id, prompt_version, model_snapshot, agent_graph_version, tool_version, env_contract_version, seed, cassette_id?)`。
- **不可变快照 + 血缘图**：IR、配置、检查结果、Playtest 轨迹、patch 均版本化并连成血缘 DAG。
- **回滚**：任意产物可回滚到历史快照。
- **审计轨迹**：谁在何时提议/审批/应用了什么，不可篡改地记录。
- **可审查 diff**：任意两版之间的结构化 diff。

---

## 9. 可观测性 / 可靠性 / 复现 / 成本

- **Tracing**：每次 Agent 运行、检查、仿真、Playtest 有端到端 trace（OpenTelemetry 风格）。
- **Metrics**：任务成功率、缺陷检出、误报率、修复通过率、成本、延迟（时序库）。
- **Logs**：结构化日志，可关联 trace。
- **成本/延迟治理**：Model Router 层做**成本作为控制而非仅指标**——per-run 配额、语义缓存（复用 KG+约束稳定前缀）、成本分层路由（一次 50 任务回归可能烧掉大量 token）。
- **可靠性**：幂等、重试、超时、降级；关键路径断路器。
- **复现**：固定模型快照 + cassette 回放贯穿 CI。

---

## 10. 权限与人工审批（内部）

- **RBAC**：角色（内容策划/数值/QA/工具/约束管理员）→ 权限。**内部**，不涉及对外 SSO/计费。
- **审批工作流**：maker-checker；按缺陷域路由到对应负责人。
- **职责分离**：提议者 ≠ 审批者。

---

## 11. 前端 Web Console 规格

| 页面 | 内容 |
|---|---|
| **Spec / KG** | 上传/编辑世界观/角色/任务/数值；可视化知识图谱；约束库管理与审批队列 |
| **Generation** | 输入策划目标；查看生成提议及其门禁结果 |
| **Review** | 统一 Review Report：按严重度分组，确定性缺陷 vs LLM 建议**分区展示**，附最小复现与建议修复 |
| **Playtest** | 自动跑任务；回放轨迹（薄渲染）；失败/卡死/循环点标注 |
| **Patch / Diff** | 结构化配置 diff；复验/回归结果；一键应用/回滚；审批操作 |
| **Eval / Bench** | 多轮 benchmark 指标：分缺陷类 BDR、误报率、修复通过率、human-edit-distance、QA 工时节省、成本/延迟 |
| **Observability** | trace/指标/日志/成本面板 |
| **Approvals** | 待我审批队列 |

---

## 12. 技术栈与实现选型

| 层 | 选型 | 备注 |
|---|---|---|
| 语言核心 | **Python** | Agent 编排、仿真、检查器、Clingo/z3 绑定都在 Python，生态最全 |
| 前端 | **React + TypeScript + Vite** | Dashboard；图可视化（Cytoscape/d3）；diff 视图 |
| 后端/API | **FastAPI** | REST + WebSocket/SSE |
| Agent 编排 | **LangGraph / 自研状态机** | 有边界节点 + 兜底 |
| 约束/KG 存储 | **图/三元组存储**（如 Neo4j / RDF / 关系建模的图）+ Datalog/ASP | **不用 pgvector 作主约束存储**（评估判定其为"RAG 思维"错配） |
| ASP | **Clingo** | 可达性/依赖/可解性 |
| SMT | **z3** | 数值不变量 |
| 经济仿真 | 自研 MC/ABM（NumPy/SimPy 风格） | what-if 报告 |
| 参考游戏 Aureus | Python 确定性内核 + 薄 2D 渲染（如 pygame / 前端 canvas 回放） | headless 权威逻辑 |
| 关系存储 | **PostgreSQL** | 关系数据、版本、审计 |
| 语义检索辅助 | **pgvector** | 仅辅助（文档检索等），非约束存储 |
| 可观测 | OpenTelemetry + 时序库（Prometheus 风格） | trace/metrics/logs |
| 对象存储 | S3 兼容 / 本地 | cassette、工件、快照 |
| 模型 | Model Router：Claude / Qwen / DeepSeek / GLM 可切换 | 成本分层路由 + 缓存 |

---

## 12A. 平台工程与生产化运维（生产级完整度）

> 本节回应"企业级=生产级工程成熟度"的核心要求：以下是一个生产级系统必须有、但常被 PRD 跳过的工程面。

### 12A.1 平台自身测试策略（区别于 §13 的"检出能力评测"）
把系统**实现正确性**与**缺陷检出率**列为**两条独立质量线**——benchmark 高分不代表检查器实现无 bug，而一个把 soundness 当卖点的系统，检查器一旦有 bug，承诺即空。
- **检查器/编译器**：约束 DSL→Clingo/z3 编译器做**差分测试**（同一约束用多种编码/多引擎交叉验证结果一致）；图算法（BFS/SCC/拓扑）做**属性测试**（随机生成图，对拍朴素实现）。
- **Schema 适配器**：round-trip 的 **property-based test**（`export(import(x)) == x` 字段级）。
- **Aureus 内核**：确定性回归测试（同 seed 同 tick 序列 → 同状态哈希）；战斗/经济公式的单元测试。
- **Agent 层**：用 cassette 固定 LLM 响应做确定性单元/集成测试；契约测试 Agent-Env 接口。
- **端到端**：API 契约测试 + 前端 e2e（关键旅程 A/B 各一条 happy path + 失败路径）。
- **CI 门禁**：以上测试 + cassette 回放 + lint/类型检查作为合并门禁。

### 12A.2 数据持久化与灾备（DR）
"可追溯+可回滚+可复现"全部依赖存储不丢——这是基础生产卫生，**非**被排除的私有化部署。
- **备份**：Postgres/图库定期快照 + WAL 归档；对象存储（cassette/工件/IR 快照）跨桶复制。
- **不可变性**：审计轨迹与内容快照 **append-only / WORM**，带内容哈希校验。
- **恢复目标**：定义 RPO/RTO 初始目标（如 RPO ≤ 1h，RTO ≤ 4h）并做恢复演练。

### 12A.3 元 schema 演进与 DB 迁移
内容快照版本化已足，但**系统自身 schema / DSL 文法**的演进需专门治理。
- **元 schema 版本 + DSL 文法版本**：每个 IR 快照声明其元 schema 版本；旧快照按其声明版本回放/校验。
- **迁移器**：新增节点/边类型或 DSL 文法变更时提供迁移器；不可自动迁移的标为"需重抽取/重编译"。
- **DB migration**：关系库用 Alembic 类迁移；迁移在 CI 中前向/回滚双测。

### 12A.4 并发与一致性
五类角色会并发编辑同一 IR/约束库。
- **乐观并发控制**：IR/约束写入用版本号 CAS；冲突显式检测，提供 diff/合并界面。
- **审批队列一致性**：约束提议审批串行化；提议者≠审批者强约束。
- **只读快照隔离**：checker/仿真/playtest 跑在固定 IR 快照上，与并发写入隔离。

### 12A.5 内部安全（非商业，但基础防护必需）
- **提示注入防护**：策划文档与**开源游戏内容**流入 Extraction/Repair/Generator 前做来源标注与消毒；抽取出的约束**必经人审**（注入至多污染"提议"，不污染权威约束）。
- **DSL→solver 沙箱**：约束编译前静态校验；ASP grounding 规模上限、z3 超时、solver 进程 cgroup/超时隔离，防畸形约束致资源耗尽 DoS。
- **越权**：RBAC 在 API 层强制；审批/应用/回滚操作鉴权。
- **密钥管理**：模型 API key 等经密钥管理（env/secret manager），不入库不入日志。

### 12A.6 运维、SLO 与容量
- **告警 + SLO**：关键路径（检查、仿真、一次回归）定义 SLO 与告警。
- **容量目标**：定义最大配置规模（实体/约束数量级）、全量 checker 运行时长预算、一次 50 任务回归的成本/延迟预算，并压测。
- **部署拓扑与 CI/CD**：容器化服务，声明式部署；蓝绿/滚动发布；cassette 回放与上述测试进 CI/CD 流水线。

---

## 13. GameForge-Bench：Benchmark 与评测体系

### 13.1 定位
"游戏内容界的 SWE-bench"——公开不存在的、带缺陷分类学的可复现内容缺陷基准。这是本项目最可辩护、最持久的贡献之一。

### 13.2 缺陷分类学（Defect Taxonomy）
结构类：悬挂引用、缺失掉落源、目标不可达、环依赖、死任务、完成条件无法触发。
数值/经济类：奖励越界、概率和≠1、经济崩坏（sink/source 失衡）、曲线非单调、抽卡期望违规。
叙事类：角色设定违反、剧情剧透、阵营关系违反、唯一性违反。

### 13.3 语料构建（双来源，保外部效度）
- **Seeded 语料**：在 Aureus 真实配置上注入上述缺陷类（程序化 + 人标注），≥500 个样本。
- **外部真实语料**：接入 1 个开源数据驱动游戏的真实 commit/补丁历史，抽取真实缺陷/热修作为**非注入**样本。Live-ops 场景下，历史回滚/补偿事件天然是免费真实标注。

### 13.4 指标（避免可 gaming 的指标）
| 指标 | 定义 |
|---|---|
| **Bug Detection Rate（分缺陷类）** | 按类报告，不合并成一个漂亮总数 |
| **False-Positive Rate**（第一 KPI） | 误报比例——决定信任 |
| **Fix Pass Rate** | 修复经复验+回归通过率 |
| **Human-Edit-Distance** | 人还需改多少（可回流做微调飞轮） |
| **QA 工时节省** | 相对人工基线 |
| **Cost / Latency** | 每样本成本与耗时 |

**Agent 专属硬核指标**（回应"agent 是否够硬核"，与上表分开报告）：
| 指标 | 定义 |
|---|---|
| **长程任务完成率** | Playtest Agent 跑通多步任务链的比例，分任务长度/缺陷类，带 CI |
| **记忆消融（memory ablation）** | 有/无 mem-trace（Recall/Compaction）的完成率差 |
| **planner-executor 消融** | 有/无规划分层的完成率差 |
| **修复搜索效率** | verifier-guided 修复的平均收敛步数、一次通过率 |
| **叙事对抗验证一致性** | perspective-diverse / quorum 的通过阈值与稳定性 |

**反 gaming/反循环**：
- 确定性检查结果与 LLM 建议**分开报告**（不混入一个数字）。
- 检查器实现**独立于** seeded 分类学（避免"我们只检出我们定义要检的类"的自证）；用外部真实语料交叉验证以打破循环。
- 所有指标带置信区间；**功效目标：每缺陷类样本量使 BDR 的 95% CI 半宽 ≤ ±5%**（据此反推最小 n，避免评估指出的"n=50 → ±14% 欠功效"问题）。
- **误报口径分两类**：oracle-FP（检查器算法误报，目标 = 0）与 constraint-FP（约束被人误批导致的误报，见 §15-R3）分别统计报告。

---

## 14. 里程碑路线图

> 规划为 M0–M4；每个里程碑有进入/退出准则。资源充足，但仍分阶段以控制风险与保证每阶段可验证。

| 里程碑 | 主题 | 交付 | 退出准则 |
|---|---|---|---|
| **M0a** | 最短垂直链 | Spec-IR 数据模型（含元 schema 版本）、Aureus **最小内核（仅任务状态机 + 网格导航）**、Python/React 脚手架 | 一条手写配置 → IR → Aureus 跑通一条 3+ 步任务链（talk→collect→turn-in） |
| **M0b** | 地基补全 | Aureus 补齐（战斗伤害/AI + 背包/经济/抽卡）、Schema Registry + Aureus 适配器（round-trip）、版本/血缘/审计骨架、DB 迁移框架 | Aureus 配置 ↔ IR 往返无损（字段级 diff=∅）；四大系统由配置驱动 |
| **M1** | 确定性可信主干 | Graph/ASP/SMT 检查器套件、约束 DSL→检查器编译（含 solver 预算/降级）、经济仿真引擎、Review Report、**开源游戏适配器（外部效度前置）** | 在 Aureus 上对 ≥8 类结构/数值缺陷 sound 检出（结构类 spurious-violation=0）；经济仿真复现一次崩坏；开源游戏配置可往返 IR |
| **M2** | 有边界 Agent 层 + Playtest | Extraction Proposer/Triager/Repair Drafter、生成门禁、Agent-Env 契约、Playtest Agent、回归框架（cassette） | Playtest Agent 在 ≥20 条任务链上可靠闭环（首批目标，具体阈值 M2 规划锁定）；修复经复验+回归+审批闭环；结果可复现 |
| **M3** | Benchmark + 外部效度 | GameForge-Bench（≥500 seeded + 开源真实语料）、完整指标、Eval 面板 | 分缺陷类指标 + 误报率 + 外部语料交叉验证报告齐全 |
| **M4** | 生产化硬化 | 完整可观测/成本治理、版本血缘/回滚/审计打磨、RBAC/审批工作流、前端全页面 | 端到端可观测；任意产物可追溯+回滚；审批工作流跑通 |

**v-next（超出本 PRD 主范围）**：真实引擎（Unity/UE）headless playtest SDK（同 Agent-Env 契约）、本地化/多语一致性 QA、模型升级回归门、human-edit-distance 微调飞轮。

---

## 15. 风险与缓解

| # | 风险（评估阶段识别） | 缓解 |
|---|---|---|
| R1 | **验证者是生成者的孪生**（LLM 检查 LLM，盲点相关） | 判对错交给**确定性预言机 + 仿真**；LLM 建议单独分区、需人确认 |
| R2 | **Benchmark 循环性**（seeded 缺陷=检查器为之设计的类，检出近乎自证） | 检查器实现独立于分类学；接**开源真实语料**交叉验证；分缺陷类 + 误报率报告 |
| R3 | **约束抽取不准**（假约束→假缺陷→资深策划弃用） | LLM 只**提议**，人撰写权威；false-positive 为第一 KPI；live-ops diff 驱动降低撰写成本 |
| R4 | **Playtest 保真度**（97%/动作 × 60 动作 ≈ 完成率崩塌；toy 世界高分无意义） | Aureus 是真实系统（真实寻路/战斗/部分可观测）；有存档点/可重试步骤；与确定性可达性交叉验证；诚实报告完成率 |
| R5 | **非确定性下不可复现**（hosted API temp=0 也不确定；模型被弃用） | 固定模型快照 + cassette 回放 + seed 化环境；CI 内回放 |
| R6 | **安全自动应用是事故源**（错误的经济/叙事改动=玩家可见事故） | 自动应用仅限可证明结构性修复；其余 maker-checker 审批 |
| R7 | **约束撰写人力成本 / ROI**（语义规则引擎经典失败原因） | live-ops diff 驱动基线不变量（不需一次性手编整个 GDD）；报告 QA 工时节省作 ROI |
| R8 | **真正的对手是内建构建期校验 + 人 + 表格** | 差异化在统一 IR + ASP/SMT/仿真 + 修复闭环 + 版本血缘 + benchmark，这些内建脚本没有 |
| R9 | **"双向 IR"新颖性依赖可信抽取，而抽取被判不可信**（内在矛盾） | 明确：新颖性落在**双向编译 + 经济仿真验证 + benchmark**；抽取退为"人权威 + LLM 提议"，不把抽取当卖点 |
| R10 | **外部效度只压在单一开源游戏上，比表述更薄** | 承认此局限；优先在 M1 接入 1 个、并预留第 2 个开源游戏作 fallback；live-ops 真实回滚/补偿事件补充真实语料 |
| R11 | **检查器/编译器自身实现有 bug → "sound" 承诺落空**（benchmark 只测检出率，测不出实现 bug） | 见 §12A 测试策略：多引擎差分测试 + 属性测试 + 适配器 round-trip property test，将"实现正确性"与"检出率"列为两条独立质量线 |

---

## 16. 验收标准（量化）

| 里程碑 | 验收 |
|---|---|
| M0a | 一条手写配置 → IR → Aureus 跑通 ≥1 条 3+ 步任务链 |
| M0b | Aureus 配置 ↔ IR 往返无损（字段级 diff = ∅）；四大系统由配置驱动运行 |
| M1 | ≥8 类结构/数值缺陷检出，给定正确约束下 **spurious-violation = 0**（algorithmic soundness；"约束写错"导致的 constraint-FP 单列，见 R3；solver `unknown`/超时降级为"未证明"不计入通过）；约束 DSL 可编译到 Clingo/z3；经济仿真复现 ≥1 次崩坏并提前预警；开源游戏配置可往返 IR |
| M2 | Playtest Agent 在 ≥20 条任务链上可靠闭环（报告完成率含置信区间，不掩饰）；修复 diff 经复验+回归+审批后 Fix Pass Rate ≥ 70%（初始目标）；同输入同快照结果可复现；**记忆消融 + planner/executor 消融 + 修复搜索效率**报告齐全 |
| M3 | GameForge-Bench ≥500 seeded + 开源真实语料；分缺陷类 BDR + false-positive rate + human-edit-distance + QA 工时节省 全部报告，带置信区间；外部语料交叉验证结论成立 |
| M4 | 端到端 trace/成本/延迟可观测；任意产物可血缘追溯 + 回滚；maker-checker 审批工作流端到端跑通 |

---

## 17. 术语表

- **Spec-IR**：Design-Spec Intermediate Representation，类型化知识图谱 + 约束存储，内容语义的权威源。
- **确定性预言机（Deterministic Oracle）**：图/ASP/SMT/仿真给出的可判定对错判定，不含 LLM。
- **有边界 Agent**：输入/输出契约明确、失败有确定性或人工兜底的 LLM 角色。
- **cassette**：录制的 LLM/环境交互，用于回放以隔离非确定性。
- **maker-checker**：提议者与审批者分离的双人审批。
- **Aureus**：自研 live-service 风格 2D RPG 参考游戏（测试床）。
- **GameForge-Bench**：游戏内容缺陷 benchmark。

---

## 附录 A：多视角评估结论摘要
七视角（创新性/竞品/技术可行性/企业化/游戏行业/商业化/红队）+ 综合 + 红队反驳的关键结论已内化进本 PRD §2–§3、§13、§15。核心：TITAN/Acorn 已占 QA 时刻 playtest；"生成-验证-修复"与"KG+约束"是常规 prior art；可辩护内核=双向 IR + 经济仿真验证 + 内容缺陷 benchmark；最大风险=自评作业与验证者孪生，靠确定性预言机+真实测试床+外部效度化解。

## 附录 B：Prior Art 对照
Self-Refine / Reflexion / RepairAgent / PatchAgent / SWE-bench（生成-验证-修复范式）；ASP-for-PCG（Adam Smith）、Graph-Constrained Reasoning、GraphRAG（约束/KG）；TITAN(2509.22170)、Lap(2507.09490)、GameEval（游戏 playtest）；EconAgent、MMO 经济 ABM（经济仿真）；Machinations.io、articy:draft/Ink/Yarn（设计工具邻居）。

## 附录 C：示例 IR 快照（节选）
```json
{
  "version": "ir@2026-07-03T1",
  "entities": {
    "quest:q_missing_caravan": {"type": "Quest", "region": "newbie_zone",
      "steps": [
        {"type": "talk", "target": "npc:lincheng"},
        {"type": "collect", "item": "item:broken_emblem", "count": 3},
        {"type": "fight", "enemy_group": "mob:bandit_small"}
      ],
      "reward": {"gold": 120, "item": "item:low_tier_blade"}},
    "npc:lincheng": {"type": "NPC", "located_in": "region:newbie_zone"},
    "item:broken_emblem": {"type": "Item", "drops_from": []}
  },
  "violations_if_checked": [
    "C_collect_needs_source: item:broken_emblem 无掉落源（drops_from=[]）",
    "C_newbie_gold_cap: reward.gold=120 > 80"
  ]
}
```
