"""REV35-UX140: workflow-path coverage of legacy UI surfaces.

``docs/reviews/rev35-ux140.md`` inventories every surface reachable
through the legacy ``--legacy-ui`` QMainWindow chain (and the standalone
``python -m htdt.native_editor`` / ``room_editor`` / ``wall_editor``
entry points) and maps each to the workflow-shell surface that owns the
same capability. These tests pin that parity map so stage (c) can delete
the legacy window + adapter without silently dropping a feature.

Stage (b) mounted the last three legacy-only surfaces —
``SeatPriorityPanel`` (リスニング集団) on the room placement context,
and ``FieldExplorerPanel`` (音場エクスプローラー) + the O531
transfer-matrix grid (伝達行列) on ``RoomPredictionPanel`` — the
tests at the bottom pin their mounts and the unique behaviors the
legacy docks had.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hashlib import sha256  # noqa: E402

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from htdt.cad_equipment import FrequencyDomain  # noqa: E402
from htdt.cad_prediction_matrix import (  # noqa: E402
    MatrixObservableContract,
    MatrixReceiverRef,
    MatrixSourceRef,
    build_prediction_matrix_spec,
)
from htdt.cad_prediction_repository import CadPredictionRepository  # noqa: E402
from htdt.cad_predictions import analyze_native_rectangular_geometry  # noqa: E402
from htdt.cad_scene import (  # noqa: E402
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
)
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
from htdt.prediction_matrix_service import PredictionMatrixService  # noqa: E402
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef  # noqa: E402
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


def _room_workspace(
    tmp_path,
    repository=None,
) -> tuple[QApplication, RoomWorkspace]:
    """RoomWorkspace mounted the way ``_make_room`` mounts it."""
    app = _app()
    if repository is None:
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


# --- stage-(b) mounts: formerly legacy-only surfaces --------------------


def test_seat_priority_panel_has_a_workflow_mount(tmp_path) -> None:
    """リスニング集団: the room placement context owns the seat-priority
    profile editor the legacy TheaterEditorWindow dock hosted."""
    app, workspace = _room_workspace(tmp_path)
    try:
        panel = workspace.seat_priority_panel
        assert isinstance(panel, SeatPriorityPanel)
        assert workspace.placement_panel.findChild(SeatPriorityPanel) is panel
        # The F1 fixture carries no seats — the honest empty state, not
        # fabricated member rows.
        assert panel.status_label.text().startswith("座席がありません")
    finally:
        _close(app, workspace)


def test_seat_priority_panel_lists_committed_seats(tmp_path) -> None:
    """The mount refreshes member rows from the committed head — the same
    contract the legacy dock followed on every rebuild."""
    app = _app()
    repository = _f1_repository(tmp_path)
    head = repository.current_head(F1_DOCUMENT_ID)
    seated = head.document.model_copy(
        update={
            "entities": head.document.entities
            + (
                SceneEntity(
                    entity_id="seat-mlp",
                    kind="seat",
                    name="MLP seat",
                    position=Position3(x_m=3.0, y_m=3.0, z_m=0.55),
                    size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.1),
                ),
            )
        }
    )
    repository.save(seated, parent_revision_id=head.revision_id)
    _, workspace = _room_workspace(tmp_path, repository)
    try:
        panel = workspace.seat_priority_panel
        assert "1 座席" in panel.status_label.text()
        assert panel.member_tree.topLevelItemCount() == 1
        # Entering/leaving contexts resyncs from the committed head.
        workspace.set_context("objects")
        workspace.set_context("placement")
        assert panel.member_tree.topLevelItemCount() == 1
    finally:
        _close(app, workspace)


def test_field_explorer_panel_has_a_workflow_mount(tmp_path) -> None:
    """音場エクスプローラー: RoomPredictionPanel owns the entry the legacy
    prediction dock's 音場ヒートマップ button opened."""
    app, workspace = _room_workspace(tmp_path)
    try:
        panel = workspace.acoustics_panel.findChild(RoomPredictionPanel)
        assert panel is not None
        assert isinstance(panel.field_explorer_panel, FieldExplorerPanel)
        # No saved run selected yet — gated off exactly like the legacy
        # dock's scalar-field button.
        assert not panel.field_explorer_button.isEnabled()
    finally:
        _close(app, workspace)


def test_field_explorer_opens_for_exact_modes_run(tmp_path) -> None:
    """Gating + open wiring end-to-end: a saved exact rectangular-modes
    run enables the entry and prepares the build form."""
    app = _app()
    repository = _f1_repository(tmp_path)
    head = repository.current_head(F1_DOCUMENT_ID)
    modes, reflections = analyze_native_rectangular_geometry(
        head, "point-mlp", max_mode_hz=150.0
    )
    assert modes.geometry_compatibility == "exact_for_model_geometry"
    CadPredictionRepository(repository).save_run((modes, reflections))
    _, workspace = _room_workspace(tmp_path, repository)
    try:
        panel = workspace.acoustics_panel.findChild(RoomPredictionPanel)
        # refresh() auto-selects the newest run — the exact modes run —
        # so the button lights up like the legacy dock's did.
        assert panel.field_explorer_button.isEnabled()
        panel._open_field_explorer()
        assert panel.field_explorer_dialog.isVisible()
        assert panel.field_explorer_panel.mode_combo.count() > 0
        assert (
            "モード候補"
            in panel.field_explorer_panel.field_status_label.text()
        )
    finally:
        _close(app, workspace)


def test_transfer_matrix_grid_has_a_workflow_mount(tmp_path) -> None:
    """伝達行列: RoomPredictionPanel renders the persisted spec×cell grid
    the legacy prediction dock's refresh_matrix_dock produced."""
    app, workspace = _room_workspace(tmp_path)
    try:
        panel = workspace.acoustics_panel.findChild(RoomPredictionPanel)
        assert panel is not None
        labels = [
            button.text()
            for button in workspace.findChildren(QPushButton)
        ]
        assert any("行列" in label for label in labels)
        # No persisted matrix spec yet — honest empty state.
        assert panel.matrix_table.rowCount() == 0
        assert panel.matrix_table.columnCount() == 0
        assert "行列なし" in panel.matrix_status_label.text()
        panel.matrix_reload_button.click()
        assert "行列なし" in panel.matrix_status_label.text()
    finally:
        _close(app, workspace)


def test_transfer_matrix_grid_renders_persisted_spec(tmp_path) -> None:
    """A saved matrix spec renders its source columns × receiver rows
    through the workflow mount."""
    app = _app()
    repository = _f1_repository(tmp_path)
    head = repository.current_head(F1_DOCUMENT_ID)
    spec = build_prediction_matrix_spec(
        document_id=F1_DOCUMENT_ID,
        scene_revision_id=head.revision_id,
        scene_content_hash=head.content_hash,
        acoustic_scene_snapshot_id="snapshot-test",
        acoustic_scene_snapshot_sha256=sha256(b"snapshot").hexdigest(),
        solver_implementation_ref=ExactExternalAuthorityRef(
            authority_id="solver:test",
            authority_version="1",
            semantic_hash_sha256=sha256(b"solver").hexdigest(),
        ),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=200.0
        ),
        sources=(
            MatrixSourceRef(
                matrix_source_id="source-fl",
                source_entity_id="speaker-fl",
                source_binding_sha256=sha256(b"fl").hexdigest(),
            ),
            MatrixSourceRef(
                matrix_source_id="source-c",
                source_entity_id="speaker-c",
                source_binding_sha256=sha256(b"c").hexdigest(),
            ),
        ),
        receivers=(
            MatrixReceiverRef(
                matrix_receiver_id="seat-mlp",
                receiver_id="seat-mlp",
                receiver_entity_id="point-mlp",
                receiver_binding_sha256=sha256(b"mlp").hexdigest(),
            ),
        ),
        observable_contract=MatrixObservableContract(
            frequency_axis_hz=(20.0, 100.0, 200.0)
        ),
    )
    PredictionMatrixService(
        repository, F1_DOCUMENT_ID
    ).repository.save_spec(spec)
    _, workspace = _room_workspace(tmp_path, repository)
    try:
        panel = workspace.acoustics_panel.findChild(RoomPredictionPanel)
        assert panel.matrix_table.columnCount() == 2
        assert panel.matrix_table.rowCount() == 1
        cell = panel.matrix_table.item(0, 0)
        assert cell is not None and cell.text()
        # No result set yet — the status row names the spec only.
        assert panel.matrix_status_label.text().startswith("matrix ")
    finally:
        _close(app, workspace)
