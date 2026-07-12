# GameForge Pre-M4 Product Closure Design

> M3 外部效度、完整指标与核心契约债务的闭合设计

> **2026-07-12 修订说明：** 产品目标、跨游戏边界和已完成的核心契约修正继续有效；
> 多轮人工 hash 审批、30-case HED、12-pair QA 及 narrative 逐例人工确认门禁，已由
> `2026-07-12-pre-m4-lean-closure-design.md` 依据用户“质量优先但不能过度设计”的反馈替代。

| 字段 | 值 |
|---|---|
| 状态 | 书面审阅已批准 |
| 日期 | 2026-07-11 |
| 范围 | M4 进入前的全部未完成门禁 |
| 真相源 | PRD `2026-07-03-gameforge-prd.md`、地基契约 `2026-07-03-gameforge-foundations-contracts.md` |
| 前置结果 | Flare B0A 终态为 `insufficient_evidence` / `stop_flare_heavy_investment` |

## 0. 决策摘要

M3 仍未完成，M4 不能开始。Flare B0A 已按预注册流程穷尽两轮批准的候选宇宙，
但没有得到合格真实案例；该负结果有效地终止 Flare 重投入，却不能替代 PRD §13.3
要求的开源游戏真实非注入缺陷语料。

本设计作出五项决定：

1. 选择 Endless Sky 作为下一外部语料候选，Wesnoth 作为明确 fallback。
2. 外部游戏用于验证 GameForge 的跨游戏扩展边界，不用于针对单一游戏调分。
3. 把 Flare harness 中通用的 Git 发现、证据冻结、裁决、qualification 和评测能力
   抽为来源无关核心；每款游戏只提供 source profile、reader 和 Adapter。
4. 在新 Adapter 落地前纠正 `DROPS_FROM`、Patch 基线拒绝和 repair cassette 脆性，
   防止把旧契约错误扩散到第二款游戏。
5. 用真实 held-out narrative 评测、human-final patches 和人工对照实验闭合
   narrative BDR、Human-Edit-Distance 和 QA 工时；不使用代理指标或合成真人数据。

实现按独立子里程碑推进，每个子里程碑分别走 design/plan/TDD/review/acceptance。
本设计锁定总边界和最终验收，不授权把全部改动揉成一个分支。

## 1. 当前证据与缺口

### 1.1 Flare 负投资结论

已批准并冻结的 expanded B0A：

- candidate universe SHA-256：
  `08873db9362bd6ff45ca05bb4e3184120fc07842cf224e8f649fb6555e57bfc3`
- approval payload SHA-256：
  `b5111dd6d65caa7675a82ac7b7c2a3735dd472eed0a2e8fc607c6b1292dc9970`
- 526 candidates，7/8 proposed groups，10 proposed cases，4/4 proposed classes
- `qualified_candidate=0`，`accepted=0`
- gate：`insufficient_evidence`
- action：`stop_flare_heavy_investment`

因此禁止 Flare 第三轮搜索、B0B、quest/loot reader 扩展或用注入案例冒充真实案例。
冻结语料、ledger、审批和哈希仍是有效审计资产，并作为通用 harness 的兼容回归。

### 1.2 Endless Sky 预调研

本地只读预调研得到：

- 9,883 个总提交
- 5,127 个触及 `data/` 的提交
- 2023 年以来 1,356 个触及 `data/` 的提交
- 610 个关键词召回，其中 562 个为单父且 changed paths 全部是 `data/**/*.txt`

已逐 diff 核实六个 config-only、非注入候选，覆盖引用、任务可用性、可达性、
完成条件、循环和奖励数值。它们只证明值得进入预注册 B0A，不是 ground truth，
也不能直接计入 BenchReport。

Endless Sky 提供 pinned engine 的 parse/reference 检查路径，许可为 GPLv3。原生 parser
可提供独立格式/引用证据，但不能判定所有任务、wormhole、cargo 或 reward 语义；
语义 qualification 仍需独立 predicate。

### 1.3 指标债务

- 四类 narrative 当前被 `run_bench.py` 硬编码为 `n=0 PENDING`。
- 当前 9 个 consistency cassette 来自同一段对话，不能构成 M3 narrative corpus。
- narrative 输出只有自由文本 `span/issue`，没有四类 taxonomy、目标实体或约束 ID。
- narrative 注入器含 `TRAIT:`、`SPOILER:` 等答案标记，不可用于正式 held-out。
- `BenchReport` 没有 Human-Edit-Distance 或 QA-hours 契约。
- repair harness 只聚合通过率/步数，不保存可供真人编辑的最终 Patch 工件。
- 没有 manual QA arm、计时协议或真实人工基线。

### 1.4 核心契约债务

- Aureus/Flare 的物品掉落生成 `ITEM -> MONSTER`，而 checker、ASP、经济仿真、
  benchmark、scenario generator 和 repair prompt 均消费 `producer -> product`。
- Repair prompt 把完整 `snapshot_id` 写进消息，无关 IR 变化会使所有 cassette miss。
- `apply_patch()` 忽略 `Patch.base_snapshot_id`，没有执行契约要求的 rebase-or-reject。
- precondition 缺字段会泄漏 `KeyError`，而不是 fail-closed `PatchRejected`。
- request hash 直接作为 Patch ID，同一语义 cassette 复用于不同 base 时会冲突。

## 2. 目标与非目标

### 2.1 目标

1. 用至少一个开源游戏的真实 config-only bug-fix 历史完成非注入 held-out 交叉验证。
2. 证明新增游戏只需 source profile/reader/Adapter，而不需修改核心 checker 的游戏特判。
3. 纠正 `DROPS_FROM` 和 Patch/cassette 语义并保持完整地基契约。
4. 四类 narrative 分别报告真实 evaluated BDR、human-confirmed FP 和置信区间。
5. 报告基于 human-final patch 的 HED 和基于 manual arm 的 QA 工时。
6. 让 BenchReport、文本视图、静态 Eval HTML 和机器验收读取同一份证据。
7. 未过任一门禁时保持 M3 未完成，禁止进入 M4。

### 2.2 非目标

- 不建设任意第三方发现、安装、沙箱和兼容生命周期组成的通用插件 SDK。
- 不为 Endless Sky、Flare 或 Wesnoth 在 `contracts/spine/checkers/metrics` 写专属规则。
- 不继续 Flare 搜索或扩 Flare reader 来挽救负 gate。
- 不实现自动 rebase、三方 merge、新 precondition DSL 或 cassette 通用 GC 服务。
- 不提前实现 M4 的 React 全页面、RBAC、maker-checker UI 或生产对象存储。
- 不把单人实验外推为一般游戏团队结论。
- 不要求 HED/QA 必须出现正收益；要求真实测量和诚实结论。

## 3. 跨游戏架构边界

### 3.1 三层所有权

#### 游戏无关核心

包括：

- `gameforge/contracts/`
- Spec-IR 与 canonical Snapshot
- Graph/ASP/SMT checker 和经济仿真
- Patch apply/verify
- benchmark taxonomy、统计、报告和 acceptance validator
- external-corpus 的 Git/证据/状态机核心

核心禁止出现游戏专属文件名、commit ID、字段名特判或源专属 evaluator。

#### Source Profile / Reader / Adapter

每款游戏可以拥有：

- source profile：仓库、pin、path policy、许可、原生 validator、closure 和 applicability
- reader：把源文件解析成该格式的 typed workbook/record tree
- Adapter：实现现有 `spine.ingestion.adapter.Adapter`，把 typed source 映射到统一 IR
- qualification predicate：只为 benchmark ground truth 服务，不进入产品 checker

Adapter 是必要的边界翻译，不是核心特化。它必须 round-trip 或在不支持反向导出的字段上
显式保存 raw/source_ref，使 query-complete 输入可重放；不得静默丢字段。

#### 不可变证据

每次外部评测保存：

- pinned repository/range 和 source profile hash
- raw before/after blobs 与 query-complete closure
- commit/PR/message/patch evidence
- human disposition/qualification/approval
- native validator 结果
- Adapter/version/checker/version/report/version
- development/held-out/reserve split
- GameForge Finding 与计分结果

这些工件使用 canonical JSON、SHA-256、不可覆盖 CAS 和离线 replay。

### 3.2 通用 external-corpus 包

目标布局：

```text
gameforge/bench/external_corpus/
  contracts.py       # source-neutral evidence/state/report contracts
  git.py             # read-only pinned Git facts
  discovery.py       # frozen search rules -> candidate universe
  adjudication.py    # human dispositions/groups/applicability/gates
  qualification.py   # before/after/native/predicate evidence
  freeze.py          # input trees, split and accepted corpus
  evaluation.py      # Adapter/checker scoring
  profiles/
    flare.py
    endless_sky.py
    wesnoth.py       # 仅 fallback 被选择时实现
```

这不是动态插件系统。profile 由仓库内显式 registry 选择，不能从不可信路径加载代码。

### 3.3 Flare 兼容要求

现有 `flare_evidence.py`、`flare_git.py`、`flare_adjudication.py`、`flare_mining.py`
保留兼容 import/CLI，允许内部委托通用核心。下列内容必须保持：

- `scenarios/flare_corpus/**` tracked bytes 不变
- 既有 candidate/approval/decision hash 不变
- 既有 discover/adjudicate 离线 replay 字节一致
- 既有负 gate 不被重新解释

通用化若要求修改冻结 JSON 或重算哈希，设计即失败；应改兼容层而不是改证据。

## 4. 外部真实语料流程

### 4.1 状态机

```text
pre_registered
  -> discovered
  -> proposed | rejected | ambiguous
  -> qualified | qualification_failed
  -> development | held_out | reserve
  -> evaluated
```

状态只能前进。人工决定、qualification 和 split 都进入哈希链。评测失败不是从 corpus
删除案例的理由。

### 4.2 SourceProfile 完整字段

```text
SourceProfile {
  source_id, profile_version,
  repository_url, pinned_head, history_range,
  config_include_globs, config_exclude_globs,
  message_rules, diff_rules, lineage_rules, candidate_order,
  license_id, notice_files,
  native_validator_commands, parser_version,
  query_complete_closure,
  taxonomy_applicability,
  qualification_predicate_ids
}
```

命令是参数数组且默认离线；不接受 shell fragment。profile、search spec 和代码 registration
commit 在 discovery 前冻结。

Adapter 尚未实现时不在 B0A profile 中写虚假的版本。进入 freeze/evaluation 前另冻结：

```text
AdapterBinding {
  source_id,
  reader_id, reader_version,
  adapter_format_id, adapter_version,
  ir_schema_version,
  mapping_spec_hash
}
```

SourceProfile 的历史发现语义与 AdapterBinding 的产品映射语义分别版本化；修改 Adapter
不会追溯性改变 candidate universe。

### 4.3 Endless Sky B0A：供给门

- 固定 2023-01-01 到 pinned head。
- 预注册 message/diff/path/lineage 规则和稳定候选顺序。
- 最多评审候选顺序中的前 80 个；不是人工挑选 80 个看起来最好的案例。
- 80 个候选全部必须有 `proposed/rejected/ambiguous` disposition、理由和 evidence refs。
- cherry-pick/backport/revert 和同一根因多提交合并成独立 fix group。
- 只有 changed paths 全部符合 config-only policy 的 group 可 proposed。
- reviewer 必须与 adjudicator 不同，并书面批准完整表及 payload hash。

投资 gate：

```text
independent proposed groups >= 8
AND domain-applicable proposed classes >= 4
```

失败即停止 Endless Sky 并用同一通用 harness 进入 Wesnoth B0A，不改变门槛。
B0A pass 只代表值得做 qualification，不计入 M3 指标。

### 4.4 B0B：真实性门

每个 proposed group 必须形成 qualification case：

1. 固定代表 commit、before parent 和 after commit。
2. 冻结 raw changed blobs 与 query-complete dependency closure。
3. 同一个 pinned native parser/validator 必须能加载 before 和 after。
4. 独立 qualification predicate 必须返回 `before=violation, after=clear`。
5. predicate 输出目标 class、entity/source span、证据和失败原因。
6. 许可/NOTICE 与 raw bytes 一起冻结。
7. 人工 reviewer 批准 qualification evidence hash。

原生 parser 通过只证明语法/引用层加载成功，不能代替第 4 步的语义 oracle。
无法构造 query-complete closure、predicate 不稳定或 evidence 不充分的案例标为
`qualification_failed`，不得静默排除。

B0B gate：

```text
independent qualified groups >= 8
AND qualified defect classes >= 4
AND frozen split contains held-out cases in at least 4 classes
```

不满足即停止该来源或转 fallback，不能用 proposed 数替代 qualified 数。B0B pass 才允许
实现正式 Adapter 和宣称存在可评测的真实 corpus。

### 4.5 防泄漏 split

- 按 independent fix group 去重，任何 lineage sibling 必须在同一 split。
- split 在生产 Adapter/checker 调整前冻结。
- development 用于 reader/Adapter/约束映射和问题诊断。
- held-out 只用于一次正式结果，不参与实现决策。
- reserve 用于最终结果暴露后确有产品级优化时的下一次独立评测。
- 每个宣称完成外部交叉验证的 class 必须在 held-out 中有案例。
- 没有 held-out 的 class 只能标 `exploratory`。

若一次 final held-out 被用于调实现，它立即降为 development；只有预注册 reserve 或新来源
可以产生新的 final 结论。不得覆盖或隐藏旧结果。

### 4.6 外部计分

对每个 held-out case：

1. 使用同一 Adapter 生成 before/after IR。
2. 使用同一通用 checker/constraint set 检查两侧。
3. 只有 before 命中 target class 和 target entity/span，且 after 清除该 Finding，才计 TP。
4. Adapter、validator、checker 或 replay 执行失败均作为 miss 留在分母，并另报 failure reason。
5. post-fix snapshots 形成外部 clean candidate 集；unexpected deterministic/unproven Finding
   进入独立人工/native adjudication。被确认的真实附带缺陷记 incidental TP，被驳回的 Finding
   才计 external FP，无法判定的保持 unproven。

报告按 source、split、defect class 给出 `n/k/rate/Wilson CI/status`；不使用外部 aggregate
headline 掩盖类间差异。

### 4.7 防特化 CI

- `gameforge/contracts`、`gameforge/spine/{ir,dsl,checkers,sim,patch.py,stats.py}` 和
  `gameforge/bench/{taxonomy,metrics,report,power}` 禁止 import `external_corpus.profiles`。
- AST/文本 lint 禁止上述核心出现 pinned OID、源路径或 `endless_sky` 专属规则。
- `gameforge/spine/ingestion` 是明确的 source-specific Adapter 边界，不受游戏名禁令影响，
  但不能被 checker/sim 反向 import。
- Flare 和 Endless Sky profile 通过同一 generic fixture contract tests。
- 生产 checker 不读取 source ID；qualification predicate 不能被生产评测调用。
- source-specific exception 必须位于 profile/reader/Adapter，并由 source_ref 暴露。

## 5. IR 与 Patch 契约纠正

### 5.1 `DROPS_FROM`

锁定 `ir-core@1` 语义：

```text
MONSTER | DROP_TABLE | INTERACTABLE | EVENT | BATTLE_ENCOUNTER
    --DROPS_FROM--> ITEM | CURRENCY
```

本次改动：

- Aureus item drop：`MONSTER -> ITEM`
- Flare direct loot：`MONSTER -> ITEM`
- Aureus currency drop：保持 `MONSTER -> CURRENCY`
- deterministic pickup/grant/reward：继续用 `GRANTS/REWARDS`

不新增 `USES_DROP_TABLE` 或借用 `REFERENCES/TRIGGERED_BY` 表达所有权。Aureus entity attrs
仍保留 `drop_table_id` 以保证源格式 round-trip，派生的 source relation 使用 Monster 作为
直接 producer。未来若正式引入 ownership edge，必须单独修改地基契约。

所有 Adapter 共享 conformance test：合法 producer、合法 product、无 `ITEM -> MONSTER`。
Graph/ASP 的正向边必须消除 missing-source，反向边不能冒充 source；经济仿真只把
producer-to-currency 视作 faucet。

当前仓库没有已发布的序列化反向 IR snapshot，故保持 `ir-core@1` 并纠错，不制造双方向
兼容层或 migrator。实施时若发现真实 DB/对象存储持久化 artifact，立即停止该子里程碑并
先写迁移设计。

### 5.2 Patch 基线语义

```text
apply_patch(current, patch):
  if current.snapshot_id != patch.base_snapshot_id:
      raise PatchRejected
  validate existing preconditions
  validate/apply ordered ops to a private graph
  return a new content-addressed snapshot
```

base mismatch 在任何 precondition/op 前拒绝，包括 no-op、add、delete 和 set。失败不能部分
提交，输入 Snapshot 保持不变。`dry_run` 使用同一语义。

本阶段选择地基契约允许的 reject 分支，不实现隐式 rebase。现有 add/delete 的目标存在性
检查和 exact base 已足够；set/replace 继续使用 `old_value`。缺字段、未知 kind 和形状错误
统一转为 `PatchRejected`，不新增 precondition vocabulary。

### 5.3 Repair request 与 Patch identity

- 从模型 user prompt 删除完整 `base_snapshot_id`。
- focus node、incident relation、evidence 或 counterexample 改变仍必须改变 request hash。
- prompt version bump 为 `repair@4`。
- Patch 的 `base_snapshot_id` 继续由确定性代码绑定调用时 Snapshot。
- `producer_run_id` 保留模型 request hash。
- Patch ID 改为 canonical hash：`{request_hash, base_snapshot_id, ops}`。

这样同一语义 request 可以跨无关 Snapshot 变化复用 cassette，但产生的 Patch 仍唯一、可审计，
且不能错误应用到其他 base。

### 5.4 Cassette 重录边界

所有确定性 IR/Patch 语义稳定后只重录一次：

- 当前活动 repair requests 全部因 `repair@4` 重录。
- generation 因 clean snapshot 变化重录 1 个活动 request。
- extraction、consistency 和 playtest 不受本次改动影响，不重录。

先 RECORD resume，再零网络 REPLAY 两次。Fix Pass Rate 必须保持 10/10。只有活动请求全部
REPLAY 成功后才删除已确认不可达的旧 repair/generation cassette；不建设通用 manifest/GC 服务。

## 6. Narrative BDR

### 6.1 正式 NarrativeCase

```text
NarrativeCase {
  case_id, generator_version, seed,
  dialogue, constraints,
  structured_facts,
  hidden_defect_class?, target_entities, target_span?,
  is_clean,
  corpus_split
}
```

正式语料从结构化事实渲染自然文本。正式 held-out 禁止出现 taxonomy 名、`TRAIT:`、
`SPOILER:`、`CONTRADICTION:` 等答案标记。每类必须覆盖：

- 显式与隐式表述
- 同义改写
- 多实体/多事件组合
- 无关干扰事实
- 多个 clean Aureus bases

distinctness 以结构化事实、目标和渲染文本共同判定，不能靠随机后缀凑 n。

### 6.2 Consistency Agent 输出与视角

`ConsistencyHint` 添加：

```text
defect_class
entity_ids
constraint_ids
span
rationale
is_suggestion=true
```

Agent I/O schema version相应升级；旧 cassette 由兼容解析读缺省字段，但不进入正式 M3 计分。

三个 perspective 都检查四类 narrative，但采用不同论证方式：

1. constraint evidence matching
2. causal/world-state reasoning
3. adversarial falsification / false-positive rejection

不再让每个 perspective 只看互斥类别。quorum key 使用规范化 class、entity、constraint 和 span，
而不是要求自由文本 issue 完全相等。rebuttal 仍只能确认首轮候选，不能注入新 hint。

### 6.3 Development 与 held-out

1. 先运行独立 development pilot，允许修语料、prompt、matcher 和 Agent 输出契约。
2. 冻结 generator、prompt、model snapshot、perspectives、threshold 和 matcher。
3. 再生成、哈希并 RECORD 正式 held-out。
4. 正式 positive held-out 每类固定 `n=381`，即保守 `p=0.5` 下满足 95% CI 半宽不超过 0.05。
5. 另生成 381 个 balanced clean controls，测 human-confirmed narrative FP。

解析失败、全 perspective fallback、超时和 cassette miss 均留在 BDR 分母，并另报 agent
availability。clean case 上无输出不是 FP，但 parse/availability 仍单独报告。

### 6.4 人工确认

评审者看不到 hidden class。输入只含 constraint、dialogue、模型 hint 和必要 source context。
每个 hint disposition：

```text
confirmed_tp | wrong_class | wrong_target | unrelated | duplicate | rejected
```

一个 positive case 只有至少一个 human-confirmed、class/target/semantic 全正确的 hint 才算 TP；
多个 hint 最多贡献一次。clean case 上任何 human-confirmed defect hint 计 FP。

- 1 位真人：允许执行，报告 `single-reviewer` 和 reviewer ID，不作人群外推。
- 2 位以上：独立 primary review；分歧由未参加该 case primary review 的人裁决。

corpus、model outputs、dispositions、reviewer identity 和 approval payload 全部哈希绑定。
quorum 不能替代 human confirmation。

## 7. Human-Edit-Distance

### 7.1 Corpus 与 evidence

正式 HED corpus 为 30 个独立 repair cases：当前 10 个 repairable deterministic/simulation
classes 每类 3 个结构或数值不同实例。每例保存：

- base Snapshot 和 Finding
- Agent draft Patch、apply status、target Snapshot（若可应用）
- deterministic verifier 结果
- human-final Patch 和 target Snapshot
- final verifier/regression 结果
- reviewer、protocol、tool 和 evidence hashes

真人可以 `accepted_unchanged`、`accepted_edited` 或 `agent_unusable`，但 case 要进入 HED
分布必须有同一 base 上的 human-final patch，并通过 checker/sim/regression。缺 human-final 的
case 标 `protocol_failure`，不得从 corpus manifest 删除。

### 7.2 语义距离

把 `base -> target` 规范化为原子图变更集合。原子 identity 至少含 object kind、object ID、
attribute path/relation endpoints 和 canonical value，因此 op 顺序和 JSON 排版不影响结果。

```text
A = atomic_delta(base, agent_target)
H = atomic_delta(base, human_target)
raw_distance = |A symmetric_difference H|
normalized_distance = 0                         if |A union H| == 0
                      |A symmetric_difference H| / |A union H| otherwise
```

无法应用的 Agent patch 使用 `A=empty` 并保留 `agent_unusable` 状态；human-final 非空时其
normalized distance 为 1。错误值改成正确值产生撤销错误 atom 和新增正确 atom，不会被低估。

报告 raw/normalized 的 mean、median、bootstrap CI，以及 unchanged acceptance、edited
acceptance、unusable 和 protocol failure rate。bootstrap seed 与 resample count 写入 protocol。
M3 gate 要求 30 个 case 全部产生可验证 human-final；protocol failure 保留证据并阻断验收，
不能通过补删 manifest 或降低 evaluated n 绕过。

## 8. QA 工时对照

### 8.1 Task 定义

一次任务要求参与者：

1. 在给定配置变更中定位缺陷；
2. 写出最小复现/根因；
3. 产生通过同一 correctness oracle 的 typed/config patch。

正式 study 为 12 个 matched pairs，每 pair 两个同类、相近难度但内容不同的实例；一例
manual，一例 GameForge-assisted。同一参与者完成 pair 两侧，实例/arm/order 按冻结 schedule
平衡。至少 2 pairs 来自 structural、numeric/simulation、external real、narrative 四个 strata；
剩余 4 pairs 按冻结 corpus 的可比性分配。

多参与者使用交叉分配，使同一实例在不同参与者间交换 arm。单参与者仍可执行，但报告范围
限定为该参与者与该任务集。

### 8.2 Arms

Manual arm 允许：源文件、编辑器、项目文档、游戏原生 parser/validator。禁止 GameForge
Finding、IR query、repair draft 和 Eval evidence。

Assisted arm 允许完整 GameForge workflow。查看 Finding、等待、审查 Agent patch、人工修改
和最终验证均属于任务流程；不能只记录模型调用时间。

两臂使用同一隐藏 correctness oracle。更快但错误的任务不算成功。

### 8.3 计时与失败

- 事件：`start/pause/resume/finish`，保存 monotonic active duration 和 elapsed duration。
- 主指标：human active minutes；elapsed time 为次指标并与模型 latency 分列。
- 每任务 active-time cap 为 20 分钟，在正式 protocol hash 中固定。
- 超时或错误结果保留，active time 按 cap 计，success=false。
- 中断原因和 protocol violation 单列，不从分母删除。

报告 paired absolute minutes saved、paired percentage、mean、median、bootstrap CI、两臂成功率
和 participant-stratified 结果。

只有 time-saved CI 下界大于 0 且 assisted success rate 不低于 manual，才可写“节省工时”。
否则状态为 `inconclusive` 或负收益。真实负结果仍满足“完成测量”，但不满足正收益声明。
正式 gate 要求 12 个 matched pairs 都有两个 arm 的有效 session evidence。任务本身超时或修复
失败是有效 outcome；计时缺失、arm 污染或没有 correctness verdict 属于 protocol failure，
并阻断验收。

## 9. BenchReport v2 与 Eval 视图

### 9.1 指标类型

```text
BinaryMetric {
  name, defect_class?, bucket,
  planned_n, evaluated_n, k, rate, ci_low, ci_high, ci_method,
  status, protocol_id, evidence_manifest
}

DistributionMetric {
  name, unit, bucket,
  planned_n, evaluated_n,
  mean, median, primary_estimate, ci_low, ci_high, ci_method,
  status, protocol_id, evidence_manifest
}
```

`status` 取：

```text
pending | measured | underpowered | inconclusive | failed
```

`planned_n` 和 `evaluated_n` 必须分开。缺 evidence 使用 `pending/failed`，不能用数值 0 冒充。

### 9.2 Report 结构

`BenchReport` 增加明确 `bench_report_schema_version = "bench-report@2"`，并包含：

- seeded per-class metrics
- deterministic/constraint/narrative FP
- bounded agent metrics
- per-class power rows（使用 evaluated n）
- external sources/splits/per-class metrics
- HED distribution + disposition rates
- QA paired-time distributions + arm success rates
- model/prompt/tool/adapter/protocol/evidence versions

当前没有发布的持久化 BenchReport API，故直接升级内部契约并同步所有消费者；不保留会产生
双重口径的旧运行路径。旧测试 fixtures 用显式 v1 fixture 测拒绝/迁移边界。

### 9.3 三个视图

- JSON 是权威机器输出。
- text view 和静态 HTML 只渲染 JSON model，不自行重算指标。
- 三者逐字段 contract test 一致。

M3 静态 HTML 展示分区、CI、status、protocol/evidence refs 和 underpowered/inconclusive。
完整 React 页面留 M4。

### 9.4 Cost / Latency

Cost/Latency 不从 QA active time 推断。每个 agent request 从 cassette 的 record-time evidence
聚合：

- normalized input/output/cache-read/cache-write tokens
- transport attempts/retries
- recorded latency milliseconds
- per-sample total tokens、mean/median/p95 latency 和 bootstrap CI

token consumption 是 M3 必须报告的 provider-independent cost unit。若存在经过人工批准且绑定
model snapshot/date/currency 的 `ModelPriceBook`，再计算 monetary estimate；没有可信 price book
时 monetary 字段为 unavailable，不能编造价格，但 token cost 与 latency 仍必须 measured。

确定性 checker/sim 另报受控运行环境下的 per-sample execution latency，保存平台、Python、
tool version 和 run manifest；不把跨机器 wall time 当作算法正确性指标。

## 10. 效果不佳时的优化纪律

允许优化，但必须保持评测独立：

1. 只用 development/pilot 定位 parser、Adapter、checker、prompt、matcher 或 workflow 根因。
2. 核心修改必须能用来源无关的 contract/property test 说明产品价值。
3. source-specific 修复只进入 profile/reader/Adapter，不进入 checker。
4. 修改后 bump 对应 version，重新冻结 request/cassette/protocol。
5. final held-out 一旦用于决策即被消费；复测必须用 reserve 或新语料。
6. HED/QA 正式 study 开始后修改 workflow，旧 evidence 保留为旧版本结果，新版本重新采集。
7. 不通过删样本、改 class、放宽 matcher 或降低 gate 改善数字。

## 11. 错误处理与 fail-closed 规则

| 失败 | 行为 |
|---|---|
| source/profile/search hash mismatch | 停止，不产生新 ledger |
| Git object/closure 缺失 | qualification failure；不得联网补齐后假装同一 run |
| native validator 失败 | qualification failure，保存 stdout/stderr/exit code |
| before/after predicate 不反转 | qualification failure |
| Adapter 解析/round-trip 失败 | held-out miss 或 development defect，不能删 case |
| checker timeout/unproven | 按既有 `unproven` 契约报告，不能当 pass |
| stale Patch base | `PatchRejected`，不尝试隐式 rebase |
| malformed precondition | `PatchRejected`，不泄漏 `KeyError` |
| cassette miss | CI/REPLAY 失败；benchmark 中保留分母 |
| human evidence 缺失 | metric pending/failed，不填 0 |
| QA timeout/incorrect | 保留 case，cap time + success=false |
| held-out 被用于调参 | 降为 development，只能换 reserve/new source |

## 12. TDD 与验证矩阵

### 12.1 通用 external harness

- 两个 synthetic Git profiles 共用 discover/adjudicate/qualify/freeze contract tests。
- config-only、merge、root、rename、binary、lineage 和 partial/promisor 边界回归。
- Flare legacy CLI/import 与 frozen bytes/hash 回归。
- profile 改动导致 registration/hash mismatch 的 fail-closed 测试。
- split group isolation、held-out immutability 和 reserve promotion 测试。

### 12.2 Adapter 与 checker

- reader property tests 和 source-ref preservation。
- `from_ir(to_ir(x))` 字段级 round-trip，或显式 raw-preservation contract。
- query-complete closure missing dependency negative tests。
- core import/AST lint 防 source 特判。
- held-out scorer 的 correct class/target/after-clear 条件与 failure denominator 测试。

### 12.3 `DROPS_FROM` / Patch / cassette

- Aureus item/currency 和 Flare direct loot 合法端点。
- 反向边不能消除 missing-source；正向边 Graph/ASP 对拍。
- economy 只消费 producer-to-currency。
- exact base apply 与 set/add/delete/no-op stale reject。
- malformed preconditions、multi-op atomicity、dry-run stale reject。
- 同语义不同 base 的 request hash 相同、Patch ID 不同、交叉 apply 拒绝。
- RECORD 后两次零网络 REPLAY 字节/结果一致，Fix Pass Rate 10/10。

### 12.4 Narrative/HED/QA

- narrative 生成可复现、无答案 marker、结构化事实独立证明注入。
- class/target/span wrong、duplicate、human rejected 均不计 TP。
- fallback/miss 留在分母；clean human-confirmed hint 计 FP。
- HED 对 op 重排/JSON 排版为 0，对错误值替换计两个 atomic changes。
- unusable Agent patch、missing human-final 不伪装为 0。
- QA timer 状态机、pause/resume、cap、incorrect outcome 和 arm isolation。
- bootstrap 固定 seed 重放一致。

### 12.5 Report/acceptance

- BenchReport v2 JSON round-trip 和旧 v1 明确拒绝/迁移测试。
- planned/evaluated n 不混用。
- JSON/text/HTML 同源逐字段测试。
- acceptance validator 对任何 pending、缺 evidence、功效不足或外部 held-out 缺失给出具体失败项。

所有子里程碑最终运行：全量 pytest、全部 import-linter contracts、Ruff、`git diff --check`、
零网络 replay 和对应 evidence verifier。

## 13. M3 机器验收门

新增确定性 `validate_m3_acceptance(report, evidence) -> [GateFailure]`。只有全部为空才允许
更新路线图：

1. seeded corpus 总量至少 500，15 类均有真实 evaluated metrics。
2. 每类 BDR 和 FP 有 CI，deterministic/simulation/llm-assisted 分区。
3. 四类 narrative 不再 pending，positive per-class CI half-width <= 0.05。
4. deterministic oracle-FP 为 0；human-confirmed narrative FP 单列。
5. 至少一个外部游戏有非注入 held-out case，并按类报告 before-hit/after-clear。
6. B0B 至少有 8 个 qualified groups、4 类，并在至少 4 类中有 held-out；至少 4 类各有
   一个 held-out TP，且 human-confirmed external FP 为 0。
7. 外部 source、raw bytes、许可、native/predicate evidence 和人工审批可离线验证。
8. HED 有 30 个可验证 human-final cases；任何 protocol failure 阻断验收。
9. QA study 有 12 个完整 matched pairs 的真实人工 evidence；manual/assisted correctness 同源。
10. agent token cost、record-time latency 和 deterministic pipeline latency 已测量并带 manifest。
11. 所有执行失败和 cassette miss 在正确分母。
12. BenchReport JSON、text、HTML 一致。
13. 全量测试、lint、Ruff、replay 和 evidence verification 通过。

HED/QA 结果可以 `inconclusive` 或负值，因为 PRD 没有正收益阈值；`pending/failed` 不能通过。
外部 class 样本少可显示 underpowered，但必须有真实 held-out 结论，不能用 injected probe 替代。

## 14. 执行顺序与分支

### 子里程碑 0：关闭 Flare B0A 分支

- 本设计书面批准后提交设计稿。
- 把当前 `m3d-flare-rich` 线性快进到 `master`。
- 验证冻结 corpus 与 `755fe2e` 基线零差异、全量 suite/lint/ruff 通过。

### 子里程碑 1：通用 external evidence + Endless Sky B0A

- 新 `codex/pre-m4-external-evidence` 分支。
- 抽通用 harness、保 Flare 兼容、注册 Endless Sky source/search spec。
- 运行最多 80 candidates 的 B0A，取得完整人工审批和投资 gate。
- pass 解锁子里程碑 3 的规划；子里程碑 2 必须先合入，才能开始新 Adapter 实现；
  fail 则用同一内核进入 Wesnoth B0A。

### 子里程碑 2：核心契约纠正

- 独立 `codex/pre-m4-core-contracts` 分支。
- TDD 修 `DROPS_FROM`、Patch base、precondition、request/Patch identity。
- 一次性重录受影响 cassette，清理确认孤儿，Fix Pass Rate 10/10。

### 子里程碑 3：外部 qualification / Adapter / held-out

- 仅在 B0A pass 后规划。
- 完成 B0B、split、reader/Adapter、freeze 和外部 scorer。
- 任何产品优化只使用 development；final 用 held-out。

### 子里程碑 4：Narrative 与人工指标

- 先 development pilot 和协议冻结，再录正式 narrative held-out。
- 采集 narrative human judgments、30 HED cases、12 QA matched pairs。
- 人工 evidence 未完成时保持分支/里程碑进行中，不生成虚假默认值。

### 子里程碑 5：Report v2 / Eval / M3 acceptance

- 统一迁移 metrics/report/CLI/static HTML。
- 冻结最终 BenchReport 与 evidence manifests。
- 运行机器验收，更新 README/CLAUDE/plans 状态并合入 master。
- 只有此后开始 M4。

每个子里程碑从最新 `master` 新建独立 `codex/` 分支，分别编写实现计划、TDD、review、
验收和合入。后续 plan 不得跨过 gate 预写不存在的 Adapter 或语料结果。

## 15. PRD 追踪

| PRD 要求 | 本设计证据 |
|---|---|
| §5.6 外部效度 | 通用 profile/Adapter + 非注入 held-out |
| §12A.1 checker/adapter 测试 | property/round-trip/differential/import lint |
| §12A.4 rebase-or-reject | exact base mismatch `PatchRejected` |
| §13.3 双来源语料 | Aureus seeded + 开源真实 commit before/after |
| §13.4 分缺陷类 BDR | seeded/narrative/external per-class metrics |
| §13.4 false positive | deterministic、constraint、narrative、external 分列 |
| §13.4 Human-Edit-Distance | 30 human-final semantic-delta cases |
| §13.4 QA 工时节省 | 12 matched-pair manual vs assisted study |
| §13.4 Cost/Latency | cassette token/recorded latency + controlled deterministic runtime |
| §13.4 CI 功效 | narrative 381/class；power 用 evaluated n |
| §16 M3 验收 | deterministic acceptance validator |

## 16. 延后到 M4

以下接口/证据在 pre-M4 已完整定义，但产品化实现留 M4：

- React 全页面 Eval、Review、Patch、Approval、Observability
- maker-checker 在线队列和 RBAC
- Patch 显式 rebase/merge UI（当前 stale patch fail-closed reject）
- 人工标注在线协作平台（当前版本化离线 evidence/CLI）
- 生产对象存储、WORM、备份/恢复和跨服务事务
- 多 source 动态插件发现/安装/沙箱

延后不改变本设计的 schema、evidence、protocol 和报告字段；M4 消费它们而不是重新定义口径。

## 17. 设计验收

本设计只有在以下条件满足后才能进入实现计划：

- 用户书面审阅本文件并明确批准。
- 文件无未决占位项或相互矛盾的 gate。
- Flare frozen evidence 和负投资结论保持不变。
- 新工作被拆为独立子里程碑，第一份计划只覆盖通用 evidence + Endless Sky B0A。
- 任何需要真人的步骤都以真实 evidence 为门禁，不允许 Agent 代签或自动补值。
