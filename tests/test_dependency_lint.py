"""CI dependency-direction gate (contract §1 硬约束)."""
import subprocess
import sys


def test_import_linter_contracts_pass():
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "lint"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_version_constants_present():
    from gameforge.contracts import versions as v

    assert v.IR_SCHEMA_VERSION == "ir-core@1"
    assert v.ENV_CONTRACT_VERSION == "env@1"
    assert v.META_SCHEMA_VERSION == "meta@1"
    assert v.FINDING_SCHEMA_VERSION == "finding@1"
    assert v.PATCH_SCHEMA_VERSION == "patch@1"
    assert v.DSL_GRAMMAR_VERSION == "dsl@1"
