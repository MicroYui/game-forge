# Pre-M4 Narrative Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents for this plan.

**Goal:** Produce power-complete, replayable seeded-oracle evidence for all four narrative defect classes while keeping every product Finding explicitly `llm-assisted` and `unproven`.

**Architecture:** The current Consistency Agent is upgraded to emit typed hints and to quorum over normalized class/entity/constraint/source-span keys. A source-neutral narrative benchmark package separately owns hidden typed facts, an independent oracle, natural-language rendering, frozen corpora, scoring, and evidence hashes; the Agent sees only natural-language constraints and dialogue. New RECORD runs use `openai/gpt-5.6-sol/pre-m4@1` in a dedicated cassette root, while an explicit legacy path keeps every historical Opus 4.8 cassette byte-identical and replayable.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Hypothesis, stdlib `hashlib`/`json`/`unicodedata`, the existing Model Router/Cassette contracts, and the OpenAI Responses gateway transport.

## Global Constraints

- Read and obey `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, and `docs/superpowers/specs/2026-07-12-pre-m4-lean-closure-design.md` before editing production code.
- TDD is mandatory: every production behavior starts with a focused failing test whose expected failure is observed.
- `spine` remains unchanged by this slice and may never import Agents, benchmark code, source profiles, or an LLM SDK.
- Production consistency code is game-neutral: it consumes arbitrary entity IDs, natural-language constraints, and dialogue; it must not branch on Aureus, Flare, Endless Sky, a fixture path, a case ID, or a benchmark seed.
- The model must never receive hidden typed facts, `is_clean`, ground-truth `defect_class`, target entities, target constraint IDs, target span, or an answer-bearing case ID.
- Formal corpus text must reject taxonomy labels and answer markers, including case-insensitive `TRAIT:`, `SPOILER:`, `CONTRADICTION:`, `UNIQUE-ROLE:`, `character_violation`, `faction_violation`, `uniqueness_violation`, and standalone `defect class`.
- New online evidence uses exactly `ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1")` over `OpenAIResponsesTransport`.
- Historical M2 evidence uses exactly `ModelSnapshot(provider="anthropic", model="claude-opus-4-8", snapshot_tag="m2a@1")`; existing files under `cassettes/` are never rewritten, reformatted, moved, or relabeled.
- All three current perspectives inspect all four narrative classes. Their only difference is method: `constraint_matching`, `causal_world_state`, and `adversarial_falsification`.
- Current quorum identity is normalized `(defect_class, entity_ids, constraint_ids, source_span)`; free-text rationale never participates in equality.
- Every emitted Finding remains `source="llm"`, `oracle_type="llm-assisted"`, and `status="unproven"`; no seeded oracle result can upgrade a product Finding to deterministic or confirmed.
- Positive TP requires the correct class, the exact normalized target entity set, and overlap with the target source span. Wrong class/target/span, partial or total parse failure, fallback, cassette miss, and runner failure remain in the positive denominator.
- Any quorum-surviving hint on a clean case counts as a narrative false positive. Clean parse failures and cassette misses remain in the clean denominator but do not count as hints.
- Development contains exactly 20 positives per class plus 80 clean controls. Verification contains exactly 381 positives per class plus 381 near-equally distributed clean controls; verification counts cannot be reduced or cases replaced after seeing results.
- Generator, renderer, oracle, prompt bundle, model snapshot, perspective order, threshold, rebuttal policy, matcher, both corpus byte hashes, and protocol hash are frozen before verification RECORD.
- Tests and REPLAY perform zero network calls. RECORD requires both `GAMEFORGE_LLM_LIVE=1` and `GAMEFORGE_LLM_KEY`, uses resume, and writes only to `cassettes/narrative/pre-m4-1/`.
- This plan does not implement HED, QA-hours, Cost/Latency aggregation, BenchReport v2, M4 UI, RBAC, approval infrastructure, or a new storage service.
- Every task ends with `git diff --check`; commits contain no AI attribution.

---

### Task 1: Versioned Structured Consistency Contracts

**Files:**
- Modify: `gameforge/contracts/versions.py`
- Modify: `gameforge/contracts/agent_io.py`
- Modify: `tests/contracts/test_agent_io.py`
- Create: `tests/contracts/test_consistency_hint_contract.py`

**Interfaces:**
- Consumes: the existing `AgentNodeResult` and Pydantic v2 validation conventions.
- Produces: `M2_AGENT_IO_SCHEMA_VERSION`, `NarrativeDefectClass`, `NarrativeConstraintInput`, the extended `DialogueNarrativeInput`, and the new `ConsistencyHint` shape.

- [x] **Step 1: Write failing contract tests for the complete hint and input shape**

```python
def test_current_consistency_hint_requires_grounded_structured_identity():
    hint = ConsistencyHint(
        defect_class="spoiler",
        entity_ids=["npc:qi", "secret:white-heron"],
        constraint_ids=["C-reveal-white-heron"],
        span="Qi named the masked envoy before the archive opened.",
        rationale="The line reveals the gated identity before its unlock.",
    )
    assert hint.is_suggestion is True
    assert hint.entity_ids == ["npc:qi", "secret:white-heron"]
    assert "issue" not in hint.model_dump()


def test_suggestion_flag_cannot_be_promoted_to_authoritative():
    with pytest.raises(ValidationError):
        ConsistencyHint(
            defect_class="spoiler",
            entity_ids=["npc:qi"],
            constraint_ids=["C-reveal"],
            span="quoted line",
            rationale="premature reveal",
            is_suggestion=False,
        )


def test_dialogue_input_carries_statements_without_hidden_ground_truth():
    value = DialogueNarrativeInput(
        dialogue="Qi names the envoy before the archive opens.",
        narrative_constraints=[
            NarrativeConstraintInput(
                constraint_id="C-reveal",
                entity_ids=["npc:qi", "secret:envoy"],
                statement="The envoy's identity may be named only after the archive opens.",
            )
        ],
    )
    payload = value.model_dump()
    assert set(payload) == {"dialogue", "narrative_constraints", "narrative_constraint_ids"}
    assert "defect_class" not in repr(payload)
```

Also test that blank strings, duplicate IDs, an unsupported class, extra fields, and empty entity/constraint lists fail validation; ID order is preserved at the contract boundary and normalized only by the quorum layer.

- [x] **Step 2: Run the contract tests and verify RED**

Run: `uv run pytest tests/contracts/test_agent_io.py tests/contracts/test_consistency_hint_contract.py -q`

Expected: failures because `NarrativeConstraintInput`, `M2_AGENT_IO_SCHEMA_VERSION`, and the new hint fields do not exist.

- [x] **Step 3: Implement the versioned models without deleting the legacy input field**

Use these exact public shapes:

```python
M2_AGENT_IO_SCHEMA_VERSION = "agent-io@1"
AGENT_IO_SCHEMA_VERSION = "agent-io@2"

NarrativeDefectClass = Literal[
    "character_violation",
    "spoiler",
    "faction_violation",
    "uniqueness_violation",
]


class NarrativeConstraintInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    constraint_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    entity_ids: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]]
    statement: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DialogueNarrativeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialogue: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    narrative_constraints: list[NarrativeConstraintInput] = Field(default_factory=list)
    narrative_constraint_ids: list[str] = Field(default_factory=list)


class ConsistencyHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    defect_class: NarrativeDefectClass
    entity_ids: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]]
    constraint_ids: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]]
    span: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    is_suggestion: Literal[True] = True
```

Field validators require each ID list to be nonempty and duplicate-free. A model validator rejects a `DialogueNarrativeInput` whose structured constraint IDs collide or whose legacy `narrative_constraint_ids` contain duplicates. Do not infer or store any target/answer field on the Agent input.

- [x] **Step 4: Run the contract tests and verify GREEN**

Run: `uv run pytest tests/contracts/test_agent_io.py tests/contracts/test_consistency_hint_contract.py -q`

Expected: all tests pass and `AgentNodeResult(...).agent_io_schema_version == "agent-io@2"`.

- [x] **Step 5: Commit the contract change**

```bash
git add gameforge/contracts/versions.py gameforge/contracts/agent_io.py tests/contracts/test_agent_io.py tests/contracts/test_consistency_hint_contract.py
git diff --cached --check
git commit -m "feat(agents): type narrative consistency hints"
```

---

### Task 2: Source-Grounded Hint Normalization and Quorum Keys

**Files:**
- Create: `gameforge/agents/consistency/normalization.py`
- Create: `tests/agents/test_consistency_normalization.py`

**Interfaces:**
- Consumes: `ConsistencyHint`, the exact dialogue string, and the allowed entity/constraint ID sets from `DialogueNarrativeInput`.
- Produces: `SourceSpan`, `HintKey`, `NormalizedHint`, `normalize_hint()`, and `tally_normalized_hints()`.

- [x] **Step 1: Write failing tests for semantic identity without rationale identity**

```python
def test_same_grounded_hint_with_different_rationale_and_id_order_shares_one_key():
    dialogue = "Qi lowered her voice. The sealed envoy is Mara. The bells continued."
    first = hint(
        entity_ids=["npc:qi", "secret:mara"],
        constraint_ids=["C-gate", "C-identity"],
        span="The sealed envoy is Mara.",
        rationale="names the envoy",
    )
    second = hint(
        entity_ids=["secret:mara", "npc:qi"],
        constraint_ids=["C-identity", "C-gate"],
        span="sealed envoy is Mara",
        rationale="reveals a gated identity",
    )
    a = normalize_hint(dialogue, first, ALLOWED_ENTITIES, ALLOWED_CONSTRAINTS)
    b = normalize_hint(dialogue, second, ALLOWED_ENTITIES, ALLOWED_CONSTRAINTS)
    assert a is not None and b is not None
    assert a.key == b.key
    assert a.key.span == SourceSpan(start=22, end=47)


def test_class_entity_constraint_or_sentence_difference_prevents_false_quorum():
    values = [
        normalized(defect_class="spoiler"),
        normalized(defect_class="character_violation"),
        normalized(entity_ids=["npc:other"]),
        normalized(constraint_ids=["C-other"]),
        normalized(span="The bells continued."),
    ]
    counts, _ = tally_normalized_hints([[item] for item in values])
    assert max(counts.values()) == 1
```

Also test Unicode NFKC normalization, whitespace/case-insensitive quote lookup, sentence expansion at `.?!。！？\n`, rejection of an absent or ambiguous quote, rejection of invented IDs, and at-most-one vote per perspective for a key.

- [x] **Step 2: Run the normalization tests and verify RED**

Run: `uv run pytest tests/agents/test_consistency_normalization.py -q`

Expected: import failure for `gameforge.agents.consistency.normalization`.

- [x] **Step 3: Implement deterministic quote location and canonical keys**

Implement the exact immutable types:

```python
@dataclass(frozen=True, order=True)
class SourceSpan:
    start: int
    end: int


@dataclass(frozen=True, order=True)
class HintKey:
    defect_class: str
    entity_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    span: SourceSpan


@dataclass(frozen=True)
class NormalizedHint:
    key: HintKey
    hint: ConsistencyHint
```

`normalize_hint()` performs these operations in order:

1. Reject an entity or constraint ID outside the supplied allowed sets.
2. NFKC-normalize and casefold dialogue and quote while collapsing every whitespace run to one ASCII space; keep a mapping from each normalized character back to its original dialogue index.
3. Require exactly one normalized quote occurrence. Zero or multiple occurrences return `None`.
4. Convert the occurrence to original offsets, then expand to the containing sentence bounded by `.?!。！？\n` or the dialogue ends; trim surrounding whitespace.
5. Build a key from the exact class, sorted unique entity IDs, sorted unique constraint IDs, and canonical sentence offsets.
6. Replace the outward hint span with the exact `dialogue[start:end]`, retain the first rationale verbatim, and force `is_suggestion=True`.

`tally_normalized_hints()` deduplicates within each perspective before counting and retains first-seen perspective/key order for deterministic output.

- [x] **Step 4: Run normalization tests and verify GREEN**

Run: `uv run pytest tests/agents/test_consistency_normalization.py -q`

Expected: all tests pass.

- [x] **Step 5: Commit the matcher primitive**

```bash
git add gameforge/agents/consistency/normalization.py tests/agents/test_consistency_normalization.py
git diff --cached --check
git commit -m "feat(agents): normalize grounded narrative hint identity"
```

---

### Task 3: Four-Class Consistency Agent and Frozen M2 Replay

**Files:**
- Create: `gameforge/agents/consistency/legacy.py`
- Modify: `gameforge/agents/consistency/assistant.py`
- Modify: `gameforge/agents/consistency/checker.py`
- Modify: `gameforge/agents/prompts/library.py`
- Modify: `gameforge/agents/harness.py`
- Modify: `tests/agents/test_consistency.py`
- Modify: `tests/agents/test_consistency_perspective.py`
- Modify: `tests/agents/test_part2_acceptance.py`
- Create: `tests/agents/test_consistency_legacy_replay.py`

**Interfaces:**
- Consumes: Task 1 contracts, Task 2 normalization, Model Router `call_model()`, current prompt registry, and historical M2 cassettes.
- Produces: `ConsistencyAssistant.run()` for `consistency@2`, `ConsistencyAssistant.run_legacy_m2()` for byte-identical `consistency@1` replay, and class-specific `Finding` conversion.

- [x] **Step 1: Rewrite the focused tests against structured outputs and three method perspectives**

Use this shared valid response shape in the current tests:

```python
_HINT = {
    "defect_class": "spoiler",
    "entity_ids": ["npc:qi", "secret:warden"],
    "constraint_ids": ["C-warden-reveal"],
    "span": "Qi says the Warden is Mara.",
    "rationale": "The line names the gated identity before the reveal point.",
}
```

Script responses under exactly these variants:

```text
consistency@2#p_constraint_matching
consistency@2#p_causal_world_state
consistency@2#p_adversarial_falsification
consistency@2#r_constraint_matching
consistency@2#r_causal_world_state
consistency@2#r_adversarial_falsification
```

Tests must prove:

- two differently worded rationales with the same normalized key pass a 2-of-3 quorum;
- same span with a wrong class, entity, or constraint does not contribute to that quorum;
- one malformed perspective is recorded but does not crash or erase two valid votes;
- all three malformed first-round responses produce empty hints and `fallback_taken=True`;
- optional rebuttal can only affirm an already-disputed normalized key;
- `rebut=False` makes exactly three calls and is the benchmark mode;
- the three system prompts each name all four classes and differ by reasoning method, not assigned class;
- `ConsistencyChecker` emits the hint's real defect class, exact entity list, primary constraint ID, full constraint IDs and quoted span in evidence, rationale as message, and strict llm-assisted/unproven partitioning.

- [x] **Step 2: Add the historical replay test and verify the current code is GREEN before refactoring**

```python
def test_m2_opus_consistency_cassettes_replay_without_rewrite():
    dialogue = Path("scenarios/agents/dialogue.txt").read_text(encoding="utf-8")
    constraints = Constraint.from_yaml(
        Path("scenarios/agents/narrative.yaml").read_text(encoding="utf-8")
    )
    result = ConsistencyAssistant().run_legacy_m2(
        DialogueNarrativeInput(
            dialogue=dialogue,
            narrative_constraint_ids=[item.id for item in constraints],
        ),
        historical_replay_router(),
    )
    assert result.agent_io_schema_version == "agent-io@1"
    assert result.request_hashes == [
        "sha256:2505227517ccf16feee1e234803c82d68bcb08760ee5343884a6179d4a19f98c",
        "sha256:f8104d12520d9ca612f9830bdfd7ed4fd9b88bca15e014221e4a94db4a06a10e",
        "sha256:7a6c972043a623a804660d3dec00ccf36dca28365d64565dea8e80ec89ab8068",
        "sha256:4487a0f2bb67077d63807a29a469a1435ba46cb9185f84b9c8e4d62030fae641",
        "sha256:6ad2d900d55ba8b79f2fc3b8e073140abaa589b03b3c8f6e746c1e6e8d06f9ed",
        "sha256:c3f7299b8e33851fc5c5d745d1207be046c45214b23620d888961d266069b63f",
    ]
    assert result.fallback_taken is False
    assert result.produced["hints"]
```

Before editing, run `git hash-object cassettes/*.json | sort > /tmp/gameforge-m2-cassettes.before`.

- [x] **Step 3: Run current-agent tests and verify RED**

Run: `uv run pytest tests/agents/test_consistency.py tests/agents/test_consistency_perspective.py tests/agents/test_part2_acceptance.py tests/agents/test_consistency_legacy_replay.py -q`

Expected: current-path failures because the prompts and parser still use `(span, issue)`, plus a missing `run_legacy_m2()` method.

- [x] **Step 4: Preserve the old implementation as an explicit compatibility path**

Move the existing @1 `(span, issue)` parser, temporal/identity/spoiler perspectives, tally, and rebuttal behavior into `legacy.py` without changing its system strings, user prompt bytes, prompt versions, parameter defaults, ordering, or request construction. Register the old strings under `consistency.legacy.*` names but continue passing the exact `consistency@1#p_*` and `consistency@1#r_*` version strings to `call_model()`.

`ConsistencyAssistant.run_legacy_m2()` delegates to that frozen implementation and explicitly sets `agent_io_schema_version=M2_AGENT_IO_SCHEMA_VERSION`. Change `agents.harness._record_agent_samples()` to call only `run_legacy_m2()` with `historical_replay_router()`; it must never attempt to record new @2 data into the root M2 cassette set.

- [x] **Step 5: Register the current prompt bundle**

The `consistency@2` base prompt must require only a JSON array with these keys:

```text
defect_class: one of character_violation, spoiler, faction_violation, uniqueness_violation
entity_ids: every entity ID named by the violated constraint, copied exactly
constraint_ids: every violated constraint ID, copied exactly
span: an exact quote from one problematic dialogue sentence
rationale: concise reasoning grounded in the supplied constraint and quote
```

Every method prompt reviews all four classes:

- `constraint_matching`: compare each sentence directly against every supplied rule and report only explicit conflicts.
- `causal_world_state`: reconstruct character state, reveal stage, faction relation, and cardinality before testing every rule.
- `adversarial_falsification`: try to explain each suspicious line consistently first; report it only when the strongest alternative interpretation still violates a rule.

The rebuttal variants receive full structured disputed hints and may return only an exact subset of those identities. They do not introduce a new hint.

- [x] **Step 6: Implement the current assistant over normalized keys**

`ConsistencyAssistant.run()` gains keyword arguments:

```python
def run(
    self,
    input: object,
    router: ModelRouter,
    *,
    perspectives: tuple[str, ...] = CURRENT_PERSPECTIVES,
    threshold: int = 2,
    rebut: bool = True,
    model_snapshot: ModelSnapshot | None = None,
) -> AgentNodeResult: ...
```

Build the user prompt from numbered structured constraints and the dialogue. Do not serialize any other `NarrativeCase` field. Parse the top-level JSON array, validate each item through `ConsistencyHint`, reject invented IDs and unlocatable spans through `normalize_hint()`, then tally normalized keys. `produced` contains deterministic diagnostics:

```python
{
    "hints": [hint.model_dump() for hint in kept],
    "perspectives": [
        {
            "name": name,
            "request_hash": request_hash,
            "parse_ok": bool,
            "raw_items": int,
            "accepted_items": int,
        }
    ],
    "threshold": threshold,
    "matcher_version": "narrative-span@1",
    "rebuttal_enabled": rebut,
}
```

A top-level parse failure yields an empty vote for that perspective. Invalid individual items are dropped and counted in `raw_items - accepted_items`. `fallback_taken` is true only when every first-round perspective has `parse_ok=False`; empty but valid arrays are not fallback.

- [x] **Step 7: Map structured hints to complete llm-assisted Findings**

For every kept hint, construct:

```python
Finding(
    id=f"{result.model_run_id}#{i}",
    source="llm",
    producer_id="consistency",
    producer_run_id=result.model_run_id,
    oracle_type="llm-assisted",
    defect_class=hint["defect_class"],
    severity="major",
    snapshot_id=snapshot.snapshot_id,
    entities=hint["entity_ids"],
    constraint_id=hint["constraint_ids"][0],
    evidence={
        "span": hint["span"],
        "rationale": hint["rationale"],
        "constraint_ids": hint["constraint_ids"],
    },
    status="unproven",
    message=hint["rationale"],
)
```

Never read benchmark facts or ground truth in `ConsistencyChecker`.

- [x] **Step 8: Run all current and legacy consistency tests**

Run: `uv run pytest tests/agents/test_consistency.py tests/agents/test_consistency_perspective.py tests/agents/test_part2_acceptance.py tests/agents/test_consistency_legacy_replay.py tests/agents/test_model_recording_policy.py -q`

Expected: all tests pass with no live network.

Then run:

```bash
git hash-object cassettes/*.json | sort > /tmp/gameforge-m2-cassettes.after
cmp /tmp/gameforge-m2-cassettes.before /tmp/gameforge-m2-cassettes.after
git diff --exit-code -- cassettes
```

Expected: `cmp` and `git diff` both exit 0.

- [x] **Step 9: Commit the Agent upgrade and compatibility path**

```bash
git add gameforge/agents/consistency gameforge/agents/prompts/library.py gameforge/agents/harness.py tests/agents
git diff --cached --check
git commit -m "feat(agents): ground narrative quorum in typed hints"
```

---

### Task 4: Hidden Typed Facts and Independent Narrative Oracle

**Files:**
- Create: `gameforge/bench/narrative/__init__.py`
- Create: `gameforge/bench/narrative/contracts.py`
- Create: `gameforge/bench/narrative/oracle.py`
- Create: `tests/bench/narrative/__init__.py`
- Create: `tests/bench/narrative/test_contracts.py`
- Create: `tests/bench/narrative/test_oracle.py`

**Interfaces:**
- Consumes: `DefectClass`, `NarrativeConstraintInput`, and canonical JSON.
- Produces: discriminated narrative fact models, `TargetSpan`, `NarrativeCase`, `OracleViolation`, `evaluate_facts()`, `seal_case()`, and canonical case bytes.

- [ ] **Step 1: Write failing tests for typed positive and clean worlds**

```python
@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (character_world(violating=True), DefectClass.character_violation),
        (spoiler_world(reveal_stage=1, allowed_stage=4), DefectClass.spoiler),
        (faction_world(cooperating=True, hostile=True), DefectClass.faction_violation),
        (unique_world(limit=1, holders=2), DefectClass.uniqueness_violation),
    ],
)
def test_oracle_derives_each_class_from_typed_facts(facts, expected):
    violations = evaluate_facts(facts)
    assert [item.defect_class for item in violations] == [expected]


@pytest.mark.parametrize(
    "facts",
    [
        character_world(violating=False),
        spoiler_world(reveal_stage=4, allowed_stage=4),
        faction_world(cooperating=False, hostile=True),
        unique_world(limit=1, holders=1),
    ],
)
def test_clean_worlds_have_no_oracle_violation(facts):
    assert evaluate_facts(facts) == ()
```

Also test strict extra-field rejection, invalid cross-fact references, duplicate fact/constraint IDs, target-span byte binding, `is_clean`/ground-truth consistency, case self-hash tamper detection, and that a positive case has exactly one oracle violation.

- [ ] **Step 2: Run the contract/oracle tests and verify RED**

Run: `uv run pytest tests/bench/narrative/test_contracts.py tests/bench/narrative/test_oracle.py -q`

Expected: collection fails because the narrative benchmark package does not exist.

- [ ] **Step 3: Implement strict discriminated fact models**

Use frozen, `extra="forbid"` Pydantic models with these predicates and fields:

```text
TraitFact:        kind="trait", fact_id, entity_id, trait_id
ActionFact:       kind="action", fact_id, entity_id, action_id, violates_trait_fact_id?
RevealGateFact:   kind="reveal_gate", fact_id, secret_id, min_stage
RevealFact:       kind="reveal", fact_id, speaker_id, secret_id, stage
MembershipFact:  kind="membership", fact_id, entity_id, faction_id
HostilityFact:   kind="hostility", fact_id, left_faction_id, right_faction_id
CooperationFact: kind="cooperation", fact_id, left_entity_id, right_entity_id
RoleLimitFact:   kind="role_limit", fact_id, role_id, max_holders
RoleHolderFact:  kind="role_holder", fact_id, role_id, entity_id
```

`NarrativeConstraint` stores `constraint_id`, sorted unique `entity_ids`, a nonempty natural-language `statement`, and hidden `source_fact_ids`. `to_agent_input(case)` converts only `constraint_id`, `entity_ids`, and `statement` to `NarrativeConstraintInput`; it never emits source facts or targets.

`TargetSpan` stores `start`, `end`, `text`, and `fact_id`. `NarrativeCase` stores exactly the approved fields plus version/hash bindings:

```text
schema_version="narrative-case@1", case_id, generator_version,
renderer_version, oracle_version, seed, split, facts, constraints, dialogue,
is_clean, defect_class?, target_entities, target_constraint_ids,
target_span?, case_sha256
```

- [ ] **Step 4: Implement the independent oracle over facts only**

`evaluate_facts()` must not import or inspect the renderer, prompt library, Agent, dialogue text, or target span. It derives violations by these exact rules:

1. `ActionFact.violates_trait_fact_id` references a `TraitFact` for the same entity -> `character_violation`.
2. `RevealFact.stage < matching RevealGateFact.min_stage` -> `spoiler`.
3. A `CooperationFact` whose actors belong to the two sides of a `HostilityFact` -> `faction_violation`.
4. Distinct `RoleHolderFact.entity_id` count greater than `RoleLimitFact.max_holders` -> `uniqueness_violation`.

Each `OracleViolation` carries the causing event fact IDs, target entity IDs, and source baseline fact IDs. Construction of `NarrativeCase` maps source baseline facts to constraint IDs, and causing event facts to renderer spans; this join is the only place oracle and renderer outputs meet.

- [ ] **Step 5: Run the contract/oracle tests and verify GREEN**

Run: `uv run pytest tests/bench/narrative/test_contracts.py tests/bench/narrative/test_oracle.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit hidden facts and oracle**

```bash
git add gameforge/bench/narrative tests/bench/narrative
git diff --cached --check
git commit -m "feat(bench): define typed narrative facts and oracle"
```

---

### Task 5: Independent Renderer and Power-Complete Frozen Corpora

**Files:**
- Create: `gameforge/bench/narrative/renderer.py`
- Create: `gameforge/bench/narrative/generator.py`
- Create: `gameforge/bench/narrative/corpus.py`
- Modify: `gameforge/bench/inject.py`
- Modify: `tests/bench/test_inject_narrative.py`
- Create: `tests/bench/narrative/test_renderer.py`
- Create: `tests/bench/narrative/test_generator.py`
- Create: `tests/bench/narrative/test_corpus.py`
- Generate: `scenarios/narrative_bench/development.jsonl`
- Generate: `scenarios/narrative_bench/verification.jsonl`
- Generate: `scenarios/narrative_bench/corpus-manifest.json`

**Interfaces:**
- Consumes: Task 4 fact/case contracts and deterministic seeds.
- Produces: `RenderedNarrative`, `render_facts()`, `generate_case()`, `build_corpora()`, `load_cases()`, and a hash-bound frozen corpus manifest.

- [ ] **Step 1: Write failing renderer tests that prove natural text and exact spans**

```python
def test_renderer_returns_exact_source_spans_without_answer_markers():
    rendered = render_facts(character_world(violating=True), render_seed=11)
    action = rendered.spans_by_fact_id["fact:action"]
    assert rendered.dialogue[action.start:action.end] == action.text
    assert action.text
    assert not ANSWER_MARKER.search(rendered.dialogue)


def test_renderer_does_not_import_or_call_the_oracle():
    tree = ast.parse(Path(renderer.__file__).read_text())
    imports = imported_modules(tree)
    assert "gameforge.bench.narrative.oracle" not in imports
```

Property tests render every supported fact combination across LF paragraphs, multi-sentence dialogue, Unicode names, explicit and implicit phrasing, distractors, and two or more entities. Every recorded span must slice back to its exact text.

- [ ] **Step 2: Run renderer tests and verify RED**

Run: `uv run pytest tests/bench/narrative/test_renderer.py -q`

Expected: import failure for the missing renderer.

- [ ] **Step 3: Implement renderer-owned phrase tables and source maps**

The renderer owns phrase tables keyed only by semantic values such as `trait_id`, `action_id`, stage label, faction posture, cooperation action, and role label. It does not receive `DefectClass`, `is_clean`, an oracle result, or a target. It renders baseline constraint context, one event sentence, and 1-3 seeded distractor sentences, returning the exact span for every rendered event fact.

Use at least these independent variation axes in every class: four setting packs, eight name sets, four baseline paraphrases, four event paraphrases, explicit/implicit surface form, target-first/target-last order, one/two distractor constraints, and two/multi-entity dialogue. Setting data is plain names and locations; no production Agent branch depends on a setting ID.

- [ ] **Step 4: Write failing generator/corpus tests for exact counts and hidden-answer isolation**

```python
def test_corpus_counts_are_exact_and_balanced():
    development = build_corpus("development")
    verification = build_corpus("verification")
    assert positive_counts(development) == {dc: 20 for dc in NARRATIVE_CLASSES}
    assert sum(case.is_clean for case in development) == 80
    assert positive_counts(verification) == {dc: 381 for dc in NARRATIVE_CLASSES}
    clean_by_family = clean_family_counts(verification)
    assert sum(clean_by_family.values()) == 381
    assert max(clean_by_family.values()) - min(clean_by_family.values()) <= 1


def test_model_payload_never_contains_hidden_answer_fields():
    case = generate_case(
        split="verification",
        defect_class=DefectClass.spoiler,
        is_clean=False,
        seed=91,
        case_id="nv-000091",
    )
    payload = to_agent_input(case).model_dump_json()
    assert case.defect_class.value not in payload
    assert case.case_id not in payload
    assert "target_span" not in payload
    assert "is_clean" not in payload
```

Also assert same inputs produce byte-identical cases, different seeds vary the text, every positive has exactly one correct oracle violation, every clean has zero, every prompt payload is unique across a split, no formal dialogue/constraint contains an answer marker, and development/verification seed domains are disjoint.

- [ ] **Step 5: Implement deterministic case and corpus construction**

`generate_case()` performs this sequence:

1. Derive a private `random.Random` seed from SHA-256 of `(generator_version, split, defect_class, is_clean, seed)`.
2. Build hidden facts and natural-language constraints without rendering.
3. Evaluate the independent oracle.
4. Render facts with a separately derived renderer seed.
5. For positives, require exactly one violation and bind its causing event fact to the renderer span; for clean cases, require zero violations and store no target.
6. Reject answer markers, repeated prompt payloads, invalid span slicing, or a target whose entities/constraints do not match the oracle.
7. Seal the case hash over canonical JSON excluding `case_sha256`.

`build_corpus("development")` creates 20 positives per class and 20 clean counterparts per family. `build_corpus("verification")` creates 381 positives per class and clean family counts `(96, 95, 95, 95)` in enum order. Seeds are derived from stable SHA-256 labels, not Python `hash()`.

Write one canonical JSON object per line, sorted by opaque case ID, with a final newline. `corpus-manifest.json` binds generator/renderer/oracle versions, each file's byte SHA-256, per-class positive counts, per-family clean counts, and its own `manifest_sha256`.

- [ ] **Step 6: Replace answer-marked legacy narrative injectors with generator delegation**

Keep the public `inject(base, narrative_class, seed)` API. For a narrative class, use the already-derived stable RNG to choose a development case seed, call `generate_case(..., is_clean=False)`, convert through `to_agent_input()`, and populate legacy `GroundTruth` from the case's class/entities. Delete all answer-bearing marker templates from `inject.py`; deterministic/simulation injectors remain untouched.

- [ ] **Step 7: Generate and validate both frozen corpora**

Run:

```bash
uv run python -m gameforge.bench.narrative.corpus --write scenarios/narrative_bench
uv run pytest tests/bench/narrative/test_renderer.py tests/bench/narrative/test_generator.py tests/bench/narrative/test_corpus.py tests/bench/test_inject_narrative.py -q
```

Expected: 160 development cases, 1,905 verification cases, exact manifest counts, unique model payloads, and no answer-marker match.

- [ ] **Step 8: Commit the renderer, generator, and frozen inputs**

```bash
git add gameforge/bench/narrative gameforge/bench/inject.py tests/bench/narrative tests/bench/test_inject_narrative.py scenarios/narrative_bench
git diff --cached --check
git commit -m "data(bench): freeze power-complete narrative corpora"
```

---

### Task 6: Narrative Outcome, Scoring, and Evidence Contracts

**Files:**
- Create: `gameforge/bench/narrative/evidence.py`
- Create: `gameforge/bench/narrative/score.py`
- Create: `tests/bench/narrative/test_evidence.py`
- Create: `tests/bench/narrative/test_score.py`

**Interfaces:**
- Consumes: frozen `NarrativeCase` values, current structured hints, Agent diagnostics, and `wilson_ci()`.
- Produces: `NarrativeCaseOutcome`, `NarrativeClassMetric`, `NarrativeFpMetric`, `NarrativeEvidenceManifest`, `span_overlaps()`, `score_case()`, and `score_outcomes()`.

- [ ] **Step 1: Write failing tests for the exact denominator semantics**

```python
def test_positive_tp_requires_class_exact_entity_set_and_span_overlap():
    case = positive_case()
    assert score_case(case, [correct_hint()]).detected is True
    assert score_case(case, [hint(defect_class="faction_violation")]).detected is False
    assert score_case(case, [hint(entity_ids=["npc:other"])]).detected is False
    assert score_case(case, [hint(span="An unrelated sentence.")]).detected is False


@pytest.mark.parametrize("status", ["fallback", "cassette_miss", "runner_error"])
def test_execution_failures_remain_positive_misses(status):
    outcome = score_case(positive_case(), [], status=status)
    metric = score_outcomes([outcome], [positive_case()]).by_class[0]
    assert metric.n == 1
    assert metric.k == 0


def test_any_surviving_hint_on_clean_is_one_false_positive_case():
    clean = clean_case()
    outcome = score_case(clean, [wrong_but_structured_hint()])
    assert outcome.false_positive is True
```

Also test partial parse failure remains an evaluated case, several hints on one clean case count once, extra wrong hints do not erase a correct positive TP, quote intervals are derived from exact dialogue offsets, and a manifest rejects missing/duplicate cases or tampered derived metrics.

- [ ] **Step 2: Run scoring/evidence tests and verify RED**

Run: `uv run pytest tests/bench/narrative/test_evidence.py tests/bench/narrative/test_score.py -q`

Expected: import failures for the missing modules.

- [ ] **Step 3: Implement immutable outcome and metric models**

Use these status values and required fields:

```text
NarrativeCaseOutcome:
  case_id, case_sha256, protocol_sha256,
  status = evaluated | partial_parse_failure | fallback | cassette_miss | runner_error,
  request_hashes, parse_failures, invalid_hint_items, hints,
  detected, false_positive, matched_hint_indexes, failure_reason?, outcome_sha256

NarrativeClassMetric:
  defect_class, split, n, k, rate, ci_low, ci_high, ci_method="wilson95"

NarrativeFpMetric:
  split, n, count, rate, ci_low, ci_high, ci_method="wilson95"
```

`NarrativeEvidenceManifest` stores `schema_version="narrative-evidence@1"`, `split`, `protocol_sha256`, `corpus_manifest_sha256`, the exact `model_snapshot`, ordered outcomes, development or verification metrics, clean FP, and a self-hash. Its validator requires the stored model snapshot to equal the protocol snapshot, reloads outcome fields, groups cases by ground truth, recomputes every `n/k/count/rate/Wilson95`, and rejects a denominator that does not equal the frozen corpus.

- [ ] **Step 4: Implement score rules without consulting model rationale**

`span_overlaps()` locates the hint's exact canonical sentence in `case.dialogue` and applies half-open interval overlap against `case.target_span`: `max(a.start, b.start) < min(a.end, b.end)`. A positive hint matches only when:

```python
hint.defect_class == case.defect_class.value
set(hint.entity_ids) == set(case.target_entities)
span_overlaps(case.dialogue, hint.span, case.target_span)
```

Constraint IDs are retained as a diagnostic `constraint_match` but are not an additional TP condition, matching the approved class/entity/span definition. Rationale is never scored. A positive case is `detected=any(matches)`; a clean case is `false_positive=bool(hints)` regardless of hint class.

- [ ] **Step 5: Run scoring/evidence tests and verify GREEN**

Run: `uv run pytest tests/bench/narrative/test_evidence.py tests/bench/narrative/test_score.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit scoring and evidence contracts**

```bash
git add gameforge/bench/narrative/evidence.py gameforge/bench/narrative/score.py tests/bench/narrative/test_evidence.py tests/bench/narrative/test_score.py
git diff --cached --check
git commit -m "feat(bench): score narrative evidence without dropped failures"
```

---

### Task 7: GPT-5.6 Record/Replay Harness and Protocol Freeze

**Files:**
- Create: `gameforge/bench/narrative/protocol.py`
- Create: `gameforge/bench/narrative/harness.py`
- Create: `tests/bench/narrative/test_protocol.py`
- Create: `tests/bench/narrative/test_harness.py`

**Interfaces:**
- Consumes: frozen corpora, `ConsistencyAssistant`, Task 6 scoring, Model Router, CassetteStore, `DEFAULT_SNAPSHOT`, and the OpenAI Responses transport.
- Produces: `NarrativeProtocol`, `prompt_bundle_sha256()`, `record_router()`, `replay_router()`, `run_cases()`, `build_evidence()`, and a gated CLI.

- [ ] **Step 1: Write failing model-policy and freeze tests**

```python
def test_narrative_record_router_uses_only_gpt56_responses(monkeypatch, tmp_path):
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "test-key")
    router = record_router(tmp_path)
    assert router.default_model_snapshot == ModelSnapshot(
        provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1"
    )
    assert isinstance(router._transport, OpenAIResponsesTransport)


def test_verification_refuses_any_protocol_or_corpus_drift(tmp_path):
    protocol = sealed_protocol()
    assert_verification_ready(protocol, frozen_corpus_manifest())
    changed = protocol.model_copy(update={"threshold": 3})
    with pytest.raises(ValueError, match="protocol_sha256"):
        assert_verification_ready(changed, frozen_corpus_manifest())


def test_replay_miss_becomes_an_outcome_instead_of_aborting_denominator(tmp_path):
    outcomes = run_cases([positive_case()], replay_router(tmp_path), protocol())
    assert outcomes[0].status == "cassette_miss"
    assert outcomes[0].detected is False
```

Also test RECORD refuses without `GAMEFORGE_LLM_LIVE=1`, REPLAY transport cannot call the network, each current case is invoked with three methods/threshold 2/rebuttal false, the protocol rejects the Opus snapshot, prompt bundle drift changes the protocol hash, and outcome order follows case ID rather than completion timing.

- [ ] **Step 2: Run protocol/harness tests and verify RED**

Run: `uv run pytest tests/bench/narrative/test_protocol.py tests/bench/narrative/test_harness.py -q`

Expected: import failures for the missing protocol and harness.

- [ ] **Step 3: Implement a small hash-bound protocol model**

`NarrativeProtocol` has exactly:

```text
schema_version="narrative-protocol@1"
generator_version, renderer_version, oracle_version
development_corpus_sha256, verification_corpus_sha256
prompt_version: StableId  # one shared registered version; initially "consistency@2"
prompt_bundle_sha256
model_snapshot={openai, gpt-5.6-sol, pre-m4@1}
perspectives=(constraint_matching, causal_world_state, adversarial_falsification)
threshold=2
rebuttal_enabled=false
matcher_version="narrative-span@1"
frozen=true
protocol_sha256
```

`prompt_bundle_sha256()` hashes the names, versions, and full text of the current base, three method, and three rebuttal prompts in sorted name order. The protocol's `prompt_version` must equal the single version returned for all seven current prompts; it starts at `consistency@2` and is bumped atomically if development tuning changes prompt text. `seal_protocol()` computes the self-hash only after corpus hashes and prompt bundle are known. `assert_verification_ready()` recomputes every version/content hash from the checked-out code and frozen files and fails closed on drift; it has no human approval or signature state.

- [ ] **Step 4: Implement dedicated routers and fail-closed live gating**

`record_router()` mirrors the proven repair harness policy but writes only to `cassettes/narrative/pre-m4-1/`, sets `resume=True`, `max_retries=8`, `retry_backoff_s=3.0`, and `default_model_snapshot=DEFAULT_SNAPSHOT`. `replay_router()` uses a transport whose `complete()` raises immediately, `RouterMode.REPLAY`, the same cassette root, and the same GPT snapshot.

No narrative harness function imports `AnthropicMessagesTransport` or chooses a model from a case. The CLI checks `GAMEFORGE_LLM_LIVE=1` and the key before constructing the live transport.

- [ ] **Step 5: Implement deterministic case execution and evidence writing**

For every case, call:

```python
assistant.run(
    to_agent_input(case),
    router,
    perspectives=protocol.perspectives,
    threshold=protocol.threshold,
    rebut=protocol.rebuttal_enabled,
    model_snapshot=protocol.model_snapshot,
)
```

Catch `CassetteReplayMiss` as a `cassette_miss` outcome and ordinary `Exception` as `runner_error`; never catch `KeyboardInterrupt` or `SystemExit`. Derive parse/fallback diagnostics from `AgentNodeResult`, validate hints through the contract again, then call `score_case()`. Sort outcomes by opaque case ID before canonical serialization.

Expose these exact CLI actions:

```text
--record-development
--replay-development
--seal-protocol
--record-verification
--replay-verification
--validate-evidence PATH
--output PATH
```

Development reads the draft settings from code. `--seal-protocol` writes `scenarios/narrative_bench/protocol.json`. Verification actions refuse unless that sealed file revalidates against the current code and corpus bytes.

- [ ] **Step 6: Run harness tests and verify GREEN**

Run: `uv run pytest tests/bench/narrative/test_protocol.py tests/bench/narrative/test_harness.py -q`

Expected: all tests pass without network.

- [ ] **Step 7: Commit the harness before any live run**

```bash
git add gameforge/bench/narrative/protocol.py gameforge/bench/narrative/harness.py tests/bench/narrative/test_protocol.py tests/bench/narrative/test_harness.py
git diff --cached --check
git commit -m "feat(bench): add resumable narrative evidence harness"
```

---

### Task 8: Development RECORD, Error Analysis, and One-Way Freeze

**Files:**
- Modify conditionally through the Step 2 failing-regression loop: `gameforge/agents/prompts/library.py`
- Modify conditionally through the Step 2 failing-regression loop: `gameforge/agents/consistency/assistant.py`
- Modify conditionally through the Step 2 failing-regression loop: `gameforge/agents/consistency/normalization.py`
- Modify: `tests/agents/test_consistency.py`
- Modify: `tests/agents/test_consistency_normalization.py`
- Generate: `cassettes/narrative/pre-m4-1/*.json`
- Generate: `scenarios/narrative_bench/development-evidence.json`
- Generate: `scenarios/narrative_bench/protocol.json`
- Create: `tests/bench/narrative/test_development_evidence.py`

**Interfaces:**
- Consumes: the 160-case development corpus and Task 7 harness.
- Produces: development-only empirical diagnostics and the one-way frozen protocol used by verification.

- [ ] **Step 1: Record the complete development split with GPT-5.6**

Run:

```bash
GAMEFORGE_LLM_LIVE=1 uv run python -m gameforge.bench.narrative.harness --record-development
uv run python -m gameforge.bench.narrative.harness --replay-development --output scenarios/narrative_bench/development-evidence.json
```

Expected: exactly 160 outcomes, each bound to `openai/gpt-5.6-sol/pre-m4@1`; 480 first-round request hashes when no router-level failure occurs; no request uses an M2 Opus snapshot.

- [ ] **Step 2: Add a development evidence contract test and inspect only development errors**

The test requires all 160 frozen cases in the denominator, four 20-case positive class metrics, one 80-case clean FP metric, no cassette miss, and canonical evidence hash validity. Produce an error table grouped by `wrong_class`, `wrong_entity`, `wrong_span`, `no_quorum`, `parse_failure`, and `clean_hint` using only development outcomes.

Use these development quality targets for prompt/matcher readiness, not as claims about verification: every class BDR at least 0.80 and clean FP at most 0.05. If a target misses, change only generic prompt wording, schema parsing, or source-span normalization; write a failing regression test from the development failure before the code change; increment `consistency@2` to the next explicit prompt version if prompt text changes; re-record only hashes changed by that version. Do not inspect or run any verification response during this loop.

- [ ] **Step 3: Seal the exact protocol once the development path is ready**

Run:

```bash
uv run python -m gameforge.bench.narrative.harness --seal-protocol
uv run pytest tests/bench/narrative/test_development_evidence.py tests/bench/narrative/test_protocol.py -q
```

Expected: `protocol.json` binds the final current prompt version/text, matcher, model, corpora, perspectives, threshold, and `rebuttal_enabled=false`; tests reject any single-field mutation.

- [ ] **Step 4: Reconfirm the M2 cassette set was untouched**

Run:

```bash
git diff --exit-code -- cassettes ':!cassettes/narrative'
uv run pytest tests/agents/test_consistency_legacy_replay.py -q
```

Expected: no historical cassette diff and the six-request Opus replay passes.

- [ ] **Step 5: Commit development evidence and the frozen protocol**

```bash
git add gameforge/agents/consistency gameforge/agents/prompts/library.py tests/agents cassettes/narrative/pre-m4-1 scenarios/narrative_bench/development-evidence.json scenarios/narrative_bench/protocol.json tests/bench/narrative/test_development_evidence.py
git diff --cached --check
git commit -m "data(bench): freeze the narrative verification protocol"
```

---

### Task 9: Verification RECORD, Byte-Identical REPLAY, and Acceptance Evidence

**Files:**
- Generate: `cassettes/narrative/pre-m4-1/*.json`
- Generate: `scenarios/narrative_bench/verification-evidence.json`
- Create: `tests/bench/narrative/test_verification_evidence.py`
- Create: `tests/bench/narrative/test_narrative_acceptance.py`

**Interfaces:**
- Consumes: the sealed protocol, 1,905 frozen verification cases, and dedicated GPT-5.6 cassettes.
- Produces: immutable per-case outcomes, four 381-case BDR metrics, a 381-case clean FP metric, and a byte-reproducible evidence manifest for BenchReport v2.

- [ ] **Step 1: Add failing acceptance tests before recording verification**

```python
def test_verification_evidence_has_power_complete_denominators():
    evidence = load_verification_evidence()
    assert {m.defect_class: m.n for m in evidence.by_class} == {
        dc: 381 for dc in NARRATIVE_CLASSES
    }
    assert evidence.clean_fp.n == 381
    assert len(evidence.outcomes) == 1_905


def test_verification_evidence_is_gpt56_and_has_no_dropped_failures():
    evidence = load_verification_evidence()
    assert evidence.model_snapshot == ModelSnapshot(
        provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1"
    )
    assert {item.case_id for item in evidence.outcomes} == set(frozen_case_ids())
```

The tests also rederive all metrics and Wilson intervals, validate every outcome/case/protocol hash, assert model payloads contain no hidden answer field, and assert old root cassettes still replay.

- [ ] **Step 2: Run acceptance tests and verify RED**

Run: `uv run pytest tests/bench/narrative/test_verification_evidence.py tests/bench/narrative/test_narrative_acceptance.py -q`

Expected: fail because `verification-evidence.json` and verification cassettes do not exist.

- [ ] **Step 3: Record every verification request with resume enabled**

Run:

```bash
GAMEFORGE_LLM_LIVE=1 uv run python -m gameforge.bench.narrative.harness --record-verification
```

Expected: the harness validates the sealed protocol before the first call, processes 1,905 cases in case-ID order, and records up to 5,715 first-round requests. If the gateway interrupts, rerun the same command; `resume=True` reuses finished hashes and calls only missing ones. Do not change the protocol, replace a case, or discard a poor response after this command begins.

- [ ] **Step 4: Produce two independent zero-network replays and compare canonical bytes**

Run:

```bash
uv run python -m gameforge.bench.narrative.harness --replay-verification --output scenarios/narrative_bench/verification-evidence.json
uv run python -m gameforge.bench.narrative.harness --replay-verification --output /tmp/narrative-verification-b.json
cmp scenarios/narrative_bench/verification-evidence.json /tmp/narrative-verification-b.json
```

Expected: `cmp` exits 0. The evidence honestly retains every wrong hint, parse failure, fallback, cassette miss, and runner failure in its correct denominator. Measured BDR/FP may be high or low; no result-based rerun is permitted.

- [ ] **Step 5: Run focused narrative acceptance**

Run:

```bash
uv run pytest tests/contracts/test_consistency_hint_contract.py tests/agents/test_consistency.py tests/agents/test_consistency_perspective.py tests/agents/test_consistency_legacy_replay.py tests/bench/test_inject_narrative.py tests/bench/narrative -q
uv run python -m gameforge.bench.narrative.harness --validate-evidence scenarios/narrative_bench/verification-evidence.json
```

Expected: all tests pass, evidence validation exits 0, four measured class rows have `n=381`, and clean FP has `n=381`.

- [ ] **Step 6: Commit immutable verification evidence**

```bash
git add cassettes/narrative/pre-m4-1 scenarios/narrative_bench/verification-evidence.json tests/bench/narrative/test_verification_evidence.py tests/bench/narrative/test_narrative_acceptance.py
git diff --cached --check
git commit -m "test(bench): measure power-complete narrative evidence"
```

---

### Task 10: Full Regression, Dependency Audit, and Narrative Slice Closure

**Files:**
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-narrative-evidence.md`
- Modify: `gameforge/bench/run_bench.py`
- Modify: `gameforge/bench/report.py`
- Modify: `tests/bench/test_run_bench.py`
- Modify: `tests/bench/test_bench_report.py`
- Create: `tests/architecture/test_narrative_boundaries.py`

**Interfaces:**
- Consumes: all current and historical evidence from Tasks 1-9.
- Produces: a closed narrative slice that remains separate from the later BenchReport v2 integration.

- [ ] **Step 1: Add architecture regression tests**

AST checks enforce:

```text
gameforge/spine/** imports no gameforge.agents or gameforge.bench
gameforge/agents/consistency/** imports no gameforge.bench
gameforge/agents/consistency/** contains no setting ID, case ID, seed, or source-profile dispatch
gameforge/bench/narrative/renderer.py imports no oracle or Agent module
gameforge/bench/narrative/oracle.py imports no renderer, prompt, Agent, or model-router module
```

Scan current formal corpus text and current prompts for forbidden per-case answer markers. The prompt may enumerate the four allowed output class labels as schema values; the corpus may contain class values only in hidden JSON fields, never inside `dialogue` or visible constraint statements.

Update the legacy BenchReport v1 wording without integrating the new metrics yet: `run_bench.py` must identify its four zero-denominator rows as a v1 compatibility view pending BenchReport v2 ingestion, and `report.py` must label the empty section `LLM-assisted BDR (narrative evidence is carried by BenchReport v2)` rather than claiming `human-confirmed`. Lock both strings in the two existing report tests. The authoritative measured values remain solely in `verification-evidence.json` until the separate Report v2 plan.

- [ ] **Step 2: Run the focused historical/new regression suite**

Run:

```bash
uv run pytest tests/contracts tests/agents tests/bench/narrative tests/bench/test_inject_narrative.py tests/architecture/test_narrative_boundaries.py -q
git diff --exit-code -- cassettes ':!cassettes/narrative'
```

Expected: all tests pass and historical cassettes have no diff.

- [ ] **Step 3: Run all repository gates**

Run:

```bash
uv run pytest -q
uv run pytest tests/architecture/test_import_contracts.py -q
uv run ruff check gameforge tests
git diff --check
```

Expected: full pytest passes with only already-declared skips, all seven import contracts pass, Ruff is clean, and `git diff --check` emits nothing.

- [ ] **Step 4: Re-run verification replay after the full suite**

Run:

```bash
uv run python -m gameforge.bench.narrative.harness --replay-verification --output /tmp/narrative-verification-final.json
cmp /tmp/narrative-verification-final.json scenarios/narrative_bench/verification-evidence.json
```

Expected: byte-identical evidence after a fresh zero-network process.

- [ ] **Step 5: Mark this plan complete and commit closure checks**

Mark Tasks 1-10 `[x]` only after their commands have passed. Do not change M3 to complete or begin M4: HED, QA-hours, Cost/Latency, BenchReport v2, combined acceptance, and the final pre-M4 audit remain outstanding.

```bash
git add docs/superpowers/plans/2026-07-12-pre-m4-narrative-evidence.md tests/architecture/test_narrative_boundaries.py gameforge/bench/run_bench.py gameforge/bench/report.py
git diff --cached --check
git commit -m "test(bench): close the narrative evidence slice"
```

---

## Plan Self-Review

- Every approved narrative requirement maps to a task: typed hints (1), normalized quorum (2), three all-class methods and llm-assisted Findings (3), hidden facts/oracle (4), marker-free power-complete corpora (5), denominator-safe scoring (6), GPT-5.6 protocol/replay (7), development-only tuning and freeze (8), immutable verification (9), and full boundary/regression gates (10).
- New GPT-5.6 evidence and historical Opus evidence have distinct model snapshots, prompt versions, entrypoints, and cassette roots. The plan contains a byte-level historical replay lock before and after live work.
- Verification never becomes a tuning set: corpus and protocol are sealed before the first verification call; misses and poor outputs remain in the evidence.
- Product Agent code sees only generic constraints and dialogue. Hidden benchmark facts, source settings, target answers, and scores cannot flow into `agents` or `spine`.
- HED, QA-hours, Cost/Latency aggregation, Report v2, combined acceptance, and M4 are deliberately absent because each is a separately testable subsystem with its own subsequent plan.
- No step contains a pending design choice, human approval gate, or placeholder implementation.
