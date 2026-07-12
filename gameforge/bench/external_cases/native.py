"""Compile and run the standalone Endless Sky DataFile syntax witness."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from gameforge.bench.external_cases.contracts import NativeEvidence
from gameforge.contracts.canonical import canonical_json


PARSER_ID = "endless-sky-datafile-native"
PARSER_VERSION = "endless-sky-datafile-native@1"
_SUMMARY = re.compile(rb"files=(\d+) nodes=(\d+) tokens=(\d+)\n")


@dataclass(frozen=True)
class NativeParserBinary:
    path: Path
    compiler: str
    source_sha256: str
    binary_sha256: str


@dataclass(frozen=True)
class NativeParserResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    summary: dict[str, int]
    input_manifest_sha256: str
    command: tuple[str, ...]


def _environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def compile_native_parser(source: str | Path, build_dir: str | Path) -> NativeParserBinary:
    source_path = Path(source).resolve(strict=True)
    output_dir = Path(build_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    configured = os.environ.get("CXX")
    compiler_path = shutil.which(configured or "c++")
    if compiler_path is None:
        raise RuntimeError(f"C++ compiler is unavailable: {configured or 'c++'}")

    version = subprocess.run(
        [compiler_path, "--version"],
        check=True,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout.decode("utf-8", errors="replace").splitlines()[0]
    binary = output_dir / "endless-sky-data-parser"
    completed = subprocess.run(
        [
            compiler_path,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-pedantic",
            str(source_path),
            "-o",
            str(binary),
        ],
        check=False,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"native parser compilation failed:\n{detail}")
    source_raw = source_path.read_bytes()
    binary_raw = binary.read_bytes()
    return NativeParserBinary(
        path=binary,
        compiler=version,
        source_sha256=hashlib.sha256(source_raw).hexdigest(),
        binary_sha256=hashlib.sha256(binary_raw).hexdigest(),
    )


def run_native_parser(
    binary: NativeParserBinary,
    paths: Iterable[str | Path],
    *,
    source_root: str | Path,
) -> NativeParserResult:
    root = Path(source_root).resolve(strict=True)
    bound: list[tuple[str, Path, bytes]] = []
    for value in paths:
        path = Path(value).resolve(strict=True)
        if not path.is_file() or path.is_symlink() or not path.is_relative_to(root):
            raise ValueError(f"native parser input is outside source_root: {value}")
        relative = path.relative_to(root).as_posix()
        bound.append((relative, path, path.read_bytes()))
    bound.sort(key=lambda item: item[0])
    if not bound:
        raise ValueError("native parser requires at least one input file")

    manifest = [
        {
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
        for relative, _, raw in bound
    ]
    manifest_sha = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    relative_paths = tuple(relative for relative, _, _ in bound)
    execution_command = (str(binary.path), *relative_paths)
    completed = subprocess.run(
        list(execution_command),
        check=False,
        cwd=root,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    summary: dict[str, int] = {}
    if completed.returncode == 0:
        match = _SUMMARY.fullmatch(completed.stdout)
        if match is None:
            raise ValueError("native parser returned noncanonical summary output")
        summary = {
            "files": int(match.group(1)),
            "nodes": int(match.group(2)),
            "tokens": int(match.group(3)),
        }
    return NativeParserResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        summary=summary,
        input_manifest_sha256=manifest_sha,
        command=(PARSER_ID, *relative_paths),
    )


def native_evidence(
    binary: NativeParserBinary,
    result: NativeParserResult,
) -> NativeEvidence:
    return NativeEvidence(
        parser_id=PARSER_ID,
        parser_version=PARSER_VERSION,
        source_sha256=binary.source_sha256,
        input_manifest_sha256=result.input_manifest_sha256,
        command=result.command,
        exit_code=result.exit_code,
        stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
        compiler=binary.compiler,
    )


__all__ = [
    "NativeParserBinary",
    "NativeParserResult",
    "compile_native_parser",
    "native_evidence",
    "run_native_parser",
]
