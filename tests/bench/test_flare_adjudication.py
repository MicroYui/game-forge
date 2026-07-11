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
    B0ADecision,
    CandidateCase,
    CandidateFixGroup,
    CandidateGroupDecision,
    CandidateLedger,
    DiffEvidence,
    DiscoveryLedger,
    EvidenceCounts,
    EvidenceArtifact,
    EvidenceRef,
    LineageLink,
    LineageResolution,
    SelectedParentEdge,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.taxonomy import DefectClass


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
            affected = sorted({*resolution.affected_group_ids, fix_group_id})
            resolution = resolution.model_copy(update={"affected_group_ids": affected})
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
    validate: bool = True,
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
    if not validate:
        return changed.model_copy(update={"review_attestation": attestation})
    return AdjudicationEvidence.model_validate(
        {**payload, "review_attestation": attestation.model_dump(mode="json")}
    )


def _separate_patch_evidence(
    discovery: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    *,
    source_oid: str,
    source_group_id: str,
    source_refs: Sequence[EvidenceRef],
    target_oid: str,
    target_group_id: str,
    target_refs: Sequence[EvidenceRef],
    validate: bool = True,
) -> tuple[DiscoveryLedger, AdjudicationEvidence]:
    link = _patch_link(
        source_oid=source_oid,
        target_oid=target_oid,
        patch_id=_oid(910),
    )
    expanded_discovery = _with_objective_links(discovery, link)
    groups = [
        group.model_copy(update={"root_cause_evidence_refs": list(source_refs)})
        if group.fix_group_id == source_group_id
        else group.model_copy(update={"root_cause_evidence_refs": list(target_refs)})
        if group.fix_group_id == target_group_id
        else group
        for group in evidence.group_decisions
    ]
    changed = evidence.model_copy(update={"group_decisions": groups})
    rebound = _rebind_evidence_to_discovery(
        changed,
        expanded_discovery,
        appended_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="separate",
                affected_group_ids=sorted([source_group_id, target_group_id]),
                rationale="Synthetic patch collision has separately reviewed root causes.",
            )
        ],
        validate=validate,
    )
    return expanded_discovery, rebound


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


def _endpoint_group_ids(discovery, evidence, resolution):
    link = next(
        item for item in discovery.objective_lineage_links if item.link_id == resolution.link_id
    )
    group_by_commit = {
        oid: group.fix_group_id for group in evidence.group_decisions for oid in group.commits
    }
    return sorted(
        {
            group_by_commit[oid]
            for oid in (link.source_oid, link.target_oid)
            if oid in group_by_commit
        }
    )


def test_lineage_affected_groups_are_exact_for_zero_and_one_group_endpoints(
    discovered_ledger, positive_evidence
):
    sizes = set()
    for resolution in positive_evidence.lineage_resolutions:
        expected = _endpoint_group_ids(discovered_ledger, positive_evidence, resolution)
        assert resolution.affected_group_ids == expected
        sizes.add(len(expected))
    assert {0, 1} <= sizes
    adjudicate(discovered_ledger, positive_evidence)


def test_lineage_affected_groups_are_exact_for_two_distinct_groups(
    discovered_ledger, positive_evidence, flare_git_repo
):
    root = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-root"
    )
    quest = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-quest"
    )
    discovery, evidence = _separate_patch_evidence(
        discovered_ledger,
        positive_evidence,
        source_oid=flare_git_repo.root,
        source_group_id="group-root",
        source_refs=root.root_cause_evidence_refs,
        target_oid=flare_git_repo.quest_fix,
        target_group_id="group-quest",
        target_refs=quest.root_cause_evidence_refs,
    )
    resolution = evidence.lineage_resolutions[-1]
    assert resolution.affected_group_ids == ["group-quest", "group-root"]
    adjudicate(discovery, evidence)


def test_lineage_resolution_rejects_duplicate_or_nondeterministic_group_ids():
    common = {
        "link_id": "a" * 64,
        "resolution": "same_group",
        "rationale": "Invalid affected-group projection.",
    }
    with pytest.raises(ValueError, match="sorted|unique"):
        LineageResolution(
            **common,
            affected_group_ids=["group-a", "group-a"],
        )
    with pytest.raises(ValueError, match="sorted|order"):
        LineageResolution(
            **common,
            affected_group_ids=["group-z", "group-a"],
        )


def test_lineage_resolution_rejects_unrelated_extra_group(discovered_ledger, positive_evidence):
    resolution = next(
        item for item in positive_evidence.lineage_resolutions if item.affected_group_ids
    )
    expected = _endpoint_group_ids(discovered_ledger, positive_evidence, resolution)
    unrelated = next(
        group.fix_group_id
        for group in positive_evidence.group_decisions
        if group.fix_group_id not in expected
    )
    replacement = resolution.model_copy(
        update={"affected_group_ids": sorted([*expected, unrelated])}
    )
    bad = _refresh_evidence(
        positive_evidence,
        lineage_resolutions=[
            replacement if item.link_id == resolution.link_id else item
            for item in positive_evidence.lineage_resolutions
        ],
    )
    with pytest.raises(AdjudicationError, match="exact|affected group"):
        adjudicate(discovered_ledger, bad)


def test_lineage_resolution_rejects_omitted_endpoint_group(
    discovered_ledger, positive_evidence, flare_git_repo
):
    root = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-root"
    )
    quest = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-quest"
    )
    discovery, evidence = _separate_patch_evidence(
        discovered_ledger,
        positive_evidence,
        source_oid=flare_git_repo.root,
        source_group_id="group-root",
        source_refs=root.root_cause_evidence_refs,
        target_oid=flare_git_repo.quest_fix,
        target_group_id="group-quest",
        target_refs=quest.root_cause_evidence_refs,
    )
    resolution = evidence.lineage_resolutions[-1]
    replacement = resolution.model_copy(
        update={"affected_group_ids": resolution.affected_group_ids[:1]}
    )
    bad = _refresh_evidence(
        evidence,
        lineage_resolutions=[*evidence.lineage_resolutions[:-1], replacement],
    )
    with pytest.raises(AdjudicationError, match="exact|affected group"):
        adjudicate(discovery, bad)


def _discovery_with_non_config_target(discovery, target_oid):
    payload = discovery.model_dump(mode="json", exclude_none=True)
    candidate = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] == target_oid
    )
    candidate["changed_paths"] = sorted([*candidate["changed_paths"], f"engine/{target_oid}.py"])
    candidate["config_only"] = False
    universe = {
        "schema_version": payload["schema_version"],
        "search_spec_sha256": payload["search_spec_sha256"],
        "search_round": payload["search_round"],
        "discovered_candidates": payload["discovered_candidates"],
        "objective_lineage_links": payload["objective_lineage_links"],
    }
    payload["candidate_universe_sha256"] = sha256_hex(canonical_bytes(universe))
    return DiscoveryLedger.model_validate(payload)


def _evidence_with_non_config_target(evidence, discovery, target_oid):
    decisions = [
        item.model_copy(
            update={
                "reason_code": "non_config_only",
                "rationale": "The lineage target also changes engine code.",
            }
        )
        if item.commit_oid == target_oid
        else item
        for item in evidence.candidate_decisions
    ]
    assert any(item.commit_oid == target_oid for item in decisions)
    changed = evidence.model_copy(update={"candidate_decisions": decisions})
    return _rebind_evidence_to_discovery(
        changed,
        discovery,
        appended_resolutions=[],
    )


@pytest.mark.parametrize("link_type", ["cherry_pick", "revert"])
def test_initial_mixed_trailer_target_uses_non_config_only_precedence(
    link_type, discovered_ledger, positive_evidence
):
    link = next(
        item for item in discovered_ledger.objective_lineage_links if item.link_type == link_type
    )
    discovery = _discovery_with_non_config_target(discovered_ledger, link.target_oid)
    evidence = _evidence_with_non_config_target(positive_evidence, discovery, link.target_oid)
    ledger, _ = adjudicate(discovery, evidence)
    decision = next(
        item for item in ledger.candidate_decisions if item.commit_oid == link.target_oid
    )
    assert decision.reason_code == "non_config_only"


def test_expanded_mixed_backport_target_uses_non_config_only_precedence(
    expanded_discovery,
    expanded_evidence,
    initial_prior_artifacts,
):
    link = next(
        item for item in expanded_discovery.objective_lineage_links if item.link_type == "backport"
    )
    discovery = _discovery_with_non_config_target(expanded_discovery, link.target_oid)
    evidence = _evidence_with_non_config_target(expanded_evidence, discovery, link.target_oid)
    ledger, _ = adjudicate(discovery, evidence, *initial_prior_artifacts)
    decision = next(
        item for item in ledger.candidate_decisions if item.commit_oid == link.target_oid
    )
    assert decision.reason_code == "non_config_only"


def test_duplicate_case_id_across_evidence_groups_is_rejected(discovered_ledger, positive_evidence):
    first, second = positive_evidence.group_decisions[:2]
    duplicate = first.case_decisions[0].model_copy(
        update={"case_id": second.case_decisions[0].case_id}
    )
    changed_first = first.model_copy(update={"case_decisions": [duplicate]})
    bad = _refresh_evidence_without_validation(
        positive_evidence,
        group_decisions=[changed_first, *positive_evidence.group_decisions[1:]],
    )
    with pytest.raises(AdjudicationError, match="case_id"):
        adjudicate(discovered_ledger, bad)


def test_duplicate_defect_class_within_evidence_group_is_rejected(
    discovered_ledger, positive_evidence
):
    first = positive_evidence.group_decisions[0]
    duplicate = first.case_decisions[0].model_copy(update={"case_id": "duplicate-class-case"})
    changed_first = first.model_copy(update={"case_decisions": [*first.case_decisions, duplicate]})
    bad = _refresh_evidence_without_validation(
        positive_evidence,
        group_decisions=[changed_first, *positive_evidence.group_decisions[1:]],
    )
    with pytest.raises(AdjudicationError, match="defect class"):
        adjudicate(discovered_ledger, bad)


def test_candidate_ledger_contract_rejects_duplicate_case_ids_across_groups(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    payload = ledger.model_dump(mode="json", exclude_none=True)
    payload["groups"][1]["cases"][0]["case_id"] = payload["groups"][0]["cases"][0]["case_id"]
    with pytest.raises(ValueError, match="case_id.*globally unique"):
        CandidateLedger.model_validate(payload)


def test_candidate_ledger_contract_rejects_duplicate_class_within_group(
    discovered_ledger, positive_evidence
):
    ledger, _ = adjudicate(discovered_ledger, positive_evidence)
    payload = ledger.model_dump(mode="json", exclude_none=True)
    duplicate = {
        **payload["groups"][0]["cases"][0],
        "case_id": "duplicate-output-class-case",
    }
    payload["groups"][0]["cases"].append(duplicate)
    with pytest.raises(ValueError, match="defect class"):
        CandidateLedger.model_validate(payload)


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
    initial_prior_artifacts,
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
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


def test_expanded_round_cannot_change_initial_root_cause_references(
    expanded_discovery, expanded_evidence, initial_prior_artifacts
):
    groups = list(expanded_evidence.group_decisions)
    groups[0] = groups[0].model_copy(
        update={"root_cause_evidence_refs": groups[1].root_cause_evidence_refs}
    )
    changed = _refresh_evidence(expanded_evidence, group_decisions=groups)
    with pytest.raises(AdjudicationError, match="initial decision"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


def test_expanded_round_cannot_swap_initial_group_adjudicators(
    expanded_discovery, expanded_evidence, initial_prior_artifacts
):
    groups = list(expanded_evidence.group_decisions)
    assert groups[0].adjudicator_id != groups[1].adjudicator_id
    first_id, second_id = groups[0].adjudicator_id, groups[1].adjudicator_id
    groups[0] = groups[0].model_copy(update={"adjudicator_id": second_id})
    groups[1] = groups[1].model_copy(update={"adjudicator_id": first_id})
    changed = _refresh_evidence(expanded_evidence, group_decisions=groups)
    with pytest.raises(AdjudicationError, match="initial decision"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


def test_expanded_round_can_only_append_new_top_level_lineage_resolutions(
    expanded_discovery, expanded_evidence, initial_ledger, initial_prior_artifacts
):
    ledger, _ = adjudicate(expanded_discovery, expanded_evidence, *initial_prior_artifacts)
    initial = [canonical_bytes(item) for item in initial_ledger.lineage_resolutions]
    expanded = [canonical_bytes(item) for item in ledger.lineage_resolutions]
    assert expanded[: len(initial)] == initial
    assert len(expanded) > len(initial)


def test_positive_expanded_replay_preserves_all_complete_initial_prefixes(
    expanded_discovery, expanded_evidence, initial_ledger, initial_prior_artifacts
):
    ledger, _ = adjudicate(expanded_discovery, expanded_evidence, *initial_prior_artifacts)
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


def _bind_expanded_to_prior_pair(evidence, prior_ledger, prior_decision, **updates):
    return _refresh_evidence(
        evidence,
        prior_candidate_ledger_sha256=sha256_hex(canonical_bytes(prior_ledger)),
        prior_decision_sha256=sha256_hex(canonical_bytes(prior_decision)),
        **updates,
    )


def _decision_for_ledger(ledger):
    return B0ADecision(
        candidate_ledger_sha256=sha256_hex(canonical_bytes(ledger)),
        gate=ledger.gate_summary,
    )


def _source_artifact(artifact_id, content):
    digest = sha256_hex(content)
    return EvidenceArtifact(
        artifact_id=artifact_id,
        artifact_type="issue",
        source_url=f"https://github.com/flareteam/flare-game/issues/{artifact_id[-1]}",
        retrieval_date="2026-07-10",
        blob_path=f"blobs/{digest}",
        blob_sha256=digest,
    )


def test_expanded_replays_authentic_prior_discovery_and_evidence(
    expanded_discovery,
    expanded_evidence,
    initial_prior_artifacts,
):
    ledger, decision = adjudicate(
        expanded_discovery,
        expanded_evidence,
        *initial_prior_artifacts,
    )
    assert ledger.search_round == "expanded"
    assert decision.candidate_ledger_sha256 == sha256_hex(canonical_bytes(ledger))


@pytest.mark.parametrize("prior_index", range(4))
def test_initial_programmatic_adjudication_forbids_each_prior_artifact(
    prior_index,
    initial_discovery,
    initial_insufficient_evidence,
    initial_prior_artifacts,
):
    supplied = [None, None, None, None]
    supplied[prior_index] = initial_prior_artifacts[prior_index]
    with pytest.raises(AdjudicationError, match="initial.*forbids.*prior"):
        adjudicate(
            initial_discovery,
            initial_insufficient_evidence,
            *supplied,
        )


@pytest.mark.parametrize("omitted_index", range(4))
def test_expanded_programmatic_adjudication_requires_each_prior_artifact(
    omitted_index,
    expanded_discovery,
    expanded_evidence,
    initial_prior_artifacts,
):
    supplied = list(initial_prior_artifacts)
    supplied[omitted_index] = None
    with pytest.raises(AdjudicationError, match="expanded.*requires all four"):
        adjudicate(
            expanded_discovery,
            expanded_evidence,
            *supplied,
        )


def test_expanded_rejects_canonical_forged_empty_prior_pair(
    expanded_discovery,
    expanded_evidence,
    initial_discovery,
    initial_insufficient_evidence,
    initial_ledger,
):
    forged = CandidateLedger.model_validate(
        {
            **initial_ledger.model_dump(mode="json", exclude_none=True),
            "groups": [],
            "candidate_decisions": [],
            "lineage_resolutions": [],
        }
    )
    forged_decision = _decision_for_ledger(forged)
    rebound = _bind_expanded_to_prior_pair(
        expanded_evidence,
        forged,
        forged_decision,
    )
    with pytest.raises(AdjudicationError, match="replay|byte-identical|prior"):
        adjudicate(
            expanded_discovery,
            rebound,
            initial_discovery,
            initial_insufficient_evidence,
            forged,
            forged_decision,
        )


@pytest.mark.parametrize("mutation", ["gate", "matrix", "upstream_hashes"])
def test_expanded_rejects_false_or_rebound_prior_derived_fields(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_discovery,
    initial_insufficient_evidence,
    initial_ledger,
):
    updates = {}
    if mutation == "gate":
        updates["gate_summary"] = initial_ledger.gate_summary.model_copy(
            update={"proposed_groups": 0, "proposed_classes": 0}
        )
    elif mutation == "matrix":
        updates["applicability_matrix"] = [
            ApplicabilityRow(
                defect_class=row.defect_class,
                domain_applicability=row.domain_applicability,
                evidence_counts=EvidenceCounts(),
                implementation_support=row.implementation_support,
            )
            for row in initial_ledger.applicability_matrix
        ]
    else:
        updates.update(
            discovery_ledger_sha256="f" * 64,
            adjudication_evidence_sha256="e" * 64,
        )
    forged = CandidateLedger.model_validate(
        {
            **initial_ledger.model_dump(mode="json", exclude_none=True),
            **{
                name: value.model_dump(mode="json")
                if hasattr(value, "model_dump")
                else [item.model_dump(mode="json") for item in value]
                if isinstance(value, list)
                else value
                for name, value in updates.items()
            },
        }
    )
    forged_decision = _decision_for_ledger(forged)
    rebound = _bind_expanded_to_prior_pair(
        expanded_evidence,
        forged,
        forged_decision,
    )
    with pytest.raises(AdjudicationError, match="replay|byte-identical|prior"):
        adjudicate(
            expanded_discovery,
            rebound,
            initial_discovery,
            initial_insufficient_evidence,
            forged,
            forged_decision,
        )


def test_expanded_source_artifacts_retain_initial_ordered_prefix_and_blob_binding(
    initial_discovery,
    initial_insufficient_evidence,
    expanded_discovery,
    expanded_evidence,
):
    initial_artifact = _source_artifact("prior-source-1", b"prior source bytes\n")
    added_artifact = _source_artifact("expanded-source-2", b"expanded source bytes\n")
    prior_evidence = _refresh_evidence(
        initial_insufficient_evidence,
        source_artifacts=[initial_artifact],
    )
    prior_ledger, prior_decision = adjudicate(initial_discovery, prior_evidence)
    expanded = _bind_expanded_to_prior_pair(
        expanded_evidence,
        prior_ledger,
        prior_decision,
        source_artifacts=[initial_artifact, added_artifact],
    )
    adjudicate(
        expanded_discovery,
        expanded,
        initial_discovery,
        prior_evidence,
        prior_ledger,
        prior_decision,
    )

    rebound_artifact = _source_artifact("prior-source-1", b"different source bytes\n")
    rebound = _bind_expanded_to_prior_pair(
        expanded,
        prior_ledger,
        prior_decision,
        source_artifacts=[rebound_artifact, added_artifact],
    )
    with pytest.raises(AdjudicationError, match="source artifact|ordered prefix"):
        adjudicate(
            expanded_discovery,
            rebound,
            initial_discovery,
            prior_evidence,
            prior_ledger,
            prior_decision,
        )


def test_expanded_retains_initial_applicability_declarations_byte_exactly(
    expanded_discovery,
    expanded_evidence,
    initial_prior_artifacts,
):
    declarations = list(expanded_evidence.applicability_declarations)
    declarations[0] = declarations[0].model_copy(update={"implementation_support": "supported"})
    changed = _refresh_evidence(
        expanded_evidence,
        applicability_declarations=tuple(declarations),
    )
    with pytest.raises(AdjudicationError, match="applicability"):
        adjudicate(
            expanded_discovery,
            changed,
            *initial_prior_artifacts,
        )


def test_expanded_rejects_mutated_initial_candidate_fact(
    initial_discovery,
    initial_insufficient_evidence,
    initial_ledger,
    initial_decision,
    expanded_discovery,
    expanded_evidence,
):
    payload = expanded_discovery.model_dump(mode="json", exclude_none=True)
    initial_oid = initial_discovery.discovered_candidates[0].commit.commit_oid
    candidate = next(
        item
        for item in payload["discovered_candidates"]
        if item["commit"]["commit_oid"] == initial_oid
    )
    candidate["commit"]["subject"] = "Mutated initial subject"
    universe = {
        "schema_version": payload["schema_version"],
        "search_spec_sha256": payload["search_spec_sha256"],
        "search_round": payload["search_round"],
        "discovered_candidates": payload["discovered_candidates"],
        "objective_lineage_links": payload["objective_lineage_links"],
    }
    payload["candidate_universe_sha256"] = sha256_hex(canonical_bytes(universe))
    changed_discovery = DiscoveryLedger.model_validate(payload)
    changed_evidence = _rebind_evidence_to_discovery(
        expanded_evidence,
        changed_discovery,
        appended_resolutions=[],
    )
    with pytest.raises(AdjudicationError, match="initial candidate|immutable"):
        adjudicate(
            changed_discovery,
            changed_evidence,
            initial_discovery,
            initial_insufficient_evidence,
            initial_ledger,
            initial_decision,
        )


@pytest.mark.parametrize("field_name", ["python_version", "unicode_version"])
def test_expanded_prior_search_binds_runtime_versions(
    field_name,
    initial_discovery,
    initial_insufficient_evidence,
    expanded_discovery,
    expanded_evidence,
):
    changed_tool = initial_discovery.discovery_tool.model_copy(
        update={field_name: f"foreign-{field_name}"}
    )
    changed_prior = DiscoveryLedger.model_validate(
        {
            **initial_discovery.model_dump(mode="json", exclude_none=True),
            "discovery_tool": changed_tool.model_dump(mode="json"),
        }
    )
    changed_prior_evidence = _rebind_evidence_to_discovery(
        initial_insufficient_evidence,
        changed_prior,
        appended_resolutions=[],
    )
    changed_ledger, changed_decision = adjudicate(
        changed_prior,
        changed_prior_evidence,
    )
    rebound = _bind_expanded_to_prior_pair(
        expanded_evidence,
        changed_ledger,
        changed_decision,
    )
    with pytest.raises(AdjudicationError, match="same registered search|runtime"):
        adjudicate(
            expanded_discovery,
            rebound,
            changed_prior,
            changed_prior_evidence,
            changed_ledger,
            changed_decision,
        )


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_candidate_decisions_are_an_unchanged_ordered_prefix(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_prior_artifacts,
):
    changed = mutate_initial_candidate_decisions(expanded_evidence, initial_ledger, mutation)
    with pytest.raises(AdjudicationError, match="initial decision|ordered prefix"):
        adjudicate(expanded_discovery, changed, *initial_prior_artifacts)


@pytest.mark.parametrize("mutation", ["change", "reorder", "prepend"])
def test_expanded_lineage_resolutions_are_an_unchanged_ordered_prefix(
    mutation,
    expanded_discovery,
    expanded_evidence,
    initial_ledger,
    initial_prior_artifacts,
):
    changed = mutate_initial_lineage_resolutions(expanded_evidence, initial_ledger, mutation)
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
    binding_field,
    expanded_discovery,
    expanded_evidence,
    foreign_initial_pair_factory,
    initial_discovery,
    initial_insufficient_evidence,
):
    foreign_initial_ledger, foreign_initial_decision, rebound_evidence = (
        foreign_initial_pair_factory(binding_field, expanded_evidence)
    )
    with pytest.raises(
        AdjudicationError,
        match="replay|byte-identical|same registered search",
    ):
        adjudicate(
            expanded_discovery,
            rebound_evidence,
            initial_discovery,
            initial_insufficient_evidence,
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
    initial_prior_artifacts,
    flare_git_repo,
):
    bad = _add_counted_lineage_endpoint(
        expanded_discovery,
        expanded_evidence,
        fix_group_id="group-backport-copy",
        commit_oid=flare_git_repo.backport,
    )
    with pytest.raises(AdjudicationError, match="backport|independent|revert_or_duplicate"):
        adjudicate(expanded_discovery, bad, *initial_prior_artifacts)


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
    initial_prior_artifacts,
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
    ledger, _ = adjudicate(discovery, evidence, *initial_prior_artifacts)
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


def test_separate_patch_groups_require_distinct_unordered_root_cause_ref_sets(
    discovered_ledger, positive_evidence, flare_git_repo
):
    root = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-root"
    )
    quest = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-quest"
    )
    first_ref = root.root_cause_evidence_refs[0]
    second_ref = quest.root_cause_evidence_refs[0]
    discovery, evidence = _separate_patch_evidence(
        discovered_ledger,
        positive_evidence,
        source_oid=flare_git_repo.root,
        source_group_id="group-root",
        source_refs=[first_ref, second_ref],
        target_oid=flare_git_repo.quest_fix,
        target_group_id="group-quest",
        target_refs=[second_ref, first_ref],
    )
    with pytest.raises(AdjudicationError, match="root-cause evidence"):
        adjudicate(discovery, evidence)


def test_duplicate_root_cause_ref_within_group_is_rejected(
    discovered_ledger, positive_evidence, flare_git_repo
):
    root = next(
        group for group in positive_evidence.group_decisions if group.fix_group_id == "group-root"
    )
    first_ref = root.root_cause_evidence_refs[0]
    discovery, evidence = _separate_patch_evidence(
        discovered_ledger,
        positive_evidence,
        source_oid=flare_git_repo.root,
        source_group_id="group-root",
        source_refs=[first_ref],
        target_oid=flare_git_repo.quest_fix,
        target_group_id="group-quest",
        target_refs=[first_ref, first_ref],
        validate=False,
    )
    with pytest.raises(AdjudicationError, match="root-cause evidence|duplicate"):
        adjudicate(discovery, evidence)


def test_non_config_candidate_cannot_be_disposed_as_non_bug(
    discovered_ledger, positive_evidence, flare_git_repo
):
    mixed = next(
        item
        for item in positive_evidence.candidate_decisions
        if item.commit_oid == flare_git_repo.mixed_fix
    )
    assert mixed.reason_code == "non_config_only"
    changed = mixed.model_copy(
        update={
            "reason_code": "non_bug",
            "rationale": "Invalidly relabeled non-config fixture candidate.",
        }
    )
    bad = _refresh_evidence(
        positive_evidence,
        candidate_decisions=[
            changed if item.commit_oid == mixed.commit_oid else item
            for item in positive_evidence.candidate_decisions
        ],
    )
    with pytest.raises(AdjudicationError, match="non_config_only|config_only"):
        adjudicate(discovered_ledger, bad)


def test_config_only_candidate_cannot_be_disposed_as_non_config_only(
    discovered_ledger, positive_evidence, flare_git_repo
):
    candidates = {item.commit.commit_oid: item for item in discovered_ledger.discovered_candidates}
    merge = next(
        item
        for item in positive_evidence.candidate_decisions
        if item.commit_oid == flare_git_repo.merge_commit
    )
    assert candidates[merge.commit_oid].config_only
    changed = merge.model_copy(
        update={
            "reason_code": "non_config_only",
            "rationale": "Invalidly relabeled config-only fixture candidate.",
        }
    )
    bad = _refresh_evidence(
        positive_evidence,
        candidate_decisions=[
            changed if item.commit_oid == merge.commit_oid else item
            for item in positive_evidence.candidate_decisions
        ],
    )
    with pytest.raises(AdjudicationError, match="non_config_only|config_only"):
        adjudicate(discovered_ledger, bad)


def test_public_gate_rejects_proposed_case_on_not_applicable_row():
    groups = make_groups(8, 4)
    case = groups[0].cases[0].model_copy(update={"defect_class": DefectClass.prob_sum_ne_1})
    groups[0] = groups[0].model_copy(update={"cases": [case]})
    with pytest.raises(AdjudicationError, match="not_applicable"):
        evaluate_provisional_gate(groups, complete_matrix(), "expanded")
