from __future__ import annotations

import argparse
from pathlib import Path
import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .application_preferences import ApplicationPreferenceStore
    from .cad_repository import SceneRepository
    from .capture_receiver_controller import CaptureReceiverController
    from .launch_intents import HTDTLaunchIntent, LaunchIntentResult
    from .project_library_repository import ProjectLibraryRepository
    from .workflow_shell import WorkflowShellWindow

from .build_info import version_string
from .native_diagnostics import (
    NativeDiagnostics,
    concise_reason,
    configure_diagnostics,
    install_exception_hooks,
    report_launch_failure,
    write_stderr,
)
from .runtime_instance import (
    SingleInstanceGuard,
    default_data_dir,
    read_lock_metadata,
)

# Lazy exports: every name here stays importable from this module (the
# public facade in ``__all__`` plus collaborators tests monkeypatch on it)
# but loads only on first attribute access. Importing them eagerly pulls
# PySide6/PyVista/VTK and the repository stack, which costs seconds and is
# never needed by the headless maintenance entry points (``--backup``,
# ``--restore``, ``--automatic-backup``, ``--seed-synthetic-demo``,
# ``--migrate-legacy-data``, ``--version``) or by the single-instance
# forwarding path.
_LAZY_EXPORTS = {
    'CadEditorWindow': ('.cad_composition', 'CadEditorWindow'),
    'ConstraintEditorWindow': ('.constraint_editor', 'ConstraintEditorWindow'),
    'MeasurementEditorWindow': (
        '.measurement_editor',
        'MeasurementEditorWindow',
    ),
    'MeasurementWorkspaceWindow': (
        '.measurement_workspace',
        'MeasurementWorkspaceWindow',
    ),
    'OptimizationWorkspaceWindow': (
        '.optimization_workspace',
        'OptimizationWorkspaceWindow',
    ),
    'PredictionWorkspaceWindow': (
        '.prediction_workspace',
        'PredictionWorkspaceWindow',
    ),
    'TheaterWorkflowWindow': ('.theater_workflow', 'TheaterWorkflowWindow'),
    'WorkflowShellWindow': ('.workflow_shell', 'WorkflowShellWindow'),
    'build_workflow_application': (
        '.workflow_application',
        'build_workflow_application',
    ),
    'QApplication': ('PySide6.QtWidgets', 'QApplication'),
    'QIcon': ('PySide6.QtGui', 'QIcon'),
    'apply_dark_theme': ('.ui_theme', 'apply_dark_theme'),
    'SceneRepository': ('.cad_repository', 'SceneRepository'),
    'ProjectLibraryRepository': (
        '.project_library_repository',
        'ProjectLibraryRepository',
    ),
    'inspect_legacy_store': ('.legacy_data', 'inspect_legacy_store'),
    'migrate_legacy_data': ('.legacy_data', 'migrate_legacy_data'),
    'create_backup': ('.native_backup', 'create_backup'),
    'restore_backup': ('.native_backup', 'restore_backup'),
    'seed_synthetic_optimization_demo': (
        '.cad_synthetic_demo',
        'seed_synthetic_optimization_demo',
    ),
    'log_default_document_classification': (
        '.default_document',
        'log_default_document_classification',
    ),
    'IncompatibleNewerSchemaError': (
        '.native_upgrade',
        'IncompatibleNewerSchemaError',
    ),
    'NativeUpgradeError': ('.native_upgrade', 'NativeUpgradeError'),
    'execute_native_upgrade': ('.native_upgrade', 'execute_native_upgrade'),
    'plan_native_upgrade': ('.native_upgrade', 'plan_native_upgrade'),
    'HTDTLaunchIntent': ('.launch_intents', 'HTDTLaunchIntent'),
    'LaunchIntentResult': ('.launch_intents', 'LaunchIntentResult'),
    'build_launch_intent': ('.launch_intents', 'build_launch_intent'),
    'complete_queued_intent': ('.launch_intents', 'complete_queued_intent'),
    'describe_launch_intent': ('.launch_intents', 'describe_launch_intent'),
    'drain_launch_intents': ('.launch_intents', 'drain_launch_intents'),
    'forward_launch_intent': ('.launch_intents', 'forward_launch_intent'),
    'route_launch_intent': ('.launch_router', 'route_launch_intent'),
}


def __getattr__(name: str):
    # Preserve the public theater-editor alias while the concrete product
    # composition advances through N80. N40-N70 behavior remains inherited
    # unchanged.
    target = (
        'OptimizationWorkspaceWindow'
        if name == 'TheaterEditorWindow'
        else name
    )
    entry = _LAZY_EXPORTS.get(target)
    if entry is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module_name, attribute = entry
    from importlib import import_module

    module = (
        import_module(module_name, __package__)
        if module_name.startswith('.')
        else import_module(module_name)
    )
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted([*globals(), *_LAZY_EXPORTS, 'TheaterEditorWindow'])

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
    repository: 'SceneRepository',
    document_id: str,
    project_library: 'ProjectLibraryRepository | None' = None,
    capture_receiver: 'CaptureReceiverController | None' = None,
    preferences: 'ApplicationPreferenceStore | None' = None,
    safe_mode: bool = False,
) -> 'WorkflowShellWindow':
    """Build the integrated workflow application while preserving the public API."""

    from .workflow_application import build_workflow_application

    return build_workflow_application(
        repository,
        document_id,
        project_library=project_library,
        capture_receiver=capture_receiver,
        preferences=preferences,
        safe_mode=safe_mode,
    )


# Button label/role for every choice ``LaunchDecision.choices`` may offer.
# The dialog renders the decision's choice list verbatim — a choice the
# decision model emits but the dialog never renders is a dead end the user
# can read but never click (round8).
_RECOVERY_CHOICE_PRESENTATION = {
    'open_normal': ('通常どおり開く', 'accept'),
    'open_safe_mode': ('セーフモードで開く', 'destructive'),
    'open_diagnostics': ('診断を開く', 'action'),
    'verify_data': ('今すぐデータを検証', 'action'),
    'choose_another_project': ('別のプロジェクトを選択', 'action'),
}


def _choose_recovery_action(
    launch_decision,
    diagnostics: NativeDiagnostics,
):
    """Show the recovery dialog; return (safe_mode_policy, post_launch_action).

    ``post_launch_action`` is a choice the dialog itself cannot perform —
    'verify_data' opens Settings > Data Management after launch, and
    'choose_another_project' lands on the Projects destination instead of
    auto-opening the project the failed session was bound to.
    """

    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(
        QMessageBox.Icon.Warning,
        "HTDTは前回予期せず終了しました",
        "HTDTは前回のセッションを異常終了から復旧しました。\n\n"
        + "\n".join(
            f"- {reason}" for reason in launch_decision.reasons
        )
        + (
            "\n\n前回の失敗はデータ関連の可能性があります — "
            "データの検証またはバックアップからの復元を検討してください。"
            if launch_decision.restore_recommended
            else "\n\nプロジェクトデータの破損とは見えません。"
            "バックアップの復元は最初の復旧手順ではありません。"
        ),
    )
    role_map = {
        'accept': QMessageBox.ButtonRole.AcceptRole,
        'destructive': QMessageBox.ButtonRole.DestructiveRole,
        'action': QMessageBox.ButtonRole.ActionRole,
    }
    buttons = {}
    for choice in launch_decision.choices:
        presentation = _RECOVERY_CHOICE_PRESENTATION.get(choice)
        if presentation is None:
            continue
        label, role = presentation
        buttons[choice] = box.addButton(label, role_map[role])
    if 'open_normal' not in buttons:
        buttons['open_normal'] = box.addButton(
            "通常どおり開く", QMessageBox.ButtonRole.AcceptRole
        )
    box.exec()
    clicked = box.clickedButton()
    choice = next(
        (c for c, button in buttons.items() if button is clicked),
        'open_normal',
    )
    if choice == 'open_safe_mode':
        diagnostics.logger.info(
            "safe mode selected: project authority stays read-only"
        )
        return launch_decision.safe_mode_policy, None
    if choice == 'open_diagnostics':
        QMessageBox.information(
            None,
            "HTDT 診断",
            f"診断は次に保存されています:\n{diagnostics.log_path}\n\n"
            "サポート用バンドルは サポート > 診断パッケージ を"
            "使ってください。",
        )
        # Diagnostics consulted first, then a guarded launch — unchanged
        # semantics from the original three-button surface.
        return launch_decision.safe_mode_policy, None
    if choice in ('verify_data', 'choose_another_project'):
        diagnostics.logger.info("recovery launch action: %s", choice)
        return None, choice
    diagnostics.logger.info("recovery launch: opening normally")
    return None, None


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
    intent: 'HTDTLaunchIntent',
    *,
    window,
    repository: 'SceneRepository',
    diagnostics: NativeDiagnostics,
) -> 'LaunchIntentResult':
    """One routing authority for menu, OS association, drop and forwarding.

    The semantic route runs through ``launch_router`` (Qt-free, testable);
    this shell layer applies the GUI-only half — the project switch, the
    Capture Inbox deep link and the Restore preview surface — and maps the
    exact outcome to user copy (#736). Success copy is shown only after
    the corresponding domain action actually ran.
    """

    from PySide6.QtWidgets import QMessageBox

    from .launch_intents import describe_launch_intent
    from .launch_router import route_launch_intent

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
                        f'{result.detail} — プロジェクトライブラリから'
                        '開いてください'
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
                        'detail': f'プロジェクトの切り替えに失敗: {exc}',
                    }
                )
            else:
                if application.document_id != result.document_id:
                    outcome = 'blocked_dirty_state'
                    result = result.model_copy(
                        update={
                            'outcome': outcome,
                            'detail': (
                                'プロジェクトを切り替える前に現在の作業を'
                                '保存または破棄する必要があります'
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
                        f'{result.detail} — 設定 > データ管理から'
                        '復元してください'
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
                            f'バックアップは有効ですがプレビュー画面が'
                            f'ビジーまたは利用不可です: {exc}'
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
            "HTDT プロジェクト",
            f"{describe_launch_intent(intent)}を開きました。",
        )
    elif outcome in ('staged_for_review', 'already_staged'):
        QMessageBox.information(
            window,
            "HTDT キャプチャ",
            f"{describe_launch_intent(intent)}を"
            f"{'レビュー用にキャプチャ受信箱へステージしました' if outcome == 'staged_for_review' else 'すでにステージ済みです'}"
            " — 証拠には昇格していません。",
        )
    elif outcome == 'preview_opened':
        QMessageBox.information(
            window,
            "HTDT バックアップ",
            f"バックアッププレビュー: {Path(intent.path).name}\n"
            f"- {result.detail}\n\n"
            "復元は常に別の明示的な操作です。",
        )
    elif outcome == 'blocked_dirty_state':
        QMessageBox.information(
            window,
            "HTDT",
            f"{describe_launch_intent(intent)}を開けませんでした: "
            f"{result.detail}",
        )
    elif outcome == 'user_action_required':
        QMessageBox.information(
            window,
            "HTDT",
            f"{describe_launch_intent(intent)}には操作が必要です: "
            f"{result.detail}",
        )
    else:
        QMessageBox.warning(
            window,
            "HTDT",
            f"{describe_launch_intent(intent)}を開けませんでした: "
            f"{result.detail or outcome}",
        )
    return result


def _run_gui(args: argparse.Namespace, diagnostics: NativeDiagnostics) -> int:
    """GUI startup boundary: failures leave a durable record and a visible reason."""

    # GUI-only collaborators load lazily through the module object: the
    # maintenance and forwarding entry points never pay the Qt/PyVista/VTK
    # import cost, and tests that monkeypatch ``native_cad.<name>`` still
    # see their doubles honored here (setattr lands in the module dict
    # before ``__getattr__`` is consulted).
    _self = sys.modules[__name__]
    QApplication = _self.QApplication
    QIcon = _self.QIcon
    apply_dark_theme = _self.apply_dark_theme
    SceneRepository = _self.SceneRepository
    OptimizationWorkspaceWindow = _self.OptimizationWorkspaceWindow
    log_default_document_classification = (
        _self.log_default_document_classification
    )
    build_launch_intent = _self.build_launch_intent
    complete_queued_intent = _self.complete_queued_intent
    drain_launch_intents = _self.drain_launch_intents
    inspect_legacy_store = _self.inspect_legacy_store
    IncompatibleNewerSchemaError = _self.IncompatibleNewerSchemaError
    NativeUpgradeError = _self.NativeUpgradeError
    execute_native_upgrade = _self.execute_native_upgrade
    plan_native_upgrade = _self.plan_native_upgrade
    ProjectLibraryRepository = _self.ProjectLibraryRepository

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
            annotate_launch,
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
            explicit_safe_mode=bool(getattr(args, 'safe_mode', False)),
            renderer_failure_detected=(
                last_failure == 'renderer_initialization'
            ),
        )
        safe_mode_policy = None
        post_launch_action = None
        if launch_decision.mode == 'safe_mode':
            # An explicit --safe-mode launch IS the user's choice — showing
            # the recovery dialog again would ask them to repeat it.
            safe_mode_policy = launch_decision.safe_mode_policy
            diagnostics.logger.info(
                'safe mode requested explicitly; skipping recovery dialog'
            )
        elif launch_decision.mode != 'normal':
            diagnostics.logger.warning(
                "recovery launch offered: %s",
                '; '.join(launch_decision.reasons),
            )
            safe_mode_policy, post_launch_action = _choose_recovery_action(
                launch_decision, diagnostics
            )
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
                "HTDT データ更新",
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
                "HTDT データ更新",
                "HTDTがプロジェクトデータを形式 "
                f"{upgrade_event.from_schema} から {upgrade_event.to_schema} "
                "へ更新しました。先に復旧用コピーを作成しています。",
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
        # Safe Mode enforces auto_open_last_project=False: the project the
        # previous session was bound to is the prime suspect, so it is
        # skipped and the next project (or a fresh default) opens instead.
        project_entry = project_library.resolve_startup_document(
            args.document_id,
            skip_last_opened=(
                safe_mode_policy is not None
                and not safe_mode_policy.auto_open_last_project
            ),
        )
        # Which project this launch committed to opening — recorded so the
        # next recovery dialog can name (and avoid) the suspect project.
        try:
            annotate_launch(
                args.data_dir,
                launch_record.launch_id,
                project_ref=(
                    getattr(project_entry, 'project_id', None)
                    or project_entry.document_id
                ),
            )
        except Exception:
            diagnostics.logger.exception('launch record annotation failed')
        # #926: the Capture receiver is one application-scoped service owned
        # by the data root — the workflow shell composes it and the app exit
        # stops it. The legacy fallback window deliberately runs without it.
        capture_receiver = None
        preferences = None
        if not args.legacy_ui:
            try:
                from .application_preferences import ApplicationPreferenceStore

                preferences = ApplicationPreferenceStore.for_data_dir(
                    args.data_dir
                )
                if (
                    safe_mode_policy is not None
                    and not safe_mode_policy.live_integrations
                ):
                    # Safe Mode policy: live integrations stay off so the
                    # capture listener cannot repeat the failure on record.
                    diagnostics.logger.info(
                        'safe mode: capture receiver not started '
                        '(live_integrations=False)'
                    )
                else:
                    from .capture_receiver_controller import (
                        CaptureReceiverController,
                    )

                    capture_receiver = CaptureReceiverController(
                        repository, preferences
                    )
            except Exception:
                diagnostics.logger.exception(
                    'capture receiver controller init failed; '
                    'receiver stays disabled'
                )
        window = (
            OptimizationWorkspaceWindow(repository, project_entry.document_id)
            if args.legacy_ui
            else build_workflow_shell(
                repository,
                project_entry.document_id,
                project_library,
                capture_receiver=capture_receiver,
                preferences=preferences,
                safe_mode=safe_mode_policy is not None,
            )
        )
        window.show()
        # Recovery-dialog follow-throughs the dialog could not perform
        # itself (#739): Verify data opens the data-management surface and
        # Choose another project lands on the Projects destination instead
        # of auto-entering the project the failed session was bound to.
        if post_launch_action == 'verify_data':
            settings_dialog = getattr(
                getattr(window, 'workflow_application', None),
                'settings_dialog',
                None,
            )
            if settings_dialog is not None:
                settings_dialog.open_settings()
        elif post_launch_action == 'choose_another_project':
            navigate = getattr(window, 'navigate', None)
            if navigate is not None:
                navigate('projects')
        # #755: the periodic trigger point the scheduler was designed for —
        # the first in-app caller. Safe Mode leaves background jobs off.
        application = getattr(window, 'workflow_application', None)
        if application is not None and safe_mode_policy is None:
            try:
                application.start_automatic_backup()
            except Exception:
                diagnostics.logger.exception(
                    'automatic backup tick failed to start'
                )
        if capture_receiver is not None:
            start_error = capture_receiver.start_if_requested()
            if start_error:
                diagnostics.logger.warning(
                    'capture receiver failed to start: %s', start_error
                )

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
            0, app, lambda: [_dispatch(i) for i in initial_intents]
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
        # #926: stop the LAN listener on exit; the requested policy in
        # preferences is untouched so next launch restores the same choice.
        if capture_receiver is not None:
            capture_receiver.shutdown()
        # #739: the session reached a clean close — the launch record is
        # completed so it no longer counts as failed-startup evidence.
        complete_launch(
            args.data_dir, launch_record.launch_id, clean=True
        )
        # #755: shutdown performs no archive work — the event loop and
        # window are already gone and a hidden post-UI backup is invisible
        # and uninterruptible. Record the clean close cheaply; the next
        # eligible scheduler tick performs any due generation.
        try:
            from .automatic_backup import AutomaticBackupScheduler

            AutomaticBackupScheduler(args.data_dir).record_clean_close()
        except Exception:
            diagnostics.logger.exception(
                "clean-close automatic backup marker failed"
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
            title="HTDTデータがこのビルドより新しいです",
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
            title="HTDTがデータを更新できませんでした",
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
            title="HTDTが起動しませんでした",
            reason=concise_reason(exc),
            recovery=(
                "この失敗でデータは変更されていません。HTDTをもう一度起動"
                "してください。再発する場合は最新のバックアップを復元し、"
                "診断ログをサポートへ共有してください。"
            ),
            log_path=diagnostics.log_path,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTDT ネイティブCADエディター")
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
        help="開くプロジェクト (.htdtproject)、キャプチャ (.htdtcapture) "
        "またはバックアップ (.htdt-backup) ファイル",
    )
    parser.add_argument(
        "--legacy-ui",
        action="store_true",
        help="既定のワークフローシェルの代わりに旧 OptimizationWorkspaceWindow 構成で起動（ロールバック用）",
    )
    # #739: an explicit Safe Mode entry point — the only way to reach the
    # guarded launch when a crash loop is too fast to use the dialog.
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="確認なしでセーフモードで起動（連携・保存済みレイアウト・自動オープンintentなし）",
    )
    # Accepted for compatibility: the workflow shell is the default launch
    # path since UX160, so the old opt-in flag no longer has an effect.
    parser.add_argument("--workflow-shell", action="store_true", help=argparse.SUPPRESS)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument(
        "--backup",
        type=Path,
        metavar="ARCHIVE",
        help="検証済みの .htdt-backup アーカイブを作成して終了",
    )
    maintenance.add_argument(
        "--restore",
        type=Path,
        metavar="ARCHIVE",
        help="検証済みの .htdt-backup アーカイブを復元して終了",
    )
    maintenance.add_argument(
        "--automatic-backup",
        action="store_true",
        help="期限の来た自動バックアップを1回実行して終了（スケジュールタスク用）",
    )
    maintenance.add_argument(
        "--seed-synthetic-demo",
        action="store_true",
        help="明示的に合成された O10-O80 開発デモをシードして終了",
    )
    maintenance.add_argument(
        "--migrate-legacy-data",
        action="store_true",
        help=(
            "廃止されたブラウザストア (htdt.sqlite3) をネイティブ"
            "プロジェクトへ移行し、アーカイブして終了"
        ),
    )
    # Reports the display version ("<version>+g<sha>[.dirty]") so a packaged
    # binary identifies the exact source build it was produced from. This is
    # the same version recorded in installer AppVersion and backup manifests.
    parser.add_argument("--version", action="version", version=f"%(prog)s {version_string()}")
    args = parser.parse_args(argv)
    if args.workflow_shell and args.legacy_ui:
        parser.error("--workflow-shell と --legacy-ui は併用できません")

    # #621: resolve the managed root through the documented precedence and
    # fail closed when a configured location is unavailable.
    from .data_relocation import (
        assert_managed_root_available,
        resolve_data_dir,
    )

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

    try:
        args.data_dir, data_dir_source = resolve_data_dir(
            args.data_dir, default=default_data_dir()
        )
        assert_managed_root_available(args.data_dir, data_dir_source)
    except Exception as exc:
        # The managed root resolution can also raise raw OSError (permission
        # denied reading the bootstrap config, relocation journal I/O) — every
        # failure here precedes QApplication, so the packaged GUI launch would
        # exit with no visible reason without report_launch_failure.
        if maintenance_request:
            print(
                f"HTDTのデータディレクトリが利用不可です: {exc}",
                file=sys.stderr,
            )
            return 1
        report_launch_failure(
            title="HTDTのデータディレクトリを開けません",
            reason=concise_reason(exc),
            recovery=(
                "保存先のドライブやフォルダを確認してからHTDTを起動し直して"
                "ください。データを移動した場合は --data-dir で新しい場所を"
                "指定してください。"
            ),
            log_path=None,
        )
        return 1
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
            # Deferred: the forwarding path is reached only when another
            # instance already holds the data-directory lock.
            from .launch_intents import build_launch_intent, forward_launch_intent

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
                    "実行中のHTDTインスタンスへドキュメントオープン要求を"
                    "転送しました"
                )
                return 0
        write_stderr(
            "HTDTのデータディレクトリは別プロセスが使用中です: "
            f"{args.data_dir}"
        )
        if not maintenance_request:
            report_launch_failure(
                title="HTDTはすでに起動しています",
                reason="別のHTDTインスタンスがこのデータディレクトリを使用中です。",
                recovery=(
                    "もう一方のHTDTウィンドウを閉じてから、HTDTを起動し直して"
                    "ください。他のインスタンスが動いていない場合は、少し待って"
                    "から再試行してください。"
                ),
                log_path=diagnostics.log_path,
            )
        return 2

    try:
        if args.backup is not None:
            from .native_backup import create_backup

            manifest = create_backup(args.data_dir, args.backup)
            print(
                f"バックアップを作成しました: {args.backup} "
                f"(schema={manifest.schema_version}, files={len(manifest.files)})"
            )
            return 0
        if args.restore is not None:
            from .native_backup import restore_backup

            manifest, pre_restore = restore_backup(args.data_dir, args.restore)
            suffix = "" if pre_restore is None else f" · 復元前バックアップ: {pre_restore}"
            print(
                f"バックアップを復元しました: {args.restore} "
                f"(schema={manifest.schema_version}, files={len(manifest.files)}){suffix}"
            )
            return 0
        if args.automatic_backup:
            from .automatic_backup import AutomaticBackupScheduler

            result = AutomaticBackupScheduler(args.data_dir).run_due('periodic')
            if result is None:
                print("自動バックアップ: 期限未到来")
            else:
                print(f"自動バックアップを作成しました: {result[0]}")
            return 0
        if args.seed_synthetic_demo:
            from .cad_repository import SceneRepository
            from .cad_synthetic_demo import seed_synthetic_optimization_demo

            repository = SceneRepository(args.data_dir / "cad-scenes.sqlite3")
            result = seed_synthetic_optimization_demo(repository)
            print(
                "合成デモをシードしました: "
                f"document={result.document_id} "
                f"search={result.search_spec_id} "
                f"validation={result.validation_id} "
                f"adaptive={result.adaptive_plan_id} "
                f"extended={result.extended_search_id} "
                f"adaptive-extended={result.adaptive_extended_plan_id}"
            )
            print("合成デモは開発専用で、実部屋の推奨機能は解放しません")
            return 0
        if args.migrate_legacy_data:
            from .cad_repository import SceneRepository
            from .legacy_data import migrate_legacy_data
            from .project_library_repository import ProjectLibraryRepository

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
        write_stderr(f"HTDT --{launch_mode} が失敗しました: {concise_reason(exc)}")
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
