from __future__ import annotations

from collections.abc import Sequence

import pytest

from gameforge.bench.flare_adjudication import (
    AdjudicationError,
    adjudicate,
    evaluate_provisional_gate,
)
from gameforge.bench.flare_evidence import (
    B0A_DEFECT_CLASSES,
    AdjudicationEvidence,
    ApplicabilityRow,
    CandidateCase,
    CandidateFixGroup,
    CandidateGroupDecision,
    CandidateLedger,
    DiffEvidence,
    DiscoveryLedger,
    EvidenceCounts,
    EvidenceRef,
    LineageLink,
    LineageResolution,
    SelectedParentEdge,
    canonical_bytes,
    sha256_hex,
)


_APPLICABLE_CLASSES = (
    "dead_quest",
    "unsatisfiable_completion",
    "cyclic_dependency",
    "missing_drop_source",
)


def _oid(value: int) -> str:
    return f"{value:040x}"


def _refresh_evidence(
    evidence: AdjudicationEvidence,
    **updates: object,
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


def _refresh_evidence_without_validation(
    evidence: AdjudicationEvidence,
    **updates: object,
) -> AdjudicationEvidence:
    """Build an attested copy whose one intentional violation is model-level."""

    changed = evidence.model_copy(update=updates)
    payload = changed.model_dump(
        mode="json",
        exclude={"review_attestation"},
        exclude_none=True,
    )
    attestation = changed.review_attestation.model_copy(
        update={"reviewed_payload_sha256": sha256_hex(canonical_bytes(payload))}
    )
    return changed.model_copy(update={"review_attestation": attestation})


def _replace_group(
    evidence: AdjudicationEvidence,
    fix_group_id: str,
    replacement: CandidateGroupDecision,
) -> AdjudicationEvidence:
    return _refresh_evidence(
        evidence,
        group_decisions=[
            replacement if item.fix_group_id == fix_group_id else item
            for item in evidence.group_decisions
        ],
    )


def real_selected_edges(
    discovery: DiscoveryLedger,
    commits: Sequence[str],
) -> list[SelectedParentEdge]:
    candidates = {item.commit.commit_oid: item for item in discovery.discovered_candidates}
    return [
        SelectedParentEdge(
            commit_oid=oid,
            parent_oid=candidates[oid].commit.diff_base_oid,
        )
        for oid in commits
    ]


def replace_group_commits(
    evidence: AdjudicationEvidence,
    *,
    fix_group_id: str,
    commits: Sequence[str],
    selected_parent_edges: Sequence[SelectedParentEdge],
) -> AdjudicationEvidence:
    original = next(item for item in evidence.group_decisions if item.fix_group_id == fix_group_id)
    replacement = original.model_copy(
        update={
            "commits": list(commits),
            "selected_parent_edges": list(selected_parent_edges),
        }
    )
    return _replace_group(evidence, fix_group_id, replacement)


def replace_selected_parent(
    evidence: AdjudicationEvidence,
    *,
    commit_oid: str,
    parent_oid: str,
) -> AdjudicationEvidence:
    group = next(item for item in evidence.group_decisions if commit_oid in item.commits)
    edges = [
        edge.model_copy(update={"parent_oid": parent_oid})
        if edge.commit_oid == commit_oid
        else edge
        for edge in group.selected_parent_edges
    ]
    return _replace_group(
        evidence,
        group.fix_group_id,
        group.model_copy(update={"selected_parent_edges": edges}),
    )


def replace_group_rationale(
    evidence: AdjudicationEvidence,
    *,
    fix_group_id: str,
    rationale: str,
) -> AdjudicationEvidence:
    group = next(item for item in evidence.group_decisions if item.fix_group_id == fix_group_id)
    return _replace_group(
        evidence,
        fix_group_id,
        group.model_copy(update={"rationale": rationale}),
    )


def replace_first_evidence_ref(
    evidence: AdjudicationEvidence,
    target_id: str,
) -> AdjudicationEvidence:
    group = evidence.group_decisions[0]
    case = group.case_decisions[0]
    changed_case = case.model_copy(
        update={
            "evidence_refs": [
                case.evidence_refs[0].model_copy(update={"target_id": target_id}),
                *case.evidence_refs[1:],
            ]
        }
    )
    changed_group = group.model_copy(
        update={"case_decisions": [changed_case, *group.case_decisions[1:]]}
    )
    return _replace_group(evidence, group.fix_group_id, changed_group)


def replace_reviewed_payload_hash(
    evidence: AdjudicationEvidence,
    digest: str,
) -> AdjudicationEvidence:
    return evidence.model_copy(
        update={
            "review_attestation": evidence.review_attestation.model_copy(
                update={"reviewed_payload_sha256": digest}
            )
        }
    )


def mutate_initial_candidate_decisions(
    evidence: AdjudicationEvidence,
    prior_ledger,
    mutation: str,
) -> AdjudicationEvidence:
    decisions = list(evidence.candidate_decisions)
    prefix_length = len(prior_ledger.candidate_decisions)
    assert prefix_length >= 2
    assert len(decisions) > prefix_length
    if mutation == "change":
        decisions[0] = decisions[0].model_copy(
            update={"rationale": "Changed after the initial adjudication."}
        )
    elif mutation == "reorder":
        decisions[0], decisions[1] = decisions[1], decisions[0]
    elif mutation == "prepend":
        decisions = [decisions[-1], *decisions[:-1]]
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return _refresh_evidence(evidence, candidate_decisions=decisions)


def mutate_initial_lineage_resolutions(
    evidence: AdjudicationEvidence,
    prior_ledger,
    mutation: str,
) -> AdjudicationEvidence:
    resolutions = list(evidence.lineage_resolutions)
    prefix_length = len(prior_ledger.lineage_resolutions)
    assert prefix_length >= 2
    assert len(resolutions) > prefix_length
    if mutation == "change":
        resolutions[0] = resolutions[0].model_copy(
            update={"rationale": "Changed after the initial adjudication."}
        )
    elif mutation == "reorder":
        resolutions[0], resolutions[1] = resolutions[1], resolutions[0]
    elif mutation == "prepend":
        resolutions = [resolutions[-1], *resolutions[:-1]]
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return _refresh_evidence(evidence, lineage_resolutions=resolutions)


def make_groups(group_count: int, class_count: int) -> list[CandidateFixGroup]:
    groups: list[CandidateFixGroup] = []
    for index in range(group_count):
        commit_oid = _oid(index + 100)
        patch_sha256 = sha256_hex(f"patch-{index}".encode())
        defect_class = _APPLICABLE_CLASSES[index % class_count]
        groups.append(
            CandidateFixGroup(
                fix_group_id=f"synthetic-group-{index}",
                group_decision_sha256=sha256_hex(canonical_bytes({"synthetic_group": index})),
                commits=[commit_oid],
                before_commit=_oid(index + 1),
                after_commit=commit_oid,
                after_committed_at=index,
                changed_paths=[f"mods/core/quests/group-{index}.txt"],
                config_only=True,
                diff_evidence=[
                    DiffEvidence(
                        commit_oid=commit_oid,
                        patch_sha256=patch_sha256,
                        patch_blob=f"blobs/{patch_sha256}",
                        commit_message=f"Synthetic fix {index}",
                    )
                ],
                cases=[
                    CandidateCase(
                        case_id=f"synthetic-case-{index}",
                        defect_class=defect_class,
                        disposition="proposed",
                        rationale=f"Synthetic proposed case {index}.",
                        evidence_refs=[EvidenceRef(kind="patch_blob", target_id=patch_sha256)],
                    )
                ],
                disposition_summary="proposed",
                rationale=f"Synthetic group {index}.",
            )
        )
    return groups


def complete_matrix() -> list[ApplicabilityRow]:
    return [
        ApplicabilityRow(
            defect_class=defect_class,
            domain_applicability=(
                "not_applicable"
                if defect_class.value in {"prob_sum_ne_1", "gacha_expectation_violation"}
                else "applicable"
            ),
            evidence_counts=EvidenceCounts(),
            implementation_support="planned",
        )
        for defect_class in B0A_DEFECT_CLASSES
    ]


def _proposed_group_for_commit(
    discovery: DiscoveryLedger,
    *,
    fix_group_id: str,
    commit_oid: str,
) -> CandidateGroupDecision:
    candidate = next(
        item for item in discovery.discovered_candidates if item.commit.commit_oid == commit_oid
    )
    patch_ref = EvidenceRef(
        kind="patch_blob",
        target_id=candidate.diff_evidence.patch_sha256,
    )
    return CandidateGroupDecision(
        fix_group_id=fix_group_id,
        commits=[commit_oid],
        selected_parent_edges=[
            SelectedParentEdge(
                commit_oid=commit_oid,
                parent_oid=candidate.commit.diff_base_oid,
            )
        ],
        root_cause_evidence_refs=[patch_ref],
        case_decisions=[
            CandidateCase(
                case_id=f"case-{fix_group_id}",
                defect_class="missing_drop_source",
                disposition="proposed",
                rationale=f"Attempt to count lineage endpoint {commit_oid}.",
                evidence_refs=[patch_ref],
            )
        ],
        adjudicator_id=f"assisted-review-{fix_group_id}",
        reviewer_id="human-review-1",
        rationale=f"Attempt to count lineage endpoint {commit_oid}.",
    )


def _add_counted_lineage_endpoint(
    discovery: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    *,
    fix_group_id: str,
    commit_oid: str,
) -> AdjudicationEvidence:
    group = _proposed_group_for_commit(
        discovery,
        fix_group_id=fix_group_id,
        commit_oid=commit_oid,
    )
    resolutions: list[LineageResolution] = []
    for resolution in evidence.lineage_resolutions:
        link = next(
            item for item in discovery.objective_lineage_links if item.link_id == resolution.link_id
        )
        if commit_oid in {link.source_oid, link.target_oid}:
            affected = [*resolution.affected_group_ids, fix_group_id]
            resolution = resolution.model_copy(
                update={"affected_group_ids": list(dict.fromkeys(affected))}
            )
        resolutions.append(resolution)
    return _refresh_evidence(
        evidence,
        group_decisions=[*evidence.group_decisions, group],
        candidate_decisions=[
            item for item in evidence.candidate_decisions if item.commit_oid != commit_oid
        ],
        lineage_resolutions=resolutions,
    )


def _patch_link(
    *,
    source_oid: str,
    target_oid: str,
    patch_id: str,
) -> LineageLink:
    payload = {
        "link_type": "patch_id",
        "source_oid": source_oid,
        "target_oid": target_oid,
        "patch_id": patch_id,
    }
    return LineageLink(
        link_id=sha256_hex(canonical_bytes(payload)),
        **payload,
    )


def _with_objective_links(
    discovery: DiscoveryLedger,
    *new_links: LineageLink,
) -> DiscoveryLedger:
    links = sorted(
        [*discovery.objective_lineage_links, *new_links],
        key=lambda link: (
            link.link_type,
            link.source_oid,
            link.target_oid,
            link.rule_id or "",
            link.patch_id or "",
            link.link_id,
        ),
    )
    universe = {
        "schema_version": discovery.schema_version,
        "search_spec_sha256": discovery.search_spec_sha256,
        "search_round": discovery.search_round,
        "discovered_candidates": [
            item.model_dump(mode="json", exclude_none=True)
            for item in discovery.discovered_candidates
        ],
        "objective_lineage_links": [
            item.model_dump(mode="json", exclude_none=True) for item in links
        ],
    }
    payload = discovery.model_dump(mode="json", exclude_none=True)
    payload["objective_lineage_links"] = [
        item.model_dump(mode="json", exclude_none=True) for item in links
    ]
    payload["candidate_universe_sha256"] = sha256_hex(canonical_bytes(universe))
    return DiscoveryLedger.model_validate(payload)


def _rebind_evidence_to_discovery(
    evidence: AdjudicationEvidence,
    discovery: DiscoveryLedger,
    *,
    appended_resolutions: Sequence[LineageResolution],
) -> AdjudicationEvidence:
    changed = evidence.model_copy(
        update={
            "discovery_ledger_sha256": sha256_hex(canonical_bytes(discovery)),
            "candidate_universe_sha256": discovery.candidate_universe_sha256,
            "lineage_resolutions": [
                *evidence.lineage_resolutions,
                *appended_resolutions,
            ],
        }
    )
    payload = changed.model_dump(
        mode="json",
        exclude={"review_attestation"},
        exclude_none=True,
    )
    attestation = changed.review_attestation.model_copy(
        update={
            "candidate_universe_sha256": discovery.candidate_universe_sha256,
            "reviewed_payload_sha256": sha256_hex(canonical_bytes(payload)),
        }
    )
    return AdjudicationEvidence.model_validate(
        {**payload, "review_attestation": attestation.model_dump(mode="json")}
    )


def test_adjudication_groups_contiguous_first_parent_commits_and_counts_groups_not_commits(
    discovered_ledger, positive_evidence
):
    ledger, decision = adjudicate(discovered_ledger, positive_evidence)
    assert decision.gate.status == "provisional_pass"
    assert decision.gate.proposed_groups == 8
    assert decision.gate.proposed_classes == 4
    assert decision.candidate_ledger_sha256 == sha256_hex(canonical_bytes(ledger))
    assert ledger.gate_summary == decision.gate
    assert all(group.config_only for group in ledger.groups if group.counts_toward_gate)


def test_group_decision_digest_binds_the_complete_unsorted_reviewed_decision(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    assert [group.fix_group_id for group in ledger.groups] == [
        decision.fix_group_id for decision in positive_evidence.group_decisions
    ]
    assert [group.group_decision_sha256 for group in ledger.groups] == [
        sha256_hex(canonical_bytes(decision)) for decision in positive_evidence.group_decisions
    ]


def test_root_group_uses_discovered_empty_tree_diff_base(
    discovered_ledger, positive_evidence, flare_git_repo
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    root = next(group for group in ledger.groups if group.fix_group_id == "group-root")
    assert root.commits == [flare_git_repo.root]
    assert root.before_commit == flare_git_repo.empty_tree_oid


def test_non_contiguous_group_is_rejected(discovered_ledger, positive_evidence, flare_git_repo):
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
        item.commit
        for item in discovered_ledger.discovered_candidates
        if item.commit.commit_oid == flare_git_repo.merge_commit
    )
    assert len(merge.parent_oids) > 1
    bad = replace_selected_parent(
        evidence_with_merge_group,
        commit_oid=flare_git_repo.merge_commit,
        parent_oid=merge.parent_oids[1],
    )
    with pytest.raises(AdjudicationError, match="first parent|selected parent"):
        adjudicate(discovered_ledger, bad)


def test_contiguous_context_commit_is_required_for_a_multicommit_group(
    discovered_ledger, evidence_with_multicommit_group, flare_git_repo
):
    ledger, _ = adjudicate(discovered_ledger, evidence_with_multicommit_group)
    group = next(item for item in ledger.groups if item.fix_group_id == "group-multicommit")
    assert group.commits == [
        flare_git_repo.multicommit_a,
        flare_git_repo.multicommit_b,
        flare_git_repo.multicommit_c,
    ]
    assert group.before_commit == flare_git_repo.before_multicommit
    assert group.after_commit == flare_git_repo.multicommit_c
    assert group.after_committed_at > 0
    assert group.changed_paths == sorted(group.changed_paths)
    assert [item.commit_oid for item in group.diff_evidence] == group.commits


def test_multilabel_group_uses_case_dispositions_not_group_summary(
    discovered_ledger, multilabel_evidence
):
    ledger, _ = adjudicate(discovered_ledger, multilabel_evidence)
    group = ledger.groups[0]
    assert {case.disposition for case in group.cases} == {"proposed", "rejected"}
    assert group.disposition_summary == "proposed"


def test_non_bug_mixed_and_revert_candidates_are_structured_without_a_class(
    discovered_ledger, evidence_with_candidate_exclusions, flare_git_repo
):
    ledger, _ = adjudicate(discovered_ledger, evidence_with_candidate_exclusions)
    reasons = {item.reason_code for item in ledger.candidate_decisions}
    assert {"non_bug", "non_config_only", "revert_or_duplicate"} <= reasons
    merge = next(
        item
        for item in ledger.candidate_decisions
        if item.commit_oid == flare_git_repo.merge_commit
    )
    assert (merge.reason_code, merge.disposition) == ("non_bug", "rejected")
    grouped = {oid for group in ledger.groups for oid in group.commits}
    excluded = {item.commit_oid for item in ledger.candidate_decisions}
    universe = {item.commit.commit_oid for item in discovered_ledger.discovered_candidates}
    assert grouped.isdisjoint(excluded)
    assert grouped | excluded == universe


def test_matrix_is_exact_and_fixed_flare_applicability_is_enforced(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    rows = {row.defect_class.value: row for row in ledger.applicability_matrix}
    assert len(rows) == 11
    assert rows["prob_sum_ne_1"].domain_applicability == "not_applicable"
    assert rows["gacha_expectation_violation"].domain_applicability == "not_applicable"
    assert rows["economy_collapse"].domain_applicability == "applicable"
    assert all(
        row.evidence_counts.qualified_candidate == row.evidence_counts.accepted == 0
        for row in rows.values()
    )


def test_not_applicable_class_cannot_be_proposed(discovered_ledger, evidence_proposing_prob_sum):
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


def test_discovery_and_candidate_universe_hashes_are_replayed(discovered_ledger, positive_evidence):
    bad_discovery = _refresh_evidence(
        positive_evidence,
        discovery_ledger_sha256="f" * 64,
    )
    with pytest.raises(AdjudicationError, match="discovery.*hash"):
        adjudicate(discovered_ledger, bad_discovery)


@pytest.mark.parametrize(
    ("groups", "classes", "status"),
    [
        (7, 4, "insufficient_evidence"),
        (8, 3, "insufficient_evidence"),
        (8, 4, "provisional_pass"),
    ],
)
def test_gate_boundaries(groups, classes, status):
    assert (
        evaluate_provisional_gate(
            make_groups(groups, classes), complete_matrix(), "expanded"
        ).status
        == status
    )


def test_initial_failure_requires_the_prefrozen_expanded_round():
    gate = evaluate_provisional_gate(make_groups(7, 4), complete_matrix(), "initial")
    assert gate.status == "expanded_round_required"
    assert gate.next_action == "run_expanded_round"


def test_duplicate_group_ids_do_not_inflate_the_standalone_gate():
    groups = make_groups(7, 4)
    gate = evaluate_provisional_gate([*groups, groups[0]], complete_matrix(), "expanded")
    assert gate.proposed_groups == 7
    assert gate.status == "insufficient_evidence"


def test_duplicate_group_ids_are_rejected_before_adjudication_mapping(
    discovered_ledger, positive_evidence
):
    duplicate = positive_evidence.group_decisions[-1].model_copy(
        update={"fix_group_id": positive_evidence.group_decisions[0].fix_group_id}
    )
    bad = _refresh_evidence_without_validation(
        positive_evidence,
        group_decisions=[*positive_evidence.group_decisions[:-1], duplicate],
    )
    with pytest.raises(AdjudicationError, match="fix_group_id|group IDs"):
        adjudicate(discovered_ledger, bad)


def test_candidate_ledger_contract_rejects_duplicate_group_ids(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    payload = ledger.model_dump(mode="json", exclude_none=True)
    payload["groups"][-1]["fix_group_id"] = payload["groups"][0]["fix_group_id"]
    with pytest.raises(ValueError, match="fix_group_id.*globally unique"):
        CandidateLedger.model_validate(payload)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_groups_are_an_unchanged_ordered_digest_prefix(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_decision,
):
    groups = list(expanded_evidence.group_decisions)
    assert len(groups) > len(initial_ledger.groups) >= 2
    if mutation == "change":
        groups[0] = groups[0].model_copy(
            update={"rationale": "Changed after seeing the initial gate."}
        )
    elif mutation == "reorder":
        groups[0], groups[1] = groups[1], groups[0]
    else:
        groups = [groups[-1], *groups[:-1]]
    changed = _refresh_evidence(expanded_evidence, group_decisions=groups)
    with pytest.raises(AdjudicationError, match="initial decision|ordered prefix"):
        adjudicate(expanded_discovery, changed, initial_ledger, initial_decision)


def test_expanded_round_cannot_change_initial_root_cause_references(
    expanded_discovery, expanded_evidence, initial_ledger, initial_decision
):
    groups = list(expanded_evidence.group_decisions)
    groups[0] = groups[0].model_copy(
        update={"root_cause_evidence_refs": groups[1].root_cause_evidence_refs}
    )
    changed = _refresh_evidence(expanded_evidence, group_decisions=groups)
    with pytest.raises(AdjudicationError, match="initial decision"):
        adjudicate(expanded_discovery, changed, initial_ledger, initial_decision)


def test_expanded_round_cannot_swap_initial_group_adjudicators(
    expanded_discovery, expanded_evidence, initial_ledger, initial_decision
):
    groups = list(expanded_evidence.group_decisions)
    assert groups[0].adjudicator_id != groups[1].adjudicator_id
    first_id, second_id = groups[0].adjudicator_id, groups[1].adjudicator_id
    groups[0] = groups[0].model_copy(update={"adjudicator_id": second_id})
    groups[1] = groups[1].model_copy(update={"adjudicator_id": first_id})
    changed = _refresh_evidence(expanded_evidence, group_decisions=groups)
    with pytest.raises(AdjudicationError, match="initial decision"):
        adjudicate(expanded_discovery, changed, initial_ledger, initial_decision)


def test_expanded_round_can_only_append_new_top_level_lineage_resolutions(
    expanded_discovery, expanded_evidence, initial_ledger, initial_decision
):
    ledger, _ = adjudicate(expanded_discovery, expanded_evidence, initial_ledger, initial_decision)
    initial = [canonical_bytes(item) for item in initial_ledger.lineage_resolutions]
    expanded = [canonical_bytes(item) for item in ledger.lineage_resolutions]
    assert expanded[: len(initial)] == initial
    assert len(expanded) > len(initial)


def test_positive_expanded_replay_preserves_all_complete_initial_prefixes(
    expanded_discovery, expanded_evidence, initial_ledger, initial_decision
):
    ledger, _ = adjudicate(expanded_discovery, expanded_evidence, initial_ledger, initial_decision)
    for prior, replayed in zip(initial_ledger.groups, ledger.groups, strict=False):
        assert canonical_bytes(replayed) == canonical_bytes(prior)
    for prior, replayed in zip(
        initial_ledger.candidate_decisions,
        ledger.candidate_decisions,
        strict=False,
    ):
        assert canonical_bytes(replayed) == canonical_bytes(prior)
    for prior, replayed in zip(
        initial_ledger.lineage_resolutions,
        ledger.lineage_resolutions,
        strict=False,
    ):
        assert canonical_bytes(replayed) == canonical_bytes(prior)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_candidate_decisions_are_an_unchanged_ordered_prefix(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_decision,
):
    changed = mutate_initial_candidate_decisions(expanded_evidence, initial_ledger, mutation)
    with pytest.raises(AdjudicationError, match="initial decision|ordered prefix"):
        adjudicate(expanded_discovery, changed, initial_ledger, initial_decision)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_lineage_resolutions_are_an_unchanged_ordered_prefix(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_decision,
):
    changed = mutate_initial_lineage_resolutions(expanded_evidence, initial_ledger, mutation)
    with pytest.raises(AdjudicationError, match="lineage resolution|ordered prefix"):
        adjudicate(expanded_discovery, changed, initial_ledger, initial_decision)


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
    binding_field,
    expanded_discovery,
    expanded_evidence,
    foreign_initial_pair_factory,
):
    foreign_initial_ledger, foreign_initial_decision, rebound_evidence = (
        foreign_initial_pair_factory(binding_field, expanded_evidence)
    )
    with pytest.raises(AdjudicationError, match="same registered search"):
        adjudicate(
            expanded_discovery,
            rebound_evidence,
            foreign_initial_ledger,
            foreign_initial_decision,
        )


def test_cherry_pick_lineage_cannot_count_both_endpoints_as_independent(
    discovered_ledger, positive_evidence, flare_git_repo
):
    bad = _add_counted_lineage_endpoint(
        discovered_ledger,
        positive_evidence,
        fix_group_id="group-loot-cherry-pick",
        commit_oid=flare_git_repo.loot_cherry_pick,
    )
    with pytest.raises(AdjudicationError, match="lineage|independent|revert_or_duplicate"):
        adjudicate(discovered_ledger, bad)


def test_backport_lineage_cannot_count_both_endpoints_as_independent(
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_decision,
    flare_git_repo,
):
    bad = _add_counted_lineage_endpoint(
        expanded_discovery,
        expanded_evidence,
        fix_group_id="group-backport-copy",
        commit_oid=flare_git_repo.backport,
    )
    with pytest.raises(AdjudicationError, match="backport|independent|revert_or_duplicate"):
        adjudicate(expanded_discovery, bad, initial_ledger, initial_decision)


def test_same_fix_lineage_transitive_closure_cannot_inflate_independence(
    discovered_ledger, positive_evidence, flare_git_repo
):
    root_to_mixed = _patch_link(
        source_oid=flare_git_repo.root,
        target_oid=flare_git_repo.mixed_fix,
        patch_id=_oid(900),
    )
    quest_to_mixed = _patch_link(
        source_oid=flare_git_repo.quest_fix,
        target_oid=flare_git_repo.mixed_fix,
        patch_id=_oid(901),
    )
    discovery = _with_objective_links(
        discovered_ledger,
        root_to_mixed,
        quest_to_mixed,
    )
    evidence = _rebind_evidence_to_discovery(
        positive_evidence,
        discovery,
        appended_resolutions=[
            LineageResolution(
                link_id=root_to_mixed.link_id,
                resolution="same_group",
                affected_group_ids=["group-root"],
                rationale="Synthetic fan-in resolves to the same objective fix.",
            ),
            LineageResolution(
                link_id=quest_to_mixed.link_id,
                resolution="same_group",
                affected_group_ids=["group-quest"],
                rationale="Synthetic fan-in resolves to the same objective fix.",
            ),
        ],
    )
    with pytest.raises(AdjudicationError, match="lineage|independent"):
        adjudicate(discovery, evidence)


def test_expanded_new_lineage_link_may_touch_an_initial_group_without_rewriting_decision(
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_decision,
    flare_git_repo,
):
    new_link = _patch_link(
        source_oid=flare_git_repo.root,
        target_oid=flare_git_repo.mixed_fix,
        patch_id=_oid(902),
    )
    discovery = _with_objective_links(expanded_discovery, new_link)
    evidence = _rebind_evidence_to_discovery(
        expanded_evidence,
        discovery,
        appended_resolutions=[
            LineageResolution(
                link_id=new_link.link_id,
                resolution="same_group",
                affected_group_ids=["group-root"],
                rationale="The expanded link adds lineage without changing the decision.",
            )
        ],
    )
    ledger, _ = adjudicate(discovery, evidence, initial_ledger, initial_decision)
    prior_root = next(
        group for group in initial_ledger.groups if group.fix_group_id == "group-root"
    )
    replayed_root = next(group for group in ledger.groups if group.fix_group_id == "group-root")
    assert replayed_root.group_decision_sha256 == prior_root.group_decision_sha256
    assert replayed_root.lineage_links == [*prior_root.lineage_links, new_link.link_id]


def test_non_config_candidate_cannot_hide_in_an_uncounted_typed_group(
    discovered_ledger, positive_evidence, flare_git_repo
):
    group = _proposed_group_for_commit(
        discovered_ledger,
        fix_group_id="group-mixed-rejected",
        commit_oid=flare_git_repo.mixed_fix,
    )
    rejected_case = group.case_decisions[0].model_copy(update={"disposition": "rejected"})
    group = group.model_copy(update={"case_decisions": [rejected_case]})
    bad = _refresh_evidence(
        positive_evidence,
        group_decisions=[*positive_evidence.group_decisions, group],
        candidate_decisions=[
            item
            for item in positive_evidence.candidate_decisions
            if item.commit_oid != flare_git_repo.mixed_fix
        ],
    )
    with pytest.raises(AdjudicationError, match="non-config-only"):
        adjudicate(discovered_ledger, bad)


def test_revert_candidate_cannot_hide_in_an_uncounted_typed_group(
    discovered_ledger, positive_evidence, flare_git_repo
):
    group = _proposed_group_for_commit(
        discovered_ledger,
        fix_group_id="group-loot-revert-rejected",
        commit_oid=flare_git_repo.loot_revert,
    )
    rejected_case = group.case_decisions[0].model_copy(update={"disposition": "rejected"})
    group = group.model_copy(update={"case_decisions": [rejected_case]})
    resolutions = []
    for resolution in positive_evidence.lineage_resolutions:
        link = next(
            item
            for item in discovered_ledger.objective_lineage_links
            if item.link_id == resolution.link_id
        )
        if flare_git_repo.loot_revert in {link.source_oid, link.target_oid}:
            resolution = resolution.model_copy(
                update={
                    "affected_group_ids": [
                        *resolution.affected_group_ids,
                        group.fix_group_id,
                    ]
                }
            )
        resolutions.append(resolution)
    bad = _refresh_evidence(
        positive_evidence,
        group_decisions=[*positive_evidence.group_decisions, group],
        candidate_decisions=[
            item
            for item in positive_evidence.candidate_decisions
            if item.commit_oid != flare_git_repo.loot_revert
        ],
        lineage_resolutions=resolutions,
    )
    with pytest.raises(AdjudicationError, match="revert_or_duplicate|revert endpoint"):
        adjudicate(discovered_ledger, bad)


def test_revert_endpoint_is_always_uncounted(discovered_ledger, positive_evidence, flare_git_repo):
    bad = _add_counted_lineage_endpoint(
        discovered_ledger,
        positive_evidence,
        fix_group_id="group-loot-revert",
        commit_oid=flare_git_repo.loot_revert,
    )
    with pytest.raises(AdjudicationError, match="revert"):
        adjudicate(discovered_ledger, bad)
