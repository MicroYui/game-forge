from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).parents[2]


def _python_files(relative: str):
    return sorted((_ROOT / relative).rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_generic_human_evidence_packages_contain_no_game_or_commit_dispatch():
    forbidden_fragments = (
        "endless_sky",
        "flare",
        "evaluate_predicate",
        "before_commit",
        "after_commit",
    )
    files = _python_files("gameforge/bench/hed") + _python_files("gameforge/bench/qa")

    for path in files:
        source = path.read_text(encoding="utf-8").lower()
        for fragment in forbidden_fragments:
            assert fragment not in source, f"{fragment} leaked into {path}"


def test_agents_and_spine_do_not_depend_on_benchmark_or_agent_layers():
    for path in _python_files("gameforge/agents/repair"):
        assert not any(
            name.startswith("gameforge.bench") for name in _imports(path)
        ), path
    for path in _python_files("gameforge/spine"):
        assert not any(
            name.startswith(("gameforge.agents", "gameforge.bench"))
            for name in _imports(path)
        ), path


def test_only_source_composition_imports_harness_and_endless_sky_runtime_together():
    offenders: list[Path] = []
    for path in _python_files("gameforge"):
        imports = _imports(path)
        has_harness = "gameforge.bench.hed.harness" in imports
        has_runtime = (
            "gameforge.bench.external_cases.endless_sky_runner" in imports
        )
        if has_harness and has_runtime:
            offenders.append(path.relative_to(_ROOT))

    assert offenders == [
        Path("gameforge/bench/external_cases/endless_sky_hed.py")
    ]


def test_only_qa_source_composition_imports_qa_harness_and_runtime_together():
    offenders: list[Path] = []
    for path in _python_files("gameforge"):
        imports = _imports(path)
        has_harness = "gameforge.bench.qa.harness" in imports
        has_runtime = (
            "gameforge.bench.external_cases.endless_sky_runner" in imports
        )
        if has_harness and has_runtime:
            offenders.append(path.relative_to(_ROOT))

    assert offenders == [
        Path("gameforge/bench/external_cases/endless_sky_qa.py")
    ]
