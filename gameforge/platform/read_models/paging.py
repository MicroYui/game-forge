"""Pure platform ports for retained, authorized materialized read pages."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from gameforge.contracts.storage import PageCursorV1, PageV1


_BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
_Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ReadPageBinding(_FrozenModel):
    """Exact query, projection, principal, and current authorization binding."""

    resource_kind: _BoundedText
    query_hash: _Sha256Hex
    authz_fingerprint: _Sha256Hex
    stable_sort_schema_id: _BoundedText
    view_schema_id: _BoundedText
    principal_binding: _Sha256Hex


class ReadPageCandidate(_FrozenModel):
    """One authorized canonical projection in stable source order."""

    resource_id: _BoundedText
    observed_revision: Annotated[int, Field(gt=0)]
    canonical_view: dict[str, JsonValue]


class RetainedReadPageItem(_FrozenModel):
    """One verified item returned by a retained page adapter."""

    resource_id: _BoundedText
    observed_revision: Annotated[int, Field(gt=0)]
    canonical_view: dict[str, JsonValue]


class MaterializedPagePort(Protocol):
    def create(
        self,
        candidates: Sequence[ReadPageCandidate],
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]: ...

    def page(
        self,
        cursor: PageCursorV1,
        *,
        binding: ReadPageBinding,
    ) -> PageV1[RetainedReadPageItem]: ...


MaterializedPageFactory = Callable[[int], MaterializedPagePort]


__all__ = [
    "MaterializedPageFactory",
    "MaterializedPagePort",
    "ReadPageBinding",
    "ReadPageCandidate",
    "RetainedReadPageItem",
]
