from __future__ import annotations

import json

import pytest

from gameforge.bench.qa.contracts import QaCorrectnessVerdict
from gameforge.bench.qa.harness import (
    QaBundleMaterial,
    finalize_session,
    read_exact_changed_paths,
    unified_submission_patch,
    write_arm_bundle,
)
from gameforge.bench.qa.protocol import load_protocol
from gameforge.bench.qa.session import transition_session

_PROTOCOL = "scenarios/external_cases/endless_sky/qa-protocol.json"


class _Clock:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


def _material(tmp_path, *, assisted=False):
    protocol = load_protocol(_PROTOCOL)
    spec = next(item for item in protocol.sessions if (item.arm == "assisted") == assisted)
    tool = tmp_path / "source-tool"
    tool.write_bytes(b"tool")
    tool.chmod(0o755)
    return protocol, spec, QaBundleMaterial(
        session=spec,
        upstream_subject="Fix a configuration defect",
        before_files={"data/example.txt": b"mission Before\n"},
        native_tool=tool,
        assistance=(
            {
                "finding": {"id": "finding-1"},
                "agent_patch": {"id": "patch-1"},
                "passed_verification": True,
                "disposition": "edited",
            }
            if assisted
            else None
        ),
    )


def _verdict() -> QaCorrectnessVerdict:
    from gameforge.bench.qa.contracts import seal_qa_verdict

    return seal_qa_verdict(
        correct=True,
        reader_round_trip=True,
        native_exit_code=0,
        predicate_status="clear",
        target_finding_clear=True,
        target_entities_preserved=True,
        new_deterministic_findings=(),
        submitted_tree_sha256="a" * 64,
        failure_reason=None,
    )


def test_generic_bundle_writes_only_current_arm_material(tmp_path):
    protocol, spec, material = _material(tmp_path, assisted=False)
    destination = tmp_path / "bundle"

    bundle = write_arm_bundle(protocol, material, destination)

    assert bundle == destination
    assert (bundle / "work/data/example.txt").read_bytes() == b"mission Before\n"
    assert (bundle / "tools/syntax-checker").read_bytes() == b"tool"
    assert not (bundle / "GAMEFORGE.json").exists()
    task = json.loads((bundle / "TASK.json").read_text())
    assert task["session_id"] == spec.session_id
    assert task["arm"] == "manual"
    assert task["syntax_check_argv"] == [
        "tools/syntax-checker",
        "work/data/example.txt",
    ]


def test_assisted_bundle_schema_is_exact(tmp_path):
    protocol, _, material = _material(tmp_path, assisted=True)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")

    assistance = json.loads((bundle / "GAMEFORGE.json").read_text())
    assert set(assistance) == {
        "finding",
        "agent_patch",
        "passed_verification",
        "disposition",
    }


def test_exact_changed_path_reader_rejects_extra_files_and_symlinks(tmp_path):
    work = tmp_path / "work"
    path = work / "data/example.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ok")
    assert read_exact_changed_paths(work, ("data/example.txt",)) == {
        "data/example.txt": b"ok"
    }

    (work / "extra.txt").write_bytes(b"extra")
    with pytest.raises(ValueError, match="exact changed_paths"):
        read_exact_changed_paths(work, ("data/example.txt",))
    (work / "extra.txt").unlink()
    path.unlink()
    path.symlink_to(work / "missing")
    with pytest.raises(ValueError, match="symlink"):
        read_exact_changed_paths(work, ("data/example.txt",))


def test_unified_patch_is_stable_posix_and_path_ordered():
    before = {"data/z.txt": b"z old\n", "data/a.txt": b"a old\n"}
    after = {"data/z.txt": b"z new\n", "data/a.txt": b"a new\n"}

    patch = unified_submission_patch(before, after)

    assert patch.index(b"--- a/data/a.txt") < patch.index(b"--- a/data/z.txt")
    assert b"+++ b/data/a.txt" in patch
    assert b"-a old\n+a new\n" in patch
    assert unified_submission_patch(before, after) == patch


def test_finish_persists_patch_verdict_and_canonical_session_evidence(tmp_path):
    protocol, spec, material = _material(tmp_path, assisted=False)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")
    transition_session(bundle, "start", clock=_Clock(100))

    session = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=lambda work: (b"patch-bytes\n", _verdict()),
        participant_attested_no_contamination=True,
        clock=_Clock(700),
    )

    assert session.events[-1].kind == "finish"
    assert session.active_ns == 600
    assert session.verdict.correct is True
    assert (bundle / "final.patch").read_bytes() == b"patch-bytes\n"
    assert (bundle / "session-evidence.json").is_file()


def test_finish_rejects_duplicate_finish_and_false_attestation_is_invalid(tmp_path):
    protocol, spec, material = _material(tmp_path, assisted=False)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")
    transition_session(bundle, "start", clock=_Clock(100))
    session = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=lambda work: (b"", _verdict()),
        participant_attested_no_contamination=False,
        clock=_Clock(200),
    )
    assert session.protocol_valid is False

    with pytest.raises(ValueError, match="transition|finished"):
        finalize_session(
            protocol,
            spec,
            bundle,
            evaluator=lambda work: (b"", _verdict()),
            participant_attested_no_contamination=True,
            clock=_Clock(300),
        )
