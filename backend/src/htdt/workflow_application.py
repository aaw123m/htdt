from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .application_pages import (
    ActivityPage,
    CaptureInboxPage,
    ProjectLibraryPage,
    ProjectLibraryService,
    ReferenceLibraryPage,
    SupportPage,
    activity_focus,
    inbox_focus,
    list_recent_revisions,
)
from .cad_input import (
    CAD_SCENE_COMMAND_IDS,
    CadCommandBindings,
    CadInputController,
    bind_cad_input_commands,
    unbind_cad_input_commands,
)
from .cad_view_state import StandardView
from .capture_inbox import CaptureInboxRepository
from .cad_display_labels import (
    revision_display_label,
    variant_display_label,
)
from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_prediction_repository import CadPredictionRepository
from .analysis_export import (
    build_analysis_export,
    render_analysis_csv,
    render_analysis_html,
    render_analysis_json,
    series_from_comparison,
    series_from_measurement_dataset,
)
from .cad_repository import SceneRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_search_repository import CadSearchRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .availability_reasons import availability_reason
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
from .equipment_library import EquipmentLibraryDialog, EquipmentLibraryService
from .installation_handoff import (
    build_installation_handoff,
    handoff_preview_text,
    write_handoff_package,
)
from .installation_output_authority import InstallationReportService
from .measurement_page_workspace import build_measurement_workspace_mount
from .measurement_workflow import MeasurementWorkflowController
from .navigation_target import NavigationTarget, NavigationTargetKind
from .project_lifecycle import ProjectLibrary, ProjectNotFoundError
from .optimization_workflow_workspace import build_optimization_workspace_mount
from .project_bundle import (
    BUNDLE_EXTENSION,
    ProjectBundleError,
    export_project_bundle,
    import_project_bundle,
)
from .project_library import ProjectLibraryEntry, ProjectLibraryError
from .project_library_repository import ProjectLibraryRepository
from .overview_readiness import OverviewReadinessService
from .overview_workspace import OverviewWorkspace
from .palette_search import (
    CommandPaletteProvider,
    PaletteResultKind,
    PaletteSearchService,
    SceneEntityPaletteProvider,
    StaticPaletteProvider,
    help_destinations,
    settings_destinations,
)
from .room_geometry_input import RoomGeometryInputController
from .room_geometry_panel import RoomGeometryPanel
from .room_prediction import RoomPredictionController, RoomPredictionPanel
from .room_acoustics_panel import (
    RoomAcousticsTabs,
    RoomTreatmentPanel,
    SurfaceMaterialPanel,
)
from .room_transform_input import RoomEntityTransformController
from .room_viewport import RoomViewport3D
from .room_workspace import RoomWorkspace
from .workspace_dirty_state import WorkspaceDirtyState
from .system_expansion_workflow import SystemExpansionWorkflowService
from .workflow_help import HelpDialog
from .workflow_navigation import (
    APPLICATION_DESTINATION_LABELS,
    ApplicationDestinationId,
    WorkspaceDeepLink,
    WorkspaceId,
)
from .workflow_settings import DataManagementDialog
from .workflow_shell import (
    TargetFocusResult,
    WorkflowShellWindow,
    WorkspaceMount,
    WorkspaceRegistration,
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


def _available(
    enabled: bool,
    reason_code: str,
    *,
    params: dict[str, object] | None = None,
) -> CommandAvailability:
    """Availability keyed by a stable catalog reason code (#776)."""
    return (
        CommandAvailability.available()
        if enabled
        else CommandAvailability.blocked(
            availability_reason(reason_code, params=params)
        )
    )


def _is_kind(workspace: RoomWorkspace, entity_id: str, kind: str) -> bool:
    try:
        return workspace.controller.document.entity(entity_id).kind == kind
    except KeyError:
        return False


@dataclass(frozen=True, slots=True)
class _NavigationProjectResolution:
    """Typed-target ``project_id`` resolved onto a document — or failed closed.

    ``status`` distinguishes the failure cause so the caller can present an
    actionable message instead of silently substituting another project.
    """

    document_id: str | None
    status: Literal['ok', 'missing', 'archived', 'deleted']
    display_name: str | None = None


class WorkflowApplicationComposition:
    """Application-root composition for UX120-UX140 and Settings.

    Repositories/services remain authoritative; this object only owns lifecycle,
    lazy workspace construction, command binding and restore-time handle rebuild.
    """

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        *,
        project_library: ProjectLibraryRepository | None = None,
        open_project: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.repository_path = Path(repository.path)
        self.data_dir = self.repository_path.parent
        self.document_id = document_id
        self.project_library = project_library or ProjectLibraryRepository(
            repository
        )
        self.project_entry = self.project_library.ensure_document_registered(
            document_id
        )
        self._open_project_callback = open_project
        self._spawned_compositions: list[WorkflowApplicationComposition] = []

        self.registry = CommandRegistry()
        register_default_commands(self.registry)
        self._restore_rebind_note: str | None = None

        registrations = build_canonical_workspace_registrations(
            {
                WorkspaceId.OVERVIEW: self._make_overview,
                WorkspaceId.ROOM: self._make_room,
                WorkspaceId.MEASUREMENT: self._make_measurement,
                WorkspaceId.OPTIMIZATION: self._make_optimization,
            }
        ) + self._application_registrations()
        self.shell = WorkflowShellWindow(registrations)
        self.registry.set_deep_link_handler(self.shell.handle_deep_link)
        self.shell.set_project_identity(document_id)
        self.shell.projectSwitchRequested.connect(
            lambda: self.shell.navigate_to_target(
                NavigationTarget(kind=NavigationTargetKind.PROJECT)
            )
        )

        self.palette_service = self._build_palette_service()
        self.command_palette = CommandPaletteController(
            self.shell,
            self.palette_service,
            context_provider=lambda: self._command_context(),
            on_deep_link=self._navigate_target,
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
        self.registry.bind(
            "installation.export_handoff",
            execute=self._export_installation_handoff,
        )
        self._apply_project_title()
        self._build_project_menu()

    # ---- project library (#450) -----------------------------------------

    def _apply_project_title(self) -> None:
        self.shell.setWindowTitle(
            f"Home Theater Digital Twin — {self.project_entry.display_name}"
        )

    def _build_project_menu(self) -> None:
        menu = self.shell.menuBar().addMenu("プロジェクト")
        menu.addAction(
            "新規プロジェクト…", self._new_project
        )
        menu.addAction(
            "プロジェクトを開く…", self._open_project_dialog
        )
        menu.addSeparator()
        menu.addAction(
            "プロジェクト名を変更…", self._rename_project
        )
        menu.addAction(
            "プロジェクトを複製…", self._duplicate_project
        )
        menu.addSeparator()
        menu.addAction(
            "プロジェクトをエクスポート…", self._export_project_bundle
        )
        menu.addAction(
            "プロジェクトをインポート…", self._import_project_bundle
        )
        menu.addSeparator()
        menu.addAction(
            "アーカイブ…",
            lambda: self._archive_dialog(archived=True),
        )
        menu.addAction(
            "アーカイブから復元…",
            lambda: self._archive_dialog(archived=False),
        )

    def _choose_project(
        self,
        entries: tuple[ProjectLibraryEntry, ...],
        title: str,
        label: str,
    ) -> ProjectLibraryEntry | None:
        if not entries:
            QMessageBox.information(
                self.shell, title, "対象のプロジェクトがありません"
            )
            return None
        dialog = QDialog(self.shell)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        listing = QListWidget(dialog)
        for entry in entries:
            item = QListWidgetItem(entry.display_name)
            item.setData(Qt.ItemDataRole.UserRole, entry.project_id)
            listing.addItem(item)
        listing.setCurrentRow(0)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        item = listing.currentItem()
        if item is None:
            return None
        project_id = item.data(Qt.ItemDataRole.UserRole)
        return next(
            entry for entry in entries if entry.project_id == project_id
        )

    def _pick_one(
        self,
        title: str,
        prompt: str,
        entries: list[tuple[str, str]],
        *,
        selected_row: int = 0,
    ) -> str | None:
        """Single-choice list dialog (#578).

        ``entries`` are ``(human label, exact id)`` pairs: the label is the
        primary text, the exact authority id stays in ``UserRole`` and is
        returned verbatim — selections never resolve by display text.
        """

        if not entries:
            return None
        dialog = QDialog(self.shell)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(prompt, dialog))
        listing = QListWidget(dialog)
        for label, item_id in entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, item_id)
            listing.addItem(item)
        listing.setCurrentRow(min(selected_row, len(entries) - 1))
        layout.addWidget(listing)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        item = listing.currentItem()
        if item is None:
            return None
        item_id = item.data(Qt.ItemDataRole.UserRole)
        return str(item_id)

    def _switch_to_project(self, entry: ProjectLibraryEntry) -> None:
        """Guarded project switch (#450): dirty/running/frozen work refuses
        exactly like window close does, then the new document opens in a
        fresh shell and this one closes."""

        if entry.document_id == self.document_id:
            return
        allowed, reason = self._can_close_application()
        if not allowed:
            self.shell.statusBar().showMessage(
                reason or "現在の処理が完了してからプロジェクトを切り替えてください"
            )
            return
        if not self.shell.close():
            return
        try:
            opened = self.project_library.open_project(entry.project_id)
        except ProjectLibraryError as exc:
            QMessageBox.warning(
                self.shell, "プロジェクトを開けません", str(exc)
            )
            return
        self._open_document(opened.document_id)

    def _open_document(self, document_id: str) -> None:
        if self._open_project_callback is not None:
            self._open_project_callback(document_id)
            return
        composition = WorkflowApplicationComposition(
            self.repository,
            document_id,
            project_library=self.project_library,
            open_project=self._open_project_callback,
        )
        self._spawned_compositions.append(composition)
        composition.shell.show()
        composition.shell.raise_()
        composition.shell.activateWindow()

    def _new_project(self) -> None:
        name, ok = QInputDialog.getText(
            self.shell, "新規プロジェクト", "プロジェクト名:"
        )
        if not ok or not name.strip():
            return
        try:
            entry = self.project_library.create_project(name)
        except ProjectLibraryError as exc:
            QMessageBox.warning(self.shell, "プロジェクトを作成できません", str(exc))
            return
        self._switch_to_project(entry)

    def _open_project_dialog(self) -> None:
        entries = tuple(
            entry
            for entry in self.project_library.list_projects()
            if entry.project_id != self.project_entry.project_id
        )
        entry = self._choose_project(
            entries, "プロジェクトを開く", "開くプロジェクト:"
        )
        if entry is not None:
            self._switch_to_project(entry)

    def _rename_project(self) -> None:
        name, ok = QInputDialog.getText(
            self.shell,
            "プロジェクト名を変更",
            "新しいプロジェクト名:",
            text=self.project_entry.display_name,
        )
        if not ok or not name.strip():
            return
        try:
            self.project_entry = self.project_library.rename_project(
                self.project_entry.project_id, name
            )
        except ProjectLibraryError as exc:
            QMessageBox.warning(self.shell, "名前を変更できません", str(exc))
            return
        self._apply_project_title()

    def _duplicate_project(self) -> None:
        name, ok = QInputDialog.getText(
            self.shell,
            "プロジェクトを複製",
            "複製後のプロジェクト名:",
            text=f"{self.project_entry.display_name} のコピー",
        )
        if not ok or not name.strip():
            return
        try:
            entry = self.project_library.duplicate_project(
                self.project_entry.project_id, name
            )
        except ProjectLibraryError as exc:
            QMessageBox.warning(self.shell, "複製できません", str(exc))
            return
        self._switch_to_project(entry)

    def _export_project_bundle(self) -> None:
        """#488: export the open project as a .htdtproject bundle."""

        selected, _filter = QFileDialog.getSaveFileName(
            self.shell,
            "プロジェクトのエクスポート先",
            str(
                Path.home()
                / f"{self.project_entry.display_name}{BUNDLE_EXTENSION}"
            ),
            f"HTDT project bundle (*{BUNDLE_EXTENSION})",
        )
        if not selected:
            return
        try:
            result = export_project_bundle(
                self.repository,
                self.document_id,
                Path(selected),
            )
        except ProjectBundleError as exc:
            QMessageBox.warning(
                self.shell, "エクスポートできません", str(exc)
            )
            return
        box = QMessageBox(self.shell)
        box.setWindowTitle("プロジェクトをエクスポートしました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            f"{result.row_count} 件のレコードと {result.asset_count} 件の"
            "アセットを書き出しました。"
        )
        box.setDetailedText(f"マニフェストSHA-256: {result.manifest_sha256}")
        box.exec()

    def _import_project_bundle(self) -> None:
        """#488: staged import; a document-id collision is offered the
        explicit import-as-copy path (new project identity)."""

        selected, _filter = QFileDialog.getOpenFileName(
            self.shell,
            "インポートするプロジェクトバンドル",
            str(Path.home()),
            f"HTDT project bundle (*{BUNDLE_EXTENSION})",
        )
        if not selected:
            return
        try:
            result = import_project_bundle(self.repository, Path(selected))
        except ProjectBundleError as exc:
            retry = QMessageBox.question(
                self.shell,
                "そのままインポートできません",
                f"{exc}\n\nコピーとして新しいプロジェクトを作成しますか？",
            )
            if retry != QMessageBox.StandardButton.Yes:
                return
            try:
                result = import_project_bundle(
                    self.repository, Path(selected), import_as_copy=True
                )
            except ProjectBundleError as retry_exc:
                QMessageBox.warning(
                    self.shell, "インポートできません", str(retry_exc)
                )
                return
        QMessageBox.information(
            self.shell,
            "プロジェクトをインポートしました",
            f"{result.imported_rows} 件のレコードと "
            f"{result.imported_assets} 件のアセットを取り込みました。",
        )
        entry = self.project_library.get_by_document_id(result.document_id)
        if entry is not None:
            self._switch_to_project(entry)

    def _archive_dialog(self, *, archived: bool) -> None:
        candidates = tuple(
            entry
            for entry in self.project_library.list_projects(
                include_archived=True
            )
            if entry.archived != archived
            and entry.project_id != self.project_entry.project_id
        )
        title = "プロジェクトをアーカイブ" if archived else "アーカイブから復元"
        entry = self._choose_project(candidates, title, title)
        if entry is None:
            return
        self.project_library.set_archived(entry.project_id, archived)

    def _command_context(self) -> CommandContext | None:
        current = self.shell.router.current_workspace_id
        if isinstance(current, WorkspaceId):
            return CommandContext(current.value)
        return CommandContext.GLOBAL

    def _application_registrations(self) -> tuple[WorkspaceRegistration, ...]:
        """Application-scope destinations (#649) — real surfaces only."""
        return (
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.PROJECTS,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.PROJECTS],
                factory=self._make_projects,
                focus_kinds=frozenset({NavigationTargetKind.PROJECT}),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.INBOX,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.INBOX],
                factory=self._make_inbox,
                focus_kinds=frozenset({
                    NavigationTargetKind.CAPTURE_DELIVERY,
                    NavigationTargetKind.CAPTURE_INBOX_ITEM,
                }),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.ACTIVITY,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.ACTIVITY],
                factory=self._make_activity,
                focus_kinds=frozenset({
                    NavigationTargetKind.ACTIVITY_JOB,
                    NavigationTargetKind.PROJECT_CHECKPOINT,
                }),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.LIBRARY,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.LIBRARY],
                factory=self._make_library,
                focus_kinds=frozenset({NavigationTargetKind.EQUIPMENT_DEFINITION}),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.SUPPORT,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.SUPPORT],
                factory=self._make_support,
                focus_kinds=frozenset({NavigationTargetKind.HELP_TOPIC}),
            ),
        )

    def _build_palette_service(self) -> PaletteSearchService:
        """Composable palette providers (#585): commands, entities, settings, help."""

        def entities() -> tuple:
            revision = self.repository.latest(self.document_id)
            return () if revision is None else revision.document.entities

        def entity_link(entity) -> WorkspaceDeepLink:
            section = (
                'placement'
                if getattr(entity, 'kind', '')
                in ('speaker', 'seat', 'measurement_point')
                else 'objects'
            )
            return WorkspaceDeepLink(
                WorkspaceId.ROOM, section, entity_id=entity.entity_id
            )

        return PaletteSearchService(
            (
                CommandPaletteProvider(self.registry),
                SceneEntityPaletteProvider(entities, entity_link),
                StaticPaletteProvider(
                    'settings',
                    PaletteResultKind.SETTINGS,
                    settings_destinations(),
                    self._open_settings_destination,
                ),
                StaticPaletteProvider(
                    'help',
                    PaletteResultKind.HELP,
                    help_destinations(),
                    self._open_help_topic,
                ),
            ),
            on_deep_link=self._navigate_target,
        )

    def _open_settings_destination(self, destination_id: str) -> bool:
        if destination_id != 'settings.data':
            return False
        self.settings_dialog.open_settings()
        return True

    def _open_help_topic(self, topic_id: str) -> bool:
        if topic_id == 'help.shortcuts':
            HelpDialog.shortcuts(self.registry, parent=self.shell).exec()
            return True
        if topic_id == 'help.palette':
            HelpDialog.palette_usage(parent=self.shell).exec()
            return True
        return False

    def _open_project(self, document_id: str) -> None:
        """Switch the whole composition to another persisted document (#649)."""
        reason = self._switch_project(document_id)
        if reason is not None:
            self.shell.statusBar().showMessage(reason)

    def _switch_project(self, document_id: str) -> str | None:
        """Guarded whole-composition project switch; returns the block reason.

        ``None`` means the composition is now bound to ``document_id`` (or
        already was). Typed navigation reuses exactly this path so a
        cross-project deep link gets the same dirty-state/running-operation
        policy as a manual Project Library switch (#610).
        """
        if document_id == self.document_id:
            return None
        if self.shell.data_mutations_frozen:
            return 'データ処理中はプロジェクトを切り替えられません'
        # #610: project switching offers the same explicit Save/Discard/
        # Recover-Draft resolution instead of a hard block.
        allowed, reason = self.shell.router.resolve_dispose_all('project_switch')
        if not allowed:
            return reason or '現在の作業を完了してからプロジェクトを切り替えてください'
        self.shell.dispose_data_workspaces()
        self._unbind_workspace_commands()
        self.document_id = document_id
        self.shell.set_project_identity(document_id)
        # #775: legacy entries recorded without project identity must never
        # replay inside the new project's namespace.
        self.shell.navigation_history.drop_unscoped_project_entries()
        if not self.shell.navigate(WorkspaceId.OVERVIEW):
            raise RuntimeError('プロジェクト切替後の概要画面を再構築できませんでした')
        return None

    # -- typed-navigation project establishment (#775) -------------------

    def navigation_project_identity(self) -> str:
        """Canonical project id stamped onto unscoped project targets.

        Registry ``project_id`` is the stable cross-device identity; a
        document not yet registered keeps its ``document_id`` as identity.
        """
        record = ProjectLibrary(self.repository_path).find_by_document(
            self.document_id
        )
        return record.project_id if record is not None else self.document_id

    def establish_navigation_project(
        self, project_id: str
    ) -> tuple[bool, str | None]:
        """Resolve a typed target's ``project_id`` and switch to it.

        Canonical registry ids and legacy document-scoped ids both resolve
        to a bound ``document_id``; missing/archived/deleted projects fail
        with an actionable message and never fall back to the current,
        same-name or newest project. The switch itself is the guarded
        ``_switch_project`` path — identical dirty-state policy to a manual
        switch, and a blocked switch leaves the current project untouched.
        """
        resolved = self._resolve_navigation_project(project_id)
        if resolved.document_id is None:
            if resolved.status == 'archived':
                return False, (
                    '対象のプロジェクトはアーカイブされています: '
                    f'{resolved.display_name or project_id}'
                )
            if resolved.status == 'deleted':
                return False, (
                    '対象のプロジェクトは削除済みです: '
                    f'{resolved.display_name or project_id}'
                )
            return False, f'対象のプロジェクトが見つかりません: {project_id}'
        reason = self._switch_project(resolved.document_id)
        if reason is not None:
            return False, reason
        return True, None

    def _resolve_navigation_project(
        self, project_id: str
    ) -> '_NavigationProjectResolution':
        """Canonical ``project_id``/``document_id`` resolution — fail closed."""
        library = ProjectLibrary(self.repository_path)
        record = None
        try:
            record = library.get_project(project_id)
        except ProjectNotFoundError:
            # Versioned normalization: older links carried the bound
            # document_id where a canonical project_id now goes (#775 E).
            record = library.find_by_document(project_id)
        if record is not None:
            if record.status != 'active':
                return _NavigationProjectResolution(
                    None, 'archived', record.display_name
                )
            return _NavigationProjectResolution(
                record.document_id, 'ok', record.display_name
            )
        for tombstone in library.list_tombstones():
            if project_id in (tombstone.project_id, tombstone.document_id):
                return _NavigationProjectResolution(
                    None, 'deleted', tombstone.display_name
                )
        # Unregistered document with a live head: a pre-registry link stays
        # resolvable against exactly that document — never a substitute.
        if self.repository.current_head(project_id) is not None:
            return _NavigationProjectResolution(project_id, 'ok', project_id)
        return _NavigationProjectResolution(None, 'missing', None)

    def _make_projects(self) -> WorkspaceMount:
        page = ProjectLibraryPage(
            ProjectLibraryService(self.repository),
            current_document_id=lambda: self.document_id,
        )
        page.project_open_requested.connect(self._open_project)
        page.commission_requested.connect(self._open_commissioning_wizard)
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: TargetFocusResult(focused=True),
        )

    def _make_inbox(self) -> WorkspaceMount:
        repository = CaptureInboxRepository(self.repository)
        page = CaptureInboxPage(
            repository.list_items,
            on_navigate=self._navigate_target,
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: inbox_focus(page, target),
        )

    def _make_activity(self) -> WorkspaceMount:
        page = ActivityPage(
            lambda limit: list_recent_revisions(self.repository, limit)
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: activity_focus(page, target),
        )

    def _make_library(self) -> WorkspaceMount:
        service = EquipmentLibraryService(self.repository)
        page = ReferenceLibraryPage(service.definitions)

        def manage() -> None:
            dialog = EquipmentLibraryDialog(service, parent=page)
            dialog.exec()
            page.refresh()

        page.manage_requested.connect(manage)
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: (
                page.focus_definition(target.primary_id)
                if target.primary_id is not None
                else TargetFocusResult(focused=True)
            ),
        )

    def _make_support(self) -> WorkspaceMount:
        page = SupportPage(self.data_dir)
        return WorkspaceMount.from_widget(
            page,
            focus_target=lambda target: TargetFocusResult(focused=True),
        )

    def _open_commissioning_wizard(self) -> None:
        """First-run project commissioning wizard (#588)."""
        from .commissioning_wizard import CommissioningWizard

        wizard = CommissioningWizard(
            self.repository,
            self.document_id,
            data_dir=self.data_dir,
            overview_service=self._build_overview_service(),
            parent=self.shell,
        )
        wizard.navigate_requested.connect(self._navigate_target)
        if wizard.exec() == wizard.DialogCode.Accepted:
            self._open_project(wizard.created_document_id)

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
        variant_service = SystemExpansionWorkflowService(
            self.repository, self.document_id
        )
        equipment_bindings = CadEquipmentBindingRepository(
            self.repository, variant_service.equipment_repository
        )
        return OverviewReadinessService(
            self.repository,
            measurement_repository,
            prediction_repository,
            search_repository,
            validation_repository,
            quality_source=CadMeasurementQualityRepository(measurement_repository),
            impact_source=self.repository,
            variant_source=variant_service,
            equipment_source=equipment_bindings,
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
            'command.blocked.editing_not_available',
        )
        has_selection = lambda: _available(  # noqa: E731
            _has_selection(), 'command.blocked.selection_required'
        )
        sel_at_least = lambda count: (  # noqa: E731
            lambda: _available(
                _min_selection(count),
                'command.blocked.min_selection',
                params={'required': count},
            )
        )
        room_ready = lambda: _available(  # noqa: E731
            workspace.controller.document.room is not None,
            'command.blocked.room_required',
        )
        clipboard_ready = lambda: _available(  # noqa: E731
            workspace.controller.can_edit
            and getattr(workspace, "_clipboard", None) is not None
            and bool(workspace._clipboard.entities),
            'command.blocked.clipboard_empty',
        )
        isolation_active = lambda: _available(  # noqa: E731
            workspace._pre_isolation_hidden is not None,
            'command.blocked.isolation_inactive',
        )
        underlays_exist = lambda: _available(  # noqa: E731
            bool(workspace.controller.underlays()),
            'command.blocked.underlay_required',
        )
        selected_speaker = lambda: _available(  # noqa: E731
            _has_selection()
            and workspace.controller.can_edit
            and workspace.controller.selected_id is not None
            and _is_kind(workspace, workspace.controller.selected_id, "speaker"),
            'command.blocked.speaker_required',
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
        material_panel = SurfaceMaterialPanel(workspace.controller)
        treatment_panel = RoomTreatmentPanel(workspace.controller)
        workspace.attach_acoustics_panel(
            RoomAcousticsTabs(prediction_panel, material_panel, treatment_panel)
        )

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
                    'room.edit.requires_editable_selection',
                ),
                "room.transform.rotate": lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not geometry_input.is_active,
                    'room.edit.requires_editable_selection',
                ),
                "room.view.fit_selection": lambda: _available(
                    workspace.controller.selected_id is not None,
                    'room.view.requires_selection',
                ),
                "room.view.fit_all": lambda: CommandAvailability.available(),
                "room.edit.cancel": lambda: _available(
                    transform_input.is_active
                    or geometry_input.is_active
                    or workspace.controller.working.has_preview,
                    'command.blocked.nothing_to_cancel',
                ),
                "room.edit.commit": lambda: _available(
                    transform_input.is_active
                    or geometry_input.is_active
                    or workspace.controller.working.has_preview,
                    'command.blocked.nothing_to_commit',
                ),
                "room.edit.duplicate": lambda: _available(
                    workspace.controller.selected_id is not None
                    and workspace.controller.can_edit
                    and not geometry_input.is_active
                    and not transform_input.is_active,
                    'room.edit.requires_editable_selection',
                ),
                "room.transform.axis_x": lambda: _available(
                    transform_input.is_active,
                    'room.transform.requires_active_transform',
                ),
                "room.transform.axis_y": lambda: _available(
                    transform_input.is_active,
                    'room.transform.requires_active_transform',
                ),
                "room.transform.axis_z": lambda: _available(
                    transform_input.is_active,
                    'room.transform.requires_active_transform',
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
                    'project.save.nothing_to_save',
                ),
            )
            self.registry.bind(
                "edit.undo",
                execute=workspace.undo,
                availability=lambda: _available(
                    workspace.controller.working.can_undo
                    and not workspace.controller.working.has_preview,
                    'command.blocked.nothing_to_undo',
                ),
            )
            self.registry.bind(
                "edit.redo",
                execute=workspace.redo,
                availability=lambda: _available(
                    workspace.controller.working.can_redo
                    and not workspace.controller.working.has_preview,
                    'command.blocked.nothing_to_redo',
                ),
            )
            self.registry.bind(
                "room.draw",
                execute=geometry_input.start_sketch,
                availability=lambda: _available(
                    workspace.controller.recovery_candidate is None
                    and not workspace.controller.working.has_preview
                    and not transform_input.is_active,
                    'room.draw.blocked_while_editing',
                ),
            )
            self.registry.bind(
                "room.add_speaker",
                execute=lambda: workspace.add_object("speaker"),
                availability=lambda: _available(
                    workspace.controller.can_edit
                    and not geometry_input.is_active
                    and not transform_input.is_active,
                    'room.add_speaker.requires_finished_room',
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
                    'room.edit.requires_editable_selection',
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
                    'command.blocked.selection_required',
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
                    'command.blocked.selection_required',
                ),
            )
            self.registry.bind(
                "room.measure",
                execute=workspace.toggle_measure,
                availability=lambda: _available(
                    workspace.controller.document.room is not None,
                    'command.blocked.room_required',
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

        def dirty_state() -> WorkspaceDirtyState:
            if prediction.before_deactivate()[0] is False:
                return 'busy'
            return workspace.controller.dirty_state()

        def focus_target(target: NavigationTarget) -> TargetFocusResult:
            if target.primary_id is None:
                return TargetFocusResult(focused=True)
            try:
                workspace.controller.document.entity(target.primary_id)
            except KeyError:
                return TargetFocusResult(
                    focused=False,
                    message='対象の項目がこのプロジェクトに存在しません',
                )
            workspace.select_entity(target.primary_id)
            return TargetFocusResult(focused=True)

        return WorkspaceMount(
            widget=workspace,
            on_activate=activate,
            on_deactivate=deactivate,
            before_deactivate=before_deactivate,
            dirty_state=dirty_state,
            resolve_dirty_state=workspace.controller.resolve_dirty_state,
            on_context_changed=workspace.set_context,
            on_entity_requested=workspace.select_entity,
            on_close=close,
            focus_kinds=frozenset({
                NavigationTargetKind.SCENE_ENTITY,
                NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE,
            }),
            focus_target=focus_target,
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
            return CommandAvailability.blocked(
                availability_reason('prediction.run.running')
            )
        if workspace.controller.working.has_preview:
            return CommandAvailability.blocked(
                availability_reason('command.blocked.edit_in_progress')
            )
        if workspace.controller.is_dirty:
            return CommandAvailability.blocked(
                availability_reason('prediction.run.requires_saved_layout')
            )
        if panel.receiver.currentData() is None:
            return CommandAvailability.blocked(
                availability_reason('prediction.run.receiver_required')
            )
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
            return CommandAvailability.blocked(
                availability_reason('measurement.import.requires_saved_scene')
            )
        return CommandAvailability.available()

    def _make_optimization(self) -> WorkspaceMount:
        mount = build_optimization_workspace_mount(
            self.repository,
            self.document_id,
            on_navigate=self._navigate_target,
        )
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
                    'project.save.unavailable_or_busy',
                ),
            )
            self.registry.bind(
                "edit.undo",
                execute=controller.undo,
                availability=lambda: _available(
                    edit_idle() and controller.working.can_undo,
                    'edit.undo.unavailable_or_busy',
                ),
            )
            self.registry.bind(
                "edit.redo",
                execute=controller.redo,
                availability=lambda: _available(
                    edit_idle() and controller.working.can_redo,
                    'edit.redo.unavailable_or_busy',
                ),
            )
            self.registry.bind(
                "optimization.compare_candidates",
                execute=controller.refresh_pareto_comparison,
                availability=lambda: _available(
                    controller.search_selected_spec_id is not None,
                    'optimization.compare.requires_spec_selection',
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

        def focus_target(target: NavigationTarget) -> TargetFocusResult:
            section = {
                NavigationTargetKind.SYSTEM_VARIANT: 'comparison',
                NavigationTargetKind.OPTIMIZATION_CANDIDATE: 'candidates',
                NavigationTargetKind.OPTIMIZATION_COMPARISON: 'comparison',
                NavigationTargetKind.COMMISSIONING_EVALUATION: 'validation',
            }.get(target.kind, 'setup')
            select_section = getattr(workspace, 'select_section', None)
            if select_section is None:
                return TargetFocusResult(focused=False)
            select_section(section)
            return TargetFocusResult(focused=True)

        mount.on_activate = activate
        mount.on_deactivate = deactivate
        mount.focus_kinds = frozenset({
            NavigationTargetKind.SYSTEM_VARIANT,
            NavigationTargetKind.OPTIMIZATION_CANDIDATE,
            NavigationTargetKind.OPTIMIZATION_COMPARISON,
            NavigationTargetKind.COMMISSIONING_EVALUATION,
        })
        mount.focus_target = focus_target
        return mount

    def _release_data_handles(self) -> None:
        self.shell.dispose_data_workspaces()
        self._unbind_workspace_commands()

    def _reopen_data_handles(self) -> None:
        self.repository = SceneRepository(self.repository_path)
        self._rebind_project_identity_after_restore()
        # Restore completion reports the actual active project (#768 D);
        # surfaced after thaw rebuilds the destination.
        self._restore_rebind_note = (
            f'アクティブプロジェクト: {self.document_id}'
            if self.document_id
            else '復元が完了しました。プロジェクトを選択してください。'
        )

    def _rebind_project_identity_after_restore(self) -> None:
        """Re-resolve the active project against the restored generation (#768).

        Whole-data replacement swapped the database: the previous
        ``document_id`` may not exist in the restored universe, and every
        pre-restore navigation entry addresses a different data epoch. The
        chip and composition must reflect the actual restored authority —
        never a stale id kept alive for UI continuity.
        """
        self.shell.navigation_history.clear()
        previous = self.document_id
        if self.repository.current_head(previous) is not None:
            # The exact pre-restore document still exists — it stays active.
            self.shell.set_project_identity(previous)
            return
        # Resolve through canonical project identity: registry first, then
        # unregistered live documents (restores from pre-registry builds).
        library = ProjectLibrary(self.repository_path)
        active = library.list_projects()
        if active:
            self.document_id = active[0].document_id
            self.shell.set_project_identity(self.document_id)
            return
        head_document_ids = [
            entry.document_id
            for entry in ProjectLibraryService(self.repository).list_projects()
        ]
        if head_document_ids:
            self.document_id = head_document_ids[0]
            self.shell.set_project_identity(self.document_id)
            return
        # The restored generation has no projects at all: route to the
        # Project Library surface for explicit selection — a non-existent
        # document id must never remain the active project.
        self.document_id = ''
        self.shell.set_project_identity(None)

    def _freeze_data_mutations(self) -> None:
        # Gate the command authority first so QShortcut activations and command
        # palette entries fail closed while shell widgets are being disabled.
        self.registry.freeze_data_mutations()
        self.shell.freeze_data_mutations()

    def _thaw_data_mutations(self) -> None:
        self.shell.thaw_data_mutations()
        self.registry.thaw_data_mutations()
        if self.shell.router.current_workspace_id is None:
            destination = (
                WorkspaceId.OVERVIEW
                if self.document_id
                else ApplicationDestinationId.PROJECTS
            )
            if not self.shell.navigate(destination):
                raise RuntimeError("復元後の画面を再構築できませんでした")
        note, self._restore_rebind_note = self._restore_rebind_note, None
        if note is not None:
            self.shell.statusBar().showMessage(note)

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
        box = QMessageBox(self.shell)
        box.setWindowTitle("Capture用機材カタログを書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"{result.definition_count} 件の機材定義を書き出しました。")
        box.setDetailedText(f"カタログSHA-256: {result.snapshot_sha256}")
        box.exec()

    def _export_installation_handoff(self) -> None:
        """Operator action behind ``installation.export_handoff`` (#453).

        Read-only handoff: the operator explicitly selects the SceneRevision
        and SystemVariant to package, reviews the completeness of every
        installation section, then writes the deterministic dimension
        sheets, settings CSV, coordinates CSV and project report into a
        chosen directory.
        """

        revisions = self.repository.list_revision_summaries(self.document_id)
        if not revisions:
            QMessageBox.warning(
                self.shell,
                "設置ハンドオフ",
                "書き出せるシーンリビジョンがありません。",
            )
            return
        revision_label_map = self.repository.revision_labels(self.document_id)
        scene_revision_id = self._pick_one(
            "設置ハンドオフ",
            "シーンリビジョンを選択してください",
            [
                (
                    revision_display_label(item, revision_label_map),
                    item.revision_id,
                )
                for item in revisions
            ],
            selected_row=len(revisions) - 1,
        )
        if scene_revision_id is None:
            return
        variants = CadSystemVariantRepository(
            self.repository
        ).list_variants(self.document_id)
        system_variant_id = self._pick_one(
            "設置ハンドオフ",
            "システムバリアントを選択してください",
            [('（なし）', '')]
            + [
                (variant_display_label(item), item.variant_id)
                for item in variants
            ],
        )
        if system_variant_id is None:
            return
        service = InstallationReportService(
            scene_repository=self.repository
        )
        handoff = build_installation_handoff(
            service,
            scene_revision_id=scene_revision_id,
            system_variant_id=system_variant_id,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        preview = QDialog(self.shell)
        preview.setWindowTitle("設置ハンドオフ プレビュー")
        preview_layout = QVBoxLayout(preview)
        preview_text = QPlainTextEdit(preview)
        preview_text.setReadOnly(True)
        preview_text.setPlainText(handoff_preview_text(handoff))
        preview_layout.addWidget(preview_text)
        preview_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=preview,
        )
        preview_buttons.accepted.connect(preview.accept)
        preview_buttons.rejected.connect(preview.reject)
        preview_layout.addWidget(preview_buttons)
        preview.resize(760, 560)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        directory = QFileDialog.getExistingDirectory(
            self.shell,
            "ハンドオフの保存先フォルダ",
        )
        if not directory:
            return
        outputs = write_handoff_package(handoff, directory)
        QMessageBox.information(
            self.shell,
            "設置ハンドオフを書き出しました",
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in outputs.values()),
        )

    def _export_analysis_bundle(self) -> None:
        """Operator action behind ``analysis.export_bundle`` (#512).

        Packages the project's persisted measurement datasets and A/B
        comparisons into the deterministic analysis export (CSV/JSON/HTML)
        via the typed series adapters — provenance and historical flags
        are derived from the real authorities, never typed in.
        """

        measurements = CadMeasurementRepository(self.repository)
        records = measurements.list_measurements(self.document_id)
        comparisons = measurements.list_comparisons(self.document_id)
        if not records and not comparisons:
            QMessageBox.warning(
                self.shell,
                "解析エクスポート",
                "書き出せる測定・比較データがありません。",
            )
            return
        head = self.repository.current_head(self.document_id)
        current_revision_id = (
            head.revision_id if head is not None else None
        )
        series = []
        for record in records:
            try:
                bundle = measurements.get_evidence_bundle(
                    record.measurement_id
                )
            except ValueError:
                continue  # measurement without an FR dataset
            series.append(
                series_from_measurement_dataset(
                    bundle.dataset,
                    record,
                    current_scene_revision_id=current_revision_id,
                )
            )
        for comparison in comparisons:
            series.append(
                series_from_comparison(
                    comparison,
                    current_scene_revision_id=current_revision_id,
                )
            )
        title, ok = QInputDialog.getText(
            self.shell,
            "解析エクスポート",
            "エクスポート名を入力してください",
            text="解析エクスポート",
        )
        if not ok or not title:
            return
        export = build_analysis_export(
            document_id=self.document_id,
            title=title,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            series=tuple(series),
        )
        directory = QFileDialog.getExistingDirectory(
            self.shell,
            "解析エクスポートの保存先フォルダ",
        )
        if not directory:
            return
        target = Path(directory)
        written = (
            target / 'analysis_export.csv',
            target / 'analysis_export.json',
            target / 'analysis_report.html',
        )
        written[0].write_text(
            render_analysis_csv(export), encoding='utf-8'
        )
        written[1].write_text(
            render_analysis_json(export), encoding='utf-8'
        )
        written[2].write_text(
            render_analysis_html(export), encoding='utf-8'
        )
        box = QMessageBox(self.shell)
        box.setWindowTitle("解析エクスポートを書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in written)
        )
        box.setDetailedText(f"spec SHA-256: {export.spec_sha256}")
        box.exec()

    def _can_close_application(self) -> tuple[bool, str | None]:
        if self.data_management_component.can_close_application:
            return True, None
        return False, "データ処理が完了してからHTDTを終了してください"


def build_workflow_application(
    repository: SceneRepository,
    document_id: str,
    *,
    project_library: ProjectLibraryRepository | None = None,
    open_project: Callable[[str], None] | None = None,
) -> WorkflowShellWindow:
    composition = WorkflowApplicationComposition(
        repository,
        document_id,
        project_library=project_library,
        open_project=open_project,
    )
    return composition.shell


__all__ = ["WorkflowApplicationComposition", "build_workflow_application"]
