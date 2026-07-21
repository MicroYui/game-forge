from __future__ import annotations

import json
from pathlib import Path

from gameforge.contracts.playtest import PlaytestTraceV1


ROOT = Path(__file__).resolve().parents[3]


def test_v1_visual_playtest_trace_fixture_is_a_real_bound_contract() -> None:
    payload = json.loads(
        (ROOT / "web/src/features/visual-foundation/playtest-trace.fixture.json").read_text(
            encoding="utf-8"
        )
    )

    trace = PlaytestTraceV1.model_validate(payload)

    assert trace.playtest_trace_schema_version == "playtest-trace@1"
    assert trace.episodes[0].action_trace[0].action == {
        "kind": "navigate_to",
        "target": "NPC:lincheng",
    }
