"""Round-8 measurement→comparison→report journey regressions.

Covers the fixes from docs/reviews/round8-journey.md:
- single-file imports carry the batch lane's duplicate classification;
- one measurement whose persisted dataset fails re-verification degrades
  to a visible-but-unusable row instead of crashing every listing;
- saved-comparison history reload re-seats the dataset selectors on the
  persisted A/B pair and renders the persisted identity + verdict;
- the analysis export carries the comparison verdict scalars and records
  omitted evidence instead of silently writing a partial file.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from htdt.analysis_export import (
    build_analysis_export,
    comparison_metadata_entries,
    render_analysis_csv,
    render_analysis_json,
    series_from_comparison,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import (
    MeasurementAssignment,
    MeasurementWorkflowController,
)
from htdt.rew_api import (
    RewFrequencyResponseSnapshot,
    decode_frequency_response,
    normalize_measurement_summaries,
)

FIXTURES = Path(__file__).parent / "fixtures"
REW_UUID = "01628624-ee2a-4a0f-99bb-9cf9e1b9c859"


REW_TEXT = b"20 70\n40 71\n80 69\n"
REW_TEXT_B = b"20 60\n40 62\n80 61\n"
REW_TEXT_C = b"20 55\n40 57\n80 56\n"


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _controller(scene_repository, document_id):
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    controller = MeasurementWorkflowController(
        scene_repository,
        document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return controller, measurement_repository, quality_repository


def _assignment(**overrides) -> MeasurementAssignment:
    values = dict(
        measurement_entity_id="point-mlp",
        evidence_type="measured",
        channel_role="front_left",
        source_speaker_ids=("speaker-fl",),
        radiation_scope="single",
        routing_evidence="manual",
    )
    values.update(overrides)
    return MeasurementAssignment(**values)


def _commit(controller, raw: bytes = REW_TEXT, evidence_type: str = "measured"):
    controller.stage_rew_text(raw, "import.txt")
    return controller.commit_pending(_assignment(evidence_type=evidence_type))


def _snapshot(external_id: str | None = None) -> RewFrequencyResponseSnapshot:
    """Real REW FR fixture — the pinned importer replays raw bytes on save."""
    summaries = normalize_measurement_summaries(
        json.loads(
            (FIXTURES / "rew_5_40_beta135_measurements.json").read_text(
                encoding="utf-8"
            )
        )
    )
    summary = dict(summaries[0])
    if external_id is not None:
        summary["uuid"] = external_id
    raw_fr = json.loads(
        (FIXTURES / "rew_5_40_beta135_frequency_response.json").read_text(
            encoding="utf-8"
        )
    )
    decoded = decode_frequency_response(
        external_id or summary["uuid"],
        raw_fr,
        requested_unit="SPL",
        requested_ppo=96,
    )
    return RewFrequencyResponseSnapshot(
        summary, {"unit": "SPL", "ppo": 96}, raw_fr, decoded
    )


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _workspace(tmp_path: Path):
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, quality_repository = _controller(
        scene_repository, revision.document_id
    )
    return controller, MeasurementPageWorkspace(controller)


# ---------------------------------------------------------------------
# Single-file lane duplicate classification (J5)


def test_single_import_flags_exact_duplicate_and_survives_revision_rebind(
    tmp_path: Path,
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    record = _commit(controller)

    pending = controller.stage_rew_text(REW_TEXT, "reexport.txt")
    assert pending.duplicate_kind == "exact_duplicate"
    assert pending.duplicate_of_measurement_id == record.measurement_id

    # Revision re-choice keeps the classification — the pending is rebuilt
    # from the same raw bytes, not reset to 'new'.
    rebound = controller.select_pending_revision(revision.revision_id)
    assert rebound.duplicate_kind == "exact_duplicate"
    assert rebound.duplicate_of_measurement_id == record.measurement_id

    # Advisory only: an explicit commit still creates a distinct record.
    duplicate = controller.commit_pending(_assignment())
    assert duplicate.measurement_id != record.measurement_id
    assert len(measurement_repository.list_measurements(revision.document_id)) == 2


def test_single_snapshot_import_flags_same_acquisition(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)

    controller.stage_rew_snapshot(_snapshot())
    record = controller.commit_pending(_assignment())

    pending = controller.stage_rew_snapshot(_snapshot())
    assert pending.duplicate_kind == "same_acquisition"
    assert pending.duplicate_of_measurement_id == record.measurement_id

    # A different REW measurement id is a genuinely new acquisition.
    fresh = controller.stage_rew_snapshot(_snapshot("rew-ext-2"))
    assert fresh.duplicate_kind == "new"
    assert fresh.duplicate_of_measurement_id is None

    fresh = controller.stage_rew_snapshot(_snapshot("rew-ext-2"))
    assert fresh.duplicate_kind == "new"
    assert fresh.duplicate_of_measurement_id is None


# ---------------------------------------------------------------------
# Per-row isolation of failed authoritative reads (J4)


def _corrupt_raw_asset(
    measurement_repository: CadMeasurementRepository, measurement_id: str
) -> None:
    dataset = measurement_repository.dataset_for_measurement(measurement_id)
    assert dataset is not None
    asset_path = measurement_repository._asset_path(dataset.source_sha256)
    assert asset_path.exists()
    asset_path.write_bytes(b"tampered")


def test_measurement_views_isolate_unverifiable_dataset(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, measurement_repository, _ = _controller(
        scene_repository, revision.document_id
    )
    broken = _commit(controller)
    good = _commit(controller, REW_TEXT_B)
    _corrupt_raw_asset(measurement_repository, broken.measurement_id)

    views = {v.measurement_id: v for v in controller.measurement_views()}
    assert len(views) == 2
    assert views[broken.measurement_id].dataset_id is None
    assert views[broken.measurement_id].dataset_error
    assert views[good.measurement_id].dataset_id is not None
    assert views[good.measurement_id].dataset_error is None

    candidate_ids = {
        row.measurement_id for row in controller.comparison_candidates()
    }
    assert good.measurement_id in candidate_ids
    assert broken.measurement_id not in candidate_ids


def test_workspace_survives_corrupt_dataset_and_comparison_history(
    tmp_path: Path,
) -> None:
    _app()
    controller, workspace = _workspace(tmp_path)
    try:
        broken = _commit(controller)
        good = _commit(controller, REW_TEXT_B, evidence_type="predicted")
        workspace.refresh()
        workspace.set_context("comparison")
        # #564: measured×predicted requires a persisted registration record
        # before the comparison can be saved.
        workspace.register_pair_button.click()
        workspace.compare_button.click()
        assert workspace.comparison_history.rowCount() == 1

        measurement_repository = controller.measurement_repository
        _corrupt_raw_asset(measurement_repository, broken.measurement_id)
        workspace.refresh()  # must not raise

        # Listing degraded: the comparison history could not be replay-
        # verified, so it reads empty with an explicit error notice.
        assert workspace.comparison_history.rowCount() == 0
        assert "比較履歴を読み込めませんでした" in workspace.notice.text()

        # The corrupt row is still listed, flagged, and excluded.
        statuses = {
            workspace.quality_table.item(r, 3).text()
            for r in range(workspace.quality_table.rowCount())
        }
        assert "検証エラー" in statuses
        quality_row = next(
            r
            for r in range(workspace.quality_table.rowCount())
            if workspace.quality_table.item(r, 3).text() == "検証エラー"
        )
        workspace.quality_table.selectRow(quality_row)
        assert "検証できません" in workspace.quality_detail.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


# ---------------------------------------------------------------------
# Comparison verdict travels with exports (J3)


def test_comparison_metadata_entries_carry_verdict(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    controller, _, _ = _controller(scene_repository, revision.document_id)
    measured = _commit(controller)
    predicted = _commit(controller, REW_TEXT_B, evidence_type="predicted")

    views = controller.measurement_views()
    dataset_a = next(
        v.dataset_id for v in views if v.measurement_id == measured.measurement_id
    )
    dataset_b = next(
        v.dataset_id for v in views if v.measurement_id == predicted.measurement_id
    )
    comparison = controller.compare_datasets(
        dataset_a, dataset_b, low_hz=20.0, high_hz=80.0
    )
    assert comparison.comparison_id

    entries = {entry.key: entry.value for entry in comparison_metadata_entries(comparison)}
    prefix = f"comparison.{comparison.comparison_id}"
    assert entries[f"{prefix}.algorithm_version"] == comparison.algorithm_version
    assert entries[f"{prefix}.label_a"] == comparison.label_a
    assert entries[f"{prefix}.label_b"] == comparison.label_b
    assert entries[f"{prefix}.level_compatibility"] == comparison.level_compatibility
    assert entries[f"{prefix}.valid_points"] == str(comparison.valid_points)
    assert f"{prefix}.mean_difference_db" in entries
    assert f"{prefix}.rms_difference_db" in entries

    bundle = build_analysis_export(
        document_id=revision.document_id,
        title="t",
        generated_at_utc="2026-09-27T00:00:00+00:00",
        series=(series_from_comparison(comparison),),
        metadata=tuple(comparison_metadata_entries(comparison)),
    )
    csv_text = render_analysis_csv(bundle)
    assert f"{prefix}.mean_difference_db" in csv_text
    assert f"{prefix}.label_a" in csv_text
    json_text = render_analysis_json(bundle)
    assert prefix in json_text


# ---------------------------------------------------------------------
# Saved comparison reload re-seats the persisted pair (J2)


def test_history_selection_rebinds_saved_pair_and_verdict(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        first = _commit(controller)
        _commit(controller, REW_TEXT_B)
        predicted = _commit(controller, REW_TEXT_C, evidence_type="predicted")
        workspace.refresh()
        workspace.set_context("comparison")

        views = {v.measurement_id: v for v in controller.measurement_views()}
        a_dataset = views[first.measurement_id].dataset_id
        other_dataset = next(
            v.dataset_id
            for v in views.values()
            if v.dataset_id not in {a_dataset, views[predicted.measurement_id].dataset_id}
        )
        b_dataset = views[predicted.measurement_id].dataset_id

        a_index = workspace.measured_combo.findData(a_dataset)
        assert a_index >= 0
        workspace.measured_combo.setCurrentIndex(a_index)
        b_index = workspace.predicted_combo.findData(b_dataset)
        assert b_index >= 0
        workspace.predicted_combo.setCurrentIndex(b_index)
        # #564: measured×predicted requires a persisted registration record
        # before the comparison can be saved.
        workspace.register_pair_button.click()
        workspace.compare_button.click()
        assert workspace.comparison_history.rowCount() == 1

        # Move the selectors to a different pair, then reload the saved row.
        other_index = workspace.measured_combo.findData(other_dataset)
        assert other_index >= 0
        workspace.measured_combo.setCurrentIndex(other_index)
        assert workspace.measured_combo.currentData() == other_dataset

        workspace.comparison_history.selectRow(0)
        assert workspace.measured_combo.currentData() == a_dataset
        assert workspace.predicted_combo.currentData() == b_dataset
        result_text = workspace.comparison_result.text()
        assert "A:" in result_text and "B:" in result_text
        assert "レベル互換性:" in result_text
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_single_import_commit_confirms_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller)
        controller.stage_rew_text(REW_TEXT, "reexport.txt")
        workspace.refresh()
        assert "既に保存されています" in workspace.pending_import_label.text()

        calls: list[bool] = []

        def _confirm(_self, *_args, **_kwargs):
            calls.append(True)
            return QMessageBox.StandardButton.Yes

        # REV32: the duplicate check is an instance QMessageBox with a help
        # button — the prompt is intercepted through exec() now.
        monkeypatch.setattr(QMessageBox, "exec", _confirm)
        workspace.set_context("assignment")
        pending_index = -1
        for index in range(workspace.assignment_scope_combo.count()):
            if workspace.assignment_scope_combo.itemData(index) == ("pending", None):
                pending_index = index
                break
        assert pending_index >= 0
        workspace.assignment_scope_combo.setCurrentIndex(pending_index)
        if workspace.source_speaker_list.count():
            workspace.source_speaker_list.item(0).setCheckState(
                Qt.CheckState.Checked
            )
        workspace._commit_assignment()
        assert calls, "duplicate commit must ask for confirmation"
        assert (
            len(
                controller.measurement_repository.list_measurements(
                    controller.document_id
                )
            )
            == 2
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
