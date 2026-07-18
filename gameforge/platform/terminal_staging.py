"""Blob-first terminal-publication staging contracts.

Terminal publication is deliberately split into three phases:

1. a short authority snapshot produces an immutable publication draft;
2. every output blob is content-addressed and verified outside the DB UoW;
3. the complete draft is deep-sealed before the write UoW, whose fresh snapshot
   revalidates only compact mutable authority, set-preflights every affected row,
   then consumes the sealed operations with bounded batch writes.

The models in this module carry no database or ObjectStore capability.  In
particular, :class:`StagedTerminalPublication` is the only blob input accepted by
the commit surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Mapping, Protocol
from weakref import WeakKeyDictionary, WeakSet

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import ObjectBinding, ObjectLocation, ObjectRef
from gameforge.contracts.lineage import object_ref_for_bytes
from gameforge.contracts.storage import ObjectStat


_UNSET = object()


class FrozenDict(dict[str, object]):
    """Serializer-compatible recursively immutable JSON object."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("sealed terminal mapping is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, _memo: dict[int, object]) -> "FrozenDict":
        return self


class FrozenList(list[object]):
    """Serializer-compatible recursively immutable JSON array.

    Pydantic serializers distinguish declared ``list`` fields from tuples.  A
    frozen list therefore stays a list subclass while rejecting every in-place
    mutation surface, avoiding both mutation holes and serializer warnings.
    """

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("sealed terminal list is immutable")

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

    def __deepcopy__(self, _memo: dict[int, object]) -> "FrozenList":
        return self


@dataclass(frozen=True, slots=True)
class BlobMaterial:
    """One exact immutable byte payload required by a terminal draft."""

    slot: str
    payload: bytes
    expected_ref: ObjectRef

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("blob material slot must be non-empty")
        if self.expected_ref != object_ref_for_bytes(self.payload):
            raise ValueError("blob material expected_ref differs from its exact payload")


@dataclass(frozen=True, slots=True)
class PreverifiedArtifactBinding:
    """Existing active binding fully verified during the read/planning phase."""

    binding: ObjectBinding
    stat: ObjectStat

    def __post_init__(self) -> None:
        if (
            self.binding.status != "active"
            or self.binding.object_ref != self.stat.ref
            or self.binding.location != self.stat.location
        ):
            raise ValueError("preverified Artifact binding is not active and exact")


@dataclass(frozen=True, slots=True)
class PreverifiedAbsentArtifactBinding:
    """Read-phase proof that this ObjectRef had no active default-store binding."""

    object_ref: ObjectRef


@dataclass(frozen=True, slots=True)
class StagedReceipt:
    """The exact backend generation produced for one draft material."""

    slot: str
    ref: ObjectRef
    location: ObjectLocation
    verified_at: str | None = None
    generation_verification_token: str | None = None

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("staged receipt slot must be non-empty")
        if self.verified_at is not None and not self.verified_at:
            raise ValueError("staged receipt verified_at must be non-empty when present")
        if self.generation_verification_token is not None and not (
            self.generation_verification_token
        ):
            raise ValueError("staged receipt generation token must be non-empty when present")
        if self.ref.key != self.location.key:
            raise ValueError("staged receipt ref/location keys differ")


@dataclass(frozen=True, slots=True)
class _TerminalPublicationState:
    """Complete publication projection retained outside its opaque handle."""

    publication_kind: str
    run_id: str
    attempt_no: int | None
    occurred_at: str
    projection_digest: str
    materials: tuple[BlobMaterial, ...]
    operations: tuple[object, ...]
    operation_projection: tuple[Mapping[str, object], ...]
    result_projection: Mapping[str, object]
    result: object
    planning_subject_digest: str | None
    runtime_authority_digest: str | None


_TERMINAL_PUBLICATION_LOCK = Lock()
_TERMINAL_PUBLICATION_STATES: WeakKeyDictionary[object, _TerminalPublicationState] = (
    WeakKeyDictionary()
)
_TERMINAL_PUBLICATION_PHASES: WeakKeyDictionary[object, str] = WeakKeyDictionary()
_CONSUMED_TERMINAL_PUBLICATIONS: WeakSet[object] = WeakSet()


def _canonical_projection(state: _TerminalPublicationState) -> Mapping[str, object]:
    projection: dict[str, object] = {
        "publication_kind": state.publication_kind,
        "run_id": state.run_id,
        "attempt_no": state.attempt_no,
        "occurred_at": state.occurred_at,
        "materials": tuple(
            {
                "slot": material.slot,
                "expected_ref": material.expected_ref.model_dump(mode="json"),
            }
            for material in state.materials
        ),
        "operations": tuple(
            deep_freeze_value(projection) for projection in state.operation_projection
        ),
        "result": deep_freeze_value(state.result_projection),
    }
    if state.planning_subject_digest is not None:
        projection["planning_subject_digest"] = state.planning_subject_digest
        projection["runtime_authority_digest"] = state.runtime_authority_digest
    return projection


def _validate_terminal_publication_state(state: _TerminalPublicationState) -> None:
    if not state.publication_kind or not state.run_id or not state.occurred_at:
        raise ValueError("terminal publication draft identity must be complete")
    for field_name, digest in (
        ("planning_subject_digest", state.planning_subject_digest),
        ("runtime_authority_digest", state.runtime_authority_digest),
        ("projection_digest", state.projection_digest),
    ):
        if digest is not None and (
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field_name} must be canonical SHA-256")
    slots = tuple(material.slot for material in state.materials)
    if len(slots) != len(set(slots)):
        raise ValueError("terminal publication draft blob slots must be unique")
    if len(state.operations) != len(state.operation_projection):
        raise ValueError("terminal publication operation projection is incomplete")
    if state.projection_digest != canonical_sha256(_canonical_projection(state)):
        raise ValueError("terminal publication projection digest is not canonical")


def _detach_materials(materials: Sequence[BlobMaterial]) -> tuple[BlobMaterial, ...]:
    """Detach every material and nested ObjectRef from caller-owned aliases."""

    return tuple(
        BlobMaterial(
            slot=material.slot,
            payload=bytes(material.payload),
            expected_ref=ObjectRef.model_validate(material.expected_ref.model_dump(mode="json")),
        )
        for material in materials
    )


def _detach_operations(
    operations: Sequence[object],
    projections: Sequence[Mapping[str, object]],
) -> tuple[object, ...]:
    """Build an alias-free operation graph matching the immutable projection."""

    detached: list[object] = []
    for operation, expected_projection in zip(operations, projections, strict=True):
        seal = getattr(operation, "terminal_seal", None)
        if callable(seal):
            retained = seal()
            project = getattr(retained, "terminal_projection", None)
            if not callable(project) or project() != expected_projection:
                raise ValueError("terminal operation differs from its canonical projection")
        else:
            retained = deepcopy(operation)
        detached.append(retained)
    return tuple(detached)


def _issue_sealed_terminal_publication(
    source: TerminalPublicationDraft,
    state: _TerminalPublicationState,
) -> TerminalPublicationDraft:
    """Consume one draft and atomically issue exactly one sealed commit handle."""

    _validate_terminal_publication_state(state)
    handle = TerminalPublicationDraft()
    with _TERMINAL_PUBLICATION_LOCK:
        if (
            _TERMINAL_PUBLICATION_STATES.get(source) is None
            or _TERMINAL_PUBLICATION_PHASES.get(source) != "draft"
        ):
            if source in _CONSUMED_TERMINAL_PUBLICATIONS:
                raise IntegrityViolation("terminal publication handle was already consumed")
            raise IntegrityViolation("terminal publication handle has an invalid phase")
        _TERMINAL_PUBLICATION_PHASES[source] = "sealed_source"
        _CONSUMED_TERMINAL_PUBLICATIONS.add(source)
        _TERMINAL_PUBLICATION_STATES[handle] = state
        _TERMINAL_PUBLICATION_PHASES[handle] = "sealed"
    return handle


def _terminal_publication_state(
    handle: TerminalPublicationDraft,
    *,
    expected_phase: str | None = None,
) -> _TerminalPublicationState:
    with _TERMINAL_PUBLICATION_LOCK:
        state = _TERMINAL_PUBLICATION_STATES.get(handle)
        phase = _TERMINAL_PUBLICATION_PHASES.get(handle)
        if state is None or phase is None:
            raise IntegrityViolation("terminal publication handle is not authority-registered")
        if expected_phase is not None and phase != expected_phase:
            if handle in _CONSUMED_TERMINAL_PUBLICATIONS:
                raise IntegrityViolation("terminal publication handle was already consumed")
            raise IntegrityViolation("terminal publication handle has an invalid phase")
        return state


def consume_terminal_publications(
    handles: Sequence[TerminalPublicationDraft],
    *,
    expected_phase: str,
) -> tuple[_TerminalPublicationState, ...]:
    """Atomically consume registered drafts before the first publication DML."""

    normalized = tuple(handles)
    if len({id(handle) for handle in normalized}) != len(normalized):
        raise IntegrityViolation("terminal publication aggregate repeats a handle")
    with _TERMINAL_PUBLICATION_LOCK:
        states: list[_TerminalPublicationState] = []
        for handle in normalized:
            state = _TERMINAL_PUBLICATION_STATES.get(handle)
            phase = _TERMINAL_PUBLICATION_PHASES.get(handle)
            if state is None or phase is None:
                raise IntegrityViolation("terminal publication handle is not authority-registered")
            if phase != expected_phase:
                if handle in _CONSUMED_TERMINAL_PUBLICATIONS:
                    raise IntegrityViolation("terminal publication handle was already consumed")
                raise IntegrityViolation("terminal publication handle has an invalid phase")
            states.append(state)
        for handle in normalized:
            _TERMINAL_PUBLICATION_PHASES[handle] = "consumed"
            _CONSUMED_TERMINAL_PUBLICATIONS.add(handle)
        return tuple(states)


@dataclass(frozen=True, slots=True, init=False, eq=False, weakref_slot=True)
class TerminalPublicationDraft:
    """Opaque terminal plan handle whose complete state is weakly registered."""

    def __init__(
        self,
        publication_kind: object = _UNSET,
        run_id: object = _UNSET,
        attempt_no: object = _UNSET,
        occurred_at: object = _UNSET,
        projection_digest: object = _UNSET,
        materials: object = _UNSET,
        operations: object = _UNSET,
        operation_projection: object = _UNSET,
        result_projection: object = _UNSET,
        result: object = _UNSET,
        planning_subject_digest: object = _UNSET,
        runtime_authority_digest: object = _UNSET,
    ) -> None:
        values = (
            publication_kind,
            run_id,
            attempt_no,
            occurred_at,
            projection_digest,
            materials,
            operations,
            operation_projection,
            result_projection,
            result,
            planning_subject_digest,
            runtime_authority_digest,
        )
        if all(value is _UNSET for value in values):
            # ``dataclasses.replace`` and ``copy.copy`` receive an unregistered
            # empty handle, never a cloned authority capability.
            return
        required = values[:10]
        if any(value is _UNSET for value in required):
            raise TypeError("terminal publication draft fields must be complete")
        planning_digest = None if planning_subject_digest is _UNSET else planning_subject_digest
        runtime_digest = None if runtime_authority_digest is _UNSET else runtime_authority_digest
        if not isinstance(publication_kind, str) or not isinstance(run_id, str):
            raise TypeError("terminal publication draft identity must be strings")
        if attempt_no is not None and (
            isinstance(attempt_no, bool) or not isinstance(attempt_no, int)
        ):
            raise TypeError("terminal publication attempt_no must be an integer or null")
        if not isinstance(occurred_at, str) or not isinstance(projection_digest, str):
            raise TypeError("terminal publication time/digest must be strings")
        if not isinstance(materials, tuple) or not isinstance(operations, tuple):
            raise TypeError("terminal publication materials/operations must be tuples")
        if not isinstance(operation_projection, tuple) or not isinstance(
            result_projection, Mapping
        ):
            raise TypeError("terminal publication projections must be canonical containers")
        if planning_digest is not None and not isinstance(planning_digest, str):
            raise TypeError("planning subject digest must be a string or null")
        if runtime_digest is not None and not isinstance(runtime_digest, str):
            raise TypeError("runtime authority digest must be a string or null")
        frozen_operation_projection = tuple(
            deep_freeze_value(projection) for projection in operation_projection
        )
        detached_operations = _detach_operations(operations, frozen_operation_projection)
        state = _TerminalPublicationState(
            publication_kind=publication_kind,
            run_id=run_id,
            attempt_no=attempt_no,
            occurred_at=occurred_at,
            projection_digest=projection_digest,
            materials=_detach_materials(materials),
            operations=detached_operations,
            operation_projection=frozen_operation_projection,
            result_projection=deep_freeze_value(result_projection),  # type: ignore[arg-type]
            result=deep_freeze_value(result),
            planning_subject_digest=planning_digest,
            runtime_authority_digest=runtime_digest,
        )
        _validate_terminal_publication_state(state)
        with _TERMINAL_PUBLICATION_LOCK:
            _TERMINAL_PUBLICATION_STATES[self] = state
            _TERMINAL_PUBLICATION_PHASES[self] = "draft"

    def __copy__(self) -> TerminalPublicationDraft:
        return TerminalPublicationDraft()

    def __deepcopy__(self, _memo: dict[int, object]) -> TerminalPublicationDraft:
        return TerminalPublicationDraft()

    @property
    def publication_kind(self) -> str:
        return _terminal_publication_state(self).publication_kind

    @property
    def run_id(self) -> str:
        return _terminal_publication_state(self).run_id

    @property
    def attempt_no(self) -> int | None:
        return _terminal_publication_state(self).attempt_no

    @property
    def occurred_at(self) -> str:
        return _terminal_publication_state(self).occurred_at

    @property
    def projection_digest(self) -> str:
        return _terminal_publication_state(self).projection_digest

    @property
    def materials(self) -> tuple[BlobMaterial, ...]:
        return _detach_materials(_terminal_publication_state(self).materials)

    @property
    def operations(self) -> tuple[object, ...]:
        return tuple(
            deepcopy(operation) for operation in _terminal_publication_state(self).operations
        )

    @property
    def operation_projection(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            deep_freeze_value(projection)
            for projection in _terminal_publication_state(self).operation_projection
        )

    @property
    def result_projection(self) -> Mapping[str, object]:
        return deep_freeze_value(_terminal_publication_state(self).result_projection)  # type: ignore[return-value]

    @property
    def result(self) -> object:
        return deep_freeze_value(_terminal_publication_state(self).result)

    @property
    def planning_subject_digest(self) -> str | None:
        return _terminal_publication_state(self).planning_subject_digest

    @property
    def runtime_authority_digest(self) -> str | None:
        return _terminal_publication_state(self).runtime_authority_digest

    def canonical_projection(self) -> Mapping[str, object]:
        return _canonical_projection(_terminal_publication_state(self))

    def seal_for_commit(
        self,
        staged: StagedTerminalPublication,
    ) -> TerminalPublicationDraft:
        """Deep-isolate and register a one-shot commit authority handle."""

        state = _terminal_publication_state(self, expected_phase="draft")
        sealed_operations: list[object] = []
        live_projections: list[Mapping[str, object]] = []
        for operation, expected_projection in zip(
            state.operations,
            state.operation_projection,
            strict=True,
        ):
            seal = getattr(operation, "terminal_seal", None)
            if callable(seal):
                sealed = seal()
                project = getattr(sealed, "terminal_projection", None)
                if not callable(project):
                    raise ValueError("sealed terminal operation has no canonical projection")
                projection = project()
                if projection != expected_projection:
                    raise ValueError("terminal operation mutated before its commit seal")
                live_projections.append(projection)
            else:
                sealed = deepcopy(operation)
                live_projections.append(deepcopy(expected_projection))
            sealed_operations.append(sealed)

        sealed_result = deep_freeze_value(state.result)
        sealed_result_projection = _project_value(sealed_result)
        if (
            not isinstance(sealed_result_projection, Mapping)
            or sealed_result_projection != state.result_projection
        ):
            raise ValueError("terminal result mutated before its commit seal")
        sealed_state = _TerminalPublicationState(
            publication_kind=state.publication_kind,
            run_id=state.run_id,
            attempt_no=state.attempt_no,
            occurred_at=state.occurred_at,
            projection_digest=state.projection_digest,
            materials=_detach_materials(state.materials),
            operations=tuple(sealed_operations),
            operation_projection=tuple(
                deep_freeze_value(projection) for projection in live_projections
            ),
            result_projection=deep_freeze_value(sealed_result_projection),  # type: ignore[arg-type]
            result=sealed_result,
            planning_subject_digest=state.planning_subject_digest,
            runtime_authority_digest=state.runtime_authority_digest,
        )
        if staged.projection_digest != sealed_state.projection_digest:
            raise ValueError("staged publication differs from its sealed draft")
        receipts = {receipt.slot: receipt for receipt in staged.receipts}
        materials_by_slot = {material.slot: material for material in sealed_state.materials}
        if len(receipts) != len(staged.receipts) or set(receipts) != set(materials_by_slot):
            raise ValueError("staged receipt slots differ from the sealed draft")
        if any(
            receipts[slot].ref != material.expected_ref
            for slot, material in materials_by_slot.items()
        ):
            raise ValueError("staged receipt ref differs from the sealed draft")
        return _issue_sealed_terminal_publication(self, sealed_state)

    def is_commit_sealed(self) -> bool:
        with _TERMINAL_PUBLICATION_LOCK:
            return _TERMINAL_PUBLICATION_PHASES.get(self) == "sealed"


def deep_freeze_value(value: object) -> object:
    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        cloned = model_copy(deep=True)
        model_fields = getattr(type(cloned), "model_fields", {})
        for field_name in model_fields:
            object.__setattr__(
                cloned,
                field_name,
                deep_freeze_value(getattr(cloned, field_name)),
            )
        return cloned
    if isinstance(value, Mapping):
        return FrozenDict({str(key): deep_freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(deep_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(deep_freeze_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(deep_freeze_value(item) for item in value)
    return deepcopy(value)


def _project_value(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _project_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_project_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("terminal result contains a non-canonical projected value")


@dataclass(frozen=True, slots=True)
class StagedTerminalPublication:
    """Verified blob receipts bound to one exact publication draft digest."""

    projection_digest: str
    receipts: tuple[StagedReceipt, ...]

    def __post_init__(self) -> None:
        if len(self.projection_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.projection_digest
        ):
            raise ValueError("staged projection digest must be canonical SHA-256")
        slots = tuple(receipt.slot for receipt in self.receipts)
        if len(slots) != len(set(slots)):
            raise ValueError("staged receipt slots must be unique")


class TerminalPublicationStager(Protocol):
    """Materialize complete drafts outside every database UnitOfWork."""

    def stage(
        self, drafts: tuple[TerminalPublicationDraft, ...]
    ) -> tuple[StagedTerminalPublication, ...]: ...


__all__ = [
    "BlobMaterial",
    "consume_terminal_publications",
    "FrozenList",
    "PreverifiedAbsentArtifactBinding",
    "PreverifiedArtifactBinding",
    "StagedReceipt",
    "StagedTerminalPublication",
    "TerminalPublicationDraft",
    "TerminalPublicationStager",
]
