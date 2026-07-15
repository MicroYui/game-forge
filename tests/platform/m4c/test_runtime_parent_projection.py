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
from gameforge.platform.publication.publisher import project_runtime_parents
from gameforge.platform.registry.defaults import _runtime_parent_rules


RULES = _runtime_parent_rules()


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
    bindings = project_runtime_parents(
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
    bindings = project_runtime_parents(
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
        project_runtime_parents(
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


def test_record_shard_count_must_equal_prompt_count() -> None:
    with pytest.raises(IntegrityViolation):
        project_runtime_parents(
            rule_set=RULES,
            manifest_scope="attempt",
            llm_execution_mode="record",
            prompt_links=(_prompt(1, 1, "art:prompt:1"), _prompt(1, 2, "art:prompt:2")),
            record_shards=((1, 1, "art:shard:1"),),  # one shard, two prompts
            closed={},
            attempt_bundle_id="art:attempt-bundle",
            run_bundle_id=None,
            replay_input_id=None,
            committed_link_counts={"current_attempt": 2, "all_attempts": 2},
        )


def test_replay_projects_input_cassette_in_both_scopes() -> None:
    for scope in ("attempt", "run"):
        bindings = project_runtime_parents(
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
        project_runtime_parents(
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
    bindings = project_runtime_parents(
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
