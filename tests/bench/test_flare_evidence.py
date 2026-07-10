import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_multi_output_publish_rolls_back_only_new_files_after_late_failure(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    real_open = Path.open

    def fail_second(path, mode="r", *args, **kwargs):
        if path == second and mode == "xb":
            raise OSError("injected second-target failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="second-target"):
        write_set_new_or_identical({first: b"new-ledger\n", second: b"new-decision\n"})
    assert not first.exists()
    assert not second.exists()


def test_multi_output_publish_never_rolls_back_preexisting_identical_file(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    first.write_bytes(b"identical-ledger\n")
    real_open = Path.open

    def fail_second(path, mode="r", *args, **kwargs):
        if path == second and mode == "xb":
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


def test_canonical_root_commit_round_trips_when_selected_parent_is_omitted():
    root = CandidateCommit(
        commit_oid="a" * 40,
        parent_oids=[],
        selected_parent_oid=None,
        diff_base_oid="b" * 40,
        committed_at=1,
        subject="root",
    )
    assert CandidateCommit.model_validate_json(canonical_bytes(root)) == root


def test_validated_git_environment_is_immutable(registered_search_spec_payload):
    spec = FlareSearchSpec.model_validate(registered_search_spec_payload)
    with pytest.raises(TypeError):
        spec.git_environment_policy.fixed["HOME"] = "/tmp"


def test_multi_output_rollback_preserves_concurrent_replacement(tmp_path: Path, monkeypatch):
    first = tmp_path / "candidate-ledger.json"
    second = tmp_path / "b0a-decision.json"
    real_open = Path.open

    def replace_first_then_fail_second(path, mode="r", *args, **kwargs):
        if path == second and mode == "xb":
            first.unlink()
            with real_open(first, "xb") as stream:
                stream.write(b"concurrent replacement\n")
            raise OSError("injected second-target failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_first_then_fail_second)
    with pytest.raises(OSError, match="second-target"):
        write_set_new_or_identical({first: b"new-ledger\n", second: b"new-decision\n"})
    assert first.read_bytes() == b"concurrent replacement\n"
    assert not second.exists()


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
