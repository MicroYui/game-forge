from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from gameforge.apps.api.read_paging import SqlMaterializedPageAdapter
from gameforge.platform.read_models.paging import ReadPageBinding, ReadPageCandidate
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base


def test_sql_materialized_page_bridge_preserves_verified_platform_projection(tmp_path) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'read-paging.db'}")
    Base.metadata.create_all(engine)
    clock = FrozenUtcClock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    binding = ReadPageBinding(
        resource_kind="runs",
        query_hash="1" * 64,
        authz_fingerprint="2" * 64,
        stable_sort_schema_id="run-id@1",
        view_schema_id="run-view@1",
        principal_binding="3" * 64,
    )
    candidates = tuple(
        ReadPageCandidate(
            resource_id=f"run:{index}",
            observed_revision=1,
            canonical_view={"run_id": f"run:{index}", "revision": 1},
        )
        for index in range(3)
    )

    try:
        with Session(engine) as first_session, first_session.begin():
            first = SqlMaterializedPageAdapter(
                first_session,
                cursor_signer=CursorSigner(signing_key=b"read-paging-key", clock=clock),
                clock=clock,
                page_size=2,
                snapshot_ttl=timedelta(minutes=5),
                max_materialized_items=10,
            ).create(candidates, binding=binding)

        assert [item.resource_id for item in first.items] == ["run:0", "run:1"]
        assert first.next_cursor is not None

        with Session(engine) as second_session, second_session.begin():
            second = SqlMaterializedPageAdapter(
                second_session,
                cursor_signer=CursorSigner(signing_key=b"read-paging-key", clock=clock),
                clock=clock,
                page_size=2,
                snapshot_ttl=timedelta(minutes=5),
                max_materialized_items=10,
            ).page(first.next_cursor, binding=binding)

        assert [item.resource_id for item in second.items] == ["run:2"]
        assert second.next_cursor is None
    finally:
        engine.dispose()
