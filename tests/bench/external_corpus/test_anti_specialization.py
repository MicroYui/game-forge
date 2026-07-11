from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROFILE_MODULE = "gameforge.bench.external_corpus.profiles"
SOURCE_TOKENS = ("endless_sky", "flare-game", "b10b7d6c")
DETERMINISTIC_CORE = (
    ROOT / "gameforge/contracts",
    ROOT / "gameforge/spine/ir",
    ROOT / "gameforge/spine/dsl",
    ROOT / "gameforge/spine/checkers",
    ROOT / "gameforge/spine/sim",
    ROOT / "gameforge/bench/taxonomy.py",
    ROOT / "gameforge/bench/metrics.py",
    ROOT / "gameforge/bench/report.py",
    ROOT / "gameforge/bench/power.py",
)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.py"))


def _absolute_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def test_deterministic_core_has_no_external_profile_import_or_source_literal() -> None:
    forbidden_imports: list[str] = []
    forbidden_tokens: list[str] = []
    for root in DETERMINISTIC_CORE:
        for path in _python_files(root):
            relative = path.relative_to(ROOT)
            for module in _absolute_imports(path):
                if module == PROFILE_MODULE or module.startswith(f"{PROFILE_MODULE}."):
                    forbidden_imports.append(f"{relative}: {module}")
            source = path.read_text(encoding="utf-8").casefold()
            for token in SOURCE_TOKENS:
                if token.casefold() in source:
                    forbidden_tokens.append(f"{relative}: {token}")

    assert forbidden_imports == [], (
        f"deterministic core imports source profiles: {forbidden_imports}"
    )
    assert forbidden_tokens == [], (
        f"deterministic core contains source-specific tokens: {forbidden_tokens}"
    )
