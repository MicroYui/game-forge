"""CI dependency-direction gate (contract §1 硬约束).

NOTE: `python -m importlinter.cli lint` is a no-op that exits 0 without running
(the module has no __main__ guard), so we drive the real `lint_imports()` API and
back it with a NEGATIVE test that injects a forbidden import and asserts the gate
trips — otherwise the gate could silently rot to a false-green.
"""

import ast
import os
import pathlib
import sys

import pytest
from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports

_SPINE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "gameforge", "spine")

# ─── contract §1 as an ALLOWLIST, not a denylist ───────────────────────────
# Hard rule 4: `spine → contracts` ONLY, and spine imports NO LLM SDK. The
# import-linter `forbidden` contract can only ban names someone remembered to
# enumerate, so a newly-added first-party package or an unlisted LLM SDK
# (cohere/mistralai/groq/litellm/…) sails through green. This inverts it:
# every spine import must be a subset of {gameforge.contracts(.*), a vetted set
# of non-LLM externals, the stdlib}. Anything else is a violation — including
# names no denylist ever mentioned.
_SPINE_ALLOWED_EXTERNAL = frozenset({"clingo", "z3", "pydantic", "yaml"})
_STDLIB = frozenset(sys.stdlib_module_names)


def _spine_import_violations() -> list[str]:
    """Every spine import that is NOT stdlib, NOT gameforge.contracts(.*), NOT spine's
    own package, and NOT an allowlisted non-LLM external. Returns ``"<file>: <module>"``
    strings. Relative imports (``from . import x``) are spine-internal and skipped."""
    violations: list[str] = []
    for path in sorted(pathlib.Path(_SPINE_DIR).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                modules.append(node.module or "")
        for mod in modules:
            if not mod:
                continue
            top = mod.split(".", 1)[0]
            if top == "gameforge":
                allowed = (
                    mod == "gameforge.spine"
                    or mod.startswith("gameforge.spine.")
                    or mod == "gameforge.contracts"
                    or mod.startswith("gameforge.contracts.")
                )
                if not allowed:
                    violations.append(f"{path.name}: {mod}")
            elif top in _STDLIB or top in _SPINE_ALLOWED_EXTERNAL:
                continue
            else:
                violations.append(f"{path.name}: {mod}")
    return violations


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
    probe = pathlib.Path("gameforge/spine/ingestion/_probe.py")
    probe.write_text("import gameforge.runtime.persistence  # noqa\n")
    try:
        # lint_imports returns non-zero exit code when a contract is broken
        rc = lint_imports(no_cache=True)
        assert rc != 0
    finally:
        probe.unlink()


def test_spine_imports_are_allowlisted():
    """Contract §1 as an allowlist: every spine import is stdlib, gameforge.contracts,
    or a vetted non-LLM external. Catches any new first-party package or LLM SDK a
    denylist would miss. This is the whitelist that import-linter cannot express for
    external packages."""
    violations = _spine_import_violations()
    assert violations == [], f"spine imports outside the allowlist: {violations}"


@pytest.mark.parametrize(
    "sdk",
    ["cohere", "mistralai", "groq", "litellm", "google.generativeai", "ollama"],
)
def test_unlisted_llm_sdk_in_spine_is_rejected(sdk):
    """An LLM SDK NOT among the original five denylist entries must STILL be caught —
    the whole reason an allowlist beats a denylist (hard rule 4: forbid ANY LLM SDK)."""
    probe = os.path.join(_SPINE_DIR, "_allowlist_probe.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write(f"import {sdk}  # probe: spine must import no LLM SDK\n")
    try:
        assert any("_allowlist_probe" in v for v in _spine_import_violations())
    finally:
        os.remove(probe)


def test_new_first_party_package_in_spine_is_rejected():
    """spine → contracts ONLY: importing any other gameforge package — even a brand-new
    one no denylist enumerates — must trip the allowlist."""
    probe = os.path.join(_SPINE_DIR, "_allowlist_probe_fp.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("import gameforge.agents  # probe: spine may not import siblings\n")
    try:
        assert any("_allowlist_probe_fp" in v for v in _spine_import_violations())
    finally:
        os.remove(probe)


@pytest.mark.parametrize("sdk", ["cohere", "mistralai", "groq", "litellm"])
def test_import_linter_denylist_covers_broad_llm_sdks(sdk):
    """Belt to the allowlist test's braces: the `uv run lint-imports` CI gate itself must
    trip on common LLM SDKs beyond the original five, so a spine LLM import fails the
    dependency gate directly (not only the allowlist unit test)."""
    probe = os.path.join(_SPINE_DIR, "_denylist_probe.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write(f"import {sdk}\n")
    try:
        assert lint_imports(no_cache=True) != EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)


@pytest.mark.parametrize("sdk", ["openai", "boto3", "vertexai", "cohere"])
def test_llm_sdk_only_allowed_outside_model_router_is_rejected(sdk):
    # Any LLM/cloud SDK imported outside runtime.model_router (here: agents) must trip the gate.
    probe = os.path.join(os.path.dirname(__file__), os.pardir, "gameforge", "agents", "_sdk_probe.py")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write(f"import {sdk}  # probe: LLM SDK only allowed in runtime.model_router\n")
    try:
        assert lint_imports(no_cache=True) != EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)


def test_model_router_may_import_openai():
    # The one allowed home for the SDK — a probe there must NOT trip the gate.
    probe = os.path.join(
        os.path.dirname(__file__), os.pardir,
        "gameforge", "runtime", "model_router", "_sdk_ok_probe.py",
    )
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("import openai  # allowed here\n")
    try:
        assert lint_imports(no_cache=True) == EXIT_STATUS_SUCCESS
    finally:
        os.remove(probe)
