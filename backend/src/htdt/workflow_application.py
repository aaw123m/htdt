from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import weakref

from PySide6.QtCore import QByteArray, QPointF, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
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
    projects_focus,
)
from . import file_dialog_memory
from .cad_input import (
    CAD_SCENE_COMMAND_IDS,
    CadCommandBindings,
    CadInputController,
    bind_cad_input_commands,
    unbind_cad_input_commands,
)
from .cad_view_state import StandardView
from .activity_center import (
    ACTIVITY_HISTORY_FILENAME,
    TERMINAL_STATES,
    ActivityCenter,
    NavigationPolicy,
    OperationClass,
    OperationState,
)
from .capture_inbox import CaptureInboxRepository
from .cad_av_sync_repository import CadAVSyncRepository
from .cad_calibration_repository import CadCalibrationRepository
from .cad_design_checkpoint_repository import CadDesignCheckpointRepository
from .cad_operating_preset_repository import CadOperatingPresetRepository
from .cad_project_activity import CadProjectActivityService
from .cad_project_activity_repository import CadProjectActivityNoteRepository
from .cad_system_variant_lifecycle import CadSystemVariantLifecycleRepository
from .commissioning_plan import CommissioningPlanRepository
from .cad_display_labels import (
    revision_display_label,
    variant_display_label,
)
from .cad_assumption_decision_repository import CadAssumptionDecisionRepository
from .cad_design_decision_repository import CadDesignDecisionRepository
from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_equipment_instance_repository import CadInstalledEquipmentRepository
from .cad_installation_context_repository import CadInstallationContextRepository
from .cad_system_health_repository import CadSystemHealthRepository
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_prediction_repository import CadPredictionRepository
from .analysis_export import (
    AnalysisExportMeta,
    build_analysis_export,
    comparison_metadata_entries,
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
from .export_io import claim_export_stem, write_export_files
from .availability_reasons import availability_reason
from .command_palette import CommandPaletteController
from .capture_receiver_controller import CaptureReceiverController
from .capture_receiver_settings import CaptureReceiverPanel
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
from .application_preferences import (
    ApplicationPreferenceStore,
    PreferenceChange,
)
from .cad_display_units import length_display_policy_from_preferences
from .capture_retention import CaptureRetentionService
from .capture_retention_ui import RetentionPolicyWidget
from .automatic_backup_runner import AutomaticBackupRunner
from .data_management_ui import build_data_management_component
from .reference_library_sources import build_reference_library_index
from .file_dialog_memory import FileDialogMemoryStore
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
from .help_registry import build_help_registry
from .localization import detect_system_locale
from .navigation_target import (
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
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
    HelpTopicPaletteProvider,
    NavigationItemPaletteProvider,
    PaletteNavigationItem,
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
from .room_workspace import RoomWorkspace, SelectionInspector
from . import dirty_state_dialog
from .user_facing_error import (
    operation_error_message,
    to_user_facing_error,
)
from .workspace_dirty_state import WorkspaceDirtyState
from .system_expansion_workflow import SystemExpansionWorkflowService
from .support_diagnostics import (
    DiagnosticPackageBuilder,
    package_filename,
    run_health_checks,
)
from .workflow_help import HelpDialog
from .authority_graph import (
    build_authority_graph,
    measurement_authority_source,
    scene_revision_authority_source,
    system_variant_authority_source,
)
from .authority_inspector_ui import AuthorityInspectorDialog
from .workflow_navigation import (
    APPLICATION_DESTINATION_LABELS,
    ApplicationDestinationId,
    WorkspaceDeepLink,
    WorkspaceId,
)
from .window_state import (
    PersistedWindowState,
    load_window_state,
    save_window_state,
)
from .workflow_settings import DataManagementDialog, PreferencesWidget
from .workflow_shell import (
    TargetFocusResult,
    WorkflowShellWindow,
    WorkspaceMount,
    WorkspaceRegistration,
    build_canonical_workspace_registrations,
)


_LOGGER = logging.getLogger(__name__)

_DISPLAY_LENGTH_PREFERENCE_KEYS = frozenset(
    {'display_input.length_unit', 'display_input.numeric_precision'}
)


def bind_inspector_display_length_policy(
    inspector: SelectionInspector,
    preferences: ApplicationPreferenceStore,
) -> None:
    """Apply the #496 length display policy to a room inspector, live.

    ``display_input.length_unit`` / ``display_input.numeric_precision`` are
    user-local presentation state — canonical storage stays SI metres. The
    subscription re-applies on later commits; the weakref keeps a destroyed
    inspector from breaking unrelated preference writes.
    """

    inspector_ref = weakref.ref(inspector)

    def apply() -> None:
        target = inspector_ref()
        if target is None:
            return
        policy = length_display_policy_from_preferences(preferences)
        try:
            target.set_display_units(
                length_unit=policy.unit, precision=policy.decimals
            )
        except RuntimeError:
            # Qt object already destroyed.
            pass

    def on_change(change: PreferenceChange) -> None:
        if change.key in _DISPLAY_LENGTH_PREFERENCE_KEYS:
            apply()

    apply()
    preferences.subscribe(on_change)


def bind_measure_display_length_policy(
    panel,
    preferences: ApplicationPreferenceStore,
) -> None:
    """Apply the #496 length display policy to the room measure panel, live.

    Same contract as :func:`bind_inspector_display_length_policy` — canonical
    storage stays SI metres; the subscription re-formats the current result
    when the user changes unit or precision.
    """

    panel_ref = weakref.ref(panel)

    def apply() -> None:
        target = panel_ref()
        if target is None:
            return
        try:
            target.set_length_policy(
                length_display_policy_from_preferences(preferences)
            )
        except RuntimeError:
            pass

    def on_change(change: PreferenceChange) -> None:
        if change.key in _DISPLAY_LENGTH_PREFERENCE_KEYS:
            apply()

    apply()
    preferences.subscribe(on_change)


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
    "room.select.all",
    "room.select.invert",
    "room.select.none",
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
        capture_receiver: CaptureReceiverController | None = None,
        preferences: ApplicationPreferenceStore | None = None,
        safe_mode: bool = False,
    ) -> None:
        self.repository = repository
        self.repository_path = Path(repository.path)
        self.data_dir = self.repository_path.parent
        # Safe Mode (#739): the launcher opted this session out of saved
        # layout restore and background jobs (automatic backups) — the
        # minimum that could repeat the risky initialization being escaped.
        self.safe_mode = safe_mode
        # App-local last-directory memory for every native file dialog
        # (round-8): dialogs reopen where the operator last worked. Safe
        # Mode binds the ephemeral store so the session neither restores
        # nor persists remembered directories.
        file_dialog_memory.configure(
            FileDialogMemoryStore.ephemeral()
            if self.safe_mode
            else FileDialogMemoryStore.for_data_dir(self.data_dir)
        )
        self.document_id = document_id
        self.capture_receiver = capture_receiver
        self._automatic_backup_runner: AutomaticBackupRunner | None = None
        # ApplicationPreferences are app-local truth shared with every
        # integration that reads them — one store per data root (#740).
        self.preferences = preferences or ApplicationPreferenceStore.for_data_dir(
            self.data_dir
        )
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
        # Canonical offline help/glossary registry (#623) — indexed by the
        # palette's help provider and rendered by HelpDialog.topic.
        self.help_registry = build_help_registry()
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
        self.shell.set_project_identity(self.project_entry.display_name)
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
        # Application activity center (#603): one registry for every
        # app-scoped long-running operation; history persists next to the
        # data root so the next session can see what ran/failed last.
        self.activity_center = ActivityCenter()
        self.activity_center.subscribe(self._persist_activity_history)
        self.data_management_controller = DataManagementController(
            backend,
            lifecycle,
            parent=self.shell,
            activity_center=self.activity_center,
        )
        self.data_management_component = build_data_management_component(
            self.data_management_controller
        )
        capture_panel = (
            CaptureReceiverPanel(
                capture_receiver,
                self.navigation_project_identity,
                parent=self.shell,
            )
            if capture_receiver is not None
            else None
        )
        preferences_panel = PreferencesWidget(
            self.preferences, parent=self.shell
        )
        self.settings_dialog = DataManagementDialog(
            self.data_management_component,
            self.shell,
            capture_panel=capture_panel,
            preferences_panel=preferences_panel,
            retention_panel=RetentionPolicyWidget(
                CaptureRetentionService(self.repository),
                is_busy=lambda: self.data_management_controller.is_busy,
                parent=self.shell,
            ),
        )
        if capture_receiver is not None:
            capture_receiver.delivery_staged.connect(
                self._announce_capture_delivery
            )
        self.shell.settingsRequested.connect(self.settings_dialog.open_settings)
        self.shell.register_close_guard(self._can_close_application)
        self.shell.workflow_application = self  # type: ignore[attr-defined]
        # Window-state persistence: restore geometry + last workspace now
        # (skipped under Safe Mode's restore_saved_layout=False policy),
        # and save on every committed close so project switches that
        # rebuild the shell keep the user's place too.
        self._restore_window_state()
        self.shell.register_close_hook(self._save_window_state)
        self.shell.register_close_hook(self._shutdown_automatic_backup)
        self.registry.bind(
            "equipment.export_capture_catalog",
            execute=self._export_capture_equipment_catalog,
        )
        self.registry.bind(
            "installation.export_handoff",
            execute=self._export_installation_handoff,
        )
        self.registry.bind(
            "analysis.export_bundle",
            execute=self._export_analysis_bundle,
        )
        self.registry.bind(
            "project.deliverables",
            execute=self._open_deliverables,
        )
        self._apply_project_title()
        self._build_project_menu()

    # ---- project library (#450) -----------------------------------------

    def _apply_project_title(self) -> None:
        # ``project_entry`` may be unbound while a restore finds no projects.
        name = (
            self.project_entry.display_name
            if self.project_entry is not None
            else None
        )
        title = (
            'Home Theater Digital Twin'
            if name is None
            else f"Home Theater Digital Twin — {name}"
        )
        if self.safe_mode:
            title += ' — セーフモード'
        self.shell.setWindowTitle(title)

    # -- window-state persistence (round8) --------------------------------

    def _restore_window_state(self) -> None:
        if self.safe_mode:
            return
        state = load_window_state(
            self.data_dir, project_ref=self._window_state_project_ref()
        )
        if state is None:
            return
        if state.geometry_b64:
            try:
                self.shell.restoreGeometry(
                    QByteArray.fromBase64(
                        QByteArray(state.geometry_b64.encode('ascii'))
                    )
                )
            except (RuntimeError, ValueError):
                _LOGGER.warning('saved window geometry could not be applied')
        if state.contexts:
            self.shell.seed_selected_contexts(state.contexts)
        if state.workspace is not None:
            try:
                if state.workspace != str(self.shell.current_workspace_id):
                    self.shell.navigate(state.workspace)
            except (RuntimeError, ValueError):
                _LOGGER.warning(
                    'saved workspace %r could not be restored',
                    state.workspace,
                )

    def _window_state_project_ref(self) -> str | None:
        return (
            self.project_entry.project_id
            if self.project_entry is not None
            else None
        )

    def _save_window_state(self) -> None:
        # Safe Mode must not touch persisted session state in either
        # direction — a reduced safe-mode session overwriting the saved
        # layout would lose the user's real preferences on close.
        if self.safe_mode:
            return
        try:
            workspace = self.shell.current_workspace_id
        except RuntimeError:
            workspace = None
        geometry = bytes(self.shell.saveGeometry().toBase64()).decode('ascii')
        save_window_state(
            self.data_dir,
            PersistedWindowState(
                geometry_b64=geometry,
                workspace=None if workspace is None else str(workspace),
                contexts=self.shell.selected_contexts(),
            ),
            project_ref=self._window_state_project_ref(),
        )

    # -- automatic backup tick (#755 round8) -------------------------------

    def start_automatic_backup(self) -> None:
        """Kick off the once-per-launch due check; no-op under Safe Mode."""

        if self.safe_mode:
            return
        if self._automatic_backup_runner is None:
            self._automatic_backup_runner = AutomaticBackupRunner(
                self.data_dir, parent=self.shell
            )
            self._automatic_backup_runner.backup_started.connect(
                self._on_automatic_backup_started
            )
            self._automatic_backup_runner.backup_completed.connect(
                self._on_automatic_backup_completed
            )
        try:
            self._automatic_backup_runner.start()
        except Exception:
            _LOGGER.exception('automatic backup check could not start')

    def _on_automatic_backup_started(self) -> None:
        operation_id = self.activity_center.submit(
            operation_kind='automatic_backup',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title='自動バックアップ',
            navigation_policy=NavigationPolicy.BACKGROUNDABLE,
        )
        self._automatic_backup_operation_id = operation_id
        self.activity_center.mark_running(operation_id)

    def _on_automatic_backup_completed(
        self, result: object, error: object
    ) -> None:
        operation_id = getattr(self, '_automatic_backup_operation_id', None)
        if error is not None:
            _LOGGER.warning('automatic backup failed: %s', error)
            if operation_id is not None:
                self.activity_center.fail(
                    operation_id, error_summary=str(error)
                )
            self.shell.statusBar().showMessage(
                '自動バックアップを作成できませんでした'
            )
            return
        if result is None or operation_id is None:
            return
        path = result[0]
        self.activity_center.complete(
            operation_id,
            result_summary=f'自動バックアップを保存しました: {path}',
        )
        self.shell.statusBar().showMessage(
            '自動バックアップを保存しました', 5000
        )

    def _shutdown_automatic_backup(self) -> None:
        if self._automatic_backup_runner is not None:
            self._automatic_backup_runner.shutdown()

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
            "デリバラブルセンター…", self._open_deliverables
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
        # Resolve/mark the target BEFORE closing this window (#919 ordering):
        # if open fails here the user keeps their open project instead of
        # being dropped out of the app with no window left.
        try:
            opened = self.project_library.open_project(entry.project_id)
        except ProjectLibraryError as exc:
            QMessageBox.warning(
                self.shell,
                "プロジェクトを開けません",
                to_user_facing_error(
                    exc, title="プロジェクトを開けませんでした"
                ).notice_text(),
            )
            return
        if not self.shell.close():
            # Close was vetoed by a dirty/running workspace — the opened
            # timestamp already bumped, which is benign.
            return
        self._open_document(opened.document_id)

    def _open_document(self, document_id: str) -> None:
        if self._open_project_callback is not None:
            self._open_project_callback(document_id)
            return
        if self.capture_receiver is not None:
            # The receiver is app-scoped and moves to the new composition —
            # disconnect this shell's announcement first or deliveries would
            # be announced once per still-referenced composition.
            try:
                self.capture_receiver.delivery_staged.disconnect(
                    self._announce_capture_delivery
                )
            except (RuntimeError, TypeError):
                pass
        composition = WorkflowApplicationComposition(
            self.repository,
            document_id,
            project_library=self.project_library,
            open_project=self._open_project_callback,
            capture_receiver=self.capture_receiver,
            preferences=self.preferences,
            safe_mode=self.safe_mode,
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
            QMessageBox.warning(
                self.shell,
                "プロジェクトを作成できません",
                to_user_facing_error(
                    exc, title="プロジェクトを作成できませんでした"
                ).notice_text(),
            )
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
            QMessageBox.warning(
                self.shell,
                "名前を変更できません",
                to_user_facing_error(
                    exc, title="名前を変更できませんでした"
                ).notice_text(),
            )
            return
        self._apply_project_title()
        # The context-bar chip binds once per project switch — re-bind the
        # renamed identity or title and chip split-brain (#919 family).
        self.shell.set_project_identity(self.project_entry.display_name)

    def _project_snapshot_decision(
        self, action_label: str
    ) -> Literal['saved', 'last_saved'] | None:
        """Resolve which persisted state an export/duplicate captures.

        Returns ``'saved'`` (the operator asked to save first and every
        blocked mount actually resolved), ``'last_saved'`` (serialize the
        last persisted state WITHOUT touching the working copy), or
        ``None`` (cancel/unresolvable — no artifact may be created).

        #918/#927: a clone or bundle created while Room workspaces hold
        unsaved edits must never silently mix generations — the operator
        chooses the source generation explicitly BEFORE the artifact is
        written.
        """
        if self.shell.data_mutations_frozen:
            return None
        blocked = [
            mount
            for _workspace_id, mount in self.shell.router.mounts()
            if mount.before_deactivate is not None
            and not mount.before_deactivate()[0]
        ]
        if not blocked:
            return 'saved'
        decision = dirty_state_dialog.choose_snapshot_action(
            action_label, self.shell
        )
        if decision is None:
            return None
        if decision == 'last_saved':
            return 'last_saved'
        # Save path: dirty mounts save directly — the operator already
        # chose "save"; other blocked states (preview, pending import,
        # recovery) still need their own explicit choice via the dialog.
        for mount in blocked:
            state = (
                None if mount.dirty_state is None else mount.dirty_state()
            )
            if state == 'dirty_recoverable' and (
                mount.resolve_dirty_state is not None
            ):
                resolved, _message = mount.resolve_dirty_state('save')
            else:
                resolved = dirty_state_dialog.resolve_mount_dirty_state(
                    mount, 'project_switch', self.shell
                )
            if not resolved:
                return None
            allowed, _reason = mount.before_deactivate()
            if not allowed:
                return None
        return 'saved'

    def _duplicate_project(self) -> None:
        # #918: decide the source generation BEFORE the clone is created.
        decision = self._project_snapshot_decision('複製')
        if decision is None:
            return
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
            QMessageBox.warning(
                self.shell,
                "複製できません",
                to_user_facing_error(
                    exc, title="複製できませんでした"
                ).notice_text(),
            )
            return
        self._switch_to_project(entry)

    def _export_project_bundle(self) -> None:
        """#488: export the open project as a .htdtproject bundle.

        #927: the dirty/running state is resolved BEFORE the bundle is
        written so the serialized project is always one exact generation —
        and the default file name stamps that generation's head revision.
        """

        decision = self._project_snapshot_decision('エクスポート')
        if decision is None:
            return
        head = self.repository.current_head(self.document_id)
        revision_tag = (
            '' if head is None else f'-{head.revision_id[:8]}'
        )
        selected, _filter = file_dialog_memory.get_save_file_name(
            self.shell,
            "プロジェクトのエクスポート先",
            'project.export_bundle',
            f"HTDTプロジェクトバンドル (*{BUNDLE_EXTENSION})",
            suggested_name=(
                f"{self.project_entry.display_name}{revision_tag}"
                f"{BUNDLE_EXTENSION}"
            ),
            default_dir=self._default_export_dir(),
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
                self.shell,
                "エクスポートできません",
                to_user_facing_error(
                    exc, title="エクスポートできませんでした"
                ).notice_text(),
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

        selected, _filter = file_dialog_memory.get_open_file_name(
            self.shell,
            "インポートするプロジェクトバンドル",
            'project.import_bundle',
            f"HTDTプロジェクトバンドル (*{BUNDLE_EXTENSION})",
            default_dir=str(Path.home()),
        )
        if not selected:
            return
        try:
            result = import_project_bundle(self.repository, Path(selected))
        except ProjectBundleError as exc:
            retry = QMessageBox.question(
                self.shell,
                "そのままインポートできません",
                f"{to_user_facing_error(exc, title="インポートできませんでした").notice_text()}\n\nコピーとして新しいプロジェクトを作成しますか？",
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
                    # Authorities with no dedicated surface — their own
                    # timeline row is the focusable record.
                    NavigationTargetKind.OPERATING_PRESET,
                    NavigationTargetKind.HEALTH_BASELINE,
                    NavigationTargetKind.HEALTH_CHECK_PLAN,
                    NavigationTargetKind.AV_SYNC_CONDITION,
                    NavigationTargetKind.PROJECT_NOTE,
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

        # Round-8 record providers: measurements / revisions / variants /
        # inbox items search through the same typed deep-link handoff as
        # entity results (round-7 deferred palette gap).
        def measurement_items() -> tuple:
            if not self.document_id:
                return ()
            records = CadMeasurementRepository(
                self.repository
            ).list_measurements(self.document_id)
            revision = self.repository.latest(self.document_id)
            entity_names = (
                {}
                if revision is None
                else {
                    entity.entity_id: entity.name
                    for entity in revision.document.entities
                }
            )
            items: list[PaletteNavigationItem] = []
            for record in records:
                target_name = entity_names.get(
                    record.measurement_entity_id, record.measurement_entity_id
                )
                items.append(
                    PaletteNavigationItem(
                        item_id=record.measurement_id,
                        title=target_name,
                        subtitle=f'測定 · {record.channel_role}',
                        keywords=(
                            record.measurement_id,
                            record.channel_role,
                            str(record.source_kind),
                            '測定',
                            'measurement',
                            'rew',
                        ),
                        deep_link=WorkspaceDeepLink(
                            WorkspaceId.MEASUREMENT,
                            'quality',
                            entity_id=record.measurement_id,
                            revision_id=record.scene_revision_id,
                            kind=NavigationTargetKind.MEASUREMENT.value,
                        ),
                    )
                )
            return tuple(items)

        def revision_items() -> tuple:
            if not self.document_id:
                return ()
            labels = self.repository.revision_labels(self.document_id)
            items: list[PaletteNavigationItem] = []
            for summary in self.repository.list_revision_summaries(
                self.document_id
            ):
                items.append(
                    PaletteNavigationItem(
                        item_id=summary.revision_id,
                        title=revision_display_label(summary, labels),
                        subtitle='履歴 · 部屋リビジョン',
                        keywords=(
                            summary.revision_id,
                            '履歴',
                            'リビジョン',
                            'revision',
                            'history',
                        ),
                        deep_link=WorkspaceDeepLink(
                            WorkspaceId.ROOM,
                            'history',
                            entity_id=summary.revision_id,
                            revision_id=summary.revision_id,
                            kind=NavigationTargetKind.SCENE_REVISION.value,
                        ),
                    )
                )
            return tuple(items)

        def variant_items() -> tuple:
            if not self.document_id:
                return ()
            variants = CadSystemVariantRepository(
                self.repository
            ).list_variants(self.document_id)
            items: list[PaletteNavigationItem] = []
            for variant in variants:
                items.append(
                    PaletteNavigationItem(
                        item_id=variant.variant_id,
                        title=variant_display_label(variant),
                        subtitle='システム提案 · 最適化',
                        keywords=(
                            variant.variant_id,
                            variant.name,
                            '提案',
                            'バリアント',
                            'variant',
                        ),
                        deep_link=WorkspaceDeepLink(
                            WorkspaceId.OPTIMIZATION,
                            'comparison',
                            entity_id=variant.variant_id,
                            kind=NavigationTargetKind.SYSTEM_VARIANT.value,
                        ),
                    )
                )
            return tuple(items)

        def inbox_items() -> tuple:
            records = CaptureInboxRepository(self.repository).list_items()
            items: list[PaletteNavigationItem] = []
            for record in records:
                items.append(
                    PaletteNavigationItem(
                        item_id=record.inbox_item_id,
                        title=(
                            record.source_detail
                            or record.capture_series_id
                        ),
                        subtitle=(
                            '受信ボックス · '
                            + str(record.disposition)
                        ),
                        keywords=(
                            record.inbox_item_id,
                            record.capture_series_id,
                            str(record.primary_classification),
                            '受信',
                            'inbox',
                            'capture',
                        ),
                        deep_link=WorkspaceDeepLink(
                            ApplicationDestinationId.INBOX,
                            entity_id=record.inbox_item_id,
                            kind=NavigationTargetKind.CAPTURE_INBOX_ITEM.value,
                        ),
                    )
                )
            return tuple(items)

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
                HelpTopicPaletteProvider(
                    self.help_registry,
                    self._open_help_topic,
                    locale=detect_system_locale,
                ),
                NavigationItemPaletteProvider(
                    'measurements', PaletteResultKind.DATA, measurement_items
                ),
                NavigationItemPaletteProvider(
                    'revisions', PaletteResultKind.DATA, revision_items
                ),
                NavigationItemPaletteProvider(
                    'variants', PaletteResultKind.DATA, variant_items
                ),
                NavigationItemPaletteProvider(
                    'inbox-items', PaletteResultKind.DATA, inbox_items
                ),
            ),
            on_deep_link=self._navigate_target,
        )

    def _open_settings_destination(self, destination_id: str) -> bool:
        if destination_id == 'settings.preferences':
            self.settings_dialog.open_preferences()
            return True
        if destination_id == 'settings.capture':
            if self.capture_receiver is None:
                return False
            self.settings_dialog.open_capture_settings()
            return True
        if destination_id != 'settings.data':
            return False
        self.settings_dialog.open_settings()
        return True

    def _announce_capture_delivery(self, record: object) -> None:
        """A paired Capture device staged a delivery into the Inbox (#926)."""
        staging_ref = getattr(record, 'staging_ref', None) or '受信ボックス'
        self.shell.statusBar().showMessage(
            f'Capture デバイスから受信しました → {staging_ref}'
            '（受信ボックスで確認）',
            15000,
        )

    def _open_help_topic(self, topic_id: str) -> bool:
        if topic_id == 'help.shortcuts':
            HelpDialog.shortcuts(self.registry, parent=self.shell).exec()
            return True
        if topic_id == 'help.palette':
            HelpDialog.palette_usage(parent=self.shell).exec()
            return True
        topic = self.help_registry.get(topic_id)
        if topic is None:
            return False
        HelpDialog.topic(
            topic,
            locale=detect_system_locale(),
            parent=self.shell,
        ).exec()
        return True

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
        # #919: resolve the canonical library entry and mark it opened
        # BEFORE unbinding anything — a failed open leaves this
        # composition fully bound to the current project.
        try:
            opened = self._open_project_entry(document_id)
        except ProjectLibraryError as exc:
            return operation_error_message(exc)
        self.shell.dispose_data_workspaces()
        self._unbind_workspace_commands()
        self._bind_project_entry(opened)
        # #775: legacy entries recorded without project identity must never
        # replay inside the new project's namespace.
        self.shell.navigation_history.drop_unscoped_project_entries()
        if not self.shell.navigate(WorkspaceId.OVERVIEW):
            raise RuntimeError('プロジェクト切替後の概要画面を再構築できませんでした')
        return None

    def _open_project_entry(self, document_id: str) -> ProjectLibraryEntry:
        """Resolve the canonical library entry for a document and mark opened."""
        entry = self.project_library.ensure_document_registered(document_id)
        return self.project_library.open_project(entry.project_id)

    def _bind_project_entry(self, entry: ProjectLibraryEntry) -> None:
        """Rebind document + canonical library entry + title + chip (#919).

        Every switch path must leave ``document_id``, ``project_entry``,
        the window title and the context-bar project chip describing the
        SAME project — never a mixture of the old and new bindings.
        """
        self.document_id = entry.document_id
        self.project_entry = entry
        self._apply_project_title()
        self.shell.set_project_identity(entry.display_name)

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

    def _open_project_by_id(self, project_id: str) -> None:
        """Open a project selected by canonical ``project_id`` (#919)."""
        ok, reason = self.establish_navigation_project(project_id)
        if not ok:
            self.shell.statusBar().showMessage(
                reason or 'プロジェクトを開けません'
            )

    def _make_projects(self) -> WorkspaceMount:
        page = ProjectLibraryPage(
            ProjectLibraryService(self.repository),
            current_document_id=lambda: self.document_id,
        )
        page.project_open_requested.connect(self._open_project_by_id)
        page.commission_requested.connect(self._open_commissioning_wizard)
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: projects_focus(page, target),
        )

    def _make_inbox(self) -> WorkspaceMount:
        repository = CaptureInboxRepository(self.repository)
        page = CaptureInboxPage(
            repository.list_items,
            on_navigate=self._navigate_target,
            inspect_item=repository.inspect,
            defer_item=repository.defer,
            reject_item=repository.reject,
            resume_item=repository.resume,
            list_projects=self.project_library.list_projects,
            assign_scope=repository.assign_scope,
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: inbox_focus(page, target),
        )

    def _make_activity(self) -> WorkspaceMount:
        # Cross-workspace project timeline (#772): the service projects
        # revisions, variants, captures, measurements, calibrations,
        # checkpoints, presets, health runs, AV-sync results and notes into
        # one read-only chronological view with deep links back to the
        # owning surface.
        repository = self.repository
        measurement_repository = CadMeasurementRepository(repository)
        variant_repository = CadSystemVariantRepository(repository)
        activity_service = CadProjectActivityService(
            scene_repository=repository,
            variant_repository=variant_repository,
            variant_lifecycle_repository=CadSystemVariantLifecycleRepository(
                scene_repository=repository,
                variant_repository=variant_repository,
            ),
            calibration_repository=CadCalibrationRepository(
                scene_repository=repository,
                system_variant_repository=variant_repository,
                measurement_repository=measurement_repository,
                quality_repository=CadMeasurementQualityRepository(
                    measurement_repository
                ),
            ),
            capture_inbox=CaptureInboxRepository(repository),
            measurement_repository=measurement_repository,
            checkpoint_repository=CadDesignCheckpointRepository(repository),
            preset_repository=CadOperatingPresetRepository(repository),
            health_repository=CadSystemHealthRepository(repository),
            av_sync_repository=CadAVSyncRepository(repository),
            notes_repository=CadProjectActivityNoteRepository(repository),
        )

        def operations() -> tuple:
            return (
                *self.activity_center.active(),
                *reversed(self.activity_center.recent(30)),
            )

        page = ActivityPage(
            lambda limit: list_recent_revisions(self.repository, limit),
            list_operations=operations,
            list_events=lambda limit: activity_service.recent(
                self.document_id, limit=limit
            ),
            open_link=self._open_activity_link,
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: activity_focus(page, target),
        )

    def _open_activity_link(self, uri: str) -> bool:
        try:
            target = navigation_target_from_uri(uri)
        except ValueError:
            return False
        return self.shell.navigate_to_target(target).ok

    def _persist_activity_history(self, operation: object) -> None:
        if getattr(operation, 'state', None) not in TERMINAL_STATES:
            return
        try:
            self.activity_center.persist_history(
                self.data_dir / ACTIVITY_HISTORY_FILENAME
            )
        except OSError:
            pass  # app-local diagnostics only — never block the operation

    def _make_library(self) -> WorkspaceMount:
        service = EquipmentLibraryService(self.repository)
        try:
            library_index = build_reference_library_index(
                self.repository, self.data_dir
            )
        except Exception:  # noqa: BLE001 - hub sections are additive; never block the page
            library_index = None
        page = ReferenceLibraryPage(
            service.definitions, library_index=library_index
        )

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
        page = SupportPage(
            self.data_dir,
            status_provider=(
                self.capture_receiver.status_lines
                if self.capture_receiver is not None
                else None
            ),
            export_diagnostics=self._export_diagnostics_package,
            open_authority_graph=self._open_authority_inspector,
        )

        def focus_target(target: NavigationTarget) -> TargetFocusResult:
            # #766: report focus only when the requested topic resolved.
            topic_id = target.primary_id
            if topic_id is None:
                return TargetFocusResult(focused=True)
            if self._open_help_topic(topic_id):
                return TargetFocusResult(focused=True)
            return TargetFocusResult(
                focused=False,
                message="対象のヘルプトピックは存在しません",
            )

        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=focus_target,
        )

    def _open_authority_inspector(self, parent: QWidget) -> None:
        """Build the live authority projection and open the inspector (#590).

        Read-side only: the graph is rebuilt from the canonical
        repositories at open time, never persisted.
        """
        if not self.document_id:
            return
        head = self.repository.current_head(self.document_id)
        head_map = {
            self.document_id: head.revision_id if head is not None else None
        }
        head_hashes = {
            self.document_id: head.content_hash if head is not None else None
        }
        graph = build_authority_graph(
            [
                scene_revision_authority_source(
                    self.repository.list_revision_summaries(self.document_id),
                    head_by_document=head_map,
                ),
                measurement_authority_source(
                    CadMeasurementRepository(
                        self.repository
                    ).list_measurements(self.document_id),
                    head_content_hash_by_document=head_hashes,
                ),
                system_variant_authority_source(
                    CadSystemVariantRepository(
                        self.repository
                    ).list_variants(self.document_id),
                    head_content_hash_by_document=head_hashes,
                ),
            ]
        )
        dialog = AuthorityInspectorDialog(
            graph, on_deep_link=self._navigate_target, parent=parent
        )
        dialog.exec()

    def _export_diagnostics_package(self, parent) -> str | None:
        """Build the bounded support bundle via DiagnosticPackageBuilder (#604).

        Honors the ``diagnostics.include_project_ids`` preference — project
        ids only enter the archive when the user opted in.
        """
        selected, _filter = file_dialog_memory.get_save_file_name(
            parent,
            "診断パッケージを保存",
            'diagnostics.export_package',
            "ZIP アーカイブ (*.zip)",
            suggested_name=package_filename(),
            default_dir=str(self.data_dir),
        )
        if not selected:
            return None
        if not selected.lower().endswith('.zip'):
            selected += '.zip'
        project_ids = {
            entry.project_id: entry.display_name
            for entry in self.project_library.list_projects()
        }
        failures: dict[str, object] = {}
        history_path = self.data_dir / ACTIVITY_HISTORY_FILENAME
        for operation in (
            *ActivityCenter.load_history(history_path),
            *self.activity_center.failed(50),
        ):
            if operation.state is OperationState.FAILED:
                failures[operation.operation_id] = operation.model_dump(
                    mode='json'
                )
        builder = DiagnosticPackageBuilder(
            self.data_dir,
            health_report=run_health_checks(self.data_dir, owns_lock=True),
            operation_failures=tuple(failures.values()),
            preferences_summary=dict(self.preferences.snapshot().values),
            project_ids=project_ids,
        )
        plan = builder.plan(
            include_project_ids=bool(
                self.preferences.get('diagnostics.include_project_ids')
            )
        )
        result = builder.build(Path(selected), plan)
        return str(result.path)

    def _open_commissioning_wizard(self) -> None:
        """First-run project commissioning wizard (#588).

        #898: the wizard converges the collected intent into the canonical
        ProjectDesignBrief and offers the built-in/user ProjectTemplates as
        a start method — template-instantiated projects land with library
        identity, instantiation provenance and the pending measurement
        pattern, while the merged wizard+template brief is written by the
        wizard on save.
        """
        from .cad_design_brief_repository import CadDesignBriefRepository
        from .cad_project_template import create_project_from_template
        from .cad_project_template_repository import (
            CadProjectTemplateRepository,
        )
        from .commissioning_wizard import CommissioningWizard

        template_repository = CadProjectTemplateRepository(self.repository)
        brief_repository = CadDesignBriefRepository(self.repository)
        template_options = tuple(
            (template.name, template)
            for template in template_repository.list_templates()
        )

        def _start_from_template(template, display_name, document_id):
            return create_project_from_template(
                self.repository,
                template,
                library=ProjectLibrary(self.repository_path),
                display_name=display_name,
                document_id=document_id,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                instantiation_repository=template_repository,
                # The wizard materializes the merged wizard+template brief
                # itself on save — no duplicate brief write here (#898).
                design_brief_repository=None,
            )

        wizard = CommissioningWizard(
            self.repository,
            self.document_id,
            data_dir=self.data_dir,
            overview_service=self._build_overview_service(),
            brief_repository=brief_repository,
            template_options=template_options,
            template_starter=_start_from_template,
            parent=self.shell,
        )
        # Summary "開く" links queue inside the modal and accept() it;
        # navigate only after exec() returns so the destination is never
        # focused behind the still-open wizard (round-7 deferred fix).
        if wizard.exec() == wizard.DialogCode.Accepted:
            self._open_project(wizard.created_document_id)
        for link in wizard.take_pending_navigations():
            self._navigate_target(link)

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
            # Tier-C secondary domain authorities (#887).
            assumption_decision_source=CadAssumptionDecisionRepository(
                self.repository
            ),
            design_decision_source=CadDesignDecisionRepository(self.repository),
            installation_source=CadInstallationContextRepository(
                self.repository, variant_service.equipment_repository
            ),
            commissioning_source=CommissioningPlanRepository(self.data_dir),
            health_source=CadSystemHealthRepository(self.repository),
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

        preferences = getattr(self, "preferences", None)
        if preferences is None:
            preferences = ApplicationPreferenceStore.for_data_dir(
                Path(self.repository.path).parent
            )
            self.preferences = preferences
        bind_inspector_display_length_policy(workspace.inspector, preferences)
        bind_measure_display_length_policy(workspace.measure_panel, preferences)

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
                    or workspace.controller.working.has_preview
                    or workspace.measure_controller.is_active
                    or bool(workspace.controller.view_state.selection),
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
                "room.select.all",
                execute=workspace.select_all,
                availability=lambda: _available(
                    bool(workspace.controller.document.entities),
                    'command.blocked.selection_required',
                ),
            )
            self.registry.bind(
                "room.select.invert",
                execute=workspace.select_invert,
                availability=lambda: _available(
                    bool(workspace.controller.document.entities),
                    'command.blocked.selection_required',
                ),
            )
            self.registry.bind(
                "room.select.none",
                execute=workspace.clear_selection,
                availability=lambda: _available(
                    bool(workspace.controller.view_state.selection),
                    'command.blocked.selection_required',
                ),
            )
            self.registry.bind(
                "room.edit.delete",
                execute=workspace.delete_selection,
                availability=lambda: _available(
                    (
                        workspace.controller.selected_id is not None
                        or (
                            geometry_input.is_active
                            and (
                                geometry_input.selected_vertex_id is not None
                                or geometry_input.selected_edge_index is not None
                            )
                        )
                    )
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
                "room.select.all",
                "room.select.invert",
                "room.select.none",
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
            if target.kind is NavigationTargetKind.SCENE_REVISION:
                if workspace.history_panel.select_revision(target.primary_id):
                    return TargetFocusResult(focused=True)
                return TargetFocusResult(
                    focused=False,
                    message='対象のリビジョンが履歴にありません',
                )
            entity_id = target.primary_id
            if target.kind is NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE:
                # Instances are their own authority; focus the bound scene
                # entity instead of reporting a missing scene item.
                instance = CadInstalledEquipmentRepository(
                    self.repository
                ).get_instance(target.primary_id)
                if (
                    instance is None
                    or instance.document_id != workspace.controller.document_id
                ):
                    return TargetFocusResult(
                        focused=False,
                        message='対象の機器インスタンスがこのプロジェクトに存在しません',
                    )
                if instance.scene_entity_id is None:
                    return TargetFocusResult(
                        focused=False,
                        message='この機器インスタンスは部屋の物体に紐付いていません',
                    )
                entity_id = instance.scene_entity_id
            try:
                workspace.controller.document.entity(entity_id)
            except KeyError:
                return TargetFocusResult(
                    focused=False,
                    message='対象の項目がこのプロジェクトに存在しません',
                )
            workspace.select_entity(entity_id)
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
                NavigationTargetKind.SCENE_REVISION,
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

        def focus_target(target: NavigationTarget) -> TargetFocusResult:
            if target.primary_id is None:
                return TargetFocusResult(focused=True)
            if (
                target.kind is NavigationTargetKind.MEASUREMENT
                and workspace.select_measurement_id(target.primary_id)
            ):
                return TargetFocusResult(focused=True)
            return TargetFocusResult(
                focused=False,
                message='対象の測定が品質一覧にありません',
            )

        mount.focus_target = focus_target
        mount.focus_kinds = frozenset({NavigationTargetKind.MEASUREMENT})
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
            if (
                target.kind is NavigationTargetKind.SYSTEM_VARIANT
                and target.primary_id is not None
            ):
                panel = getattr(
                    workspace, 'system_expansion_compare_panel', None
                )
                selector = getattr(panel, 'selector', None)
                if selector is None or not selector.select_variant(
                    target.primary_id
                ):
                    return TargetFocusResult(
                        focused=False,
                        message='対象の提案が比較一覧にありません',
                    )
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
            # Rebind the canonical entry as well so title/chip cannot keep
            # describing the pre-restore binding (#919).
            self._bind_project_entry(
                self.project_library.ensure_document_registered(previous)
            )
            return
        # Resolve through canonical project identity: registry first, then
        # unregistered live documents (restores from pre-registry builds).
        library = ProjectLibrary(self.repository_path)
        active = library.list_projects()
        if active:
            self._bind_project_entry(
                self.project_library.ensure_document_registered(
                    active[0].document_id
                )
            )
            return
        head_document_ids = [
            entry.document_id
            for entry in ProjectLibraryService(self.repository).list_projects()
        ]
        if head_document_ids:
            self._bind_project_entry(
                self.project_library.ensure_document_registered(
                    head_document_ids[0]
                )
            )
            return
        # The restored generation has no projects at all: route to the
        # Project Library surface for explicit selection — a non-existent
        # document id must never remain the active project.
        self.document_id = ''
        self.project_entry = None
        self._apply_project_title()
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

    def _default_export_dir(self) -> str:
        """First-run base for export dialogs: the ``files.export_dir``
        preference when the operator set one, else the profile home."""
        configured = str(self.preferences.get('files.export_dir') or '')
        return configured or str(Path.home())

    def _export_capture_equipment_catalog(self) -> None:
        """Operator action behind ``equipment.export_capture_catalog``.

        Global read-only export: writes the deterministic HTDT -> HTDT-Capture
        equipment picker snapshot through a normal save-dialog path and
        reports the definition count plus the exact snapshot identity.
        """

        selected, _filter = file_dialog_memory.get_save_file_name(
            self.shell,
            "Capture用機材カタログの保存先",
            'equipment.export_capture_catalog',
            "HTDT機材カタログ (*.json)",
            suggested_name="htdt-equipment-catalog.json",
            default_dir=self._default_export_dir(),
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
        directory = file_dialog_memory.get_existing_directory(
            self.shell,
            "ハンドオフの保存先フォルダ",
            'project.export_handoff',
            default_dir=self._default_export_dir(),
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

    def _open_deliverables(self) -> None:
        """Project Deliverables Center (#900).

        One project-scoped surface for generatable outputs: availability,
        pinned source authorities and missing inputs are computed live by
        DeliverablesCatalogService; generation routes to the existing
        domain commands and input deep-links open the owning workspace.
        """
        from .deliverables_catalog import DeliverablesCatalogService
        from .deliverables_dialog import DeliverablesDialog

        generators = {
            'installation.export_handoff': self._export_installation_handoff,
            'analysis.export_bundle': self._export_analysis_bundle,
            'equipment.export_capture_catalog': (
                self._export_capture_equipment_catalog
            ),
        }
        dialog = DeliverablesDialog(
            DeliverablesCatalogService(
                self.repository,
                self.document_id,
                overview_service=self._build_overview_service(),
            ),
            document_id=self.document_id,
            on_command=lambda command_id: generators.get(
                command_id, lambda: None
            )(),
            on_navigate=self._navigate_target,
            parent=self.shell,
        )
        dialog.exec()

    def _export_analysis_bundle(self) -> None:
        """Operator action behind ``analysis.export_bundle`` (#512).

        Packages the project's persisted measurement datasets and A/B
        comparisons into the deterministic analysis export (CSV/JSON/HTML)
        via the typed series adapters — provenance and historical flags
        are derived from the real authorities, never typed in.
        """

        measurements = CadMeasurementRepository(self.repository)
        records = measurements.list_measurements(self.document_id)
        comparisons: tuple = ()
        metadata: list[AnalysisExportMeta] = []
        try:
            comparisons = tuple(measurements.list_comparisons(self.document_id))
        except Exception as exc:
            # The comparison index validates all-or-nothing; when it fails
            # the export still ships its measurement series and records
            # that the comparisons could not be verified.
            metadata.append(
                AnalysisExportMeta(
                    key='omitted.comparisons',
                    value=str(exc)[:500],
                )
            )
        if not records and not comparisons and not metadata:
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
        omitted_measurements = 0
        for record in records:
            try:
                bundle = measurements.get_evidence_bundle(
                    record.measurement_id
                )
            except ValueError as exc:
                # 'no frequency-response dataset' is the routine no-dataset
                # case; anything else is a failed re-verification — record
                # the omission in the bundle so the file does not claim
                # completeness it does not have.
                if not str(exc).startswith(
                    'measurement has no frequency-response dataset'
                ):
                    omitted_measurements += 1
                    metadata.append(
                        AnalysisExportMeta(
                            key=f'omitted.measurement.{record.measurement_id}',
                            value=str(exc)[:500],
                        )
                    )
                continue
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
            metadata.extend(comparison_metadata_entries(comparison))
        if not series:
            QMessageBox.warning(
                self.shell,
                "解析エクスポート",
                "検証を通った測定・比較データがなく、書き出せる内容がありません。",
            )
            return
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
            metadata=tuple(metadata),
        )
        directory = file_dialog_memory.get_existing_directory(
            self.shell,
            "解析エクスポートの保存先フォルダ",
            'project.export_analysis',
            default_dir=self._default_export_dir(),
        )
        if not directory:
            return
        target = Path(directory)
        # One generation per stem: re-exporting into the same folder
        # never overwrites or mixes with a previous export — a fresh
        # ``analysis-N`` stem is claimed and the three members are
        # published atomically or not at all.
        stem = claim_export_stem(
            target, 'analysis', ('_export.csv', '_export.json', '_report.html')
        )
        written = tuple(
            write_export_files(
                target,
                {
                    f'{stem}_export.csv': render_analysis_csv(export),
                    f'{stem}_export.json': render_analysis_json(export),
                    f'{stem}_report.html': render_analysis_html(export),
                },
            ).values()
        )
        box = QMessageBox(self.shell)
        box.setWindowTitle("解析エクスポートを書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in written)
        )
        details = [f"仕様 SHA-256: {export.spec_sha256}"]
        if omitted_measurements:
            details.append(
                f"{omitted_measurements} 件の測定データは検証に失敗したため"
                "除外しました（ファイル内の metadata に記録されています）"
            )
        if metadata and any(m.key == 'omitted.comparisons' for m in metadata):
            details.append(
                "保存済み比較を検証できなかったため、比較は除外しました"
            )
        box.setDetailedText("\n".join(details))
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
    capture_receiver: CaptureReceiverController | None = None,
    preferences: ApplicationPreferenceStore | None = None,
    safe_mode: bool = False,
) -> WorkflowShellWindow:
    composition = WorkflowApplicationComposition(
        repository,
        document_id,
        project_library=project_library,
        open_project=open_project,
        capture_receiver=capture_receiver,
        preferences=preferences,
        safe_mode=safe_mode,
    )
    return composition.shell


__all__ = ["WorkflowApplicationComposition", "build_workflow_application"]
