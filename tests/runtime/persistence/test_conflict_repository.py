from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import sha256_lowerhex, typed_canonical_json
from gameforge.contracts.diff import (
    CollectionIdentityV1,
    ConflictSet,
    ConflictSetContextV1,
    MergeConflict,
    ThreeWayMergePolicyV1,
    compute_merge_policy_digest,
)
from gameforge.contracts.errors import CursorExpired, CursorInvalid, IntegrityViolation
from gameforge.contracts.storage import RefValue
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ArtifactRow,
    Base,
    ConflictSetRow,
    MergeConflictRow,
)


NOW = datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc)
SIGNING_KEY = b"conflict-set-repository-test-key"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'conflicts.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _repository(
    session: Session,
    *,
    now: datetime = NOW,
    page_size: int = 2,
    signing_key: bytes = SIGNING_KEY,
) -> SqlConflictSetRepository:
    clock = FrozenUtcClock(now)
    return SqlConflictSetRepository(
        session,
        cursor_signer=CursorSigner(signing_key=signing_key, clock=clock),
        clock=clock,
        page_size=page_size,
        snapshot_ttl=timedelta(minutes=5),
    )


def _seed_patch(session: Session, artifact_id: str) -> None:
    session.add(
        ArtifactRow(
            artifact_id=artifact_id,
            lineage_schema_version="lineage@1",
            kind="patch",
            version_tuple={},
            lineage=[],
            payload_hash="a" * 64,
            created_at="2026-07-14T09:00:00Z",
            meta={},
            object_ref=None,
        )
    )


def _conflict(identifier: str, path: str, value: int) -> MergeConflict:
    return MergeConflict(
        id=identifier,
        path=path,
        kind="both_changed",
        base={"presence": "present", "value": value},
        current={"presence": "present", "value": value + 1},
        proposed={"presence": "present", "value": value + 2},
        allowed_resolutions=("keep_current", "take_proposed", "custom"),
    )


def _conflicts() -> tuple[MergeConflict, ...]:
    return (
        _conflict("conflict:a", "/a", 1),
        _conflict("conflict:b", "/b", 2),
        _conflict("conflict:c", "/c", 3),
    )


def _conflict_set(
    *,
    identifier: str = "conflict-set:1",
    artifact_id: str = "artifact:patch:1",
    count: int = 3,
) -> ConflictSet:
    return ConflictSet(
        id=identifier,
        base_snapshot_id="snapshot:base",
        current_snapshot_id="snapshot:current",
        proposed_patch_artifact_id=artifact_id,
        expected_ref_revision=7,
        conflict_count=count,
        non_conflicting_ops_digest="b" * 64,
        created_at="2026-07-14T09:00:00Z",
    )


def _context(
    *,
    artifact_id: str = "artifact:patch:1",
    ref_revision: int = 7,
) -> ConflictSetContextV1:
    identities = (CollectionIdentityV1(path="/items", identity_key="id"),)
    policy = ThreeWayMergePolicyV1(
        policy_version="merge-policy:1",
        collection_identities=identities,
        policy_digest=compute_merge_policy_digest("merge-policy:1", identities),
    )
    return ConflictSetContextV1(
        subject_series_id="patch-series:1",
        expected_subject_artifact_id=artifact_id,
        expected_approval_id="approval:patch:1",
        expected_subject_head_revision=3,
        expected_workflow_revision=5,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id="artifact:current", revision=ref_revision),
        merge_policy=policy,
    )


def test_put_get_context_and_exact_replay_are_immutable(engine: Engine) -> None:
    conflict_set = _conflict_set()
    context = _context()
    conflicts = _conflicts()

    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        assert repository.put(conflict_set, context, conflicts) == conflict_set
        assert repository.put(conflict_set, context, conflicts) == conflict_set
        assert repository.get(conflict_set.id) == conflict_set
        assert repository.get_context(conflict_set.id) == context

        rows = session.scalars(
            select(MergeConflictRow)
            .where(MergeConflictRow.conflict_set_id == conflict_set.id)
            .order_by(MergeConflictRow.ordinal)
        ).all()
        assert [(row.ordinal, row.conflict_id, row.path) for row in rows] == [
            (1, "conflict:a", "/a"),
            (2, "conflict:b", "/b"),
            (3, "conflict:c", "/c"),
        ]
        stored = session.get(ConflictSetRow, conflict_set.id)
        assert stored is not None
        assert stored.context == context.model_dump(mode="json")


def test_same_id_with_any_changed_content_is_integrity_failure(engine: Engine) -> None:
    conflict_set = _conflict_set()
    context = _context()
    conflicts = _conflicts()

    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        repository.put(conflict_set, context, conflicts)

        changed_set = conflict_set.model_copy(
            update={"non_conflicting_ops_digest": "c" * 64}
        )
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            repository.put(changed_set, context, conflicts)

        changed_context = context.model_copy(update={"expected_workflow_revision": 6})
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            repository.put(conflict_set, changed_context, conflicts)

        changed_conflicts = (
            conflicts[0].model_copy(update={"kind": "delete_vs_change"}),
            *conflicts[1:],
        )
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            repository.put(conflict_set, context, changed_conflicts)

        typed_changed_wire = conflicts[0].model_dump(mode="python")
        typed_changed_wire["base"]["value"] = True
        typed_changed_conflicts = (
            MergeConflict.model_validate(typed_changed_wire),
            *conflicts[1:],
        )
        with pytest.raises(IntegrityViolation, match="different immutable content"):
            repository.put(conflict_set, context, typed_changed_conflicts)


@pytest.mark.parametrize(
    ("conflict_set", "context", "conflicts", "message"),
    [
        (_conflict_set(count=2), _context(), _conflicts(), "conflict_count"),
        (_conflict_set(), _context(), tuple(reversed(_conflicts())), "sorted"),
        (
            _conflict_set(),
            _context(),
            (_conflicts()[0], _conflicts()[0], _conflicts()[2]),
            "conflict IDs",
        ),
        (_conflict_set(), _context(ref_revision=8), _conflicts(), "ref revision"),
        (
            _conflict_set(),
            _context(artifact_id="artifact:other"),
            _conflicts(),
            "subject artifact",
        ),
    ],
)
def test_put_rejects_broken_collection_and_context_closure(
    engine: Engine,
    conflict_set: ConflictSet,
    context: ConflictSetContextV1,
    conflicts: tuple[MergeConflict, ...],
    message: str,
) -> None:
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        with pytest.raises(IntegrityViolation, match=message):
            _repository(session).put(conflict_set, context, conflicts)


def test_put_requires_the_proposed_patch_artifact(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="proposed Patch Artifact"):
            _repository(session).put(_conflict_set(), _context(), _conflicts())


def test_conflicts_are_cursor_paged_with_a_signed_immutable_snapshot(
    engine: Engine,
) -> None:
    conflict_set = _conflict_set()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        repository.put(conflict_set, _context(), _conflicts())

        first = repository.page_conflicts(conflict_set.id)
        assert [item.path for item in first.items] == ["/a", "/b"]
        assert first.next_cursor is not None
        assert first.expires_at == "2026-07-14T09:35:00Z"

        second = repository.page_conflicts(conflict_set.id, first.next_cursor)
        assert [item.path for item in second.items] == ["/c"]
        assert second.next_cursor is None
        assert second.read_snapshot_id == first.read_snapshot_id


def test_cursor_page_rejects_row_content_changed_after_snapshot(engine: Engine) -> None:
    conflict_set = _conflict_set()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session, page_size=1)
        repository.put(conflict_set, _context(), _conflicts())
        first = repository.page_conflicts(conflict_set.id)
        assert first.next_cursor is not None

        row = session.get(MergeConflictRow, (conflict_set.id, 2))
        assert row is not None
        row.current = {"presence": "present", "value": 999}
        session.flush()

        with pytest.raises(IntegrityViolation, match="content digest"):
            repository.page_conflicts(conflict_set.id, first.next_cursor)


def test_cursor_page_rejects_child_and_row_digest_changed_after_snapshot(
    engine: Engine,
) -> None:
    conflict_set = _conflict_set()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session, page_size=1)
        repository.put(conflict_set, _context(), _conflicts())
        first = repository.page_conflicts(conflict_set.id)
        assert first.next_cursor is not None

        row = session.get(MergeConflictRow, (conflict_set.id, 2))
        assert row is not None
        tampered_wire = _conflicts()[1].model_dump(mode="python")
        tampered_wire["current"]["value"] = 999
        tampered = MergeConflict.model_validate(tampered_wire)
        row.current = tampered.current.model_dump(mode="json")
        digest_payload = {
            "digest_schema_version": "merge-conflict-content@1",
            "conflict": tampered.model_dump(mode="json"),
        }
        row.content_digest = sha256_lowerhex(
            typed_canonical_json(digest_payload).encode("utf-8")
        )
        session.flush()

        with pytest.raises(IntegrityViolation, match="content digest"):
            repository.page_conflicts(conflict_set.id, first.next_cursor)


def test_cursor_is_bound_to_signature_query_and_ttl(engine: Engine) -> None:
    first_set = _conflict_set()
    second_set = _conflict_set(
        identifier="conflict-set:2",
        artifact_id="artifact:patch:2",
        count=1,
    )
    second_conflicts = (_conflict("conflict:z", "/z", 9),)
    with Session(engine) as session, session.begin():
        _seed_patch(session, first_set.proposed_patch_artifact_id)
        _seed_patch(session, second_set.proposed_patch_artifact_id)
        repository = _repository(session, page_size=1)
        repository.put(first_set, _context(), _conflicts())
        repository.put(
            second_set,
            _context(artifact_id=second_set.proposed_patch_artifact_id),
            second_conflicts,
        )
        cursor = repository.page_conflicts(first_set.id).next_cursor
        assert cursor is not None

        with pytest.raises(CursorInvalid):
            repository.page_conflicts(
                first_set.id,
                cursor.model_copy(update={"position": "0"}),
            )
        with pytest.raises(CursorInvalid):
            repository.page_conflicts(second_set.id, cursor)

    with Session(engine) as session, session.begin():
        with pytest.raises(CursorExpired):
            _repository(
                session,
                now=NOW + timedelta(minutes=5),
                page_size=1,
            ).page_conflicts(first_set.id, cursor)


def test_reads_fail_closed_for_corrupt_structure_or_context(engine: Engine) -> None:
    conflict_set = _conflict_set()
    context = _context()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        repository.put(conflict_set, context, _conflicts())

    with Session(engine) as session, session.begin():
        session.execute(
            update(MergeConflictRow)
            .where(
                MergeConflictRow.conflict_set_id == conflict_set.id,
                MergeConflictRow.ordinal == 2,
            )
            .values(path="/0")
        )
        with pytest.raises(IntegrityViolation, match="content digest|path order"):
            _repository(session).load_bounded(conflict_set.id)

    with Session(engine) as session, session.begin():
        session.execute(
            update(MergeConflictRow)
            .where(
                MergeConflictRow.conflict_set_id == conflict_set.id,
                MergeConflictRow.ordinal == 2,
            )
            .values(path="/b")
        )
        row = session.get(ConflictSetRow, conflict_set.id)
        assert row is not None
        corrupt_context = dict(row.context)
        corrupt_context["expected_ref"] = {
            "artifact_id": "artifact:current",
            "revision": 99,
        }
        row.context = corrupt_context
        session.flush()
        with pytest.raises(IntegrityViolation, match="ref revision"):
            _repository(session).get_context(conflict_set.id)

    with Session(engine) as session, session.begin():
        row = session.get(ConflictSetRow, conflict_set.id)
        assert row is not None
        row.context = context.model_dump(mode="json")
        session.execute(
            delete(MergeConflictRow).where(
                MergeConflictRow.conflict_set_id == conflict_set.id,
                MergeConflictRow.ordinal == 3,
            )
        )
        session.flush()
        with pytest.raises(IntegrityViolation, match="row count"):
            _repository(session).load_bounded(conflict_set.id)


def test_metadata_and_first_page_recompute_the_aggregate_digest(engine: Engine) -> None:
    conflict_set = _conflict_set()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        _repository(session).put(conflict_set, _context(), _conflicts())

        row = session.get(ConflictSetRow, conflict_set.id)
        assert row is not None
        changed_context = dict(row.context)
        changed_context["expected_workflow_revision"] = 6
        row.context = changed_context
        session.flush()

        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="content digest"):
            repository.get_context(conflict_set.id)
        with pytest.raises(IntegrityViolation, match="content digest"):
            repository.page_conflicts(conflict_set.id)


def test_conflict_digest_distinguishes_nested_missing_from_null(engine: Engine) -> None:
    conflict_set = _conflict_set(count=1)
    conflict = MergeConflict(
        id="conflict:null",
        path="/nested",
        kind="both_changed",
        base={"presence": "present", "value": {"x": 1}},
        current={"presence": "present", "value": {"x": None}},
        proposed={"presence": "present", "value": {"x": 2}},
        allowed_resolutions=("keep_current", "take_proposed", "custom"),
    )
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        repository.put(conflict_set, _context(), (conflict,))

        row = session.get(MergeConflictRow, (conflict_set.id, 1))
        assert row is not None
        row.current = {"presence": "present", "value": {}}
        session.flush()

        with pytest.raises(IntegrityViolation, match="content digest"):
            repository.load_bounded(conflict_set.id)


def test_bounded_load_rejects_an_extra_row_before_parsing_unbounded_content(
    engine: Engine,
) -> None:
    conflict_set = _conflict_set()
    with Session(engine) as session, session.begin():
        _seed_patch(session, conflict_set.proposed_patch_artifact_id)
        repository = _repository(session)
        repository.put(conflict_set, _context(), _conflicts())
        session.add(
            MergeConflictRow(
                conflict_set_id=conflict_set.id,
                ordinal=4,
                conflict_id="conflict:extra",
                path="/z",
                kind="concurrent_change",
                base={"presence": "present", "value": 1},
                current={"presence": "present", "value": 2},
                proposed={"presence": "present", "value": 3},
                allowed_resolutions=["keep_current", "take_proposed", "custom"],
                content_digest="0" * 64,
            )
        )
        session.flush()

        with pytest.raises(IntegrityViolation, match="row count"):
            repository.load_bounded(conflict_set.id)


def test_absent_conflict_set_is_not_synthesized(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        assert repository.get("conflict-set:missing") is None
        assert repository.get_context("conflict-set:missing") is None
        with pytest.raises(KeyError):
            repository.page_conflicts("conflict-set:missing")
