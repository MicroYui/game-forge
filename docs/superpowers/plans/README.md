# 里程碑实现计划（Plans）

每个里程碑的详细实现计划放这里，命名 `M0a-<topic>.md`、`M0b-...` 等。

**执行约定**：
- 计划由 `writing-plans` skill 产出；执行时以计划为准（`executing-plans` / `subagent-driven-development`）。
- 动代码前先读根目录 `CLAUDE.md` → PRD → 地基契约 → 本目录当前里程碑计划。
- 全程 TDD；里程碑完成后更新 `CLAUDE.md` 里程碑状态表。

**当前计划文件**：
- `2026-07-14-m4c-api-worker.md` — M4c API 网关与持久 worker（🔄 实现中；按 19 个顺序 TDD Task 执行）
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
- `2026-07-12-pre-m4-human-evidence.md` — HED + QA-hours（HED 与 QA 协议/工具 ✅ 已完成；8 个真人 QA sessions / 4 对 matched pairs 由产品负责人明确延后，保持 `qa.evidence_missing`，不得伪造）
- `2026-07-12-pre-m4-cost-latency-report-v2.md` — Cost/Latency + BenchReport v2 + combined acceptance（✅ 非真人范围完成；6 个 Agent workload、903 个确定性 runtime 样本、JSON/text/static HTML 同模型视图与完整非真人 audit 全绿；combined acceptance 唯一失败为延后的 `qa.evidence_missing`）

所有非真人 pre-M4 工作与最终 audit 已完成。M3 umbrella 现在只剩产品负责人明确延后的
真人 QA：8 个 participant sessions / 4 对 matched pairs；在导入前，报告与 combined acceptance
必须继续只显示 `qa.evidence_missing`，不得填零或虚构。`2026-07-12-pre-m4-lean-closure-design.md`
已替代旧计划中的额外人工 hash attestation 门禁：上游 bug-fix commit/PR 提供真人 provenance，
固定 before/after predicate、native parser 与通用 checker 直接完成 qualification。

Flare B0A 与通用 B0A 状态机冻结为历史 replay 资产，不再扩展；Flare B0B、Corpus Freeze 与
M3d-1..4 均未进入，也不得继续写 Flare reader/quest/loot-table 计划或进行第三轮搜索。Flare 的
negative investment decision 保持有效；Endless Sky 使用已完成的精简 external-case 路径。
`DROPS_FROM` 与 repair cassette/apply 债务已闭合。M3 工程实现已完成；真人 QA 由产品负责人延后，
combined acceptance 继续如实保留 `qa.evidence_missing`，但不阻塞 M4。M4 完整设计与
`M4a → M4b → M4c → M4d → M4e` 实施顺序已冻结；M4a/M4b 已完成，当前实施 M4c。
M3b/M3c 当时没有独立历史计划文件，不事后补写虚构计划。
