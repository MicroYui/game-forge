# Pre-M4 External Cases and Endless Sky Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents for this plan.

**Goal:** Turn eight frozen, config-only Endless Sky bug-fix commits into replayable before/after evidence that the source-neutral GameForge checker detects four real defect classes without source-specific checker branches.

**Architecture:** A small `external-case@1` package owns source-neutral evidence contracts, tree hashing, qualification, and scoring. An Endless Sky lossless reader and Adapter stay at the ingestion boundary; independent source predicates stay in the benchmark boundary. The existing `GraphChecker` and `ASPChecker` gain only generic dependency/access semantics, while the legacy B0A approval machinery remains frozen replay material.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Hypothesis, Clingo, stdlib Git/subprocess/hashlib, and a standalone C++17 syntax witness derived from the pinned Endless Sky `DataFile` parser.

## Global Constraints

- Read and obey `docs/superpowers/specs/2026-07-03-gameforge-prd.md`, `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md`, and `docs/superpowers/specs/2026-07-12-pre-m4-lean-closure-design.md` before editing production code.
- TDD is mandatory: every production behavior starts with a focused failing test whose expected failure is observed.
- `spine` may import only `contracts`, other `spine` modules, and the standard library; it must not import `bench`, Agents, or any LLM SDK.
- Checker, taxonomy, metrics, and reporting code must contain no `endless_sky`, upstream object name, path, PR, or commit-OID special case.
- Source-specific behavior is limited to the Endless Sky reader, Adapter, fixture builder, native parser, and independent qualification predicates.
- The eight frozen commits and their development/verification splits cannot be replaced after a miss.
- New online Agent evidence uses `openai/gpt-5.6-sol/pre-m4@1`; this slice is deterministic and performs no LLM call. Historical Opus cassettes remain byte-identical.
- The legacy Flare/Endless Sky B0A code and artifacts remain replayable but receive no new approval, nonce, assignment-table, or attestation feature.
- All raw fixtures retain upstream GPL-3.0-or-later notice, commit/path provenance, and SHA-256 bindings.
- No live network is permitted in tests or evidence replay. Fixture extraction reads the local pinned bare repository only.
- Every task ends with `git diff --check`; commits contain no AI attribution.

---

### Task 1: Source-Neutral `external-case@1` Contracts

**Files:**
- Create: `gameforge/bench/external_cases/__init__.py`
- Create: `gameforge/bench/external_cases/contracts.py`
- Create: `gameforge/bench/external_cases/tree.py`
- Create: `tests/bench/external_cases/__init__.py`
- Create: `tests/bench/external_cases/test_contracts.py`
- Create: `tests/bench/external_cases/test_tree.py`

**Interfaces:**
- Consumes: `DefectClass`, `canonical_json`, and regular files below a trusted fixture root.
- Produces: `TargetLocator`, `ExternalCaseSpec`, `TreeFile`, `TreeArtifact`, `NativeEvidence`, `PredicateEvidence`, `FindingEvidence`, `HumanTarget`, `ExternalCaseEvidence`, `ExternalCorpusManifest`, `tree_artifact()`, `read_tree()`, `content_sha256()`, and `canonical_bytes()`.

- [x] **Step 1: Write failing strict-contract and hash-binding tests**

```python
def test_case_spec_requires_nonempty_targets_and_config_paths():
    with pytest.raises(ValidationError):
        case_spec(target_locators=())
    with pytest.raises(ValidationError):
        case_spec(changed_paths=("source/Mission.cpp",))


def test_manifest_hash_binds_every_case_and_rejects_tampering():
    manifest = corpus_manifest()
    assert manifest.manifest_sha256 == content_sha256(
        manifest, exclude={"manifest_sha256"}
    )
    payload = manifest.model_dump(mode="json")
    payload["cases"][0]["upstream_subject"] = "tampered"
    with pytest.raises(ValueError, match="manifest_sha256"):
        ExternalCorpusManifest.model_validate(payload)
```

```python
def test_tree_artifact_partitions_exact_regular_files(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_bytes(b"alpha\n")
    artifact = tree_artifact(tmp_path)
    assert artifact.files[0].path == "data/a.txt"
    assert read_tree(tmp_path, artifact) == {"data/a.txt": b"alpha\n"}
    (tmp_path / "data/a.txt").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="sha256"):
        read_tree(tmp_path, artifact)
```

- [x] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_contracts.py tests/bench/external_cases/test_tree.py -q`

Expected: collection fails because `gameforge.bench.external_cases` does not exist.

- [x] **Step 3: Implement the full immutable evidence models and canonical self-hashes**

Use strict frozen Pydantic models. The core field shapes are:

```python
class TargetLocator(_StrictModel):
    path: PosixRelativePath
    record_kind: StableId
    record_name: NonEmptyStr


class ExternalCaseSpec(_StrictModel):
    schema_version: Literal["external-case-spec@1"]
    case_id: StableId
    source_id: StableId
    source_repository: HttpUrl
    license_id: StableId
    before_commit: Oid
    after_commit: Oid
    upstream_subject: NonEmptyStr
    upstream_pr: int | None = Field(default=None, gt=0)
    changed_paths: tuple[PosixRelativePath, ...]
    defect_class: DefectClass
    target_locators: tuple[TargetLocator, ...]
    split: Literal["development", "verification"]
    predicate_id: StableId


class ExternalCaseEvidence(_StrictModel):
    schema_version: Literal["external-case@1"]
    spec: ExternalCaseSpec
    before_tree: TreeArtifact
    after_tree: TreeArtifact
    native_before: NativeEvidence
    native_after: NativeEvidence
    predicate_before: PredicateEvidence
    predicate_after: PredicateEvidence
    reader_version: VersionId
    adapter_version: VersionId
    mapping_spec_sha256: Sha256
    findings_before: tuple[FindingEvidence, ...]
    findings_after: tuple[FindingEvidence, ...]
    human_target: HumanTarget
    agent_patch_sha256: Sha256 | None = None
    agent_target_snapshot_id: str | None = None
    qualification_status: Literal["qualified", "miss"]
    failure_reasons: tuple[NonEmptyStr, ...] = ()
    evidence_sha256: Sha256
```

`ExternalCaseEvidence` and `ExternalCorpusManifest` validate their own hashes after model construction. `content_sha256()` serializes `model_dump(mode="json", exclude=...)` through `canonical_json` and returns a bare 64-character digest. `canonical_bytes()` appends one newline to canonical JSON.

`tree_artifact()` walks only regular files, rejects symlinks and special files, sorts normalized POSIX paths, records byte size and SHA-256, and binds the ordered file descriptors into `tree_sha256`. `read_tree()` rechecks path containment, regular-file type, size, per-file digest, and aggregate digest before returning bytes.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/bench/external_cases/test_contracts.py tests/bench/external_cases/test_tree.py -q`

Expected: all tests pass.

- [x] **Step 5: Commit the source-neutral evidence contract**

```bash
git add gameforge/bench/external_cases tests/bench/external_cases
git diff --cached --check
git commit -m "feat(bench): add lean external case evidence contracts"
```

---

### Task 2: Lossless Endless Sky Reader

**Files:**
- Create: `gameforge/spine/ingestion/endless_sky_reader.py`
- Create: `tests/spine/ingestion/test_endless_sky_reader.py`
- Create: `tests/spine/ingestion/test_endless_sky_reader_property.py`

**Interfaces:**
- Consumes: raw UTF-8 bytes and a normalized repository-relative source path.
- Produces: `SourceSpan`, `DataToken`, `PhysicalLine`, `DataNode`, `DataFile`, `EndlessSkyTree`, `parse_data_file(bytes, path)`, `render_data_file(data_file)`, `read_source_tree(mapping)`, `render_source_tree(tree)`, and `top_level_chunks(data_file)`.

- [x] **Step 1: Write failing example tests for quotes, comments, indentation, and exact bytes**

```python
def test_reader_builds_token_tree_and_round_trips_exact_bytes():
    raw = (
        b'# preamble\r\n'
        b'mission "Quest Name"\r\n'
        b'\tto offer # comment\r\n'
        b'\t\thas `Quest Zero: done`\r\n'
        b'\r\n'
    )
    parsed = parse_data_file(raw, "data/missions.txt")
    mission = parsed.roots[0]
    assert [token.value for token in mission.tokens] == ["mission", "Quest Name"]
    assert mission.children[0].tokens[0].value == "to"
    assert mission.source_span.start_line == 2
    assert render_data_file(parsed) == raw
```

```python
def test_top_level_chunks_partition_every_input_byte_once():
    raw = b"# banner\nmission A\n\tsource X\n\nmission B\n\tdestination Y"
    chunks = top_level_chunks(parse_data_file(raw, "data/a.txt"))
    assert b"".join(chunk.raw for chunk in chunks) == raw
    assert [(chunk.kind, chunk.name) for chunk in chunks] == [
        ("mission", "A"),
        ("mission", "B"),
    ]
```

- [x] **Step 2: Run example tests and verify RED**

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_reader.py -q`

Expected: import failure for the missing reader.

- [x] **Step 3: Implement a byte-preserving physical-line parser and indentation tree**

The reader must:

```python
@dataclass(frozen=True)
class DataToken:
    value: str
    raw: bytes
    quote: Literal["bare", "double", "backtick"]


@dataclass(frozen=True)
class DataNode:
    tokens: tuple[DataToken, ...]
    children: tuple["DataNode", ...]
    indent: bytes
    source_span: SourceSpan
    line_index: int


@dataclass(frozen=True)
class DataFile:
    path: str
    raw: bytes
    lines: tuple[PhysicalLine, ...]
    roots: tuple[DataNode, ...]
```

Split input with `bytes.splitlines(keepends=True)` while preserving a final non-newline line. Tokenize the content portion using Endless Sky rules: whitespace separates bare tokens; `"..."` and `` `...` `` group tokens; `#` begins a comment only outside quotes. Indentation is the exact leading space/tab prefix. Non-comment nodes become children of the most recent node with strictly smaller indentation width, matching upstream `DataFile`; comments and blank lines remain `PhysicalLine` entries but not semantic nodes. Reject invalid UTF-8, NUL bytes, and an unterminated quote with `EndlessSkyParseError(path, line, reason)`.

`render_data_file()` concatenates `PhysicalLine.raw`, not normalized tokens. `top_level_chunks()` uses root line indices and adjacent-root boundaries so chunks form an exact, ordered partition including preamble, inter-record trivia, and trailing bytes.

- [x] **Step 4: Add and observe a failing Hypothesis round-trip property**

```python
@given(source_files())
@settings(max_examples=300)
def test_render_parse_is_byte_exact(raw):
    parsed = parse_data_file(raw, "data/property.txt")
    assert render_data_file(parsed) == raw
    assert b"".join(c.raw for c in top_level_chunks(parsed)) == raw
```

Generate valid bare/quoted/backtick tokens, tabs or spaces, comments, blank lines, CRLF/LF, nested indentation, and optional final newline. The first run must fail on at least one unsupported generated shape before adjusting the parser, not the assertion.

- [x] **Step 5: Complete the reader until example and property tests pass**

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_reader.py tests/spine/ingestion/test_endless_sky_reader_property.py -q`

Expected: all tests pass with 300 property examples.

- [x] **Step 6: Commit the reader**

```bash
git add gameforge/spine/ingestion/endless_sky_reader.py tests/spine/ingestion/test_endless_sky_reader.py tests/spine/ingestion/test_endless_sky_reader_property.py
git diff --cached --check
git commit -m "feat(ingestion): add lossless Endless Sky data reader"
```

---

### Task 3: Frozen Case Registration and Fixture Extraction

**Files:**
- Create: `gameforge/bench/external_cases/endless_sky_fixture.py`
- Create: `scenarios/external_cases/endless_sky/case-specs.json`
- Create: `scenarios/external_cases/endless_sky/mapping-spec.json`
- Create: `scenarios/external_cases/endless_sky/NOTICE.md`
- Create: `scenarios/external_cases/endless_sky/LICENSE.upstream.txt`
- Generate: `scenarios/external_cases/endless_sky/cases/**/{before,after}/data/**/*.txt`
- Generate: `scenarios/external_cases/endless_sky/cases/**/upstream.patch`
- Generate: `scenarios/external_cases/endless_sky/cases/**/context.json`
- Create: `tests/bench/external_cases/test_endless_sky_registration.py`
- Create: `tests/bench/external_cases/test_endless_sky_fixture.py`

**Interfaces:**
- Consumes: local bare repo `/Users/liyifan/.cache/gameforge/endless-sky.git`, pinned head `b10b7d6c24496e2f67a230a2553b344e200ba289`, and the eight frozen commits in the approved design.
- Produces: canonical case specs, exact parent/commit versions of every changed config file, the exact upstream patch, source context, and deterministic fixture tree descriptors.

- [x] **Step 1: Write failing registration tests that lock all eight cases**

```python
EXPECTED = {
    ("dangling_reference", "development"): "02e6ded1e7cb9ef7a8e401e71c9accd6133a68b5",
    ("dangling_reference", "verification"): "61425f7538b33ed5bddd77ea9c29ffd7737a242b",
    ("cyclic_dependency", "development"): "2476129506e96086b00b09e1999dcb10ff8390fd",
    ("cyclic_dependency", "verification"): "95b5c4e95f715c2a13c201396d6dda5ea33d8cf7",
    ("unreachable_target", "development"): "9e437162fffef43da5f836d1f92bb265ccc75c52",
    ("unreachable_target", "verification"): "34383dd960f42de2537a06c2bb0ba3f35a8a73c0",
    ("dead_quest", "development"): "de8385df680ba81c70f13b380ef0b13070eba49b",
    ("dead_quest", "verification"): "9b29c95b99e67efbd1acda09a9994fe37405278e",
}


def test_registration_is_exact_and_balanced():
    specs = load_case_specs(CORPUS / "case-specs.json")
    assert {(s.defect_class.value, s.split): s.after_commit for s in specs} == EXPECTED
    assert len({s.case_id for s in specs}) == 8
    assert all(s.changed_paths and s.target_locators for s in specs)
```

- [x] **Step 2: Run registration tests and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_endless_sky_registration.py -q`

Expected: missing case specs and loader.

- [x] **Step 3: Implement the bounded local-Git extractor**

`extract_case(repo, spec, output_root)` must use `subprocess.run([...], shell=False)` with a fixed `LANG=C`, `LC_ALL=C`, `TZ=UTC`, no inherited `GIT_*`, and these operations only:

```text
git rev-parse <after>^1
git merge-base --is-ancestor <after> <pinned_head>
git diff-tree --name-only --no-renames -r <before> <after>
git show <before>:<path>
git show <after>:<path>
git diff --binary --full-index --no-renames <before> <after> -- <changed_paths...>
git show -s --format=%s%x00%b <after>
```

It rejects merge commits, path mismatches, any non-`data/**/*.txt` path, and a subject/body mismatch with the frozen spec. It writes exact blob bytes and patch bytes, then derives `context.json` without case-ID branches:

- all resource names referenced by the selected target records and their matching upstream asset paths;
- all selected mission destinations that the after version protects with `clearance` or `has "landing access: ..."`;
- target record names obtained from diff-hunk containment and checked against `target_locators`.

The mapping spec contains only source grammar/IR rules and versions. It must not contain a commit OID, path, PR, or object name.

- [x] **Step 4: Register exact specs and extract fixtures**

Run:

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_fixture \
  --repo /Users/liyifan/.cache/gameforge/endless-sky.git \
  --corpus scenarios/external_cases/endless_sky \
  --pinned-head b10b7d6c24496e2f67a230a2553b344e200ba289
```

Expected: eight case directories, exact before/after files, eight patches, and no network access.

- [x] **Step 5: Verify fixture provenance and byte hashes**

Tests independently run read-only `git show` against the local bare repo when present and require every committed fixture byte to match. Offline mode still verifies committed SHA-256 tree descriptors, patch digests, GPL notice, case count, paths, parents, and pinned-head ancestry recorded by the extractor.

Run: `uv run pytest tests/bench/external_cases/test_endless_sky_registration.py tests/bench/external_cases/test_endless_sky_fixture.py -q`

Expected: all tests pass.

- [x] **Step 6: Commit the frozen source corpus**

```bash
git add gameforge/bench/external_cases/endless_sky_fixture.py scenarios/external_cases/endless_sky tests/bench/external_cases/test_endless_sky_registration.py tests/bench/external_cases/test_endless_sky_fixture.py
git diff --cached --check
git commit -m "data(bench): freeze eight Endless Sky bug-fix cases"
```

---

### Task 4: Independent Native Parser Witness

**Files:**
- Create: `scenarios/external_cases/endless_sky/native/endless_sky_data_parser.cpp`
- Create: `scenarios/external_cases/endless_sky/native/source-provenance.json`
- Create: `gameforge/bench/external_cases/native.py`
- Create: `tests/bench/external_cases/test_native_parser.py`

**Interfaces:**
- Consumes: fixture file paths, a C++17 compiler, and the standalone parser source.
- Produces: `compile_native_parser()`, `run_native_parser()`, deterministic node/token summaries, and `NativeEvidence` for each before/after tree.

- [x] **Step 1: Write a failing compile/run conformance test**

```python
def test_native_parser_matches_python_node_and_token_counts(tmp_path):
    binary = compile_native_parser(CORPUS / "native/endless_sky_data_parser.cpp", tmp_path)
    result = run_native_parser(binary, [FIXTURE])
    parsed = parse_data_file(FIXTURE.read_bytes(), "data/example.txt")
    assert result.exit_code == 0
    assert result.summary == {
        "files": 1,
        "nodes": count_nodes(parsed),
        "tokens": count_tokens(parsed),
    }
```

- [x] **Step 2: Run the native test and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_native_parser.py -q`

Expected: native source/runner is missing.

- [x] **Step 3: Implement the standalone C++17 parser witness**

The program accepts one or more file paths, reads binary bytes, validates UTF-8, tokenizes bare/double/backtick tokens, applies strict-smaller indentation parenting, rejects NUL and unterminated quotes, and prints exactly one canonical line:

```text
files=<n> nodes=<n> tokens=<n>
```

The source header and `source-provenance.json` bind the derivation to upstream `source/DataFile.cpp`, `source/DataFile.h`, `source/DataNode.cpp`, `source/DataNode.h`, and `source/text/Utf8.*` at pinned commit `b10b7d6c...`, list their Git blob OIDs, state that this is a syntax witness rather than the full game engine, and apply GPL-3.0-or-later to the standalone file.

`compile_native_parser()` resolves `CXX` or `c++`, captures compiler/version, invokes `-std=c++17 -O2`, and hashes source/binary/stdout/stderr. `run_native_parser()` sorts paths, computes the input-manifest SHA-256 in Python, and never invokes a shell. The C++ witness does parsing only; it does not carry an unnecessary second cryptographic implementation.

- [x] **Step 4: Verify native/Python conformance over all 16 trees**

Run: `uv run pytest tests/bench/external_cases/test_native_parser.py -q`

Expected: all fixture files parse on both implementations and the node/token totals agree.

- [x] **Step 5: Commit the native witness**

```bash
git add scenarios/external_cases/endless_sky/native gameforge/bench/external_cases/native.py tests/bench/external_cases/test_native_parser.py
git diff --cached --check
git commit -m "test(bench): add independent Endless Sky parser witness"
```

---

### Task 5: Generic Dependency and Access-Gate Checker Semantics

**Files:**
- Modify: `gameforge/spine/checkers/graph.py`
- Modify: `gameforge/spine/checkers/asp.py`
- Modify: `tests/spine/checkers/test_graph.py`
- Modify: `tests/spine/checkers/test_asp.py`
- Modify: `tests/spine/checkers/test_asp_vs_graph_differential.py`

**Interfaces:**
- Consumes: existing IR relations only.
- Produces: cycle analysis over repeatable `HAS_STEP | PRECEDES | REQUIRES` edges and generic access proof over `Quest --HAS_STEP--> QuestStep --LOCATED_IN--> Region --GATED_BY--> UnlockCondition`, discharged by `Quest --REQUIRES|UNLOCKS--> UnlockCondition`.

- [x] **Step 1: Add failing graph tests for self-requirement, bounded transitions, and access gates**

```python
def test_self_requirement_is_a_dependency_cycle():
    q = Entity(id="quest:q", type=NodeType.QUEST)
    rel = Relation(id="requires", type=EdgeType.REQUIRES, src_id=q.id, dst_id=q.id)
    assert len(_findings([q], [rel], "cyclic_dependency")) == 1


def test_once_only_transition_does_not_form_repeatable_cycle():
    rels = [
        Relation(id="a", type=EdgeType.PRECEDES, src_id="d:a", dst_id="d:b"),
        Relation(
            id="b", type=EdgeType.PRECEDES, src_id="d:b", dst_id="d:a",
            attrs={"repeatability": "once"},
        ),
    ]
    assert _findings(dialogue_nodes(), rels, "cyclic_dependency") == []


def test_gated_destination_requires_prior_access_or_quest_unlock():
    entities, relations = gated_quest()
    assert len(_findings(entities, relations, "unreachable_target")) == 1
    for edge_type in (EdgeType.REQUIRES, EdgeType.UNLOCKS):
        proof = Relation(id=edge_type.value, type=edge_type, src_id="quest:q", dst_id="gate:x")
        assert _findings(entities, relations + [proof], "unreachable_target") == []
```

- [x] **Step 2: Run graph tests and verify RED**

Run: `uv run pytest tests/spine/checkers/test_graph.py -q`

Expected: self-requirement and access-gate assertions fail; bounded edge still forms a cycle.

- [x] **Step 3: Implement the generic graph behavior**

Set `_DEPENDENCY_EDGES = (HAS_STEP, PRECEDES, REQUIRES)`. `_dependency_adj()` excludes only relations with `attrs.get("repeatability") == "once"`; no other source convention is recognized.

Add `_gated_destination()` to `GraphChecker.check()` after nav reachability. For each quest step `LOCATED_IN` a region, collect the region's outgoing `GATED_BY` conditions. Emit `unreachable_target` for any gate not matched by the quest's outgoing `REQUIRES` or `UNLOCKS`. Evidence contains quest, step, region, gate, and `access_proofs=[]`. This rule is source-neutral and runs without a `NavProvider` because the gate graph itself is the deterministic reachability proof.

- [x] **Step 4: Add failing ASP differential examples and property generation**

Extend random dependency edges to include `REQUIRES` and relation attrs `repeatability=None|once`. Add fixed examples for a self-loop and a two-node cycle with one once-only edge. The shared finding sets must still agree.

Run: `uv run pytest tests/spine/checkers/test_asp.py tests/spine/checkers/test_asp_vs_graph_differential.py -q`

Expected: ASP disagrees because it lacks `REQUIRES` and edge attrs.

- [x] **Step 5: Extend the independent ASP encoding**

Emit scalar relation attrs as `edge_attr(RelationId, Key, Value)`. Replace dependency rules with:

```prolog
dependency_type("HAS_STEP").
dependency_type("PRECEDES").
dependency_type("REQUIRES").
bounded(R) :- edge_attr(R, "repeatability", "once").
dep_edge(X,Y) :- edge(R,T,X,Y), dependency_type(T), not bounded(R).
```

Do not call GraphChecker. Update the module contract comments and atom budget estimate for relation attrs.

- [x] **Step 6: Run graph/ASP tests and verify GREEN**

Run: `uv run pytest tests/spine/checkers/test_graph.py tests/spine/checkers/test_asp.py tests/spine/checkers/test_asp_vs_graph_differential.py -q`

Expected: all examples and 200 property cases pass.

- [x] **Step 7: Commit the generic checker improvement**

```bash
git add gameforge/spine/checkers/graph.py gameforge/spine/checkers/asp.py tests/spine/checkers/test_graph.py tests/spine/checkers/test_asp.py tests/spine/checkers/test_asp_vs_graph_differential.py
git diff --cached --check
git commit -m "feat(checkers): support gated and bounded dependencies"
```

---

### Task 6: Endless Sky Adapter and Raw Preservation

**Files:**
- Create: `gameforge/spine/ingestion/endless_sky_adapter.py`
- Create: `tests/spine/ingestion/test_endless_sky_adapter.py`
- Create: `tests/spine/ingestion/test_endless_sky_adapter_roundtrip.py`

**Interfaces:**
- Consumes: `EndlessSkyTree`, source-neutral values translated into `EndlessSkyTarget(path, record_kind, record_name)`, and `EndlessSkyContext(resources, restricted_destinations)`.
- Produces: `EndlessSkyTxtAdapter.to_ir(...) -> Snapshot`, `from_ir(snapshot) -> dict[str, bytes]`, `quest_id(name)`, `dialogue_label_id(quest, label)`, and version `endless-sky-adapter@1`.

- [ ] **Step 1: Write failing raw-preservation and base mapping tests**

```python
def test_adapter_round_trips_unknown_records_and_exact_file_bytes():
    tree = read_source_tree({"data/mixed.txt": MIXED_BYTES})
    snapshot = EndlessSkyTxtAdapter().to_ir(tree, targets=(), context=EMPTY_CONTEXT)
    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {"data/mixed.txt": MIXED_BYTES}


def test_mission_maps_to_generic_quest_start_step_destination_and_gate():
    snapshot = adapt(MISSION_WITH_CLEARANCE, target("mission", "Deliver"), restricted=("Mars",))
    graph = snapshot.to_graph()
    assert graph.get_node(quest_id("Deliver")).type is NodeType.QUEST
    assert one_edge(graph, EdgeType.STARTS_AT).src_id == quest_id("Deliver")
    assert one_edge(graph, EdgeType.HAS_STEP).src_id == quest_id("Deliver")
    assert one_edge(graph, EdgeType.LOCATED_IN).dst_id == region_id("Mars")
    assert one_edge(graph, EdgeType.GATED_BY).src_id == region_id("Mars")
    assert one_edge(graph, EdgeType.UNLOCKS).src_id == quest_id("Deliver")
```

- [ ] **Step 2: Run Adapter tests and verify RED**

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_adapter.py -q`

Expected: missing Adapter module.

- [ ] **Step 3: Implement lossless raw envelopes and generic mission mapping**

Every top-level chunk produces exactly one raw-holder entity with these attrs:

```python
{
    "source_kind": chunk.kind,
    "source_name": chunk.name,
    "source_order": chunk.index,
    "source_chunk_b64": base64.b64encode(chunk.raw).decode("ascii"),
    "reader_version": READER_VERSION,
}
```

Selected `mission` records use `NodeType.QUEST`; unselected/unknown chunks use `NodeType.EVENT` and IDs derived from `sha256(path + row)` so they cannot collide. Empty files receive one raw-holder Event. `from_ir()` uses only raw-holder attrs, groups by `SourceRef.file`, sorts `source_order`, decodes strict base64, rejects duplicate order or version mismatch, and concatenates chunks.

For a selected mission:

- create one lifecycle `QUEST_STEP` and `HAS_STEP`;
- create `STARTS_AT` for explicit `source` or offer triggers `landing|job|assisting|boarding|entering|spaceport`;
- create destination `REGION` and `QuestStep --LOCATED_IN--> Region`;
- create `Region --GATED_BY--> UnlockCondition` for context-declared restricted destinations;
- map `clearance` to `Quest --UNLOCKS--> UnlockCondition`;
- map `to offer/has "landing access: X"` to `Quest --REQUIRES--> UnlockCondition`;
- map mission-state `has "Name: offered|done"` to `Quest --REQUIRES--> Quest`, declaring the referenced mission from the same source tree as a real dependency entity with its own start/lifecycle mapping;
- add no source-specific checker flag or case name.

- [ ] **Step 4: Add fixture-wide failing round-trip tests, then make them pass**

```python
@pytest.mark.parametrize("case,side", all_case_sides())
def test_every_external_tree_round_trips_byte_exact(case, side):
    tree = load_case_tree(case, side)
    snapshot = adapt_case(tree, case)
    assert EndlessSkyTxtAdapter().from_ir(snapshot) == render_source_tree(tree)
```

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_adapter_roundtrip.py -q`

Expected: all unit and 16 fixture-side tests pass.

- [ ] **Step 5: Commit base Adapter mapping**

```bash
git add gameforge/spine/ingestion/endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_adapter_roundtrip.py
git diff --cached --check
git commit -m "feat(ingestion): map Endless Sky missions into generic IR"
```

---

### Task 7: Dialogue References and Repeatable Control Flow

**Files:**
- Modify: `gameforge/spine/ingestion/endless_sky_adapter.py`
- Modify: `tests/spine/ingestion/test_endless_sky_adapter.py`
- Create: `tests/spine/ingestion/test_endless_sky_dialogue_mapping.py`

**Interfaces:**
- Consumes: selected mission `conversation` token trees.
- Produces: `DIALOGUE_NODE` label nodes, `PRECEDES` control edges, ordinary dangling `REFERENCES` edges for unresolved control targets, and `repeatability="once"` only when a monotonic guard is proven.

- [ ] **Step 1: Write failing source-agnostic dialogue shape tests**

```python
def test_missing_choice_merge_becomes_an_ordinary_dangling_relation():
    snapshot = adapt_dialogue(CHOICE_BRANCH_COLLISION)
    findings = GraphChecker().check(snapshot)
    assert one(findings, "dangling_reference").evidence["edge_type"] == "REFERENCES"


def test_explicit_merge_label_clears_the_dangling_relation():
    snapshot = adapt_dialogue(CHOICE_WITH_EXPLICIT_MERGE)
    assert by_class(GraphChecker().check(snapshot), "dangling_reference") == []


def test_monotonic_display_guard_marks_only_the_guarded_transition_once():
    snapshot = adapt_dialogue(TWO_LABEL_LOOP_WITH_GUARD_AND_SET)
    edges = [r for r in snapshot.relations.values() if r.type is EdgeType.PRECEDES]
    assert sum(r.attrs == {"repeatability": "once"} for r in edges) == 1
    assert by_class(GraphChecker().check(snapshot), "cyclic_dependency") == []
```

- [ ] **Step 2: Run dialogue tests and verify RED**

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_dialogue_mapping.py -q`

Expected: no dialogue relations exist.

- [ ] **Step 3: Implement structural control-flow mapping without object-name branches**

Within each conversation:

1. Create an entry DialogueNode plus one node per `label`.
2. Associate each `choice` with the nearest preceding label or entry.
3. An explicit descendant `goto X` adds `PRECEDES(source, label:X)`; absent `label:X` naturally leaves a dangling endpoint.
4. For an option without a terminal, scan subsequent siblings until the next label or terminal goto. If the path falls into a label explicitly targeted by a different option from the same choice, emit `REFERENCES(option, unresolved:<stable-source-span>)` to a deliberately absent endpoint. This represents a missing merge reference in generic graph form.
5. A transition is `repeatability="once"` only when its option/path contains `to display/not FLAG` and the target label block contains `action/set FLAG` before the next label. Guards that do not match a target set remain repeatable.

Stable IDs derive from quest ID, label text, and source span. Relation evidence retains `SourceRef` to the goto/choice line.

- [ ] **Step 4: Verify development cases before implementing verification-specific assertions**

Run the two development fixtures only:

```bash
uv run pytest tests/spine/ingestion/test_endless_sky_dialogue_mapping.py \
  -k 'generic or development' -q
```

Expected: the development conversation-loop before snapshot is cyclic and its after snapshot is clear; the sound case is handled by the same generic reference mapping described below.

Map selected `effect` records and child `sound NAME` into `EFFECT --REFERENCES--> EFFECT(resource_kind="sound")`. Context-declared existing resources become entities; unknown resource names remain missing endpoints. This makes the development sound case an ordinary dangling reference.

- [ ] **Step 5: Run all dialogue/reference Adapter tests and verify GREEN**

Run: `uv run pytest tests/spine/ingestion/test_endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_dialogue_mapping.py -q`

Expected: all tests pass, including generic variants with different quest/label/flag names.

- [ ] **Step 6: Commit dialogue mapping**

```bash
git add gameforge/spine/ingestion/endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_adapter.py tests/spine/ingestion/test_endless_sky_dialogue_mapping.py
git diff --cached --check
git commit -m "feat(ingestion): map dialogue references and bounded loops"
```

---

### Task 8: Independent Endless Sky Qualification Predicates

**Files:**
- Create: `gameforge/bench/external_cases/endless_sky_predicates.py`
- Create: `tests/bench/external_cases/test_endless_sky_predicates.py`

**Interfaces:**
- Consumes: raw `EndlessSkyTree`, `ExternalCaseSpec`, and `context.json`; it does not consume Adapter output or GameForge findings.
- Produces: `evaluate_predicate(predicate_id, tree, targets, context) -> PredicateEvidence` for `reference_resolves`, `dependency_acyclic`, `target_reachable`, and `mission_offerable`.

- [ ] **Step 1: Write failing predicate tests using synthetic names**

```python
@pytest.mark.parametrize(
    ("predicate_id", "before", "after"),
    [
        ("reference_resolves", BAD_REFERENCE, GOOD_REFERENCE),
        ("dependency_acyclic", SELF_REQUIRE, PRIOR_REQUIRE),
        ("target_reachable", MISSING_ACCESS, ACCESS_PROVED),
        ("mission_offerable", MISSING_SOURCE, SOURCE_PRESENT),
    ],
)
def test_predicate_transitions_violation_to_clear(predicate_id, before, after):
    assert evaluate(before, predicate_id).status == "violation"
    assert evaluate(after, predicate_id).status == "clear"
```

- [ ] **Step 2: Run predicate tests and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_endless_sky_predicates.py -q`

Expected: predicate module is missing.

- [ ] **Step 3: Implement predicates directly over token trees**

- `reference_resolves`: for target effect sounds, compare references with context resources; for conversation choices, independently trace implicit paths and require a non-colliding label/terminal.
- `dependency_acyclic`: build a source-native graph from mission-state conditions and conversation gotos; suppress a back-edge only when a matching `not FLAG`/target `set FLAG` pair proves one-shot traversal; run a local color DFS, not GraphChecker or Adapter helpers.
- `target_reachable`: for each target mission destination in `restricted_destinations`, require either a bare `clearance` child or `to offer` condition `has "landing access: <destination>"`.
- `mission_offerable`: require explicit `source` or one of the registered offer-trigger directives. Destination alone does not count.

Every predicate returns structured target/path/line evidence. A parse error or unknown predicate returns `unproven`, never `clear`.

- [ ] **Step 4: Run predicates against all frozen cases**

```python
@pytest.mark.parametrize("case", load_case_specs(CASE_SPECS))
def test_frozen_case_predicate_is_before_violation_after_clear(case):
    assert evaluate_case_side(case, "before").status == "violation"
    assert evaluate_case_side(case, "after").status == "clear"
```

Run: `uv run pytest tests/bench/external_cases/test_endless_sky_predicates.py -q`

Expected: all synthetic and eight frozen transitions pass.

- [ ] **Step 5: Commit independent predicates**

```bash
git add gameforge/bench/external_cases/endless_sky_predicates.py tests/bench/external_cases/test_endless_sky_predicates.py
git diff --cached --check
git commit -m "feat(bench): qualify Endless Sky cases with independent predicates"
```

---

### Task 9: Qualification Runner, Scoring, and Frozen Evidence Manifest

**Files:**
- Create: `gameforge/bench/external_cases/qualify.py`
- Create: `gameforge/bench/external_cases/endless_sky_runner.py`
- Generate: `scenarios/external_cases/endless_sky/external-corpus-manifest.json`
- Create: `tests/bench/external_cases/test_qualify.py`
- Create: `tests/bench/external_cases/test_endless_sky_evidence_replay.py`

**Interfaces:**
- Consumes: case specs, verified raw trees, native evidence, predicates, Adapter snapshots, Graph/ASP findings, mapping-spec hash, and upstream patches.
- Produces: eight `ExternalCaseEvidence` rows, per-class development/verification metrics, after-clean FP evidence, and a self-hashed `ExternalCorpusManifest`.

- [ ] **Step 1: Write failing denominator and fail-closed qualification tests**

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "native_before_failed",
        "predicate_before_clear",
        "predicate_after_violation",
        "checker_before_miss",
        "checker_after_hit",
        "mapping_hash_changed",
    ],
)
def test_any_required_evidence_failure_remains_a_scored_miss(mutation):
    evidence = qualify_case(**mutated_inputs(mutation))
    assert evidence.qualification_status == "miss"
    assert evidence.failure_reasons


def test_verification_scorer_keeps_all_frozen_cases_in_denominator():
    score = score_external_cases(one_hit_one_miss_per_class())
    assert {(m.defect_class, m.n) for m in score.verification} == {
        (name, 1) for name in FOUR_CLASSES
    }
```

- [ ] **Step 2: Run qualifier tests and verify RED**

Run: `uv run pytest tests/bench/external_cases/test_qualify.py -q`

Expected: qualifier module is missing.

- [ ] **Step 3: Implement generic qualification and Wilson scoring**

`qualify_case()` is source-neutral. It requires:

- both native results exit zero;
- predicate before=`violation`, after=`clear`;
- reader/Adapter/mapping versions match the corpus registration;
- at least one confirmed before Finding of the expected class intersects the Adapter-resolved target entity IDs;
- no confirmed or unproven after Finding of that class intersects target IDs;
- no deterministic/unproven finding at all on the after scoped snapshot;
- upstream patch digest matches `HumanTarget`.

Every failed condition is appended to `failure_reasons`; it never raises a row out of the denominator. `score_external_cases()` separates development and verification, reports `k/n/rate/Wilson95` per class, and reports after-clean FP as `after snapshots with any finding / all after snapshots`.

- [ ] **Step 4: Implement the source-bound runner and generate evidence**

The runner translates target locators to `EndlessSkyTarget`, loads source context, invokes both readers, the Adapter, GraphChecker, ASPChecker for cycle differential evidence, independent predicates, and the compiled native witness. It canonicalizes full Finding evidence, writes a temporary manifest, immediately reloads/revalidates it, then atomically replaces the committed manifest.

Run:

```bash
uv run python -m gameforge.bench.external_cases.endless_sky_runner \
  --corpus scenarios/external_cases/endless_sky
```

Expected: 8/8 qualified; four development rows excluded from the headline; four verification rows report one case each; after external oracle-FP is 0/8.

- [ ] **Step 5: Prove offline evidence replay is byte-identical**

```python
def test_evidence_replay_is_byte_identical(tmp_path):
    first = replay_corpus(CORPUS, tmp_path / "one")
    second = replay_corpus(CORPUS, tmp_path / "two")
    expected = (CORPUS / "external-corpus-manifest.json").read_bytes()
    assert first == second == expected
```

Run: `uv run pytest tests/bench/external_cases/test_qualify.py tests/bench/external_cases/test_endless_sky_evidence_replay.py -q`

Expected: all tests pass without network or LLM calls.

- [ ] **Step 6: Commit the qualification result**

```bash
git add gameforge/bench/external_cases/qualify.py gameforge/bench/external_cases/endless_sky_runner.py scenarios/external_cases/endless_sky/external-corpus-manifest.json tests/bench/external_cases/test_qualify.py tests/bench/external_cases/test_endless_sky_evidence_replay.py
git diff --cached --check
git commit -m "feat(bench): qualify real external defects before and after fixes"
```

---

### Task 10: Anti-Specialization, Legacy Replay, and Slice Acceptance

**Files:**
- Modify: `tests/bench/external_corpus/test_anti_specialization.py`
- Create: `tests/bench/external_cases/test_anti_specialization.py`
- Create: `tests/bench/external_cases/test_external_cases_acceptance.py`
- Modify: `docs/superpowers/plans/README.md`
- Modify: `docs/superpowers/plans/2026-07-12-pre-m4-external-cases-adapter.md`

**Interfaces:**
- Consumes: all prior tasks and the frozen legacy B0A/Flare tests.
- Produces: a machine gate for this slice and a status anchor that does not claim narrative/HED/QA/Report v2 completion.

- [ ] **Step 1: Add failing AST/text anti-specialization tests**

Scan `gameforge/contracts`, `gameforge/spine/checkers`, `gameforge/spine/dsl`, `gameforge/spine/sim`, `gameforge/bench/taxonomy.py`, `metrics.py`, `report.py`, `power.py`, and `external_cases/qualify.py`. Reject:

- imports from Endless Sky reader/Adapter/predicates/profile modules;
- literals `endless_sky`, any frozen 8-character OID prefix, any frozen object name, or a `data/...` source path;
- `if`/`match` branches comparing a generic defect class to a source ID.

The source-specific reader, Adapter, fixture builder, predicates, and runner are explicitly outside this scan.

- [ ] **Step 2: Run anti-specialization tests and verify RED on a temporary probe**

The test creates a temporary copied core module containing `if source_id == "endless_sky":` and proves the scanner reports it; then scans the real core and expects no violations.

Run: `uv run pytest tests/bench/external_cases/test_anti_specialization.py -q`

Expected: the probe is rejected and the real core is clean after scanner implementation.

- [ ] **Step 3: Add the complete slice acceptance test**

Acceptance requires:

```python
def test_external_case_slice_acceptance():
    manifest = load_manifest(MANIFEST)
    assert len(manifest.cases) == 8
    assert sum(c.spec.split == "verification" for c in manifest.cases) == 4
    assert {c.spec.defect_class.value for c in manifest.cases} == FOUR_CLASSES
    assert all(c.qualification_status == "qualified" for c in manifest.cases)
    assert all(c.predicate_before.status == "violation" for c in manifest.cases)
    assert all(c.predicate_after.status == "clear" for c in manifest.cases)
    assert all(not c.findings_after for c in manifest.cases)
    assert manifest.after_oracle_fp.count == 0
```

Also assert:

- all 16 reader round trips are byte exact;
- all case/mapping/native/evidence hashes validate;
- no `agent_patch_sha256` or HED result is fabricated in this slice;
- legacy `scenarios/flare_corpus/**` bytes match their frozen Git anchor;
- legacy Endless Sky B0A still derives `awaiting_human_evidence` and is not imported by the new runner.

- [ ] **Step 4: Run focused and legacy regressions**

Run:

```bash
uv run pytest tests/bench/external_cases tests/bench/external_corpus \
  tests/bench/test_flare_evidence.py tests/bench/test_flare_adjudication.py \
  tests/spine/ingestion tests/spine/checkers -q
```

Expected: all tests pass; no live network; the historical B0A status remains unchanged while the new external-case manifest is qualified.

- [ ] **Step 5: Run repository-wide verification**

Run:

```bash
uv run pytest -q
uv run lint-imports
uv run ruff check gameforge tests
git diff --check
```

Expected: full pytest passes (the pre-existing platform-dependent skip may remain), all 7 import contracts are kept, Ruff is clean, and no whitespace errors exist.

- [ ] **Step 6: Update status without starting M4**

Mark this plan complete in `docs/superpowers/plans/README.md`. State the measured result, exact test counts, four classes, 8/8 denominator, verification split, after FP, and native/reader replay. Keep narrative evidence, HED, QA-hours, BenchReport v2, and final pre-M4 audit explicitly pending.

- [ ] **Step 7: Commit slice acceptance**

```bash
git add tests/bench/external_corpus/test_anti_specialization.py tests/bench/external_cases/test_anti_specialization.py tests/bench/external_cases/test_external_cases_acceptance.py docs/superpowers/plans/README.md docs/superpowers/plans/2026-07-12-pre-m4-external-cases-adapter.md
git diff --cached --check
git commit -m "test(bench): accept the real external defect slice"
```

## Self-Review

- Spec coverage: the plan implements lean design §§3.1–3.5 and machine-acceptance items for external cases; narrative, HED, QA, cost/latency, and BenchReport v2 remain separate later plans by design.
- No approval/attestation expansion: the new path has no reviewer, nonce, blind-state, assignment, or approval payload type.
- Type consistency: specs carry `target_locators`; the Adapter consumes translated `EndlessSkyTarget`; predicates consume the original locators; qualification stores resolved target entity IDs only in evidence.
- Source boundaries: only reader/Adapter/fixture/native/predicate/runner know Endless Sky; generic contracts/tree/qualifier/checkers do not.
- Soundness: before/after facts are established independently by raw-tree predicate and GameForge checker; ASP differential remains independent for dependency cycles.
- Denominator integrity: all eight frozen cases remain in evidence even when any parser, predicate, Adapter, checker, or native step fails.
- Raw preservation: all changed config bytes are exact upstream blobs and every one of 16 source trees has reader and Adapter byte-round-trip coverage.
- No hidden M4 work: no frontend, RBAC, object storage, WORM, multi-tenant security, or generic plugin platform is introduced.
