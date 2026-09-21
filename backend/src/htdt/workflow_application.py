from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QMenu

from .cad_input import (
    CAD_SCENE_COMMAND_IDS,
    CadCommandBindings,
    CadInputController,
    bind_cad_input_commands,
    unbind_cad_input_commands,
)
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


_WORKSPACE_COMMAND_IDS = (
    "project.save",
    "edit.undo",
    "edit.redo",
    "room.draw",
    "room.add_speaker",
    "measurements.import_rew",
    "prediction.run",
    "optimization.compare_candidates",
    *CAD_SCENE_COMMAND_IDS,
)


def _available(enabled: bool, reason: str) -> CommandAvailability:
    return (
        CommandAvailability.available()
        if enabled
        else CommandAvailability.unavailable(reason)
    )


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

    def _build_overview_service(self) -> OverviewReadinessService:
        measurement_repository = CadMeasurementRepository(self.repository)
        prediction_repository = CadPredictionRepository(self.repository)
        search_repository = CadSearchRepository(self.repository)
        roomsim_repository = CadRoomSimRepository(self.repository, search_repository)
        objective_repository = CadObjectiveRepository(self.repository, search_repository)
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
        )

    def _navigate_target(self, target: WorkspaceDeepLink) -> bool:
        return self.shell.handle_deep_link(target)

    def _unbind_workspace_commands(self) -> None:
        for command_id in _WORKSPACE_COMMAND_IDS:
            try:
                self.registry.unbind(command_id)
            except KeyError:
                pass

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
                    workspace.controller.is_dirty
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
                "prediction.run",
            ):
                self.registry.unbind(command_id)

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
            "room.view.fit_selection",
            "room.view.fit_all",
        )
        menu = QMenu(workspace)
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
