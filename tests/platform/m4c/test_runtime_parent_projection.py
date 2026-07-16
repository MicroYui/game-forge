"""Seam #4 — RECORD/REPLAY runtime-parent projection (M4c Task 10).

Task 9's terminal publisher projected only ``published_intermediate`` (prompt
renders) and ``closed_attempt_failure`` runtime parents, so RECORD/REPLAY Runs
failed closed at ``validate_runtime_parents``. Task 10 wires RECORD response
capture + attempt/run cassette bundles, so the publisher must also project
``record_shard``, ``attempt_bundle``, ``run_bundle`` and ``replay_input`` parents.
These exercise the pure projection helper against the *real*
``_runtime_parent_rules()`` rule set for every mode.
"""

from __future__ import annotations

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import RunIntermediateArtifactLinkV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.publication.lineage import ParentInfo
from gameforge.platform.publication.publisher import project_runtime_parents
from gameforge.platform.registry.defaults import _runtime_parent_rules


RULES = _runtime_parent_rules()


def _project(**kwargs):
    prompt_links = kwargs.get("prompt_links", ())
    record_shards = kwargs.get("record_shards", ())
    closed = kwargs.get("closed", {})
    infos = {
        link.artifact_id: ParentInfo(
            artifact_id=link.artifact_id,
            kind="source_rendered",
            payload_schema_id="source-rendered@1",
            version_tuple=VersionTuple(),
        )
        for link in prompt_links
    }
    infos.update(
        {
            artifact_id: ParentInfo(
                artifact_id=artifact_id,
                kind="cassette_bundle",
                payload_schema_id="cassette-record-shard@1",
                version_tuple=VersionTuple(),
            )
            for _, _, artifact_id in record_shards
        }
    )
    infos.update(
        {
            artifact_id: ParentInfo(
                artifact_id=artifact_id,
                kind="run_failure",
                payload_schema_id="run-failure@1",
                version_tuple=VersionTuple(),
            )
            for artifact_id in closed
        }
    )
    for key in ("attempt_bundle_id", "run_bundle_id", "replay_input_id"):
        artifact_id = kwargs.get(key)
        if artifact_id is not None:
            infos[artifact_id] = ParentInfo(
                artifact_id=artifact_id,
                kind="cassette_bundle",
                payload_schema_id="cassette-bundle@1",
                version_tuple=VersionTuple(),
            )
    kwargs.setdefault(
        "consumed_response_call_keys",
        frozenset((attempt_no, ordinal) for attempt_no, ordinal, _ in record_shards),
    )
    return project_runtime_parents(
        **kwargs,
        run_id="run:1",
        current_attempt_no=1,
        artifact_info_by_id=infos,
    )


def _prompt(attempt_no: int, call_ordinal: int, artifact_id: str) -> RunIntermediateArtifactLinkV1:
    return RunIntermediateArtifactLinkV1(
        run_id="run:1",
        attempt_no=attempt_no,
        call_ordinal=call_ordinal,
        artifact_id=artifact_id,
        role="prompt_rendered",
        request_hash="a" * 64,
        fencing_token=1,
        published_at="2026-07-14T12:00:00Z",
    )


def test_record_attempt_scope_projects_shards_and_attempt_bundle() -> None:
    bindings = _project(
        rule_set=RULES,
        manifest_scope="attempt",
        llm_execution_mode="record",
        prompt_links=(_prompt(1, 1, "art:prompt:1"), _prompt(1, 2, "art:prompt:2")),
        record_shards=((1, 1, "art:shard:1"), (1, 2, "art:shard:2")),
        closed={},
        attempt_bundle_id="art:attempt-bundle",
        run_bundle_id=None,
        replay_input_id=None,
        committed_link_counts={"current_attempt": 2, "all_attempts": 2},
    )
    scopes = sorted(b.cassette_scope for b in bindings if b.cassette_scope is not None)
    assert scopes == ["attempt_bundle", "record_shard", "record_shard"]
    # The attempt bundle is a published intermediate; shards carry their ordinals.
    bundle = next(b for b in bindings if b.cassette_scope == "attempt_bundle")
    assert bundle.role == "intermediate" and bundle.publication == "run_published"
    shard_ordinals = sorted(b.ordinal for b in bindings if b.cassette_scope == "record_shard")
    assert shard_ordinals == [1, 2]


def test_record_run_scope_projects_all_shards_run_bundle_and_closed_failures() -> None:
    bindings = _project(
        rule_set=RULES,
        manifest_scope="run",
        llm_execution_mode="record",
        prompt_links=(_prompt(1, 1, "art:prompt:1"),),
        record_shards=((1, 1, "art:shard:1"),),
        closed={"art:prior-failure": 1},
        attempt_bundle_id=None,
        run_bundle_id="art:run-bundle",
        replay_input_id=None,
        committed_link_counts={"current_attempt": 1, "all_attempts": 1},
    )
    scopes = sorted(b.cassette_scope for b in bindings if b.cassette_scope is not None)
    assert scopes == ["record_shard", "run_bundle"]
    assert any(b.artifact_id == "art:prior-failure" for b in bindings)


def test_record_run_scope_missing_run_bundle_fails_closed() -> None:
    with pytest.raises(IntegrityViolation):
        _project(
            rule_set=RULES,
            manifest_scope="run",
            llm_execution_mode="record",
            prompt_links=(_prompt(1, 1, "art:prompt:1"),),
            record_shards=((1, 1, "art:shard:1"),),
            closed={},
            attempt_bundle_id=None,
            run_bundle_id=None,  # record run manifest requires exactly one run bundle
            replay_input_id=None,
            committed_link_counts={"current_attempt": 1, "all_attempts": 1},
        )


def test_record_prompt_without_consumed_response_needs_no_shard() -> None:
    bindings = _project(
        rule_set=RULES,
        manifest_scope="attempt",
        llm_execution_mode="record",
        prompt_links=(_prompt(1, 1, "art:prompt:1"), _prompt(1, 2, "art:prompt:2")),
        record_shards=((1, 1, "art:shard:1"),),
        consumed_response_call_keys=frozenset({(1, 1)}),
        closed={},
        attempt_bundle_id="art:attempt-bundle",
        run_bundle_id=None,
        replay_input_id=None,
        committed_link_counts={"current_attempt": 2, "all_attempts": 2},
    )
    assert [
        binding.ordinal for binding in bindings if binding.cassette_scope == "record_shard"
    ] == [1]


def test_record_shards_must_exactly_match_consumed_response_calls() -> None:
    with pytest.raises(IntegrityViolation, match="consumed responses"):
        _project(
            rule_set=RULES,
            manifest_scope="attempt",
            llm_execution_mode="record",
            prompt_links=(_prompt(1, 1, "art:prompt:1"), _prompt(1, 2, "art:prompt:2")),
            record_shards=((1, 1, "art:shard:1"),),
            consumed_response_call_keys=frozenset({(1, 2)}),
            closed={},
            attempt_bundle_id="art:attempt-bundle",
            run_bundle_id=None,
            replay_input_id=None,
            committed_link_counts={"current_attempt": 2, "all_attempts": 2},
        )


def test_replay_projects_input_cassette_in_both_scopes() -> None:
    for scope in ("attempt", "run"):
        bindings = _project(
            rule_set=RULES,
            manifest_scope=scope,
            llm_execution_mode="replay",
            prompt_links=(_prompt(1, 1, "art:prompt:1"),),
            record_shards=(),
            closed={},
            attempt_bundle_id=None,
            run_bundle_id=None,
            replay_input_id="art:replay-input",
            committed_link_counts={"current_attempt": 1, "all_attempts": 1},
        )
        replay = next(b for b in bindings if b.cassette_scope == "replay_input")
        assert replay.role == "input" and replay.publication == "existing"


def test_replay_missing_input_cassette_fails_closed() -> None:
    with pytest.raises(IntegrityViolation):
        _project(
            rule_set=RULES,
            manifest_scope="run",
            llm_execution_mode="replay",
            prompt_links=(),
            record_shards=(),
            closed={},
            attempt_bundle_id=None,
            run_bundle_id=None,
            replay_input_id=None,  # replay requires exactly one input cassette
            committed_link_counts={"current_attempt": 0, "all_attempts": 0},
        )


def test_not_applicable_projects_no_cassette_parents() -> None:
    bindings = _project(
        rule_set=RULES,
        manifest_scope="run",
        llm_execution_mode="not_applicable",
        prompt_links=(),
        record_shards=(),
        closed={},
        attempt_bundle_id=None,
        run_bundle_id=None,
        replay_input_id=None,
        committed_link_counts={"current_attempt": 0, "all_attempts": 0},
    )
    assert all(b.cassette_scope is None for b in bindings)
