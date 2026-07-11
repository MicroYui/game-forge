# Pre-M4 External Evidence + Endless Sky B0A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a source-neutral external-corpus evidence harness, preserve every frozen Flare B0A byte and legacy entry point, and execute the preregistered first 80 Endless Sky B0A candidates through an honest human-attestation gate.

**Architecture:** The new `gameforge.bench.external_corpus` package owns source-neutral contracts, read-only Git facts, discovery, adjudication, canonical evidence publication, and an explicit in-repo source registry. Flare remains a frozen legacy serialization and CLI surface backed by compatibility adapters; Endless Sky supplies only a source profile and evidence. No source ID, path, commit, or field rule may enter contracts, spine, checkers, simulation, metrics, or report code.

**Tech Stack:** Python 3.12, Pydantic v2, stdlib `subprocess`/`hashlib`/`pathlib`, Git argument-array commands, pytest, Hypothesis, import-linter, Ruff.

## Global Constraints

- Truth sources are `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, and `docs/superpowers/specs/2026-07-11-pre-m4-product-closure-design.md`.
- This plan covers only generic external evidence, frozen Flare compatibility, and Endless Sky B0A. It does not implement B0B qualification, an Endless Sky reader/Adapter, held-out evaluation, or any M4 surface.
- `scenarios/flare_corpus/**` must remain byte-identical to commit `755fe2e`; the approved universe `08873db9362bd6ff45ca05bb4e3184120fc07842cf224e8f649fb6555e57bfc3`, approval payload `b5111dd6d65caa7675a82ac7b7c2a3735dd472eed0a2e8fc607c6b1292dc9970`, and negative gate must not change.
- Existing imports from `flare_evidence`, `flare_git`, `flare_adjudication`, and `flare_mining`, plus `python -m gameforge.bench.flare_mining`, remain compatible.
- Generic code must not contain `endless_sky`, Flare paths, source-specific pinned OIDs, or source-specific taxonomy decisions. Those values live only in `external_corpus/profiles/**` or `scenarios/external_corpus/**`.
- Source profiles are explicit registry entries, not dynamically loaded plugins. No plugin discovery, sandbox, installer, lock service, or multi-writer protocol is added.
- All Git subprocesses use argument arrays with `shell=False`, a fixed locale/timezone/config environment, and no network. Discovery rejects partial/promisor repositories and nonempty repository-local attributes before reading evidence.
- The real Endless Sky mirror must be complete and offline before discovery. The planning-only partial mirror under `~/.cache` is not admissible evidence input.
- Candidate discovery is deterministic and exhaustive over the registered range before applying the registered order and limit. No manual candidate picking is permitted.
- B0A uses at most the first 80 candidates, gate `independent proposed groups >= 8 AND domain-applicable proposed classes >= 4`.
- No Agent may create a human reviewer identity, review timestamp, approval, or attestation. Without a real human attestation bound to the complete payload hash, the B0A decision remains unavailable and the source cannot enter B0B.
- TDD is mandatory. Each implementation task starts red, turns green narrowly, then runs all affected legacy tests.
- Commits contain no AI attribution footer. `AGENTS.md` remains untracked and must never be staged.

---

## File Map

| File | Responsibility |
|---|---|
| `gameforge/bench/external_corpus/contracts.py` | Complete source-neutral profile, discovery, adjudication, gate, Adapter binding, canonical JSON, CAS, and immutable-publication contracts |
| `gameforge/bench/external_corpus/git.py` | Read-only local Git boundary and objective commit/path/patch facts |
| `gameforge/bench/external_corpus/discovery.py` | Profile-driven exhaustive discovery, stable limiting, lineage, CAS, and ledger creation |
| `gameforge/bench/external_corpus/adjudication.py` | Offline assignment/evidence replay, independent-group derivation, and supply gate |
| `gameforge/bench/external_corpus/mining.py` | Generic `discover`, `review-package`, and `adjudicate` CLI |
| `gameforge/bench/external_corpus/profiles/__init__.py` | Explicit source-profile registry |
| `gameforge/bench/external_corpus/profiles/flare.py` | Flare legacy-to-generic compatibility binding only |
| `gameforge/bench/external_corpus/profiles/endless_sky.py` | Endless Sky profile identity and source-bound checks only |
| `gameforge/bench/flare_*.py` | Legacy import/CLI wrappers; frozen JSON behavior remains unchanged |
| `tests/bench/external_corpus/**` | Generic contracts, synthetic Git, discovery, adjudication, CLI, profile, and anti-specialization tests |
| `scenarios/external_corpus/endless_sky/**` | Registered upstream notices/profile and later immutable B0A evidence |

### Public Interfaces Locked by This Plan

| Symbol | Locked signature |
|---|---|
| `canonical_bytes` | `(BaseModel \| Mapping[str, object]) -> bytes` |
| `sha256_hex` | `(bytes) -> str` |
| `load_canonical` | `(Path, type[ModelT], str) -> ModelT` |
| `write_new_or_identical` | `(Path, bytes) -> None` |
| `write_set_new_or_identical` | `(Mapping[Path, bytes]) -> None` |
| `put_blob` | `(Path, bytes) -> tuple[str, str]` |
| `ReadOnlyGitRepo` | constructor accepts `str \| Path`; exposes `preflight`, `resolve`, `reachable_commits`, `commit_metadata`, `changed_paths`, `patch_bytes`, `eligible_patch_bytes`, `stable_patch_id`, and `git_version` |
| `discover_candidates` | `(ReadOnlyGitRepo, SourceProfile, SearchRegistration, Path) -> DiscoveryLedger` |
| `build_review_package` | `(DiscoveryLedger) -> ReviewPackage` |
| `adjudicate` | `(DiscoveryLedger, AdjudicationEvidence) -> tuple[CandidateLedger, B0ADecision]` |

Every implementation step below names the exact source to move or the exact invariant to add.

---

### Task 1: Source-Neutral Contracts and Canonical Evidence I/O

**Files:**
- Create: `gameforge/bench/external_corpus/__init__.py`
- Create: `gameforge/bench/external_corpus/contracts.py`
- Create: `tests/bench/external_corpus/__init__.py`
- Create: `tests/bench/external_corpus/test_contracts.py`
- Modify: `gameforge/bench/flare_evidence.py`
- Test: `tests/bench/test_flare_evidence.py`

**Interfaces:**
- Consumes: `DefectClass`, `Bucket`, `CLASS_META`, `gameforge.contracts.canonical.canonical_json`.
- Produces: strict source-neutral models, canonical I/O functions, and legacy Flare re-exports.

- [ ] **Step 1: Write failing contract tests**

Add tests that instantiate the complete profile and binding rather than a reduced fixture:

```python
def test_source_profile_binds_complete_discovery_and_future_qualification_surface(
    source_profile_fixture,
):
    profile = source_profile_fixture
    assert profile.schema_version == "external-source-profile@1"
    assert profile.history_range.expected_commit_count > 0
    assert profile.candidate_order == (
        CandidateOrderTerm(field="committed_at", direction="descending"),
        CandidateOrderTerm(field="commit_oid", direction="ascending"),
    )
    assert profile.b0a_protocol.candidate_limit == 80
    assert profile.b0a_protocol.minimum_independent_groups == 8
    assert profile.b0a_protocol.minimum_domain_applicable_classes == 4
    assert profile.native_validator_commands[0].network == "forbidden"
    assert profile.query_complete_closure
    assert profile.qualification_predicate_ids


def test_adapter_binding_is_separate_from_discovery_profile(source_profile_fixture):
    assert "adapter_version" not in SourceProfile.model_fields
    binding = AdapterBinding(
        source_id=source_profile_fixture.source_id,
        reader_id="reader.endless_sky",
        reader_version="reader.endless_sky@1",
        adapter_format_id="endless-sky-data",
        adapter_version="adapter.endless_sky@1",
        ir_schema_version="ir-core@1",
        mapping_spec_sha256="0" * 64,
    )
    assert binding.source_id == source_profile_fixture.source_id


def test_review_attestation_requires_a_human_and_binds_the_unreviewed_payload(
    adjudication_payload_fixture,
):
    payload = adjudication_payload_fixture
    reviewed_hash = sha256_hex(canonical_bytes(payload.model_dump(mode="json")))
    with pytest.raises(ValidationError, match="human"):
        ReviewAttestation(
            reviewer_kind="agent",
            reviewer_id="model-reviewer",
            candidate_universe_sha256=payload.candidate_universe_sha256,
            reviewed_payload_sha256=reviewed_hash,
            reviewed_at="2026-07-11T00:00:00Z",
            written_statement="I reviewed the complete candidate assignment table.",
        )
    attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="human-reviewer",
        candidate_universe_sha256=payload.candidate_universe_sha256,
        reviewed_payload_sha256=reviewed_hash,
        reviewed_at="2026-07-11T00:00:00Z",
        written_statement="I reviewed and approve the complete candidate assignment table.",
    )
    assert attestation.reviewed_payload_sha256 == reviewed_hash
```

Also test: extra fields forbidden; all paths normalized repository-relative POSIX; SHA/OID formats; unique rule IDs; exactly one history lower-bound form; nonempty argument-array commands; no shell fragments; unique taxonomy rows; domain applicability independent from implementation support; candidate limit/order/count invariants; canonical newline; immutable write conflict; symlink/FIFO rejection; CAS hash verification.

- [ ] **Step 2: Run the new tests and verify the missing-package failure**

Run:

```bash
uv run pytest tests/bench/external_corpus/test_contracts.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'gameforge.bench.external_corpus'`.

- [ ] **Step 3: Move existing source-neutral primitives without changing their serialized form**

Move these exact definitions from `flare_evidence.py` into `external_corpus/contracts.py`: `_StrictModel`, `Oid`, `Sha256`, `StableId`, `NonEmptyStr`, `_compile_regex`, `_validate_posix_relative`, `RegexRule`, `LineageRegexRule`, `GitCommandSpec`, `GitEnvironmentPolicy`, `CandidateCommit`, `SelectionReason`, `DiffEvidence`, `LineageLink`, `DiscoveredCandidate`, `EvidenceRef`, `EvidenceArtifact`, `canonical_bytes`, `sha256_hex`, `posix_glob_matches`, `read_regular_file`, `write_new_or_identical`, `write_set_new_or_identical`, and `put_blob`. Add a separate `VersionId` constrained by `^[A-Za-z0-9][A-Za-z0-9._:@-]*$`; do not broaden `StableId`, because rule/source/reviewer IDs do not need `@`.

Keep legacy behavior by importing and re-exporting the moved names from `flare_evidence.py`:

```python
from gameforge.bench.external_corpus.contracts import (
    CandidateCommit,
    DiffEvidence,
    DiscoveredCandidate,
    EvidenceArtifact,
    EvidenceRef,
    GitCommandSpec,
    GitEnvironmentPolicy,
    LineageLink,
    LineageRegexRule,
    NonEmptyStr,
    Oid,
    RegexRule,
    SelectionReason,
    Sha256,
    StableId,
    canonical_bytes,
    posix_glob_matches,
    put_blob,
    read_regular_file,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)
```

Do not move Flare literals, the legacy Flare `ReviewAttestation`, Flare applicability logic,
round-prefix rules, or Flare ledger models. The generic `ReviewAttestation` is a new model with
`reviewer_kind="human"` and `reviewed_at`; adding those fields to the legacy class would change frozen
serialization and is forbidden.

- [ ] **Step 4: Add the complete new source-neutral models**

Implement these exact model families and fields:

```python
class HistoryRange(_StrictModel):
    committed_at_gte: int | None = None
    after_exclusive_oid: Oid | None = None
    expected_commit_count: Annotated[int, Field(gt=0)]


class CandidateOrderTerm(_StrictModel):
    field: Literal["committed_at", "commit_oid"]
    direction: Literal["ascending", "descending"]


class NativeValidatorCommand(_StrictModel):
    command_id: StableId
    argv: tuple[NonEmptyStr, ...]
    network: Literal["forbidden"] = "forbidden"


class TaxonomyApplicability(_StrictModel):
    defect_class: DefectClass
    domain_applicability: Literal["applicable", "not_applicable"]
    implementation_support: Literal["implemented", "planned", "unsupported"]
    rationale: NonEmptyStr


class B0AProtocol(_StrictModel):
    candidate_limit: Annotated[int, Field(gt=0)]
    expected_matched_candidate_count: Annotated[int, Field(ge=0)]
    expected_config_only_candidate_count: Annotated[int, Field(ge=0)]
    minimum_independent_groups: Annotated[int, Field(gt=0)]
    minimum_domain_applicable_classes: Annotated[int, Field(gt=0)]


class SourceProfile(_StrictModel):
    schema_version: Literal["external-source-profile@1"]
    source_id: StableId
    profile_version: VersionId
    repository_url: NonEmptyStr
    pinned_head: Oid
    history_range: HistoryRange
    config_include_globs: tuple[NonEmptyStr, ...]
    config_exclude_globs: tuple[NonEmptyStr, ...]
    message_rules: tuple[RegexRule, ...]
    diff_rules: tuple[RegexRule, ...]
    lineage_rules: tuple[LineageRegexRule, ...]
    candidate_order: tuple[CandidateOrderTerm, CandidateOrderTerm]
    license_id: StableId
    notice_files: tuple[NonEmptyStr, ...]
    native_validator_commands: tuple[NativeValidatorCommand, ...]
    parser_version: VersionId
    query_complete_closure: tuple[StableId, ...]
    taxonomy_applicability: tuple[TaxonomyApplicability, ...]
    qualification_predicate_ids: tuple[StableId, ...]
    b0a_protocol: B0AProtocol


class AdapterBinding(_StrictModel):
    source_id: StableId
    reader_id: StableId
    reader_version: VersionId
    adapter_format_id: StableId
    adapter_version: VersionId
    ir_schema_version: VersionId
    mapping_spec_sha256: Sha256


class SearchRegistration(_StrictModel):
    project_commit_oid: Oid
    profile_repo_relative_path: str


class ReviewAttestation(_StrictModel):
    reviewer_kind: Literal["human"]
    reviewer_id: StableId
    reviewed_at: datetime
    written_statement: NonEmptyStr
    candidate_universe_sha256: Sha256
    reviewed_payload_sha256: Sha256
```

Validators must enforce: one or neither lower bound, never both; exact two-term order with each field once; unique rule IDs across message/diff/lineage; nonempty include globs; unique normalized excludes/notices/closure/predicates; `expected_config_only_candidate_count <= expected_matched_candidate_count`; minimum classes no larger than applicable taxonomy rows; exactly one row for every `DefectClass`; command argv has no NUL and first token is not a shell; registration path ends `.json`.

Add source-neutral discovery/adjudication models mirroring the existing Flare evidence strength but with `schema_version="external-corpus-b0a@1"`, `source_id`, no Flare round fields, and profile-driven taxonomy/gate thresholds. The exact persisted families are `CommitMetadata`, `DiscoveryTool`, `DiscoveryLedger`, `CandidateCase`, `CandidateDisposition`, `SelectedParentEdge`, `LineageResolution`, `CandidateGroupDecision`, `CandidateFixGroup`, `EvidenceCounts`, `ApplicabilityRow`, `GateSummary`, `AdjudicationPayload`, `AdjudicationEvidence`, `CandidateLedger`, `B0ADecision`, and `ReviewPackage`.

Lock their fields as follows:

| Model | Fields |
|---|---|
| `CommitMetadata` | `commit`, `full_message` |
| `DiscoveryTool` | `tool_version`, `project_commit_oid`, `git_version`, `python_implementation`, `python_version`, `python_build`, `unicode_version` |
| `DiscoveryLedger` | `schema_version`, `source_id`, `source_profile`, `source_profile_sha256`, `search_registration`, `observed_history_count`, `matched_candidate_count`, `config_only_candidate_count`, `discovery_tool`, `discovered_candidates`, `objective_lineage_links`, `candidate_universe_sha256` |
| `CandidateCase` | `case_id`, `defect_class`, `disposition`, `rationale`, `evidence_refs` |
| `CandidateDisposition` | `commit_oid`, `disposition`, `reason_code`, `rationale`, `evidence_refs`, `adjudicator_id` |
| `SelectedParentEdge` | `commit_oid`, `parent_oid` |
| `LineageResolution` | `link_id`, `resolution`, `affected_group_ids`, `rationale` |
| `CandidateGroupDecision` | `fix_group_id`, `commits`, `selected_parent_edges`, `root_cause_evidence_refs`, `case_decisions`, `adjudicator_id`, `rationale` |
| `CandidateFixGroup` | `fix_group_id`, `group_decision_sha256`, `commits`, `before_commit`, `after_commit`, `after_committed_at`, `changed_paths`, `config_only`, `diff_evidence`, `cases`, `disposition_summary`, `rationale`, `lineage_links`, `counts_toward_gate` |
| `EvidenceCounts` | `proposed`, `qualified_candidate`, `accepted`, `rejected`, `ambiguous` |
| `ApplicabilityRow` | `defect_class`, `domain_applicability`, `implementation_support`, `evidence_counts` |
| `GateSummary` | `status`, `independent_proposed_groups`, `domain_applicable_proposed_classes`, `required_groups`, `required_classes`, `reason_code_counts`, `failure_reasons`, `next_action` |
| `AdjudicationPayload` | `schema_version`, `source_id`, `evidence_revision`, `discovery_ledger_sha256`, `candidate_universe_sha256`, `source_artifacts`, `group_decisions`, `candidate_decisions`, `lineage_resolutions` |
| `AdjudicationEvidence` | every `AdjudicationPayload` field plus `review_attestation` |
| `CandidateLedger` | `schema_version`, `source_id`, `source_profile`, `source_profile_sha256`, `search_registration`, `discovery_ledger_sha256`, `candidate_universe_sha256`, `adjudication_evidence_sha256`, `evidence_revision`, `adjudicator_ids`, `reviewer_ids`, `groups`, `candidate_decisions`, `applicability_matrix`, `gate_summary`, `lineage_resolutions` |
| `B0ADecision` | `schema_version`, `source_id`, `candidate_ledger_sha256`, `gate` |
| `ReviewPackage` | `schema_version`, `source_id`, `candidate_universe_sha256`, `discovery_ledger_sha256`, `review_status`, `rows` |

`ReviewAttestation` is the only generic model allowed to carry reviewer identity. Per-row and per-group
models carry only adjudicator identity; this prevents an Agent proposal from pre-populating a fake human
reviewer throughout the payload. `EvidenceCounts.qualified_candidate` and `.accepted` exist for the full
contract but generic B0A validators require both to remain zero.

- [ ] **Step 5: Run focused and legacy contract tests**

Run:

```bash
uv run pytest tests/bench/external_corpus/test_contracts.py tests/bench/test_flare_evidence.py -q
git diff --exit-code 755fe2e -- scenarios/flare_corpus
```

Expected: both test modules pass; the Git diff is empty.

- [ ] **Step 6: Commit the contract extraction**

```bash
git add gameforge/bench/external_corpus gameforge/bench/flare_evidence.py tests/bench/external_corpus tests/bench/test_flare_evidence.py
git commit -m "refactor(bench): extract external evidence contracts"
```

---

### Task 2: Generic Read-Only Git Boundary and Discovery Engine

**Files:**
- Create: `gameforge/bench/external_corpus/git.py`
- Create: `gameforge/bench/external_corpus/discovery.py`
- Create: `tests/bench/external_corpus/git_fixture.py`
- Create: `tests/bench/external_corpus/test_git.py`
- Create: `tests/bench/external_corpus/test_discovery.py`
- Modify: `gameforge/bench/flare_git.py`
- Modify: `tests/bench/test_flare_discover.py`
- Test: `tests/bench/test_flare_direct_match_replay.py`

**Interfaces:**
- Consumes: Task 1 contracts and immutable CAS functions.
- Produces: `ReadOnlyGitRepo` and `discover_candidates()` shared by all registered profiles.

- [ ] **Step 1: Write a two-profile synthetic discovery test first**

Build one deterministic Git fixture with root, ordinary, merge, rename, binary, mixed-path, direct-match, adjacent, cherry-pick-trailer, revert, and duplicate-patch commits. Assert two different profiles select different candidates solely from profile data:

```python
def test_profiles_share_one_engine_without_source_conditionals(generic_git_repo, blob_dir):
    flare_like = profile_fixture(
        source_id="fixture_flare",
        include=("mods/**/*.txt",),
        message_pattern="(?i)fix",
        order_direction="ascending",
        limit=100,
    )
    sky_like = profile_fixture(
        source_id="fixture_sky",
        include=("data/**/*.txt",),
        message_pattern="(?i)(?:fix|missing)",
        order_direction="descending",
        limit=3,
    )
    flare_result = discover_candidates(repo, flare_like, registration(flare_like), blob_dir)
    sky_result = discover_candidates(repo, sky_like, registration(sky_like), blob_dir)
    assert flare_result.source_id == "fixture_flare"
    assert sky_result.source_id == "fixture_sky"
    assert len(sky_result.discovered_candidates) == 3
    assert [c.commit.committed_at for c in sky_result.discovered_candidates] == sorted(
        (c.commit.committed_at for c in sky_result.discovered_candidates), reverse=True
    )
```

Add negative tests for wrong head, wrong expected range count, wrong matched/config-only totals, registration/profile mismatch, partial/promisor clones, repo-local attributes, option-shaped public revisions, duplicate paths, unsupported rename status, malformed binary diff, missing CAS, and any use of `shell=True`.

- [ ] **Step 2: Run focused tests and verify they fail before implementation**

```bash
uv run pytest tests/bench/external_corpus/test_git.py tests/bench/external_corpus/test_discovery.py -q
```

Expected: import failure for `external_corpus.git` or `external_corpus.discovery`.

- [ ] **Step 3: Move the Git boundary and make history range source-neutral**

Move `GitEvidenceError`, `_CommitMetadata`, path/OID validation, repository discovery, fixed child environment, partial/promisor checks, local attribute checks, `_run`, `resolve`, metadata/path/patch/patch-id/version methods from `flare_git.py` to `external_corpus/git.py`.

Replace only the Flare-specific `reachable_commits(spec)` argument with `reachable_commits(profile)`:

```python
def reachable_commits(self, profile: SourceProfile) -> list[str]:
    revision = profile.pinned_head
    if profile.history_range.after_exclusive_oid is not None:
        revision = f"{profile.history_range.after_exclusive_oid}..{profile.pinned_head}"
    args = ["rev-list", "--topo-order", "--reverse"]
    if profile.history_range.committed_at_gte is not None:
        args.append(f"--since={profile.history_range.committed_at_gte}")
    args.append(revision)
    commits = self._ascii_oid_lines(self._run(args), "Git history")
    if len(commits) != profile.history_range.expected_commit_count:
        raise GitEvidenceError(
            "reachable revision count differs from frozen expectation: "
            f"expected {profile.history_range.expected_commit_count}, observed {len(commits)}"
        )
    return commits
```

Retain `--no-renames`, binary-safe patch bytes, fixed `core.attributesFile=/dev/null`, fixed locale/timezone, and first-parent diff selection. Expose `_preflight_object_reads()` as public `preflight()` while retaining the legacy private alias.

- [ ] **Step 4: Implement exhaustive generic discovery**

`discover_candidates()` must perform this exact sequence:

1. Revalidate profile and registration from JSON-mode dumps.
2. Preflight repository completeness and attributes once.
3. Resolve and compare the pinned head.
4. Read the complete registered history and objective commit/path facts.
5. Mark a commit matched when it has at least one eligible path and any registered message or eligible-patch diff rule matches.
6. Derive `config_only` only from all changed paths matching include and not exclude globs.
7. Derive trailer links and stable patch-id equivalence; do not infer semantic sameness from subject text.
8. Sort the full matched set by registered terms/directions, assert registered matched and config-only totals, then select the first `candidate_limit`.
9. Store full patches for selected candidates in CAS, plus exact eligible patch bytes when a diff rule was used.
10. Bind the embedded profile hash, registration, runtime versions, totals, selected candidates, lineage links, and candidate-universe hash in `DiscoveryLedger`.

The candidate-universe hash input is exactly:

```python
universe_payload = {
    "source_id": profile.source_id,
    "profile_sha256": sha256_hex(canonical_bytes(profile)),
    "ordered_candidate_oids": [
        candidate.commit.commit_oid for candidate in selected_candidates
    ],
}
```

- [ ] **Step 5: Preserve Flare legacy behavior through a compatibility bridge**

Keep every public Flare signature. `flare_git.ReadOnlyGitRepo` re-exports the generic class. The legacy `discover_candidates(repo, FlareSearchSpec, registration, round_name, blob_dir)` converts the selected Flare round to an internal generic discovery policy, invokes shared objective Git/discovery helpers, then constructs the existing Flare `DiscoveryLedger` model. It must not serialize a new `SourceProfile` into frozen Flare artifacts.

Add a golden test that runs the legacy CLI against its fixture and compares canonical bytes before and after the refactor. Do not update expected Flare JSON.

- [ ] **Step 6: Run generic and all Flare discovery tests**

```bash
uv run pytest \
  tests/bench/external_corpus/test_git.py \
  tests/bench/external_corpus/test_discovery.py \
  tests/bench/test_flare_discover.py \
  tests/bench/test_flare_direct_match_replay.py -q
git diff --exit-code 755fe2e -- scenarios/flare_corpus
```

Expected: all pass; no frozen-byte diff.

- [ ] **Step 7: Commit generic discovery**

```bash
git add gameforge/bench/external_corpus gameforge/bench/flare_git.py tests/bench
git commit -m "refactor(bench): generalize external candidate discovery"
```

---

### Task 3: Generic Offline Adjudication, Review Package, and CLI

**Files:**
- Create: `gameforge/bench/external_corpus/adjudication.py`
- Create: `gameforge/bench/external_corpus/mining.py`
- Create: `tests/bench/external_corpus/test_adjudication.py`
- Create: `tests/bench/external_corpus/test_mining_cli.py`
- Modify: `gameforge/bench/flare_adjudication.py`
- Modify: `gameforge/bench/flare_mining.py`
- Test: `tests/bench/test_flare_adjudication.py`
- Test: `tests/bench/test_flare_mining_cli.py`

**Interfaces:**
- Consumes: generic discovery ledger, CAS, profile taxonomy/gate, human-reviewed evidence.
- Produces: deterministic review package, candidate ledger, B0A decision, and stable CLI exit codes.

- [ ] **Step 1: Write failing adjudication and CLI tests**

Test these exact outcomes:

```python
def test_gate_counts_independent_groups_and_applicable_classes_only():
    ledger, decision = adjudicate(discovery, reviewed_evidence(groups=8, classes=4))
    assert decision.gate.status == "pass"
    assert decision.gate.next_action == "proceed_to_b0b"
    assert decision.gate.independent_proposed_groups == 8
    assert decision.gate.domain_applicable_proposed_classes == 4


def test_unattested_or_agent_attested_payload_cannot_produce_a_decision():
    with pytest.raises(AdjudicationError, match="human review attestation"):
        adjudicate(discovery, unreviewed_payload)


def test_every_selected_candidate_has_exactly_one_assignment():
    evidence = reviewed_evidence().model_copy(
        update={"candidate_decisions": reviewed_evidence().candidate_decisions[:-1]}
    )
    with pytest.raises(AdjudicationError, match="every discovered candidate"):
        adjudicate(discovery, evidence)
```

Also cover: reviewer differs from all adjudicators; full payload hash binding; group IDs/case IDs/assignments unique; groups contiguous on first parent when multi-commit; non-config candidates cannot enter proposed groups; evidence refs resolve and belong to owner; lineage siblings cannot both count; reverts never count; not-applicable cases cannot be proposed; gate boundaries 7/4, 8/3, 8/4; CAS tamper; canonical input; immutable outputs; one-line errors; exit 0 pass, 3 insufficient, 1 evidence/domain failure, 2 argparse failure.

- [ ] **Step 2: Run the new tests red**

```bash
uv run pytest tests/bench/external_corpus/test_adjudication.py tests/bench/external_corpus/test_mining_cli.py -q
```

Expected: missing-module failures.

- [ ] **Step 3: Implement source-neutral adjudication**

Adapt the proven Flare algorithms without its two-round prefix state:

- `_validate_evidence_refs()` resolves commit message, patch blob, lineage link, and source artifact refs and enforces owner locality.
- `_validate_assignments()` requires the selected universe exactly once across groups or candidate decisions.
- `_derive_group()` binds before/after first-parent endpoints, changed paths, diff evidence, case decisions, and a canonical group-decision hash.
- `_validate_lineage()` computes connected components over objective patch-id/cherry-pick/backport links and makes only the reviewed canonical representative count. Revert endpoints never count.
- `derive_applicability_matrix()` uses the profile's complete taxonomy rows; it has no source-specific expected values.
- `evaluate_supply_gate()` reads both thresholds from `profile.b0a_protocol` and returns only `pass/proceed_to_b0b` or `insufficient_evidence/stop_source_and_use_fallback`.
- `adjudicate()` replays everything offline and never calls Git, a network, an LLM, or a source-specific predicate.

Make Flare's `evaluate_provisional_gate()` call the shared threshold-count primitive while preserving legacy statuses `provisional_pass`, `expanded_round_required`, and `insufficient_evidence`. Keep Flare prior-round validation in the legacy module.

- [ ] **Step 4: Implement a non-approving review package**

`build_review_package()` emits one row per selected candidate in exact discovery order with commit, subject, timestamp, changed paths, config-only flag, patch digest, lineage links, empty assignment fields, and the candidate-universe hash. It must not emit a reviewer identity or an adjudication disposition.

The package has `review_status="awaiting_human"`; attempting to parse it as `AdjudicationEvidence` fails. This keeps Agent-produced analysis distinct from human ground truth.

- [ ] **Step 5: Implement the generic CLI**

Support exactly:

```text
python -m gameforge.bench.external_corpus.mining discover
  --repo PATH --profile PATH --registration-commit OID
  --registration-path PATH --out PATH --blob-dir PATH

python -m gameforge.bench.external_corpus.mining review-package
  --ledger PATH --out PATH

python -m gameforge.bench.external_corpus.mining adjudicate
  --ledger PATH --evidence PATH --blob-dir PATH
  --out PATH --decision-out PATH
```

The generic CLI uses the same canonical loader, CAS verifier, and immutable writers. It has no source-specific subcommands and never auto-fills review fields.

- [ ] **Step 6: Run generic and legacy adjudication/CLI suites**

```bash
uv run pytest \
  tests/bench/external_corpus/test_adjudication.py \
  tests/bench/external_corpus/test_mining_cli.py \
  tests/bench/test_flare_adjudication.py \
  tests/bench/test_flare_mining_cli.py -q
git diff --exit-code 755fe2e -- scenarios/flare_corpus
```

Expected: pass and no frozen-byte diff.

- [ ] **Step 7: Commit adjudication and CLI**

```bash
git add gameforge/bench/external_corpus gameforge/bench/flare_adjudication.py gameforge/bench/flare_mining.py tests/bench
git commit -m "feat(bench): add generic external evidence workflow"
```

---

### Task 4: Explicit Profiles and Anti-Specialization Gates

**Files:**
- Create: `gameforge/bench/external_corpus/profiles/__init__.py`
- Create: `gameforge/bench/external_corpus/profiles/flare.py`
- Create: `gameforge/bench/external_corpus/profiles/endless_sky.py`
- Create: `tests/bench/external_corpus/test_profiles.py`
- Create: `tests/bench/external_corpus/test_anti_specialization.py`
- Modify: `tests/test_dependency_lint.py`

**Interfaces:**
- Consumes: `SourceProfile` and legacy `FlareSearchSpec`.
- Produces: explicit `get_profile_binding(source_id)` registry and core source-name lint.

- [ ] **Step 1: Write profile conformance and forbidden-import tests**

```python
@pytest.mark.parametrize("source_id", ["flare", "endless_sky"])
def test_registered_profile_uses_the_generic_contract(source_id):
    binding = get_profile_binding(source_id)
    assert binding.source_id == source_id
    assert binding.profile_model is SourceProfile
    assert callable(binding.validate_source_profile)


def test_deterministic_core_has_no_external_profile_import_or_source_literal():
    forbidden_roots = (
        ROOT / "gameforge/contracts",
        ROOT / "gameforge/spine/ir",
        ROOT / "gameforge/spine/dsl",
        ROOT / "gameforge/spine/checkers",
        ROOT / "gameforge/spine/sim",
    )
    assert_no_import(forbidden_roots, "gameforge.bench.external_corpus.profiles")
    assert_no_tokens(forbidden_roots, {"endless_sky", "flare-game", "b10b7d6c"})
```

Also scan `gameforge/bench/taxonomy.py`, `metrics.py`, `report.py`, and `power.py`. The ingestion Adapter boundary is intentionally excluded from the source-name ban, but checkers/sim may not import ingestion.

- [ ] **Step 2: Run tests red**

```bash
uv run pytest tests/bench/external_corpus/test_profiles.py tests/bench/external_corpus/test_anti_specialization.py tests/test_dependency_lint.py -q
```

Expected: registry/profile modules are absent.

- [ ] **Step 3: Implement a static registry**

Use a literal mapping, not entry points or filesystem discovery:

```python
PROFILE_BINDINGS: Mapping[str, SourceProfileBinding] = MappingProxyType(
    {
        "flare": FLARE_PROFILE_BINDING,
        "endless_sky": ENDLESS_SKY_PROFILE_BINDING,
    }
)


def get_profile_binding(source_id: str) -> SourceProfileBinding:
    try:
        return PROFILE_BINDINGS[source_id]
    except KeyError as exc:
        raise ValueError(f"unknown external source profile: {source_id}") from exc
```

The Flare binding converts legacy search rules into an in-memory `SourceProfile` only for generic conformance/testing; it never rewrites frozen Flare JSON. The Endless Sky binding validates source ID, repository URL, pin, path policy, and license fields from a registered JSON profile; no game semantics enter generic modules.

- [ ] **Step 4: Run profile, dependency, import-linter, and Ruff gates**

```bash
uv run pytest tests/bench/external_corpus/test_profiles.py tests/bench/external_corpus/test_anti_specialization.py tests/test_dependency_lint.py -q
uv run lint-imports
uv run ruff check gameforge/bench tests/bench tests/test_dependency_lint.py
```

Expected: all pass; 7 import contracts kept.

- [ ] **Step 5: Commit profiles and lints**

```bash
git add gameforge/bench/external_corpus/profiles tests/bench/external_corpus tests/test_dependency_lint.py
git commit -m "test(bench): enforce source-neutral external evidence"
```

---

### Task 5: Preregister Endless Sky B0A Before Discovery

**Files:**
- Create: `scenarios/external_corpus/endless_sky/source-profile.json`
- Create: `scenarios/external_corpus/endless_sky/NOTICE`
- Create: `scenarios/external_corpus/endless_sky/LICENSE.endless-sky.txt`
- Create: `scenarios/external_corpus/endless_sky/COPYRIGHT.endless-sky`
- Create: `tests/bench/external_corpus/test_endless_sky_registration.py`
- Modify: `docs/superpowers/plans/README.md`

**Interfaces:**
- Consumes: profile contracts/registry and upstream pin `b10b7d6c24496e2f67a230a2553b344e200ba289`.
- Produces: a canonical, committed, hash-addressable B0A search registration before any discovery artifact exists.

- [ ] **Step 1: Write registration tests before adding artifacts**

```python
def test_registered_endless_sky_profile_is_canonical_and_exact():
    raw = PROFILE_PATH.read_bytes()
    profile = SourceProfile.model_validate_json(raw)
    assert raw == canonical_bytes(profile)
    assert profile.repository_url == "https://github.com/endless-sky/endless-sky.git"
    assert profile.pinned_head == "b10b7d6c24496e2f67a230a2553b344e200ba289"
    assert profile.history_range.committed_at_gte == 1672531200
    assert profile.history_range.expected_commit_count == 2508
    assert profile.b0a_protocol.expected_matched_candidate_count == 610
    assert profile.b0a_protocol.expected_config_only_candidate_count == 562
    assert profile.b0a_protocol.candidate_limit == 80


def test_registration_contains_no_discovery_or_adjudication_result():
    names = {path.name for path in PROFILE_PATH.parent.iterdir()}
    assert "candidate-ledger.discovered.json" not in names
    assert "adjudication-evidence.json" not in names
    assert "candidate-ledger.json" not in names
    assert "b0a-decision.json" not in names
```

- [ ] **Step 2: Run registration tests red**

```bash
uv run pytest tests/bench/external_corpus/test_endless_sky_registration.py -q
```

Expected: profile path is missing.

- [ ] **Step 3: Create the canonical profile with exact preregistered values**

The canonical profile must encode:

```json
{
  "schema_version": "external-source-profile@1",
  "source_id": "endless_sky",
  "profile_version": "endless-sky-b0a@1",
  "repository_url": "https://github.com/endless-sky/endless-sky.git",
  "pinned_head": "b10b7d6c24496e2f67a230a2553b344e200ba289",
  "history_range": {
    "committed_at_gte": 1672531200,
    "expected_commit_count": 2508
  },
  "config_include_globs": ["data/**/*.txt"],
  "config_exclude_globs": [],
  "message_rules": [
    {"rule_id": "subject.fix_or_missing", "pattern": "(?i)(?:fix|missing)"}
  ],
  "diff_rules": [],
  "lineage_rules": [
    {"rule_id": "trailer.backport_of", "link_type": "backport", "pattern": "(?m)^Backport-of: ([0-9a-f]{40})$"},
    {"rule_id": "trailer.cherry_pick_x", "link_type": "cherry_pick", "pattern": "(?m)^\\(cherry picked from commit ([0-9a-f]{40})\\)$"},
    {"rule_id": "trailer.git_revert", "link_type": "revert", "pattern": "(?m)^This reverts commit ([0-9a-f]{40})\\.$"}
  ],
  "candidate_order": [
    {"field": "committed_at", "direction": "descending"},
    {"field": "commit_oid", "direction": "ascending"}
  ],
  "license_id": "GPL-3.0-or-later",
  "notice_files": ["copyright", "license.txt", "credits.txt"],
  "native_validator_commands": [
    {
      "command_id": "endless_sky.parse_and_check_references",
      "argv": ["{engine_binary}", "--resources", "{case_root}", "--config", "{scratch_config}", "--parse-save"],
      "network": "forbidden"
    }
  ],
  "parser_version": "endless-sky-parser.b10b7d6c",
  "query_complete_closure": [
    "changed_files",
    "referenced_data_nodes",
    "mission_condition_dependencies",
    "map_route_dependencies",
    "outfit_ship_dependencies"
  ],
  "qualification_predicate_ids": [
    "reference_resolves",
    "drop_source_exists",
    "target_reachable",
    "dependency_acyclic",
    "mission_offerable",
    "mission_completion_satisfiable",
    "reward_within_declared_bounds",
    "narrative_character_consistent",
    "narrative_spoiler_free",
    "narrative_faction_consistent",
    "narrative_unique"
  ],
  "b0a_protocol": {
    "candidate_limit": 80,
    "expected_matched_candidate_count": 610,
    "expected_config_only_candidate_count": 562,
    "minimum_independent_groups": 8,
    "minimum_domain_applicable_classes": 4
  }
}
```

Add all 15 taxonomy rows. Mark `gacha_expectation_violation`, `prob_sum_ne_1`, `non_monotonic_curve`, and `economy_collapse` domain `not_applicable` with source-semantic reasons; mark the remaining 11 domain `applicable`. Mark all implementation support `planned` because B0A precedes Adapter implementation.

- [ ] **Step 4: Freeze upstream notices from the pinned tree**

Use raw bytes from the pinned commit, not the moving branch:

```bash
env GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$ENDLESS_SKY_REPO" show b10b7d6c24496e2f67a230a2553b344e200ba289:license.txt > /tmp/endless-sky-license.txt
env GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$ENDLESS_SKY_REPO" show b10b7d6c24496e2f67a230a2553b344e200ba289:copyright > /tmp/endless-sky-copyright
```

Add those bytes with `apply_patch` or a byte-preserving copy command reviewed by `shasum -a 256`. `NOTICE` records upstream URL, pinned OID, the three upstream notice paths, and that only patch evidence/config text may later be redistributed under GPL-3.0-or-later.

- [ ] **Step 5: Run canonical/profile tests and commit the registration alone**

```bash
uv run pytest tests/bench/external_corpus/test_endless_sky_registration.py -q
git diff --check
git add scenarios/external_corpus/endless_sky tests/bench/external_corpus/test_endless_sky_registration.py docs/superpowers/plans/README.md
git commit -m "data(bench): preregister Endless Sky B0A"
```

Expected: this commit contains no discovered ledger, patch CAS, dispositions, or decision. Its OID becomes `SearchRegistration.project_commit_oid`.

---

### Task 6: Run and Freeze the Real First-80 Discovery Universe

**Files:**
- Create: `scenarios/external_corpus/endless_sky/candidate-ledger.discovered.json`
- Create: `scenarios/external_corpus/endless_sky/review-package.json`
- Create: `scenarios/external_corpus/endless_sky/blobs/<sha256>`
- Create: `tests/bench/external_corpus/test_endless_sky_discovery_replay.py`

**Interfaces:**
- Consumes: the Task 5 registration commit, complete upstream mirror, generic discovery/CLI.
- Produces: immutable selected universe and a non-approving full review package.

- [ ] **Step 1: Obtain and verify a complete offline mirror**

The admissible command bypasses the user's HTTPS-to-SSH rewrite and does not use partial clone:

```bash
env GIT_CONFIG_GLOBAL=/dev/null git clone --mirror \
  https://github.com/endless-sky/endless-sky.git \
  "$HOME/.cache/gameforge/evidence/endless-sky-complete.git"
env GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$HOME/.cache/gameforge/evidence/endless-sky-complete.git" fsck --full --strict
env GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$HOME/.cache/gameforge/evidence/endless-sky-complete.git" \
  cat-file -e 'b10b7d6c24496e2f67a230a2553b344e200ba289^{commit}'
test "$(env GIT_CONFIG_GLOBAL=/dev/null git --git-dir="$HOME/.cache/gameforge/evidence/endless-sky-complete.git" \
  rev-list --count b10b7d6c24496e2f67a230a2553b344e200ba289)" = "9883"
```

Expected: fsck success, the exact pinned commit exists, and its reachable history count is exact. The
remote branch may advance after preregistration without changing this run. A promisor marker or config
is a hard failure.

- [ ] **Step 2: Add a replay test before creating real outputs**

The test loads tracked profile/ledger/CAS and asserts:

```python
assert ledger.observed_history_count == 2508
assert ledger.matched_candidate_count == 610
assert ledger.config_only_candidate_count == 562
assert len(ledger.discovered_candidates) == 80
assert sum(candidate.config_only for candidate in ledger.discovered_candidates) == 75
assert ledger.discovered_candidates[0].commit.commit_oid == "c55df3918b9aa6052bda0aca7f6b6fe4d10a1d77"
assert review_package.review_status == "awaiting_human"
assert review_package.candidate_universe_sha256 == ledger.candidate_universe_sha256
```

It verifies every patch blob hash and recomputes every message direct-match from the embedded profile without Git/network.

- [ ] **Step 3: Run the real discovery once from the registration commit**

```bash
REGISTRATION_COMMIT=$(git log -1 --format=%H -- scenarios/external_corpus/endless_sky/source-profile.json)
uv run python -m gameforge.bench.external_corpus.mining discover \
  --repo "$HOME/.cache/gameforge/evidence/endless-sky-complete.git" \
  --profile scenarios/external_corpus/endless_sky/source-profile.json \
  --registration-commit "$REGISTRATION_COMMIT" \
  --registration-path scenarios/external_corpus/endless_sky/source-profile.json \
  --out scenarios/external_corpus/endless_sky/candidate-ledger.discovered.json \
  --blob-dir scenarios/external_corpus/endless_sky/blobs
```

Expected stderr: `discovery complete: selected=80 matched=610 config_only=562`. Any different count stops the run; do not edit thresholds after seeing results.

- [ ] **Step 4: Generate the complete non-approving review package**

```bash
uv run python -m gameforge.bench.external_corpus.mining review-package \
  --ledger scenarios/external_corpus/endless_sky/candidate-ledger.discovered.json \
  --out scenarios/external_corpus/endless_sky/review-package.json
```

Expected: exactly 80 ordered rows, `review_status=awaiting_human`, no disposition, adjudicator, reviewer, or attestation fields.

- [ ] **Step 5: Verify deterministic replay twice and Flare freeze**

Run discovery into two temporary output/CAS directories using the same registration, then compare canonical ledgers and sorted blob hashes byte-for-byte. Run:

```bash
uv run pytest tests/bench/external_corpus/test_endless_sky_discovery_replay.py -q
git diff --exit-code 755fe2e -- scenarios/flare_corpus
git diff --check
```

Expected: pass and no Flare diff.

- [ ] **Step 6: Commit the discovered universe separately from review**

```bash
git add scenarios/external_corpus/endless_sky tests/bench/external_corpus/test_endless_sky_discovery_replay.py
git commit -m "data(bench): freeze Endless Sky B0A universe"
```

---

### Task 7: Complete the 80-Candidate Disposition Table Without Fabricating Review

**Files:**
- Create after real review: `scenarios/external_corpus/endless_sky/adjudication-evidence.json`
- Create after real review: `scenarios/external_corpus/endless_sky/candidate-ledger.json`
- Create after real review: `scenarios/external_corpus/endless_sky/b0a-decision.json`
- Create: `tests/bench/external_corpus/test_endless_sky_b0a_replay.py`
- Modify: `scenarios/external_corpus/endless_sky/blobs/<sha256>` only by adding cited source artifacts

**Interfaces:**
- Consumes: frozen review package and patch CAS; a genuine independent human attestation.
- Produces: complete assignment ledger and pass/fail investment decision, or no decision if human evidence is absent.

- [ ] **Step 1: Perform evidence-backed candidate analysis**

For all 80 rows in order, inspect the full commit message and patch. Assign each commit exactly once to either a contiguous fix group or a candidate-level disposition. Use only these disposition values: `proposed`, `rejected`, `ambiguous`; use structured reason codes for non-config, style/typo-only, non-bug, insufficient semantic evidence, out-of-scope, duplicate lineage, and revert.

Each proposed case records a `DefectClass`, exact evidence refs, affected source spans/identifiers, and a root-cause rationale. Similar subjects do not establish shared root cause; only patch/trailer/upstream source evidence may group commits.

Agent analysis may populate an unreviewed `AdjudicationPayload` outside the final evidence path. It must identify its adjudicator as an Agent and must not populate any human review field.

- [ ] **Step 2: Generate and hash the complete unreviewed payload**

Run the payload validator and print:

```text
candidate_universe_sha256=<64 lowercase hex>
reviewed_payload_sha256=<64 lowercase hex>
candidate_rows=80
assigned_rows=80
```

The payload hash excludes only `review_attestation`; it includes source/discovery/universe bindings,
all dispositions, groups, cases, evidence refs, source artifacts, and lineage resolutions. Taxonomy
applicability is already hash-bound inside the embedded source profile in the referenced discovery ledger.

- [ ] **Step 3: Apply the non-negotiable human evidence gate**

A human reviewer who is not any listed adjudicator must inspect the complete 80-row table and the cited diffs, then provide `reviewer_kind=human`, stable `reviewer_id`, UTC review time, exact candidate-universe hash, and exact payload hash. If no such review occurs, do not create `adjudication-evidence.json`, `candidate-ledger.json`, or `b0a-decision.json`; record the submilestone as `awaiting_human_evidence` and continue only with independent pre-M4 work such as the core-contract correction plan.

- [ ] **Step 4: Replay an actually attested payload**

Only after Step 3 succeeds:

```bash
uv run python -m gameforge.bench.external_corpus.mining adjudicate \
  --ledger scenarios/external_corpus/endless_sky/candidate-ledger.discovered.json \
  --evidence scenarios/external_corpus/endless_sky/adjudication-evidence.json \
  --blob-dir scenarios/external_corpus/endless_sky/blobs \
  --out scenarios/external_corpus/endless_sky/candidate-ledger.json \
  --decision-out scenarios/external_corpus/endless_sky/b0a-decision.json
```

Expected exit is 0 for `pass` or 3 for `insufficient_evidence`; both are valid empirical outcomes. Exit 1 is an evidence failure and blocks any decision.

- [ ] **Step 5: Test offline replay and commit the reviewed decision**

```bash
uv run pytest tests/bench/external_corpus/test_endless_sky_b0a_replay.py -q
git diff --exit-code 755fe2e -- scenarios/flare_corpus
git diff --check
git add scenarios/external_corpus/endless_sky tests/bench/external_corpus/test_endless_sky_b0a_replay.py
git commit -m "data(bench): record Endless Sky B0A decision"
```

If gate passes, only then write the B0B/Adapter plan. If it fails, stop Endless Sky investment and write a separate Wesnoth B0A plan using the same harness and unchanged thresholds.

---

### Task 8: Submilestone Acceptance and Status Anchor

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/README.md`
- Test: full repository suite

**Interfaces:**
- Consumes: Tasks 1-7 and their empirical gate state.
- Produces: truthful project status and a reviewable branch.

- [ ] **Step 1: Add an acceptance test for the engineering boundary**

The acceptance test must verify:

- generic contracts/discovery/adjudication/CLI and both profile conformance tests pass;
- Flare legacy imports and CLI replay existing frozen artifacts;
- `git diff 755fe2e -- scenarios/flare_corpus` is empty;
- Endless Sky registration precedes discovery in Git history;
- selected universe is exactly 80 from the registered order and counts bind 610/562;
- no source tokens/imports occur in forbidden core;
- B0A status is derived from artifacts: `awaiting_human_evidence`, `pass`, or `insufficient_evidence`; never inferred from prose.

- [ ] **Step 2: Update status without overstating evidence**

If no attestation exists, state that the reusable harness and frozen discovery universe are complete but B0A and M3 remain blocked on genuine review evidence. If decision is `pass`, state that B0A supply passed and B0B planning is unlocked only after the independent core-contract branch lands. If decision is `insufficient_evidence`, state that Endless Sky stopped and Wesnoth B0A is next. In all cases M4 remains unstarted.

- [ ] **Step 3: Run complete verification from a clean network-disabled shell**

```bash
uv run pytest
uv run lint-imports
uv run ruff check .
git diff --check
git diff --exit-code 755fe2e -- scenarios/flare_corpus
```

Expected: all tests pass with only the already-known optional transport skip; 7 import contracts kept; Ruff clean; no whitespace or frozen-byte diff.

- [ ] **Step 4: Request code review and fix all findings**

Use `requesting-code-review` against the approved design and this plan. Review specifically for source leakage, evidence/hash replay gaps, Flare compatibility, candidate-order bias, accidental human-evidence fabrication, and unnecessary concurrency/security machinery. Re-run Step 3 after every fix.

- [ ] **Step 5: Commit status documentation**

```bash
git add CLAUDE.md README.md docs/superpowers/plans/README.md tests
git commit -m "docs(pre-m4): record external evidence status"
```

Do not merge this branch as a completed B0A if Task 7 lacks genuine human evidence. The generic harness and preregistered/discovered universe may be reviewed and merged as an engineering increment while the empirical status remains explicitly `awaiting_human_evidence`.

---

## Self-Review

### Spec Coverage

- Design §3 source-neutral core/Profile/Adapter boundaries: Tasks 1, 2, and 4.
- Design §3.3 Flare compatibility and frozen bytes: every implementation task plus Task 8.
- Design §4.2 complete SourceProfile and separate AdapterBinding: Task 1.
- Design §4.3 first 80, 8 groups/4 classes, independent reviewer: Tasks 5-7.
- Design §4.4 B0B gate: intentionally not implemented; Task 7 only unlocks a later plan after B0A pass.
- Design §4.7 anti-specialization: Task 4.
- Design §12.1 generic synthetic profiles, Git boundaries, split/gate replay: Tasks 1-4; split/freeze implementation remains B0B by approved scope.
- Human-evidence integrity: Global Constraints, Tasks 3 and 7.

### Intentional Deferrals

- Qualification predicates are named and versioned in `SourceProfile`; their execution belongs to a post-pass B0B plan.
- `AdapterBinding` is fully contracted now; no false reader/Adapter version is instantiated during B0A.
- Wesnoth has no profile implementation until Endless Sky fails; the registry does not include a fake fallback artifact.
- No source-specific checker, simulation, metric, Patch, report, or frontend change occurs in this plan.

### Consistency Checks

- Upstream head: `b10b7d6c24496e2f67a230a2553b344e200ba289`.
- History reachable from the pinned upstream head: 9,883 commits.
- Registered UTC range start: Unix `1672531200` (2023-01-01T00:00:00Z).
- Registered range: 2,508 commits.
- `fix|missing` matched candidates with an eligible config path: 610.
- Config-only matched candidates: 562.
- Mechanical first-80 selection: 75 config-only, 5 mixed; first OID `c55df3918b9aa6052bda0aca7f6b6fe4d10a1d77`.
- Supply gate: 8 independent proposed groups and 4 domain-applicable proposed classes.
- Flare frozen universe/approval hashes remain the approved values in Global Constraints.

### Placeholder Scan

This plan contains no unresolved implementation placeholder, fabricated result, or future Adapter assumption. Runtime hashes produced only after the preregistration commit are intentionally derived outputs and are always bound and verified by the contracts rather than guessed in advance.
