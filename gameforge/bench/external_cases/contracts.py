"""Source-neutral contracts for lean external before/after evidence.

Unlike the historical B0A research ledger, these models bind product evidence:
upstream provenance, exact source trees, independent predicates, native-parser
results, generic GameForge findings, and the human-authored target patch.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json


Oid = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$", min_length=1),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VersionId = StableId


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalized_posix(value: str, *, suffix: str | None = None) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("must be a nonempty normalized POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("must be a normalized repository-relative POSIX path")
    if suffix is not None and path.suffix != suffix:
        raise ValueError(f"must end in {suffix}")
    return value


def _json_value(value: Any, *, exclude: set[str] | None = None) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude=exclude or set())
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def content_sha256(value: Any, *, exclude: set[str] | None = None) -> str:
    """Hash canonical JSON for a model or JSON-compatible evidence value."""

    payload = _json_value(value, exclude=exclude)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_bytes(value: Any, *, exclude: set[str] | None = None) -> bytes:
    return (canonical_json(_json_value(value, exclude=exclude)) + "\n").encode("utf-8")


class TargetLocator(_StrictModel):
    path: str
    record_kind: StableId
    record_name: NonEmptyStr

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalized_posix(value, suffix=".txt")


class ExternalCaseSpec(_StrictModel):
    schema_version: Literal["external-case-spec@1"]
    case_id: StableId
    source_id: StableId
    source_repository: NonEmptyStr
    license_id: StableId
    before_commit: Oid
    after_commit: Oid
    upstream_subject: NonEmptyStr
    upstream_pr: int | None = Field(default=None, gt=0)
    changed_paths: tuple[str, ...]
    defect_class: DefectClass
    target_locators: tuple[TargetLocator, ...]
    split: Literal["development", "verification"]
    predicate_id: StableId

    @field_validator("source_repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.params or parsed.query:
            raise ValueError("source_repository must be a plain HTTPS URL")
        return value

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("changed_paths must be nonempty and unique")
        normalized = tuple(_normalized_posix(value, suffix=".txt") for value in values)
        if any(not value.startswith("data/") for value in normalized):
            raise ValueError("changed_paths must match data/**/*.txt")
        return normalized

    @field_validator("target_locators")
    @classmethod
    def validate_targets(cls, values: tuple[TargetLocator, ...]) -> tuple[TargetLocator, ...]:
        if not values:
            raise ValueError("target_locators must not be empty")
        identities = [(item.path, item.record_kind, item.record_name) for item in values]
        if len(identities) != len(set(identities)):
            raise ValueError("target_locators must be unique")
        return values

    @model_validator(mode="after")
    def validate_case_binding(self) -> ExternalCaseSpec:
        if self.before_commit == self.after_commit:
            raise ValueError("before_commit and after_commit must differ")
        changed = set(self.changed_paths)
        if any(target.path not in changed for target in self.target_locators):
            raise ValueError("target locator paths must be present in changed_paths")
        return self


class TreeFile(_StrictModel):
    path: str
    sha256: Sha256
    size: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalized_posix(value)


class TreeArtifact(_StrictModel):
    files: tuple[TreeFile, ...]
    tree_sha256: Sha256

    @model_validator(mode="after")
    def validate_tree_hash(self) -> TreeArtifact:
        if not self.files:
            raise ValueError("tree artifact must contain at least one file")
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("tree files must be unique and sorted by path")
        expected = content_sha256(self.files)
        if self.tree_sha256 != expected:
            raise ValueError("tree_sha256 does not bind files")
        return self


class NativeEvidence(_StrictModel):
    parser_id: StableId
    parser_version: VersionId
    source_sha256: Sha256
    input_manifest_sha256: Sha256
    command: tuple[NonEmptyStr, ...]
    exit_code: int
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    compiler: NonEmptyStr

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("native command must not be empty")
        return value


class PredicateEvidence(_StrictModel):
    predicate_id: StableId
    status: Literal["violation", "clear", "unproven"]
    target_locators: tuple[TargetLocator, ...]
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_locators")
    @classmethod
    def validate_targets(cls, value: tuple[TargetLocator, ...]) -> tuple[TargetLocator, ...]:
        if not value:
            raise ValueError("predicate evidence must bind at least one target")
        return value


class FindingEvidence(_StrictModel):
    finding_id: NonEmptyStr
    defect_class: NonEmptyStr
    status: Literal["confirmed", "unproven"]
    entities: tuple[str, ...]
    evidence_sha256: Sha256


class HumanTarget(_StrictModel):
    patch_path: str
    patch_sha256: Sha256

    @field_validator("patch_path")
    @classmethod
    def validate_patch_path(cls, value: str) -> str:
        return _normalized_posix(value, suffix=".patch")


class ExternalCaseEvidence(_StrictModel):
    schema_version: Literal["external-case@1"]
    spec: ExternalCaseSpec
    before_tree: TreeArtifact
    after_tree: TreeArtifact
    native_before: NativeEvidence
    native_after: NativeEvidence
    predicate_before: PredicateEvidence
    predicate_after: PredicateEvidence
    reader_version: VersionId
    adapter_version: VersionId
    mapping_spec_sha256: Sha256
    findings_before: tuple[FindingEvidence, ...]
    findings_after: tuple[FindingEvidence, ...]
    human_target: HumanTarget
    agent_patch_sha256: Sha256 | None = None
    agent_target_snapshot_id: NonEmptyStr | None = None
    qualification_status: Literal["qualified", "miss"]
    failure_reasons: tuple[NonEmptyStr, ...] = ()
    evidence_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> ExternalCaseEvidence:
        payload = dict(values)
        payload.pop("evidence_sha256", None)
        payload["evidence_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_evidence(self) -> ExternalCaseEvidence:
        if self.predicate_before.predicate_id != self.spec.predicate_id:
            raise ValueError("predicate_before does not match case predicate_id")
        if self.predicate_after.predicate_id != self.spec.predicate_id:
            raise ValueError("predicate_after does not match case predicate_id")
        if self.qualification_status == "qualified" and self.failure_reasons:
            raise ValueError("qualified evidence cannot contain failure_reasons")
        if self.qualification_status == "miss" and not self.failure_reasons:
            raise ValueError("miss evidence must contain failure_reasons")
        expected = content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected:
            raise ValueError("evidence_sha256 does not bind external case evidence")
        return self


class ExternalCorpusManifest(_StrictModel):
    schema_version: Literal["external-corpus-manifest@1"]
    source_id: StableId
    pinned_head: Oid
    repository_url: NonEmptyStr
    reader_version: VersionId
    adapter_version: VersionId
    mapping_spec_sha256: Sha256
    cases: tuple[ExternalCaseEvidence, ...]
    manifest_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> ExternalCorpusManifest:
        payload = dict(values)
        payload.pop("manifest_sha256", None)
        payload["manifest_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_manifest(self) -> ExternalCorpusManifest:
        if not self.cases:
            raise ValueError("external corpus manifest must contain cases")
        case_ids = [case.spec.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("external corpus case ids must be unique")
        for case in self.cases:
            if case.spec.source_id != self.source_id:
                raise ValueError("case source_id differs from manifest")
            if case.reader_version != self.reader_version:
                raise ValueError("case reader_version differs from manifest")
            if case.adapter_version != self.adapter_version:
                raise ValueError("case adapter_version differs from manifest")
            if case.mapping_spec_sha256 != self.mapping_spec_sha256:
                raise ValueError("case mapping_spec_sha256 differs from manifest")
        expected = content_sha256(self, exclude={"manifest_sha256"})
        if self.manifest_sha256 != expected:
            raise ValueError("manifest_sha256 does not bind external corpus manifest")
        return self


__all__ = [
    "ExternalCaseEvidence",
    "ExternalCaseSpec",
    "ExternalCorpusManifest",
    "FindingEvidence",
    "HumanTarget",
    "NativeEvidence",
    "PredicateEvidence",
    "TargetLocator",
    "TreeArtifact",
    "TreeFile",
    "canonical_bytes",
    "content_sha256",
]
