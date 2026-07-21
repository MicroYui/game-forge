"""Publish one Journey-A RegressionSuite from an exact browser Finding revision."""

from __future__ import annotations

import argparse
from pathlib import Path
import warnings

if __name__ == "__main__":
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
    )

from sqlalchemy.orm import Session

from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow
from tests.e2e.m4c.test_journey_a import _publish_regression_suite
from tests.e2e.m4d_support.journey_a_live import _retained_harness


def materialize_regression_suite(
    *,
    workspace: Path,
    base_artifact_id: str,
    finding_path: Path,
) -> str:
    """Materialize the suite without starting an API, worker, or network client."""

    finding = FindingRevisionV1.model_validate_json(finding_path.read_text(encoding="utf-8"))
    harness = _retained_harness(workspace)
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            base = session.get(ArtifactRow, base_artifact_id)
            if base is None or base.kind != "ir_snapshot":
                raise ValueError("Journey A regression fixture base must be an IR Snapshot")
            base_version_tuple = VersionTuple.model_validate(base.version_tuple)
    finally:
        engine.dispose()

    return _publish_regression_suite(
        harness,
        base_artifact_id=base_artifact_id,
        base_version_tuple=base_version_tuple,
        target=finding,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--base-artifact-id", required=True)
    parser.add_argument("--finding", required=True, type=Path)
    args = parser.parse_args()
    suite_id = materialize_regression_suite(
        workspace=args.workspace.resolve(),
        base_artifact_id=args.base_artifact_id,
        finding_path=args.finding.resolve(),
    )
    print(suite_id)


if __name__ == "__main__":
    main()
