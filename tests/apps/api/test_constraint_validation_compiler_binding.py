from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gameforge.apps.api.local import create_local_app
from tests.apps.api.test_local_composition import _config, _seed_and_bootstrap


def test_local_composition_exposes_the_exact_builtin_compiler_binding(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'compiler-binding.db'}"
    _seed_and_bootstrap(database_url)
    app = create_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        response = client.get(
            "/api/v1/execution-profiles/builtin.constraint_compiler/versions/1/"
            "constraint-validation-binding"
        )

    assert login.status_code == 204
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "binding_schema_version": "constraint-validation-compiler-binding@1",
        "compiler_profile": {
            "profile_id": "builtin.constraint_compiler",
            "version": 1,
        },
        "profile_payload_hash": body["profile_payload_hash"],
        "run_kind": {"kind": "constraint_proposal.validate", "version": 1},
        "differential_engines": [
            {"engine_id": "clingo", "version": 1},
            {"engine_id": "graph-reference", "version": 1},
            {"engine_id": "numeric-reference", "version": 1},
            {"engine_id": "z3", "version": 1},
        ],
    }
    assert len(body["profile_payload_hash"]) == 64
