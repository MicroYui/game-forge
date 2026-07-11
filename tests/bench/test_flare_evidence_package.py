from __future__ import annotations

import asyncio
import builtins
import gzip
import hashlib
import io
from itertools import pairwise
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tarfile
from typing import TYPE_CHECKING, TypeVar
import zlib

import pytest
from pydantic import BaseModel

if TYPE_CHECKING:
    from gameforge.bench.flare_evidence import AdjudicationEvidence, DiscoveryLedger


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "scenarios/flare_corpus"
INITIAL = CORPUS / "b0a/initial"
REGISTRATION_COMMIT = "140aa5b99e9748022d735e67014c0bb8bd67cc77"
REGISTRATION_PATH = "scenarios/flare_corpus/search-spec.json"
PINNED_HEAD = "fe23b5ba73f99f0c3969f8b23dbabaa8f7a6b602"

EXPECTED_HASHES = {
    "search-spec.json": "53e841dddaf22c560bd9f6ccc297aee6379ad277bcec39174e41d0bd5f463efa",
    "b0a/initial/candidate-ledger.discovered.json": (
        "aab1d14897f67b39e3ec6858c1b7c8c4ddb90736a9e53fcd3ebc260bad54acc1"
    ),
    "b0a/initial/adjudication-evidence.json": (
        "756ef83a72c8a88829066ee1f7ff6f4841e5d4f1e85e8970205bd2faea7e58da"
    ),
    "b0a/initial/candidate-ledger.json": (
        "37d140680406883588de9f10e51434040ebd1220150703192bb12a2acde3a315"
    ),
    "b0a/initial/b0a-decision.json": (
        "737d598549955a382785797885c52dcdb18bfad25ebe541b3d4996f82cc73f5c"
    ),
    "candidate-ledger.discovered.json": (
        "eb0cadfacab53e1ea2ae499c755bb5d78f2497a85be3de98c4ac56e22baded12"
    ),
    "adjudication-evidence.json": (
        "cbd6a7949802b6ce045e155569b8fb3677b5949df300fa499ab4d4012d969698"
    ),
    "candidate-ledger.json": (
        "d5f89c6ac90d00f1f7cfce06319159a8de5863fd9e8fd3ae96e26bc551c0b350"
    ),
    "b0a-decision.json": (
        "aebe50b78cbe4cd690288092170bfa2ff8a1a90498fef8c142a978ae3c889088"
    ),
}
EXPECTED_LICENSE_SHA256 = (
    "3f941b3b89cf7b8370ceb83cc76d2120d471b58735d8ca60238a751a48d7f72f"
)
EXPECTED_PACKAGE_FILE_HASHES = {
    "CREDITS.flare-engine-wiki.md": (
        "cea8e5a576cb5dd410fce2a0d1f35f502e37bf0120a394d86d9032afec74933b"
    ),
    "CREDITS.flare-game": (
        "c82830ffec90140ad625da05d4b9cde94f15c53ce3fd7a4451bbae0463761bdb"
    ),
    "CREDITS.flare-game-v0.18-wiki.md": (
        "27a9547404480f9193dd30d0ca9f9a2ecad01f471981611423a068e1d816d9e1"
    ),
    "CREDITS.flare-game-wiki.md": (
        "c33ffea3a608251b1c6bbee5f73930070a2fc276bb3aa1603b285ea5dc63e22d"
    ),
    "LICENSE.flare-game": EXPECTED_LICENSE_SHA256,
    "LICENSES/GNU-Unifont-5.1-README.txt": (
        "7a471fc9dfd5b351ad06ea48fdf24eef5a0dd30cdbc4d5a0ed0a6958d7bcb2d4"
    ),
    "LICENSES/GNU-Unifont-5.1-debian-copyright.txt": (
        "da62a3a3c3e84f220221ac4555b8471bac64b2124e7e0edd0f6d66eb2fa1ec04"
    ),
    "LICENSES/GPL-2.0-or-later.txt": (
        "edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6"
    ),
    "LICENSES/GPL-2.0.txt": (
        "edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6"
    ),
    "LICENSES/GPL-3.0-or-later.txt": (
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"
    ),
    "LICENSES/MPlus-font-license.txt": (
        "83ca32cb858125c07a5f950ec078af6522b27ec64fedd2910d547066caf945df"
    ),
    "LICENSES/OFL-1.1.txt": (
        "8eea8287e5876b539670cadb82e99f9a7afddec6f6730811be1daf25d2e9bcfd"
    ),
    "LICENSES/OFL-1.1-AlexBrush-1.003.txt": (
        "513b7871a360a6eccd426bab59743fbff36764588f47381423f04ad6b3d821d9"
    ),
    "LICENSES/OFL-1.1-LiberationSans-2.00.0.txt": (
        "93fed46019c38bbe566b479d22148e2e8a1e85ada614accb0211c37b2c61c19b"
    ),
    "LICENSES/TexturaLibera-0.2.2.txt": (
        "842baac67b2f1bab31be56a7608a825f40170c6327f3c89d84038af262a6c3d4"
    ),
    "NOTICE": "5b7cd77a9982c6c247aa8eca91729e67802cedae5116b037134233e19bfef90a",
    "README.flare-game": (
        "81c00b17aaab60d4fe5124163b92863846d76c8c670a772b99a4de6d1b7a3808"
    ),
    "sources/GNU-Unifont-5.1/unifont-5.1.ttf.gz": (
        "d29898ed5f0749bb6dfe516b44f78400df885d994aa29c8cd76e3b86c488e1c8"
    ),
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.diff.gz": (
        "8b93cb2bb1f6123ce8bada5fd6145812913cf2bc23c0751f0ad11f807db6b06a"
    ),
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.dsc": (
        "7c748366fc1b010742b58e467057baecb1aa3b01f33fe39722ac6260f7b60900"
    ),
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914.orig.tar.gz": (
        "4d2aafedd64c48b8703f2abd4e10a5a8087d21120707cb6171c97ff0661b0edd"
    ),
}
EXPECTED_PACKAGE_FILE_SIZES = {
    "CREDITS.flare-engine-wiki.md": 5_332,
    "CREDITS.flare-game": 3_454,
    "CREDITS.flare-game-v0.18-wiki.md": 2_870,
    "CREDITS.flare-game-wiki.md": 21_809,
    "LICENSE.flare-game": 22_240,
    "LICENSES/GNU-Unifont-5.1-README.txt": 14_760,
    "LICENSES/GNU-Unifont-5.1-debian-copyright.txt": 5_153,
    "LICENSES/GPL-2.0-or-later.txt": 17_984,
    "LICENSES/GPL-2.0.txt": 17_984,
    "LICENSES/GPL-3.0-or-later.txt": 35_147,
    "LICENSES/MPlus-font-license.txt": 221,
    "LICENSES/OFL-1.1.txt": 4_016,
    "LICENSES/OFL-1.1-AlexBrush-1.003.txt": 4_389,
    "LICENSES/OFL-1.1-LiberationSans-2.00.0.txt": 4_414,
    "LICENSES/TexturaLibera-0.2.2.txt": 249,
    "NOTICE": 14_752,
    "README.flare-game": 5_143,
    "adjudication-evidence.json": 313_528,
    "b0a-decision.json": 489,
    "b0a/initial/adjudication-evidence.json": 37_907,
    "b0a/initial/b0a-decision.json": 456,
    "b0a/initial/candidate-ledger.discovered.json": 84_468,
    "b0a/initial/candidate-ledger.json": 44_630,
    "candidate-ledger.discovered.json": 848_473,
    "candidate-ledger.json": 322_211,
    "search-spec.json": 4_264,
    "sources/GNU-Unifont-5.1/unifont-5.1.ttf.gz": 3_105_363,
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.diff.gz": 8_799,
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.dsc": 1_952,
    "sources/GNU-Unifont-5.1/unifont_5.1.20080914.orig.tar.gz": 8_550_619,
}
EXPECTED_UNIFONT_PATCH_SHA256 = (
    "8dc7071c371da43b6246f0a9b9377c92db62f6322631b11988d1772791a421ee"
)
EXPECTED_UNIFONT_TTF_SHA256 = (
    "849a59a9729c3dcd84137ebf11e6f65aebf673f8322a3adabe9f087329daa581"
)
EXPECTED_UNIFONT_TTF_SIZE = 16_336_376
EXPECTED_UNIFONT_SOURCE_TTF_SHA256 = (
    "4e10ac7c83c720c695c97e9361d26b4d9caf60d1cd1887302253b6ee30d55fa6"
)
EXPECTED_UNIFONT_SOURCE_TTF_SIZE = 16_336_380
EXPECTED_UNIFONT_HEX_SHA256 = (
    "f3497e227e3e7152b3a0ae31204ceebd06252a03411430d6f49143a77574b7d2"
)
EXPECTED_UNIFONT_HEX_SIZE = 3_905_540
EXPECTED_DEBIAN_UNIFONT_INPUT_GLYPHS = 53_430
EXPECTED_DEBIAN_UNIFONT_OUTPUT_GLYPHS = 53_388
EXPECTED_UNIVERSE_HASHES = {
    "initial": "a93adacdb1f78d5edb89901ddde4e5fd108d5ef41ce00859df691cc3cf8628cd",
    "expanded": "08873db9362bd6ff45ca05bb4e3184120fc07842cf224e8f649fb6555e57bfc3",
}
EXPECTED_APPROVAL_PAYLOAD_HASHES = {
    "initial": "5292d194288bbae9e5b6f9304494fed0b11e9ab22e51aa07f3266f8178728648",
    "expanded": "b5111dd6d65caa7675a82ac7b7c2a3735dd472eed0a2e8fc607c6b1292dc9970",
}
EXPECTED_INITIAL_GATE = {
    "status": "expanded_round_required",
    "proposed_groups": 2,
    "proposed_classes": 4,
    "required_groups": 8,
    "required_classes": 4,
    "reason_code_counts": {
        "insufficient_context": 1,
        "non_bug": 4,
        "non_config_only": 37,
        "out_of_taxonomy": 18,
    },
    "failure_reasons": ["fewer than eight independent proposed groups"],
    "next_action": "run_expanded_round",
}
EXPECTED_GATE = {
    "status": "insufficient_evidence",
    "proposed_groups": 7,
    "proposed_classes": 4,
    "required_groups": 8,
    "required_classes": 4,
    "reason_code_counts": {
        "insufficient_context": 1,
        "non_bug": 86,
        "non_config_only": 336,
        "out_of_taxonomy": 94,
        "revert_or_duplicate": 2,
    },
    "failure_reasons": ["fewer than eight independent proposed groups"],
    "next_action": "stop_flare_heavy_investment",
}
EXPECTED_GROUP_IDS = [
    "initial-warden-key-source",
    "initial-abasi-hoi-journal",
    "expanded-empyrean-bear-trap-effect-reference",
    "expanded-arrow-wall-power-reference",
    "expanded-alpha-demo-element-registry",
    "expanded-abasi-nazia-journal",
    "expanded-antlion-ranged-include",
]
EXPECTED_MIXED_LICENSE_PATCH_PATHS = {
    "8dc7071c371da43b6246f0a9b9377c92db62f6322631b11988d1772791a421ee": {
        "mods/fantasycore/fonts/unifont-5.1.ttf"
    },
    "34ef3f5a1efb5f09ff589a0ca18a3f74b3ef589ea1548e5322aeef00bd574e11": {
        "src/Utils.cpp"
    },
    "13f4267a8b4493e9634a847bd8ee8755f20f8ffaa1411ee8b16487d9c56b2dc4": {
        "src/MapIso.cpp",
        "src/Utils.cpp",
        "src/Utils.h",
    },
    "1415b1943ca507e7ebed88616669c4ebae12e0137ff594deb399f2afafcb1832": {
        "mods/default/default/fonts/LiberationSans-Regular.ttf",
        "mods/default/default/languages/xgettext.py",
    },
    "2405d0368406d4866cacdfe5bdc1c9b4a4e6b0dd9d82f12e0917143973789ec2": {
        "mods/empyrean_campaign/fonts/AlexBrush-Regular-OTF.otf"
    },
    "1105e6ef2c8071135b4b458548a6afd563eeb87a22afccbf0d0db2d69ef41a57": {
        "mods/empyrean_campaign/fonts/TexturaLiberaTenuisX-Bold.otf"
    },
}
EXPECTED_MATRIX = [
    ("dangling_reference", "applicable", "found", 5),
    ("missing_drop_source", "applicable", "found", 1),
    ("unreachable_target", "applicable", "not_found", 0),
    ("cyclic_dependency", "applicable", "not_found", 0),
    ("dead_quest", "applicable", "found", 2),
    ("unsatisfiable_completion", "applicable", "found", 2),
    ("reward_out_of_range", "applicable", "not_found", 0),
    ("prob_sum_ne_1", "not_applicable", "not_found", 0),
    ("non_monotonic_curve", "applicable", "not_found", 0),
    ("gacha_expectation_violation", "not_applicable", "not_found", 0),
    ("economy_collapse", "applicable", "not_found", 0),
]

EXPECTED_MINING_COMMANDS = [
    "uv run python -m gameforge.bench.flare_mining discover "
    "--repo /tmp/gameforge-flare-game.git "
    "--search-spec scenarios/flare_corpus/search-spec.json "
    f"--registration-commit {REGISTRATION_COMMIT} "
    f"--registration-path {REGISTRATION_PATH} "
    "--round initial "
    "--out scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json "
    "--blob-dir scenarios/flare_corpus/blobs",
    "uv run python -m gameforge.bench.flare_mining adjudicate "
    "--ledger scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json "
    "--evidence scenarios/flare_corpus/b0a/initial/adjudication-evidence.json "
    "--blob-dir scenarios/flare_corpus/blobs "
    "--out scenarios/flare_corpus/b0a/initial/candidate-ledger.json "
    "--decision-out scenarios/flare_corpus/b0a/initial/b0a-decision.json",
    "uv run python -m gameforge.bench.flare_mining discover "
    "--repo /tmp/gameforge-flare-game.git "
    "--search-spec scenarios/flare_corpus/search-spec.json "
    f"--registration-commit {REGISTRATION_COMMIT} "
    f"--registration-path {REGISTRATION_PATH} "
    "--round expanded "
    "--out scenarios/flare_corpus/candidate-ledger.discovered.json "
    "--blob-dir scenarios/flare_corpus/blobs",
    "uv run python -m gameforge.bench.flare_mining adjudicate "
    "--ledger scenarios/flare_corpus/candidate-ledger.discovered.json "
    "--evidence scenarios/flare_corpus/adjudication-evidence.json "
    "--blob-dir scenarios/flare_corpus/blobs "
    "--prior-discovery scenarios/flare_corpus/b0a/initial/candidate-ledger.discovered.json "
    "--prior-evidence scenarios/flare_corpus/b0a/initial/adjudication-evidence.json "
    "--prior-ledger scenarios/flare_corpus/b0a/initial/candidate-ledger.json "
    "--prior-decision scenarios/flare_corpus/b0a/initial/b0a-decision.json "
    "--out scenarios/flare_corpus/candidate-ledger.json "
    "--decision-out scenarios/flare_corpus/b0a-decision.json",
]


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _load_canonical(relative_path: str, model_type: type[_ModelT]) -> tuple[bytes, _ModelT]:
    from gameforge.bench.flare_evidence import canonical_bytes

    raw = (CORPUS / relative_path).read_bytes()
    model = model_type.model_validate_json(raw)
    independently_canonical = (
        json.dumps(
            json.loads(raw),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    assert raw == independently_canonical
    assert raw == canonical_bytes(model)
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_HASHES[relative_path]
    return raw, model


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_GIT_BASE85_ALPHABET = (
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    b"!#$%&()*+-;<=>?@^_`{|}~"
)
_GIT_BASE85_VALUES = {
    encoded: value for value, encoded in enumerate(_GIT_BASE85_ALPHABET)
}


def _decode_git_binary_literal(patch: bytes, path: str) -> bytes:
    diff_header = f"diff --git a/{path} b/{path}\n".encode()
    section = patch.split(diff_header, maxsplit=1)[1]
    binary_patch = section.split(b"GIT binary patch\n", maxsplit=1)[1]
    literal_header, encoded_body = binary_patch.split(b"\n", maxsplit=1)
    assert literal_header.startswith(b"literal ")
    expected_size = int(literal_header.removeprefix(b"literal "))

    compressed = bytearray()
    for line in encoded_body.split(b"\n\n", maxsplit=1)[0].splitlines():
        length_code, encoded = line[0], line[1:]
        if ord("A") <= length_code <= ord("Z"):
            decoded_length = length_code - ord("A") + 1
        else:
            assert ord("a") <= length_code <= ord("z")
            decoded_length = length_code - ord("a") + 27
        assert len(encoded) == ((decoded_length + 3) // 4) * 5

        decoded_line = bytearray()
        for offset in range(0, len(encoded), 5):
            value = 0
            for encoded_byte in encoded[offset : offset + 5]:
                value = value * 85 + _GIT_BASE85_VALUES[encoded_byte]
            decoded_line.extend(value.to_bytes(4, byteorder="big"))
        compressed.extend(decoded_line[:decoded_length])

    literal = zlib.decompress(bytes(compressed))
    assert len(literal) == expected_size
    return literal


def _sfnt_tables(font: bytes) -> dict[bytes, bytes]:
    assert len(font) >= 12
    table_count = struct.unpack_from(">H", font, 4)[0]
    assert 12 + table_count * 16 <= len(font)

    tables: dict[bytes, bytes] = {}
    for index in range(table_count):
        tag, _checksum, offset, length = struct.unpack_from(
            ">4sIII", font, 12 + index * 16
        )
        assert tag not in tables
        assert offset <= len(font) and length <= len(font) - offset
        tables[tag] = font[offset : offset + length]
    return tables


def _sfnt_glyphs(tables: dict[bytes, bytes]) -> tuple[bytes, ...]:
    head = tables[b"head"]
    maxp = tables[b"maxp"]
    loca = tables[b"loca"]
    glyf = tables[b"glyf"]
    assert len(head) >= 54 and len(maxp) >= 6

    glyph_count = struct.unpack_from(">H", maxp, 4)[0]
    loca_format = struct.unpack_from(">h", head, 50)[0]
    if loca_format == 0:
        byte_count = (glyph_count + 1) * 2
        assert len(loca) >= byte_count
        offsets = tuple(
            value * 2
            for value in struct.unpack_from(f">{glyph_count + 1}H", loca)
        )
    else:
        assert loca_format == 1
        byte_count = (glyph_count + 1) * 4
        assert len(loca) >= byte_count
        offsets = struct.unpack_from(f">{glyph_count + 1}I", loca)

    assert all(left <= right for left, right in pairwise(offsets))
    assert offsets[-1] <= len(glyf)
    return tuple(glyf[start:end] for start, end in pairwise(offsets))


def _unifont_hex_records(payload: bytes) -> dict[int, bytes]:
    records: dict[int, bytes] = {}
    for line in payload.splitlines():
        codepoint_hex, glyph = line.split(b":", maxsplit=1)
        assert len(codepoint_hex) == 4
        codepoint = int(codepoint_hex, 16)
        assert codepoint not in records
        records[codepoint] = glyph
    return records


def _assert_corresponding_unifont_source(source_ttf: bytes, patch_ttf: bytes) -> None:
    source_tables = _sfnt_tables(source_ttf)
    patch_tables = _sfnt_tables(patch_ttf)
    for tag in (b"cmap", b"maxp", b"hhea"):
        assert source_tables[tag] == patch_tables[tag]

    glyph_count = struct.unpack_from(">H", source_tables[b"maxp"], 4)[0]
    assert glyph_count == 63_449
    source_glyphs = _sfnt_glyphs(source_tables)
    patch_glyphs = _sfnt_glyphs(patch_tables)
    assert len(source_glyphs) == len(patch_glyphs) == glyph_count
    assert [
        glyph_id
        for glyph_id, (source_glyph, patch_glyph) in enumerate(
            zip(source_glyphs, patch_glyphs, strict=True)
        )
        if source_glyph != patch_glyph
    ] == [0]

    source_hmtx = source_tables[b"hmtx"]
    patch_hmtx = patch_tables[b"hmtx"]
    assert len(source_hmtx) == len(patch_hmtx) >= 4
    assert [
        offset
        for offset, (source_byte, patch_byte) in enumerate(
            zip(source_hmtx, patch_hmtx, strict=True)
        )
        if source_byte != patch_byte
    ] == [1]
    assert source_hmtx[4:] == patch_hmtx[4:]


def _all_evidence_refs(evidence: AdjudicationEvidence):
    for group in evidence.group_decisions:
        yield from group.root_cause_evidence_refs
        for case in group.case_decisions:
            yield from case.evidence_refs
    for decision in evidence.candidate_decisions:
        yield from decision.evidence_refs


def _assert_evidence_refs_are_relevant(
    discovery: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    candidates = {
        item.commit.commit_oid: item for item in discovery.discovered_candidates
    }
    patch_owners: dict[str, set[str]] = {}
    for commit_oid, candidate in candidates.items():
        patch_owners.setdefault(candidate.diff_evidence.patch_sha256, set()).add(
            commit_oid
        )
    links = {item.link_id: item for item in discovery.objective_lineage_links}
    artifact_ids = {item.artifact_id for item in evidence.source_artifacts}

    def assert_relevant(ref, allowed_commits: set[str]) -> None:
        if ref.kind == "commit_message":
            assert ref.target_id in allowed_commits
        elif ref.kind == "patch_blob":
            assert patch_owners[ref.target_id] & allowed_commits
        elif ref.kind == "lineage_link":
            link = links[ref.target_id]
            assert {link.source_oid, link.target_oid} & allowed_commits
        else:
            assert ref.kind == "source_artifact"
            assert ref.target_id in artifact_ids

    for decision in evidence.candidate_decisions:
        assert decision.commit_oid in candidates
        for ref in decision.evidence_refs:
            assert_relevant(ref, {decision.commit_oid})

    for group in evidence.group_decisions:
        group_commits = set(group.commits)
        assert group_commits <= candidates.keys()
        for ref in group.root_cause_evidence_refs:
            assert_relevant(ref, group_commits)
        for case in group.case_decisions:
            for ref in case.evidence_refs:
                assert_relevant(ref, group_commits)


def _install_offline_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = os.path.abspath("/tmp/gameforge-flare-game.git")
    upstream_alias = os.path.realpath(upstream)
    original_stat = os.stat
    try:
        upstream_stat = original_stat(upstream)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        upstream_identity = None
    else:
        upstream_identity = (upstream_stat.st_dev, upstream_stat.st_ino)

    def targets_upstream(value: object) -> bool:
        if isinstance(value, int):
            return False
        try:
            candidate = os.path.abspath(os.fsdecode(os.fspath(value)))
        except (TypeError, ValueError):
            return False
        if any(
            os.path.commonpath((candidate, root)) == root
            for root in {upstream, upstream_alias}
        ):
            return True
        if upstream_identity is None:
            return False
        while True:
            try:
                candidate_stat = original_stat(candidate)
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                pass
            else:
                if (candidate_stat.st_dev, candidate_stat.st_ino) == upstream_identity:
                    return True
            parent = os.path.dirname(candidate)
            if parent == candidate:
                return False
            candidate = parent

    def forbid_process(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline package verification must not execute a process")

    for name in (
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "run",
    ):
        monkeypatch.setattr(subprocess, name, forbid_process)
    for name in (
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
    ):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbid_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbid_process)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbid_process)

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline package verification must not access the network")

    for name in (
        "create_connection",
        "getaddrinfo",
        "getfqdn",
        "gethostbyaddr",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
        "socket",
    ):
        monkeypatch.setattr(socket, name, forbid_network)

    def guarded(original):
        def wrapper(path, *args, **kwargs):
            if targets_upstream(path):
                raise AssertionError(
                    "offline package verification must not read the upstream mirror"
                )
            return original(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
    for name in ("listdir", "lstat", "open", "readlink", "scandir", "stat"):
        monkeypatch.setattr(os, name, guarded(getattr(os, name)))
    monkeypatch.setenv("PATH", "")


def _run_gameforge_git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_project_status_tracks_the_committed_b0a_decision():
    decision_path = "scenarios/flare_corpus/b0a-decision.json"
    committed_decision = _run_gameforge_git("show", f"HEAD:{decision_path}").stdout
    assert committed_decision == (ROOT / decision_path).read_bytes()
    gate = json.loads(committed_decision)["gate"]
    decision_state = (gate["status"], gate["next_action"])

    expected_bindings = {
        ("insufficient_evidence", "stop_flare_heavy_investment"): {
            "CLAUDE.md": {
                "M3 incomplete": ("| M3 |", "🔄 未完成"),
                "M4 not started and blocked": ("| M4 |", "⬜ 未开始", "阻塞"),
                "B0B not entered": ("不进入 B0B",),
                "next source or waiver": ("新外部语料", "书面 PRD scope waiver"),
            },
            "README.md": {
                "M3 incomplete": ("| **M3** |", "🔄 incomplete"),
                "M4 not started and blocked": (
                    "| **M4** |",
                    "⬜ not started",
                    "blocked",
                ),
                "B0B not entered": ("B0B", "were not entered"),
                "next source or waiver": (
                    "different external corpus",
                    "written PRD scope waiver",
                ),
            },
            "docs/superpowers/plans/README.md": {
                "M3 incomplete": ("M3 umbrella 仍未完成",),
                "M4 not started and blocked": ("M4", "未开始", "阻塞"),
                "B0B not entered": ("Flare B0B", "均未进入"),
                "next source or waiver": ("新的外部真实语料源", "书面 PRD scope waiver"),
            },
            "docs/superpowers/specs/2026-07-10-m3d-flare-rich-design.md": {
                "M3 incomplete": ("M3 umbrella 仍为未完成",),
                "M4 blocked": ("M4", "阻塞"),
                "B0B not entered": ("B0B、Corpus Freeze、M3d-1..4 均未进入",),
                "next source or waiver": ("新的外部真实语料源", "书面 PRD scope waiver"),
            },
        }
    }
    assert decision_state in expected_bindings, (
        "project-status bindings must be defined for the committed B0A decision: "
        f"{decision_state}"
    )

    for relative_path, concepts in expected_bindings[decision_state].items():
        raw_document = (ROOT / relative_path).read_text(encoding="utf-8")
        document = " ".join(raw_document.split())
        compact_document = "".join(raw_document.split())
        for concept, fragments in concepts.items():
            missing = [
                fragment
                for fragment in fragments
                if fragment not in document
                and "".join(fragment.split()) not in compact_document
            ]
            assert not missing, f"{relative_path} does not bind {concept}; missing {missing}"


def test_offline_guards_install_when_upstream_is_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    upstream = os.path.abspath("/tmp/gameforge-flare-game.git")
    upstream_alias = os.path.realpath(upstream)
    original_stat = os.stat

    def hide_upstream(path, *args, **kwargs):
        if not isinstance(path, int):
            candidate = os.path.abspath(os.fsdecode(os.fspath(path)))
            if candidate == upstream or candidate.startswith(upstream + os.sep):
                raise FileNotFoundError(candidate)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", hide_upstream)
    _install_offline_guards(monkeypatch)

    for upstream_path in {Path(upstream), Path(upstream_alias)}:
        with pytest.raises(AssertionError, match="must not read the upstream mirror"):
            (upstream_path / "HEAD").read_bytes()


def test_flare_harness_imports_under_offline_guards_in_isolated_process():
    script = f"""
import importlib
from pathlib import Path
import runpy
import socket
import subprocess
import sys

root = Path({str(ROOT)!r})
sys.path.insert(0, str(root))
namespace = runpy.run_path(str(root / "tests/bench/test_flare_evidence_package.py"))
monkeypatch = namespace["pytest"].MonkeyPatch()
namespace["_install_offline_guards"](monkeypatch)

try:
    subprocess.run(["git", "--version"])
except AssertionError:
    pass
else:
    raise AssertionError("process guard was not installed before production imports")

try:
    socket.create_connection(("example.invalid", 443))
except AssertionError:
    pass
else:
    raise AssertionError("network guard was not installed before production imports")

try:
    Path("/tmp/gameforge-flare-game.git/HEAD").read_bytes()
except AssertionError:
    pass
else:
    raise AssertionError("upstream guard was not installed before production imports")

for module_name in (
    "gameforge.bench.flare_evidence",
    "gameforge.bench.flare_git",
    "gameforge.bench.flare_adjudication",
    "gameforge.bench.flare_mining",
):
    importlib.import_module(module_name)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_frozen_flare_package_replays_offline_without_processes_or_upstream(
    monkeypatch: pytest.MonkeyPatch,
    registered_search_spec_bytes: bytes,
    registered_search_spec_sha256: str,
):
    upstream_alias = Path("/tmp/gameforge-flare-game.git").resolve()
    _install_offline_guards(monkeypatch)
    from gameforge.bench.flare_adjudication import adjudicate
    from gameforge.bench.flare_evidence import (
        AdjudicationEvidence,
        B0ADecision,
        CandidateLedger,
        DiscoveryLedger,
        FlareSearchSpec,
        canonical_bytes,
        verify_discovery_direct_matches,
    )

    for upstream_path in {
        Path("/tmp/gameforge-flare-game.git"),
        upstream_alias,
    }:
        with pytest.raises(AssertionError, match="must not read the upstream mirror"):
            (upstream_path / "HEAD").read_bytes()
    with pytest.raises(AssertionError, match="must not execute a process"):
        subprocess.run(["git", "--version"])
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.gethostbyname("example.invalid")

    expected_json_paths = set(EXPECTED_HASHES)
    actual_json_paths = {
        path.relative_to(CORPUS).as_posix() for path in CORPUS.rglob("*.json")
    }
    assert actual_json_paths == expected_json_paths
    assert not list(CORPUS.rglob(".gameforge-*.tmp"))

    spec_raw, spec = _load_canonical("search-spec.json", FlareSearchSpec)
    initial_discovery_raw, initial_discovery = _load_canonical(
        "b0a/initial/candidate-ledger.discovered.json", DiscoveryLedger
    )
    initial_evidence_raw, initial_evidence = _load_canonical(
        "b0a/initial/adjudication-evidence.json", AdjudicationEvidence
    )
    initial_ledger_raw, initial_ledger = _load_canonical(
        "b0a/initial/candidate-ledger.json", CandidateLedger
    )
    initial_decision_raw, initial_decision = _load_canonical(
        "b0a/initial/b0a-decision.json", B0ADecision
    )
    discovery_raw, discovery = _load_canonical(
        "candidate-ledger.discovered.json", DiscoveryLedger
    )
    evidence_raw, evidence = _load_canonical(
        "adjudication-evidence.json", AdjudicationEvidence
    )
    ledger_raw, ledger = _load_canonical("candidate-ledger.json", CandidateLedger)
    decision_raw, decision = _load_canonical("b0a-decision.json", B0ADecision)

    verify_discovery_direct_matches(CORPUS / "blobs", initial_discovery)
    verify_discovery_direct_matches(CORPUS / "blobs", discovery)

    assert spec_raw == registered_search_spec_bytes
    assert hashlib.sha256(spec_raw).hexdigest() == registered_search_spec_sha256
    assert spec.pinned_head == PINNED_HEAD
    assert spec.expected_revision_count == 7049
    assert initial_discovery.candidate_universe_sha256 == EXPECTED_UNIVERSE_HASHES[
        "initial"
    ]
    assert discovery.candidate_universe_sha256 == EXPECTED_UNIVERSE_HASHES["expanded"]
    assert initial_discovery.search_registration.project_commit_oid == REGISTRATION_COMMIT
    assert discovery.search_registration.project_commit_oid == REGISTRATION_COMMIT
    assert initial_discovery.search_registration.repo_relative_path == REGISTRATION_PATH
    assert discovery.search_registration.repo_relative_path == REGISTRATION_PATH
    assert initial_discovery.discovery_tool == discovery.discovery_tool
    assert discovery.discovery_tool.model_dump(mode="json") == {
        "tool_version": "gameforge-flare-discovery@1",
        "project_commit_oid": REGISTRATION_COMMIT,
        "git_version": "git version 2.54.0",
        "python_implementation": "CPython",
        "python_version": "3.12.13",
        "python_build": ["main", "Jun 23 2026 15:44:24"],
        "unicode_version": "15.0.0",
    }

    assert initial_evidence.discovery_ledger_sha256 == hashlib.sha256(
        initial_discovery_raw
    ).hexdigest()
    assert initial_ledger.discovery_ledger_sha256 == hashlib.sha256(
        initial_discovery_raw
    ).hexdigest()
    assert initial_ledger.adjudication_evidence_sha256 == hashlib.sha256(
        initial_evidence_raw
    ).hexdigest()
    assert initial_decision.candidate_ledger_sha256 == hashlib.sha256(
        initial_ledger_raw
    ).hexdigest()
    assert evidence.discovery_ledger_sha256 == hashlib.sha256(discovery_raw).hexdigest()
    assert ledger.discovery_ledger_sha256 == hashlib.sha256(discovery_raw).hexdigest()
    assert ledger.adjudication_evidence_sha256 == hashlib.sha256(evidence_raw).hexdigest()
    assert decision.candidate_ledger_sha256 == hashlib.sha256(ledger_raw).hexdigest()
    assert evidence.prior_candidate_ledger_sha256 == hashlib.sha256(
        initial_ledger_raw
    ).hexdigest()
    assert evidence.prior_decision_sha256 == hashlib.sha256(
        initial_decision_raw
    ).hexdigest()

    replayed_initial_ledger, replayed_initial_decision = adjudicate(
        initial_discovery,
        initial_evidence,
    )
    assert canonical_bytes(replayed_initial_ledger) == initial_ledger_raw
    assert canonical_bytes(replayed_initial_decision) == initial_decision_raw
    replayed_ledger, replayed_decision = adjudicate(
        discovery,
        evidence,
        initial_discovery,
        initial_evidence,
        initial_ledger,
        initial_decision,
    )
    assert canonical_bytes(replayed_ledger) == ledger_raw
    assert canonical_bytes(replayed_decision) == decision_raw

    assert initial_evidence.review_attestation.model_dump(mode="json") == {
        "reviewer_id": "human-review-liyifan",
        "review_scope": "complete_b0a_adjudication",
        "approval": "approved",
        "review_revision": "human-review-liyifan-initial-r1",
        "written_statement": (
            "我已审阅 Flare B0A initial-r1 的完整 62-candidate disposition table，批准 "
            "candidate universe SHA-256 "
            "a93adacdb1f78d5edb89901ddde4e5fd108d5ef41ce00859df691cc3cf8628cd "
            "对应的 approval payload SHA-256 "
            "5292d194288bbae9e5b6f9304494fed0b11e9ab22e51aa07f3266f8178728648；"
            "reviewer_id=human-review-liyifan。"
        ),
        "candidate_universe_sha256": EXPECTED_UNIVERSE_HASHES["initial"],
        "reviewed_payload_sha256": EXPECTED_APPROVAL_PAYLOAD_HASHES["initial"],
    }

    assert evidence.review_attestation.model_dump(mode="json") == {
        "reviewer_id": "human-review-liyifan",
        "review_scope": "complete_b0a_adjudication",
        "approval": "approved",
        "review_revision": "human-review-liyifan-expanded-r1",
        "written_statement": (
            "我已审阅 Flare B0A expanded-r1 的完整 526-candidate assignment table，批准 "
            "candidate universe SHA-256 "
            "08873db9362bd6ff45ca05bb4e3184120fc07842cf224e8f649fb6555e57bfc3 "
            "对应的 approval payload SHA-256 "
            "b5111dd6d65caa7675a82ac7b7c2a3735dd472eed0a2e8fc607c6b1292dc9970；"
            "reviewer_id=human-review-liyifan"
        ),
        "candidate_universe_sha256": EXPECTED_UNIVERSE_HASHES["expanded"],
        "reviewed_payload_sha256": EXPECTED_APPROVAL_PAYLOAD_HASHES["expanded"],
    }

    assert len(initial_discovery.discovered_candidates) == 62
    assert len(initial_ledger.groups) == 2
    assert len(initial_ledger.candidate_decisions) == 60
    assert len(initial_ledger.lineage_resolutions) == 0
    assert (
        sum(
            case.disposition == "proposed"
            for group in initial_ledger.groups
            for case in group.cases
        )
        == 4
    )
    assert initial_ledger.gate_summary.model_dump(mode="json") == EXPECTED_INITIAL_GATE
    assert initial_decision.gate.model_dump(mode="json") == EXPECTED_INITIAL_GATE
    assert len(discovery.discovered_candidates) == 526
    assert len(ledger.groups) == 7
    assert [group.fix_group_id for group in ledger.groups] == EXPECTED_GROUP_IDS
    assert len(ledger.candidate_decisions) == 519
    assert len(ledger.lineage_resolutions) == 7
    assert sum(case.disposition == "proposed" for group in ledger.groups for case in group.cases) == 10
    assert sum(item.disposition == "rejected" for item in ledger.candidate_decisions) == 518
    assert sum(item.disposition == "ambiguous" for item in ledger.candidate_decisions) == 1
    assert all(
        group.config_only and any(case.disposition == "proposed" for case in group.cases)
        for group in ledger.groups
    )
    assert ledger.gate_summary.model_dump(mode="json") == EXPECTED_GATE
    assert decision.gate.model_dump(mode="json") == EXPECTED_GATE

    matrix_projection = [
        (
            row.defect_class.value,
            row.domain_applicability,
            row.evidence_availability,
            row.evidence_counts.proposed,
        )
        for row in ledger.applicability_matrix
    ]
    assert matrix_projection == EXPECTED_MATRIX
    assert all(row.implementation_support == "planned" for row in ledger.applicability_matrix)
    assert all(row.evidence_counts.rejected == 0 for row in ledger.applicability_matrix)
    assert all(row.evidence_counts.ambiguous == 0 for row in ledger.applicability_matrix)
    assert all(row.evidence_counts.qualified_candidate == 0 for row in ledger.applicability_matrix)
    assert all(row.evidence_counts.accepted == 0 for row in ledger.applicability_matrix)
    assert initial_evidence.source_artifacts == []
    assert evidence.source_artifacts == []

    candidate_oids = {item.commit.commit_oid for item in discovery.discovered_candidates}
    lineage_ids = {item.link_id for item in discovery.objective_lineage_links}
    source_artifact_ids = {item.artifact_id for item in evidence.source_artifacts}
    patch_digests = {
        item.diff_evidence.patch_sha256 for item in discovery.discovered_candidates
    }
    candidates_by_patch: dict[str, list] = {}
    for item in discovery.discovered_candidates:
        candidates_by_patch.setdefault(item.diff_evidence.patch_sha256, []).append(item)
    for patch_sha256, required_paths in EXPECTED_MIXED_LICENSE_PATCH_PATHS.items():
        assert len(candidates_by_patch[patch_sha256]) == 1
        assert required_paths <= set(candidates_by_patch[patch_sha256][0].changed_paths)
    all_changed_paths = {
        path for item in discovery.discovered_candidates for path in item.changed_paths
    }
    assert any(path.endswith(".png") for path in all_changed_paths)
    assert any(path.endswith(".ogg") for path in all_changed_paths)
    for ref in _all_evidence_refs(evidence):
        resolved = {
            "commit_message": candidate_oids,
            "patch_blob": patch_digests,
            "lineage_link": lineage_ids,
            "source_artifact": source_artifact_ids,
        }[ref.kind]
        assert ref.target_id in resolved
    _assert_evidence_refs_are_relevant(initial_discovery, initial_evidence)
    _assert_evidence_refs_are_relevant(discovery, evidence)

    decided_candidate_oids = {
        item.commit_oid for item in ledger.candidate_decisions
    } | {commit_oid for group in ledger.groups for commit_oid in group.commits}
    assert decided_candidate_oids == candidate_oids
    assert {item.link_id for item in ledger.lineage_resolutions} == lineage_ids

    blob_paths = list((CORPUS / "blobs").iterdir())
    assert len(blob_paths) == 519
    assert all(path.is_file() and not path.is_symlink() for path in blob_paths)
    assert all(re.fullmatch(r"[0-9a-f]{64}", path.name) for path in blob_paths)
    assert {path.name for path in blob_paths} == patch_digests
    blob_sizes = {path.name: path.stat().st_size for path in blob_paths}
    referenced_patch_bytes = sum(
        blob_sizes[item.diff_evidence.patch_sha256]
        for item in discovery.discovered_candidates
    )
    unique_patch_bytes = sum(blob_sizes.values())
    assert (referenced_patch_bytes, unique_patch_bytes) == (332_834_369, 306_007_216)
    assert referenced_patch_bytes - unique_patch_bytes == 26_827_153
    for path in blob_paths:
        assert _sha256_file(path) == path.name

    assert CORPUS.is_dir() and not CORPUS.is_symlink()
    corpus_entries = list(CORPUS.rglob("*"))
    assert all(not path.is_symlink() for path in corpus_entries)
    assert all(path.is_file() or path.is_dir() for path in corpus_entries)
    actual_files = {
        path.relative_to(CORPUS).as_posix() for path in corpus_entries if path.is_file()
    }
    expected_files = {
        *EXPECTED_HASHES,
        *EXPECTED_PACKAGE_FILE_HASHES,
        "NOTICE",
        *(f"blobs/{digest}" for digest in patch_digests),
    }
    assert actual_files == expected_files
    actual_directories = {
        path.relative_to(CORPUS).as_posix() for path in corpus_entries if path.is_dir()
    }
    assert actual_directories == {
        "LICENSES",
        "b0a",
        "b0a/initial",
        "blobs",
        "sources",
        "sources/GNU-Unifont-5.1",
    }

    for relative_path, expected_sha256 in EXPECTED_PACKAGE_FILE_HASHES.items():
        package_path = CORPUS / relative_path
        assert package_path.is_file() and not package_path.is_symlink()
        assert _sha256_file(package_path) == expected_sha256
    assert set(EXPECTED_PACKAGE_FILE_SIZES) == {
        *EXPECTED_HASHES,
        *EXPECTED_PACKAGE_FILE_HASHES,
    }
    for relative_path, expected_size in EXPECTED_PACKAGE_FILE_SIZES.items():
        assert (CORPUS / relative_path).stat().st_size == expected_size

    unifont_source_dir = CORPUS / "sources/GNU-Unifont-5.1"
    dsc = (unifont_source_dir / "unifont_5.1.20080914-1.3.dsc").read_text(
        encoding="utf-8"
    )
    assert "Format: 1.0" in dsc
    assert "Version: 1:5.1.20080914-1.3" in dsc
    for source_name in (
        "unifont_5.1.20080914.orig.tar.gz",
        "unifont_5.1.20080914-1.3.diff.gz",
    ):
        assert (
            f"{EXPECTED_PACKAGE_FILE_HASHES[f'sources/GNU-Unifont-5.1/{source_name}']} "
            f"{EXPECTED_PACKAGE_FILE_SIZES[f'sources/GNU-Unifont-5.1/{source_name}']} "
            f"{source_name}"
        ) in dsc

    orig_source = unifont_source_dir / "unifont_5.1.20080914.orig.tar.gz"
    with tarfile.open(orig_source, mode="r:gz") as source_archive:
        source_members = set(source_archive.getnames())
        required_source_members = {
            "unifont-5.1.20080914/Makefile",
            "unifont-5.1.20080914/font/hexsrc/blanks.hex",
            "unifont-5.1.20080914/font/hexsrc/rc-base.hex",
            "unifont-5.1.20080914/font/hexsrc/rc-cjk.hex",
            "unifont-5.1.20080914/font/hexsrc/rc-hangul.hex",
            "unifont-5.1.20080914/font/hexsrc/rc-priv.hex",
            "unifont-5.1.20080914/font/hexsrc/masks.hex",
            "unifont-5.1.20080914/font/hexsrc/substitutes.hex",
            "unifont-5.1.20080914/font/hexsrc/wqy-cjk.hex",
            "unifont-5.1.20080914/font/precompiled/unifont.hex",
            "unifont-5.1.20080914/font/precompiled/unifont.ttf",
            "unifont-5.1.20080914/font/ttfsrc/Makefile",
            "unifont-5.1.20080914/font/ttfsrc/all.pe",
            "unifont-5.1.20080914/src/hex2sfd",
        }
        assert required_source_members <= source_members
        makefile_stream = source_archive.extractfile(
            "unifont-5.1.20080914/font/Makefile"
        )
        assert makefile_stream is not None
        makefile = makefile_stream.read()
        assert b"VERSION = 5.1.20080907" in makefile
        assert (
            b"UNIFILES = $(HEXDIR)/blanks.hex $(HEXDIR)/rc-base.hex "
            b"$(HEXDIR)/wqy-cjk.hex \\\n\t$(HEXDIR)/rc-hangul.hex "
            b"$(HEXDIR)/rc-priv.hex"
        ) in makefile

        unifont_hex_stream = source_archive.extractfile(
            "unifont-5.1.20080914/font/precompiled/unifont.hex"
        )
        assert unifont_hex_stream is not None
        unifont_hex = unifont_hex_stream.read()
        assert len(unifont_hex) == EXPECTED_UNIFONT_HEX_SIZE
        assert hashlib.sha256(unifont_hex).hexdigest() == EXPECTED_UNIFONT_HEX_SHA256
        assert len(unifont_hex.splitlines()) == 63_446

        debian_default_records: dict[int, bytes] = {}
        for source_name in ("rc-base.hex", "wqy-cjk.hex", "rc-hangul.hex"):
            source_stream = source_archive.extractfile(
                f"unifont-5.1.20080914/font/hexsrc/{source_name}"
            )
            assert source_stream is not None
            records = _unifont_hex_records(source_stream.read())
            assert debian_default_records.keys().isdisjoint(records)
            debian_default_records.update(records)
        assert len(debian_default_records) == EXPECTED_DEBIAN_UNIFONT_INPUT_GLYPHS

        substitutes_stream = source_archive.extractfile(
            "unifont-5.1.20080914/font/hexsrc/substitutes.hex"
        )
        assert substitutes_stream is not None
        substitutes = _unifont_hex_records(substitutes_stream.read())
        deleted_codepoints = {
            codepoint for codepoint, glyph in substitutes.items() if not glyph
        }
        replacement_codepoints = set(substitutes) - deleted_codepoints
        assert len(deleted_codepoints) == 42
        assert set(substitutes) <= debian_default_records.keys()
        debian_default_output = (
            debian_default_records.keys() - deleted_codepoints
        ) | replacement_codepoints
        assert len(debian_default_output) == EXPECTED_DEBIAN_UNIFONT_OUTPUT_GLYPHS

        source_ttf_stream = source_archive.extractfile(
            "unifont-5.1.20080914/font/precompiled/unifont.ttf"
        )
        assert source_ttf_stream is not None
        source_ttf = source_ttf_stream.read()
        assert len(source_ttf) == EXPECTED_UNIFONT_SOURCE_TTF_SIZE
        assert (
            hashlib.sha256(source_ttf).hexdigest()
            == EXPECTED_UNIFONT_SOURCE_TTF_SHA256
        )

    debian_patch = gzip.decompress(
        (unifont_source_dir / "unifont_5.1.20080914-1.3.diff.gz").read_bytes()
    )
    assert (
        b"-UNIFILES = $(HEXDIR)/blanks.hex $(HEXDIR)/rc-base.hex "
        b"$(HEXDIR)/wqy-cjk.hex \\\n-\t$(HEXDIR)/rc-hangul.hex "
        b"$(HEXDIR)/rc-priv.hex"
    ) in debian_patch
    assert (
        b"+UNIFILES = $(HEXDIR)/rc-base.hex $(HEXDIR)/wqy-cjk.hex \\\n+\t$(HEXDIR)/rc-hangul.hex"
    ) in debian_patch

    exact_upstream_ttf = gzip.decompress(
        (unifont_source_dir / "unifont-5.1.ttf.gz").read_bytes()
    )
    assert len(exact_upstream_ttf) == EXPECTED_UNIFONT_TTF_SIZE
    assert hashlib.sha256(exact_upstream_ttf).hexdigest() == EXPECTED_UNIFONT_TTF_SHA256
    patch_ttf = _decode_git_binary_literal(
        (CORPUS / f"blobs/{EXPECTED_UNIFONT_PATCH_SHA256}").read_bytes(),
        "mods/fantasycore/fonts/unifont-5.1.ttf",
    )
    assert patch_ttf == exact_upstream_ttf
    _assert_corresponding_unifont_source(source_ttf, patch_ttf)

    license_bytes = (CORPUS / "LICENSE.flare-game").read_bytes()
    assert license_bytes.startswith(
        b"Creative Commons Legal Code\n\nAttribution-ShareAlike 3.0 Unported\n"
    )

    notice = (CORPUS / "NOTICE").read_text(encoding="utf-8")
    notice_flat = " ".join(notice.split())
    required_notice_fragments = [
        "https://github.com/flareteam/flare-game.git",
        PINNED_HEAD,
        "all-reachable",
        "7,049",
        REGISTRATION_COMMIT,
        EXPECTED_HASHES["search-spec.json"],
        "gameforge-flare-discovery@1",
        "git version 2.54.0",
        "CPython 3.12.13",
        "Unicode 15.0.0",
        "2026-07-11",
        "prior exploratory",
        "non-blind",
        "Flare: Empyrean Campaign",
        "Copyright (c) 2010-2013 Clint Bellanger",
        "61939e26d397b51c0c36da8cb861f89c8bf1d6db",
        "f15e755a853d28050919534118c296db6b5196e2",
        "CREDITS.flare-game-wiki.md",
        "CREDITS.flare-game-v0.18-wiki.md",
        "CREDITS.flare-engine-wiki.md",
        "CC BY-SA 3.0 Unported",
        "GPL version 3 or later",
        "GPL version 2 or later",
        "font embedding exception",
        "GNU Unifont 5.1 has composite licensing",
        "Roman Czyborra",
        "Wen Quan Yi",
        "Baekmuk",
        "SIL Open Font License, Version 1.1",
        'Reserved Font Name "Alex Brush"',
        "M+ font license",
        "LICENSES/GNU-Unifont-5.1-debian-copyright.txt",
        "LICENSES/GPL-2.0.txt",
        "LICENSES/OFL-1.1-AlexBrush-1.003.txt",
        "LICENSES/OFL-1.1-LiberationSans-2.00.0.txt",
        "LICENSES/TexturaLibera-0.2.2.txt",
        EXPECTED_UNIFONT_PATCH_SHA256,
        EXPECTED_UNIFONT_TTF_SHA256,
        "sources/GNU-Unifont-5.1/unifont-5.1.ttf.gz",
        "sources/GNU-Unifont-5.1/unifont_5.1.20080914.orig.tar.gz",
        "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.diff.gz",
        "sources/GNU-Unifont-5.1/unifont_5.1.20080914-1.3.dsc",
        "https://unifoundry.com/pub/unifont/unifont-5.1.20080907/unifont-5.1.ttf.gz",
        "https://snapshot.debian.org/file/f4c8211b877f42ae03e42f9a05bca41ee267708e/"
        "unifont_5.1.20080914.orig.tar.gz",
        "https://snapshot.debian.org/file/da0ea8f32361f80579c234f949fdb4a25570984f/"
        "unifont_5.1.20080914-1.3.diff.gz",
        "https://snapshot.debian.org/file/1a4f29f30e43a1b7424b4a789b051a796da6e015/"
        "unifont_5.1.20080914-1.3.dsc",
        "https://snapshot.debian.org/package/unifont/1%3A5.1.20080914-1.3/",
        "complete corresponding source",
        "does not claim a byte-identical rebuild",
        "63,446",
        "complete Git binary patches",
        "LICENSE.flare-game does not license every byte in blobs/",
        "not a blind or held-out sample",
        "candidate-selection recall is unknown",
        "No endorsement by Flare authors or contributors is implied.",
        f"https://github.com/flareteam/flare-game/blob/{PINNED_HEAD}/LICENSE.txt",
        f"https://github.com/flareteam/flare-game/blob/{PINNED_HEAD}/README",
        f"https://github.com/flareteam/flare-game/blob/{PINNED_HEAD}/CREDITS.txt",
        "Patches are retained as audit evidence, not as a checkout or runnable "
        "redistribution of the whole game.",
    ]
    for fragment in required_notice_fragments:
        assert fragment in notice_flat
    for command in EXPECTED_MINING_COMMANDS:
        assert command in notice_flat


def test_search_registration_predates_results_and_contains_only_the_frozen_spec(
    registered_search_spec_bytes: bytes,
    registered_search_spec_sha256: str,
):
    _run_gameforge_git("merge-base", "--is-ancestor", REGISTRATION_COMMIT, "HEAD")
    registered = _run_gameforge_git(
        "show",
        f"{REGISTRATION_COMMIT}:{REGISTRATION_PATH}",
    ).stdout
    packaged = (CORPUS / "search-spec.json").read_bytes()
    assert registered == packaged == registered_search_spec_bytes
    assert hashlib.sha256(registered).hexdigest() == registered_search_spec_sha256
    assert registered_search_spec_sha256 == EXPECTED_HASHES["search-spec.json"]

    changed_paths = _run_gameforge_git(
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        REGISTRATION_COMMIT,
    ).stdout.decode("utf-8").splitlines()
    assert changed_paths == [REGISTRATION_PATH]

    registered_tree = set(
        _run_gameforge_git(
            "ls-tree",
            "-r",
            "--name-only",
            REGISTRATION_COMMIT,
        ).stdout.decode("utf-8").splitlines()
    )
    registered_corpus_tree = (
        _run_gameforge_git(
            "ls-tree",
            "-r",
            "--name-only",
            REGISTRATION_COMMIT,
            "--",
            "scenarios/flare_corpus",
        )
        .stdout.decode("utf-8")
        .splitlines()
    )
    assert registered_corpus_tree == [REGISTRATION_PATH]
    result_paths = {
        f"scenarios/flare_corpus/{path}"
        for path in EXPECTED_HASHES
        if path != "search-spec.json"
    }
    assert REGISTRATION_PATH in registered_tree
    assert registered_tree.isdisjoint(result_paths)
