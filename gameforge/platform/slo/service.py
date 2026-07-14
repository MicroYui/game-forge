"""SLO definition registration across authoritative and telemetry boundaries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.observability import MetricDescriptorRefV1
from gameforge.contracts.slo import SLODefinitionV1


class MetricDescriptorRetainer(Protocol):
    def retain_metric_descriptors(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        descriptor_refs: Sequence[MetricDescriptorRefV1],
        expires_at: datetime | None = None,
    ) -> None: ...


class SLODefinitionRepository(Protocol):
    def put_definition(self, definition: SLODefinitionV1) -> SLODefinitionV1: ...

    def list_live_definitions(self, *, limit: int) -> Sequence[SLODefinitionV1]: ...


class SLODefinitionUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


@dataclass(slots=True)
class SLODefinitionCapabilities:
    definitions: SLODefinitionRepository | None


SLODefinitionCapabilityBinder = Callable[[Any], SLODefinitionCapabilities]
SLO_RETENTION_RECONCILIATION_OWNER_ID = "slo-authority-reconciliation@1"
DEFAULT_SLO_RECONCILIATION_LIMIT = 10_000


class SLODefinitionService:
    """Keep exact descriptor retention aligned with authoritative SLO definitions."""

    def __init__(
        self,
        *,
        descriptor_retainer: MetricDescriptorRetainer,
        unit_of_work: SLODefinitionUnitOfWork,
        bind_capabilities: SLODefinitionCapabilityBinder,
    ) -> None:
        self._descriptor_retainer = descriptor_retainer
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities

    def register(self, definition: SLODefinitionV1) -> SLODefinitionV1:
        refs = _definition_descriptor_refs(definition)
        self._descriptor_retainer.retain_metric_descriptors(
            owner_kind="slo",
            owner_id=definition.slo_id,
            descriptor_refs=refs,
            expires_at=None,
        )

        # A failed authority commit may leave a conservative pin. Releasing it
        # here would create a cross-store compensation race with valid replays.
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            definitions = capabilities.definitions
            if definitions is None:
                raise IntegrityViolation("SLO definition repository capability is unavailable")
            return definitions.put_definition(definition)

    def reconcile_retention(
        self,
        *,
        max_definitions: int = DEFAULT_SLO_RECONCILIATION_LIMIT,
    ) -> int:
        """Re-pin every authoritative v1 definition before retention purge."""

        if isinstance(max_definitions, bool) or max_definitions < 1:
            raise QueryTooBroad("SLO retention reconciliation limit must be positive")
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            definitions = capabilities.definitions
            if definitions is None:
                raise IntegrityViolation("SLO definition repository capability is unavailable")
            live_definitions = tuple(definitions.list_live_definitions(limit=max_definitions))
            if len(live_definitions) > max_definitions:
                raise QueryTooBroad(
                    "SLO retention reconciliation exceeds the configured definition limit",
                    max_definitions=max_definitions,
                )

        if not live_definitions:
            return 0
        self._descriptor_retainer.retain_metric_descriptors(
            owner_kind="slo",
            owner_id=SLO_RETENTION_RECONCILIATION_OWNER_ID,
            descriptor_refs=_definitions_descriptor_refs(live_definitions),
            expires_at=None,
        )
        return len(live_definitions)


def _definition_descriptor_refs(
    definition: SLODefinitionV1,
) -> tuple[MetricDescriptorRefV1, ...]:
    by_key = {
        (
            ref.metric_name,
            ref.descriptor_version,
            ref.descriptor_digest,
        ): ref
        for ref in (definition.sli.eligible.descriptor, definition.sli.good.descriptor)
    }
    return tuple(by_key[key] for key in sorted(by_key))


def _definitions_descriptor_refs(
    definitions: Sequence[SLODefinitionV1],
) -> tuple[MetricDescriptorRefV1, ...]:
    by_key: dict[tuple[str, int, str], MetricDescriptorRefV1] = {}
    for definition in definitions:
        for ref in _definition_descriptor_refs(definition):
            by_key[(ref.metric_name, ref.descriptor_version, ref.descriptor_digest)] = ref
    return tuple(by_key[key] for key in sorted(by_key))


__all__ = [
    "DEFAULT_SLO_RECONCILIATION_LIMIT",
    "MetricDescriptorRetainer",
    "SLODefinitionCapabilities",
    "SLODefinitionCapabilityBinder",
    "SLODefinitionRepository",
    "SLODefinitionService",
    "SLODefinitionUnitOfWork",
    "SLO_RETENTION_RECONCILIATION_OWNER_ID",
]
