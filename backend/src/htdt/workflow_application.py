from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from .cad_input import (
    CAD_SCENE_COMMAND_IDS,
    CadCommandBindings,
    CadInputController,
    bind_cad_input_commands,
    unbind_cad_input_commands,
)
from .cad_view_state import StandardView
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_prediction_repository import CadPredictionRepository
from .cad_repository import SceneRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_search_repository import CadSearchRepository
from .command_palette import CommandPaletteController
from .command_registry import (
    CommandAvailability,
    CommandContext,
    CommandRegistry,
    register_default_commands,
)
from .data_management import (
    ApplicationDataLifecycle,
    DataManagementBackend,
    DataManagementController,
)
from .data_management_ui import build_data_management_component
from .equipment_catalog_export import export_equipment_catalog_snapshot
from .measurement_page_workspace import build_measurement_workspace_mount
from .measurement_workflow import MeasurementWorkflowController
from .optimization_workflow_workspace import build_optimization_workspace_mount
from .overview_readiness import OverviewReadinessService
from .overview_workspace import OverviewWorkspace
from .room_geometry_input import RoomGeometryInputController
from .room_geometry_panel import RoomGeometryPanel
from .room_prediction import RoomPredictionController, RoomPredictionPanel
from .room_transform_input import RoomEntityTransformController
from .room_viewport import RoomViewport3D
from .room_workspace import RoomWorkspace
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_settings import DataManagementDialog
from .workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


_ROOM_TOOL_COMMAND_IDS = (
    "room.view.perspective",
    "room.view.top",
    "room.view.front",
    "room.view.rear",
    "room.view.left",
    "room.view.right",
    "room.view.isolate_selection",
    "room.view.isolate_kind",
    "room.view.isolate_clear",
    "room.view.section_toggle",
    "room.view.save_named",
    "room.underlay.import",
    "room.underlay.calibrate",
    "room.layout.copy",
    "room.layout.paste",
    "room.layout.mirror_x",
    "room.layout.mirror_y",
    "room.layout.pair_speaker",
    "room.layout.align_min_x",
    "room.layout.align_max_x",
    "room.layout.align_min_y",
    "room.layout.align_max_y",
    "room.layout.align_center_x",
    "room.layout.align_center_y",
    "room.layout.distribute_x",
    "room.layout.distribute_y",
    "room.layout.seat_row",
    "room.seating.layout",
    "room.constraint.centerline_x",
    "room.constraint.centerline_y",
    "room.constraint.symmetric",
    "room.constraint.equal_spacing",
    "room.constraint.fixed_distance",
    "room.constraint.remove",
    "room.constraint.guides_toggle",
)

_WORKSPACE_COMMAND_IDS = (
    "project.save",
    "edit.undo",
    "edit.redo",
    "room.draw",
    "room.add_speaker",
    "room.edit.delete",
    "room.edit.toggle_hide",
    "room.edit.toggle_lock",
    "room.measure",
    "room.view.history",
    "measurements.import_rew",
    "prediction.run",
    "optimization.compare_candidates",
    *_ROOM_TOOL_COMMAND_IDS,
    *CAD_SCENE_COMMAND_IDS,
)


def _available(enabled: bool, reason: str) -> CommandAvailability:
    return (
        CommandAvailability.available()
        if enabled
        else CommandAvailability.unavailable(reason)
    )


def _is_kind(workspace: RoomWorkspace, entity_id: str, kind: str) -> bool:
    try:
        return workspace.controller.document.entity(entity_id).kind == kind
    except KeyError:
        return False


class WorkflowApplicationComposition:
    """Application-root composition for UX120-UX140 and Settings.

    Repositories/services remain authoritative; this object only owns lifecycle,
    lazy workspace construction, command binding and restore-time handle rebuild.
    """

    def __init__(self, repository: SceneRepository, document_id: str) -> None:
        self.repository = repository
        self.repository_path = Path(repository.path)
        self.data_dir = self.repository_path.parent
        self.document_id = document_id

        self.registry = CommandRegistry()
        register_default_commands(self.registry)

        registrations = build_canonical_workspace_registrations(
            {
                WorkspaceId.OVERVIEW: self._make_overview,
                WorkspaceId.ROOM: self._make_room,
                WorkspaceId.MEASUREMENT: self._make_measurement,
                WorkspaceId.OPTIMIZATION: self._make_optimization,
            }
        )
        self.shell = WorkflowShellWindow(registrations)
        self.registry.set_deep_link_handler(self.shell.handle_deep_link)

        self.command_palette = CommandPaletteController(
            self.shell,
            self.registry,
            context_provider=lambda: CommandContext(self.shell.current_workspace_id.value),
        )
        self.shell.command_registry = self.registry  # type: ignore[attr-defined]
        self.shell.command_palette_controller = self.command_palette  # type: ignore[attr-defined]

        backend = DataManagementBackend(self.data_dir)
        lifecycle = ApplicationDataLifecycle(
            freeze_mutations=self._freeze_data_mutations,
            release_data_handles=self._release_data_handles,
            reopen_data_handles=self._reopen_data_handles,
            thaw_mutations=self._thaw_data_mutations,
        )
        self.data_management_controller = DataManagementController(
            backend,
            lifecycle,
            parent=self.shell,
        )
        self.data_management_component = build_data_management_component(
            self.data_management_controller
        )
        self.settings_dialog = DataManagementDialog(
            self.data_management_component,
            self.shell,
        )
        self.shell.settingsRequested.connect(self.settings_dialog.open_settings)
        self.shell.register_close_guard(self._can_close_application)
        self.shell.workflow_application = self  # type: ignore[attr-defined]
        self.registry.bind(
            "equipment.export_capture_catalog",
            execute=self._export_capture_equipment_catalog,
        )

    def _build_overview_service(self) -> OverviewReadinessService:
        measurement_repository = CadMeasurementRepository(self.repository)
        prediction_repository = CadPredictionRepository(self.repository)
        search_repository = CadSearchRepository(self.repository)
        roomsim_repository = CadRoomSimRepository(self.repository, search_repository)
        objective_repository = CadObjectiveRepository(
            self.repository,
            search_repository,
            measurement_repository=measurement_repository,
            roomsim_repository=roomsim_repository,
        )
        validation_repository = CadModelValidationRepository(
            search_repository,
            roomsim_repository,
            measurement_repository,
            objective_repository,
        )
        return OverviewReadinessService(
            self.repository,
            measurement_repository,
            prediction_repository,
            search_repository,
            validation_repository,
            quality_source=CadMeasurementQualityRepository(measurement_repository),
        )

    def _navigate_target(self, target: WorkspaceDeepLink) -> bool:
        return self.shell.handle_deep_link(target)

    def _unbind_workspace_commands(self) -> None:
        for command_id in _WORKSPACE_COMMAND_IDS:
            try:
                self.registry.unbind(command_id)
            except KeyError:
                pass

    def _bind_room_tool_commands(self, workspace: RoomWorkspace) -> None:
        """Bind the room CAD tool commands (views, underlay, layout, seating,
        constraints) to the workspace methods that implement them."""

        def _has_selection() -> bool:
            return bool(workspace.controller.view_state.selection) or (
                workspace.controller.selected_id is not None
            )

        def _min_selection(count: int) -> bool:
            if not workspace.controller.can_edit:
                return False
            selection = workspace.controller.view_state.selection
            if not selection and workspace.controller.selected_id is not None:
                selection = (workspace.controller.selected_id,)
            return len(selection) >= count

        always = lambda: CommandAvailability.available()  # noqa: E731
        editable = lambda: _available(  # noqa: E731
            workspace.controller.can_edit,
            "編集できる状態ではありません",
        )
        has_selection = lambda: _available(  # noqa: E731
            _has_selection(), "項目を選択してください"
        )
        sel_at_least = lambda count: (  # noqa: E731
            lambda: _available(
                _min_selection(count), f"{count}つ以上の項目を選択してください"
            )
        )
        room_ready = lambda: _available(  # noqa: E731
            workspace.controller.document.room is not None,
            "先に部屋を作成してください",
        )
        clipboard_ready = lambda: _available(  # noqa: E731
            workspace.controller.can_edit
            and getattr(workspace, "_clipboard", None) is not None
            and bool(workspace._clipboard.entities),
            "先にコピーしてください",
        )
        isolation_active = lambda: _available(  # noqa: E731
            workspace._pre_isolation_hidden is not None,
            "分離中ではありません",
        )
        underlays_exist = lambda: _available(  # noqa: E731
            bool(workspace.controller.underlays()),
            "下図をインポートしてください",
        )
        selected_speaker = lambda: _available(  # noqa: E731
            _has_selection()
            and workspace.controller.can_edit
            and workspace.controller.selected_id is not None
            and _is_kind(workspace, workspace.controller.selected_id, "speaker"),
            "スピーカーを選択してください",
        )

        bindings: dict[str, tuple] = {
            "room.view.perspective": (
                lambda: workspace.apply_standard_view(StandardView.PERSPECTIVE),
                always,
            ),
            "room.view.top": (
                lambda: workspace.apply_standard_view(StandardView.TOP),
                always,
            ),
            "room.view.front": (
                lambda: workspace.apply_standard_view(StandardView.FRONT),
                always,
            ),
            "room.view.rear": (
                lambda: workspace.apply_standard_view(StandardView.REAR),
                always,
            ),
            "room.view.left": (
                lambda: workspace.apply_standard_view(StandardView.LEFT),
                always,
            ),
            "room.view.right": (
                lambda: workspace.apply_standard_view(StandardView.RIGHT),
                always,
            ),
            "room.view.isolate_selection": (
                workspace.isolate_selection,
                has_selection,
            ),
            "room.view.isolate_kind": (workspace.isolate_kind, has_selection),
            "room.view.isolate_clear": (
                workspace.clear_isolation,
                isolation_active,
            ),
            "room.view.section_toggle": (workspace.toggle_section, always),
            "room.view.save_named": (workspace.save_named_view, always),
            "room.underlay.import": (
                workspace.import_underlay_dialog,
                room_ready,
            ),
            "room.underlay.calibrate": (
                workspace.arm_first_underlay_calibration,
                underlays_exist,
            ),
            "room.layout.copy": (workspace.layout_copy, has_selection),
            "room.layout.paste": (workspace.layout_paste, clipboard_ready),
            "room.layout.mirror_x": (
                lambda: workspace.layout_mirror("x"),
                sel_at_least(1),
            ),
            "room.layout.mirror_y": (
                lambda: workspace.layout_mirror("y"),
                sel_at_least(1),
            ),
            "room.layout.pair_speaker": (
                workspace.layout_pair_speaker,
                selected_speaker,
            ),
            "room.layout.align_min_x": (
                lambda: workspace.layout_align("x", "min"),
                sel_at_least(2),
            ),
            "room.layout.align_max_x": (
                lambda: workspace.layout_align("x", "max"),
                sel_at_least(2),
            ),
            "room.layout.align_min_y": (
                lambda: workspace.layout_align("y", "min"),
                sel_at_least(2),
            ),
            "room.layout.align_max_y": (
                lambda: workspace.layout_align("y", "max"),
                sel_at_least(2),
            ),
            "room.layout.align_center_x": (
                lambda: workspace.layout_align("x", "center"),
                sel_at_least(2),
            ),
            "room.layout.align_center_y": (
                lambda: workspace.layout_align("y", "center"),
                sel_at_least(2),
            ),
            "room.layout.distribute_x": (
                lambda: workspace.layout_distribute("x"),
                sel_at_least(3),
            ),
            "room.layout.distribute_y": (
                lambda: workspace.layout_distribute("y"),
                sel_at_least(3),
            ),
            "room.layout.seat_row": (workspace.open_seating_layout, editable),
            "room.seating.layout": (workspace.open_seating_layout, editable),
            "room.constraint.centerline_x": (
                lambda: workspace.add_centerline_constraint("x"),
                has_selection,
            ),
            "room.constraint.centerline_y": (
                lambda: workspace.add_centerline_constraint("y"),
                has_selection,
            ),
            "room.constraint.symmetric": (
                workspace.add_symmetric_pair_constraint,
                sel_at_least(2),
            ),
            "room.constraint.equal_spacing": (
                lambda: workspace.add_equal_spacing_constraint(None),
                sel_at_least(3),
            ),
            "room.constraint.fixed_distance": (
                workspace.add_fixed_distance_auto,
                sel_at_least(2),
            ),
            "room.constraint.remove": (
                workspace.remove_constraints_touching_selection,
                has_selection,
            ),
            "room.constraint.guides_toggle": (workspace.toggle_guides, always),
        }
        for command_id, (execute, availability) in bindings.items():
            self.registry.bind(
                command_id,
                execute=execute,
                availability=availability,
            )

    def _make_overview(self) -> WorkspaceMount:
        page = OverviewWorkspace(
            self._build_overview_service(),
            self.document_id,
            navigate=self._navigate_target,
        )

        def activate() -> None:
            self._unbind_workspace_commands()
            page.refresh()

        return WorkspaceMount.from_widget(page, on_activate=activate)

    def _make_room(self) -> WorkspaceMount:
        workspace = RoomWorkspace(self.repository, self.document_id)
        if not isinstance(workspace.viewport, RoomViewport3D):
            raise TypeError("UX120 Room workspace requires RoomViewport3D")

        geometry_input = RoomGeometryInputController(workspace, workspace.viewport)
        workspace.attach_geometry_input(geometry_input)
        geometry_panel = RoomGeometryPanel(geometry_input)
        workspace.attach_geometry_panel(geometry_panel)
        transform_input = RoomEntityTransformController(workspace, workspace.viewport)
        workspace.attach_transform_input(transform_input)
        # Hard placement constraints (#486): reject drag commits that would
        # introduce a violation, mirroring the legacy dock's blocking gate.
        transform_input.commit_gate = workspace.controller.move_commit_gate
        workspace.optimizeRequested.connect(
            lambda: self.shell.navigate(WorkspaceId.OPTIMIZATION)
        )
        prediction = RoomPredictionController(
            self.repository,
            workspace.controller,
            parent=workspace,
        )
        prediction_panel = RoomPredictionPanel(prediction)
        workspace.attach_acoustics_panel(prediction_panel)

        def show_prediction_overlay(results: object) -> None:
            if (
                isinstance(results, tuple)
                and results
                and prediction.result_is_current(results[0])
            ):
                workspace.set_prediction_results(results)
            else:
                workspace.set_prediction_results(())

        prediction.runSelected.connect(show_prediction_overlay)
        prediction_panel.findingSelected.connect(
            lambda finding: workspace.set_prediction_focus(
                getattr(finding, "spatial", None)
            )
        )

        cad_input = CadInputController(
            shortcut_parent=workspace,
            viewport=workspace.viewport.interactor,
            registry=self.registry,
            viewport_port=workspace.viewport,
        )
        workspace.cad_input_controller = cad_input  # type: ignore[attr-defined]

        bindings = CadCommandBindings(
            move=transform_input.arm_move,
            rotate=transform_input.arm_rotate,
            fit_selection=workspace.fit_selection,
            fit_all=workspace.fit_all,
            cancel=workspace.cancel_active_operation,
            commit=workspace.commit_active_operation,
            duplicate=workspace.duplicate_selected,
            constrain_axis=workspace.constrain_axis,
            availability={
                "room.transform.move": lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not geometry_input.is_active,
                    "編集できる項目を選択してください",
                ),
                "room.transform.rotate": lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not geometry_input.is_active,
                    "編集できる項目を選択してください",
                ),
                "room.view.fit_selection": lambda: _available(
                    workspace.controller.selected_id is not None,
                    "表示する項目を選択してください",
                ),
                "room.view.fit_all": lambda: CommandAvailability.available(),
                "room.edit.cancel": lambda: _available(
                    transform_input.is_active
                    or geometry_input.is_active
                    or workspace.controller.working.has_preview,
                    "キャンセルする操作はありません",
                ),
                "room.edit.commit": lambda: _available(
                    transform_input.is_active
                    or geometry_input.is_active
                    or workspace.controller.working.has_preview,
                    "確定する操作はありません",
                ),
                "room.edit.duplicate": lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not geometry_input.is_active
                    and not transform_input.is_active,
                    "複製できる項目を選択してください",
                ),
                "room.transform.axis_x": lambda: _available(
                    transform_input.is_active,
                    "移動または回転を開始してから軸を指定してください",
                ),
                "room.transform.axis_y": lambda: _available(
                    transform_input.is_active,
                    "移動または回転を開始してから軸を指定してください",
                ),
                "room.transform.axis_z": lambda: _available(
                    transform_input.is_active,
                    "移動または回転を開始してから軸を指定してください",
                ),
            },
        )

        def bind_room_commands() -> None:
            self.registry.bind(
                "project.save",
                execute=workspace.save,
                availability=lambda: _available(
                    (
                        workspace.controller.is_dirty
                        or workspace.has_focused_text_editor()
                    )
                    and workspace.controller.recovery_candidate is None
                    and not workspace.controller.working.has_preview,
                    "保存する変更がありません",
                ),
            )
            self.registry.bind(
                "edit.undo",
                execute=workspace.undo,
                availability=lambda: _available(
                    workspace.controller.working.can_undo
                    and not workspace.controller.working.has_preview,
                    "元に戻せる操作はありません",
                ),
            )
            self.registry.bind(
                "edit.redo",
                execute=workspace.redo,
                availability=lambda: _available(
                    workspace.controller.working.can_redo
                    and not workspace.controller.working.has_preview,
                    "やり直せる操作はありません",
                ),
            )
            self.registry.bind(
                "room.draw",
                execute=geometry_input.start_sketch,
                availability=lambda: _available(
                    workspace.controller.recovery_candidate is None
                    and not workspace.controller.working.has_preview
                    and not transform_input.is_active,
                    "復旧または編集中の操作を完了してから部屋を描いてください",
                ),
            )
            self.registry.bind(
                "room.add_speaker",
                execute=lambda: workspace.add_object("speaker"),
                availability=lambda: _available(
                    workspace.controller.can_edit
                    and not geometry_input.is_active
                    and not transform_input.is_active,
                    "部屋を作成し、編集中の操作を完了してからスピーカーを追加してください",
                ),
            )
            self.registry.bind(
                "prediction.run",
                execute=prediction_panel.run_prediction,
                availability=lambda: self._room_prediction_availability(
                    workspace,
                    prediction,
                    prediction_panel,
                ),
            )
            self._bind_room_tool_commands(workspace)
            self.registry.bind(
                "room.edit.delete",
                execute=workspace.delete_selection,
                availability=lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not transform_input.is_active,
                    "削除できる項目を選択してください",
                ),
            )
            self.registry.bind(
                "room.edit.toggle_hide",
                execute=lambda: bool(
                    workspace.set_selected_hidden(
                        not all(
                            workspace.controller.view_state.is_hidden(eid)
                            for eid in workspace.controller.view_state.selection
                        )
                    )
                ),
                availability=lambda: _available(
                    bool(workspace.controller.view_state.selection),
                    "項目を選択してください",
                ),
            )
            self.registry.bind(
                "room.edit.toggle_lock",
                execute=lambda: bool(
                    workspace.set_selected_locked(
                        not all(
                            workspace.controller.view_state.is_locked(eid)
                            for eid in workspace.controller.view_state.selection
                        )
                    )
                ),
                availability=lambda: _available(
                    bool(workspace.controller.view_state.selection),
                    "項目を選択してください",
                ),
            )
            self.registry.bind(
                "room.measure",
                execute=workspace.toggle_measure,
                availability=lambda: _available(
                    workspace.controller.document.room is not None,
                    "部屋を作成してください",
                ),
            )
            bind_cad_input_commands(self.registry, bindings)
            cad_input.refresh_shortcuts()

        def activate() -> None:
            self._unbind_workspace_commands()
            workspace.activate()
            prediction_panel.refresh()
            show_prediction_overlay(prediction.refresh_selection())
            bind_room_commands()

        def deactivate() -> None:
            unbind_cad_input_commands(self.registry)
            for command_id in (
                "project.save",
                "edit.undo",
                "edit.redo",
                "room.draw",
                "room.add_speaker",
                "room.edit.delete",
                "room.edit.toggle_hide",
                "room.edit.toggle_lock",
                "room.measure",
                "prediction.run",
                *_ROOM_TOOL_COMMAND_IDS,
            ):
                try:
                    self.registry.unbind(command_id)
                except KeyError:
                    pass

        def close() -> None:
            deactivate()
            cad_input.dispose()
            prediction.dispose()
            # RoomWorkspace owns the geometry/transform controller lifetime.
            workspace.close()

        workspace.viewport.contextMenuRequested.connect(
            lambda _local, global_pos: self._open_room_context_menu(
                workspace,
                QPointF(global_pos),
            )
        )

        def before_deactivate() -> tuple[bool, str | None]:
            allowed, reason = prediction.before_deactivate()
            if not allowed:
                return allowed, reason
            return workspace.before_deactivate()

        return WorkspaceMount(
            widget=workspace,
            on_activate=activate,
            on_deactivate=deactivate,
            before_deactivate=before_deactivate,
            on_context_changed=workspace.set_context,
            on_entity_requested=workspace.select_entity,
            on_close=close,
        )

    def _open_room_context_menu(
        self,
        workspace: RoomWorkspace,
        global_position: QPointF,
    ) -> None:
        command_ids = (
            "room.transform.move",
            "room.transform.rotate",
            "room.edit.duplicate",
            "room.edit.delete",
            "room.edit.toggle_hide",
            "room.edit.toggle_lock",
            "room.measure",
            "room.view.fit_selection",
            "room.view.fit_all",
        )
        menu = QMenu(workspace)

        # Descriptive edit history (#662): Undo/Redo items name the exact change
        # they apply; the bounded tail is a read-only "recent edits" listing.
        controller = workspace.controller
        undo_label = controller.undo_label
        redo_label = controller.redo_label
        for command_id, label in (
            ("edit.undo", f"元に戻す: {undo_label}" if undo_label else "元に戻す"),
            ("edit.redo", f"やり直す: {redo_label}" if redo_label else "やり直す"),
        ):
            definition = self.registry.definition(command_id)
            availability = self.registry.availability(command_id)
            if definition.shortcut:
                label = f"{label}    {definition.shortcut}"
            action = menu.addAction(label)
            action.setEnabled(availability.enabled)
            if availability.disabled_reason:
                action.setToolTip(availability.disabled_reason)
            action.triggered.connect(
                lambda checked=False, target=command_id: self.registry.execute(target)
            )
        entries = controller.history_entries(limit=5)
        if entries:
            history_header = menu.addAction("最近の編集")
            history_header.setEnabled(False)
            for entry in reversed(entries):
                marker = "●" if entry.applied else "○"
                item = menu.addAction(f"{marker} {entry.label}")
                item.setEnabled(False)
        menu.addSeparator()

        for command_id in command_ids:
            definition = self.registry.definition(command_id)
            availability = self.registry.availability(command_id)
            label = definition.display_name
            if definition.shortcut:
                label = f"{label}    {definition.shortcut}"
            action = menu.addAction(label)
            action.setEnabled(availability.enabled)
            if availability.disabled_reason:
                action.setToolTip(availability.disabled_reason)
            action.triggered.connect(
                lambda checked=False, target=command_id: self.registry.execute(target)
            )
        menu.exec(global_position.toPoint())

    @staticmethod
    def _room_prediction_availability(
        workspace: RoomWorkspace,
        prediction: RoomPredictionController,
        panel: RoomPredictionPanel,
    ) -> CommandAvailability:
        if prediction.is_busy:
            return CommandAvailability.unavailable("予測を実行中です")
        if workspace.controller.working.has_preview:
            return CommandAvailability.unavailable(
                "編集中の操作を確定またはキャンセルしてください"
            )
        if workspace.controller.is_dirty:
            return CommandAvailability.unavailable(
                "予測の前に現在の配置を保存してください"
            )
        if panel.receiver.currentData() is None:
            return CommandAvailability.unavailable("受音点を選択してください")
        return CommandAvailability.available()

    def _make_measurement(self) -> WorkspaceMount:
        controller = MeasurementWorkflowController(self.repository, self.document_id)
        mount = build_measurement_workspace_mount(controller)
        workspace = mount.widget
        original_activate = mount.on_activate

        def activate() -> None:
            self._unbind_workspace_commands()
            if original_activate is not None:
                original_activate()
            self.registry.bind(
                "measurements.import_rew",
                execute=workspace.import_rew_text_dialog,  # type: ignore[attr-defined]
                availability=lambda: self._measurement_import_availability(controller),
            )

        def deactivate() -> None:
            self.registry.unbind("measurements.import_rew")

        mount.on_activate = activate
        mount.on_deactivate = deactivate
        return mount

    @staticmethod
    def _measurement_import_availability(
        controller: MeasurementWorkflowController,
    ) -> CommandAvailability:
        try:
            controller.latest_revision()
        except Exception:
            return CommandAvailability.unavailable(
                "部屋を保存してからREWを読み込んでください"
            )
        return CommandAvailability.available()

    def _make_optimization(self) -> WorkspaceMount:
        mount = build_optimization_workspace_mount(self.repository, self.document_id)
        workspace = mount.widget
        controller = workspace.controller  # type: ignore[attr-defined]
        original_activate = mount.on_activate
        original_deactivate = mount.on_deactivate

        def edit_idle() -> bool:
            return (
                controller.active_search_worker_count() == 0
                and controller.active_extended_worker_count() == 0
                and not controller._rew_tasks
                and controller.scene.recovery_candidate is None
                and not controller.working.has_preview
            )

        def activate() -> None:
            self._unbind_workspace_commands()
            if original_activate is not None:
                original_activate()
            self.registry.bind(
                "project.save",
                execute=controller.save,
                availability=lambda: _available(
                    edit_idle() and controller.working.is_dirty,
                    "保存する変更がないか、候補生成・REW読込・編集操作が実行中です",
                ),
            )
            self.registry.bind(
                "edit.undo",
                execute=controller.undo,
                availability=lambda: _available(
                    edit_idle() and controller.working.can_undo,
                    "元に戻せる操作がないか、処理が実行中です",
                ),
            )
            self.registry.bind(
                "edit.redo",
                execute=controller.redo,
                availability=lambda: _available(
                    edit_idle() and controller.working.can_redo,
                    "やり直せる操作がないか、処理が実行中です",
                ),
            )
            self.registry.bind(
                "optimization.compare_candidates",
                execute=controller.refresh_pareto_comparison,
                availability=lambda: _available(
                    controller.search_selected_spec_id is not None,
                    "比較する探索仕様を選択してください",
                ),
            )

        def deactivate() -> None:
            if original_deactivate is not None:
                original_deactivate()
            for command_id in (
                "project.save",
                "edit.undo",
                "edit.redo",
                "optimization.compare_candidates",
            ):
                self.registry.unbind(command_id)

        mount.on_activate = activate
        mount.on_deactivate = deactivate
        return mount

    def _release_data_handles(self) -> None:
        self.shell.dispose_data_workspaces()
        self._unbind_workspace_commands()

    def _reopen_data_handles(self) -> None:
        self.repository = SceneRepository(self.repository_path)

    def _freeze_data_mutations(self) -> None:
        # Gate the command authority first so QShortcut activations and command
        # palette entries fail closed while shell widgets are being disabled.
        self.registry.freeze_data_mutations()
        self.shell.freeze_data_mutations()

    def _thaw_data_mutations(self) -> None:
        self.shell.thaw_data_mutations()
        self.registry.thaw_data_mutations()
        if self.shell.router.current_workspace_id is None:
            if not self.shell.navigate(WorkspaceId.OVERVIEW):
                raise RuntimeError("復元後の概要画面を再構築できませんでした")

    def _export_capture_equipment_catalog(self) -> None:
        """Operator action behind ``equipment.export_capture_catalog``.

        Global read-only export: writes the deterministic HTDT -> HTDT-Capture
        equipment picker snapshot through a normal save-dialog path and
        reports the definition count plus the exact snapshot identity.
        """

        selected, _filter = QFileDialog.getSaveFileName(
            self.shell,
            "Capture用機材カタログの保存先",
            str(Path.home() / "htdt-equipment-catalog.json"),
            "HTDT equipment catalog (*.json)",
        )
        if not selected:
            return
        result = export_equipment_catalog_snapshot(
            self.repository,
            Path(selected),
        )
        QMessageBox.information(
            self.shell,
            "Capture用機材カタログを書き出しました",
            f"{result.definition_count} 件の機材定義を書き出しました。\n"
            f"カタログSHA-256: {result.snapshot_sha256}",
        )

    def _can_close_application(self) -> tuple[bool, str | None]:
        if self.data_management_component.can_close_application:
            return True, None
        return False, "データ処理が完了してからHTDTを終了してください"


def build_workflow_application(
    repository: SceneRepository,
    document_id: str,
) -> WorkflowShellWindow:
    composition = WorkflowApplicationComposition(repository, document_id)
    return composition.shell


__all__ = ["WorkflowApplicationComposition", "build_workflow_application"]
