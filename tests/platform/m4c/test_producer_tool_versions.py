"""Conformance: admission's producer tool_version map matches each handler's constant.

``admission._PRODUCER_TOOL_VERSIONS`` stamps the run's producer ``tool_version`` so the
terminal publisher's ``producer_value`` VersionTuple projection matches the executor's
PRIMARY output Artifact. That map hardcodes strings that MUST stay in lock-step with each
deterministic/agent handler's primary-artifact ``tool_version`` — drift only fails closed
at terminal publish (safe but surprising). This test imports the handler constants and
asserts the map is exactly correct, so drift is caught at test time, not runtime.
"""

from __future__ import annotations

from gameforge.platform.run_handlers.bench import BENCH_TOOL_VERSION
from gameforge.platform.run_handlers.checker import CHECKER_TOOL_VERSION
from gameforge.platform.run_handlers.constraint_proposal import EXTRACTION_TOOL_VERSION
from gameforge.platform.run_handlers.constraint_validation import (
    EVIDENCE_TOOL_VERSION as CONSTRAINT_VALIDATION_TOOL_VERSION,
)
from gameforge.platform.run_handlers.generation import GENERATION_TOOL_VERSION
from gameforge.platform.run_handlers.patch_validation import (
    EVIDENCE_TOOL_VERSION as PATCH_VALIDATION_TOOL_VERSION,
)
from gameforge.platform.run_handlers.playtest import PLAYTEST_TOOL_VERSION
from gameforge.platform.run_handlers.repair import REPAIR_TOOL_VERSION
from gameforge.platform.run_handlers.review import REVIEW_TOOL_VERSION
from gameforge.platform.run_handlers.rollback_validation import (
    VALIDATION_TOOL_VERSION as ROLLBACK_VALIDATION_TOOL_VERSION,
)
from gameforge.platform.run_handlers.simulation import SIMULATION_TOOL_VERSION
from gameforge.platform.run_handlers.task_suite import TASK_SUITE_TOOL_VERSION
from gameforge.platform.runs.admission import _PRODUCER_TOOL_VERSIONS


# payload_schema_version -> the handler's PRIMARY-artifact tool_version constant.
# The validation kinds' PRIMARY artifact is the ``evidence-set@1`` the handler seals;
# admission stamps that producer tool so the terminal publisher can publish it (Task 17b).
_EXPECTED_BY_SCHEMA: dict[str, str] = {
    "checker-run@1": CHECKER_TOOL_VERSION,
    "simulation-run@1": SIMULATION_TOOL_VERSION,
    "task-suite-derive@1": TASK_SUITE_TOOL_VERSION,
    "review-run@1": REVIEW_TOOL_VERSION,
    "bench-run@1": BENCH_TOOL_VERSION,
    "playtest-run@1": PLAYTEST_TOOL_VERSION,
    "generation-propose@1": GENERATION_TOOL_VERSION,
    "constraint-proposal-propose@1": EXTRACTION_TOOL_VERSION,
    "patch-repair@1": REPAIR_TOOL_VERSION,
    "patch-validation@1": PATCH_VALIDATION_TOOL_VERSION,
    "constraint-validation@1": CONSTRAINT_VALIDATION_TOOL_VERSION,
    "rollback-validation@1": ROLLBACK_VALIDATION_TOOL_VERSION,
}


def test_producer_tool_versions_match_handler_constants_exactly() -> None:
    # No missing / extra schema keys: the map covers exactly the generic/resource
    # admission kinds whose PRIMARY artifact tool_version the publisher re-projects.
    assert set(_PRODUCER_TOOL_VERSIONS) == set(_EXPECTED_BY_SCHEMA)
    # Each stamped producer tool equals the handler's primary-artifact constant, so a
    # future handler tool bump that forgets the admission map fails this test.
    for schema, expected in _EXPECTED_BY_SCHEMA.items():
        assert _PRODUCER_TOOL_VERSIONS[schema] == expected, schema
