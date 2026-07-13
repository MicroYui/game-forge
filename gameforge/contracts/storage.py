"""M4 storage capability Protocols and bounded wire records."""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from datetime import datetime
from typing import (
    Any,
    BinaryIO,
    Generic,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator
from typing_extensions import Annotated

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.lineage import (
    AuditActor,
    AuditRecordV2,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
MAX_PAGE_ITEMS = 1000


class _ImmutableJsonDict(dict[str, Any]):
    @staticmethod
    def _reject_mutation(*args: Any, **kwargs: Any) -> None:
        raise TypeError("canonical_view is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation

    def __copy__(self) -> "_ImmutableJsonDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ImmutableJsonDict":
        memo[id(self)] = self
        return self


class _ImmutableJsonList(list[Any]):
    @staticmethod
    def _reject_mutation(*args: Any, **kwargs: Any) -> None:
        raise TypeError("canonical_view is immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation

    def __copy__(self) -> "_ImmutableJsonList":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ImmutableJsonList":
        memo[id(self)] = self
        return self


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _ImmutableJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _ImmutableJsonList(_freeze_json(item) for item in value)
    return value


def compute_page_query_hash(
    *,
    api_version: str,
    resource_kind: str,
    filters: Mapping[str, Any],
    stable_sort: Sequence[str],
    page_projection: Sequence[str],
) -> str:
    """Bind a cursor to the exact canonical query shape."""

    return canonical_sha256(
        {
            "api_version": api_version,
            "resource_kind": resource_kind,
            "filters": filters,
            "stable_sort": list(stable_sort),
            "page_projection": list(page_projection),
        }
    )


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RefValue(FrozenModel):
    artifact_id: NonEmptyStr
    revision: PositiveInt


class RefCasRequestV1(FrozenModel):
    """Required ``expected`` preserves absent-ref CAS distinct from omission."""

    request_schema_version: Literal["ref-cas@1"] = "ref-cas@1"
    name: NonEmptyStr
    expected: RefValue | None
    new_artifact_id: NonEmptyStr


class ReadSnapshotV1(FrozenModel):
    snapshot_schema_version: Literal["read-snapshot@1"] = "read-snapshot@1"
    snapshot_id: NonEmptyStr
    resource_kind: NonEmptyStr
    query_hash: Sha256Hex
    authz_fingerprint: Sha256Hex
    stable_sort_schema_id: NonEmptyStr
    strategy: Literal["immutable_high_watermark", "materialized_view"]
    high_watermark: NonNegativeInt | None = None
    materialized_item_count: NonNegativeInt | None = None
    created_at: NonEmptyStr
    expires_at: NonEmptyStr

    @model_validator(mode="after")
    def _strategy_fields(self) -> "ReadSnapshotV1":
        if self.strategy == "immutable_high_watermark":
            if self.high_watermark is None or self.materialized_item_count is not None:
                raise ValueError("immutable snapshot requires only high_watermark")
        elif self.materialized_item_count is None or self.high_watermark is not None:
            raise ValueError("materialized snapshot requires only materialized_item_count")
        return self


class MaterializedReadItemV1(FrozenModel):
    snapshot_id: NonEmptyStr
    ordinal: PositiveInt
    resource_id: NonEmptyStr
    observed_revision: PositiveInt
    view_schema_id: NonEmptyStr
    canonical_view: dict[str, JsonValue]
    view_hash: Sha256Hex

    @model_validator(mode="after")
    def _canonical_view_hash(self) -> "MaterializedReadItemV1":
        if self.view_hash != canonical_sha256(self.canonical_view):
            raise ValueError("view_hash does not match canonical_view")
        object.__setattr__(self, "canonical_view", _freeze_json(self.canonical_view))
        return self


class PageCursorV1(FrozenModel):
    cursor_schema_version: Literal["page-cursor@1"] = "page-cursor@1"
    snapshot_id: NonEmptyStr
    position: NonEmptyStr
    page_size: Annotated[int, Field(gt=0, le=MAX_PAGE_ITEMS)]
    query_hash: Sha256Hex
    opaque_signature: NonEmptyStr


T = TypeVar("T")


class PageV1(FrozenModel, Generic[T]):
    page_schema_version: Literal["page@1"] = "page@1"
    read_snapshot_id: NonEmptyStr
    items: tuple[T, ...]
    next_cursor: PageCursorV1 | None = None
    expires_at: NonEmptyStr

    @model_validator(mode="after")
    def _bounded_page(self) -> "PageV1[T]":
        if len(self.items) > MAX_PAGE_ITEMS:
            raise ValueError(f"page contains more than {MAX_PAGE_ITEMS} items")
        if self.next_cursor is not None:
            if self.next_cursor.snapshot_id != self.read_snapshot_id:
                raise ValueError("page and cursor read snapshot differ")
            if len(self.items) > self.next_cursor.page_size:
                raise ValueError("page contains more items than cursor page_size")
        return self


Page = PageV1


class StoredObject(FrozenModel):
    ref: ObjectRef
    location: ObjectLocation

    @model_validator(mode="after")
    def _same_key(self) -> "StoredObject":
        if self.ref.key != self.location.key:
            raise ValueError("ObjectRef and ObjectLocation keys differ")
        return self


class ObjectStat(FrozenModel):
    ref: ObjectRef
    location: ObjectLocation
    verified_at: NonEmptyStr
    retention_until: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _same_key(self) -> "ObjectStat":
        if self.ref.key != self.location.key:
            raise ValueError("ObjectRef and ObjectLocation keys differ")
        return self


class GcCandidate(FrozenModel):
    candidate_schema_version: Literal["object-gc-candidate@1"] = "object-gc-candidate@1"
    location: ObjectLocation
    object_ref: ObjectRef | None = None
    observed_at: NonEmptyStr

    @model_validator(mode="after")
    def _same_key(self) -> "GcCandidate":
        if self.object_ref is not None and self.object_ref.key != self.location.key:
            raise ValueError("GC candidate keys differ")
        return self


GcCollectionResult = Literal[
    "deleted",
    "retained_referenced",
    "retained_generation_changed",
    "retention_active",
]


class RefTransitionV1(FrozenModel):
    transition_schema_version: Literal["ref-transition@1"] = "ref-transition@1"
    transition_id: NonEmptyStr
    kind: Literal["rollback"] = "rollback"
    ref_name: NonEmptyStr
    from_ref: RefValue
    to_ref: RefValue
    approval_item_id: NonEmptyStr
    actor: AuditActor
    initiated_by: AuditActor | None = None
    request_id: NonEmptyStr
    occurred_at: NonEmptyStr

    @staticmethod
    def _id_for(payload: dict[str, Any]) -> str:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return f"ref-transition:sha256:{digest}"

    @classmethod
    def create(cls, **values: Any) -> "RefTransitionV1":
        payload = cls._canonical_payload(values)
        return cls(transition_id=cls._id_for(payload), **values)

    @classmethod
    def _canonical_payload(cls, values: dict[str, Any]) -> dict[str, Any]:
        raw = {
            "transition_schema_version": values.get(
                "transition_schema_version", "ref-transition@1"
            ),
            "kind": values.get("kind", "rollback"),
            "ref_name": values["ref_name"],
            "from_ref": values["from_ref"],
            "to_ref": values["to_ref"],
            "approval_item_id": values["approval_item_id"],
            "actor": values["actor"],
            "initiated_by": values.get("initiated_by"),
            "request_id": values["request_id"],
            "occurred_at": values["occurred_at"],
        }
        return {
            key: value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            for key, value in raw.items()
        }

    @model_validator(mode="after")
    def _verify_transition_id(self) -> "RefTransitionV1":
        payload = self.model_dump(mode="json", exclude={"transition_id"})
        if self.transition_id != self._id_for(payload):
            raise ValueError("transition_id does not match canonical payload")
        return self


@runtime_checkable
class UtcClock(Protocol):
    def now_utc(self) -> datetime: ...


@runtime_checkable
class MonotonicClock(Protocol):
    def now_ns(self) -> int: ...


@runtime_checkable
class Repository(Protocol[T]):
    def get(self, identifier: str) -> T | None: ...

    def put(self, item: T) -> T: ...

    def page(self, cursor: PageCursorV1 | None = None) -> PageV1[T]: ...


@runtime_checkable
class RefStore(Protocol):
    def get(self, name: str) -> RefValue | None: ...

    def history(self, name: str, cursor: PageCursorV1 | None = None) -> PageV1[RefValue]: ...

    def compare_and_set(
        self, name: str, expected: RefValue | None, new_artifact_id: str
    ) -> RefValue: ...


@runtime_checkable
class AuditSink(Protocol):
    def append(self, record: AuditRecordV2) -> AuditRecordV2: ...


@runtime_checkable
class ObjectBindingRepository(Protocol):
    def resolve(self, ref: ObjectRef, store_id: str | None = None) -> ObjectBinding: ...

    def bind_verified(
        self,
        ref: ObjectRef,
        location: ObjectLocation,
        expected_revision: int | None,
    ) -> ObjectBinding: ...

    def retire(self, binding: ObjectBinding, expected_revision: int) -> ObjectBinding: ...


@runtime_checkable
class ObjectStore(Protocol):
    def put_verified(self, source: bytes | BinaryIO) -> StoredObject: ...

    def open(self, location: ObjectLocation) -> BinaryIO: ...

    def stat(self, location: ObjectLocation) -> ObjectStat: ...

    def list_versions(self, cursor: PageCursorV1 | None = None) -> PageV1[ObjectStat]: ...

    def delete_if_generation(self, location: ObjectLocation) -> bool: ...


@runtime_checkable
class ObjectGc(Protocol):
    def plan(self, cursor: PageCursorV1 | None, safe_before: str) -> PageV1[GcCandidate]: ...

    def collect(self, candidate: GcCandidate) -> GcCollectionResult: ...


@runtime_checkable
class Transaction(Protocol):
    refs: RefStore
    audit: AuditSink
    approvals: Any
    lineage: Repository[Any]
    object_bindings: ObjectBindingRepository
    runs: Any
    cost: Any


@runtime_checkable
class UnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Transaction]: ...


@runtime_checkable
class StorageFacade(Protocol):
    unit_of_work: UnitOfWork
    objects: ObjectStore
    object_gc: ObjectGc
