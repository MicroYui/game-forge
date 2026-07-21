"""Request-scoped bridges for retained observability read pages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from gameforge.apps.api.pagination import OpaquePageCursorCodec
from gameforge.contracts.cost import (
    BudgetSetSnapshotV1,
    CostSettlementSummaryV1,
    UsageEntryV1,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.platform.read_models.authorization import ReadAuthorizationBinding
from gameforge.platform.read_models.observability import RunCostReadPage
from gameforge.platform.read_models.paging import (
    MaterializedPageFactory,
    ReadPageBinding,
    ReadPageCandidate,
)


class CostUsageRepository(Protocol):
    """The existing SqlCostRepository read surface used by this bridge."""

    def list_usage(
        self,
        *,
        run_id: str,
        attempt_no: int | None = None,
        limit: int = 100,
        after: tuple[str, str] | None = None,
    ) -> Sequence[UsageEntryV1]: ...

    def summarize_run_settlement(self, *, run_id: str) -> CostSettlementSummaryV1: ...


class _RetainedCostItemV1(BaseModel):
    """Cost-only materialized wrapper that freezes summary with every page item."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    retained_schema_version: Literal["retained-cost-item@1"] = "retained-cost-item@1"
    item_kind: Literal["usage", "summary"]
    settlement_summary: CostSettlementSummaryV1
    usage: UsageEntryV1 | None = None

    @model_validator(mode="after")
    def _closed_shape(self) -> _RetainedCostItemV1:
        if (self.item_kind == "usage") != (self.usage is not None):
            raise ValueError("retained cost item kind differs from its usage payload")
        return self


class SqlCostUsagePageAdapter:
    """Materialize one complete bounded Run usage view, then page it stably."""

    def __init__(
        self,
        *,
        repository: CostUsageRepository,
        page_factory: MaterializedPageFactory,
        cursor_codec: OpaquePageCursorCodec,
        max_materialized_items: int,
        repository_batch_size: int = 1000,
    ) -> None:
        if not callable(page_factory):
            raise TypeError("page_factory must be callable")
        if not isinstance(cursor_codec, OpaquePageCursorCodec):
            raise TypeError("cursor_codec must be OpaquePageCursorCodec")
        for name, value in (
            ("max_materialized_items", max_materialized_items),
            ("repository_batch_size", repository_batch_size),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be positive")
        self._repository = repository
        self._page_factory = page_factory
        self._cursor_codec = cursor_codec
        self._max_items = max_materialized_items
        self._batch_size = repository_batch_size

    def page(
        self,
        *,
        run_id: str,
        budget_set: BudgetSetSnapshotV1,
        cursor: str | None,
        limit: int,
        authorization: ReadAuthorizationBinding,
        query_hash: str,
    ) -> RunCostReadPage:
        if budget_set.run_id != run_id:
            raise IntegrityViolation("cost budget set belongs to another Run")
        binding = ReadPageBinding(
            resource_kind="cost_usage",
            query_hash=query_hash,
            authz_fingerprint=authorization.authz_fingerprint,
            stable_sort_schema_id="cost-usage-recorded-at-id@1",
            view_schema_id="retained-cost-item@1",
            principal_binding=authorization.principal_binding,
        )
        pager = self._page_factory(limit)
        if cursor is None:
            settlement_summary = self._repository.summarize_run_settlement(run_id=run_id)
            if type(settlement_summary) is not CostSettlementSummaryV1:
                raise IntegrityViolation("cost repository returned an invalid settlement summary")
            complete_usage = self._load_complete(run_id)
            if settlement_summary.usage_entry_count != len(complete_usage):
                raise IntegrityViolation(
                    "cost settlement summary differs from retained usage authority"
                )
            retained_items = tuple(
                _RetainedCostItemV1(
                    item_kind="usage",
                    settlement_summary=settlement_summary,
                    usage=item,
                )
                for item in complete_usage
            )
            if not retained_items:
                retained_items = (
                    _RetainedCostItemV1(
                        item_kind="summary",
                        settlement_summary=settlement_summary,
                    ),
                )
            retained = pager.create(
                tuple(
                    ReadPageCandidate(
                        resource_id=(
                            item.usage.usage_id
                            if item.usage is not None
                            else "cost-summary:"
                            + canonical_sha256({"run_id": run_id, "query_hash": query_hash})
                        ),
                        observed_revision=1,
                        canonical_view=item.model_dump(mode="json"),
                    )
                    for item in retained_items
                ),
                binding=binding,
            )
        else:
            retained = pager.page(
                self._cursor_codec.decode(cursor),
                binding=binding,
            )
        usage: list[UsageEntryV1] = []
        settlement_summary: CostSettlementSummaryV1 | None = None
        for item in retained.items:
            try:
                parsed = _RetainedCostItemV1.model_validate(item.canonical_view)
            except (TypeError, ValueError, ValidationError) as exc:
                raise IntegrityViolation("retained cost view is invalid") from exc
            if settlement_summary is None:
                settlement_summary = parsed.settlement_summary
            elif settlement_summary != parsed.settlement_summary:
                raise IntegrityViolation("retained cost summary differs within one snapshot")
            if parsed.usage is None:
                expected_id = "cost-summary:" + canonical_sha256(
                    {"run_id": run_id, "query_hash": query_hash}
                )
                if item.resource_id != expected_id:
                    raise IntegrityViolation(
                        "retained cost summary identity differs from its query"
                    )
                continue
            if parsed.usage.usage_id != item.resource_id or parsed.usage.run_id != run_id:
                raise IntegrityViolation("retained cost usage identity differs from its Run")
            usage.append(parsed.usage)
        if settlement_summary is None:
            raise IntegrityViolation("retained cost view has no settlement summary")
        return RunCostReadPage(
            budget_set=budget_set,
            settlement_summary=settlement_summary,
            usage_entries=tuple(usage),
            next_cursor=(
                None
                if retained.next_cursor is None
                else self._cursor_codec.encode(retained.next_cursor)
            ),
        )

    def _load_complete(self, run_id: str) -> tuple[UsageEntryV1, ...]:
        result: list[UsageEntryV1] = []
        after: tuple[str, str] | None = None
        previous_key: tuple[str, str] | None = None
        while len(result) <= self._max_items:
            remaining = self._max_items + 1 - len(result)
            batch_limit = min(self._batch_size, remaining)
            batch = tuple(
                self._repository.list_usage(
                    run_id=run_id,
                    limit=batch_limit,
                    after=after,
                )
            )
            if len(batch) > batch_limit:
                raise IntegrityViolation("cost repository exceeded its requested batch limit")
            for item in batch:
                if type(item) is not UsageEntryV1 or item.run_id != run_id:
                    raise IntegrityViolation("cost repository returned an invalid Run usage entry")
                recorded_at = item.model_dump(mode="json")["recorded_at"]
                key = (recorded_at, item.usage_id)
                if previous_key is not None and key <= previous_key:
                    raise IntegrityViolation("cost repository usage order is not stable and unique")
                result.append(item)
                previous_key = key
            if len(batch) < batch_limit:
                break
            if not batch:
                break
            assert previous_key is not None
            after = previous_key
        if len(result) > self._max_items:
            raise QueryTooBroad(
                "cost usage query exceeds the configured materialization bound",
                max_items=self._max_items,
            )
        return tuple(result)


__all__ = ["CostUsageRepository", "SqlCostUsagePageAdapter"]
