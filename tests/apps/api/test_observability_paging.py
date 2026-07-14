from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from gameforge.apps.api.observability_paging import SqlCostUsagePageAdapter
from gameforge.apps.api.pagination import OpaquePageCursorCodec
from gameforge.apps.api.read_paging import SqlMaterializedPageAdapter
from gameforge.contracts.cost import (
    BudgetSetSnapshotV1,
    BudgetSnapshotV1,
    CacheHitObservationV1,
    CostAmountV1,
    LatencyObservationV1,
    MonetaryObservationV1,
    TokenUsageObservationV1,
    UsageEntryV1,
)
from gameforge.contracts.errors import QueryTooBroad
from gameforge.platform.read_models.authorization import ReadAuthorizationBinding
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _budget_set() -> BudgetSetSnapshotV1:
    amount = CostAmountV1(dimension="request", value=10, unit="request")
    return BudgetSetSnapshotV1(
        budget_set_snapshot_id="budget-set:run:1",
        run_id="run:1",
        selection_policy_version="selection@1",
        snapshots=(
            BudgetSnapshotV1(
                snapshot_id="budget-snapshot:run:1",
                budget_id="budget:run:1",
                scope_kind="run",
                scope_id="run:1",
                policy_version="budget-policy@1",
                budget_revision_at_freeze=1,
                limits=(amount,),
                reserved=(),
                consumed=(),
                captured_at=NOW,
            ),
        ),
        captured_at=NOW,
    )


def _usage(index: int) -> UsageEntryV1:
    return UsageEntryV1(
        usage_id=f"usage:{index}",
        reservation_group_id=f"reservation-group:{index}",
        budget_reservation_ids=(f"reservation:{index}",),
        scope="attempt_call",
        run_id="run:1",
        attempt_no=1,
        request_hash="sha256:" + f"{index:x}" * 64,
        transport_attempt=index,
        execution_source="online",
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=False),
        retry_index=index - 1,
        token_usage=TokenUsageObservationV1(
            status="reported",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        ),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=1),
        wall_time_ns=1,
        monetary=MonetaryObservationV1(status="unavailable"),
        routing_decision_kind="native",
        routing_decision_id=f"routing:{index}",
        fencing_token_at_reserve=1,
        recorded_at=NOW + timedelta(microseconds=index),
    )


class _Repository:
    def __init__(self, items: tuple[UsageEntryV1, ...]) -> None:
        self.items = items
        self.calls = 0

    def list_usage(
        self,
        *,
        run_id: str,
        attempt_no: int | None = None,
        limit: int = 100,
        after: tuple[str, str] | None = None,
    ) -> tuple[UsageEntryV1, ...]:
        del attempt_no
        self.calls += 1
        selected = tuple(item for item in self.items if item.run_id == run_id)
        if after is not None:
            selected = tuple(
                item
                for item in selected
                if (item.model_dump(mode="json")["recorded_at"], item.usage_id) > after
            )
        return selected[:limit]


def _adapter(session: Session, repository: _Repository) -> SqlCostUsagePageAdapter:
    clock = FrozenUtcClock(NOW)
    signer = CursorSigner(signing_key=b"cost-page-key", clock=clock)
    return SqlCostUsagePageAdapter(
        repository=repository,
        page_factory=lambda page_size: SqlMaterializedPageAdapter(
            session,
            cursor_signer=signer,
            clock=clock,
            page_size=page_size,
            snapshot_ttl=timedelta(minutes=5),
            max_materialized_items=10,
        ),
        cursor_codec=OpaquePageCursorCodec(),
        max_materialized_items=10,
        repository_batch_size=1,
    )


def test_cost_usage_pages_materialize_once_and_resume_through_existing_read_views(
    tmp_path,
) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'cost-pages.db'}")
    Base.metadata.create_all(engine)
    repository = _Repository((_usage(1), _usage(2)))
    authorization = ReadAuthorizationBinding(
        principal_binding="1" * 64,
        authz_fingerprint="2" * 64,
    )
    try:
        with Session(engine) as session, session.begin():
            first = _adapter(session, repository).page(
                run_id="run:1",
                budget_set=_budget_set(),
                cursor=None,
                limit=1,
                authorization=authorization,
                query_hash="3" * 64,
            )
        assert [item.usage_id for item in first.usage_entries] == ["usage:1"]
        assert first.next_cursor is not None
        calls_after_materialization = repository.calls
        repository.items = (_usage(1), _usage(2), _usage(3))

        with Session(engine) as session, session.begin():
            second = _adapter(session, repository).page(
                run_id="run:1",
                budget_set=_budget_set(),
                cursor=first.next_cursor,
                limit=1,
                authorization=authorization,
                query_hash="3" * 64,
            )
        assert [item.usage_id for item in second.usage_entries] == ["usage:2"]
        assert second.next_cursor is None
        assert repository.calls == calls_after_materialization
    finally:
        engine.dispose()


def test_cost_usage_materialization_rejects_an_unbounded_complete_view(tmp_path) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'cost-pages.db'}")
    Base.metadata.create_all(engine)
    repository = _Repository((_usage(1), _usage(2)))
    clock = FrozenUtcClock(NOW)
    try:
        with Session(engine) as session, session.begin(), pytest.raises(QueryTooBroad):
            SqlCostUsagePageAdapter(
                repository=repository,
                page_factory=lambda page_size: SqlMaterializedPageAdapter(
                    session,
                    cursor_signer=CursorSigner(signing_key=b"cost-page-key", clock=clock),
                    clock=clock,
                    page_size=page_size,
                    snapshot_ttl=timedelta(minutes=5),
                    max_materialized_items=1,
                ),
                cursor_codec=OpaquePageCursorCodec(),
                max_materialized_items=1,
                repository_batch_size=1,
            ).page(
                run_id="run:1",
                budget_set=_budget_set(),
                cursor=None,
                limit=1,
                authorization=ReadAuthorizationBinding(
                    principal_binding="1" * 64,
                    authz_fingerprint="2" * 64,
                ),
                query_hash="3" * 64,
            )
    finally:
        engine.dispose()
