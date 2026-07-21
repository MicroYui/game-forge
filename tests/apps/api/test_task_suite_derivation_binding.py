from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gameforge.apps.api.local import create_local_app
from tests.apps.api.test_local_composition import _config, _seed_and_bootstrap


def test_local_composition_exposes_complete_task_suite_derivation_binding(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'task-suite-binding.db'}"
    _seed_and_bootstrap(database_url)
    app = create_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        response = client.get(
            "/api/v1/execution-profiles/builtin.task_suite_derivation/versions/2/"
            "task-suite-derivation-binding"
        )

    assert login.status_code == 204
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "binding_schema_version": "task-suite-derivation-binding@1",
        "derivation_profile": {
            "profile_id": "builtin.task_suite_derivation",
            "version": 2,
        },
        "profile_payload_hash": body["profile_payload_hash"],
        "run_kind": {"kind": "task_suite.derive", "version": 1},
        "target_environment_profile": {
            "profile_id": "builtin.environment",
            "version": 1,
        },
        "completion_oracle_registry_ref": {
            "registry_version": 1,
            "digest": body["completion_oracle_registry_ref"]["digest"],
        },
        "max_scenarios": 1024,
        "max_total_prepared_artifact_bytes": 256 * 1024 * 1024,
    }
    assert len(body["profile_payload_hash"]) == 64
    assert len(body["completion_oracle_registry_ref"]["digest"]) == 64
