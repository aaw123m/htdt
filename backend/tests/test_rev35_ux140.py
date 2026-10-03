"""REV35-UX140 stage (a): workflow-path coverage of legacy UI surfaces.

``docs/reviews/rev35-ux140.md`` inventories every surface reachable
through the legacy ``--legacy-ui`` QMainWindow chain (and the standalone
``python -m htdt.native_editor`` / ``room_editor`` / ``wall_editor``
entry points) and maps each to the workflow-shell surface that owns the
same capability. These tests pin that parity map so stages (b) and (c)
can migrate and delete the legacy window + adapter without silently
dropping a feature.

Three mounts are genuinely unported — the stage-(b) list — and are
covered by strict ``xfail`` tests at the bottom so they flip loudly the
moment a workflow mount lands:

* ``SeatPriorityPanel`` (リスニング集団 dock; mounted only by
  ``theater_editor.py``)
* ``FieldExplorerPanel`` (音場エクスプローラー dock; mounted only by
  ``prediction_workspace.py``)
* the O531 transfer-matrix grid (伝達行列; rendered only by the legacy
  ``prediction_workspace.py`` dock via
  ``PredictionMatrixService.matrix_presentation``)
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from htdt.cad_scene import F1_DOCUMENT_ID  # noqa: E402
from htdt.field_explorer_panel import FieldExplorerPanel  # noqa: E402
from htdt.measurement_page_workspace import MeasurementPageWorkspace  # noqa: E402
from htdt.measurement_workflow import MeasurementWorkflowController  # noqa: E402
from htdt.optimization_workflow_workspace import (  # noqa: E402
    OptimizationWorkflowWorkspace,
)
from htdt.room_acoustics_panel import (  # noqa: E402
    RoomAcousticsTabs,
    RoomTreatmentPanel,
    SurfaceMaterialPanel,
)
from htdt.room_geometry_input import RoomGeometryInputController  # noqa: E402
from htdt.room_geometry_panel import RoomGeometryPanel  # noqa: E402
from htdt.room_prediction import (  # noqa: E402
    RoomPredictionController,
    RoomPredictionPanel,
)
from htdt.room_transform_input import RoomEntityTransformController  # noqa: E402
from htdt.room_workspace import ROOM_CONTEXT_IDS, RoomWorkspace  # noqa: E402
from htdt.seat_priority_panel import SeatPriorityPanel  # noqa: E402
from htdt.workflow_navigation import (  # noqa: E402
    CANONICAL_WORKSPACE_CONTEXTS,
    WorkspaceId,
)

from test_optimization_workflow_workspace import (  # noqa: E402
    FakeOptimizationViewport,
)
from test_room_cadux import FakeRoomViewport, _f1_repository  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _room_workspace(tmp_path) -> tuple[QApplication, RoomWorkspace]:
    """RoomWorkspace mounted the way ``_make_room`` mounts it."""
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    geometry_input = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry_input)
    workspace.attach_geometry_panel(RoomGeometryPanel(geometry_input))
    transform_input = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform_input)
    transform_input.commit_gate = workspace.controller.move_commit_gate
    prediction = RoomPredictionController(
        repository, workspace.controller, parent=workspace
    )
    workspace.attach_acoustics_panel(
        RoomAcousticsTabs(
            RoomPredictionPanel(prediction),
            SurfaceMaterialPanel(workspace.controller),
            RoomTreatmentPanel(workspace.controller),
        )
    )
    return app, workspace


def _close(app: QApplication, widget) -> None:
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_room_workspace_covers_native_editor_surfaces(tmp_path) -> None:
    """NativeEditorWindow's scene tree, inspector, toolbars and recovery
    strip all have workflow mounts on RoomWorkspace."""
    app, workspace = _room_workspace(tmp_path)
    try:
        canonical = tuple(
            context.context_id
            for context in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.ROOM]
        )
        assert ROOM_CONTEXT_IDS == canonical

        # シーン tree dock / inspector dock (native_editor.py)
        assert workspace.objects_panel is not None
        assert workspace.inspector is not None

        # 編集 toolbar: save/undo/redo + move/rotate arming + dirty state
        assert workspace.geometry_input is not None
        assert workspace.transform_input is not None
        for verb in ("save", "undo", "redo"):
            assert callable(getattr(workspace, verb)), verb
        assert callable(workspace.controller.dirty_state)

        # 表示 toolbar: hide/lock/delete via the objects panel, standard
        # views + fit-all on the workspace
        for signal_name in (
            "hideRequested",
            "lockRequested",
            "deleteRequested",
        ):
            assert hasattr(workspace.objects_panel, signal_name), signal_name
        assert callable(workspace.apply_standard_view)
        assert callable(workspace.fit_all)

        # スナップ toolbar: snap toggles live on the overlay controls
        assert workspace.overlay_controls.object_snap is not None

        # 復旧 toolbar: draft recover/discard banner
        assert hasattr(workspace.recovery_banner, "recoverRequested")
        assert hasattr(workspace.recovery_banner, "discardRequested")
    finally:
        _close(app, workspace)


def test_room_workspace_covers_wall_and_aiming_surfaces(tmp_path) -> None:
    """WallEditorWindow's 壁 toolbar and TheaterWorkflowWindow's
    座席へ向ける action both exist on the workflow room surface."""
    app, workspace = _room_workspace(tmp_path)
    try:
        panel = workspace.geometry_panel
        assert isinstance(panel, RoomGeometryPanel)
        # 壁 toolbar: 編集/終了/分割(中点挿入)/結合/削除/ドア開口追加
        for field in (
            "edit_button",
            "finish_button",
            "insert_midpoint_button",
            "ensure_walls_button",
            "merge_wall_button",
            "delete_wall_button",
            "add_opening_button",
            "apply_opening_button",
            "delete_opening_button",
        ):
            assert isinstance(getattr(panel, field, None), QPushButton), field
        # 座席へ向ける: aim targets/angles live on the selection inspector
        for verb in (
            "aim_targets",
            "aim_selected_speaker_at",
            "clear_selected_speaker_aim",
            "align_selected_cabinet_to_aim",
        ):
            assert callable(getattr(workspace.controller, verb)), verb
        assert hasattr(workspace.inspector, "aimTargetRequested")
    finally:
        _close(app, workspace)


def test_room_workspace_covers_room_and_acoustics_surfaces(tmp_path) -> None:
    """RoomEditorWindow's 部屋 dock + ConstraintEditorWindow +
    MeasurementEditorWindow-adjacent panels all have workflow mounts."""
    app, workspace = _room_workspace(tmp_path)
    try:
        # 部屋 dock: geometry panel + treatment/material acoustics tabs
        assert workspace.geometry_panel is not None
        assert isinstance(workspace.acoustics_panel, RoomAcousticsTabs)
        # 制約 dock / 履歴 / 映像・座席 / システム拡張 / 基準
        assert workspace.constraints_panel is not None
        assert workspace.history_panel is not None
        assert workspace.video_panel is not None
        assert workspace.system_expansion_panel is not None
        assert workspace.standards_panel is not None
        # 計測ツール (measure dock of the legacy window)
        assert workspace.measure_panel is not None
        # 予測 dock parity: receiver / mode / environment profile /
        # run / cancel controls all exist on RoomPredictionPanel (the
        # environment combo + 新規… dialog own the sound-speed source,
        # matching the legacy dock's sound-speed chain)
        prediction_panel = workspace.acoustics_panel.findChild(
            RoomPredictionPanel
        )
        assert prediction_panel is not None
        for field in (
            "receiver",
            "max_mode",
            "environment",
            "environment_new",
            "run_button",
            "cancel_button",
        ):
            assert getattr(prediction_panel, field, None) is not None, field
    finally:
        _close(app, workspace)


def test_optimization_workspace_covers_legacy_dock_controls(tmp_path) -> None:
    """Every control the legacy 最適化 dock mounted is bound on the
    workflow controller — sampled per legacy dock section."""
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeOptimizationViewport(parent),
    )
    try:
        controller = workspace.controller
        sections = {
            # spec save/load/reauthor + axis editing + linked variables
            "search/spec": (
                "search_axis_combo",
                "search_axis_tree",
                "search_save_button",
                "search_reauthor_button",
                "search_generate_button",
                "search_cancel_button",
                "search_preview_button",
                "search_apply_button",
                "search_next_button",
                "search_prev_button",
                "search_candidate_tree",
                "linked_add_button",
                "linked_remove_button",
            ),
            "extended search": (
                "extended_generate_button",
                "extended_axis_tree",
                "extended_spec_tree",
                "extended_candidate_tree",
            ),
            "objectives/pareto": (
                "objective_list",
                "pareto_refresh_button",
                "pareto_tree",
            ),
            "campaign": (
                "campaign_tree",
                "campaign_assignment_tree",
                "rew_combo",
                "rew_refresh_button",
            ),
            "validation/adaptive": (
                "validation_refresh_button",
                "validation_tree",
                "adaptive_build_button",
                "adaptive_cancel_button",
                "adaptive_tree",
            ),
            "measurement plan": (
                "measurement_plan_button",
                "measurement_plan_tree",
                "measurement_complete_button",
            ),
            "robustness": ("robustness_tree",),
        }
        for section, fields in sections.items():
            for field in fields:
                assert getattr(controller, field, None) is not None, (
                    f"{section}: {field}"
                )
    finally:
        _close(app, workspace)


def test_measurement_workspace_covers_legacy_rew_dock(tmp_path) -> None:
    """MeasurementEditorWindow's 実測 dock: REW import/list/load +
    A/B comparison + diff plot + target authoring all have mounts."""
    app = _app()
    repository = _f1_repository(tmp_path)
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    workspace = MeasurementPageWorkspace(controller)
    try:
        # REWテキスト取り込み / 一覧更新 / 選択REWを読み込み
        for field in (
            "text_import_button",
            "rew_refresh_button",
            "rew_combo",
            "rew_read_button",
        ):
            assert getattr(workspace, field, None) is not None, field
        # 実測グラフ / A-B比較 / 差分プロット / 位相 / 品質
        for field in (
            "import_preview_plot",
            "comparison_plot",
            "difference_plot",
            "phase_plot",
            "quality_plot",
        ):
            assert getattr(workspace, field, None) is not None, field
        # Every declared measurement context selects a real page
        for context in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.MEASUREMENT]:
            workspace.set_context(context.context_id)
    finally:
        _close(app, workspace)


# --- stage-(b) list: surfaces that are legacy-only today ---------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "UX140 stage (b) TODO: SeatPriorityPanel (リスニング集団) is mounted "
        "only by the legacy TheaterEditorWindow; port a SeatPriorityProfile "
        "authoring surface to the workflow room workspace or document a "
        "deliberate drop in docs/reviews/rev35-ux140.md"
    ),
)
def test_seat_priority_panel_has_a_workflow_mount(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        assert workspace.findChild(SeatPriorityPanel) is not None
    finally:
        _close(app, workspace)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "UX140 stage (b) TODO: FieldExplorerPanel (音場エクスプローラー) is "
        "mounted only by the legacy prediction dock "
        "(prediction_workspace._open_field_explorer); the workflow "
        "RoomPredictionPanel has no field-explorer entry"
    ),
)
def test_field_explorer_panel_has_a_workflow_mount(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        assert workspace.findChild(FieldExplorerPanel) is not None
    finally:
        _close(app, workspace)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "UX140 stage (b) TODO: the O531 transfer-matrix grid (伝達行列, "
        "#986) is rendered only by the legacy prediction dock's "
        "refresh_matrix_dock via PredictionMatrixService.matrix_presentation; "
        "no workflow surface renders the persisted spec grid"
    ),
)
def test_transfer_matrix_grid_has_a_workflow_mount(tmp_path) -> None:
    app, workspace = _room_workspace(tmp_path)
    try:
        labels = [
            button.text()
            for button in workspace.findChildren(QPushButton)
        ]
        assert any("行列" in label for label in labels)
    finally:
        _close(app, workspace)
