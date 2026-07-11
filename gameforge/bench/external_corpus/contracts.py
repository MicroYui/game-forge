"""Source-neutral contracts for auditable external-corpus evidence."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections import Counter
from datetime import date, datetime, timedelta
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping, TypeVar
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

from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json


GIT_EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
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


Oid = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", min_length=1),
]
VersionId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$", min_length=1),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ModelT = TypeVar("ModelT", bound=BaseModel)


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
        if re.compile(value).groups != 1:
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


class HistoryRange(_StrictModel):
    committed_at_gte: Annotated[int, Field(ge=0)] | None = None
    after_exclusive_oid: Oid | None = None
    expected_commit_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_lower_bound(self) -> HistoryRange:
        if self.committed_at_gte is not None and self.after_exclusive_oid is not None:
            raise ValueError("history range accepts only one lower bound")
        return self


class CandidateOrderTerm(_StrictModel):
    field: Literal["committed_at", "commit_oid"]
    direction: Literal["ascending", "descending"]


class NativeValidatorCommand(_StrictModel):
    command_id: StableId
    argv: tuple[NonEmptyStr, ...]
    network: Literal["forbidden"] = "forbidden"

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("validator command argv must not be empty")
        if any("\x00" in token for token in value):
            raise ValueError("validator command argv must not contain NUL")
        if PurePosixPath(value[0]).name.lower() in {
            "bash",
            "cmd",
            "cmd.exe",
            "powershell",
            "pwsh",
            "sh",
            "zsh",
        }:
            raise ValueError("validator command must not invoke a shell")
        if any(re.search(r"[;|&<>`\n\r]|\$\(", token) is not None for token in value):
            raise ValueError("validator command argv must not contain shell fragments")
        return value


class TaxonomyApplicability(_StrictModel):
    defect_class: DefectClass
    domain_applicability: Literal["applicable", "not_applicable"]
    implementation_support: Literal["implemented", "planned", "unsupported"]
    rationale: NonEmptyStr


class B0AProtocol(_StrictModel):
    candidate_limit: Annotated[int, Field(gt=0)]
    expected_matched_candidate_count: Annotated[int, Field(ge=0)]
    expected_config_only_candidate_count: Annotated[int, Field(ge=0)]
    minimum_independent_groups: Annotated[int, Field(gt=0)]
    minimum_domain_applicable_classes: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_counts(self) -> B0AProtocol:
        if self.expected_config_only_candidate_count > self.expected_matched_candidate_count:
            raise ValueError("config-only candidate count cannot exceed matched count")
        return self


class SourceProfile(_StrictModel):
    schema_version: Literal["external-source-profile@1"]
    source_id: StableId
    profile_version: VersionId
    repository_url: NonEmptyStr
    pinned_head: Oid
    history_range: HistoryRange
    config_include_globs: tuple[NonEmptyStr, ...]
    config_exclude_globs: tuple[NonEmptyStr, ...]
    message_rules: tuple[RegexRule, ...]
    diff_rules: tuple[RegexRule, ...]
    lineage_rules: tuple[LineageRegexRule, ...]
    candidate_order: tuple[CandidateOrderTerm, CandidateOrderTerm]
    license_id: StableId
    notice_files: tuple[NonEmptyStr, ...]
    native_validator_commands: tuple[NativeValidatorCommand, ...]
    parser_version: VersionId
    query_complete_closure: tuple[StableId, ...]
    taxonomy_applicability: tuple[TaxonomyApplicability, ...]
    qualification_predicate_ids: tuple[StableId, ...]
    b0a_protocol: B0AProtocol

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("repository_url must be a plain HTTPS URL")
        return value

    @field_validator("config_include_globs", "config_exclude_globs")
    @classmethod
    def validate_globs(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        if info.field_name == "config_include_globs" and not values:
            raise ValueError("config include globs must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("config globs must be unique")
        return tuple(_validate_posix_relative(value) for value in values)

    @field_validator("notice_files")
    @classmethod
    def validate_notice_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("notice files must be nonempty and unique")
        return tuple(_validate_posix_relative(value) for value in values)

    @field_validator("native_validator_commands")
    @classmethod
    def validate_commands(
        cls, values: tuple[NativeValidatorCommand, ...]
    ) -> tuple[NativeValidatorCommand, ...]:
        ids = [command.command_id for command in values]
        if not values or len(ids) != len(set(ids)):
            raise ValueError("native validator commands must be nonempty with unique IDs")
        return values

    @field_validator("query_complete_closure", "qualification_predicate_ids")
    @classmethod
    def validate_id_tuple(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError(f"{info.field_name} must be nonempty and unique")
        return values

    @model_validator(mode="after")
    def validate_complete_profile(self) -> SourceProfile:
        rule_ids = [rule.rule_id for rule in self.message_rules]
        rule_ids.extend(rule.rule_id for rule in self.diff_rules)
        rule_ids.extend(rule.rule_id for rule in self.lineage_rules)
        if not self.message_rules and not self.diff_rules:
            raise ValueError("at least one discovery rule is required")
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("source profile rule IDs must be globally unique")

        order_fields = [term.field for term in self.candidate_order]
        if order_fields != ["committed_at", "commit_oid"]:
            raise ValueError("candidate order must be committed_at followed by commit_oid")

        taxonomy_classes = [row.defect_class for row in self.taxonomy_applicability]
        if len(taxonomy_classes) != len(DefectClass) or set(taxonomy_classes) != set(DefectClass):
            raise ValueError("taxonomy applicability must contain every defect class exactly once")
        applicable_count = sum(
            row.domain_applicability == "applicable" for row in self.taxonomy_applicability
        )
        if self.b0a_protocol.minimum_domain_applicable_classes > applicable_count:
            raise ValueError("class gate cannot exceed domain-applicable taxonomy classes")
        if (
            self.b0a_protocol.expected_matched_candidate_count
            > self.history_range.expected_commit_count
        ):
            raise ValueError("matched candidate count cannot exceed history count")
        return self


class AdapterBinding(_StrictModel):
    source_id: StableId
    reader_id: StableId
    reader_version: VersionId
    adapter_format_id: StableId
    adapter_version: VersionId
    ir_schema_version: VersionId
    mapping_spec_sha256: Sha256


class SearchRegistration(_StrictModel):
    project_commit_oid: Oid
    profile_repo_relative_path: str

    @field_validator("profile_repo_relative_path")
    @classmethod
    def validate_profile_path(cls, value: str) -> str:
        return _validate_posix_relative(value, suffix=".json")


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
        else:
            if self.selected_parent_oid is not None:
                raise ValueError("a root commit cannot have selected_parent_oid")
            if self.diff_base_oid != GIT_EMPTY_TREE_OID:
                raise ValueError("a root commit diff_base_oid must be the SHA-1 empty-tree OID")
        return self


class SelectionReason(_StrictModel):
    kind: Literal["direct_match", "adjacent_context", "lineage_context"]
    rule_ids: list[StableId] = Field(default_factory=list)
    anchor_oid: Oid | None = None
    lineage_link_id: Sha256 | None = None

    @model_validator(mode="after")
    def validate_reason_details(self) -> SelectionReason:
        if self.rule_ids != sorted(set(self.rule_ids)):
            raise ValueError("selection rule IDs must be sorted and unique")
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
        payload: dict[str, str | None] = {
            "link_type": self.link_type,
            "source_oid": self.source_oid,
            "target_oid": self.target_oid,
        }
        if self.link_type == "patch_id":
            payload["patch_id"] = self.patch_id
        else:
            payload["rule_id"] = self.rule_id
        if self.link_id != sha256_hex(canonical_bytes(payload)):
            raise ValueError("lineage link_id does not bind its semantic fields")
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
    reviewer_kind: Literal["human"]
    reviewer_id: StableId
    reviewed_at: datetime
    written_statement: NonEmptyStr
    candidate_universe_sha256: Sha256
    reviewed_payload_sha256: Sha256

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("reviewed_at must be timezone-aware UTC")
        return value


class CommitMetadata(_StrictModel):
    commit: CandidateCommit
    full_message: str


class DiscoveryTool(_StrictModel):
    tool_version: VersionId
    project_commit_oid: Oid
    git_version: NonEmptyStr
    python_implementation: NonEmptyStr
    python_version: NonEmptyStr
    python_build: tuple[str, str]
    unicode_version: NonEmptyStr


class DiscoveryLedger(_StrictModel):
    schema_version: Literal["external-corpus-b0a@1"] = "external-corpus-b0a@1"
    source_id: StableId
    source_profile: SourceProfile
    source_profile_sha256: Sha256
    search_registration: SearchRegistration
    observed_history_count: Annotated[int, Field(gt=0)]
    matched_candidate_count: Annotated[int, Field(ge=0)]
    config_only_candidate_count: Annotated[int, Field(ge=0)]
    discovery_tool: DiscoveryTool
    discovered_candidates: list[DiscoveredCandidate]
    objective_lineage_links: list[LineageLink]
    candidate_universe_sha256: Sha256

    @model_validator(mode="after")
    def validate_discovery_bindings(self) -> DiscoveryLedger:
        if self.source_id != self.source_profile.source_id:
            raise ValueError("source_id must match source_profile")
        expected_profile_sha = sha256_hex(canonical_bytes(self.source_profile))
        if self.source_profile_sha256 != expected_profile_sha:
            raise ValueError("source_profile_sha256 does not bind source_profile")
        if self.observed_history_count != self.source_profile.history_range.expected_commit_count:
            raise ValueError("observed_history_count differs from the registered history range")
        protocol = self.source_profile.b0a_protocol
        if self.matched_candidate_count != protocol.expected_matched_candidate_count:
            raise ValueError("matched_candidate_count differs from the registered expectation")
        if self.config_only_candidate_count != protocol.expected_config_only_candidate_count:
            raise ValueError("config_only_candidate_count differs from the registered expectation")
        if self.config_only_candidate_count > self.matched_candidate_count:
            raise ValueError("config_only_candidate_count cannot exceed matched_candidate_count")
        expected_selected_count = min(
            protocol.candidate_limit, self.matched_candidate_count
        )
        if len(self.discovered_candidates) != expected_selected_count:
            raise ValueError(
                "selected candidate count differs from the registered candidate limit"
            )
        if self.discovery_tool.project_commit_oid != self.search_registration.project_commit_oid:
            raise ValueError("discovery tool commit must match search registration commit")

        candidate_oids = [item.commit.commit_oid for item in self.discovered_candidates]
        if len(candidate_oids) != len(set(candidate_oids)):
            raise ValueError("discovered candidate commit OIDs must be unique")
        expected_order = list(self.discovered_candidates)
        for term in reversed(self.source_profile.candidate_order):
            expected_order.sort(
                key=lambda item: getattr(item.commit, term.field),
                reverse=term.direction == "descending",
            )
        if self.discovered_candidates != expected_order:
            raise ValueError("discovered_candidates differ from registered candidate order")

        rule_ids = {
            rule.rule_id
            for rule in (
                *self.source_profile.message_rules,
                *self.source_profile.diff_rules,
                *self.source_profile.lineage_rules,
            )
        }
        candidate_oid_set = set(candidate_oids)
        for candidate in self.discovered_candidates:
            expected_eligible = [
                path
                for path in candidate.changed_paths
                if any(
                    posix_glob_matches(path, pattern)
                    for pattern in self.source_profile.config_include_globs
                )
                and not any(
                    posix_glob_matches(path, pattern)
                    for pattern in self.source_profile.config_exclude_globs
                )
            ]
            if candidate.eligible_paths != expected_eligible:
                raise ValueError("candidate eligible_paths differ from source profile")
            if candidate.config_only != (
                bool(candidate.changed_paths)
                and len(candidate.changed_paths) == len(expected_eligible)
            ):
                raise ValueError("candidate config_only must be derived from eligible paths")
            for reason in candidate.selection_reasons:
                if not set(reason.rule_ids) <= rule_ids:
                    raise ValueError("selection reason uses an unknown source-profile rule")

        link_ids = [link.link_id for link in self.objective_lineage_links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("objective lineage link IDs must be unique")
        for link in self.objective_lineage_links:
            if link.source_oid not in candidate_oid_set or link.target_oid not in candidate_oid_set:
                raise ValueError("lineage endpoints must belong to the selected candidate universe")

        universe_payload = {
            "source_id": self.source_id,
            "profile_sha256": self.source_profile_sha256,
            "ordered_candidate_oids": candidate_oids,
        }
        if self.candidate_universe_sha256 != sha256_hex(canonical_bytes(universe_payload)):
            raise ValueError("candidate_universe_sha256 does not bind the candidate universe")
        return self


class CandidateCase(_StrictModel):
    case_id: StableId
    defect_class: DefectClass
    disposition: Literal["proposed", "rejected", "ambiguous"]
    rationale: NonEmptyStr
    evidence_refs: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_refs(self) -> CandidateCase:
        keys = [(ref.kind, ref.target_id) for ref in self.evidence_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate-case evidence refs must be unique")
        return self


class CandidateDisposition(_StrictModel):
    commit_oid: Oid
    disposition: Literal["rejected", "ambiguous"]
    reason_code: Literal[
        "non_bug",
        "out_of_taxonomy",
        "out_of_scope",
        "non_config_only",
        "style_or_typo_only",
        "insufficient_semantic_evidence",
        "insufficient_context",
        "indeterminate_oracle",
        "duplicate_lineage",
        "revert",
    ]
    rationale: NonEmptyStr
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    adjudicator_id: StableId

    @model_validator(mode="after")
    def validate_disposition(self) -> CandidateDisposition:
        ambiguous_reasons = {
            "insufficient_semantic_evidence",
            "insufficient_context",
            "indeterminate_oracle",
        }
        expected = "ambiguous" if self.reason_code in ambiguous_reasons else "rejected"
        if self.disposition != expected:
            raise ValueError(f"reason_code {self.reason_code} requires {expected}")
        refs = [(ref.kind, ref.target_id) for ref in self.evidence_refs]
        if len(refs) != len(set(refs)):
            raise ValueError("candidate-disposition evidence refs must be unique")
        return self


class SelectedParentEdge(_StrictModel):
    commit_oid: Oid
    parent_oid: Oid

    @model_validator(mode="after")
    def validate_distinct_endpoints(self) -> SelectedParentEdge:
        if self.commit_oid == self.parent_oid:
            raise ValueError("selected parent edge endpoints must differ")
        return self


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
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def validate_group_decision(self) -> CandidateGroupDecision:
        if len(self.commits) != len(set(self.commits)):
            raise ValueError("group commits must be unique")
        if [edge.commit_oid for edge in self.selected_parent_edges] != self.commits:
            raise ValueError("selected_parent_edges must cover group commits in order")
        refs = [(ref.kind, ref.target_id) for ref in self.root_cause_evidence_refs]
        if len(refs) != len(set(refs)):
            raise ValueError("root-cause evidence refs must be unique")
        case_ids = [case.case_id for case in self.case_decisions]
        classes = [case.defect_class for case in self.case_decisions]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("group case IDs must be unique")
        if len(classes) != len(set(classes)):
            raise ValueError("a fix group may contain at most one case per defect class")
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
    counts_toward_gate: bool

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
        case_ids = [case.case_id for case in self.cases]
        classes = [case.defect_class for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("fix-group case IDs must be unique")
        if len(classes) != len(set(classes)):
            raise ValueError("a fix group may contain at most one case per defect class")
        dispositions = {case.disposition for case in self.cases}
        expected_summary = (
            "ambiguous"
            if "ambiguous" in dispositions
            else "proposed"
            if "proposed" in dispositions
            else "rejected"
        )
        if self.disposition_summary != expected_summary:
            raise ValueError("disposition_summary must be derived from case dispositions")
        expected_gate = self.config_only and "proposed" in dispositions
        if self.counts_toward_gate != expected_gate:
            raise ValueError("counts_toward_gate must be derived from group evidence")
        if self.lineage_links != sorted(set(self.lineage_links)):
            raise ValueError("lineage_links must be sorted and unique")
        return self


class EvidenceCounts(_StrictModel):
    proposed: Annotated[int, Field(ge=0)] = 0
    qualified_candidate: Literal[0] = 0
    accepted: Literal[0] = 0
    rejected: Annotated[int, Field(ge=0)] = 0
    ambiguous: Annotated[int, Field(ge=0)] = 0


class ApplicabilityRow(_StrictModel):
    defect_class: DefectClass
    domain_applicability: Literal["applicable", "not_applicable"]
    implementation_support: Literal["implemented", "planned", "unsupported"]
    evidence_counts: EvidenceCounts


class GateSummary(_StrictModel):
    status: Literal["pass", "insufficient_evidence"]
    independent_proposed_groups: Annotated[int, Field(ge=0)]
    domain_applicable_proposed_classes: Annotated[int, Field(ge=0)]
    required_groups: Annotated[int, Field(gt=0)]
    required_classes: Annotated[int, Field(gt=0)]
    reason_code_counts: dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict
    )
    failure_reasons: list[NonEmptyStr] = Field(default_factory=list)
    next_action: Literal["proceed_to_b0b", "stop_source_and_use_fallback"]

    @model_validator(mode="after")
    def validate_status_action(self) -> GateSummary:
        passed = (
            self.independent_proposed_groups >= self.required_groups
            and self.domain_applicable_proposed_classes >= self.required_classes
        )
        expected_status = "pass" if passed else "insufficient_evidence"
        expected_action = (
            "proceed_to_b0b" if passed else "stop_source_and_use_fallback"
        )
        if self.status != expected_status:
            raise ValueError("gate status does not match proposed group/class counts")
        if self.next_action != expected_action:
            raise ValueError("next_action does not match gate status")
        if passed and self.failure_reasons:
            raise ValueError("a passing gate cannot contain failure_reasons")
        if not passed and not self.failure_reasons:
            raise ValueError("an insufficient gate requires failure_reasons")
        return self


class AdjudicationPayload(_StrictModel):
    schema_version: Literal["external-corpus-b0a@1"] = "external-corpus-b0a@1"
    source_id: StableId
    evidence_revision: VersionId
    discovery_ledger_sha256: Sha256
    candidate_universe_sha256: Sha256
    source_artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    group_decisions: list[CandidateGroupDecision]
    candidate_decisions: list[CandidateDisposition]
    lineage_resolutions: list[LineageResolution]

    @model_validator(mode="after")
    def validate_payload(self) -> AdjudicationPayload:
        artifact_ids = [artifact.artifact_id for artifact in self.source_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("source artifact IDs must be unique")
        group_ids = [group.fix_group_id for group in self.group_decisions]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("fix_group_id values must be globally unique")
        case_ids = [
            case.case_id for group in self.group_decisions for case in group.case_decisions
        ]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be globally unique")
        grouped = [oid for group in self.group_decisions for oid in group.commits]
        decided = [item.commit_oid for item in self.candidate_decisions]
        if len(grouped) != len(set(grouped)) or len(decided) != len(set(decided)):
            raise ValueError("candidate assignments must be unique")
        if set(grouped) & set(decided):
            raise ValueError("grouped and candidate-level decisions must be disjoint")
        resolution_ids = [item.link_id for item in self.lineage_resolutions]
        if len(resolution_ids) != len(set(resolution_ids)):
            raise ValueError("lineage resolution link IDs must be unique")
        return self


class AdjudicationEvidence(AdjudicationPayload):
    review_attestation: ReviewAttestation

    @model_validator(mode="after")
    def validate_attestation(self) -> AdjudicationEvidence:
        attestation = self.review_attestation
        if attestation.candidate_universe_sha256 != self.candidate_universe_sha256:
            raise ValueError("attestation candidate-universe hash does not match")
        adjudicators = {
            *(group.adjudicator_id for group in self.group_decisions),
            *(item.adjudicator_id for item in self.candidate_decisions),
        }
        if attestation.reviewer_id in adjudicators:
            raise ValueError("reviewer must differ from every adjudicator")
        payload_data = {
            name: getattr(self, name) for name in AdjudicationPayload.model_fields
        }
        payload = AdjudicationPayload.model_validate(payload_data)
        expected_hash = sha256_hex(canonical_bytes(payload))
        if attestation.reviewed_payload_sha256 != expected_hash:
            raise ValueError("review attestation does not bind the adjudication payload")
        return self


class CandidateLedger(_StrictModel):
    schema_version: Literal["external-corpus-b0a@1"] = "external-corpus-b0a@1"
    source_id: StableId
    source_profile: SourceProfile
    source_profile_sha256: Sha256
    search_registration: SearchRegistration
    discovery_ledger_sha256: Sha256
    candidate_universe_sha256: Sha256
    adjudication_evidence_sha256: Sha256
    evidence_revision: VersionId
    adjudicator_ids: list[StableId]
    reviewer_ids: list[StableId]
    groups: list[CandidateFixGroup]
    candidate_decisions: list[CandidateDisposition]
    applicability_matrix: list[ApplicabilityRow]
    gate_summary: GateSummary
    lineage_resolutions: list[LineageResolution]

    @model_validator(mode="after")
    def validate_candidate_ledger(self) -> CandidateLedger:
        if self.source_id != self.source_profile.source_id:
            raise ValueError("source_id must match source_profile")
        if self.source_profile_sha256 != sha256_hex(canonical_bytes(self.source_profile)):
            raise ValueError("source_profile_sha256 does not bind source_profile")
        if not self.adjudicator_ids or self.adjudicator_ids != sorted(
            set(self.adjudicator_ids)
        ):
            raise ValueError("adjudicator_ids must be nonempty, sorted, and unique")
        if not self.reviewer_ids or self.reviewer_ids != sorted(set(self.reviewer_ids)):
            raise ValueError("reviewer_ids must be nonempty, sorted, and unique")
        if set(self.adjudicator_ids) & set(self.reviewer_ids):
            raise ValueError("reviewers must differ from adjudicators")

        group_ids = [group.fix_group_id for group in self.groups]
        case_ids = [case.case_id for group in self.groups for case in group.cases]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("candidate ledger fix_group_id values must be unique")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("candidate ledger case_id values must be unique")
        grouped = [oid for group in self.groups for oid in group.commits]
        decided = [item.commit_oid for item in self.candidate_decisions]
        if len(grouped) != len(set(grouped)) or len(decided) != len(set(decided)):
            raise ValueError("candidate assignments must be unique")
        if set(grouped) & set(decided):
            raise ValueError("grouped and candidate-level decisions must be disjoint")

        matrix_classes = [row.defect_class for row in self.applicability_matrix]
        if len(matrix_classes) != len(DefectClass) or set(matrix_classes) != set(DefectClass):
            raise ValueError("applicability_matrix must contain every defect class exactly once")
        profile_rows = {
            row.defect_class: row for row in self.source_profile.taxonomy_applicability
        }
        for row in self.applicability_matrix:
            profile_row = profile_rows[row.defect_class]
            if (
                row.domain_applicability != profile_row.domain_applicability
                or row.implementation_support != profile_row.implementation_support
            ):
                raise ValueError("applicability_matrix must preserve source-profile taxonomy")

        proposed_groups = sum(group.counts_toward_gate for group in self.groups)
        proposed_classes = {
            case.defect_class
            for group in self.groups
            if group.counts_toward_gate
            for case in group.cases
            if case.disposition == "proposed"
            and profile_rows[case.defect_class].domain_applicability == "applicable"
        }
        protocol = self.source_profile.b0a_protocol
        if self.gate_summary.required_groups != protocol.minimum_independent_groups:
            raise ValueError("gate required_groups differs from source profile")
        if self.gate_summary.required_classes != protocol.minimum_domain_applicable_classes:
            raise ValueError("gate required_classes differs from source profile")
        if self.gate_summary.independent_proposed_groups != proposed_groups:
            raise ValueError("gate independent_proposed_groups differs from groups")
        if self.gate_summary.domain_applicable_proposed_classes != len(proposed_classes):
            raise ValueError("gate domain_applicable_proposed_classes differs from cases")

        reason_counts = Counter(item.reason_code for item in self.candidate_decisions)
        if self.gate_summary.reason_code_counts != dict(sorted(reason_counts.items())):
            raise ValueError("gate reason_code_counts differs from candidate decisions")
        resolution_ids = [item.link_id for item in self.lineage_resolutions]
        if len(resolution_ids) != len(set(resolution_ids)):
            raise ValueError("lineage resolution link IDs must be unique")
        return self


class B0ADecision(_StrictModel):
    schema_version: Literal["external-corpus-b0a@1"] = "external-corpus-b0a@1"
    source_id: StableId
    candidate_ledger_sha256: Sha256
    gate: GateSummary


class ReviewPackageRow(_StrictModel):
    commit: CandidateCommit
    full_message: str
    changed_paths: list[str]
    config_only: bool
    patch_sha256: Sha256
    lineage_links: list[Sha256] = Field(default_factory=list)

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("review-package changed_paths must be sorted and unique")
        return [_validate_posix_relative(value) for value in values]

    @field_validator("lineage_links")
    @classmethod
    def validate_lineage_links(cls, values: list[str]) -> list[str]:
        if values != sorted(set(values)):
            raise ValueError("review-package lineage_links must be sorted and unique")
        return values


class ReviewPackage(_StrictModel):
    schema_version: Literal["external-corpus-b0a@1"] = "external-corpus-b0a@1"
    source_id: StableId
    candidate_universe_sha256: Sha256
    discovery_ledger_sha256: Sha256
    review_status: Literal["awaiting_human"]
    rows: list[ReviewPackageRow]


def canonical_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
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
        or ".." in path_parts
        or "." in path_parts
        or ".." in pattern_parts
        or "." in pattern_parts
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


def read_regular_file(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"path is not a regular file: {path}")
    return path.read_bytes()


def load_canonical(path: Path, model_type: type[ModelT], expected_sha256: str) -> ModelT:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected digest must be 64 lowercase hexadecimal characters")
    raw = read_regular_file(path)
    actual_sha256 = sha256_hex(raw)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"canonical artifact digest mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    model = model_type.model_validate_json(raw)
    if canonical_bytes(model) != raw:
        raise ValueError(f"artifact is not canonical JSON: {path}")
    return model


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
                raise FileExistsError(f"target already exists with different bytes: {path}")

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
