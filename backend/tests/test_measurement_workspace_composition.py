from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QSplitter

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
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
        assert workspace.pages.currentIndex() == 6
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
        # #1002 added another splitter (the correspondence compare pane) —
        # locate the split that actually hosts the quality table+detail.
        for split in splitters:
            children = {split.widget(i) for i in range(split.count())}
            if (
                workspace.quality_table.parentWidget() in children
                and workspace.quality_detail.parentWidget() in children
            ):
                break
        else:
            raise AssertionError(
                "no splitter hosts both quality table and detail"
            )
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
        assert "プレビュー" in workspace.comparison_state_label.text()
        # #564: measured-vs-predicted comparison is gated on a persisted
        # registration record — saving without it must be refused, then
        # succeed once the pair is registered.
        workspace.compare_button.click()
        assert "プレビュー" in workspace.comparison_state_label.text()
        assert "登録" in workspace.registration_state_label.text()
        workspace.register_pair_button.click()
        assert "登録済み" in workspace.registration_state_label.text()
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
