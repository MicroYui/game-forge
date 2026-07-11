from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from gameforge.bench.external_corpus.contracts import (
    B0AProtocol,
    CandidateOrderTerm,
    HistoryRange,
    LineageRegexRule,
    NativeValidatorCommand,
    RegexRule,
    SourceProfile,
    TaxonomyApplicability,
)
from gameforge.bench.external_corpus.profiles import (
    PROFILE_BINDINGS,
    get_profile_binding,
)
from gameforge.bench.external_corpus.profiles.endless_sky import (
    ENDLESS_SKY_PINNED_HEAD,
    ENDLESS_SKY_REPOSITORY_URL,
    validate_endless_sky_source_profile,
)
from gameforge.bench.external_corpus.profiles.flare import (
    legacy_search_spec_to_profile,
    validate_flare_source_profile,
)
from gameforge.bench.flare_evidence import FlareSearchSpec
from gameforge.bench.taxonomy import DefectClass


ROOT = Path(__file__).resolve().parents[3]
FLARE_SEARCH_SPEC = ROOT / "scenarios/flare_corpus/search-spec.json"


def _endless_sky_profile() -> SourceProfile:
    not_applicable = {
        DefectClass.economy_collapse,
        DefectClass.gacha_expectation_violation,
        DefectClass.non_monotonic_curve,
        DefectClass.prob_sum_ne_1,
    }
    return SourceProfile(
        schema_version="external-source-profile@1",
        source_id="endless_sky",
        profile_version="endless-sky-b0a@1",
        repository_url=ENDLESS_SKY_REPOSITORY_URL,
        pinned_head=ENDLESS_SKY_PINNED_HEAD,
        history_range=HistoryRange(
            committed_at_gte=1672531200,
            expected_commit_count=2508,
        ),
        config_include_globs=("data/**/*.txt",),
        config_exclude_globs=(),
        message_rules=(
            RegexRule(rule_id="subject.fix_or_missing", pattern="(?i)(?:fix|missing)"),
        ),
        diff_rules=(),
        lineage_rules=(
            LineageRegexRule(
                rule_id="trailer.backport_of",
                link_type="backport",
                pattern=r"(?m)^Backport-of: ([0-9a-f]{40})$",
            ),
            LineageRegexRule(
                rule_id="trailer.cherry_pick_x",
                link_type="cherry_pick",
                pattern=r"(?m)^\(cherry picked from commit ([0-9a-f]{40})\)$",
            ),
            LineageRegexRule(
                rule_id="trailer.git_revert",
                link_type="revert",
                pattern=r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
            ),
        ),
        candidate_order=(
            CandidateOrderTerm(field="committed_at", direction="descending"),
            CandidateOrderTerm(field="commit_oid", direction="ascending"),
        ),
        license_id="GPL-3.0-or-later",
        notice_files=("copyright", "license.txt", "credits.txt"),
        native_validator_commands=(
            NativeValidatorCommand(
                command_id="endless_sky.parse_and_check_references",
                argv=(
                    "{engine_binary}",
                    "--resources",
                    "{case_root}",
                    "--config",
                    "{scratch_config}",
                    "--parse-save",
                ),
                network="forbidden",
            ),
        ),
        parser_version="endless-sky-parser.b10b7d6c",
        query_complete_closure=(
            "changed_files",
            "referenced_data_nodes",
            "mission_condition_dependencies",
            "map_route_dependencies",
            "outfit_ship_dependencies",
        ),
        taxonomy_applicability=tuple(
            TaxonomyApplicability(
                defect_class=defect_class,
                domain_applicability=(
                    "not_applicable" if defect_class in not_applicable else "applicable"
                ),
                implementation_support="planned",
                rationale=f"Registered B0A applicability for {defect_class.value}",
            )
            for defect_class in DefectClass
        ),
        qualification_predicate_ids=(
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
            "narrative_unique",
        ),
        b0a_protocol=B0AProtocol(
            candidate_limit=80,
            expected_matched_candidate_count=610,
            expected_config_only_candidate_count=562,
            minimum_independent_groups=8,
            minimum_domain_applicable_classes=4,
        ),
    )


@pytest.mark.parametrize("source_id", ["flare", "endless_sky"])
def test_registered_profile_uses_the_generic_contract(source_id: str) -> None:
    binding = get_profile_binding(source_id)

    assert binding.source_id == source_id
    assert binding.profile_model is SourceProfile
    assert callable(binding.validate_source_profile)


def test_profile_registry_is_static_and_unknown_sources_fail_closed() -> None:
    assert isinstance(PROFILE_BINDINGS, MappingProxyType)
    with pytest.raises(TypeError):
        PROFILE_BINDINGS["unknown"] = PROFILE_BINDINGS["flare"]  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown external source profile: unknown"):
        get_profile_binding("unknown")


def test_flare_legacy_search_spec_maps_to_generic_profile_in_memory() -> None:
    search_spec = FlareSearchSpec.model_validate_json(FLARE_SEARCH_SPEC.read_bytes())

    profile = legacy_search_spec_to_profile(search_spec)

    assert profile.source_id == "flare"
    assert profile.repository_url == search_spec.source_repo
    assert profile.pinned_head == search_spec.pinned_head
    assert profile.history_range.after_exclusive_oid == search_spec.after_exclusive
    assert profile.history_range.expected_commit_count == search_spec.expected_revision_count
    assert profile.config_include_globs == search_spec.config_path_globs
    assert profile.config_exclude_globs == search_spec.excluded_path_globs
    assert [rule.rule_id for rule in profile.message_rules] == [
        rule.rule_id for search_round in search_spec.rounds for rule in search_round.message_regexes
    ]
    assert [rule.rule_id for rule in profile.diff_rules] == [
        rule.rule_id for search_round in search_spec.rounds for rule in search_round.diff_regexes
    ]
    assert profile.lineage_rules == search_spec.lineage_regexes
    assert profile.candidate_order == (
        CandidateOrderTerm(field="committed_at", direction="ascending"),
        CandidateOrderTerm(field="commit_oid", direction="ascending"),
    )
    assert profile.b0a_protocol.expected_matched_candidate_count == 526
    assert profile.b0a_protocol.expected_config_only_candidate_count == 190
    assert validate_flare_source_profile(profile) == profile


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_id", "other", "source_id"),
        ("repository_url", "https://example.test/other.git", "repository_url"),
        ("pinned_head", "0" * 40, "pinned_head"),
        ("config_include_globs", ("other/**/*.txt",), "config_include_globs"),
        ("license_id", "MIT", "license_id"),
    ],
)
def test_endless_sky_binding_rejects_mismatched_source_contract(
    field: str,
    value: object,
    message: str,
) -> None:
    profile = _endless_sky_profile().model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        validate_endless_sky_source_profile(profile)


def test_endless_sky_binding_accepts_the_preregistered_source_contract() -> None:
    profile = _endless_sky_profile()

    assert validate_endless_sky_source_profile(profile) == profile
