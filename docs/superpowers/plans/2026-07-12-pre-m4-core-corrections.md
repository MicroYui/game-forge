# Pre-M4 Core Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the `DROPS_FROM` producer direction, enforce exact-base Patch application, stabilize repair request identity, and re-record only active repair/generation evidence with `gpt-5.6-sol` while retaining a 10/10 deterministic Fix Pass Rate.

**Architecture:** The deterministic spine rejects stale Patches before all other work and preserves its existing precondition vocabulary. Source-specific Adapters translate item drops into the already-consumed producer-to-product IR direction, while repair request identity excludes the base hash and Patch identity binds the model run, exact base, and typed ops. The repair harness uses the new model for active repair/generation recordings and a separate historical replay route for unchanged extraction/consistency evidence; playtest remains untouched.

**Tech Stack:** Python 3.12, Pydantic v2, canonical SHA-256 JSON, Graph/Clingo checkers, ModelRouter RECORD/REPLAY, OpenAI Responses transport, pytest, import-linter, Ruff.

## Global Constraints

- Truth sources are `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, `docs/superpowers/specs/2026-07-11-pre-m4-product-closure-design.md`, and `docs/superpowers/specs/2026-07-12-pre-m4-core-corrections-design.md`.
- Keep `ir-core@1`; do not introduce dual `DROPS_FROM` directions, a persisted-snapshot migrator, or a new ownership relation.
- `DROPS_FROM` is `MONSTER | DROP_TABLE | INTERACTABLE | EVENT | BATTLE_ENCOUNTER -> ITEM | CURRENCY`.
- Patch base mismatch is fail-closed `PatchRejected`; do not implement rebase, merge, a new precondition DSL, or missing-vs-null semantics.
- Repair prompt version is `repair@4`; model request identity excludes `base_snapshot_id`, but Patch identity includes `{request_hash, base_snapshot_id, ops}`.
- New active recordings use `openai / gpt-5.6-sol / pre-m4@1` through `/v1/responses` and omit `temperature`.
- Historical extraction, consistency, and playtest cassettes remain byte-identical and tied to `anthropic / claude-opus-4-8 / m2a@1` with their original request parameters.
- No live call occurs before Tasks 1-4 are committed and their focused deterministic tests pass.
- RECORD is explicitly gated by `GAMEFORGE_LLM_LIVE=1` and `GAMEFORGE_LLM_KEY`; REPLAY never falls back to live transport.
- Fix Pass Rate must remain exactly `10/10`. Do not edit scenarios, weaken verifiers, raise the search budget, drop failures from the denominator, or change the acceptance threshold to improve the number.
- `scenarios/flare_corpus/**` and `scenarios/external_corpus/endless_sky/**` remain unchanged; Endless Sky remains `awaiting_human_evidence`.
- TDD is mandatory: each deterministic behavior starts with a focused failing test and only then receives the narrow implementation.
- Commits contain no AI attribution footer. Never stage the untracked root `AGENTS.md`.

---

## File Map

| File | Responsibility in this sub-milestone |
|---|---|
| `gameforge/spine/patch.py` | Exact-base gate and malformed-precondition normalization |
| `gameforge/agents/repair/drafter.py` | Stable repair request and base-bound Patch ID |
| `gameforge/agents/prompts/library.py` | `repair@4` version boundary |
| `gameforge/spine/ingestion/aureus_adapter.py` | Aureus producer-to-item relation |
| `gameforge/spine/ingestion/flare_adapter.py` | Flare producer-to-item relation |
| `gameforge/spine/sim/economy.py` | Legal-producer filter for currency faucets |
| `gameforge/agents/harness.py` | Active-vs-historical model replay/record routing |
| `tests/spine/test_patch.py` | stale-base, malformed, atomicity contract tests |
| `tests/agents/test_repair_drafter_context.py` | request reuse and Patch identity tests |
| `tests/agents/test_prompt_library.py` | prompt-version regression lock |
| `tests/spine/ingestion/test_drops_from_conformance.py` | cross-Adapter endpoint contract |
| `tests/spine/checkers/test_drop_direction.py` | Graph/ASP forward/reverse differential contract |
| `tests/spine/ingestion/test_aureus_adapter.py` | Aureus derived-edge and round-trip checks |
| `tests/spine/ingestion/test_flare_adapter.py` | Flare direct-loot direction and round-trip checks |
| `tests/spine/sim/test_economy.py` | malformed-source exclusion |
| `tests/agents/test_model_recording_policy.py` | active and historical model snapshot routing |
| `cassettes/*.json` | new active repair/generation records and removal of unreachable old ones |
| `README.md`, `CLAUDE.md`, `docs/superpowers/plans/README.md` | verified progress anchors only after final acceptance |

---

### Task 1: Exact-Base and Malformed-Precondition Patch Rejection

**Files:**
- Modify: `tests/spine/test_patch.py`
- Modify: `gameforge/spine/patch.py`

**Interfaces:**
- Consumes: `Snapshot.snapshot_id`, `Patch.base_snapshot_id`, existing `entity_exists` and `attr_equals` dictionaries.
- Produces: `apply_patch(snapshot, patch) -> Snapshot` and `dry_run(snapshot, patch) -> GraphDiff`, both exact-base and fail-closed.

- [ ] **Step 1: Bind the existing test helper to a real Snapshot**

Change `_patch` to take the base Snapshot explicitly so every existing happy-path test expresses the new contract:

```python
def _patch(snap: Snapshot, ops, preconditions=None, patch_id="p1") -> Patch:
    return Patch(
        id=patch_id,
        base_snapshot_id=snap.snapshot_id,
        target_snapshot_id="",
        side_effect_risk="low",
        ops=ops,
        preconditions=preconditions or [],
        produced_by="agent",
        producer_run_id="r1",
        rationale="test",
    )
```

Update every existing `_patch(...)` call to `_patch(snap, ...)` without changing its assertion.

- [ ] **Step 2: Add stale-base, malformed-shape, and atomicity tests**

Add these behaviors to `tests/spine/test_patch.py`:

```python
@pytest.mark.parametrize(
    "ops",
    [
        [],
        [TypedOp(op_id="add", op="add_entity", target="item:new",
                 new_value={"type": "ITEM", "attrs": {}})],
        [TypedOp(op_id="delete", op="delete_relation", target="rel:1")],
        [TypedOp(op_id="set", op="set_entity_attr", target="q:1.reward_gold",
                 old_value=120, new_value=80)],
    ],
)
def test_stale_base_rejects_every_patch_shape_before_work(ops):
    snap = _base_snapshot()
    patch = _patch(snap, ops).model_copy(
        update={
            "base_snapshot_id": "sha256:stale",
            "preconditions": [{"kind": "attr_equals"}],
        }
    )
    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap, patch)


@pytest.mark.parametrize(
    "condition",
    [
        {},
        {"kind": "entity_exists"},
        {"kind": "entity_exists", "id": ""},
        {"kind": "entity_exists", "id": 7},
        {"kind": "attr_equals"},
        {"kind": "attr_equals", "target": "q:1.reward_gold"},
        {"kind": "attr_equals", "target": "q:1", "value": 120},
        {"kind": "attr_equals", "target": 7, "value": 120},
    ],
)
def test_malformed_precondition_is_patch_rejected(condition):
    snap = _base_snapshot()
    with pytest.raises(PatchRejected, match="malformed precondition|unknown kind"):
        apply_patch(snap, _patch(snap, [], preconditions=[condition]))


def test_late_op_failure_never_mutates_input_snapshot():
    snap = _base_snapshot()
    patch = _patch(snap, [
        TypedOp(op_id="first", op="set_entity_attr", target="q:1.reward_gold",
                old_value=120, new_value=80),
        TypedOp(op_id="second", op="delete_entity", target="missing"),
    ])
    with pytest.raises(PatchRejected):
        apply_patch(snap, patch)
    assert snap.to_graph().get_node("q:1").attrs["reward_gold"] == 120


def test_dry_run_rejects_stale_base():
    snap = _base_snapshot()
    patch = _patch(snap, []).model_copy(update={"base_snapshot_id": "sha256:stale"})
    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        dry_run(snap, patch)
```

- [ ] **Step 3: Run the focused tests and confirm the expected RED failures**

Run:

```bash
uv run pytest tests/spine/test_patch.py -q
```

Expected: stale-base cases do not raise, and malformed conditions leak `KeyError` or another non-`PatchRejected` exception. Existing happy paths remain green after the helper update.

- [ ] **Step 4: Implement the exact-base gate before graph construction**

At the top of `apply_patch`:

```python
if snapshot.snapshot_id != patch.base_snapshot_id:
    raise PatchRejected(
        "base snapshot mismatch: "
        f"patch expects {patch.base_snapshot_id!r}, current is {snapshot.snapshot_id!r}"
    )
```

This check must precede `snapshot.to_graph()` and `_check_preconditions(...)`.

- [ ] **Step 5: Validate the two existing precondition shapes explicitly**

Replace unchecked dictionary indexing in `_check_preconditions` with exact shape guards:

```python
def _required_nonempty_str(cond: dict[str, Any], field: str, kind: object) -> str:
    value = cond.get(field)
    if not isinstance(value, str) or not value:
        raise PatchRejected(
            f"malformed precondition {kind!r}: {field!r} must be a non-empty string"
        )
    return value
```

For `entity_exists`, require a non-empty string `id`. For `attr_equals`, require
a non-empty string `target`, a non-empty dotted attr path, and literal presence
of the `value` key (`if "value" not in cond: raise PatchRejected(...)`). Keep
unknown kinds rejected. Do not add any new condition kind.

- [ ] **Step 6: Run Patch and direct consumers**

Run:

```bash
uv run pytest tests/spine/test_patch.py tests/agents/test_generation.py tests/agents/test_repair_verify.py tests/agents/test_repair_search.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit the Patch contract correction**

```bash
git add gameforge/spine/patch.py tests/spine/test_patch.py
git commit -m "fix(spine): reject stale and malformed patches"
```

---

### Task 2: Stable Repair Request and Base-Bound Patch Identity

**Files:**
- Modify: `tests/agents/test_prompt_library.py`
- Modify: `tests/agents/test_repair_drafter_context.py`
- Modify: `gameforge/agents/prompts/library.py`
- Modify: `gameforge/agents/repair/drafter.py`

**Interfaces:**
- Consumes: model `request_hash`, exact `Snapshot.snapshot_id`, ordered `list[TypedOp]`.
- Produces: stable repair request hash, `Patch.producer_run_id=request_hash`, and deterministic base-bound `Patch.id`.

- [ ] **Step 1: Change prompt-version expectations to `repair@4`**

In `tests/agents/test_prompt_library.py`, require both repair templates to be
`repair@4`, including the refine render assertion.

- [ ] **Step 2: Add a request-reuse and Patch-identity integration test**

Add an in-memory transport to `tests/agents/test_repair_drafter_context.py`:

```python
class _FixedOpsTransport:
    def __init__(self):
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=json.dumps([
            {"op": "set_entity_attr", "target": "q.reward_gold",
             "old_value": 120, "new_value": 80}
        ]))
```

Create two Snapshots with identical entity IDs, focus attrs, relations, and
catalogs, but different attrs on an unrelated non-focus entity. Draft the same
finding twice through one PASSTHROUGH router:

```python
def _stable_context_snapshot(unrelated_name: str) -> Snapshot:
    return Snapshot.from_entities_relations(
        [
            Entity(id="q", type=NodeType.QUEST, attrs={"reward_gold": 120}),
            Entity(id="npc:unrelated", type=NodeType.NPC,
                   attrs={"name": unrelated_name}),
        ],
        [],
    )


def test_semantic_request_reuses_model_run_but_patch_identity_binds_base(tmp_path):
    snap_a = _stable_context_snapshot("before")
    snap_b = _stable_context_snapshot("after")
    transport = _FixedOpsTransport()
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )
    drafter = RepairDrafter()

    patch_a = drafter.draft(_finding(snap_a, ["q"]), snap_a, router)
    patch_b = drafter.draft(_finding(snap_b, ["q"]), snap_b, router)

    assert patch_a is not None and patch_b is not None
    assert snap_a.snapshot_id != snap_b.snapshot_id
    assert len(transport.calls) == 1
    assert patch_a.producer_run_id == patch_b.producer_run_id
    assert patch_a.id != patch_b.id
    assert patch_a.base_snapshot_id == snap_a.snapshot_id
    assert patch_b.base_snapshot_id == snap_b.snapshot_id
    with pytest.raises(PatchRejected, match="base snapshot mismatch"):
        apply_patch(snap_a, patch_b)


def test_user_prompt_omits_base_snapshot_identity():
    snap = _stable_context_snapshot("irrelevant")
    prompt = RepairDrafter()._build_user_prompt(_finding(snap, ["q"]), snap)

    assert snap.snapshot_id not in prompt
    assert "base_snapshot_id" not in prompt
```

Add imports for `pytest`, `ModelResponse`, `CassetteStore`, `ModelRouter`,
`RouterMode`, `PatchRejected`, and `apply_patch`.

Also directly assert `_build_user_prompt(...)` contains neither the concrete
Snapshot ID nor the label `base_snapshot_id`.

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/agents/test_prompt_library.py tests/agents/test_repair_drafter_context.py -q
```

Expected: tests fail because the version is `repair@3`, the prompt contains the
base ID, both model calls occur, and Patch IDs equal request hashes.

- [ ] **Step 4: Add deterministic Patch-ID construction**

In `gameforge/agents/repair/drafter.py`, import `hashlib` and
`gameforge.contracts.canonical.canonical_json`, then add:

```python
def _patch_id(request_hash: str, base_snapshot_id: str, ops: list[TypedOp]) -> str:
    payload = {
        "request_hash": request_hash,
        "base_snapshot_id": base_snapshot_id,
        "ops": [op.model_dump(mode="json") for op in ops],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
```

Construct the Patch with:

```python
id=_patch_id(request_hash, snapshot.snapshot_id, ops),
base_snapshot_id=snapshot.snapshot_id,
producer_run_id=request_hash,
```

- [ ] **Step 5: Remove only the dynamic base line and bump both repair prompts**

Delete:

```python
parts.append(f"base_snapshot_id: {snapshot.snapshot_id}")
```

Do not remove focus nodes, incident relations, evidence, entity catalog, edge
types, or counterexamples. Change both library registrations to `repair@4`:

```python
("repair.system", "repair@4", _REPAIR),
("repair.refine", "repair@4", _REPAIR_REFINE),
```

Update the drafter module docstring so it no longer calls the request hash the
Patch ID.

- [ ] **Step 6: Run all repair unit tests**

Run:

```bash
uv run pytest tests/agents/test_prompt_library.py tests/agents/test_repair_drafter_context.py tests/agents/test_repair_search.py tests/agents/test_repair_verify.py -q
```

Expected: all pass without a cassette or network.

- [ ] **Step 7: Commit the identity correction**

```bash
git add gameforge/agents/prompts/library.py gameforge/agents/repair/drafter.py tests/agents/test_prompt_library.py tests/agents/test_repair_drafter_context.py
git commit -m "fix(agents): decouple repair requests from patch bases"
```

---

### Task 3: Producer-to-Product `DROPS_FROM` Across Adapters and Oracles

**Files:**
- Create: `tests/spine/ingestion/test_drops_from_conformance.py`
- Create: `tests/spine/checkers/test_drop_direction.py`
- Modify: `tests/spine/ingestion/test_aureus_adapter.py`
- Modify: `tests/spine/ingestion/test_flare_adapter.py`
- Modify: `tests/spine/sim/test_economy.py`
- Modify: `gameforge/spine/ingestion/aureus_adapter.py`
- Modify: `gameforge/spine/ingestion/flare_adapter.py`
- Modify: `gameforge/spine/sim/economy.py`

**Interfaces:**
- Consumes: existing `EdgeType.DROPS_FROM` and `ir-core@1` NodeTypes.
- Produces: one unambiguous producer-to-product relation direction without changing round-trip source attrs.

- [ ] **Step 1: Add cross-Adapter endpoint conformance tests**

In `test_drops_from_conformance.py`, build:

- an Aureus workbook containing a Monster with a referenced DropTable item and
  a currency faucet; and
- the existing Flare sample through `read_flare_dir`.

For every derived `DROPS_FROM`, resolve both endpoints and assert:

```python
LEGAL_PRODUCERS = {
    NodeType.MONSTER,
    NodeType.DROP_TABLE,
    NodeType.INTERACTABLE,
    NodeType.EVENT,
    NodeType.BATTLE_ENCOUNTER,
}
LEGAL_PRODUCTS = {NodeType.ITEM, NodeType.CURRENCY}

assert source.type in LEGAL_PRODUCERS
assert product.type in LEGAL_PRODUCTS
```

The Aureus fixture must explicitly observe both `MONSTER -> ITEM` and
`MONSTER -> CURRENCY`; the Flare fixture must observe `MONSTER -> ITEM`.

- [ ] **Step 2: Add Graph/ASP forward-vs-reverse differential tests**

In `test_drop_direction.py`, construct one collect step, its Item, and a
Monster, then parameterize over `GraphChecker()` and `ASPChecker()`:

```python
def _snapshot_with_drop(src_id: str, dst_id: str) -> Snapshot:
    entities = [
        Entity(
            id="step",
            type=NodeType.QUEST_STEP,
            attrs={"kind": "collect", "item": "item"},
        ),
        Entity(id="item", type=NodeType.ITEM),
        Entity(id="monster", type=NodeType.MONSTER),
    ]
    relations = [
        Relation(
            id="drop",
            type=EdgeType.DROPS_FROM,
            src_id=src_id,
            dst_id=dst_id,
        )
    ]
    return Snapshot.from_entities_relations(entities, relations)


def _missing_source(checker, snapshot: Snapshot) -> list:
    return [
        finding
        for finding in checker.check(snapshot)
        if finding.defect_class == "missing_drop_source"
    ]


@pytest.mark.parametrize("checker", [GraphChecker(), ASPChecker()])
def test_forward_drop_source_clears_missing_source(checker):
    snapshot = _snapshot_with_drop("monster", "item")
    assert not _missing_source(checker, snapshot)


@pytest.mark.parametrize("checker", [GraphChecker(), ASPChecker()])
def test_reverse_drop_edge_cannot_masquerade_as_source(checker):
    snapshot = _snapshot_with_drop("item", "monster")
    assert _missing_source(checker, snapshot)
```

Filter only `defect_class == "missing_drop_source"`; unrelated findings do not
change the result.

- [ ] **Step 3: Update Adapter-specific expected directions and add the economy negative case**

In `test_aureus_adapter.py`, add a DropTable item case and require an outgoing
Monster edge to the Item. In `test_flare_adapter.py`, change the neighbor lookup
from incoming-to-Monster to outgoing-from-Monster and assert Item destinations.

In `test_economy.py`, construct `ITEM --DROPS_FROM--> CURRENCY` where the Item
carries gold attrs and assert `EconomyModel.from_snapshot(...).sources == []`.

- [ ] **Step 4: Run the new tests and confirm RED only at the real violations**

Run:

```bash
uv run pytest tests/spine/ingestion/test_drops_from_conformance.py tests/spine/checkers/test_drop_direction.py tests/spine/ingestion/test_aureus_adapter.py tests/spine/ingestion/test_flare_adapter.py tests/spine/sim/test_economy.py -q
```

Expected: Adapter endpoint tests fail for item drops and the illegal economy
producer is currently accepted. Existing manual forward-edge Graph/ASP cases
remain green; reverse cases report missing source.

- [ ] **Step 5: Flip only the two reverse Adapter producers**

In `AureusCsvAdapter.to_ir`, emit:

```python
id=rid.next(EdgeType.DROPS_FROM, monster["monster_id"], entry["item"]),
type=EdgeType.DROPS_FROM,
src_id=monster["monster_id"],
dst_id=entry["item"],
```

In `FlareTxtAdapter.to_ir`, emit:

```python
id=rid.next(EdgeType.DROPS_FROM, monster_entity_id, item_entity_id),
type=EdgeType.DROPS_FROM,
src_id=monster_entity_id,
dst_id=item_entity_id,
```

Rewrite the nearby comments to state the one producer-to-product convention.
Do not change source attrs or `from_ir`.

- [ ] **Step 6: Filter economy faucets to contract-legal producers**

Define a module-local immutable set:

```python
_DROP_PRODUCER_TYPES = frozenset({
    NodeType.MONSTER,
    NodeType.DROP_TABLE,
    NodeType.INTERACTABLE,
    NodeType.EVENT,
    NodeType.BATTLE_ENCOUNTER,
})
```

After resolving `producer`, continue unless `producer.type` is in this set.
Do not add a source-specific condition.

- [ ] **Step 7: Run Adapter, checker, simulator, and benchmark direction suites**

Run:

```bash
uv run pytest tests/spine/ingestion tests/spine/checkers tests/spine/sim/test_economy.py tests/bench/test_inject_structural.py tests/bench/test_external.py -q
```

Expected: all pass, including field-level and byte-level round trips.

- [ ] **Step 8: Commit the IR direction correction**

```bash
git add gameforge/spine/ingestion/aureus_adapter.py gameforge/spine/ingestion/flare_adapter.py gameforge/spine/sim/economy.py tests/spine/ingestion/test_drops_from_conformance.py tests/spine/checkers/test_drop_direction.py tests/spine/ingestion/test_aureus_adapter.py tests/spine/ingestion/test_flare_adapter.py tests/spine/sim/test_economy.py
git commit -m "fix(spine): normalize drop relations to producer direction"
```

---

### Task 4: Separate Active Repair Recording from Historical Agent Evidence

**Files:**
- Modify: `tests/agents/test_model_recording_policy.py`
- Modify: `gameforge/agents/harness.py`

**Interfaces:**
- Produces: active repair `replay_router()` on `DEFAULT_SNAPSHOT`, historical sample `historical_replay_router()` on `M2_REPLAY_SNAPSHOT`, and unchanged playtest replay policy.

- [ ] **Step 1: Write model-routing policy tests first**

Replace the parameterized historical-replay assertion with explicit contracts:

```python
def test_repair_replay_uses_active_default(tmp_path):
    assert harness.replay_router(str(tmp_path)).default_model_snapshot == DEFAULT_SNAPSHOT


def test_historical_agent_samples_keep_m2_snapshot(tmp_path):
    assert (
        harness.historical_replay_router(str(tmp_path)).default_model_snapshot
        == M2_REPLAY_SNAPSHOT
    )


def test_playtest_replay_keeps_m2_snapshot(tmp_path):
    assert (
        playtest_harness.replay_router(str(tmp_path)).default_model_snapshot
        == M2_REPLAY_SNAPSHOT
    )
```

Add a no-network routing test with two PASSTHROUGH transports:

```python
class _RecordingJsonTransport:
    def __init__(self):
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized="[]")


def test_agent_sample_recording_routes_only_generation_to_active_model(tmp_path):
    active_transport = _RecordingJsonTransport()
    historical_transport = _RecordingJsonTransport()
    active_router = ModelRouter(
        active_transport,
        CassetteStore(tmp_path / "active"),
        mode=RouterMode.PASSTHROUGH,
        default_model_snapshot=DEFAULT_SNAPSHOT,
    )
    historical_router = ModelRouter(
        historical_transport,
        CassetteStore(tmp_path / "historical"),
        mode=RouterMode.PASSTHROUGH,
        default_model_snapshot=M2_REPLAY_SNAPSHOT,
    )

    harness._record_agent_samples(active_router, historical_router)

    assert {req.agent_node_id for req in active_transport.calls} == {"generation"}
    historical_nodes = {req.agent_node_id for req in historical_transport.calls}
    assert historical_nodes == {"extraction", "consistency"}
    assert all(req.model_snapshot == DEFAULT_SNAPSHOT for req in active_transport.calls)
    assert all(
        req.model_snapshot == M2_REPLAY_SNAPSHOT
        for req in historical_transport.calls
    )
```

Add imports for `ModelResponse`, `CassetteStore`, `ModelRouter`, and
`RouterMode`.

- [ ] **Step 2: Run the policy tests RED**

Run:

```bash
uv run pytest tests/agents/test_model_recording_policy.py -q
```

Expected: `historical_replay_router` is absent, repair replay is still pinned to
M2, and `_record_agent_samples` uses one router for every role.

- [ ] **Step 3: Implement the two replay policies**

Change `harness.replay_router` to `default_model_snapshot=DEFAULT_SNAPSHOT` and
add:

```python
def historical_replay_router(cassettes_root: str = _CASSETTES_ROOT) -> ModelRouter:
    return ModelRouter(
        _NoLiveTransport(),
        CassetteStore(cassettes_root),
        mode=RouterMode.REPLAY,
        default_model_snapshot=M2_REPLAY_SNAPSHOT,
    )
```

Do not alter `playtest_harness.replay_router`.

- [ ] **Step 4: Route unchanged samples through historical REPLAY**

Change the internal sample function signature to:

```python
def _record_agent_samples(
    active_router: ModelRouter,
    historical_router: ModelRouter,
) -> None:
```

Use `historical_router` for Extraction and Consistency, and `active_router` for
Generation. In `_run_record`, construct the historical router from the same
cassette root and pass both routers. A historical miss remains a printed
best-effort failure and must never reach the active transport.

- [ ] **Step 5: Run policy and no-network Agent tests**

Run:

```bash
uv run pytest tests/agents/test_model_recording_policy.py tests/agents/test_agent_base.py tests/runtime/model_router -q
```

Expected: all pass.

- [ ] **Step 6: Confirm the expected pre-record cassette RED without changing code**

Run:

```bash
uv run pytest tests/agents/test_part2_acceptance.py::test_fix_pass_rate_ge_70pct -q
```

Expected: `CassetteReplayMiss` for a `repair@4` / `gpt-5.6-sol` request. This is
the required proof that the active acceptance path does not silently reuse old
Opus `repair@3` evidence.

- [ ] **Step 7: Commit the recording-policy correction**

```bash
git add gameforge/agents/harness.py tests/agents/test_model_recording_policy.py
git commit -m "fix(agents): separate active and historical replay policy"
```

---

### Task 5: One-Time `gpt-5.6-sol` Record, Double Replay, and Bounded Cleanup

**Files:**
- Create/Modify/Delete: `cassettes/*.json` only as produced or made unreachable by the active harness
- Do not modify: `cassettes/playtest/**`
- Do not modify: root-level extraction/consistency cassette files

**Interfaces:**
- Consumes: stable Tasks 1-4 semantics and the local gateway at `http://localhost:4141`.
- Produces: complete active repair/generation cassette coverage and 10/10 replay evidence.

- [ ] **Step 1: Freeze a local before-inventory of historical evidence**

Record outside the repository diff:

```bash
find cassettes -maxdepth 1 -type f -name '*.json' -print0 | xargs -0 shasum -a 256
find cassettes/playtest -type f -name '*.json' -print0 | xargs -0 shasum -a 256
```

Also record the root counts by `agent_node_id` with:

```bash
jq -r '.agent_node_id' cassettes/*.json | sort | uniq -c
```

Expected before recording: 30 repair, 2 generation, 1 extraction, and 9
consistency root cassettes.

- [ ] **Step 2: Verify the gateway and secret gate without printing the key**

Require `GAMEFORGE_LLM_KEY` to be loaded from the environment or the gitignored
local `.env`. Confirm only presence and that the gateway accepts a local HTTP
connection. Never echo the key or write it to a tracked file.

- [ ] **Step 3: Run the one active RECORD pass with resume**

Run:

```bash
GAMEFORGE_LLM_LIVE=1 uv run python -m gameforge.agents.harness --record
```

Expected:

- all live requests name `openai/gpt-5.6-sol/pre-m4@1`;
- request params omit `temperature`;
- repair result is `attempted: 10`, `passed: 10`, `fix_pass_rate: 100.0%`;
- extraction and consistency are replayed from historical files, not sent to the active transport;
- one active generation sample is recorded for the new clean Snapshot.

If the run is interrupted or the gateway returns transient errors, rerun the
same command. `resume=True` must reuse completed active records.

- [ ] **Step 4: If and only if 10/10 is not reached, use systematic debugging**

Do not change a scenario or verifier. Identify the exact failing class, inspect
its model ops, Patch rejection or verifier counterexample, and compare the
model-visible context to the intended repair. Add a failing regression test for
the root cause before any prompt or parser adjustment. Any prompt change bumps
the version beyond `repair@4` and requires a fresh complete active re-record;
there is no partial evidence reuse across a prompt change.

- [ ] **Step 5: Run zero-live REPLAY twice**

Run twice in separate processes:

```bash
uv run python -m gameforge.agents.harness --replay
```

Expected both times: identical per-scenario rows and aggregate values, including
`10/10` and the same search-step distribution.

Then run the field-equality lock:

```bash
uv run pytest tests/agents/test_part2_acceptance.py::test_repair_search_reproducible -q
```

Expected: pass.

- [ ] **Step 6: Validate new records before deleting any old record**

Use `jq` to require every root cassette with `agent_node_id` equal to `repair`
or `generation` and model `gpt-5.6-sol` to carry provider `openai` and snapshot
tag `pre-m4@1`. Confirm no active request has `temperature` in its hashed
request policy by the model-routing unit tests and captured transport requests.

- [ ] **Step 7: Delete only unreachable historical repair/generation files**

Delete root `cassettes/*.json` files satisfying both:

```text
agent_node_id in {repair, generation}
model_snapshot == anthropic/claude-opus-4-8/m2a@1
```

Keep every extraction/consistency root file and every `cassettes/playtest/**`
file. Do not build a generic GC or manifest service.

- [ ] **Step 8: Replay again after cleanup and compare historical hashes**

Run:

```bash
uv run python -m gameforge.agents.harness --replay
uv run pytest tests/agents/test_part2_acceptance.py -q
```

Expected: 10/10 and all M2a-part2 acceptance anchors pass. Compare the before
inventory: extraction, consistency, and playtest hashes must be identical.

- [ ] **Step 9: Commit the bounded cassette refresh**

Stage only root cassette additions/deletions and inspect the staged summary:

```bash
git add cassettes/*.json
git diff --cached --stat
git commit -m "record(agents): refresh active repair corpus with gpt-5.6-sol"
```

---

### Task 6: Full Acceptance, Frozen-Evidence Regression, and Progress Anchors

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/plans/README.md`
- Optionally modify only verified completion metadata in: `docs/superpowers/specs/2026-07-12-pre-m4-core-corrections-design.md`

**Interfaces:**
- Produces: auditable completion state without claiming M3 or M4 completion.

- [ ] **Step 1: Run focused product acceptance**

Run:

```bash
uv run pytest tests/spine/test_patch.py tests/spine/ingestion tests/spine/checkers tests/spine/sim/test_economy.py tests/agents/test_prompt_library.py tests/agents/test_repair_drafter_context.py tests/agents/test_model_recording_policy.py tests/agents/test_part2_acceptance.py -q
```

Expected: all pass.

- [ ] **Step 2: Verify frozen external evidence and anti-specialization**

Run:

```bash
uv run pytest tests/bench/external_corpus tests/bench/test_flare_evidence.py tests/bench/test_flare_evidence_package.py tests/bench/test_flare_direct_match_replay.py -q
```

Expected: all pass; Endless Sky remains `awaiting_human_evidence` and no
adjudication/decision artifact appears.

Confirm tracked Flare frozen bytes did not change from their approved baseline:

```bash
git diff --exit-code 755fe2e -- scenarios/flare_corpus
```

Expected: no output.

- [ ] **Step 3: Run the full repository gates serially**

Run without another pytest process in parallel:

```bash
uv run pytest -q
uv run lint-imports
uv run ruff check .
git diff --check
```

Expected: full pytest green with only the existing intentional skip, seven
import contracts kept and zero broken, Ruff clean, and no whitespace errors.

- [ ] **Step 4: Update progress anchors with measured facts only**

Add a completed Pre-M4 core-corrections row/paragraph stating:

- `DROPS_FROM` is producer-to-product in Aureus and Flare;
- Patch base mismatch and malformed preconditions fail closed;
- repair request identity no longer includes the base hash;
- active repair/generation recordings use `gpt-5.6-sol`;
- Fix Pass Rate remains 10/10 under double REPLAY;
- historical M2 cassettes and external frozen evidence remain unchanged; and
- M3 remains open on human external evidence, narrative BDR, HED, QA-hours, and Report v2.

Use the actual final test count and commit IDs; do not copy a planned value.

- [ ] **Step 5: Re-run doc-sensitive gates and inspect the complete diff**

Run:

```bash
uv run pytest tests/test_dependency_lint.py tests/bench/external_corpus/test_pre_m4_external_acceptance.py -q
uv run ruff check .
git diff --check
git status --short
```

Expected: green; only intentional task files are modified, and root `AGENTS.md`
is not staged.

- [ ] **Step 6: Commit completion anchors**

```bash
git add README.md CLAUDE.md docs/superpowers/plans/README.md docs/superpowers/specs/2026-07-12-pre-m4-core-corrections-design.md
git commit -m "docs(roadmap): close pre-M4 core corrections"
```

- [ ] **Step 7: Request code review and resolve every correctness finding**

Review specifically for:

- stale Patch bypasses or exception leakage;
- model-visible repair inputs accidentally removed;
- Patch ID/request ID collision or nondeterminism;
- reverse/invalid `DROPS_FROM` endpoints;
- accidental refresh/deletion of historical cassettes;
- source-specific conditions in core code; and
- overstated M3/M4 status.

After fixes, rerun every affected focused test and the complete gates in Step 3.

---

## Final Acceptance Checklist

- [ ] `apply_patch` and `dry_run` reject every stale base before other evaluation.
- [ ] Malformed preconditions surface only as `PatchRejected`.
- [ ] Multi-op failure cannot mutate the input Snapshot.
- [ ] Repair requests exclude base IDs but retain all semantic context.
- [ ] `producer_run_id` is the request hash; Patch ID is canonical and base-bound.
- [ ] Aureus and Flare item drops are producer-to-item; currency drops remain producer-to-currency.
- [ ] Graph and ASP agree that only a forward edge satisfies collect-source existence.
- [ ] Economy ignores illegal producer types.
- [ ] New active repair/generation cassettes use `gpt-5.6-sol`; historical evidence bytes are unchanged.
- [ ] Fix Pass Rate is 10/10 in two zero-live REPLAY runs.
- [ ] Full pytest, seven import contracts, Ruff, and `git diff --check` pass.
- [ ] Flare and Endless Sky frozen evidence is unchanged and no human gate is bypassed.
- [ ] Roadmap still shows M3 incomplete and M4 not started.
