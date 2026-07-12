from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
SOURCE_IMPORTS = (
    "gameforge.bench.external_corpus.profiles",
    "gameforge.bench.external_cases.endless_sky_fixture",
    "gameforge.bench.external_cases.endless_sky_predicates",
    "gameforge.bench.external_cases.endless_sky_runner",
    "gameforge.spine.ingestion.endless_sky_adapter",
    "gameforge.spine.ingestion.endless_sky_reader",
)
CORE_PATHS = (
    ROOT / "gameforge/contracts",
    ROOT / "gameforge/spine/checkers",
    ROOT / "gameforge/spine/dsl",
    ROOT / "gameforge/spine/sim",
    ROOT / "gameforge/bench/taxonomy.py",
    ROOT / "gameforge/bench/metrics.py",
    ROOT / "gameforge/bench/report.py",
    ROOT / "gameforge/bench/power.py",
    ROOT / "gameforge/bench/external_cases/qualify.py",
)
_DATA_PATH = re.compile(r"(?:^|[^A-Za-z0-9_])data/[A-Za-z0-9_. /-]+")
_SOURCE_IDENTIFIERS = {"source", "source_id", "source_name", "game_id"}
_DEFECT_IDENTIFIERS = {"defect", "defect_class", "taxonomy_class"}


def _frozen_literals() -> tuple[str, ...]:
    registration = json.loads((CORPUS / "case-specs.json").read_bytes())
    values = {registration["source_id"], registration["pinned_head"][:8]}
    for case in registration["cases"]:
        values.add(case["before_commit"][:8])
        values.add(case["after_commit"][:8])
        values.update(target["record_name"] for target in case["target_locators"])
    return tuple(sorted(values, key=str.casefold))


FROZEN_LITERALS = _frozen_literals()


def _python_files(path: Path) -> list[Path]:
    return [path] if path.is_file() else sorted(path.rglob("*.py"))


def _resolved_imports(tree: ast.AST, path: Path, root: Path) -> list[str]:
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


def _identifier(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if isinstance(node, ast.Attribute):
        return node.attr.casefold()
    return None


def _source_dispatches(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = (node.left, *node.comparators)
            identifiers = {
                identifier
                for operand in operands
                if (identifier := _identifier(operand)) is not None
            }
            has_literal = any(
                isinstance(operand, ast.Constant) and isinstance(operand.value, str)
                for operand in operands
            )
            if (
                identifiers & _SOURCE_IDENTIFIERS
                and (has_literal or identifiers & _DEFECT_IDENTIFIERS)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Match):
            identifier = _identifier(node.subject)
            if identifier in _SOURCE_IDENTIFIERS:
                lines.append(node.lineno)
    return lines


def scan_source_neutral_core(
    paths: tuple[Path, ...],
    *,
    root: Path = ROOT,
) -> list[str]:
    violations: list[str] = []
    for core_path in paths:
        for path in _python_files(core_path):
            relative = path.relative_to(root)
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for module in _resolved_imports(tree, path, root):
                if any(
                    module == forbidden or module.startswith(f"{forbidden}.")
                    for forbidden in SOURCE_IMPORTS
                ):
                    violations.append(f"{relative}: source import {module}")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                value = node.value
                for literal in FROZEN_LITERALS:
                    if literal.casefold() in value.casefold():
                        violations.append(
                            f"{relative}:{node.lineno}: frozen literal {literal}"
                        )
                if _DATA_PATH.search(value):
                    violations.append(f"{relative}:{node.lineno}: source data path")
            for line in _source_dispatches(tree):
                violations.append(f"{relative}:{line}: source-specific dispatch")
    return sorted(set(violations))


def test_scanner_rejects_a_source_branch_probe(tmp_path) -> None:
    probe = tmp_path / "gameforge/bench/qualify_probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        'def qualify(source_id, defect_class):\n'
        '    if source_id == "endless_sky":\n'
        '        return defect_class == "cyclic_dependency"\n'
        '    return False\n',
        encoding="utf-8",
    )

    violations = scan_source_neutral_core((probe,), root=tmp_path)

    assert any("frozen literal" in violation for violation in violations)
    assert any("source-specific dispatch" in violation for violation in violations)


def test_source_neutral_core_contains_no_frozen_case_specialization() -> None:
    assert scan_source_neutral_core(CORE_PATHS) == []
