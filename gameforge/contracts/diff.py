"""Bounded field-diff and three-way rebase wire contracts for M4."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.storage import PageV1


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JsonPointer = str


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _is_json_pointer(value: str) -> bool:
    if value == "":
        return True
    if not value.startswith("/"):
        return False
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


class _MissingJsonValueState(_FrozenModel):
    presence: Literal["missing"] = "missing"


class _PresentJsonValueState(_FrozenModel):
    presence: Literal["present"] = "present"
    value: JsonValue


class JsonValueState(
    RootModel[
        Annotated[
            _MissingJsonValueState | _PresentJsonValueState,
            Field(discriminator="presence"),
        ]
    ]
):
    """A JSON value whose absent and explicitly-null states cannot collapse."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    @property
    def presence(self) -> Literal["missing", "present"]:
        return self.root.presence

    @property
    def value(self) -> JsonValue:
        if isinstance(self.root, _MissingJsonValueState):
            raise AttributeError("a missing JSON value has no value field")
        return self.root.value


class SnapshotDiffEntry(_FrozenModel):
    path: JsonPointer
    before: JsonValueState
    after: JsonValueState

    @field_validator("path")
    @classmethod
    def _valid_pointer(cls, value: str) -> str:
        if not _is_json_pointer(value):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return value

    @model_validator(mode="after")
    def _changed(self) -> "SnapshotDiffEntry":
        if self.before == self.after:
            raise ValueError("a diff entry must change the value state")
        return self


class SnapshotDiff(_FrozenModel):
    diff_schema_version: Literal["snapshot-diff@1"] = "snapshot-diff@1"
    base_snapshot_id: NonEmptyStr
    target_snapshot_id: NonEmptyStr
    entry_count: int = Field(ge=0)


class SnapshotDiffEntryPage(_FrozenModel):
    """One bounded, snapshot-bound page of a diff's entries."""

    diff: SnapshotDiff
    page: PageV1[SnapshotDiffEntry]

    @model_validator(mode="after")
    def _canonical_page(self) -> "SnapshotDiffEntryPage":
        paths = tuple(entry.path for entry in self.page.items)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("diff entries must be uniquely sorted by JSON Pointer")
        if self.diff.entry_count < len(paths):
            raise ValueError("a page cannot exceed the declared entry_count")
        return self


ConflictChoice = Literal["keep_current", "take_proposed", "custom"]
_CHOICE_ORDER: dict[ConflictChoice, int] = {
    "keep_current": 0,
    "take_proposed": 1,
    "custom": 2,
}


class MergeConflict(_FrozenModel):
    id: NonEmptyStr
    path: JsonPointer
    kind: NonEmptyStr
    base: JsonValueState
    current: JsonValueState
    proposed: JsonValueState
    allowed_resolutions: tuple[ConflictChoice, ...] = Field(min_length=1, max_length=3)

    @field_validator("path")
    @classmethod
    def _valid_pointer(cls, value: str) -> str:
        if not _is_json_pointer(value):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return value

    @model_validator(mode="after")
    def _canonical_resolutions(self) -> "MergeConflict":
        expected = tuple(sorted(set(self.allowed_resolutions), key=_CHOICE_ORDER.__getitem__))
        if self.allowed_resolutions != expected:
            raise ValueError("allowed_resolutions must be stable-unique and canonical")
        return self


class ConflictSet(_FrozenModel):
    """Immutable conflict metadata; conflict rows are cursor-paged separately."""

    schema_version: Literal["conflict-set@1"] = "conflict-set@1"
    id: NonEmptyStr
    base_snapshot_id: NonEmptyStr
    current_snapshot_id: NonEmptyStr
    proposed_patch_artifact_id: NonEmptyStr
    expected_ref_revision: int = Field(ge=1)
    conflict_count: int = Field(ge=1)
    non_conflicting_ops_digest: Sha256Hex
    created_at: NonEmptyStr


class _KeepCurrentResolution(_FrozenModel):
    conflict_id: NonEmptyStr
    choice: Literal["keep_current"] = "keep_current"


class _TakeProposedResolution(_FrozenModel):
    conflict_id: NonEmptyStr
    choice: Literal["take_proposed"] = "take_proposed"


class _CustomResolution(_FrozenModel):
    conflict_id: NonEmptyStr
    choice: Literal["custom"] = "custom"
    custom_value: JsonValue


class ConflictResolution(
    RootModel[
        Annotated[
            _KeepCurrentResolution | _TakeProposedResolution | _CustomResolution,
            Field(discriminator="choice"),
        ]
    ]
):
    model_config = ConfigDict(frozen=True, validate_default=True)

    def __init__(self, **data: object) -> None:
        super().__init__(root=data)

    @property
    def conflict_id(self) -> str:
        return self.root.conflict_id

    @property
    def choice(self) -> ConflictChoice:
        return self.root.choice

    @property
    def custom_value(self) -> JsonValue:
        if not isinstance(self.root, _CustomResolution):
            raise AttributeError("a non-custom resolution has no custom_value")
        return self.root.custom_value


class RebaseResult(_FrozenModel):
    status: Literal["clean", "conflicted"]
    new_patch_artifact_id: NonEmptyStr | None = None
    conflict_set_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _closed_result(self) -> "RebaseResult":
        if self.status == "clean":
            if self.new_patch_artifact_id is None or self.conflict_set_id is not None:
                raise ValueError("clean rebase requires only new_patch_artifact_id")
        elif self.conflict_set_id is None or self.new_patch_artifact_id is not None:
            raise ValueError("conflicted rebase requires only conflict_set_id")
        return self
