"""A project's ref namespace has an owner; the generic draft routes must honour it.

``GameProjectV1`` pins ``content_ref_name`` to ``projects/{project_id}/content/head``
(`gameforge/contracts/projects.py`), and the project's own publish path enforces that
binding. The generic workflow routes never did: ``_patch_draft`` validated
``expected_ref`` against the base Artifact but wrote ``ref_name`` straight into the
target binding unchecked.

The window is every project between creation and its first publish. While the ref does
not exist yet, the apply-side CAS compares ``refs.get(ref_name)`` (None) against
``expected_ref`` (None), they agree, and a draft built from ANY snapshot is accepted
into ANY project's namespace. Reproduced against a real running stack before this test
was written: HTTP 201, with a pending approval bound to the other project's ref.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import require_actor
from gameforge.apps.api.local import build_local_api_resources
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.contracts.projects import GameProjectV1
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.projects import SqlProjectRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.ir.snapshot import Snapshot
from tests.apps.api.test_local_workflow_composition import (
    _config,
    _headers,
    _maker_actor,
    _seed_base,
    _seed_local_governance,
)

_OWNER = "project:owner"
_STRANGER = "project:stranger"


def _seed_own_snapshot(resources, config, clock, marker: str) -> str:
    """A second, distinct ir_snapshot — production gives each project its own bootstrap."""

    snapshot = Snapshot.from_entities_relations(
        [
            Entity(
                id=f"npc:{marker}",
                type=NodeType.NPC,
                attrs={"name": marker},
            )
        ],
        [],
    )
    payload = canonical_json(snapshot.content_payload).encode("utf-8")
    stored = resources.object_store.put_verified(payload)
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id=snapshot.snapshot_id, tool_version="own@1"),
        lineage=(),
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": "ir-core@1",
            "domain_scope": DomainScope(domain_ids=("builtin",)).model_dump(mode="json"),
        },
        created_at="2026-07-28T00:00:00Z",
    )
    signer = CursorSigner(signing_key=b"ref-ownership-seed-cursor-key-00", clock=clock)
    with Session(resources.engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(
            session, resources.object_store, config.object_store_id
        )
        bindings.bind_verified(stored.ref, stored.location, None)
        SqlArtifactRepository(
            session, binding_repository=bindings, cursor_signer=signer, clock=clock
        ).put(artifact)
    return artifact.artifact_id


def _seed_project(engine, project_id: str, bootstrap_artifact_id: str) -> str:
    """One project head whose content ref does not exist yet — the vulnerable window."""

    content_ref_name = f"projects/{project_id}/content/head"
    with Session(engine) as session, session.begin():
        SqlProjectRepository(session).create_project(
            GameProjectV1(
                project_id=project_id,
                project_key=project_id.removeprefix("project:"),
                display_name=project_id,
                status="draft",
                domain_scope=DomainScope(domain_ids=("builtin",)),
                bootstrap_snapshot_artifact_id=bootstrap_artifact_id,
                content_ref_name=content_ref_name,
                constraint_ref_name=f"projects/{project_id}/constraints/head",
                created_by="human:maker",
                created_at="2026-07-28T00:00:00Z",
                updated_at="2026-07-28T00:00:00Z",
                revision=1,
            )
        )
    return content_ref_name


def _draft(
    client: TestClient,
    *,
    base_artifact_id: str,
    ref_name: str,
    key: str,
    expected_ref: dict | None = None,
):
    return client.post(
        "/api/v1/patches",
        json={
            "request_schema_version": "human-patch-draft-request@1",
            "base_snapshot_artifact_id": base_artifact_id,
            "constraint_snapshot_artifact_id": None,
            "ref_name": ref_name,
            "expected_ref": expected_ref,
            "expected_to_fix": [],
            "preconditions": [],
            "side_effect_risk": "low",
            "ops": [
                {
                    "op_id": f"op:{key}",
                    "op": "set_entity_attr",
                    "target": "q:1.reward_gold",
                    "old_value": 120,
                    "new_value": 80,
                }
            ],
            "rationale": f"namespace ownership probe {key}",
            "candidate_export_profiles": [],
        },
        headers=_headers(f"ref-ownership:{key}"),
    )


def test_a_draft_may_not_claim_another_project_s_unborn_content_ref(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'ref-ownership.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _seed_local_governance(engine, clock)
    engine.dispose()

    config = _config(tmp_path, database_url)
    resources = build_local_api_resources(config)
    base_artifact_id, _ = _seed_base(resources, config, clock)
    # Each project owns its own bootstrap, as production does. The drafter holds the
    # OWNER's content and aims it at the STRANGER's namespace.
    stranger_bootstrap = _seed_own_snapshot(resources, config, clock, "stranger")
    stranger_ref = _seed_project(resources.engine, _STRANGER, stranger_bootstrap)
    _seed_project(resources.engine, _OWNER, base_artifact_id)

    app = create_app(resources.dependencies)
    app.dependency_overrides[require_actor] = lambda: _maker_actor(resources.engine, clock)

    with TestClient(app, base_url="https://gameforge.test") as client:
        claimed = _draft(
            client,
            base_artifact_id=base_artifact_id,
            ref_name=stranger_ref,
            key="stranger",
        )
        approvals = client.get("/api/v1/approvals")

    assert claimed.status_code == 409, claimed.text
    # Nothing may be left bound to a namespace the drafter does not own.
    assert approvals.status_code == 200, approvals.text
    bound = [
        item
        for item in approvals.json()["items"]
        if (item["approval"].get("target_binding") or {}).get("ref_name") == stranger_ref
    ]
    assert bound == []


def test_a_draft_onto_an_unowned_ref_still_works(tmp_path: Path) -> None:
    """The check must not cost the ordinary case: a ref no project claims is fine."""

    database_url = f"sqlite:///{tmp_path / 'ref-ownership-open.db'}"
    clock = SystemUtcClock()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _seed_local_governance(engine, clock)
    engine.dispose()

    config = _config(tmp_path, database_url)
    resources = build_local_api_resources(config)
    base_artifact_id, base_ref = _seed_base(resources, config, clock)

    app = create_app(resources.dependencies)
    app.dependency_overrides[require_actor] = lambda: _maker_actor(resources.engine, clock)

    with TestClient(app, base_url="https://gameforge.test") as client:
        drafted = _draft(
            client,
            base_artifact_id=base_artifact_id,
            ref_name="content/head",
            key="open",
            expected_ref=base_ref,
        )

    assert drafted.status_code == 201, drafted.text
