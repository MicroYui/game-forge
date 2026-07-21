from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.apps.api.local import create_local_app
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.contracts.playtest import TaskSuiteV1
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from tests.apps.api.test_local_composition import _config, _seed_and_bootstrap


def _suite_payload(
    label: str,
    *,
    config_artifact_id: str,
    constraint_artifact_id: str,
    environment_profile: ProfileRefV1,
) -> TaskSuiteV1:
    reset_payload = {
        "scenario_id": f"scenario:{label}",
        "config_export_artifact_id": config_artifact_id,
        "quest_ids": [f"quest:{label}"],
        "start_seed": 0,
    }
    return TaskSuiteV1.model_validate(
        {
            "suite_profile": {
                "profile_id": "builtin.task_suite_derivation",
                "version": 2,
            },
            "source_preview_artifact_id": f"preview:{label}",
            "config_export_artifact_id": config_artifact_id,
            "constraint_snapshot_artifact_id": constraint_artifact_id,
            "environment_profile": environment_profile.model_dump(mode="json"),
            "env_contract_version": "generic-agent-env@1",
            "completion_oracle_registry_ref": {
                "registry_version": 1,
                "digest": "5" * 64,
            },
            "episodes": [
                {
                    "episode_id": f"episode:{label}",
                    "scenario_spec_artifact_id": f"scenario-artifact:{label}",
                    "completion_oracle": {
                        "oracle_id": "all-quests-completed",
                        "version": 1,
                        "params_schema_id": "all-quests-completed-params@1",
                        "params": {"quest_ids": [f"quest:{label}"]},
                    },
                    "domain_scope": {"domain_ids": ["builtin"]},
                    "reset_binding": {
                        "reset_schema_id": "generic-env-reset@1",
                        "payload_hash": canonical_sha256(reset_payload),
                        "payload": reset_payload,
                    },
                    "step_budget": 200,
                }
            ],
        }
    )


def _seed_task_suites(app, suites: tuple[TaskSuiteV1, ...]) -> tuple[str, ...]:
    resources = app.state.local_resources
    clock = SystemUtcClock()
    prepared = []
    for suite in suites:
        payload = canonical_json(suite.model_dump(mode="json")).encode("utf-8")
        stored = resources.object_store.put_verified(payload)
        artifact = build_artifact_v2(
            kind="task_suite",
            version_tuple=VersionTuple(
                ir_snapshot_id=f"snapshot:{suite.source_preview_artifact_id}",
                constraint_snapshot_id=(f"snapshot:{suite.constraint_snapshot_artifact_id}"),
                tool_version="task-suite-filter-test@1",
                env_contract_version=suite.env_contract_version,
            ),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "task-suite@1",
                "domain_scope": DomainScope(domain_ids=("builtin",)).model_dump(mode="json"),
            },
        )
        prepared.append((artifact, stored))

    with Session(resources.engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session,
            resources.object_store,
            "local:test",
        )
        artifacts = SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(
                signing_key=b"task-suite-filter-cursor-key",
                clock=clock,
            ),
            clock=clock,
        )
        for artifact, stored in prepared:
            bindings.bind_verified(stored.ref, stored.location, None)
            artifacts.put(artifact)
    return tuple(artifact.artifact_id for artifact, _stored in prepared)


def test_local_task_suite_filters_are_exact_materialized_and_cursor_bound(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'task-suite-filters.db'}"
    _seed_and_bootstrap(database_url)
    app = create_local_app(config=_config(tmp_path, database_url))
    environment = ProfileRefV1(profile_id="builtin.environment", version=1)
    matching = (
        _suite_payload(
            "matching-a",
            config_artifact_id="config:target",
            constraint_artifact_id="constraint:target",
            environment_profile=environment,
        ),
        _suite_payload(
            "matching-b",
            config_artifact_id="config:target",
            constraint_artifact_id="constraint:target",
            environment_profile=environment,
        ),
    )
    unrelated = _suite_payload(
        "unrelated",
        config_artifact_id="config:other",
        constraint_artifact_id="constraint:other",
        environment_profile=ProfileRefV1(
            profile_id="builtin.environment",
            version=2,
        ),
    )
    matching_ids = _seed_task_suites(app, (*matching, unrelated))[:2]
    query = {
        "config_artifact_id": "config:target",
        "constraint_artifact_id": "constraint:target",
        "environment_profile_id": environment.profile_id,
        "environment_profile_version": environment.version,
        "limit": 1,
    }

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        first = client.get("/api/v1/task-suites", params=query)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["next_cursor"] is not None

        crossed = client.get(
            "/api/v1/task-suites",
            params={
                **query,
                "constraint_artifact_id": "constraint:other",
                "cursor": first_body["next_cursor"],
            },
        )

        late = _suite_payload(
            "matching-late",
            config_artifact_id="config:target",
            constraint_artifact_id="constraint:target",
            environment_profile=environment,
        )
        late_id = _seed_task_suites(app, (late,))[0]
        second = client.get(
            "/api/v1/task-suites",
            params={**query, "cursor": first_body["next_cursor"]},
        )
        fresh = client.get("/api/v1/task-suites", params={**query, "limit": 10})

    assert login.status_code == 204
    assert crossed.status_code == 400
    assert crossed.json()["code"] == "invalid_cursor"
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["read_snapshot_id"] == first_body["read_snapshot_id"]
    assert second_body["next_cursor"] is None
    retained_ids = tuple(
        item["artifact"]["artifact_id"] for item in (*first_body["items"], *second_body["items"])
    )
    assert retained_ids == tuple(sorted(matching_ids))

    assert fresh.status_code == 200, fresh.text
    assert tuple(item["artifact"]["artifact_id"] for item in fresh.json()["items"]) == tuple(
        sorted((*matching_ids, late_id))
    )
