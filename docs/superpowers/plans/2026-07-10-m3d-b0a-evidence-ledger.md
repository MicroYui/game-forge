# M3d B0A Flare Evidence Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a deterministic, offline-reviewable B0A mining harness that freezes the Flare candidate universe, groups independent config-only fixes with explicit human evidence, and returns `provisional_pass`, the nonterminal `expanded_round_required`, or terminal `insufficient_evidence` at the 8-group/4-class investment gate.

**Architecture:** `flare_evidence.py` owns strict evidence contracts, canonical bytes, and the filesystem CAS; `flare_git.py` is the only read-only Git boundary and produces objective candidate facts; `flare_adjudication.py` consumes an explicit offline decision file and derives group summaries, the 11-class matrix, and the gate; `flare_mining.py` exposes only `discover` and `adjudicate`. B0A never imports or runs the Flare adapter, a checker, an LLM, or network code.

**Tech Stack:** Python 3.12 via `uv`; pydantic v2; stdlib `subprocess`, `hashlib`, `pathlib`, `json`, and `argparse`; pytest; import-linter; Ruff.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-10-m3d-flare-rich-design.md`; B0A ends at the provisional gate in design §9.
- B0A implements `discover` and `adjudicate` only. `probe`, qualification replay, temporal split, `freeze`, InputTree, production reader/checker/matcher/scorer, and `external@2` are later plans.
- Source is `https://github.com/flareteam/flare-game.git`, pinned through `fe23b5ba73f99f0c3969f8b23dbabaa8f7a6b602`; omitted `after_exclusive` is parsed as `None` and means all 7,049 commits reachable through that head, including merged branch commits. First-parent continuity applies to each manually grouped multi-commit fix, not to candidate discovery.
- The search spec freezes `initial` and `expanded` rounds before the first harness discovery output is produced. No post-output search rule may be added.
- The exact search spec is committed alone before harness discovery. Every ledger records that registration commit and the canonical search-spec SHA-256. Prior exploratory knowledge is disclosed; this is pre-registered and non-blind, not a claim of blind candidate selection.
- Git invocations are read-only argument arrays with `shell=False`; `{repo}` resolves to the repository Git directory for command execution (a bare repository is already its Git directory), while diagnostics retain the exact CLI `--repo` path. The harness never clones, fetches, opens GitHub, or accepts a shell fragment. Untracked or staged worktree `.gitattributes` therefore cannot alter evidence bytes.
- Discover records facts and objective `patch_id | cherry_pick | backport | revert` links. It never assigns a taxonomy label or infers root cause. The candidate universe starts with unique-OID direct frozen-rule matches plus frozen one-edge first-parent context, then repeatedly follows selected targets' frozen cherry-pick/backport/revert trailer captures to reachable source OIDs. Each candidate records `direct_match | adjacent_context | lineage_context` and its rule/link IDs. Patch-ID collisions are computed only inside the closed candidate universe and never select a candidate.
- Human root-cause grouping and taxonomy proposals come only from a checked-in offline evidence JSON. Every candidate is decided as `proposed`, `rejected`, or `ambiguous` with rationale and evidence references.
- Evidence references are structured and mechanically resolved to a discovered commit/message, patch blob, objective lineage link, or frozen source artifact. A hash-bound approval attestation from a reviewer distinct from every adjudicator is mandatory for both initial and expanded evidence.
- B0A covers exactly the 11 deterministic/simulation taxonomy classes. `prob_sum_ne_1` and `gacha_expectation_violation` are `not_applicable`; `economy_collapse` is `applicable` even when evidence is absent.
- A `not_applicable` class cannot have a proposed case. `evidence_availability` is derived from case counts, never declared independently. The provisional gate passes only with at least 8 independent config-only fix groups and at least 4 domain-applicable proposed classes. `qualified_candidate` and `accepted` counts remain zero in B0A.
- Canonical JSON is UTF-8, sorted, compact, and newline-terminated. CAS names are lowercase 64-hex SHA-256. Existing output may be reused only when bytes are identical; differing bytes are never overwritten.
- Provenance is a one-way hash chain with no self-hashes: registered search spec -> canonical `DiscoveryLedger` bytes -> canonical full `AdjudicationEvidence` bytes -> canonical `CandidateLedger` bytes -> `B0ADecision`. Expanded evidence binds the prior candidate-ledger and decision bytes and must also receive the prior raw discovery and evidence so the derived pair can be replayed byte-for-byte.
- The registered discovery implementation is the trust anchor for raw Git facts and completeness of the 7,049-commit universe. Ledger validators recompute all facts derivable from the embedded frame and reject internally inconsistent canonical payloads; they do not claim to package a cryptographic proof of every upstream commit. The downstream attestation and canonical hash chain prevent unnoticed post-review drift.
- Both terminal results are valid B0A outcomes. `insufficient_evidence` stops Flare-heavy M3d work but does not satisfy PRD §13.3 or complete M3.
- No production dependency is added. The full gate remains `uv run pytest`, `uv run lint-imports`, and `uv run ruff check .`.

## Trusted Local Single-Writer Publication Note (2026-07-11) — ✅ complete

The B0A evidence directory is trusted local storage with one cooperative writer. It does not defend
against same-permission mutators, ancestor replacement, bind mounts, or filesystem-name aliases, and
does not promise a cross-path transaction or power-loss directory durability. The approved evidence
bytes, hashes, gate, and frozen corpus do not change.

Existing leaves must be regular files with identical bytes; symlinks, FIFOs, directories, and
different bytes are rejected. Every missing value is fully written and file-`fsync`ed to an exclusive
same-directory staging file before any final is published. After a normal target recheck, standard
library `os.replace` publishes finals in mapping order, with the complete prefix re-read before the
next item. Caught failures clean only staging created by that call; published finals remain available
for an identical retry. An abrupt process exit may leave Git-ignored staging or a complete prefix.
Ledger remains first and decision remains the completion marker.

---

### Task 1: Strict B0A contracts, canonical output, and patch CAS

**Files:**
- Modify: `.gitignore`
- Create: `gameforge/bench/flare_evidence.py`
- Create: `tests/bench/conftest.py`
- Create: `tests/bench/test_flare_evidence.py`

**Interfaces:**
- Produces: `FLARE_B0A_SCHEMA_VERSION = "flare-b0a@1"`
- Produces: `B0A_DEFECT_CLASSES: tuple[DefectClass, ...]`
- Produces: strict pydantic models `RegexRule`, `LineageRegexRule`, `GitCommandSpec`, `GitEnvironmentPolicy`, `SearchAdjacency`, `SearchRound`, `FlareSearchSpec`, `SearchRegistration`, `DiscoveryTool`, `CandidateCommit`, `SelectionReason`, `DiffEvidence`, `LineageLink`, `DiscoveredCandidate`, `DiscoveryLedger`, `EvidenceRef`, `EvidenceArtifact`, `ReviewAttestation`, `CandidateCase`, `CandidateDisposition`, `CandidateFixGroup`, `EvidenceCounts`, `ApplicabilityDeclaration`, `ApplicabilityRow`, `GateSummary`, `CandidateLedger`, `AdjudicationEvidence`, and `B0ADecision`
- Produces: `canonical_bytes(value: BaseModel | Mapping[str, Any]) -> bytes`
- Produces: `sha256_hex(data: bytes) -> str`
- Produces: `read_regular_file(path: Path) -> bytes`, rejecting non-regular leaf paths
- Produces: `posix_glob_matches(path: str, pattern: str) -> bool`, where `**` matches zero or more complete path components
- Produces: `write_new_or_identical(path: Path, data: bytes) -> None`, using the same staged publication protocol as the set writer
- Produces: `write_set_new_or_identical(outputs: Mapping[Path, bytes]) -> None`, which preflights every target, stages and `fsync`s every missing value before publication, then publishes complete bytes atomically per final path in mapping order while retaining any complete prefix after a later failure
- Produces: `put_blob(blob_dir: Path, data: bytes) -> tuple[str, str]`, returning `(sha256, f"blobs/{sha256}")`
- Consumes: `DefectClass`, `Bucket`, and `CLASS_META` from `gameforge.bench.taxonomy`

- [ ] **Step 1: Write failing contract and storage tests**

In `tests/bench/conftest.py`, define `REGISTERED_SEARCH_SPEC_PAYLOAD` as an independent literal copy of the exact Task 5 JSON payload, plus `REGISTERED_SEARCH_SPEC_BYTES` and `REGISTERED_SEARCH_SPEC_SHA256` derived with stdlib canonical JSON and SHA-256 rather than the production helper. Expose `registered_search_spec_payload`, `registered_search_spec_bytes`, and `registered_search_spec_sha256` fixtures; the payload fixture returns a deep copy. This is the schema lock shared by Task 1 and the package test: any later edit to the registered file must still match the independently expected bytes and hash exactly.

```python
# tests/bench/test_flare_evidence.py
import copy
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import gameforge.bench.flare_evidence as flare_evidence
from gameforge.bench.flare_evidence import (
    B0A_DEFECT_CLASSES,
    ApplicabilityDeclaration,
    ApplicabilityRow,
    CandidateCase,
    CandidateDisposition,
    EvidenceCounts,
    EvidenceRef,
    FlareSearchSpec,
    ReviewAttestation,
    SearchRegistration,
    canonical_bytes,
    posix_glob_matches,
    put_blob,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)


def test_b0a_scope_is_exactly_the_eleven_non_narrative_classes():
    assert len(B0A_DEFECT_CLASSES) == 11
    assert {item.value for item in B0A_DEFECT_CLASSES} >= {
        "dead_quest", "missing_drop_source", "economy_collapse"
    }
    assert "spoiler" not in {item.value for item in B0A_DEFECT_CLASSES}


def test_models_forbid_unknown_fields_and_b0a_cannot_claim_qualified():
    with pytest.raises(ValidationError, match="disposition"):
        CandidateCase(
            case_id="case-1",
            defect_class="dead_quest",
            disposition="qualified_candidate",
            rationale="not allowed before B0B",
            evidence_refs=[EvidenceRef(kind="commit_message", target_id="a" * 40)],
        )


def test_applicability_declaration_cannot_contain_derived_fields():
    with pytest.raises(ValidationError, match="evidence_counts"):
        ApplicabilityDeclaration(
            defect_class="dead_quest",
            domain_applicability="applicable",
            implementation_support="planned",
            evidence_counts=EvidenceCounts(proposed=1),
        )
    with pytest.raises(ValidationError, match="evidence_availability"):
        ApplicabilityDeclaration(
            defect_class="dead_quest",
            domain_applicability="applicable",
            implementation_support="planned",
            evidence_availability="found",
        )
    with pytest.raises(ValidationError):
        ApplicabilityRow(
            defect_class="dead_quest",
            domain_applicability="applicable",
            evidence_availability="found",
            evidence_counts=EvidenceCounts(
                proposed=1,
                qualified_candidate=0,
                accepted=0,
                rejected=0,
                ambiguous=0,
            ),
            implementation_support="planned",
            surprise=True,
        )


def test_out_of_taxonomy_rejection_has_no_fake_defect_class():
    decision = CandidateDisposition(
        commit_oid="a" * 40,
        disposition="rejected",
        reason_code="out_of_taxonomy",
        rationale="real timing bug, but no existing deterministic taxonomy predicate",
        evidence_refs=[EvidenceRef(kind="patch_blob", target_id="b" * 64)],
        adjudicator_id="assisted-review-1",
        reviewer_id="human-review-1",
    )
    assert "defect_class" not in decision.model_dump()


def test_canonical_bytes_are_stable_and_new_or_identical_is_immutable(tmp_path: Path):
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}\n'
    assert canonical_bytes(EvidenceCounts()) == (
        b'{"accepted":0,"ambiguous":0,"proposed":0,'
        b'"qualified_candidate":0,"rejected":0}\n'
    )
    target = tmp_path / "ledger.json"
    write_new_or_identical(target, b"same\n")
    write_new_or_identical(target, b"same\n")
    with pytest.raises(FileExistsError):
        write_new_or_identical(target, b"different\n")


def test_multi_output_publish_preflights_all_targets(tmp_path: Path):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    second.write_bytes(b"existing-different\n")
    with pytest.raises(FileExistsError):
        write_set_new_or_identical({first: b"new-ledger\n", second: b"new-decision\n"})
    assert not first.exists()
    assert second.read_bytes() == b"existing-different\n"


def test_multi_output_stages_all_files_before_publishing(tmp_path: Path, monkeypatch):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    real_create = flare_evidence._create_staging_file
    real_replace = os.replace
    staging_creates = 0
    published = []

    def fail_second(target):
        nonlocal staging_creates
        staging_creates += 1
        if staging_creates == 2:
            raise OSError("injected second-target failure")
        return real_create(target)

    def record_publish(source, target):
        published.append(Path(target))
        return real_replace(source, target)

    monkeypatch.setattr(flare_evidence, "_create_staging_file", fail_second)
    monkeypatch.setattr(os, "replace", record_publish)
    with pytest.raises(OSError, match="second-target"):
        write_set_new_or_identical({first: b"new-ledger\n", second: b"new-decision\n"})
    assert published == []
    assert not first.exists()
    assert not second.exists()


def test_multi_output_publish_never_modifies_preexisting_identical_file(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    first.write_bytes(b"identical-ledger\n")
    real_open = Path.open

    def fail_second(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise OSError("injected second-target failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="second-target"):
        write_set_new_or_identical({
            first: b"identical-ledger\n", second: b"new-decision\n"
        })
    assert first.read_bytes() == b"identical-ledger\n"
    assert not second.exists()


def test_blob_store_uses_lowercase_hex_and_verifies_existing_content(tmp_path: Path):
    digest, relative = put_blob(tmp_path / "blobs", b"patch bytes")
    assert len(digest) == 64 and digest == digest.lower()
    assert relative == f"blobs/{digest}"
    assert (tmp_path / relative).read_bytes() == b"patch bytes"
    assert put_blob(tmp_path / "blobs", b"patch bytes") == (digest, relative)
    (tmp_path / relative).write_bytes(b"tampered bytes")
    with pytest.raises(FileExistsError):
        put_blob(tmp_path / "blobs", b"patch bytes")


@pytest.mark.parametrize(
    "path",
    ["mods/settings.txt", "mods/core/settings.txt", "mods/core/quests/chapter/one.txt"],
)
def test_posix_double_star_matches_zero_one_or_many_components(path: str):
    assert posix_glob_matches(path, "mods/**/*.txt")
    assert not posix_glob_matches(path, "mods/**/languages/**/*.txt")


def test_explicit_localization_file_glob_is_not_confused_with_directory_glob():
    path = "mods/default/engine/languages.txt"
    assert not posix_glob_matches(path, "mods/**/languages/**")
    assert posix_glob_matches(path, "mods/**/languages.txt")


def test_search_spec_requires_the_complete_frozen_contract(
    registered_search_spec_payload
):
    spec = FlareSearchSpec.model_validate(registered_search_spec_payload)
    assert spec.message_field == "subject_percent_s_utf8"
    assert spec.lineage_message_field == "full_percent_B_utf8"
    assert spec.diff_match_scope == "eligible_path_patch_bytes"
    assert spec.diff_merge_policy == "exclude_multi_parent_commits_from_diff_direct"
    assert spec.path_glob_semantics == "component_fnmatch_double_star_zero_or_more"
    assert spec.candidate_path_gate == "any_changed_path_eligible"
    assert spec.config_only_rule == "all_changed_paths_eligible"
    assert spec.git_environment_policy.inherit_allowlist == ("PATH",)
    assert spec.git_environment_policy.drop_inherited_prefixes == ("GIT_",)
    assert [item.name for item in spec.rounds] == ["initial", "expanded"]
    assert spec.adjacency.first_parent_predecessor_edges == 1
    assert spec.adjacency.first_parent_child_edges == 1

    for field, bad_value in {
        "history_walk": "first_parent",
        "candidate_order": ["commit_oid"],
        "stop_condition": "first_100",
        "message_field": "full_percent_B_utf8",
        "diff_match_scope": "whole_patch",
        "diff_merge_policy": "include_merge_commits",
        "path_glob_semantics": "python_pathlib_match",
        "candidate_path_gate": "all_changed_paths_eligible",
        "config_only_rule": "any_changed_path_eligible",
    }.items():
        with pytest.raises(ValidationError, match=field):
            FlareSearchSpec.model_validate({**registered_search_spec_payload, field: bad_value})

    changed_commands = copy.deepcopy(registered_search_spec_payload)
    changed_commands["git_commands"]["patch_args"].remove("--no-textconv")
    with pytest.raises(ValidationError, match="git_commands"):
        FlareSearchSpec.model_validate(changed_commands)

    changed_environment = copy.deepcopy(registered_search_spec_payload)
    changed_environment["git_environment_policy"]["inherit_allowlist"].append("HOME")
    with pytest.raises(ValidationError, match="git_environment_policy"):
        FlareSearchSpec.model_validate(changed_environment)


def test_review_attestation_is_bound_to_the_payload_hash():
    payload = {"evidence_revision": "initial-r1", "group_decisions": []}
    attestation = ReviewAttestation(
        reviewer_id="human-review-1",
        review_scope="complete_b0a_adjudication",
        approval="approved",
        review_revision="review-r1",
        written_statement="I reviewed and approve the complete B0A disposition table.",
        candidate_universe_sha256="a" * 64,
        reviewed_payload_sha256=sha256_hex(canonical_bytes(payload)),
    )
    assert attestation.reviewed_payload_sha256 == sha256_hex(canonical_bytes(payload))


def test_evidence_ref_is_structured_and_rejects_kind_target_mismatch():
    assert EvidenceRef(kind="commit_message", target_id="a" * 40).target_id == "a" * 40
    with pytest.raises(ValidationError):
        EvidenceRef(kind="patch_blob", target_id="not-a-sha256")


def test_search_registration_requires_commit_and_repo_relative_json_path():
    registration = SearchRegistration(
        project_commit_oid="a" * 40,
        repo_relative_path="scenarios/flare_corpus/search-spec.json",
    )
    assert registration.project_commit_oid == "a" * 40
    with pytest.raises(ValidationError):
        SearchRegistration(project_commit_oid="a" * 40, repo_relative_path="/tmp/spec.json")
```

Also add subprocess regressions that exit uncatchably during single-output and second multi-output staging writes. Neither case may expose a partial final path; the multi-output case must publish no final path because all missing values are staged before the first publication. A normal retry must succeed without deleting a residual hidden staging file from an earlier interrupted process. Add a Git regression proving `.gameforge-*.tmp` is ignored under `scenarios/flare_corpus` while canonical corpus JSON is not. Add publication-failure regressions proving an already published complete prefix remains reusable, caught failures clean only the current call's unpublished staging, and a retry can add the decision completion marker.

- [ ] **Step 2: Run the tests to verify RED**

Run: `uv run pytest tests/bench/test_flare_evidence.py -q`

Expected: collection fails because `gameforge.bench.flare_evidence` does not exist.

- [ ] **Step 3: Implement the strict models and immutable storage primitives**

Use a shared base with `ConfigDict(extra="forbid", frozen=True)`. Validate OIDs as 40 lowercase hex, blob hashes as 64 lowercase hex, relative blob paths as `blobs/{sha256}`, unique rule IDs, unique round names exactly `initial, expanded`, exact 11-class matrix membership, fixed Flare applicability for the two N/A classes and `economy_collapse`, and zero B0A `qualified_candidate/accepted` counts. Derive `B0A_DEFECT_CLASSES` from `CLASS_META` where the bucket is not `Bucket.llm_assisted`; do not duplicate the taxonomy list. `ApplicabilityDeclaration` is the strict three-field human input (`defect_class`, `domain_applicability`, `implementation_support`) and rejects counts/availability; `ApplicabilityRow` is the derived output that adds `EvidenceCounts` and `evidence_availability`. Never round-trip a declaration through the output model.

Freeze the command arrays, `GitEnvironmentPolicy`, and all semantic mode strings from Task 5 as module constants. `FlareSearchSpec` rejects any differing command flag, environment policy, history/order/stop/message/diff/path/adjacency mode, malformed placeholder, non-ASCII diff regex, invalid regex, or wrong round ordering; execution derives its argv and child environment from the validated spec rather than maintaining a second hard-coded command. Source/head/count and the actual regex/glob lists remain data so the deterministic local DAG fixture can use its own source OIDs, while the committed-package test requires the exact Task 5 values.

`EvidenceCounts` is the only home for `proposed`, `qualified_candidate`, `accepted`, `rejected`, and `ambiguous`; `ApplicabilityRow.evidence_availability` is derived as `found` iff any count is nonzero. `AdjudicationEvidence.applicability_declarations` is typed as `tuple[ApplicabilityDeclaration, ...]`, never as rows. `EvidenceRef.kind` is the closed set `commit_message | patch_blob | lineage_link | source_artifact`; its `target_id` format is validated by kind. `EvidenceArtifact` binds an artifact ID, `issue | pull_request`, source URL, retrieval date, blob path, and blob SHA-256. `ReviewAttestation` binds `review_scope="complete_b0a_adjudication"`, `approval="approved"`, reviewer/revision, a nonempty written approval statement, candidate-universe hash, and the canonical adjudication payload hash. The adjudication-payload hash excludes only the attestation field itself; changing any decision, rationale, reference, artifact, source hash, or prior hash invalidates approval.

`canonical_bytes` must first turn a `BaseModel` into `model_dump(mode="json", exclude_none=True)`, then call the repository's `canonical_json` and append one newline; test both a mapping and an actual model because `canonical_json` does not serialize pydantic objects itself. Canonical JSON omits `None`, so the committed all-reachable search spec omits `after_exclusive` instead of physically storing JSON `null`. Implement `posix_glob_matches` as a memoized component matcher: ordinary components use `fnmatchcase`, while a `**` component recursively consumes either zero components or one path component and remains active. Do not use `PurePosixPath.match`, whose Python 3.12 `**` behavior does not recursively match the fixture and real Flare paths. Both evidence writers implement the trusted single-writer note above: leaf regular-file checks, complete and synced same-directory staging, `os.replace` publication in mapping order, prefix replay, and cleanup limited to the current call's unpublished staging. An uncatchable interruption may leave reserved hidden staging files or a complete published prefix, but never a partial final target. Ignore `.gameforge-*.tmp` repository-wide so residual staging files cannot enter the broad Task 5 `git add`. `put_blob` must hash raw bytes, verify an existing blob before reuse, and call `write_new_or_identical`.

- [ ] **Step 4: Run the focused tests to verify GREEN**

Run: `uv run pytest tests/bench/test_flare_evidence.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add gameforge/bench/flare_evidence.py tests/bench/conftest.py tests/bench/test_flare_evidence.py
git commit -m "feat(bench): define Flare B0A evidence contracts"
```

---

### Task 2: Read-only Git boundary and deterministic candidate discovery

**Files:**
- Create: `gameforge/bench/flare_git.py`
- Modify: `tests/bench/conftest.py`
- Create: `tests/bench/flare_git_fixture.py`
- Create: `tests/bench/test_flare_discover.py`

**Interfaces:**
- Produces: `class GitEvidenceError(RuntimeError)`
- Produces: `class ReadOnlyGitRepo`, with `resolve(oid)`, `reachable_commits(spec)`, `commit_facts(oid)`, `changed_paths(parent, oid)`, `patch_bytes(parent, oid)`, `eligible_patch_bytes(parent, oid, eligible_paths)`, and `stable_patch_id(patch)`
- Produces: `discover_candidates(repo: ReadOnlyGitRepo, spec: FlareSearchSpec, registration: SearchRegistration, round_name: Literal["initial", "expanded"], blob_dir: Path) -> DiscoveryLedger`
- Consumes: Task 1 models and storage primitives

- [ ] **Step 1: Add a deterministic local Git DAG fixture**

`tests/bench/flare_git_fixture.py` must initialize a repository with fixed author/committer identity and UTC timestamps and create an exact DAG:

1. a directly matching root config commit, used to lock the empty-tree diff base;
2. eight independent config-only fix groups across four proposed classes, with one group formed by the contiguous first-parent range `A -> B -> C`: A and C directly match frozen rules while B is a config-touching `adjacent_context` commit whose message/diff does not match;
3. one mixed config + Python change;
4. a config-only loot fix, then a side branch created from its parent and `git cherry-pick -x` of that exact source commit;
5. a manual backport carrying exactly `Backport-of: <40-lower-hex-source-oid>`, where one referenced reachable source is more than one first-parent edge from every direct match and does not itself match a search regex;
6. `git revert --no-edit` of the original loot fix, retaining exactly `This reverts commit <40-lower-hex-source-oid>.`;
7. one matching commit that changes only `mods/test/languages/readme.txt`, one matching commit that changes both a behavior config and that excluded localization path, one config diff accompanied by a non-UTF-8 binary sibling, and one neutral-subject mixed commit on a unique eligible path where only the engine sibling contains a diff-signature key and no adjacent direct anchor shares that path;
8. merge the side branch using `git merge --no-ff`, so every link endpoint is reachable from the pinned head and the selected merge-parent edge is recorded.

Return a dataclass containing the repository path and every named OID. Assert the source/target OIDs embedded by cherry-pick, backport, and revert are exact. `tests/bench/conftest.py` retains Task 1's independent registered-search fixtures and adds all shared fixtures (`flare_git_repo`, `search_registration`, both round search specs, discovery/evidence models, initial and expanded negative-gate path variants, and blob paths). It also exposes `foreign_initial_pair_factory`, which mutates exactly one named prior-ledger provenance field and refreshes the decision, expanded evidence prior hashes, and approval hash for the swapped-prior negative tests; it does not import Task 3 implementation code while Task 2 is being developed. Helper constructors used only by one test stay in that test module. All subprocess calls in the fixture use argument arrays and `shell=False`.

- [ ] **Step 2: Write failing discovery tests**

```python
# tests/bench/test_flare_discover.py
import os
import subprocess

import pytest

from gameforge.bench.flare_evidence import canonical_bytes, sha256_hex
from gameforge.bench.flare_git import GitEvidenceError, ReadOnlyGitRepo, discover_candidates


def test_discover_is_byte_stable_and_keeps_non_config_candidates_for_rejection(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    first = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "a" / "blobs"
    )
    second = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "b" / "blobs"
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    by_oid = {item.commit.commit_oid: item for item in first.discovered_candidates}
    assert by_oid[flare_git_repo.quest_fix].config_only is True
    assert by_oid[flare_git_repo.mixed_fix].config_only is False
    assert by_oid[flare_git_repo.mixed_fix].changed_paths == [
        "engine/runtime.py", "mods/core/quests/test.txt"
    ]
    assert flare_git_repo.localization_only not in by_oid
    assert by_oid[flare_git_repo.behavior_and_localization].config_only is False
    assert by_oid[flare_git_repo.non_utf8_binary_sibling].config_only is False
    assert flare_git_repo.engine_key_only not in by_oid
    if flare_git_repo.merge_commit in by_oid:
        assert "direct_match" not in {
            reason.kind for reason in by_oid[flare_git_repo.merge_commit].selection_reasons
        }


def test_patch_evidence_and_objective_lineage_are_offline_replayable(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    ledger = discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path), search_spec, search_registration,
        "expanded", tmp_path / "blobs"
    )
    for item in ledger.discovered_candidates:
        blob = tmp_path / item.diff_evidence.patch_blob
        assert blob.read_bytes()
        assert item.diff_evidence.patch_sha256 == sha256_hex(blob.read_bytes())
    link_types = {link.link_type for link in ledger.objective_lineage_links}
    assert {"patch_id", "cherry_pick", "backport", "revert"} <= link_types
    links = {(link.link_type, link.source_oid, link.target_oid) for link in (
        ledger.objective_lineage_links
    )}
    assert ("cherry_pick", flare_git_repo.loot_fix, flare_git_repo.loot_cherry_pick) in links
    assert ("backport", flare_git_repo.remote_backport_source, flare_git_repo.backport) in links
    assert ("revert", flare_git_repo.loot_fix, flare_git_repo.loot_revert) in links
    assert any(
        link.link_type == "patch_id"
        and {link.source_oid, link.target_oid}
        == {flare_git_repo.loot_fix, flare_git_repo.loot_cherry_pick}
        for link in ledger.objective_lineage_links
    )
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    merge = repo.commit_facts(flare_git_repo.merge_commit)
    assert merge.selected_parent_oid == merge.parent_oids[0]
    root = repo.commit_facts(flare_git_repo.root)
    assert root.diff_base_oid == flare_git_repo.empty_tree_oid


def test_direct_matches_expand_one_first_parent_edge_for_complete_grouping(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    ledger = discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path), search_spec, search_registration,
        "expanded", tmp_path / "blobs"
    )
    by_oid = {item.commit.commit_oid: item for item in ledger.discovered_candidates}
    assert by_oid[flare_git_repo.multicommit_a].selection_reasons[0].kind == "direct_match"
    assert by_oid[flare_git_repo.multicommit_b].selection_reasons[0].kind == "adjacent_context"
    assert by_oid[flare_git_repo.multicommit_c].selection_reasons[0].kind == "direct_match"
    assert by_oid[flare_git_repo.remote_backport_source].selection_reasons[0].kind == (
        "lineage_context"
    )


def test_expanded_round_is_a_superset_of_initial_under_union_semantics(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    initial = discover_candidates(
        repo, search_spec, search_registration, "initial", tmp_path / "initial" / "blobs"
    )
    expanded = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "expanded" / "blobs"
    )
    initial_oids = {item.commit.commit_oid for item in initial.discovered_candidates}
    expanded_oids = {item.commit.commit_oid for item in expanded.discovered_candidates}
    assert initial_oids <= expanded_oids


def test_discover_rejects_wrong_head_and_never_invokes_a_shell(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    calls = []
    real_run = subprocess.run

    def guarded_run(args, **kwargs):
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    with pytest.raises(GitEvidenceError, match="pinned head"):
        discover_candidates(
            ReadOnlyGitRepo(flare_git_repo.path),
            search_spec.model_copy(update={"pinned_head": "f" * 40}),
            search_registration,
            "initial",
            tmp_path / "blobs",
        )
    assert calls


def test_successful_discovery_uses_only_argument_arrays(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    calls = []
    real_run = subprocess.run

    def guarded_run(args, **kwargs):
        assert isinstance(args, list)
        assert kwargs.get("shell", False) is False
        calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path), search_spec, search_registration,
        "expanded", tmp_path / "blobs"
    )
    assert calls


def test_repository_git_config_and_locale_cannot_change_patch_bytes(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    repo = ReadOnlyGitRepo(flare_git_repo.path)
    clean = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "clean" / "blobs"
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "color.ui", "always"], check=True
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.noprefix", "true"], check=True
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.algorithm", "histogram"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.interHunkContext", "100"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.suppressBlankEmpty", "true"],
        check=True,
    )
    order_file = tmp_path / "reverse.order"
    order_file.write_text("mods/core/quests/test.txt\n*\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "diff.orderFile", str(order_file)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(flare_git_repo.path), "config", "i18n.logOutputEncoding", "ISO-8859-1"],
        check=True,
    )
    attributes_file = tmp_path / "global.attributes"
    attributes_file.write_text("*.txt -diff\n", encoding="utf-8")
    subprocess.run(
        [
            "git", "-C", str(flare_git_repo.path), "config",
            "core.attributesFile", str(attributes_file),
        ],
        check=True,
    )
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=99")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    polluted = discover_candidates(
        repo, search_spec, search_registration, "expanded", tmp_path / "polluted" / "blobs"
    )
    assert canonical_bytes(clean) == canonical_bytes(polluted)


def test_git_child_environment_is_minimal_and_drops_inherited_git_overrides(
    flare_git_repo, search_spec, search_registration, tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=99")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    real_run = subprocess.run
    child_environments = []

    def guarded_run(args, **kwargs):
        child_environments.append(kwargs["env"])
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path), search_spec, search_registration,
        "initial", tmp_path / "blobs"
    )
    expected_keys = {"PATH"} | set(search_spec.git_environment_policy.fixed)
    assert child_environments
    assert all(set(env) == expected_keys for env in child_environments)
    assert all(env["PATH"] == os.environ["PATH"] for env in child_environments)
    assert all("GIT_DIFF_OPTS" not in env for env in child_environments)
    assert all("GIT_CONFIG_COUNT" not in env for env in child_environments)
    assert all("GIT_CONFIG_KEY_0" not in env for env in child_environments)
    assert all("GIT_CONFIG_VALUE_0" not in env for env in child_environments)


def test_repo_local_info_attributes_are_rejected(
    flare_git_repo, search_spec, search_registration, tmp_path
):
    info_attributes = flare_git_repo.git_dir / "info" / "attributes"
    info_attributes.write_text("*.txt -diff\n", encoding="utf-8")
    with pytest.raises(GitEvidenceError, match="info/attributes"):
        discover_candidates(
            ReadOnlyGitRepo(flare_git_repo.path), search_spec, search_registration,
            "initial", tmp_path / "blobs"
        )
```

- [ ] **Step 3: Run the tests to verify RED**

Run: `uv run pytest tests/bench/test_flare_discover.py -q`

Expected: collection fails because `gameforge.bench.flare_git` does not exist.

- [ ] **Step 4: Implement discovery without taxonomy inference**

Use these exact Git semantics:

Resolve a normal clone or linked worktree to its Git directory before executing evidence commands; leave a bare repository unchanged. Every call begins with `["git", "--no-optional-locks", "--no-replace-objects", "-c", "color.ui=false", "-c", "core.attributesFile=/dev/null", "-c", "core.quotePath=true", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.renames=false", "-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false", "-c", "diff.interHunkContext=0", "-c", "diff.suppressBlankEmpty=false", "-c", "diff.orderFile=/dev/null", "-C", str(repo.git_dir)]`. Preserve the caller's unmodified input path separately for errors. Construct every subprocess environment from the frozen `GitEnvironmentPolicy`, never from `os.environ.copy()`: inherit exactly `PATH`, discard every inherited name beginning with `GIT_`, then apply the fixed values `LC_ALL=C`, `LANG=C`, `TZ=UTC`, `GIT_OPTIONAL_LOCKS=0`, `GIT_NO_REPLACE_OBJECTS=1`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, and `GIT_ATTR_NOSYSTEM=1`. A missing inherited `PATH` is a `GitEvidenceError`. This explicitly prevents `GIT_DIFF_OPTS` and the `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` injection channel from reaching Git. Once per discovery, before its first Git object query, reject a nonempty repo-local `$GIT_DIR/info/attributes`, effective partial-clone/promisor configuration, or a `pack/*.promisor` marker. The trusted local repository is cooperative and static for that discovery; do not re-run the preflight on every Git command, and allow ordinary local alternates or symlinked object-store directories.

- resolve head: common prefix plus `["rev-parse", "--verify", f"{spec.pinned_head}^{{commit}}"]`;
- history from root: common prefix plus `["rev-list", "--topo-order", "--reverse", spec.pinned_head]`;
- bounded history: the same array with `f"{spec.after_exclusive}..{spec.pinned_head}"` as the final argument;
- metadata: common prefix plus `["show", "-s", "--no-show-signature", "--encoding=UTF-8", "--format=%H%x00%P%x00%ct%x00%s%x00%B", oid]`;
- paths: common prefix plus `["diff-tree", "--no-commit-id", "--name-status", "--no-renames", "-r", "-z", parent, oid]`;
- patch: common prefix plus `["diff", "--binary", "--full-index", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3", "--inter-hunk-context=0", "--diff-algorithm=myers", "--no-indent-heuristic", "--submodule=short", "--ignore-submodules=none", parent, oid]`;
- patch ID: pass patch bytes on stdin to `git patch-id --stable`.

Validate that the reachable list has exactly `spec.expected_revision_count` entries. Decode both the frozen `%s` search subject and full `%B` evidence message as UTF-8 with strict errors. Keep patch bytes raw and record exact `git --version`, the literal harness version `gameforge-flare-discovery@1`, `platform.python_implementation()`, `platform.python_version()`, the two-field `platform.python_build()`, and `unicodedata.unidata_version`; every field binds expanded prior-search equality. For a root commit, derive Git's SHA-1 empty-tree OID with `git hash-object -t tree --stdin` without `-w` and diff against it. For a merge, compare the commit to parent 1 and record that selected edge plus all parent OIDs. Normalize paths to relative POSIX paths and reject absolute paths, `..`, NUL, or exact duplicate paths. Case-distinct Git paths remain distinct because B0A reads Git objects without checking out the tree. Use Task 1's slash-aware `posix_glob_matches`; a candidate is config-only only when every changed path is allowlisted and none is excluded.

A direct match has at least one allowlisted, non-excluded changed path and matches any subject or diff regex in the selected round union. Subject regexes operate on strict UTF-8 `%s`. Diff rules never directly select a multi-parent merge commit; merged branch commits remain independently reachable and searchable. For a single-parent/root commit, diff rules search only the raw patch for the sorted eligible paths, produced by the same frozen patch arguments followed by `--` and those literal paths; a key changed only in an engine/localization sibling cannot select the commit. Validate that every diff regex is ASCII, encode it once, and compile a `bytes` pattern; never decode a patch merely to search it. Starting only from direct matches, traverse exactly one nonrecursive first-parent edge backward and one first-parent-child edge forward; include a neighboring commit only when it shares at least one exact eligible path with that direct anchor. Then parse the frozen trailer grammars from the full `%B` evidence message and include every explicitly referenced source OID that is reachable from the pinned head as `lineage_context`, repeating to a fixed point; an unreachable source is an error. Retain mixed direct or context commits for auditable rejection. Deduplicate the union and record every selection reason/rule ID. Freeze objective trailer grammars as `(?m)^\\(cherry picked from commit ([0-9a-f]{40})\\)$`, `(?m)^Backport-of: ([0-9a-f]{40})$`, and `(?m)^This reverts commit ([0-9a-f]{40})\\.$`.

Emit exactly one `DiscoveredCandidate` per unique commit OID, sorted by `(committed_at, commit_oid)`, plus objective links with stable `link_id` sorted by their complete tuple. `DiscoveryLedger` embeds the complete `FlareSearchSpec` as `search_frame` and records `search_spec_sha256`, `search_registration {project_commit_oid, repo_relative_path}`, `observed_revision_count`, `search_round`, `discovery_tool {tool_version, project_commit_oid, git_version, python_implementation, python_version, python_build, unicode_version}`, and `candidate_universe_sha256`. The discovery tool version is the exact registered literal, and its `project_commit_oid` must equal `search_registration.project_commit_oid`. The universe hash domain is canonical `{schema_version, search_spec_sha256, search_round, discovered_candidates, objective_lineage_links}` only; it excludes the hash field itself and all machine-local filesystem paths. Revalidation recomputes exact eligible paths and `config_only`, enforces unique commit OIDs, exact path uniqueness and the root empty-tree base, rebuilds each semantic link ID, and validates reason order/uniqueness plus every direct/adjacent/lineage cross-reference. For each selected round, schema validation recomputes all subject-regex rule IDs and requires the recorded message portion of its direct reason to match exactly; rules from distinct rounds cannot be combined. Diff-regex exactness is CAS-backed: offline replay reads the full patch blob, extracts precisely the eligible changed-path blocks under the frozen patch format, recomputes every diff rule ID, and requires the complete per-round direct reasons to equal the recorded reasons. Frozen trailer captures and recorded trailer links must be complete in both directions: every capture in a target's full `%B` message produces the same typed/source/target/rule link, and every such link is supported by that capture plus the exact source-side `lineage_context` reason. A lineage selection reason may not use a `patch_id` link. Starting from candidates with direct or valid adjacent reasons, the validator repeats target-to-source trailer traversal to a fixed point and requires it to cover the complete candidate universe, rejecting disconnected lineage-only components. All link endpoints and frozen trailer rule IDs/types must agree with the embedded search frame. A separate provenance test verifies that the registration commit contains the same canonical spec and predates every result file.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run: `uv run pytest tests/bench/test_flare_evidence.py tests/bench/test_flare_discover.py -q`

Expected: all tests pass twice with identical candidate JSON and blob hashes.

- [ ] **Step 6: Commit**

```bash
git add gameforge/bench/flare_git.py tests/bench/conftest.py tests/bench/flare_git_fixture.py tests/bench/test_flare_discover.py
git commit -m "feat(bench): add deterministic Flare candidate discovery"
```

---

### Task 3: Offline adjudication, independent groups, and provisional gate

**Files:**
- Modify: `gameforge/bench/flare_evidence.py`
- Create: `gameforge/bench/flare_adjudication.py`
- Modify: `tests/bench/conftest.py`
- Create: `tests/bench/test_flare_adjudication.py`

**Interfaces:**
- Produces: `class AdjudicationError(ValueError)`
- Produces: `adjudicate(discovered: DiscoveryLedger, evidence: AdjudicationEvidence, prior_discovery: DiscoveryLedger | None = None, prior_evidence: AdjudicationEvidence | None = None, prior_ledger: CandidateLedger | None = None, prior_decision: B0ADecision | None = None) -> tuple[CandidateLedger, B0ADecision]`
- Produces: `derive_applicability_matrix(groups: Sequence[CandidateFixGroup], declared: Sequence[ApplicabilityDeclaration]) -> tuple[ApplicabilityRow, ...]`
- Produces: `evaluate_provisional_gate(groups: Sequence[CandidateFixGroup], matrix: Sequence[ApplicabilityRow], search_round: str) -> GateSummary`
- Consumes: Task 1 contracts and Task 2 immutable discovery facts

**Post-Task-2 audit clarification:** The approved expanded-round rule requires every initial
group decision to remain byte-equivalent, but the Task 1 ledger retained only aggregate
adjudicator/reviewer sets and dropped per-group root-cause references and identity assignment.
Add `group_decision_sha256: Sha256` to `CandidateFixGroup`, derived only by `adjudicate` as
`sha256_hex(canonical_bytes(the complete CandidateGroupDecision))`. Do not sort any decision
arrays before hashing and do not accept this digest from evidence. Both evidence group decisions
and ledger groups require globally unique `fix_group_id` values. In an expanded ledger, the
initial group IDs must be an ordered prefix, each prefix digest must equal the corresponding
prior group digest, and new groups may appear only as a suffix. Standalone gate evaluation counts
distinct group IDs and must not allow duplicates to inflate the threshold. This is an audit
commitment only; it does not change any valid gate count or outcome.

The test sketches below are behavioral examples, not permission to create internally stale
Pydantic copies. Every mutation intended to reach adjudication semantics must refresh the review
attestation and keep unrelated fields valid. Commit-range tests use real fixture OIDs and matching
`selected_parent_edges`; tests locate the multicommit group by `fix_group_id` rather than assuming
it is at index zero. The dedicated candidate-exclusions fixture must include a reviewed
`non_bug/rejected` decision (use the merge candidate), in addition to `non_config_only` and
`revert_or_duplicate`. A root group's first edge uses its discovered empty-tree `diff_base_oid`.

- [ ] **Step 1: Write failing adjudication and gate tests**

```python
# tests/bench/test_flare_adjudication.py
import pytest

from gameforge.bench.flare_evidence import canonical_bytes
from gameforge.bench.flare_adjudication import (
    AdjudicationError,
    adjudicate,
    evaluate_provisional_gate,
)


def test_adjudication_groups_contiguous_first_parent_commits_and_counts_groups_not_commits(
    discovered_ledger, positive_evidence
):
    ledger, decision = adjudicate(discovered_ledger, positive_evidence)
    assert decision.gate.status == "provisional_pass"
    assert decision.gate.proposed_groups == 8
    assert decision.gate.proposed_classes == 4
    assert all(group.config_only for group in ledger.groups if group.counts_toward_gate)


def test_non_contiguous_group_is_rejected(
    discovered_ledger, positive_evidence, flare_git_repo
):
    bad = replace_group_commits(
        positive_evidence,
        fix_group_id="group-multicommit",
        commits=[flare_git_repo.multicommit_a, flare_git_repo.multicommit_c],
        selected_parent_edges=real_selected_edges(
            discovered_ledger,
            [flare_git_repo.multicommit_a, flare_git_repo.multicommit_c],
        ),
    )
    with pytest.raises(AdjudicationError, match="complete first-parent range"):
        adjudicate(discovered_ledger, bad)


def test_missing_or_wrong_selected_merge_parent_is_rejected(
    discovered_ledger, evidence_with_merge_group, flare_git_repo
):
    merge = next(
        item.commit for item in discovered_ledger.discovered_candidates
        if item.commit.commit_oid == flare_git_repo.merge_commit
    )
    assert len(merge.parent_oids) > 1
    bad = replace_selected_parent(
        evidence_with_merge_group, commit_oid=flare_git_repo.merge_commit,
        parent_oid=merge.parent_oids[1],
    )
    with pytest.raises(AdjudicationError, match="first parent|selected parent"):
        adjudicate(discovered_ledger, bad)


def test_contiguous_context_commit_is_required_for_a_multicommit_group(
    discovered_ledger, evidence_with_multicommit_group, flare_git_repo
):
    ledger, _ = adjudicate(discovered_ledger, evidence_with_multicommit_group)
    group = next(
        item for item in ledger.groups if item.fix_group_id == "group-multicommit"
    )
    assert group.commits == [
        flare_git_repo.multicommit_a,
        flare_git_repo.multicommit_b,
        flare_git_repo.multicommit_c,
    ]
    assert group.before_commit == flare_git_repo.before_multicommit
    assert group.after_commit == flare_git_repo.multicommit_c
    assert group.after_committed_at > 0
    assert group.changed_paths == sorted(group.changed_paths)
    assert [item.commit_oid for item in group.diff_evidence] == (
        group.commits
    )
    incomplete = replace_group_commits(
        evidence_with_multicommit_group,
        fix_group_id="group-multicommit",
        commits=[flare_git_repo.multicommit_a, flare_git_repo.multicommit_c],
        selected_parent_edges=real_selected_edges(
            discovered_ledger,
            [flare_git_repo.multicommit_a, flare_git_repo.multicommit_c],
        ),
    )
    with pytest.raises(AdjudicationError, match="complete first-parent range"):
        adjudicate(discovered_ledger, incomplete)


def test_multilabel_group_uses_case_dispositions_not_group_summary(
    discovered_ledger, multilabel_evidence
):
    ledger, _ = adjudicate(discovered_ledger, multilabel_evidence)
    group = ledger.groups[0]
    assert {case.disposition for case in group.cases} == {"proposed", "rejected"}
    assert group.disposition_summary == "proposed"


def test_non_bug_mixed_and_revert_candidates_are_structured_without_a_class(
    discovered_ledger, evidence_with_candidate_exclusions
):
    ledger, _ = adjudicate(discovered_ledger, evidence_with_candidate_exclusions)
    reasons = {item.reason_code for item in ledger.candidate_decisions}
    assert {"non_bug", "non_config_only", "revert_or_duplicate"} <= reasons
    grouped = {oid for group in ledger.groups for oid in group.commits}
    excluded = {item.commit_oid for item in ledger.candidate_decisions}
    universe = {item.commit.commit_oid for item in discovered_ledger.discovered_candidates}
    assert grouped.isdisjoint(excluded)
    assert grouped | excluded == universe


def test_matrix_is_exact_and_fixed_flare_applicability_is_enforced(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    rows = {row.defect_class: row for row in ledger.applicability_matrix}
    assert len(rows) == 11
    assert rows["prob_sum_ne_1"].domain_applicability == "not_applicable"
    assert rows["gacha_expectation_violation"].domain_applicability == "not_applicable"
    assert rows["economy_collapse"].domain_applicability == "applicable"
    assert all(
        row.evidence_counts.qualified_candidate == row.evidence_counts.accepted == 0
        for row in rows.values()
    )


def test_not_applicable_class_cannot_be_proposed(
    discovered_ledger, evidence_proposing_prob_sum
):
    with pytest.raises(AdjudicationError, match="not_applicable"):
        adjudicate(discovered_ledger, evidence_proposing_prob_sum)


def test_dangling_evidence_reference_or_stale_review_hash_is_rejected(
    discovered_ledger, positive_evidence
):
    dangling = replace_first_evidence_ref(positive_evidence, "f" * 64)
    with pytest.raises(AdjudicationError, match="evidence ref"):
        adjudicate(discovered_ledger, dangling)
    stale = replace_reviewed_payload_hash(positive_evidence, "0" * 64)
    with pytest.raises(AdjudicationError, match="attestation"):
        adjudicate(discovered_ledger, stale)


@pytest.mark.parametrize(
    ("groups", "classes", "status"),
    [(7, 4, "insufficient_evidence"), (8, 3, "insufficient_evidence"),
     (8, 4, "provisional_pass")],
)
def test_gate_boundaries(groups, classes, status):
    assert evaluate_provisional_gate(
        make_groups(groups, classes), complete_matrix(), "expanded"
    ).status == status


def test_initial_failure_requires_the_prefrozen_expanded_round():
    gate = evaluate_provisional_gate(make_groups(7, 4), complete_matrix(), "initial")
    assert gate.status == "expanded_round_required"
    assert gate.next_action == "run_expanded_round"


def test_expanded_round_cannot_relabel_or_regroup_initial_candidates(
    expanded_discovery, expanded_evidence, initial_prior_artifacts
):
    changed = replace_group_rationale(
        expanded_evidence,
        fix_group_id=expanded_evidence.group_decisions[0].fix_group_id,
        rationale="changed after seeing the initial gate",
    )
    with pytest.raises(AdjudicationError, match="initial decision"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


def test_expanded_round_can_only_append_new_top_level_lineage_resolutions(
    expanded_discovery, expanded_evidence, initial_ledger, initial_prior_artifacts
):
    ledger, _ = adjudicate(
        expanded_discovery, expanded_evidence, *initial_prior_artifacts
    )
    initial = [canonical_bytes(item) for item in initial_ledger.lineage_resolutions]
    expanded = [canonical_bytes(item) for item in ledger.lineage_resolutions]
    assert expanded[:len(initial)] == initial
    assert len(expanded) > len(initial)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_candidate_decisions_are_an_unchanged_ordered_prefix(
    mutation, expanded_discovery, expanded_evidence, initial_ledger,
    initial_prior_artifacts,
):
    changed = mutate_initial_candidate_decisions(
        expanded_evidence, initial_ledger, mutation
    )
    with pytest.raises(AdjudicationError, match="initial decision|ordered prefix"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_lineage_resolutions_are_an_unchanged_ordered_prefix(
    mutation, expanded_discovery, expanded_evidence, initial_ledger,
    initial_prior_artifacts,
):
    changed = mutate_initial_lineage_resolutions(
        expanded_evidence, initial_ledger, mutation
    )
    with pytest.raises(AdjudicationError, match="lineage resolution|ordered prefix"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


@pytest.mark.parametrize(
    "binding_field",
    [
        "search_frame",
        "search_spec_sha256",
        "search_registration",
        "observed_revision_count",
        "discovery_tool",
    ],
)
def test_expanded_prior_must_match_each_registered_search_binding_field(
    binding_field, expanded_discovery, expanded_evidence,
    foreign_initial_pair_factory, initial_discovery, initial_insufficient_evidence,
):
    # The factory changes only the named ledger binding, then refreshes the
    # decision, both evidence prior hashes, and the complete approval hash.
    foreign_initial_ledger, foreign_initial_decision, rebound_evidence = (
        foreign_initial_pair_factory(binding_field, expanded_evidence)
    )
    with pytest.raises(AdjudicationError, match="replay|same registered search"):
        adjudicate(
            expanded_discovery, rebound_evidence,
            initial_discovery, initial_insufficient_evidence,
            foreign_initial_ledger, foreign_initial_decision,
        )
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `uv run pytest tests/bench/test_flare_adjudication.py -q`

Expected: collection fails because `gameforge.bench.flare_adjudication` does not exist.

- [ ] **Step 3: Implement evidence validation and derived decisions**

`AdjudicationEvidence` has these required top-level fields:

```text
schema_version
evidence_revision
search_round: initial | expanded
discovery_ledger_sha256
candidate_universe_sha256
prior_candidate_ledger_sha256?  # required together for expanded, forbidden for initial
prior_decision_sha256?
source_artifacts[]               # optional offline issue/PR CAS records
applicability_declarations[]     # exact 11 class/domain/support rows; no counts
group_decisions[]
candidate_decisions[]
lineage_resolutions[]            # top-level and keyed by stable objective link_id
review_attestation
```

The approval payload is every `AdjudicationEvidence` field except `review_attestation`; its canonical SHA-256 must equal `review_attestation.reviewed_payload_sha256`. `applicability_declarations` is exactly 11 strict `ApplicabilityDeclaration` values containing only the `defect_class`, `domain_applicability`, and `implementation_support` triples; evidence availability and counts are absent because adjudication derives them from cases. The attestation also repeats the candidate-universe hash, declares `approval="approved"`, and identifies the human reviewer and review revision. All group/candidate adjudicator IDs must differ from the reviewer ID. Expanded evidence must identify the exact prior candidate-ledger and decision SHA-256 values. Each group decision includes:

```text
fix_group_id
commits[]
selected_parent_edges[]
root_cause_evidence_refs[]
case_decisions[]       # class, proposed|rejected|ambiguous, rationale, evidence_refs
adjudicator_id
reviewer_id
rationale
```

Top-level `lineage_resolutions[]` resolves each discovered objective link as `same_group | separate`, with rationale and `affected_group_ids` equal to the sorted exact set of groups containing the two endpoints: zero when both endpoints have candidate dispositions, one when only one endpoint is grouped or both share a group, and two when they occupy distinct groups. Omissions, extras, duplicates, and nondeterministic order are invalid. Existing resolutions are append-only across rounds; lifting them out of group decisions allows a new expanded candidate to link to an initial candidate without rewriting the initial group. The evidence file also has `candidate_decisions[]` entries with commit OID,
`rejected | ambiguous`, a closed `reason_code`, rationale, evidence refs, adjudicator, and
reviewer. These entries intentionally have no defect class.

The closed reason codes are `non_bug | out_of_taxonomy | non_config_only | indeterminate_oracle | revert_or_duplicate | insufficient_context`. `indeterminate_oracle` and `insufficient_context` require `ambiguous`; the other four require `rejected`. Any non-config-only candidate uses `non_config_only`, including a cherry-pick, backport, or revert target. Only a config-only non-primary lineage target uses `revert_or_duplicate`; all such mixed targets remain candidate-level and never count.

Reject evidence when its source/candidate/prior hash is wrong; approval is stale; an evidence ref does not resolve; a commit is unknown or assigned twice; a grouped range omits an intervening first-parent commit; a selected merge parent differs from discovery's exact `selected_parent_oid`; or any commit in a proposed group is not config-only. Membership in `parent_oids` is insufficient because B0A grouping is first-parent continuous. Every discovered candidate must appear in exactly one group or one `CandidateDisposition`; the two sets must be disjoint and their union must equal the candidate universe. Use a candidate disposition, without a fake defect class, for non-bugs, out-of-taxonomy changes, non-config-only commits, indeterminate oracles, duplicate/backport/revert evidence, and insufficient context. Matrix rejected/ambiguous counts include only typed cases; untyped exclusions are counted by `reason_code` in the gate summary. During expanded adjudication, every initial group and candidate-level decision must be byte-equivalent after canonical projection, including group ID, commits, cases, dispositions, labels, rationale, reviewer/adjudicator, and evidence refs. `CandidateFixGroup.group_decision_sha256` binds the complete reviewed group projection. Initial group IDs and initial candidate-decision commit IDs must remain ordered prefixes of their expanded sequences; new decisions may only be appended. Existing top-level lineage resolutions must also be byte-equivalent; expanded evidence may append new groups, candidate decisions, and resolutions only for newly materialized links, but may not relabel, regroup, reorder, insert before, or delete an initial candidate. Reject duplicate group IDs before building any mapping or evaluating the gate.

Expanded adjudication requires all four prior artifacts: raw initial discovery, raw initial evidence, derived candidate ledger, and decision. Initial adjudication forbids every prior artifact. The prior raw pair is replayed through initial adjudication, and both replayed outputs must be byte-identical to the supplied derived pair before any expanded-prefix check. The prior ledger must be `search_round="initial"`; its decision must point to the full canonical prior-ledger bytes and have `expanded_round_required`; and the replayed ledger's schema version, complete `search_frame`, `search_spec_sha256`, `search_registration`, `observed_revision_count`, and complete runtime-bearing `discovery_tool {tool_version, project_commit_oid, git_version, python_implementation, python_version, python_build, unicode_version}` must equal the corresponding binding fields in the expanded discovery. Both expanded-evidence prior hashes must match that exact derived pair. The expanded evidence must retain the complete initial `source_artifacts` sequence as a canonical ordered prefix and preserve initial applicability declarations byte-for-byte. Each initial candidate retains immutable commit/path/config/diff facts, every initial selection reason, and every initial objective link; only new expanded-round reasons, candidates, and links may be added. Every added direct reason uses only expanded-round rule IDs, an added adjacent reason is anchored by an expanded-round direct match, an added lineage reason references a new link, and every new link touches at least one newly discovered candidate. Combined with exact trailer-message/reason validation and rooted fixed-point closure, these prefix rules prevent an expanded-only unrooted lineage component.

Derive every `CandidateFixGroup` entirely from discovery facts and reviewed decisions: `group_decision_sha256` hashes the complete canonical reviewed group decision; `before_commit` is the selected diff base of the first commit (its first parent, or the empty-tree OID for a root); `after_commit`/`after_committed_at` come from the final commit; `changed_paths` is the sorted union; `config_only` is the conjunction; and `diff_evidence[]` preserves the ordered per-commit patch/message records. `lineage_links[]` contains the resolved stable link IDs touching the group. No combined patch is synthesized during offline adjudication and no group field may require reopening the Flare clone.

Objective lineage cannot inflate independence. `cherry_pick` and `backport` links must resolve as the same fix, with at most one endpoint in a counted group. A config-only non-primary endpoint is excluded as `revert_or_duplicate` unless a continuous group can contain it; a non-config-only endpoint instead uses the higher-priority `non_config_only`. A revert endpoint is always uncounted under the same precedence. Only a raw `patch_id` collision may resolve as independent, and only with nonempty root-cause evidence showing distinct fixes. Add negative tests that try to count both cherry-pick/backport endpoints or a revert and assert the gate rejects them.

Derive group disposition with priority `ambiguous`, then `proposed`, then `rejected`, and keep mixed case-level outcomes. Enforce globally unique `case_id` values across the evidence and at most one case per defect class within a group, matching the B0A unit `(fix_group_id, defect_class)`. A group counts toward the gate only when it is config-only and contains at least one proposed case. Reject proposed cases on the two N/A rows. Count classes from domain-applicable proposed cases and groups from distinct `fix_group_id`. Derive evidence counts and availability from cases, then validate the declared domain and implementation axes. For an initial-round failure set `status="expanded_round_required"` and `next_action="run_expanded_round"`; for an expanded failure set `status="insufficient_evidence"` and `next_action="stop_flare_heavy_investment"`.

`CandidateLedger` repeats the full `search_frame`, search-spec registration, search round, `observed_revision_count`, complete `discovery_tool`, discovery-ledger/candidate-universe/adjudication-evidence hashes, evidence revision, prior ledger/decision hashes when expanded, and derived adjudicator/reviewer identities, then contains groups, candidate decisions, applicability matrix, gate summary, and top-level lineage resolutions. Its `adjudication_evidence_sha256` hashes the full canonical evidence including the attestation. `B0ADecision` is deliberately minimal: schema version, full canonical candidate-ledger SHA-256, and a gate summary byte-equivalent to the ledger's gate. There are no self-hash fields and no redundant transitive hash copies: each downstream object binds the complete canonical upstream bytes. Tests must mutate one provenance field at a time and prove replay rejects the tampered chain.

All helper constructors referenced by this test (`make_groups`, `complete_matrix`, evidence replacement helpers) are defined before the tests in the same module. Every replacement helper rebuilds a fully valid `AdjudicationEvidence` and refreshes the approval-payload hash; a targeted negative test must leave only its intended semantic violation. Reusable pydantic fixtures live in `tests/bench/conftest.py`; `foreign_initial_pair_factory` changes exactly one requested prior-ledger binding field, recomputes `B0ADecision.candidate_ledger_sha256`, replaces both expanded-evidence prior hashes, and refreshes the evidence approval-payload hash/attestation so every other provenance check is valid. Add explicit regression tests for the exact group-decision digest, changed initial root-cause refs, swapped per-group adjudicator assignments, duplicate group IDs, initial-group reorder/insertion, duplicate IDs not inflating the gate, candidate-decision change/reorder/prepend, and lineage-resolution change/reorder/prepend. Positive expanded replay compares the complete canonical initial prefixes, not only ID sets. The stated GREEN run cannot rely on undeclared pytest fixtures.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest tests/bench/test_flare_evidence.py tests/bench/test_flare_discover.py tests/bench/test_flare_adjudication.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add gameforge/bench/flare_evidence.py gameforge/bench/flare_adjudication.py \
  tests/bench/conftest.py tests/bench/test_flare_adjudication.py
git commit -m "feat(bench): add Flare B0A adjudication gate"
```

---

### Task 4: Mining CLI, exit codes, and deterministic end-to-end fixture

**Files:**
- Create: `gameforge/bench/flare_mining.py`
- Create: `tests/bench/test_flare_mining_cli.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int`
- CLI: `python -m gameforge.bench.flare_mining discover --repo REPO --search-spec SEARCH_SPEC --registration-commit OID --registration-path REPO_RELATIVE_PATH --round initial|expanded --out DISCOVERY_LEDGER --blob-dir BLOB_DIR`
- CLI: `python -m gameforge.bench.flare_mining adjudicate --ledger DISCOVERY_LEDGER --evidence EVIDENCE --blob-dir BLOB_DIR [--prior-discovery PRIOR_DISCOVERY --prior-evidence PRIOR_EVIDENCE --prior-ledger PRIOR_LEDGER --prior-decision PRIOR_DECISION] --out CANDIDATE_LEDGER --decision-out DECISION`
- Exit codes: `0` for successful discovery or a valid `provisional_pass`, `3` for a valid `expanded_round_required` or `insufficient_evidence`, and `1` for validated domain/tool failures; argparse keeps its standard `SystemExit(2)` for invalid syntax

**Post-Task-3 preflight clarification:** Canonical-input tests cover every JSON input, not only
the discovery ledger and evidence: add a noncanonical discover search spec, expanded prior ledger
and prior decision, plus at least one invalid UTF-8 input. Parameterize both lone-prior-flag syntax
errors and assert every exit-1 input failure leaves both outputs absent. CAS replay covers both
discovery patch blobs and `EvidenceArtifact` source blobs; resolve either as `blob_dir / digest`,
not by appending the recorded `blobs/{digest}` metadata path to `blob_dir`. Build a Task-4-local
approved evidence value with a resolving `source_artifact` ref and test missing and tampered bytes.

Every valid exit-3 test verifies the full raw canonical chain: discovery-ledger hash, full
adjudication-evidence hash, candidate-ledger hash in the decision, gate equality, and for expanded
rounds both prior hashes. The expanded negative path first publishes its initial negative
ledger/decision through this CLI, then consumes those actual files together with their raw initial
discovery/evidence as the four prior artifacts. Repeat
adjudication into an independent output root and compare complete ledger/decision bytes. Add a
literal-equal output path case, a discover immutable-output conflict, and a representative
one-line stderr/no-traceback assertion.

Registration provenance remains outside this runtime CLI: `--repo` is the upstream Flare clone,
while the registration commit belongs to GameForge history. `discover` constructs and records a
strict `SearchRegistration` but must not resolve that commit in `--repo` or restore a production
provenance helper. The deterministic E2E test asserts exact registration commit/path round-trip.
These clarifications add coverage only and do not change any valid outcome or the 0/1/2/3 mapping.

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/bench/test_flare_mining_cli.py
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gameforge.bench.flare_evidence import (
    AdjudicationEvidence,
    B0ADecision,
    CandidateLedger,
    DiscoveryLedger,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.flare_mining import main


def assert_complete_chain(
    ledger_path, decision_path, discovered_path, evidence_path,
    prior_ledger_path=None, prior_decision_path=None,
):
    ledger_bytes = ledger_path.read_bytes()
    decision_bytes = decision_path.read_bytes()
    ledger = CandidateLedger.model_validate_json(ledger_bytes)
    decision = B0ADecision.model_validate_json(decision_bytes)
    assert ledger_bytes == canonical_bytes(ledger)
    assert decision_bytes == canonical_bytes(decision)
    assert ledger.discovery_ledger_sha256 == sha256_hex(discovered_path.read_bytes())
    assert ledger.adjudication_evidence_sha256 == sha256_hex(evidence_path.read_bytes())
    assert decision.candidate_ledger_sha256 == sha256_hex(ledger_bytes)
    assert decision.gate == ledger.gate_summary
    if prior_ledger_path is None:
        assert prior_decision_path is None
        assert ledger.prior_candidate_ledger_sha256 is None
        assert ledger.prior_decision_sha256 is None
    else:
        assert ledger.prior_candidate_ledger_sha256 == sha256_hex(
            prior_ledger_path.read_bytes()
        )
        assert ledger.prior_decision_sha256 == sha256_hex(
            prior_decision_path.read_bytes()
        )
    return ledger, decision


def approved_evidence_with_source_artifact(base, artifact_bytes):
    digest = sha256_hex(artifact_bytes)
    payload = base.model_dump(
        mode="json", exclude={"review_attestation"}, exclude_none=True
    )
    artifact_id = "flare-issue-source-1"
    payload["source_artifacts"] = [{
        "artifact_id": artifact_id,
        "artifact_type": "issue",
        "source_url": "https://github.com/flareteam/flare-game/issues/1",
        "retrieval_date": "2026-07-10",
        "blob_path": f"blobs/{digest}",
        "blob_sha256": digest,
    }]
    payload["group_decisions"][0]["root_cause_evidence_refs"].append({
        "kind": "source_artifact", "target_id": artifact_id,
    })
    attestation = base.review_attestation.model_dump(mode="json", exclude_none=True)
    attestation["reviewed_payload_sha256"] = sha256_hex(canonical_bytes(payload))
    payload["review_attestation"] = attestation
    return AdjudicationEvidence.model_validate(payload), digest


def test_discover_then_adjudicate_is_byte_deterministic(
    flare_git_repo, search_spec_path, initial_positive_evidence_path, tmp_path
):
    discovered = tmp_path / "candidate-ledger.discovered.json"
    blobs = tmp_path / "blobs"
    assert main([
        "discover", "--repo", str(flare_git_repo.path),
        "--search-spec", str(search_spec_path),
        "--registration-commit", "a" * 40,
        "--registration-path", "scenarios/flare_corpus/search-spec.json",
        "--round", "initial",
        "--out", str(discovered), "--blob-dir", str(blobs),
    ]) == 0
    first = discovered.read_bytes()
    discovery_model = DiscoveryLedger.model_validate_json(first)
    assert discovery_model.search_registration.project_commit_oid == "a" * 40
    assert discovery_model.search_registration.repo_relative_path == (
        "scenarios/flare_corpus/search-spec.json"
    )
    assert main([
        "discover", "--repo", str(flare_git_repo.path),
        "--search-spec", str(search_spec_path),
        "--registration-commit", "a" * 40,
        "--registration-path", "scenarios/flare_corpus/search-spec.json",
        "--round", "initial",
        "--out", str(discovered), "--blob-dir", str(blobs),
    ]) == 0
    assert discovered.read_bytes() == first

    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    assert main([
        "adjudicate", "--ledger", str(discovered),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blobs),
        "--out", str(ledger), "--decision-out", str(decision),
    ]) == 0
    assert b'"status":"provisional_pass"' in decision.read_bytes()
    first_ledger = ledger.read_bytes()
    first_decision = decision.read_bytes()

    second_ledger = tmp_path / "second" / "candidate-ledger.json"
    second_decision = tmp_path / "second" / "b0a-decision.json"
    assert main([
        "adjudicate", "--ledger", str(discovered),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blobs),
        "--out", str(second_ledger), "--decision-out", str(second_decision),
    ]) == 0
    assert second_ledger.read_bytes() == first_ledger
    assert second_decision.read_bytes() == first_decision


@pytest.mark.parametrize("round_name", ["initial", "expanded"])
def test_valid_negative_gate_writes_complete_canonical_outputs_and_uses_exit_three(
    round_name, request, blob_dir, tmp_path
):
    discovered = request.getfixturevalue(f"{round_name}_discovered_path")
    evidence = request.getfixturevalue(f"{round_name}_insufficient_evidence_path")
    ledger_path = tmp_path / round_name / "ledger.json"
    decision_path = tmp_path / round_name / "decision.json"
    args = [
        "adjudicate", "--ledger", str(discovered),
        "--evidence", str(evidence), "--blob-dir", str(blob_dir),
        "--out", str(ledger_path), "--decision-out", str(decision_path),
    ]
    prior_discovery_path = prior_evidence_path = None
    prior_ledger_path = prior_decision_path = None
    if round_name == "expanded":
        prior_discovery_path = request.getfixturevalue("initial_discovered_path")
        prior_evidence_path = request.getfixturevalue("initial_insufficient_evidence_path")
        prior_ledger_path = request.getfixturevalue("initial_ledger_path")
        prior_decision_path = request.getfixturevalue("initial_decision_path")
        args[5:5] = [
            "--prior-discovery", str(prior_discovery_path),
            "--prior-evidence", str(prior_evidence_path),
            "--prior-ledger", str(prior_ledger_path),
            "--prior-decision", str(prior_decision_path),
        ]

    assert main(args) == 3
    ledger, decision = assert_complete_chain(
        ledger_path, decision_path, discovered, evidence,
        prior_ledger_path, prior_decision_path,
    )
    expected = "expanded_round_required" if round_name == "initial" else "insufficient_evidence"
    expected_action = (
        "run_expanded_round" if round_name == "initial"
        else "stop_flare_heavy_investment"
    )
    assert decision.gate.status == expected
    assert decision.gate.next_action == expected_action


def test_expanded_exit_three_consumes_the_cli_published_initial_pair(
    initial_discovered_path, initial_insufficient_evidence_path,
    expanded_discovered_path, expanded_insufficient_evidence_path,
    blob_dir, tmp_path
):
    initial_ledger = tmp_path / "published-initial" / "ledger.json"
    initial_decision = tmp_path / "published-initial" / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_insufficient_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(initial_ledger), "--decision-out", str(initial_decision),
    ]) == 3

    expanded_ledger = tmp_path / "published-expanded" / "ledger.json"
    expanded_decision = tmp_path / "published-expanded" / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(expanded_discovered_path),
        "--evidence", str(expanded_insufficient_evidence_path),
        "--prior-discovery", str(initial_discovered_path),
        "--prior-evidence", str(initial_insufficient_evidence_path),
        "--prior-ledger", str(initial_ledger),
        "--prior-decision", str(initial_decision),
        "--blob-dir", str(blob_dir),
        "--out", str(expanded_ledger), "--decision-out", str(expanded_decision),
    ]) == 3
    initial_model, initial_marker = assert_complete_chain(
        initial_ledger, initial_decision,
        initial_discovered_path, initial_insufficient_evidence_path,
    )
    assert initial_marker.gate.status == "expanded_round_required"
    assert initial_model.gate_summary == initial_marker.gate
    expanded_model, expanded_marker = assert_complete_chain(
        expanded_ledger, expanded_decision,
        expanded_discovered_path, expanded_insufficient_evidence_path,
        initial_ledger, initial_decision,
    )
    assert expanded_marker.gate.status == "insufficient_evidence"
    assert expanded_model.gate_summary == expanded_marker.gate


def test_expanded_requires_all_prior_files_and_initial_rejects_them(
    expanded_discovered_path, expanded_evidence_path,
    initial_discovered_path, initial_positive_evidence_path,
    initial_ledger_path, initial_decision_path, blob_dir, tmp_path
):
    common_out = [
        "--blob-dir", str(blob_dir),
        "--out", str(tmp_path / "ledger.json"),
        "--decision-out", str(tmp_path / "decision.json"),
    ]
    assert main([
        "adjudicate", "--ledger", str(expanded_discovered_path),
        "--evidence", str(expanded_evidence_path), *common_out,
    ]) == 1
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "decision.json").exists()
    assert main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_positive_evidence_path),
        "--prior-discovery", str(initial_discovered_path),
        "--prior-evidence", str(initial_positive_evidence_path),
        "--prior-ledger", str(initial_ledger_path),
        "--prior-decision", str(initial_decision_path), *common_out,
    ]) == 1
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "decision.json").exists()


@pytest.mark.parametrize(
    ("lone_flag", "fixture_name"),
    [
        ("--prior-discovery", "initial_discovered_path"),
        ("--prior-evidence", "initial_insufficient_evidence_path"),
        ("--prior-ledger", "initial_ledger_path"),
        ("--prior-decision", "initial_decision_path"),
    ],
)
def test_lone_prior_flag_is_an_argparse_syntax_error(
    lone_flag, fixture_name, request,
    expanded_discovered_path, expanded_evidence_path, blob_dir, tmp_path,
):
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    with pytest.raises(SystemExit) as exc:
        main([
            "adjudicate", "--ledger", str(expanded_discovered_path),
            "--evidence", str(expanded_evidence_path),
            lone_flag, str(request.getfixturevalue(fixture_name)),
            "--blob-dir", str(blob_dir),
            "--out", str(out), "--decision-out", str(decision),
        ])
    assert exc.value.code == 2
    assert not out.exists() and not decision.exists()


def test_adjudicate_preflights_both_outputs_before_writing(
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    decision.write_bytes(b"conflicting-existing-decision\n")
    assert main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(ledger), "--decision-out", str(decision),
    ]) == 1
    assert not ledger.exists()
    assert decision.read_bytes() == b"conflicting-existing-decision\n"


def test_adjudicate_writes_decision_last_and_reuses_prefix_after_marker_failure(
    initial_discovered_path, initial_positive_evidence_path, blob_dir,
    tmp_path, monkeypatch
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    real_replace = os.replace
    publish_attempts = []

    def fail_completion_marker(source, target):
        publish_attempts.append(Path(target))
        if Path(target) == decision:
            raise OSError("injected decision-marker failure")
        return real_replace(source, target)

    args = [
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(ledger), "--decision-out", str(decision),
    ]
    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_completion_marker)
        assert main(args) == 1

    assert publish_attempts == [ledger, decision]
    CandidateLedger.model_validate_json(ledger.read_bytes())
    assert not decision.exists()

    assert main(args) == 0
    assert_complete_chain(
        ledger, decision, initial_discovered_path, initial_positive_evidence_path
    )


def test_adjudicate_rejects_identical_output_paths_before_writing(
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path, capsys
):
    exact = tmp_path / "same.json"
    assert main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(exact), "--decision-out", str(exact),
    ]) == 1
    assert not exact.exists()
    assert "output paths" in capsys.readouterr().err


def test_discover_rejects_noncanonical_search_spec_without_output(
    flare_git_repo, search_spec_path, tmp_path
):
    changed = tmp_path / "noncanonical-search-spec.json"
    changed.write_bytes(b" \n" + search_spec_path.read_bytes())
    out = tmp_path / "discovered.json"
    blobs = tmp_path / "blobs"
    assert main([
        "discover", "--repo", str(flare_git_repo.path),
        "--search-spec", str(changed),
        "--registration-commit", "a" * 40,
        "--registration-path", "scenarios/flare_corpus/search-spec.json",
        "--round", "initial", "--out", str(out), "--blob-dir", str(blobs),
    ]) == 1
    assert not out.exists() and not blobs.exists()


@pytest.mark.parametrize(
    ("input_flag", "fixture_name"),
    [
        ("--ledger", "expanded_discovered_path"),
        ("--evidence", "expanded_evidence_path"),
        ("--prior-discovery", "initial_discovered_path"),
        ("--prior-evidence", "initial_insufficient_evidence_path"),
        ("--prior-ledger", "initial_ledger_path"),
        ("--prior-decision", "initial_decision_path"),
    ],
)
def test_adjudicate_rejects_noncanonical_json_without_outputs(
    input_flag, fixture_name, request,
    expanded_discovered_path, expanded_evidence_path,
    initial_discovered_path, initial_insufficient_evidence_path,
    initial_ledger_path, initial_decision_path, blob_dir, tmp_path,
):
    inputs = {
        "--ledger": expanded_discovered_path,
        "--evidence": expanded_evidence_path,
        "--prior-discovery": initial_discovered_path,
        "--prior-evidence": initial_insufficient_evidence_path,
        "--prior-ledger": initial_ledger_path,
        "--prior-decision": initial_decision_path,
    }
    source = request.getfixturevalue(fixture_name)
    changed = tmp_path / f"noncanonical-{input_flag[2:]}.json"
    changed.write_bytes(b" \n" + source.read_bytes())
    inputs[input_flag] = changed
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(inputs["--ledger"]),
        "--evidence", str(inputs["--evidence"]),
        "--prior-discovery", str(inputs["--prior-discovery"]),
        "--prior-evidence", str(inputs["--prior-evidence"]),
        "--prior-ledger", str(inputs["--prior-ledger"]),
        "--prior-decision", str(inputs["--prior-decision"]),
        "--blob-dir", str(blob_dir),
        "--out", str(out), "--decision-out", str(decision),
    ]) == 1
    assert not out.exists() and not decision.exists()


def test_adjudicate_rejects_invalid_utf8_without_outputs(
    initial_positive_evidence_path, blob_dir, tmp_path
):
    invalid = tmp_path / "invalid-utf8-ledger.json"
    invalid.write_bytes(b"\xff")
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(invalid),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(out), "--decision-out", str(decision),
    ]) == 1
    assert not out.exists() and not decision.exists()


@pytest.mark.parametrize("blob_state", ["missing", "tampered"])
def test_adjudicate_rejects_missing_or_tampered_patch_cas_without_outputs(
    blob_state,
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path
):
    replay_blobs = tmp_path / "replay-blobs"
    shutil.copytree(blob_dir, replay_blobs)
    discovered = DiscoveryLedger.model_validate_json(
        initial_discovered_path.read_text(encoding="utf-8")
    )
    digest = discovered.discovered_candidates[0].diff_evidence.patch_sha256
    if blob_state == "missing":
        (replay_blobs / digest).unlink()
    else:
        (replay_blobs / digest).write_bytes(b"tampered patch bytes")
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(replay_blobs),
        "--out", str(out), "--decision-out", str(decision),
    ]) == 1
    assert not out.exists() and not decision.exists()


@pytest.mark.parametrize("blob_state", ["present", "missing", "tampered"])
def test_adjudicate_replays_evidence_artifact_cas_at_digest_root(
    blob_state, positive_evidence, initial_discovered_path, blob_dir, tmp_path
):
    replay_blobs = tmp_path / "artifact-blobs"
    shutil.copytree(blob_dir, replay_blobs)
    artifact_bytes = b'{"issue":1,"state":"closed"}\n'
    evidence, digest = approved_evidence_with_source_artifact(
        positive_evidence, artifact_bytes
    )
    evidence_path = tmp_path / "artifact-evidence.json"
    evidence_path.write_bytes(canonical_bytes(evidence))
    if blob_state == "present":
        (replay_blobs / digest).write_bytes(artifact_bytes)
    elif blob_state == "tampered":
        (replay_blobs / digest).write_bytes(b"tampered artifact bytes")

    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    result = main([
        "adjudicate", "--ledger", str(initial_discovered_path),
        "--evidence", str(evidence_path), "--blob-dir", str(replay_blobs),
        "--out", str(out), "--decision-out", str(decision),
    ])
    assert result == (0 if blob_state == "present" else 1)
    assert out.exists() == decision.exists() == (blob_state == "present")


def test_discover_rejects_immutable_output_conflict(
    flare_git_repo, search_spec_path, tmp_path
):
    out = tmp_path / "discovered.json"
    out.write_bytes(b"conflicting-existing-ledger\n")
    assert main([
        "discover", "--repo", str(flare_git_repo.path),
        "--search-spec", str(search_spec_path),
        "--registration-commit", "a" * 40,
        "--registration-path", "scenarios/flare_corpus/search-spec.json",
        "--round", "initial",
        "--out", str(out), "--blob-dir", str(tmp_path / "blobs"),
    ]) == 1
    assert out.read_bytes() == b"conflicting-existing-ledger\n"


def test_domain_failure_is_one_stderr_line_without_traceback(
    initial_positive_evidence_path, blob_dir, tmp_path, capsys
):
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert main([
        "adjudicate", "--ledger", str(tmp_path / "missing-ledger.json"),
        "--evidence", str(initial_positive_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(out), "--decision-out", str(decision),
    ]) == 1
    stderr = capsys.readouterr().err
    assert len(stderr.splitlines()) == 1
    assert "Traceback" not in stderr
    assert not out.exists() and not decision.exists()


def test_module_entrypoint_distinguishes_gate_outcome_from_syntax_error(
    initial_discovered_path, initial_insufficient_evidence_path, blob_dir, tmp_path
):
    ledger = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    command = [
        sys.executable, "-m", "gameforge.bench.flare_mining", "adjudicate",
        "--ledger", str(initial_discovered_path),
        "--evidence", str(initial_insufficient_evidence_path),
        "--blob-dir", str(blob_dir),
        "--out", str(ledger),
        "--decision-out", str(decision),
    ]
    assert subprocess.run(command, check=False).returncode == 3
    _, marker = assert_complete_chain(
        ledger, decision,
        initial_discovered_path, initial_insufficient_evidence_path,
    )
    assert marker.gate.status == "expanded_round_required"
    assert subprocess.run(
        [sys.executable, "-m", "gameforge.bench.flare_mining", "probe"], check=False
    ).returncode == 2


def test_cli_has_no_probe_or_freeze_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["probe"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `uv run pytest tests/bench/test_flare_mining_cli.py -q`

Expected: collection fails because `gameforge.bench.flare_mining` does not exist.

- [ ] **Step 3: Implement the CLI as a thin orchestration layer**

Load all JSON with strict UTF-8 and pydantic validation, then require the input bytes to equal `canonical_bytes(validated_model)`. Verify every referenced CAS blob before adjudication, including prior discovery patches and prior evidence source artifacts. For both current and prior discovery, replay direct matches from the full patch CAS: extract the eligible file blocks, recompute exact message/diff rule IDs per selected round, and require equality with recorded direct reasons before trusting the ledger. Initial ledgers reject all four prior arguments; expanded ledgers require all four, replay the raw prior pair, require the prior decision status to be `expanded_round_required`, verify that the decision points to the canonical prior-ledger bytes, verify both evidence prior hashes, and enforce Task 3's full same-registered-search binding. Argparse enforces that the four prior flags are all present or all absent.

Print a one-line result to stderr. Before constructing the output mapping, reject `--out` and `--decision-out` only when their `Path` values are directly equal, preventing duplicate mapping keys; resolved, hardlink, case-fold, and other filesystem aliases are outside the trusted-local contract. Precompute ledger and decision bytes in memory and preflight both output targets. `write_set_new_or_identical` stages and file-`fsync`s both missing values before publication, then publishes the ledger first and decision last through standard-library `os.replace`, rechecking the ledger before the decision publication. Only a canonical decision with the complete expected bytes is the completed revision marker. Interruption during staging leaves both final paths absent; interruption or failure during ordered publication may leave a complete ledger-only prefix, which an identical retry must reuse. Caught failures clean only the current call's unpublished staging and never unlink a published final. Convert `GitEvidenceError`, `AdjudicationError`, pydantic validation failures, missing/noncanonical files, CAS/hash failures, literal output-path collisions, and immutable-output conflicts to exit 1 without a traceback. A valid negative gate is complete, so both canonical outputs must exist before returning 3. Do not catch `KeyboardInterrupt` or argparse's `SystemExit`. End the module with `raise SystemExit(main())`. Keep business logic in Tasks 1-3.

- [ ] **Step 4: Run the B0A end-to-end test set twice**

Run:

```bash
uv run pytest tests/bench/test_flare_evidence.py tests/bench/test_flare_discover.py tests/bench/test_flare_adjudication.py tests/bench/test_flare_mining_cli.py -q
uv run pytest tests/bench/test_flare_evidence.py tests/bench/test_flare_discover.py tests/bench/test_flare_adjudication.py tests/bench/test_flare_mining_cli.py -q
```

Expected: both runs pass with the same test count.

- [ ] **Step 5: Commit**

```bash
git add gameforge/bench/flare_mining.py tests/bench/test_flare_mining_cli.py
git commit -m "feat(bench): expose Flare B0A mining CLI"
```

---

### Task 5: Freeze and adjudicate the real Flare candidate universe

**Files:**
- Create: `scenarios/flare_corpus/search-spec.json`
- Create: `scenarios/flare_corpus/adjudication-evidence.json`
- Create: `scenarios/flare_corpus/candidate-ledger.discovered.json`
- Create: `scenarios/flare_corpus/candidate-ledger.json`
- Create: `scenarios/flare_corpus/b0a-decision.json`
- Create: `scenarios/flare_corpus/NOTICE`
- Create: `scenarios/flare_corpus/LICENSE.flare-game`
- Create: `scenarios/flare_corpus/blobs/{sha256}` for every discovered patch
- Always preserve: `scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json`, `candidate-ledger.json`, `adjudication-evidence.json`, and `b0a-decision.json`
- Create: `tests/bench/test_flare_evidence_package.py`

**Interfaces:**
- Consumes: the local mirror `/tmp/gameforge-flare-game.git`
- Produces: the immutable B0A evidence package and its actual gate result

Prior dry runs against the pinned mirror estimated 44 direct / 62 post-adjacency candidates for `initial`, and roughly 439 direct / at least 526 post-adjacency candidates for `expanded` after excluding merge commits from the diff arm. These numbers are workload estimates only: they are not frozen expected counts, gate inputs, acceptance criteria, or stop conditions. The committed discovery ledger records the actual deterministic counts.

- [ ] **Step 1: Write the exact canonical search spec**

Write the complete semantic JSON payload frozen below in this plan; do not paraphrase it or add a candidate-count expectation. The block is formatted for review, so normalize it once to sorted compact UTF-8 plus one newline before validation and commit; those normalized bytes are the registered artifact. `message_field` is the UTF-8 `%s` subject while full `%B` remains evidence, every regex and path glob is literal, diff rules see only eligible-path patch bytes, `after_exclusive` is omitted for the all-reachable range, selected round semantics are the union through that round, and adjacency is exactly one nonrecursive first-parent edge in both directions sharing an exact eligible path. The stop condition is exhaustion of the reachable range, never a candidate-count cap.

```json
{
  "adjacency": {
    "first_parent_child_edges": 1,
    "first_parent_predecessor_edges": 1,
    "include_reachable_lineage_sources": true,
    "nonrecursive": true,
    "require_shared_exact_eligible_path_with_anchor": true
  },
  "candidate_order": ["committed_at", "commit_oid"],
  "candidate_path_gate": "any_changed_path_eligible",
  "config_path_globs": ["mods/**/*.txt"],
  "config_only_rule": "all_changed_paths_eligible",
  "diff_merge_policy": "exclude_multi_parent_commits_from_diff_direct",
  "diff_match_scope": "eligible_path_patch_bytes",
  "diff_regex_encoding": "ascii_bytes",
  "excluded_path_globs": [
    "mods/**/README*.txt",
    "mods/**/animations/**",
    "mods/**/books/**",
    "mods/**/cutscenes/**",
    "mods/**/docs/**",
    "mods/**/languages/**",
    "mods/**/languages.txt",
    "mods/**/licenses/**",
    "mods/**/menus/**",
    "mods/**/readme*.txt",
    "mods/**/soundfx/**",
    "mods/**/tilesetdefs/**"
  ],
  "expected_revision_count": 7049,
  "git_commands": {
    "common_prefix": [
      "git", "--no-optional-locks", "--no-replace-objects",
      "-c", "color.ui=false",
      "-c", "core.attributesFile=/dev/null",
      "-c", "core.quotePath=true",
      "-c", "diff.noprefix=false",
      "-c", "diff.mnemonicPrefix=false",
      "-c", "diff.renames=false",
      "-c", "diff.algorithm=myers",
      "-c", "diff.indentHeuristic=false",
      "-c", "diff.interHunkContext=0",
      "-c", "diff.suppressBlankEmpty=false",
      "-c", "diff.orderFile=/dev/null",
      "-C", "{repo}"
    ],
    "empty_tree_args": ["hash-object", "-t", "tree", "--stdin"],
    "eligible_path_suffix": ["--", "{eligible_paths...}"],
    "history_args": ["rev-list", "--topo-order", "--reverse", "{revision_range}"],
    "metadata_args": [
      "show", "-s", "--no-show-signature", "--encoding=UTF-8",
      "--format=%H%x00%P%x00%ct%x00%s%x00%B", "{commit}"
    ],
    "patch_args": [
      "diff", "--binary", "--full-index", "--no-color", "--no-ext-diff",
      "--no-textconv", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
      "--unified=3", "--inter-hunk-context=0", "--diff-algorithm=myers",
      "--no-indent-heuristic", "--submodule=short", "--ignore-submodules=none",
      "{parent}", "{commit}"
    ],
    "patch_id_args": ["patch-id", "--stable"],
    "paths_args": [
      "diff-tree", "--no-commit-id", "--name-status", "--no-renames",
      "-r", "-z", "{parent}", "{commit}"
    ],
    "resolve_args": ["rev-parse", "--verify", "{pinned_head}^{commit}"],
    "version_command": ["git", "--version"]
  },
  "git_environment_policy": {
    "drop_inherited_prefixes": ["GIT_"],
    "fixed": {
      "GIT_ATTR_NOSYSTEM": "1",
      "GIT_CONFIG_GLOBAL": "/dev/null",
      "GIT_CONFIG_NOSYSTEM": "1",
      "GIT_NO_REPLACE_OBJECTS": "1",
      "GIT_OPTIONAL_LOCKS": "0",
      "LANG": "C",
      "LC_ALL": "C",
      "TZ": "UTC"
    },
    "inherit_allowlist": ["PATH"]
  },
  "history_walk": "all_reachable_topo_order",
  "issue_pr_discovery": "disabled_offline_only",
  "lineage_regexes": [
    {
      "link_type": "backport",
      "pattern": "(?m)^Backport-of: ([0-9a-f]{40})$",
      "rule_id": "trailer.backport_of"
    },
    {
      "link_type": "cherry_pick",
      "pattern": "(?m)^\\(cherry picked from commit ([0-9a-f]{40})\\)$",
      "rule_id": "trailer.cherry_pick_x"
    },
    {
      "link_type": "revert",
      "pattern": "(?m)^This reverts commit ([0-9a-f]{40})\\.$",
      "rule_id": "trailer.git_revert"
    }
  ],
  "lineage_message_field": "full_percent_B_utf8",
  "message_field": "subject_percent_s_utf8",
  "path_eligibility": "include_and_not_exclude",
  "path_glob_semantics": "component_fnmatch_double_star_zero_or_more",
  "pinned_head": "fe23b5ba73f99f0c3969f8b23dbabaa8f7a6b602",
  "rounds": [
    {
      "diff_regexes": [],
      "message_regexes": [
        {
          "pattern": "(?i)\\A(?=[^\\r\\n]*\\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \\t]+appearing|not[ \\t]+being[ \\t]+able|completed[ \\t]+before)\\b)(?=[^\\r\\n]*\\b(?:quests?|status(?:es)?|loot|drops?|references?|spawns?|chests?|enem(?:y|ies)|items?)\\b)[^\\r\\n]*\\Z",
          "rule_id": "initial.message_bug_and_domain"
        }
      ],
      "name": "initial"
    },
    {
      "diff_regexes": [
        {
          "pattern": "(?m)^[+-](?![+-])[ \\t]*(?:requires_status|requires_not_status|set_status|unset_status|pickup_status|loot|chance|weight|requires_item|item)[ \\t]*=",
          "rule_id": "expanded.diff_behavior_key"
        }
      ],
      "message_regexes": [
        {
          "pattern": "(?i)\\A(?!merge(?:[ \\t]|\\Z))(?=[^\\r\\n]*\\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \\t]+appearing|not[ \\t]+being[ \\t]+able|completed[ \\t]+before)\\b)[^\\r\\n]*\\Z",
          "rule_id": "expanded.message_bug_language"
        }
      ],
      "name": "expanded"
    }
  ],
  "schema_version": "flare-b0a@1",
  "selected_round_semantics": "union_through_selected",
  "source_repo": "https://github.com/flareteam/flare-game.git",
  "stop_condition": "exhaust_reachable_range"
}
```

Validate both schema and canonical bytes without producing discovery output:

Run:

```bash
uv run python -c 'import json; from pathlib import Path; p=Path("scenarios/flare_corpus/search-spec.json"); d=json.loads(p.read_text(encoding="utf-8")); p.write_text(json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")'
uv run python -c 'from pathlib import Path; from gameforge.bench.flare_evidence import FlareSearchSpec, canonical_bytes; p=Path("scenarios/flare_corpus/search-spec.json"); m=FlareSearchSpec.model_validate_json(p.read_text(encoding="utf-8")); assert p.read_bytes()==canonical_bytes(m)'
uv run python -c 'import runpy; from pathlib import Path; expected=runpy.run_path("tests/bench/conftest.py")["REGISTERED_SEARCH_SPEC_BYTES"]; assert Path("scenarios/flare_corpus/search-spec.json").read_bytes()==expected'
```

Expected: exit 0.

- [ ] **Step 2: Commit the search registration before discovery**

```bash
git add scenarios/flare_corpus/search-spec.json
git commit -m "data(bench): preregister Flare B0A search"
git diff-tree --no-commit-id --name-only -r HEAD
```

Expected: the commit contains only `scenarios/flare_corpus/search-spec.json`. Resolve its full OID in every later command with `git log -1 --format=%H -- scenarios/flare_corpus/search-spec.json`; do not rely on a shell variable surviving between sessions. No discovery ledger, patch blob, adjudication file, or result may exist in that commit. A provenance test later verifies the registered path bytes and ancestry.

- [ ] **Step 3: Run initial discovery into its permanent evidence revision**

Run:

```bash
uv run python -m gameforge.bench.flare_mining discover --repo /tmp/gameforge-flare-game.git --search-spec scenarios/flare_corpus/search-spec.json --registration-commit "$(git log -1 --format=%H -- scenarios/flare_corpus/search-spec.json)" --registration-path scenarios/flare_corpus/search-spec.json --round initial --out scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json --blob-dir scenarios/flare_corpus/blobs
```

Expected: exit 0; the ledger records the observed direct/context counts and universe hash, and every referenced blob exists. Do not edit the registered spec in response to these results.

- [ ] **Step 4: Adjudicate every initial candidate from raw patch evidence**

For each candidate, record one of:

- a proposed case with an existing B0A taxonomy class, concrete commit/message/patch evidence references, and an independent `fix_group_id` supported by root-cause evidence;
- rejection as non-bug, out-of-taxonomy, non-config-only, indeterminate oracle, duplicate/backport/revert, or mixed engine/schema change;
- ambiguity with the exact missing evidence needed.

Resolve every objective lineage link and build the complete initial disposition table. Present the table, candidate-universe hash, and canonical approval-payload SHA-256 to the user for written review. Only after explicit approval, add a `ReviewAttestation` containing that exact hash, the user's reviewer identity/revision/written statement, and an identity distinct from the assisted adjudicator. Recompute the full evidence hash, then run:

```bash
uv run python -m gameforge.bench.flare_mining adjudicate --ledger scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json --evidence scenarios/flare_corpus/b0a/initial/adjudication-evidence.json --blob-dir scenarios/flare_corpus/blobs --out scenarios/flare_corpus/b0a/initial/candidate-ledger.json --decision-out scenarios/flare_corpus/b0a/initial/b0a-decision.json
```

Expected: exit 0 for an initial `provisional_pass`, or exit 3 with `status="expanded_round_required"` and `next_action="run_expanded_round"`.

- [ ] **Step 5: If initial is insufficient, discover and separately review the frozen expanded round**

Run expanded discovery first, without adjudicating:

```bash
uv run python -m gameforge.bench.flare_mining discover --repo /tmp/gameforge-flare-game.git --search-spec scenarios/flare_corpus/search-spec.json --registration-commit "$(git log -1 --format=%H -- scenarios/flare_corpus/search-spec.json)" --registration-path scenarios/flare_corpus/search-spec.json --round expanded --out scenarios/flare_corpus/candidate-ledger.discovered.json --blob-dir scenarios/flare_corpus/blobs
```

Review the expanded ledger in deterministic contiguous batches of 50 candidates in ledger order `(committed_at, commit_oid)`; the final batch contains the remainder. Each batch record carries its one-based batch number, inclusive candidate-index range, first/last OID, decisions, rationales, and evidence refs. Batch files are working aids only: they cannot carry an approval attestation, cannot change ordering, and cannot be fed to `adjudicate`. After all batches, concatenate by candidate order, validate exact universe coverage and initial projection, and build one full expanded evidence payload that binds both prior derived-file hashes, repeats the complete initial source-artifact prefix, applicability declarations, group/candidate decisions, and resolutions byte-equivalently, and adds decisions only for newly discovered candidates or newly materialized objective links.

Present the complete disposition table and delta, expanded universe hash, both prior hashes, and the single complete approval-payload hash to the user. Expanded adjudication is forbidden until the user gives a second explicit written approval bound to that complete payload. There is exactly one final `ReviewAttestation`; no batch-level or partial approval is accepted. Then run:

```bash
uv run python -m gameforge.bench.flare_mining adjudicate --ledger scenarios/flare_corpus/candidate-ledger.discovered.json --evidence scenarios/flare_corpus/adjudication-evidence.json --blob-dir scenarios/flare_corpus/blobs --prior-discovery scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json --prior-evidence scenarios/flare_corpus/b0a/initial/adjudication-evidence.json --prior-ledger scenarios/flare_corpus/b0a/initial/candidate-ledger.json --prior-decision scenarios/flare_corpus/b0a/initial/b0a-decision.json --out scenarios/flare_corpus/candidate-ledger.json --decision-out scenarios/flare_corpus/b0a-decision.json
```

Expected: exit 0 with `provisional_pass`, or exit 3 with `insufficient_evidence` and `next_action="stop_flare_heavy_investment"`. Do not change the search spec or any initial decision after either result.

- [ ] **Step 6: If initial passes, publish it once at the root evidence paths**

Publish all four reviewed initial files through one ordered, preflighted WORM call, never with `cp` and never one-by-one. Each final pathname is published atomically, but the four-path sequence is not represented as a filesystem transaction. All missing values are staged before the first publication, and a failure during publication retains a complete reusable prefix:

```bash
uv run python -c 'from pathlib import Path; from gameforge.bench.flare_evidence import write_set_new_or_identical; src=Path("scenarios/flare_corpus/b0a/initial"); dst=Path("scenarios/flare_corpus"); names=("candidate-ledger.discovered.json","adjudication-evidence.json","candidate-ledger.json","b0a-decision.json"); write_set_new_or_identical({dst/n:(src/n).read_bytes() for n in names})'
```

The root discovery, evidence, ledger, and decision must be byte-equal to the preserved initial revision. Any different target found during preflight fails before the first root file is created. Before each later `os.replace`, the complete earlier prefix is reread; a changed prefix fails without publishing the later completion path or deleting any final. Caught failures may clean only staging created by that call.

- [ ] **Step 7: Add attribution and verify the package without the upstream clone**

`NOTICE` records the upstream URL, pinned head, all-reachable walk, resolved registration commit/spec hash, exact mining commands and tool versions, Flare license, extraction date, prior exploratory/non-blind status, and the fact that patches are evidence rather than a whole-game redistribution. Publish the pinned upstream license bytes to `LICENSE.flare-game` through `write_new_or_identical`.

`tests/bench/test_flare_evidence_package.py` has two separate checks:

1. with the upstream Flare path unavailable and Git subprocesses forbidden, reject reserved `.gameforge-*.tmp` staging files, require canonical bytes for every JSON; assert the packaged search spec is byte-equal to `registered_search_spec_bytes` and its SHA-256 equals `registered_search_spec_sha256` from the independent shared fixtures; then verify the complete hash chain, all CAS blobs and evidence refs, attestation, prior projection, candidate coverage, lineage constraints, exact 11-row matrix, and re-derived ledger/decision/gate;
2. using only the GameForge repository Git history, verify the recorded registration commit is an ancestor, contains bytes equal to both the packaged file and `registered_search_spec_bytes` at the recorded path, has the fixed expected spec hash, and contains none of the discovery/result paths.

- [ ] **Step 8: Commit the evidence separately from implementation**

```bash
git add scenarios/flare_corpus tests/bench/test_flare_evidence_package.py
git commit -m "data(bench): freeze Flare B0A candidate ledger"
```

---

### Task 6: B0A acceptance, truthful status, and next-plan branch

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-m3d-flare-rich-design.md`
- Modify: `docs/superpowers/plans/README.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: committed `b0a-decision.json`
- Produces: one truthful project state and the correct next planning boundary

- [ ] **Step 1: Run focused and full verification from a clean evidence package**

Run:

```bash
uv run pytest tests/bench/test_flare_evidence.py tests/bench/test_flare_discover.py tests/bench/test_flare_adjudication.py tests/bench/test_flare_mining_cli.py -q
uv run pytest -q
uv run lint-imports
uv run ruff check .
git diff --check
```

Expected: all tests pass, all import-linter contracts are kept, Ruff is clean, and `git diff --check` is silent.

- [ ] **Step 2: Update status from evidence, without claiming more than B0A**

For `provisional_pass`, record the exact proposed group/class counts and state: `B0A passed; B0B qualification is next; no candidate is qualified or accepted yet`.

For `insufficient_evidence`, record the exact counts/reasons and state: `Flare-heavy M3d investment stopped; PRD §13.3 remains unmet; choose another external source or approve a written scope waiver before M4`.

In both outcomes, change the old M3 `✅` marker to an honest in-progress/blocked state and keep narrative BDR, Human-Edit-Distance, QA-hours, `DROPS_FROM`, and repair cassette/apply semantics visible as separate pre-M4 debts. Do not backfill fictional M3b/M3c historical plans.

- [ ] **Step 3: Self-review the committed ledger against the approved B0A gate**

Check all five conditions directly: 8 independent groups, 4 proposed classes, config-only for every counted group, at least one proposed case per counted group, and all 11 applicability rows. Search the evidence for missing dispositions, unreferenced blobs, unresolved objective links, `qualified_candidate > 0`, or `accepted > 0`; any hit fails B0A acceptance.

- [ ] **Step 4: Commit the acceptance record**

```bash
git add docs/superpowers/specs/2026-07-10-m3d-flare-rich-design.md docs/superpowers/plans/README.md CLAUDE.md README.md
git commit -m "docs(m3d): record Flare B0A investment decision"
```

- [ ] **Step 5: Branch to the evidence-driven next plan**

When positive, invoke `writing-plans` for B0B qualification/probe only, handing off the actual proposed units/classes. B0A contains no dialect or complexity facts: B0B must first compute the frozen dialect and complexity tuple for every proposed unit, then select the oldest-dialect and lexicographic-maximum probes. When negative, do not write a Flare reader plan; select a new external corpus or obtain the explicit PRD scope decision first.

---

## Self-Review

- **Spec coverage:** D1 search freeze/two rounds, D2 config-only and lineage, D4 11-class matrix, §4.1 candidate ledger/CAS, §5 discover/adjudicate boundary, §8.1 B0A tests, and §9 provisional gate all map to Tasks 1-6.
- **Scope:** `probe`, qualification registry/replay, witnesses, locators, engine evidence, complexity-max semantic feasibility, temporal split, freeze, reader/checker, matcher/scorer, reports, panel, and M4 platform surfaces are absent by design and named as later work.
- **Type consistency:** discovery returns `DiscoveryLedger`; adjudication returns `CandidateLedger` with `groups`, `applicability_matrix`, and `gate_summary`; case disposition is the truth source, while group disposition is derived.
- **Determinism:** Git command semantics, head/range, candidate order, patch bytes, CAS key, canonical JSON, candidate-universe hash, immutable output, and the no-clone replay check are all locked by tests.
- **Honesty:** B0A cannot emit `qualified_candidate`, `accepted`, or `validated`; an expanded-round negative result is preserved as useful evidence but explicitly leaves M3 external validity incomplete.
- **No over-design:** four focused Python modules, one filesystem blob directory, no database/service/network/LLM, and no production Flare semantic implementation before the investment gate.
