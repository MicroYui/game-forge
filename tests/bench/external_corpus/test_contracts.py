from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.bench.external_corpus.contracts import (
    AdapterBinding,
    AdjudicationEvidence,
    AdjudicationPayload,
    ApplicabilityRow,
    B0AProtocol,
    B0ADecision,
    CandidateCommit,
    CandidateDisposition,
    CandidateFixGroup,
    CandidateGroupDecision,
    CandidateLedger,
    CandidateOrderTerm,
    CandidateCase,
    DiscoveryLedger,
    DiscoveryTool,
    DiffEvidence,
    DiscoveredCandidate,
    EvidenceArtifact,
    EvidenceCounts,
    EvidenceRef,
    GateSummary,
    GitCommandSpec,
    GitEnvironmentPolicy,
    HistoryRange,
    LineageLink,
    LineageResolution,
    NativeValidatorCommand,
    RegexRule,
    ReviewAttestation,
    ReviewPackage,
    SearchRegistration,
    SelectedParentEdge,
    SelectionReason,
    SourceProfile,
    TaxonomyApplicability,
    canonical_bytes,
    load_canonical,
    posix_glob_matches,
    put_blob,
    read_regular_file,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)
from gameforge.bench import flare_evidence
from gameforge.bench.taxonomy import DefectClass


def _taxonomy_applicability() -> tuple[TaxonomyApplicability, ...]:
    return tuple(
        TaxonomyApplicability(
            defect_class=defect_class,
            domain_applicability="applicable",
            implementation_support="planned",
            rationale=f"{defect_class.value} is represented in the fixture domain",
        )
        for defect_class in DefectClass
    )


def _source_profile() -> SourceProfile:
    return SourceProfile(
        schema_version="external-source-profile@1",
        source_id="fixture_source",
        profile_version="fixture-source@1",
        repository_url="https://example.test/fixture.git",
        pinned_head="1" * 40,
        history_range=HistoryRange(committed_at_gte=1, expected_commit_count=10),
        config_include_globs=("data/**/*.txt",),
        config_exclude_globs=(),
        message_rules=(RegexRule(rule_id="message.fix", pattern="(?i)fix"),),
        diff_rules=(),
        lineage_rules=(),
        candidate_order=(
            CandidateOrderTerm(field="committed_at", direction="descending"),
            CandidateOrderTerm(field="commit_oid", direction="ascending"),
        ),
        license_id="GPL-3.0-or-later",
        notice_files=("LICENSE",),
        native_validator_commands=(
            NativeValidatorCommand(
                command_id="fixture.parse",
                argv=("{engine_binary}", "--parse"),
                network="forbidden",
            ),
        ),
        parser_version="fixture-parser@1",
        query_complete_closure=("changed_files", "referenced_records"),
        taxonomy_applicability=_taxonomy_applicability(),
        qualification_predicate_ids=("reference_resolves",),
        b0a_protocol=B0AProtocol(
            candidate_limit=80,
            expected_matched_candidate_count=10,
            expected_config_only_candidate_count=9,
            minimum_independent_groups=8,
            minimum_domain_applicable_classes=4,
        ),
    )


def _candidate() -> DiscoveredCandidate:
    commit = CandidateCommit(
        commit_oid="1" * 40,
        parent_oids=["2" * 40],
        selected_parent_oid="2" * 40,
        diff_base_oid="2" * 40,
        committed_at=10,
        subject="Fix missing reference",
    )
    return DiscoveredCandidate(
        commit=commit,
        changed_paths=["data/a.txt"],
        eligible_paths=["data/a.txt"],
        config_only=True,
        selection_reasons=[SelectionReason(kind="direct_match", rule_ids=["message.fix"])],
        diff_evidence=DiffEvidence(
            commit_oid=commit.commit_oid,
            patch_sha256="3" * 64,
            patch_blob="blobs/" + "3" * 64,
            commit_message="Fix missing reference",
        ),
    )


def _discovery_ledger() -> DiscoveryLedger:
    profile_data = _source_profile().model_dump(mode="json")
    profile_data["b0a_protocol"]["expected_matched_candidate_count"] = 1
    profile_data["b0a_protocol"]["expected_config_only_candidate_count"] = 1
    profile = SourceProfile.model_validate(profile_data)
    profile_sha256 = sha256_hex(canonical_bytes(profile))
    candidate = _candidate()
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": [candidate.commit.commit_oid],
    }
    return DiscoveryLedger(
        schema_version="external-corpus-b0a@1",
        source_id=profile.source_id,
        source_profile=profile,
        source_profile_sha256=profile_sha256,
        search_registration=SearchRegistration(
            project_commit_oid="4" * 40,
            profile_repo_relative_path="scenarios/external/source-profile.json",
        ),
        observed_history_count=10,
        matched_candidate_count=1,
        config_only_candidate_count=1,
        discovery_tool=DiscoveryTool(
            tool_version="external-discovery@1",
            project_commit_oid="4" * 40,
            git_version="git version 2.50.0",
            python_implementation="CPython",
            python_version="3.12.0",
            python_build=("main", "Jul 11 2026"),
            unicode_version="15.1.0",
        ),
        discovered_candidates=[candidate],
        objective_lineage_links=[],
        candidate_universe_sha256=sha256_hex(canonical_bytes(universe)),
    )


def _adjudication_payload() -> AdjudicationPayload:
    discovery = _discovery_ledger()
    candidate = discovery.discovered_candidates[0]
    case = CandidateCase(
        case_id="case.dangling",
        defect_class=DefectClass.dangling_reference,
        disposition="proposed",
        rationale="The before state references a missing identifier fixed by this patch.",
        evidence_refs=[
            EvidenceRef(kind="commit_message", target_id=candidate.commit.commit_oid)
        ],
    )
    group = CandidateGroupDecision(
        fix_group_id="group.dangling",
        commits=[candidate.commit.commit_oid],
        selected_parent_edges=[
            SelectedParentEdge(
                commit_oid=candidate.commit.commit_oid,
                parent_oid=candidate.commit.diff_base_oid,
            )
        ],
        root_cause_evidence_refs=[
            EvidenceRef(kind="patch_blob", target_id=candidate.diff_evidence.patch_sha256)
        ],
        case_decisions=[case],
        adjudicator_id="agent-adjudicator",
        rationale="One commit fixes one independently evidenced root cause.",
    )
    return AdjudicationPayload(
        schema_version="external-corpus-b0a@1",
        source_id=discovery.source_id,
        evidence_revision="fixture-evidence@1",
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovery)),
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        source_artifacts=[],
        group_decisions=[group],
        candidate_decisions=[],
        lineage_resolutions=[],
    )


def test_source_profile_binds_discovery_and_future_qualification_surface() -> None:
    profile = _source_profile()

    assert profile.schema_version == "external-source-profile@1"
    assert profile.history_range.expected_commit_count == 10
    assert profile.candidate_order == (
        CandidateOrderTerm(field="committed_at", direction="descending"),
        CandidateOrderTerm(field="commit_oid", direction="ascending"),
    )
    assert profile.b0a_protocol.candidate_limit == 80
    assert profile.b0a_protocol.minimum_independent_groups == 8
    assert profile.b0a_protocol.minimum_domain_applicable_classes == 4
    assert profile.native_validator_commands[0].network == "forbidden"
    assert profile.query_complete_closure == ("changed_files", "referenced_records")
    assert profile.qualification_predicate_ids == ("reference_resolves",)


def test_source_profile_rejects_ambiguous_or_incomplete_registration() -> None:
    profile = _source_profile().model_dump(mode="json")

    both_bounds = {**profile}
    both_bounds["history_range"] = {
        **profile["history_range"],
        "after_exclusive_oid": "9" * 40,
    }
    with pytest.raises(ValidationError, match="only one lower bound"):
        SourceProfile.model_validate(both_bounds)

    duplicate_rule = {**profile, "diff_rules": [profile["message_rules"][0]]}
    with pytest.raises(ValidationError, match="globally unique"):
        SourceProfile.model_validate(duplicate_rule)

    wrong_order = {**profile, "candidate_order": list(reversed(profile["candidate_order"]))}
    with pytest.raises(ValidationError, match="committed_at followed by commit_oid"):
        SourceProfile.model_validate(wrong_order)

    missing_taxonomy = {
        **profile,
        "taxonomy_applicability": profile["taxonomy_applicability"][:-1],
    }
    with pytest.raises(ValidationError, match="every defect class"):
        SourceProfile.model_validate(missing_taxonomy)

    shell_command = {**profile}
    shell_command["native_validator_commands"] = [
        {
            "command_id": "fixture.parse",
            "argv": ["sh", "-c", "parse data"],
            "network": "forbidden",
        }
    ]
    with pytest.raises(ValidationError, match="must not invoke a shell"):
        SourceProfile.model_validate(shell_command)

    shell_fragment = {**profile}
    shell_fragment["native_validator_commands"] = [
        {
            "command_id": "fixture.parse",
            "argv": ["engine", "--parse;rm", "data"],
            "network": "forbidden",
        }
    ]
    with pytest.raises(ValidationError, match="shell fragments"):
        SourceProfile.model_validate(shell_fragment)


def test_source_profile_count_and_applicability_axes_are_independent() -> None:
    profile = _source_profile().model_dump(mode="json")
    profile["taxonomy_applicability"][0] = {
        **profile["taxonomy_applicability"][0],
        "domain_applicability": "not_applicable",
        "implementation_support": "implemented",
        "rationale": "The domain lacks this mechanic even though the generic checker exists.",
    }
    assert SourceProfile.model_validate(profile).taxonomy_applicability[0].implementation_support == (
        "implemented"
    )

    too_many_matches = _source_profile().model_dump(mode="json")
    too_many_matches["b0a_protocol"]["expected_matched_candidate_count"] = 11
    with pytest.raises(ValidationError, match="cannot exceed history count"):
        SourceProfile.model_validate(too_many_matches)

    too_many_config_only = _source_profile().model_dump(mode="json")
    too_many_config_only["b0a_protocol"]["expected_config_only_candidate_count"] = 11
    with pytest.raises(ValidationError, match="cannot exceed matched count"):
        SourceProfile.model_validate(too_many_config_only)

    no_lower_bound = _source_profile().model_dump(mode="json")
    no_lower_bound["history_range"]["committed_at_gte"] = None
    validated = SourceProfile.model_validate(no_lower_bound)
    assert validated.history_range.committed_at_gte is None
    assert validated.b0a_protocol.candidate_limit > validated.b0a_protocol.expected_matched_candidate_count


def test_adapter_binding_is_versioned_separately_from_discovery_profile() -> None:
    profile = _source_profile()

    assert "adapter_version" not in SourceProfile.model_fields
    binding = AdapterBinding(
        source_id=profile.source_id,
        reader_id="reader.fixture",
        reader_version="reader.fixture@1",
        adapter_format_id="fixture-data",
        adapter_version="adapter.fixture@1",
        ir_schema_version="ir-core@1",
        mapping_spec_sha256="0" * 64,
    )

    assert binding.source_id == profile.source_id
    assert binding.ir_schema_version == "ir-core@1"


def test_adapter_binding_rejects_non_version_identifier() -> None:
    try:
        AdapterBinding(
            source_id="fixture_source",
            reader_id="reader.fixture",
            reader_version="reader fixture 1",
            adapter_format_id="fixture-data",
            adapter_version="adapter.fixture@1",
            ir_schema_version="ir-core@1",
            mapping_spec_sha256="0" * 64,
        )
    except ValidationError as exc:
        assert "reader_version" in str(exc)
    else:
        raise AssertionError("invalid version identifier was accepted")


def test_review_attestation_requires_human_identity_and_utc_payload_binding() -> None:
    payload_hash = "a" * 64
    with pytest.raises(ValidationError, match="human"):
        ReviewAttestation(
            reviewer_kind="agent",
            reviewer_id="model-reviewer",
            reviewed_at="2026-07-11T00:00:00Z",
            written_statement="I reviewed the complete candidate assignment table.",
            candidate_universe_sha256="b" * 64,
            reviewed_payload_sha256=payload_hash,
        )

    attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="human-reviewer",
        reviewed_at="2026-07-11T00:00:00Z",
        written_statement="I reviewed and approve the complete candidate assignment table.",
        candidate_universe_sha256="b" * 64,
        reviewed_payload_sha256=payload_hash,
    )
    assert attestation.reviewed_at.isoformat() == "2026-07-11T00:00:00+00:00"

    with pytest.raises(ValidationError, match="UTC"):
        ReviewAttestation(
            reviewer_kind="human",
            reviewer_id="human-reviewer",
            reviewed_at="2026-07-11T08:00:00+08:00",
            written_statement="I reviewed and approve the complete candidate assignment table.",
            candidate_universe_sha256="b" * 64,
            reviewed_payload_sha256=payload_hash,
        )


def test_source_neutral_primitives_keep_strict_paths_hashes_and_extra_fields() -> None:
    registration = SearchRegistration(
        project_commit_oid="1" * 40,
        profile_repo_relative_path="scenarios/external/source-profile.json",
    )
    assert registration.profile_repo_relative_path.endswith(".json")

    with pytest.raises(ValidationError, match="repository-relative POSIX"):
        SearchRegistration.model_validate(
            {**registration.model_dump(), "profile_repo_relative_path": "../profile.json"}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SourceProfile.model_validate({**_source_profile().model_dump(), "adapter_version": "x@1"})
    with pytest.raises(ValidationError):
        EvidenceRef(kind="patch_blob", target_id="not-a-digest")
    with pytest.raises(ValidationError):
        DiffEvidence(
            commit_oid="1" * 40,
            patch_sha256="2" * 64,
            patch_blob="blobs/" + "3" * 64,
            commit_message="fix",
        )


def test_candidate_primitives_preserve_first_parent_and_path_invariants() -> None:
    commit = CandidateCommit(
        commit_oid="1" * 40,
        parent_oids=["2" * 40],
        selected_parent_oid="2" * 40,
        diff_base_oid="2" * 40,
        committed_at=1,
        subject="Fix data",
    )
    patch = DiffEvidence(
        commit_oid=commit.commit_oid,
        patch_sha256="3" * 64,
        patch_blob="blobs/" + "3" * 64,
        commit_message="Fix data",
    )
    candidate = DiscoveredCandidate(
        commit=commit,
        changed_paths=["data/a.txt"],
        eligible_paths=["data/a.txt"],
        config_only=True,
        selection_reasons=[SelectionReason(kind="direct_match", rule_ids=["message.fix"])],
        diff_evidence=patch,
    )
    assert candidate.diff_evidence.commit_oid == commit.commit_oid

    with pytest.raises(ValidationError, match="sorted and unique"):
        DiscoveredCandidate.model_validate(
            {**candidate.model_dump(), "changed_paths": ["data/z.txt", "data/a.txt"]}
        )
    with pytest.raises(ValidationError, match="first parent"):
        CandidateCommit(
            commit_oid="1" * 40,
            parent_oids=["2" * 40, "4" * 40],
            selected_parent_oid="4" * 40,
            diff_base_oid="4" * 40,
            committed_at=1,
            subject="merge",
        )
    with pytest.raises(ValidationError, match="endpoints must differ"):
        LineageLink(
            link_id="5" * 64,
            link_type="patch_id",
            source_oid="1" * 40,
            target_oid="1" * 40,
            patch_id="6" * 40,
        )

    link_payload = {
        "link_type": "patch_id",
        "source_oid": "1" * 40,
        "target_oid": "2" * 40,
        "patch_id": "6" * 40,
    }
    link = LineageLink(
        link_id=sha256_hex(canonical_bytes(link_payload)),
        **link_payload,
    )
    assert link.patch_id == "6" * 40
    with pytest.raises(ValidationError, match="link_id does not bind"):
        LineageLink(link_id="5" * 64, **link_payload)


def test_canonical_loader_and_immutable_publication_fail_closed(tmp_path: Path) -> None:
    profile = _source_profile()
    data = canonical_bytes(profile)
    digest = sha256_hex(data)
    path = tmp_path / "source-profile.json"

    assert data.endswith(b"\n")
    write_new_or_identical(path, data)
    write_new_or_identical(path, data)
    assert load_canonical(path, SourceProfile, digest) == profile

    with pytest.raises(FileExistsError, match="different bytes"):
        write_new_or_identical(path, b"{}\n")
    with pytest.raises(ValueError, match="digest"):
        load_canonical(path, SourceProfile, "0" * 64)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical"):
        load_canonical(noncanonical, SourceProfile, sha256_hex(noncanonical.read_bytes()))

    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError, match="regular file"):
        read_regular_file(link)
    with pytest.raises(FileExistsError, match="not reusable"):
        write_new_or_identical(link, data)

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="regular file"):
        read_regular_file(fifo)


def test_write_set_is_all_or_conflict_and_cas_verifies_existing_bytes(tmp_path: Path) -> None:
    conflict = tmp_path / "conflict"
    conflict.write_bytes(b"old")
    absent = tmp_path / "absent"
    with pytest.raises(FileExistsError, match="different bytes"):
        write_set_new_or_identical({absent: b"new", conflict: b"replacement"})
    assert not absent.exists()

    blob_dir = tmp_path / "blobs"
    digest, relative = put_blob(blob_dir, b"patch bytes")
    assert relative == f"blobs/{digest}"
    assert read_regular_file(blob_dir / digest) == b"patch bytes"
    (blob_dir / digest).write_bytes(b"tampered")
    with pytest.raises(FileExistsError, match="different bytes"):
        put_blob(blob_dir, b"patch bytes")


@pytest.mark.parametrize(
    ("path", "pattern", "matches"),
    [
        ("data/a.txt", "data/**/*.txt", True),
        ("data/nested/a.txt", "data/**/*.txt", True),
        ("src/a.py", "data/**/*.txt", False),
        ("../data/a.txt", "data/**/*.txt", False),
    ],
)
def test_posix_glob_contract(path: str, pattern: str, matches: bool) -> None:
    assert posix_glob_matches(path, pattern) is matches


def test_legacy_flare_entry_points_reexport_generic_primitives() -> None:
    assert flare_evidence.CandidateCommit is CandidateCommit
    assert flare_evidence.DiffEvidence is DiffEvidence
    assert flare_evidence.EvidenceArtifact is EvidenceArtifact
    assert flare_evidence.GitCommandSpec is GitCommandSpec
    assert flare_evidence.GitEnvironmentPolicy is GitEnvironmentPolicy
    assert flare_evidence.canonical_bytes is canonical_bytes
    assert flare_evidence.put_blob.__module__ == "gameforge.bench.flare_evidence"


def test_discovery_ledger_binds_profile_counts_order_and_candidate_universe() -> None:
    ledger = _discovery_ledger()

    assert ledger.source_profile_sha256 == sha256_hex(canonical_bytes(ledger.source_profile))
    assert ledger.observed_history_count == ledger.source_profile.history_range.expected_commit_count
    assert ledger.matched_candidate_count == 1
    assert ledger.config_only_candidate_count == 1
    assert len(ledger.discovered_candidates) == 1

    truncated = ledger.model_dump(mode="json")
    truncated["discovered_candidates"] = []
    truncated["candidate_universe_sha256"] = sha256_hex(
        canonical_bytes(
            {
                "source_id": ledger.source_id,
                "profile_sha256": ledger.source_profile_sha256,
                "ordered_candidate_oids": [],
            }
        )
    )
    with pytest.raises(ValidationError, match="registered candidate limit"):
        DiscoveryLedger.model_validate(truncated)

    invalid = ledger.model_dump(mode="json")
    invalid["candidate_universe_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="candidate_universe_sha256"):
        DiscoveryLedger.model_validate(invalid)

    invalid = ledger.model_dump(mode="json")
    invalid["source_profile_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="source_profile_sha256"):
        DiscoveryLedger.model_validate(invalid)


def test_generic_adjudication_requires_attestation_over_the_complete_payload() -> None:
    payload = _adjudication_payload()
    attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="human-reviewer",
        reviewed_at="2026-07-11T00:00:00Z",
        written_statement="I reviewed and approve the complete candidate assignment table.",
        candidate_universe_sha256=payload.candidate_universe_sha256,
        reviewed_payload_sha256=sha256_hex(canonical_bytes(payload)),
    )
    evidence = AdjudicationEvidence(
        **payload.model_dump(mode="json"),
        review_attestation=attestation,
    )
    assert evidence.review_attestation.reviewer_kind == "human"

    invalid = evidence.model_dump(mode="json")
    invalid["evidence_revision"] = "tampered@2"
    with pytest.raises(ValidationError, match="does not bind"):
        AdjudicationEvidence.model_validate(invalid)

    invalid = payload.model_dump(mode="json")
    invalid["group_decisions"][0]["adjudicator_id"] = "human-reviewer"
    rebound_payload = AdjudicationPayload.model_validate(invalid)
    invalid["review_attestation"] = {
        **attestation.model_dump(mode="json"),
        "reviewed_payload_sha256": sha256_hex(canonical_bytes(rebound_payload)),
    }
    with pytest.raises(ValidationError, match="reviewer must differ"):
        AdjudicationEvidence.model_validate(invalid)


def test_generic_b0a_models_preserve_assignment_and_gate_contract() -> None:
    discovery = _discovery_ledger()
    payload = _adjudication_payload()
    candidate = discovery.discovered_candidates[0]
    case = payload.group_decisions[0].case_decisions[0]
    group = CandidateFixGroup(
        fix_group_id="group.dangling",
        group_decision_sha256=sha256_hex(canonical_bytes(payload.group_decisions[0])),
        commits=[candidate.commit.commit_oid],
        before_commit=candidate.commit.diff_base_oid,
        after_commit=candidate.commit.commit_oid,
        after_committed_at=candidate.commit.committed_at,
        changed_paths=candidate.changed_paths,
        config_only=True,
        diff_evidence=[candidate.diff_evidence],
        cases=[case],
        disposition_summary="proposed",
        rationale="One config-only root cause.",
        lineage_links=[],
        counts_toward_gate=True,
    )
    rows = [
        ApplicabilityRow(
            defect_class=row.defect_class,
            domain_applicability=row.domain_applicability,
            implementation_support=row.implementation_support,
            evidence_counts=EvidenceCounts(
                proposed=1 if row.defect_class is DefectClass.dangling_reference else 0
            ),
        )
        for row in discovery.source_profile.taxonomy_applicability
    ]
    gate = GateSummary(
        status="insufficient_evidence",
        independent_proposed_groups=1,
        domain_applicable_proposed_classes=1,
        required_groups=8,
        required_classes=4,
        reason_code_counts={},
        failure_reasons=["independent proposed groups 1 < 8"],
        next_action="stop_source_and_use_fallback",
    )
    attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="human-reviewer",
        reviewed_at="2026-07-11T00:00:00Z",
        written_statement="I reviewed and approve the complete candidate assignment table.",
        candidate_universe_sha256=payload.candidate_universe_sha256,
        reviewed_payload_sha256=sha256_hex(canonical_bytes(payload)),
    )
    evidence = AdjudicationEvidence(
        **payload.model_dump(mode="json"), review_attestation=attestation
    )
    ledger = CandidateLedger(
        schema_version="external-corpus-b0a@1",
        source_id=discovery.source_id,
        source_profile=discovery.source_profile,
        source_profile_sha256=discovery.source_profile_sha256,
        search_registration=discovery.search_registration,
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovery)),
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        adjudication_evidence_sha256=sha256_hex(canonical_bytes(evidence)),
        evidence_revision=payload.evidence_revision,
        adjudicator_ids=["agent-adjudicator"],
        reviewer_ids=["human-reviewer"],
        groups=[group],
        candidate_decisions=[],
        applicability_matrix=rows,
        gate_summary=gate,
        lineage_resolutions=[],
    )
    decision = B0ADecision(
        schema_version="external-corpus-b0a@1",
        source_id=discovery.source_id,
        candidate_ledger_sha256=sha256_hex(canonical_bytes(ledger)),
        gate=gate,
    )
    assert decision.gate.next_action == "stop_source_and_use_fallback"

    duplicate = ledger.model_dump(mode="json")
    duplicate["candidate_decisions"] = [
        CandidateDisposition(
            commit_oid=candidate.commit.commit_oid,
            disposition="rejected",
            reason_code="non_bug",
            rationale="Duplicate assignment for contract rejection.",
            evidence_refs=[
                EvidenceRef(kind="commit_message", target_id=candidate.commit.commit_oid)
            ],
            adjudicator_id="agent-adjudicator",
        ).model_dump(mode="json")
    ]
    with pytest.raises(ValidationError, match="disjoint"):
        CandidateLedger.model_validate(duplicate)

    with pytest.raises(ValidationError, match="qualified_candidate"):
        EvidenceCounts(proposed=1, qualified_candidate=1)


def test_review_package_cannot_carry_a_human_or_adjudication_result() -> None:
    discovery = _discovery_ledger()
    package = ReviewPackage(
        schema_version="external-corpus-b0a@1",
        source_id=discovery.source_id,
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovery)),
        review_status="awaiting_human",
        rows=[],
    )
    assert package.review_status == "awaiting_human"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReviewPackage.model_validate(
            {**package.model_dump(mode="json"), "reviewer_id": "fake-reviewer"}
        )


def test_lineage_resolution_and_candidate_disposition_are_strict() -> None:
    excluded_resolution = LineageResolution(
        link_id="4" * 64,
        resolution="same_group",
        affected_group_ids=[],
        rationale="Both lineage endpoints are candidate-level rejections.",
    )
    assert excluded_resolution.affected_group_ids == []

    resolution = LineageResolution(
        link_id="5" * 64,
        resolution="separate",
        affected_group_ids=["group.a", "group.b"],
        rationale="Independent root causes despite objective lineage.",
    )
    assert resolution.affected_group_ids == ["group.a", "group.b"]
    with pytest.raises(ValidationError, match="sorted and unique"):
        LineageResolution(
            link_id="5" * 64,
            resolution="same_group",
            affected_group_ids=["group.b", "group.a"],
            rationale="Invalid order.",
        )
    with pytest.raises(ValidationError, match="requires ambiguous"):
        CandidateDisposition(
            commit_oid="1" * 40,
            disposition="rejected",
            reason_code="insufficient_semantic_evidence",
            rationale="Cannot prove the semantic fault.",
            evidence_refs=[EvidenceRef(kind="commit_message", target_id="1" * 40)],
            adjudicator_id="agent-adjudicator",
        )
    out_of_scope = CandidateDisposition(
        commit_oid="2" * 40,
        disposition="rejected",
        reason_code="out_of_scope",
        rationale="The change is outside the registered benchmark taxonomy.",
        evidence_refs=[EvidenceRef(kind="commit_message", target_id="2" * 40)],
        adjudicator_id="agent-adjudicator",
    )
    assert out_of_scope.reason_code == "out_of_scope"
