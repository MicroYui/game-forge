"""Composition bridges from pure read-model page ports to SQLite adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
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
        snapshot_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> None:
        self._session = session
        self._cursor_signer = cursor_signer
        self._clock = clock
        self._page_size = page_size
        self._snapshot_ttl = snapshot_ttl
        self._max_materialized_items = max_materialized_items
        self._snapshot_session_factory = snapshot_session_factory

    def _repository(self, session: Session) -> SqlMaterializedReadViewRepository:
        return SqlMaterializedReadViewRepository(
            session,
            cursor_signer=self._cursor_signer,
            clock=self._clock,
            page_size=self._page_size,
            snapshot_ttl=self._snapshot_ttl,
            max_materialized_snapshot_items=self._max_materialized_items,
        )

    def create(
        self,
        candidates: Sequence[ReadPageCandidate],
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]:
        exact_candidates = tuple(
            MaterializedReadCandidate(
                resource_id=item.resource_id,
                observed_revision=item.observed_revision,
                canonical_view=item.canonical_view,
            )
            for item in candidates
        )
        if self._snapshot_session_factory is None:
            page = self._repository(self._session).create(
                exact_candidates,
                binding=_binding(binding),
            )
        else:
            with self._snapshot_session_factory() as session:
                page = self._repository(session).create(
                    exact_candidates,
                    binding=_binding(binding),
                )
        return _project_page(page)

    def page(
        self,
        cursor: PageCursorV1,
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]:
        return _project_page(
            self._repository(self._session).page(cursor, binding=_binding(binding))
        )


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
