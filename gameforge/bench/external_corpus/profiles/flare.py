"""Compatibility binding from the frozen Flare B0A search spec to SourceProfile."""

from __future__ import annotations

from gameforge.bench.external_corpus.contracts import (
    B0AProtocol,
    CandidateOrderTerm,
    HistoryRange,
    NativeValidatorCommand,
    SourceProfile,
    TaxonomyApplicability,
)
from gameforge.bench.flare_evidence import FlareSearchSpec
from gameforge.bench.taxonomy import DefectClass


FLARE_SOURCE_ID = "flare"
FLARE_REPOSITORY_URL = "https://github.com/flareteam/flare-game.git"
FLARE_PINNED_HEAD = "fe23b5ba73f99f0c3969f8b23dbabaa8f7a6b602"
FLARE_LICENSE_ID = "LicenseRef-Flare-Mixed"
FLARE_EXPANDED_MATCHED_COUNT = 526
FLARE_EXPANDED_CONFIG_ONLY_COUNT = 190

_NOT_APPLICABLE = {
    DefectClass.gacha_expectation_violation,
    DefectClass.prob_sum_ne_1,
}


def _taxonomy_applicability() -> tuple[TaxonomyApplicability, ...]:
    return tuple(
        TaxonomyApplicability(
            defect_class=defect_class,
            domain_applicability=(
                "not_applicable" if defect_class in _NOT_APPLICABLE else "applicable"
            ),
            implementation_support="planned",
            rationale=f"Legacy Flare B0A compatibility classification for {defect_class.value}",
        )
        for defect_class in DefectClass
    )


def legacy_search_spec_to_profile(search_spec: FlareSearchSpec) -> SourceProfile:
    """Build an in-memory generic profile without changing frozen Flare artifacts."""

    spec = FlareSearchSpec.model_validate(search_spec.model_dump(mode="json"))
    profile = SourceProfile(
        schema_version="external-source-profile@1",
        source_id=FLARE_SOURCE_ID,
        profile_version="flare-b0a-legacy@1",
        repository_url=spec.source_repo,
        pinned_head=spec.pinned_head,
        history_range=HistoryRange(
            after_exclusive_oid=spec.after_exclusive,
            expected_commit_count=spec.expected_revision_count,
        ),
        config_include_globs=spec.config_path_globs,
        config_exclude_globs=spec.excluded_path_globs,
        message_rules=tuple(
            rule for search_round in spec.rounds for rule in search_round.message_regexes
        ),
        diff_rules=tuple(
            rule for search_round in spec.rounds for rule in search_round.diff_regexes
        ),
        lineage_rules=spec.lineage_regexes,
        candidate_order=tuple(
            CandidateOrderTerm(field=field, direction="ascending")
            for field in spec.candidate_order
        ),
        license_id=FLARE_LICENSE_ID,
        notice_files=(
            "NOTICE",
            "LICENSE.flare-game",
            "README.flare-game",
            "CREDITS.flare-game",
        ),
        native_validator_commands=(
            NativeValidatorCommand(
                command_id="flare.config_parse",
                argv=("{flare_parser_binary}", "{case_root}"),
                network="forbidden",
            ),
        ),
        parser_version="flare-profile-bridge@1",
        query_complete_closure=(
            "changed_files",
            "referenced_records",
            "quest_status_dependencies",
            "loot_table_dependencies",
        ),
        taxonomy_applicability=_taxonomy_applicability(),
        qualification_predicate_ids=(
            "reference_resolves",
            "drop_source_exists",
            "target_reachable",
            "dependency_acyclic",
            "quest_offerable",
            "quest_completion_satisfiable",
            "reward_within_declared_bounds",
            "curve_monotonic",
            "economy_stable",
            "narrative_character_consistent",
            "narrative_spoiler_free",
            "narrative_faction_consistent",
            "narrative_unique",
        ),
        b0a_protocol=B0AProtocol(
            candidate_limit=FLARE_EXPANDED_MATCHED_COUNT,
            expected_matched_candidate_count=FLARE_EXPANDED_MATCHED_COUNT,
            expected_config_only_candidate_count=FLARE_EXPANDED_CONFIG_ONLY_COUNT,
            minimum_independent_groups=8,
            minimum_domain_applicable_classes=4,
        ),
    )
    return validate_flare_source_profile(profile)


def validate_flare_source_profile(profile: SourceProfile) -> SourceProfile:
    """Validate that an in-memory compatibility profile stays source-bound."""

    validated = SourceProfile.model_validate(profile.model_dump(mode="json"))
    expected = {
        "source_id": FLARE_SOURCE_ID,
        "repository_url": FLARE_REPOSITORY_URL,
        "pinned_head": FLARE_PINNED_HEAD,
        "license_id": FLARE_LICENSE_ID,
    }
    for field, expected_value in expected.items():
        if getattr(validated, field) != expected_value:
            raise ValueError(f"flare source profile has unexpected {field}")
    return validated


__all__ = [
    "FLARE_EXPANDED_CONFIG_ONLY_COUNT",
    "FLARE_EXPANDED_MATCHED_COUNT",
    "FLARE_LICENSE_ID",
    "FLARE_PINNED_HEAD",
    "FLARE_REPOSITORY_URL",
    "FLARE_SOURCE_ID",
    "legacy_search_spec_to_profile",
    "validate_flare_source_profile",
]
