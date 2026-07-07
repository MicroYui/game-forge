# M2a — Agent 地基（Model Router / Cassette + 契约§7 + 编排 harness）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **实现子 agent 用 sonnet5。**

**Goal:** 落地 M2 契约 §7（Model Router / Cassette / `request_hash`）+ 6 个 agent 角色 I/O 契约 + 确定性 Model Router（RECORD/REPLAY/PASSTHROUGH）+ Cassette 录放 + 自研确定性 agent 编排 harness——全程 TDD、**零实网 LLM 调用**（用可注入 stub transport + 手写 cassette fixture），为 M2a-part2（各 agent 实体 + verifier-guided 修复搜索）与 M2b（Playtest）铺好可复现地基。

**Architecture:** 契约机器可读类型落 `contracts/`（单一真相源，契约 1）；LLM 传输/录放实现落 `runtime/`（`runtime.model_router` 是**唯一**可 import LLM SDK 的包，import-linter 强制）；agent 编排落 `agents/`（经 `ModelRouter` 调用，自身不直连 SDK）。`spine` 全程不参与（已 allowlist 墙死）。复现靠 `request_hash`（`contracts/canonical.py` 的 canonical_json）+ cassette replay，CI/测试**强制 REPLAY**。

**Tech Stack:** Python 3.12 (uv), pydantic v2, stdlib `hashlib`/`json`/`enum`/`os`, `openai` SDK（仅 `runtime.model_router`，指向本地网关 `localhost:4141`）, pytest, import-linter, ruff。

## Global Constraints

Copied verbatim from CLAUDE.md 硬规则 + 地基契约 + M2 设计（`docs/superpowers/specs/2026-07-07-m2-agent-layer-design.md`）。每个 task 隐含包含本节。

- **不简化，只延后**：6 个 agent 角色 I/O 契约（Extraction/Triage/Repair/Consistency/Generation/Playtest）**字段一次定全**；Playtest I/O 现定、实现留 M2b。`request_hash`/cassette record 字段集一次定死。
- **确定性优先 / 可复现只承诺回放**：`request_hash` 用 `contracts.canonical.canonical_json`；相同 `ModelRequest`（除 `cache_key`/schema_version 外）→ 相同 hash → REPLAY 逐位复现。solver/LLM 不确定性由 cassette 隔离。CI/测试**强制 REPLAY，零实网调用**。
- **依赖方向单向（CI 强制）**：`agents → {contracts, spine, env, game, runtime}`；`runtime → {contracts}`（+ `openai` 仅 `runtime.model_router`）；`spine` 永不碰 LLM（已 allowlist 强制，M1 commit `c5f4c05`）。新增 import-linter 契约：LLM SDK（openai/litellm/anthropic/…）**仅允许 `gameforge.runtime.model_router`**。
- **每个 LLM 输出必有确定性预言机或人工兜底**：本 plan 只建**传输/录放/编排地基**，不含 LLM 语义判定；`AgentNodeResult.fallback_taken` 与 `request_hashes` 字段现定，供 part2 的兜底与可追溯用。
- **密钥绝不入库**：网关 key 从 `GAMEFORGE_LLM_KEY`/`.env`（gitignored）读；仓库只留指针。
- **TDD 全程**：每 task test-first（写失败测试→跑→实现→跑→commit）。
- **Package namespace**：`gameforge.<pkg>`；下文短形式（`contracts.model_router` 等）一律按 `gameforge.` 前缀读。
- **Git**：commit 信息不带任何 AI 协作者署名 / "Generated with"。主干分支 `master`。
- **schema_version 常量**（`contracts/versions.py`）：本 plan 新增 `MODEL_ROUTER_SCHEMA_VERSION="model-router@1"`、`CASSETTE_SCHEMA_VERSION="cassette@1"`、`AGENT_IO_SCHEMA_VERSION="agent-io@1"`。

---

## Repo layout delta produced by this plan

```
gameforge/
  contracts/
    versions.py         # MODIFY: + MODEL_ROUTER/CASSETTE/AGENT_IO schema versions
    __init__.py         # MODIFY: re-export new constants + types
    model_router.py     # CREATE: ModelSnapshot/Message/ToolSchemaRef/ModelRequest/ModelResponse + request_hash()
    cassette.py         # CREATE: CassetteRecord + CASSETTE_MISS sentinel
    agent_io.py         # CREATE: AgentRole + AgentNodeResult + 6 角色 Input/Output (字段一次定全)
  runtime/
    secrets/
      env.py            # CREATE: get_llm_key() 从 GAMEFORGE_LLM_KEY 读
    model_router/
      transport.py      # CREATE: LlmTransport Protocol + OpenAITransport (SDK 唯一落点) + StubTransport
      router.py         # CREATE: ModelRouter (RECORD/REPLAY/PASSTHROUGH + retry/quota + 会话内 cache)
    cassette/
      store.py          # CREATE: CassetteStore.record/replay (flat cassettes/<hash>.json)
  agents/
    prompts/
      registry.py       # CREATE: prompt 注册表 (name -> (version, template))
    orchestrator.py     # CREATE: AgentNode Protocol + run_graph 确定性顺序执行
pyproject.toml          # MODIFY: + openai dep; + import-linter "LLM SDK only in runtime.model_router"
tests/                  # 对应各 task 的测试 + tests/agents/test_foundations_acceptance.py
```

---

## Task 1: contracts §7 — Model Router schema + `request_hash` + 版本常量

**Files:**
- Create: `gameforge/contracts/model_router.py`
- Modify: `gameforge/contracts/versions.py`, `gameforge/contracts/__init__.py`
- Test: `tests/contracts/test_model_router.py`

**Interfaces:**
- Consumes: `contracts.canonical.canonical_json`, `contracts.versions.MODEL_ROUTER_SCHEMA_VERSION`.
- Produces: `ModelSnapshot{provider,model,snapshot_tag}`、`ToolSchemaRef{name,version}`、`Message{role,content,tool_calls}`、`ModelRequest{model_router_schema_version,model_snapshot,messages,params,tool_schemas,agent_node_id,prompt_version,cache_key}`、`ModelResponse{response_normalized,raw_response,latency_ms,token_usage,finish_reason,tool_calls}`、`request_hash(req:ModelRequest)->str`（`"sha256:<hex>"`）。

- [ ] **Step 1: Write failing test** — `tests/contracts/test_model_router.py`:
```python
from gameforge.contracts.model_router import (
    Message, ModelRequest, ModelSnapshot, ToolSchemaRef, request_hash,
)


def _req(**over):
    base = dict(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content="hi")],
        params={"temperature": 0.0},
        agent_node_id="triage",
        prompt_version="triage@1",
    )
    base.update(over)
    return ModelRequest(**base)


def test_request_hash_is_deterministic_and_prefixed():
    assert request_hash(_req()) == request_hash(_req())
    assert request_hash(_req()).startswith("sha256:")


def test_request_hash_excludes_cache_key_and_schema_version():
    # cache_key is a routing hint, not part of what determines the model output
    assert request_hash(_req(cache_key="abc")) == request_hash(_req(cache_key=None))


def test_request_hash_changes_with_semantic_fields():
    assert request_hash(_req()) != request_hash(_req(prompt_version="triage@2"))
    assert request_hash(_req()) != request_hash(
        _req(messages=[Message(role="user", content="different")])
    )
    assert request_hash(_req()) != request_hash(
        _req(tool_schemas=[ToolSchemaRef(name="patch", version="patch@1")])
    )
```
- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/contracts/test_model_router.py -v` → FAIL (module missing).
- [ ] **Step 3: Add version constants** — append to `gameforge/contracts/versions.py`:
```python
MODEL_ROUTER_SCHEMA_VERSION = "model-router@1"
CASSETTE_SCHEMA_VERSION = "cassette@1"
AGENT_IO_SCHEMA_VERSION = "agent-io@1"
```
- [ ] **Step 4: Implement** `gameforge/contracts/model_router.py`:
```python
"""Model Router request/response schema (contract §7) — single source of truth.

Only the deterministic request_hash lives here; HTTP-to-gateway + record/replay
are runtime/ concerns. request_hash EXCLUDES cache_key / schema_version — it is
exactly the set of fields that determine the model's output (contract §7).
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.versions import MODEL_ROUTER_SCHEMA_VERSION


class ModelSnapshot(BaseModel):
    provider: str
    model: str
    snapshot_tag: str  # pins a served version; guards against silent upgrades


class ToolSchemaRef(BaseModel):
    name: str
    version: str


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ModelRequest(BaseModel):
    model_router_schema_version: str = MODEL_ROUTER_SCHEMA_VERSION
    model_snapshot: ModelSnapshot
    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)
    tool_schemas: list[ToolSchemaRef] = Field(default_factory=list)
    agent_node_id: str
    prompt_version: str
    cache_key: str | None = None  # semantic-cache hint; NOT part of request_hash


class ModelResponse(BaseModel):
    response_normalized: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


def request_hash(req: ModelRequest) -> str:
    payload = {
        "model_snapshot": req.model_snapshot.model_dump(),
        "messages": [m.model_dump() for m in req.messages],
        "tool_schema_versions": [[t.name, t.version] for t in req.tool_schemas],
        "params": req.params,
        "agent_node_id": req.agent_node_id,
        "prompt_version": req.prompt_version,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
```
- [ ] **Step 5: Re-export** — in `gameforge/contracts/__init__.py` add (follow existing re-export style):
```python
from gameforge.contracts.model_router import (  # noqa: F401
    Message, ModelRequest, ModelResponse, ModelSnapshot, ToolSchemaRef, request_hash,
)
from gameforge.contracts.versions import (  # noqa: F401
    AGENT_IO_SCHEMA_VERSION, CASSETTE_SCHEMA_VERSION, MODEL_ROUTER_SCHEMA_VERSION,
)
```
- [ ] **Step 6: Run to verify pass** — `uv run pytest tests/contracts/test_model_router.py -v` → PASS.
- [ ] **Step 7: Commit** — `git commit -am "feat(contracts): Model Router schema + request_hash (契约§7, canonical-json 稳定)"`

---

## Task 2: contracts §7 — Cassette record schema + 6 角色 agent I/O 契约

**Files:**
- Create: `gameforge/contracts/cassette.py`, `gameforge/contracts/agent_io.py`
- Modify: `gameforge/contracts/__init__.py`
- Test: `tests/contracts/test_cassette.py`, `tests/contracts/test_agent_io.py`

**Interfaces:**
- Consumes: `contracts.model_router.{ModelResponse,ModelSnapshot}`, `contracts.findings.{Finding,Patch}`, `contracts.versions.{CASSETTE_SCHEMA_VERSION,AGENT_IO_SCHEMA_VERSION}`.
- Produces:
  - `CassetteRecord{cassette_schema_version,request_hash,agent_node_id,model_snapshot,response,recorded_at}`；`CASSETTE_MISS`（sentinel）。
  - `AgentRole=Literal["extraction","triage","repair","consistency","generation","playtest"]`；`AgentNodeResult{agent_io_schema_version,role,fallback_taken,model_run_id,request_hashes,produced}`；6 角色 `*Input`/`*Output`（见实现）。

- [ ] **Step 1: Write failing tests** — `tests/contracts/test_cassette.py`:
```python
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot


def test_cassette_record_round_trips():
    r = CassetteRecord(
        request_hash="sha256:x", agent_node_id="triage",
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        response=ModelResponse(response_normalized="ok"),
    )
    assert CassetteRecord.model_validate(r.model_dump()) == r
    assert r.cassette_schema_version == "cassette@1"


def test_cassette_miss_is_a_distinct_sentinel():
    assert CASSETTE_MISS is not None
    assert CASSETTE_MISS != CassetteRecord.model_construct()
```
`tests/contracts/test_agent_io.py`:
```python
from gameforge.contracts.agent_io import AgentNodeResult, PatchDraft, TriagedFindings


def test_agent_node_result_tracks_llm_calls():
    r = AgentNodeResult(role="triage", model_run_id="run1", request_hashes=["sha256:a", "sha256:b"])
    assert r.agent_io_schema_version == "agent-io@1"
    assert r.request_hashes == ["sha256:a", "sha256:b"]
    assert r.fallback_taken is False


def test_all_six_roles_have_output_models():
    #字段一次定全：即便 M2a 不实现 playtest，其 I/O 也可构造
    from gameforge.contracts.agent_io import (
        ConsistencyHints, ContentProposal, EntityConstraintProposals,
        PlaytestReport,
    )
    assert EntityConstraintProposals().proposals == []
    assert TriagedFindings().clusters == []
    assert PatchDraft.model_fields["passed_verification"].default is False
    assert ConsistencyHints().hints == []
    assert ContentProposal().passed_gate is False
    assert PlaytestReport().completed is False
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `gameforge/contracts/cassette.py`:
```python
"""Cassette record schema (contract §7) — record/replay isolates nondeterminism."""
from __future__ import annotations

from pydantic import BaseModel

from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.contracts.versions import CASSETTE_SCHEMA_VERSION


class CassetteRecord(BaseModel):
    cassette_schema_version: str = CASSETTE_SCHEMA_VERSION
    request_hash: str
    agent_node_id: str
    model_snapshot: ModelSnapshot
    response: ModelResponse
    recorded_at: str | None = None


class _CassetteMiss:
    """Sentinel returned by CassetteStore.replay when no record exists."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CASSETTE_MISS"


CASSETTE_MISS = _CassetteMiss()
```
- [ ] **Step 4: Implement** `gameforge/contracts/agent_io.py`:
```python
"""Agent-role I/O contracts (PRD §7.5) — 6 roles, fields once (不简化只延后).

M2a implements extraction/triage/repair/consistency/generation; playtest I/O is
defined here now, implemented in M2b.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.findings import Finding, Patch
from gameforge.contracts.versions import AGENT_IO_SCHEMA_VERSION

AgentRole = Literal[
    "extraction", "triage", "repair", "consistency", "generation", "playtest"
]


class AgentNodeResult(BaseModel):
    agent_io_schema_version: str = AGENT_IO_SCHEMA_VERSION
    role: AgentRole
    fallback_taken: bool = False
    model_run_id: str
    request_hashes: list[str] = Field(default_factory=list)  # traces every LLM call
    produced: dict[str, Any] = Field(default_factory=dict)


# --- Extraction Proposer ---
class DesignDocInput(BaseModel):
    doc_text: str
    doc_version: str


class ConstraintProposal(BaseModel):
    proposed_id: str
    kind: str
    assert_expr: str
    rationale: str
    needs_human_authoring: bool = True  # LLM proposes; human authors authoritative


class EntityConstraintProposals(BaseModel):
    proposals: list[ConstraintProposal] = Field(default_factory=list)


# --- Defect Triager ---
class FindingsInput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class TriagedCluster(BaseModel):
    cluster_id: str
    finding_ids: list[str]
    priority: Literal["p0", "p1", "p2", "p3"]
    suspected_root_cause: str


class TriagedFindings(BaseModel):
    clusters: list[TriagedCluster] = Field(default_factory=list)


# --- Repair Drafter ---
class FindingContextInput(BaseModel):
    finding: Finding
    snapshot_id: str


class PatchDraft(BaseModel):
    patch: Patch
    search_steps: int = 0
    passed_verification: bool = False


# --- Consistency Assistant ---
class DialogueNarrativeInput(BaseModel):
    dialogue: str
    narrative_constraint_ids: list[str] = Field(default_factory=list)


class ConsistencyHint(BaseModel):
    span: str
    issue: str
    is_suggestion: bool = True  # llm-assisted; human-confirmed, never authoritative


class ConsistencyHints(BaseModel):
    hints: list[ConsistencyHint] = Field(default_factory=list)


# --- Content Generator ---
class DesignGoalInput(BaseModel):
    goal: str
    grounding_snapshot_id: str


class ContentProposal(BaseModel):
    proposed_ops: list[dict[str, Any]] = Field(default_factory=list)
    passed_gate: bool = False  # must pass checker+sim gate before candidacy


# --- Playtest Agent (I/O defined @M2a, impl @M2b) ---
class PlaytestInput(BaseModel):
    scenario: str
    seed: int


class PlaytestReport(BaseModel):
    action_trace: list[dict[str, Any]] = Field(default_factory=list)
    defect_findings: list[Finding] = Field(default_factory=list)
    completed: bool = False
```
- [ ] **Step 5: Re-export** — add to `gameforge/contracts/__init__.py`:
```python
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord  # noqa: F401
from gameforge.contracts.agent_io import (  # noqa: F401
    AgentNodeResult, AgentRole,
)
```
- [ ] **Step 6: Run to verify pass** → PASS.
- [ ] **Step 7: Commit** — `git commit -am "feat(contracts): Cassette record + 6 agent 角色 I/O 契约 (字段一次定全)"`

---

## Task 3: 依赖门禁 — LLM SDK 仅限 `runtime.model_router` + openai 依赖

**Files:**
- Modify: `pyproject.toml`（`[project].dependencies` += `openai`；`[tool.importlinter]` + 新契约）
- Test: `tests/test_dependency_lint.py`（新增 negative 测试）

**Interfaces:**
- Produces: 一条 import-linter forbidden 契约——`gameforge.agents`/`gameforge.spine`/`gameforge.env`/`gameforge.game`/`gameforge.contracts`/`gameforge.platform`/`gameforge.apps`/`gameforge.bench` 禁 import `openai`/`anthropic`/`litellm`/`langchain`/`langgraph`/`llama_index`；`gameforge.runtime` 除 `model_router` 外亦禁。**`gameforge.runtime.model_router` 是唯一允许 LLM SDK 的源。**

- [ ] **Step 1: Write failing test** — 追加到 `tests/test_dependency_lint.py`:
```python
def test_llm_sdk_only_allowed_in_model_router():
    # openai imported anywhere else in gameforge (here: agents) must trip the gate.
    probe = os.path.join(os.path.dirname(__file__), os.pardir, "gameforge", "agents", "_sdk_probe.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("import openai  # probe: LLM SDK only allowed in runtime.model_router\n")
    try:
        assert lint_imports(no_cache=True) != EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)


def test_model_router_may_import_openai():
    # The one allowed home for the SDK — a probe there must NOT trip the gate.
    probe = os.path.join(
        os.path.dirname(__file__), os.pardir,
        "gameforge", "runtime", "model_router", "_sdk_ok_probe.py",
    )
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("import openai  # allowed here\n")
    try:
        assert lint_imports(no_cache=True) == EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)
```
- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_dependency_lint.py::test_llm_sdk_only_allowed_in_model_router -v` → FAIL（当前无此契约，openai 未被禁）。
- [ ] **Step 3: Add dependency** — `pyproject.toml` `[project].dependencies` += `"openai>=1.40"`.
- [ ] **Step 4: Add import-linter contract** — 在 `pyproject.toml` `[tool.importlinter]` 末尾追加：
```toml
[[tool.importlinter.contracts]]
name = "LLM SDK only in runtime.model_router"
type = "forbidden"
source_modules = [
    "gameforge.agents",
    "gameforge.spine",
    "gameforge.env",
    "gameforge.game",
    "gameforge.contracts",
    "gameforge.platform",
    "gameforge.apps",
    "gameforge.bench",
    "gameforge.runtime.cassette",
    "gameforge.runtime.config",
    "gameforge.runtime.observability",
    "gameforge.runtime.persistence",
    "gameforge.runtime.secrets",
]
forbidden_modules = [
    "openai", "anthropic", "litellm", "langchain", "langgraph", "llama_index",
    "cohere", "mistralai", "groq", "google", "ollama",
]
```
> 注：`gameforge.runtime` 整体不入 source（否则会连带禁掉 `runtime.model_router`）；改为逐个列出 runtime 的**其它**子包。`runtime.model_router` 不在 source → 允许 openai。
- [ ] **Step 5: uv sync + 验证** — `uv sync && uv run pytest tests/test_dependency_lint.py -v && uv run lint-imports` → PASS（7 契约 KEPT，含新契约；两个 probe 测试自建自删）。
- [ ] **Step 6: Commit** — `git commit -am "chore(deps/lint): openai 依赖 + import-linter 契约 'LLM SDK 仅限 runtime.model_router'"`

---

## Task 4: runtime — LlmTransport 抽象 + OpenAITransport（SDK 唯一落点）+ 密钥读取

**Files:**
- Create: `gameforge/runtime/model_router/transport.py`, `gameforge/runtime/secrets/env.py`
- Test: `tests/runtime/model_router/test_transport.py`, `tests/runtime/secrets/test_env.py`

**Interfaces:**
- Consumes: `contracts.model_router.{ModelRequest,ModelResponse}`, `openai`（仅此文件）, `runtime.secrets.env.get_llm_key`.
- Produces:
  - `class LlmTransport(Protocol){complete(req:ModelRequest)->ModelResponse}`。
  - `class OpenAITransport(base_url:str, api_key:str, client=None)`：`complete()` 调 `client.chat.completions.create(...)` 并映射成 `ModelResponse`；`client` 可注入（测试用 fake，不触网）。
  - `class StubTransport(responses:dict[str,ModelResponse])`：`complete()` 按 `request_hash(req)` 返回预置响应并记录 `calls`（供 router/agent 测试用，零依赖）。
  - `get_llm_key()->str`（`runtime/secrets/env.py`；缺失 `GAMEFORGE_LLM_KEY` → `RuntimeError`）。

- [ ] **Step 1: Write failing tests** — `tests/runtime/secrets/test_env.py`:
```python
import pytest
from gameforge.runtime.secrets.env import get_llm_key


def test_get_llm_key_reads_env(monkeypatch):
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "sk-test")
    assert get_llm_key() == "sk-test"


def test_get_llm_key_raises_when_absent(monkeypatch):
    monkeypatch.delenv("GAMEFORGE_LLM_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_llm_key()
```
`tests/runtime/model_router/test_transport.py`:
```python
from gameforge.contracts.model_router import Message, ModelRequest, ModelSnapshot, request_hash
from gameforge.runtime.model_router.transport import OpenAITransport, StubTransport


def _req(content="hi"):
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content=content)],
        agent_node_id="triage", prompt_version="triage@1",
    )


class _FakeChatCompletions:
    def create(self, **kw):
        class _Msg:  # minimal openai-response shape
            content = "hello from model"
            tool_calls = None
        class _Choice:
            message = _Msg()
            finish_reason = "stop"
        class _Usage:
            def model_dump(self): return {"prompt_tokens": 3, "completion_tokens": 4}
        class _Resp:
            choices = [_Choice()]
            usage = _Usage()
            def model_dump(self): return {"id": "x", "choices": []}
        return _Resp()


class _FakeClient:
    def __init__(self): self.chat = type("C", (), {"completions": _FakeChatCompletions()})()


def test_openai_transport_maps_response():
    t = OpenAITransport(base_url="http://localhost:4141", api_key="sk-x", client=_FakeClient())
    resp = t.complete(_req())
    assert resp.response_normalized == "hello from model"
    assert resp.finish_reason == "stop"
    assert resp.token_usage == {"prompt_tokens": 3, "completion_tokens": 4}


def test_stub_transport_returns_by_request_hash():
    from gameforge.contracts.model_router import ModelResponse
    r = _req()
    stub = StubTransport({request_hash(r): ModelResponse(response_normalized="canned")})
    assert stub.complete(r).response_normalized == "canned"
    assert stub.calls == [r]
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `gameforge/runtime/secrets/env.py`:
```python
"""Read the LLM-gateway key from the environment — never from a committed file."""
from __future__ import annotations

import os


def get_llm_key() -> str:
    key = os.environ.get("GAMEFORGE_LLM_KEY")
    if not key:
        raise RuntimeError(
            "GAMEFORGE_LLM_KEY not set — put it in a gitignored .env; never commit the key."
        )
    return key
```
- [ ] **Step 4: Implement** `gameforge/runtime/model_router/transport.py`:
```python
"""LLM transport — the ONLY module allowed to import an LLM SDK (import-linter).

OpenAITransport talks to the OpenAI-compatible gateway (localhost:4141). The
underlying client is injectable so unit tests exercise response-mapping with a
fake and never touch the network. StubTransport serves canned responses keyed by
request_hash for deterministic router/agent tests.
"""
from __future__ import annotations

import time
from typing import Protocol

import openai  # the one allowed SDK import (import-linter contract)

from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash


class LlmTransport(Protocol):
    def complete(self, req: ModelRequest) -> ModelResponse: ...


class OpenAITransport:
    def __init__(self, base_url: str, api_key: str, client=None) -> None:
        self._client = client or openai.OpenAI(base_url=base_url, api_key=api_key)

    def complete(self, req: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        resp = self._client.chat.completions.create(
            model=req.model_snapshot.model,
            messages=[m.model_dump(exclude_none=True) for m in req.messages],
            **req.params,
        )
        choice = resp.choices[0]
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        return ModelResponse(
            response_normalized=choice.message.content or "",
            raw_response=resp.model_dump() if hasattr(resp, "model_dump") else {},
            latency_ms=int((time.monotonic() - started) * 1000),
            token_usage={k: int(v) for k, v in usage.items() if isinstance(v, int)},
            finish_reason=getattr(choice, "finish_reason", "") or "",
            tool_calls=[tc if isinstance(tc, dict) else tc.model_dump() for tc in tool_calls],
        )


class StubTransport:
    """Deterministic transport for tests: returns canned responses by request_hash."""

    def __init__(self, responses: dict[str, ModelResponse]) -> None:
        self._responses = responses
        self.calls: list[ModelRequest] = []

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        return self._responses[request_hash(req)]
```
> 注：`latency_ms` 不进 cassette 的复现比较（record 存但 REPLAY 只比 `response_normalized`/`tool_calls`）——见 Task 6/8。
- [ ] **Step 5: Run to verify pass** → PASS。
- [ ] **Step 6: Commit** — `git commit -am "feat(runtime/model_router): LlmTransport + OpenAITransport(SDK 唯一落点) + StubTransport + 密钥读取"`

---

## Task 5: runtime — CassetteStore（录/放，flat `cassettes/<hash>.json`）

**Files:**
- Create: `gameforge/runtime/cassette/store.py`
- Test: `tests/runtime/cassette/test_store.py`

**Interfaces:**
- Consumes: `contracts.cassette.{CassetteRecord,CASSETTE_MISS}`, stdlib `json`/`pathlib`.
- Produces: `class CassetteStore(root:str|Path)`：`record(rec:CassetteRecord)->None`（写 `<root>/<request_hash 去 "sha256:" 前缀>.json`）、`replay(request_hash:str)->CassetteRecord|_CassetteMiss`（命中读回、未命中返回 `CASSETTE_MISS`）。`request_hash` 已含 `agent_node_id`，故 flat 布局即 O(1) 唯一键；record 内仍存 `agent_node_id` 供人读/分组。

- [ ] **Step 1: Write failing test** — `tests/runtime/cassette/test_store.py`:
```python
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.runtime.cassette.store import CassetteStore


def _rec(h="sha256:abc"):
    return CassetteRecord(
        request_hash=h, agent_node_id="triage",
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        response=ModelResponse(response_normalized="ok"),
    )


def test_record_then_replay_round_trips(tmp_path):
    store = CassetteStore(tmp_path)
    store.record(_rec())
    got = store.replay("sha256:abc")
    assert got.response.response_normalized == "ok"
    assert got == _rec()


def test_replay_miss_returns_sentinel(tmp_path):
    assert CassetteStore(tmp_path).replay("sha256:nope") is CASSETTE_MISS


def test_record_is_stable_on_disk(tmp_path):
    store = CassetteStore(tmp_path)
    store.record(_rec())
    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert files == ["abc.json"]
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `gameforge/runtime/cassette/store.py`:
```python
"""Cassette store — deterministic record/replay of LLM responses (contract §7).

Flat layout: <root>/<hex>.json where hex = request_hash without the "sha256:"
prefix. request_hash already encodes agent_node_id, so the hash alone is a
unique O(1) key; the record body keeps agent_node_id for human browsing.
"""
from __future__ import annotations

import json
from pathlib import Path

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord, _CassetteMiss


class CassetteStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, request_hash: str) -> Path:
        hex_part = request_hash.split(":", 1)[-1]
        return self._root / f"{hex_part}.json"

    def record(self, rec: CassetteRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(rec.request_hash)
        path.write_text(
            json.dumps(rec.model_dump(), sort_keys=True, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def replay(self, request_hash: str) -> CassetteRecord | _CassetteMiss:
        path = self._path(request_hash)
        if not path.exists():
            return CASSETTE_MISS
        return CassetteRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
```
- [ ] **Step 4: Run to verify pass** → PASS。
- [ ] **Step 5: Commit** — `git commit -am "feat(runtime/cassette): CassetteStore 录/放 (flat request_hash 键, 未命中 sentinel)"`

---

## Task 6: runtime — ModelRouter（RECORD/REPLAY/PASSTHROUGH + retry/quota + 会话内 cache）

**Files:**
- Create: `gameforge/runtime/model_router/router.py`
- Test: `tests/runtime/model_router/test_router.py`

**Interfaces:**
- Consumes: `contracts.model_router.{ModelRequest,ModelResponse,request_hash}`, `contracts.cassette.{CassetteRecord,CASSETTE_MISS}`, `runtime.model_router.transport.LlmTransport`, `runtime.cassette.store.CassetteStore`.
- Produces:
  - `class RouterMode(str, Enum){RECORD,REPLAY,PASSTHROUGH}`。
  - `class CassetteReplayMiss(Exception)`。
  - `class QuotaExceeded(Exception)`。
  - `class ModelRouter(transport, store, mode=REPLAY, max_retries=2, max_calls=None)`：`call(req)->ModelResponse`。REPLAY→`store.replay`，MISS→`CassetteReplayMiss`；RECORD→`transport.complete`(+retry) 后 `store.record`；PASSTHROUGH→仅 `transport.complete`。会话内按 `request_hash` 去重缓存（避免重复实调）。`max_calls` 到顶→`QuotaExceeded`。

- [ ] **Step 1: Write failing test** — `tests/runtime/model_router/test_router.py`:
```python
import pytest
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import (
    CassetteReplayMiss, ModelRouter, QuotaExceeded, RouterMode,
)
from gameforge.runtime.model_router.transport import StubTransport


def _req(content="hi"):
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content=content)],
        agent_node_id="triage", prompt_version="triage@1",
    )


def test_record_then_replay_reproduces(tmp_path):
    req = _req()
    stub = StubTransport({request_hash(req): ModelResponse(response_normalized="live-answer")})
    store = CassetteStore(tmp_path)
    rec_router = ModelRouter(stub, store, mode=RouterMode.RECORD)
    assert rec_router.call(req).response_normalized == "live-answer"

    # REPLAY with a transport that would blow up if called → proves no live call
    class _Boom:
        def complete(self, r): raise AssertionError("REPLAY must not hit transport")
    rep_router = ModelRouter(_Boom(), store, mode=RouterMode.REPLAY)
    assert rep_router.call(req).response_normalized == "live-answer"


def test_replay_miss_raises(tmp_path):
    router = ModelRouter(StubTransport({}), CassetteStore(tmp_path), mode=RouterMode.REPLAY)
    with pytest.raises(CassetteReplayMiss):
        router.call(_req())


def test_session_cache_dedups_live_calls(tmp_path):
    req = _req()
    stub = StubTransport({request_hash(req): ModelResponse(response_normalized="x")})
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD)
    router.call(req)
    router.call(req)
    assert len(stub.calls) == 1  # second call served from session cache


def test_quota_enforced(tmp_path):
    req_a, req_b = _req("a"), _req("b")
    stub = StubTransport({
        request_hash(req_a): ModelResponse(response_normalized="a"),
        request_hash(req_b): ModelResponse(response_normalized="b"),
    })
    router = ModelRouter(stub, CassetteStore(tmp_path), mode=RouterMode.RECORD, max_calls=1)
    router.call(req_a)
    with pytest.raises(QuotaExceeded):
        router.call(req_b)
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `gameforge/runtime/model_router/router.py`:
```python
"""Model Router (contract §7) — RECORD/REPLAY/PASSTHROUGH over a transport + cassette.

REPLAY is the CI/test mode: zero live calls, deterministic. RECORD hits the live
transport and writes cassettes. Reproducibility = same request_hash + REPLAY ->
same ModelResponse (PRD §5.5: 只承诺回放复现).
"""
from __future__ import annotations

from enum import Enum

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.transport import LlmTransport


class RouterMode(str, Enum):
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"


class CassetteReplayMiss(Exception):
    def __init__(self, request_hash: str) -> None:
        super().__init__(f"cassette miss on REPLAY for {request_hash}")
        self.request_hash = request_hash


class QuotaExceeded(Exception):
    pass


class ModelRouter:
    def __init__(
        self,
        transport: LlmTransport,
        store: CassetteStore,
        mode: RouterMode = RouterMode.REPLAY,
        max_retries: int = 2,
        max_calls: int | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._mode = mode
        self._max_retries = max_retries
        self._max_calls = max_calls
        self._live_calls = 0
        self._session_cache: dict[str, ModelResponse] = {}

    def call(self, req: ModelRequest) -> ModelResponse:
        h = request_hash(req)
        if h in self._session_cache:
            return self._session_cache[h]

        if self._mode is RouterMode.REPLAY:
            rec = self._store.replay(h)
            if rec is CASSETTE_MISS:
                raise CassetteReplayMiss(h)
            self._session_cache[h] = rec.response
            return rec.response

        # RECORD / PASSTHROUGH → live transport
        if self._max_calls is not None and self._live_calls >= self._max_calls:
            raise QuotaExceeded(f"live-call quota {self._max_calls} exhausted")
        resp = self._complete_with_retry(req)
        self._live_calls += 1

        if self._mode is RouterMode.RECORD:
            self._store.record(
                CassetteRecord(
                    request_hash=h,
                    agent_node_id=req.agent_node_id,
                    model_snapshot=req.model_snapshot,
                    response=resp,
                )
            )
        self._session_cache[h] = resp
        return resp

    def _complete_with_retry(self, req: ModelRequest) -> ModelResponse:
        last: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                return self._transport.complete(req)
            except Exception as exc:  # transient gateway errors → retry then degrade
                last = exc
        raise RuntimeError(f"transport failed after {self._max_retries + 1} attempts: {last}")
```
- [ ] **Step 4: Run to verify pass** → PASS。
- [ ] **Step 5: Commit** — `git commit -am "feat(runtime/model_router): ModelRouter RECORD/REPLAY/PASSTHROUGH + retry/quota + 会话内去重"`

---

## Task 7: agents — 确定性编排 harness + prompt 注册表

**Files:**
- Create: `gameforge/agents/orchestrator.py`, `gameforge/agents/prompts/registry.py`, `gameforge/agents/prompts/__init__.py`
- Test: `tests/agents/test_orchestrator.py`, `tests/agents/test_prompts.py`

**Interfaces:**
- Consumes: `contracts.agent_io.AgentNodeResult`, `runtime.model_router.router.ModelRouter`.
- Produces:
  - `agents/prompts/registry.py`：`register_prompt(name:str, version:str, template:str)`、`get_prompt(name:str)->tuple[str,str]`（返回 `(version, template)`；未注册→`KeyError`）、`render(name:str, **kw)->tuple[str,str]`（返回 `(version, 填充后的 template)`）。
  - `agents/orchestrator.py`：`class AgentNode(Protocol){node_id:str; run(input, router:ModelRouter)->AgentNodeResult}`、`run_graph(nodes:list[AgentNode], inputs:dict[str,object], router)->list[AgentNodeResult]`（按 `nodes` 顺序确定性执行，`inputs[node.node_id]` 取输入；无并发）。

- [ ] **Step 1: Write failing tests** — `tests/agents/test_prompts.py`:
```python
import pytest
from gameforge.agents.prompts.registry import get_prompt, register_prompt, render


def test_register_and_render():
    register_prompt("triage.system", "triage@1", "Triage {n} findings.")
    assert get_prompt("triage.system") == ("triage@1", "Triage {n} findings.")
    assert render("triage.system", n=3) == ("triage@1", "Triage 3 findings.")


def test_unregistered_raises():
    with pytest.raises(KeyError):
        get_prompt("nope")
```
`tests/agents/test_orchestrator.py`:
```python
from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.agents.orchestrator import run_graph
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport


class _EchoNode:
    node_id = "echo"

    def run(self, input, router):
        req = ModelRequest(
            model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
            messages=[Message(role="user", content=input["text"])],
            agent_node_id=self.node_id, prompt_version="echo@1",
        )
        resp = router.call(req)
        return AgentNodeResult(
            role="triage", model_run_id="run1", request_hashes=[request_hash(req)],
            produced={"answer": resp.response_normalized},
        )


def test_run_graph_is_deterministic_under_replay(tmp_path):
    node = _EchoNode()
    probe_req = ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content="hi")],
        agent_node_id="echo", prompt_version="echo@1",
    )
    stub = StubTransport({request_hash(probe_req): ModelResponse(response_normalized="pong")})
    store = CassetteStore(tmp_path)

    rec = ModelRouter(stub, store, mode=RouterMode.RECORD)
    out1 = run_graph([node], {"echo": {"text": "hi"}}, rec)

    rep = ModelRouter(StubTransport({}), store, mode=RouterMode.REPLAY)
    out2 = run_graph([node], {"echo": {"text": "hi"}}, rep)

    assert out1[0].produced == {"answer": "pong"}
    assert out1[0].model_dump() == out2[0].model_dump()  # replay reproduces byte-identically
```
- [ ] **Step 2: Run to verify failure** → FAIL.
- [ ] **Step 3: Implement** `gameforge/agents/prompts/__init__.py`（一行 docstring）和 `gameforge/agents/prompts/registry.py`:
```python
"""Prompt registry — every prompt carries a prompt_version (进 request_hash + 版本元组)."""
from __future__ import annotations

_PROMPTS: dict[str, tuple[str, str]] = {}


def register_prompt(name: str, version: str, template: str) -> None:
    _PROMPTS[name] = (version, template)


def get_prompt(name: str) -> tuple[str, str]:
    return _PROMPTS[name]  # KeyError if unregistered — fail loud


def render(name: str, **kwargs) -> tuple[str, str]:
    version, template = _PROMPTS[name]
    return version, template.format(**kwargs)
```
- [ ] **Step 4: Implement** `gameforge/agents/orchestrator.py`:
```python
"""Deterministic agent orchestration (决策B: 自研状态机, 不引 LangGraph).

Nodes run in the given order, no concurrency, no hidden state — so a run under
REPLAY reproduces byte-identically. Each node is a typed I/O contract that
reaches the LLM only through ModelRouter (never a direct SDK call).
"""
from __future__ import annotations

from typing import Protocol

from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.runtime.model_router.router import ModelRouter


class AgentNode(Protocol):
    node_id: str

    def run(self, input: object, router: ModelRouter) -> AgentNodeResult: ...


def run_graph(
    nodes: list[AgentNode],
    inputs: dict[str, object],
    router: ModelRouter,
) -> list[AgentNodeResult]:
    return [node.run(inputs[node.node_id], router) for node in nodes]
```
- [ ] **Step 5: Run to verify pass** → PASS。
- [ ] **Step 6: Commit** — `git commit -am "feat(agents): 确定性编排 harness (run_graph) + prompt 注册表 (prompt_version)"`

---

## Task 8: 地基验收 — 录放复现 e2e + 文档收尾

**Files:**
- Test: `tests/agents/test_foundations_acceptance.py`
- Modify: `README.md`（+ M2a-part1 段）, `docs/superpowers/plans/README.md`, memory `gameforge-milestone-progress.md`

**Interfaces:**
- Consumes: 全部 Task 1–7 组件。
- Produces: 地基复现验收锚点——一个玩具 agent 节点经 `RECORD`（stub transport）写 cassette，再经 `REPLAY`（会对实网调用 assert 的哑 transport）**逐位复现** `AgentNodeResult`；证明"同输入同 model_snapshot + REPLAY → 逐步复现"（M2 §16 复现验收的地基部分，无实网）。

- [ ] **Step 1: Write failing acceptance test** — `tests/agents/test_foundations_acceptance.py`:
```python
"""M2a-part1 地基验收：Model Router + Cassette + 编排 的确定性复现 (零实网)。"""
from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.agents.orchestrator import run_graph
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport

_SNAP = ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1")


class _TwoCallNode:
    node_id = "twocall"

    def run(self, input, router):
        hashes = []
        answers = []
        for turn in input["turns"]:
            req = ModelRequest(
                model_snapshot=_SNAP,
                messages=[Message(role="user", content=turn)],
                agent_node_id=self.node_id, prompt_version="twocall@1",
            )
            answers.append(router.call(req).response_normalized)
            hashes.append(request_hash(req))
        return AgentNodeResult(role="triage", model_run_id="r", request_hashes=hashes,
                               produced={"answers": answers})


def _req(text):
    return ModelRequest(model_snapshot=_SNAP, messages=[Message(role="user", content=text)],
                        agent_node_id="twocall", prompt_version="twocall@1")


def test_foundations_record_then_replay_reproduces_byte_identical(tmp_path):
    turns = ["look around", "open the door"]
    canned = {request_hash(_req(t)): ModelResponse(response_normalized=f"did:{t}") for t in turns}
    store = CassetteStore(tmp_path)

    rec_out = run_graph([_TwoCallNode()], {"twocall": {"turns": turns}},
                        ModelRouter(StubTransport(canned), store, mode=RouterMode.RECORD))

    class _NoLive:
        def complete(self, r): raise AssertionError("REPLAY hit the network")

    rep_out = run_graph([_TwoCallNode()], {"twocall": {"turns": turns}},
                        ModelRouter(_NoLive(), store, mode=RouterMode.REPLAY))

    assert rec_out[0].produced == {"answers": ["did:look around", "did:open the door"]}
    assert rec_out[0].model_dump() == rep_out[0].model_dump()
    # every LLM call is a committed cassette file
    assert len(list(tmp_path.glob("*.json"))) == 2
```
- [ ] **Step 2: Run to verify failure** → FAIL（若任一地基缺失）。
- [ ] **Step 3: 实现补齐** — 若测试暴露缺口，回到对应 Task 修复（不应有——本 task 纯集成验收）。跑 `uv run pytest tests/agents/test_foundations_acceptance.py -v` → PASS。
- [ ] **Step 4: 全量门禁** —
```bash
uv run pytest -q
uv run lint-imports
uv run ruff check .
```
Expected：全绿；import-linter **7 契约 KEPT**（含新 "LLM SDK only in runtime.model_router"）；无实网调用。
- [ ] **Step 5: 文档收尾** — `README.md` 加 M2a-part1 段（Model Router/Cassette/编排 地基，交付 vs 延后 part2）；`docs/superpowers/plans/README.md` 记录本 plan；memory `gameforge-milestone-progress.md` 加"M2a-part1 ✅：契约§7 落地 + Router/Cassette/编排 地基 + LLM-SDK-仅限-model_router 契约 + 复现验收(零实网)"。
- [ ] **Step 6: Commit** — `git commit -am "feat(m2a-part1): 契约§7 + Model Router/Cassette + 编排地基 + 录放复现验收 (零实网 LLM)"`

---

## Self-Review

**1. Spec coverage**（对 M2 设计 §3–§5/§7 + 契约 7）：
- 契约 §7 ModelRouter/Cassette/`request_hash` → Task 1,2 ✔
- 6 角色 agent I/O 契约（字段一次定全，Playtest 现定实现延后）→ Task 2 ✔
- LLM SDK 仅限 `runtime.model_router` 依赖契约 → Task 3 ✔
- Model Router RECORD/REPLAY/PASSTHROUGH + retry/quota + 语义缓存（会话内 exact 去重；稳定前缀 prefix-cache 延后 part2）→ Task 4,6 ✔
- Cassette 录/放，CI 只回放 → Task 5,6 ✔
- 自研确定性编排 harness + prompt_version 注册表 → Task 7 ✔
- 复现验收（同输入 + REPLAY 逐位复现，零实网）→ Task 8 ✔
- **延后到 M2a-part2**：Extraction/Triager/Repair Drafter + verifier-guided 修复搜索 + 生成门禁 + Consistency（真实 LLM 语义 + 各自兜底 + Fix Pass Rate ≥70% 验收 + 实网录制 pass）；**延后 M2b**：Playtest + mem-trace + 消融。接口（`agent_io.py` 六角色、`PatchDraft` 等）本 plan 已全定。

**2. Placeholder scan**：每个 code step 有完整可运行代码 + 精确签名；无 TBD/TODO；retry/quota/cache 均给了具体实现而非"add error handling"。

**3. Type consistency**：`ModelRequest`/`ModelResponse`/`request_hash`（T1）被 T2/T4/T5/T6/T7/T8 一致复用；`CassetteRecord`/`CASSETTE_MISS`/`_CassetteMiss`（T2）被 T5/T6 复用；`AgentNodeResult`（T2）被 T7/T8 生产；`LlmTransport`/`StubTransport`（T4）被 T6/T7/T8 注入；`CassetteStore`（T5）被 T6/T7/T8 复用；`ModelRouter`/`RouterMode`（T6）被 T7/T8 复用。`request_hash` 前缀 `"sha256:"` 与 store `_path` 去前缀一致。

**Deferred（接口现定 — 不简化只延后）**：稳定前缀语义缓存的 KG-prefix 切分（part2）；各 agent 的 LLM 语义实现与兜底（part2）；verifier-guided 修复搜索循环（part2）；实网录制 pass + Fix Pass Rate/复现的真实 cassette（part2，先验网关连通）；Playtest/mem-trace/消融（M2b）。
