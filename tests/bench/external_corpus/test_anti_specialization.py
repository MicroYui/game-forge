from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE_MODULES = (
    "gameforge.bench.external_corpus.profiles",
    "gameforge.bench.external_cases.endless_sky_predicates",
    "gameforge.spine.ingestion.endless_sky_adapter",
    "gameforge.spine.ingestion.endless_sky_reader",
)
SOURCE_TOKENS = ("endless_sky", "flare", "b10b7d6c")
DETERMINISTIC_CORE = (
    ROOT / "gameforge/contracts",
    ROOT / "gameforge/spine/ir",
    ROOT / "gameforge/spine/dsl",
    ROOT / "gameforge/spine/checkers",
    ROOT / "gameforge/spine/sim",
    ROOT / "gameforge/spine/patch.py",
    ROOT / "gameforge/spine/stats.py",
    ROOT / "gameforge/bench/taxonomy.py",
    ROOT / "gameforge/bench/metrics.py",
    ROOT / "gameforge/bench/report.py",
    ROOT / "gameforge/bench/power.py",
    ROOT / "gameforge/bench/external_cases/qualify.py",
)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.py"))


def _resolved_imports(path: Path, root: Path = ROOT) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = path.relative_to(root).with_suffix("").parts
    package = ".".join(module_parts[:-1])
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative, package)
            else:
                base = node.module or ""
            if base:
                imports.append(base)
                imports.extend(f"{base}.{alias.name}" for alias in node.names)
    return imports


def test_import_scan_resolves_package_and_relative_profile_imports(tmp_path) -> None:
    module = tmp_path / "gameforge/spine/checkers/probe.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from gameforge.bench.external_corpus import profiles\n"
        "from ...bench.external_corpus import profiles as relative_profiles\n",
        encoding="utf-8",
    )

    imports = _resolved_imports(module, tmp_path)

    assert imports.count(SOURCE_MODULES[0]) == 2


def test_deterministic_core_has_no_external_profile_import_or_source_literal() -> None:
    forbidden_imports: list[str] = []
    forbidden_tokens: list[str] = []
    for root in DETERMINISTIC_CORE:
        for path in _python_files(root):
            relative = path.relative_to(ROOT)
            for module in _resolved_imports(path):
                if any(
                    module == source or module.startswith(f"{source}.")
                    for source in SOURCE_MODULES
                ):
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
