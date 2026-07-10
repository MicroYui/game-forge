# M3d Flare-RICH 真实缺陷语料设计

> 日期：2026-07-10
> 状态：已获用户书面批准（2026-07-10）
> 前置：M3a/M3b/M3c 切片与 pre-M4 economy sink 增量已落地；M3 umbrella 仍未满足 PRD §16 全部验收
> 顺序：B0 证据闸门 -> M3d Flare-RICH -> M3d 验收/必要优化 -> M4
> 范围：本文闭合 M3 的外部真实语料债务，是进入 M4 的必要门；narrative BDR、Human-Edit-Distance 与 QA 工时节省另行闭合

## 1. 背景与判断

GameForge-Bench 当前有两类 Flare 外部信号：

1. 把当前 Flare 内容片段导入 IR 后运行检查器，暴露 `isolated_node` 适配器完整性问题；
2. 在真实 Flare 拓扑上注入 `dangling_reference`，验证检查器不只适用于 Aureus 拓扑。

这两类信号都不是 Flare 历史中的真实非注入缺陷。当前
`ExternalReport.n_defect_samples == 0`，而 PRD §13.3 明确要求从开源游戏的真实
commit/补丁历史中抽取修复前后样本。因此 M3d 不是一个可有可无的新方向，而是 M3
尚未偿还的外部效度债务；在 M4 固化 API、血缘和 Eval UI 之前先补它更合理。

预调研在 Flare 的 7049 个提交中找到了多批真实 bug-fix 候选，任务状态与掉落配置
尤其丰富。但目前只能得出“原始候选丰富”，不能得出“7 类、13+ 个合格样本已经成立”：

- commit diff 能证明发生了修复，不自动证明它属于 GameForge 的某个 taxonomy；
- 同一根因的 follow-up、cherry-pick 或 revert/reapply 不是独立样本；
- 指向存在但设计上错误的对象，通常没有确定性 oracle；
- 任务日志显示时机等真实 bug 可能落在现有 11 类之外；
- adapter 尚未表达历史 dialect、状态生产/消费和 query-complete 文件闭包。

所以推荐路线不是直接投入完整 adapter，而是先做 B0 证据闸门。只有 B0 证明独立、
可审计、可静态判定的语料达到最低规模，才进入 M3d 重型实现。

## 2. 目标与非目标

### 2.1 目标

1. 冻结可复现的候选搜索框架和完整 candidate ledger，验证“RICH”是否成立。
2. 若证据闸门通过，建立来自 `flareteam/flare-game` 真实 config-only 修复的
   before/after 语料；每个样本可追到 fix lineage、commit、文件哈希、changed hunk、
   语义见证和人工裁决。
3. 只扩展 accepted cases 所需的 Flare reader、quest/status、loot 与引用语义，形成
   对目标查询完备的 semantic slice，而不是重做完整 Flare 引擎。
4. 建立 Flare 专属 applicability、dialect 和 constraint profile；不复用 Aureus 的
   数值边界或隐含世界假设。
5. 对每个真实修复单元分别报告 pre-fix recall、post-fix target clearance 和 paired
   discrimination；工具失败留在冻结分母内。
6. development 与预注册的 temporal evaluation 按独立 fix group 分离；首次评估后
   保留原始版本证据，不能调参后在同一 cohort 上重新宣称外部验证。
7. 正式 benchmark 完全离线、确定性、零 LLM；同一冻结输入与工具版本输出字节一致。
8. 扩展 `BenchReport.external` 与 M3c 静态 Eval 视图，为 M4 提供稳定 JSON 契约。

### 2.2 非目标

- 不新增 taxonomy 类别来迁就 Flare 历史；out-of-taxonomy bug 进入排除清单。
- 不运行或重实现完整 Flare engine、存档系统、战斗、渲染、音频或 UI 语义。
- 不把平衡调整、设计偏好或“合法但选错对象”的变化伪装成确定性缺陷。
- 不把修复后提交假定为全仓库 clean；只裁决目标 finding 的 paired 变化。
- 不用目标 commit SHA、文件名或实体名编写 checker 特判。
- 不在 CI 中 clone/fetch 上游仓库；联网只属于可选 mining 输入准备。
- 不建设数据库、分布式爬虫、通用 artifact service 或 blob GC。
- 不实现 M4 的 FastAPI、RBAC、maker-checker、完整 React 页面和可观测平台。

## 3. 核心决策

### D1. B0 是投资闸门，“RICH”不是预设结论

B0 分成两个连续阶段：

**B0A evidence ledger** 冻结上游 head、revision range、路径白名单、搜索规则版本和
候选集合，并完成人工初裁。进入 B0B 的 provisional gate 为：

- 至少 8 个互相独立的 `fix_group_id`，不是 8 个 raw commits；
- 至少覆盖 4 个现有 deterministic/simulation taxonomy 类别；
- 每个 group 都是 config-only 修复，并至少有一个可映射 taxonomy 的 proposed case；
- 11 个类别都有完整的领域适用性、证据状态和实现状态三维矩阵；
- 每个候选，无论接受、拒绝或存疑，都保存可离线复核的 diff evidence 和裁决理由。

B0A 的 case disposition 使用 `proposed | rejected | ambiguous`；`qualified_candidate`
只允许由 B0B 逐 unit 资格审计产生，`accepted` 只允许由 Corpus Freeze Gate 产生。
provisional gate 按至少含一个 `proposed` case 的独立 group 计数。group disposition 只是由
case dispositions 派生的摘要，不作为多标签 group 的统计或裁决真源。

**B0B qualification audit + bounded feasibility spike** 对每个拟接受 unit 完成人工
资格审计：冻结 taxonomy label、raw source/diff witness、因果 hunk、目标 identity、dialect
证据和 query-complete closure requirements。然后为每个拟覆盖类别至少选择一个代表
case，并覆盖最老 dialect 和风险复杂度最高的闭包，验证输入可重建、相关事实可抽取、
声明式 qualification predicate 可重放，且现有或明确新增的通用 oracle 能表达该类。
search frame 预先冻结 complexity tuple 与排序：`(distinct relevant file-family count,
mod-stack depth, include/append/override depth, relevant-unknown count, input bytes, file count)`，
代表 case 取 lexicographic maximum，不能凭主观印象挑“复杂”样本。

B0B 结束时按相同的 8 groups / 4 classes 口径重新计数，但只计算通过逐 unit 资格审计的
`qualified_candidate`；这才是 final investment gate。B0B 只冻结 qualified-candidate
集合、qualification registry、split assignment 和投资结论。紧接着的 Corpus Freeze
Gate 只用 raw Git objects、独立 qualification replay 和人工证据验证 lineage、config-only、
InputTree、locator、witness 与 ground truth；它在生产 reader/checker 实现前冻结 final
accepted manifest 与主分母。生产 reader 的当前能力不是排除条件，后续无法解析、覆盖或
匹配的 accepted unit 必须保留并记 `execution_failure`。

8 groups / 4 classes 只是“值得投资”的最低门槛，不是统计功效、检出效果或 M3d 完成
声明。若初裁不足，只允许扩大已冻结规则允许的历史范围、issue/PR 证据和非 `fix`
命名提交搜索；不得放宽资格、扩 taxonomy 或按结果改标签。二次裁决仍不足时输出
`insufficient_evidence`，停止 Flare 重型 adapter 投资。

初轮不足是非终态 `expanded_round_required`，只允许执行预冻结的 expanded round。expanded
evidence 必须引用初轮 ledger/decision 哈希，逐 candidate 保持初轮 disposition、label、
rationale 与 evidence refs 不变，只为新发现候选追加裁决；harness 必须拒绝借扩搜改写初轮
决定。只有二轮仍不足才产生终态 `insufficient_evidence`。

“扩大”必须在 search frame 中事先声明为 `initial` 与 `expanded` 两轮；两轮各自的 revision
range、查询规则、排序和停止条件在首次 discover 前共同冻结。不得在看到初轮门禁结果后
新增搜索规则。B0A 只实现 `discover`、`adjudicate` 和 provisional gate；`probe` 属于 B0B，
`freeze` 属于 Corpus Freeze Gate。

`insufficient_evidence` 与 `validated` 是互斥终态。前者是诚实的负结果，但不满足
PRD §13.3，也不能被写成 M3d 完成；此时进入 M4 前必须另选外部语料源或由用户明确
批准范围豁免。

### D2. 搜索框架与独立修复 lineage 必须先冻结

candidate search frame 至少记录：

- source repository、pinned head、可达 revision range 和 Git object IDs；
- 配置路径/扩展名 allowlist、排除路径和 config-only 判定规则；
- commit message、diff signature、issue/PR evidence file 和相邻提交的查询规则及工具版本；
- 候选排序、去重、停止条件和生成 ledger 的命令参数。

revision range 必须明确端点的包含语义与 history walk；patch evidence 必须冻结精确 Git
参数和原始输出字节。所有 CAS key 使用 lowercase 64-hex SHA-256，`sha256:<hex>` 只用于
已有 Snapshot ID 契约，不能混作 blob 文件名。mining 输出按 canonical JSON 写入；目标已
存在时只允许逐字节相同的幂等重放，任何差异必须写入新的 evidence revision，禁止覆盖。

qualified/accepted 修复严格限定为 config-only：所有行为相关 changed paths 都必须属于冻结的
配置/内容 allowlist；包含引擎源码、脚本运行时、构建逻辑或 schema 解释器修改的
commit 不接受。这样 before/after 可在同一已固定的历史语义下比较，不混入引擎版本
变化。

统计与 split 的根单位是独立 `fix_group_id`。工具只自动识别 path/message、patch-id、
明确 cherry-pick/backport 和 revert 等客观信号；同一 issue/根因、semantic follow-up 和
多 commit 修复由人工给出 evidence 后分组，harness 只验证边界与证据完整性，不尝试
自动理解根因。issue/PR 证据由显式离线 JSON 输入，不在 mining 命令中隐式联网查询。

多 commit group 的 before/after 必须构成连续、完整的 first-parent range，range 内每个
commit 都通过 config-only allowlist；merge commit 必须记录选择的 parent edge。夹带
引擎/schema/其他行为改动或非连续拼接的 group 直接拒绝。一个 group 可以有多个
taxonomy cases，但不会因多个 hunk 或 targets 膨胀独立样本数。

Wilson 区间只能称为“在冻结 purposive corpus 上的条件性描述区间”。candidate
selection recall 未知，不能把区间外推为所有 Flare 历史 bug 的总体置信区间。

### D3. commit diff 是证据来源，不是 taxonomy ground truth

主计分单位固定为 `(fix_group_id, defect_class)`。同一单位可以包含多个 target，target
只作为嵌套诊断；只有全部目标满足 paired predicate，该单位才算成功。

每个 accepted unit 必须同时具备：

1. commit message、issue/PR 或相邻上下文证明该 lineage 在修 bug；
2. parent/before 与 final fix/after 边界明确，且修复是 config-only；
3. `qualification_rule_id` 指向冻结、带哈希的声明式逐类资格规则；
4. `before_semantic_witness` 证明修复前违反该规则；
5. `after_resolution_witness` 证明修复后目标被解决，而不是被解析器漏读；
6. changed hunk 与 witness 的因果链接；
7. 精确、稳定、分侧的 target locators；
8. query-complete closure 和历史 dialect 证据足以支持 closed-world 判断。

qualification rule 不是第二套 checker。它是人审的 taxonomy predicate，定义该类需要
哪些结构化事实；witness 直接引用 raw source、diff hunk 和对应 engine revision 的文档/
代码证据。独立 replay validator 只校验规则版本、locator 可解析、hunk/hash 一致以及
before/after facts 满足声明式 predicate，不复刻被评估 checker 的搜索算法。每次裁决
保存 adjudicator、independent reviewer 和 decision revision，但不提前建设 M4 RBAC。

locator 不能只存会漂移的行号。最小结构为：

```text
TargetLocator {
  path,
  block_key,
  key?,
  occurrence?,
  relation_identity?,
  missing_target_identity?,
  context_sha256
}
```

每个 target 有 `before_locator` 和可选 `after_locator`；删除、改名或移动修复允许 after
locator 缺失或不同。`target_fingerprint` 由 semantic identity 生成，不由物理行号生成。
逐类 matcher 必须显式注册，不能让 scorer 猜测不同 checker 的临时 evidence 字典。

标签、locator 或资格规则在首次 checker 运行后若被证明错误，必须生成新 corpus
version，并作废旧 version 的 headline；不得在原版本中改分母。若错误是在 registered
first evaluation 后发现，同一 revision window 的修正版只能标为
`post_hoc_corrected_analysis`，不能重新获得 first-evaluation claim。M3d 要恢复
`validated`，必须预注册来自不重叠后续 revision window 的新 corpus；单纯降级 capability
claim 只适用于真实执行故障，不适用于错误 ground truth。

### D4. applicability 拆成三个正交维度

11 类矩阵不再使用一个 `supported | not_found | not_applicable | unsupported` 字段混合
不同概念，而是分别记录：

```text
domain_applicability: applicable | not_applicable
evidence_availability: found | not_found
evidence_counts: {proposed, qualified_candidate, accepted, rejected, ambiguous}
implementation_support: planned | supported | unsupported
```

B0A 阶段 `qualified_candidate == 0` 且 `accepted == 0`。B0B 将通过逐 unit 审计的
`proposed` 转为 `qualified_candidate`，Corpus Freeze Gate 再派生 `accepted`。同一 group
的不同 taxonomy cases 可以有不同 disposition。

`qualified_candidate` 只属于领域适用且通过 B0 资格审计的 case；`accepted` 还必须
通过只审独立 ground truth/输入完整性的 Corpus Freeze Gate。生产 reader、matcher 或
checker 能力不是 accepted 资格。运行时 checker miss 或 adapter error 不能把它改成
`unsupported`。同一类别可以同时有 accepted、rejected 和 ambiguous 候选，所以证据轴
使用 availability + disposition counts，而不是强迫一个类别只有一个 disposition。

Flare 的 loot `chance/weight` 是独立抽取语义，不要求总和为 1，所以
`prob_sum_ne_1` 为 `not_applicable`。Flare 没有 gacha/pity 契约，
`gacha_expectation_violation` 为 `not_applicable`。Flare 存在金币来源和 vendor 回收口，
所以 `economy_collapse` 的领域状态是 `applicable`；若历史证据或静态时序不足，分别记
`not_found` 和 `unsupported`，不能伪装成永久 N/A。

### D5. 使用公开、非盲、预注册的 temporal evaluation

本项目没有独立 corpus custodian；同一执行者在 B0 已经看过历史候选。因此私有 bundle
或事后 seal 不能恢复盲性，也会提前引入属于 M4 的权限与工件托管。M3d 采用更弱但诚实
的 **pre-registered, non-blind temporal evaluation**：

1. B0B 在 checker/profile 优化前按 `fix_group_id` 冻结 split；同一根因、issue、
   follow-up、cherry-pick 或多标签 group 不得跨 split。
2. groups 按 `(after_committed_at, fix_group_id)` 排序，并只允许一个时间 cutoff：较早
   prefix 是 development，较新 suffix 是 evaluation。枚举满足 evaluation 至少 25% 且
   不少于 2 groups 的 cutoff，依次按“dev/eval 都有独立 group 的类别数最多、dialect
   coverage 最大、evaluation groups 最少、cutoff key 最小”确定唯一选择。无法满足的
   strata 明确列出。某类只有在两侧都有独立 group 时才报告 evaluation BDR，singleton
   只作定性 case；多标签 group 整体分配，不拆分。
3. B0B 把 provisional split assignments 写进 candidate ledger；Corpus Freeze Gate 只能
   删除被独立证据证明 ground truth 无效的 units，不能因 reader/coverage/matcher 能力
   删除，也不能重新分配剩余 groups。随后公开的
   `evaluation-registration.json` 保存原 assignment hash、split algorithm/version、final
   group/case IDs、qualification registry hash、target matcher contract hash、scoring
   protocol hash、corpus hash 和预期 strata。它所在 Git commit 必须是首次评估
   checker/profile/matcher freeze revision 的祖先。
4. 首次评估要求 clean worktree，并保存 corpus、reader、profile、checker、matcher、
   runner revision 以及 report SHA-256。该报告不可被后续结果覆盖。
5. 相同 cohort 后续在 CI 重跑只叫 regression。根据 evaluation 做的优化可以修产品，
   但不能重写首次报告；只有新的、不重叠的后续 revision window 才能形成新的 temporal
   external-validation claim。

公开 annotation 会带来实现者认知泄漏，因此本设计不称它为 blind/held-out。缓解措施是
预注册、精确 matcher、禁止样本特判、保留首次结果和逐 failure 审计；剩余偏差必须在
报告中显式声明。evaluation 中的历史语法、adapter error 或 matcher failure仍计端到端
失败，不能移出分母。

### D6. 冻结分母与完整 paired outcome

主分母在首次运行前冻结为所有 accepted、domain-applicable 的
`(fix_group_id, defect_class)` units。每个 unit 的多个 targets 必须全部满足。结果先由
两个布尔 verdict 表达，再派生状态：

| before target finding | after target finding | paired status |
|---|---|---|
| present | absent | `detected_and_fixed` |
| present | present | `detected_but_persists` |
| absent | absent | `missed` |
| absent | present | `unexpected_after` |

adapter、reader、checker、timeout、coverage 或 matcher 错误产生 `execution_failure`。
这些错误在所有端到端指标中均算失败，并按原因另报。

逐类、逐 split 分别报告：

- `pre_fix_recall`：before target present / 全部冻结 units；
- `post_fix_target_clearance`：after target absent / 全部冻结 units；
- `end_to_end_bdr`：`detected_and_fixed` / 全部冻结 units；
- 可选 `conditional_detector_rate`：仅供诊断的可执行子集指标，禁止作为 headline。

Wilson 95% 区间按 `(fix_group_id, defect_class)` 计算，不按 target 计算，不把类别合并成
一个漂亮总分，并明确标注 corpus-conditional 和 under-powered。旧 ExternalReport 的
`n_defect_samples/detected/detection_rate/ci_*` 在 schema bump 后只保留一版兼容读取；
静态 Eval 不再展示混合 rate，验收也不使用它。

每个非 `detected_and_fixed` unit 还必须有结构化 triage：

```text
ExternalFailureTriage {
  unit_id,
  root_cause: corpus | reader | profile | matcher | checker | static_limit,
  evidence_refs[],
  action: fixed | accepted_limit | invalidates_corpus_version | new_cohort_required,
  followup_revision?
}
```

triage 解释结果但不改变原 status、split、label 或分母。

评分分析计划也必须在 Corpus Freeze Gate 冻结为 canonical `scoring-protocol.json`。它覆盖
unit/target 聚合、完整 2x2、execution-error mapping、各指标分子/分母、Wilson 计算、
headline eligibility 和 capability-state rules。registration 保存其 schema/version/hash；
正式 scorer 直接消费并验证该 protocol，不能在首次 evaluation 后只改实现逻辑而保持
registration 不变。

### D7. 状态语义是 case-driven proof slice，不是文件名映射

Flare 的 `[quest]` block 表示条件化 journal-state view，不默认等同可执行
`QUEST_STEP`，也不能凭文件顺序生成 `PRECEDES`。现有 GraphChecker 的 `dead_quest`
只检查 `STARTS_AT/HAS_STEP`，`unsatisfiable_completion` 只检查 `PRECEDES`；它们不能
直接裁决 Flare status graph。

B0 positive 后必须先补一份由 qualified candidates 驱动的 semantic-slice addendum，再写
M3d 重型实现计划。若状态类进入 final accepted corpus，新增独立的通用 status-flow checker，
而不是把 Flare 规则塞进 bench scorer。addendum 至少冻结：

- status 命名空间、mod/context scope、初始状态和允许的外部 producer；
- `requires_status` / `requires_not_status` 的正负极性、group 和 AND/OR quantifier；
- `set_status`、`unset_status`、`pickup_status`、随机 effect 的不同语义；只实现样本需要
  且能从对应 engine revision 证明的 effect；
- NPC variant、map/event context、dialogue 内动作顺序与分支；
- 基于可达 producer 的 least-fixed-point reachability；
- 每个进入 final accepted corpus 的 taxonomy class 的精确 proof rule。

候选进入相应类别时，最低 proof rules 为：

- `dead_quest`：目标 activation event/journal state 没有可达 producer；
- `unsatisfiable_completion`：activation 可达，但 completion producer 不可达；
- `cyclic_dependency`：阻断目标的依赖 SCC 没有可达的外部 producer；
- `missing_drop_source`：目标 item demand 存在，但 query-complete producer index 中没有
  可达 producer。

无本地 producer 的 status 可能来自初始存档或外部条件，不能自动报 dangling。
`requires_not_status` 必须保留负极性，`unset_status` 必须保留 clear effect。影响结论的
negation、removal、order、branch 或 variant 未被建模时，qualification 为 `unproven`，
不能接受该 case；冻结后才暴露的同类问题则记 execution failure。

### D8. 只构建 query-complete closure

每个 case 从明确的 mod stack、load order 和 input roots 出发，构建对目标查询完备的
闭包：

- 展开相关 `INCLUDE`、`APPEND`、override 和依赖 mod；
- dangling-reference case 需要完整 definition index；
- “不存在 producer”类结论必须反向扫描该历史 mod stack 中所有能生产/消费相关
  status 或 item 的受支持文件族；
- closure 记录每个文件的角色以及为何与 query 相关。

这不是全 Flare semantic closure。音频、贴图、UI 等与目标 oracle 无关的未知语法不应
污染 case；影响结论的未知语法必须产生结构化 `unsupported_syntax`、
`unresolved_include` 或 `coverage_gap`，不得静默忽略。

reader 使用 Flare 专属结果 envelope，而不重做所有 adapter 协议：

```text
FlareReadResult {
  snapshot,
  diagnostics[],
  coverage {roots, files_seen, files_parsed, relevant_unknowns[]}
}
```

读取预算限制 include 深度、文件数、总字节和物理行数。freeze 拒绝绝对路径、`..`、
大小写冲突、symlink、非 regular file 和非 UTF-8 accepted input。所有 manifest path
使用规范化相对 POSIX 路径。

### D9. 物理无损与语义正确性分开验证

adapter/reader 同时维护两条质量线：

1. **物理往返**：受支持文本 `render(parse(x)) == x`，保留重复 key、注释、空行、
   include 和历史语法；blob SHA-256 始终基于原始 bytes。
2. **语义 golden graph**：冻结的 development fixture 产生稳定 entities、relations、
   attrs、source locators、diagnostics、coverage 和 snapshot ID。

Flare reader 产出的 `SourceRef.row` 改为真实 1-based 物理行：实体使用 block 起始行，
派生 relation 使用对应 key 行。record ordinal 另存 attrs，不能继续冒充行号。这个变更
不要求 Aureus CSV 的 row 语义一起迁移。重复 `loot=` 等 key 通过 occurrence 和 relation
identity 区分。

### D10. `DROPS_FROM` 是现有 IR 契约修复

当前 Graph/ASP/economy/scenario/prompt 已按 `producer -> produced object` 消费
`DROPS_FROM`，但 Aureus 与 Flare adapter 的部分物品掉落仍生成 `item -> monster`。
当前仓库没有提交序列化的 `ir-core@1` snapshot payload，也没有已发布的反向边兼容承诺；
因此这里按 adapter 违反既有意图的 bug 修复处理，不把它扩张成全局 `ir-core@2` 迁移。
foundations contract 必须补写方向和合法端点：

```text
MONSTER | DROP_TABLE | INTERACTABLE | EVENT | BATTLE_ENCOUNTER
    --DROPS_FROM--> ITEM | CURRENCY
```

随机或 loot-table 驱动的掉落使用 `DROPS_FROM`；确定性任务奖励、脚本授予或 pickup 使用
`GRANTS/REWARDS`，其 producer 为对应 `QUEST_STEP/EVENT/INTERACTABLE/NPC`。若实际 source
引用一个 `DROP_TABLE`，用明确的 ownership/trigger relation 连接 source 与 table，再由
table 指向 item；missing-source reachability 必须能回溯到真实 source。具体映射只为
accepted feature matrix 中出现的 producer 实现，不能把文件路径误当 item ID。

修复必须审计 foundations contract、Aureus/Flare adapters、fixtures、injector、Graph/ASP checker、economy
simulator、scenario generator、agent prompt、repair operations 和 tests。relation ID、
snapshot ID 与 repair request 会变化，因此 cassette 只在语义与 tests 稳定后统一重录；
版本仍为 `ir-core@1`，但 changelog 必须记录方向纠正。若 B0 或仓库审计发现真实持久化
反向边 artifact/外部兼容需求，则停止本项，另写独立 migration spec；不能把迁移器临时
塞进 M3d。

该契约 bug 已由当前仓库审计独立确认，因此不以 B0 positive 为前提。即使 B0 最终为
`insufficient_evidence`，也必须在进入 M4 前通过单独计划修复方向、审计跨系统消费者并
处理 snapshot/cassette 影响；B0 negative 只停止 Flare 重型 reader/checker 投资。

## 4. 语料与存储契约

### 4.1 Candidate ledger 与 runtime manifest 分离

candidate ledger 保存搜索与裁决过程，不要求 rejected/ambiguous 候选拥有完整输入树：

```text
CandidateLedger {
  schema_version,
  source_repo,
  search_frame,
  search_spec_sha256,
  candidate_universe_sha256,
  search_round: initial | expanded,
  observed_revision_count,
  discovery_tool {tool_version, project_commit_oid, git_version},
  evidence_revision,
  applicability_matrix,
  gate_summary,
  groups: [CandidateFixGroup...]
  candidate_decisions: [CandidateDisposition...]
}

CandidateFixGroup {
  fix_group_id,
  commits[],
  before_commit,
  after_commit,
  after_committed_at,
  changed_paths[],
  config_only,
  diff_evidence: [{commit_oid, patch_sha256, patch_blob, commit_message}],
  cases: [CandidateCase...],
  disposition_summary: proposed | qualified_candidate | rejected | ambiguous,
  rationale,
  lineage_links[]
}

CandidateCase {
  case_id,
  defect_class,
  disposition: proposed | qualified_candidate | rejected | ambiguous,
  rationale,
  evidence_refs[]
}

CandidateDisposition {
  commit_oid,
  disposition: rejected | ambiguous,
  reason_code,
  rationale,
  evidence_refs[],
  adjudicator_id,
  reviewer_id
}
```

`disposition_summary` 必须由 cases 确定性派生；任何 gate、split 或指标都读取 case/unit
disposition，而不是摘要。B0A gate summary 记录 proposed group/class 数、失败原因和
`provisional_pass | expanded_round_required | insufficient_evidence`。`fix_group_id`、人工 root-cause grouping 证据、
merge parent edge 与客观 lineage link 类型均由 schema 固定，不能使用自由文本暗示分组。

不属于现有 taxonomy、不是 bug、非 config-only、revert/backport 重复或缺乏可判定 oracle
的候选不得伪造 `CandidateCase.defect_class`；它们进入 `candidate_decisions`。最终 ledger 中
`groups[].commits` 与 `candidate_decisions[].commit_oid` 必须不重叠且并集恰好覆盖 discovery
universe。三维 matrix 的 rejected/ambiguous 只统计已有 defect-class case；无类别排除在
gate summary 中按 `reason_code` 单列。

所有候选的规范化 patch/diff evidence 都进入离线 blob；因此 rejected/ambiguous 的排除
理由不依赖未来仍存在的本地 Git clone。

qualification registry 与 ledger 一起冻结。registry 为每个 `qualification_rule_id` 定义
适用 defect class、必需 witness fields、声明式 before/after predicate 和 engine evidence
requirements，并把 canonical SHA-256 写入 registration/manifest。witness 使用结构化模型：

```text
SemanticWitness {
  fact_kind,
  subject_locator,
  predicate,
  object_or_value?,
  polarity?,
  supporting_locators[],
  engine_evidence_refs[]
}

EngineEvidence {
  evidence_id,
  repo,
  revision,
  path,
  context_or_range,
  blob_sha256,
  excerpt_sha256,
  license_id
}

CausalHunk {
  path,
  patch_blob_sha256,
  before_context_sha256,
  after_context_sha256
}

Adjudication {
  decision,
  adjudicator_id,
  reviewer_id,
  decision_revision
}
```

`engine_evidence_refs` 只能引用 registry 中的 `EngineEvidence.evidence_id`。对应最小 engine
代码/文档 blob 与许可证进入同一 filesystem CAS；URL、未来 local clone 或裸行号不能作为
唯一证据。qualification replay 会校验 revision/path/blob/excerpt/license 全链。

runtime manifest 在 Corpus Freeze Gate 后生成，只包含逐 unit 通过独立 ground-truth 与
raw InputTree 验证的 accepted groups；不按生产 reader/coverage 能力筛选：

```text
ExternalCorpusManifest {
  schema_version,
  canonical_encoding_version,
  corpus_id,
  manifest_sha256,
  source_repo,
  source_license,
  candidate_ledger_sha256,
  qualification_registry_sha256,
  matcher_contract_sha256,
  scoring_protocol_sha256,
  evaluation_registration_sha256,
  created_from_tool_version,
  applicability_matrix,
  groups: [ExternalFixGroup...]
}

ExternalFixGroup {
  fix_group_id,
  commits[],
  split: development | temporal_evaluation,
  dialect {dialect_id, engine_repo, engine_revision, evidence},
  before: InputTree,
  after: InputTree,
  cases: [ExternalCase...]
}

InputTree {
  git_commit_oid,
  git_tree_oid,
  content_tree_sha256,
  closure_manifest_sha256,
  input_fingerprint,
  mod_stack[{mod_id, root, precedence, version}],
  roots[],
  files[{path, blob_sha256, size}],
  closure_entries[{path, role, rationale, evidence_refs[]}]
}

ExternalCase {
  case_id,
  defect_class,
  qualification_rule_id,
  before_semantic_witness,
  after_resolution_witness,
  causal_hunks[],
  targets[{target_fingerprint, before_locator, after_locator?}],
  oracle_profile,
  adjudication
}
```

Git OID 与冻结内容的 SHA-256 分开保存：

- `content_tree_sha256` 对按 path 排序的 `path + NUL + blob_sha256 + NUL + size` 计算；
- `closure_manifest_sha256` 对 canonical、按 path/role 排序的 `closure_entries` 计算；每个
  entry 必须引用 `files` 中的 path，并保存可审计 rationale/evidence；
- `input_fingerprint` 对 canonical `mod_stack + precedence + roots + dialect_id +
  engine_revision + content_tree_sha256 + closure_manifest_sha256` 计算。

run identity、registration 和 provenance 使用 `input_fingerprint`，不能只用文件树 hash；
所有 canonical encoding 明确定义且不依赖 wall-clock、inode 或遍历顺序。

corpus/registration 哈希构成无环 DAG：

1. `corpus_payload_sha256` 覆盖 corpus schema version、canonical encoding version、
   qualification DSL version/hash、matcher-contract version/hash、scoring-protocol
   version/hash、source/license、candidate-ledger hash、applicability、groups、InputTrees 和
   cases，但排除 corpus ID、registration、结果与生成时间；
2. `corpus_id = corpus_payload_sha256`；
3. registration 自带 schema/canonical-encoding version 并引用 `corpus_payload_sha256`，
   `registration_sha256` 再哈希 registration；
4. final manifest envelope 引用两者，`manifest_sha256` 单独覆盖该 envelope，并排除自身；
5. result filename 使用 `corpus_id`，结果内容同时引用 `manifest_sha256`。

### 4.2 极简 filesystem CAS

不提交完整上游 Git 历史。accepted before/after query-complete closure、所有候选 patch
以及 qualification 所需的最小 engine 文档/代码 evidence 写入极简内容寻址目录：

```text
scenarios/flare_corpus/
  NOTICE
  LICENSE.flare-game
  LICENSE.flare-engine
  candidate-ledger.json
  qualification-rules.json
  matcher-contract.json
  scoring-protocol.json
  evaluation-registration.json
  manifest.json
  results/<corpus-id>-first-evaluation.json
  blobs/<sha256>
```

相同文件跨 group/split 复用一个 blob。CAS 不提供数据库、引用计数、网络接口或 GC；
它只是由 manifest 映射驱动的只读 filesystem layout。NOTICE 记录仓库、许可证、commit、
engine revision 和提取方法。B0 同时报告原始/去重字节数；若实际没有跨 group 复用，
实现计划应退回更可读的 per-group 目录，但 manifest 的 path/hash 契约保持不变。

ledger、rules、registration、manifest、patch 和 input blobs 全部公开；这与 D5 的非盲口径
一致。first-evaluation result 按 corpus ID 新增，不允许覆盖旧 corpus/version 的原始结果。

## 5. Mining、adjudication 与 evaluation harness

核心命令使用本地 clone，正式 runner 不联网：

```text
python -m gameforge.bench.flare_mining discover --repo <local-clone> --search-spec <json>
python -m gameforge.bench.flare_mining adjudicate --ledger <json> --evidence <json>
python -m gameforge.bench.flare_mining probe --ledger <json> --repo <local-clone>
python -m gameforge.bench.flare_mining freeze --ledger <json> --repo <local-clone>
python -m gameforge.bench.flare_external --manifest scenarios/flare_corpus/manifest.json
```

- `discover` 只做客观 path/message/diff/patch-id 筛选，不自动赋予 ground truth；
- `adjudicate` 从显式离线 evidence file 读取 issue/PR 与人工 lineage 决策，并校验 search
  frame、config-only、证据和三维 applicability；
- `probe` 生成 B0 feasibility report 与 case-driven semantic requirements；
- `freeze` 在 Corpus Freeze Gate 验证 Git objects、path 安全、blob/tree/input hashes、
  qualification replay、engine evidence、matcher/scoring contracts 和许可证，然后生成
  manifest 与 evaluation registration；生产 reader 的支持情况不参与资格筛选；
- `flare_external` 是零网络、确定性的正式 runner；只有 registration Git commit 是当前
  checker/profile freeze revision 的祖先、worktree clean 且所有 hash 匹配时，结果才可标
  `registered_first_evaluation`，否则只能标 `regression` 或 `invalid_provenance`。

所有 Git 调用使用参数数组和只读子命令，不接受任意 shell 片段。harness 本身不访问
GitHub；网络失败只影响用户自行准备 local clone/evidence，不影响 committed corpus、测试
和 benchmark。

## 6. Checker、matcher 与优化规则

每个参与真实语料的 checker 必须统一填充可匹配 finding contract：

- `defect_class`；
- 稳定 `entities` 与适用时的 `relations`；
- `minimal_repro.source_ref`；
- 逐类 matcher 所需的稳定 evidence keys；
- `producer_id` 与 `producer_run_id`。

reader/profile/checker/matcher/tool revisions 放在 run/report envelope 的 version tuple；Finding
通过 `producer_run_id` 引用该 envelope，不为此 bump `finding@1` 或在每条 finding 冗余
版本字段。

scorer 只调用注册的 per-class matcher，并同时核对 target fingerprint、before/after
locator 与 finding identity。单纯同类别、同文件或同 block 的无关 finding 不算命中。

若 development 效果不好，按以下顺序定位：

1. corpus qualification、locator 或 causal witness 错误；
2. query-complete closure 或 dialect 证据错误；
3. reader/adapter 丢失语义或引用；
4. Flare profile 与可审计规则不符；
5. checker 存在可泛化的 soundness/coverage 缺口；
6. case 实际超出静态可判定范围。

只允许基于格式/engine 证据、通用图/状态语义和 development cases 修复 1-5。每次优化
必须新增反例、reference-content finding 裁决和跨 adapter regression test。第 6 项只有
在独立 qualification 证据证明原 ground truth 无效时才能移除 case；生产 reader/checker
尚未实现某语义不是 `static_limit`，而是保留在分母的 execution failure。冻结后的 ground
truth 修订必须遵守 D3 的新 corpus/新 revision-window 规则。

禁止按 SHA、具体路径、实体名特判；禁止删除失败、事后切换 applicability、改变冻结
分母或用 conditional metric 替代端到端指标。

## 7. ExternalReport 与 reference-content 信号

`ExternalReport` 新增显式 `external_schema_version = "external@2"`，并加入：

- corpus、ledger、registration、tool、reader、checker、matcher 与 source revisions；
- candidate fix-group counts，以及 proposed/qualified/accepted/rejected/ambiguous unit counts；
- 三维 applicability matrix；
- development/evaluation 的 per-class unit metrics 与 corpus-conditional CI；
- full paired outcome、execution failure、target 级诊断与 provenance；
- under-powered、selection-recall-unknown、non-blind 和 post-evaluation regression 标记；
- reference-content finding adjudications。

“current Flare clean snapshot”改称 **reference snapshot**。它在 evaluation registration
之前显式 pin 到一个 development after tree，不能从 evaluation 中选择；这样避免额外
承诺导入整个当前 Flare，也不会把 evaluation 内容带回 development 优化。每个 emitted
finding 的人工裁决为：

```text
true_defect | false_positive | adapter_artifact | unproven | not_reviewed
```

`not_reviewed` 只允许作为中间状态。报告同时显示 review coverage；只有
`not_reviewed == 0` 且 `unproven == 0` 时才给 end-to-end flagged precision point estimate，
其中 `false_positive` 与 `adapter_artifact` 都不是 TP，但另行分层。否则只显示已审子集与
按未决项全 TP/全非 TP 计算的最宽上下界。没有标注所有 non-findings，所以不能称为
FPR。无关 finding 不因出现在修复后 tree 就自动算 FP。

M3c 静态 HTML 面板同步展示上述数据，继续保持无 JS、自包含和 HTML escaping。它是
pre-M4 验收视图；完整筛选、审批、diff 导航和运行操作仍属于 M4。

## 8. 测试策略

### 8.1 B0 ledger、资格与冻结

- search frame、候选排序和 discover 输出确定性；
- 客观 patch-id/cherry-pick/revert 信号确定性，人工 root-cause grouping 证据完整；
- 多 commit group 是连续 first-parent config-only range，merge parent edge 明确；
- 非 config-only group 必须拒绝；
- 11 类三维 matrix 缺项必须拒绝；
- qualification registry hash、逐类 witness schema 和独立 replay validator；
- engine evidence blob/revision/excerpt/license 可离线复核；
- matcher contract 与 scoring protocol hashes 进入 corpus/registration 且实际 runner 强校验；
- before witness、after witness、causal hunk、双侧 locator 或双人裁决缺失必须拒绝；
- rejected/ambiguous patch evidence 可离线复核；
- split 按 group 和时间窗口确定性，任何 lineage 不跨 split；未满足 strata 可见；
- registration Git commit 必须早于 checker/profile freeze revision，dirty worktree 拒绝首次评估。

### 8.2 Storage 与输入安全

- blob SHA-256、content tree、closure manifest、input fingerprint、Git OID 和 manifest round-trip；
- mod stack、load order、roots 与 path-to-blob mapping 可重建两侧输入；
- 绝对路径、`..`、symlink、非 regular file、大小写冲突和非 UTF-8 输入拒绝；
- NOTICE、许可证、engine dialect/revision 和 attribution 完整；
- 两次 freeze 产物字节一致。

### 8.3 Reader、adapter 与 semantic slice

- 现有 line-level property round-trip 继续成立；
- physical row、duplicate-key occurrence 和 relation source locator 准确；
- development cases 与通用 feature fixtures 所需的 include/append/override 与历史 dialect；
- query-complete definition/producer index 和相关 unknown syntax fail closed；
- semantic addendum 的 accepted feature matrix 逐项有正例、反例和 engine evidence；
- 若相应 feature 被接受，覆盖负 guard 极性、unset effect、初始/外部 status、LFP 与阻断 SCC；
- Flare 专属 diagnostics/coverage envelope；
- relevant-unknown fail-closed、locator 与 diagnostics contract 永远是硬门禁；
- `ir-core@1` 的 `DROPS_FROM source -> produced object` 跨系统 contract tests。

### 8.4 Matcher 与 scorer

- 精确 target 命中，同类无关 finding 不得冒充；
- target 删除、改名、移动和重复 key occurrence；
- 完整 2x2 paired truth table 与 `execution_failure`；
- scoring protocol 固定 unit aggregation、错误映射、CI、headline 和 capability states；
- adapter/matcher/checker failure 仍在冻结分母；
- 多 targets 不增加 Wilson n，任一 target 失败则 unit 失败；
- per-class、per-split 指标隔离，不生成混合 headline；
- 同 corpus 调标签/分母被拒绝，corpus bump 后旧报告仍可追溯。

### 8.5 端到端与全量门禁

- committed manifest+blobs 零网络生成完整 report；
- registered first evaluation 固定所有 revision 与 report hash，旧结果不可覆盖；
- 相同输入连续两次 JSON 字节一致，后续运行明确标为 regression；
- static HTML 正确呈现 per-class、split、CI、failure、N/A 和功效警告；
- reference finding adjudication 不被误称 FPR；
- 全量 pytest、7 import-linter contracts、Ruff、M0-M2 验收与 M3a-c regression 无回归；
- `DROPS_FROM` 契约修复后 repair cassettes 统一重录并通过稳定性审计。

## 9. 阶段、验收与 M4 进入条件

本节定义 M3d 外部效度工作的退出条件，是进入 M4 的必要条件而非全部充分条件。M3d
之外仍须按 PRD §16 闭合 narrative BDR、Human-Edit-Distance 与 QA 工时节省，并修复已
确认的 repair cassette/apply 并发语义债务；未完成时不得仅凭 M3d `validated` 进入 M4。

### Phase B0A：证据账本

- 冻结 search frame、候选 universe 和 candidate ledger；
- 完成 fix lineage 聚类、config-only 审计和 11 类三维矩阵；
- 达到或否定 8 independent groups / 4 proposed classes 的 provisional gate。

### Phase B0B：可行性探针

- 对全部拟接受候选完成物理语法扫描；
- 每个 unit 完成 raw qualification/witness/closure-requirement 审计；
- 每类代表、最老 dialect 和按冻结 complexity tuple 选出的最高风险 closure 完成 bounded
  semantic probe；
- 产出带哈希的 qualification registry、semantic-slice addendum、target matcher contract、
  scoring protocol、qualified-candidate set 和预注册 temporal split；
- negative 时输出 `insufficient_evidence` 并停止。

### Corpus Freeze Gate：逐 unit 冻结

- 从 raw Git objects 冻结全部 qualified candidates 的 before/after InputTrees，不运行生产
  reader 或被测 checker；
- 逐 unit 重放独立 qualification，并验证 lineage、config-only、witness、locator、dialect
  evidence、query-complete raw closure requirements、engine evidence、matcher/scoring contracts；
- 只有上述独立 ground truth/input 证据无效的 unit 才能在冻结前拒绝；reader、coverage、
  matcher/checker 尚不支持的 unit 不得删除。任何拒绝都要重新检查 8 groups / 4 classes
  投资门；
- 对保留 groups 重新验证 evaluation 至少 25%、至少 2 groups、registration strata，以及
  至少两个类别在 development/evaluation 两侧都有独立 group；禁止重新分配。失败时不得
  生成 registration 或运行 checker，只能按原 search frame 补充候选并创建新的 provisional
  corpus version，或输出 `insufficient_evidence`；
- 全部通过后生成 final accepted manifest、`evaluation-registration.json` 和冻结主分母。

### Phase M3d-1：reader、IR 与 profile

- 全部 accepted raw InputTrees 可零网络重建；开发期只对 development split 运行生产
  reader，temporal evaluation 在首次 registered run 前不执行；
- physical round-trip、diagnostics/coverage 和 development semantic golden graph 通过；
- Flare profile、accepted proof rules 与 target matcher implementation 符合已冻结 contract，
  未知相关语义 fail closed；
- 完成 `ir-core@1` `DROPS_FROM` 方向契约修复和跨系统审计。

### Phase M3d-2：checker、matcher 与 development 优化

- 所有 development units 产生完整 paired outcome；
- 通用 soundness/coverage 修复有反例与 reference-content 回归；
- denominator、labels、locators 和 matcher contract 已在 Corpus Freeze Gate 冻结；实现只能
  修到符合该 contract，改变 contract 必须创建新 provisional corpus/registration version；
- development 优化完成后冻结 checker/profile/matcher code revisions，供首次 evaluation。

### Phase M3d-3：registered temporal evaluation

- 冻结代码在 clean worktree 上首次运行预注册 evaluation split；
- 验证 registration commit ancestry，保存全版本 tuple、原始 report hash 和逐 unit 结果；
- evaluation failure 可驱动产品修复，但原 cohort 后续结果只记 regression；新的外部验证
  需要不重叠的后续 revision window。

### Phase M3d-4：报告与全量验收

- `BenchReport.external` 与静态 Eval 面板消费稳定契约；
- reference snapshot emitted findings 已结构化裁决且 `not_reviewed == 0`；
- benchmark 零网络、确定性、可复现；
- 全量质量门禁和 cassette 稳定性审计通过；
- CLAUDE/README/计划索引记录真实结论与限制。

只有以下条件同时满足，才把 M3d 外部效度轨标为 `validated`：

1. B0 positive，Corpus Freeze Gate 后仍达到 8 independent groups / 4 classes；
2. 至少两个类别在 development/evaluation 各有独立 group；其余类别只按实际证据标为
   development-only 或 qualitative，不借用 4-class 投资门宣称 evaluation 覆盖；
3. registered first evaluation 已生成且所有 units 留在冻结分母；当前 release 的
   regression run 中 execution failures 为零。这个工程门不要求不可覆盖的首次报告事后
   变成零；若首次评估出现执行故障，原始失败永久保留，受影响类在新 cohort 成功前必须
   标为 `evaluation_infrastructure_failed`，M3d 只能以该能力降级状态完成外部效度轨；
4. registered first evaluation 后没有发现 label/rule/witness/locator/eligibility ground-truth
   错误；一旦发现，该 corpus 只能产出 `post_hoc_corrected_analysis`，M3d 在不重叠 revision
   window 的新预注册 corpus 通过前不能标为 `validated`；
5. 每个 `missed/persists/unexpected_after` 都有结构化 root-cause triage。development 中
   可泛化且可修复的缺口必须在首次评估前修复；evaluation 暴露的同类缺口可以修产品，
   但首次结果原样保留。detector 负结果不使 benchmark 工程失效，而是把对应 capability
   claim 限定为 `not externally validated`；该限制必须同步写入 ExternalReport、静态
   Eval、README 和 CLAUDE；
6. reference review coverage 为 100%，所有文档、测试与 provenance 门禁通过。

完成上述条件仍不能单独进入 M4；还必须满足本节开头列出的 PRD §16 其余 M3 指标、
无条件 `DROPS_FROM` 契约修复以及 repair cassette/apply 并发语义前置门。

`validated` 表示“真实语料资格与评估过程成立”，不表示小样本证明了总体效果，也不把
某类的 detector miss 伪装成成功。per-class capability state、结果和宽 CI 必须原样展示。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 真实 bug 多但 taxonomy 覆盖少 | B0 严格资格与负出口；不扩 taxonomy 凑数 |
| 人工搜索形成 selection bias | 冻结 search frame；只作 corpus-conditional 描述 |
| commit/hunk 膨胀样本量 | 按独立 fix group/class 计分；targets 只作嵌套明细 |
| 事后排除失败抬高指标 | 首次运行前冻结分母；execution failure 计失败 |
| 状态图缺少上下文导致误判 | case-driven proof slice；context scope；unknown -> unproven |
| 证明不存在 producer 时闭包不完整 | query-complete reverse producer/definition index |
| non-blind evaluation 认知泄漏 | Git 预注册 + 禁止特判 + 首次结果保留；报告显式声明非盲限制 |
| 历史 dialect 演进 | 记录 engine revision/evidence；相关未知语义 fail closed |
| `DROPS_FROM` 修复改变 snapshot/cassette | 明确 `ir-core@1` 契约纠正；最后统一重录与审计 |
| fixture 体积膨胀 | 极简 filesystem CAS 去重，不引入服务或数据库 |
| reference tree 仍含潜伏缺陷 | finding 逐项裁决，不称 clean 或 FPR |
| M4 固化临时 external contract | M3d 先稳定 corpus/report schema，再进入 M4 |

## 11. 明确延后到 M4 的消费面

M3d 产出的 ledger、manifest、registration、paired results、per-class metrics 和 provenance
是 M4 的输入。M4 负责：

- 把 corpus/bench run 记录为平台级版本血缘工件；
- 通过 API 暴露运行、样本、diff 和 trace；
- 在完整 Eval 页面提供筛选、对比和血缘导航；
- 对 corpus 更新与人工裁决实施 RBAC/maker-checker；
- 记录成本、延迟和端到端 observability。

这些消费面在 M3d 中只定义稳定 JSON 与离线静态验收视图，不提前建设平台能力。
