# GameForge Pre-M4 Lean Closure Design

> 用产品证据替代审批仪式，闭合 M3 后再进入 M4

| 字段 | 值 |
|---|---|
| 状态 | 已批准：依据用户对后续实现的持续授权，以及“质量优先但不能过度设计”的明确反馈 |
| 日期 | 2026-07-12 |
| 范围 | M4 开始前仍未满足的外部真实语料、narrative BDR、HED、QA-hours、Cost/Latency 与 BenchReport v2 |
| 真相源 | PRD `2026-07-03-gameforge-prd.md`、地基契约 `2026-07-03-gameforge-foundations-contracts.md` |
| 修订关系 | 保留 `2026-07-11-pre-m4-product-closure-design.md` 的产品目标和跨游戏边界；替代其中额外引入的多轮人工审批、30-case HED、12-pair QA 与 narrative 逐例人工确认门禁 |

## 0. 决策摘要

PRD 要求真实外部缺陷、完整指标和可复现证据，但不要求把离线 benchmark 建成审批平台。
现有 B0A 通用化累计约 7,000 行代码、80 行候选审批表和多层 hash attestation，最终仍停在
`awaiting_human_evidence`。这说明流程证明了“谁批准过表”，却没有直接证明 GameForge 能否在
真实 before/after 配置上检出并修复缺陷。

本设计选择以下收口方式：

1. 上游开源项目的 bug-fix commit、PR 编号和作者历史是外部真人来源；GameForge 再用离线
   before/after predicate、原生 parser 和通用 checker 做确定性 qualification，不增加独立签字层。
2. Endless Sky 固定 8 个 config-only commit，覆盖 4 类，每类一例用于 Adapter 映射开发、
   一例在 mapping version 冻结后验证。验证失败保留在分母，不能换样本。
3. 新 reader 是 lossless token tree；Adapter 只把可证明的任务、条件、对话、空间和引用语义
   映射到通用 IR，其余字段原样保留，因此不是 Endless Sky 特化 checker。
4. narrative 使用无答案标记的结构化事实生成器作为 benchmark oracle。产品中的 Finding 仍是
   `llm-assisted/unproven`，但 seeded benchmark 不再要求真人逐条重复确认已知注入事实。
5. HED 使用上游作者的 after commit 作为 human-final target，覆盖同一批 8 个真实案例。
6. QA-hours 是唯一不可由确定性证据替代的人类指标：实现 4 个 matched pairs、单参与者
   case-study 协议，总 active-time cap 为 64 分钟；不外推为行业总体收益。
7. BenchReport v2、JSON/text/HTML、机器验收和 Cost/Latency 在同一 evidence manifest 上闭合。

## 1. 方案比较

### 方案 A：保留原研究级审批链

优点是每一步都有签字和 hash。缺点是 ground truth 仍要靠后续 predicate 才成立，且审批流程
已经比 Adapter、checker 和指标本身更复杂。它与用户“不再逐项审批”的要求冲突，不采用。

### 方案 B：让 LLM 自动裁决外部案例与人工指标

实现快，但会把 LLM 建议伪装成真人 ground truth，直接违反 PRD 的确定性优先与人工兜底。
不采用。

### 方案 C：上游真人 provenance + 确定性 qualification（采用）

保留 commit/PR/raw bytes/license/hash/replay，把“这个修复是否真的消除了目标缺陷”交给
before/after predicate 和通用 checker。只有 QA 劳动时间保留真实参与者，因为该量无法从代码
或模型诚实推导。这是产品价值、证据强度和工程复杂度之间最合适的边界。

## 2. 不可改变的产品边界

- `spine` 继续只依赖 `contracts`，不得 import Agent、LLM SDK 或 source profile。
- 通用 checker、taxonomy、metrics 和 report 禁止出现 `endless_sky`、commit OID 或源字段特判。
- source-specific 代码只允许存在于 reader、Adapter、fixture builder 和 qualification predicate。
- qualification predicate 只建立 benchmark ground truth，生产 checker 不能调用它。
- LLM 输出仍是提议；Patch 必须 exact-base、确定性复验并保持可审计。
- Flare 冻结 bytes、负投资结论和兼容 replay 保持不变。
- M4 的 React 全页面、RBAC、审批队列、生产对象存储和 WORM 不提前实现。

## 3. 真实外部语料

### 3.1 精简证据契约

新增 `external-case@1`，只保存能证明产品结论的字段：

```text
ExternalCase {
  case_id, source_id, source_repository, license_id,
  before_commit, after_commit, upstream_subject, upstream_pr,
  changed_paths, defect_class, target_locators,
  split: development | verification,
  predicate_id,
  before_tree_sha256, after_tree_sha256,
  native_before, native_after,
  predicate_before, predicate_after,
  reader_version, adapter_version, mapping_spec_sha256,
  finding_before, findings_after,
  agent_patch?, agent_target?, human_target,
  evidence_sha256
}

ExternalCorpusManifest {
  schema_version, source_id, pinned_head, repository_url,
  reader_version, adapter_version, mapping_spec_sha256,
  cases, manifest_sha256
}
```

`target_locators` 是非空有序列表，允许一个上游 commit 同时修复多个同类目标。`native_*` 保存
命令、exit code、stdout/stderr hash；`predicate_*` 必须分别为 violation/clear。
所有 raw trees 与 patch 保存内容 hash。没有 reviewer、approval payload、nonce、盲审状态机或
候选分配表，因为它们不改变 before/after 的事实。

### 3.2 固定案例

以下 8 个 commit 在任何 Adapter 实现前固定，不能因结果不好替换：

| class | split | commit | upstream evidence |
|---|---|---|---|
| dangling_reference | development | `02e6ded1e7cb9ef7a8e401e71c9accd6133a68b5` | non-existent sound reference, PR #10424 |
| dangling_reference | verification | `61425f7538b33ed5bddd77ea9c29ffd7737a242b` | missing goto/label, PR #9557 |
| cyclic_dependency | development | `2476129506e96086b00b09e1999dcb10ff8390fd` | potential conversation loop, PR #12045 |
| cyclic_dependency | verification | `95b5c4e95f715c2a13c201396d6dda5ea33d8cf7` | mission offer condition refers to itself, PR #9348 |
| unreachable_target | development | `9e437162fffef43da5f836d1f92bb265ccc75c52` | missions omit clearance for a restricted destination, PR #11977 |
| unreachable_target | verification | `34383dd960f42de2537a06c2bb0ba3f35a8a73c0` | jobs can select destinations without landing clearance, PR #11174 |
| dead_quest | development | `de8385df680ba81c70f13b380ef0b13070eba49b` | Terraforming 7 has no mission source, PR #4576 |
| dead_quest | verification | `9b29c95b99e67efbd1acda09a9994fe37405278e` | Free Worlds mission has no source definition, upstream commit |

每类 development case 可用于修 reader/Adapter mapping。verification case 在本设计中已冻结；
reader/Adapter、mapping spec 和通用 checker 禁止出现其 commit、路径、对象名或专属分支。
若 verification case 暴露 Adapter 缺陷，结果记 miss；修复后只能使用预先冻结的 reserve commit
形成新 report version，旧结果不可覆盖。这里不声称开发者从未看过公开 diff，防泄漏依靠的是
冻结样本、禁止特判、mapping version 和不可覆盖结果，而不是不可审计的记忆假设。

### 3.3 Lossless reader 与 Adapter

reader 把缩进式数据解析为 `DataNode(kind, tokens, children, source_span)`，保留原始 token、引号、
顺序、缩进和未知节点。`render(parse(bytes)) == bytes` 是第一 property。

Adapter 映射：

- `mission` -> `Quest`
- mission lifecycle/condition blocks -> `QuestStep`、`requires`、`precedes`、`gated_by`
- conversation `label/goto/branch/choice` -> `DialogueNode` 和有向边
- planet/system/wormhole/destination/clearance -> `Region`、`path_to`、`gated_by`
- named resource references（sound/effect/outfit/ship/phrase）-> typed entity/reference relation
- 未映射 token tree 保存在 Adapter-owned raw envelope，`from_ir(to_ir(x))` 必须逐字节还原

映射使用既有 IR 类型和关系；不得修改 checker 来识别 Endless Sky 字段。

### 3.4 qualification 与计分

每例必须同时满足：

1. changed paths 全部是配置文件；
2. before/after 均可由同一 reader 解析并逐字节 round-trip；
3. 固定上游 `DataFile` 词法/缩进语义的独立 C++ native parser 在两侧均能运行；该 witness
   保留上游 commit、派生说明、许可证与 source hash，不冒充当前环境不存在的完整游戏引擎；
   若未来接入完整引擎且原生 validator 本身以目标错误失败，则允许 before non-zero、after zero，
   但必须保存精确 stderr evidence；
4. 独立 predicate 返回 before violation、after clear；
5. 同一 mapping version 生成 before/after IR；
6. 通用 checker 在 before 命中正确 class/target，after 清除；执行错误和 unproven 都是 miss。

外部完成门：8/8 qualified、4 类均有 verification case，verification 按类报告 TP/miss 与
Wilson CI，after snapshots 上 external oracle-FP=0。开发例不计 final headline。

### 3.5 旧 B0A 代码

现有 Flare/Endless Sky B0A 工件作为历史审计资产保留，兼容 replay 继续测试，但不再扩展
`AdjudicationEvidence`、review package 或 human-attestation 状态机。新产品路径不 import 它们。
M3 完成后做引用审计：只有确认为未被兼容 replay 消费的代码才删除；不为追求行数进行冒险式
大重写。

## 4. Narrative BDR

### 4.1 case 与 oracle

`NarrativeCase` 使用结构化事实和模板渲染自然文本，保存：

```text
case_id, generator_version, seed, split,
facts, constraints, dialogue,
is_clean, defect_class?, target_entities, target_span
```

正式文本禁止 taxonomy 名、`TRAIT:`、`SPOILER:`、`CONTRADICTION:`、`UNIQUE-ROLE:` 等答案
标记。每类覆盖显式/隐式、同义改写、多实体、干扰事实和多个 Aureus 背景。

ground truth 来自生成前的 typed facts；renderer 和 oracle 独立实现并做 property tests。模型看不到
hidden class。positive TP 必须 class 正确、target entity 正确且 span 与目标句重叠；错误类、错误
目标、parse failure、fallback 和 cassette miss 均保留在分母。clean case 的任何结构化 hint 都计 FP。

### 4.2 Agent 输出

`ConsistencyHint` 升级为：

```text
defect_class, entity_ids, constraint_ids, span, rationale, is_suggestion=true
```

三个 perspective 都检查全部四类，区别只在推理方法：constraint matching、causal/world-state、
adversarial falsification。quorum key 为规范化 class/entity/constraint/span，不再比较自由文本 issue。
Finding 仍为 `oracle_type=llm-assisted`、`status=unproven`。

### 4.3 规模与复现

- development：每类 20 positive + 80 clean，用于 prompt/schema/matcher 调试；不进正式指标。
- verification：每类固定 381 positive，另有 381 balanced clean controls，满足最坏情况
  `p=0.5` 下 Wilson 95% CI 半宽不超过 0.05。
- 固定 generator、prompt、`openai/gpt-5.6-sol/pre-m4@1`、perspectives、threshold 和 matcher 后
  才 RECORD verification；随后两次零网络 REPLAY 必须逐字节一致。

这里报告的是 `seeded-oracle narrative BDR/FP`，不是“真人确认率”。产品 UI 仍要求人确认每个
真实项目 hint；benchmark 不伪造真人身份，也不让人工重复标注已知 generator oracle。

## 5. Human-Edit-Distance

上游 after commit 是真实 human-final patch。对全部 8 个 external cases：

1. 在 before IR 上生成 Agent typed Patch；
2. 应用并确定性复验；不可用 Patch 的 Agent delta 为空且 disposition=`agent_unusable`；
3. 将 before -> agent target 和 before -> upstream after 规范化为 atomic semantic deltas；
4. 报告 symmetric-difference raw/normalized distance、mean、median、bootstrap CI、unchanged、edited、
   unusable 和 protocol failure。

8 例全部进入分母；不能只保留 Agent 成功的案例。报告明确为单一开源项目的 8-case case study，
不外推到行业总体。契约保留任意样本量，未来 M4 可以追加真实人工审批后的生产 patches。

## 6. QA-hours Case Study

使用 8 个 external cases 组成 4 个同类 matched pairs。一个真实参与者完成每 pair 两侧：一侧
manual、一侧 assisted；arm 与顺序按固定 schedule 交替。manual 可用源文件、编辑器和原生
parser，不能看 GameForge Finding/IR/Patch；assisted 可用完整 GameForge workflow。

每 session 保存 `start/pause/resume/finish` monotonic events、active/elapsed duration、最终 patch、
同一 correctness verdict 和 protocol hash。每 session active cap=8 分钟，总 cap=64 分钟。超时或
错误是有效 outcome；计时缺失、arm 污染或无 verdict 是 protocol failure。

报告 paired minutes/percentage、median、bootstrap CI 和两臂成功率。只有 CI 下界大于 0 且 assisted
成功率不低于 manual 才允许写“节省”；否则写 `inconclusive` 或负收益。单参与者结果只描述该
参与者和任务集。

这一步不需要参与者审批设计或 hash，只需要真实完成盲化任务；Agent 不能代替参与者。

## 7. BenchReport v2

使用完整指标对象：

```text
BinaryMetric {
  name, defect_class?, bucket, planned_n, evaluated_n,
  k, rate?, ci_low?, ci_high?, ci_method,
  status, protocol_id, evidence_manifest
}

DistributionMetric {
  name, unit, bucket, planned_n, evaluated_n,
  mean?, median?, primary_estimate?, ci_low?, ci_high?, ci_method,
  status, protocol_id, evidence_manifest
}
```

`status = pending | measured | underpowered | inconclusive | failed`。缺 evidence 时数值为 null，
不能用 0 冒充。`BenchReport` 升级为 `bench-report@2`，包含 seeded、FP、agent、power、external、
narrative、HED、QA、Cost/Latency 与所有版本/evidence refs。

JSON 是权威输出；text 和静态 HTML 只渲染同一 model。三者逐字段 contract test。旧 v1 没有
已发布 API，直接拒绝并给出清晰 schema error，不维持双口径。

Cost/Latency 从 cassette 的 token usage、attempts 和 record-time latency 聚合；确定性 pipeline
在固定环境 manifest 下单独测量。没有可信 price book 时只报告 token cost，不编造货币价格。

## 8. 机器验收

`validate_m3_acceptance(report, manifests)` 只有在下列条件全部成立时返回空列表：

1. seeded corpus >=500，15 类均有 evaluated BDR；
2. narrative 四类各 381 positive，381 clean controls，CI 与 availability 已报告；
3. deterministic oracle-FP=0，narrative seeded-oracle FP 单列；
4. external 8 cases/4 classes 全部 qualification 完整，4 个 verification cases 按类计分；
5. external verification 每类至少一个 before-hit/after-clear，after external oracle-FP=0；
6. HED 8/8 有 upstream human target，失败 Agent patch 未被删除；
7. QA 4/4 matched pairs 有两臂真实 session evidence 和 correctness verdict；
8. Agent token/latency 与 deterministic runtime manifest 已测量；
9. JSON/text/HTML 同源；所有 cassette miss、parse/Adapter/checker 失败在正确分母；
10. 全量 pytest、7 条 import contracts、Ruff、`git diff --check` 和两次 REPLAY 通过。

HED/QA 可以差或无正收益；真实负结果仍满足“完成测量”。`pending`、伪造真人、删失败样本或
source-specific checker 特判不能通过。

## 9. 实施切片

1. **External cases + lossless reader/Adapter**：冻结精简 manifest，TDD parser/round-trip、IR mapping、
   predicates 和 verification scorer；保留旧 B0A replay，不再扩审批状态机。
2. **Narrative evidence**：升级 hint 契约/prompt/quorum，开发集调试，冻结并 RECORD verification。
3. **HED + QA protocol**：复用 external cases，生成 Agent drafts、HED；实现 timer/task bundle，采集
   4 个真实 matched pairs。
4. **Report v2 + acceptance**：统一 schema、views、cost/latency、evidence verifier 和最终 gate。
5. **Pre-M4 audit**：逐条对 PRD §13/§16，删除确认无消费者的过度设计，运行全量验证并更新路线图。

每个切片独立 plan/TDD/commit/review。不得因为 QA 人类数据尚未采集而停止其他切片；在真实
session evidence 到齐前，M3 保持进行中，M4 不开始。

## 10. 自审结论

- 没有 TBD/TODO 或依赖未来审批的设计决策。
- PRD 的真实外部语料、15 类 BDR、FP、HED、QA-hours、Cost/Latency、CI 和 Eval 输出均有证据路径。
- 人类不可替代部分只剩 QA 劳动时间；外部 provenance 和 HED 使用真实上游作者，不伪造身份。
- 核心 checker 不感知游戏来源，Endless Sky 只存在于 reader/Adapter/fixture/predicate 边界。
- 未提前实现 M4 平台能力，也没有新增插件系统、审批服务、rebase/merge 或安全竞态框架。
