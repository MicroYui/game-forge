# 里程碑实现计划（Plans）

每个里程碑的详细实现计划放这里，命名 `M0a-<topic>.md`、`M0b-...` 等。

**执行约定**：
- 计划由 `writing-plans` skill 产出；执行时以计划为准（`executing-plans` / `subagent-driven-development`）。
- 动代码前先读根目录 `CLAUDE.md` → PRD → 地基契约 → 本目录当前里程碑计划。
- 全程 TDD；里程碑完成后更新 `CLAUDE.md` 里程碑状态表。

**当前计划文件**：
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
- `2026-07-11-pre-m4-external-evidence-b0a.md` — pre-M4 外部证据通用化 + Endless Sky B0A（🔄 进行中；generic harness 与 Flare 冻结兼容已完成；两轮代码审查共发现并修复 generic direct-match replay、递归外部 revert lineage，以及 selected revert 与等价 lineage 组件 disposition 冲突三个缺陷；已从 registration anchor `687f36fb6ab499d3667fe43429fec4a25132c97a` 重新冻结并在两个独立临时目录逐字节回放 first-80 discovery：610 matched / 562 config-only，universe `f22981b17b43e02caaa494193e6a4b8cd92bbc0c312f9d5f1db249da7365793f`；当前派生状态为 `awaiting_human_evidence`，没有最终 ledger/decision，未跨 gate 进入 Adapter/B0B）
- `2026-07-12-pre-m4-core-corrections.md` — pre-M4 核心契约修正（✅ 已完成；`5fdfb32`、`f403a5c`、`35330e8`、`5adaab0`、`586b579`、`cc0fbc4`：Patch exact-base/fail-closed、`DROPS_FROM` producer→product、稳定 repair request/Patch identity、`gpt-5.6-sol` 当前证据与 Opus 历史证据分流、bench clean base 跨 checkout 稳定；双 REPLAY 10/10；962 passed、1 skipped，7 契约 kept）

M3 umbrella 仍未完成。Flare B0B、Corpus Freeze 与 M3d-1..4 均未进入；不得继续写
Flare reader/quest/loot-table 实现计划或进行第三轮搜索。Endless Sky 只有在重新冻结的完整
80 行 payload 获得独立真人 hash attestation 且 B0A gate 为 `pass` 后，才可单独规划
B0B/Adapter。narrative BDR、
Human-Edit-Distance、QA-hours 与 BenchReport v2 仍须分别规划。`DROPS_FROM` 与 repair
cassette/apply 债务已由 `2026-07-12-pre-m4-core-corrections.md` 闭合。
Flare 的 negative investment decision 和 Endless Sky 的未审批分析均不改变里程碑门禁：
M3 仍未完成；M4 未开始，且仍受上述 pre-M4 gates 阻塞。
M3b/M3c 当时没有独立历史计划文件，不事后补写虚构计划。
