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
- `2026-07-07-m2b-1-playtest-core.md` — M2b-1（Playtest agent 核心：状态抽象 + planner/executor + verifier-grounding + 反思 + 主循环 + 回归 harness + 消融机制；REPLAY/scripted 冒烟，零实网 LLM；已完成。≥20 链生成器与真实录制 = M2b-1b，记忆消融 = M2b-2）
