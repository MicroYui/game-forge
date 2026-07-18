"""Transaction-bound ObjectRef to backend-location bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.lineage import ObjectBinding, ObjectLocation, ObjectRef
from gameforge.contracts.storage import MAX_PAGE_ITEMS, ObjectStat, ObjectStore
from gameforge.runtime.persistence.models import ArtifactRow, ObjectBindingRow


@dataclass(frozen=True, slots=True)
class ObjectReferenceState:
    """The three independent facts ObjectGc needs for one backend generation."""

    exact_active_binding: bool = False
    artifact_referenced: bool = False
    any_active_binding_for_ref: bool = False


def _location_key(location: ObjectLocation) -> tuple[str, str, str]:
    return location.store_id, location.key, location.backend_generation


def _require_store_id(store_id: str, *, field_name: str) -> str:
    if not isinstance(store_id, str) or not store_id:
        raise ValueError(f"{field_name} must be a non-empty string")
    return store_id


def _require_revision(revision: int, *, field_name: str) -> int:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return revision


def _binding_from_row(row: ObjectBindingRow) -> ObjectBinding:
    try:
        ref = ObjectRef(
            object_ref_schema_version=row.object_ref_schema_version,
            key=row.object_key,
            sha256=row.object_sha256,
            size_bytes=row.object_size_bytes,
        )
        location = ObjectLocation(
            location_schema_version=row.location_schema_version,
            store_id=row.store_id,
            key=row.object_key,
            backend_generation=row.backend_generation,
            etag=row.etag,
            storage_class=row.storage_class,
        )
        return ObjectBinding(
            binding_schema_version=row.binding_schema_version,
            object_ref=ref,
            location=location,
            status=row.status,
            revision=row.revision,
            verified_at=row.verified_at,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored ObjectBinding row is invalid",
            object_key=row.object_key,
            store_id=row.store_id,
        ) from exc


def _require_same_ref(stored: ObjectRef, requested: ObjectRef) -> None:
    if stored != requested:
        raise IntegrityViolation(
            "stored ObjectRef differs from the requested ObjectRef",
            object_key=requested.key,
            stored_sha256=stored.sha256,
            requested_sha256=requested.sha256,
            stored_size_bytes=stored.size_bytes,
            requested_size_bytes=requested.size_bytes,
        )


class SqlObjectBindingRepository:
    """Persist revision-CAS bindings without committing their UnitOfWork."""

    def __init__(
        self,
        session: Session,
        object_store: ObjectStore,
        default_store_id: str,
    ) -> None:
        self._session = session
        self._object_store = object_store
        self._default_store_id = _require_store_id(
            default_store_id,
            field_name="default_store_id",
        )

    def resolve(
        self,
        ref: ObjectRef,
        store_id: str | None = None,
    ) -> ObjectBinding:
        selected_store = (
            self._default_store_id
            if store_id is None
            else _require_store_id(store_id, field_name="store_id")
        )
        row = self._get_row(ref.key, selected_store)
        if row is None:
            raise FileNotFoundError(f"no ObjectBinding for {ref.key!r} in store {selected_store!r}")
        binding = _binding_from_row(row)
        _require_same_ref(binding.object_ref, ref)
        if binding.status != "active":
            raise FileNotFoundError(
                f"ObjectBinding for {ref.key!r} in store {selected_store!r} is retired"
            )
        return binding

    def resolve_many(
        self,
        refs: Sequence[ObjectRef],
        store_id: str | None = None,
    ) -> dict[str, ObjectBinding | None]:
        """Resolve an exact ObjectRef set without one query per replay dependency."""

        selected_store = (
            self._default_store_id
            if store_id is None
            else _require_store_id(store_id, field_name="store_id")
        )
        by_key: dict[str, ObjectRef] = {}
        for ref in refs:
            retained = by_key.setdefault(ref.key, ref)
            if retained != ref:
                raise IntegrityViolation(
                    "one object key is bound to conflicting ObjectRefs",
                    object_key=ref.key,
                )
        result: dict[str, ObjectBinding | None] = dict.fromkeys(by_key)
        keys = tuple(by_key)
        max_keys = min(MAX_PAGE_ITEMS, 900)
        for offset in range(0, len(keys), max_keys):
            rows = self._session.scalars(
                select(ObjectBindingRow).where(
                    ObjectBindingRow.store_id == selected_store,
                    ObjectBindingRow.object_key.in_(keys[offset : offset + max_keys]),
                )
            ).all()
            for row in rows:
                binding = _binding_from_row(row)
                _require_same_ref(binding.object_ref, by_key[row.object_key])
                if binding.status == "active":
                    result[row.object_key] = binding
        return result

    def bind_verified(
        self,
        ref: ObjectRef,
        location: ObjectLocation,
        expected_revision: int | None,
    ) -> ObjectBinding:
        if expected_revision is not None:
            _require_revision(expected_revision, field_name="expected_revision")
        stat = self._stat_exact(ref, location)
        row = self._get_row(ref.key, location.store_id)
        if row is None:
            if expected_revision is not None:
                raise Conflict(
                    "ObjectBinding create expected an existing revision",
                    object_key=ref.key,
                    store_id=location.store_id,
                    expected_revision=expected_revision,
                    actual_revision=None,
                )
            binding = ObjectBinding(
                object_ref=ref,
                location=location,
                status="active",
                revision=1,
                verified_at=stat.verified_at,
            )
            self._session.add(self._row_for(binding))
            self._session.flush()
            return binding

        current = _binding_from_row(row)
        _require_same_ref(current.object_ref, ref)
        if current.status == "active" and current.location == location:
            if expected_revision is not None and expected_revision != current.revision:
                self._raise_conflict(current, expected_revision)
            return current

        if expected_revision is None or expected_revision != current.revision:
            self._raise_conflict(current, expected_revision)

        next_binding = ObjectBinding(
            object_ref=ref,
            location=location,
            status="active",
            revision=current.revision + 1,
            verified_at=stat.verified_at,
        )
        result = self._session.execute(
            update(ObjectBindingRow)
            .where(
                ObjectBindingRow.object_key == ref.key,
                ObjectBindingRow.store_id == location.store_id,
                ObjectBindingRow.revision == expected_revision,
            )
            .values(
                binding_schema_version=next_binding.binding_schema_version,
                object_ref_schema_version=ref.object_ref_schema_version,
                location_schema_version=location.location_schema_version,
                object_sha256=ref.sha256,
                object_size_bytes=ref.size_bytes,
                backend_generation=location.backend_generation,
                etag=location.etag,
                storage_class=location.storage_class,
                status="active",
                revision=next_binding.revision,
                verified_at=next_binding.verified_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._raise_conflict(current, expected_revision)
        self._session.flush()
        self._session.expire(row)
        return next_binding

    def retire(
        self,
        binding: ObjectBinding,
        expected_revision: int,
    ) -> ObjectBinding:
        _require_revision(expected_revision, field_name="expected_revision")
        row = self._get_row(binding.object_ref.key, binding.location.store_id)
        if row is None:
            raise Conflict(
                "ObjectBinding retire target does not exist",
                object_key=binding.object_ref.key,
                store_id=binding.location.store_id,
                expected_revision=expected_revision,
                actual_revision=None,
            )
        current = _binding_from_row(row)
        _require_same_ref(current.object_ref, binding.object_ref)
        if expected_revision != current.revision or binding != current:
            self._raise_conflict(current, expected_revision)
        if current.status != "active":
            self._raise_conflict(current, expected_revision)
        self._require_retire_preserves_referenced_ref(current)

        retired = current.model_copy(update={"status": "retired", "revision": current.revision + 1})
        result = self._session.execute(
            update(ObjectBindingRow)
            .where(
                ObjectBindingRow.object_key == current.object_ref.key,
                ObjectBindingRow.store_id == current.location.store_id,
                ObjectBindingRow.revision == expected_revision,
                ObjectBindingRow.status == "active",
                ObjectBindingRow.backend_generation == current.location.backend_generation,
            )
            .values(status="retired", revision=retired.revision)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._raise_conflict(current, expected_revision)
        self._session.flush()
        self._session.expire(row)
        return retired

    def _require_retire_preserves_referenced_ref(self, current: ObjectBinding) -> None:
        binding_rows = self._session.scalars(
            select(ObjectBindingRow)
            .where(ObjectBindingRow.object_key == current.object_ref.key)
            .execution_options(populate_existing=True)
        ).all()
        has_other_active_binding = False
        for row in binding_rows:
            peer = _binding_from_row(row)
            _require_same_ref(peer.object_ref, current.object_ref)
            if peer.status == "active" and peer.location.store_id != current.location.store_id:
                has_other_active_binding = True

        artifact_rows = self._session.execute(
            select(ArtifactRow.artifact_id, ArtifactRow.object_ref).where(
                ArtifactRow.lineage_schema_version == "lineage@2",
                ArtifactRow.object_ref.is_not(None),
                ArtifactRow.object_ref["key"].as_string() == current.object_ref.key,
            )
        ).all()
        artifact_referenced = False
        for artifact_id, raw_ref in artifact_rows:
            try:
                artifact_ref = ObjectRef.model_validate(raw_ref)
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation(
                    "stored ArtifactV2 ObjectRef is invalid",
                    artifact_id=artifact_id,
                ) from exc
            _require_same_ref(artifact_ref, current.object_ref)
            artifact_referenced = True

        if artifact_referenced and not has_other_active_binding:
            raise Conflict(
                "cannot retire the last active ObjectBinding referenced by an ArtifactV2",
                object_key=current.object_ref.key,
                store_id=current.location.store_id,
                expected_revision=current.revision,
            )

    def has_active_binding(self, ref: ObjectRef) -> bool:
        rows = self._session.scalars(
            select(ObjectBindingRow)
            .where(ObjectBindingRow.object_key == ref.key)
            .execution_options(populate_existing=True)
        ).all()
        found = False
        for row in rows:
            binding = _binding_from_row(row)
            _require_same_ref(binding.object_ref, ref)
            found = found or binding.status == "active"
        return found

    def reference_states(
        self,
        stats: Sequence[ObjectStat],
    ) -> dict[tuple[str, str, str], ObjectReferenceState]:
        if len(stats) > MAX_PAGE_ITEMS:
            raise ValueError(f"reference-state query must be bounded to {MAX_PAGE_ITEMS} items")
        if not stats:
            return {}

        by_location: dict[tuple[str, str, str], ObjectStat] = {}
        refs_by_key: dict[str, ObjectRef] = {}
        for stat in stats:
            if not isinstance(stat, ObjectStat):
                raise TypeError("reference-state query requires ObjectStat items")
            location_key = _location_key(stat.location)
            duplicate = by_location.get(location_key)
            if duplicate is not None and duplicate != stat:
                raise IntegrityViolation(
                    "duplicate object generation has conflicting ObjectStat values",
                    store_id=location_key[0],
                    object_key=location_key[1],
                    backend_generation=location_key[2],
                )
            by_location[location_key] = stat
            previous_ref = refs_by_key.get(stat.ref.key)
            if previous_ref is not None and previous_ref != stat.ref:
                raise IntegrityViolation(
                    "one content-addressed key has conflicting ObjectRefs",
                    object_key=stat.ref.key,
                )
            refs_by_key[stat.ref.key] = stat.ref

        object_keys = tuple(sorted(refs_by_key))
        binding_rows = self._session.scalars(
            select(ObjectBindingRow)
            .where(ObjectBindingRow.object_key.in_(object_keys))
            .execution_options(populate_existing=True)
        ).all()
        bindings: dict[tuple[str, str], ObjectBinding] = {}
        active_refs: set[ObjectRef] = set()
        for row in binding_rows:
            binding = _binding_from_row(row)
            _require_same_ref(binding.object_ref, refs_by_key[binding.object_ref.key])
            bindings[(binding.location.store_id, binding.object_ref.key)] = binding
            if binding.status == "active":
                active_refs.add(binding.object_ref)

        referenced_refs: set[ObjectRef] = set()
        artifact_rows = self._session.execute(
            select(ArtifactRow.artifact_id, ArtifactRow.object_ref).where(
                ArtifactRow.lineage_schema_version == "lineage@2",
                ArtifactRow.object_ref.is_not(None),
                ArtifactRow.object_ref["key"].as_string().in_(object_keys),
            )
        ).all()
        for artifact_id, raw_ref in artifact_rows:
            try:
                artifact_ref = ObjectRef.model_validate(raw_ref)
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation(
                    "stored ArtifactV2 ObjectRef is invalid",
                    artifact_id=artifact_id,
                ) from exc
            _require_same_ref(artifact_ref, refs_by_key[artifact_ref.key])
            referenced_refs.add(artifact_ref)

        states: dict[tuple[str, str, str], ObjectReferenceState] = {}
        for location_key, stat in by_location.items():
            binding = bindings.get((stat.location.store_id, stat.ref.key))
            exact_active = False
            if binding is not None and binding.status == "active":
                if binding.location.backend_generation == stat.location.backend_generation:
                    if binding.location != stat.location:
                        raise IntegrityViolation(
                            "active ObjectBinding location differs from ObjectStore stat",
                            store_id=stat.location.store_id,
                            object_key=stat.location.key,
                            backend_generation=stat.location.backend_generation,
                        )
                    exact_active = True
            states[location_key] = ObjectReferenceState(
                exact_active_binding=exact_active,
                artifact_referenced=stat.ref in referenced_refs,
                any_active_binding_for_ref=stat.ref in active_refs,
            )
        return states

    def _get_row(self, object_key: str, store_id: str) -> ObjectBindingRow | None:
        return self._session.scalar(
            select(ObjectBindingRow)
            .where(
                ObjectBindingRow.object_key == object_key,
                ObjectBindingRow.store_id == store_id,
            )
            .execution_options(populate_existing=True)
        )

    def _stat_exact(self, ref: ObjectRef, location: ObjectLocation) -> ObjectStat:
        try:
            stat = self._object_store.stat(location)
        except FileNotFoundError as exc:
            raise IntegrityViolation(
                "ObjectStore location is not available for binding",
                object_key=ref.key,
                store_id=location.store_id,
                backend_generation=location.backend_generation,
            ) from exc
        if not isinstance(stat, ObjectStat):
            raise IntegrityViolation(
                "ObjectStore stat returned an invalid record",
                object_key=ref.key,
                store_id=location.store_id,
            )
        if stat.ref != ref or stat.location != location:
            raise IntegrityViolation(
                "ObjectStore stat does not exactly match the requested binding",
                object_key=ref.key,
                store_id=location.store_id,
                backend_generation=location.backend_generation,
            )
        return stat

    @staticmethod
    def _row_for(binding: ObjectBinding) -> ObjectBindingRow:
        return ObjectBindingRow(
            object_key=binding.object_ref.key,
            store_id=binding.location.store_id,
            binding_schema_version=binding.binding_schema_version,
            object_ref_schema_version=binding.object_ref.object_ref_schema_version,
            location_schema_version=binding.location.location_schema_version,
            object_sha256=binding.object_ref.sha256,
            object_size_bytes=binding.object_ref.size_bytes,
            backend_generation=binding.location.backend_generation,
            etag=binding.location.etag,
            storage_class=binding.location.storage_class,
            status=binding.status,
            revision=binding.revision,
            verified_at=binding.verified_at,
        )

    @staticmethod
    def _raise_conflict(
        current: ObjectBinding,
        expected_revision: int | None,
    ) -> None:
        raise Conflict(
            "ObjectBinding revision or state changed",
            object_key=current.object_ref.key,
            store_id=current.location.store_id,
            expected_revision=expected_revision,
            actual_revision=current.revision,
            actual_status=current.status,
            actual_backend_generation=current.location.backend_generation,
        )


__all__ = [
    "ObjectReferenceState",
    "SqlObjectBindingRepository",
]
