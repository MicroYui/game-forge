"""GameForge-Bench clean-base identity must be checkout independent."""

from __future__ import annotations

import shutil

from gameforge.bench import bases


def test_clean_base_snapshot_identity_is_checkout_independent(tmp_path, monkeypatch):
    expected = bases.clean_base()
    relocated = tmp_path / "another-checkout" / "scenarios" / "defects" / "clean"
    shutil.copytree(bases._CLEAN_DIR, relocated)
    monkeypatch.setattr(bases, "_CLEAN_DIR", relocated)

    actual = bases.clean_base()

    assert actual.snapshot_id == expected.snapshot_id
    source_files = {
        source_ref.file
        for source_ref in [
            *(entity.source_ref for entity in actual.entities.values()),
            *(relation.source_ref for relation in actual.relations.values()),
        ]
        if source_ref is not None
    }
    assert source_files == {"scenarios/defects/clean"}
