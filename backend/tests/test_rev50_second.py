"""REV50-SECOND: second-pass regression tests over REV49's merged changes.

Each test pins a defect found when re-reviewing REV49's own diffs —
they fail against the pre-fix implementation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cad_body_geometry import (  # noqa: E402
    DOCUMENT_ID,
    _furniture,
    _scene,
)
from test_rev44_install_surfaces import _app  # noqa: E402

from htdt.acceptance_checks import (  # noqa: E402
    AutoCheckResult,
    check_persistence_probe,
)
from htdt.acceptance_page import _apply_check_result  # noqa: E402
from htdt.cad_acceptance import AcceptanceStepRecord  # noqa: E402
from htdt.cad_acceptance_repository import AcceptanceRunRepository  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_review_package import derived_yaw_steps  # noqa: E402
from htdt.cad_scene import EntityBodyGeometry  # noqa: E402
from htdt.room_workspace import (  # noqa: E402
    RoomWorkspaceController,
    SelectionInspector,
    _UNSET,
)


def _result(verdict: str, detail: str = 'detail'):
    return AutoCheckResult(
        verdict=verdict, detail_ja=detail, evidence={'k': 'v'}
    )


def _ctx(tmp_path: Path, repo, *, boot_id=None):
    from htdt.acceptance_checks import CheckContext

    return CheckContext(
        data_dir=tmp_path,
        db_path=tmp_path / 'cad-scenes.sqlite3',
        rew_base_url='http://127.0.0.1:4735',
        run_id='ac-test',
        step_id='step-1',
        repository=repo,
        boot_id=boot_id,
    )


# ---------------------------------------------------------------------------
# 受入検証: a dropped worker result must not commit a phantom revision
# ---------------------------------------------------------------------------


def test_check_result_dropped_on_decided_step_returns_none() -> None:
    steps = [
        AcceptanceStepRecord(
            step_id='step-1',
            kind='auto',
            auto_check='probe',
            status='passed',
            verdict_source='human_confirm',
        )
    ]
    assert (
        _apply_check_result(steps, 'step-1', _result('pass'), False) is None
    )


def test_check_result_dropped_on_unknown_step_returns_none() -> None:
    steps = [
        AcceptanceStepRecord(
            step_id='step-1', kind='auto', auto_check='probe'
        )
    ]
    assert (
        _apply_check_result(steps, 'ghost', _result('pass'), False) is None
    )


def test_check_result_pending_step_mutates() -> None:
    steps = [
        AcceptanceStepRecord(
            step_id='step-1', kind='auto', auto_check='probe'
        )
    ]
    updated = _apply_check_result(steps, 'step-1', _result('pass'), False)
    assert updated is not None
    assert updated[0].status == 'passed'
    assert updated[0].verdict_source == 'auto_check'


def test_check_result_unavailable_stays_pending() -> None:
    steps = [
        AcceptanceStepRecord(
            step_id='step-1', kind='auto', auto_check='probe'
        )
    ]
    updated = _apply_check_result(
        steps, 'step-1', _result('unavailable', 'REW down'), False
    )
    assert updated is not None
    assert updated[0].status == 'pending'
    assert updated[0].check_detail['verdict'] == 'unavailable'


# ---------------------------------------------------------------------------
# 受入検証: persistence probe — a non-dict JSON marker is corrupt, not a crash
# ---------------------------------------------------------------------------


def test_persistence_probe_reissues_non_dict_marker(tmp_path: Path) -> None:
    db = tmp_path / 'cad-scenes.sqlite3'
    repo = AcceptanceRunRepository(db)
    # A foreign/corrupt probe asset that parses as JSON but is not an
    # object — previously hit ``marker.get`` → AttributeError surfaced as
    # a cryptic 'unavailable' instead of a clean re-issue.
    repo.attach_evidence(
        'ac-test',
        'step-1',
        kind='persistence_probe',
        filename='persistence-probe.json',
        payload=json.dumps([1, 2, 3]).encode('utf-8'),
    )
    restarted = _ctx(
        tmp_path, AcceptanceRunRepository(db), boot_id='other-boot'
    )
    result = check_persistence_probe(restarted, '')
    assert result.verdict == 'deferred'
    assert result.evidence.get('probe_recorded') is True


# ---------------------------------------------------------------------------
# 部屋 inspector: radius baseline must be unit-independent (SI authority)
# ---------------------------------------------------------------------------


def test_radius_dirty_flag_survives_display_unit_switch(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    entity = _furniture(
        'column',
        body=EntityBodyGeometry(kind='cylinder', radius_m=0.35),
    )
    repository.save(_scene(entity), parent_revision_id=None)
    controller = RoomWorkspaceController(repository, DOCUMENT_ID)
    controller.set_selection('column')

    _app()
    inspector = SelectionInspector()
    inspector.set_entity(entity, editable=True)
    # Baseline was stored in DISPLAY units pre-fix — a unit switch left a
    # phantom dirty flag that committed a spurious body_geometry rewrite.
    inspector.set_display_units(length_unit='inch')
    assert inspector._body_geometry_edited(entity) is _UNSET
    inspector.set_display_units(length_unit='mm')
    assert inspector._body_geometry_edited(entity) is _UNSET
    # And a real edit still registers.
    inspector.radius_field.set_value_m(0.4)
    assert inspector._body_geometry_edited(entity) is not _UNSET
    inspector.deleteLater()


# ---------------------------------------------------------------------------
# Field explorer: probe_x must carry the same accessible name as Y/Z
# ---------------------------------------------------------------------------


def test_probe_axes_have_accessible_names(tmp_path: Path) -> None:
    from htdt.cad_prediction_repository import CadPredictionRepository
    from htdt.field_explorer_panel import FieldExplorerPanel

    _app()
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    prediction_repository = CadPredictionRepository(scene_repository)
    panel = FieldExplorerPanel(
        scene_repository, prediction_repository, 'rev50-doc'
    )
    try:
        for axis_spin in (panel.probe_x, panel.probe_y, panel.probe_z):
            assert axis_spin.accessibleName()
    finally:
        panel.deleteLater()
        _app().processEvents()


# ---------------------------------------------------------------------------
# Review package: derived_yaw_steps validates the step it is handed
# ---------------------------------------------------------------------------


def test_derived_yaw_steps_rejects_non_positive_step() -> None:
    with pytest.raises(ValueError):
        derived_yaw_steps(0)
    with pytest.raises(ValueError):
        derived_yaw_steps(-15)


def test_derived_yaw_steps_matches_declared_step() -> None:
    assert derived_yaw_steps(30) == (-30, 30, -60, 60, -90, 90, -120, 120)
    assert derived_yaw_steps(60) == (-60, 60, -120, 120)
    # A step that does not divide the reach truncates to multiples inside.
    assert derived_yaw_steps(45) == (-45, 45, -90, 90)
