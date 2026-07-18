"""Immutable transaction-bound SQL Artifact repository."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta, timezone
from threading import Lock
from typing import Any, Sequence
from weakref import WeakKeyDictionary, WeakSet

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import BigInteger, func, literal_column, select
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.lineage import (
    ArtifactV1,
    ArtifactV2,
    ObjectRef,
    VersionTuple,
    parse_artifact,
)
from gameforge.contracts.storage import (
    MAX_PAGE_ITEMS,
    PageCursorV1,
    PageV1,
    ReadSnapshotV1,
    UtcClock,
    compute_page_query_hash,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    ObjectBindingRow,
    ReadSnapshotRow,
)
from gameforge.runtime.persistence.object_bindings import (
    SqlObjectBindingRepository,
    _PreflightedTerminalObjectBindings,
    _binding_from_row,
    _current_transaction_identity,
)


ArtifactWire = ArtifactV1 | ArtifactV2
_MAX_SQL_IN_ITEMS = 900
_ARTIFACT_QUERY_HASH = compute_page_query_hash(
    api_version="storage@1",
    resource_kind="artifacts",
    filters={},
    stable_sort=("artifact_id:asc",),
    page_projection=(
        "lineage_schema_version",
        "kind",
        "version_tuple",
        "lineage",
        "payload_hash",
        "object_ref",
        "created_at",
        "meta",
    ),
)
_ARTIFACT_AUTHZ_FINGERPRINT = canonical_sha256(
    {"scope": "artifact-repository-internal", "resource_kind": "artifacts"}
)
_STABLE_SORT_SCHEMA_ID = "artifact-id-asc@1"
# Artifact rows are append-only: DELETE/VACUUM must not run while a read snapshot is retained.
_ARTIFACT_ROWID = literal_column("artifacts.rowid", type_=BigInteger())


class _FrozenJsonDict(dict[str, object]):
    """JSON-serializer-compatible mapping with every ordinary mutator closed."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("sealed Artifact row projection is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenJsonList(list[object]):
    """JSON-serializer-compatible sequence with every ordinary mutator closed."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("sealed Artifact row projection is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    __ior__ = _immutable


class _FrozenVersionTuple(VersionTuple):
    """VersionTuple retaining wire equality while closing field reassignment."""

    model_config = ConfigDict(frozen=True)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VersionTuple) and all(
            getattr(self, field_name) == getattr(other, field_name)
            for field_name in VersionTuple.model_fields
        )

    __hash__ = None


class _FrozenArtifactV1(ArtifactV1):
    """Legacy return value whose complete preflight result cannot be rewritten."""

    model_config = ConfigDict(frozen=True)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ArtifactV1) and (
            self.artifact_id,
            self.lineage_schema_version,
            self.kind,
            self.version_tuple,
            self.lineage,
            self.payload_hash,
            self.created_at,
            self.meta,
        ) == (
            other.artifact_id,
            other.lineage_schema_version,
            other.kind,
            other.version_tuple,
            other.lineage,
            other.payload_hash,
            other.created_at,
            other.meta,
        )

    __hash__ = None


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenJsonDict({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_result_value(value: object) -> object:
    """Recursively detach and freeze a validated Artifact result value."""

    if isinstance(value, VersionTuple):
        return _FrozenVersionTuple.model_validate(value.model_dump(mode="json"))
    if isinstance(value, BaseModel):
        if not type(value).model_config.get("frozen", False):
            return _freeze_json(value.model_dump(mode="json"))
        cloned = value.model_copy(deep=True)
        for field_name in type(cloned).model_fields:
            object.__setattr__(
                cloned,
                field_name,
                _freeze_result_value(getattr(cloned, field_name)),
            )
        return cloned
    if isinstance(value, Mapping):
        return _FrozenJsonDict(
            {str(key): _freeze_result_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenJsonList(_freeze_result_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_result_value(item) for item in value)
    return value


def _freeze_artifact_result(item: ArtifactWire) -> ArtifactWire:
    """Create an equality-compatible deep-frozen result detached from caller input."""

    if isinstance(item, ArtifactV1) and not isinstance(item, ArtifactV2):
        cloned: ArtifactWire = _FrozenArtifactV1.model_validate(item.model_dump(mode="json"))
    else:
        cloned = item.model_copy(deep=True)
    for field_name in type(cloned).model_fields:
        object.__setattr__(
            cloned,
            field_name,
            _freeze_result_value(getattr(cloned, field_name)),
        )
    return cloned


@dataclass(frozen=True, slots=True)
class _ArtifactRowProjection:
    """Exact precomputed SQL row values; no ORM instance crosses the seal boundary."""

    artifact_id: str
    lineage_schema_version: str
    kind: str
    version_tuple: _FrozenJsonDict
    lineage: _FrozenJsonList
    payload_hash: str | None
    created_at: str | None
    meta: _FrozenJsonDict
    object_ref: _FrozenJsonDict | None

    def build_row(self) -> ArtifactRow:
        return ArtifactRow(
            artifact_id=self.artifact_id,
            lineage_schema_version=self.lineage_schema_version,
            kind=self.kind,
            version_tuple=self.version_tuple,
            lineage=self.lineage,
            payload_hash=self.payload_hash,
            created_at=self.created_at,
            meta=self.meta,
            object_ref=self.object_ref,
        )


@dataclass(frozen=True, slots=True)
class _ArtifactPreflightState:
    """Complete immutable projection retained outside its opaque handle."""

    owner: SqlArtifactRepository
    transaction_identity: tuple[object, object]
    rows: tuple[_ArtifactRowProjection, ...]
    stored: tuple[ArtifactWire, ...]
    binding_preflight: _PreflightedTerminalObjectBindings | None


_ARTIFACT_SEAL_STATES_LOCK = Lock()
_ARTIFACT_SEAL_STATES: WeakKeyDictionary[object, _ArtifactPreflightState] = WeakKeyDictionary()
_CONSUMED_ARTIFACT_SEALS: WeakSet[object] = WeakSet()


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _PreflightedArtifactWrites:
    """Opaque one-shot Artifact DML handle with no instance authority fields."""


def _issue_artifact_preflight(state: _ArtifactPreflightState) -> _PreflightedArtifactWrites:
    handle = _PreflightedArtifactWrites()
    with _ARTIFACT_SEAL_STATES_LOCK:
        _ARTIFACT_SEAL_STATES[handle] = state
    return handle


def _consume_artifact_preflight(
    handle: _PreflightedArtifactWrites,
    owner: SqlArtifactRepository,
) -> tuple[tuple[_ArtifactRowProjection, ...], tuple[ArtifactWire, ...]]:
    """Atomically validate and consume only externally registered authority."""

    with _ARTIFACT_SEAL_STATES_LOCK:
        state = _ARTIFACT_SEAL_STATES.get(handle)
        if state is None:
            raise IntegrityViolation("Artifact batch lacks a trusted preflight seal")
        if handle in _CONSUMED_ARTIFACT_SEALS:
            raise IntegrityViolation("Artifact preflight seal was already consumed")
        if state.owner is not owner:
            raise IntegrityViolation("Artifact preflight seal belongs to another repository")
        current_identity = _current_transaction_identity(owner._session)
        if any(
            retained is not current
            for retained, current in zip(
                state.transaction_identity,
                current_identity,
                strict=True,
            )
        ):
            raise IntegrityViolation("Artifact preflight seal belongs to another transaction")
        if state.binding_preflight is not None:
            bindings = owner._binding_repository
            if not isinstance(bindings, SqlObjectBindingRepository):
                raise IntegrityViolation(
                    "Artifact preflight lost its transaction-bound ObjectBinding repository"
                )
            state.binding_preflight.require_applied(bindings)
        _CONSUMED_ARTIFACT_SEALS.add(handle)
        return state.rows, state.stored


def _row_wire(row: ArtifactRow) -> dict[str, Any]:
    base = {
        "artifact_id": row.artifact_id,
        "lineage_schema_version": row.lineage_schema_version,
        "kind": row.kind,
        "version_tuple": row.version_tuple,
        "lineage": row.lineage,
        "payload_hash": row.payload_hash,
        "created_at": row.created_at,
        "meta": row.meta,
    }
    if row.lineage_schema_version == "lineage@1":
        if row.object_ref is not None:
            raise IntegrityViolation(
                "stored lineage@1 artifact must not contain an ObjectRef",
                artifact_id=row.artifact_id,
            )
        # Historical M0b rows allowed null in this JSON column and were read as {}.
        if base["meta"] is None:
            base["meta"] = {}
    else:
        base["object_ref"] = row.object_ref
    return base


def _parse_stored_wire(value: Any, *, artifact_id: str, source: str) -> ArtifactWire:
    try:
        parsed = parse_artifact(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            f"{source} contains an invalid stored artifact",
            artifact_id=artifact_id,
        ) from exc
    if parsed.artifact_id != artifact_id:
        raise IntegrityViolation(
            f"{source} artifact identity differs from its storage key",
            artifact_id=artifact_id,
            parsed_artifact_id=parsed.artifact_id,
        )
    if isinstance(parsed, ArtifactV2) and canonical_json(
        parsed.model_dump(mode="json")
    ) != canonical_json(value):
        raise IntegrityViolation(
            f"{source} ArtifactV2 wire is not canonical",
            artifact_id=artifact_id,
        )
    return parsed


def _revalidate_for_put(item: ArtifactWire) -> ArtifactWire:
    if not isinstance(item, (ArtifactV1, ArtifactV2)):
        raise IntegrityViolation("artifact put requires an ArtifactV1 or ArtifactV2")
    if isinstance(item, ArtifactV1):
        unexpected_fields = set(item.__dict__) - set(type(item).model_fields)
        if unexpected_fields:
            raise IntegrityViolation(
                "invalid artifact wire contains fields outside lineage@1",
                artifact_id=item.artifact_id,
                fields=sorted(unexpected_fields),
            )
    wire = item.model_dump(mode="json")
    try:
        parsed = parse_artifact(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "invalid artifact wire or content identity",
            artifact_id=getattr(item, "artifact_id", None),
        ) from exc
    if canonical_json(parsed.model_dump(mode="json")) != canonical_json(wire):
        raise IntegrityViolation(
            "invalid artifact wire is not canonical",
            artifact_id=item.artifact_id,
        )
    return parsed


def _immutable_identity(item: ArtifactWire) -> str:
    return canonical_json(item.model_dump(mode="json", exclude={"created_at"}))


def _artifact_row(item: ArtifactWire) -> ArtifactRow:
    wire = item.model_dump(mode="json")
    return ArtifactRow(
        artifact_id=wire["artifact_id"],
        lineage_schema_version=wire["lineage_schema_version"],
        kind=wire["kind"],
        version_tuple=wire["version_tuple"],
        lineage=wire["lineage"],
        payload_hash=wire["payload_hash"],
        created_at=wire["created_at"],
        meta=wire["meta"],
        object_ref=wire.get("object_ref"),
    )


def _artifact_row_projection(item: ArtifactWire) -> _ArtifactRowProjection:
    wire = item.model_dump(mode="json")
    version_tuple = _freeze_json(wire["version_tuple"])
    lineage = _freeze_json(wire["lineage"])
    meta = _freeze_json(wire["meta"])
    object_ref = _freeze_json(wire.get("object_ref"))
    if not isinstance(version_tuple, _FrozenJsonDict):  # pragma: no cover - contract invariant
        raise IntegrityViolation("Artifact VersionTuple projection is not an object")
    if not isinstance(lineage, _FrozenJsonList):  # pragma: no cover - contract invariant
        raise IntegrityViolation("Artifact lineage projection is not an array")
    if not isinstance(meta, _FrozenJsonDict):  # pragma: no cover - contract invariant
        raise IntegrityViolation("Artifact metadata projection is not an object")
    if object_ref is not None and not isinstance(object_ref, _FrozenJsonDict):
        raise IntegrityViolation("Artifact ObjectRef projection is not an object")
    return _ArtifactRowProjection(
        artifact_id=wire["artifact_id"],
        lineage_schema_version=wire["lineage_schema_version"],
        kind=wire["kind"],
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=wire["payload_hash"],
        created_at=wire["created_at"],
        meta=meta,
        object_ref=object_ref,
    )


def _snapshot_from_row(row: ReadSnapshotRow) -> ReadSnapshotV1:
    try:
        snapshot = ReadSnapshotV1(
            snapshot_schema_version=row.snapshot_schema_version,
            snapshot_id=row.snapshot_id,
            resource_kind=row.resource_kind,
            query_hash=row.query_hash,
            authz_fingerprint=row.authz_fingerprint,
            stable_sort_schema_id=row.stable_sort_schema_id,
            strategy=row.strategy,
            high_watermark=row.high_watermark,
            materialized_item_count=row.materialized_item_count,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored artifact read snapshot is invalid",
            snapshot_id=row.snapshot_id,
        ) from exc
    if (
        snapshot.resource_kind != "artifacts"
        or snapshot.authz_fingerprint != _ARTIFACT_AUTHZ_FINGERPRINT
        or snapshot.stable_sort_schema_id != _STABLE_SORT_SCHEMA_ID
        or snapshot.strategy != "immutable_high_watermark"
    ):
        raise IntegrityViolation(
            "stored artifact read snapshot metadata is invalid",
            snapshot_id=row.snapshot_id,
        )
    return snapshot


def _encode_position(artifact_id: str) -> str:
    return canonical_json({"artifact_id": artifact_id})


def _decode_position(position: str) -> str:
    try:
        value = json.loads(position)
    except (TypeError, ValueError) as exc:
        raise CursorInvalid("artifact cursor position is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"artifact_id"}
        or not isinstance(value["artifact_id"], str)
        or not value["artifact_id"]
    ):
        raise CursorInvalid("artifact cursor position is invalid")
    return value["artifact_id"]


class SqlArtifactRepository:
    """Persist ArtifactV1/V2 without owning the surrounding transaction."""

    def __init__(
        self,
        session: Session,
        *,
        binding_repository: SqlObjectBindingRepository | None,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int = 100,
        snapshot_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_ITEMS:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_ITEMS}")
        if snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        self._session = session
        self._binding_repository = binding_repository
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl

    def get(self, identifier: str) -> ArtifactWire | None:
        row = self._session.get(ArtifactRow, identifier)
        if row is None:
            return None
        try:
            wire = _row_wire(row)
        except IntegrityViolation:
            raise
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "stored artifact row is invalid",
                artifact_id=identifier,
            ) from exc
        return _parse_stored_wire(wire, artifact_id=identifier, source="stored artifact row")

    def get_many(self, identifiers: Sequence[str]) -> dict[str, ArtifactWire | None]:
        """Read an exact Artifact set with bounded SQL statements."""

        selected = tuple(dict.fromkeys(identifiers))
        if any(not isinstance(identifier, str) or not identifier for identifier in selected):
            raise ValueError("artifact identifiers must be non-empty strings")
        retained: dict[str, ArtifactWire | None] = dict.fromkeys(selected)
        for offset in range(0, len(selected), _MAX_SQL_IN_ITEMS):
            rows = self._session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.artifact_id.in_(selected[offset : offset + _MAX_SQL_IN_ITEMS])
                )
            ).all()
            for row in rows:
                identifier = row.artifact_id
                try:
                    wire = _row_wire(row)
                    retained[identifier] = _parse_stored_wire(
                        wire,
                        artifact_id=identifier,
                        source="stored artifact row",
                    )
                except IntegrityViolation:
                    raise
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "stored artifact row is invalid",
                        artifact_id=identifier,
                    ) from exc
        return retained

    def put(self, item: ArtifactWire) -> ArtifactWire:
        parsed = _revalidate_for_put(item)
        if isinstance(parsed, ArtifactV2):
            if self._binding_repository is None or not self._binding_repository.has_active_binding(
                parsed.object_ref
            ):
                raise IntegrityViolation(
                    "ArtifactV2 publication requires an active ObjectBinding",
                    artifact_id=parsed.artifact_id,
                    object_key=parsed.object_ref.key,
                )

        existing = self.get(parsed.artifact_id)
        if existing is None:
            self._session.add(_artifact_row(parsed))
            self._session.flush()
            return parsed
        if _immutable_identity(existing) != _immutable_identity(parsed):
            raise IntegrityViolation(
                "artifact id is already bound to different immutable content",
                artifact_id=parsed.artifact_id,
            )
        return existing

    def put_many(self, items: Sequence[ArtifactWire]) -> tuple[ArtifactWire, ...]:
        """Compatibility facade over the sealed Artifact preflight/apply boundary."""

        normalized = tuple(items)
        if not normalized:
            return ()
        return self.put_preflighted_many(self.preflight_put_many(normalized))

    def preflight_put_many(
        self,
        items: Sequence[ArtifactWire],
        *,
        binding_preflight: _PreflightedTerminalObjectBindings | None = None,
    ) -> _PreflightedArtifactWrites:
        """Validate and seal one immutable Artifact aggregate before DML.

        Validation and retained-row comparison finish before any row is added.  The
        method preserves input order (including exact idempotent duplicates), checks
        every lineage@2 ObjectRef against current or sealed future binding authority,
        and prebuilds the exact rows later consumed by ``put_preflighted_many``.
        """

        parsed_items = tuple(_revalidate_for_put(item) for item in items)
        if not parsed_items:
            raise ValueError("Artifact preflight requires at least one item")

        refs = tuple(item.object_ref for item in parsed_items if isinstance(item, ArtifactV2))
        read_phase_active_by_key: dict[str, bool] | None = None
        if binding_preflight is None:
            self._require_active_bindings_many(refs)
        else:
            if not isinstance(self._binding_repository, SqlObjectBindingRepository):
                raise IntegrityViolation(
                    "sealed Artifact publication requires its SQL ObjectBinding repository"
                )
            read_phase_active_by_key = binding_preflight.authorize_artifact_refs(
                self._binding_repository,
                refs,
            )
        retained_by_id = self.get_many(tuple(item.artifact_id for item in parsed_items))
        pending_by_id: dict[str, ArtifactWire] = {}
        results: list[ArtifactWire] = []
        for parsed in parsed_items:
            retained = retained_by_id[parsed.artifact_id]
            if retained is None:
                retained = pending_by_id.get(parsed.artifact_id)
            if retained is None:
                pending_by_id[parsed.artifact_id] = parsed
                results.append(parsed)
                continue
            if _immutable_identity(retained) != _immutable_identity(parsed):
                raise IntegrityViolation(
                    "artifact id is already bound to different immutable content",
                    artifact_id=parsed.artifact_id,
                )
            if (
                read_phase_active_by_key is not None
                and isinstance(parsed, ArtifactV2)
                and not read_phase_active_by_key[parsed.object_ref.key]
            ):
                raise IntegrityViolation(
                    "retained Artifact has no read-phase active ObjectBinding",
                    artifact_id=parsed.artifact_id,
                    object_key=parsed.object_ref.key,
                )
            results.append(retained)

        return _issue_artifact_preflight(
            _ArtifactPreflightState(
                owner=self,
                transaction_identity=_current_transaction_identity(self._session),
                rows=tuple(_artifact_row_projection(item) for item in pending_by_id.values()),
                stored=tuple(_freeze_artifact_result(item) for item in results),
                binding_preflight=binding_preflight,
            )
        )

    def put_preflighted_many(
        self,
        preflight: _PreflightedArtifactWrites,
    ) -> tuple[ArtifactWire, ...]:
        """Consume a trusted preflight seal and issue only its planned DML/flush."""

        if not isinstance(preflight, _PreflightedArtifactWrites):
            raise IntegrityViolation("Artifact batch lacks a trusted preflight seal")
        projections, stored = _consume_artifact_preflight(preflight, self)
        if projections:
            self._session.add_all(projection.build_row() for projection in projections)
            self._session.flush()
        return stored

    def _require_active_bindings_many(self, refs: Sequence[ObjectRef]) -> None:
        if not refs:
            return
        if self._binding_repository is None:
            first = refs[0]
            raise IntegrityViolation(
                "ArtifactV2 publication requires an active ObjectBinding",
                object_key=getattr(first, "key", None),
            )

        # Production uses the SQL repository.  Its scalar API deliberately accepts
        # an active binding in any configured store, so the batch path performs the
        # same all-store projection instead of narrowing authority to the default
        # store exposed by ``resolve_many``.
        if isinstance(self._binding_repository, SqlObjectBindingRepository):
            requested_by_key: dict[str, ObjectRef] = {}
            for ref in refs:
                key = ref.key
                retained = requested_by_key.setdefault(key, ref)
                if retained != ref:
                    raise IntegrityViolation(
                        "one object key is bound to conflicting ObjectRefs",
                        object_key=key,
                    )
            active_keys: set[str] = set()
            keys = tuple(requested_by_key)
            for offset in range(0, len(keys), _MAX_SQL_IN_ITEMS):
                rows = self._session.scalars(
                    select(ObjectBindingRow).where(
                        ObjectBindingRow.object_key.in_(keys[offset : offset + _MAX_SQL_IN_ITEMS])
                    )
                ).all()
                for row in rows:
                    binding = _binding_from_row(row)
                    requested = requested_by_key[row.object_key]
                    if binding.object_ref != requested:
                        raise IntegrityViolation(
                            "stored ObjectRef differs from the requested ObjectRef",
                            object_key=row.object_key,
                        )
                    if binding.status == "active":
                        active_keys.add(row.object_key)
            missing = tuple(key for key in keys if key not in active_keys)
            if missing:
                raise IntegrityViolation(
                    "ArtifactV2 publication requires an active ObjectBinding",
                    object_key=missing[0],
                )
            return

        # Lightweight test/in-memory adapters retain compatibility with the scalar
        # capability; production never takes this fallback.
        for ref in refs:
            if not self._binding_repository.has_active_binding(ref):
                raise IntegrityViolation(
                    "ArtifactV2 publication requires an active ObjectBinding",
                    object_key=ref.key,
                )

    def page(self, cursor: PageCursorV1 | None = None) -> PageV1[ArtifactWire]:
        if cursor is None:
            snapshot = self._create_snapshot()
            position = None
        else:
            self._cursor_signer.verify_signature(cursor)
            row = self._session.get(ReadSnapshotRow, cursor.snapshot_id)
            if row is None:
                raise CursorExpired("artifact read snapshot is no longer retained")
            snapshot = _snapshot_from_row(row)
            self._cursor_signer.verify(
                cursor,
                expected_snapshot=snapshot,
                expected_query_hash=_ARTIFACT_QUERY_HASH,
                requested_page_size=self._page_size,
                snapshot_is_retained=lambda snapshot_id: (
                    self._session.get(ReadSnapshotRow, snapshot_id) is not None
                ),
            )
            position = _decode_position(cursor.position)

        high_watermark = snapshot.high_watermark
        if high_watermark is None:
            raise IntegrityViolation(
                "artifact read snapshot has no immutable high watermark",
                snapshot_id=snapshot.snapshot_id,
            )
        statement = select(ArtifactRow).where(_ARTIFACT_ROWID <= high_watermark)
        if position is not None:
            anchor = self._session.scalar(
                select(_ARTIFACT_ROWID)
                .select_from(ArtifactRow)
                .where(
                    ArtifactRow.artifact_id == position,
                    _ARTIFACT_ROWID <= high_watermark,
                )
            )
            if anchor is None:
                raise IntegrityViolation(
                    "artifact cursor anchor is missing from its read snapshot",
                    snapshot_id=snapshot.snapshot_id,
                    artifact_id=position,
                )
            statement = statement.where(ArtifactRow.artifact_id > position)
        rows = self._session.scalars(
            statement.order_by(ArtifactRow.artifact_id).limit(self._page_size + 1)
        ).all()

        values = tuple(
            _parse_stored_wire(
                _row_wire(row),
                artifact_id=row.artifact_id,
                source="stored artifact row",
            )
            for row in rows
        )
        items = values[: self._page_size]
        next_cursor = None
        if len(values) > self._page_size:
            next_cursor = self._cursor_signer.issue(
                snapshot=snapshot,
                position=_encode_position(items[-1].artifact_id),
                page_size=self._page_size,
            )
        return PageV1[ArtifactWire](
            read_snapshot_id=snapshot.snapshot_id,
            items=items,
            next_cursor=next_cursor,
            expires_at=snapshot.expires_at,
        )

    def _create_snapshot(self) -> ReadSnapshotV1:
        high_watermark = self._session.scalar(
            select(func.coalesce(func.max(_ARTIFACT_ROWID), 0)).select_from(ArtifactRow)
        )
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise IntegrityViolation("artifact repository clock must return UTC")
        created_at = now.astimezone(timezone.utc)
        expires_at = created_at + self._snapshot_ttl
        snapshot = ReadSnapshotV1(
            snapshot_id=f"artifact-read-snapshot:{uuid.uuid4().hex}",
            resource_kind="artifacts",
            query_hash=_ARTIFACT_QUERY_HASH,
            authz_fingerprint=_ARTIFACT_AUTHZ_FINGERPRINT,
            stable_sort_schema_id=_STABLE_SORT_SCHEMA_ID,
            strategy="immutable_high_watermark",
            high_watermark=high_watermark,
            created_at=created_at.isoformat().replace("+00:00", "Z"),
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )
        self._session.add(
            ReadSnapshotRow(
                snapshot_id=snapshot.snapshot_id,
                snapshot_schema_version=snapshot.snapshot_schema_version,
                resource_kind=snapshot.resource_kind,
                query_hash=snapshot.query_hash,
                authz_fingerprint=snapshot.authz_fingerprint,
                stable_sort_schema_id=snapshot.stable_sort_schema_id,
                strategy=snapshot.strategy,
                high_watermark=snapshot.high_watermark,
                materialized_item_count=snapshot.materialized_item_count,
                created_at=snapshot.created_at,
                expires_at=snapshot.expires_at,
            )
        )
        self._session.flush()
        return snapshot


__all__ = ["SqlArtifactRepository"]
