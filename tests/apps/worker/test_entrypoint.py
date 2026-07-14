from __future__ import annotations

import pytest

from gameforge.apps.worker.__main__ import main


def test_worker_entrypoint_is_explicitly_unconfigured_before_composition() -> None:
    with pytest.raises(RuntimeError, match="not configured"):
        main()
