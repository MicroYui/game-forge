# 里程碑实现计划（Plans）

每个里程碑的详细实现计划放这里，命名 `M0a-<topic>.md`、`M0b-...` 等。

**执行约定**：
- 计划由 `writing-plans` skill 产出；执行时以计划为准（`executing-plans` / `subagent-driven-development`）。
- 动代码前先读根目录 `CLAUDE.md` → PRD → 地基契约 → 本目录当前里程碑计划。
- 全程 TDD；里程碑完成后更新 `CLAUDE.md` 里程碑状态表。

**当前计划文件**：
- `2026-07-19-m4d-web-console.md` — M4d Web Console（✅ Task 1–19、V1–V3 与 D2 已完成：产品负责人选择并最终批准 A — Editorial，70 张视觉证据、21 项无障碍门禁、隔离 QA Runner 可操作状态、真实 Journey A/B 与 constraint publication 均已闭合；最终门禁为前端 67 files / 614 tests、E2E 5、visual 70、a11y 21、npm audit 0，OpenAPI/streaming 78、M4c E2E 31、non-Bench 4974 passed/1 skipped、Bench 790、依赖 27、导入契约 7/7、exact 77 operations / 4 artifacts。后续 `participant-04` 八场/四对真人证据已完整导入并通过 combined acceptance；M4 仍在进行中，M4e production/DR adapters 待推进）
- `2026-07-14-m4c-api-worker.md` — M4c API 网关与持久 worker（✅ 已完成；Task 1–19 全闭合，focused 2316 passed、non-Bench 4766 passed/1 skipped、Bench 761 passed、迁移 35 passed、OpenAPI/streaming 73 passed、历史 cassette 12 passed，7 契约 kept）
- `2026-07-14-m4b-observability-cost-reliability.md` — M4b 可观测、成本治理与可靠性（✅ 已完成；focused 290 passed/1 skipped，修复后全仓分区 2756 passed/1 skipped，7 契约 kept；下一片按冻结顺序进入 M4c）
- `2026-07-13-m4a-platform-core.md` — M4a 平台核心与持久化地基（✅ 已完成；`b3c40f74`；slice 1230 passed、全仓 2509 passed/1 skipped、7 契约 kept；下一片按冻结顺序进入 M4b）
- `M0a-vertical-slice.md` — M0a（已完成）
- `2026-07-06-m0b-foundations.md` — M0b（已完成）
- `2026-07-06-m1-deterministic-trunk.md` — M1（已完成）
- `2026-07-07-m2a-agent-foundations.md` — M2a-part1（地基：契约§7 Model Router/Cassette + 编排 harness + 录放复现验收，零实网 LLM；已完成）
- `2026-07-07-m2a-part2-agents.md` — M2a-part2（5 个有边界 agent 角色 + 生成门禁 + verifier-guided repair；已完成）
- `2026-07-07-m2b-1-playtest-core.md` — M2b-1（Playtest agent 核心：状态抽象 + planner/executor + verifier-grounding + 反思 + 主循环 + 回归 harness + 消融机制；REPLAY/scripted 冒烟，零实网 LLM；已完成。≥20 链生成器与真实录制 = M2b-1b，记忆消融 = M2b-2）
- `2026-07-08-m2b-2-memory.md` — M2b-2（MemTrace 记忆、消融与 consistency quorum；已完成）
- `2026-07-10-m3a-bench-metrics.md` — M3a（taxonomy、seeded corpus、指标与 BenchReport 契约；已完成）
- `2026-07-10-economy-sink-adapter.md` — pre-M4 economy sink 增量（已完成）
- `2026-07-10-m3d-b0a-evidence-ledger.md` — M3d B0A（已完成执行，但投资 gate 为终态负结果：526 candidates、7/8 proposed groups、10 proposed cases、4/4 classes、0 qualified/accepted，`insufficient_evidence`）
- `2026-07-11-pre-m4-external-evidence-b0a.md` — pre-M4 外部证据通用化 + Endless Sky B0A（历史冻结、不再推进；generic harness 与 Flare 冻结兼容已完成；两轮代码审查共发现并修复 generic direct-match replay、递归外部 revert lineage，以及 selected revert 与等价 lineage 组件 disposition 冲突三个缺陷；已从 registration anchor `687f36fb6ab499d3667fe43429fec4a25132c97a` 重新冻结并在两个独立临时目录逐字节回放 first-80 discovery：610 matched / 562 config-only，universe `f22981b17b43e02caaa494193e6a4b8cd92bbc0c312f9d5f1db249da7365793f`；终态派生状态为 `awaiting_human_evidence`，没有最终 ledger/decision，未跨 gate 进入 Adapter/B0B）
- `2026-07-12-pre-m4-core-corrections.md` — pre-M4 核心契约修正（✅ 已完成；`5fdfb32`、`f403a5c`、`35330e8`、`5adaab0`、`586b579`、`cc0fbc4`：Patch exact-base/fail-closed、`DROPS_FROM` producer→product、稳定 repair request/Patch identity、`gpt-5.6-sol` 当前证据与 Opus 历史证据分流、bench clean base 跨 checkout 稳定；双 REPLAY 10/10；962 passed、1 skipped，7 契约 kept）
- `../specs/2026-07-12-pre-m4-lean-closure-design.md` — pre-M4 剩余产品证据的精简设计：上游真人 provenance + 确定性 qualification，取消额外人工 hash 审批仪式；当前在线 Agent 证据使用 `gpt-5.6-sol`，历史 Opus 证据保持原样
- `2026-07-12-pre-m4-external-cases-adapter.md` — 8 个固定 Endless Sky 真实 before/after case + lossless reader/Adapter + 通用 checker qualification（✅ 已完成；8/8 qualified，verification 四类各 1/1，after oracle-FP 0/8；16 棵 source tree 的 reader/Adapter/native witness 与两次离线 evidence replay 逐字节一致；manifest SHA-256 `8a6bec74c87af36a1ddf1b592422f24eafb699315c76fc8c529821c70bec182a`；1076 passed、1 skipped，7 契约 kept）
- `2026-07-12-pre-m4-narrative-evidence.md` — 4 类 narrative seeded-oracle 证据（✅ 已完成；验证集 1905 个请求轨迹，当前证据固定为 `openai/gpt-5.6-sol/pre-m4@1`；逐字节 REPLAY、功效与依赖边界全绿）
- `2026-07-12-pre-m4-human-evidence.md` — HED + QA-hours（✅ 完成；唯一 accepted/measured 的 `participant-04` 完成 8 个 protocol-valid sessions / 4 个 matched pairs，manual 0/4、assisted 3/4；权威 `qa-evidence@2` 结论 `savings`；旧工作区仅审计且不计入分母或得分）
- `2026-07-12-pre-m4-cost-latency-report-v2.md` — Cost/Latency + BenchReport v2 + combined acceptance（✅ 完成；6 个 Agent workload、903 个确定性 runtime 样本、JSON/text/static HTML 同模型视图，QA 绑定 `participant-04` 冻结协议与证据；combined acceptance 返回 `[]`）

所有 pre-M4 工作与最终 audit 均已完成。权威 QA 目录只接受
`participant-04` 的 8-session / 4-pair 证据：八场均 protocol-valid，manual 0/4、assisted
3/4；权威 `qa-evidence@2` 的 `evidence_sha256` 为
`e7e76d9a846efd7eeaae2b06641e170c15878f7cbf1ff98a79a733b1aa451142`，结论为 `savings`，
mean 3.407599574483333 分钟、median 4.203912946883333 分钟、95% CI
[1.2129956309041665, 5.037277463891666]。最终验收前发现旧聚合实现偏离已批准设计 §11
`QA timeout/incorrect | cap time + success=false`；修复后正确 session 使用 actual capped active，
错误/超时使用 8 分钟，monotonic events 与 raw active 未改写且仅作审计，不冒充重写 session。
canonical import、patch replay、确定性 verdict 重验、报告三视图和 combined acceptance 全绿；
`qa.evidence_missing` 已闭合。更早的拒收尝试和
launcher/infrastructure preflight 仅保留审计事实，不进入 observation、denominator、score 或
BenchReport，因此 `participant-04` 是唯一 accepted/measured 正式实验。

`2026-07-12-pre-m4-lean-closure-design.md` 已替代旧计划中的额外人工 hash attestation 门禁：
上游 bug-fix commit/PR 提供真人 provenance，固定 before/after predicate、native parser 与通用
checker 直接完成 qualification。

历史治理阶段的准确状态是“M3 工程实现已完成，`qa.evidence_missing` 不阻塞 M4”；
`participant-04` 后来闭合的是该独立真人证据缺口，不改写当时的治理决定。

Flare B0A 与通用 B0A 状态机冻结为历史 replay 资产，不再扩展；Flare B0B、Corpus Freeze 与
M3d-1..4 均未进入，也不得继续写 Flare reader/quest/loot-table 计划或进行第三轮搜索。Flare 的
negative investment decision 保持有效；Endless Sky 使用已完成的精简 external-case 路径。
`DROPS_FROM` 与 repair cassette/apply 债务已闭合。M3 产品与证据验收均已完成，combined
acceptance 无失败。M4 完整设计与
`M4a → M4b → M4c → M4d → M4e` 实施顺序已冻结；M4a/M4b/M4c/M4d 已完成，M4d Task 1–19、V1–V3 与 D2 全部闭合，产品负责人已选择并最终批准 A — Editorial，八页 70 张视觉证据、WCAG/键盘门禁、隔离 QA Runner 全状态、真实双身份 Journey A/B 与 constraint publication 均已通过。M4 仍在进行中，下一工程切片是 M4e production/DR adapters。后续真人 QA 是独立 M3 证据闭环，不改写 M4d 当时的工程门禁。
M3b/M3c 当时没有独立历史计划文件，不事后补写虚构计划。
