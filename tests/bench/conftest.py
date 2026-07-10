from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence

import pytest

from gameforge.bench.flare_evidence import (
    B0A_DEFECT_CLASSES,
    FLARE_B0A_SCHEMA_VERSION,
    AdjudicationEvidence,
    ApplicabilityDeclaration,
    ApplicabilityRow,
    B0ADecision,
    CandidateCase,
    CandidateDisposition,
    CandidateFixGroup,
    CandidateGroupDecision,
    CandidateLedger,
    DiscoveryLedger,
    EvidenceCounts,
    EvidenceRef,
    FlareSearchSpec,
    GateSummary,
    LineageResolution,
    ReviewAttestation,
    SearchRegistration,
    SelectedParentEdge,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.flare_git import ReadOnlyGitRepo, discover_candidates
from tests.bench.flare_git_fixture import build_flare_git_repo


REGISTERED_SEARCH_SPEC_PAYLOAD = {
    "adjacency": {
        "first_parent_child_edges": 1,
        "first_parent_predecessor_edges": 1,
        "include_reachable_lineage_sources": True,
        "nonrecursive": True,
        "require_shared_exact_eligible_path_with_anchor": True,
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
        "mods/**/tilesetdefs/**",
    ],
    "expected_revision_count": 7049,
    "git_commands": {
        "common_prefix": [
            "git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "color.ui=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.mnemonicPrefix=false",
            "-c",
            "diff.renames=false",
            "-c",
            "diff.algorithm=myers",
            "-c",
            "diff.indentHeuristic=false",
            "-c",
            "diff.interHunkContext=0",
            "-c",
            "diff.suppressBlankEmpty=false",
            "-c",
            "diff.orderFile=/dev/null",
            "-C",
            "{repo}",
        ],
        "empty_tree_args": ["hash-object", "-t", "tree", "--stdin"],
        "eligible_path_suffix": ["--", "{eligible_paths...}"],
        "history_args": ["rev-list", "--topo-order", "--reverse", "{revision_range}"],
        "metadata_args": [
            "show",
            "-s",
            "--no-show-signature",
            "--encoding=UTF-8",
            "--format=%H%x00%P%x00%ct%x00%s%x00%B",
            "{commit}",
        ],
        "patch_args": [
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "--inter-hunk-context=0",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--submodule=short",
            "--ignore-submodules=none",
            "{parent}",
            "{commit}",
        ],
        "patch_id_args": ["patch-id", "--stable"],
        "paths_args": [
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            "{parent}",
            "{commit}",
        ],
        "resolve_args": ["rev-parse", "--verify", "{pinned_head}^{commit}"],
        "version_command": ["git", "--version"],
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
            "TZ": "UTC",
        },
        "inherit_allowlist": ["PATH"],
    },
    "history_walk": "all_reachable_topo_order",
    "issue_pr_discovery": "disabled_offline_only",
    "lineage_regexes": [
        {
            "link_type": "backport",
            "pattern": "(?m)^Backport-of: ([0-9a-f]{40})$",
            "rule_id": "trailer.backport_of",
        },
        {
            "link_type": "cherry_pick",
            "pattern": r"(?m)^\(cherry picked from commit ([0-9a-f]{40})\)$",
            "rule_id": "trailer.cherry_pick_x",
        },
        {
            "link_type": "revert",
            "pattern": r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
            "rule_id": "trailer.git_revert",
        },
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
                    "pattern": r"(?i)\A(?=[^\r\n]*\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \t]+appearing|not[ \t]+being[ \t]+able|completed[ \t]+before)\b)(?=[^\r\n]*\b(?:quests?|status(?:es)?|loot|drops?|references?|spawns?|chests?|enem(?:y|ies)|items?)\b)[^\r\n]*\Z",
                    "rule_id": "initial.message_bug_and_domain",
                }
            ],
            "name": "initial",
        },
        {
            "diff_regexes": [
                {
                    "pattern": r"(?m)^[+-](?![+-])[ \t]*(?:requires_status|requires_not_status|set_status|unset_status|pickup_status|loot|chance|weight|requires_item|item)[ \t]*=",
                    "rule_id": "expanded.diff_behavior_key",
                }
            ],
            "message_regexes": [
                {
                    "pattern": r"(?i)\A(?!merge(?:[ \t]|\Z))(?=[^\r\n]*\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \t]+appearing|not[ \t]+being[ \t]+able|completed[ \t]+before)\b)[^\r\n]*\Z",
                    "rule_id": "expanded.message_bug_language",
                }
            ],
            "name": "expanded",
        },
    ],
    "schema_version": "flare-b0a@1",
    "selected_round_semantics": "union_through_selected",
    "source_repo": "https://github.com/flareteam/flare-game.git",
    "stop_condition": "exhaust_reachable_range",
}
REGISTERED_SEARCH_SPEC_BYTES = (
    json.dumps(
        REGISTERED_SEARCH_SPEC_PAYLOAD,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    + b"\n"
)
REGISTERED_SEARCH_SPEC_SHA256 = hashlib.sha256(REGISTERED_SEARCH_SPEC_BYTES).hexdigest()

_ADJUDICATOR_ID = "assisted-review-1"
_REVIEWER_ID = "human-review-1"


def _candidate_index(discovery: DiscoveryLedger):
    return {
        candidate.commit.commit_oid: candidate
        for candidate in discovery.discovered_candidates
    }


def _patch_ref(candidate) -> EvidenceRef:
    return EvidenceRef(
        kind="patch_blob",
        target_id=candidate.diff_evidence.patch_sha256,
    )


def _group_decision(
    discovery: DiscoveryLedger,
    fix_group_id: str,
    commits: Sequence[str],
    defect_class: str,
    *,
    disposition: str = "proposed",
) -> CandidateGroupDecision:
    candidates = _candidate_index(discovery)
    selected = [candidates[oid] for oid in commits]
    return CandidateGroupDecision(
        fix_group_id=fix_group_id,
        commits=list(commits),
        selected_parent_edges=[
            SelectedParentEdge(
                commit_oid=item.commit.commit_oid,
                parent_oid=item.commit.diff_base_oid,
            )
            for item in selected
        ],
        root_cause_evidence_refs=[_patch_ref(selected[0])],
        case_decisions=[
            CandidateCase(
                case_id=f"case-{fix_group_id}-{defect_class}",
                defect_class=defect_class,
                disposition=disposition,
                rationale=f"Reviewed {disposition} fixture case for {fix_group_id}.",
                evidence_refs=[_patch_ref(selected[-1])],
            )
        ],
        adjudicator_id=_ADJUDICATOR_ID,
        reviewer_id=_REVIEWER_ID,
        rationale=f"Reviewed fixture group {fix_group_id}.",
    )


def _applicability_declarations() -> tuple[ApplicabilityDeclaration, ...]:
    return tuple(
        ApplicabilityDeclaration(
            defect_class=defect_class,
            domain_applicability=(
                "not_applicable"
                if defect_class.value
                in {"prob_sum_ne_1", "gacha_expectation_violation"}
                else "applicable"
            ),
            implementation_support="planned",
        )
        for defect_class in B0A_DEFECT_CLASSES
    )


def _candidate_dispositions(
    discovery: DiscoveryLedger,
    grouped_commits: set[str],
    duplicate_oids: set[str],
) -> list[CandidateDisposition]:
    dispositions: list[CandidateDisposition] = []
    for candidate in discovery.discovered_candidates:
        oid = candidate.commit.commit_oid
        if oid in grouped_commits:
            continue
        if not candidate.config_only:
            reason_code = "non_config_only"
            disposition = "rejected"
        elif oid in duplicate_oids:
            reason_code = "revert_or_duplicate"
            disposition = "rejected"
        else:
            reason_code = "insufficient_context"
            disposition = "ambiguous"
        dispositions.append(
            CandidateDisposition(
                commit_oid=oid,
                disposition=disposition,
                reason_code=reason_code,
                rationale=f"Fixture exclusion for {oid} uses {reason_code}.",
                evidence_refs=[_patch_ref(candidate)],
                adjudicator_id=_ADJUDICATOR_ID,
                reviewer_id=_REVIEWER_ID,
            )
        )
    return dispositions


def _lineage_resolutions(
    discovery: DiscoveryLedger,
    groups: Sequence[CandidateGroupDecision],
) -> list[LineageResolution]:
    commit_groups = {
        oid: group.fix_group_id
        for group in groups
        for oid in group.commits
    }
    group_ids = {group.fix_group_id for group in groups}
    resolutions: list[LineageResolution] = []
    for link in discovery.objective_lineage_links:
        affected = sorted(
            {
                commit_groups[oid]
                for oid in (link.source_oid, link.target_oid)
                if oid in commit_groups
            }
        )
        if not affected and link.link_type == "patch_id" and "group-loot" in group_ids:
            affected = ["group-loot"]
        if not affected and link.link_type == "backport" and "group-remote" in group_ids:
            affected = ["group-remote"]
        if not affected:
            raise AssertionError(f"fixture lineage link has no affected group: {link.link_id}")
        resolutions.append(
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=affected,
                rationale=f"Fixture resolves {link.link_type} as one objective lineage.",
            )
        )
    return resolutions


def _approved_evidence(
    discovery: DiscoveryLedger,
    *,
    evidence_revision: str,
    groups: Sequence[CandidateGroupDecision],
    dispositions: Sequence[CandidateDisposition],
    resolutions: Sequence[LineageResolution],
    prior_candidate_ledger_sha256: str | None = None,
    prior_decision_sha256: str | None = None,
) -> AdjudicationEvidence:
    payload = {
        "schema_version": FLARE_B0A_SCHEMA_VERSION,
        "evidence_revision": evidence_revision,
        "search_round": discovery.search_round,
        "discovery_ledger_sha256": sha256_hex(canonical_bytes(discovery)),
        "candidate_universe_sha256": discovery.candidate_universe_sha256,
        "source_artifacts": [],
        "applicability_declarations": [
            item.model_dump(mode="json") for item in _applicability_declarations()
        ],
        "group_decisions": [item.model_dump(mode="json") for item in groups],
        "candidate_decisions": [item.model_dump(mode="json") for item in dispositions],
        "lineage_resolutions": [item.model_dump(mode="json") for item in resolutions],
    }
    if prior_candidate_ledger_sha256 is not None:
        payload["prior_candidate_ledger_sha256"] = prior_candidate_ledger_sha256
    if prior_decision_sha256 is not None:
        payload["prior_decision_sha256"] = prior_decision_sha256
    attestation = ReviewAttestation(
        reviewer_id=_REVIEWER_ID,
        review_scope="complete_b0a_adjudication",
        approval="approved",
        review_revision=f"review-{evidence_revision}",
        written_statement="I reviewed and approve the complete fixture disposition table.",
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        reviewed_payload_sha256=sha256_hex(canonical_bytes(payload)),
    )
    return AdjudicationEvidence.model_validate(
        {**payload, "review_attestation": attestation.model_dump(mode="json")}
    )


def _refresh_evidence(
    evidence: AdjudicationEvidence,
    **updates,
) -> AdjudicationEvidence:
    changed = evidence.model_copy(update=updates)
    payload = changed.model_dump(
        mode="json",
        exclude={"review_attestation"},
        exclude_none=True,
    )
    attestation = changed.review_attestation.model_copy(
        update={"reviewed_payload_sha256": sha256_hex(canonical_bytes(payload))}
    )
    return AdjudicationEvidence.model_validate(
        {**payload, "review_attestation": attestation.model_dump(mode="json")}
    )


def _derived_group(
    discovery: DiscoveryLedger,
    decision: CandidateGroupDecision,
    resolutions: Sequence[LineageResolution],
) -> CandidateFixGroup:
    candidates = _candidate_index(discovery)
    selected = [candidates[oid] for oid in decision.commits]
    dispositions = {item.disposition for item in decision.case_decisions}
    summary = (
        "ambiguous"
        if "ambiguous" in dispositions
        else "proposed"
        if "proposed" in dispositions
        else "rejected"
    )
    return CandidateFixGroup(
        fix_group_id=decision.fix_group_id,
        commits=list(decision.commits),
        before_commit=selected[0].commit.diff_base_oid,
        after_commit=selected[-1].commit.commit_oid,
        after_committed_at=selected[-1].commit.committed_at,
        changed_paths=sorted(
            {path for candidate in selected for path in candidate.changed_paths}
        ),
        config_only=all(candidate.config_only for candidate in selected),
        diff_evidence=[candidate.diff_evidence for candidate in selected],
        cases=list(decision.case_decisions),
        disposition_summary=summary,
        rationale=decision.rationale,
        lineage_links=[
            resolution.link_id
            for resolution in resolutions
            if decision.fix_group_id in resolution.affected_group_ids
        ],
    )


def _applicability_matrix(
    declarations: Sequence[ApplicabilityDeclaration],
    groups: Sequence[CandidateFixGroup],
) -> list[ApplicabilityRow]:
    cases = [case for group in groups for case in group.cases]
    matrix: list[ApplicabilityRow] = []
    for declaration in declarations:
        class_cases = [
            case for case in cases if case.defect_class == declaration.defect_class
        ]
        matrix.append(
            ApplicabilityRow(
                defect_class=declaration.defect_class,
                domain_applicability=declaration.domain_applicability,
                evidence_counts=EvidenceCounts(
                    proposed=sum(case.disposition == "proposed" for case in class_cases),
                    rejected=sum(case.disposition == "rejected" for case in class_cases),
                    ambiguous=sum(case.disposition == "ambiguous" for case in class_cases),
                ),
                implementation_support=declaration.implementation_support,
            )
        )
    return matrix


def _expected_candidate_ledger(
    discovery: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> CandidateLedger:
    groups = [
        _derived_group(discovery, decision, evidence.lineage_resolutions)
        for decision in evidence.group_decisions
    ]
    proposed_groups = [group for group in groups if group.counts_toward_gate]
    proposed_classes = {
        case.defect_class
        for group in proposed_groups
        for case in group.cases
        if case.disposition == "proposed"
    }
    passed = len(proposed_groups) >= 8 and len(proposed_classes) >= 4
    if passed:
        status = "provisional_pass"
        next_action = "proceed_to_b0b"
        failure_reasons = []
    elif discovery.search_round == "initial":
        status = "expanded_round_required"
        next_action = "run_expanded_round"
        failure_reasons = ["fewer than eight independent proposed groups"]
    else:
        status = "insufficient_evidence"
        next_action = "stop_flare_heavy_investment"
        failure_reasons = ["expanded search found fewer than eight proposed groups"]
    reason_counts: dict[str, int] = {}
    for disposition in evidence.candidate_decisions:
        reason_counts[disposition.reason_code] = reason_counts.get(
            disposition.reason_code, 0
        ) + 1
    gate = GateSummary(
        status=status,
        proposed_groups=len(proposed_groups),
        proposed_classes=len(proposed_classes),
        reason_code_counts=dict(sorted(reason_counts.items())),
        failure_reasons=failure_reasons,
        next_action=next_action,
    )
    adjudicator_ids = sorted(
        {
            *[item.adjudicator_id for item in evidence.group_decisions],
            *[item.adjudicator_id for item in evidence.candidate_decisions],
        }
    )
    reviewer_ids = sorted(
        {
            *[item.reviewer_id for item in evidence.group_decisions],
            *[item.reviewer_id for item in evidence.candidate_decisions],
        }
    )
    return CandidateLedger(
        search_frame=discovery.search_frame,
        search_spec_sha256=discovery.search_spec_sha256,
        search_registration=discovery.search_registration,
        search_round=discovery.search_round,
        observed_revision_count=discovery.observed_revision_count,
        discovery_tool=discovery.discovery_tool,
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovery)),
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        adjudication_evidence_sha256=sha256_hex(canonical_bytes(evidence)),
        evidence_revision=evidence.evidence_revision,
        prior_candidate_ledger_sha256=evidence.prior_candidate_ledger_sha256,
        prior_decision_sha256=evidence.prior_decision_sha256,
        adjudicator_ids=adjudicator_ids,
        reviewer_ids=reviewer_ids,
        groups=groups,
        candidate_decisions=list(evidence.candidate_decisions),
        applicability_matrix=_applicability_matrix(
            evidence.applicability_declarations, groups
        ),
        gate_summary=gate,
        lineage_resolutions=list(evidence.lineage_resolutions),
    )


@pytest.fixture
def registered_search_spec_payload():
    return copy.deepcopy(REGISTERED_SEARCH_SPEC_PAYLOAD)


@pytest.fixture
def registered_search_spec_bytes():
    return REGISTERED_SEARCH_SPEC_BYTES


@pytest.fixture
def registered_search_spec_sha256():
    return REGISTERED_SEARCH_SPEC_SHA256


@pytest.fixture
def flare_git_repo(tmp_path):
    return build_flare_git_repo(tmp_path / "flare-repo")


@pytest.fixture
def search_spec(flare_git_repo):
    payload = copy.deepcopy(REGISTERED_SEARCH_SPEC_PAYLOAD)
    payload["pinned_head"] = flare_git_repo.head
    payload["expected_revision_count"] = flare_git_repo.revision_count
    return FlareSearchSpec.model_validate(payload)


@pytest.fixture
def initial_search_spec(search_spec):
    return search_spec


@pytest.fixture
def expanded_search_spec(search_spec):
    return search_spec


@pytest.fixture
def search_registration():
    return SearchRegistration(
        project_commit_oid="a" * 40,
        repo_relative_path="scenarios/flare_corpus/search-spec.json",
    )


@pytest.fixture
def blob_dir(tmp_path):
    return tmp_path / "blobs"


@pytest.fixture
def initial_discovery(flare_git_repo, search_spec, search_registration, blob_dir):
    return discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "initial",
        blob_dir,
    )


@pytest.fixture
def expanded_discovery(flare_git_repo, search_spec, search_registration, blob_dir):
    return discover_candidates(
        ReadOnlyGitRepo(flare_git_repo.path),
        search_spec,
        search_registration,
        "expanded",
        blob_dir,
    )


@pytest.fixture
def discovered_ledger(initial_discovery):
    return initial_discovery


def _eight_groups(discovery: DiscoveryLedger, flare_git_repo):
    specs = [
        ("group-root", [flare_git_repo.root], "dead_quest"),
        ("group-quest", [flare_git_repo.quest_fix], "dead_quest"),
        (
            "group-multicommit",
            [
                flare_git_repo.multicommit_a,
                flare_git_repo.multicommit_b,
                flare_git_repo.multicommit_c,
            ],
            "unsatisfiable_completion",
        ),
        ("group-reference", [flare_git_repo.reference_fix], "cyclic_dependency"),
        ("group-spawn", [flare_git_repo.spawn_fix], "missing_drop_source"),
        ("group-status", [flare_git_repo.status_fix], "unsatisfiable_completion"),
        ("group-chest", [flare_git_repo.chest_fix], "cyclic_dependency"),
        ("group-loot", [flare_git_repo.loot_fix], "missing_drop_source"),
    ]
    return [
        _group_decision(discovery, group_id, commits, defect_class)
        for group_id, commits, defect_class in specs
    ]


def _duplicate_oids(flare_git_repo) -> set[str]:
    return {
        flare_git_repo.loot_cherry_pick,
        flare_git_repo.backport,
        flare_git_repo.loot_revert,
        flare_git_repo.merge_commit,
    }


@pytest.fixture
def positive_evidence(initial_discovery, flare_git_repo):
    groups = _eight_groups(initial_discovery, flare_git_repo)
    grouped = {oid for group in groups for oid in group.commits}
    dispositions = _candidate_dispositions(
        initial_discovery,
        grouped,
        _duplicate_oids(flare_git_repo),
    )
    resolutions = _lineage_resolutions(initial_discovery, groups)
    return _approved_evidence(
        initial_discovery,
        evidence_revision="initial-positive-r1",
        groups=groups,
        dispositions=dispositions,
        resolutions=resolutions,
    )


@pytest.fixture
def evidence_with_multicommit_group(positive_evidence):
    return positive_evidence


@pytest.fixture
def evidence_with_candidate_exclusions(positive_evidence):
    return positive_evidence


@pytest.fixture
def evidence_with_merge_group(positive_evidence, initial_discovery, flare_git_repo):
    merge_group = _group_decision(
        initial_discovery,
        "group-merge",
        [flare_git_repo.merge_commit],
        "missing_drop_source",
    )
    candidate_decisions = [
        item
        for item in positive_evidence.candidate_decisions
        if item.commit_oid != flare_git_repo.merge_commit
    ]
    return _refresh_evidence(
        positive_evidence,
        group_decisions=[*positive_evidence.group_decisions, merge_group],
        candidate_decisions=candidate_decisions,
    )


@pytest.fixture
def multilabel_evidence(positive_evidence):
    first = positive_evidence.group_decisions[0]
    rejected = first.case_decisions[0].model_copy(
        update={
            "case_id": "case-group-root-rejected-reference",
            "defect_class": "dangling_reference",
            "disposition": "rejected",
            "rationale": "The same group is not a dangling-reference case.",
        }
    )
    changed_first = first.model_copy(
        update={"case_decisions": [*first.case_decisions, rejected]}
    )
    return _refresh_evidence(
        positive_evidence,
        group_decisions=[changed_first, *positive_evidence.group_decisions[1:]],
    )


@pytest.fixture
def evidence_proposing_prob_sum(positive_evidence):
    first = positive_evidence.group_decisions[0]
    changed_case = first.case_decisions[0].model_copy(
        update={
            "case_id": "case-group-root-probability",
            "defect_class": "prob_sum_ne_1",
        }
    )
    changed_first = first.model_copy(update={"case_decisions": [changed_case]})
    return _refresh_evidence(
        positive_evidence,
        group_decisions=[changed_first, *positive_evidence.group_decisions[1:]],
    )


@pytest.fixture
def initial_insufficient_evidence(initial_discovery, flare_git_repo):
    groups = [
        group
        for group in _eight_groups(initial_discovery, flare_git_repo)
        if group.fix_group_id != "group-chest"
    ]
    grouped = {oid for group in groups for oid in group.commits}
    dispositions = _candidate_dispositions(
        initial_discovery,
        grouped,
        _duplicate_oids(flare_git_repo),
    )
    return _approved_evidence(
        initial_discovery,
        evidence_revision="initial-insufficient-r1",
        groups=groups,
        dispositions=dispositions,
        resolutions=_lineage_resolutions(initial_discovery, groups),
    )


@pytest.fixture
def initial_ledger(initial_discovery, initial_insufficient_evidence):
    return _expected_candidate_ledger(
        initial_discovery,
        initial_insufficient_evidence,
    )


@pytest.fixture
def initial_decision(initial_ledger):
    return B0ADecision(
        candidate_ledger_sha256=sha256_hex(canonical_bytes(initial_ledger)),
        gate=initial_ledger.gate_summary,
    )


def _expanded_evidence(
    expanded_discovery,
    initial_insufficient_evidence,
    initial_ledger,
    initial_decision,
    flare_git_repo,
    *,
    remote_disposition: str,
    revision: str,
):
    remote_group = _group_decision(
        expanded_discovery,
        "group-remote",
        [flare_git_repo.remote_backport_source],
        "dead_quest",
        disposition=remote_disposition,
    )
    groups = [*initial_insufficient_evidence.group_decisions, remote_group]
    prior_candidate_oids = {
        item.commit_oid for item in initial_insufficient_evidence.candidate_decisions
    }
    candidates = _candidate_index(expanded_discovery)
    added_dispositions = [
        CandidateDisposition(
            commit_oid=flare_git_repo.backport,
            disposition="rejected",
            reason_code="revert_or_duplicate",
            rationale="The manual backport duplicates the reviewed remote source.",
            evidence_refs=[_patch_ref(candidates[flare_git_repo.backport])],
            adjudicator_id=_ADJUDICATOR_ID,
            reviewer_id=_REVIEWER_ID,
        )
    ]
    assert flare_git_repo.backport not in prior_candidate_oids
    initial_link_ids = {
        item.link_id for item in initial_insufficient_evidence.lineage_resolutions
    }
    resolutions = [
        *initial_insufficient_evidence.lineage_resolutions,
        *[
            item
            for item in _lineage_resolutions(expanded_discovery, groups)
            if item.link_id not in initial_link_ids
        ],
    ]
    return _approved_evidence(
        expanded_discovery,
        evidence_revision=revision,
        groups=groups,
        dispositions=[
            *initial_insufficient_evidence.candidate_decisions,
            *added_dispositions,
        ],
        resolutions=resolutions,
        prior_candidate_ledger_sha256=sha256_hex(canonical_bytes(initial_ledger)),
        prior_decision_sha256=sha256_hex(canonical_bytes(initial_decision)),
    )


@pytest.fixture
def expanded_evidence(
    expanded_discovery,
    initial_insufficient_evidence,
    initial_ledger,
    initial_decision,
    flare_git_repo,
):
    return _expanded_evidence(
        expanded_discovery,
        initial_insufficient_evidence,
        initial_ledger,
        initial_decision,
        flare_git_repo,
        remote_disposition="proposed",
        revision="expanded-positive-r1",
    )


@pytest.fixture
def expanded_insufficient_evidence(
    expanded_discovery,
    initial_insufficient_evidence,
    initial_ledger,
    initial_decision,
    flare_git_repo,
):
    return _expanded_evidence(
        expanded_discovery,
        initial_insufficient_evidence,
        initial_ledger,
        initial_decision,
        flare_git_repo,
        remote_disposition="rejected",
        revision="expanded-insufficient-r1",
    )


@pytest.fixture
def foreign_initial_pair_factory(
    initial_ledger,
    initial_decision,
):
    def factory(binding_field, expanded_evidence):
        replacements = {
            "search_frame": initial_ledger.search_frame.model_copy(
                update={"pinned_head": "f" * 40}
            ),
            "search_spec_sha256": "f" * 64,
            "search_registration": initial_ledger.search_registration.model_copy(
                update={"project_commit_oid": "b" * 40}
            ),
            "observed_revision_count": initial_ledger.observed_revision_count + 1,
            "discovery_tool": initial_ledger.discovery_tool.model_copy(
                update={"tool_version": "foreign-flare-discovery@1"}
            ),
        }
        if binding_field not in replacements:
            raise AssertionError(f"unknown prior binding field: {binding_field}")
        foreign_ledger = initial_ledger.model_copy(
            update={binding_field: replacements[binding_field]}
        )
        foreign_decision = initial_decision.model_copy(
            update={
                "candidate_ledger_sha256": sha256_hex(canonical_bytes(foreign_ledger))
            }
        )
        rebound_evidence = _refresh_evidence(
            expanded_evidence,
            prior_candidate_ledger_sha256=sha256_hex(canonical_bytes(foreign_ledger)),
            prior_decision_sha256=sha256_hex(canonical_bytes(foreign_decision)),
        )
        return foreign_ledger, foreign_decision, rebound_evidence

    return factory


def _write_canonical(path, model):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(model))
    return path


@pytest.fixture
def search_spec_path(tmp_path, search_spec):
    return _write_canonical(tmp_path / "search-spec.json", search_spec)


@pytest.fixture
def initial_discovered_path(tmp_path, initial_discovery):
    return _write_canonical(
        tmp_path / "initial" / "candidate-ledger.discovered.json",
        initial_discovery,
    )


@pytest.fixture
def expanded_discovered_path(tmp_path, expanded_discovery):
    return _write_canonical(
        tmp_path / "expanded" / "candidate-ledger.discovered.json",
        expanded_discovery,
    )


@pytest.fixture
def initial_positive_evidence_path(tmp_path, positive_evidence):
    return _write_canonical(
        tmp_path / "initial" / "positive-adjudication-evidence.json",
        positive_evidence,
    )


@pytest.fixture
def initial_insufficient_evidence_path(tmp_path, initial_insufficient_evidence):
    return _write_canonical(
        tmp_path / "initial" / "insufficient-adjudication-evidence.json",
        initial_insufficient_evidence,
    )


@pytest.fixture
def expanded_evidence_path(tmp_path, expanded_evidence):
    return _write_canonical(
        tmp_path / "expanded" / "adjudication-evidence.json",
        expanded_evidence,
    )


@pytest.fixture
def expanded_insufficient_evidence_path(tmp_path, expanded_insufficient_evidence):
    return _write_canonical(
        tmp_path / "expanded" / "insufficient-adjudication-evidence.json",
        expanded_insufficient_evidence,
    )


@pytest.fixture
def initial_ledger_path(tmp_path, initial_ledger):
    return _write_canonical(
        tmp_path / "initial" / "candidate-ledger.json",
        initial_ledger,
    )


@pytest.fixture
def initial_decision_path(tmp_path, initial_decision):
    return _write_canonical(
        tmp_path / "initial" / "b0a-decision.json",
        initial_decision,
    )


@pytest.fixture
def blob_paths(blob_dir, initial_discovery, expanded_discovery):
    return sorted(path for path in blob_dir.iterdir() if path.is_file())
