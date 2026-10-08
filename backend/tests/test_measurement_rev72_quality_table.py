"""REV72-QTABLE (#969): quality-page saved-measurement table — combinable
filters with honest counts, id-keyed selection, adaptive detail layout.

The table's visual order is decoupled from record identity (sorting and
filtering reorder/hide rows), so every row→record lookup resolves the
measurement_id bound on each item's UserRole — these tests pin that down.
"""
from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneEntity, make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController


def _saved_scene(tmp_path: Path, extra_entities=()):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    document = make_f1_scene()
    if extra_entities:
        document = document.model_copy(
            update={"entities": document.entities + tuple(extra_entities)}
        )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    return scene_repository, revision


def _seat_point() -> SceneEntity:
    return SceneEntity(
        entity_id="point-seat2",
        kind="measurement_point",
        name="Seat 2",
        position=Position3(x_m=3.6, y_m=3.0, z_m=1.1),
    )


def _save_dataset(
    measurement_repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    entity_id: str = "point-mlp",
    channel_role: str = "front_left",
    quality_status: str = "unknown",
):
    processing = {"fixture_raw": measurement_id}
    source = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        entity_id,
        measurement_id=measurement_id,
        evidence_type="measured",
        channel_role=channel_role,
        source_kind="unknown",
        quality_status=quality_status,
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f"dataset-{measurement_id}",
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status="valid",
        processing_json=canonical_json(processing),
        source_sha256=sha256(source).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename=f"{measurement_id}.json",
        raw_bytes=source,
    )
    return record, dataset


def _save_retake_report(
    quality_repository: CadMeasurementQualityRepository,
    record,
    dataset,
) -> None:
    """A replay-validated report with a clipping observation: the honest
    verdict is 要再測定 (RETAKE), so the row exercises attention facets."""
    observation = build_measurement_observation(
        observation_id=f"obs-{record.measurement_id}",
        measurement_id=record.measurement_id,
        source_kind="manual",
        observed_at_utc="2026-09-22T01:03:00+00:00",
        clipping_detected=True,
    )
    quality_repository.save_observation(observation)
    quality_repository.save_report(
        build_measurement_quality_report(
            measurement=record,
            dataset=dataset,
            evidence=CadMeasurementQualityEvidence(
                clipping_detected=True,
                evidence_source="manual",
            ),
            profile=build_measurement_quality_profile(),
            observation=observation_binding(observation),
        )
    )


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path: Path, specs, extra_entities=()):
    """specs: iterable of dicts for _save_dataset; returns (ws, repos...)."""
    _app()
    scene_repository, revision = _saved_scene(tmp_path, extra_entities)
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(
        measurement_repository
    )
    saved = {}
    for spec in specs:
        spec = dict(spec)
        measurement_id = spec.pop("measurement_id")
        retake = spec.pop("retake", False)
        record, dataset = _save_dataset(
            measurement_repository, revision, measurement_id, **spec
        )
        if retake:
            _save_retake_report(quality_repository, record, dataset)
        saved[measurement_id] = record
    controller = MeasurementWorkflowController(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    workspace = MeasurementPageWorkspace(controller)
    workspace.set_context("quality")
    _app().processEvents()
    return workspace, saved


def _row_ids(workspace: MeasurementPageWorkspace) -> list[str]:
    ids = []
    for row_index in range(workspace.quality_table.rowCount()):
        item = workspace.quality_table.item(row_index, 0)
        ids.append(item.data(Qt.ItemDataRole.UserRole))
    return ids


def _select_id(workspace: MeasurementPageWorkspace, measurement_id: str) -> int:
    row_index = workspace._quality_row_index_for_id(measurement_id)
    assert row_index is not None, f"{measurement_id} not visible"
    workspace.quality_table.selectRow(row_index)
    _app().processEvents()
    return row_index


def _set_combo_data(combo, data) -> None:
    index = combo.findData(data)
    assert index >= 0, f"combo has no entry for {data!r}"
    combo.setCurrentIndex(index)
    _app().processEvents()


# -- combinable filters + honest counts -----------------------------------


def test_filters_combine_and_count_is_honest(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-fl-mlp"},
            {"measurement_id": "m-c-mlp", "channel_role": "center"},
            {
                "measurement_id": "m-fl-seat",
                "entity_id": "point-seat2",
            },
        ],
        extra_entities=(_seat_point(),),
    )
    try:
        assert workspace.quality_table.rowCount() == 3
        assert workspace.quality_count_label.text() == "3件中3件を表示"

        _set_combo_data(workspace.quality_channel_filter, "front_left")
        assert workspace.quality_table.rowCount() == 2
        assert workspace.quality_count_label.text() == "3件中2件を表示"
        assert set(_row_ids(workspace)) == {"m-fl-mlp", "m-fl-seat"}

        # Filters intersect — channel + position narrows to one row.
        _set_combo_data(workspace.quality_position_filter, "Seat 2")
        assert workspace.quality_table.rowCount() == 1
        assert _row_ids(workspace) == ["m-fl-seat"]
        assert workspace.quality_count_label.text() == "3件中1件を表示"

        workspace.quality_filter_clear.click()
        _app().processEvents()
        assert workspace.quality_table.rowCount() == 3
        assert workspace.quality_count_label.text() == "3件中3件を表示"
        assert workspace.quality_channel_filter.currentIndex() == 0
        assert workspace.quality_position_filter.currentIndex() == 0
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_verdict_and_state_facets(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-ok"},
            {"measurement_id": "m-bad", "quality_status": "invalid"},
            {"measurement_id": "m-retake", "retake": True},
        ],
    )
    try:
        verdict_labels = [
            workspace.quality_verdict_combo.itemText(i)
            for i in range(workspace.quality_verdict_combo.count())
        ]
        assert "品質: 未確認" in verdict_labels
        assert "品質: invalid" in verdict_labels

        _set_combo_data(workspace.quality_verdict_combo, "invalid")
        assert _row_ids(workspace) == ["m-bad"]

        workspace.quality_verdict_combo.setCurrentIndex(0)
        _set_combo_data(workspace.quality_state_combo, "retake")
        assert _row_ids(workspace) == ["m-retake"]

        _set_combo_data(workspace.quality_state_combo, "attention")
        assert set(_row_ids(workspace)) == {"m-bad", "m-retake"}
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_search_matches_id_ja_role_and_position(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-fl-mlp"},
            {
                "measurement_id": "m-sub-seat",
                "entity_id": "point-seat2",
                "channel_role": "subwoofer",
            },
        ],
        extra_entities=(_seat_point(),),
    )
    try:
        workspace.quality_search_edit.setText("m-sub")
        _app().processEvents()
        assert _row_ids(workspace) == ["m-sub-seat"]

        # JA channel-role labels are searchable too.
        workspace.quality_search_edit.setText("サブウーファー")
        _app().processEvents()
        assert _row_ids(workspace) == ["m-sub-seat"]

        workspace.quality_search_edit.setText("seat 2")
        _app().processEvents()
        assert _row_ids(workspace) == ["m-sub-seat"]

        workspace.quality_search_edit.setText("存在しない")
        _app().processEvents()
        assert workspace.quality_table.rowCount() == 0
        assert workspace.quality_count_label.text() == "2件中0件を表示"
        assert workspace.quality_detail.text() == (
            "絞り込み条件に一致する測定はありません"
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


# -- id-keyed selection ----------------------------------------------------


def test_selection_identity_survives_sort_and_refresh(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-a"},
            {"measurement_id": "m-b", "channel_role": "center"},
            {"measurement_id": "m-c", "channel_role": "subwoofer"},
        ],
    )
    try:
        _select_id(workspace, "m-b")
        assert workspace._selected_quality_view().measurement_id == "m-b"

        # Sorting reorders visual rows — the UserRole id, not the index,
        # binds row to record.
        workspace.quality_table.sortByColumn(
            0, Qt.SortOrder.DescendingOrder
        )
        _app().processEvents()
        selected = workspace.quality_table.selectedItems()
        assert selected[0].data(Qt.ItemDataRole.UserRole) == "m-b"
        assert (
            workspace._selected_quality_view().measurement_id == "m-b"
        )
        assert "測定ID: m-b" in workspace.quality_detail.text()

        # refresh() restores selection keyed on the same id.
        workspace.refresh()
        _app().processEvents()
        selected = workspace.quality_table.selectedItems()
        assert selected[0].data(Qt.ItemDataRole.UserRole) == "m-b"
        assert workspace.quality_selection_note.text() == ""
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_filter_hidden_selection_states_reason(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-fl"},
            {"measurement_id": "m-c", "channel_role": "center"},
        ],
    )
    try:
        _select_id(workspace, "m-c")
        _set_combo_data(workspace.quality_channel_filter, "front_left")

        # The hidden selection is never silently moved to another record —
        # the note states why it is gone.
        assert _row_ids(workspace) == ["m-fl"]
        assert "絞り込み条件で非表示" in workspace.quality_selection_note.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_vanished_selection_states_reason(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [{"measurement_id": "m-fl"}],
    )
    try:
        _select_id(workspace, "m-fl")
        # Measurements are never deleted through the repository, so the
        # "vanished" wording is exercised on the note path directly.
        workspace._update_quality_selection_note("m-gone")
        assert "現在の一覧にありません" in (
            workspace.quality_selection_note.text()
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_deep_link_reaches_filter_hidden_row(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-fl"},
            {"measurement_id": "m-c", "channel_role": "center"},
        ],
    )
    try:
        _set_combo_data(workspace.quality_channel_filter, "front_left")
        assert _row_ids(workspace) == ["m-fl"]

        # A deep link must reach its row even when filters hide it —
        # the filters reset instead of selecting invisibly.
        assert workspace.select_measurement_id("m-c")
        _app().processEvents()
        assert workspace.quality_channel_filter.currentIndex() == 0
        selected = workspace.quality_table.selectedItems()
        assert selected[0].data(Qt.ItemDataRole.UserRole) == "m-c"
        assert workspace._selected_quality_view().measurement_id == "m-c"

        assert not workspace.select_measurement_id("m-absent")
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


# -- detail region ---------------------------------------------------------


def test_detail_names_strict_id_and_next_action(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [
            {"measurement_id": "m-ok"},
            {"measurement_id": "m-retake", "retake": True},
        ],
    )
    try:
        _select_id(workspace, "m-retake")
        detail = workspace.quality_detail.text()
        assert "測定ID: m-retake" in detail
        assert "MLP" in detail
        # Next-action path is stated, not implied by a good/bad score.
        assert "再測定を推奨" in workspace.retake_label.text()
        assert workspace.retake_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_empty_listing_is_honest(tmp_path: Path) -> None:
    workspace, _saved = _workspace(tmp_path, [])
    try:
        assert workspace.quality_table.rowCount() == 0
        assert workspace.quality_count_label.text() == "0件中0件を表示"
        assert workspace.quality_detail.text() == "保存済み測定はありません"
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


# -- adaptive layout -------------------------------------------------------


def test_narrow_width_stacks_detail_below_table(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [{"measurement_id": "m-fl"}],
    )
    try:
        workspace.show()
        workspace.resize(1280, 720)
        _app().processEvents()
        assert (
            workspace.quality_split.orientation()
            == Qt.Orientation.Horizontal
        )

        workspace.resize(900, 720)
        _app().processEvents()
        assert (
            workspace.quality_split.orientation()
            == Qt.Orientation.Vertical
        )

        workspace.resize(1280, 720)
        _app().processEvents()
        assert (
            workspace.quality_split.orientation()
            == Qt.Orientation.Horizontal
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()


def test_a11y_names_on_filter_controls(tmp_path: Path) -> None:
    workspace, _saved = _workspace(
        tmp_path,
        [{"measurement_id": "m-fl"}],
    )
    try:
        assert workspace.quality_verdict_combo.accessibleName()
        assert workspace.quality_state_combo.accessibleName()
        assert workspace.quality_channel_filter.accessibleName()
        assert workspace.quality_position_filter.accessibleName()
        assert workspace.quality_search_edit.accessibleName()
        assert workspace.quality_filter_clear.accessibleName()
        assert workspace.quality_table.accessibleName()
        assert workspace.quality_detail.accessibleName()
        # Detail text is keyboard-selectable and focusable.
        assert workspace.quality_detail.focusPolicy() != Qt.FocusPolicy.NoFocus
        interactions = workspace.quality_detail.textInteractionFlags()
        assert interactions & Qt.TextInteractionFlag.TextSelectableByKeyboard
    finally:
        workspace.close()
        workspace.deleteLater()
        _app().processEvents()
