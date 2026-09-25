from __future__ import annotations

import argparse
from pathlib import Path
import sys
import zipfile

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .build_info import version_string
from .cad_composition import CadEditorWindow
from .cad_repository import SceneRepository
from .cad_synthetic_demo import seed_synthetic_optimization_demo
from .constraint_editor import ConstraintEditorWindow
from .default_document import log_default_document_classification
from .measurement_editor import MeasurementEditorWindow
from .measurement_workspace import MeasurementWorkspaceWindow
from .legacy_data import inspect_legacy_store, migrate_legacy_data
from .native_backup import create_backup, restore_backup
from .native_diagnostics import (
    NativeDiagnostics,
    concise_reason,
    configure_diagnostics,
    install_exception_hooks,
    report_launch_failure,
    write_stderr,
)
from .native_upgrade import (
    IncompatibleNewerSchemaError,
    NativeUpgradeError,
    execute_native_upgrade,
    plan_native_upgrade,
)
from .launch_intents import (
    HTDTLaunchIntent,
    LaunchIntentResult,
    build_launch_intent,
    complete_queued_intent,
    describe_launch_intent,
    drain_launch_intents,
    forward_launch_intent,
)
from .launch_router import route_launch_intent
from .native_editor import default_data_dir
from .optimization_workspace import OptimizationWorkspaceWindow
from .project_bundle import import_project_bundle
from .project_library_repository import ProjectLibraryRepository
from .prediction_workspace import PredictionWorkspaceWindow
from .runtime_instance import SingleInstanceGuard, read_lock_metadata
from .theater_workflow import TheaterWorkflowWindow
from .ui_theme import apply_dark_theme
from .workflow_application import build_workflow_application
from .workflow_shell import WorkflowShellWindow

# Preserve the public theater-editor alias while the concrete product composition
# advances through N80. N40-N70 behavior remains inherited unchanged.
TheaterEditorWindow = OptimizationWorkspaceWindow

__all__ = [
    "CadEditorWindow",
    "TheaterEditorWindow",
    "TheaterWorkflowWindow",
    "ConstraintEditorWindow",
    "MeasurementEditorWindow",
    "MeasurementWorkspaceWindow",
    "PredictionWorkspaceWindow",
    "OptimizationWorkspaceWindow",
    "WorkflowShellWindow",
    "build_workflow_shell",
    "main",
]


def build_workflow_shell(
    repository: SceneRepository,
    document_id: str,
    project_library: ProjectLibraryRepository | None = None,
) -> WorkflowShellWindow:
    """Build the integrated workflow application while preserving the public API."""

    return build_workflow_application(
        repository, document_id, project_library=project_library
    )


def _packaged_application_icon() -> Path | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        runtime_icon = Path(bundle_root) / "htdt_branding" / "HTDT.png"
        if runtime_icon.is_file():
            return runtime_icon

    executable_icon = Path(sys.executable).resolve().with_name("HTDT.ico")
    if executable_icon.is_file():
        return executable_icon

    return None


def _route_launch_intent(
    intent: HTDTLaunchIntent,
    *,
    window,
    repository: SceneRepository,
    diagnostics: NativeDiagnostics,
) -> LaunchIntentResult:
    """One routing authority for menu, OS association, drop and forwarding.

    The semantic route runs through ``launch_router`` (Qt-free, testable);
    this shell layer applies the GUI-only half — the project switch, the
    Capture Inbox deep link and the Restore preview surface — and maps the
    exact outcome to user copy (#736). Success copy is shown only after
    the corresponding domain action actually ran.
    """

    from PySide6.QtWidgets import QMessageBox

    window.raise_()
    window.activateWindow()
    diagnostics.logger.info(
        "launch intent routed: kind=%s source=%s path=%s",
        intent.kind,
        intent.source,
        intent.path,
    )

    result = route_launch_intent(intent, repository=repository)
    outcome = result.outcome
    application = getattr(window, 'workflow_application', None)

    if outcome == 'routed_and_opened':
        if application is None:
            outcome = 'user_action_required'
            result = result.model_copy(
                update={
                    'outcome': outcome,
                    'detail': (
                        f'{result.detail} — open it from the project '
                        'library'
                    ),
                }
            )
        else:
            try:
                application._open_project(result.document_id)
            except Exception as exc:
                diagnostics.logger.exception(
                    'project switch failed for %s', intent.path
                )
                outcome = 'failed'
                result = result.model_copy(
                    update={
                        'outcome': outcome,
                        'detail': f'project switch failed: {exc}',
                    }
                )
            else:
                if application.document_id != result.document_id:
                    outcome = 'blocked_dirty_state'
                    result = result.model_copy(
                        update={
                            'outcome': outcome,
                            'detail': (
                                'current work must be saved or discarded '
                                'before switching projects'
                            ),
                        }
                    )

    if outcome in ('staged_for_review', 'already_staged'):
        if application is not None:
            try:
                from .navigation_target import (
                    NavigationTarget,
                    NavigationTargetKind,
                )
                from .workflow_navigation import ApplicationDestinationId

                application.shell.navigate_to_target(
                    NavigationTarget(
                        kind=NavigationTargetKind.CAPTURE_INBOX_ITEM,
                        object_ids=(
                            (result.inbox_item_id,) if result.inbox_item_id else ()
                        ),
                        preferred_destination=ApplicationDestinationId.INBOX,
                    )
                )
            except Exception:
                diagnostics.logger.exception(
                    'inbox deep link failed for %s', intent.path
                )

    if outcome == 'preview_opened' and intent.kind == 'preview_backup':
        if application is None:
            outcome = 'user_action_required'
            result = result.model_copy(
                update={
                    'outcome': outcome,
                    'detail': (
                        f'{result.detail} — open Settings > Data '
                        'Management to restore it'
                    ),
                }
            )
        else:
            try:
                application.settings_dialog.open_settings()
                application.data_management_controller.preview_restore(
                    Path(intent.path)
                )
            except Exception as exc:
                diagnostics.logger.warning(
                    'restore preview failed for %s: %s', intent.path, exc
                )
                outcome = 'user_action_required'
                result = result.model_copy(
                    update={
                        'outcome': outcome,
                        'detail': (
                            f'backup is valid but the preview surface is '
                            f'busy or unavailable: {exc}'
                        ),
                    }
                )

    diagnostics.logger.info(
        'launch intent outcome: %s (%s) for %s',
        outcome,
        result.detail,
        intent.path,
    )

    # User copy maps 1:1 from the outcome contract — no message may claim
    # more than the outcome states.
    if outcome in ('routed_and_opened',):
        QMessageBox.information(
            window,
            "HTDT project",
            f"Opened {describe_launch_intent(intent)}.",
        )
    elif outcome in ('staged_for_review', 'already_staged'):
        QMessageBox.information(
            window,
            "HTDT capture",
            f"{'Staged' if outcome == 'staged_for_review' else 'Already staged'} "
            f"{describe_launch_intent(intent)} for review in the Capture "
            "Inbox — nothing was promoted to evidence.",
        )
    elif outcome == 'preview_opened':
        QMessageBox.information(
            window,
            "HTDT backup",
            f"Backup preview: {Path(intent.path).name}\n"
            f"- {result.detail}\n\n"
            "Restore is always a separate, explicit action.",
        )
    elif outcome == 'blocked_dirty_state':
        QMessageBox.information(
            window,
            "HTDT",
            f"Could not open {describe_launch_intent(intent)}: "
            f"{result.detail}",
        )
    elif outcome == 'user_action_required':
        QMessageBox.information(
            window,
            "HTDT",
            f"{describe_launch_intent(intent)} needs your action: "
            f"{result.detail}",
        )
    else:
        QMessageBox.warning(
            window,
            "HTDT",
            f"Could not open {describe_launch_intent(intent)}: "
            f"{result.detail or outcome}",
        )
    return result


def _run_gui(args: argparse.Namespace, diagnostics: NativeDiagnostics) -> int:
    """GUI startup boundary: failures leave a durable record and a visible reason."""

    # #739: set before the try so failure paths can complete the record
    # only when this attempt got far enough to create one.
    launch_record = None
    try:
        app = QApplication([sys.argv[0]])
        app.setApplicationVersion(version_string())
        icon_path = _packaged_application_icon()
        if icon_path is not None:
            app.setWindowIcon(QIcon(str(icon_path)))
        apply_dark_theme(app)
        # #739: Recovery Launch — decide from concrete previous-session
        # evidence and bounded launch history BEFORE repeating risky
        # initialization. The launch record is written up front so a
        # crash in this attempt counts as a failed launch next time.
        from .startup_recovery import (
            classify_startup_failure,
            complete_launch,
            decide_launch,
            load_recovery_metadata,
            record_launch,
        )
        from .support_diagnostics import previous_session_unexpected_end
        from datetime import datetime, timezone

        unclean = previous_session_unexpected_end(args.data_dir)
        recovery_metadata = load_recovery_metadata(args.data_dir)
        # The runtime.json marker is only produced by an installed
        # single-instance forwarder; the launch records are the durable
        # unclean evidence this build itself guarantees — a previous
        # record never completed clean means that session died.
        last_record = (
            recovery_metadata.records[-1]
            if recovery_metadata.records
            else None
        )
        last_failure = recovery_metadata.last_failure_class(
            version_string()
        )
        launch_decision = decide_launch(
            unclean_previous_session=(
                unclean.unexpected_end
                or (
                    last_record is not None
                    and last_record.clean_exit is not True
                )
            ),
            metadata=recovery_metadata,
            build_id=version_string(),
            renderer_failure_detected=(
                last_failure == 'renderer_initialization'
            ),
        )
        safe_mode_policy = None
        if launch_decision.mode != 'normal':
            from PySide6.QtWidgets import QMessageBox

            diagnostics.logger.warning(
                "recovery launch offered: %s",
                '; '.join(launch_decision.reasons),
            )
            normal_button = QMessageBox.ButtonRole.AcceptRole
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "HTDT recovered from an unexpected session",
                "HTDT recovered from an unexpected previous session.\n\n"
                + "\n".join(
                    f"- {reason}" for reason in launch_decision.reasons
                )
                + (
                    "\n\nThe previous failure looks data-related — "
                    "consider Verify data or restoring a backup."
                    if launch_decision.restore_recommended
                    else "\n\nThis does not look like project-data "
                    "corruption; restoring a backup is not the first "
                    "recovery step."
                ),
            )
            open_normal = box.addButton("Open normally", normal_button)
            safe_mode = box.addButton(
                "Open in Safe Mode",
                QMessageBox.ButtonRole.DestructiveRole,
            )
            diagnostics_button = box.addButton(
                "Open diagnostics",
                QMessageBox.ButtonRole.ActionRole,
            )
            box.exec()
            clicked = box.clickedButton()
            if clicked is safe_mode:
                safe_mode_policy = launch_decision.safe_mode_policy
                diagnostics.logger.info(
                    "safe mode selected: project authority stays read-only"
                )
            elif clicked is diagnostics_button:
                QMessageBox.information(
                    None,
                    "HTDT diagnostics",
                    f"Diagnostics are stored at:\n{diagnostics.log_path}\n\n"
                    "Use Support > Package Diagnostics for a support "
                    "bundle.",
                )
                safe_mode_policy = launch_decision.safe_mode_policy
            else:
                diagnostics.logger.info("recovery launch: opening normally")
        launch_record = record_launch(
            args.data_dir,
            build_id=version_string(),
            launch_mode=(
                'safe_mode'
                if safe_mode_policy is not None
                else launch_decision.mode
            ),
            started_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        # #606: run the explicit upgrade lifecycle before any repository
        # opens the store — preflight, mandatory recovery copy, migration,
        # verification and an operational journal entry.
        upgrade_plan = plan_native_upgrade(args.data_dir)
        if upgrade_plan.requires_data_update:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                None,
                "HTDT data update",
                upgrade_plan.upgrade_copy_ja,
            )
        upgrade_event = execute_native_upgrade(args.data_dir)
        if upgrade_event.outcome == 'completed':
            diagnostics.logger.info(
                "data upgrade applied: schema v%s -> v%s (recovery copy: %s)",
                upgrade_event.from_schema,
                upgrade_event.to_schema,
                upgrade_event.recovery_snapshot_ref,
            )
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                None,
                "HTDT data update",
                "HTDT updated your project data from format "
                f"{upgrade_event.from_schema} to {upgrade_event.to_schema}. "
                "A recovery copy was created first.",
            )
        repository = SceneRepository(args.data_dir / "cad-scenes.sqlite3")
        # #627: surface what the legacy default document actually holds
        # (untouched synthetic fixture vs. real user project) in diagnostics.
        # Read-only; the report never alters persisted data.
        log_default_document_classification(repository, diagnostics.logger)
        composition = "legacy-optimization" if args.legacy_ui else "workflow-shell"
        diagnostics.logger.info("root composition: %s", composition)
        # #612: launch intents passed on the command line (e.g. a Windows
        # file-association launch) may name the project to open.
        # Safe Mode (#739) does not auto-open them: repeating the same
        # auto-open is exactly the risky initialization being escaped.
        initial_intents = (
            []
            if safe_mode_policy is not None
            else [
                build_launch_intent(path, source='file_association')
                for path in getattr(args, 'open_paths', None) or ()
            ]
        )
        # #736: a descriptor may fast-path startup to its document only
        # when the document is actually registered in this data root —
        # otherwise initial launch must behave exactly like a forwarded
        # intent: the router reports "not registered" instead of the
        # startup path silently creating an empty project under that id.
        project_library = ProjectLibraryRepository(repository)
        for intent in initial_intents:
            if (
                intent.kind == 'open_project'
                and intent.document_id
                and project_library.get_by_document_id(intent.document_id)
                is not None
            ):
                args.document_id = intent.document_id
        # #598: detect the retired browser authority in the same data root.
        # Read-only; legacy data outside the canonical store is warned in
        # diagnostics and in Settings > Data Management rather than migrated
        # implicitly.
        legacy_report = inspect_legacy_store(args.data_dir)
        if legacy_report.state in {'populated', 'unreadable'}:
            diagnostics.logger.warning(
                'legacy browser data detected (state=%s counts=%s): '
                'htdt.sqlite3 is retired and no longer backed up; run '
                '`htdt-native --migrate-legacy-data` to migrate and archive it',
                legacy_report.state,
                legacy_report.table_counts,
            )
        else:
            diagnostics.logger.info(
                'legacy browser store state: %s', legacy_report.state
            )
        # #450: resolve the project to open through the library — most recent
        # project wins, existing documents migrate in as named projects, and
        # an explicit --document-id still binds (and registers) directly.
        project_entry = project_library.resolve_startup_document(
            args.document_id
        )
        window = (
            OptimizationWorkspaceWindow(repository, project_entry.document_id)
            if args.legacy_ui
            else build_workflow_shell(
                repository, project_entry.document_id, project_library
            )
        )
        window.show()

        def _dispatch(intent: HTDTLaunchIntent) -> LaunchIntentResult:
            return _route_launch_intent(
                intent,
                window=window,
                repository=repository,
                diagnostics=diagnostics,
            )

        # Route launch-time intents once the event loop is up, then keep
        # draining the single-instance forward queue for the life of the
        # window.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            0, lambda: [_dispatch(i) for i in initial_intents]
        )

        # Unparented on purpose: the router's window abstraction is not
        # necessarily a QObject, and the local reference keeps the pump
        # alive through app.exec() either way.
        intent_pump = QTimer()
        intent_pump.setInterval(800)

        def _drain() -> None:
            # #736: retire each queue file only after its semantic
            # dispatch produced an outcome — a crash before completion
            # leaves the file queued for the next drain.
            for queued in drain_launch_intents(args.data_dir):
                result = _dispatch(queued.intent)
                complete_queued_intent(
                    queued, succeeded=result.outcome != 'failed'
                )

        intent_pump.timeout.connect(_drain)
        intent_pump.start()
        exit_code = int(app.exec())
        # #739: the session reached a clean close — the launch record is
        # completed so it no longer counts as failed-startup evidence.
        complete_launch(
            args.data_dir, launch_record.launch_id, clean=True
        )
        # #617: a clean close with changed managed data earns a validated
        # rotating generation. Failures are logged, never fatal to exit.
        try:
            from .automatic_backup import AutomaticBackupScheduler

            AutomaticBackupScheduler(args.data_dir).run_due('clean_close')
        except Exception:
            diagnostics.logger.exception(
                "clean-close automatic backup failed"
            )
        return exit_code
    except IncompatibleNewerSchemaError as exc:
        if launch_record is not None:
            complete_launch(
                args.data_dir,
                launch_record.launch_id,
                clean=False,
                failure_class='schema_incompatibility',
            )
        diagnostics.log_startup_failure(exc)
        report_launch_failure(
            title="HTDT data is newer than this build",
            reason=str(exc),
            recovery=str(exc),
            log_path=diagnostics.log_path,
        )
        return 1
    except NativeUpgradeError as exc:
        if launch_record is not None:
            complete_launch(
                args.data_dir,
                launch_record.launch_id,
                clean=False,
                failure_class='migration_failure',
            )
        diagnostics.log_startup_failure(exc)
        report_launch_failure(
            title="HTDT could not update your data",
            reason=concise_reason(exc),
            recovery=str(exc),
            log_path=diagnostics.log_path,
        )
        return 1
    except Exception as exc:
        if launch_record is not None:
            try:
                complete_launch(
                    args.data_dir,
                    launch_record.launch_id,
                    clean=False,
                    failure_class=classify_startup_failure(exc),
                )
            except Exception:
                pass
        diagnostics.log_startup_failure(exc)
        report_launch_failure(
            title="HTDT did not start",
            reason=concise_reason(exc),
            recovery=(
                "Your data was not modified by this failure. Start HTDT again; "
                "if the problem repeats, restore your most recent backup and "
                "share the diagnostic log with support."
            ),
            log_path=diagnostics.log_path,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTDT native CAD editor")
    # #621: --data-dir > bootstrap config > platform default. A bootstrap
    # root that is unavailable fails closed rather than silently reopening
    # the default location.
    parser.add_argument("--data-dir", type=Path, default=None)
    # #450: the project library owns the default document; --document-id is
    # now an explicit-override path only, not the happy path.
    parser.add_argument("--document-id", default=None)
    # #612: files passed positionally are document-open intents — the Windows
    # file associations invoke `HTDT.exe "%1"` which lands here.
    parser.add_argument(
        "open_paths",
        nargs="*",
        type=Path,
        metavar="FILE",
        help="project (.htdtproject), capture (.htdtcapture) or backup "
        "(.htdt-backup) files to open",
    )
    parser.add_argument(
        "--legacy-ui",
        action="store_true",
        help="launch the legacy OptimizationWorkspaceWindow composition instead of the default workflow shell (rollback)",
    )
    # Accepted for compatibility: the workflow shell is the default launch
    # path since UX160, so the old opt-in flag no longer has an effect.
    parser.add_argument("--workflow-shell", action="store_true", help=argparse.SUPPRESS)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument(
        "--backup",
        type=Path,
        metavar="ARCHIVE",
        help="create a validated .htdt-backup archive and exit",
    )
    maintenance.add_argument(
        "--restore",
        type=Path,
        metavar="ARCHIVE",
        help="restore a validated .htdt-backup archive and exit",
    )
    maintenance.add_argument(
        "--automatic-backup",
        action="store_true",
        help="run one due automatic backup generation and exit (scheduled tasks)",
    )
    maintenance.add_argument(
        "--seed-synthetic-demo",
        action="store_true",
        help="seed an explicitly synthetic O10-O80 development demo and exit",
    )
    maintenance.add_argument(
        "--migrate-legacy-data",
        action="store_true",
        help=(
            "migrate the retired browser store (htdt.sqlite3) into native "
            "projects, archive it, and exit"
        ),
    )
    # Reports the display version ("<version>+g<sha>[.dirty]") so a packaged
    # binary identifies the exact source build it was produced from. This is
    # the same version recorded in installer AppVersion and backup manifests.
    parser.add_argument("--version", action="version", version=f"%(prog)s {version_string()}")
    args = parser.parse_args(argv)
    if args.workflow_shell and args.legacy_ui:
        parser.error("--workflow-shell and --legacy-ui cannot be combined")

    # #621: resolve the managed root through the documented precedence and
    # fail closed when a configured location is unavailable.
    from .data_relocation import (
        ManagedDataUnavailableError,
        assert_managed_root_available,
        resolve_data_dir,
    )

    try:
        args.data_dir, data_dir_source = resolve_data_dir(
            args.data_dir, default=default_data_dir()
        )
        assert_managed_root_available(args.data_dir, data_dir_source)
    except ManagedDataUnavailableError as exc:
        print(f"HTDT data directory unavailable: {exc}", file=sys.stderr)
        return 1

    if args.backup is not None:
        launch_mode = "backup"
    elif args.restore is not None:
        launch_mode = "restore"
    elif args.automatic_backup:
        launch_mode = "automatic-backup"
    elif args.seed_synthetic_demo:
        launch_mode = "seed-synthetic-demo"
    elif args.migrate_legacy_data:
        launch_mode = "migrate-legacy-data"
    else:
        launch_mode = "gui"
    maintenance_request = launch_mode != "gui"
    diagnostics = configure_diagnostics(args.data_dir)
    install_exception_hooks(diagnostics)
    diagnostics.log_session_start(launch_mode)

    guard = SingleInstanceGuard(args.data_dir)
    if not guard.acquire():
        diagnostics.log_lock_contention(read_lock_metadata(args.data_dir))
        # #612: a second launch carrying file-open intents hands them to the
        # running instance through the drop queue, then exits quietly — a
        # double-clicked project file must not surface a failure.
        if args.open_paths:
            forwarded = True
            for path in args.open_paths:
                try:
                    forward_launch_intent(
                        args.data_dir,
                        build_launch_intent(path, source='forwarded'),
                    )
                except OSError:
                    forwarded = False
            if forwarded:
                write_stderr(
                    "forwarded document-open request(s) to the running HTDT "
                    "instance"
                )
                return 0
        write_stderr(
            "HTDT data directory is already in use by another process: "
            f"{args.data_dir}"
        )
        if not maintenance_request:
            report_launch_failure(
                title="HTDT is already running",
                reason="Another HTDT instance is already using this data directory.",
                recovery=(
                    "Close the other HTDT window, then start HTDT again. "
                    "If no other instance is running, wait a moment and retry."
                ),
                log_path=diagnostics.log_path,
            )
        return 2

    try:
        if args.backup is not None:
            manifest = create_backup(args.data_dir, args.backup)
            print(
                f"backup created: {args.backup} "
                f"(schema={manifest.schema_version}, files={len(manifest.files)})"
            )
            return 0
        if args.restore is not None:
            manifest, pre_restore = restore_backup(args.data_dir, args.restore)
            suffix = "" if pre_restore is None else f" · pre-restore backup: {pre_restore}"
            print(
                f"backup restored: {args.restore} "
                f"(schema={manifest.schema_version}, files={len(manifest.files)}){suffix}"
            )
            return 0
        if args.automatic_backup:
            from .automatic_backup import AutomaticBackupScheduler

            result = AutomaticBackupScheduler(args.data_dir).run_due('periodic')
            if result is None:
                print("automatic backup: not due")
            else:
                print(f"automatic backup created: {result[0]}")
            return 0
        if args.seed_synthetic_demo:
            repository = SceneRepository(args.data_dir / "cad-scenes.sqlite3")
            result = seed_synthetic_optimization_demo(repository)
            print(
                "synthetic demo seeded: "
                f"document={result.document_id} "
                f"search={result.search_spec_id} "
                f"validation={result.validation_id} "
                f"adaptive={result.adaptive_plan_id} "
                f"extended={result.extended_search_id} "
                f"adaptive-extended={result.adaptive_extended_plan_id}"
            )
            print("synthetic demo is development-only and does not unlock owned-room recommendation")
            return 0
        if args.migrate_legacy_data:
            repository = SceneRepository(args.data_dir / "cad-scenes.sqlite3")
            result = migrate_legacy_data(
                args.data_dir,
                project_library=ProjectLibraryRepository(repository),
            )
            print(f"legacy migration: {result.model_dump_json()}")
            return 0

        return _run_gui(args, diagnostics)
    except Exception as exc:
        if not maintenance_request:
            raise
        # Maintenance commands run headless (scheduled tasks, CI): report the
        # failure through diagnostics and exit nonzero — a modal failure dialog
        # would hang the caller on a machine with nobody to dismiss it.
        diagnostics.log_startup_failure(exc)
        write_stderr(f"HTDT --{launch_mode} failed: {concise_reason(exc)}")
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
