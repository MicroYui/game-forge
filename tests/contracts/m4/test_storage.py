from __future__ import annotations

from typing import BinaryIO, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from gameforge.contracts.lineage import AuditActor, ObjectLocation, ObjectRef
from gameforge.contracts.storage import (
    GcCandidate,
    MAX_PAGE_ITEMS,
    ObjectGc,
    ObjectStat,
    ObjectStore,
    PageCursorV1,
    PageV1,
    RefCasRequestV1,
    RefStore,
    RefTransitionV1,
    RefValue,
    Repository,
    StoredObject,
    UnitOfWork,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _ref() -> ObjectRef:
    return ObjectRef(
        object_ref_schema_version="object-ref@1",
        key=f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}",
        sha256=_HASH_A,
        size_bytes=3,
    )


def _location(generation: str = "g1") -> ObjectLocation:
    return ObjectLocation(
        location_schema_version="object-location@1",
        store_id="local-primary",
        key=f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}",
        backend_generation=generation,
    )


def test_ref_cas_requires_explicit_expected_and_preserves_null_vs_missing() -> None:
    request = RefCasRequestV1(name="refs/main", expected=None, new_artifact_id="artifact:new")
    assert request.model_dump()["expected"] is None

    with pytest.raises(ValidationError):
        RefCasRequestV1.model_validate({"name": "refs/main", "new_artifact_id": "artifact:new"})


def test_ref_value_revision_is_positive_and_frozen() -> None:
    value = RefValue(artifact_id="artifact:a", revision=1)
    with pytest.raises(ValidationError):
        RefValue(artifact_id="artifact:a", revision=0)
    with pytest.raises(ValidationError):
        value.revision = 2


def test_page_cursor_and_page_are_bounded_and_query_bound() -> None:
    cursor = PageCursorV1(
        cursor_schema_version="page-cursor@1",
        snapshot_id="read:1",
        position="artifact:a",
        page_size=2,
        query_hash=_HASH_A,
        opaque_signature=_HASH_B,
    )
    page = PageV1[str](
        page_schema_version="page@1",
        read_snapshot_id="read:1",
        items=("a", "b"),
        next_cursor=cursor,
        expires_at="2026-07-13T12:00:00Z",
    )
    assert page.items == ("a", "b")
    assert PageV1[str].model_validate(page.model_dump(mode="json")) == page

    with pytest.raises(ValidationError):
        PageV1[str](
            page_schema_version="page@1",
            read_snapshot_id="read:1",
            items=("a", "b", "c"),
            next_cursor=cursor,
            expires_at="2026-07-13T12:00:00Z",
        )

    with pytest.raises(ValidationError):
        PageV1[int](
            read_snapshot_id="read:1",
            items=tuple(range(MAX_PAGE_ITEMS + 1)),
            expires_at="2026-07-13T12:00:00Z",
        )

    with pytest.raises(ValidationError):
        PageCursorV1(
            snapshot_id="read:1",
            position="artifact:a",
            page_size=MAX_PAGE_ITEMS + 1,
            query_hash=_HASH_A,
            opaque_signature=_HASH_B,
        )


def test_ref_transition_id_is_content_derived_and_exact() -> None:
    actor = AuditActor(principal_id="human:b", principal_kind="human")
    transition = RefTransitionV1.create(
        from_ref=RefValue(artifact_id="artifact:b", revision=2),
        to_ref=RefValue(artifact_id="artifact:a", revision=3),
        ref_name="refs/main",
        approval_item_id="approval:1",
        actor=actor,
        request_id="request:1",
        occurred_at="2026-07-13T12:00:00Z",
    )
    assert transition.transition_id.startswith("ref-transition:sha256:")
    assert RefTransitionV1.model_validate(transition.model_dump()) == transition
    with pytest.raises(ValidationError):
        RefTransitionV1.model_validate(
            {**transition.model_dump(), "transition_id": "ref-transition:sha256:" + _HASH_B}
        )


def test_object_wire_records_bind_concrete_generation() -> None:
    stored = StoredObject(ref=_ref(), location=_location())
    stat = ObjectStat(
        ref=stored.ref,
        location=stored.location,
        verified_at="2026-07-13T12:00:00Z",
    )
    candidate = GcCandidate(
        location=stored.location,
        object_ref=stored.ref,
        observed_at="2026-07-13T12:05:00Z",
    )
    assert stat.location.backend_generation == candidate.location.backend_generation


def test_storage_protocols_freeze_required_capability_methods() -> None:
    assert {"get", "put", "page"} <= set(Repository.__dict__)
    assert {"get", "history", "compare_and_set"} <= set(RefStore.__dict__)
    assert {"put_verified", "open", "stat", "list_versions", "delete_if_generation"} <= set(
        ObjectStore.__dict__
    )
    assert {"plan", "collect"} <= set(ObjectGc.__dict__)
    assert "begin" in UnitOfWork.__dict__
    source_type = get_type_hints(ObjectStore.put_verified)["source"]
    assert set(get_args(source_type)) == {bytes, BinaryIO}
