"""Source-neutral reviewed-evidence fixtures for adjudication and CLI tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from gameforge.bench.external_corpus.contracts import (
    AdjudicationEvidence,
    AdjudicationPayload,
    B0AProtocol,
    CandidateCase,
    CandidateCommit,
    CandidateDisposition,
    CandidateGroupDecision,
    CandidateOrderTerm,
    CommitMetadata,
    DiffEvidence,
    DiscoveredCandidate,
    DiscoveryLedger,
    DiscoveryTool,
    EvidenceArtifact,
    EvidenceRef,
    HistoryRange,
    LineageLink,
    LineageRegexRule,
    NativeValidatorCommand,
    RegexRule,
    ReviewAttestation,
    SearchRegistration,
    SelectedParentEdge,
    SelectionReason,
    SourceProfile,
    TaxonomyApplicability,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.taxonomy import DefectClass


def oid(value: int) -> str:
    return f"{value:040x}"


def _patch_bytes(index: int, paths: list[str]) -> bytes:
    blocks: list[bytes] = []
    for path in paths:
        value = (
            "eligible-marker\n"
            if index == 9 and path.startswith("data/")
            else f"fixture-{index}-{path}\n"
        )
        blocks.append(
            (
                f"diff --git a/{path} b/{path}\n"
                "new file mode 100644\n"
                "index 0000000..1111111\n"
                "--- /dev/null\n"
                f"+++ b/{path}\n"
                "@@ -0,0 +1 @@\n"
                f"+{value}"
            ).encode()
        )
    return b"".join(blocks)


def taxonomy_rows() -> tuple[TaxonomyApplicability, ...]:
    applicable = set(list(DefectClass)[:4])
    return tuple(
        TaxonomyApplicability(
            defect_class=defect_class,
            domain_applicability=("applicable" if defect_class in applicable else "not_applicable"),
            implementation_support="planned",
            rationale=f"Fixture applicability for {defect_class.value}",
        )
        for defect_class in DefectClass
    )


def source_profile(*, candidate_count: int = 10, config_only_count: int = 10) -> SourceProfile:
    return SourceProfile(
        schema_version="external-source-profile@1",
        source_id="fixture_source",
        profile_version="fixture-source@1",
        repository_url="https://example.test/fixture-source.git",
        pinned_head=oid(9999),
        history_range=HistoryRange(expected_commit_count=candidate_count),
        config_include_globs=("data/**/*.txt",),
        config_exclude_globs=(),
        message_rules=(RegexRule(rule_id="message.fix", pattern="(?i)fix"),),
        diff_rules=(
            RegexRule(rule_id="diff.eligible_marker", pattern="eligible-marker"),
        ),
        lineage_rules=(),
        candidate_order=(
            CandidateOrderTerm(field="committed_at", direction="ascending"),
            CandidateOrderTerm(field="commit_oid", direction="ascending"),
        ),
        license_id="GPL-3.0-or-later",
        notice_files=("LICENSE",),
        native_validator_commands=(
            NativeValidatorCommand(command_id="fixture.parse", argv=("fixture-engine", "-p")),
        ),
        parser_version="fixture-parser@1",
        query_complete_closure=("changed_files", "referenced_records"),
        taxonomy_applicability=taxonomy_rows(),
        qualification_predicate_ids=("fixture.before_after",),
        b0a_protocol=B0AProtocol(
            candidate_limit=candidate_count,
            expected_matched_candidate_count=candidate_count,
            expected_config_only_candidate_count=config_only_count,
            minimum_independent_groups=8,
            minimum_domain_applicable_classes=4,
        ),
    )


def discovery_ledger(*, non_config_indices: set[int] | None = None) -> DiscoveryLedger:
    non_config_indices = {9, *(non_config_indices or set())}
    count = 10
    profile = source_profile(config_only_count=count - len(non_config_indices))
    candidates: list[DiscoveredCandidate] = []
    for index in range(count):
        commit_oid = oid(100 + index)
        parent_oid = oid(1000 + index)
        changed_paths = [f"data/content/{index}.txt"]
        eligible_paths = list(changed_paths)
        if index in non_config_indices:
            changed_paths.append(f"src/runtime_{index}.py")
            changed_paths.sort()
        patch = _patch_bytes(index, changed_paths)
        patch_sha256 = sha256_hex(patch)
        eligible_patch = _patch_bytes(index, eligible_paths) if index == 9 else None
        eligible_patch_sha256 = (
            sha256_hex(eligible_patch) if eligible_patch is not None else None
        )
        commit = CandidateCommit(
            commit_oid=commit_oid,
            parent_oids=[parent_oid],
            selected_parent_oid=parent_oid,
            diff_base_oid=parent_oid,
            committed_at=100 + index,
            subject=f"Fix fixture content {index}",
        )
        candidates.append(
            DiscoveredCandidate(
                commit=commit,
                changed_paths=changed_paths,
                eligible_paths=eligible_paths,
                config_only=index not in non_config_indices,
                selection_reasons=[
                    SelectionReason(
                        kind="direct_match",
                        rule_ids=(
                            ["diff.eligible_marker", "message.fix"]
                            if index == 9
                            else ["message.fix"]
                        ),
                    )
                ],
                diff_evidence=DiffEvidence(
                    commit_oid=commit_oid,
                    patch_sha256=patch_sha256,
                    patch_blob=f"blobs/{patch_sha256}",
                    eligible_patch_sha256=eligible_patch_sha256,
                    eligible_patch_blob=(
                        f"blobs/{eligible_patch_sha256}"
                        if eligible_patch_sha256 is not None
                        else None
                    ),
                    commit_message=f"Fix fixture content {index}\n",
                ),
            )
        )
    profile_sha256 = sha256_hex(canonical_bytes(profile))
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": [item.commit.commit_oid for item in candidates],
    }
    return DiscoveryLedger(
        source_id=profile.source_id,
        source_profile=profile,
        source_profile_sha256=profile_sha256,
        search_registration=SearchRegistration(
            project_commit_oid=oid(9000),
            profile_repo_relative_path="scenarios/external_corpus/fixture/profile.json",
        ),
        observed_history_count=count,
        matched_candidate_count=count,
        config_only_candidate_count=count - len(non_config_indices),
        discovery_tool=DiscoveryTool(
            tool_version="external-discovery@1",
            project_commit_oid=oid(9000),
            git_version="git version fixture",
            python_implementation="CPython",
            python_version="3.12.0",
            python_build=("fixture", "fixture"),
            unicode_version="15.1.0",
        ),
        discovered_candidates=candidates,
        objective_lineage_links=[],
        candidate_universe_sha256=sha256_hex(canonical_bytes(universe)),
    )


def patch_ref(candidate: DiscoveredCandidate) -> EvidenceRef:
    return EvidenceRef(kind="patch_blob", target_id=candidate.diff_evidence.patch_sha256)


def reviewed_evidence(
    discovery: DiscoveryLedger,
    *,
    group_count: int = 8,
    class_count: int = 4,
    group_indices: list[int] | None = None,
    source_artifacts: list[EvidenceArtifact] | None = None,
) -> AdjudicationEvidence:
    applicable_classes = list(DefectClass)[:class_count]
    selected_indices = list(range(group_count)) if group_indices is None else list(group_indices)
    selected_index_set = set(selected_indices)
    groups: list[CandidateGroupDecision] = []
    for case_index, candidate_index in enumerate(selected_indices):
        candidate = discovery.discovered_candidates[candidate_index]
        groups.append(
            CandidateGroupDecision(
                fix_group_id=f"group.{candidate_index}",
                commits=[candidate.commit.commit_oid],
                selected_parent_edges=[
                    SelectedParentEdge(
                        commit_oid=candidate.commit.commit_oid,
                        parent_oid=candidate.commit.diff_base_oid,
                    )
                ],
                root_cause_evidence_refs=[patch_ref(candidate)],
                case_decisions=[
                    CandidateCase(
                        case_id=f"case.{candidate_index}",
                        defect_class=applicable_classes[case_index % len(applicable_classes)],
                        disposition="proposed",
                        rationale=f"Reviewed proposed fixture case {candidate_index}",
                        evidence_refs=[patch_ref(candidate)],
                    )
                ],
                adjudicator_id="adjudicator.fixture",
                rationale=f"Reviewed fixture group {candidate_index}",
            )
        )
    decisions = [
        CandidateDisposition(
            commit_oid=candidate.commit.commit_oid,
            disposition="rejected",
            reason_code=("non_config_only" if not candidate.config_only else "non_bug"),
            rationale=f"Reviewed excluded fixture candidate {index}",
            evidence_refs=[patch_ref(candidate)],
            adjudicator_id="adjudicator.fixture",
        )
        for index, candidate in enumerate(discovery.discovered_candidates)
        if index not in selected_index_set
    ]
    payload = AdjudicationPayload(
        source_id=discovery.source_id,
        evidence_revision="fixture-r1",
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovery)),
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        source_artifacts=source_artifacts or [],
        group_decisions=groups,
        candidate_decisions=decisions,
        lineage_resolutions=[],
    )
    attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="human.fixture",
        reviewed_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        written_statement="I reviewed the complete fixture candidate assignment table.",
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        reviewed_payload_sha256=sha256_hex(canonical_bytes(payload)),
    )
    return AdjudicationEvidence.model_validate(
        {
            **payload.model_dump(mode="json"),
            "review_attestation": attestation.model_dump(mode="json"),
        }
    )


def discovery_with_lineage(link_type: str) -> DiscoveryLedger:
    discovery = discovery_ledger()
    source_oid = discovery.discovered_candidates[0].commit.commit_oid
    target_oid = discovery.discovered_candidates[1].commit.commit_oid
    profile_payload = discovery.source_profile.model_dump(mode="json")
    if link_type == "patch_id":
        link_payload = {
            "link_type": "patch_id",
            "source_oid": source_oid,
            "target_oid": target_oid,
            "patch_id": oid(7000),
        }
    else:
        rule = LineageRegexRule(
            rule_id=f"trailer.{link_type}",
            link_type=link_type,
            pattern=rf"(?m)^{link_type}: ([0-9a-f]{{40}})$",
        )
        profile_payload["lineage_rules"] = [rule.model_dump(mode="json")]
        link_payload = {
            "link_type": link_type,
            "source_oid": source_oid,
            "target_oid": target_oid,
            "rule_id": rule.rule_id,
        }
    link = LineageLink(
        link_id=sha256_hex(canonical_bytes(link_payload)),
        **link_payload,
    )
    profile = SourceProfile.model_validate(profile_payload)
    profile_sha256 = sha256_hex(canonical_bytes(profile))
    candidate_oids = [candidate.commit.commit_oid for candidate in discovery.discovered_candidates]
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": candidate_oids,
    }
    payload = discovery.model_dump(mode="json")
    if link_type != "patch_id":
        payload["discovered_candidates"][1]["diff_evidence"]["commit_message"] += (
            f"{link_type}: {source_oid}\n"
        )
    payload.update(
        {
            "source_profile": profile.model_dump(mode="json"),
            "source_profile_sha256": profile_sha256,
            "objective_lineage_links": [link.model_dump(mode="json")],
            "candidate_universe_sha256": sha256_hex(canonical_bytes(universe)),
        }
    )
    return DiscoveryLedger.model_validate(payload)


def discovery_with_external_lineage_siblings() -> DiscoveryLedger:
    discovery = discovery_ledger()
    external_source_oid = oid(42)
    profile_payload = discovery.source_profile.model_dump(mode="json")
    rule = LineageRegexRule(
        rule_id="trailer.backport",
        link_type="backport",
        pattern=r"(?m)^Backport-of: ([0-9a-f]{40})$",
    )
    profile_payload["lineage_rules"] = [rule.model_dump(mode="json")]
    profile = SourceProfile.model_validate(profile_payload)
    profile_sha256 = sha256_hex(canonical_bytes(profile))

    links = []
    for candidate in discovery.discovered_candidates[:2]:
        link_payload = {
            "link_type": "backport",
            "source_oid": external_source_oid,
            "target_oid": candidate.commit.commit_oid,
            "rule_id": rule.rule_id,
        }
        links.append(
            LineageLink(
                link_id=sha256_hex(canonical_bytes(link_payload)),
                **link_payload,
            )
        )

    candidate_oids = [
        candidate.commit.commit_oid for candidate in discovery.discovered_candidates
    ]
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": candidate_oids,
    }
    payload = discovery.model_dump(mode="json")
    for candidate in payload["discovered_candidates"][:2]:
        candidate["diff_evidence"]["commit_message"] += (
            f"Backport-of: {external_source_oid}\n"
        )
    payload.update(
        {
            "source_profile": profile.model_dump(mode="json"),
            "source_profile_sha256": profile_sha256,
            "lineage_context_commits": [
                CommitMetadata(
                    commit=CandidateCommit(
                        commit_oid=external_source_oid,
                        parent_oids=[oid(41)],
                        selected_parent_oid=oid(41),
                        diff_base_oid=oid(41),
                        committed_at=42,
                        subject="Original external fix",
                    ),
                    full_message="Original external fix\n",
                ).model_dump(mode="json")
            ],
            "objective_lineage_links": [
                link.model_dump(mode="json") for link in links
            ],
            "candidate_universe_sha256": sha256_hex(canonical_bytes(universe)),
        }
    )
    return DiscoveryLedger.model_validate(payload)


def discovery_with_recursive_external_revert() -> DiscoveryLedger:
    discovery = discovery_ledger()
    selected_oid = discovery.discovered_candidates[0].commit.commit_oid
    external_revert_oid = oid(42)
    external_original_oid = oid(41)
    profile_payload = discovery.source_profile.model_dump(mode="json")
    backport_rule = LineageRegexRule(
        rule_id="trailer.backport",
        link_type="backport",
        pattern=r"(?m)^Backport-of: ([0-9a-f]{40})$",
    )
    revert_rule = LineageRegexRule(
        rule_id="trailer.revert",
        link_type="revert",
        pattern=r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
    )
    profile_payload["lineage_rules"] = [
        backport_rule.model_dump(mode="json"),
        revert_rule.model_dump(mode="json"),
    ]
    profile = SourceProfile.model_validate(profile_payload)
    profile_sha256 = sha256_hex(canonical_bytes(profile))

    link_payloads = [
        {
            "link_type": "backport",
            "source_oid": external_revert_oid,
            "target_oid": selected_oid,
            "rule_id": backport_rule.rule_id,
        },
        {
            "link_type": "revert",
            "source_oid": external_original_oid,
            "target_oid": external_revert_oid,
            "rule_id": revert_rule.rule_id,
        },
    ]
    links = [
        LineageLink(
            link_id=sha256_hex(canonical_bytes(link_payload)),
            **link_payload,
        )
        for link_payload in link_payloads
    ]
    candidate_oids = [
        candidate.commit.commit_oid for candidate in discovery.discovered_candidates
    ]
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": candidate_oids,
    }
    payload = discovery.model_dump(mode="json")
    payload["discovered_candidates"][0]["diff_evidence"]["commit_message"] += (
        f"Backport-of: {external_revert_oid}\n"
    )
    payload.update(
        {
            "source_profile": profile.model_dump(mode="json"),
            "source_profile_sha256": profile_sha256,
            "lineage_context_commits": [
                CommitMetadata(
                    commit=CandidateCommit(
                        commit_oid=external_original_oid,
                        parent_oids=[oid(40)],
                        selected_parent_oid=oid(40),
                        diff_base_oid=oid(40),
                        committed_at=41,
                        subject="Original external fix",
                    ),
                    full_message="Original external fix\n",
                ).model_dump(mode="json"),
                CommitMetadata(
                    commit=CandidateCommit(
                        commit_oid=external_revert_oid,
                        parent_oids=[external_original_oid],
                        selected_parent_oid=external_original_oid,
                        diff_base_oid=external_original_oid,
                        committed_at=42,
                        subject="Revert original external fix",
                    ),
                    full_message=(
                        "Revert original external fix\n\n"
                        f"This reverts commit {external_original_oid}.\n"
                    ),
                ).model_dump(mode="json"),
            ],
            "objective_lineage_links": [link.model_dump(mode="json") for link in links],
            "candidate_universe_sha256": sha256_hex(canonical_bytes(universe)),
        }
    )
    return DiscoveryLedger.model_validate(payload)


def discovery_with_selected_revert_in_equivalence_lineage() -> DiscoveryLedger:
    discovery = discovery_with_lineage("backport")
    revert_oid = discovery.discovered_candidates[0].commit.commit_oid
    original_oid = oid(41)
    profile_payload = discovery.source_profile.model_dump(mode="json")
    revert_rule = LineageRegexRule(
        rule_id="trailer.revert",
        link_type="revert",
        pattern=r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
    )
    profile_payload["lineage_rules"].append(revert_rule.model_dump(mode="json"))
    profile = SourceProfile.model_validate(profile_payload)
    profile_sha256 = sha256_hex(canonical_bytes(profile))

    revert_payload = {
        "link_type": "revert",
        "source_oid": original_oid,
        "target_oid": revert_oid,
        "rule_id": revert_rule.rule_id,
    }
    revert_link = LineageLink(
        link_id=sha256_hex(canonical_bytes(revert_payload)),
        **revert_payload,
    )
    links = sorted(
        [*discovery.objective_lineage_links, revert_link],
        key=lambda link: (
            link.link_type,
            link.source_oid,
            link.target_oid,
            link.rule_id or "",
            link.patch_id or "",
            link.link_id,
        ),
    )
    candidate_oids = [
        candidate.commit.commit_oid for candidate in discovery.discovered_candidates
    ]
    universe = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": candidate_oids,
    }
    payload = discovery.model_dump(mode="json")
    payload["discovered_candidates"][0]["diff_evidence"]["commit_message"] += (
        f"This reverts commit {original_oid}.\n"
    )
    payload.update(
        {
            "source_profile": profile.model_dump(mode="json"),
            "source_profile_sha256": profile_sha256,
            "lineage_context_commits": [
                CommitMetadata(
                    commit=CandidateCommit(
                        commit_oid=original_oid,
                        parent_oids=[oid(40)],
                        selected_parent_oid=oid(40),
                        diff_base_oid=oid(40),
                        committed_at=41,
                        subject="Original fix later reverted by selected candidate",
                    ),
                    full_message="Original fix later reverted by selected candidate\n",
                ).model_dump(mode="json")
            ],
            "objective_lineage_links": [link.model_dump(mode="json") for link in links],
            "candidate_universe_sha256": sha256_hex(canonical_bytes(universe)),
        }
    )
    return DiscoveryLedger.model_validate(payload)


def write_cas(discovery: DiscoveryLedger, blob_dir: Path) -> None:
    blob_dir.mkdir(parents=True, exist_ok=True)
    for index, candidate in enumerate(discovery.discovered_candidates):
        data = _patch_bytes(index, candidate.changed_paths)
        assert sha256_hex(data) == candidate.diff_evidence.patch_sha256
        (blob_dir / candidate.diff_evidence.patch_sha256).write_bytes(data)
        eligible_digest = candidate.diff_evidence.eligible_patch_sha256
        if eligible_digest is not None:
            eligible_data = _patch_bytes(index, candidate.eligible_paths)
            assert sha256_hex(eligible_data) == eligible_digest
            (blob_dir / eligible_digest).write_bytes(eligible_data)
