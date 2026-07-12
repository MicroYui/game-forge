# Pre-M4 Human Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents for this plan.

**Goal:** Measure Human-Edit-Distance against all eight upstream human fixes and collect a protocol-valid four-pair QA-hours case study without introducing game-specific logic into GameForge's Agent, checker, metric, or report contracts.

**Architecture:** A source-neutral HED package compares semantic IR deltas produced from the same before snapshot: one target comes from the verifier-gated Repair Agent and one from the upstream author's after commit. A separate source-neutral QA package freezes a counterbalanced matched-pair schedule, records monotonic timer events, and scores the same correctness contract for both arms; a thin Endless Sky composition boundary materializes source files and calls the existing reader, Adapter, predicate, native parser, and generic checkers. New Agent evidence is recorded with `openai/gpt-5.6-sol/pre-m4@1`; all historical Opus 4.8 cassettes remain byte-identical.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Hypothesis, stdlib `hashlib`/`json`/`random`/`statistics`/`time`, existing Model Router/Cassette contracts, `GraphChecker` + `ASPChecker`, and the existing Endless Sky lossless reader/Adapter/native predicate evidence path.

## Global Constraints

- Read and obey `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, and `docs/superpowers/specs/2026-07-12-pre-m4-lean-closure-design.md` before editing production code.
- TDD is mandatory: every production behavior starts with a focused failing test whose expected failure is observed.
- `spine` remains independent of Agents and benchmark code. This slice does not add any source profile, benchmark oracle, or LLM dependency to `spine`.
- `gameforge/bench/hed/**` and `gameforge/bench/qa/**` contain no `endless_sky`, `flare`, upstream commit OID, source path, case ID, or source-field dispatch. Source-specific loading and verdict code stays under `gameforge/bench/external_cases/`.
- HED measures semantic graph changes, not raw serialization noise. It excludes `source_ref` plus Adapter-owned round-trip envelope attributes `source_chunk_b64`, `source_kind`, `source_name`, `source_order`, and `reader_version`; every other IR attribute remains semantic.
- Relation identity for HED is the canonical multiset of `(type, src_id, dst_id, semantic attrs)`, never the source-derived `relation.id` or `source_ref`.
- Normalized HED is Jaccard distance `|agent_delta symmetric_difference human_delta| / |agent_delta union human_delta|`; both empty is `0.0`. An Agent-unusable outcome uses an empty Agent delta and therefore remains measured rather than disappearing from the denominator.
- All eight frozen external cases stay in HED's planned denominator. Cassette miss, runner error, hash mismatch, missing human target, or inability to reconstruct a case is a protocol failure, not an Agent failure.
- HED uses the already-frozen Repair prompts without result-driven tuning, `max_steps=4`, `run_regression=False`, and exactly `ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1")`.
- HED RECORD writes only to `cassettes/hed/pre-m4-1/`; REPLAY and tests make zero network calls. Files outside that root, especially historical M2 cassettes, are never rewritten or relabeled.
- The HED evidence stores every proposed final typed Patch, every request hash, the deterministic verifier result, both semantic delta sets, raw/normalized distance, and a content hash. A failed verifier cannot be represented as a usable Agent target.
- Bootstrap intervals use a frozen stdlib percentile protocol: seed `20260712`, `10_000` resamples, two-sided 95%, sorted sample statistics, lower index `floor(0.025 * (B-1))`, upper index `ceil(0.975 * (B-1))`.
- QA uses exactly four defect-class matched pairs and eight sessions, one `manual` and one `assisted` session per pair. Split/arm/order are frozen before the first session and counterbalanced across the four pairs.
- Both QA arms receive the same upstream subject and same before source files. Manual receives no GameForge Finding, IR, Agent Patch, target locator, predicate evidence, after file, or upstream patch. Assisted additionally receives only the GameForge Finding and HED Agent proposal/result.
- QA session time comes only from ordered `time.monotonic_ns()` events with the state machine `start -> (pause -> resume)* -> finish`. Active time excludes paused intervals; elapsed time does not. Each session has an active cap of 480 seconds and the total design cap is 3,840 seconds.
- A timeout or incorrect final patch is a valid QA outcome. Missing events, invalid state transition, missing final patch, arm contamination attestation failure, or missing deterministic correctness verdict is a protocol failure.
- QA correctness uses the same source submission for both arms and requires: lossless parse, native parser success, independent predicate clear, target-class generic Finding clear, and no new generic deterministic Finding relative to the frozen before case.
- QA reports paired active-minute differences, paired percentage differences, their mean/median/bootstrap CI, and arm success rates with Wilson CIs. It may claim savings only when the paired-minute CI lower bound is greater than zero and assisted success rate is not below manual; otherwise the conclusion is `inconclusive` or `negative`.
- Participant evidence is a one-person, eight-case study only. No field, text, or UI may generalize it to industry-wide productivity.
- This plan does not implement Cost/Latency aggregation, BenchReport v2, combined M3 acceptance, M4 UI, RBAC, approval infrastructure, or a storage service.
- Every task ends with `git diff --check`; commits contain no AI attribution.

---

### Task 1: Reconstructable External Case Runtime

**Files:**
- Modify: `gameforge/bench/external_cases/endless_sky_runner.py`
- Modify: `gameforge/bench/external_cases/__init__.py`
- Create: `tests/bench/external_cases/test_case_runtime.py`

**Interfaces:**
- Consumes: `ExternalCaseSpec`, frozen case `context.json`, lossless source trees, `EndlessSkyTxtAdapter`, `GraphChecker`, and `ASPChecker`.
- Produces: source-specific `EndlessSkyCaseRuntime`, `load_case_runtime()`, and `validate_submitted_tree()` for use only by composition-layer harnesses.

- [ ] **Step 1: Write failing runtime reconstruction tests**

```python
def test_case_runtime_reconstructs_the_manifest_bound_snapshots_and_finding():
    manifest = load_manifest(MANIFEST)
    case = manifest.cases[0]
    runtime = load_case_runtime(CORPUS, case.spec)
    assert runtime.spec == case.spec
    assert runtime.before_snapshot.snapshot_id != runtime.human_target_snapshot.snapshot_id
    assert runtime.target_entity_ids == case.target_entity_ids
    assert runtime.target_finding.defect_class == case.spec.defect_class.value
    assert set(runtime.target_finding.entities) & set(case.target_entity_ids)
    assert runtime.before_tree.tree_sha256 == case.before_tree.tree_sha256
    assert runtime.human_target_tree.tree_sha256 == case.after_tree.tree_sha256


def test_submission_verdict_uses_the_same_rules_for_before_and_upstream_after():
    runtime = load_case_runtime(CORPUS, manifest.cases[0].spec)
    before = validate_submitted_tree(runtime, runtime.before_raw)
    after = validate_submitted_tree(runtime, runtime.human_target_raw)
    assert before.correct is False
    assert before.predicate_status == "violation"
    assert after.correct is True
    assert after.predicate_status == "clear"
```

Also cover all eight cases, stable finding selection (`graph` before `asp`, then Finding ID), target preservation, lossless reader failure, native parser failure, unproven predicate, remaining target-class Finding, and newly introduced deterministic Finding.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_case_runtime.py -q`

Expected: import failures because the runtime and submission verdict interfaces do not exist.

- [ ] **Step 3: Add the source-specific runtime boundary**

Implement immutable dataclasses with these public shapes:

```python
@dataclass(frozen=True)
class EndlessSkyCaseRuntime:
    spec: ExternalCaseSpec
    context: dict[str, Any]
    before_raw: dict[str, bytes]
    human_target_raw: dict[str, bytes]
    before_tree: TreeArtifact
    human_target_tree: TreeArtifact
    before_snapshot: Snapshot
    human_target_snapshot: Snapshot
    target_entity_ids: tuple[str, ...]
    target_finding: Finding


@dataclass(frozen=True)
class SubmissionVerdict:
    correct: bool
    reader_round_trip: bool
    native_exit_code: int | None
    predicate_status: Literal["violation", "clear", "unproven"]
    target_finding_clear: bool
    new_deterministic_findings: tuple[tuple[str, tuple[str, ...]], ...]
    submitted_tree_sha256: str | None
    failure_reason: str | None
```

`load_case_runtime(corpus, spec, *, native_binary=None)` must use the same context/target helpers already used by `_case_evidence`, rebuild both snapshots, choose a confirmed matching Finding that intersects `target_entity_ids`, and fail if there is no unique stable choice. Extract shared private construction logic rather than copy it.

`validate_submitted_tree(runtime, raw_by_path, *, native_binary=None)` reparses exactly `spec.changed_paths`, verifies byte round-trip, evaluates the registered source predicate, adapts to generic IR, checks target preservation, compares generic deterministic Finding keys against the before baseline, and returns a total verdict instead of raising for participant mistakes. Infrastructure/hash/configuration failures still raise.

- [ ] **Step 4: Refactor the external manifest runner through the shared runtime path**

`_case_evidence()` must consume `load_case_runtime()` so external qualification and HED/QA cannot silently build different before/after IR. Replaying the corpus must remain byte-identical to the committed `external-corpus-manifest.json`.

- [ ] **Step 5: Run focused and replay tests**

Run:

```bash
uv run pytest tests/bench/external_cases -q
uv run python -m gameforge.bench.external_cases.endless_sky_runner --corpus scenarios/external_cases/endless_sky
git diff --exit-code -- scenarios/external_cases/endless_sky/external-corpus-manifest.json
```

Expected: tests pass and external evidence bytes do not change.

- [ ] **Step 6: Commit the reconstructable runtime**

```bash
git add gameforge/bench/external_cases/endless_sky_runner.py \
  gameforge/bench/external_cases/__init__.py \
  tests/bench/external_cases/test_case_runtime.py
git diff --cached --check
git commit -m "refactor(bench): expose reconstructable external cases"
```

---

### Task 2: Semantic Delta and Shared Bootstrap Statistics

**Files:**
- Create: `gameforge/bench/stats.py`
- Create: `gameforge/bench/hed/__init__.py`
- Create: `gameforge/bench/hed/delta.py`
- Create: `tests/bench/test_evidence_stats.py`
- Create: `tests/bench/hed/__init__.py`
- Create: `tests/bench/hed/test_delta.py`

**Interfaces:**
- Consumes: arbitrary `Snapshot` values and finite numeric samples.
- Produces: `AtomicDelta`, `semantic_delta()`, `symmetric_difference_distance()`, `percentile_bootstrap_ci()`, and `percentile()`.

- [ ] **Step 1: Write failing property and unit tests**

```python
def test_round_trip_envelope_and_source_refs_do_not_count_as_semantic_edits():
    before = snapshot(entity(raw="YQ==", source_row=1), relation(source_row=2))
    after = snapshot(entity(raw="Yg==", source_row=99), relation(source_row=100))
    assert semantic_delta(before, after) == ()


def test_relation_ids_do_not_change_relation_semantics():
    before = snapshot(relation_id="source-line-10", edge="requires", src="q", dst="gate")
    after = snapshot(relation_id="source-line-40", edge="requires", src="q", dst="gate")
    assert semantic_delta(before, after) == ()


def test_jaccard_symmetric_difference_is_bounded_and_exact():
    human = (delta("delete_relation", "a"), delta("add_relation", "b"))
    agent = (delta("delete_relation", "a"), delta("add_relation", "c"))
    raw, normalized = symmetric_difference_distance(agent, human)
    assert raw == 2
    assert normalized == pytest.approx(2 / 3)


def test_percentile_bootstrap_is_seeded_and_uses_the_declared_indexes():
    first = percentile_bootstrap_ci([1.0, 2.0, 4.0, 8.0], statistics.mean)
    second = percentile_bootstrap_ci([1.0, 2.0, 4.0, 8.0], statistics.mean)
    assert first == second
    assert first.method == "percentile-bootstrap95"
    assert first.seed == 20260712 and first.resamples == 10_000
```

Use Hypothesis to prove semantic delta ordering is stable under entity/relation insertion order, identical snapshots have an empty delta, Jaccard distance is symmetric and bounded in `[0,1]`, and bootstrap output is finite for every nonempty finite sample.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/bench/test_evidence_stats.py tests/bench/hed/test_delta.py -q`

Expected: import failures for the new modules.

- [ ] **Step 3: Implement the exact semantic projection**

```python
_NON_SEMANTIC_ATTRS = frozenset({
    "source_chunk_b64", "source_kind", "source_name", "source_order", "reader_version",
})

@dataclass(frozen=True, order=True)
class AtomicDelta:
    kind: Literal["add_entity", "delete_entity", "set_entity_attr", "add_relation", "delete_relation"]
    target: str
    field: str | None
    old_json: str | None
    new_json: str | None
```

Entity identity is `entity.id`; include type and every semantic attribute. Relation projection is a sorted multiset of canonical JSON payloads `{type, src_id, dst_id, attrs}` with occurrence ordinals added after sorting. Attribute values use the existing `canonical_json()` function. Return a sorted tuple and reject snapshots containing non-canonical non-finite values rather than stringifying them.

`symmetric_difference_distance()` treats the delta tuples as sets, rejects duplicate deltas, returns `(raw_count, normalized)`, and defines empty/empty as `(0, 0.0)`.

- [ ] **Step 4: Implement the frozen bootstrap protocol**

```python
@dataclass(frozen=True)
class BootstrapInterval:
    low: float
    high: float
    method: Literal["percentile-bootstrap95"] = "percentile-bootstrap95"
    seed: int = 20260712
    resamples: int = 10_000
```

Use a fresh `random.Random(seed)` per call. Draw `n` indexes with replacement for each resample, sort the `10_000` statistic values, and select the exact lower/upper indexes declared in Global Constraints. `percentile()` uses linear interpolation and is shared by HED/QA medians only where no bootstrap is involved.

- [ ] **Step 5: Run tests and commit**

```bash
uv run pytest tests/bench/test_evidence_stats.py tests/bench/hed/test_delta.py -q
git add gameforge/bench/stats.py gameforge/bench/hed tests/bench/test_evidence_stats.py tests/bench/hed
git diff --cached --check
git commit -m "feat(bench): define semantic edit distance"
```

---

### Task 3: Frozen HED Protocol and Evidence Contracts

**Files:**
- Create: `gameforge/bench/hed/contracts.py`
- Create: `gameforge/bench/hed/protocol.py`
- Create: `tests/bench/hed/test_contracts.py`
- Create: `tests/bench/hed/test_protocol.py`

**Interfaces:**
- Consumes: external manifest hash, current Repair prompt bundle, `DEFAULT_SNAPSHOT`, and Task 2 delta/stat types.
- Produces: `HedProtocol`, `HedCaseOutcome`, `HedMetric`, `HedEvidenceManifest`, canonical load/write/seal/validate helpers.

- [ ] **Step 1: Write failing strict-contract tests**

```python
def test_protocol_binds_gpt56_prompts_external_denominator_and_metric_rules():
    protocol = seal_protocol(load_manifest(EXTERNAL_MANIFEST))
    assert protocol.model_snapshot == ModelSnapshot(
        provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1"
    )
    assert protocol.external_case_count == 8
    assert protocol.max_steps == 4
    assert protocol.run_regression is False
    assert protocol.distance_metric == "semantic-jaccard-symmetric-difference@1"
    assert protocol.bootstrap_seed == 20260712
    assert protocol.bootstrap_resamples == 10_000


def test_unusable_agent_is_measured_as_empty_delta_not_dropped():
    outcome = seal_outcome(
        status="agent_unusable",
        human_delta=(delta("delete_relation", "r"),),
        agent_delta=(),
        patch=failed_patch(),
        passed_verification=False,
    )
    assert outcome.raw_distance == 1
    assert outcome.normalized_distance == 1.0
    assert outcome.disposition == "unusable"


def test_protocol_failure_cannot_carry_a_fake_distance_or_agent_target():
    payload = valid_hed_outcome_payload()
    payload.update(
        status="protocol_failure",
        disposition="protocol_failure",
        normalized_distance=0.0,
        raw_distance=0,
        failure_reason="cassette miss",
    )
    with pytest.raises(ValidationError):
        HedCaseOutcome.model_validate(payload)
```

Also reject extra fields, duplicate/unsorted case IDs and deltas, invalid hashes, missing upstream target, usable Patch without passed verification, unusable without a retained Patch when one was proposed, mismatched `planned_n/evaluated_n`, and any metric not rederived from outcomes.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/bench/hed/test_contracts.py tests/bench/hed/test_protocol.py -q`

Expected: imports fail for the HED contracts and protocol.

- [ ] **Step 3: Implement the frozen protocol**

```python
class HedProtocol(_StrictModel):
    schema_version: Literal["hed-protocol@1"] = "hed-protocol@1"
    external_manifest_sha256: Sha256
    external_case_ids: tuple[StableId, ...]
    external_case_count: Literal[8]
    repair_prompt_version: StableId
    repair_prompt_bundle_sha256: Sha256
    model_snapshot: ModelSnapshot
    max_steps: Literal[4] = 4
    run_regression: Literal[False] = False
    semantic_delta_version: Literal["semantic-ir-delta@1"]
    distance_metric: Literal["semantic-jaccard-symmetric-difference@1"]
    bootstrap_seed: Literal[20260712]
    bootstrap_resamples: Literal[10000]
    frozen: Literal[True] = True
    protocol_sha256: Sha256
```

The prompt bundle contains `repair.system` and `repair.refine`, sorted by name, exact version, and exact text. `assert_protocol_ready()` rechecks prompt bytes, model snapshot, external manifest canonical bytes/hash, eight exact case IDs, and every frozen constant before any RECORD call.

- [ ] **Step 4: Implement denominator-safe evidence contracts**

Use these outcome states and invariants:

```python
HedOutcomeStatus = Literal["evaluated", "agent_unusable", "protocol_failure"]
HedDisposition = Literal["unchanged", "edited", "unusable", "protocol_failure"]

class HedCaseOutcome(_StrictModel):
    case_id: StableId
    external_case_evidence_sha256: Sha256
    protocol_sha256: Sha256
    status: HedOutcomeStatus
    disposition: HedDisposition
    before_snapshot_id: NonEmptyStr
    human_target_snapshot_id: NonEmptyStr
    target_finding: Finding
    request_hashes: tuple[RequestHash, ...]
    search_steps: int
    patch: Patch | None
    patch_sha256: Sha256 | None
    passed_verification: bool
    agent_target_snapshot_id: NonEmptyStr | None
    human_delta: tuple[AtomicDeltaModel, ...]
    agent_delta: tuple[AtomicDeltaModel, ...]
    raw_distance: int | None
    normalized_distance: float | None
    failure_reason: NonEmptyStr | None
    outcome_sha256: Sha256
```

`HedMetric` contains `planned_n=8`, `evaluated_n`, mean, median, primary estimate (mean normalized distance), bootstrap mean CI, raw mean/median, and counts for unchanged/edited/unusable/protocol failure. `HedEvidenceManifest` binds protocol, external manifest, GPT-5.6 snapshot, all eight outcomes, metric, and `evidence_sha256`.

- [ ] **Step 5: Run tests and commit**

```bash
uv run pytest tests/bench/hed/test_contracts.py tests/bench/hed/test_protocol.py -q
git add gameforge/bench/hed/contracts.py gameforge/bench/hed/protocol.py \
  tests/bench/hed/test_contracts.py tests/bench/hed/test_protocol.py
git diff --cached --check
git commit -m "feat(bench): freeze human edit distance protocol"
```

---

### Task 4: HED Repair RECORD/REPLAY Harness

**Files:**
- Create: `gameforge/bench/hed/harness.py`
- Create: `gameforge/bench/external_cases/endless_sky_hed.py`
- Create: `tests/bench/hed/test_harness.py`
- Create: `tests/bench/external_cases/test_endless_sky_hed.py`
- Create: `tests/architecture/test_human_evidence_boundaries.py`

**Interfaces:**
- Consumes: source-neutral `HedCaseInput`, HED protocol/contracts, `repair_search()`, `GraphChecker`, `ASPChecker`, `ModelRouter`, and `CassetteStore`. The separate `endless_sky_hed.py` composition module converts `EndlessSkyCaseRuntime` values to `HedCaseInput` and owns CLI wiring.
- Produces: generic `run_hed_cases()`, `build_hed_evidence()`, `record_router()`, `replay_router()`, source-specific CLI seal/record/replay/validate actions, and a complete request-hash trace.

- [ ] **Step 1: Write failing harness tests with fake routers**

```python
def test_verified_patch_becomes_a_measured_agent_target(fake_case, passing_router):
    outcome = run_hed_case(fake_case, passing_router, protocol())
    assert outcome.status == "evaluated"
    assert outcome.passed_verification is True
    assert outcome.agent_target_snapshot_id is not None
    assert outcome.request_hashes
    assert outcome.normalized_distance is not None


def test_failed_search_retains_final_patch_and_scores_empty_agent_delta(fake_case, failing_router):
    outcome = run_hed_case(fake_case, failing_router, protocol())
    assert outcome.status == "agent_unusable"
    assert outcome.patch is not None
    assert outcome.agent_delta == ()
    assert outcome.normalized_distance == 1.0


def test_cassette_miss_remains_in_the_eight_case_denominator(fake_case, miss_router):
    outcome = run_hed_case(fake_case, miss_router, protocol())
    assert outcome.status == "protocol_failure"
    assert outcome.normalized_distance is None
    assert outcome.request_hashes == (EXPECTED_MISS_HASH,)
```

Also test multiple refinement request hashes are preserved in call order, a malformed Patch never becomes a target, stable case ordering, all eight outcomes required, replay makes no transport call, RECORD requires both environment gate and key, and canonical evidence bytes are identical across two processes.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/bench/hed/test_harness.py tests/bench/external_cases/test_endless_sky_hed.py tests/architecture/test_human_evidence_boundaries.py -q`

Expected: imports fail for the harness and boundary test.

- [ ] **Step 3: Implement a request-tracking router facade**

```python
class TrackingRouter:
    def __init__(self, router: ModelRouter) -> None:
        self._router = router
        self.request_hashes: list[str] = []

    @property
    def default_model_snapshot(self) -> ModelSnapshot | None:
        return self._router.default_model_snapshot

    def call(self, request: ModelRequest) -> ModelResponse:
        digest = request_hash(request)
        self.request_hashes.append(digest)
        return self._router.call(request)
```

Do not change `PatchDraft` or the historical Repair Agent API merely to expose benchmark tracing.

- [ ] **Step 4: Implement deterministic case execution**

The generic harness consumes this immutable input, with no source profile import:

```python
@dataclass(frozen=True)
class HedCaseInput:
    case_id: str
    external_case_evidence_sha256: str
    before_snapshot: Snapshot
    human_target_snapshot: Snapshot
    target_finding: Finding
```

For each input in sorted case-ID order:

1. Recheck its external case evidence hash and reconstruct both snapshots.
2. Derive the human semantic delta from before to upstream after.
3. Run `repair_search(target_finding, before, [GraphChecker(), ASPChecker()], tracker, max_steps=4, run_regression=False)`.
4. A `passed_verification=True` Patch must apply exactly to the before snapshot; recompute generic findings and reject it if the target class remains, a new deterministic Finding appears, or a protected target entity disappears.
5. Derive Agent delta only for that independently revalidated target.
6. Map failed search to `agent_unusable`, retaining the final Patch and hashes but scoring an empty Agent delta.
7. Map cassette/infrastructure/hash errors to `protocol_failure` with null distance; never collapse those into Agent quality.

- [ ] **Step 5: Implement generic RECORD/REPLAY plus the source composition CLI**

```text
python -m gameforge.bench.external_cases.endless_sky_hed --seal-protocol
GAMEFORGE_LLM_LIVE=1 python -m gameforge.bench.external_cases.endless_sky_hed --record
python -m gameforge.bench.external_cases.endless_sky_hed --replay --output <path>
python -m gameforge.bench.external_cases.endless_sky_hed --validate-evidence <path>
```

Use `cassettes/hed/pre-m4-1`, `OpenAIResponsesTransport`, `resume=True`, eight retries, three-second exponential backoff, and `DEFAULT_SNAPSHOT`. The no-live transport must raise if REPLAY ever attempts network access.

- [ ] **Step 6: Add architecture guards**

AST/text tests enforce:

```text
gameforge/bench/hed/** contains no endless_sky, flare, source path, frozen case ID, commit OID, or source predicate import
gameforge/bench/qa/** follows the same rule once it exists
gameforge/agents/repair/** imports no gameforge.bench or external-case package
gameforge/spine/** imports no gameforge.agents or gameforge.bench
only gameforge/bench/external_cases/endless_sky_hed.py imports both the generic HED harness and Endless Sky runtime
```

- [ ] **Step 7: Run tests and commit**

```bash
uv run pytest tests/bench/hed/test_harness.py tests/bench/external_cases/test_endless_sky_hed.py tests/architecture/test_human_evidence_boundaries.py -q
git add gameforge/bench/hed/harness.py gameforge/bench/external_cases/endless_sky_hed.py \
  tests/bench/hed/test_harness.py tests/bench/external_cases/test_endless_sky_hed.py \
  tests/architecture/test_human_evidence_boundaries.py
git diff --cached --check
git commit -m "feat(bench): add replayable edit distance harness"
```

---

### Task 5: Freeze and Measure Eight-Case HED Evidence

**Files:**
- Create: `scenarios/external_cases/endless_sky/hed-protocol.json`
- Create: `scenarios/external_cases/endless_sky/hed-evidence.json`
- Create: `cassettes/hed/pre-m4-1/*.json`
- Create: `tests/bench/hed/test_measured_evidence.py`

**Interfaces:**
- Consumes: Tasks 1-4, all eight frozen cases, the current frozen Repair prompts, and the local GPT-5.6 gateway.
- Produces: immutable GPT-5.6 HED cassettes and canonical eight-case evidence.

- [ ] **Step 1: Add acceptance tests before live recording**

```python
def test_measured_hed_evidence_is_complete_and_rederivable():
    protocol = load_protocol(PROTOCOL)
    evidence = load_evidence(EVIDENCE)
    external = load_manifest(EXTERNAL)
    validate_evidence(evidence, protocol, external, load_all_case_runtimes())
    assert evidence.model_snapshot == ModelSnapshot(
        provider="openai", model="gpt-5.6-sol", snapshot_tag="pre-m4@1"
    )
    assert len(evidence.outcomes) == 8
    assert {item.case_id for item in evidence.outcomes} == {
        item.spec.case_id for item in external.cases
    }
    assert evidence.metric.planned_n == 8
    assert evidence.metric.evaluated_n + evidence.metric.protocol_failure == 8
    assert all(item.human_delta for item in evidence.outcomes)
```

Also rehash every referenced external case, Patch, outcome, cassette record, and manifest; assert every request hash resolves under `cassettes/hed/pre-m4-1`; assert no HED cassette uses Opus; and assert `git diff --exit-code -- cassettes ':!cassettes/hed'`.

- [ ] **Step 2: Run acceptance and verify RED**

Run: `uv run pytest tests/bench/hed/test_measured_evidence.py -q`

Expected: fail because the frozen protocol, cassettes, and evidence do not exist.

- [ ] **Step 3: Seal the HED protocol before the first call**

Run: `uv run python -m gameforge.bench.external_cases.endless_sky_hed --seal-protocol`

Expected: writes canonical `hed-protocol.json`, validates the exact eight-case denominator and GPT-5.6 policy, and prints `protocol_sha256`.

- [ ] **Step 4: Record all eight cases with resume**

Run: `GAMEFORGE_LLM_LIVE=1 uv run python -m gameforge.bench.external_cases.endless_sky_hed --record`

Expected: records at most 32 Repair calls, never edits the frozen protocol, never drops an Agent-unusable result, and writes canonical evidence. Gateway interruption is handled by rerunning the identical command with cassette resume.

- [ ] **Step 5: Perform two independent zero-network replays**

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_hed --replay --output /tmp/hed-replay-a.json
uv run python -m gameforge.bench.external_cases.endless_sky_hed --replay --output /tmp/hed-replay-b.json
cmp /tmp/hed-replay-a.json /tmp/hed-replay-b.json
cmp /tmp/hed-replay-a.json scenarios/external_cases/endless_sky/hed-evidence.json
```

Expected: both comparisons exit 0.

- [ ] **Step 6: Run HED acceptance and commit immutable evidence**

```bash
uv run pytest tests/bench/hed tests/architecture/test_human_evidence_boundaries.py -q
git add scenarios/external_cases/endless_sky/hed-protocol.json \
  scenarios/external_cases/endless_sky/hed-evidence.json \
  cassettes/hed/pre-m4-1 tests/bench/hed/test_measured_evidence.py
git diff --cached --check
git commit -m "test(bench): measure human edit distance"
```

Record the actual mean, median, CI, disposition counts, protocol SHA, evidence SHA, and cassette count in this plan after the commit. Do not tune or rerun based on the measured score.

---

### Task 6: Frozen Matched-Pair QA Protocol

**Files:**
- Create: `gameforge/bench/qa/__init__.py`
- Create: `gameforge/bench/qa/contracts.py`
- Create: `gameforge/bench/qa/protocol.py`
- Create: `tests/bench/qa/__init__.py`
- Create: `tests/bench/qa/test_contracts.py`
- Create: `tests/bench/qa/test_protocol.py`
- Create: `scenarios/external_cases/endless_sky/qa-protocol.json`

**Interfaces:**
- Consumes: external manifest and measured HED evidence.
- Produces: `QaProtocol`, exact eight-session counterbalanced schedule, strict timer/verdict/session evidence contracts, canonical seal/load/write helpers.

- [ ] **Step 1: Write failing protocol and state-contract tests**

```python
def test_schedule_has_four_complete_counterbalanced_pairs():
    protocol = seal_qa_protocol(external(), hed())
    assert len(protocol.sessions) == 8
    assert len({item.pair_id for item in protocol.sessions}) == 4
    for pair_id in {item.pair_id for item in protocol.sessions}:
        pair = [item for item in protocol.sessions if item.pair_id == pair_id]
        assert {item.arm for item in pair} == {"manual", "assisted"}
        assert len({item.defect_class for item in pair}) == 1
        assert {item.split for item in pair} == {"development", "verification"}
    assert sum(item.arm == "assisted" and item.split == "development" for item in protocol.sessions) == 2
    assert sum(item.arm == "assisted" and item.order <= 4 for item in protocol.sessions) == 2


def test_session_events_require_a_valid_monotonic_state_machine():
    values = valid_qa_session_values()
    values["events"] = (start(100), pause(200), resume(400), finish(700))
    session = QaSessionEvidence.seal(**values)
    assert session.active_ns == 400
    assert session.elapsed_ns == 600
    invalid = valid_qa_session_values()
    invalid["events"] = (start(100), resume(200), finish(300))
    with pytest.raises(ValidationError):
        QaSessionEvidence.seal(**invalid)
```

Also reject duplicate cases/orders, mixed classes within a pair, missing arm, HED/external hash mismatch, assisted case without a matching HED outcome, non-increasing monotonic values, finish without final patch/verdict, contaminated arm marked valid, and derived durations that do not rederive from events.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/bench/qa/test_contracts.py tests/bench/qa/test_protocol.py -q`

Expected: imports fail for the QA package.

- [ ] **Step 3: Implement the frozen schedule algorithm**

Sort the four defect classes by enum value and pair each class's development/verification cases. Apply this exact four-row pattern by class index:

```text
0: development/manual first, verification/assisted second
1: development/assisted first, verification/manual second
2: verification/manual first, development/assisted second
3: verification/assisted first, development/manual second
```

Global orders are pair order then within-pair order (`1..8`). Bind `participant_id="participant-01"`, `active_cap_ns=480_000_000_000`, `total_active_cap_ns=3_840_000_000_000`, external manifest SHA, HED evidence SHA, correctness protocol ID, schedule, and protocol SHA. The generated file is frozen before Task 8 starts.
`seal_qa_protocol()` must reject HED evidence with any protocol-failure outcome or
`metric.evaluated_n != 8`; an Agent-unusable but protocol-valid HED outcome remains
eligible and is shown honestly in the assisted arm.

- [ ] **Step 4: Implement strict session/verdict contracts**

```python
class QaEvent(_StrictModel):
    kind: Literal["start", "pause", "resume", "finish"]
    monotonic_ns: int = Field(ge=0)

class QaCorrectnessVerdict(_StrictModel):
    correct: bool
    reader_round_trip: bool
    native_exit_code: int | None
    predicate_status: Literal["violation", "clear", "unproven"]
    target_finding_clear: bool
    new_deterministic_findings: tuple[FindingKey, ...]
    submitted_tree_sha256: Sha256 | None
    verdict_sha256: Sha256

class QaSessionEvidence(_StrictModel):
    schema_version: Literal["qa-session@1"]
    protocol_sha256: Sha256
    session_id: StableId
    participant_id: Literal["participant-01"]
    case_id: StableId
    pair_id: StableId
    arm: Literal["manual", "assisted"]
    order: int
    events: tuple[QaEvent, ...]
    active_ns: int
    elapsed_ns: int
    capped_active_ns: int
    timed_out: bool
    final_patch_path: str
    final_patch_sha256: Sha256
    participant_attested_no_contamination: bool
    verdict: QaCorrectnessVerdict
    protocol_valid: bool
    failure_reasons: tuple[NonEmptyStr, ...]
    evidence_sha256: Sha256
```

- [ ] **Step 5: Seal protocol, run tests, and commit**

```bash
uv run pytest tests/bench/qa/test_contracts.py tests/bench/qa/test_protocol.py -q
uv run python -m gameforge.bench.qa.protocol --seal
git add gameforge/bench/qa tests/bench/qa \
  scenarios/external_cases/endless_sky/qa-protocol.json
git diff --cached --check
git commit -m "feat(bench): freeze QA hours protocol"
```

---

### Task 7: Arm-Isolated QA Bundle, Timer, and Correctness CLI

**Files:**
- Create: `gameforge/bench/external_cases/endless_sky_qa.py`
- Create: `gameforge/bench/qa/session.py`
- Create: `gameforge/bench/qa/harness.py`
- Create: `tests/bench/external_cases/test_endless_sky_qa.py`
- Create: `tests/bench/qa/test_session.py`
- Create: `tests/bench/qa/test_harness.py`

**Interfaces:**
- Consumes: `QaProtocol`, HED evidence, source-specific runtime/verdict boundary, filesystem bundle root, monotonic clock callable.
- Produces: one-session-at-a-time task bundles, append-safe canonical session state, unified final patch, deterministic correctness verdict, and CLI actions.

- [ ] **Step 1: Write failing bundle-isolation tests**

```python
def test_manual_bundle_contains_no_gameforge_or_answer_material(tmp_path):
    bundle = prepare_session(protocol(), manual_session(), tmp_path)
    names = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
    assert "TASK.json" in names
    assert all("finding" not in name.casefold() for name in names)
    assert all("agent" not in name.casefold() for name in names)
    payload = "\n".join(path.read_text(errors="ignore") for path in bundle.rglob("*") if path.is_file())
    assert "target_locators" not in payload
    assert "predicate" not in payload
    assert "upstream.patch" not in payload


def test_assisted_bundle_adds_only_finding_and_agent_proposal(tmp_path):
    bundle = prepare_session(protocol(), assisted_session(), tmp_path)
    assistance = json.loads((bundle / "GAMEFORGE.json").read_text())
    assert set(assistance) == {"finding", "agent_patch", "passed_verification", "disposition"}
```

Both bundles must include only the exact before `changed_paths`, a source-neutral `TASK.json` containing session/case/order/arm/upstream subject, a `work/` directory, and the same compiled syntax-only native parser under `tools/`. Neither includes after bytes, upstream patch, target locator, predicate context, or another session.

- [ ] **Step 2: Write failing timer/finish tests with injected clock**

Cover start/pause/resume/finish, invalid transitions, duplicate finish, timeout cap, paused time exclusion, submission tree path traversal rejection, changed-path enforcement, unified diff generation, verdict persistence, attestation false -> protocol failure, and atomic write via temporary file plus `os.replace`.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest tests/bench/external_cases/test_endless_sky_qa.py \
  tests/bench/qa/test_session.py tests/bench/qa/test_harness.py -q
```

Expected: imports fail for the new CLI/session modules.

- [ ] **Step 4: Implement source-specific materialization and verdict composition**

`endless_sky_qa.py` is the only QA module allowed to import the Endless Sky reader/Adapter/predicate/native code. It implements the following concrete composition functions; `write_arm_bundle()` and `seal_qa_verdict()` are generic helpers defined in `gameforge.bench.qa.harness`:

```python
def materialize_case(
    case_id: str,
    destination: Path,
    *,
    assisted: HedCaseOutcome | None,
) -> Path:
    runtime = load_runtime_by_case_id(case_id)
    native_binary = compile_qa_native_parser(destination / "tools")
    return write_arm_bundle(runtime, destination, native_binary, assisted=assisted)


def evaluate_submission(
    case_id: str,
    work_root: Path,
) -> tuple[bytes, QaCorrectnessVerdict]:
    runtime = load_runtime_by_case_id(case_id)
    submitted = read_exact_changed_paths(runtime.spec, work_root)
    patch = unified_submission_patch(runtime.before_raw, submitted)
    verdict = validate_submitted_tree(runtime, submitted)
    return patch, seal_qa_verdict(verdict)
```

The patch is a deterministic unified diff from frozen before bytes to submission bytes with POSIX paths, LF diff metadata, and stable path order. The submission verdict delegates to Task 1 and seals every field/hash.

- [ ] **Step 5: Implement generic session lifecycle and CLI**

```text
python -m gameforge.bench.external_cases.endless_sky_qa next --workspace <outside-repo-path>
python -m gameforge.bench.external_cases.endless_sky_qa start --workspace <outside-repo-path> --session <id>
python -m gameforge.bench.external_cases.endless_sky_qa pause --workspace <outside-repo-path> --session <id>
python -m gameforge.bench.external_cases.endless_sky_qa resume --workspace <outside-repo-path> --session <id>
python -m gameforge.bench.external_cases.endless_sky_qa finish --workspace <outside-repo-path> --session <id> --attest-no-contamination
python -m gameforge.bench.external_cases.endless_sky_qa status --workspace <outside-repo-path>
```

`next` refuses to expose order `N+1` before order `N` has finished. It creates only the current bundle and prints its absolute path, session ID, arm, active cap, and timer state. `finish` reads the bundle's `work/`, calls the source-specific evaluator through an injected callback, writes `final.patch` and canonical `session-evidence.json`, and never mutates frozen source fixtures.

- [ ] **Step 6: Run tests and commit**

```bash
uv run pytest tests/bench/external_cases/test_endless_sky_qa.py \
  tests/bench/qa/test_session.py tests/bench/qa/test_harness.py \
  tests/architecture/test_human_evidence_boundaries.py -q
git add gameforge/bench/external_cases/endless_sky_qa.py \
  gameforge/bench/qa/session.py gameforge/bench/qa/harness.py \
  tests/bench/external_cases/test_endless_sky_qa.py \
  tests/bench/qa/test_session.py tests/bench/qa/test_harness.py \
  tests/architecture/test_human_evidence_boundaries.py
git diff --cached --check
git commit -m "feat(bench): add matched-pair QA session harness"
```

---

### Task 8: Collect Eight Real QA Sessions and Score the Case Study

**Files:**
- Create: `gameforge/bench/qa/score.py`
- Create: `scenarios/external_cases/endless_sky/qa-sessions/*.json`
- Create: `scenarios/external_cases/endless_sky/qa-patches/*.patch`
- Create: `scenarios/external_cases/endless_sky/qa-evidence.json`
- Create: `tests/bench/qa/test_score.py`
- Create: `tests/bench/qa/test_measured_evidence.py`
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-human-evidence.md`

**Interfaces:**
- Consumes: all eight protocol-ordered real participant sessions.
- Produces: `QaPairOutcome`, `QaScore`, `QaEvidenceManifest`, canonical scoring/validation, and the measured one-participant case-study artifact.

- [ ] **Step 1: Write failing paired-score tests before collecting data**

```python
def test_score_uses_all_four_pairs_and_same_correctness_contract():
    score = score_sessions(protocol(), complete_sessions())
    assert score.planned_pairs == 4
    assert score.evaluated_pairs == 4
    assert len(score.pairs) == 4
    assert score.manual_success.n == score.assisted_success.n == 4


def test_savings_claim_requires_positive_lower_bound_and_no_success_regression():
    assert score_sessions(protocol(), clearly_faster_assisted()).conclusion == "savings"
    assert score_sessions(protocol(), wide_interval()).conclusion == "inconclusive"
    assert score_sessions(protocol(), assisted_less_correct()).conclusion != "savings"
    assert score_sessions(protocol(), assisted_slower()).conclusion == "negative"
```

Also test percentage denominator uses manual capped active time, manual zero time yields a protocol failure, incorrect/timed-out outcomes remain valid, any invalid session makes its pair unevaluated, metric/hash tampering fails, and session order/case/arm must exactly match the frozen protocol.

- [ ] **Step 2: Run score tests and verify RED**

Run: `uv run pytest tests/bench/qa/test_score.py tests/bench/qa/test_measured_evidence.py -q`

Expected: score import failure and missing measured evidence.

- [ ] **Step 3: Implement paired scoring**

```python
class QaPairOutcome(_StrictModel):
    pair_id: StableId
    defect_class: DefectClass
    manual_session_id: StableId
    assisted_session_id: StableId
    manual_minutes: float
    assisted_minutes: float
    saved_minutes: float
    saved_fraction: float
    manual_correct: bool
    assisted_correct: bool
    pair_sha256: Sha256

class QaScore(_StrictModel):
    planned_pairs: Literal[4]
    evaluated_pairs: int
    protocol_failure_pairs: int
    mean_saved_minutes: float | None
    median_saved_minutes: float | None
    saved_minutes_ci_low: float | None
    saved_minutes_ci_high: float | None
    mean_saved_fraction: float | None
    median_saved_fraction: float | None
    saved_fraction_ci_low: float | None
    saved_fraction_ci_high: float | None
    manual_success: BinaryMetric
    assisted_success: BinaryMetric
    conclusion: Literal["savings", "inconclusive", "negative", "failed"]
```

Use capped active minutes. `negative` requires `mean_saved_minutes < 0`; `failed` requires any protocol failure or fewer than four evaluated pairs; otherwise apply the strict savings rule from Global Constraints.

- [ ] **Step 4: Prepare an external QA workspace and conduct sessions in frozen order**

Use a directory outside the repository, for example `/tmp/gameforge-qa-participant-01`. For each order `1..8`:

1. Run `next`; do not inspect future sessions or repository after/upstream/predicate material.
2. Run `start` immediately before active work.
3. Use `pause`/`resume` for interruptions.
4. Edit only the current bundle's `work/` files. Manual uses the source files/editor/native parser only; assisted may also use `GAMEFORGE.json`.
5. Run `finish --attest-no-contamination`; timeout or incorrect is retained and does not authorize a retry.

The Agent cannot perform or simulate these participant sessions. If real participant evidence is not available, stop this task with the protocol and harness complete; do not fabricate times, attestations, or patches.

- [ ] **Step 5: Import, validate, and freeze session evidence**

Run:

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_qa import-evidence \
  --workspace /tmp/gameforge-qa-participant-01 \
  --output scenarios/external_cases/endless_sky
uv run python -m gameforge.bench.external_cases.endless_sky_qa validate-evidence \
  scenarios/external_cases/endless_sky/qa-evidence.json
```

Expected: exactly eight canonical session files and patches are copied by content hash, every verdict revalidates against the frozen submission, and `qa-evidence.json` reports four pairs without changing any session result.

- [ ] **Step 6: Run measured acceptance and commit**

```bash
uv run pytest tests/bench/qa -q
git add gameforge/bench/qa/score.py tests/bench/qa/test_score.py \
  tests/bench/qa/test_measured_evidence.py \
  scenarios/external_cases/endless_sky/qa-sessions \
  scenarios/external_cases/endless_sky/qa-patches \
  scenarios/external_cases/endless_sky/qa-evidence.json \
  docs/superpowers/plans/2026-07-12-pre-m4-human-evidence.md
git diff --cached --check
git commit -m "test(bench): measure QA hours case study"
```

Record exact arm times/successes, paired estimates/CIs, conclusion, evidence SHA, and protocol-validity count in this plan before committing. Do not repeat a session based on outcome.

---

### Task 9: Human-Evidence Slice Regression and Closure

**Files:**
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-human-evidence.md`
- Modify: `docs/superpowers/plans/README.md`
- Modify: `tests/architecture/test_human_evidence_boundaries.py`
- Create: `tests/bench/test_human_evidence_acceptance.py`

**Interfaces:**
- Consumes: frozen external, narrative, HED, and QA evidence plus all historical cassettes.
- Produces: a closed HED/QA slice ready for the separate Cost/Latency + BenchReport v2 plan.

- [ ] **Step 1: Add combined human-evidence acceptance**

```python
def test_pre_m4_human_evidence_slice_is_complete_and_honest():
    external = load_external()
    hed = load_hed()
    qa = load_qa()
    assert len(external.cases) == len(hed.outcomes) == 8
    assert hed.metric.planned_n == 8
    assert qa.score.planned_pairs == qa.score.evaluated_pairs == 4
    assert qa.score.manual_success.n == qa.score.assisted_success.n == 4
    assert all(item.verdict is not None for item in qa.sessions)
    assert report_language(qa).casefold().count("industry") == 0
```

Revalidate all nested hashes, all HED cassettes, all QA final patch bytes, all deterministic verdicts, both bootstrap intervals, and absence of source specialization in core HED/QA/Agent/checker code.

- [ ] **Step 2: Run focused historical/new regression**

```bash
uv run pytest tests/bench/hed tests/bench/qa tests/bench/external_cases \
  tests/bench/test_human_evidence_acceptance.py \
  tests/architecture/test_human_evidence_boundaries.py -q
git diff --exit-code -- cassettes ':!cassettes/hed'
```

Expected: all pass; only the dedicated HED cassette root is new.

- [ ] **Step 3: Run all repository gates**

```bash
uv run pytest -q
uv run pytest tests/test_dependency_lint.py -q
uv run ruff check gameforge tests
git diff --check
```

Expected: full suite passes with only already-declared skips, dependency gates pass, Ruff is clean, and whitespace diff is clean.

- [ ] **Step 4: Re-run HED and external evidence after the full suite**

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_hed --replay --output /tmp/hed-final.json
cmp /tmp/hed-final.json scenarios/external_cases/endless_sky/hed-evidence.json
uv run python -m gameforge.bench.external_cases.endless_sky_runner --corpus scenarios/external_cases/endless_sky
git diff --exit-code -- scenarios/external_cases/endless_sky/external-corpus-manifest.json
```

Expected: byte-identical HED REPLAY and external deterministic evidence.

- [ ] **Step 5: Mark this plan complete and commit closure**

Mark Tasks 1-9 `[x]` only after their commands pass. Update `plans/README.md` to say HED and QA-hours are complete while Cost/Latency, BenchReport v2, combined acceptance, and final pre-M4 audit remain. Do not mark M3 complete or begin M4.

```bash
git add docs/superpowers/plans/2026-07-12-pre-m4-human-evidence.md \
  docs/superpowers/plans/README.md \
  tests/architecture/test_human_evidence_boundaries.py \
  tests/bench/test_human_evidence_acceptance.py
git diff --cached --check
git commit -m "test(bench): close human evidence slice"
```

---

## Plan Self-Review

- Every approved HED requirement maps to a task: exact eight-case denominator and upstream human targets (1/3/5), semantic atomic deltas and symmetric-difference distance (2), Agent-unusable/protocol-failure separation (3/4), GPT-5.6 RECORD/REPLAY and immutable evidence (4/5), and mean/median/bootstrap/disposition reporting (3/5).
- Every approved QA requirement maps to a task: four same-class matched pairs and counterbalancing (6), arm isolation and monotonic timing (6/7), identical deterministic correctness in both arms (1/7), real participant-only sessions (8), paired minutes/percentage/CIs/success and conservative conclusion language (8), and no industry extrapolation (8/9).
- `bench/hed` and `bench/qa` remain game-neutral. Source-specific parsing, Adapter context, predicates, native witness, fixtures, and submission validation stay under `bench/external_cases` and are injected only at harness composition.
- The plan changes no foundational Finding/Patch/IR/Env contract and does not add a plugin registry, approval state machine, nonce, lock, database, service, or M4 platform feature.
- New Agent evidence and historical Agent evidence have separate model snapshots and cassette roots. Historical Opus bytes are guarded before slice closure.
- The only unavoidable user action is the real QA case study. Its harness, frozen schedule, tasks, timers, and deterministic verdicts are completed first; no synthetic person, inferred duration, or Agent-operated substitute is permitted.
- Cost/Latency aggregation, BenchReport v2, combined M3 acceptance, final pre-M4 audit, and M4 remain deliberately outside this independently testable slice.
- No task contains a pending design choice, result-dependent sample replacement, or implementation placeholder.
