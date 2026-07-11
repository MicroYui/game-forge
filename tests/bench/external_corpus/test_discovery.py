from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import gameforge.bench.external_corpus.discovery as discovery_module
from gameforge.bench.external_corpus.contracts import (
    B0AProtocol,
    CandidateOrderTerm,
    HistoryRange,
    LineageRegexRule,
    NativeValidatorCommand,
    RegexRule,
    SearchRegistration,
    SourceProfile,
    TaxonomyApplicability,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.external_corpus.discovery import discover_candidates
from gameforge.bench.external_corpus.git import GitEvidenceError, ReadOnlyGitRepo
from gameforge.bench.taxonomy import DefectClass
from tests.bench.external_corpus.git_fixture import GenericGitFixture, build_generic_git_repo


@pytest.fixture
def generic_git_repo(tmp_path) -> GenericGitFixture:
    return build_generic_git_repo(tmp_path / "upstream")


def _taxonomy() -> tuple[TaxonomyApplicability, ...]:
    return tuple(
        TaxonomyApplicability(
            defect_class=defect_class,
            domain_applicability="applicable",
            implementation_support="planned",
            rationale=f"{defect_class.value} is represented by the fixture source",
        )
        for defect_class in DefectClass
    )


def _profile(
    fixture: GenericGitFixture,
    *,
    source_id: str,
    include: tuple[str, ...],
    message_pattern: str,
    order_direction: str,
    limit: int,
    matched: int,
    config_only: int,
) -> SourceProfile:
    return SourceProfile(
        schema_version="external-source-profile@1",
        source_id=source_id,
        profile_version=f"{source_id}@1",
        repository_url=f"https://example.test/{source_id}.git",
        pinned_head=fixture.head,
        history_range=HistoryRange(expected_commit_count=fixture.revision_count),
        config_include_globs=include,
        config_exclude_globs=(),
        message_rules=(RegexRule(rule_id="message.fix", pattern=message_pattern),),
        diff_rules=(
            RegexRule(
                rule_id="diff.requires_status",
                pattern=r"(?m)^[+-](?![+-]).*requires_status",
            ),
        ),
        lineage_rules=(
            LineageRegexRule(
                rule_id="trailer.backport",
                link_type="backport",
                pattern=r"(?m)^Backport-of: ([0-9a-f]{40})$",
            ),
            LineageRegexRule(
                rule_id="trailer.cherry_pick",
                link_type="cherry_pick",
                pattern=r"(?m)^\(cherry picked from commit ([0-9a-f]{40})\)$",
            ),
            LineageRegexRule(
                rule_id="trailer.revert",
                link_type="revert",
                pattern=r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
            ),
        ),
        candidate_order=(
            CandidateOrderTerm(field="committed_at", direction=order_direction),
            CandidateOrderTerm(field="commit_oid", direction="ascending"),
        ),
        license_id="GPL-3.0-or-later",
        notice_files=("LICENSE",),
        native_validator_commands=(
            NativeValidatorCommand(command_id="fixture.parse", argv=("fixture-engine", "-p")),
        ),
        parser_version="fixture-parser@1",
        query_complete_closure=("changed_files", "referenced_records"),
        taxonomy_applicability=_taxonomy(),
        qualification_predicate_ids=("fixture.before_after",),
        b0a_protocol=B0AProtocol(
            candidate_limit=limit,
            expected_matched_candidate_count=matched,
            expected_config_only_candidate_count=config_only,
            minimum_independent_groups=1,
            minimum_domain_applicable_classes=1,
        ),
    )


def _registration(source_id: str) -> SearchRegistration:
    return SearchRegistration(
        project_commit_oid="1" * 40,
        profile_repo_relative_path=f"scenarios/external_corpus/{source_id}/profile.json",
    )


def _sky_profile(fixture: GenericGitFixture, *, limit: int = 3) -> SourceProfile:
    return _profile(
        fixture,
        source_id="fixture_sky",
        include=("data/**/*.txt",),
        message_pattern=r"(?i)(?:fix|missing)",
        order_direction="descending",
        limit=limit,
        matched=10,
        config_only=7,
    )


def test_profiles_share_one_engine_without_source_conditionals(generic_git_repo, tmp_path):
    repo = ReadOnlyGitRepo(generic_git_repo.path)
    mods_profile = _profile(
        generic_git_repo,
        source_id="fixture_mods",
        include=("mods/**/*.txt",),
        message_pattern=r"(?i)fix",
        order_direction="ascending",
        limit=100,
        matched=1,
        config_only=1,
    )
    sky_profile = _sky_profile(generic_git_repo)

    mods = discover_candidates(
        repo, mods_profile, _registration(mods_profile.source_id), tmp_path / "mods-blobs"
    )
    sky = discover_candidates(
        repo, sky_profile, _registration(sky_profile.source_id), tmp_path / "sky-blobs"
    )

    assert [item.commit.commit_oid for item in mods.discovered_candidates] == [
        generic_git_repo.mods_fix
    ]
    assert sky.source_id == "fixture_sky"
    assert len(sky.discovered_candidates) == 3
    committed_at = [item.commit.committed_at for item in sky.discovered_candidates]
    assert committed_at == sorted(committed_at, reverse=True)
    assert sky.matched_candidate_count == 10
    assert sky.config_only_candidate_count == 7


def test_generic_profile_does_not_infer_unregistered_adjacent_context(
    generic_git_repo, tmp_path
):
    profile = _sky_profile(generic_git_repo, limit=100)

    ledger = discover_candidates(
        ReadOnlyGitRepo(generic_git_repo.path),
        profile,
        _registration(profile.source_id),
        tmp_path / "blobs",
    )
    selected_oids = {
        candidate.commit.commit_oid for candidate in ledger.discovered_candidates
    }

    assert generic_git_repo.data_fix in selected_oids
    assert generic_git_repo.data_missing in selected_oids
    assert generic_git_repo.data_adjacent not in selected_oids
    assert all(
        reason.kind != "adjacent_context"
        for candidate in ledger.discovered_candidates
        for reason in candidate.selection_reasons
    )


def test_full_discovery_records_lineage_patch_ids_and_cas(generic_git_repo, tmp_path):
    profile = _sky_profile(generic_git_repo, limit=100)
    blob_dir = tmp_path / "blobs"

    ledger = discover_candidates(
        ReadOnlyGitRepo(generic_git_repo.path),
        profile,
        _registration(profile.source_id),
        blob_dir,
    )
    by_oid = {item.commit.commit_oid: item for item in ledger.discovered_candidates}

    assert any(
        reason.kind == "lineage_context"
        for reason in by_oid[generic_git_repo.mods_fix].selection_reasons
    )
    assert any(
        link.link_type == "patch_id"
        and {link.source_oid, link.target_oid}
        == {generic_git_repo.duplicate_source, generic_git_repo.duplicate_copy}
        for link in ledger.objective_lineage_links
    )
    assert any(
        link.link_type == "backport"
        and link.source_oid == generic_git_repo.mods_fix
        and link.target_oid == generic_git_repo.backport
        for link in ledger.objective_lineage_links
    )
    for candidate in ledger.discovered_candidates:
        blob = blob_dir / candidate.diff_evidence.patch_sha256
        assert blob.read_bytes()
        assert sha256_hex(blob.read_bytes()) == candidate.diff_evidence.patch_sha256


def test_discovery_is_byte_stable(generic_git_repo, tmp_path):
    profile = _sky_profile(generic_git_repo)
    registration = _registration(profile.source_id)
    repo = ReadOnlyGitRepo(generic_git_repo.path)

    first = discover_candidates(repo, profile, registration, tmp_path / "first")
    second = discover_candidates(repo, profile, registration, tmp_path / "second")

    assert canonical_bytes(first) == canonical_bytes(second)


def test_discovery_rejects_wrong_head_and_registered_totals(generic_git_repo, tmp_path):
    profile = _sky_profile(generic_git_repo)
    wrong_head_payload = profile.model_dump(mode="json")
    wrong_head_payload["pinned_head"] = "f" * 40
    wrong_head = SourceProfile.model_validate(wrong_head_payload)

    with pytest.raises(GitEvidenceError, match="pinned head"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            wrong_head,
            _registration(wrong_head.source_id),
            tmp_path / "wrong-head",
        )

    wrong_count_payload = profile.model_dump(mode="json")
    wrong_count_payload["b0a_protocol"]["expected_matched_candidate_count"] = 12
    wrong_count = SourceProfile.model_validate(wrong_count_payload)
    wrong_count_blobs = tmp_path / "wrong-count"
    with pytest.raises(GitEvidenceError, match="matched candidate count"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            wrong_count,
            _registration(wrong_count.source_id),
            wrong_count_blobs,
        )
    assert not wrong_count_blobs.exists()

    wrong_config_payload = profile.model_dump(mode="json")
    wrong_config_payload["b0a_protocol"]["expected_config_only_candidate_count"] = 9
    wrong_config = SourceProfile.model_validate(wrong_config_payload)
    wrong_config_blobs = tmp_path / "wrong-config-count"
    with pytest.raises(GitEvidenceError, match="config-only candidate count"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            wrong_config,
            _registration(wrong_config.source_id),
            wrong_config_blobs,
        )
    assert not wrong_config_blobs.exists()


def test_discovery_revalidates_constructed_profile_and_registration(generic_git_repo, tmp_path):
    profile = _sky_profile(generic_git_repo)
    invalid_profile = SourceProfile.model_construct(
        **{**profile.__dict__, "repository_url": "http://invalid.test/repo"}
    )
    with pytest.raises(ValidationError, match="repository_url"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            invalid_profile,
            _registration(profile.source_id),
            tmp_path / "invalid-profile",
        )

    invalid_registration = SearchRegistration.model_construct(
        project_commit_oid="1" * 40,
        profile_repo_relative_path="../profile.json",
    )
    with pytest.raises(ValidationError, match="profile_repo_relative_path"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            profile,
            invalid_registration,
            tmp_path / "invalid-registration",
        )


def test_discovery_fails_closed_when_cas_publication_does_not_materialize_blob(
    generic_git_repo, tmp_path, monkeypatch
):
    profile = _sky_profile(generic_git_repo)

    def missing_blob(_blob_dir: Path, data: bytes) -> tuple[str, str]:
        digest = sha256_hex(data)
        return digest, f"blobs/{digest}"

    monkeypatch.setattr(discovery_module, "put_blob", missing_blob)
    with pytest.raises(GitEvidenceError, match="CAS"):
        discover_candidates(
            ReadOnlyGitRepo(generic_git_repo.path),
            profile,
            _registration(profile.source_id),
            tmp_path / "missing-cas",
        )
