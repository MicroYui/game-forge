from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from gameforge.apps.worker.publication import WorkerManifestLedger
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    RunIntermediateArtifactLinkV1,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunToolIntermediateLinkV1,
)
from gameforge.runtime.persistence.models import (
    RunIntermediateArtifactLinkRow,
    RunToolIntermediateLinkRow,
)
from gameforge.runtime.persistence.runs import SqlRunRepository


_RUN_ID = "run:manifest-capacity"
_NOW = "2026-07-17T00:00:00Z"
_HASH = "a" * 64
_MORE_THAN_LEGACY_BOUND = 1_025


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _ListingSession:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self, _query: object) -> _ScalarResult:
        return _ScalarResult(self._rows)

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class _AuthorityListingRepository(SqlRunRepository):
    def __init__(
        self,
        rows: list[object],
        *,
        routes: tuple[RunModelRouteLinkV1, ...] = (),
        consumptions: tuple[RunModelResponseConsumptionV1, ...] = (),
    ) -> None:
        super().__init__(cast(Session, _ListingSession(rows)))
        self._routes = {
            (item.run_id, item.attempt_no, item.call_ordinal, item.route_ordinal): item
            for item in routes
        }
        self._consumptions = {
            (item.run_id, item.attempt_no, item.call_ordinal, item.route_ordinal): item
            for item in consumptions
        }

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None:
        return self._routes.get((run_id, attempt_no, call_ordinal, route_ordinal))

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None:
        return self._consumptions.get((run_id, attempt_no, call_ordinal, route_ordinal))


def _prompt(call_ordinal: int) -> RunIntermediateArtifactLinkV1:
    return RunIntermediateArtifactLinkV1(
        run_id=_RUN_ID,
        attempt_no=1,
        call_ordinal=call_ordinal,
        route_ordinal=1,
        artifact_id=f"artifact:prompt:{call_ordinal}",
        role="prompt_rendered",
        request_hash=_HASH,
        fencing_token=1,
        published_at=_NOW,
    )


def _tool_context(call_ordinal: int) -> RunToolIntermediateLinkV1:
    return RunToolIntermediateLinkV1(
        run_id=_RUN_ID,
        attempt_no=1,
        target_call_ordinal=call_ordinal,
        artifact_id=f"artifact:context:{call_ordinal}",
        agent_node_id="repair",
        prompt_version="repair@1",
        payload_hash=_HASH,
        fencing_token=1,
        published_at=_NOW,
    )


def _route(call_ordinal: int) -> RunModelRouteLinkV1:
    return RunModelRouteLinkV1(
        run_id=_RUN_ID,
        attempt_no=1,
        call_ordinal=call_ordinal,
        route_ordinal=1,
        prompt_artifact_id=f"artifact:prompt:{call_ordinal}",
        request_hash=_HASH,
        routing_decision_kind="native",
        routing_decision_id=f"route:{call_ordinal}",
        fencing_token=1,
        published_at=_NOW,
    )


def _consumption(call_ordinal: int) -> RunModelResponseConsumptionV1:
    return RunModelResponseConsumptionV1(
        run_id=_RUN_ID,
        attempt_no=1,
        call_ordinal=call_ordinal,
        route_ordinal=1,
        execution_source="online",
        reservation_group_id=f"reservation:{call_ordinal}",
        transport_attempt=1,
        response_digest=_HASH,
        consumed_at=_NOW,
    )


def test_runtime_authority_listings_retain_more_than_legacy_bound() -> None:
    prompts = tuple(_prompt(index) for index in range(1, _MORE_THAN_LEGACY_BOUND + 1))
    prompt_rows = [
        RunIntermediateArtifactLinkRow(**item.model_dump(mode="python")) for item in prompts
    ]
    prompt_repository = _AuthorityListingRepository(prompt_rows)
    assert (
        len(prompt_repository.list_prompt_render_links(_RUN_ID, attempt_no=None))
        == _MORE_THAN_LEGACY_BOUND
    )

    contexts = tuple(_tool_context(index) for index in range(1, _MORE_THAN_LEGACY_BOUND + 1))
    context_rows = [
        RunToolIntermediateLinkRow(**item.model_dump(mode="python")) for item in contexts
    ]
    context_repository = _AuthorityListingRepository(context_rows)
    assert (
        len(context_repository.list_tool_intermediate_links(_RUN_ID, attempt_no=None))
        == _MORE_THAN_LEGACY_BOUND
    )

    routes = tuple(_route(index) for index in range(1, _MORE_THAN_LEGACY_BOUND + 1))
    route_rows = [
        SimpleNamespace(
            run_id=item.run_id,
            attempt_no=item.attempt_no,
            call_ordinal=item.call_ordinal,
            route_ordinal=item.route_ordinal,
        )
        for item in routes
    ]
    route_repository = _AuthorityListingRepository(route_rows, routes=routes)
    assert (
        len(route_repository.list_model_route_links(_RUN_ID, attempt_no=None))
        == _MORE_THAN_LEGACY_BOUND
    )

    consumptions = tuple(_consumption(index) for index in range(1, _MORE_THAN_LEGACY_BOUND + 1))
    consumption_rows = [
        SimpleNamespace(
            run_id=item.run_id,
            attempt_no=item.attempt_no,
            call_ordinal=item.call_ordinal,
            route_ordinal=item.route_ordinal,
        )
        for item in consumptions
    ]
    consumption_repository = _AuthorityListingRepository(
        consumption_rows,
        consumptions=consumptions,
    )
    assert (
        len(
            consumption_repository.list_model_response_consumptions(
                _RUN_ID,
                attempt_no=None,
            )
        )
        == _MORE_THAN_LEGACY_BOUND
    )


@pytest.mark.parametrize(
    "method_name",
    [
        "list_prompt_render_links",
        "list_tool_intermediate_links",
        "list_model_route_links",
        "list_model_response_consumptions",
    ],
)
def test_runtime_authority_listing_rejects_limit_above_hard_cap(
    method_name: str,
) -> None:
    repository = _AuthorityListingRepository([])
    method = cast(Any, getattr(repository, method_name))

    with pytest.raises(IntegrityViolation):
        method(
            _RUN_ID,
            attempt_no=None,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS + 1,
        )


class _LedgerRuns:
    def __init__(self) -> None:
        self.limits: list[tuple[str, int]] = []

    def _record(self, name: str, limit: int) -> tuple[()]:
        self.limits.append((name, limit))
        return ()

    def list_prompt_render_links(
        self, _run_id: str, *, attempt_no: int | None, limit: int
    ) -> tuple[()]:
        return self._record("prompt", limit)

    def list_tool_intermediate_links(
        self, _run_id: str, *, attempt_no: int | None, limit: int
    ) -> tuple[()]:
        return self._record("tool", limit)

    def list_model_route_links(
        self, _run_id: str, *, attempt_no: int | None, limit: int
    ) -> tuple[()]:
        return self._record("route", limit)

    def list_model_response_consumptions(
        self, _run_id: str, *, attempt_no: int | None, limit: int
    ) -> tuple[()]:
        return self._record("response", limit)


def test_worker_manifest_ledger_uses_the_shared_hard_cap_for_every_listing() -> None:
    runs = _LedgerRuns()
    ledger = WorkerManifestLedger(runs, object())

    assert ledger.prompt_links(_RUN_ID, attempt_no=None) == ()
    assert ledger.tool_intermediate_links(_RUN_ID, attempt_no=None) == ()
    assert ledger.model_route_links(_RUN_ID, attempt_no=None) == ()
    assert ledger.model_response_consumptions(_RUN_ID, attempt_no=None) == ()

    assert runs.limits == [
        ("prompt", MAX_RUN_MANIFEST_PARENT_BINDINGS),
        ("tool", MAX_RUN_MANIFEST_PARENT_BINDINGS),
        ("route", MAX_RUN_MANIFEST_PARENT_BINDINGS),
        ("response", MAX_RUN_MANIFEST_PARENT_BINDINGS),
    ]
