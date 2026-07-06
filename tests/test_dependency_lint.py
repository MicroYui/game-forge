"""CI dependency-direction gate (contract §1 硬约束).

NOTE: `python -m importlinter.cli lint` is a no-op that exits 0 without running
(the module has no __main__ guard), so we drive the real `lint_imports()` API and
back it with a NEGATIVE test that injects a forbidden import and asserts the gate
trips — otherwise the gate could silently rot to a false-green.
"""

import os

from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports

_SPINE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "gameforge", "spine")


def test_import_linter_contracts_pass():
    assert lint_imports(no_cache=True) == EXIT_STATUS_SUCCESS


def test_dependency_lint_catches_llm_sdk_in_spine():
    """A forbidden import inside spine MUST break the gate (guards against silent rot)."""
    probe = os.path.join(_SPINE_DIR, "_forbidden_import_probe.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("import anthropic  # gate probe: spine must never import an LLM SDK\n")
    try:
        assert lint_imports(no_cache=True) != EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)


def test_version_constants_present():
    from gameforge.contracts import versions as v

    assert v.IR_SCHEMA_VERSION == "ir-core@1"
    assert v.ENV_CONTRACT_VERSION == "env@1"
    assert v.META_SCHEMA_VERSION == "meta@1"
    assert v.FINDING_SCHEMA_VERSION == "finding@1"
    assert v.PATCH_SCHEMA_VERSION == "patch@1"
    assert v.DSL_GRAMMAR_VERSION == "dsl@1"


def test_m0b_version_constants_present():
    from gameforge.contracts import versions as v

    assert v.LINEAGE_SCHEMA_VERSION == "lineage@1"
    assert v.AUDIT_SCHEMA_VERSION == "audit@1"
    assert v.TOOL_VERSION.startswith("gameforge@")


def test_spine_cannot_import_runtime():
    # contract §1: spine → contracts ONLY. Injecting a runtime import must trip the gate.
    import pathlib

    from importlinter.cli import lint_imports

    probe = pathlib.Path("gameforge/spine/ingestion/_probe.py")
    probe.write_text("import gameforge.runtime.persistence  # noqa\n")
    try:
        # lint_imports returns non-zero exit code when a contract is broken
        rc = lint_imports()
        assert rc != 0
    finally:
        probe.unlink()
