"""Strict contracts and immutable storage for the Flare B0A evidence ledger."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.canonical import canonical_json


FLARE_B0A_SCHEMA_VERSION = "flare-b0a@1"

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
GIT_FIXED_ENVIRONMENT: Mapping[str, str] = MappingProxyType({
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "TZ": "UTC",
})

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

Oid = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", min_length=1),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _compile_regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    return pattern


def _validate_posix_relative(value: str, *, suffix: str | None = None) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("must be a nonempty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("must be a repository-relative POSIX path")
    if str(path) != value:
        raise ValueError("must be a normalized POSIX path")
    if suffix is not None and path.suffix != suffix:
        raise ValueError(f"must end in {suffix}")
    return value


def _validate_blob_binding(path: str, digest: str) -> None:
    if path != f"blobs/{digest}":
        raise ValueError("blob path must be blobs/{sha256}")


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


def _validate_applicability_value(
    defect_class: DefectClass, domain_applicability: str
) -> None:
    expected = _expected_applicability(defect_class)
    if domain_applicability != expected:
        raise ValueError(
            f"{defect_class.value} domain_applicability must be {expected} for Flare"
        )


def _validate_exact_matrix(rows: list[Any] | tuple[Any, ...], name: str) -> None:
    classes = [row.defect_class for row in rows]
    if len(classes) != len(B0A_DEFECT_CLASSES) or set(classes) != set(B0A_DEFECT_CLASSES):
        raise ValueError(f"{name} must contain each B0A defect class exactly once")


class RegexRule(_StrictModel):
    rule_id: StableId
    pattern: NonEmptyStr

    _validate_pattern = field_validator("pattern")(_compile_regex)


class LineageRegexRule(_StrictModel):
    rule_id: StableId
    link_type: Literal["backport", "cherry_pick", "revert"]
    pattern: NonEmptyStr

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        _compile_regex(value)
        compiled = re.compile(value)
        if compiled.groups != 1:
            raise ValueError("lineage regex must have exactly one OID capture group")
        return value


class GitCommandSpec(_StrictModel):
    common_prefix: tuple[str, ...]
    empty_tree_args: tuple[str, ...]
    eligible_path_suffix: tuple[str, ...]
    history_args: tuple[str, ...]
    metadata_args: tuple[str, ...]
    patch_args: tuple[str, ...]
    patch_id_args: tuple[str, ...]
    paths_args: tuple[str, ...]
    resolve_args: tuple[str, ...]
    version_command: tuple[str, ...]

    @model_validator(mode="after")
    def validate_frozen_commands(self) -> GitCommandSpec:
        expected = {
            "common_prefix": GIT_COMMON_PREFIX,
            "empty_tree_args": GIT_EMPTY_TREE_ARGS,
            "eligible_path_suffix": GIT_ELIGIBLE_PATH_SUFFIX,
            "history_args": GIT_HISTORY_ARGS,
            "metadata_args": GIT_METADATA_ARGS,
            "patch_args": GIT_PATCH_ARGS,
            "patch_id_args": GIT_PATCH_ID_ARGS,
            "paths_args": GIT_PATHS_ARGS,
            "resolve_args": GIT_RESOLVE_ARGS,
            "version_command": GIT_VERSION_COMMAND,
        }
        changed = [name for name, value in expected.items() if getattr(self, name) != value]
        if changed:
            raise ValueError(f"git_commands differ from frozen contract: {', '.join(changed)}")
        return self


class GitEnvironmentPolicy(_StrictModel):
    drop_inherited_prefixes: tuple[str, ...]
    fixed: Mapping[str, str]
    inherit_allowlist: tuple[str, ...]

    @field_validator("fixed", mode="after")
    @classmethod
    def freeze_fixed_environment(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("fixed")
    def serialize_fixed_environment(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def validate_frozen_policy(self) -> GitEnvironmentPolicy:
        if self.drop_inherited_prefixes != GIT_DROP_INHERITED_PREFIXES:
            raise ValueError("git_environment_policy.drop_inherited_prefixes differs")
        if self.inherit_allowlist != GIT_INHERIT_ALLOWLIST:
            raise ValueError("git_environment_policy.inherit_allowlist differs")
        if self.fixed != GIT_FIXED_ENVIRONMENT:
            raise ValueError("git_environment_policy.fixed differs")
        return self


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
    tool_version: NonEmptyStr
    project_commit_oid: Oid
    git_version: NonEmptyStr


class CandidateCommit(_StrictModel):
    commit_oid: Oid
    parent_oids: list[Oid]
    selected_parent_oid: Oid | None = None
    diff_base_oid: Oid
    committed_at: Annotated[int, Field(ge=0)]
    subject: str

    @model_validator(mode="after")
    def validate_parent_selection(self) -> CandidateCommit:
        if len(self.parent_oids) != len(set(self.parent_oids)):
            raise ValueError("parent_oids must be unique")
        if self.parent_oids:
            if self.selected_parent_oid != self.parent_oids[0]:
                raise ValueError("selected_parent_oid must be the first parent")
            if self.diff_base_oid != self.selected_parent_oid:
                raise ValueError("diff_base_oid must be the selected parent")
        elif self.selected_parent_oid is not None:
            raise ValueError("a root commit cannot have selected_parent_oid")
        return self


class SelectionReason(_StrictModel):
    kind: Literal["direct_match", "adjacent_context", "lineage_context"]
    rule_ids: list[StableId] = Field(default_factory=list)
    anchor_oid: Oid | None = None
    lineage_link_id: Sha256 | None = None

    @model_validator(mode="after")
    def validate_reason_details(self) -> SelectionReason:
        if len(self.rule_ids) != len(set(self.rule_ids)):
            raise ValueError("selection rule IDs must be unique")
        if self.kind == "direct_match":
            if not self.rule_ids or self.anchor_oid is not None or self.lineage_link_id is not None:
                raise ValueError("direct_match requires only rule_ids")
        elif self.kind == "adjacent_context":
            if self.anchor_oid is None or self.rule_ids or self.lineage_link_id is not None:
                raise ValueError("adjacent_context requires only anchor_oid")
        elif self.lineage_link_id is None or self.rule_ids or self.anchor_oid is not None:
            raise ValueError("lineage_context requires only lineage_link_id")
        return self


class DiffEvidence(_StrictModel):
    commit_oid: Oid
    patch_sha256: Sha256
    patch_blob: str
    commit_message: str

    @model_validator(mode="after")
    def validate_patch_blob(self) -> DiffEvidence:
        _validate_blob_binding(self.patch_blob, self.patch_sha256)
        return self


class LineageLink(_StrictModel):
    link_id: Sha256
    link_type: Literal["patch_id", "cherry_pick", "backport", "revert"]
    source_oid: Oid
    target_oid: Oid
    rule_id: StableId | None = None
    patch_id: Oid | None = None

    @model_validator(mode="after")
    def validate_link_evidence(self) -> LineageLink:
        if self.source_oid == self.target_oid:
            raise ValueError("lineage endpoints must differ")
        if self.link_type == "patch_id":
            if self.patch_id is None or self.rule_id is not None:
                raise ValueError("patch_id links require only patch_id evidence")
        elif self.rule_id is None or self.patch_id is not None:
            raise ValueError("trailer links require only rule_id evidence")
        return self


class DiscoveredCandidate(_StrictModel):
    commit: CandidateCommit
    changed_paths: list[str]
    eligible_paths: list[str]
    config_only: bool
    selection_reasons: list[SelectionReason]
    diff_evidence: DiffEvidence

    @field_validator("changed_paths", "eligible_paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("candidate paths must be sorted and unique")
        return [_validate_posix_relative(value) for value in values]

    @model_validator(mode="after")
    def validate_candidate(self) -> DiscoveredCandidate:
        if not self.changed_paths:
            raise ValueError("changed_paths must not be empty")
        if not self.selection_reasons:
            raise ValueError("selection_reasons must not be empty")
        if not set(self.eligible_paths) <= set(self.changed_paths):
            raise ValueError("eligible_paths must be a subset of changed_paths")
        if self.diff_evidence.commit_oid != self.commit.commit_oid:
            raise ValueError("diff evidence must refer to the candidate commit")
        return self


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
        if self.observed_revision_count != self.search_frame.expected_revision_count:
            raise ValueError("observed_revision_count differs from registered expectation")
        candidate_keys = [
            (item.commit.committed_at, item.commit.commit_oid)
            for item in self.discovered_candidates
        ]
        if candidate_keys != sorted(set(candidate_keys)):
            raise ValueError("discovered_candidates must be sorted and unique")
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


class EvidenceRef(_StrictModel):
    kind: Literal["commit_message", "patch_blob", "lineage_link", "source_artifact"]
    target_id: str

    @field_validator("target_id")
    @classmethod
    def validate_target_id(cls, value: str, info: Any) -> str:
        kind = info.data.get("kind")
        pattern = {
            "commit_message": r"[0-9a-f]{40}",
            "patch_blob": r"[0-9a-f]{64}",
            "lineage_link": r"[0-9a-f]{64}",
            "source_artifact": r"[A-Za-z0-9][A-Za-z0-9._:-]*",
        }.get(kind)
        if pattern is None or re.fullmatch(pattern, value) is None:
            raise ValueError(f"target_id is invalid for evidence kind {kind}")
        return value


class EvidenceArtifact(_StrictModel):
    artifact_id: StableId
    artifact_type: Literal["issue", "pull_request"]
    source_url: str
    retrieval_date: date
    blob_path: str
    blob_sha256: Sha256

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def validate_blob(self) -> EvidenceArtifact:
        _validate_blob_binding(self.blob_path, self.blob_sha256)
        return self


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
        if not values or len(values) != len(set(values)):
            raise ValueError("affected_group_ids must be nonempty and unique")
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
        root_cause_refs = [
            (ref.kind, ref.target_id) for ref in self.root_cause_evidence_refs
        ]
        if len(root_cause_refs) != len(set(root_cause_refs)):
            raise ValueError("duplicate root-cause evidence refs are not allowed")
        if [edge.commit_oid for edge in self.selected_parent_edges] != self.commits:
            raise ValueError("selected_parent_edges must cover group commits in order")
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
        expected = (
            "found" if any(validated_counts.model_dump().values()) else "not_found"
        )
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
        expected = (
            "found" if any(self.evidence_counts.model_dump().values()) else "not_found"
        )
        if self.evidence_availability != expected:
            raise ValueError("evidence_availability must be derived from evidence_counts")
        return self


class GateSummary(_StrictModel):
    status: Literal[
        "provisional_pass", "expanded_round_required", "insufficient_evidence"
    ]
    proposed_groups: Annotated[int, Field(ge=0)]
    proposed_classes: Annotated[int, Field(ge=0)]
    required_groups: Literal[8] = 8
    required_classes: Literal[4] = 4
    reason_code_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict
    )
    failure_reasons: list[NonEmptyStr] = Field(default_factory=list)
    next_action: Literal[
        "proceed_to_b0b", "run_expanded_round", "stop_flare_heavy_investment"
    ]

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
        _validate_exact_matrix(
            self.applicability_declarations, "applicability_declarations"
        )
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
        payload = self.model_dump(
            mode="json", exclude={"review_attestation"}, exclude_none=True
        )
        expected_hash = sha256_hex(canonical_bytes(payload))
        if self.review_attestation.reviewed_payload_sha256 != expected_hash:
            raise ValueError("review attestation does not bind the adjudication payload")
        return self


class B0ADecision(_StrictModel):
    schema_version: Literal["flare-b0a@1"] = FLARE_B0A_SCHEMA_VERSION
    candidate_ledger_sha256: Sha256
    gate: GateSummary


def canonical_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    payload: Any
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=True)
    else:
        payload = value
    return canonical_json(payload).encode("utf-8") + b"\n"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def posix_glob_matches(path: str, pattern: str) -> bool:
    path_parts = tuple(path.split("/"))
    pattern_parts = tuple(pattern.split("/"))
    if (
        not path
        or not pattern
        or path.startswith("/")
        or pattern.startswith("/")
        or "\\" in path
        or "\\" in pattern
        or "" in path_parts
        or "" in pattern_parts
    ):
        return False

    @lru_cache(maxsize=None)
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        component = pattern_parts[pattern_index]
        if component == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], component)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _stream_identity(stream: Any) -> tuple[int, int]:
    metadata = os.fstat(stream.fileno())
    return metadata.st_dev, metadata.st_ino


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == identity:
        path.unlink(missing_ok=True)


def write_new_or_identical(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.open("xb")
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise FileExistsError(f"target already exists and is not reusable: {path}") from exc
        if existing != data:
            raise FileExistsError(f"target already exists with different bytes: {path}")
    else:
        identity = _stream_identity(stream)
        try:
            with stream:
                stream.write(data)
        except BaseException:
            _unlink_if_same_file(path, identity)
            raise


def write_set_new_or_identical(outputs: Mapping[Path, bytes]) -> None:
    items = list(outputs.items())
    missing: list[tuple[Path, bytes]] = []
    for path, data in items:
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise FileExistsError(
                    f"target already exists and is not reusable: {path}"
                ) from exc
            if existing != data:
                raise FileExistsError(f"target already exists with different bytes: {path}")
        else:
            missing.append((path, data))

    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        for path, data in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                created.append((path, _stream_identity(stream)))
                stream.write(data)
    except BaseException:
        for path, identity in reversed(created):
            _unlink_if_same_file(path, identity)
        raise


def put_blob(blob_dir: Path, data: bytes) -> tuple[str, str]:
    digest = sha256_hex(data)
    write_new_or_identical(blob_dir / digest, data)
    return digest, f"blobs/{digest}"
