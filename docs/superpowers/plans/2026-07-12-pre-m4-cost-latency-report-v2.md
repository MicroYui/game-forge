# Pre-M4 Cost, Latency, and BenchReport v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents for this plan.

**Goal:** Measure cassette-bound Agent token/record-time latency and controlled deterministic pipeline latency, then publish the complete `bench-report@2` JSON/text/static-HTML model and an honest combined M3 acceptance gate without fabricating the still-pending real QA study.

**Architecture:** Source-neutral metric and evidence contracts live under `gameforge/bench/`; a thin Agent-cost composition module maps frozen narrative, HED, repair, and playtest request traces to immutable cassette records while preserving each workload's model snapshot. A separate deterministic runtime harness times the existing checker/simulation pipeline under a hash-bound environment manifest. `BenchReport` composes seeded, narrative, external, HED, QA, Agent, cost, latency, and power evidence into one strict v2 model; JSON is canonical authority, and text/HTML render only that model. The acceptance validator consumes the report plus typed manifests and returns structured failures until every PRD gate, including real QA evidence, is genuinely satisfied.

**Tech Stack:** Python 3.12, Pydantic v2, stdlib `time`/`platform`/`importlib.metadata`/`statistics`, existing Model Router/Cassette contracts, pytest, Hypothesis, Graph/ASP/SMT/economy pipeline, canonical JSON, static HTML.

---

## Global Constraints

- Read and obey `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, `docs/superpowers/specs/2026-07-12-pre-m4-lean-closure-design.md` §§7-9, and `docs/superpowers/specs/2026-07-11-pre-m4-product-closure-design.md` §§9-13 before editing production code.
- TDD is mandatory. Every production behavior starts with a focused failing test, and the expected RED failure must be observed before implementation.
- `spine` remains independent of Agent, cassette, report, source profile, and benchmark evidence code. Timing wraps existing public deterministic behavior from `bench`; it does not move clocks or metrics into `spine`.
- Core metrics, report contracts, renderers, and acceptance logic contain no `endless_sky`, `flare`, Aureus field name, upstream commit OID, or source-specific predicate. Source composition stays under `bench/external_cases`; Agent workflow replay composition stays in the dedicated `bench/agent_costs.py` bridge.
- Current pre-M4 Agent evidence is exactly `openai/gpt-5.6-sol/pre-m4@1`. Historical evidence remains under its recorded model snapshot, including `anthropic/claude-opus-4-8/m2a@1`; no cassette is relabeled, rewritten, or migrated merely to make reports uniform.
- Cost is provider-independent token evidence: normalized input/output/cache-read/cache-write components plus provider-reported total tokens. Monetary estimates stay unavailable unless a separately approved, dated, currency-bound price book exists. This plan creates no price book and reports no currency amount.
- A repeated logical request hash within one sample is a Model Router session-cache reuse. Its cassette tokens and record-time latency are counted once for that sample; logical request count and cache-reuse count remain visible.
- Historical cassette schema v1 did not persist failed transport attempts. Those records report `unknown_transport_attempt_records`; they are never guessed as one attempt. New RECORDs persist exact successful-call attempt/retry counts without changing old bytes.
- Cassette latency is the successful transport's recorded `ModelResponse.latency_ms`. It is not replay wall time, QA active time, or an invented end-to-end retry duration.
- Deterministic runtime measurement uses `time.perf_counter_ns`, compiles constraints once, records setup separately, and measures each deterministic/simulation seeded sample plus the distinct clean baseline. It stores OS, architecture, Python, package/tool versions, clock resolution, CPU count, seed, corpus sizes, and constraint bundle hash. Runtime values are descriptive environment-bound evidence, not correctness claims or cross-machine promises.
- `BinaryMetric` and `DistributionMetric` use `status = pending | measured | underpowered | inconclusive | failed`. Missing evidence has null estimates, never numeric zero. `planned_n` and `evaluated_n` are distinct and validated.
- BenchReport v1 has no published compatibility promise. `load_bench_report()` rejects missing/v1 schema with a clear `bench-report@2` error; there is no parallel v1 runtime path.
- The v2 report contains separate sections for seeded deterministic/simulation BDR, narrative seeded-oracle BDR/FP, deterministic/constraint FP, bounded Agent metrics, per-class power, external development/verification evidence, HED, QA, Agent cost/latency, deterministic runtime, versions, and evidence references.
- JSON, text, and HTML are projections of the same immutable v2 model. Renderers do not load artifacts, call checkers, recompute CIs, or mutate metrics.
- QA stays `pending` until `scenarios/external_cases/endless_sky/qa-evidence.json` exists and validates. The report and acceptance gate must name the missing evidence; they must not synthesize participant time, edits, correctness, or attestation.
- `validate_m3_acceptance()` returns all structured failures in deterministic order. It never raises for an unmet product gate, but malformed/tampered typed evidence fails closed during loading.
- This plan does not implement React pages, M4 observability services, RBAC, approval queues, WORM storage, pricing control, or a dynamic plugin system.
- Every task ends with focused tests and `git diff --check`; commits contain no AI attribution.

## File Structure

- Modify `gameforge/contracts/cassette.py` and `gameforge/runtime/model_router/router.py`: preserve transport attempt/retry evidence on future RECORDs while parsing historical v1 files unchanged.
- Create `gameforge/bench/report_contracts.py`: strict v2 metric, section, version, evidence-reference, and report models plus canonical JSON loading/writing.
- Create `gameforge/bench/cost_latency.py`: cassette normalization, per-sample aggregation, bootstrap distributions, hash-bound Agent cost evidence, and deterministic replay validation.
- Create `gameforge/bench/agent_costs.py`: the only Agent-importing composition bridge for narrative/HED request manifests and repair/playtest REPLAY traces.
- Modify `gameforge/bench/metrics.py`: expose the existing one-snapshot review pipeline as a public function for controlled timing without changing scoring semantics.
- Create `gameforge/bench/runtime_evidence.py`: environment manifest and deterministic per-sample timing evidence.
- Rewrite `gameforge/bench/report.py`: translate all frozen evidence and current deterministic score into `bench-report@2`; create honest pending QA metrics when evidence is absent.
- Rewrite `gameforge/bench/panel.py`: static HTML renderer over v2 only.
- Rewrite `gameforge/bench/run_bench.py`: CLI for one-model JSON/text/HTML output and explicit artifact writing.
- Create `gameforge/bench/acceptance.py`: structured combined M3 validator and CLI.
- Create `scenarios/bench/agent-cost-latency-evidence.json`, `scenarios/bench/deterministic-runtime-evidence.json`, `scenarios/bench/bench-report.json`, `scenarios/bench/bench-report.txt`, and `scenarios/bench/bench-report.html` after their contracts and validators pass.
- Modify existing report/panel/run tests and add focused cost/runtime/acceptance tests under `tests/bench/`.

---

### Task 1: Preserve Transport Attempt Evidence on Future Cassettes

**Files:**
- Modify: `gameforge/contracts/cassette.py`
- Modify: `gameforge/runtime/model_router/router.py`
- Modify: `tests/runtime/cassette/test_cassette_store.py`
- Modify: `tests/runtime/model_router/test_router.py`

**Interfaces:**
- `CassetteRecord.transport_attempts: int | None = None`
- `CassetteRecord.transport_retries: int | None = None`
- Historical records with neither field remain valid and byte-untouched.
- New successful RECORDs persist `attempts >= 1` and `retries == attempts - 1`.

- [ ] **Step 1: Write failing cassette compatibility and retry tests**

```python
def test_historical_cassette_without_attempt_fields_remains_valid():
    record = CassetteRecord.model_validate(HISTORICAL_V1_DICT)
    assert record.transport_attempts is None
    assert record.transport_retries is None


def test_record_persists_exact_transport_attempts_after_retries(tmp_path):
    transport = FlakyTransport(fail_times=2, response=ModelResponse(response_normalized="ok"))
    router = ModelRouter(transport, CassetteStore(tmp_path), RouterMode.RECORD, max_retries=3)
    router.call(request())
    stored = CassetteStore(tmp_path).replay(request_hash(request()))
    assert stored.transport_attempts == 3
    assert stored.transport_retries == 2
```

Also test first-attempt success stores `1/0`, exhausted retries write no cassette, PASSTHROUGH behavior remains unchanged, and `transport_retries` cannot differ from `transport_attempts - 1`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/runtime/cassette/test_cassette_store.py tests/runtime/model_router/test_router.py -q`

Expected: new field assertions fail because the contract and router do not preserve attempt counts.

- [ ] **Step 3: Implement the additive cassette fields and retry return value**

Use strict nonnegative validation and a model validator:

```python
class CassetteRecord(BaseModel):
    cassette_schema_version: str = CASSETTE_SCHEMA_VERSION
    request_hash: str
    agent_node_id: str
    model_snapshot: ModelSnapshot
    response: ModelResponse
    transport_attempts: int | None = Field(default=None, ge=1)
    transport_retries: int | None = Field(default=None, ge=0)
    recorded_at: str | None = None

    @model_validator(mode="after")
    def validate_attempts(self):
        if (self.transport_attempts is None) != (self.transport_retries is None):
            raise ValueError("cassette transport attempts and retries must appear together")
        if self.transport_attempts is not None and self.transport_retries != self.transport_attempts - 1:
            raise ValueError("cassette transport retries must equal attempts - 1")
        return self
```

Change `_complete_with_retry()` to return `(ModelResponse, attempts_used)` and populate the fields only when a new RECORD is written. Do not rewrite a record returned by `resume=True`.

- [ ] **Step 4: Run focused tests, historical parsing, and commit**

Run:

```bash
uv run pytest tests/runtime/cassette tests/runtime/model_router/test_router.py -q
uv run python -c 'from gameforge.runtime.cassette.store import CassetteStore; from pathlib import Path; [CassetteStore(p.parent).replay("sha256:" + p.stem) for p in Path("cassettes").glob("*.json")]'
git diff --check
```

Commit:

```bash
git add gameforge/contracts/cassette.py gameforge/runtime/model_router/router.py \
  tests/runtime/cassette/test_cassette_store.py tests/runtime/model_router/test_router.py
git diff --cached --check
git commit -m "feat(runtime): preserve cassette transport attempts"
```

---

### Task 2: Define Strict Metric and BenchReport v2 Contracts

**Files:**
- Create: `gameforge/bench/report_contracts.py`
- Rewrite: `tests/bench/test_bench_report.py`

**Interfaces:**

```python
MetricStatus = Literal["pending", "measured", "underpowered", "inconclusive", "failed"]

class BinaryMetric(StrictModel):
    name: StableId
    defect_class: DefectClass | None
    bucket: StableId
    planned_n: int
    evaluated_n: int
    k: int
    rate: float | None
    ci_low: float | None
    ci_high: float | None
    ci_method: str | None
    status: MetricStatus
    protocol_id: str | None
    evidence_ref: StableId | None

class DistributionMetric(StrictModel):
    name: StableId
    unit: StableId
    bucket: StableId
    planned_n: int
    evaluated_n: int
    mean: float | None
    median: float | None
    p95: float | None
    primary_estimate: float | None
    ci_low: float | None
    ci_high: float | None
    ci_method: str | None
    status: MetricStatus
    protocol_id: str | None
    evidence_ref: StableId | None
```

Add strict models for `PowerMetric`, `EvidenceArtifactRef`, `VersionRef`, `ExternalSection`, `NarrativeSection`, `HedSection`, `QaSection`, `TokenTotals`, `AgentCostSection`, `DeterministicRuntimeSection`, `CostLatencySection`, `BenchMeta`, and `BenchReport` with `schema_version="bench-report@2"`.

- [ ] **Step 1: Write failing strict-contract tests**

```python
def test_pending_metric_has_null_estimates_not_fake_zero():
    metric = BinaryMetric.pending(
        name="qa_manual_success", bucket="qa", planned_n=4,
        protocol_id=QA_PROTOCOL_SHA, evidence_ref=None,
    )
    assert metric.evaluated_n == metric.k == 0
    assert metric.rate is metric.ci_low is metric.ci_high is None
    assert metric.status == "pending"


def test_measured_binary_metric_rederives_rate_and_wilson_interval():
    metric = BinaryMetric.wilson(
        name="bdr", defect_class=DefectClass.spoiler,
        bucket="llm_assisted", planned_n=381, evaluated_n=381, k=381,
        status="measured", protocol_id="narrative-protocol@1", evidence_ref="narrative",
    )
    assert metric.rate == 1.0
    assert metric.ci_method == "wilson95"


def test_load_report_rejects_v1_with_clear_schema_error(tmp_path):
    path = tmp_path / "v1.json"
    path.write_text('{"seeded":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="bench-report@2"):
        load_bench_report(path)
```

Also test `k <= evaluated_n <= planned_n`, complete/null estimate sets, finite floats, CI ordering, `underpowered` vs `measured`, canonical JSON float parsing, unique metric identities, unique evidence/version refs, section-specific bucket constraints, and `BenchReport` requiring exactly 15 distinct defect classes across seeded+narrative.

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `uv run pytest tests/bench/test_bench_report.py -q`

Expected: import failure because `report_contracts.py` and v2 types do not exist.

- [ ] **Step 3: Implement factories, validation, and canonical I/O**

`BinaryMetric.wilson()` calls the existing `wilson_ci`; `DistributionMetric.measured()` computes no statistics itself and validates a complete caller-supplied set. `canonical_report_bytes()` uses `canonical_json(report.model_dump(mode="json")) + "\n"`. `load_bench_report()` checks the raw `schema_version` before Pydantic validation and rejects anything except `bench-report@2`.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/bench/test_bench_report.py -q
uv run ruff check gameforge/bench/report_contracts.py tests/bench/test_bench_report.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/report_contracts.py tests/bench/test_bench_report.py
git diff --cached --check
git commit -m "feat(bench): define BenchReport v2 contracts"
```

---

### Task 3: Aggregate Hash-Bound Agent Token and Record-Time Latency Evidence

**Files:**
- Create: `gameforge/bench/cost_latency.py`
- Create: `tests/bench/test_cost_latency.py`

**Interfaces:**

```python
class AgentRequestSample(StrictModel):
    sample_id: StableId
    logical_request_hashes: tuple[RequestHash, ...]
    recorded_request_hashes: tuple[RequestHash, ...]
    logical_requests: int
    recorded_requests: int
    session_cache_reuses: int
    tokens: TokenTotals
    recorded_latency_ms: int

class AgentWorkloadEvidence(StrictModel):
    workload_id: StableId
    model_snapshot: ModelSnapshot
    cassette_root: NormalizedPath
    protocol_id: str
    source_evidence_sha256: Sha256
    planned_n: int
    evaluated_n: int
    samples: tuple[AgentRequestSample, ...]
    tokens: TokenTotals
    tokens_per_sample: DistributionMetric
    request_latency_ms: DistributionMetric
    logical_requests: int
    recorded_requests: int
    session_cache_reuses: int
    known_transport_attempts: int
    known_transport_retries: int
    unknown_transport_attempt_records: int
    monetary_status: Literal["unavailable"]
    price_book_ref: None

class AgentCostLatencyEvidence(StrictModel):
    schema_version: Literal["agent-cost-latency-evidence@1"]
    workloads: tuple[AgentWorkloadEvidence, ...]
    evidence_sha256: Sha256
```

- [ ] **Step 1: Write failing normalization and denominator tests**

Use synthetic OpenAI Responses, legacy OpenAI, and Anthropic cassette shapes:

```python
def test_token_normalization_preserves_cache_components_without_double_counting():
    assert normalize_tokens(openai_record()) == TokenTotals(
        input_tokens=100, output_tokens=20, cache_read_tokens=60,
        cache_write_tokens=40, reported_total_tokens=120,
    )
    assert normalize_tokens(anthropic_record()) == TokenTotals(
        input_tokens=10, output_tokens=5, cache_read_tokens=70,
        cache_write_tokens=30, reported_total_tokens=115,
    )


def test_repeated_logical_hash_is_one_recorded_request_and_visible_cache_reuse(tmp_path):
    sample = aggregate_sample("case-1", (HASH_A, HASH_A, HASH_B), tmp_path)
    assert sample.logical_requests == 3
    assert sample.recorded_requests == 2
    assert sample.session_cache_reuses == 1
```

Also test hash/path mismatch, missing cassette, model-snapshot mismatch, negative/malformed usage, inconsistent provider total, missing token usage, missing latency, mixed models in one workload, stable sample ordering, bootstrap reproducibility, p95, attempt known/unknown aggregation, manifest hash tampering, and full revalidation against cassette bytes.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/bench/test_cost_latency.py -q`

Expected: import failure because cost evidence contracts and aggregation do not exist.

- [ ] **Step 3: Implement source-neutral cassette aggregation**

Normalize these aliases:

```python
INPUT_KEYS = ("input_tokens", "prompt_tokens", "input")
OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output")
CACHE_READ_KEYS = ("cache_read_tokens", "cache_read")
CACHE_WRITE_KEYS = ("cache_write_tokens", "cache_write")
```

For OpenAI Responses, read `raw_response.usage.input_tokens_details.cached_tokens` and `cache_write_tokens` when top-level normalized fields omit them. Preserve the provider's `total_tokens` when present; otherwise sum mutually exclusive Anthropic components. Each sample deduplicates hashes in first-occurrence order for record evidence while retaining the complete logical sequence.

Use frozen `percentile_bootstrap_ci(..., statistics.fmean)` and `percentile(..., 0.5/0.95)` for per-sample total tokens and per-record latency. A workload is `measured` only when all planned samples and every referenced cassette validate.

- [ ] **Step 4: Run focused/property tests and commit**

Run:

```bash
uv run pytest tests/bench/test_cost_latency.py -q
uv run ruff check gameforge/bench/cost_latency.py tests/bench/test_cost_latency.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/cost_latency.py tests/bench/test_cost_latency.py
git diff --cached --check
git commit -m "feat(bench): aggregate cassette cost and latency"
```

---

### Task 4: Compose Narrative, HED, Repair, and Playtest Request Traces

**Files:**
- Create: `gameforge/bench/agent_costs.py`
- Create: `tests/bench/test_agent_costs.py`
- Modify: `tests/architecture/test_human_evidence_boundaries.py`

**Interfaces:**
- `SampleTrace = dataclass(frozen=True, sample_id: str, request_hashes: tuple[str, ...])`
- `narrative_verification_traces(evidence) -> tuple[SampleTrace, ...]`
- `hed_traces(evidence) -> tuple[SampleTrace, ...]`
- `repair_replay_traces() -> tuple[SampleTrace, ...]`
- `playtest_replay_traces(variant) -> tuple[SampleTrace, ...]`
- `build_agent_cost_evidence(...) -> AgentCostLatencyEvidence`

- [ ] **Step 1: Write failing trace-composition tests**

```python
def test_narrative_trace_uses_all_1905_frozen_verification_cases():
    traces = narrative_verification_traces(load_narrative_evidence())
    assert len(traces) == 1905
    assert sum(len(row.request_hashes) for row in traces) == 5715


def test_hed_trace_keeps_14_logical_requests_but_only_10_recorded_requests():
    traces = hed_traces(load_hed_evidence())
    assert sum(len(row.request_hashes) for row in traces) == 14
    assert sum(len(dict.fromkeys(row.request_hashes)) for row in traces) == 10
```

For repair/playtest, use fake one-case harness injection in unit tests, then one measured acceptance test over committed REPLAY data. Assert repair covers ten scenarios; playtest covers twenty chains separately for `layered`, `flat`, and `memory_on`; every replay metric matches the existing Agent metric result; no live transport is constructible; and source-specific names do not enter `cost_latency.py` or report contracts.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/bench/test_agent_costs.py tests/architecture/test_human_evidence_boundaries.py -q`

Expected: missing composition module and boundary assertions fail.

- [ ] **Step 3: Implement the Agent-only composition bridge**

Use a small `RequestTrackingRouter` facade that records `request_hash(request)` and delegates to a shared REPLAY `ModelRouter`. For repair, run each scenario as a one-element corpus. For playtest, run each chain as a one-element corpus with the exact frozen `RECORD_MAX_STEPS`, model snapshot, planner flag, and memory factory. The bridge may import Agents; `cost_latency.py`, `report_contracts.py`, `report.py`, and `acceptance.py` may not.

Do not include development narrative calls in the final headline workload. Include these six workloads:

1. `narrative-verification` (`openai/gpt-5.6-sol/pre-m4@1`, 1905 samples)
2. `external-hed` (`openai/gpt-5.6-sol/pre-m4@1`, 8 samples)
3. `repair-search` (the exact snapshot recorded by its cassettes, 10 samples)
4. `playtest-layered` (`anthropic/claude-opus-4-8/m2a@1`, 20 samples)
5. `playtest-flat` (`anthropic/claude-opus-4-8/m2a@1`, 20 samples)
6. `playtest-memory-on` (`anthropic/claude-opus-4-8/m2a@1`, 20 samples)

The historical Opus workload remains historical evidence; no request is regenerated with GPT-5.6 merely for presentation consistency.

- [ ] **Step 4: Run REPLAY trace tests and commit**

Run:

```bash
uv run pytest tests/bench/test_agent_costs.py tests/bench/test_agent_metrics.py \
  tests/architecture/test_human_evidence_boundaries.py -q
git diff --exit-code -- cassettes
uv run ruff check gameforge/bench/agent_costs.py tests/bench/test_agent_costs.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/agent_costs.py tests/bench/test_agent_costs.py \
  tests/architecture/test_human_evidence_boundaries.py
git diff --cached --check
git commit -m "feat(bench): compose Agent cost traces"
```

---

### Task 5: Measure the Controlled Deterministic Pipeline

**Files:**
- Modify: `gameforge/bench/metrics.py`
- Create: `gameforge/bench/runtime_evidence.py`
- Create: `tests/bench/test_runtime_evidence.py`
- Modify: `tests/bench/test_metrics.py`

**Interfaces:**

```python
def run_pipeline(snapshot: Snapshot, checkers, *, needs_nav: bool) -> ReviewReport: ...

class RuntimeEnvironment(StrictModel):
    system: str
    release: str
    machine: str
    python_implementation: str
    python_version: str
    cpu_count: int | None
    perf_counter_resolution_ns: int
    tool_version: str
    package_versions: tuple[VersionRef, ...]

class RuntimeSample(StrictModel):
    sample_id: StableId
    defect_class: DefectClass | None
    bucket: Literal["deterministic", "simulation", "clean"]
    elapsed_ns: int

class DeterministicRuntimeEvidence(StrictModel):
    schema_version: Literal["deterministic-runtime-evidence@1"]
    workload_id: Literal["seeded-checker-sim-pipeline"]
    seed: int
    per_class_n: dict[DefectClass, int]
    distinct_clean_n: int
    constraints_sha256: Sha256
    setup_elapsed_ns: int
    samples: tuple[RuntimeSample, ...]
    per_sample_ms: DistributionMetric
    environment: RuntimeEnvironment
    evidence_sha256: Sha256
```

- [ ] **Step 1: Write failing clock-injected evidence tests**

```python
def test_runtime_measurement_times_setup_once_and_each_sample_once(fake_clock):
    evidence = measure_runtime(small_corpus(), constraints(), clock_ns=fake_clock)
    assert len(evidence.samples) == 3
    assert evidence.setup_elapsed_ns > 0
    assert evidence.per_sample_ms.evaluated_n == 3
    assert evidence.per_sample_ms.status == "measured"


def test_runtime_manifest_rejects_environment_or_sample_tampering():
    with pytest.raises(ValidationError):
        DeterministicRuntimeEvidence.model_validate(tampered_payload())
```

Also test narrative samples are excluded, one distinct clean snapshot is included, sample IDs/order are deterministic, zero/negative elapsed values fail, constraints hash is content-bound and path-independent, package versions include `clingo`, `z3-solver`, and `pydantic`, canonical load/write works, and exposing `run_pipeline` does not alter seeded score results.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/bench/test_runtime_evidence.py tests/bench/test_metrics.py -q`

Expected: missing runtime evidence module/public pipeline function.

- [ ] **Step 3: Implement measurement and environment binding**

Rename `_run_pipeline` to `run_pipeline` without behavioral changes. `measure_seeded_runtime()` compiles constraints inside the measured setup interval, warms no hidden extra sample, then measures the 11 deterministic/simulation classes in enum/index order and one entry per distinct clean snapshot. Exceptions abort evidence generation; they are not converted to zero latency.

The distribution converts nanoseconds to milliseconds only after all positive integer timings are captured. Use the shared frozen bootstrap protocol for the mean CI and `percentile` for median/p95.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/bench/test_runtime_evidence.py tests/bench/test_metrics.py -q
uv run ruff check gameforge/bench/runtime_evidence.py gameforge/bench/metrics.py \
  tests/bench/test_runtime_evidence.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/metrics.py gameforge/bench/runtime_evidence.py \
  tests/bench/test_runtime_evidence.py tests/bench/test_metrics.py
git diff --cached --check
git commit -m "feat(bench): measure deterministic pipeline runtime"
```

---

### Task 6: Compose BenchReport v2 from Frozen Evidence

**Files:**
- Rewrite: `gameforge/bench/report.py`
- Rewrite: `gameforge/bench/run_bench.py`
- Rewrite: `tests/bench/test_run_bench.py`
- Modify: `tests/bench/test_bench_report.py`

**Interfaces:**
- `build_bench_report(...) -> BenchReport`
- `build_qa_section(protocol, evidence: QaEvidenceManifest | None) -> QaSection`
- `write_report_bundle(report, output_dir) -> tuple[Path, Path, Path]`
- CLI: `python -m gameforge.bench.run_bench --output-dir scenarios/bench`

- [ ] **Step 1: Write failing composition tests**

```python
def test_v2_report_contains_all_fifteen_measured_class_metrics():
    report = build_bench_report(small_seeded_score=score(), evidence_bundle=frozen_bundle())
    classes = {row.defect_class for row in (*report.seeded, *report.narrative.bdr)}
    assert classes == set(DefectClass)
    assert all(row.evaluated_n > 0 for row in report.narrative.bdr)


def test_missing_qa_evidence_is_pending_and_never_fake_zero():
    section = build_qa_section(load_qa_protocol(), None)
    assert section.conclusion == "pending"
    assert section.paired_saved_minutes.status == "pending"
    assert section.paired_saved_minutes.mean is None
    assert section.manual_success.rate is None
```

Also assert the report uses Endless Sky `external-corpus-manifest@1`, not the legacy Flare compatibility report; HED contains all 8 outcomes including 2 unusable; Agent metrics preserve their own model snapshots; cost has six workloads; deterministic runtime ref/hash matches; power uses evaluated n; every evidence ref resolves; report canonical round-trip is byte-identical; and small test corpora cannot masquerade as final measured acceptance.

- [ ] **Step 2: Run composition tests and verify RED**

Run: `uv run pytest tests/bench/test_bench_report.py tests/bench/test_run_bench.py -q`

Expected: existing v1 fields/constructors do not satisfy v2 composition.

- [ ] **Step 3: Implement v2 evidence translation**

Translate existing evidence without recomputing or renaming its semantics:

- 11 deterministic/simulation `Metric` rows -> `BinaryMetric`
- narrative verification `by_class` -> four measured `BinaryMetric` rows
- deterministic oracle-FP, constraint-FP, narrative seeded-oracle FP, and external after-oracle FP -> separate rows
- existing bounded Agent REPLAY metrics -> separate Agent rows
- external development and verification metrics -> `ExternalSection`
- HED normalized/raw distribution and four disposition rates -> `HedSection`
- QA evidence -> paired distributions/success metrics/conclusion; absent evidence -> pending fields
- Agent cost and deterministic runtime evidence -> `CostLatencySection`
- tool/schema/adapter/prompt/model/protocol identifiers -> sorted `VersionRef` rows
- all input file hashes -> sorted `EvidenceArtifactRef` rows

Do not silently call live Agent recording. Report composition may recompute deterministic seeded metrics and bounded Agent outcome metrics under REPLAY, but loads cost/runtime/evidence artifacts from explicit paths.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/bench/test_bench_report.py tests/bench/test_run_bench.py \
  tests/bench/test_agent_metrics.py -q
uv run ruff check gameforge/bench/report.py gameforge/bench/run_bench.py \
  tests/bench/test_bench_report.py tests/bench/test_run_bench.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/report.py gameforge/bench/run_bench.py \
  tests/bench/test_bench_report.py tests/bench/test_run_bench.py
git diff --cached --check
git commit -m "feat(bench): compose BenchReport v2"
```

---

### Task 7: Render Text and Static HTML from the Same v2 Model

**Files:**
- Rewrite: `gameforge/bench/panel.py`
- Modify: `gameforge/bench/report.py`
- Rewrite: `tests/bench/test_panel.py`
- Modify: `tests/bench/test_bench_report.py`

**Interfaces:**
- `report_projection(report) -> tuple[ViewRow, ...]`
- `format_text(report) -> str`
- `render_html(report) -> str`

- [ ] **Step 1: Write failing projection/renderer contract tests**

```python
def test_text_and_html_render_every_authoritative_projection_row():
    report = complete_sample_report()
    rows = report_projection(report)
    text = format_text(report)
    html = render_html(report)
    for row in rows:
        assert row.row_id in text
        assert f'data-row-id="{row.row_id}"' in html


def test_pending_values_render_as_unavailable_not_zero():
    report = report_with_pending_qa()
    assert "qa.paired_saved_minutes" in format_text(report)
    assert "pending" in format_text(report)
    assert ">0.000<" not in render_html(report)
```

Also test no JavaScript, escaped text/attributes, every section present, model snapshots visible, external development/verification separated, HED unusable count visible, QA single-participant scope visible, token components/unknown attempts visible, deterministic environment visible, power/underpowered visible, and no renderer imports evidence loaders/checkers/Agents.

- [ ] **Step 2: Run renderer tests and verify RED**

Run: `uv run pytest tests/bench/test_panel.py tests/bench/test_bench_report.py -q`

Expected: v1 HTML/text renderers omit v2 sections and projection IDs.

- [ ] **Step 3: Implement one projection and two pure renderers**

`report_projection()` is the only flattening layer. It emits stable row IDs, section, label, status, formatted value, denominator, interval, and evidence ref. Text and HTML iterate those rows; neither reads raw section internals independently. HTML remains self-contained with inline CSS and no `<script>`.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/bench/test_panel.py tests/bench/test_bench_report.py -q
uv run ruff check gameforge/bench/panel.py gameforge/bench/report.py \
  tests/bench/test_panel.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/panel.py gameforge/bench/report.py \
  tests/bench/test_panel.py tests/bench/test_bench_report.py
git diff --cached --check
git commit -m "feat(bench): render BenchReport v2 views"
```

---

### Task 8: Implement the Combined M3 Acceptance Gate

**Files:**
- Create: `gameforge/bench/acceptance.py`
- Create: `tests/bench/test_m3_acceptance.py`
- Modify: `tests/architecture/test_human_evidence_boundaries.py`

**Interfaces:**

```python
class GateFailure(StrictModel):
    code: StableId
    path: str
    message: str

class M3EvidenceBundle(StrictModel):
    external: ExternalCorpusManifest
    narrative: NarrativeEvidenceManifest
    hed: HedEvidenceManifest
    qa: QaEvidenceManifest | None
    agent_cost: AgentCostLatencyEvidence
    deterministic_runtime: DeterministicRuntimeEvidence

def validate_m3_acceptance(
    report: BenchReport,
    evidence: M3EvidenceBundle,
) -> tuple[GateFailure, ...]: ...
```

- [ ] **Step 1: Write a complete synthetic passing bundle and one test per gate**

```python
def test_complete_m3_bundle_has_no_gate_failures():
    assert validate_m3_acceptance(complete_report(), complete_bundle()) == ()


def test_current_missing_real_qa_evidence_is_a_specific_failure_not_an_exception():
    failures = validate_m3_acceptance(report_with_pending_qa(), bundle_without_qa())
    assert [item.code for item in failures] == ["qa.evidence_missing"]
```

Add independent mutations for: corpus <500; missing class; pending/evaluated mismatch; narrative n !=381; narrative clean n !=381; CI half-width >0.05; deterministic oracle-FP nonzero; external !=8 cases/4 classes; missing verification hit/after-clear; external after FP nonzero; HED !=8/protocol failure/missing human target; QA !=4 valid pairs; missing Agent token or latency; missing deterministic runtime; cassette miss/unknown workload denominator; view hashes mismatch; evidence hash/path mismatch; model relabeling; and source-specific report/checker boundary violations.

- [ ] **Step 2: Run acceptance tests and verify RED**

Run: `uv run pytest tests/bench/test_m3_acceptance.py tests/architecture/test_human_evidence_boundaries.py -q`

Expected: acceptance module and structured failures do not exist.

- [ ] **Step 3: Implement all ten lean-design machine gates**

Return failures sorted by `(code, path, message)`. Gate validation must cross-check the report against the typed source manifests, not just trust copied report numbers. Unknown historical transport attempts remain reportable and do not fail item 8 because token and record-time latency are measured; a missing/malformed cassette or missing token/latency value does fail.

The current real repository is expected to return only QA-related failures until the participant evidence is imported. Any other failure must be fixed before freezing the report.

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
uv run pytest tests/bench/test_m3_acceptance.py \
  tests/architecture/test_human_evidence_boundaries.py -q
uv run ruff check gameforge/bench/acceptance.py tests/bench/test_m3_acceptance.py
git diff --check
```

Commit:

```bash
git add gameforge/bench/acceptance.py tests/bench/test_m3_acceptance.py \
  tests/architecture/test_human_evidence_boundaries.py
git diff --cached --check
git commit -m "feat(bench): add combined M3 acceptance gate"
```

---

### Task 9: Freeze Measured Cost, Runtime, and Current Report Artifacts

**Files:**
- Create: `scenarios/bench/agent-cost-latency-evidence.json`
- Create: `scenarios/bench/deterministic-runtime-evidence.json`
- Create: `scenarios/bench/bench-report.json`
- Create: `scenarios/bench/bench-report.txt`
- Create: `scenarios/bench/bench-report.html`
- Create: `tests/bench/test_measured_cost_latency.py`
- Create: `tests/bench/test_measured_report_v2.py`
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-cost-latency-report-v2.md`

- [ ] **Step 1: Add measured-artifact acceptance tests before generation**

The tests load exact committed paths, validate every nested hash, reaggregate every cassette referenced by Agent cost evidence, verify all six workload denominators/model snapshots, validate the runtime environment/sample denominator, parse all three report views, and assert combined acceptance has no failure except the explicit real-QA evidence failure.

- [ ] **Step 2: Run measured tests and verify RED**

Run: `uv run pytest tests/bench/test_measured_cost_latency.py tests/bench/test_measured_report_v2.py -q`

Expected: files are absent.

- [ ] **Step 3: Generate deterministic Agent cost evidence under zero-network REPLAY**

Run:

```bash
uv run python -m gameforge.bench.agent_costs \
  --output scenarios/bench/agent-cost-latency-evidence.json
uv run python -m gameforge.bench.agent_costs \
  --output /tmp/agent-cost-latency-replay.json
cmp scenarios/bench/agent-cost-latency-evidence.json \
  /tmp/agent-cost-latency-replay.json
git diff --exit-code -- cassettes
```

Expected: two independent zero-network aggregations are byte-identical; no cassette changes.

- [ ] **Step 4: Measure the full controlled deterministic workload once**

Run:

```bash
uv run python -m gameforge.bench.runtime_evidence \
  --output scenarios/bench/deterministic-runtime-evidence.json
uv run python -m gameforge.bench.runtime_evidence \
  --validate scenarios/bench/deterministic-runtime-evidence.json
```

Record in this plan: environment ID, setup milliseconds, evaluated sample count, mean/median/p95 per-sample latency, bootstrap CI, and evidence SHA. Do not compare a second machine's absolute wall time as a correctness assertion.

- [ ] **Step 5: Build all three report views from one v2 model**

Run:

```bash
uv run python -m gameforge.bench.run_bench --output-dir scenarios/bench
uv run python -m gameforge.bench.run_bench --validate-bundle scenarios/bench
uv run python -m gameforge.bench.acceptance --report scenarios/bench/bench-report.json
```

Expected before real QA import: JSON/text/HTML validate; the acceptance CLI exits nonzero and prints only explicit QA evidence/session/pair failures. Any non-QA failure blocks this task.

- [ ] **Step 6: Run measured tests and commit immutable artifacts**

Run:

```bash
uv run pytest tests/bench/test_measured_cost_latency.py \
  tests/bench/test_measured_report_v2.py -q
git diff --check
```

Commit:

```bash
git add scenarios/bench tests/bench/test_measured_cost_latency.py \
  tests/bench/test_measured_report_v2.py \
  docs/superpowers/plans/2026-07-12-pre-m4-cost-latency-report-v2.md
git diff --cached --check
git commit -m "test(bench): freeze pre-M4 cost and report evidence"
```

---

### Task 10: Regression, QA Follow-Up, and Pre-M4 Handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-cost-latency-report-v2.md`
- Modify: `docs/superpowers/plans/README.md`
- Modify after real sessions: `scenarios/bench/bench-report.{json,txt,html}`
- Modify after real sessions: `tests/bench/test_measured_report_v2.py`

- [ ] **Step 1: Run focused evidence and historical replay regression**

```bash
uv run pytest tests/bench/hed tests/bench/qa tests/bench/narrative \
  tests/bench/external_cases tests/bench/test_cost_latency.py \
  tests/bench/test_agent_costs.py tests/bench/test_runtime_evidence.py \
  tests/bench/test_bench_report.py tests/bench/test_panel.py \
  tests/bench/test_run_bench.py tests/bench/test_m3_acceptance.py \
  tests/bench/test_measured_cost_latency.py tests/bench/test_measured_report_v2.py -q
git diff --exit-code -- cassettes
```

- [ ] **Step 2: Run all repository gates**

```bash
uv run pytest -q
uv run lint-imports
uv run pytest tests/test_dependency_lint.py -q
uv run ruff check gameforge tests
git diff --check
```

- [ ] **Step 3: Revalidate immutable external, narrative, HED, and cost evidence**

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_runner \
  --corpus scenarios/external_cases/endless_sky
git diff --exit-code -- scenarios/external_cases/endless_sky/external-corpus-manifest.json
uv run python -m gameforge.bench.external_cases.endless_sky_hed \
  --replay --output /tmp/hed-report-v2-replay.json
cmp /tmp/hed-report-v2-replay.json \
  scenarios/external_cases/endless_sky/hed-evidence.json
uv run python -m gameforge.bench.narrative.harness \
  --replay-verification --output /tmp/narrative-report-v2-replay.json
cmp /tmp/narrative-report-v2-replay.json \
  scenarios/narrative_bench/verification-evidence.json
uv run python -m gameforge.bench.agent_costs \
  --output /tmp/agent-cost-final.json
cmp /tmp/agent-cost-final.json \
  scenarios/bench/agent-cost-latency-evidence.json
```

- [ ] **Step 4: Keep the current slice open only for real QA evidence**

Before the participant finishes, update `plans/README.md` to state: Cost/Latency, runtime measurement, BenchReport v2, three views, and acceptance automation are complete; real QA evidence and final pre-M4 audit remain open; M3 is still in progress and M4 has not started.

- [ ] **Step 5: After the human-evidence plan imports eight real sessions, rebuild and close**

Run the Task 9 report/acceptance commands again. The final acceptance result must be an empty tuple, all measured tests must pass, and only then may this plan and the human-evidence plan be marked complete. Update the AGENTS milestone table and `plans/README.md` in the separate final pre-M4 audit commit; do not begin M4 in this plan.

---

## Plan Self-Review

- **Spec coverage:** Lean design §7 maps to Tasks 2/6/7; cassette token/latency and truthful missing price/attempt data map to Tasks 1/3/4/9; deterministic runtime manifest maps to Task 5/9; all ten machine gates map to Task 8; three same-model views map to Task 7/9; QA honesty and final blocking behavior map to Tasks 6/8/10.
- **Model policy:** New narrative/HED evidence remains `openai/gpt-5.6-sol/pre-m4@1`; historical Opus playtest evidence remains `anthropic/claude-opus-4-8/m2a@1`; the report displays both and never rewrites either.
- **No placeholders:** Every implementation task has exact files, interfaces, RED tests, commands, expected failures, verification, and commit boundaries. The only pending external fact is the real participant's QA sessions, which this plan explicitly refuses to fabricate.
- **Type consistency:** `BinaryMetric`, `DistributionMetric`, `PowerMetric`, `TokenTotals`, `AgentCostLatencyEvidence`, `DeterministicRuntimeEvidence`, `BenchReport`, `M3EvidenceBundle`, and `GateFailure` names are stable across tasks. `planned_n/evaluated_n` and `status` semantics are identical in all sections.
- **Product neutrality:** Source-specific reconstruction stays in existing `external_cases`; Agent workflow tracing is isolated in `agent_costs`; report/metrics/runtime/acceptance never branch on a game or upstream object name.
- **Scope discipline:** M4 UI, observability services, approval/RBAC, price governance, production storage, and plugin infrastructure remain deferred. This slice closes PRD M3 evidence and report contracts only.
