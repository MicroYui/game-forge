"""Clean-process import-order regressions for the terminal publication package."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "imports",
    (
        (
            "from gameforge.platform.runs.commands import RunCommandService\n"
            "from gameforge.platform.publication import TerminalPublisher"
        ),
        (
            "from gameforge.platform.publication import TerminalPublisher\n"
            "from gameforge.platform.runs.lifecycle import RunLifecycleService"
        ),
    ),
)
def test_terminal_publication_public_api_has_no_import_order_dependency(
    imports: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", imports],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
