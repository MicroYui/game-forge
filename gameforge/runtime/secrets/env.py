"""Read the LLM-gateway key from the environment — never from a committed file."""
from __future__ import annotations

import os


def get_llm_key() -> str:
    key = os.environ.get("GAMEFORGE_LLM_KEY")
    if not key:
        raise RuntimeError(
            "GAMEFORGE_LLM_KEY not set — put it in a gitignored .env; never commit the key."
        )
    return key
