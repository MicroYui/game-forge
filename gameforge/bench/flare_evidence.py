"""Strict contracts and immutable storage for the Flare B0A evidence ledger."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from gameforge.bench.external_corpus.contracts import (
    CandidateCommit as CandidateCommit,
    DiffEvidence,
    DiscoveredCandidate,
    EvidenceArtifact,
    EvidenceRef,
    GitCommandSpec,
    GitEnvironmentPolicy,
    LineageLink,
    LineageRegexRule,
    NonEmptyStr,
    Oid,
    RegexRule,
    SelectionReason,
    Sha256,
    StableId,
    _StrictModel,
    _validate_posix_relative,
    canonical_bytes,
    posix_glob_matches,
    read_regular_file,
    sha256_hex,
)
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass


FLARE_B0A_SCHEMA_VERSION = "flare-b0a@1"
DISCOVERY_TOOL_VERSION = "gameforge-flare-discovery@1"
GIT_EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
B0A_DEFECT_CLASSES: tuple[DefectClass, ...] = tuple(
    defect_class
    for defect_class, metadata in CLASS_META.items()
    if metadata.bucket is not Bucket.llm_assisted
)

GIT_COMMON_PREFIX = (
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
)
GIT_EMPTY_TREE_ARGS = ("hash-object", "-t", "tree", "--stdin")
GIT_ELIGIBLE_PATH_SUFFIX = ("--", "{eligible_paths...}")
GIT_HISTORY_ARGS = ("rev-list", "--topo-order", "--reverse", "{revision_range}")
GIT_METADATA_ARGS = (
    "show",
    "-s",
    "--no-show-signature",
    "--encoding=UTF-8",
    "--format=%H%x00%P%x00%ct%x00%s%x00%B",
    "{commit}",
)
GIT_PATCH_ARGS = (
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
)
GIT_PATCH_ID_ARGS = ("patch-id", "--stable")
GIT_PATHS_ARGS = (
    "diff-tree",
    "--no-commit-id",
    "--name-status",
    "--no-renames",
    "-r",
    "-z",
    "{parent}",
    "{commit}",
)
GIT_RESOLVE_ARGS = ("rev-parse", "--verify", "{pinned_head}^{commit}")
GIT_VERSION_COMMAND = ("git", "--version")

GIT_INHERIT_ALLOWLIST = ("PATH",)
GIT_DROP_INHERITED_PREFIXES = ("GIT_",)
GIT_FIXED_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
)

HISTORY_WALK = "all_reachable_topo_order"
CANDIDATE_ORDER = ("committed_at", "commit_oid")
STOP_CONDITION = "exhaust_reachable_range"
MESSAGE_FIELD = "subject_percent_s_utf8"
LINEAGE_MESSAGE_FIELD = "full_percent_B_utf8"
DIFF_MATCH_SCOPE = "eligible_path_patch_bytes"
DIFF_MERGE_POLICY = "exclude_multi_parent_commits_from_diff_direct"
DIFF_REGEX_ENCODING = "ascii_bytes"
PATH_ELIGIBILITY = "include_and_not_exclude"
PATH_GLOB_SEMANTICS = "component_fnmatch_double_star_zero_or_more"
CANDIDATE_PATH_GATE = "any_changed_path_eligible"
CONFIG_ONLY_RULE = "all_changed_paths_eligible"
SELECTED_ROUND_SEMANTICS = "union_through_selected"
ISSUE_PR_DISCOVERY = "disabled_offline_only"

def _expected_applicability(defect_class: DefectClass) -> str:
    if defect_class in {
        DefectClass.prob_sum_ne_1,
        DefectClass.gacha_expectation_violation,
    }:
        return "not_applicable"
    return "applicable"


def _validate_b0a_class(defect_class: DefectClass) -> None:
    if defect_class not in B0A_DEFECT_CLASSES:
        raise ValueError(f"{defect_class.value} is outside the B0A taxonomy")


def _validate_applicability_value(defect_class: DefectClass, domain_applicability: str) -> None:
    expected = _expected_applicability(defect_class)
    if domain_applicability != expected:
        raise ValueError(f"{defect_class.value} domain_applicability must be {expected} for Flare")


def _validate_exact_matrix(rows: list[Any] | tuple[Any, ...], name: str) -> None:
    classes = [row.defect_class for row in rows]
    if len(classes) != len(B0A_DEFECT_CLASSES) or set(classes) != set(B0A_DEFECT_CLASSES):
        raise ValueError(f"{name} must contain each B0A defect class exactly once")


class SearchAdjacency(_StrictModel):
    first_parent_child_edges: Literal[1]
    first_parent_predecessor_edges: Literal[1]
    include_reachable_lineage_sources: Literal[True]
    nonrecursive: Literal[True]
    require_shared_exact_eligible_path_with_anchor: Literal[True]


class SearchRound(_StrictModel):
    name: Literal["initial", "expanded"]
    message_regexes: tuple[RegexRule, ...]
    diff_regexes: tuple[RegexRule, ...]

    @model_validator(mode="after")
    def validate_round(self) -> SearchRound:
        rule_ids = [rule.rule_id for rule in (*self.message_regexes, *self.diff_regexes)]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("round rule IDs must be unique")
        for rule in self.diff_regexes:
            try:
                encoded = rule.pattern.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("diff regexes must contain ASCII only") from exc
            try:
                re.compile(encoded)
            except re.error as exc:
                raise ValueError(f"invalid bytes diff regex: {exc}") from exc
        return self


class FlareSearchSpec(_StrictModel):
    schema_version: Literal["flare-b0a@1"]
    source_repo: NonEmptyStr
    pinned_head: Oid
    after_exclusive: Oid | None = None
    expected_revision_count: Annotated[int, Field(gt=0)]
    config_path_globs: tuple[NonEmptyStr, ...]
    excluded_path_globs: tuple[NonEmptyStr, ...]
    path_eligibility: Literal["include_and_not_exclude"]
    candidate_path_gate: Literal["any_changed_path_eligible"]
    config_only_rule: Literal["all_changed_paths_eligible"]
    history_walk: Literal["all_reachable_topo_order"]
    candidate_order: tuple[Literal["committed_at"], Literal["commit_oid"]]
    stop_condition: Literal["exhaust_reachable_range"]
    message_field: Literal["subject_percent_s_utf8"]
    lineage_message_field: Literal["full_percent_B_utf8"]
    diff_match_scope: Literal["eligible_path_patch_bytes"]
    diff_merge_policy: Literal["exclude_multi_parent_commits_from_diff_direct"]
    diff_regex_encoding: Literal["ascii_bytes"]
    path_glob_semantics: Literal["component_fnmatch_double_star_zero_or_more"]
    selected_round_semantics: Literal["union_through_selected"]
    issue_pr_discovery: Literal["disabled_offline_only"]
    adjacency: SearchAdjacency
    git_commands: GitCommandSpec
    git_environment_policy: GitEnvironmentPolicy
    lineage_regexes: tuple[LineageRegexRule, ...]
    rounds: tuple[SearchRound, SearchRound]

    @field_validator("config_path_globs", "excluded_path_globs")
    @classmethod
    def validate_globs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("glob list must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("glob values must be unique")
        for value in values:
            _validate_posix_relative(value)
        return values

    @model_validator(mode="after")
    def validate_search_contract(self) -> FlareSearchSpec:
        if self.after_exclusive == self.pinned_head:
            raise ValueError("after_exclusive must differ from pinned_head")
        if tuple(item.name for item in self.rounds) != ("initial", "expanded"):
            raise ValueError("rounds must be ordered exactly as initial, expanded")
        lineage_types = [rule.link_type for rule in self.lineage_regexes]
        if sorted(lineage_types) != ["backport", "cherry_pick", "revert"]:
            raise ValueError("lineage_regexes must cover backport, cherry_pick, and revert")
        rule_ids = [rule.rule_id for rule in self.lineage_regexes]
        rule_ids.extend(
            rule.rule_id
            for round_spec in self.rounds
            for rule in (*round_spec.message_regexes, *round_spec.diff_regexes)
        )
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("all search rule IDs must be unique")
        return self


class SearchRegistration(_StrictModel):
    project_commit_oid: Oid
    repo_relative_path: str

    @field_validator("repo_relative_path")
    @classmethod
    def validate_repo_relative_path(cls, value: str) -> str:
        return _validate_posix_relative(value, suffix=".json")


class DiscoveryTool(_StrictModel):
    tool_version: Literal[DISCOVERY_TOOL_VERSION]
    project_commit_oid: Oid
    git_version: NonEmptyStr
    python_implementation: NonEmptyStr
    python_version: NonEmptyStr
    python_build: tuple[str, str]
    unicode_version: NonEmptyStr


def _selected_search_rounds(
    search_frame: FlareSearchSpec,
    search_round: Literal["initial", "expanded"],
) -> tuple[SearchRound, ...]:
    selected_count = 1 if search_round == "initial" else 2
    return search_frame.rounds[:selected_count]


def derive_direct_match_reasons(
    search_frame: FlareSearchSpec,
    search_round: Literal["initial", "expanded"],
    *,
    subject: str,
    parent_count: int,
    eligible_paths: Sequence[str],
    eligible_patch: bytes | None,
) -> list[SelectionReason]:
    """Recompute exact per-round direct reasons under the frozen search contract."""

    if not eligible_paths:
        return []
    reasons: list[SelectionReason] = []
    for round_spec in _selected_search_rounds(search_frame, search_round):
        matched_rule_ids = {
            rule.rule_id
            for rule in round_spec.message_regexes
            if re.search(rule.pattern, subject) is not None
        }
        if round_spec.diff_regexes and parent_count <= 1:
            if eligible_patch is None:
                raise ValueError("eligible patch bytes are required for diff direct-match replay")
            matched_rule_ids.update(
                rule.rule_id
                for rule in round_spec.diff_regexes
                if re.search(rule.pattern.encode("ascii"), eligible_patch) is not None
            )
        if matched_rule_ids:
            reasons.append(
                SelectionReason(kind="direct_match", rule_ids=sorted(matched_rule_ids))
            )
    return sorted(reasons, key=lambda reason: tuple(reason.rule_ids))


def _git_c_quote_path(value: bytes) -> bytes:
    escapes = {
        0x07: b"\\a",
        0x08: b"\\b",
        0x09: b"\\t",
        0x0A: b"\\n",
        0x0B: b"\\v",
        0x0C: b"\\f",
        0x0D: b"\\r",
        0x22: b'\\"',
        0x5C: b"\\\\",
    }
    rendered = bytearray()
    quoted = False
    for byte in value:
        escape = escapes.get(byte)
        if escape is not None:
            rendered.extend(escape)
            quoted = True
        elif byte < 0x20 or byte >= 0x7F:
            rendered.extend(f"\\{byte:03o}".encode("ascii"))
            quoted = True
        else:
            rendered.append(byte)
    result = bytes(rendered)
    return b'"' + result + b'"' if quoted else result


def _frozen_diff_header(path: str) -> bytes:
    encoded = path.encode("utf-8", errors="strict")
    return b"diff --git " + _git_c_quote_path(b"a/" + encoded) + b" " + _git_c_quote_path(
        b"b/" + encoded
    )


def extract_eligible_patch_bytes(
    full_patch: bytes,
    *,
    changed_paths: Sequence[str],
    eligible_paths: Sequence[str],
) -> bytes:
    """Derive Git's path-filtered patch by selecting exact full-patch file blocks."""

    changed = list(changed_paths)
    eligible = set(eligible_paths)
    if not changed or len(changed) != len(set(changed)):
        raise ValueError("full patch replay requires unique changed paths")
    if not eligible <= set(changed):
        raise ValueError("eligible patch replay paths must be changed paths")
    starts = [match.start() for match in re.finditer(rb"(?m)^diff --git ", full_patch)]
    if not starts or starts[0] != 0:
        raise ValueError("full patch does not start with a Git file-diff block")
    expected_headers = {_frozen_diff_header(path): path for path in changed}
    if len(expected_headers) != len(changed):
        raise ValueError("changed paths do not map to unique frozen diff headers")

    seen: set[str] = set()
    selected: list[bytes] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(full_patch)
        block = full_patch[start:end]
        header, separator, _body = block.partition(b"\n")
        if not separator:
            raise ValueError("Git file-diff block has no header terminator")
        path = expected_headers.get(header)
        if path is None:
            raise ValueError("full patch contains a file-diff header outside changed_paths")
        if path in seen:
            raise ValueError("full patch contains a duplicate changed-path block")
        seen.add(path)
        if path in eligible:
            selected.append(block)
    if seen != set(changed):
        raise ValueError("full patch blocks do not exactly cover changed_paths")
    return b"".join(selected)


def _validate_direct_match_metadata(
    candidate: DiscoveredCandidate,
    search_frame: FlareSearchSpec,
    search_round: Literal["initial", "expanded"],
) -> None:
    selected_rounds = _selected_search_rounds(search_frame, search_round)
    direct_reasons = [
        reason for reason in candidate.selection_reasons if reason.kind == "direct_match"
    ]
    if direct_reasons and not candidate.eligible_paths:
        raise ValueError("direct-match candidates require at least one eligible path")

    reason_by_round: dict[int, SelectionReason] = {}
    for reason in direct_reasons:
        reason_ids = set(reason.rule_ids)
        matching_rounds = [
            index
            for index, round_spec in enumerate(selected_rounds)
            if reason_ids
            <= {
                rule.rule_id
                for rule in (*round_spec.message_regexes, *round_spec.diff_regexes)
            }
        ]
        if len(matching_rounds) != 1:
            raise ValueError(
                "direct-match rule IDs must belong to exactly one selected search round"
            )
        round_index = matching_rounds[0]
        if round_index in reason_by_round:
            raise ValueError("each selected search round permits at most one direct-match reason")
        round_spec = selected_rounds[round_index]
        message_matches = {
            rule.rule_id
            for rule in round_spec.message_regexes
            if re.search(rule.pattern, candidate.commit.subject) is not None
        }
        message_rule_ids = {rule.rule_id for rule in round_spec.message_regexes}
        diff_rule_ids = {rule.rule_id for rule in round_spec.diff_regexes}
        if reason_ids & message_rule_ids != message_matches:
            raise ValueError(
                "direct-match message rule IDs must exactly match the candidate subject"
            )
        if len(candidate.commit.parent_oids) > 1 and reason_ids & diff_rule_ids:
            raise ValueError("merge commits cannot claim a diff direct-match rule")
        reason_by_round[round_index] = reason

    if not candidate.eligible_paths:
        return
    for round_index, round_spec in enumerate(selected_rounds):
        message_matches = {
            rule.rule_id
            for rule in round_spec.message_regexes
            if re.search(rule.pattern, candidate.commit.subject) is not None
        }
        reason = reason_by_round.get(round_index)
        if message_matches and reason is None:
            raise ValueError("matching candidate subjects require a direct-match reason")
        if len(candidate.commit.parent_oids) > 1:
            actual_ids = set() if reason is None else set(reason.rule_ids)
            if actual_ids != message_matches:
                raise ValueError(
                    "merge direct-match reasons must exactly equal matching message rules"
                )


class DiscoveryLedger(_StrictModel):
    schema_version: Literal["flare-b0a@1"] = FLARE_B0A_SCHEMA_VERSION
    search_frame: FlareSearchSpec
    search_spec_sha256: Sha256
    search_registration: SearchRegistration
    search_round: Literal["initial", "expanded"]
    observed_revision_count: Annotated[int, Field(gt=0)]
    discovery_tool: DiscoveryTool
    discovered_candidates: list[DiscoveredCandidate]
    objective_lineage_links: list[LineageLink]
    candidate_universe_sha256: Sha256

    @model_validator(mode="after")
    def validate_discovery_bindings(self) -> DiscoveryLedger:
        if self.search_spec_sha256 != sha256_hex(canonical_bytes(self.search_frame)):
            raise ValueError("search_spec_sha256 does not bind search_frame")
        if self.discovery_tool.project_commit_oid != self.search_registration.project_commit_oid:
            raise ValueError("discovery tool commit must match search registration commit")
        if self.observed_revision_count != self.search_frame.expected_revision_count:
            raise ValueError("observed_revision_count differs from registered expectation")
        candidate_keys = [
            (item.commit.committed_at, item.commit.commit_oid)
            for item in self.discovered_candidates
        ]
        if candidate_keys != sorted(set(candidate_keys)):
            raise ValueError("discovered_candidates must be sorted and unique")
        candidate_oid_sequence = [item.commit.commit_oid for item in self.discovered_candidates]
        if len(candidate_oid_sequence) != len(set(candidate_oid_sequence)):
            raise ValueError("discovered candidate commit OIDs must be unique")
        candidate_by_oid = {item.commit.commit_oid: item for item in self.discovered_candidates}
        candidate_oids = set(candidate_by_oid)
        reason_order = {
            "direct_match": 0,
            "adjacent_context": 1,
            "lineage_context": 2,
        }

        def reason_key(reason: SelectionReason) -> tuple[int, str, str, tuple[str, ...]]:
            return (
                reason_order[reason.kind],
                reason.anchor_oid or "",
                reason.lineage_link_id or "",
                tuple(reason.rule_ids),
            )

        for candidate in self.discovered_candidates:
            expected_eligible = [
                path
                for path in candidate.changed_paths
                if any(
                    posix_glob_matches(path, pattern)
                    for pattern in self.search_frame.config_path_globs
                )
                and not any(
                    posix_glob_matches(path, pattern)
                    for pattern in self.search_frame.excluded_path_globs
                )
            ]
            if candidate.eligible_paths != expected_eligible:
                raise ValueError("candidate eligible_paths differ from the embedded search frame")
            expected_config_only = len(candidate.changed_paths) == len(expected_eligible)
            if candidate.config_only != expected_config_only:
                raise ValueError("candidate config_only must be derived from eligible_paths")
            canonical_reasons = [canonical_bytes(reason) for reason in candidate.selection_reasons]
            expected_reasons = sorted(
                dict(zip(canonical_reasons, candidate.selection_reasons, strict=True)).values(),
                key=reason_key,
            )
            if candidate.selection_reasons != expected_reasons or len(canonical_reasons) != len(
                set(canonical_reasons)
            ):
                raise ValueError("candidate selection_reasons must be sorted and unique")
            _validate_direct_match_metadata(candidate, self.search_frame, self.search_round)

        link_ids = [link.link_id for link in self.objective_lineage_links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("objective lineage link IDs must be unique")
        link_keys = [
            (
                link.link_type,
                link.source_oid,
                link.target_oid,
                link.rule_id or "",
                link.patch_id or "",
                link.link_id,
            )
            for link in self.objective_lineage_links
        ]
        if link_keys != sorted(link_keys):
            raise ValueError("objective_lineage_links must be deterministically sorted")
        lineage_rules = {rule.rule_id: rule for rule in self.search_frame.lineage_regexes}
        links_by_id = {link.link_id: link for link in self.objective_lineage_links}
        for link in self.objective_lineage_links:
            payload = {
                "link_type": link.link_type,
                "source_oid": link.source_oid,
                "target_oid": link.target_oid,
            }
            if link.link_type == "patch_id":
                payload["patch_id"] = link.patch_id
            else:
                payload["rule_id"] = link.rule_id
                rule = lineage_rules.get(link.rule_id or "")
                if rule is None:
                    raise ValueError("trailer link uses an unknown lineage rule")
                if rule.link_type != link.link_type:
                    raise ValueError("trailer link type differs from its frozen lineage rule")
            if link.link_id != sha256_hex(canonical_bytes(payload)):
                raise ValueError("lineage link_id does not bind its semantic fields")
            if link.source_oid not in candidate_oids or link.target_oid not in candidate_oids:
                raise ValueError("every lineage endpoint must belong to the candidate universe")
            if link.link_type != "patch_id":
                target_message = candidate_by_oid[link.target_oid].diff_evidence.commit_message
                if not any(
                    match.group(1) == link.source_oid
                    for match in re.finditer(rule.pattern, target_message)
                ):
                    raise ValueError(
                        "trailer link must match its source in the target commit message"
                    )

        trailer_link_keys = {
            (link.link_type, link.source_oid, link.target_oid, link.rule_id)
            for link in self.objective_lineage_links
            if link.link_type != "patch_id"
        }
        for target in self.discovered_candidates:
            for rule in self.search_frame.lineage_regexes:
                for match in re.finditer(rule.pattern, target.diff_evidence.commit_message):
                    expected_link = (
                        rule.link_type,
                        match.group(1),
                        target.commit.commit_oid,
                        rule.rule_id,
                    )
                    if expected_link not in trailer_link_keys:
                        raise ValueError(
                            "every frozen trailer match requires its objective lineage link"
                        )

        direct_oids = {
            candidate.commit.commit_oid
            for candidate in self.discovered_candidates
            if any(reason.kind == "direct_match" for reason in candidate.selection_reasons)
        }
        for candidate in self.discovered_candidates:
            oid = candidate.commit.commit_oid
            for reason in candidate.selection_reasons:
                if reason.kind == "adjacent_context":
                    anchor = candidate_by_oid.get(reason.anchor_oid or "")
                    if anchor is None or anchor.commit.commit_oid not in direct_oids:
                        raise ValueError("adjacent reason anchor must be a direct candidate")
                    exact_edge = (
                        candidate.commit.selected_parent_oid == anchor.commit.commit_oid
                        or anchor.commit.selected_parent_oid == oid
                    )
                    if not exact_edge:
                        raise ValueError(
                            "adjacent reason anchor must be an exact first-parent neighbor"
                        )
                    if set(candidate.eligible_paths).isdisjoint(anchor.eligible_paths):
                        raise ValueError("adjacent reason anchor must share an exact eligible path")
                elif reason.kind == "lineage_context":
                    link = links_by_id.get(reason.lineage_link_id or "")
                    if link is None or link.source_oid != oid:
                        raise ValueError(
                            "lineage reason must resolve to a link whose source is the candidate"
                        )
                    if link.link_type == "patch_id":
                        raise ValueError("lineage reason must resolve to a trailer link")

        lineage_reason_pairs = {
            (candidate.commit.commit_oid, reason.lineage_link_id)
            for candidate in self.discovered_candidates
            for reason in candidate.selection_reasons
            if reason.kind == "lineage_context"
        }
        for link in self.objective_lineage_links:
            if link.link_type != "patch_id" and (link.source_oid, link.link_id) not in (
                lineage_reason_pairs
            ):
                raise ValueError("every trailer link requires its source lineage reason")

        rooted_oids = {
            candidate.commit.commit_oid
            for candidate in self.discovered_candidates
            if any(
                reason.kind in {"direct_match", "adjacent_context"}
                for reason in candidate.selection_reasons
            )
        }
        pending_links = [
            link for link in self.objective_lineage_links if link.link_type != "patch_id"
        ]
        changed = True
        while changed:
            changed = False
            for link in pending_links:
                if link.target_oid in rooted_oids and link.source_oid not in rooted_oids:
                    rooted_oids.add(link.source_oid)
                    changed = True
        if rooted_oids != candidate_oids:
            raise ValueError("every candidate must have a rooted direct or adjacent selection seed")
        universe = {
            "schema_version": self.schema_version,
            "search_spec_sha256": self.search_spec_sha256,
            "search_round": self.search_round,
            "discovered_candidates": [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.discovered_candidates
            ],
            "objective_lineage_links": [
                link.model_dump(mode="json", exclude_none=True)
                for link in self.objective_lineage_links
            ],
        }
        if self.candidate_universe_sha256 != sha256_hex(canonical_bytes(universe)):
            raise ValueError("candidate_universe_sha256 does not bind the candidate universe")
        return self


def verify_discovery_direct_matches(blob_dir: Path, ledger: DiscoveryLedger) -> None:
    """Replay every direct-match decision from the frozen patch CAS without Git."""

    ledger = DiscoveryLedger.model_validate(
        ledger.model_dump(mode="json", exclude_none=True)
    )
    for candidate in ledger.discovered_candidates:
        digest = candidate.diff_evidence.patch_sha256
        try:
            full_patch = read_regular_file(blob_dir / digest)
        except OSError as exc:
            raise ValueError(f"unable to read discovery patch CAS blob {digest}") from exc
        if sha256_hex(full_patch) != digest:
            raise ValueError(f"discovery patch CAS blob does not match digest {digest}")

        try:
            eligible_patch = extract_eligible_patch_bytes(
                full_patch,
                changed_paths=candidate.changed_paths,
                eligible_paths=candidate.eligible_paths,
            )
            expected = derive_direct_match_reasons(
                ledger.search_frame,
                ledger.search_round,
                subject=candidate.commit.subject,
                parent_count=len(candidate.commit.parent_oids),
                eligible_paths=candidate.eligible_paths,
                eligible_patch=eligible_patch,
            )
        except ValueError as exc:
            raise ValueError(
                "direct-match replay failed for commit "
                f"{candidate.commit.commit_oid}: {exc}"
            ) from exc

        actual = [
            reason
            for reason in candidate.selection_reasons
            if reason.kind == "direct_match"
        ]
        if actual != expected:
            raise ValueError(
                "direct-match replay differs from recorded reasons for commit "
                f"{candidate.commit.commit_oid}"
            )


class ReviewAttestation(_StrictModel):
    reviewer_id: StableId
    review_scope: Literal["complete_b0a_adjudication"]
    approval: Literal["approved"]
    review_revision: StableId
    written_statement: NonEmptyStr
    candidate_universe_sha256: Sha256
    reviewed_payload_sha256: Sha256


class CandidateCase(_StrictModel):
    case_id: StableId
    defect_class: DefectClass
    disposition: Literal["proposed", "rejected", "ambiguous"]
    rationale: NonEmptyStr
    evidence_refs: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_class(self) -> CandidateCase:
        _validate_b0a_class(self.defect_class)
        return self


class CandidateDisposition(_StrictModel):
    commit_oid: Oid
    disposition: Literal["rejected", "ambiguous"]
    reason_code: Literal[
        "non_bug",
        "out_of_taxonomy",
        "non_config_only",
        "indeterminate_oracle",
        "revert_or_duplicate",
        "insufficient_context",
    ]
    rationale: NonEmptyStr
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    adjudicator_id: StableId
    reviewer_id: StableId

    @model_validator(mode="after")
    def validate_disposition(self) -> CandidateDisposition:
        ambiguous_reasons = {"indeterminate_oracle", "insufficient_context"}
        expected = "ambiguous" if self.reason_code in ambiguous_reasons else "rejected"
        if self.disposition != expected:
            raise ValueError(f"reason_code {self.reason_code} requires {expected}")
        if self.adjudicator_id == self.reviewer_id:
            raise ValueError("reviewer must differ from adjudicator")
        return self


class SelectedParentEdge(_StrictModel):
    commit_oid: Oid
    parent_oid: Oid


class LineageResolution(_StrictModel):
    link_id: Sha256
    resolution: Literal["same_group", "separate"]
    affected_group_ids: list[StableId]
    rationale: NonEmptyStr

    @field_validator("affected_group_ids")
    @classmethod
    def validate_group_ids(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("affected_group_ids must be sorted and unique")
        return values


class CandidateGroupDecision(_StrictModel):
    fix_group_id: StableId
    commits: list[Oid] = Field(min_length=1)
    selected_parent_edges: list[SelectedParentEdge] = Field(min_length=1)
    root_cause_evidence_refs: list[EvidenceRef] = Field(min_length=1)
    case_decisions: list[CandidateCase] = Field(min_length=1)
    adjudicator_id: StableId
    reviewer_id: StableId
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def validate_group_decision(self) -> CandidateGroupDecision:
        if len(self.commits) != len(set(self.commits)):
            raise ValueError("group commits must be unique")
        root_cause_refs = [(ref.kind, ref.target_id) for ref in self.root_cause_evidence_refs]
        if len(root_cause_refs) != len(set(root_cause_refs)):
            raise ValueError("duplicate root-cause evidence refs are not allowed")
        if [edge.commit_oid for edge in self.selected_parent_edges] != self.commits:
            raise ValueError("selected_parent_edges must cover group commits in order")
        defect_classes = [case.defect_class for case in self.case_decisions]
        if len(defect_classes) != len(set(defect_classes)):
            raise ValueError("a fix group may contain at most one case per defect class")
        if self.adjudicator_id == self.reviewer_id:
            raise ValueError("reviewer must differ from adjudicator")
        return self


class CandidateFixGroup(_StrictModel):
    fix_group_id: StableId
    group_decision_sha256: Sha256
    commits: list[Oid] = Field(min_length=1)
    before_commit: Oid
    after_commit: Oid
    after_committed_at: Annotated[int, Field(ge=0)]
    changed_paths: list[str] = Field(min_length=1)
    config_only: bool
    diff_evidence: list[DiffEvidence] = Field(min_length=1)
    cases: list[CandidateCase] = Field(min_length=1)
    disposition_summary: Literal["proposed", "rejected", "ambiguous"]
    rationale: NonEmptyStr
    lineage_links: list[Sha256] = Field(default_factory=list)

    @property
    def counts_toward_gate(self) -> bool:
        return self.config_only and any(case.disposition == "proposed" for case in self.cases)

    @model_validator(mode="after")
    def validate_group(self) -> CandidateFixGroup:
        if len(self.commits) != len(set(self.commits)):
            raise ValueError("group commits must be unique")
        if self.after_commit != self.commits[-1]:
            raise ValueError("after_commit must be the final group commit")
        if self.changed_paths != sorted(set(self.changed_paths)):
            raise ValueError("changed_paths must be sorted and unique")
        for path in self.changed_paths:
            _validate_posix_relative(path)
        if [item.commit_oid for item in self.diff_evidence] != self.commits:
            raise ValueError("diff_evidence must follow the complete commit range")
        defect_classes = [case.defect_class for case in self.cases]
        if len(defect_classes) != len(set(defect_classes)):
            raise ValueError("a fix group may contain at most one case per defect class")
        case_dispositions = {case.disposition for case in self.cases}
        expected_summary = (
            "ambiguous"
            if "ambiguous" in case_dispositions
            else "proposed"
            if "proposed" in case_dispositions
            else "rejected"
        )
        if self.disposition_summary != expected_summary:
            raise ValueError("disposition_summary must be derived from case dispositions")
        return self


class EvidenceCounts(_StrictModel):
    proposed: Annotated[int, Field(ge=0)] = 0
    qualified_candidate: Literal[0] = 0
    accepted: Literal[0] = 0
    rejected: Annotated[int, Field(ge=0)] = 0
    ambiguous: Annotated[int, Field(ge=0)] = 0


class ApplicabilityDeclaration(_StrictModel):
    defect_class: DefectClass
    domain_applicability: Literal["applicable", "not_applicable"]
    implementation_support: Literal["planned", "supported", "unsupported"]

    @model_validator(mode="after")
    def validate_flare_applicability(self) -> ApplicabilityDeclaration:
        _validate_b0a_class(self.defect_class)
        _validate_applicability_value(self.defect_class, self.domain_applicability)
        return self


class ApplicabilityRow(_StrictModel):
    defect_class: DefectClass
    domain_applicability: Literal["applicable", "not_applicable"]
    evidence_availability: Literal["found", "not_found"]
    evidence_counts: EvidenceCounts
    implementation_support: Literal["planned", "supported", "unsupported"]

    @model_validator(mode="before")
    @classmethod
    def derive_evidence_availability(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        raw_counts = data.get("evidence_counts")
        if isinstance(raw_counts, EvidenceCounts):
            validated_counts = raw_counts
        elif isinstance(raw_counts, Mapping):
            validated_counts = EvidenceCounts.model_validate(raw_counts)
        else:
            return data
        expected = "found" if any(validated_counts.model_dump().values()) else "not_found"
        provided = data.get("evidence_availability")
        if provided is not None and provided != expected:
            raise ValueError("evidence_availability must be derived from evidence_counts")
        data["evidence_counts"] = validated_counts
        data["evidence_availability"] = expected
        return data

    @model_validator(mode="after")
    def validate_flare_applicability(self) -> ApplicabilityRow:
        _validate_b0a_class(self.defect_class)
        _validate_applicability_value(self.defect_class, self.domain_applicability)
        expected = "found" if any(self.evidence_counts.model_dump().values()) else "not_found"
        if self.evidence_availability != expected:
            raise ValueError("evidence_availability must be derived from evidence_counts")
        return self


class GateSummary(_StrictModel):
    status: Literal["provisional_pass", "expanded_round_required", "insufficient_evidence"]
    proposed_groups: Annotated[int, Field(ge=0)]
    proposed_classes: Annotated[int, Field(ge=0)]
    required_groups: Literal[8] = 8
    required_classes: Literal[4] = 4
    reason_code_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    failure_reasons: list[NonEmptyStr] = Field(default_factory=list)
    next_action: Literal["proceed_to_b0b", "run_expanded_round", "stop_flare_heavy_investment"]

    @model_validator(mode="after")
    def validate_status_action(self) -> GateSummary:
        expected_action = {
            "provisional_pass": "proceed_to_b0b",
            "expanded_round_required": "run_expanded_round",
            "insufficient_evidence": "stop_flare_heavy_investment",
        }[self.status]
        if self.next_action != expected_action:
            raise ValueError("next_action does not match gate status")
        passed = self.proposed_groups >= 8 and self.proposed_classes >= 4
        if (self.status == "provisional_pass") != passed:
            raise ValueError("gate status does not match proposed group/class counts")
        return self


class CandidateLedger(_StrictModel):
    schema_version: Literal["flare-b0a@1"] = FLARE_B0A_SCHEMA_VERSION
    search_frame: FlareSearchSpec
    search_spec_sha256: Sha256
    search_registration: SearchRegistration
    search_round: Literal["initial", "expanded"]
    observed_revision_count: Annotated[int, Field(gt=0)]
    discovery_tool: DiscoveryTool
    discovery_ledger_sha256: Sha256
    candidate_universe_sha256: Sha256
    adjudication_evidence_sha256: Sha256
    evidence_revision: StableId
    prior_candidate_ledger_sha256: Sha256 | None = None
    prior_decision_sha256: Sha256 | None = None
    adjudicator_ids: list[StableId]
    reviewer_ids: list[StableId]
    groups: list[CandidateFixGroup]
    candidate_decisions: list[CandidateDisposition]
    applicability_matrix: list[ApplicabilityRow]
    gate_summary: GateSummary
    lineage_resolutions: list[LineageResolution]

    @model_validator(mode="after")
    def validate_candidate_ledger(self) -> CandidateLedger:
        _validate_exact_matrix(self.applicability_matrix, "applicability_matrix")
        has_prior = (
            self.prior_candidate_ledger_sha256 is not None,
            self.prior_decision_sha256 is not None,
        )
        if self.search_round == "expanded" and has_prior != (True, True):
            raise ValueError("expanded candidate ledger requires both prior hashes")
        if self.search_round == "initial" and has_prior != (False, False):
            raise ValueError("initial candidate ledger forbids prior hashes")
        group_ids = [group.fix_group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("candidate ledger fix_group_id values must be globally unique")
        case_ids = [case.case_id for group in self.groups for case in group.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("candidate ledger case_id values must be globally unique")
        grouped = [oid for group in self.groups for oid in group.commits]
        excluded = [item.commit_oid for item in self.candidate_decisions]
        if len(grouped) != len(set(grouped)) or len(excluded) != len(set(excluded)):
            raise ValueError("candidate assignments must be unique")
        if set(grouped) & set(excluded):
            raise ValueError("grouped and candidate-level decisions must be disjoint")
        if set(self.adjudicator_ids) & set(self.reviewer_ids):
            raise ValueError("reviewers must differ from adjudicators")
        return self


class AdjudicationEvidence(_StrictModel):
    schema_version: Literal["flare-b0a@1"] = FLARE_B0A_SCHEMA_VERSION
    evidence_revision: StableId
    search_round: Literal["initial", "expanded"]
    discovery_ledger_sha256: Sha256
    candidate_universe_sha256: Sha256
    prior_candidate_ledger_sha256: Sha256 | None = None
    prior_decision_sha256: Sha256 | None = None
    source_artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    applicability_declarations: tuple[ApplicabilityDeclaration, ...]
    group_decisions: list[CandidateGroupDecision]
    candidate_decisions: list[CandidateDisposition]
    lineage_resolutions: list[LineageResolution]
    review_attestation: ReviewAttestation

    @model_validator(mode="after")
    def validate_adjudication_evidence(self) -> AdjudicationEvidence:
        _validate_exact_matrix(self.applicability_declarations, "applicability_declarations")
        has_prior = (
            self.prior_candidate_ledger_sha256 is not None,
            self.prior_decision_sha256 is not None,
        )
        if self.search_round == "expanded" and has_prior != (True, True):
            raise ValueError("expanded adjudication requires both prior hashes")
        if self.search_round == "initial" and has_prior != (False, False):
            raise ValueError("initial adjudication forbids prior hashes")
        artifact_ids = [artifact.artifact_id for artifact in self.source_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("source artifact IDs must be unique")
        group_ids = [group.fix_group_id for group in self.group_decisions]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("evidence fix_group_id values must be globally unique")
        case_ids = [case.case_id for group in self.group_decisions for case in group.case_decisions]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evidence case_id values must be globally unique")
        reviewer_id = self.review_attestation.reviewer_id
        if any(group.reviewer_id != reviewer_id for group in self.group_decisions):
            raise ValueError("group reviewer IDs must match the review attestation")
        if any(item.reviewer_id != reviewer_id for item in self.candidate_decisions):
            raise ValueError("candidate reviewer IDs must match the review attestation")
        if any(group.adjudicator_id == reviewer_id for group in self.group_decisions):
            raise ValueError("reviewer must differ from every group adjudicator")
        if any(item.adjudicator_id == reviewer_id for item in self.candidate_decisions):
            raise ValueError("reviewer must differ from every candidate adjudicator")
        if self.review_attestation.candidate_universe_sha256 != self.candidate_universe_sha256:
            raise ValueError("attestation candidate-universe hash does not match")
        payload = self.model_dump(mode="json", exclude={"review_attestation"}, exclude_none=True)
        expected_hash = sha256_hex(canonical_bytes(payload))
        if self.review_attestation.reviewed_payload_sha256 != expected_hash:
            raise ValueError("review attestation does not bind the adjudication payload")
        return self


class B0ADecision(_StrictModel):
    schema_version: Literal["flare-b0a@1"] = FLARE_B0A_SCHEMA_VERSION
    candidate_ledger_sha256: Sha256
    gate: GateSummary


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _create_staging_file(target: Path) -> tuple[Path, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    while True:
        staging_path = target.parent / f".gameforge-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(staging_path, flags, 0o600)
        except FileExistsError:
            continue
        return staging_path, descriptor


def _write_staged_file(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("staging write made no progress")
        offset += written
    os.fsync(descriptor)


def _verify_output(path: Path, data: bytes) -> None:
    try:
        published = read_regular_file(path)
    except OSError as exc:
        raise FileExistsError(f"published target is not reusable: {path}") from exc
    if published != data:
        raise FileExistsError(f"published target has different bytes: {path}")


def write_new_or_identical(path: Path, data: bytes) -> None:
    write_set_new_or_identical({path: data})


def write_set_new_or_identical(outputs: Mapping[Path, bytes]) -> None:
    items = [(Path(path), data) for path, data in outputs.items()]
    missing: list[tuple[Path, bytes]] = []
    for path, data in items:
        try:
            existing = read_regular_file(path)
        except FileNotFoundError:
            missing.append((path, data))
        except OSError as exc:
            raise FileExistsError(
                f"target already exists and is not reusable: {path}"
            ) from exc
        else:
            if existing != data:
                raise FileExistsError(
                    f"target already exists with different bytes: {path}"
                )

    staged: dict[Path, Path] = {}
    owned_staging: set[Path] = set()
    try:
        for path, data in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging_path, descriptor = _create_staging_file(path)
            staged[path] = staging_path
            owned_staging.add(staging_path)
            try:
                _write_staged_file(descriptor, data)
            finally:
                os.close(descriptor)

        missing_paths = set(staged)
        for path, data in items:
            if path not in missing_paths:
                _verify_output(path, data)

        for index, (path, data) in enumerate(items):
            for prior_path, prior_data in items[:index]:
                _verify_output(prior_path, prior_data)

            staging_path = staged.get(path)
            if staging_path is None:
                _verify_output(path, data)
                continue

            if _path_entry_exists(path):
                _verify_output(path, data)
                continue

            os.replace(staging_path, path)
            owned_staging.discard(staging_path)
            _verify_output(path, data)

        for path, data in items:
            _verify_output(path, data)
    finally:
        for staging_path in owned_staging:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass



def put_blob(blob_dir: Path, data: bytes) -> tuple[str, str]:
    digest = sha256_hex(data)
    write_new_or_identical(blob_dir / digest, data)
    return digest, f"blobs/{digest}"
