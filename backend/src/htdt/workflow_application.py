from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
from threading import Event
from typing import Literal
from uuid import uuid4
import weakref

from PySide6.QtCore import QByteArray, QObject, QPointF, Qt, QTimer, Slot
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
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
    OperationTransitionError,
)
from .capture_inbox import CaptureInboxRepository
from .capture_watch_failures import (
    WATCH_QUEUE_ARRIVAL_SOURCE,
    CaptureWatchFailureQueue,
    WatchFailureClass,
    WatchRetryVerdict,
    verify_watch_retry,
    write_watch_failure_diagnostic,
)
from .cad_av_sync_repository import CadAVSyncRepository
from .cad_calibration_repository import CadCalibrationRepository
from .cad_calibration_wizard import derive_wizard_state
from .cad_calibration_wizard_repository import (
    CadCalibrationWizardRepository,
)
from .cad_channel_verification_repository import (
    CadChannelVerificationRepository,
)
from .cad_commissioning_orchestrator import (
    COMMISSIONING_STAGE_ORDER,
    derive_run_state,
)
from .cad_commissioning_orchestrator_repository import (
    CadCommissioningOrchestratorRepository,
)
from .cad_sweep_acquisition import WasapiAudioBackend
from .cad_calibration_workflow import CadCalibrationWorkflowService
from .cad_correction_qualification import qualification_scope_label
from .cad_correction_qualification_repository import (
    CadCorrectionQualificationRepository,
)
from .cad_design_checkpoint_repository import CadDesignCheckpointRepository
from .cad_operating_preset_repository import CadOperatingPresetRepository
from .cad_project_activity import (
    CadProjectActivityService,
    _event_sort_key,
)
from .cad_project_activity_repository import CadProjectActivityNoteRepository
from .cad_system_variant_lifecycle import CadSystemVariantLifecycleRepository
from .commissioning_plan import CommissioningPlanRepository
from .cad_display_labels import (
    calibration_reason_label,
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
    AnalysisExportBundle,
    AnalysisExportMeta,
    AnalysisSeries,
    build_analysis_export,
    comparison_export_parts,
    render_analysis_csv,
    render_analysis_html,
    render_analysis_json,
    series_from_measurement_dataset,
)
from .cad_repository import SceneRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_search_repository import CadSearchRepository
from .cad_system_variant_repository import CadSystemVariantRepository
from .export_io import write_export_generation
from .availability_reasons import availability_reason
from .accessible_labels import wire_label_buddies
from .command_palette import (
    CommandPaletteController,
    CommandShortcutBinder,
    flush_focused_text_editor,
    focused_text_editor,
)
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
from .automatic_backup import managed_data_fingerprint
from .automatic_backup_runner import AutomaticBackupRunner
from .capture_watch_runner import CaptureWatchRunner
from .storage_watch_runner import StorageWatchRunner
from .data_management_ui import build_data_management_component
from .reference_library_sources import build_reference_library_index
from .reference_library_browser import (
    build_reference_library_detail_resolver,
    collect_usage_sites,
)
from .file_dialog_memory import FileDialogMemoryStore
from .equipment_catalog_export import export_equipment_catalog_snapshot
from .equipment_library import EquipmentLibraryDialog, EquipmentLibraryService
from .installation_handoff import (
    build_installation_handoff,
    handoff_preview_text,
    write_handoff_package,
)
from .installation_output_authority import InstallationReportService
from .help_registry import build_help_registry
from .localization import (
    LanguagePolicy,
    LocalizationService,
    build_workflow_catalog,
    detect_system_locale,
    resolve_locale,
)
from .native_diagnostics import concise_reason, push_uncaught_sink
from .native_worker import WORKER_CANCELLED, NativeWorkerPool
from .launch_intents import build_launch_intent
from .launch_router import route_capture_intent
from .navigation_target import (
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from .project_lifecycle import ProjectLibrary, ProjectNotFoundError
from .project_bundle import (
    BUNDLE_EXTENSION,
    BundleImportConflictError,
    collect_project_bundle,
    export_project_bundle,
    import_project_bundle,
)
from .project_library import ProjectLibraryEntry, ProjectLibraryError
from .project_library_repository import ProjectLibraryRepository
from .overview_readiness import OverviewReadinessService
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
from .rew_api import RewApiClient, validate_rew_api_url
from . import dirty_state_dialog
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .user_facing_error import (
    operation_error_message,
    to_user_facing_error,
    warn_user,
)
from .workspace_dirty_state import (
    DirtyResolutionAction,
    WorkspaceDirtyState,
)
from .system_expansion_workflow import SystemExpansionWorkflowService
from .support_diagnostics import (
    DiagnosticPackageBuilder,
    PackageCategory,
    capture_receiver_probe,
    package_filename,
    rew_api_probe,
    run_health_checks,
    semantic_integrity_check,
    vtk_probe,
)
from .support_health_runner import SupportHealthRunner
from .workflow_help import GlossaryDialog, HelpDialog
from .first_run_wizard import FirstRunWizardDialog
from .first_run_wizard_state import (
    FirstRunWizardFacts,
    WizardStage,
    derive_wizard_progress,
    first_incomplete_stage,
)
from .first_run_wizard_store import (
    FirstRunWizardRecord,
    load_wizard_state,
    save_wizard_state,
)
from .authority_graph import (
    build_authority_graph,
    measurement_authority_source,
    scene_revision_authority_source,
    scene_revision_node_id,
    system_variant_authority_source,
)
from .authority_inspector_ui import AuthorityInspectorDialog
from .workflow_navigation import (
    APPLICATION_DESTINATION_HINTS,
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

_REW_ENDPOINT_PREFERENCE_KEYS = frozenset(
    {'integrations.rew_host', 'integrations.rew_port'}
)


def _storage_bytes_label(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size):,} {unit}"
            return f"{size:,.1f} {unit}"
        size /= 1024.0
    return f"{value:,} B"


# Round 14 (memory/startup): the workspace/page modules are the heavy end
# of the import graph — PyVista/VTK, matplotlib and pyqtgraph come in
# through room_viewport, the measurement and optimization mounts, and the
# application pages pull most of the project-surface stack. They are used
# only inside the lazy WorkspaceMount factories below, so they resolve on
# first attribute access (same _LAZY_EXPORTS pattern as native_cad.py):
# boot pays for the shell alone, and each workspace pays its own import
# the first time the user mounts it. Tests that monkeypatch
# ``workflow_application.<name>`` still work — ``setattr`` lands in the
# module dict before ``__getattr__`` is consulted.
_LAZY_IMPORTS = {
    'ActivityPage': ('.application_pages', 'ActivityPage'),
    'CaptureInboxPage': ('.application_pages', 'CaptureInboxPage'),
    'capture_inbox_item_project_id': (
        '.capture_inbox',
        'capture_inbox_item_project_id',
    ),
    'CaptureEntityPromotionService': (
        '.capture_entity_promotion',
        'CaptureEntityPromotionService',
    ),
    'FieldReturnRepository': (
        '.field_return_ingestion',
        'FieldReturnRepository',
    ),
    'mission_return_reconciliation_lines': (
        '.mission_reconciliation',
        'mission_return_reconciliation_lines',
    ),
    'mission_return_reconciliation_context': (
        '.mission_reconciliation',
        'mission_return_reconciliation_context',
    ),
    'record_return_rebase_decision': (
        '.mission_reconciliation',
        'record_return_rebase_decision',
    ),
    'apply_returned_tasks': (
        '.mission_reconciliation',
        'apply_returned_tasks',
    ),
    'build_mission': ('.capture_mission', 'build_mission'),
    'build_mission_package': (
        '.capture_mission',
        'build_mission_package',
    ),
    'HTDTProjectReference': (
        '.project_identity',
        'HTDTProjectReference',
    ),
    'CaptureReceiverError': (
        '.capture_receiver',
        'CaptureReceiverError',
    ),
    'CaptureSemanticPromotionRepository': (
        '.capture_semantic_promotion',
        'CaptureSemanticPromotionRepository',
    ),
    'AcceptancePage': ('.acceptance_page', 'AcceptancePage'),
    'VerificationWizardPage': (
        '.verification_wizard_page',
        'VerificationWizardPage',
    ),
    'ProjectLibraryPage': ('.application_pages', 'ProjectLibraryPage'),
    'ProjectLibraryService': ('.application_pages', 'ProjectLibraryService'),
    'ReferenceLibraryPage': ('.application_pages', 'ReferenceLibraryPage'),
    'SupportPage': ('.application_pages', 'SupportPage'),
    'activity_focus': ('.application_pages', 'activity_focus'),
    'inbox_focus': ('.application_pages', 'inbox_focus'),
    'list_recent_revisions': ('.application_pages', 'list_recent_revisions'),
    'count_recent_revisions': ('.application_pages', 'count_recent_revisions'),
    'list_revisions_page': (
        '.application_pages',
        'list_revisions_page',
    ),
    'list_known_document_ids': (
        '.application_pages',
        'list_known_document_ids',
    ),
    'projects_focus': ('.application_pages', 'projects_focus'),
    'ApplicabilityEnvelopeDialog': (
        '.applicability_envelope_panel',
        'ApplicabilityEnvelopeDialog',
    ),
    'compose_applicability_envelope': (
        '.cad_applicability_envelope',
        'compose_applicability_envelope',
    ),
    'context_for_document': (
        '.cad_applicability_envelope',
        'context_for_document',
    ),
    'load_envelope_evidence': (
        '.cad_applicability_envelope',
        'load_envelope_evidence',
    ),
    'CredentialVaultDialog': (
        '.credential_vault_panel',
        'CredentialVaultDialog',
    ),
    'CredentialVaultService': (
        '.cad_credential_vault',
        'CredentialVaultService',
    ),
    'CadCredentialVaultRepository': (
        '.cad_credential_vault_repository',
        'CadCredentialVaultRepository',
    ),
    'platform_vault': ('.cad_credential_vault', 'platform_vault'),
    'SolverOutputDiagnosticsDialog': (
        '.solver_output_diagnostics_ui',
        'SolverOutputDiagnosticsDialog',
    ),
    'open_solver_output_ledger': (
        '.solver_output_ledger',
        'open_solver_output_ledger',
    ),
    'build_measurement_workspace_mount': (
        '.measurement_page_workspace',
        'build_measurement_workspace_mount',
    ),
    'MeasurementWorkflowController': (
        '.measurement_workflow',
        'MeasurementWorkflowController',
    ),
    'MeasurementWorkflowError': (
        '.measurement_workflow',
        'MeasurementWorkflowError',
    ),
    'build_optimization_workspace_mount': (
        '.optimization_workflow_workspace',
        'build_optimization_workspace_mount',
    ),
    'OverviewWorkspace': ('.overview_workspace', 'OverviewWorkspace'),
    'PresentationWorkspace': (
        '.presentation_workspace',
        'PresentationWorkspace',
    ),
    'VideoCommissioningWorkspace': (
        '.video_commissioning_workspace',
        'VideoCommissioningWorkspace',
    ),
    'RoomGeometryInputController': (
        '.room_geometry_input',
        'RoomGeometryInputController',
    ),
    'RoomGeometryPanel': ('.room_geometry_panel', 'RoomGeometryPanel'),
    'RoomPredictionController': (
        '.room_prediction',
        'RoomPredictionController',
    ),
    'RoomPredictionPanel': ('.room_prediction', 'RoomPredictionPanel'),
    'PredictionAuthorityLane': (
        '.cad_prediction_registration',
        'PredictionAuthorityLane',
    ),
    'RoomAcousticsTabs': ('.room_acoustics_panel', 'RoomAcousticsTabs'),
    'RoomTreatmentPanel': ('.room_acoustics_panel', 'RoomTreatmentPanel'),
    'RoomSurveyPanel': ('.room_survey_panel', 'RoomSurveyPanel'),
    'SurfaceMaterialPanel': ('.room_acoustics_panel', 'SurfaceMaterialPanel'),
    'ReflectionGuidancePanel': (
        '.reflection_guidance_ui',
        'ReflectionGuidancePanel',
    ),
    'RoomEntityTransformController': (
        '.room_transform_input',
        'RoomEntityTransformController',
    ),
    'RoomViewport3D': ('.room_viewport', 'RoomViewport3D'),
    'RoomWorkspace': ('.room_workspace', 'RoomWorkspace'),
    'SelectionInspector': ('.room_workspace', 'SelectionInspector'),
    'GeometryIntakePanel': (
        '.geometry_intake_panel',
        'GeometryIntakePanel',
    ),
    'GeometryIntakeController': (
        '.geometry_intake_controller',
        'GeometryIntakeController',
    ),
    'IfcDiffReviewPanel': (
        '.ifc_diff_review_panel',
        'IfcDiffReviewPanel',
    ),
}


def __getattr__(name: str):
    entry = _LAZY_IMPORTS.get(name)
    if entry is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module_name, attribute = entry
    from importlib import import_module

    module = import_module(module_name, __package__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted([*globals(), *_LAZY_IMPORTS])


def bind_display_length_policy(
    widget,
    preferences: ApplicationPreferenceStore,
    apply_policy,
) -> None:
    """Subscribe a widget's length-policy setter to preference commits.

    ``display_input.length_unit`` / ``display_input.numeric_precision`` are
    user-local presentation state — canonical storage stays SI metres. The
    subscription re-applies on later commits; the weakref keeps a destroyed
    widget from breaking unrelated preference writes.
    """

    widget_ref = weakref.ref(widget)

    def apply() -> None:
        target = widget_ref()
        if target is None:
            return
        try:
            apply_policy(
                target,
                length_display_policy_from_preferences(preferences),
            )
        except RuntimeError:
            # Qt object already destroyed.
            pass

    def on_change(change: PreferenceChange) -> None:
        if widget_ref() is None:
            # The widget is gone; prune instead of lingering on the
            # app-scoped store forever.
            preferences.unsubscribe(on_change)
            return
        if change.key in _DISPLAY_LENGTH_PREFERENCE_KEYS:
            apply()

    apply()
    preferences.subscribe(on_change)


def bind_length_policy_widget(
    widget,
    preferences: ApplicationPreferenceStore,
) -> None:
    """Bind a ``set_length_policy`` widget — the common #496 shape."""

    bind_display_length_policy(
        widget, preferences, lambda target, policy: target.set_length_policy(policy)
    )


def bind_inspector_display_length_policy(
    inspector: SelectionInspector,
    preferences: ApplicationPreferenceStore,
) -> None:
    """Apply the #496 length display policy to a room inspector, live."""

    bind_display_length_policy(
        inspector,
        preferences,
        lambda target, policy: target.set_display_units(
            length_unit=policy.unit, precision=policy.decimals
        ),
    )


def bind_measure_display_length_policy(
    panel,
    preferences: ApplicationPreferenceStore,
) -> None:
    """Apply the #496 length display policy to the room measure panel, live."""

    bind_length_policy_widget(panel, preferences)


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


@dataclass(frozen=True, slots=True)
class _AnalysisExportPreparation:
    """Worker-produced half of the analysis export (#512).

    ``series``/``metadata`` cross the thread boundary as plain immutable
    data; the UI resumes with the prompts and queues the write job.
    """

    series: tuple[AnalysisSeries, ...]
    metadata: tuple[AnalysisExportMeta, ...]
    omitted_measurements: int


@dataclass(frozen=True, slots=True)
class _AnalysisExportWriteResult:
    """Outcome of the analysis-export write job for the completion dialog."""

    export: AnalysisExportBundle
    written: tuple[Path, ...]
    omitted_measurements: int
    comparisons_failed: bool


def _write_analysis_export(
    target: Path,
    export: AnalysisExportBundle,
    *,
    exclude_members: frozenset[str] = frozenset(),
) -> tuple[Path, ...]:
    """Publish the three members as one ``analysis-N/`` generation dir.

    One generation per stem: re-exporting into the same folder never
    overwrites or mixes with a previous export — a fresh ``analysis-N``
    directory is reserved, staged and published as a whole or not at
    all (see :func:`htdt.export_io.write_export_generation`).
    ``exclude_members`` drops member filenames the export preflight
    withheld (#989).
    """
    generation = write_export_generation(
        target,
        'analysis',
        {
            name: content
            for name, content in {
                'export.csv': render_analysis_csv(export),
                'export.json': render_analysis_json(export),
                'report.html': render_analysis_html(export),
            }.items()
            if name not in exclude_members
        },
        bom_suffixes=('.csv',),
    )
    return generation.members


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
        self._storage_watch_runner: StorageWatchRunner | None = None
        self._capture_watch_runner: CaptureWatchRunner | None = None
        # #1022: bounded manual-reprocess queue for watch drops that
        # exhausted the runner's retry cap — persisted under the data dir
        # so a permanently-failed file keeps a recovery surface across
        # restarts instead of depending on a 15s statusbar line.
        self._watch_failure_queue = CaptureWatchFailureQueue.for_data_dir(
            self.data_dir
        )
        # ApplicationPreferences are app-local truth shared with every
        # integration that reads them — one store per data root (#740).
        self.preferences = preferences or ApplicationPreferenceStore.for_data_dir(
            self.data_dir
        )
        # One presentation-locale service per composition (#624): the stored
        # language policy resolves against the detected system locale. Widget
        # text stays hardcoded Japanese today; the service already owns the
        # surfaces that can render English content (help topics, the retry
        # affordance label).
        self.localization = LocalizationService(
            build_workflow_catalog(),
            policy=self._language_policy(),
            system_locale=detect_system_locale(),
        )
        self.project_library = project_library or ProjectLibraryRepository(
            repository
        )
        self.project_entry = self.project_library.ensure_document_registered(
            document_id
        )
        self._open_project_callback = open_project
        self._spawned_compositions: list[WorkflowApplicationComposition] = []
        # Weak back-reference to the composition that spawned this one during
        # a close+respawn project switch. The chain is flattened on every
        # respawn (see ``_open_document``) so dead intermediate graphs become
        # collectable instead of accumulating one per switch; the weakref
        # keeps the new live composition from pinning its dead parent.
        self._switch_parent: weakref.ReferenceType[
            WorkflowApplicationComposition
        ] | None = None

        self.registry = CommandRegistry()
        register_default_commands(self.registry)
        # One failure surface for every command executor: an exception that
        # would otherwise escape the Qt slot into sys.excepthook is reported
        # as a mapped operator warning instead of only reaching the log.
        # Retryable failure classes (rew.*, transient io.*, authority races)
        # get a 再試行 button that re-executes the same command.
        self.registry.set_error_handler(
            lambda definition, exc: warn_user(
                self.shell,
                f'「{definition.display_name}」',
                exc,
                on_retry=lambda: self.registry.execute(definition.command_id),
                retry_label=self.localization.tr('action.retry'),
            )
        )
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
                WorkspaceId.PRESENTATION: self._make_presentation,
                WorkspaceId.VIDEO: self._make_video,
            },
            # Unmounted destinations resolve capabilities from the
            # registration — declare room's focus kinds so a deep link
            # (e.g. the campaign 3D view) is focusable on the FIRST click,
            # not only after the mount exists (#1006 first-click fix).
            focus_kinds={
                WorkspaceId.ROOM: frozenset({
                    NavigationTargetKind.SCENE_ENTITY,
                    NavigationTargetKind.SCENE_REVISION,
                    NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE,
                    NavigationTargetKind.MEASUREMENT_CAMPAIGN,
                }),
            },
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
            on_help_topic=self._open_help_topic,
        )
        self.shell.command_registry = self.registry  # type: ignore[attr-defined]
        self.shell.command_palette_controller = self.command_palette  # type: ignore[attr-defined]
        # project.save is a GLOBAL command: the workspaces bind their own
        # Ctrl+S inside their widget trees, but Overview/Measurement and
        # the app destinations still need a key path to reach a mounted
        # workspace's dirty document. WindowShortcut yields to their more
        # specific WidgetWithChildren bindings inside those workspaces.
        self._shell_save_binder = CommandShortcutBinder(
            self.shell,
            self.registry,
            command_ids=("project.save",),
            shortcut_context=Qt.ShortcutContext.WindowShortcut,
        )

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
        # Round10: uncaught exceptions also land here as failed
        # pseudo-operations — the status-bar line fades, this record does
        # not. Bounded per session so a crash-looping slot cannot flood the
        # history surface.
        self._uncaught_op_count = 0
        self._release_uncaught_sink = push_uncaught_sink(
            self._record_uncaught_operation
        )
        self.data_management_controller = DataManagementController(
            backend,
            lifecycle,
            parent=self.shell,
            activity_center=self.activity_center,
        )
        # REV19: project-bundle export/import run on this pool — they used
        # to freeze the UI thread for 30s+ on large projects. The busy flag
        # additionally gates close/project-switch through
        # ``_can_close_application``, and a close hook drains the pool.
        self._bundle_busy = False
        self._bundle_import_path: Path | None = None
        # The status-bar line the in-flight bundle job posted; cleared on
        # completion only while it is still the current message, so a
        # finished job never keeps claiming it is running and a fresher
        # notice is never wiped (#REV24-UXFLOW).
        self._bundle_status_message: str | None = None
        self._bundle_pool = NativeWorkerPool(self.shell)
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
        self._preferences_panel = preferences_panel
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
        # The settings dialog is built once outside the router, so its
        # caption labels get their buddies here rather than at mount time.
        wire_label_buddies(self.settings_dialog)
        if capture_receiver is not None:
            capture_receiver.delivery_staged.connect(
                self._announce_capture_delivery
            )
        self.shell.settingsRequested.connect(self.settings_dialog.open_settings)
        self.shell.paletteRequested.connect(self.command_palette.open)
        self.shell.helpRequested.connect(
            lambda: self._open_help_topic('help.shortcuts')
        )
        self.shell.register_close_guard(self._can_close_application)
        self.shell.workflow_application = self  # type: ignore[attr-defined]
        # Window-state persistence: restore geometry + last workspace now
        # (skipped under Safe Mode's restore_saved_layout=False policy),
        # and save on every committed close so project switches that
        # rebuild the shell keep the user's place too.
        self._restore_window_state()
        self.shell.register_close_hook(self._save_window_state)
        self.shell.register_close_hook(self._shutdown_bundle_pool)
        self.shell.register_close_hook(self._shutdown_automatic_backup)
        self.shell.register_close_hook(self._shutdown_background_runners)
        self.shell.register_close_hook(self._account_for_exit_operations)
        self.shell.register_close_hook(self._release_uncaught_sink)
        self.shell.register_close_hook(self._release_preference_watch)
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
        self.registry.bind(
            "calibration.export_settings",
            execute=self._export_calibration_settings,
        )
        self._apply_project_title()
        self._build_project_menu()
        # Mounted workspaces cache their controllers — a mid-session
        # endpoint flip reaches them through this rebind hook instead of
        # needing a remount or restart.
        self.preferences.subscribe(self._on_preference_change)

    def _language_policy(self) -> LanguagePolicy:
        """Stored language choice, guarded: unknown/stale values fall back to
        following the system locale rather than breaking presentation."""
        try:
            return LanguagePolicy(self.preferences.get('general.language'))
        except EXPECTED_OPERATION_ERRORS:
            # Invalid stored value → follow the system locale.
            return LanguagePolicy.SYSTEM_DEFAULT

    def _presentation_locale(self) -> object:
        return resolve_locale(
            self._language_policy(),
            system_locale=detect_system_locale(),
        )

    def _record_uncaught_operation(
        self,
        diagnostics: object,
        exc: BaseException,
        window: object | None,
    ) -> None:
        """Activity-Center record for an uncaught exception (round10).

        The transient status-bar notice still says "look here"; this pseudo-
        operation keeps the event in the persistent operation history with
        the log path and a safe one-line reason. Routed by window so a
        spawned composition's crash lands on its own center; capped per
        session so a crash-looping Qt slot cannot flood the history.
        """
        owner = getattr(window, 'workflow_application', None) or self
        if owner is not self:
            owner._record_uncaught_operation(diagnostics, exc, window)
            return
        if self._uncaught_op_count >= 20:
            return
        self._uncaught_op_count += 1
        try:
            log_path = getattr(diagnostics, 'log_path', None)
            detail = f'{operation_error_message(exc)} ({concise_reason(exc)})'
            if log_path is not None:
                detail += f' — ログ: {log_path}'
            operation_id = self.activity_center.submit(
                operation_kind='uncaught_exception',
                operation_class=OperationClass.COMPUTE,
                title='予期しないエラー',
                domain_payload={'detail': 'GUIスレッドで捕捉されなかった例外'},
            )
            self.activity_center.fail(operation_id, error_summary=detail)
        except Exception:  # error-boundary: reporting — the diagnostics path itself must never raise
            _LOGGER.exception('failed to record uncaught exception')

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

    # -- REW endpoint preference (#740) -------------------------------------

    def _make_rew_client(self) -> RewApiClient:
        """REW API client bound to the persisted endpoint preference."""
        return RewApiClient(self.preferences.rew_api_base_url())

    def _on_preference_change(self, change: PreferenceChange) -> None:
        if change.key in _REW_ENDPOINT_PREFERENCE_KEYS:
            self._apply_rew_endpoint()

    def _apply_rew_endpoint(self) -> None:
        """Rebind mounted REW clients to the persisted endpoint.

        Updating ``base_url`` in place keeps in-flight work untouched and
        picks the new endpoint up on the next request; injected non-
        ``RewApiClient`` test doubles are left alone.
        """
        try:
            base_url = validate_rew_api_url(
                self.preferences.rew_api_base_url()
            )
        except ValueError:
            # Definition constraints make this unreachable; stay fail-closed
            # rather than clobbering a working client with a bad URL.
            return
        for workspace_id in (
            WorkspaceId.MEASUREMENT,
            WorkspaceId.OPTIMIZATION,
        ):
            mount = self.shell.router.mount(workspace_id)
            controller = getattr(
                getattr(mount, 'widget', None), 'controller', None
            )
            client = getattr(controller, 'rew_client', None)
            if isinstance(client, RewApiClient):
                client.base_url = base_url

    def _release_preference_watch(self) -> None:
        """Detach this composition's observers from the app-scoped store.

        The preference store outlives a composition across close+respawn
        project switches — without the release, every dead composition
        stays subscribed and keeps mutating its disposed workspace mounts
        on each live commit, and a raising dead listener would surface as
        ``PreferenceNotificationError`` on an unrelated write.
        """

        self.preferences.unsubscribe(self._on_preference_change)
        self._preferences_panel.release()

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
        except EXPECTED_OPERATION_ERRORS:
            _LOGGER.exception('automatic backup check could not start')

    def _on_automatic_backup_started(self) -> None:
        operation_id = self.activity_center.submit(
            operation_kind='automatic_backup',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title='自動バックアップ',
            input_authority_refs=(
                f'managed-data:{managed_data_fingerprint(self.data_dir)}',
            ),
            navigation_policy=NavigationPolicy.BACKGROUNDABLE,
        )
        self._automatic_backup_operation_id = operation_id
        self.activity_center.mark_running(operation_id)

    def _on_automatic_backup_completed(
        self, result: object, error: object
    ) -> None:
        operation_id = getattr(self, '_automatic_backup_operation_id', None)
        # The op can already be terminal when this queued slot runs.
        if error is not None:
            _LOGGER.warning('automatic backup failed: %s', error)
            if operation_id is not None:
                try:
                    self.activity_center.fail(
                        operation_id,
                        error_summary=operation_error_message(error),
                    )
                except OperationTransitionError:
                    pass
            self.shell.statusBar().showMessage(
                '自動バックアップを作成できませんでした '
                f'· {operation_error_message(error)}'
            )
            return
        if result is None:
            # The runner re-evaluated as not-due after ``backup_started`` (e.g.
            # a manual backup satisfied the interval) — the submitted op still
            # needs its terminal state or it stays RUNNING forever.
            if operation_id is not None:
                try:
                    self.activity_center.complete(
                        operation_id,
                        result_summary='バックアップは不要と再評価されました',
                    )
                except OperationTransitionError:
                    pass
            return
        if operation_id is None:
            return
        path = result[0]
        try:
            self.activity_center.complete(
                operation_id,
                result_summary=f'自動バックアップを保存しました: {path}',
            )
        except OperationTransitionError:
            pass
        self.shell.statusBar().showMessage(
            '自動バックアップを保存しました', 5000
        )

    def _shutdown_automatic_backup(self) -> None:
        if self._automatic_backup_runner is not None:
            self._automatic_backup_runner.shutdown()

    # -- periodic storage integrity scan (REV42) --------------------------

    def start_storage_watch(self) -> None:
        """Start the scheduled read-only inventory scan; no-op in Safe Mode.

        The ``maintenance.storage_watch_enabled`` preference is re-read
        every tick, so toggling it takes effect without a restart and the
        default-on check can be created unconditionally.
        """

        if self.safe_mode:
            return
        if self._storage_watch_runner is None:
            self._storage_watch_runner = StorageWatchRunner(
                self.data_dir, self.preferences, parent=self.shell
            )
            self._storage_watch_runner.integrity_notice.connect(
                self._on_storage_watch_notice
            )
            self._storage_watch_runner.scan_failed.connect(
                self._on_storage_watch_failed
            )
        try:
            self._storage_watch_runner.start()
        except EXPECTED_OPERATION_ERRORS:
            _LOGGER.exception('storage integrity scan could not start')

    def _on_storage_watch_notice(self, report: object) -> None:
        """A reportable scan: missing referenced files or reclaimable bulk."""

        missing = len(getattr(report, 'missing_referenced', ()))
        reclaimable = int(getattr(report, 'reclaimable_bytes', 0))
        parts = []
        if missing:
            parts.append(f'参照切れ {missing} 件')
        if reclaimable:
            parts.append(f'未参照 {_storage_bytes_label(reclaimable)} 回収可能')
        summary = ' / '.join(parts) or 'ストレージに要注意の検出'
        operation_id = self.activity_center.submit(
            operation_kind='storage_integrity_scan',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title='ストレージ定期スキャン',
            input_authority_refs=(
                f'managed-data:{managed_data_fingerprint(self.data_dir)}',
            ),
            navigation_policy=NavigationPolicy.BACKGROUNDABLE,
        )
        self.activity_center.mark_running(operation_id)
        self.activity_center.complete(
            operation_id,
            result_summary=f'{summary} — データ管理で確認してください',
        )
        self.shell.statusBar().showMessage(
            f'ストレージスキャンで検出: {summary} — データ管理で確認', 15000
        )

    def _on_storage_watch_failed(self, message: object) -> None:
        self.shell.statusBar().showMessage(
            f'ストレージの定期スキャンに失敗しました · {message}', 8000
        )

    # -- capture drop-folder watch (REV42) --------------------------------

    def start_capture_watch(self) -> None:
        """Start the opt-in ``.htdtcapture`` drop-folder watch; Safe Mode off.

        The watch path lives in ``integrations.capture_watch_dir`` and is
        re-read every tick — the runner costs nothing while it stays
        empty, so it can start unconditionally.
        """

        if self.safe_mode:
            return
        if self._capture_watch_runner is None:
            self._capture_watch_runner = CaptureWatchRunner(
                self.repository, self.preferences, parent=self.shell
            )
            self._capture_watch_runner.scan_completed.connect(
                self._on_capture_watch_completed
            )
            self._capture_watch_runner.routes_exhausted.connect(
                self._on_capture_watch_exhausted
            )
            self._capture_watch_runner.entries_skipped.connect(
                self._on_capture_watch_skipped
            )
        try:
            self._capture_watch_runner.start()
        except EXPECTED_OPERATION_ERRORS:
            _LOGGER.exception('capture watch folder could not start')

    def _submit_capture_watch_op(self, *, title: str) -> str:
        return self.activity_center.submit(
            operation_kind='capture_watch_stage',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title=title,
            input_authority_refs=(
                f'managed-data:{managed_data_fingerprint(self.data_dir)}',
            ),
            navigation_policy=NavigationPolicy.BACKGROUNDABLE,
            deep_link=WorkspaceDeepLink(ApplicationDestinationId.INBOX),
        )

    def _on_capture_watch_completed(self, results: object) -> None:
        """One scan batch of routed drops — stage notice, never promotion.

        Terminal state is typed-accurate (#1022): an all-staged batch ends
        COMPLETED, an all-failed batch ends FAILED via ``fail()`` — never
        a completed row whose result_summary smuggles the error — and a
        mixed batch splits success/failure into separate operation rows
        because the activity model has no partial terminal state.
        """

        staged = [
            (path, result)
            for path, result, error in results
            if result is not None
            and getattr(result, 'outcome', None) == 'staged_for_review'
        ]
        # Every non-success outcome is a reportable failure — a routing
        # raise (result is None), a rejected bundle ('failed',
        # 'invalid_or_unsupported') or one needing operator help
        # ('user_action_required'). Only 'already_staged' re-arrivals stay
        # quiet: the drop reached the inbox earlier and only bumps its
        # arrival counter. Anything else falling through silently would
        # lose the drop with zero user signal (REV43-SEAMS).
        failed = [
            path
            for path, result, error in results
            if result is None
            or getattr(result, 'outcome', None)
            not in ('staged_for_review', 'already_staged')
        ]
        # A drop that finally staged closes its failure-queue entry —
        # resolved evidence, not a lingering failure row.
        for path, _result in staged:
            self._watch_failure_queue.resolve(path)
        if not staged and not failed:
            # e.g. only already_staged duplicates — nothing to report.
            return
        staged_summary = (
            f'{len(staged)} 件のキャプチャを受信ボックスへステージしました'
        )
        failed_summary = ''
        if failed:
            names = '、'.join(Path(p).name for p in failed[:3])
            failed_summary = (
                f'{len(failed)} 件は取り込めませんでした（{names}）'
            )
        if staged and not failed:
            operation_id = self._submit_capture_watch_op(
                title='キャプチャ監視フォルダー'
            )
            self.activity_center.mark_running(operation_id)
            self.activity_center.complete(
                operation_id,
                result_summary=f'{staged_summary} — 証拠には昇格していません',
            )
            self.shell.statusBar().showMessage(
                f'{staged_summary} — 受信ボックスで確認', 15000
            )
        elif failed and not staged:
            operation_id = self._submit_capture_watch_op(
                title='キャプチャ監視フォルダー'
            )
            self.activity_center.mark_running(operation_id)
            self.activity_center.fail(
                operation_id,
                error_summary=failed_summary,
            )
            self.shell.statusBar().showMessage(
                f'キャプチャ監視フォルダー: {failed_summary} — '
                '失敗キューで再処理できます',
                15000,
            )
        else:
            # Partial batch: one operation row per outcome so the success
            # reads completed and the failure reads failed — a single
            # completed row would lie about the drops that never landed.
            operation_id = self._submit_capture_watch_op(
                title='キャプチャ監視フォルダー'
            )
            self.activity_center.mark_running(operation_id)
            self.activity_center.complete(
                operation_id,
                result_summary=f'{staged_summary} — 証拠には昇格していません',
            )
            failed_id = self._submit_capture_watch_op(
                title='キャプチャ監視フォルダー（失敗）'
            )
            self.activity_center.mark_running(failed_id)
            self.activity_center.fail(
                failed_id,
                error_summary=failed_summary,
            )
            self.shell.statusBar().showMessage(
                f'{staged_summary} / {failed_summary} — '
                '受信ボックスで確認',
                15000,
            )
        self._refresh_inbox_mount()

    def _on_capture_watch_exhausted(self, records: object) -> None:
        """Drops that hit the route cap — persist them for manual reprocess.

        Emitted after ``scan_completed``: the transient line already said
        the drops failed; this adds the durable queue entry and a pointer
        to it. A wedged file keeps its ``_seen`` marker, so without this
        queue it would have no inbox row and no recovery path at all.
        """

        count = 0
        for record in records or ():
            self._watch_failure_queue.record(
                path=record.path,
                error_kind=record.error_kind,
                failure_class=record.failure_class,
                detail=record.detail,
                attempts=record.attempts,
                mtime_ns=record.mtime_ns,
                size=record.size,
                watch_root=record.watch_root,
            )
            count += 1
        if not count:
            return
        self.shell.statusBar().showMessage(
            f'{count} 件を受信ボックスの失敗キューに記録しました — '
            'そこから再処理できます',
            15000,
        )
        self._refresh_inbox_mount()

    def _on_capture_watch_skipped(self, records: object) -> None:
        """Dirents the scan refused — links, reparse points, specials.

        #1019: a skipped entry is not a routable failure — it never
        tried to import — but it still owes the operator a durable,
        honest record, so each one lands in the same bounded failure
        queue as exhausted routes with a ``unsupported`` class (the
        retry gate refuses them: the fix is dropping a real file, not
        re-routing a link). Emitted once per signature per entry; an
        unchanged skip stays quiet.
        """

        count = 0
        for record in records or ():
            self._watch_failure_queue.record(
                path=record.path,
                error_kind=record.error_kind,
                failure_class=WatchFailureClass.UNSUPPORTED,
                detail=record.detail,
                attempts=1,
                mtime_ns=record.mtime_ns,
                size=record.size,
                watch_root=record.watch_root,
            )
            count += 1
        if not count:
            return
        self.shell.statusBar().showMessage(
            f'キャプチャ監視フォルダー: {count} 件をスキップしました'
            '（リンク・特殊ファイルなど）— 失敗キューで理由を確認できます',
            15000,
        )
        self._refresh_inbox_mount()

    def _refresh_inbox_mount(self) -> None:
        # A mounted inbox page must show the new rows immediately — the
        # same refresh its own on_activate performs.
        mount = self.shell.router.mount(ApplicationDestinationId.INBOX)
        page = getattr(mount, 'widget', None) if mount is not None else None
        refresh = getattr(page, 'refresh', None)
        if callable(refresh):
            refresh()

    def _current_watch_root(self) -> str | None:
        """The watch folder currently armed, for the retry epoch check."""

        try:
            raw = str(
                self.preferences.get('integrations.capture_watch_dir') or ''
            ).strip()
        except EXPECTED_OPERATION_ERRORS:
            return None
        return raw or None

    def _verify_watch_failure(self, path: str) -> tuple[object, str]:
        """Live verdict for one queued drop — the page gates on this."""

        entry = self._watch_failure_queue.get(path)
        if entry is None:
            return (
                WatchRetryVerdict.DELETED,
                '失敗キューの記録が見つかりません',
            )
        return verify_watch_retry(
            entry, watch_root_now=self._current_watch_root()
        )

    def _retry_watch_failure(self, path: str) -> None:
        """「安全に再試行」— verified same-file re-route (#1022).

        Path + fingerprint + watch epoch must all still match what the
        queue recorded; a replaced/deleted/mid-write file or a
        reconfigured watch root is refused before the route runs.
        """

        entry = self._watch_failure_queue.get(path)
        if entry is None:
            self.shell.statusBar().showMessage(
                '失敗キューの記録が見つかりません', 8000
            )
            return
        verdict, reason = verify_watch_retry(
            entry, watch_root_now=self._current_watch_root()
        )
        if verdict is not WatchRetryVerdict.OK:
            QMessageBox.warning(self.shell, '安全な再試行', reason)
            return
        self._route_watch_failure_entry(
            entry, task_prefix='capture.watch-queue.retry'
        )

    def _import_watch_failure_file(self, path: str) -> None:
        """「このファイルを選んで取込」— explicit operator import (#1022).

        Deliberately skips the fingerprint/epoch gates: the operator
        asserts the file at the recorded path is what they want now, and
        the route re-validates content itself. Only the file's existence
        is checked — a missing file has nothing to import.
        """

        entry = self._watch_failure_queue.get(path)
        if entry is None:
            self.shell.statusBar().showMessage(
                '失敗キューの記録が見つかりません', 8000
            )
            return
        if not Path(entry.path).is_file():
            QMessageBox.warning(
                self.shell,
                'このファイルを選んで取込',
                'ファイルが見つかりません（削除または移動されました）',
            )
            return
        self._route_watch_failure_entry(
            entry, task_prefix='capture.watch-queue.import'
        )

    def _route_watch_failure_entry(
        self, entry: object, *, task_prefix: str
    ) -> None:
        """Worker-thread re-route for one queued drop.

        Ingest + stage can take a while for a large bundle, so the route
        runs on the bundle pool (same lane as bundle import) — the busy
        flag also gates close the way it does there. The operation row is
        RUNNING while the route is in flight; completion lands its real
        terminal state.
        """

        if self._bundle_busy:
            self.shell.statusBar().showMessage(
                '別のファイル処理が進行中です — 完了後に再試行してください',
                8000,
            )
            return
        operation_id = self.activity_center.submit(
            operation_kind='capture_watch_stage',
            operation_class=OperationClass.DATA_MANAGEMENT,
            title='キャプチャ失敗キュー再取り込み',
            input_authority_refs=(
                f'managed-data:{managed_data_fingerprint(self.data_dir)}',
            ),
            navigation_policy=NavigationPolicy.BACKGROUNDABLE,
            deep_link=WorkspaceDeepLink(ApplicationDestinationId.INBOX),
        )
        self.activity_center.mark_running(operation_id)
        self._begin_bundle_job(
            f'{entry.basename} を取り込んでいます…'
        )
        target = Path(entry.path)

        def job(_cancel: object) -> object:
            route_result = route_capture_intent(
                build_launch_intent(target),
                repository=self.repository,
                arrival_source=WATCH_QUEUE_ARRIVAL_SOURCE,
            )
            return (entry.path, route_result)

        self._bundle_pool.start(
            f'{task_prefix}.{operation_id}',
            job,
            self._bundle_job_completed,
        )

    def _watch_queue_route_done(
        self, task_key: str, result: object, error: object
    ) -> None:
        """Terminal handling for a failure-queue re-route — UI thread."""

        operation_id = task_key.rsplit('.', 1)[-1]
        if error == WORKER_CANCELLED:
            try:
                self.activity_center.confirm_cancelled(operation_id)
            except (OperationTransitionError, KeyError):
                pass
            self.shell.statusBar().showMessage(
                'キャプチャの再取り込みを中止しました', 8000
            )
            return
        if isinstance(error, Exception):
            try:
                self.activity_center.fail(
                    operation_id,
                    error_summary=operation_error_message(error),
                )
            except (OperationTransitionError, KeyError):
                pass
            warn_user(
                self.shell, 'キャプチャを再取り込みできませんでした', error
            )
            return
        path, route_result = result
        outcome = getattr(route_result, 'outcome', None)
        detail = str(getattr(route_result, 'detail', '') or '')
        if outcome in ('staged_for_review', 'already_staged'):
            self._watch_failure_queue.resolve(path)
            try:
                self.activity_center.complete(
                    operation_id,
                    result_summary=detail
                    or '受信ボックスへステージしました',
                )
            except (OperationTransitionError, KeyError):
                pass
            self.shell.statusBar().showMessage(
                '受信ボックスへステージしました — 証拠には昇格していません',
                15000,
            )
        else:
            # An honest miss: the entry stays queued with its attempt
            # count bumped and the route's own reason shown — never
            # auto-repair, never a fake success.
            self._watch_failure_queue.record_retry_attempt(path)
            try:
                self.activity_center.fail(
                    operation_id,
                    error_summary=detail
                    or '再取り込みできませんでした',
                )
            except (OperationTransitionError, KeyError):
                pass
            self.shell.statusBar().showMessage(
                f'再取り込みできませんでした: {detail}' if detail
                else '再取り込みできませんでした — 失敗キューを確認',
                15000,
            )
        self._refresh_inbox_mount()

    def _diagnose_watch_failure(self, path: str) -> None:
        """「サポート診断」— a sanitized note under the diagnostics dir.

        Writes basename/fingerprint/reason plus the entry's stable
        correlation id — never the full path or the raw bundle.
        """

        entry = self._watch_failure_queue.get(path)
        if entry is None:
            self.shell.statusBar().showMessage(
                '失敗キューの記録が見つかりません', 8000
            )
            return
        try:
            note_path = write_watch_failure_diagnostic(
                self.data_dir, entry
            )
        except OSError as exc:
            warn_user(self.shell, 'サポート診断', exc)
            return
        _LOGGER.info(
            'capture watch failure diagnostic note written [diag: %s]',
            entry.diagnostic_id,
        )
        box = QMessageBox(self.shell)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle('サポート診断')
        box.setText(
            '診断メモを保存しました '
            f'[diag: {entry.diagnostic_id}]\n{note_path}\n\n'
            'サポートへ共有する場合は診断パッケージに含めてください。'
        )
        box.exec()

    def _shutdown_background_runners(self) -> None:
        for runner in (
            self._storage_watch_runner,
            self._capture_watch_runner,
        ):
            if runner is not None:
                runner.shutdown()

    def _account_for_exit_operations(self) -> None:
        """Record operations still active at close for next-launch recovery.

        Every close-guarded op (workspaces, data management) is already drained
        or blocked by the guard chain — anything still active here outlived its
        executor's own shutdown budget (e.g. an automatic-backup run that the
        pool cancelled and whose completion is never delivered). ``prepare_shutdown``
        accounts for them honestly: cancellable actives get a cancel request,
        the rest land in the detached/lingering report — the op is never
        fabricated terminal. Persisting then writes ``active_operations`` so
        the next session's recovery surface can say what was running.
        """
        try:
            report = self.activity_center.prepare_shutdown()
        except Exception:  # error-boundary: teardown — shutdown accounting logs and returns, never raises
            _LOGGER.exception('exit-time operation accounting failed')
            return
        if not report.active_at_exit:
            return
        try:
            self.activity_center.persist_history(
                self.data_dir / ACTIVITY_HISTORY_FILENAME
            )
        except OSError:
            pass

    def _build_project_menu(self) -> None:
        menu = self.shell.menuBar().addMenu("プロジェクト(&P)")
        menu.addAction(
            "新規プロジェクト…(&N)", self._new_project
        )
        menu.addAction(
            "プロジェクトを開く…(&O)", self._open_project_dialog
        )
        menu.addSeparator()
        # #886: guided golden-path wizard — reopenable any time; stage
        # progress re-derives from canonical state on open.
        menu.addAction(
            "初回セットアップウィザード…(&W)", self._open_first_run_wizard
        )
        menu.addSeparator()
        menu.addAction(
            "プロジェクト名を変更…(&R)", self._rename_project
        )
        menu.addAction(
            "プロジェクトを複製…(&D)", self._duplicate_project
        )
        menu.addAction(
            "現在のプロジェクトをテンプレートとして保存…(&T)",
            self._save_project_as_template,
        )
        menu.addSeparator()
        menu.addAction(
            "プロジェクトをエクスポート…(&E)", self._export_project_bundle
        )
        menu.addAction(
            "プロジェクトをインポート…(&I)", self._import_project_bundle
        )
        menu.addSeparator()
        menu.addAction(
            "デリバラブルセンター…(&C)", self._open_deliverables
        )
        menu.addSeparator()
        menu.addAction(
            "アーカイブ…(&A)",
            lambda: self._archive_dialog(archived=True),
        )
        menu.addAction(
            "アーカイブ解除…(&U)",
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
        # #986: same search/sort/state vocabulary as the Projects page —
        # rows stay pinned to stable project_id in UserRole; the display
        # name is never the identity.
        from .application_pages import (
            PROJECT_FILTER_ALL,
            PROJECT_FILTER_ARCHIVED,
            PROJECT_SORT_CREATED,
            PROJECT_SORT_NAME,
            PROJECT_SORT_RECENT,
            filter_project_entries,
        )

        search_edit = QLineEdit(dialog)
        search_edit.setPlaceholderText("プロジェクト名で検索…")
        search_edit.setAccessibleName("プロジェクト名で検索")
        search_edit.setToolTip("表示名の部分一致で一覧を絞り込みます")
        layout.addWidget(search_edit)
        controls = QHBoxLayout()
        sort_combo = QComboBox(dialog)
        for _label, _key in (
            ("最近使った順", PROJECT_SORT_RECENT),
            ("作成日時", PROJECT_SORT_CREATED),
            ("名前", PROJECT_SORT_NAME),
        ):
            sort_combo.addItem(_label, _key)
        sort_combo.setToolTip("一覧の並べ替え方法を選びます")
        controls.addWidget(sort_combo)
        state_combo = QComboBox(dialog)
        for _label, _key in (
            ("全件", PROJECT_FILTER_ALL),
            ("アーカイブ済み", PROJECT_FILTER_ARCHIVED),
        ):
            state_combo.addItem(_label, _key)
        state_combo.setToolTip("アーカイブ済みのみ表示します")
        controls.addWidget(state_combo)
        layout.addLayout(controls)
        listing = QListWidget(dialog)

        def _refill() -> None:
            current_id = None
            item = listing.currentItem()
            if item is not None:
                current_id = item.data(Qt.ItemDataRole.UserRole)
            listing.clear()
            restore_row = 0
            for row, entry in enumerate(
                filter_project_entries(
                    entries,
                    text=search_edit.text(),
                    sort=sort_combo.currentData(),
                    state=state_combo.currentData(),
                )
            ):
                item = QListWidgetItem(
                    entry.display_name
                    + ("（アーカイブ済み）" if entry.archived else "")
                )
                item.setData(Qt.ItemDataRole.UserRole, entry.project_id)
                listing.addItem(item)
                if current_id is not None and entry.project_id == current_id:
                    restore_row = row
            if listing.count():
                listing.setCurrentRow(restore_row)

        search_edit.textChanged.connect(lambda *_a: _refill())
        sort_combo.currentIndexChanged.connect(lambda *_a: _refill())
        state_combo.currentIndexChanged.connect(lambda *_a: _refill())
        _refill()
        # Enter/Return or double-click on a row accepts the dialog — the
        # picker's primary gesture, matching the command palette's list.
        listing.itemActivated.connect(lambda *_item: dialog.accept())
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
        # Enter/Return or double-click on a row accepts the pick.
        listing.itemActivated.connect(lambda *_item: dialog.accept())
        layout.addWidget(listing)
        # The prompt is the list's caption; buddy it so the control's
        # accessible name is the question being asked.
        wire_label_buddies(dialog)
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
        # The switch tears this shell down through close(), so the
        # dirty-state prompts must name the real context — a "アプリケー
        # ションの終了" title would mislabel a project switch (#REV18).
        self.shell._deactivation_context = 'project_switch'
        try:
            if not self.shell.close():
                # Close was vetoed by a dirty/running workspace — the opened
                # timestamp already bumped, which is benign.
                return
        finally:
            self.shell._deactivation_context = 'exit'
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
        composition._switch_parent = weakref.ref(self)
        # Each dead hop keeps exactly one successor link, repointed straight
        # at the newest leaf: intermediate compositions lose their only
        # strong upward reference and are freed with their shells instead
        # of chaining dead graphs (activity center, registries, panels)
        # for the rest of the process lifetime.
        self._spawned_compositions = [composition]
        ancestor_ref = self._switch_parent
        while ancestor_ref is not None:
            ancestor = ancestor_ref()
            if ancestor is None:
                break
            ancestor._spawned_compositions = [composition]
            ancestor_ref = ancestor._switch_parent
        # The close hooks shut this composition's backup runner down; the
        # respawned composition needs its own due check or automatic
        # backups silently stop for the rest of the process lifetime.
        if self._automatic_backup_runner is not None:
            composition.start_automatic_backup()
        # Same respawn rule for the REV42 background lanes — a project
        # switch must not silently stop the storage watch or the opt-in
        # capture drop folder for the rest of the process lifetime.
        if self._storage_watch_runner is not None:
            composition.start_storage_watch()
        if self._capture_watch_runner is not None:
            composition.start_capture_watch()
        composition.shell.show()
        composition.shell.raise_()
        composition.shell.activateWindow()
        # This composition is closed for good: free its shell/widget tree
        # instead of retaining one hidden window per project switch. The
        # launch-intent pump only reads the python-level
        # ``workflow_application`` attribute off the dead wrapper, which
        # stays readable after C++ deletion.
        self.shell.deleteLater()

    def live_composition(self) -> 'WorkflowApplicationComposition':
        """The deepest spawned composition — the window the user sees.

        ``_switch_to_project`` chains compositions by closing the old
        shell and spawning a new one, while the launch-intent pump keeps
        addressing the FIRST window it was bound to. Routing must never
        rebind a closed composition to a new project while the visible
        window stays behind.
        """
        composition = self
        while composition._spawned_compositions:
            composition = composition._spawned_compositions[-1]
        return composition

    def _prompt_project_name(
        self, title: str, label: str, *, text: str = ''
    ) -> str | None:
        """Prompt for a project name until a non-empty one or Cancel.

        A blank name used to dead-end the flow on a warning box, forcing
        the operator to reopen the dialog from the menu. Re-prompting
        keeps the entered intent alive; Cancel still aborts.
        """
        while True:
            name, ok = QInputDialog.getText(
                self.shell, title, label, text=text
            )
            if not ok:
                return None
            if name.strip():
                return name.strip()
            QMessageBox.warning(
                self.shell, title, "プロジェクト名を入力してください"
            )

    def _new_project(self) -> None:
        name = self._prompt_project_name("新規プロジェクト", "プロジェクト名:")
        if name is None:
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
            if self.project_entry is None
            or entry.project_id != self.project_entry.project_id
        )
        entry = self._choose_project(
            entries, "プロジェクトを開く", "開くプロジェクト:"
        )
        if entry is not None:
            self._switch_to_project(entry)

    def _require_bound_project(self) -> bool:
        """Menu entries bound to the active project refuse silently when a
        restore left ``project_entry`` unbound — the Projects surface is
        where a new identity is picked, not a crash into excepthook."""
        if self.project_entry is not None:
            return True
        self.shell.statusBar().showMessage("先にプロジェクトを選択してください")
        return False

    def _rename_project(self) -> None:
        if not self._require_bound_project():
            return
        name = self._prompt_project_name(
            "プロジェクト名を変更",
            "新しいプロジェクト名:",
            text=self.project_entry.display_name,
        )
        if name is None:
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
        if not self._require_bound_project():
            return
        # #918: decide the source generation BEFORE the clone is created.
        decision = self._project_snapshot_decision('複製')
        if decision is None:
            return
        name = self._prompt_project_name(
            "プロジェクトを複製",
            "複製後のプロジェクト名:",
            text=f"{self.project_entry.display_name} のコピー",
        )
        if name is None:
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

    def _save_project_as_template(self) -> None:
        """Persist the current project's speaker layout as a user template.

        The persisted head revision is the authority — unsaved edits go
        through the same snapshot decision as duplicate/export, then the
        template pins that exact revision's content hash so a saved
        template can never silently mix generations.
        """
        if not self._require_bound_project():
            return
        decision = self._project_snapshot_decision('テンプレート保存')
        if decision is None:
            return
        revision = self.repository.latest(self.document_id)
        if revision is None:
            QMessageBox.warning(
                self.shell,
                "テンプレートを保存できません",
                "保存済みの部屋データがありません。先に部屋を保存してください。",
            )
            return
        document = revision.document
        if not any(entity.kind == 'speaker' for entity in document.entities):
            QMessageBox.warning(
                self.shell,
                "テンプレートを保存できません",
                "テンプレートに含められるスピーカーがありません。",
            )
            return

        dialog = QDialog(self.shell)
        dialog.setWindowTitle("テンプレートとして保存")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name_edit = QLineEdit(dialog)
        version_edit = QLineEdit('1', dialog)
        reference_combo = QComboBox(dialog)
        reference_combo.addItem(
            "（基準点なし — 役割と名前のみ保存）", None
        )
        for entity in document.entities:
            if entity.kind == 'seat':
                reference_combo.addItem(
                    f"座席を基準: {entity.name or entity.entity_id}",
                    ('seat_listener', entity.entity_id),
                )
            elif entity.kind == 'measurement_point':
                reference_combo.addItem(
                    f"計測点を基準: {entity.name or entity.entity_id}",
                    ('measurement_point', entity.entity_id),
                )
        form.addRow("テンプレート名:", name_edit)
        form.addRow("バージョン:", version_edit)
        form.addRow("レイアウト基準点:", reference_combo)
        layout.addLayout(form)
        hint = QLabel(
            "スピーカーの役割・名前・相対位置だけが保存されます。"
            "測定結果・機器割当・校正データはテンプレートに含まれません。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        title = "テンプレートを保存できません"
        while True:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            name = name_edit.text().strip()
            version = version_edit.text().strip() or '1'
            if name:
                break
            QMessageBox.warning(
                self.shell, title, "テンプレート名を入力してください"
            )

        reference_choice = reference_combo.currentData()
        try:
            from .cad_project_template import (
                build_template_layout_reference,
                save_document_as_template,
            )
            from .cad_project_template_repository import (
                CadProjectTemplateRepository,
                ProjectTemplateConflictError,
            )

            layout_reference = None
            if reference_choice is not None:
                layout_reference = build_template_layout_reference(
                    document,
                    scene_revision_id=revision.revision_id,
                    source_kind=reference_choice[0],
                    source_entity_id=reference_choice[1],
                )
            template = save_document_as_template(
                document,
                template_id=f'user-{uuid4().hex[:12]}',
                version=version,
                name=name,
                layout_reference=layout_reference,
            )
            CadProjectTemplateRepository(self.repository).save_template(
                template
            )
        except (ProjectTemplateConflictError, ValueError) as exc:
            QMessageBox.warning(
                self.shell, title, operation_error_message(exc)
            )
            return
        QMessageBox.information(
            self.shell,
            "テンプレートを保存しました",
            f"テンプレート「{name}」を保存しました。\n"
            "新規プロジェクトウィザードの開始方法から利用できます。",
        )

    def _export_project_bundle(self) -> None:
        """#488: export the open project as a .htdtproject bundle.

        #927: the dirty/running state is resolved BEFORE the bundle is
        written so the serialized project is always one exact generation —
        and the default file name stamps that generation's head revision.

        #989: before any destination is chosen the dependency closure is
        collected and reviewed through the export preflight — per-table/
        per-asset classifications, sizes, risk flags and the send scope
        (完全再現用 / 外部レビュー用). External shares build an allowlist
        ``ExportRedactionManifest``; denied elements are physically
        omitted from the archive and recorded as omissions.
        """
        if not self._require_bound_project() or self._bundle_busy:
            return
        decision = self._project_snapshot_decision('エクスポート')
        if decision is None:
            return
        self._begin_bundle_job("エクスポート内容を検査しています…")
        self._bundle_pool.start(
            "project.bundle.export.preflight",
            lambda _cancel_event: collect_project_bundle(
                self.repository, self.document_id
            ),
            self._bundle_job_completed,
        )

    def _bundle_preflight_done(self, plan, error) -> None:
        """UI half of the bundle preflight (#989): review → manifest → write."""
        from .cad_code_policy_repository import CadCodePolicyRepository
        from .export_preflight import (
            BUNDLE_VERDICT_MEMBER,
            PreflightPlan,
            analyze_bundle_plan,
            build_manifest,
            ensure_export_policy,
            evaluate_elements,
            record_confirmations,
            stored_classification_map,
            verdict_payload,
        )
        from .export_preflight_dialog import ExportPreflightDialog

        if isinstance(error, Exception):
            warn_user(self.shell, "エクスポート内容を検査できませんでした", error)
            return
        if plan is None:
            return
        code_repository = CadCodePolicyRepository(self.repository)
        stored = stored_classification_map(code_repository, self.document_id)
        head = self.repository.current_head(self.document_id)
        preflight = PreflightPlan(
            export_kind='project_bundle',
            document_id=self.document_id,
            source_revision_id=plan.head_revision_id,
            source_sha256=(
                None if head is None else head.content_hash
            ),
            elements=analyze_bundle_plan(plan, stored),
            payload=plan,
        )
        dialog = ExportPreflightDialog(
            preflight,
            title='プロジェクトのエクスポート',
            default_scope='private_archive',
            parent=self.shell,
        )
        if dialog.exec() != ExportPreflightDialog.DialogCode.Accepted:
            return
        scope = dialog.scope()
        # The operator's confirmations become sealed authority before the
        # manifest/eligibility evaluation reads them.
        record_confirmations(code_repository, preflight)
        preflight.policy = ensure_export_policy(
            code_repository, self.document_id
        )
        write_plan = plan
        if scope == 'external_review':
            try:
                build_manifest(
                    code_repository,
                    preflight,
                    bundle_kind='client_package',
                    policy=preflight.policy,
                )
            except ValueError as exc:
                QMessageBox.warning(self.shell, "出力できません", str(exc))
                return
            blockers = evaluate_elements(preflight)
            if blockers:
                QMessageBox.warning(
                    self.shell,
                    "出力できません",
                    "外部送付の条件を満たさない項目があります:\n"
                    + "\n".join(blockers),
                )
                return
            write_plan = plan.apply_exclusions(
                exclude_tables=frozenset(
                    element.element_id[len('table:'):]
                    for element in preflight.excluded()
                    if element.kind == 'table'
                ),
                exclude_asset_digests=frozenset(
                    element.element_id[len('asset:'):]
                    for element in preflight.excluded()
                    if element.kind == 'asset'
                ),
                exclusion_reason=(
                    'export preflight: excluded from external sharing '
                    '(classification/rights unconfirmed or non-exportable)'
                ),
            )
            preflight.payload = write_plan
        # The reviewed plan pins the head at collection time — a mid-flow
        # source change invalidates the review and forces a fresh pass.
        current = self.repository.current_head(self.document_id)
        if (
            write_plan.head_revision_id is not None
            and current is not None
            and current.revision_id != write_plan.head_revision_id
        ):
            QMessageBox.warning(
                self.shell,
                "再検査が必要です",
                "検査中にプロジェクトの内容が変更されました。"
                "最新の内容で再度エクスポート検査を行ってください。",
            )
            return
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
        # The approved verdict travels inside the archive so a receiver
        # can inspect what passed review and what was withheld.
        preflight_verdict = verdict_payload(
            preflight,
            scope=scope,
            destination=selected,
            written_members=list(write_plan.member_names()),
        )
        self._last_export_preflight = (preflight, scope, selected)
        # Runs on the bundle worker pool — a large project's snapshot
        # + zip walk used to freeze the UI thread for tens of seconds
        # (#REV19). The pool relays the completion onto the UI thread, so
        # ``_bundle_job_completed`` — a bound method of this plain
        # composition — runs where the wait cursor and result dialog live.
        self._begin_bundle_job(
            "プロジェクトバンドルをエクスポートしています…"
        )
        self._bundle_pool.start(
            "project.bundle.export",
            lambda _cancel_event: export_project_bundle(
                self.repository,
                self.document_id,
                Path(selected),
                write_plan=write_plan,
                extra_members={
                    BUNDLE_VERDICT_MEMBER: json.dumps(
                        preflight_verdict, ensure_ascii=False, indent=2,
                        sort_keys=True,
                    ).encode('utf-8'),
                },
            ),
            self._bundle_job_completed,
        )

    def _run_export_preflight(
        self,
        preflight,
        *,
        title: str,
        bundle_kind: str,
        default_scope: str,
    ) -> bool:
        """Shared preflight flow for member-file exports (#989).

        Shows the review dialog, persists the operator's confirmed
        classifications + export policy, and for external scope builds
        the allowlist ``ExportRedactionManifest`` and runs the canonical
        eligibility gate. Returns True when the export may proceed —
        ``preflight`` then carries the approved include/exclude marks.
        """
        from .cad_code_policy_repository import CadCodePolicyRepository
        from .export_preflight import (
            build_manifest,
            ensure_export_policy,
            evaluate_elements,
            record_confirmations,
        )
        from .export_preflight_dialog import ExportPreflightDialog

        code_repository = CadCodePolicyRepository(self.repository)
        dialog = ExportPreflightDialog(
            preflight,
            title=title,
            default_scope=default_scope,
            parent=self.shell,
        )
        if dialog.exec() != ExportPreflightDialog.DialogCode.Accepted:
            return False
        scope = dialog.scope()
        record_confirmations(code_repository, preflight)
        preflight.policy = ensure_export_policy(
            code_repository, self.document_id
        )
        if scope == 'external_review':
            try:
                build_manifest(
                    code_repository,
                    preflight,
                    bundle_kind=bundle_kind,
                    policy=preflight.policy,
                )
            except ValueError as exc:
                QMessageBox.warning(self.shell, "出力できません", str(exc))
                return False
            blockers = evaluate_elements(preflight)
            if blockers:
                QMessageBox.warning(
                    self.shell,
                    "出力できません",
                    "外部送付の条件を満たさない項目があります:\n"
                    + "\n".join(blockers),
                )
                return False
        self._last_export_preflight = (preflight, scope)
        return True

    def _export_postcheck(self, preflight_state) -> list[str]:
        """Post-export verification for #989 — inspect + verdict sidecar.

        Returns extra detail lines for the completion dialog. Runs on the
        UI thread after the write job: re-opens the artifact, checks the
        member set against the approved selection and re-scans for
        secret-like content, then saves the verdict JSON beside the
        output. Nothing is transmitted anywhere.
        """
        from .export_preflight import (
            inspect_exported,
            verdict_payload,
            write_verdict_sidecar,
        )

        if not preflight_state:
            return []
        preflight, scope, destination = preflight_state
        lines: list[str] = []
        try:
            payload = preflight.payload
            expected = (
                list(payload.member_names())
                if hasattr(payload, 'member_names')
                else preflight.expected_member_names()
            )
            inspection = inspect_exported(
                Path(destination), expected_members=expected
            )
            payload = verdict_payload(
                preflight,
                scope=scope,
                destination=destination,
                written_members=inspection.get('actual_members', []),
                inspection=inspection,
            )
            sidecar = write_verdict_sidecar(Path(destination), payload)
            verdict = inspection.get('verdict')
            if verdict == 'ok':
                lines.append(
                    '検査: 出力内容は承認済みの選択と一致し、'
                    '機密パターンは検出されませんでした。'
                )
            else:
                lines.append(
                    f'検査: {verdict} — 詳細は検査レコードを確認してください。'
                )
            if preflight.reproducibility() == 'degraded':
                lines.append(
                    '再現性: degraded — 除外項目あり（incomplete）。'
                )
            if preflight.manifest is not None:
                lines.append(
                    f'出力マニフェスト: {preflight.manifest.manifest_id}'
                )
            lines.append(f'検査レコード: {sidecar.name}')
        except Exception as exc:  # inspection must never break the export
            lines.append(f'検査レコードの保存に失敗: {exc}')
        return lines

    def _import_project_bundle(self) -> None:
        """#488: staged import; a document-id collision is offered the
        explicit import-as-copy path (new project identity)."""
        if self._bundle_busy:
            return
        selected, _filter = file_dialog_memory.get_open_file_name(
            self.shell,
            "インポートするプロジェクトバンドル",
            'project.import_bundle',
            f"HTDTプロジェクトバンドル (*{BUNDLE_EXTENSION})",
            default_dir=str(Path.home()),
        )
        if not selected:
            return
        # Offloaded like export (#REV19); the record-identity-conflict
        # retry question is asked in ``_bundle_job_completed``, which the
        # pool delivers on the UI thread after the first attempt fails.
        self._bundle_import_path = Path(selected)
        self._begin_bundle_job(
            "プロジェクトバンドルをインポートしています…"
        )
        self._bundle_pool.start(
            "project.bundle.import",
            lambda _cancel_event: import_project_bundle(
                self.repository, Path(selected)
            ),
            self._bundle_job_completed,
        )

    def _archive_dialog(self, *, archived: bool) -> None:
        candidates = tuple(
            entry
            for entry in self.project_library.list_projects(
                include_archived=True
            )
            if entry.archived != archived
            and (
                self.project_entry is None
                or entry.project_id != self.project_entry.project_id
            )
        )
        title = "プロジェクトをアーカイブ" if archived else "アーカイブ解除"
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
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.PROJECTS],
                factory=self._make_projects,
                focus_kinds=frozenset({NavigationTargetKind.PROJECT}),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.INBOX,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.INBOX],
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.INBOX],
                factory=self._make_inbox,
                focus_kinds=frozenset({
                    NavigationTargetKind.CAPTURE_DELIVERY,
                    NavigationTargetKind.CAPTURE_INBOX_ITEM,
                }),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.ACTIVITY,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.ACTIVITY],
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.ACTIVITY],
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
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.LIBRARY],
                factory=self._make_library,
                focus_kinds=frozenset({NavigationTargetKind.EQUIPMENT_DEFINITION}),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.SUPPORT,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.SUPPORT],
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.SUPPORT],
                factory=self._make_support,
                focus_kinds=frozenset({NavigationTargetKind.HELP_TOPIC}),
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.ACCEPTANCE,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.ACCEPTANCE],
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.ACCEPTANCE],
                factory=self._make_acceptance,
            ),
            WorkspaceRegistration(
                workspace_id=ApplicationDestinationId.VERIFICATION,
                label=APPLICATION_DESTINATION_LABELS[ApplicationDestinationId.VERIFICATION],
                hint=APPLICATION_DESTINATION_HINTS[ApplicationDestinationId.VERIFICATION],
                factory=self._make_verification_wizard,
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
                        subtitle='システムバリアント · 最適化',
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
                    # Capture settings exist only when the receiver service is
                    # live — surfacing the entry without it would be a dead link.
                    tuple(
                        destination
                        for destination in settings_destinations()
                        if destination.destination_id != 'settings.capture'
                        or self.capture_receiver is not None
                    ),
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
                    locale=self._presentation_locale,
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
            f'キャプチャデバイスから受信しました → {staging_ref}'
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
        if topic_id == 'help.glossary':
            # REV32-TERMS: the TermId-registry-driven glossary surface.
            GlossaryDialog(
                self.help_registry,
                locale=self._presentation_locale(),
                parent=self.shell,
            ).exec()
            return True
        topic = self.help_registry.get(topic_id)
        if topic is None:
            return False
        HelpDialog.topic(
            topic,
            locale=self._presentation_locale(),
            command_registry=self.registry,
            help_registry=self.help_registry,
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
        # The switch is committed: persist the outgoing project's live
        # layout under ITS project ref before rebinding — the close hook
        # only fires at shell close, so without this the outgoing
        # project's mid-session window-state changes are dropped and the
        # live context map leaks into the target project.
        self._save_window_state()
        self.shell.dispose_data_workspaces()
        self._unbind_workspace_commands()
        self._bind_project_entry(opened)
        # #775: legacy entries recorded without project identity must never
        # replay inside the new project's namespace.
        self.shell.navigation_history.drop_unscoped_project_entries()
        if not self.shell.navigate(WorkspaceId.OVERVIEW):
            raise RuntimeError('プロジェクト切替後の概要画面を再構築できませんでした')
        # Replay the target project's own persisted layout — the same
        # artifact the close+respawn path restores at composition build.
        self.shell.reset_selected_contexts()
        self._restore_window_state()
        # #886: resume an unfinished wizard after the switch settles
        # (enabled only in the real GUI run — see native_cad._run_gui).
        if self._wizard_auto_show_enabled:
            QTimer.singleShot(0, self._maybe_show_first_run_wizard)
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
        _self = sys.modules[__name__]
        page = _self.ProjectLibraryPage(
            _self.ProjectLibraryService(self.repository),
            current_document_id=lambda: self.document_id,
        )
        page.project_open_requested.connect(self._open_project_by_id)
        page.commission_requested.connect(self._open_commissioning_wizard)
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: _self.projects_focus(page, target),
        )

    def _make_inbox(self) -> WorkspaceMount:
        repository = CaptureInboxRepository(self.repository)
        _self = sys.modules[__name__]

        def list_items(**kwargs):
            # surface ingestions a crash left committed but unstaged
            repository.reconcile_orphaned_ingestions()
            return repository.list_items(**kwargs)

        entity_promotion = _self.CaptureEntityPromotionService(
            repository.ingestion_repository,
            self.repository,
            semantic_promotion_repository=_self.CaptureSemanticPromotionRepository(
                self.repository,
                repository.ingestion_repository,
            ),
        )

        def promote_item(lineage_digest: str, reason: str):
            item = repository.get(lineage_digest)
            document_id = (
                _self.capture_inbox_item_project_id(item)
                if item is not None
                else None
            )
            if document_id is None:
                raise ValueError(
                    'promotion requires a project scope; assign one first'
                )
            inspection = repository.inspect(lineage_digest)
            available = (
                inspection.available_authority_kinds
                if inspection is not None
                else ()
            )
            kinds = tuple(
                kind
                for kind in ('annotations', 'measurements')
                if kind in available
            ) or ('annotations',)
            return repository.promote(
                lineage_digest,
                kinds,
                reason=reason,
                executor=entity_promotion.promotion_executor(document_id),
            )

        page = _self.CaptureInboxPage(
            list_items,
            on_navigate=self._navigate_target,
            inspect_item=repository.inspect,
            defer_item=repository.defer,
            reject_item=repository.reject,
            resume_item=repository.resume,
            promote_item=promote_item,
            list_projects=self.project_library.list_projects,
            assign_scope=repository.assign_scope,
            list_contributions=lambda: _self.FieldReturnRepository(
                self.repository.path
            ).list_staged(),
            reconcile_contribution=lambda contribution: (
                _self.mission_return_reconciliation_lines(
                    contribution, self.repository
                )
            ),
            resolve_return_evidence=lambda contribution: (
                _self.FieldReturnRepository(
                    self.repository.path
                ).resolve_return_refs(contribution.contribution_id)
            ),
            rebase_context=lambda contribution: (
                _self.mission_return_reconciliation_context(
                    contribution, self.repository
                )
            ),
            rebase_record=lambda contribution, task_id, current, reason, by: (
                _self.record_return_rebase_decision(
                    contribution,
                    self.repository,
                    task_id=task_id,
                    current_target_id=current,
                    mapping_reason=reason,
                    decided_by=by,
                )
            ),
            apply_record=lambda contribution, applied_by: (
                _self.apply_returned_tasks(
                    contribution,
                    self.repository,
                    applied_by=applied_by,
                )
            ),
            discard_record=lambda contribution: (
                _self.FieldReturnRepository(
                    self.repository.path
                ).discard_staged(contribution.contribution_id)
            ),
            list_missions=(
                (
                    lambda: self.capture_receiver.service.list_mission_packages()
                )
                if self.capture_receiver is not None
                else None
            ),
            list_mission_pairings=(
                (lambda: self.capture_receiver.service.list_pairings())
                if self.capture_receiver is not None
                else None
            ),
            issue_mission=(
                (
                    lambda entry, purpose, room_name, pairing_id: (
                        self._issue_capture_mission(
                            entry, purpose, room_name, pairing_id
                        )
                    )
                )
                if self.capture_receiver is not None
                else None
            ),
            export_mission=(
                (
                    lambda package_id, destination: (
                        self.capture_receiver.service.export_mission_package(
                            package_id, Path(destination)
                        )
                    )
                )
                if self.capture_receiver is not None
                else None
            ),
            list_watch_failures=(
                lambda: self._watch_failure_queue.entries()
            ),
            verify_watch_failure=self._verify_watch_failure,
            retry_watch_failure=self._retry_watch_failure,
            import_watch_failure=self._import_watch_failure_file,
            diagnose_watch_failure=self._diagnose_watch_failure,
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: _self.inbox_focus(page, target),
        )

    def _issue_capture_mission(
        self,
        entry,
        purpose: str,
        room_name: str,
        pairing_id: str | None,
    ):
        """Build + queue a mission for a library project (pull lane)."""
        _self = sys.modules[__name__]
        if self.capture_receiver is None:
            raise _self.CaptureReceiverError(
                'capture receiver is not configured'
            )
        revision = self.repository.latest(entry.document_id)
        if revision is None:
            raise _self.CaptureReceiverError(
                f'プロジェクト「{entry.display_name}」'
                'に保存済みのシーンがありません'
            )
        project = _self.HTDTProjectReference(
            project_id=entry.project_id,
            document_id=entry.document_id,
            project_name=entry.display_name,
            room_name=room_name,
        )
        mission = _self.build_mission(
            revision.document,
            project=project,
            purpose=purpose,
            room_name=room_name,
            scene_revision_id=revision.revision_id,
        )
        package = _self.build_mission_package(mission)
        return self.capture_receiver.service.queue_mission(
            package, pairing_id=pairing_id
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

        _self = sys.modules[__name__]

        def list_revisions(
            document_id: str | None, limit: int, after: str | None
        ) -> tuple:
            # document_id=None is the page's explicit global-scope request;
            # ``after`` is the opaque keyset cursor from the previous page
            # (#1017 — None reads the newest window).
            return _self.list_revisions_page(
                self.repository,
                limit,
                scope='global' if document_id is None else 'project',
                document_id=document_id,
                after=after,
            )

        def count_revisions(document_id: str | None) -> int:
            return _self.count_recent_revisions(
                self.repository,
                scope='global' if document_id is None else 'project',
                document_id=document_id,
            )

        def list_events(document_id: str | None) -> tuple:
            # The canonical projection stays CadProjectActivityService.events;
            # the global view just merges every document's own projection.
            if document_id is not None:
                return tuple(reversed(activity_service.events(document_id)))
            merged = [
                event
                for doc_id in _self.list_known_document_ids(self.repository)
                for event in activity_service.events(doc_id)
            ]
            merged.sort(
                key=lambda event: _event_sort_key(event.occurred_at_utc),
                reverse=True,
            )
            return tuple(merged)

        project_refs: set[str] = set()
        if self.project_entry is not None:
            project_refs.add(self.project_entry.project_id)
            project_refs.add(self.project_entry.display_name)
        else:
            project_refs.add(self.navigation_project_identity())
        library_names = {
            entry.document_id: entry.display_name
            for entry in _self.ProjectLibraryService(
                self.repository
            ).list_projects()
        }
        page = _self.ActivityPage(
            list_revisions,
            count_revisions=count_revisions,
            list_operations=operations,
            list_events=list_events,
            open_link=self._open_activity_link,
            document_id=self.document_id or None,
            project_refs=project_refs,
            document_label=lambda doc_id: library_names.get(doc_id, doc_id),
        )

        def _queue_refresh(_operation: object) -> None:
            # Listeners fire synchronously on the mutating thread, and
            # refresh is pull-on-activate otherwise — queue it so the ops
            # table tracks op state while the page stays open. A deleted
            # receiver drops its posted calls, so the page's teardown is safe.
            QTimer.singleShot(0, page, page.refresh)

        self.activity_center.subscribe(_queue_refresh)
        # The activity center is composition-scoped and outlives this mount
        # (dispose_mounts on in-place project swaps): drop the listener when
        # the page is destroyed or every remount adds a dead refresh closure
        # that keeps firing stale singleShots and pinning the dead page.
        page.destroyed.connect(
            lambda: self.activity_center.unsubscribe(_queue_refresh)
        )
        return WorkspaceMount.from_widget(
            page,
            on_activate=page.refresh,
            focus_target=lambda target: _self.activity_focus(page, target),
        )

    def _open_activity_link(self, uri: str) -> bool:
        try:
            target = navigation_target_from_uri(uri)
        except ValueError:
            return False
        # #1023: a foreign-project target switches the composition, which
        # disposes the activity page — the very widget emitting the
        # itemActivated signal this call runs inside. Defer the actual
        # navigation one event-loop tick so that teardown never happens
        # mid-signal on the emitting table. The shell (not the page) is the
        # timer's context: it outlives the page, and a dead receiver drops
        # the queued navigation instead of firing into teardown.
        QTimer.singleShot(
            0,
            self.shell,
            lambda: self.shell.navigate_to_target(target),
        )
        return True

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
        except EXPECTED_OPERATION_ERRORS as exc:
            # Hub sections are additive — degrade but log, never silently
            # fake an empty index over a store failure (#815).
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='参考ライブラリ索引の構築')
            library_index = None
        detail_resolver = None
        usage_resolver = None
        open_target = None
        if library_index is not None:
            # #990: read-only projections rebuilt per refresh — the detail
            # factory re-opens the canonical repositories each call so
            # entries added via 管理 never show a stale 記録なし.
            detail_resolver = (
                lambda: build_reference_library_detail_resolver(
                    self.repository, self.data_dir
                )
            )
            usage_resolver = lambda: collect_usage_sites(  # noqa: E731
                self.repository, self.document_id
            )
            open_target = lambda target: QTimer.singleShot(  # noqa: E731
                0,
                self.shell,
                lambda: self.shell.navigate_to_target(target),
            )
        page = sys.modules[__name__].ReferenceLibraryPage(
            service.definitions,
            library_index=library_index,
            detail_resolver=detail_resolver,
            usage_resolver=usage_resolver,
            open_target=open_target,
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

    def _support_health_job_factory(self):
        """Freeze a health-check worker job from UI-thread snapshots (#1018).

        Called on the UI thread inside ``SupportHealthRunner.start`` —
        preferences and the live receiver object are read here, never on
        the worker thread. Everything the job closes over is an immutable
        snapshot taken at dispatch time.
        """
        data_dir = self.data_dir
        rew_url = self.preferences.rew_api_base_url()
        receiver = self.capture_receiver
        if receiver is None:
            receiver_state = {'enabled': False}
        else:
            receiver_state = {
                'enabled': True,
                'running': bool(receiver.running),
                'requested_enabled': bool(receiver.requested_enabled),
                'last_error': receiver.last_error,
            }

        def job(cancel_event) -> object:
            return run_health_checks(
                data_dir,
                integrity_runner=lambda path: semantic_integrity_check(
                    path, is_cancelled=cancel_event.is_set
                ),
                integration_probes=(
                    rew_api_probe(rew_url),
                    capture_receiver_probe(receiver_state),
                    vtk_probe(),
                ),
                # This process owns the data-dir lock while a project is
                # open — same honesty as the diagnostics export path.
                owns_lock=True,
            )

        return job

    def _make_support(self) -> WorkspaceMount:
        def open_activity_workspace(_parent) -> None:
            self.shell.navigate_to_target(
                NavigationTarget(
                    kind=NavigationTargetKind.WORKSPACE,
                    object_ids=(WorkspaceId.ACTIVITY.value,),
                    preferred_destination=WorkspaceId.ACTIVITY,
                )
            )

        page = sys.modules[__name__].SupportPage(
            self.data_dir,
            status_provider=(
                self.capture_receiver.status_lines
                if self.capture_receiver is not None
                else None
            ),
            export_diagnostics=self._export_diagnostics_package,
            open_authority_graph=self._open_authority_inspector,
            open_solver_diagnostics=self._open_solver_diagnostics,
            open_applicability_envelope=self._open_applicability_envelope,
            open_credential_vault=self._open_credential_vault,
            health_runner=SupportHealthRunner(
                self.data_dir,
                self.activity_center,
                self._support_health_job_factory,
            ),
            open_data_management=(
                lambda _w: self.settings_dialog.open_settings()
            ),
            open_preferences=(
                lambda _w: self.settings_dialog.open_preferences()
            ),
            open_capture_settings=(
                lambda _w: self.settings_dialog.open_capture_settings()
            ),
            open_activity=open_activity_workspace,
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

    def _make_acceptance(self) -> WorkspaceMount:
        """REV48 guided acceptance wizard — physical gates as steps."""
        page = sys.modules[__name__].AcceptancePage(
            self.data_dir,
            rew_base_url=self.preferences.rew_api_base_url,
        )
        return WorkspaceMount.from_widget(page, on_activate=page.refresh)

    def _make_verification_wizard(self) -> WorkspaceMount:
        """REV59-GUIDEDWIZ — zero-knowledge issue verification wizard."""
        page = sys.modules[__name__].VerificationWizardPage(
            self.data_dir,
        )
        return WorkspaceMount.from_widget(page, on_activate=page.refresh)

    def _open_authority_inspector(
        self,
        parent: QWidget,
        initial_node_id: str | None = None,
    ) -> None:
        """Build the live authority projection and open the inspector (#590).

        Read-side only: the graph is rebuilt from the canonical
        repositories at open time, never persisted. ``initial_node_id``
        preselects the node the caller asked to inspect (e.g. the scene
        revision a solver-ledger row resolved to).
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
            graph,
            on_deep_link=self._navigate_target,
            initial_node_id=initial_node_id,
            parent=parent,
        )
        dialog.exec()

    def _open_applicability_envelope(self, parent: QWidget) -> None:
        """Compose the seven-dimension applicability envelope and open it
        (#814).

        Read-only: the evidence bundle is loaded from the sealed stores at
        open time, the envelope is a view, never persisted. The context is
        derived from the sealed refs themselves (``context_for_document``)
        — the app layer does not declare a solver identity on the
        document's behalf.
        """
        if not self.document_id:
            return
        _self = sys.modules[__name__]
        bundle = _self.load_envelope_evidence(
            self.repository, document_id=self.document_id
        )
        context = _self.context_for_document(
            bundle, document_id=self.document_id
        )
        envelope = _self.compose_applicability_envelope(
            bundle,
            context=context,
            evaluated_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        dialog = _self.ApplicabilityEnvelopeDialog(envelope, parent=parent)
        dialog.exec()

    def _open_credential_vault(self, parent: QWidget) -> None:
        """Open the credential-vault operator surface for the current
        document (#951).

        Built at open time over the sealed reference/event tables plus
        the live platform vault — metadata-only views; secret material
        never leaves ``SecretMaterial``. The surface lives inside the
        existing サポート destination (no new top-level workspace) and
        is honest about a locked/unavailable vault.
        """
        if not self.document_id:
            return
        _self = sys.modules[__name__]
        service = _self.CredentialVaultService(
            _self.CadCredentialVaultRepository(self.repository),
            _self.platform_vault(),
        )
        dialog = _self.CredentialVaultDialog(
            service, self.document_id, parent=parent,
        )
        dialog.exec()

    # -- first-run wizard (#886) ----------------------------------------

    #: Off by default — enabled by ``native_cad._run_gui`` after the
    #: window is shown so test-booted compositions never auto-open a
    #: modal dialog.
    _wizard_auto_show_enabled = False

    def enable_first_run_wizard_autoshow(self) -> None:
        """Turn on first-run auto-show and schedule the first check."""
        self._wizard_auto_show_enabled = True
        QTimer.singleShot(0, self._maybe_show_first_run_wizard)

    def _first_run_facts(self) -> FirstRunWizardFacts:
        """Assemble the canonical-state snapshot the wizard derives from.

        Every fact reads a sealed authority or the Overview readiness
        aggregation — the wizard itself stores only display resume state.
        """
        _self = sys.modules[__name__]
        if not self.document_id:
            return FirstRunWizardFacts(
                reference_theater_available=(
                    self._reference_theater_available()),
            )
        vm = self._build_overview_service().read(self.document_id)
        step_status = {step.key: step.status for step in vm.golden_path_steps}
        revision = self.repository.current_head(self.document_id)
        speakers = ()
        roles_ok = False
        if revision is not None:
            speakers = tuple(
                entity
                for entity in revision.document.entities
                if entity.kind == 'speaker'
            )
            notice_codes = {
                notice.code for notice in (*vm.blockers, *vm.warnings)
            }
            roles_ok = bool(speakers) and not any(
                code in {'speaker.role_missing', 'speaker.role_duplicate'}
                for code in notice_codes
            )
        equipment_unresolved = sum(
            1
            for notice in (*vm.blockers, *vm.warnings)
            if notice.code == 'equipment.binding_missing'
        )
        backend_available = False
        backend_reason: str | None = None
        try:
            backend = _self.WasapiAudioBackend()
            backend_available = backend.available()
            if not backend_available:
                backend_reason = backend.unavailable_reason()
        except Exception as exc:  # error-boundary: a probe failure reports as unavailable, never crashes the wizard
            backend_reason = operation_error_message(exc)
        calibration_done = False
        channel_verified = False
        readback_verified = False
        try:
            calib_repo = _self.CadCalibrationWizardRepository(
                self.repository
            )
            for run in calib_repo.list_runs(self.document_id):
                state = _self.derive_wizard_state(
                    run, calib_repo.list_transitions(run.run_id)
                )
                if state.completed:
                    calibration_done = True
                    break
            cv_repo = _self.CadChannelVerificationRepository(
                self.repository
            )
            channel_verified = any(
                verdict.map_state == 'verified'
                for verdict in cv_repo.list_verdicts(self.document_id)
            )
            orch_repo = _self.CadCommissioningOrchestratorRepository(
                self.repository
            )
            readback_index = _self.COMMISSIONING_STAGE_ORDER.index(
                'readback_verify'
            )
            for run in orch_repo.list_runs(self.document_id):
                state = _self.derive_run_state(
                    run, orch_repo.list_transitions(run.run_id)
                )
                furthest = _self.COMMISSIONING_STAGE_ORDER.index(
                    state.furthest_stage
                )
                if state.completed or furthest >= readback_index:
                    readback_verified = True
                    break
        except Exception:  # error-boundary: an authority read failure reports the stage as not done — it never fabricates progress
            pass
        return FirstRunWizardFacts(
            project_exists=bool(self.document_id),
            room_saved=step_status.get('room') == 'done',
            speakers_present=bool(speakers),
            speaker_roles_ok=roles_ok,
            equipment_unresolved_count=equipment_unresolved,
            audio_backend_available=backend_available,
            audio_backend_reason=backend_reason,
            calibration_complete=calibration_done,
            channel_verified=channel_verified,
            baseline_measured=step_status.get('measurement') == 'done',
            has_candidates=step_status.get('comparison') == 'done'
            or step_status.get('comparison') == 'current',
            deploy_applied=step_status.get('apply') == 'done',
            deploy_readback_verified=readback_verified,
            verify_measured=step_status.get('verify') == 'done',
            reference_theater_available=(
                self._reference_theater_available()),
        )

    def _reference_theater_available(self) -> bool:
        """#891: the packaged fixture verifies against its manifest pin."""
        try:
            from .cad_reference_theater import (
                reference_theater_available,
            )

            return reference_theater_available()
        except Exception:  # error-boundary: probe failure reads as unavailable
            return False

    def _open_reference_theater_project(self) -> None:
        """#891: materialize the packaged Reference Theater and switch to it.

        Invoked from the wizard's offer button; the fixture materializes
        through the same library path a user-opened project takes.
        """
        try:
            from .cad_reference_theater import (
                materialize_reference_theater,
            )

            result = materialize_reference_theater(
                self.repository, self.project_library)
        except Exception as exc:  # error-boundary: surface message, never crash
            self.shell.statusBar().showMessage(
                operation_error_message(exc))
            return
        reason = self._switch_project(result.document_id)
        if reason is not None:
            self.shell.statusBar().showMessage(reason)

    def _open_first_run_wizard(self) -> None:
        """Open the guided setup wizard; persist resume state on close."""
        dialog = FirstRunWizardDialog(
            self._first_run_facts,
            navigate=self._navigate_target,
            parent=self.shell,
            open_reference_theater=(
                self._open_reference_theater_project),
        )
        dialog.exec()
        self._save_wizard_record(dialog)

    def _maybe_show_first_run_wizard(self) -> None:
        """Auto-open when the wizard never ran or was left unfinished.

        A 'dismissed'/'completed' record suppresses the auto-show — the
        wizard always reopens from the project menu.
        """
        record = load_wizard_state(self.data_dir)
        if record is not None and record.status != 'active':
            return
        views = derive_wizard_progress(self._first_run_facts())
        if first_incomplete_stage(views) is None:
            # Nothing left to guide — record completion so the menu entry
            # is the only way back in.
            self._save_wizard_record(None)
            return
        self._open_first_run_wizard()

    def _save_wizard_record(
        self, dialog: FirstRunWizardDialog | None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record = load_wizard_state(self.data_dir)
        started = record.started_utc if record is not None else now
        views = derive_wizard_progress(self._first_run_facts())
        if first_incomplete_stage(views) is None:
            status = 'completed'
        elif dialog is not None and dialog.deferred:
            status = 'dismissed'
        else:
            status = 'active'
        save_wizard_state(
            self.data_dir,
            FirstRunWizardRecord(
                started_utc=started,
                last_shown_stage=(
                    dialog.current_stage.value
                    if dialog is not None and dialog.current_stage
                    else None
                ),
                status=status,
                updated_utc=now,
            ),
        )

    def _open_solver_diagnostics(self, parent: QWidget) -> None:
        """Open the solver-output ledger view for the current document.

        REV24-SURFACE: read-only projection over the solver stack's
        bound/unbound payload ledger — the same classification
        ``audit_table_modes`` reports. The projection is rebuilt at open
        time and never persisted.
        """
        if not self.document_id:
            return
        _self = sys.modules[__name__]
        ledger = _self.open_solver_output_ledger(
            self.repository.path, self.document_id
        )
        dialog = _self.SolverOutputDiagnosticsDialog(
            ledger,
            self.repository.list_revision_summaries(self.document_id),
            self.repository.revision_labels(self.document_id),
            open_authority_graph=lambda parent_, revision_id: (
                self._open_authority_inspector(
                    parent_, scene_revision_node_id(revision_id)
                )
            ),
            parent=parent,
        )
        dialog.exec()

    def _export_diagnostics_package(self, parent) -> str | None:
        """Build the bounded support bundle via DiagnosticPackageBuilder (#604).

        Honors the ``diagnostics.include_project_ids`` preference — project
        ids only enter the archive when the user opted in. #884: the exact
        export content is previewed (per-member classification, size,
        sha256, redaction counts) BEFORE a destination is chosen; the
        preview is produced by the same staging pass as the export, so the
        two can never disagree.
        """
        from .support_bundle_collectors import (
            collect_audio_context,
            collect_gpu_context,
            collect_release_evidence,
            collect_runtime_context,
            collect_workflow_state,
        )
        from .support_bundle_dialog import SupportBundlePreviewDialog

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
            context_providers={
                PackageCategory.RUNTIME_CONTEXT: collect_runtime_context,
                PackageCategory.GPU_CONTEXT: collect_gpu_context,
                PackageCategory.AUDIO_CONTEXT: collect_audio_context,
                PackageCategory.WORKFLOW_STATE: lambda: collect_workflow_state(
                    current_workspace_id=(
                        self.shell.router.current_workspace_id
                    ),
                ),
                PackageCategory.RELEASE_EVIDENCE: (
                    lambda: collect_release_evidence(self.data_dir)
                ),
            },
        )

        def _plan(include_project_ids: bool):
            return builder.plan(include_project_ids=include_project_ids)

        initial_opt_in = bool(
            self.preferences.get('diagnostics.include_project_ids')
        )
        dialog = SupportBundlePreviewDialog(
            builder.preview(_plan(initial_opt_in)),
            include_project_ids=initial_opt_in,
            project_ids_available=bool(project_ids),
            restage=lambda include: builder.preview(_plan(include)),
            parent=parent,
        )
        if dialog.exec() != SupportBundlePreviewDialog.DialogCode.Accepted:
            return None
        # Persist the operator's opt-in choice so it stays sticky.
        if dialog.include_project_ids != initial_opt_in:
            self.preferences.set(
                'diagnostics.include_project_ids', dialog.include_project_ids
            )

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
        result = builder.build(
            Path(selected), _plan(dialog.include_project_ids)
        )
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
        # Workspaces that own project.save rebind it in their activate()
        # right after this unbind; everywhere else the key routes to any
        # mounted workspace holding a dirty document.
        self.registry.bind(
            "project.save",
            execute=self._save_dirty_workspaces,
            availability=self._dirty_workspace_availability,
        )

    def _mounted_dirty_workspaces(self) -> tuple[QWidget, ...]:
        dirty: list[QWidget] = []
        for destination, mount in self.shell.router.mounts():
            if not isinstance(destination, WorkspaceId):
                continue
            workspace = mount.widget
            controller = getattr(workspace, 'controller', None)
            if controller is None:
                continue
            if focused_text_editor(workspace) is not None:
                dirty.append(workspace)
                continue
            if getattr(controller, 'is_dirty', False) or getattr(
                getattr(controller, 'working', None), 'is_dirty', False
            ):
                dirty.append(workspace)
        return tuple(dirty)

    def _dirty_workspace_availability(self) -> CommandAvailability:
        if self._mounted_dirty_workspaces():
            return CommandAvailability.available()
        return CommandAvailability.blocked(
            availability_reason('project.save.nothing_to_save')
        )

    def _save_dirty_workspaces(self) -> None:
        for workspace in self._mounted_dirty_workspaces():
            save = getattr(workspace, 'save', None)
            if callable(save):
                save()
                continue
            controller = getattr(workspace, 'controller', None)
            controller_save = getattr(controller, 'save', None)
            if callable(controller_save):
                flush_focused_text_editor(workspace)
                controller_save()

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
        _self = sys.modules[__name__]
        page = _self.OverviewWorkspace(
            self._build_overview_service(),
            self.document_id,
            navigate=self._navigate_target,
        )

        def activate() -> None:
            self._unbind_workspace_commands()
            page.refresh()

        return WorkspaceMount.from_widget(page, on_activate=activate)

    def _make_presentation(self) -> WorkspaceMount:
        _self = sys.modules[__name__]
        page = _self.PresentationWorkspace(
            self.repository,
            self.document_id,
            navigate=self._navigate_target,
            # #985: package exports register their worker-lane jobs in the
            # shared ActivityCenter (progress/cancel/history).
            activity_center=self.activity_center,
        )

        def activate() -> None:
            self._unbind_workspace_commands()
            page.refresh()

        return WorkspaceMount.from_widget(
            page,
            on_activate=activate,
            on_context_changed=page.set_context,
        )

    def _make_video(self) -> WorkspaceMount:
        _self = sys.modules[__name__]
        page = _self.VideoCommissioningWorkspace(
            self.repository,
            self.document_id,
            navigate=self._navigate_target,
        )

        def activate() -> None:
            self._unbind_workspace_commands()
            page.refresh()

        return WorkspaceMount.from_widget(
            page,
            on_activate=activate,
            on_context_changed=page.set_context,
        )

    def _make_room(self) -> WorkspaceMount:
        _self = sys.modules[__name__]
        workspace = _self.RoomWorkspace(
            self.repository,
            self.document_id,
            on_navigate=self._navigate_target,
        )
        if not isinstance(workspace.viewport, _self.RoomViewport3D):
            raise TypeError("UX120 Room workspace requires RoomViewport3D")

        preferences = getattr(self, "preferences", None)
        if preferences is None:
            preferences = ApplicationPreferenceStore.for_data_dir(
                Path(self.repository.path).parent
            )
            self.preferences = preferences
        bind_inspector_display_length_policy(workspace.inspector, preferences)
        bind_measure_display_length_policy(workspace.measure_panel, preferences)
        installation_panel = getattr(workspace, 'installation_panel', None)
        if installation_panel is not None:
            bind_length_policy_widget(installation_panel, preferences)
        rack_workspace_panel = getattr(workspace, 'rack_workspace_panel', None)
        if rack_workspace_panel is not None:
            bind_length_policy_widget(rack_workspace_panel, preferences)
        cable_run_panel = getattr(workspace, 'cable_run_panel', None)
        if cable_run_panel is not None:
            bind_length_policy_widget(cable_run_panel, preferences)

        geometry_input = _self.RoomGeometryInputController(workspace, workspace.viewport)
        workspace.attach_geometry_input(geometry_input)
        geometry_panel = _self.RoomGeometryPanel(geometry_input)
        # REV44: the persisted solver-stack lane — provider reads and the
        # management dialog both re-verify through it against the shared
        # content-addressed authority store under the data dir. Created
        # here because the #866 intake chain shares its dispatch
        # repository (same authority resolvers as production dispatch).
        prediction_lane = _self.PredictionAuthorityLane(self.repository)
        # #866 REV70: the intake panel mounts on the same geometry dock
        # page — the controller owns the sealed intake chain + decision
        # persistence; the panel only renders it.
        intake_controller = _self.GeometryIntakeController(
            self.repository,
            self.document_id,
            dispatch_repository=prediction_lane.dispatch_repository,
        )
        intake_panel = _self.GeometryIntakePanel()
        # #981: the diff-review panel mounts alongside the intake panel —
        # re-imported IFC revisions are reconciled here before apply.
        diff_panel = _self.IfcDiffReviewPanel()
        # #1004: the survey panel mounts on the geometry dock — the click
        # target for authority detail (campaign/instrument/calibration,
        # hashes, blocked-task reasons) since overlay actors stay
        # unpickable. Mode combo mirrors the OverlayControls 測量 group.
        survey_panel = _self.RoomSurveyPanel(workspace.survey_overlay)
        geometry_dock = QWidget()
        dock_layout = QVBoxLayout(geometry_dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.addWidget(geometry_panel)
        dock_layout.addWidget(intake_panel)
        dock_layout.addWidget(diff_panel)
        dock_layout.addWidget(survey_panel)
        dock_layout.addStretch(1)
        workspace.bind_survey_overlay(survey_panel)

        def _refresh_geometry_dock() -> None:
            refresh = getattr(geometry_panel, 'refresh', None)
            if callable(refresh):
                refresh()
            workspace._sync_geometry_intake_panel()

        geometry_dock.refresh = _refresh_geometry_dock  # type: ignore[attr-defined]
        workspace.attach_geometry_panel(geometry_dock)
        workspace.bind_geometry_intake(intake_controller, intake_panel)
        workspace.bind_ifc_diff_review(intake_controller, diff_panel)
        bind_length_policy_widget(geometry_panel, preferences)
        bind_length_policy_widget(workspace.video_panel, preferences)
        transform_input = _self.RoomEntityTransformController(workspace, workspace.viewport)
        workspace.attach_transform_input(transform_input)
        # Hard placement constraints (#486): reject drag commits that would
        # introduce a violation, mirroring the legacy dock's blocking gate.
        transform_input.commit_gate = workspace.controller.move_commit_gate
        workspace.optimizeRequested.connect(
            lambda: self.shell.navigate(WorkspaceId.OPTIMIZATION)
        )
        prediction = _self.RoomPredictionController(
            self.repository,
            workspace.controller,
            parent=workspace,
            provider_repository=prediction_lane.provider_repository,
        )
        prediction_panel = _self.RoomPredictionPanel(
            prediction, prediction_lane=prediction_lane
        )
        field_explorer_panel = getattr(
            prediction_panel, 'field_explorer_panel', None
        )
        if field_explorer_panel is not None:
            bind_length_policy_widget(field_explorer_panel, preferences)
            # #999: 音場を3D表示 — panel requests drive the workspace overlay;
            # viewport Esc disarm flows back to the panel checkbox.
            field_explorer_panel.field3DRequested.connect(
                workspace.show_field_overlay_3d
            )
            field_explorer_panel.field3DCleared.connect(
                workspace.clear_field_overlay_3d
            )
            workspace.field3DProbeDisarmed.connect(
                field_explorer_panel.set_3d_probe_off
            )
        material_panel = _self.SurfaceMaterialPanel(workspace.controller)
        treatment_panel = _self.RoomTreatmentPanel(workspace.controller)
        # #1009: the 被覆 section renders the resolved overlay scene —
        # the same authority the viewport draws from.
        treatment_panel.coverage_provider = workspace.treatment_overlay.resolve
        # #1008: the 製作プレビュー dialog arms/clears the read-only
        # fabrication overlay through the workspace facade.
        treatment_panel.fabrication_host = workspace
        # #876/REV36: persisted R150 path artifacts replay into ranked
        # reflection guidance — a read-only dock tab next to prediction.
        guidance_panel = _self.ReflectionGuidancePanel(workspace.controller)
        # REV40: the same replayed view feeds the viewport overlay under
        # the persisted display_input.reflection_guidance_overlay policy.
        workspace.bind_reflection_guidance(guidance_panel, preferences)
        workspace.attach_acoustics_panel(
            _self.RoomAcousticsTabs(
                prediction_panel,
                material_panel,
                treatment_panel,
                guidance_panel,
            )
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

        def resolve_dirty_state(
            action: DirtyResolutionAction,
        ) -> tuple[bool, str | None]:
            if action == 'stop_busy':
                # D1/#REV19: abandon wedged prediction work and continue;
                # detached workers' completions are disconnected inside
                # stop_all so late results can never apply.
                report = prediction.stop()
                if report.all_stopped:
                    return True, '実行中の予測を中止しました'
                return True, '実行中の予測を中止しました · 停止が遅延している処理の結果は適用されません'
            return workspace.resolve_dirty_state(action)

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
            if target.kind is NavigationTargetKind.MEASUREMENT_CAMPAIGN:
                # #1006: 「3Dで測定位置を確認」 — arm the read-only campaign
                # overlay (design id or a selected cell's target entity).
                return workspace.focus_campaign_overlay(target)
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
            resolve_dirty_state=resolve_dirty_state,
            on_context_changed=workspace.set_context,
            on_entity_requested=workspace.select_entity,
            on_close=close,
            focus_kinds=frozenset({
                NavigationTargetKind.SCENE_ENTITY,
                NavigationTargetKind.SCENE_REVISION,
                NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE,
                NavigationTargetKind.MEASUREMENT_CAMPAIGN,
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
        # exec() returns once the popup closes; deleting it then also drops
        # the item shortcuts it carried, so nothing outlives the menu.
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

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
            action = menu.addAction(label)
            if definition.shortcut:
                action.setShortcut(QKeySequence(definition.shortcut))
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
            # Say when the tail hides older edits, and when the bounded
            # history has already evicted its oldest commands — otherwise
            # the menu reads as if the shown slice were the whole history.
            hidden = controller.working.history_length - len(entries)
            dropped = controller.working.history_dropped
            if hidden or dropped:
                parts: list[str] = []
                if hidden:
                    parts.append(f"さらに {hidden} 件")
                if dropped:
                    parts.append(f"履歴上限で最古 {dropped} 件は破棄済み")
                tail = menu.addAction("… " + " · ".join(parts))
                tail.setEnabled(False)
        menu.addSeparator()

        for command_id in command_ids:
            definition = self.registry.definition(command_id)
            availability = self.registry.availability(command_id)
            action = menu.addAction(definition.display_name)
            if definition.shortcut:
                action.setShortcut(QKeySequence(definition.shortcut))
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
        _self = sys.modules[__name__]
        controller = _self.MeasurementWorkflowController(
            self.repository,
            self.document_id,
            rew_client=self._make_rew_client(),
        )
        mount = _self.build_measurement_workspace_mount(
            controller,
            on_navigate=self._navigate_target,
            help_registry=self.help_registry,
            open_help=self._open_help_topic,
            preferences=self.preferences,
            activity_center=self.activity_center,
        )
        workspace = mount.widget
        original_activate = mount.on_activate
        original_deactivate = mount.on_deactivate

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
            # Chain the mount's own deactivation (REV40-REWAUTO: stops the
            # REW automation poll while the page is hidden).
            if original_deactivate is not None:
                original_deactivate()

        def focus_target(target: NavigationTarget) -> TargetFocusResult:
            if target.primary_id is None:
                return TargetFocusResult(focused=True)
            if (
                target.kind is NavigationTargetKind.MEASUREMENT
                and workspace.select_measurement_id(target.primary_id)
            ):
                return TargetFocusResult(focused=True)
            # REV44-HEALTHSYNC: the AV-sync + health record surfaces live on
            # this workspace's quality context — verify the linked record
            # still resolves, then land on the page that hosts it.
            record_kinds = {
                NavigationTargetKind.AV_SYNC_CONDITION: '対象のAV同期条件',
                NavigationTargetKind.HEALTH_BASELINE: '対象のベースライン',
                NavigationTargetKind.HEALTH_CHECK_PLAN: '対象のチェック計画',
            }
            if target.kind in record_kinds:
                if target.kind is NavigationTargetKind.AV_SYNC_CONDITION:
                    exists = (
                        CadAVSyncRepository(self.repository).get_condition(
                            target.primary_id
                        )
                        is not None
                    )
                elif target.kind is NavigationTargetKind.HEALTH_BASELINE:
                    exists = (
                        CadSystemHealthRepository(self.repository).get_baseline(
                            target.primary_id
                        )
                        is not None
                    )
                else:
                    exists = (
                        CadSystemHealthRepository(self.repository).get_plan(
                            target.primary_id
                        )
                        is not None
                    )
                if not exists:
                    return TargetFocusResult(
                        focused=False,
                        message=f'{record_kinds[target.kind]}が記録にありません',
                    )
                workspace.set_context('quality')
                return TargetFocusResult(focused=True)
            return TargetFocusResult(
                focused=False,
                message='対象の測定が品質一覧にありません',
            )

        mount.focus_target = focus_target
        mount.focus_kinds = frozenset(
            {
                NavigationTargetKind.MEASUREMENT,
                NavigationTargetKind.AV_SYNC_CONDITION,
                NavigationTargetKind.HEALTH_BASELINE,
                NavigationTargetKind.HEALTH_CHECK_PLAN,
            }
        )
        mount.on_activate = activate
        mount.on_deactivate = deactivate
        return mount

    @staticmethod
    def _measurement_import_availability(
        controller: MeasurementWorkflowController,
    ) -> CommandAvailability:
        try:
            controller.latest_revision()
        except sys.modules[__name__].MeasurementWorkflowError:
            # Only a genuinely unsaved scene blocks the command — store
            # failures surface via the uncaught diagnostics boundary.
            return CommandAvailability.blocked(
                availability_reason('measurement.import.requires_saved_scene')
            )
        return CommandAvailability.available()

    def _make_optimization(self) -> WorkspaceMount:
        _self = sys.modules[__name__]
        mount = _self.build_optimization_workspace_mount(
            self.repository,
            self.document_id,
            on_navigate=self._navigate_target,
            rew_client=self._make_rew_client(),
        )
        workspace = mount.widget
        controller = workspace.controller  # type: ignore[attr-defined]
        original_activate = mount.on_activate
        original_deactivate = mount.on_deactivate

        # Workspace-scoped shortcuts over the shared registry — the Room
        # mount's CommandShortcutBinder pattern applied to the Optimize
        # workspace. WidgetWithChildrenShortcut scopes Ctrl+S/Z/Y to this
        # widget tree, so the Room binder over the same command ids never
        # conflicts; execute() re-checks live availability on every fire.
        shortcuts = CommandShortcutBinder(
            workspace,
            self.registry,
            command_ids=("project.save", "edit.undo", "edit.redo"),
            shortcut_context=Qt.ShortcutContext.WidgetWithChildrenShortcut,
        )

        def save_with_pending_edits() -> None:
            # project.save is a GLOBAL shortcut: it fires while a form field
            # still owns focus, before editingFinished commits the value —
            # flush it first like the Room mount's commit_pending_editor.
            flush_focused_text_editor(workspace)
            controller.save()

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
                execute=save_with_pending_edits,
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
            shortcuts.refresh()

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
            f'アクティブプロジェクト: {self.project_entry.display_name}'
            if self.project_entry is not None
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
            for entry in sys.modules[__name__].ProjectLibraryService(
                self.repository
            ).list_projects()
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
            "キャプチャ用機材カタログの保存先",
            'equipment.export_capture_catalog',
            "HTDT機材カタログ (*.json)",
            suggested_name="htdt-equipment-catalog.json",
            default_dir=self._default_export_dir(),
        )
        if not selected:
            return
        try:
            result = export_equipment_catalog_snapshot(
                self.repository,
                Path(selected),
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "機材カタログを書き出せませんでした", exc
            )
            return
        box = QMessageBox(self.shell)
        box.setWindowTitle("キャプチャ用機材カタログを書き出しました")
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
        service = InstallationReportService.for_scene_repository(
            self.repository
        )
        try:
            handoff = build_installation_handoff(
                service,
                scene_revision_id=scene_revision_id,
                system_variant_id=system_variant_id,
                generated_at_utc=datetime.now(timezone.utc).isoformat(),
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "設置ハンドオフを作成できませんでした", exc
            )
            return
        # #989: sensitive-data preflight BEFORE the package preview —
        # the operator reviews each member file's classification, rights
        # and risk flags; external shares build an allowlist manifest
        # and withheld members are physically not written.
        from .export_preflight import (
            PreflightPlan,
            analyze_member_files,
            stored_classification_map,
        )
        from .cad_code_policy_repository import CadCodePolicyRepository
        from .installation_handoff import (
            HANDOFF_DIMENSIONS_FILENAME,
            HANDOFF_ENTITIES_FILENAME,
            HANDOFF_REPORT_FILENAME,
            HANDOFF_SETTINGS_FILENAME,
            render_dimension_sheets_csv,
            render_handoff_report_html,
            render_installation_csv,
            render_settings_csv,
        )

        member_contents = {
            'report': render_handoff_report_html(handoff),
            'dimensions': render_dimension_sheets_csv(handoff),
            'settings': render_settings_csv(handoff),
            'entities': render_installation_csv(handoff.output),
        }
        member_filenames = {
            'report': HANDOFF_REPORT_FILENAME,
            'dimensions': HANDOFF_DIMENSIONS_FILENAME,
            'settings': HANDOFF_SETTINGS_FILENAME,
            'entities': HANDOFF_ENTITIES_FILENAME,
        }
        head = self.repository.current_head(self.document_id)
        preflight = PreflightPlan(
            export_kind='installation_handoff',
            document_id=self.document_id,
            source_revision_id=scene_revision_id,
            source_sha256=(None if head is None else head.content_hash),
            elements=analyze_member_files(
                member_contents,
                stored_classification_map(
                    CadCodePolicyRepository(self.repository),
                    self.document_id,
                ),
                filename_map=member_filenames,
            ),
            payload=member_contents,
        )
        if not self._run_export_preflight(
            preflight,
            title='設置ハンドオフ',
            bundle_kind='client_package',
            default_scope='external_review',
        ):
            return
        preview = QDialog(self.shell)
        preview.setWindowTitle("設置ハンドオフプレビュー")
        preview_layout = QVBoxLayout(preview)
        preview_text = QPlainTextEdit(preview)
        preview_text.setReadOnly(True)
        preview_text.setAccessibleName("設置ハンドオフ内容プレビュー")
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
            "ハンドオフの保存先フォルダー",
            'project.export_handoff',
            default_dir=self._default_export_dir(),
        )
        if not directory:
            return
        excluded_keys = frozenset(
            element.element_id[len('member:'):]
            for element in preflight.excluded()
            if element.kind == 'member'
        )
        try:
            outputs = write_handoff_package(
                handoff, directory, exclude_members=excluded_keys
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "設置ハンドオフを書き出せませんでした", exc
            )
            return
        self._last_export_preflight = (*self._last_export_preflight, directory)
        box = QMessageBox(self.shell)
        box.setWindowTitle("設置ハンドオフを書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in outputs.values())
        )
        box.setDetailedText("\n".join(
            self._export_postcheck(self._last_export_preflight)
        ))
        self._last_export_preflight = None
        box.exec()

    def _open_deliverables(self) -> None:
        """Project Deliverables Center (#900).

        One project-scoped surface for generatable outputs: availability,
        pinned source authorities and missing inputs are computed live by
        DeliverablesCatalogService; generation routes to the existing
        domain commands and input deep-links open the owning workspace.
        """
        if not self._require_bound_project():
            return
        from .deliverables_catalog import DeliverablesCatalogService
        from .deliverables_dialog import DeliverablesDialog

        generators = {
            'installation.export_handoff': self._export_installation_handoff,
            'analysis.export_bundle': self._export_analysis_bundle,
            'equipment.export_capture_catalog': (
                self._export_capture_equipment_catalog
            ),
            'calibration.export_settings': self._export_calibration_settings,
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

    def _calibration_workflow_service(self) -> CadCalibrationWorkflowService:
        """The lifecycle facade over the persisted calibration authority."""
        measurements = CadMeasurementRepository(self.repository)
        return CadCalibrationWorkflowService(
            CadCalibrationRepository(
                scene_repository=self.repository,
                system_variant_repository=CadSystemVariantRepository(
                    self.repository
                ),
                measurement_repository=measurements,
                quality_repository=CadMeasurementQualityRepository(
                    measurements
                ),
            )
        )

    def _correction_qualification_scopes(self) -> dict[str, str]:
        """{plan_id: JA scope label} for plans with current qualification.

        Only records sealed against the plan's exact semantic hash are
        shown — a re-planned correction reads 未修飾 until it is qualified
        again (#568).
        """

        try:
            repository = CadCorrectionQualificationRepository(self.repository)
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='訂正修飾の読み込み')
            return {}
        labels: dict[str, str] = {}
        try:
            plans = CadCalibrationRepository(
                scene_repository=self.repository,
                system_variant_repository=CadSystemVariantRepository(
                    self.repository
                ),
                measurement_repository=CadMeasurementRepository(
                    self.repository
                ),
                quality_repository=CadMeasurementQualityRepository(
                    CadMeasurementRepository(self.repository)
                ),
            ).list_plans(self.document_id)
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='校正プランの読み込み')
            return {}
        for plan in plans:
            record = repository.current_for_correction(
                self.document_id, plan.plan_id, plan.plan_semantic_sha256
            )
            if record is not None:
                labels[plan.plan_id] = qualification_scope_label(record.scope)
        return labels

    def _export_calibration_settings(self) -> None:
        """Operator action behind ``calibration.export_settings``.

        Writes the deterministic generic-biquad settings (JSON + CSV) for a
        SUPPORTED calibration plan through a normal save-directory pick.
        Exporting is never applying — the workflow records the 'exported'
        lifecycle fact and ``mark_user_applied`` stays a separate act.
        """

        service = self._calibration_workflow_service()
        try:
            plans = service.repository.list_plans(self.document_id)
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "校正プランを読み込めませんでした", exc
            )
            return
        if not plans:
            QMessageBox.warning(
                self.shell,
                "校正設定の書き出し",
                "書き出せる校正プランがありません。",
            )
            return
        supported = [
            plan for plan in plans if plan.support_state == 'SUPPORTED'
        ]
        if not supported:
            reasons = sorted(
                {
                    reason
                    for plan in plans
                    for reason in plan.unsupported_reasons
                }
            )
            QMessageBox.warning(
                self.shell,
                "校正設定の書き出し",
                "校正プランはありますが未対応のため書き出せません:\n"
                + "\n".join(
                    f"・{calibration_reason_label(reason)}"
                    for reason in reasons
                ),
            )
            return
        qualification_scopes = self._correction_qualification_scopes()
        plan_id = self._pick_one(
            "校正設定の書き出し",
            "書き出す校正プランを選択してください",
            [
                (
                    f'{plan.plan_id} · {plan.sample_rate_hz} Hz · '
                    f'{plan.created_at_utc} · 修飾状態: '
                    f'{qualification_scopes.get(plan.plan_id, "未修飾")}',
                    plan.plan_id,
                )
                for plan in supported
            ],
            selected_row=len(supported) - 1,
        )
        if plan_id is None:
            return
        try:
            result = service.export_settings(
                plan_id,
                created_at_utc=datetime.now(timezone.utc).isoformat(),
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "校正設定を書き出せませんでした", exc
            )
            return
        directory = file_dialog_memory.get_existing_directory(
            self.shell,
            "校正設定の保存先フォルダー",
            'calibration.export_settings',
            default_dir=self._default_export_dir(),
        )
        if not directory:
            return
        target = Path(directory)
        try:
            written = write_export_generation(
                target,
                'calibration',
                {
                    'settings.json': result.json_text,
                    'settings.csv': result.csv_text,
                },
                bom_suffixes=('.csv',),
                manifest_extra={'export_id': result.export.export_id},
            ).members
        except EXPECTED_OPERATION_ERRORS as exc:
            warn_user(
                self.shell, "校正設定を書き出せませんでした", exc
            )
            return
        box = QMessageBox(self.shell)
        box.setWindowTitle("校正設定を書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in written)
            + "\n\nこの書き出しは「適用」ではありません。"
            "機器への適用結果は測定・検証ワークスペースで記録してください。"
        )
        box.setDetailedText(
            f"エクスポートID: {result.export.export_id}\n"
            f"設定SHA-256: "
            f"{result.export.exported_settings_semantic_sha256}"
        )
        box.exec()

    def _export_analysis_bundle(self) -> None:
        """Operator action behind ``analysis.export_bundle`` (#512).

        Packages the project's persisted measurement datasets and A/B
        comparisons into the deterministic analysis export (CSV/JSON/HTML)
        via the typed series adapters — provenance and historical flags
        are derived from the real authorities, never typed in.

        The evidence re-verification (SHA-256 over every measurement
        asset) and series assembly run on the bundle pool: their cost
        scales with total stored asset bytes, so a project with real
        capture data would stall the shell behind the dialogs. The UI
        resumes in ``_analysis_export_prepare_done`` for the prompts,
        then a second job performs the render + write.
        """
        if self._bundle_busy:
            return
        self._begin_bundle_job("解析エクスポートを作成しています…")
        self._bundle_pool.start(
            "project.bundle.analysis_export.prepare",
            self._analysis_export_prepare,
            self._bundle_job_completed,
        )

    def _analysis_export_prepare(
        self, cancel_event: Event
    ) -> _AnalysisExportPreparation | None:
        """Worker half of the analysis export — runs off the UI thread.

        ``None`` means the project holds nothing exportable at all; a
        cooperative stop still surfaces as ``WORKER_CANCELLED`` because
        ``NativeWorker.run`` re-checks the flag after the op returns.
        """
        measurements = CadMeasurementRepository(self.repository)
        records = measurements.list_measurements(self.document_id)
        comparisons: tuple = ()
        metadata: list[AnalysisExportMeta] = []
        try:
            comparisons = tuple(measurements.list_comparisons(self.document_id))
        except EXPECTED_OPERATION_ERRORS as exc:
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
            return None
        head = self.repository.current_head(self.document_id)
        current_revision_id = (
            head.revision_id if head is not None else None
        )
        series = []
        omitted_measurements = 0
        for record in records:
            if cancel_event.is_set():
                return None
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
            # The comparison surface renders three curves — side A levels,
            # side B levels and the A−B difference — plus the semantics
            # payload, so the bundle ships all of it like the workspace's
            # single-comparison export does.
            comparison_series, comparison_metadata = comparison_export_parts(
                comparison,
                current_scene_revision_id=current_revision_id,
            )
            series.extend(comparison_series)
            metadata.extend(comparison_metadata)
        return _AnalysisExportPreparation(
            series=tuple(series),
            metadata=tuple(metadata),
            omitted_measurements=omitted_measurements,
        )

    def _analysis_export_prepare_done(
        self, result: _AnalysisExportPreparation | None, error: object
    ) -> None:
        """UI half of the analysis export: prompt, then queue the write."""
        if isinstance(error, Exception):
            warn_user(
                self.shell,
                "解析エクスポートを作成できませんでした",
                error,
            )
            return
        if result is None:
            QMessageBox.warning(
                self.shell,
                "解析エクスポート",
                "書き出せる測定・比較データがありません。",
            )
            return
        if not result.series:
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
        if not ok:
            return
        if not title:
            QMessageBox.warning(
                self.shell, "解析エクスポート", "エクスポート名を入力してください"
            )
            return
        export = build_analysis_export(
            document_id=self.document_id,
            title=title,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            series=result.series,
            metadata=result.metadata,
        )
        # #989: sensitive-data preflight — the operator reviews each
        # member's classification/rights/risk before the destination
        # prompt; external shares gate through the allowlist manifest.
        from .cad_code_policy_repository import CadCodePolicyRepository
        from .export_preflight import (
            PreflightPlan,
            analyze_member_files,
            stored_classification_map,
        )

        members = {
            'export.csv': render_analysis_csv(export),
            'export.json': render_analysis_json(export),
            'report.html': render_analysis_html(export),
        }
        head = self.repository.current_head(self.document_id)
        preflight = PreflightPlan(
            export_kind='analysis_export',
            document_id=self.document_id,
            source_revision_id=None,
            source_sha256=(None if head is None else head.content_hash),
            elements=analyze_member_files(
                members,
                stored_classification_map(
                    CadCodePolicyRepository(self.repository),
                    self.document_id,
                ),
            ),
            payload=members,
        )
        if not self._run_export_preflight(
            preflight,
            title='解析エクスポート',
            bundle_kind='client_package',
            default_scope='external_review',
        ):
            return
        directory = file_dialog_memory.get_existing_directory(
            self.shell,
            "解析エクスポートの保存先フォルダー",
            'project.export_analysis',
            default_dir=self._default_export_dir(),
        )
        if not directory:
            return
        self._analysis_export_preflight = (preflight, directory)
        excluded_members = frozenset(
            element.element_id[len('member:'):]
            for element in preflight.excluded()
            if element.kind == 'member'
        )
        comparisons_failed = any(
            meta.key == 'omitted.comparisons' for meta in result.metadata
        )
        omitted = result.omitted_measurements
        self._begin_bundle_job("解析エクスポートを書き出しています…")
        self._bundle_pool.start(
            "project.bundle.analysis_export.write",
            lambda _cancel_event: _AnalysisExportWriteResult(
                export=export,
                written=_write_analysis_export(
                    Path(directory), export,
                    exclude_members=excluded_members,
                ),
                omitted_measurements=omitted,
                comparisons_failed=comparisons_failed,
            ),
            self._bundle_job_completed,
        )

    def _analysis_export_write_done(
        self, result: _AnalysisExportWriteResult | None, error: object
    ) -> None:
        """Surface the written analysis export — runs on the UI thread."""
        if isinstance(error, Exception):
            warn_user(
                self.shell, "解析エクスポートを書き出せませんでした", error
            )
            return
        if result is None:
            return
        box = QMessageBox(self.shell)
        box.setWindowTitle("解析エクスポートを書き出しました")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "次のファイルを書き出しました:\n"
            + "\n".join(str(path) for path in result.written)
        )
        details = [f"仕様 SHA-256: {result.export.spec_sha256}"]
        if result.omitted_measurements:
            details.append(
                f"{result.omitted_measurements} 件の測定データは検証に失敗したため"
                "除外しました（ファイル内の metadata に記録されています）"
            )
        if result.comparisons_failed:
            details.append(
                "保存済み比較を検証できなかったため、比較は除外しました"
            )
        preflight_state = getattr(self, '_analysis_export_preflight', None)
        if preflight_state:
            preflight, directory = preflight_state
            self._last_export_preflight = (
                preflight,
                getattr(self, '_last_export_preflight', (None, None))[1]
                if isinstance(getattr(self, '_last_export_preflight', None), tuple)
                else 'external_review',
                directory,
            )
            details.extend(
                self._export_postcheck(self._last_export_preflight)
            )
            self._analysis_export_preflight = None
            self._last_export_preflight = None
        box.setDetailedText("\n".join(details))
        box.exec()

    def _can_close_application(self) -> tuple[bool, str | None]:
        if self._bundle_busy:
            return False, "プロジェクトバンドル処理が完了してから終了してください"
        if self.data_management_component.can_close_application:
            return True, None
        return False, "データ処理が完了してからHTDTを終了してください"

    def _shutdown_bundle_pool(self) -> None:
        self._bundle_pool.shutdown()

    def _begin_bundle_job(self, message: str) -> None:
        self._bundle_busy = True
        self._bundle_status_message = message
        self.shell.statusBar().showMessage(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    @Slot(object, object, object)
    def _bundle_job_completed(
        self, key: object, result: object, error: object
    ) -> None:
        """Apply the finished bundle job's outcome on the UI thread.

        Late completions can never reach here after close/project-switch —
        the close-hook shutdown releases each task record inside the pool,
        and the pool drops any delivery that outlives its record.
        """
        task_key = str(key)
        self._bundle_busy = False
        QApplication.restoreOverrideCursor()
        # The in-flight line must not outlive the job — without this the
        # status bar kept claiming an export/import was still running after
        # it finished. Only clear it while it is still displayed: a newer
        # notice posted mid-job stays.
        if (
            self._bundle_status_message is not None
            and self.shell.statusBar().currentMessage()
            == self._bundle_status_message
        ):
            self.shell.statusBar().clearMessage()
        self._bundle_status_message = None
        if task_key.startswith("capture.watch-queue."):
            self._watch_queue_route_done(task_key, result, error)
            return
        if error == WORKER_CANCELLED:
            self.shell.statusBar().showMessage(
                "プロジェクトバンドル処理を中止しました"
            )
            return
        if task_key.startswith("project.bundle.analysis_export.prepare"):
            self._analysis_export_prepare_done(result, error)
            return
        if task_key.startswith("project.bundle.analysis_export.write"):
            self._analysis_export_write_done(result, error)
            return
        if task_key.startswith("project.bundle.export.preflight"):
            self._bundle_preflight_done(result, error)
            return
        if task_key.startswith("project.bundle.export"):
            if isinstance(error, Exception):
                QMessageBox.warning(
                    self.shell,
                    "エクスポートできません",
                    to_user_facing_error(
                        error, title="エクスポートできませんでした"
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
            details = [f"マニフェストSHA-256: {result.manifest_sha256}"]
            details.extend(
                self._export_postcheck(getattr(self, '_last_export_preflight', None))
            )
            self._last_export_preflight = None
            box.setDetailedText('\n'.join(details))
            box.exec()
            return
        if task_key.startswith("project.bundle.import"):
            if (
                isinstance(error, BundleImportConflictError)
                and task_key == "project.bundle.import"
                and self._bundle_import_path is not None
            ):
                retry = QMessageBox.question(
                    self.shell,
                    "そのままインポートできません",
                    f"{to_user_facing_error(error, title='インポートできませんでした').notice_text()}\n\nコピーとして新しいプロジェクトを作成しますか？",
                )
                if retry == QMessageBox.StandardButton.Yes:
                    self._begin_bundle_job(
                        "プロジェクトバンドルをコピーとしてインポートしています…"
                    )
                    source = self._bundle_import_path
                    self._bundle_pool.start(
                        "project.bundle.import_copy",
                        lambda _cancel_event: import_project_bundle(
                            self.repository,
                            source,
                            import_as_copy=True,
                        ),
                        self._bundle_job_completed,
                    )
                return
            if isinstance(error, Exception):
                warn_user(self.shell, "インポートできませんでした", error)
                return
            QMessageBox.information(
                self.shell,
                "プロジェクトをインポートしました",
                f"{result.imported_rows} 件のレコードと {result.imported_assets} 件のアセットを取り込みました。",
            )
            entry = self.project_library.get_by_document_id(result.document_id)
            if entry is not None:
                self._switch_to_project(entry)


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
