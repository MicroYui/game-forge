from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES
from gameforge.bench.narrative.corpus import load_cases
from gameforge.bench.narrative.generator import ANSWER_MARKER
from gameforge.bench.narrative.protocol import PROMPT_NAMES

ROOT = Path(__file__).resolve().parents[2]
SPINE = ROOT / "gameforge/spine"
CONSISTENCY = ROOT / "gameforge/agents/consistency"
RENDERER = ROOT / "gameforge/bench/narrative/renderer.py"
ORACLE = ROOT / "gameforge/bench/narrative/oracle.py"
CORPUS_ROOT = ROOT / "scenarios/narrative_bench"

_SPECIALIZATION_IDENTIFIERS = {
    "benchmark_family",
    "case_id",
    "fixture_id",
    "game_id",
    "seed",
    "setting_id",
    "source_id",
    "source_profile",
    "source_profile_id",
}
_GAME_PROFILE_LITERALS = ("aureus", "endless_sky", "flare")
_HIDDEN_MARKER = re.compile(
    r"(?i)(?:\b(?:benchmark_family|case_id|case_sha256|is_clean|seed|"
    r"source_fact_ids|target_constraint_ids|target_entities|target_span)\b|"
    r"nv-[0-9a-f]{24})"
)


def _python_files(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    return tuple(sorted(path.rglob("*.py")))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolved_imports(path: Path) -> tuple[str, ...]:
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    modules: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative, package)
            else:
                base = node.module or ""
            if base:
                modules.append(base)
                if node.module is None:
                    modules.extend(f"{base}.{alias.name}" for alias in node.names)
    return tuple(modules)


def _forbidden_imports(path: Path, prefixes: tuple[str, ...]) -> list[str]:
    return [
        f"{source.relative_to(ROOT)}: {module}"
        for source in _python_files(path)
        for module in _resolved_imports(source)
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)
    ]


def _identifier(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.arg):
        return node.arg
    return None


def _consistency_specializations() -> list[str]:
    violations: list[str] = []
    for path in _python_files(CONSISTENCY):
        tree = _tree(path)
        relative = path.relative_to(ROOT)
        for node in ast.walk(tree):
            identifier = _identifier(node)
            if identifier in _SPECIALIZATION_IDENTIFIERS:
                violations.append(f"{relative}:{node.lineno}: {identifier}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                folded = node.value.casefold()
                for literal in _GAME_PROFILE_LITERALS:
                    if literal in folded:
                        violations.append(f"{relative}:{node.lineno}: {literal}")
    return sorted(set(violations))


def test_spine_never_imports_agents_or_benchmark_code():
    assert _forbidden_imports(
        SPINE,
        ("gameforge.agents", "gameforge.bench"),
    ) == []


def test_consistency_agent_has_no_benchmark_dependency_or_game_specialization():
    assert _forbidden_imports(CONSISTENCY, ("gameforge.bench",)) == []
    assert _consistency_specializations() == []


def test_renderer_and_oracle_keep_independent_dependency_boundaries():
    assert _forbidden_imports(
        RENDERER,
        ("gameforge.agents", "gameforge.bench.narrative.oracle"),
    ) == []
    assert _forbidden_imports(
        ORACLE,
        (
            "gameforge.agents",
            "gameforge.bench.narrative.renderer",
            "gameforge.contracts.model_router",
            "gameforge.runtime.model_router",
        ),
    ) == []


def test_frozen_visible_corpus_and_current_prompts_contain_no_answer_markers():
    class_labels = tuple(item.value for item in NARRATIVE_CLASSES)
    for split in ("development", "verification"):
        for case in load_cases(CORPUS_ROOT / f"{split}.jsonl"):
            visible_texts = (
                case.dialogue,
                *(constraint.statement for constraint in case.constraints),
            )
            for text in visible_texts:
                assert case.case_id not in text
                assert not _HIDDEN_MARKER.search(text)
                assert not ANSWER_MARKER.search(text)
                assert all(label not in text for label in class_labels)

    register_all_prompts()
    for prompt_name in PROMPT_NAMES:
        _, prompt_text = get_prompt(prompt_name)
        assert not _HIDDEN_MARKER.search(prompt_text)
