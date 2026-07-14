"""Composition bridges from pure read-model page ports to SQLite adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy.orm import Session

from gameforge.contracts.storage import MaterializedReadItemV1, PageCursorV1, PageV1, UtcClock
from gameforge.platform.read_models.paging import (
    MaterializedPagePort,
    ReadPageBinding,
    ReadPageCandidate,
    RetainedReadPageItem,
)
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.read_views import (
    MaterializedReadBinding,
    MaterializedReadCandidate,
    SqlMaterializedReadViewRepository,
)


class SqlMaterializedPageAdapter(MaterializedPagePort):
    """Adapt one request-scoped SQL transaction to the pure platform page port."""

    def __init__(
        self,
        session: Session,
        *,
        cursor_signer: CursorSigner,
        clock: UtcClock,
        page_size: int,
        snapshot_ttl: timedelta,
        max_materialized_items: int,
    ) -> None:
        self._repository = SqlMaterializedReadViewRepository(
            session,
            cursor_signer=cursor_signer,
            clock=clock,
            page_size=page_size,
            snapshot_ttl=snapshot_ttl,
            max_materialized_snapshot_items=max_materialized_items,
        )

    def create(
        self,
        candidates: Sequence[ReadPageCandidate],
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]:
        page = self._repository.create(
            tuple(
                MaterializedReadCandidate(
                    resource_id=item.resource_id,
                    observed_revision=item.observed_revision,
                    canonical_view=item.canonical_view,
                )
                for item in candidates
            ),
            binding=_binding(binding),
        )
        return _project_page(page)

    def page(
        self,
        cursor: PageCursorV1,
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]:
        return _project_page(self._repository.page(cursor, binding=_binding(binding)))


def _binding(value: ReadPageBinding) -> MaterializedReadBinding:
    return MaterializedReadBinding(
        resource_kind=value.resource_kind,
        query_hash=value.query_hash,
        authz_fingerprint=value.authz_fingerprint,
        stable_sort_schema_id=value.stable_sort_schema_id,
        view_schema_id=value.view_schema_id,
        principal_binding=value.principal_binding,
    )


def _project_page(
    page: PageV1[MaterializedReadItemV1],
) -> PageV1[RetainedReadPageItem]:
    return PageV1[RetainedReadPageItem](
        read_snapshot_id=page.read_snapshot_id,
        items=tuple(
            RetainedReadPageItem(
                resource_id=item.resource_id,
                observed_revision=item.observed_revision,
                canonical_view=item.canonical_view,
            )
            for item in page.items
        ),
        next_cursor=page.next_cursor,
        expires_at=page.expires_at,
    )


__all__ = ["SqlMaterializedPageAdapter"]
