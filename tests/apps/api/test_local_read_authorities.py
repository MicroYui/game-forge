"""Focused fail-closed tests for the local production read-domain adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gameforge.apps.api.local_reads import (
    _ArtifactDomainAuthority,
    _ContentPermissionAuthority,
    _RunResultPlaytestSelection,
)
from gameforge.apps.api.run_read_domain import resolve_run_read_domain
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation, QueryTooBroad
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    compute_domain_registry_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV1,
    ArtifactV2,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.jobs import (
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunResultSummaryV1,
    RunResultV1,
)
from gameforge.contracts.playtest import (
    MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES,
    MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES,
    MAX_PLAYTEST_TRACE_JSON_BYTES,
    MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES,
    CompletionOracleRefV1,
    PlaytestEpisodeSeedBindingV1,
    PlaytestEpisodeTraceV1,
    PlaytestExecutionEnvelopeV1,
    PlaytestTraceMarkerV1,
    PlaytestTraceV1,
    bind_exact_playtest_trace_bytes,
)
from gameforge.contracts.seeds import derive_subseed_v1


BUILTIN = DomainScope(domain_ids=("builtin",))
OTHER = DomainScope(domain_ids=("other",))
_HASH = "a" * 64


def _registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="builtin", display_name="Built-in", status="active"),
        DomainDefinitionV1(domain_id="other", display_name="Other", status="active"),
        DomainDefinitionV1(domain_id="retired", display_name="Retired", status="deprecated"),
    )
    version = "local-read-authority@1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _artifact(
    artifact_id: str,
    *,
    scope: DomainScope | None,
    lineage: tuple[str, ...] = (),
    kind: str = "ir_snapshot",
    schema_id: str = "ir-core@1",
) -> ArtifactV1:
    meta = {"payload_schema_id": schema_id}
    if scope is not None:
        meta["domain_scope"] = scope.model_dump(mode="json")
    return ArtifactV1(
        artifact_id=artifact_id,
        kind=kind,
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=list(lineage),
        meta=meta,
    )


class _Artifacts:
    def __init__(self, *artifacts: ArtifactV1 | ArtifactV2) -> None:
        self._values = {artifact.artifact_id: artifact for artifact in artifacts}

    def get(self, artifact_id: str):
        return self._values.get(artifact_id)


class _Payloads:
    def __init__(self, scope: DomainScope = BUILTIN) -> None:
        self._scope = scope

    def load(self, *args: object, **kwargs: object):
        del args, kwargs
        return SimpleNamespace(domain_scope=self._scope)


class _PayloadBindings:
    def __init__(self, schemas: dict[str, str] | None = None) -> None:
        self._schemas = schemas or {}

    def resolve(self, artifact_id: str):
        schema_id = self._schemas.get(artifact_id)
        return None if schema_id is None else SimpleNamespace(payload_schema_id=schema_id)


def _authority(
    *artifacts: ArtifactV1 | ArtifactV2,
    payload_scope: DomainScope = BUILTIN,
    trusted_schemas: dict[str, str] | None = None,
):
    return _ArtifactDomainAuthority(
        artifacts=_Artifacts(*artifacts),  # type: ignore[arg-type]
        registry=_registry(),
        payloads=_Payloads(payload_scope),  # type: ignore[arg-type]
        payload_bindings=_PayloadBindings(trusted_schemas),  # type: ignore[arg-type]
    )


def test_artifact_domain_inherits_exact_parent_scope() -> None:
    parent = _artifact("artifact:parent", scope=BUILTIN)
    child = _artifact("artifact:child", scope=None, lineage=(parent.artifact_id,))

    assert _authority(parent, child).resolve(child) == BUILTIN


def test_modern_explicit_artifact_domain_does_not_scan_historical_lineage() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=("artifact:retained-but-not-needed",),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    assert _authority(artifact).resolve(artifact) == BUILTIN


def test_typed_payload_and_metadata_domain_mismatch_fails_closed() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    with pytest.raises(IntegrityViolation, match="typed payload domains disagree"):
        _authority(
            proposal,
            payload_scope=OTHER,
            trusted_schemas={proposal.artifact_id: "constraint-proposal@1"},
        ).resolve(proposal)


def test_workflow_schema_binding_closes_missing_metadata_domain_seam() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"domain_scope": BUILTIN.model_dump(mode="json")},
    )

    with pytest.raises(IntegrityViolation, match="typed payload domains disagree"):
        _authority(
            proposal,
            payload_scope=OTHER,
            trusted_schemas={proposal.artifact_id: "constraint-proposal@1"},
        ).resolve(proposal)


def test_unbound_constraint_metadata_cannot_select_typed_domain_authority() -> None:
    object_ref = object_ref_for_bytes(b"{}")
    proposal = build_artifact_v2(
        kind="constraint_proposal",
        version_tuple=VersionTuple(tool_version="test@1"),
        lineage=(),
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": "constraint-proposal@1"},
    )

    with pytest.raises(DependencyUnavailable, match="no authoritative"):
        _authority(proposal).resolve(proposal)


def test_artifact_domain_cannot_exceed_lineage_authority() -> None:
    parent = _artifact("artifact:parent", scope=BUILTIN)
    child = _artifact("artifact:child", scope=OTHER, lineage=(parent.artifact_id,))

    with pytest.raises(IntegrityViolation, match="exceeds its lineage authority"):
        _authority(parent, child).resolve(child)


def test_artifact_domain_resolves_deep_lineage_without_recursion() -> None:
    artifacts = [_artifact("artifact:depth:0", scope=BUILTIN)]
    for index in range(1, 900):
        artifacts.append(
            _artifact(
                f"artifact:depth:{index}",
                scope=None,
                lineage=(artifacts[-1].artifact_id,),
            )
        )

    assert _authority(*artifacts).resolve(artifacts[-1]) == BUILTIN


def test_artifact_domain_rejects_lineage_over_public_traversal_bound() -> None:
    artifact = _artifact(
        "artifact:wide",
        scope=BUILTIN,
        lineage=tuple(f"artifact:parent:{index}" for index in range(1_001)),
    )

    with pytest.raises(QueryTooBroad, match="traversal bound"):
        _authority(artifact).resolve(artifact)


def test_artifact_domain_rejects_lineage_cycle() -> None:
    first = _artifact("artifact:cycle:a", scope=None, lineage=("artifact:cycle:b",))
    second = _artifact("artifact:cycle:b", scope=None, lineage=("artifact:cycle:a",))

    with pytest.raises(IntegrityViolation, match="cycle"):
        _authority(first, second).resolve(first)


def test_artifact_domain_rejects_duplicate_legacy_edges() -> None:
    parent = _artifact("artifact:duplicate:parent", scope=BUILTIN)
    root = _artifact(
        "artifact:duplicate:root",
        scope=None,
        lineage=(parent.artifact_id, parent.artifact_id),
    )

    with pytest.raises(IntegrityViolation, match="repeats a parent"):
        _authority(parent, root).resolve(root)


def test_artifact_domain_rejects_over_cap_edges_before_duplicate_projection() -> None:
    parent = _artifact("artifact:over-cap:parent", scope=BUILTIN)
    root = _artifact(
        "artifact:over-cap:root",
        scope=None,
        lineage=(parent.artifact_id,) * 10_001,
    )

    with pytest.raises(QueryTooBroad, match="edge bound"):
        _authority(parent, root).resolve(root)


def test_artifact_domain_rejects_dense_lineage_over_edge_bound() -> None:
    artifacts = [_artifact("artifact:dense:0", scope=BUILTIN)]
    for index in range(1, 150):
        artifacts.append(
            _artifact(
                f"artifact:dense:{index}",
                scope=None,
                lineage=tuple(item.artifact_id for item in artifacts),
            )
        )

    with pytest.raises(QueryTooBroad, match="edge bound"):
        _authority(*artifacts).resolve(artifacts[-1])


def test_artifact_domain_resolves_shared_diamond_once_semantically() -> None:
    base = _artifact("artifact:diamond:base", scope=BUILTIN)
    left = _artifact("artifact:diamond:left", scope=None, lineage=(base.artifact_id,))
    right = _artifact("artifact:diamond:right", scope=None, lineage=(base.artifact_id,))
    root = _artifact(
        "artifact:diamond:root",
        scope=None,
        lineage=(left.artifact_id, right.artifact_id),
    )

    assert _authority(base, left, right, root).resolve(root) == BUILTIN


@pytest.mark.parametrize(
    ("artifact", "message"),
    (
        (_artifact("artifact:missing", scope=None, lineage=("artifact:gone",)), "unavailable"),
        (
            _artifact(
                "artifact:unknown",
                scope=DomainScope(domain_ids=("unknown",)),
            ),
            "unknown domain",
        ),
    ),
)
def test_missing_or_unknown_artifact_domain_authority_fails_closed(
    artifact: ArtifactV1,
    message: str,
) -> None:
    with pytest.raises(IntegrityViolation, match=message):
        _authority(artifact).resolve(artifact)


def test_deprecated_artifact_domain_remains_readable() -> None:
    scope = DomainScope(domain_ids=("retired",))
    artifact = _artifact("artifact:historical", scope=scope)

    assert _authority(artifact).resolve(artifact) == scope


def test_legacy_run_fallback_covers_all_retained_domains() -> None:
    run = SimpleNamespace(
        resource_domain_scope=None,
        payload=SimpleNamespace(params=object()),
    )

    assert resolve_run_read_domain(run, _registry(), None) == DomainScope(
        domain_ids=("builtin", "other", "retired")
    )


class _ApprovalPermissions:
    def __init__(self, permission: Permission | None) -> None:
        self._permission = permission

    def for_artifact(self, *args: object, **kwargs: object) -> Permission:
        del args, kwargs
        if self._permission is None:
            raise DependencyUnavailable("not workflow-bound", component="test")
        return self._permission


class _Domains:
    def __init__(
        self,
        scope: DomainScope | None,
        *,
        failure: Exception | None = None,
        legacy_fallback: bool = False,
    ) -> None:
        self._scope = scope
        self._failure = failure
        self._legacy_fallback = legacy_fallback

    def resolve(self, artifact: ArtifactV1) -> DomainScope:
        del artifact
        if self._failure is not None:
            raise self._failure
        assert self._scope is not None
        return self._scope

    def legacy_workflow_fallback_allowed(self, artifact: ArtifactV1) -> bool:
        del artifact
        return self._legacy_fallback


def _read_permission(scope: DomainScope) -> Permission:
    return Permission(action="read", resource_kind="artifact", domain_scope=scope)


def test_legacy_workflow_artifact_uses_retained_approval_domain() -> None:
    artifact = _artifact("artifact:legacy-evidence", scope=None)
    permission = _read_permission(BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(permission),  # type: ignore[arg-type]
        domains=_Domains(  # type: ignore[arg-type]
            None,
            failure=DependencyUnavailable("legacy lineage", component="test"),
            legacy_fallback=True,
        ),
    )

    assert authority.for_artifact(artifact, resource_kind="artifact") == permission


def test_legacy_typed_metadata_does_not_block_exact_approval_fallback() -> None:
    proposal = _artifact(
        "artifact:legacy-proposal",
        scope=None,
        kind="constraint_proposal",
        schema_id="constraint-proposal@1",
    )
    permission = _read_permission(BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(permission),  # type: ignore[arg-type]
        domains=_authority(proposal),
    )

    assert authority.for_artifact(proposal, resource_kind="artifact") == permission


def test_workflow_and_immutable_artifact_domain_mismatch_fails_closed() -> None:
    artifact = _artifact("artifact:mismatch", scope=OTHER)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(_read_permission(BUILTIN)),  # type: ignore[arg-type]
        domains=_Domains(OTHER),  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrityViolation, match="authorities disagree"):
        authority.for_artifact(artifact, resource_kind="artifact")


def test_non_workflow_artifact_requires_immutable_domain_authority() -> None:
    artifact = _artifact("artifact:standalone", scope=BUILTIN)
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(None),  # type: ignore[arg-type]
        domains=_Domains(BUILTIN),  # type: ignore[arg-type]
    )

    assert authority.for_artifact(artifact, resource_kind="artifact") == _read_permission(BUILTIN)


def test_workflow_fallback_rejects_partial_modern_domain_lineage() -> None:
    legacy_parent = _artifact("artifact:legacy-parent", scope=None)
    root = _artifact(
        "artifact:modern-root",
        scope=OTHER,
        lineage=(legacy_parent.artifact_id,),
    )
    authority = _ContentPermissionAuthority(
        approvals=_ApprovalPermissions(_read_permission(BUILTIN)),  # type: ignore[arg-type]
        domains=_authority(legacy_parent, root),
    )

    with pytest.raises(DependencyUnavailable, match="no authoritative"):
        authority.for_artifact(root, resource_kind="artifact")


def _playtest_trace(
    *,
    run_kind: RunKindRef,
    environment: ProfileRefV1,
    planner: ProfileRefV1,
) -> PlaytestTraceV1:
    suite_id = "artifact:suite"
    episode_id = "episode:1"
    scenario_id = "artifact:scenario"
    case_id = f"{suite_id}:{episode_id}"
    episode_seed = derive_subseed_v1(
        root_seed=7,
        run_kind=run_kind,
        profile=environment,
        case_id=case_id,
        replication_index=0,
    )
    state_hash = f"sha256:{'b' * 64}"
    episode = PlaytestEpisodeTraceV1(
        episode_id=episode_id,
        scenario_spec_artifact_id=scenario_id,
        seed=episode_seed,
        seed_binding=PlaytestEpisodeSeedBindingV1(
            root_seed=7,
            run_kind=run_kind,
            profile=environment,
            case_id=case_id,
            replication_index=0,
            seed=episode_seed,
        ),
        step_budget=1,
        execution_step_limit=1,
        completion_oracle=CompletionOracleRefV1(
            oracle_id="test",
            version=1,
            params_schema_id="test@1",
            params={},
        ),
        completed=False,
        terminal_reason="agent_stopped",
        initial_state_hash=state_hash,
        final_state_hash=state_hash,
        action_trace=(),
        markers=(
            PlaytestTraceMarkerV1(
                kind="failure",
                state_hash=state_hash,
                detail="agent_stopped",
            ),
        ),
    )
    per_episode_upper = min(
        MAX_PLAYTEST_TRACE_JSON_BYTES,
        2 + MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES + 1,
    )
    payload = {
        "config_artifact_id": "artifact:config",
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "task_suite_artifact_id": suite_id,
        "environment_profile": environment.model_dump(mode="json"),
        "planner_policy": planner.model_dump(mode="json"),
        "env_contract_version": "agent-env@1",
        "interaction_mode": "autonomous",
        "seed": 7,
        "requested_max_steps_per_episode": 1,
        "planner_memory_mode": "off",
        "execution_envelope": PlaytestExecutionEnvelopeV1(
            planner_profile_payload_hash=_HASH,
            selected_episode_count=1,
            total_step_limit=1,
            model_call_upper_bound=3,
            total_trace_byte_upper_bound=(
                MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES
                + per_episode_upper
                + MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES
            ),
            actual_model_calls=0,
            total_action_count=0,
            total_action_trace_bytes=2,
            actual_trace_bytes=1,
        ).model_dump(mode="json"),
        "episodes": [episode.model_dump(mode="json")],
    }
    return PlaytestTraceV1.model_validate(bind_exact_playtest_trace_bytes(payload))


def _playtest_selection_fixture(
    *,
    result_attempt: int = 1,
    bind_primary: bool = True,
    bind_input: bool = True,
    primary_scope: DomainScope = BUILTIN,
    invalid_trace_payload: bool = False,
    wrong_trace_config: bool = False,
    replay: bool = False,
    invalid_replay_scope: bool = False,
    extra_manifest_parent: bool = False,
):
    run_kind = RunKindRef(kind="playtest.run", version=1)
    environment = ProfileRefV1(profile_id="environment:test", version=1)
    planner = ProfileRefV1(profile_id="planner:test", version=1)
    trace = _playtest_trace(run_kind=run_kind, environment=environment, planner=planner)
    terminal_tuple = VersionTuple(
        ir_snapshot_id="snapshot:playtest",
        constraint_snapshot_id="constraint:playtest",
        env_contract_version="agent-env@1",
        tool_version="playtest@1",
        seed=7,
    )
    input_tuple = VersionTuple(
        ir_snapshot_id="snapshot:playtest",
        constraint_snapshot_id="constraint:playtest",
        env_contract_version="agent-env@1",
        seed=7,
    )
    trace_payload = trace.model_dump(mode="json")
    if invalid_trace_payload:
        trace_payload = {}
    elif wrong_trace_config:
        trace_payload["config_artifact_id"] = "artifact:wrong-config"
    primary_ref = object_ref_for_bytes(canonical_json(trace_payload).encode("utf-8"))
    trace_input_ids = (
        "artifact:config",
        "artifact:constraint",
        "artifact:scenario",
        "artifact:suite",
    )
    cassette_id = "artifact:cassette" if replay else None
    input_ids = (*trace_input_ids, *((cassette_id,) if cassette_id is not None else ()))
    primary = build_artifact_v2(
        kind="playtest_trace",
        version_tuple=terminal_tuple,
        lineage=trace_input_ids,
        payload_hash=primary_ref.sha256,
        object_ref=primary_ref,
        meta={"payload_schema_id": "playtest-trace@1"},
    )
    result = RunResultV1(
        run_id="run:playtest",
        attempt_no=result_attempt,
        run_kind=run_kind,
        primary_artifact_id=primary.artifact_id,
        produced_artifact_ids=(primary.artifact_id,),
        finding_count=0,
        outcome_code="playtest_completed",
        summary=RunResultSummaryV1(
            outcome_code="playtest_completed",
            primary_artifact_kind="playtest_trace",
            produced_artifact_count=1,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=RunManifestVersionProjectionV1(
            manifest_scope="run",
            attempt_no=result_attempt,
            run_kind=run_kind,
            run_payload_hash=_HASH,
            frozen_input_version_tuple=input_tuple,
            terminal_version_tuple=terminal_tuple,
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="test",
                policy_version=1,
                digest=_HASH,
            ),
            parents=(
                *(
                    (
                        RunManifestParentBindingV1(
                            artifact_id=artifact_id,
                            role="input",
                            publication="existing",
                            cassette_scope=(
                                None
                                if invalid_replay_scope or artifact_id != cassette_id
                                else "replay_input"
                            ),
                        )
                        for artifact_id in input_ids
                    )
                    if bind_input
                    else ()
                ),
                RunManifestParentBindingV1(
                    artifact_id=primary.artifact_id,
                    role="output",
                    publication="run_published",
                ),
                *(
                    (
                        RunManifestParentBindingV1(
                            artifact_id="artifact:extra",
                            role="evidence",
                            publication="existing",
                        ),
                    )
                    if extra_manifest_parent
                    else ()
                ),
            ),
        ),
    )
    manifest_ref = object_ref_for_bytes(result.model_dump_json().encode("utf-8"))
    manifest = build_artifact_v2(
        kind="run_result",
        version_tuple=terminal_tuple,
        lineage=tuple(
            sorted(
                (
                    *(input_ids if bind_input else ()),
                    *((primary.artifact_id,) if bind_primary else ()),
                    *(("artifact:extra",) if extra_manifest_parent else ()),
                )
            )
        ),
        payload_hash=manifest_ref.sha256,
        object_ref=manifest_ref,
        meta={"payload_schema_id": "run-result@1"},
    )
    run = SimpleNamespace(
        run_id="run:playtest",
        kind=run_kind,
        status="succeeded",
        current_attempt_no=1,
        result_artifact_id=manifest.artifact_id,
        payload_hash=_HASH,
        payload=SimpleNamespace(
            version_tuple=input_tuple,
            input_artifact_ids=input_ids,
            cassette_artifact_id=cassette_id,
            llm_execution_mode="replay" if replay else "not_applicable",
            seed=7,
            params=PlaytestRunPayloadV1(
                config_artifact_id="artifact:config",
                constraint_snapshot_artifact_id="artifact:constraint",
                task_suite_artifact_id="artifact:suite",
                episodes=(
                    PlaytestEpisodeBindingV1(
                        episode_id="episode:1",
                        scenario_spec_artifact_id="artifact:scenario",
                    ),
                ),
                environment_profile=environment,
                planner_policy=planner,
                max_steps_per_episode=1,
                interaction_mode="autonomous",
            ),
            resolved_profiles=(
                ResolvedExecutionProfileBindingV1(
                    field_path="/params/planner_policy",
                    profile=planner,
                    expected_profile_kind="playtest_planner",
                    profile_payload_hash=_HASH,
                    catalog_version=1,
                    catalog_digest=_HASH,
                ),
            ),
        ),
        resource_domain_scope=BUILTIN,
    )

    def read_payload(artifact_id: str):
        if artifact_id == manifest.artifact_id:
            return SimpleNamespace(
                artifact=manifest,
                payload_schema_id="run-result@1",
                payload=result.model_dump(mode="json"),
            )
        assert artifact_id == primary.artifact_id
        return SimpleNamespace(
            artifact=primary,
            payload_schema_id="playtest-trace@1",
            payload=trace_payload,
        )

    input_artifacts = tuple(
        _artifact(artifact_id, scope=primary_scope) for artifact_id in input_ids
    )
    payloads = SimpleNamespace(read=read_payload)
    selection = _RunResultPlaytestSelection(
        runs=SimpleNamespace(get=lambda run_id: run),  # type: ignore[arg-type]
        artifacts=_Artifacts(primary, manifest, *input_artifacts),  # type: ignore[arg-type]
        payloads=payloads,  # type: ignore[arg-type]
        domains=_authority(primary, *input_artifacts),
    )
    return selection, primary


def test_playtest_result_selection_closes_exact_run_manifest_authority() -> None:
    selection, primary = _playtest_selection_fixture()

    assert selection.result_artifact_id("run:playtest") == primary.artifact_id

    replay_selection, replay_primary = _playtest_selection_fixture(replay=True)
    assert replay_selection.result_artifact_id("run:playtest") == replay_primary.artifact_id


@pytest.mark.parametrize(
    "fixture_args",
    (
        {"result_attempt": 2},
        {"bind_primary": False},
        {"bind_input": False},
        {"invalid_trace_payload": True},
        {"wrong_trace_config": True},
        {"replay": True, "invalid_replay_scope": True},
        {"extra_manifest_parent": True},
    ),
)
def test_playtest_result_selection_rejects_stale_or_unbound_primary(
    fixture_args: dict[str, object],
) -> None:
    selection, _ = _playtest_selection_fixture(**fixture_args)

    with pytest.raises(IntegrityViolation):
        selection.result_artifact_id("run:playtest")


def test_playtest_result_selection_rejects_cross_domain_primary() -> None:
    selection, _ = _playtest_selection_fixture(primary_scope=OTHER)

    with pytest.raises(IntegrityViolation, match="domains differ"):
        selection.result_artifact_id("run:playtest")
