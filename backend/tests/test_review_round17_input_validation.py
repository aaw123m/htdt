"""Round-17 input-validation regressions.

Covers the fixes from docs/reviews/round17-input-validation.md:
- the measurement capture form returned parse-level errors but let
  model-level validation escape as an uncaught exception — a parsed
  ``sample_rate_hz <= 0`` or a non-finite ``avr_volume_db`` died inside
  the slot with no notice;
- ``CriterionRule`` accepted a non-finite float ``expected`` for
  ``equals`` — ``_compare`` then failed every observation silently and
  the canonical-JSON digest crashed at save with a cryptic error.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, Size3, make_f1_scene
from htdt.cad_standards import CriterionRule, _compare
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _workspace(tmp_path: Path) -> MeasurementPageWorkspace:
    _app()
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return MeasurementPageWorkspace(controller)


# ---------------------------------------------------------------------
# Measurement capture form: parsed-but-invalid values must return a
# Japanese notice, never an uncaught ValidationError.


@pytest.mark.parametrize("bad_rate", ["-5", "0", "-1"])
def test_capture_form_rejects_nonpositive_sample_rate(
    tmp_path: Path, bad_rate: str
) -> None:
    workspace = _workspace(tmp_path)
    workspace.mic_sample_rate_edit.setText(bad_rate)

    capture, _direction, error = workspace._collect_acquisition_capture()

    assert capture is None
    assert error is not None
    assert "整数" in error


@pytest.mark.parametrize("bad_volume", ["nan", "inf", "-inf", "1e999"])
def test_capture_form_rejects_nonfinite_avr_volume(
    tmp_path: Path, bad_volume: str
) -> None:
    workspace = _workspace(tmp_path)
    workspace.avr_volume_edit.setText(bad_volume)

    capture, _direction, error = workspace._collect_acquisition_capture()

    assert capture is None
    assert error is not None
    assert "dB" in error


def test_capture_form_accepts_valid_rate_and_volume(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.mic_sample_rate_edit.setText("48000")
    workspace.avr_volume_edit.setText("-12.5")

    capture, _direction, error = workspace._collect_acquisition_capture()

    assert error is None
    assert capture is not None
    assert capture.sample_rate_hz == 48000
    assert capture.playback is not None
    assert capture.playback.avr_volume_db == -12.5


def test_capture_form_rejects_nonnumeric_rate_and_volume(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.mic_sample_rate_edit.setText("abc")
    capture, _direction, error = workspace._collect_acquisition_capture()
    assert capture is None
    assert error is not None

    workspace.mic_sample_rate_edit.setText("")
    workspace.avr_volume_edit.setText("db")
    capture, _direction, error = workspace._collect_acquisition_capture()
    assert capture is None
    assert error is not None


# ---------------------------------------------------------------------
# CriterionRule: a non-finite float expected value for ``equals`` is
# rejected at construction (previously accepted — silent FAIL on every
# comparison plus a canonical-JSON digest crash at save).


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_criterion_rule_equals_rejects_nonfinite_expected(bad: float) -> None:
    with pytest.raises(ValueError):
        CriterionRule(operator="equals", expected=bad)


def test_criterion_rule_equals_still_accepts_legitimate_expected() -> None:
    numeric = CriterionRule(operator="equals", expected=-20.0)
    assert _compare(-20.0, numeric)

    integral = CriterionRule(operator="equals", expected=2)
    assert _compare(2, integral)

    textual = CriterionRule(operator="equals", expected="standard")
    assert _compare("standard", textual)
    assert not _compare("other", textual)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_size3_rejects_nonfinite_dimensions(bad: float) -> None:
    with pytest.raises(ValueError):
        Size3(x_m=bad, y_m=0.1, z_m=0.1)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_room_prism_rejects_nonfinite_dimensions(bad: float) -> None:
    with pytest.raises(ValueError):
        RoomPrism(width_m=6.0, depth_m=4.0, height_m=bad)


def test_size3_and_room_prism_still_accept_legitimate_values() -> None:
    Size3(x_m=0.001, y_m=10.0, z_m=1e9)
    RoomPrism(width_m=5e-324, depth_m=4.0, height_m=2.4)
