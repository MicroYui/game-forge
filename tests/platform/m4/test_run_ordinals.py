from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.jobs import (
    PlaytestProvideInputPayloadV1,
    RunCommandRecordV1,
    RunCommandV1,
    RunFindingLinkV1,
    canonical_payload_hash,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.commands import (
    PromptRenderPublicationRequest,
    RunCommandCapabilities,
)
from gameforge.platform.runs.state import (
    validate_command_binding,
    validate_finding_link_binding,
    validate_run_immutable_bindings,
)
from tests.platform.m4.test_run_create_claim import (
    _HASH_A,
    _HASH_B,
    _create_request,
    _harness,
    RunClaimRequest,
)


def _claim(harness):
    claim = harness.service.claim_next(
        RunClaimRequest(
            worker=AuditActor(
                principal_id="service:worker:1",
                principal_kind="service",
            ),
            lease_id="lease:1",
            lease_duration_ns=30_000_000_000,
        )
    )
    assert claim is not None
    return claim


def _publication_request(
    *,
    artifact_id: str = "artifact:prompt:1",
    request_hash: str = _HASH_A,
    idempotency_key: str = "prompt-call:1",
) -> PromptRenderPublicationRequest:
    return PromptRenderPublicationRequest(
        run_id="run:1",
        attempt_no=1,
        expected_fencing_token=1,
        artifact_id=artifact_id,
        request_hash=request_hash,
        idempotency_scope="run:1/attempt:1",
        idempotency_key=idempotency_key,
    )


def test_prompt_publication_consumes_attempt_head_only_with_the_link() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)

    first = harness.service.publish_prompt_rendered(_publication_request())
    second = harness.service.publish_prompt_rendered(
        _publication_request(
            artifact_id="artifact:prompt:2",
            request_hash=_HASH_B,
            idempotency_key="prompt-call:2",
        )
    )

    assert first.replayed is False
    assert first.link.call_ordinal == 1
    assert second.link.call_ordinal == 2
    attempt = harness.state.attempts[("run:1", 1)]
    assert attempt.next_call_ordinal == 3
    assert tuple(harness.state.intermediate_links) == (
        ("run:1", 1, 1),
        ("run:1", 1, 2),
    )
    assert harness.publication.prompt_publications == [first.link, second.link]


def test_prompt_idempotency_replay_does_not_allocate_a_new_ordinal() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)
    first = harness.service.publish_prompt_rendered(_publication_request())

    replay = harness.service.publish_prompt_rendered(_publication_request())

    assert replay.replayed is True
    assert replay.link == first.link
    assert harness.state.attempts[("run:1", 1)].next_call_ordinal == 2
    assert len(harness.publication.prompt_publications) == 1

    with pytest.raises(Conflict, match="idempotency"):
        harness.service.publish_prompt_rendered(
            _publication_request(artifact_id="artifact:other", request_hash=_HASH_B)
        )
    assert harness.state.attempts[("run:1", 1)].next_call_ordinal == 2


def test_prompt_replay_fails_closed_when_gateway_state_is_detached_from_authority() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)
    first = harness.service.publish_prompt_rendered(_publication_request())
    del harness.state.intermediate_links[("run:1", 1, first.link.call_ordinal)]

    with pytest.raises(IntegrityViolation, match="detached"):
        harness.service.publish_prompt_rendered(_publication_request())

    assert harness.state.attempts[("run:1", 1)].next_call_ordinal == 2
    assert harness.state.intermediate_links == {}
    assert harness.publication.prompt_publications == [first.link]


def test_prompt_replay_requires_the_retained_attempt_head_to_have_consumed_ordinal() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)
    first = harness.service.publish_prompt_rendered(_publication_request())
    attempt = harness.state.attempts[("run:1", 1)]
    harness.state.attempts[("run:1", 1)] = attempt.model_copy(
        update={"next_call_ordinal": first.link.call_ordinal}
    )

    with pytest.raises(IntegrityViolation, match="not consumed"):
        harness.service.publish_prompt_rendered(_publication_request())

    assert harness.publication.prompt_publications == [first.link]


def test_prompt_publication_rejects_wrong_attempt_or_fencing_without_a_hole() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)

    with pytest.raises(IntegrityViolation, match="attempt"):
        harness.service.publish_prompt_rendered(
            _publication_request().model_copy(update={"attempt_no": 2})
        )
    with pytest.raises(Conflict, match="fencing"):
        harness.service.publish_prompt_rendered(
            _publication_request().model_copy(update={"expected_fencing_token": 2})
        )
    assert harness.state.attempts[("run:1", 1)].next_call_ordinal == 1
    assert harness.state.intermediate_links == {}


def test_publication_gateway_is_required_instead_of_a_bare_allocator() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    _claim(harness)
    harness.service._bind_capabilities = (  # type: ignore[method-assign]
        lambda transaction: RunCommandCapabilities(
            runs=harness.publication.repo,
            registry=harness.registry,
            admission=harness.admission,
            publication=None,
        )
    )

    with pytest.raises(IntegrityViolation, match="publication"):
        harness.service.publish_prompt_rendered(_publication_request())
    assert harness.state.attempts[("run:1", 1)].next_call_ordinal == 1


def test_immutable_run_bindings_and_finding_links_are_closed_before_persistence() -> None:
    harness = _harness()
    queued = harness.service.create_run(_create_request()).run
    claim = _claim(harness)
    corrupted = claim.run.model_copy(update={"request_hash": _HASH_B})
    with pytest.raises(IntegrityViolation, match="immutable"):
        validate_run_immutable_bindings(previous=queued, current=corrupted)

    link = RunFindingLinkV1(
        run_id=claim.run.run_id,
        attempt_no=claim.attempt.attempt_no,
        ordinal=1,
        finding_id="finding:1",
        finding_revision=1,
        finding_digest=_HASH_A,
        evidence_artifact_id="artifact:finding-evidence",
    )
    validate_finding_link_binding(run=claim.run, attempt=claim.attempt, link=link)
    with pytest.raises(IntegrityViolation, match="Finding"):
        validate_finding_link_binding(
            run=claim.run,
            attempt=claim.attempt,
            link=link.model_copy(update={"run_id": "run:other"}),
        )


def test_command_binding_uses_the_retained_run_kind_allowlist_and_revision() -> None:
    harness = _harness()
    run = harness.service.create_run(_create_request()).run
    command = RunCommandV1(
        command_id="command:1",
        client_id="browser:1",
        client_seq=1,
        idempotency_key="input:1",
        expected_run_revision=run.revision,
        type="provide_input",
        payload_schema_id="playtest-provide-input@1",
        payload=PlaytestProvideInputPayloadV1(
            interaction_id="interaction:1",
            expected_state_hash=_HASH_A,
            choice_id="choice:a",
        ),
    )
    record = RunCommandRecordV1(
        run_id=run.run_id,
        command=command,
        request_hash=canonical_payload_hash(command),
        actor=AuditActor(principal_id="human:a", principal_kind="human"),
        status="pending",
        revision=1,
        created_at="2026-07-14T12:00:00Z",
    )
    definition = harness.registry.definition.model_copy(
        update={"allowed_command_schema_ids": ("playtest-provide-input@1",)}
    )
    validate_command_binding(run=run, definition=definition, record=record)

    with pytest.raises(InvalidStateTransition, match="stale"):
        validate_command_binding(
            run=run,
            definition=definition,
            record=record.model_copy(
                update={"command": command.model_copy(update={"expected_run_revision": 2})}
            ),
        )
    with pytest.raises(InvalidStateTransition, match="not allowed"):
        validate_command_binding(
            run=run,
            definition=harness.registry.definition,
            record=record,
        )
