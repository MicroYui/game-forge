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
- `2026-07-11-pre-m4-external-evidence-b0a.md` — pre-M4 外部证据通用化 + Endless Sky B0A（🔄 进行中；只覆盖 generic harness、Flare 冻结兼容与 B0A，不跨 gate 预写 Adapter/B0B）

M3 umbrella 仍未完成。Flare B0B、Corpus Freeze 与 M3d-1..4 均未进入；不得继续写
Flare reader/quest/loot-table 实现计划或进行第三轮搜索。下一份计划只能选择新的外部
真实语料源，或在取得书面 PRD scope waiver 后记录范围决策。narrative BDR、
Human-Edit-Distance、QA-hours、`DROPS_FROM` 与 repair cassette/apply 仍须分别规划。
Flare B0A 的 negative investment decision 不改变里程碑门禁：M3 仍未完成；M4 未开始，
且仍受上述 pre-M4 gates 阻塞。
M3b/M3c 当时没有独立历史计划文件，不事后补写虚构计划。
