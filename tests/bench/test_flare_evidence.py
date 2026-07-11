import copy
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import gameforge.bench.flare_evidence as flare_evidence
from gameforge.bench.flare_evidence import (
    B0A_DEFECT_CLASSES,
    ApplicabilityDeclaration,
    ApplicabilityRow,
    CandidateCase,
    CandidateCommit,
    CandidateDisposition,
    EvidenceCounts,
    EvidenceRef,
    FlareSearchSpec,
    RegexRule,
    ReviewAttestation,
    SearchRegistration,
    SearchRound,
    canonical_bytes,
    posix_glob_matches,
    put_blob,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)


_GIT_EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _make_non_regular_path(path: Path, kind: str, data: bytes) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
        return None
    if kind == "fifo":
        os.mkfifo(path)
        return None

    external = path.parent.parent / f"external-{kind}-{path.name}"
    external.write_bytes(data)
    if kind == "symlink":
        path.symlink_to(external)
    else:  # pragma: no cover - parametrization is the closed input set
        raise AssertionError(f"unsupported non-regular path kind: {kind}")
    return external


def _assert_unsafe_kind(path: Path, kind: str) -> None:
    metadata = path.lstat()
    predicates = {
        "symlink": stat.S_ISLNK,
        "fifo": stat.S_ISFIFO,
        "directory": stat.S_ISDIR,
    }
    assert predicates[kind](metadata.st_mode)


def test_b0a_scope_is_exactly_the_eleven_non_narrative_classes():
    assert len(B0A_DEFECT_CLASSES) == 11
    assert {item.value for item in B0A_DEFECT_CLASSES} >= {
        "dead_quest",
        "missing_drop_source",
        "economy_collapse",
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
        b'{"accepted":0,"ambiguous":0,"proposed":0,"qualified_candidate":0,"rejected":0}\n'
    )
    target = tmp_path / "ledger.json"
    write_new_or_identical(target, b"same\n")
    write_new_or_identical(target, b"same\n")
    with pytest.raises(FileExistsError):
        write_new_or_identical(target, b"different\n")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_writer_rejects_non_regular_existing_target_without_blocking(
    tmp_path: Path, kind: str
):
    payload = b"immutable-ledger\n"
    target = tmp_path / "candidate-ledger.json"
    external = _make_non_regular_path(target, kind, payload)
    script = r"""
import sys
from pathlib import Path

from gameforge.bench.flare_evidence import write_new_or_identical

try:
    write_new_or_identical(Path(sys.argv[1]), sys.argv[2].encode("ascii"))
except OSError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(target), payload.decode("ascii")],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"writer blocked on existing {kind}")

    assert completed.returncode == 0, completed.stderr.decode()
    _assert_unsafe_kind(target, kind)
    if external is not None:
        assert external.read_bytes() == payload


def test_writer_uses_portable_standard_library_replace(tmp_path: Path, monkeypatch):
    target = tmp_path / "candidate-ledger.json"
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)
    write_new_or_identical(target, b"complete-ledger\n")

    assert len(calls) == 1
    staging, destination = calls[0]
    assert staging.parent == destination.parent == tmp_path
    assert destination == target
    assert not staging.exists()
    assert target.read_bytes() == b"complete-ledger\n"


def test_single_output_abrupt_exit_never_publishes_partial_bytes_and_retry_succeeds(
    tmp_path: Path,
):
    target = tmp_path / "candidate-ledger.discovered.json"
    payload = b"complete-discovery-ledger\n"
    script = r"""
import os
import sys
from pathlib import Path

import gameforge.bench.flare_evidence as flare_evidence
from gameforge.bench.flare_evidence import write_new_or_identical

target = Path(sys.argv[1])
payload = sys.argv[2].encode("ascii")
real_write = os.write


def exit_after_partial_write(descriptor, data):
    real_write(descriptor, data[:7])
    os.fsync(descriptor)
    os._exit(74)


flare_evidence.os.write = exit_after_partial_write
write_new_or_identical(target, payload)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(target), payload.decode("ascii")],
        check=False,
    )

    assert completed.returncode == 74
    assert not target.exists()
    staging_files = list(tmp_path.glob(".gameforge-*.tmp"))
    assert len(staging_files) == 1
    assert staging_files[0].read_bytes() == payload[:7]

    write_new_or_identical(target, payload)
    assert target.read_bytes() == payload


def test_multi_output_retry_after_publish_exit_reuses_complete_prefix(tmp_path: Path):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    script = r"""
import os
import sys
from pathlib import Path

from gameforge.bench.flare_evidence import write_set_new_or_identical

ledger = Path(sys.argv[1])
decision = Path(sys.argv[2])
real_replace = os.replace


def exit_after_first_publish(source, target):
    real_replace(source, target)
    if Path(target) == ledger:
        os._exit(75)


os.replace = exit_after_first_publish
write_set_new_or_identical(
    {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(ledger), str(decision)],
        check=False,
    )

    assert completed.returncode == 75
    assert ledger.read_bytes() == b"complete-ledger\n"
    assert not decision.exists()
    stale_residue = list(tmp_path.glob(".gameforge-*.tmp"))
    assert len(stale_residue) == 1
    assert stale_residue[0].read_bytes() == b"complete-decision\n"

    write_set_new_or_identical(
        {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
    )
    assert ledger.read_bytes() == b"complete-ledger\n"
    assert decision.read_bytes() == b"complete-decision\n"
    assert stale_residue[0].exists()



def test_multi_output_publish_preflights_all_targets(tmp_path: Path):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    second.write_bytes(b"existing-different\n")
    with pytest.raises(FileExistsError):
        write_set_new_or_identical({first: b"new-ledger\n", second: b"new-decision\n"})
    assert not first.exists()
    assert second.read_bytes() == b"existing-different\n"


def test_multi_output_writer_does_not_resolve_filesystem_aliases(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"

    def unexpected_alias_probe(*_args, **_kwargs):
        raise AssertionError("filesystem aliases are outside the writer contract")

    monkeypatch.setattr(Path, "resolve", unexpected_alias_probe)
    monkeypatch.setattr(Path, "samefile", unexpected_alias_probe)

    write_set_new_or_identical({first: b"ledger\n", second: b"decision\n"})

    assert first.read_bytes() == b"ledger\n"
    assert second.read_bytes() == b"decision\n"


def test_multi_output_stages_all_files_before_publishing(tmp_path: Path, monkeypatch):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    real_create = flare_evidence._create_staging_file
    real_replace = os.replace
    staging_creates = 0
    published: list[Path] = []

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
    assert list(tmp_path.glob(".gameforge-*.tmp")) == []


def test_multi_output_publish_never_modifies_preexisting_identical_file(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    first.write_bytes(b"identical-ledger\n")

    def fail_staging(_target):
        raise OSError("injected staging failure")

    monkeypatch.setattr(flare_evidence, "_create_staging_file", fail_staging)
    with pytest.raises(OSError, match="staging failure"):
        write_set_new_or_identical(
            {first: b"identical-ledger\n", second: b"new-decision\n"}
        )

    assert first.read_bytes() == b"identical-ledger\n"
    assert not second.exists()


def test_multi_output_abrupt_exit_never_publishes_partial_completion_marker(
    tmp_path: Path,
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    script = r"""
import os
import sys
from pathlib import Path

import gameforge.bench.flare_evidence as flare_evidence
from gameforge.bench.flare_evidence import write_set_new_or_identical

ledger = Path(sys.argv[1])
decision = Path(sys.argv[2])
real_write = os.write
state = {"writes": 0}


def exit_during_second_write(descriptor, data):
    state["writes"] += 1
    if state["writes"] == 2:
        os._exit(73)
    return real_write(descriptor, data)


flare_evidence.os.write = exit_during_second_write
write_set_new_or_identical(
    {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(ledger), str(decision)],
        check=False,
    )

    assert completed.returncode == 73
    assert not ledger.exists()
    assert not decision.exists()
    staging_files = list(tmp_path.glob(".gameforge-*.tmp"))
    assert len(staging_files) == 2
    assert sorted(item.read_bytes() for item in staging_files) == [
        b"",
        b"complete-ledger\n",
    ]


def test_multi_output_publish_failure_retains_complete_prefix_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch,
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    real_replace = os.replace

    def fail_decision_publish(source, target):
        if Path(target) == decision:
            raise OSError("injected decision publish failure")
        return real_replace(source, target)

    with monkeypatch.context() as context:
        context.setattr(os, "replace", fail_decision_publish)
        with pytest.raises(OSError, match="decision publish"):
            write_set_new_or_identical(
                {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
            )

    assert ledger.read_bytes() == b"complete-ledger\n"
    assert not decision.exists()
    assert list(tmp_path.glob(".gameforge-*.tmp")) == []

    write_set_new_or_identical(
        {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
    )
    assert decision.read_bytes() == b"complete-decision\n"


def test_multi_output_fsyncs_every_staging_file_before_first_publish(
    tmp_path: Path,
    monkeypatch,
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    real_fsync = os.fsync
    real_replace = os.replace
    events: list[str] = []

    def record_fsync(descriptor):
        events.append("fsync:file")
        return real_fsync(descriptor)

    def record_publish(source, target):
        events.append(f"publish:{Path(target).name}")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_publish)
    write_set_new_or_identical(
        {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
    )

    assert events == [
        "fsync:file",
        "fsync:file",
        f"publish:{ledger.name}",
        f"publish:{decision.name}",
    ]


def test_decision_is_not_published_if_ledger_prefix_no_longer_matches(
    tmp_path: Path,
    monkeypatch,
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    real_replace = os.replace

    def corrupt_after_ledger_publish(source, target):
        result = real_replace(source, target)
        if Path(target) == ledger:
            ledger.write_bytes(b"corrupt-ledger\n")
        return result

    monkeypatch.setattr(os, "replace", corrupt_after_ledger_publish)
    with pytest.raises(FileExistsError, match="published target.*different bytes"):
        write_set_new_or_identical(
            {ledger: b"complete-ledger\n", decision: b"complete-decision\n"}
        )

    assert ledger.read_bytes() == b"corrupt-ledger\n"
    assert not decision.exists()


def test_publish_failure_cleans_only_staging_created_by_this_call(
    tmp_path: Path,
    monkeypatch,
):
    target = tmp_path / "candidate-ledger.json"
    existing_residue = tmp_path / ".gameforge-existing.tmp"
    existing_residue.write_bytes(b"older interrupted run\n")

    def fail_publish(_source, _target):
        raise OSError("injected publish failure")

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failure"):
        write_new_or_identical(target, b"complete-ledger\n")

    assert existing_residue.read_bytes() == b"older interrupted run\n"
    assert list(tmp_path.glob(".gameforge-*.tmp")) == [existing_residue]



def test_corpus_packaging_ignores_abrupt_exit_staging_files():
    repository_root = Path(__file__).resolve().parents[2]
    staging_path = "scenarios/flare_corpus/.gameforge-deadbeef.tmp"
    corpus_path = "scenarios/flare_corpus/b0a-decision.json"

    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", staging_path],
        cwd=repository_root,
        check=False,
    )
    canonical = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", corpus_path],
        cwd=repository_root,
        check=False,
    )

    assert ignored.returncode == 0
    assert canonical.returncode == 1


def test_blob_store_uses_lowercase_hex_and_verifies_existing_content(tmp_path: Path):
    digest, relative = put_blob(tmp_path / "blobs", b"patch bytes")
    assert len(digest) == 64 and digest == digest.lower()
    assert relative == f"blobs/{digest}"
    assert (tmp_path / relative).read_bytes() == b"patch bytes"
    assert put_blob(tmp_path / "blobs", b"patch bytes") == (digest, relative)
    (tmp_path / relative).write_bytes(b"tampered bytes")
    with pytest.raises(FileExistsError):
        put_blob(tmp_path / "blobs", b"patch bytes")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_blob_store_rejects_non_regular_existing_digest_without_blocking(
    tmp_path: Path, kind: str
):
    payload = b"patch bytes"
    digest = sha256_hex(payload)
    blob_dir = tmp_path / "blobs"
    target = blob_dir / digest
    external = _make_non_regular_path(target, kind, payload)
    script = r"""
import sys
from pathlib import Path

from gameforge.bench.flare_evidence import put_blob

try:
    put_blob(Path(sys.argv[1]), sys.argv[2].encode("ascii"))
except OSError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(blob_dir), payload.decode("ascii")],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"blob store blocked on existing {kind}")

    assert completed.returncode == 0, completed.stderr.decode()
    _assert_unsafe_kind(target, kind)
    if external is not None:
        assert external.read_bytes() == payload


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


def test_search_spec_requires_the_complete_frozen_contract(registered_search_spec_payload):
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


def test_canonical_root_commit_round_trips_when_selected_parent_is_omitted():
    root = CandidateCommit(
        commit_oid="a" * 40,
        parent_oids=[],
        selected_parent_oid=None,
        diff_base_oid=_GIT_EMPTY_TREE_OID,
        committed_at=1,
        subject="root",
    )
    assert CandidateCommit.model_validate_json(canonical_bytes(root)) == root

    with pytest.raises(ValidationError, match="empty-tree"):
        CandidateCommit(
            commit_oid="a" * 40,
            parent_oids=[],
            selected_parent_oid=None,
            diff_base_oid="b" * 40,
            committed_at=1,
            subject="root",
        )


def test_validated_git_environment_is_immutable(registered_search_spec_payload):
    spec = FlareSearchSpec.model_validate(registered_search_spec_payload)
    with pytest.raises(TypeError):
        spec.git_environment_policy.fixed["HOME"] = "/tmp"


def test_evidence_availability_is_derived_after_count_validation():
    row = ApplicabilityRow(
        defect_class="dead_quest",
        domain_applicability="applicable",
        evidence_counts={"proposed": "0"},
        implementation_support="planned",
    )
    assert row.evidence_availability == "not_found"


def test_diff_regex_must_compile_in_the_ascii_bytes_domain():
    with pytest.raises(ValidationError, match="bytes"):
        SearchRound(
            name="initial",
            message_regexes=[],
            diff_regexes=[RegexRule(rule_id="diff.bytes", pattern="(?u)a")],
        )
