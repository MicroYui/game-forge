from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from gameforge.apps.worker.components import (
    WorkerArtifactBlobReader,
    WorkerExactFindingRevisionLoader,
    WorkerFindingHeadRevisionResolver,
    WorkerPreparedArtifactStore,
    build_trusted_components,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.workflow import FindingEvidenceBindingV1
from gameforge.contracts.jobs import RunFindingLinkV1
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.run_handlers import (
    CheckerRunHandler,
    GenerationProposalHandler,
    RepairSearchHandler,
    ReviewRunHandler,
    SimulationRunHandler,
)
from gameforge.platform.run_handlers.patch_validation import PatchValidationHandler
from gameforge.platform.run_handlers.playtest import PlaytestRunHandler
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.spine.ir.snapshot import Snapshot


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
SIGNING_KEY = b"worker-finding-authority-signing-key"


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'worker-findings.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _revision(
    revision: int,
    *,
    finding_id: str = "finding:checker:missing-node",
    supersedes_revision: int | None = None,
) -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id=finding_id,
        revision=revision,
        supersedes_revision=supersedes_revision,
        created_at=f"2026-07-17T09:00:0{revision}Z",
        payload=FindingPayloadV1(
            source="checker",
            producer_id="graph-checker@1",
            producer_run_id=f"run:{revision}",
            oracle_type="deterministic",
            defect_class="dangling_reference",
            severity="major",
            snapshot_id="snapshot:exact",
            entities=["quest:one"],
            relations=["requires:one"],
            evidence={"missing_entity_id": "item:missing"},
            minimal_repro={"entity_id": "quest:one"},
            status="confirmed",
            confidence=1.0,
            message=f"exact revision {revision}",
        ),
    )


def _put(engine: Engine, revision: FindingRevisionV1) -> None:
    clock = FrozenUtcClock(NOW)
    with Session(engine) as session:
        repository = SqlFindingRepository(
            session,
            cursor_signer=CursorSigner(signing_key=SIGNING_KEY, clock=clock),
            clock=clock,
        )
        repository.put(
            revision,
            expected_current_revision=revision.supersedes_revision,
        )
        session.commit()


def test_exact_loader_materialises_only_the_bound_immutable_revision(engine: Engine) -> None:
    retained = _revision(1)
    _put(engine, retained)
    loader = WorkerExactFindingRevisionLoader(
        engine=engine,
        cursor_signing_key=SIGNING_KEY,
        clock=FrozenUtcClock(NOW),
    )
    binding = FindingEvidenceBindingV1(
        finding_id=retained.finding_id,
        finding_revision=retained.revision,
        evidence_artifact_id="artifact:evidence:one",
        finding_digest=finding_revision_digest(retained),
    )

    loaded = loader(object(), SimpleNamespace(findings=(binding,)))  # type: ignore[arg-type]

    assert len(loaded) == 1
    assert loaded[0].id == retained.finding_id
    assert loaded[0].producer_run_id == retained.payload.producer_run_id
    assert loaded[0].message == retained.payload.message
    # Persistence time is intentionally outside finding_revision_digest and must not
    # leak into the semantic legacy projection consumed by Agent requests.
    assert loaded[0].created_at is None


def test_exact_loader_rejects_missing_or_digest_mismatched_revision(engine: Engine) -> None:
    retained = _revision(1)
    _put(engine, retained)
    loader = WorkerExactFindingRevisionLoader(
        engine=engine,
        cursor_signing_key=SIGNING_KEY,
        clock=FrozenUtcClock(NOW),
    )

    with pytest.raises(IntegrityViolation, match="digest differs"):
        loader.load_exact(
            finding_id=retained.finding_id,
            finding_revision=retained.revision,
            finding_digest="0" * 64,
        )
    with pytest.raises(IntegrityViolation, match="unavailable"):
        loader.load_exact(
            finding_id=retained.finding_id,
            finding_revision=retained.revision + 1,
            finding_digest="0" * 64,
        )


def test_exact_loader_enumerates_evidence_linked_revisions(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _revision(
        1,
        finding_id="finding-series:playtest:episode-1:completion",
    )
    _put(engine, retained)
    digest = finding_revision_digest(retained)
    link = RunFindingLinkV1(
        run_id=retained.payload.producer_run_id,
        attempt_no=1,
        ordinal=1,
        finding_id=retained.finding_id,
        finding_revision=retained.revision,
        finding_digest=digest,
        evidence_artifact_id="artifact:playtest-trace",
    )

    def _linked(self, evidence_artifact_ids, *, max_items):
        del self, max_items
        return (link,) if link.evidence_artifact_id in evidence_artifact_ids else ()

    monkeypatch.setattr(
        SqlRunRepository,
        "list_finding_links_by_evidence_artifact_ids",
        _linked,
    )
    loader = WorkerExactFindingRevisionLoader(
        engine=engine,
        cursor_signing_key=SIGNING_KEY,
        clock=FrozenUtcClock(NOW),
    )

    linked = loader.list_linked_exact(evidence_artifact_ids=(link.evidence_artifact_id,))

    assert len(linked) == 1
    assert linked[0].evidence_artifact_id == link.evidence_artifact_id
    assert linked[0].revision == retained


def test_head_resolver_returns_the_verified_current_revision(engine: Engine) -> None:
    resolver = WorkerFindingHeadRevisionResolver(
        engine=engine,
    )
    assert resolver(("finding:absent",)) == {"finding:absent": None}

    first = _revision(1)
    _put(engine, first)
    assert resolver((first.finding_id,)) == {first.finding_id: 1}

    second = _revision(2, supersedes_revision=1)
    _put(engine, second)
    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", observe)
    try:
        revisions = resolver(("finding:absent", first.finding_id))
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert revisions == {"finding:absent": None, first.finding_id: 2}
    assert len(statements) == 1
    assert "finding_heads" in statements[0]
    assert "finding_revisions" not in statements[0]


def test_worker_composition_injects_one_sql_finding_authority_into_all_consumers(
    engine: Engine, tmp_path
) -> None:
    clock = FrozenUtcClock(NOW)
    object_store = LocalObjectStore(
        tmp_path / "objects",
        store_id="local:test",
        clock=clock,
        cursor_signing_key=SIGNING_KEY,
    )
    blobs = WorkerArtifactBlobReader(
        engine=engine,
        object_store=object_store,
        object_store_id="local:test",
        cursor_signing_key=SIGNING_KEY,
        clock=clock,
    )
    components = build_trusted_components(
        registry=build_builtin_registry(),
        blobs=blobs,
        store=WorkerPreparedArtifactStore(object_store),
    )

    checker = components.executors["checker_runner@1"]
    simulation = components.executors["simulation_runner@1"]
    review = components.executors["review_runner@1"]
    playtest = components.executors["playtest_runner@1"]
    generation = components.executors["generation_proposer@1"]
    repair = components.executors["repair_search@1"]
    patch_validation = components.executors["patch_validator@1"]
    assert isinstance(checker, CheckerRunHandler)
    assert isinstance(simulation, SimulationRunHandler)
    assert isinstance(review, ReviewRunHandler)
    assert isinstance(playtest, PlaytestRunHandler)
    assert isinstance(generation, GenerationProposalHandler)
    assert isinstance(repair, RepairSearchHandler)
    assert isinstance(patch_validation, PatchValidationHandler)
    assert checker.finding_head_revision is blobs.finding_head_revision
    assert simulation.finding_head_revision is blobs.finding_head_revision
    assert review.finding_head_revision is blobs.finding_head_revision
    assert playtest.finding_head_revision is blobs.finding_head_revision
    assert generation.finding_loader is blobs.finding_revision_loader
    assert repair.finding_loader is blobs.finding_revision_loader
    assert patch_validation.finding_revision_loader is blobs.finding_revision_loader

    numeric = Constraint(
        id="C_cap",
        kind="numeric",
        oracle="deterministic",
        **{"assert": "reward_gold <= 80"},
        severity="major",
    )
    generation_checkers = generation.agent_runner.checker_factory(Snapshot({}, {}), (numeric,))
    assert [checker.id for checker in generation_checkers] == [
        "graph",
        "compiled:smt:C_cap",
    ]
    with pytest.raises(IntegrityViolation, match="exact resolved binding"):
        review.checker_resolver(ProfileRefV1(profile_id="unknown", version=1), [])
