"""Prompt registry — every prompt carries a prompt_version (进 request_hash + 版本元组)."""
from __future__ import annotations

_PROMPTS: dict[str, tuple[str, str]] = {}


def register_prompt(name: str, version: str, template: str) -> None:
    _PROMPTS[name] = (version, template)


def get_prompt(name: str) -> tuple[str, str]:
    return _PROMPTS[name]  # KeyError if unregistered — fail loud


def render(name: str, **kwargs) -> tuple[str, str]:
    version, template = _PROMPTS[name]
    return version, template.format(**kwargs)
