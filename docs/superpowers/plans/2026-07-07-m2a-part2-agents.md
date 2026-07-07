# M2a-part2 — 有边界 Agent 语义 + verifier-guided 修复搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **实现子 agent 用 sonnet5。**

**Goal:** 在 part1 的确定性 LLM 地基（ModelRouter/Cassette/编排 + opus messages transport）之上，落地 6 个有边界 agent 的**真实 LLM 语义**（Extraction Proposer / Defect Triager / Repair Drafter / Consistency Assistant / Content Generator + 生成门禁）与 **verifier-guided 修复搜索**（propose→verify→refine），并通过 M2 §16 part2 全部验收：**Fix Pass Rate ≥ 70%**、同输入 REPLAY 可复现、确定性/llm-assisted 严格分区、**记忆消融外**的修复搜索效率报告。**不简化只延后、质量优先。**

**Architecture:** 每个 agent 是 `agents/` 下的 `AgentNode`（part1 Protocol），只经 `ModelRouter`（opus via `AnthropicMessagesTransport`）调 LLM，产出 `contracts/agent_io.py` 的 typed 输出 + `AgentNodeResult`（含 `request_hashes`/`fallback_taken`）。**每个 LLM 输出必有确定性预言机或人工兜底**：Extraction→约束可编译校验+人审队列；Triager→不改判确定性结论（只聚类）；Repair→spine 检查器(Clingo/z3)+经济仿真+Aureus 回归充当验证器；Consistency→llm-assisted 严格分区+quorum；Generator→检查器+仿真门禁。修复搜索的对/错**永远由确定性验证器给**，LLM 只提议。CI/测试 **REPLAY 零实网**；`Fix Pass Rate` 由一次**真实 opus 录制 pass** 生成的 cassette 在 REPLAY 下测得。

**Tech Stack:** Python 3.12 (uv), pydantic v2, stdlib `json`/`re`, part1 的 `ModelRouter`/`AnthropicMessagesTransport`/`CassetteStore`/`run_graph`/prompt registry, spine 的 `compile_all`/`build_review_report`/`apply_patch`/`dry_run`/`EconomySimulator`/`snapshot_to_world`+`AureusEnv`, pytest。

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 + 地基契约 + M2 设计 §6。每个 task 隐含包含本节。

- **不简化，只延后**：6 个 agent 角色全部实现真实语义（Playtest 除外——其 I/O 已在 part1 定，实现属 M2b）。修复搜索是真·多轮 propose→verify→refine（非一次性 diff）。
- **确定性优先 / verifier-grounding**：修复/生成的对错由 spine 检查器（Graph/ASP-Clingo/SMT-z3）+ 经济仿真 + Aureus 回归给出，**绝不 LLM 自评**。solver `unproven` ≠ pass。
- **依赖方向（CI 强制，7 契约不破）**：`agents → {contracts, spine, env, game, runtime}`；LLM 只经 `runtime.model_router`（agents 不直连 SDK/HTTP）；`spine` 永不碰 LLM。
- **可复现只承诺回放**：所有 agent 在同 `(input, model_snapshot)` + REPLAY 下逐位复现；`Fix Pass Rate` 在 REPLAY 下可重复测得同值。CI/pytest **零实网调用**（真实调用仅在 `GAMEFORGE_LLM_LIVE=1` 的显式录制/门控测试）。
- **确定性 vs llm-assisted 严格分区（契约6）**：Consistency Assistant 产 `oracle_type="llm-assisted"` Finding，经 `ReviewReport.partition` 只落 `llm_assisted_findings`，绝不进确定性桶/统计。
- **模型**：`ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag=...)`（决策 D，opus 全程质量优先；opus 仅 `/v1/messages` 可达，用 `AnthropicMessagesTransport`）。
- **密钥**：`GAMEFORGE_LLM_KEY` 从 gitignored `.env` 读（`runtime.secrets.env.get_llm_key`）。cassette 入库、CI 只回放。
- **Git**：commit 无 AI 署名 / "Generated with"。分支 **`m2a-part2`**（已含 opus transport `bbd36fb`；part2 完成后连同 transport 一起 review + 并入 master）。
- **Package**：`gameforge.<pkg>`；短形式按前缀读。

## 关键决策（best-judgment；可回改）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| P2-D1 | agent 输出协议 | **LLM 返回 JSON（可含 ``` 围栏/前后散文），用 `raw_decode` 抽首个 JSON 值**；解析失败→兜底（fallback_taken=True，空/人审输出） | 稳健、可确定性解析、对 opus 友好 |
| P2-D2 | 修复搜索验证器 | **spine 检查器(全套)必跑 + 经济仿真(有经济实体时) + Aureus 回归(patched snapshot 能建 world 时 best-effort)**；目标 Finding 消解且**不新增 deterministic Finding**才算 verify 通过 | 用尽所有确定性预言机，verifier-grounding |
| P2-D3 | 修复搜索预算 | **默认 max_steps=4 轮**；每轮失败把反例（新 Finding/拒绝原因/回归失败）回灌 prompt | 收敛 vs 成本平衡；报搜索效率 |
| P2-D4 | Fix Pass Rate 语料 | **`scenarios/defects/` 的 9 个可修复缺陷场景**（structural+numeric）；"pass"=搜索在预算内产出通过 verify(+regression) 的 patch | 复用 M1 语料，缺陷类分布真实 |
| P2-D5 | Consistency quorum | **同 prompt 采样 N=3（温度略升，seed 化到 prompt_version 变体），多数一致的 hint 保留**；每次采样是独立 request_hash → cassette | perspective-diverse 的基础形态；M2b 上对抗辩论 |
| P2-D6 | 录制 pass | **`GAMEFORGE_LLM_LIVE=1` 下跑一次 RECORD**，写 cassette 入 `cassettes/`；`Fix Pass Rate<70%` 则迭代 prompt/预算并重录（**调 agent，不调阈值**） | 质量优先、不简化 |

---

## Repo layout delta

```
gameforge/agents/
  base.py                 # CREATE: LlmAgentNode 基类 + parse_json_block + call_model 助手
  extraction/proposer.py  # CREATE: Extraction Proposer (doc→ConstraintProposals, 编译校验 oracle)
  triage/triager.py       # CREATE: Defect Triager (Findings→clusters, 不改判)
  consistency/assistant.py# CREATE: Consistency Assistant (dialogue→llm-assisted hints, quorum)
  consistency/checker.py  # CREATE: ConsistencyChecker (Checker Protocol, 产 llm-assisted Finding, 分区)
  repair/drafter.py       # CREATE: Repair Drafter (Finding+ctx→候选 TypedOps)
  repair/verify.py        # CREATE: 确定性验证器 (checkers+sim+aureus regression)
  repair/search.py        # CREATE: verifier-guided 修复搜索 (propose→verify→refine)
  generation/generator.py # CREATE: Content Generator (goal→候选 TypedOps)
  generation/gate.py      # CREATE: 生成门禁 (apply→checkers+sim→pass/reject)
  prompts/library.py      # CREATE: 注册所有 agent 的 prompt (prompt_version)
  harness.py              # CREATE: 跑修复搜索语料 → Fix Pass Rate + 搜索效率 (RECORD/REPLAY)
scenarios/agents/         # CREATE: extraction 输入文档样本 + consistency 对话样本 + narrative 约束
cassettes/                # CREATE (RECORD pass 产出, 入库): 真实 opus cassette
tests/agents/**           # 各 task 的测试 + tests/agents/test_part2_acceptance.py
```

---

## Task 1: agents/base — LlmAgentNode 基类 + JSON 解析 + call_model

**Files:** Create `gameforge/agents/base.py`; Test `tests/agents/test_agent_base.py`.

**Interfaces:**
- Consumes: `contracts.model_router.{Message,ModelRequest,ModelResponse,ModelSnapshot,request_hash}`, `agents.prompts.registry.render`, `runtime.model_router.router.ModelRouter`.
- Produces:
  - `DEFAULT_SNAPSHOT: ModelSnapshot` (`anthropic`/`claude-opus-4-8`/`m2a@1`).
  - `parse_json_block(text:str) -> dict|list`：strip ``` 围栏，用 `json.JSONDecoder().raw_decode` 从首个 `{`/`[` 解析；无 JSON → `raise AgentParseError`。
  - `class AgentParseError(Exception)`。
  - `call_model(router, agent_node_id, user_prompt, prompt_version, *, system=None, params=None, snapshot=DEFAULT_SNAPSHOT) -> tuple[ModelResponse, str]`：构造 `ModelRequest`（system→`Message(role="system")` 若给；user→`Message(role="user")`；`params` 默认 `{"max_tokens":2048,"temperature":0}`），`router.call(req)`，返回 `(resp, request_hash(req))`。

- [ ] **Step 1: failing test** `tests/agents/test_agent_base.py`:
```python
import pytest
from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport


def test_parse_json_block_handles_fences_and_prose():
    assert parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_block('Sure! [1, 2, 3] done.') == [1, 2, 3]
    with pytest.raises(AgentParseError):
        parse_json_block("no json here")


def test_call_model_builds_request_and_returns_hash(tmp_path):
    from gameforge.agents.base import DEFAULT_SNAPSHOT
    probe = ModelRequest(
        model_snapshot=DEFAULT_SNAPSHOT,
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="probe", prompt_version="p@1",
    )
    stub = StubTransport({request_hash(probe): ModelResponse(response_normalized='{"ok": true}')})
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD)
    resp, h = call_model(router, "probe", "hi", "p@1", system="sys")
    assert h == request_hash(probe)
    assert parse_json_block(resp.response_normalized) == {"ok": True}
```
- [ ] **Step 2: run→FAIL.**
- [ ] **Step 3: implement** `gameforge/agents/base.py`:
```python
"""Shared agent-node plumbing: deterministic JSON parsing + router call helper.

Agents reach the LLM ONLY through ModelRouter. Every model output is parsed
deterministically; parse failure is a fallback signal, never a crash upstream.
"""
from __future__ import annotations

import json

from gameforge.agents.prompts.registry import render  # noqa: F401  (re-exported for agents)
from gameforge.contracts.model_router import (
    Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash,
)
from gameforge.runtime.model_router.router import ModelRouter

DEFAULT_SNAPSHOT = ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag="m2a@1")


class AgentParseError(Exception):
    pass


def parse_json_block(text: str):
    t = text.strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        if len(parts) >= 3:
            body = parts[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            t = body.strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise AgentParseError(f"no JSON object/array in model output: {text[:120]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    except json.JSONDecodeError as exc:
        raise AgentParseError(str(exc)) from exc
    return obj


def call_model(
    router: ModelRouter,
    agent_node_id: str,
    user_prompt: str,
    prompt_version: str,
    *,
    system: str | None = None,
    params: dict | None = None,
    snapshot: ModelSnapshot = DEFAULT_SNAPSHOT,
) -> tuple[ModelResponse, str]:
    messages: list[Message] = []
    if system is not None:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=user_prompt))
    req = ModelRequest(
        model_snapshot=snapshot,
        messages=messages,
        params=params or {"max_tokens": 2048, "temperature": 0},
        agent_node_id=agent_node_id,
        prompt_version=prompt_version,
    )
    return router.call(req), request_hash(req)
```
- [ ] **Step 4: run→PASS.**
- [ ] **Step 5: commit** `feat(agents): LlmAgentNode 地基 — 稳健 JSON 解析 + call_model 助手`

---

## Task 2: agents/prompts/library — 所有 agent prompt 注册（prompt_version）

**Files:** Create `gameforge/agents/prompts/library.py`; Test `tests/agents/test_prompt_library.py`.

**Interfaces:**
- Produces: `register_all_prompts()`（幂等注册所有 agent 的 system prompt，每个带 `prompt_version`）；模块 import 时自动调用一次。prompt 名与版本（进 `request_hash`）：
  - `extraction.system` `extraction@1`、`triage.system` `triage@1`、`repair.system` `repair@1`、`repair.refine` `repair@1`、`consistency.system` `consistency@1`、`generation.system` `generation@1`。
- 每个 prompt 明确要求**只输出 JSON**、给出目标 schema、并声明"你只提议，权威判定由确定性验证器/人给出"。

- [ ] **Step 1: failing test**:
```python
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt


def test_all_agent_prompts_registered():
    register_all_prompts()
    for name, ver in [("extraction.system","extraction@1"),("triage.system","triage@1"),
                      ("repair.system","repair@1"),("repair.refine","repair@1"),
                      ("consistency.system","consistency@1"),("generation.system","generation@1")]:
        v, tmpl = get_prompt(name)
        assert v == ver and "JSON" in tmpl
```
- [ ] **Step 2: FAIL → 3: implement** `library.py`（`register_prompt(...)` 逐个；每个模板含 schema 描述 + "output ONLY a JSON ..."；`repair.refine` 含 `{counterexample}` 占位；文件末尾调用 `register_all_prompts()`）。写具体、可用的英文 prompt（非占位）。→ **4: PASS → 5: commit** `feat(agents): 全 agent prompt 注册表 (prompt_version 化)`。

---

## Task 3: agents/extraction — Extraction Proposer（doc→约束提议 + 可编译 oracle）

**Files:** Create `gameforge/agents/extraction/proposer.py`; Test `tests/agents/test_extraction.py`.

**Interfaces:**
- Consumes: `agents.base.{call_model,parse_json_block}`, `contracts.agent_io.{DesignDocInput,EntityConstraintProposals,ConstraintProposal,AgentNodeResult}`, `contracts.dsl.Constraint`, `spine.dsl.ast.parse_assert`.
- Produces: `class ExtractionProposer`（`node_id="extraction"`，实现 `AgentNode`）：`run(input:DesignDocInput, router)->AgentNodeResult`。LLM 提议 `[{proposed_id,kind,assert_expr,rationale}]` → 对每条 **deterministic oracle**：`assert_expr` 能过 `parse_assert`（数值/结构表达式合法）则保留、否则丢弃并计入丢弃数；全部 `needs_human_authoring=True`（人撰写为权威）。解析/调用失败 → `fallback_taken=True`，空提议。`produced={"proposals":[...], "dropped": n}`。

- [ ] **Step 1: failing test**（StubTransport 回一段含合法 + 非法 assert 的 JSON，断言非法被 oracle 丢弃、合法保留、needs_human_authoring=True）。
- [ ] **Step 2–4: FAIL→implement→PASS.** 实现要点：`parse_assert(p["assert_expr"])` 抛 `DslError` → 丢弃。
- [ ] **Step 5: commit** `feat(agents/extraction): Extraction Proposer (约束提议 + parse_assert 可编译 oracle + 人审兜底)`

---

## Task 4: agents/triage — Defect Triager（聚类，不改判确定性结论）

**Files:** Create `gameforge/agents/triage/triager.py`; Test `tests/agents/test_triage.py`.

**Interfaces:**
- Produces: `class DefectTriager`（`node_id="triage"`）：`run(input:FindingsInput, router)->AgentNodeResult`，LLM 产 `[{cluster_id,finding_ids,priority,suspected_root_cause}]`。**Oracle（关键）**：`cluster` 的 `finding_ids` 必须 ⊆ 输入 findings 的 id（越界 id 丢弃）；**绝不复述/篡改任何 Finding 的 `status`/`oracle_type`/`defect_class`**——triager 只输出 `TriagedCluster`，原 Finding 对象**原样透传**不进 LLM 输出结构。`produced={"triaged": TriagedFindings(...).model_dump(), "input_findings_untouched": True}`。

- [ ] **Step 1: failing test**（构造 3 个 Finding，StubTransport 回 2 个 cluster 其中一个含越界 id；断言越界 id 被剔除、cluster 覆盖合法 id、且返回结构里不含任何被改写的 Finding 判定字段）。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/triage): Defect Triager (聚类/优先级, 不改判确定性结论)`

---

## Task 5: agents/consistency — Consistency Assistant + ConsistencyChecker（llm-assisted, 严格分区, quorum）

**Files:** Create `gameforge/agents/consistency/assistant.py`, `gameforge/agents/consistency/checker.py`; Test `tests/agents/test_consistency.py`.

**Interfaces:**
- `class ConsistencyAssistant`（`node_id="consistency"`）：`run(input:DialogueNarrativeInput, router)->AgentNodeResult`。**quorum（P2-D5）**：以 3 个 prompt_version 变体（`consistency@1#s0/#s1/#s2`，各自独立 `request_hash`→cassette）采样，解析每次的 `hints`，**按 `(span,issue)` 多数（≥2/3）保留**。`produced={"hints":[ConsistencyHint...], "samples":3}`。
- `class ConsistencyChecker`（实现 `Checker` Protocol，`id="consistency"`；构造接收 `assistant`+`router`+narrative 约束+对话源）：`check(snapshot, nav=None)->[Finding]`——把 quorum 通过的 hint 转 `Finding(source="llm", oracle_type="llm-assisted", status="unproven", defect_class="narrative_inconsistency", ...)`。**验证锚点**：这些 Finding 经 `build_review_report`/`ReviewReport.partition` **只落 `llm_assisted_findings`，绝不进 `deterministic_findings`**——这是 M1 `LlmRoutedChecker` 占位的真实评估落点。

- [ ] **Step 1: failing test**：StubTransport 对 3 个变体 request 回不同 hints（2 个采样都报同一 `(span,issue)`，1 个不报）→ 断言 quorum 保留该 hint；`ConsistencyChecker().check(snap)` 产 `oracle_type="llm-assisted"` Finding；喂给 `build_review_report(snap, [consistency_checker])` → `report.llm_assisted_findings != [] and report.deterministic_findings == []`。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/consistency): Consistency Assistant (quorum) + ConsistencyChecker (llm-assisted 严格分区, M1 LlmRouted 真实评估落点)`

---

## Task 6: agents/repair — Repair Drafter + 确定性验证器 + verifier-guided 修复搜索（CORE）

**Files:** Create `gameforge/agents/repair/drafter.py`, `gameforge/agents/repair/verify.py`, `gameforge/agents/repair/search.py`; Test `tests/agents/test_repair_verify.py`, `tests/agents/test_repair_search.py`.

**Interfaces:**
- `verify.py`：
  - `@dataclass VerifyResult{ok:bool, target_resolved:bool, new_deterministic:list[Finding], regression_ok:bool, detail:str}`。
  - `verify_patch(base_snapshot, patched_snapshot, checkers, target_defect_class, *, run_regression=True) -> VerifyResult`：跑 `build_review_report(patched_snapshot, checkers)`；`target_resolved` = patched 里无 `defect_class==target` 的 deterministic Finding；`new_deterministic` = patched 的 deterministic findings 里 base 没有的（按 `(defect_class, tuple(entities))` 差集）；经济实体存在则跑 `EconomySimulator` 断言无新 `economy_collapse`；`run_regression` 且 `snapshot_to_world(patched)` 成功则建 `AureusEnv`、`reset`+跑一小段 `observe`/`navigate` 序列断言不崩（`regression_ok`）。`ok = target_resolved and not new_deterministic and regression_ok`。
- `drafter.py`：`class RepairDrafter`（`node_id="repair"`）：`draft(finding, snapshot, router, *, counterexample=None) -> Patch|None`——LLM（`repair.system`，refine 时叠 `repair.refine` 带 `counterexample`）产 `[{op,target,old_value,new_value,...}]` → 构造 `Patch(produced_by="agent", base_snapshot_id=snapshot.snapshot_id, ...ops=[TypedOp...])`；解析失败→`None`。
- `search.py`：`repair_search(finding, snapshot, checkers, router, *, max_steps=4) -> PatchDraft`——loop：`draft`→`apply_patch`（`PatchRejected`→反例=拒绝原因）→`verify_patch`→通过则 `PatchDraft(patch, search_steps=i+1, passed_verification=True)`；否则反例=（新 Finding 摘要 / 未消解）回灌下一轮 `draft`；超预算→`PatchDraft(patch=最后一次或空, search_steps=max_steps, passed_verification=False)`。**验证器全确定性**（spine+仿真+Aureus），LLM 只提议。

- [ ] **Step 1: verify 单测** `test_repair_verify.py`：构造一个 `reward_out_of_range` 脏 snapshot + 约束；手工造一个正确 patch（set reward 到区间内）→ `apply_patch` → `verify_patch(...).ok is True`；再造一个把 reward 改成另一个越界值的 patch → `verify_patch(...).target_resolved is False`；造一个引入 `dangling_reference` 的 patch → `new_deterministic != [] and ok is False`。
- [ ] **Step 2: search 单测** `test_repair_search.py`：StubTransport 第一次回一个**坏** patch（不消解）、refine 后第二次回**好** patch → `repair_search(...).passed_verification is True and search_steps == 2`；证明 propose→verify→refine 真闭环、验证器判对错。用手写 cassette/StubTransport keyed by 两次不同 request_hash（refine 轮的 prompt 含 counterexample → 不同 hash）。
- [ ] **Step 3–4: FAIL→implement→PASS**（`verify.py` 用 §interface 的确定性验证器；`search.py` 用 apply_patch/PatchRejected/verify_patch）。
- [ ] **Step 5: commit** `feat(agents/repair): Repair Drafter + 确定性验证器 (checkers+sim+aureus) + verifier-guided 修复搜索 (propose→verify→refine)`

---

## Task 7: agents/generation — Content Generator + 生成门禁

**Files:** Create `gameforge/agents/generation/generator.py`, `gameforge/agents/generation/gate.py`; Test `tests/agents/test_generation.py`.

**Interfaces:**
- `generator.py`：`class ContentGenerator`（`node_id="generation"`）：`run(input:DesignGoalInput, router)->AgentNodeResult`——LLM 产候选 `proposed_ops:[TypedOp-dict]`，grounded 在 `grounding_snapshot_id`（prompt 里给可用实体/区间摘要）。
- `gate.py`：`gate_proposal(base_snapshot, proposed_ops, checkers) -> tuple[bool, list[Finding]]`——构造 `Patch(ops=...)`→`apply_patch`→`build_review_report(new_snap, checkers)`（+经济仿真）→ `passed = 无新 deterministic Finding 且无 economy_collapse`；返回 `(passed, blocking_findings)`。`ContentProposal.passed_gate` 由此置位。**生成物永远是提议，未过门禁不得进候选。**

- [ ] **Step 1: failing test**：StubTransport 回一个会引入越界 reward 的 proposal → `gate_proposal(...)[0] is False` 且 blocking 含 `reward_out_of_range`；回一个合法 proposal → `passed is True`。
- [ ] **Step 2–4: FAIL→implement→PASS. 5: commit** `feat(agents/generation): Content Generator + 检查器/仿真生成门禁 (提议永过门禁)`

---

## Task 8: harness + 录制 pass + part2 验收

**Files:** Create `gameforge/agents/harness.py`, `scenarios/agents/` (extraction doc + consistency 对话 + narrative 约束样本), `cassettes/` (RECORD 产出), `tests/agents/test_part2_acceptance.py`; Modify `README.md`, `docs/superpowers/plans/README.md`, memory (controller 负责 memory)。

**Interfaces:**
- `harness.py`：
  - `run_repair_corpus(scenario_dirs, constraints_path, router, *, max_steps=4) -> RepairCorpusResult`——对每个缺陷场景 load snapshot+compile 约束+跑该场景**注入缺陷类**的 `repair_search`，汇总 `RepairCorpusResult{attempted:int, passed:int, fix_pass_rate:float, per_scenario:list[{scenario,defect_class,passed,search_steps}], avg_steps:float, first_pass_rate:float}`。
  - `record_pass(...)` / `replay_pass(...)` 薄封装（RouterMode）。
- `scenarios/agents/`：extraction 输入文档（策划案片段，含可抽约束）、consistency 对话样本 + `narrative.yaml`（叙事约束）。
- **§16 part2 验收** `tests/agents/test_part2_acceptance.py`（全 REPLAY，零实网）：
  1. `test_fix_pass_rate_ge_70pct`：`replay_pass` 跑 9 场景语料 → `result.fix_pass_rate >= 0.70`。
  2. `test_repair_search_reproducible`：同语料 REPLAY 跑两次 → `result` 逐字段相等（复现）。
  3. `test_deterministic_and_llm_strictly_partitioned`：跑一个带 ConsistencyChecker 的 review → `llm_assisted_findings != [] and all(f.oracle_type!="llm-assisted" for f in deterministic_findings)`。
  4. `test_generation_gate_blocks_defective_proposal`（REPLAY）。
  5. `test_search_efficiency_report_present`：`result.avg_steps` / `result.first_pass_rate` 有值且合理。
  6. `test_extraction_and_triage_smoke`（REPLAY，各产结构化输出且兜底不崩）。

- [ ] **Step 1: 写 harness + 语料 + 上述验收测试（先 FAIL：无 cassette→REPLAY miss）。**
- [ ] **Step 2: 真实录制 pass**（**需 `GAMEFORGE_LLM_LIVE=1` + 网关**）：`GAMEFORGE_LLM_LIVE=1 uv run python -m gameforge.agents.harness --record`（对 9 修复场景 + extraction/consistency/generation 样本跑 RECORD，写 `cassettes/`）。**若 `fix_pass_rate < 0.70`：迭代 `repair.system`/`repair.refine` prompt、`max_steps`、验证器反例措辞并重录（调 agent 不调阈值，P2-D6），直到 ≥70%。** 录制是本地/人工触发；提交 `cassettes/` 入库。
- [ ] **Step 3: 全量验收 run（REPLAY）**：`uv run pytest -q`（零实网全绿，含 part2 验收）、`uv run lint-imports`（7 契约 KEPT）、`uv run ruff check .`。Expected：Fix Pass Rate ≥70%、复现、严格分区、门禁、搜索效率报告齐全。
- [ ] **Step 4: 收尾文档** `README.md`（M2a-part2 段：交付 6 agent + 修复搜索 + 验收数据 vs 延后 M2b）；`plans/README.md`。（memory 由 controller 更新。）
- [ ] **Step 5: commit** `feat(m2a-part2): 6 有边界 agent + verifier-guided 修复搜索; Fix Pass Rate≥70% + 复现 + 严格分区 + 搜索效率 (REPLAY 零实网, 真实 opus 录制)`

---

## Self-Review

**1. Spec coverage**（M2 设计 §6 + §16 part2 + §13.4）：
- Extraction Proposer（提议 + 可编译 oracle + 人审兜底）→ Task 3 ✔
- Defect Triager（聚类不改判）→ Task 4 ✔
- Consistency Assistant + llm-assisted 严格分区 + quorum（M1 LlmRouted 真实评估）→ Task 5 ✔
- Repair Drafter + verifier-guided 修复搜索（propose→verify→refine，确定性验证器）→ Task 6 ✔
- Content Generator + 生成门禁 → Task 7 ✔
- Fix Pass Rate ≥70% + 复现 + 搜索效率报告 → Task 8 ✔
- agent 只经 ModelRouter/opus messages transport、CI 零实网、cassette 回放 → Tasks 1–8（依赖 lint 7 契约）✔
- **延后 M2b**：Playtest（长程 + mem-trace + 消融）；对抗性叙事辩论进阶（part2 只基础 quorum）。

**2. Placeholder scan**：Task 1/5/7 给全代码/接口；Task 2/3/4/6 给精确接口 + 关键测试锚点 + 实现要点（oracle/兜底具体）；prompt 文本 Task 2 要求写实（非占位）。修复搜索的验证器/反例回灌是具体机制非"add refine"。真实录制 pass 显式为一等步骤（含 <70% 的迭代重录）。

**3. Type consistency**：`AgentNodeResult`/6 角色 I/O（part1 `agent_io.py`）被 2–7 生产；`call_model`/`parse_json_block`（T1）被 2–7 复用；`compile_all`/`build_review_report`/`apply_patch`/`verify_patch`（spine+T6）贯穿修复搜索/门禁；`ModelRouter`/`AnthropicMessagesTransport`/`CassetteStore`（part1）贯穿；`ConsistencyChecker` 实现 `Checker` Protocol 被 `build_review_report` 消费。

**Deferred（接口现定 — 不简化只延后）**：Playtest Agent + mem-trace + 记忆/planner 消融 + ≥20 链回归（M2b）；对抗性叙事辩论（part2 基础 quorum，进阶 M2b）；稳定前缀语义缓存的 KG-prefix 切分（M2b/性能）。
