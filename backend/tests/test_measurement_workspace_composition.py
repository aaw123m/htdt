from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication, QPushButton, QSplitter

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementAssignment, MeasurementWorkflowController
from htdt.workflow_navigation import (
    CANONICAL_WORKSPACE_CONTEXTS,
    WorkspaceId,
    normalize_workspace_context,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    return controller, MeasurementPageWorkspace(controller)


def _commit(controller, entity: str, evidence: str, role: str, raw: bytes):
    controller.stage_rew_text(raw, f"{evidence}.txt")
    return controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id=entity,
            evidence_type=evidence,
            channel_role=role,
            source_speaker_ids=("speaker-fl",),
        )
    )


def test_every_declared_context_selects_a_real_page(tmp_path: Path) -> None:
    """#786: a visible context tab must never raise unknown measurement context."""
    app = _app()
    _, workspace = _workspace(tmp_path)
    try:
        for context in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.MEASUREMENT]:
            workspace.set_context(context.context_id)
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_calibration_context_selects_the_onboarding_page(
    tmp_path: Path,
) -> None:
    """#786: 'calibration' deep links land on the instrument-onboarding page
    (promoted to a canonical context in round 7 — it no longer aliases to
    'campaign')."""
    assert (
        normalize_workspace_context(WorkspaceId.MEASUREMENT, "calibration")
        == "calibration"
    )
    app = _app()
    _, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("calibration")
        assert workspace.pages.currentIndex() == 5
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_persistent_context_tracks_selection(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        workspace.refresh()
        workspace.set_context("quality")
        workspace.quality_table.selectRow(0)
        context = workspace.context_label.text()
        assert "フロント左" in context
        assert "実測" in context
        assert "品質" in context
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_quality_page_uses_split_layout(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("quality")
        splitters = workspace.findChildren(QSplitter)
        assert splitters, "quality page must place table and detail side by side"
        split = splitters[0]
        children = {split.widget(i) for i in range(split.count())}
        assert workspace.quality_table.parentWidget() in children
        assert workspace.quality_detail.parentWidget() in children
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_channel_combo_uses_typed_labels_with_raw_data(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("assignment")
        assert workspace.channel_combo.itemData(1) == "front_left"
        assert "FL" in workspace.channel_combo.itemText(1)
        assert "front_left" not in workspace.channel_combo.itemText(1)
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_assignment_summary_previews_semantics(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        controller.stage_rew_text(b"20 70\n40 71\n80 69\n", "m.txt")
        workspace.refresh()
        workspace.set_context("assignment")
        summary = workspace.assignment_summary_label.text()
        assert "保存内容" in summary
        assert "実測" in summary
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_preview_saved_distinction_and_metrics(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        _commit(controller, "point-mlp", "predicted", "front_left", b"20 69\n40 70\n80 68\n")
        workspace.refresh()
        workspace.set_context("comparison")
        # #970: an unregistered measured×predicted pair is fail-closed —
        # the state label, the field-adjacent reason and the disabled save
        # button all say so instead of letting a preview pose as saveable.
        assert "比較不能" in workspace.comparison_state_label.text()
        assert "未登録" in workspace.comparison_state_label.text()
        assert "未登録" in workspace.selection_reason_label.text()
        assert "登録レコードを作成" in workspace.save_guidance_label.text()
        assert not workspace.compare_button.isEnabled()
        # The at-a-glance strip shows the selected pair's source, scene
        # binding and the registration verdict.
        summary = workspace.pair_summary_label.text()
        assert "実測" in summary and "予測" in summary
        assert "SceneRevision" in summary
        assert "未登録" in summary
        # #564: measured-vs-predicted comparison is gated on a persisted
        # registration record — registering the pair clears the blocker
        # and re-enables save.
        assert "登録" in workspace.registration_state_label.text()
        workspace.register_pair_button.click()
        assert "登録済み" in workspace.registration_state_label.text()
        assert workspace.compare_button.isEnabled()
        assert "プレビュー" in workspace.comparison_state_label.text()
        workspace.compare_button.click()
        assert "保存済み" in workspace.comparison_state_label.text()
        metrics = {
            workspace.comparison_metrics.item(r, 0).text()
            for r in range(workspace.comparison_metrics.rowCount())
        }
        assert {"RMS差", "形状RMS", "平均差", "有効点", "実帯域"} == metrics
        # Saved history selection reloads the exact persisted comparison.
        workspace.comparison_history.selectRow(0)
        assert "保存済み" in workspace.comparison_state_label.text()
        assert "保存済み比較" in workspace.context_label.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def _plot_state_text(plot: pg.PlotWidget) -> str:
    viewbox = plot.getPlotItem().getViewBox()
    return " ".join(
        child.textItem.toPlainText()
        for child in viewbox.allChildren()
        if isinstance(child, pg.TextItem)
    )


def _select_combo_by_freq_floor(combo, controller, floor_hz: float) -> None:
    """Select the combo entry whose dataset starts above ``floor_hz``."""
    for index in range(combo.count()):
        dataset_id = combo.itemData(index)
        dataset = controller.dataset(dataset_id) if dataset_id else None
        if dataset is not None and dataset.frequency_hz[0] > floor_hz:
            combo.setCurrentIndex(index)
            return
    raise AssertionError("no dataset with min freq above floor")


def _select_combo_by_freq_ceiling(combo, controller, ceiling_hz: float) -> None:
    """Select the combo entry whose dataset ends below ``ceiling_hz``."""
    for index in range(combo.count()):
        dataset_id = combo.itemData(index)
        dataset = controller.dataset(dataset_id) if dataset_id else None
        if dataset is not None and dataset.frequency_hz[-1] < ceiling_hz:
            combo.setCurrentIndex(index)
            return
    raise AssertionError("no dataset with max freq below ceiling")


def test_comparison_band_overlap_reason_and_recovery(tmp_path: Path) -> None:
    """#970: the band gate mirrors the pinned overlap rule, live."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        _commit(controller, "point-mlp", "measured", "front_right", b"40 70\n80 71\n120 69\n")
        _commit(controller, "point-mlp", "measured", "center", b"5000 70\n10000 71\n20000 69\n")
        workspace.refresh()
        workspace.set_context("comparison")
        # The auto-pair is alphabetical (C 5000+ vs FR 40–120) and already
        # fail-closed on the disjoint band rule.
        assert "帯域の交差なし" in workspace.comparison_state_label.text()
        assert "別のペア" in workspace.save_guidance_label.text()
        assert not workspace.compare_button.isEnabled()
        # Overlapping pair (A 20–80 Hz, B 40–120 Hz): the gate clears.
        _select_combo_by_freq_ceiling(workspace.measured_combo, controller, 100.0)
        _select_combo_by_freq_ceiling(workspace.predicted_combo, controller, 200.0)
        assert workspace.compare_button.isEnabled()
        assert "比較不能" not in workspace.comparison_state_label.text()
        # Narrow the requested band below the common range — blocked with
        # the nearest resolvable reason and the concrete common range.
        workspace.compare_high.setValue(30.0)
        assert "帯域の交差なし" in workspace.comparison_state_label.text()
        assert "重なり" in workspace.selection_reason_label.text()
        assert "40.0–80.0 Hz" in workspace.save_guidance_label.text()
        assert not workspace.compare_button.isEnabled()
        workspace.compare_high.setValue(20000.0)
        assert workspace.compare_button.isEnabled()
        # Genuinely disjoint pair: the honest guidance is to pick another
        # pair, not to chase a band that cannot intersect.
        _select_combo_by_freq_floor(workspace.predicted_combo, controller, 4000.0)
        assert "帯域の交差なし" in workspace.comparison_state_label.text()
        assert "別のペア" in workspace.save_guidance_label.text()
        assert not workspace.compare_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_details_toggle_preserves_state(tmp_path: Path) -> None:
    """#970: opening/closing 詳細条件 never mutates selection or evidence."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        _commit(controller, "point-mlp", "predicted", "front_left", b"20 69\n40 70\n80 68\n")
        workspace.refresh()
        workspace.set_context("comparison")
        workspace.register_pair_button.click()
        workspace.compare_button.click()
        assert "保存済み" in workspace.comparison_state_label.text()

        snapshot = (
            workspace.measured_combo.currentIndex(),
            workspace.predicted_combo.currentIndex(),
            workspace.compare_low.value(),
            workspace.compare_high.value(),
            workspace.comparison_state_label.text(),
            workspace.comparison_history.rowCount(),
            workspace.pair_summary_label.text(),
        )
        toggles = [
            button
            for button in workspace.comparison_details_block.findChildren(QPushButton)
            if button.isCheckable()
        ]
        assert len(toggles) == 1
        toggles[0].click()
        app.processEvents()
        toggles[0].click()
        app.processEvents()
        assert snapshot == (
            workspace.measured_combo.currentIndex(),
            workspace.predicted_combo.currentIndex(),
            workspace.compare_low.value(),
            workspace.compare_high.value(),
            workspace.comparison_state_label.text(),
            workspace.comparison_history.rowCount(),
            workspace.pair_summary_label.text(),
        )
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_phase_lane_shows_reason_not_silence(tmp_path: Path) -> None:
    """#970: an unauthorized/absent phase lane states its grounds."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        _commit(controller, "point-mlp", "measured", "front_right", b"20 69\n40 70\n80 68\n")
        workspace.refresh()
        workspace.set_context("comparison")
        # REW text carries no phase — the lane stays visible and names the
        # missing data per side instead of disappearing.
        assert not workspace.phase_compare_plot.isHidden()
        state_text = _plot_state_text(workspace.phase_compare_plot)
        assert "位相は表示できません" in state_text
        assert "A: 位相データがありません" in state_text
        assert "B: 位相データがありません" in state_text
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_stale_registration_is_fail_closed(tmp_path: Path) -> None:
    """#970: a superseded head revision turns the pair gate stale."""
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        _commit(controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n")
        _commit(controller, "point-mlp", "predicted", "front_left", b"20 69\n40 70\n80 68\n")
        workspace.refresh()
        workspace.set_context("comparison")
        workspace.register_pair_button.click()
        assert workspace.compare_button.isEnabled()

        moved = make_f1_scene().model_copy(
            update={"room": RoomPrism(width_m=6.5, depth_m=4.0, height_m=2.4)}
        )
        controller.scene_repository.save(
            moved,
            parent_revision_id=controller.latest_revision().revision_id,
        )
        workspace.refresh()
        assert "stale" in workspace.comparison_state_label.text()
        assert "古い" in workspace.selection_reason_label.text()
        assert "再登録" in workspace.save_guidance_label.text()
        assert not workspace.compare_button.isEnabled()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_campaign_page_guides_source_target_matrix(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("campaign")
        workspace.campaign_create_button.click()
        assert workspace.campaign_plan_combo.count() == 1
        workspace.campaign_open_button.click()
        # make_f1_scene: speakers (FL, FR, C) × 1 target (point-mlp) = cells.
        rows = workspace.campaign_table.rowCount()
        assert rows >= 1
        step_text = workspace.campaign_step_label.text()
        assert "フロント左" in step_text and "リピート" in step_text
        statuses = {
            workspace.campaign_table.item(r, 3).text()
            for r in range(rows)
        }
        assert statuses == {"未着手"}
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_campaign_cell_commit_and_resume(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        record = _commit(
            controller, "point-mlp", "measured", "front_left", b"20 70\n40 71\n80 69\n"
        )
        workspace.set_context("campaign")
        workspace.campaign_create_button.click()
        workspace.campaign_open_button.click()
        workspace.campaign_table.selectRow(0)
        index = workspace.campaign_measurement_combo.findData(record.measurement_id)
        assert index >= 0
        workspace.campaign_measurement_combo.setCurrentIndex(index)
        workspace.campaign_commit_button.click()
        assert workspace.campaign_table.item(0, 3).text() == "品質確認待ち"
        assert "未着手" in {
            workspace.campaign_table.item(r, 3).text()
            for r in range(1, workspace.campaign_table.rowCount())
        }
        # Progress is counts, not a percentage.
        assert "完了 0 /" in workspace.campaign_progress_label.text()
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_comparison_band_presets(tmp_path: Path) -> None:
    app = _app()
    controller, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("comparison")
        buttons = {
            button.text(): button
            for button in workspace.findChildren(QPushButton)
        }
        workspace.compare_low.setValue(100.0)
        buttons["全帯域"].click()
        assert workspace.compare_low.value() == 20.0
        assert workspace.compare_high.value() == 20000.0
        buttons["低域"].click()
        assert workspace.compare_high.value() == 300.0
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_set_context_same_context_does_not_refresh(tmp_path: Path) -> None:
    """REV25-UIPERF: the shell re-issues the route context on every
    activation; a same-context set_context must not trigger a full refresh."""
    app = _app()
    _, workspace = _workspace(tmp_path)
    try:
        calls = []
        original_refresh = workspace.refresh

        def _counted() -> None:
            calls.append(1)
            original_refresh()

        workspace.refresh = _counted
        workspace.set_context("import")  # initial context — unchanged
        assert not calls
        workspace.set_context("quality")
        assert len(calls) == 1
        workspace.set_context("quality")  # repeated — still no extra refresh
        assert len(calls) == 1
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
