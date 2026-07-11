"""Profile-driven candidate discovery for external game-content histories."""

from __future__ import annotations

import platform
import re
import unicodedata
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Literal, Sequence

from gameforge.bench.external_corpus.contracts import (
    CandidateOrderTerm,
    CommitMetadata,
    DiffEvidence,
    DiscoveredCandidate,
    DiscoveryLedger,
    DiscoveryTool,
    LineageLink,
    LineageRegexRule,
    RegexRule,
    SearchRegistration,
    SelectionReason,
    SourceProfile,
    canonical_bytes,
    posix_glob_matches,
    put_blob,
    read_regular_file,
    sha256_hex,
)
from gameforge.bench.external_corpus.git import GitEvidenceError, ReadOnlyGitRepo


DISCOVERY_TOOL_VERSION = "external-discovery@1"
_OID_RE = re.compile(r"[0-9a-f]{40}")
_REASON_ORDER = {"direct_match": 0, "adjacent_context": 1, "lineage_context": 2}


@dataclass(frozen=True)
class DirectRuleGroup:
    message_rules: tuple[RegexRule, ...]
    diff_rules: tuple[RegexRule, ...]


@dataclass(frozen=True)
class DiscoveryPolicy:
    """Internal source-neutral rules shared by generic and legacy serializers."""

    include_globs: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    direct_rule_groups: tuple[DirectRuleGroup, ...]
    lineage_rules: tuple[LineageRegexRule, ...]
    candidate_order: tuple[CandidateOrderTerm, CandidateOrderTerm]
    include_first_parent_adjacent_context: bool
    candidate_limit: int | None
    expected_matched_candidate_count: int | None = None
    expected_config_only_candidate_count: int | None = None


@dataclass(frozen=True)
class ObjectiveDiscovery:
    candidates: tuple[DiscoveredCandidate, ...]
    lineage_links: tuple[LineageLink, ...]
    matched_candidate_count: int
    config_only_candidate_count: int


@dataclass(frozen=True)
class _CommitState:
    metadata: CommitMetadata
    changed_paths: tuple[str, ...]
    eligible_paths: tuple[str, ...]

    @property
    def config_only(self) -> bool:
        return bool(self.changed_paths) and len(self.changed_paths) == len(self.eligible_paths)


def _is_eligible(path: str, policy: DiscoveryPolicy) -> bool:
    return any(posix_glob_matches(path, pattern) for pattern in policy.include_globs) and not any(
        posix_glob_matches(path, pattern) for pattern in policy.exclude_globs
    )


def _link_id(payload: dict[str, str]) -> str:
    return sha256_hex(canonical_bytes(payload))


def _trailer_link(
    *,
    link_type: Literal["cherry_pick", "backport", "revert"],
    source_oid: str,
    target_oid: str,
    rule_id: str,
) -> LineageLink:
    payload = {
        "link_type": link_type,
        "source_oid": source_oid,
        "target_oid": target_oid,
        "rule_id": rule_id,
    }
    return LineageLink(link_id=_link_id(payload), **payload)


def _patch_link(*, source_oid: str, target_oid: str, patch_id: str) -> LineageLink:
    payload = {
        "link_type": "patch_id",
        "source_oid": source_oid,
        "target_oid": target_oid,
        "patch_id": patch_id,
    }
    return LineageLink(link_id=_link_id(payload), **payload)


def _reason_key(reason: SelectionReason) -> tuple[int, str, str, tuple[str, ...]]:
    return (
        _REASON_ORDER[reason.kind],
        reason.anchor_oid or "",
        reason.lineage_link_id or "",
        tuple(reason.rule_ids),
    )


def _sorted_reasons(reasons: Sequence[SelectionReason]) -> list[SelectionReason]:
    unique = {canonical_bytes(reason): reason for reason in reasons}
    return sorted(unique.values(), key=_reason_key)


def _link_sort_key(link: LineageLink) -> tuple[str, str, str, str, str, str]:
    return (
        link.link_type,
        link.source_oid,
        link.target_oid,
        link.rule_id or "",
        link.patch_id or "",
        link.link_id,
    )


def _ordered_oids(
    oids: Sequence[str],
    states: dict[str, _CommitState],
    order: tuple[CandidateOrderTerm, CandidateOrderTerm],
) -> list[str]:
    ordered = list(oids)
    for term in reversed(order):
        ordered.sort(
            key=lambda oid: getattr(states[oid].metadata.commit, term.field),
            reverse=term.direction == "descending",
        )
    return ordered


def _direct_reasons(
    repo: ReadOnlyGitRepo,
    state: _CommitState,
    policy: DiscoveryPolicy,
) -> list[SelectionReason]:
    if not state.eligible_paths:
        return []
    eligible_patch: bytes | None = None
    if (
        any(group.diff_rules for group in policy.direct_rule_groups)
        and len(state.metadata.commit.parent_oids) <= 1
    ):
        eligible_patch = repo.eligible_patch_bytes(
            state.metadata.commit.diff_base_oid,
            state.metadata.commit.commit_oid,
            state.eligible_paths,
        )
    reasons: list[SelectionReason] = []
    for group in policy.direct_rule_groups:
        rule_ids = [
            rule.rule_id
            for rule in group.message_rules
            if re.search(rule.pattern, state.metadata.commit.subject) is not None
        ]
        if eligible_patch is not None:
            for rule in group.diff_rules:
                try:
                    compiled = re.compile(rule.pattern.encode("utf-8"))
                except re.error as exc:
                    raise GitEvidenceError(f"invalid diff rule: {rule.rule_id}") from exc
                if compiled.search(eligible_patch) is not None:
                    rule_ids.append(rule.rule_id)
        if rule_ids:
            reasons.append(SelectionReason(kind="direct_match", rule_ids=sorted(set(rule_ids))))
    return sorted(reasons, key=lambda reason: tuple(reason.rule_ids))


def _verify_cas_blob(blob_dir: Path, digest: str) -> None:
    try:
        data = read_regular_file(blob_dir / digest)
    except OSError as exc:
        raise GitEvidenceError(f"CAS publication did not materialize blob {digest}") from exc
    if sha256_hex(data) != digest:
        raise GitEvidenceError(f"CAS publication produced a digest mismatch for {digest}")


def discover_objective_candidates(
    repo: ReadOnlyGitRepo,
    history: Sequence[str],
    policy: DiscoveryPolicy,
    blob_dir: Path,
) -> ObjectiveDiscovery:
    """Discover objective candidates after repository/range preflight."""

    reachable = set(history)
    states: dict[str, _CommitState] = {}
    for oid in history:
        metadata = repo.commit_metadata(oid)
        changed_paths = tuple(repo.changed_paths(metadata.commit.diff_base_oid, oid))
        eligible_paths = tuple(path for path in changed_paths if _is_eligible(path, policy))
        states[oid] = _CommitState(
            metadata=metadata,
            changed_paths=changed_paths,
            eligible_paths=eligible_paths,
        )

    reasons: dict[str, list[SelectionReason]] = {}
    direct_oids: set[str] = set()
    for oid in history:
        direct_reasons = _direct_reasons(repo, states[oid], policy)
        if direct_reasons:
            direct_oids.add(oid)
            reasons[oid] = direct_reasons

    if policy.include_first_parent_adjacent_context:
        first_parent_children: dict[str, list[str]] = {}
        for oid, state in states.items():
            parents = state.metadata.commit.parent_oids
            if parents:
                first_parent_children.setdefault(parents[0], []).append(oid)
        for children in first_parent_children.values():
            children.sort()

        for anchor_oid in sorted(direct_oids):
            anchor = states[anchor_oid]
            neighbors: list[str] = []
            parents = anchor.metadata.commit.parent_oids
            if parents and parents[0] in reachable:
                neighbors.append(parents[0])
            neighbors.extend(first_parent_children.get(anchor_oid, ()))
            anchor_paths = set(anchor.eligible_paths)
            for neighbor_oid in sorted(set(neighbors)):
                if anchor_paths.isdisjoint(states[neighbor_oid].eligible_paths):
                    continue
                reasons.setdefault(neighbor_oid, []).append(
                    SelectionReason(kind="adjacent_context", anchor_oid=anchor_oid)
                )

    objective_links: dict[str, LineageLink] = {}
    pending = sorted(reasons)
    parsed_targets: set[str] = set()
    while pending:
        target_oid = pending.pop(0)
        if target_oid in parsed_targets:
            continue
        parsed_targets.add(target_oid)
        message = states[target_oid].metadata.full_message
        for rule in policy.lineage_rules:
            for match in re.finditer(rule.pattern, message):
                source_oid = match.group(1)
                if _OID_RE.fullmatch(source_oid) is None:
                    raise GitEvidenceError(
                        f"lineage rule {rule.rule_id} produced an invalid source OID"
                    )
                if source_oid not in reachable:
                    raise GitEvidenceError(
                        f"lineage source {source_oid} is unreachable from the pinned head"
                    )
                link = _trailer_link(
                    link_type=rule.link_type,
                    source_oid=source_oid,
                    target_oid=target_oid,
                    rule_id=rule.rule_id,
                )
                objective_links[link.link_id] = link
                reasons.setdefault(source_oid, []).append(
                    SelectionReason(kind="lineage_context", lineage_link_id=link.link_id)
                )
                if source_oid not in parsed_targets and source_oid not in pending:
                    pending.append(source_oid)
                    pending.sort()

    all_candidate_oids = _ordered_oids(tuple(reasons), states, policy.candidate_order)
    patches: dict[str, bytes] = {}
    patch_ids: dict[str, list[str]] = {}
    for oid in all_candidate_oids:
        state = states[oid]
        if not state.changed_paths:
            raise GitEvidenceError(f"selected candidate {oid} has no changed paths")
        patch = repo.patch_bytes(state.metadata.commit.diff_base_oid, oid)
        if not patch:
            raise GitEvidenceError(f"selected candidate {oid} has an empty selected-edge patch")
        patches[oid] = patch
        patch_id = repo.stable_patch_id(patch)
        patch_ids.setdefault(patch_id, []).append(oid)

    order_index = {oid: index for index, oid in enumerate(all_candidate_oids)}
    for patch_id, matching_oids in patch_ids.items():
        ordered_matching = sorted(matching_oids, key=order_index.__getitem__)
        for source_oid, target_oid in combinations(ordered_matching, 2):
            link = _patch_link(
                source_oid=source_oid,
                target_oid=target_oid,
                patch_id=patch_id,
            )
            objective_links[link.link_id] = link

    matched_count = len(all_candidate_oids)
    config_only_count = sum(states[oid].config_only for oid in all_candidate_oids)
    if (
        policy.expected_matched_candidate_count is not None
        and matched_count != policy.expected_matched_candidate_count
    ):
        raise GitEvidenceError(
            "matched candidate count differs from frozen expectation: "
            f"expected {policy.expected_matched_candidate_count}, observed {matched_count}"
        )
    if (
        policy.expected_config_only_candidate_count is not None
        and config_only_count != policy.expected_config_only_candidate_count
    ):
        raise GitEvidenceError(
            "config-only candidate count differs from frozen expectation: "
            f"expected {policy.expected_config_only_candidate_count}, "
            f"observed {config_only_count}"
        )
    selected_oids = (
        all_candidate_oids
        if policy.candidate_limit is None
        else all_candidate_oids[: policy.candidate_limit]
    )
    selected_set = set(selected_oids)
    selected_links = sorted(
        (
            link
            for link in objective_links.values()
            if link.source_oid in selected_set and link.target_oid in selected_set
        ),
        key=_link_sort_key,
    )

    candidates: list[DiscoveredCandidate] = []
    diff_rule_ids = {
        rule.rule_id for group in policy.direct_rule_groups for rule in group.diff_rules
    }
    for oid in selected_oids:
        state = states[oid]
        patch_sha256, patch_blob = put_blob(blob_dir, patches[oid])
        _verify_cas_blob(blob_dir, patch_sha256)
        selected_reasons = _sorted_reasons(reasons[oid])
        if any(diff_rule_ids.intersection(reason.rule_ids) for reason in selected_reasons):
            eligible_patch = repo.eligible_patch_bytes(
                state.metadata.commit.diff_base_oid,
                oid,
                state.eligible_paths,
            )
            eligible_sha256, _eligible_blob = put_blob(blob_dir, eligible_patch)
            _verify_cas_blob(blob_dir, eligible_sha256)
        candidates.append(
            DiscoveredCandidate(
                commit=state.metadata.commit,
                changed_paths=list(state.changed_paths),
                eligible_paths=list(state.eligible_paths),
                config_only=state.config_only,
                selection_reasons=selected_reasons,
                diff_evidence=DiffEvidence(
                    commit_oid=oid,
                    patch_sha256=patch_sha256,
                    patch_blob=patch_blob,
                    commit_message=state.metadata.full_message,
                ),
            )
        )

    return ObjectiveDiscovery(
        candidates=tuple(candidates),
        lineage_links=tuple(selected_links),
        matched_candidate_count=matched_count,
        config_only_candidate_count=config_only_count,
    )


def discover_candidates(
    repo: ReadOnlyGitRepo,
    profile: SourceProfile,
    registration: SearchRegistration,
    blob_dir: Path,
) -> DiscoveryLedger:
    """Discover and bind the registered candidate universe for one source."""

    profile = SourceProfile.model_validate(profile.model_dump(mode="json"))
    registration = SearchRegistration.model_validate(registration.model_dump(mode="json"))
    repo.preflight()
    try:
        resolved_head = repo.resolve(profile.pinned_head)
    except GitEvidenceError as exc:
        raise GitEvidenceError("unable to resolve pinned head") from exc
    if resolved_head != profile.pinned_head:
        raise GitEvidenceError("resolved pinned head differs from the source profile")

    history = repo.reachable_commits(profile)
    if profile.pinned_head not in set(history):
        raise GitEvidenceError("pinned head is absent from its reachable history")
    policy = DiscoveryPolicy(
        include_globs=profile.config_include_globs,
        exclude_globs=profile.config_exclude_globs,
        direct_rule_groups=(
            DirectRuleGroup(
                message_rules=profile.message_rules,
                diff_rules=profile.diff_rules,
            ),
        ),
        lineage_rules=profile.lineage_rules,
        candidate_order=profile.candidate_order,
        include_first_parent_adjacent_context=False,
        candidate_limit=profile.b0a_protocol.candidate_limit,
        expected_matched_candidate_count=profile.b0a_protocol.expected_matched_candidate_count,
        expected_config_only_candidate_count=(
            profile.b0a_protocol.expected_config_only_candidate_count
        ),
    )
    objective = discover_objective_candidates(repo, history, policy, blob_dir)

    profile_sha256 = sha256_hex(canonical_bytes(profile))
    universe_payload = {
        "source_id": profile.source_id,
        "profile_sha256": profile_sha256,
        "ordered_candidate_oids": [
            candidate.commit.commit_oid for candidate in objective.candidates
        ],
    }
    return DiscoveryLedger(
        source_id=profile.source_id,
        source_profile=profile,
        source_profile_sha256=profile_sha256,
        search_registration=registration,
        observed_history_count=len(history),
        matched_candidate_count=objective.matched_candidate_count,
        config_only_candidate_count=objective.config_only_candidate_count,
        discovery_tool=DiscoveryTool(
            tool_version=DISCOVERY_TOOL_VERSION,
            project_commit_oid=registration.project_commit_oid,
            git_version=repo.git_version(),
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
            python_build=platform.python_build(),
            unicode_version=unicodedata.unidata_version,
        ),
        discovered_candidates=list(objective.candidates),
        objective_lineage_links=list(objective.lineage_links),
        candidate_universe_sha256=sha256_hex(canonical_bytes(universe_payload)),
    )
