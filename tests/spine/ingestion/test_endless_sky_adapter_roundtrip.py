from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.spine.ingestion.endless_sky_adapter import (
    EndlessSkyContext,
    EndlessSkyResource,
    EndlessSkyTarget,
    EndlessSkyTxtAdapter,
)
from gameforge.spine.ingestion.endless_sky_reader import (
    read_source_tree,
    render_source_tree,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
REGISTRATION = load_case_specs(CORPUS / "case-specs.json")
CASE_SIDES = [
    (spec, side)
    for spec in REGISTRATION.cases
    for side in ("before", "after")
]


@pytest.mark.parametrize(
    ("spec", "side"),
    CASE_SIDES,
    ids=[f"{spec.case_id}-{side}" for spec, side in CASE_SIDES],
)
def test_every_external_tree_round_trips_byte_exact(spec, side: str) -> None:
    case_root = CORPUS / "cases" / spec.case_id
    side_root = case_root / side
    source = {
        path: (side_root / path).read_bytes()
        for path in spec.changed_paths
    }
    tree = read_source_tree(source)
    context_payload = json.loads((case_root / "context.json").read_bytes())
    context = EndlessSkyContext(
        resources=tuple(
            EndlessSkyResource(kind=item["kind"], name=item["name"])
            for item in context_payload["resources"]
        ),
        restricted_destinations=tuple(context_payload["restricted_destinations"]),
    )
    targets = tuple(
        EndlessSkyTarget(
            path=target.path,
            record_kind=target.record_kind,
            record_name=target.record_name,
        )
        for target in spec.target_locators
    )

    snapshot = EndlessSkyTxtAdapter().to_ir(
        tree,
        targets=targets,
        context=context,
    )

    assert EndlessSkyTxtAdapter().from_ir(snapshot) == render_source_tree(tree)
