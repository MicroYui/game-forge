from __future__ import annotations

import json

import pytest

import gameforge.bench.qa.harness as harness_module
from gameforge.bench.qa.contracts import QaCorrectnessVerdict
from gameforge.bench.qa.harness import (
    QaBundleMaterial,
    finalize_session,
    read_exact_changed_paths,
    unified_submission_patch,
    write_arm_bundle,
)
from gameforge.bench.qa.protocol import load_protocol
from gameforge.bench.qa.session import (
    freeze_session_submission,
    load_state,
    transition_session,
)

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
    return (
        protocol,
        spec,
        QaBundleMaterial(
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
    assert read_exact_changed_paths(work, ("data/example.txt",)) == {"data/example.txt": b"ok"}

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


def test_unified_patch_preserves_missing_final_newlines():
    patch = unified_submission_patch(
        {"data/a.txt": b"old"},
        {"data/a.txt": b"new"},
    )

    assert b"-old\n\\ No newline at end of file\n" in patch
    assert b"+new\n\\ No newline at end of file\n" in patch


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


def test_finish_is_idempotent_but_cannot_change_frozen_attestation(tmp_path):
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

    duplicate = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=lambda work: pytest.fail("completed finish must not rerun oracle"),
        participant_attested_no_contamination=False,
        clock=_Clock(),
    )
    assert duplicate == session

    with pytest.raises(ValueError, match="attestation"):
        finalize_session(
            protocol,
            spec,
            bundle,
            evaluator=lambda work: pytest.fail("attestation mismatch must fail first"),
            participant_attested_no_contamination=True,
            clock=_Clock(),
        )


def test_evaluator_failure_reuses_frozen_time_and_submission_on_retry(tmp_path):
    protocol, spec, material = _material(tmp_path, assisted=False)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")
    transition_session(bundle, "start", clock=_Clock(100))

    def fail(work):
        assert work.name.startswith(".qa-frozen-submission-")
        assert (work / "data/example.txt").read_bytes() == b"mission Before\n"
        raise RuntimeError("oracle unavailable")

    with pytest.raises(RuntimeError, match="oracle unavailable"):
        finalize_session(
            protocol,
            spec,
            bundle,
            evaluator=fail,
            participant_attested_no_contamination=True,
            clock=_Clock(200),
        )

    state = load_state(bundle)
    assert state.status == "finished"
    assert state.events[-1].monotonic_ns == 200
    assert state.participant_attested_no_contamination is True
    assert not (bundle / "work").exists()
    assert not (bundle / "final.patch").exists()
    assert not (bundle / "session-evidence.json").exists()

    live = bundle / "work/data/example.txt"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"edited after frozen finish\n")

    def recover(work):
        assert (work / "data/example.txt").read_bytes() == b"mission Before\n"
        return b"patch-bytes\n", _verdict()

    recovered = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=recover,
        participant_attested_no_contamination=True,
        clock=_Clock(),
    )
    assert recovered.active_ns == 100


def test_deadline_freeze_can_bind_attestation_and_finalize_later(tmp_path):
    protocol, spec, material = _material(tmp_path, assisted=False)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")
    transition_session(bundle, "start", clock=_Clock(100))

    frozen = freeze_session_submission(bundle, clock=_Clock(200))
    assert frozen.participant_attested_no_contamination is None

    session = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=lambda work: (b"patch-bytes\n", _verdict()),
        participant_attested_no_contamination=True,
        clock=_Clock(),
    )

    assert session.events[-1].monotonic_ns == 200
    assert session.participant_attested_no_contamination is True


def test_evidence_write_failure_recovers_from_frozen_submission(tmp_path, monkeypatch):
    protocol, spec, material = _material(tmp_path, assisted=False)
    bundle = write_arm_bundle(protocol, material, tmp_path / "bundle")
    transition_session(bundle, "start", clock=_Clock(100))
    real_atomic_write = harness_module._atomic_write
    failed = False
    evaluated: list[bytes] = []

    def fail_evidence_once(path, raw):  # noqa: ANN001
        nonlocal failed
        if path.name == "session-evidence.json" and not failed:
            failed = True
            raise OSError("evidence disk unavailable")
        return real_atomic_write(path, raw)

    def evaluate(work):
        raw = (work / "data/example.txt").read_bytes()
        evaluated.append(raw)
        return b"patch-bytes\n", _verdict()

    monkeypatch.setattr(harness_module, "_atomic_write", fail_evidence_once)
    with pytest.raises(OSError, match="evidence disk unavailable"):
        finalize_session(
            protocol,
            spec,
            bundle,
            evaluator=evaluate,
            participant_attested_no_contamination=True,
            clock=_Clock(200),
        )

    assert (bundle / "final.patch").read_bytes() == b"patch-bytes\n"
    assert not (bundle / "session-evidence.json").exists()
    live = bundle / "work/data/example.txt"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"late editor save\n")

    recovered = finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=evaluate,
        participant_attested_no_contamination=True,
        clock=_Clock(),
    )

    assert recovered.events[-1].monotonic_ns == 200
    assert evaluated == [b"mission Before\n", b"mission Before\n"]
    assert (bundle / "session-evidence.json").is_file()
