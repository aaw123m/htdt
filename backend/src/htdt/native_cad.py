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
    'newer_schema_dialog_copy_ja': (
        '.native_upgrade',
        'newer_schema_dialog_copy_ja',
    ),
    'upgrade_failure_recovery_ja': (
        '.native_upgrade',
        'upgrade_failure_recovery_ja',
    ),
    'execute_native_upgrade': ('.native_upgrade', 'execute_native_upgrade'),
    'plan_native_upgrade': ('.native_upgrade', 'plan_native_upgrade'),
    'HTDTLaunchIntent': ('.launch_intents', 'HTDTLaunchIntent'),
    'LaunchIntentResult': ('.launch_intents', 'LaunchIntentResult'),
    'build_launch_intent': ('.launch_intents', 'build_launch_intent'),
    'complete_queued_intent': ('.launch_intents', 'complete_queued_intent'),
    'describe_launch_intent': ('.launch_intents', 'describe_launch_intent'),
    'drain_launch_intents': ('.launch_intents', 'drain_launch_intents'),
    'ensure_intent_incoming_dir': (
        '.launch_intents',
        'ensure_intent_incoming_dir',
    ),
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
    'restore_backup': ('バックアップから復元', 'action'),
    'verify_data': ('今すぐデータを検証', 'action'),
    'choose_another_project': ('別のプロジェクトを選択', 'action'),
}


def _restorable_backups(data_dir: Path) -> list[Path]:
    """Newest-first restorable archives; empty when none exist.

    Read-only listing — generation/snapshot discovery must never break
    the launch path it advises.
    """

    try:
        from .automatic_backup import list_restorable_backups

        return list(list_restorable_backups(Path(data_dir)))
    except Exception:
        return []


def _create_startup_splash(app) -> object | None:
    """Minimal honest splash for the pre-window phase (round8-lifecycle).

    Painted programmatically — no image asset dependency — and every
    failure inside returns None so a cosmetic surface can never break
    launch. Messages come from ``_splash_status`` at each init seam.
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
        from PySide6.QtWidgets import QApplication, QSplashScreen

        # QPixmap aborts the process without a live QGuiApplication — a
        # fatal, not a Python exception, so it must be gated explicitly.
        if QApplication.instance() is None:
            return None
        pixmap = QPixmap(460, 240)
        pixmap.fill(QColor('#1b1d23'))
        painter = QPainter(pixmap)
        try:
            painter.setPen(QColor('#3c4048'))
            painter.drawRect(pixmap.rect().adjusted(0, 0, -1, -1))
            painter.setPen(QColor('#e8eaed'))
            title_font = QFont()
            title_font.setPointSize(15)
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.drawText(
                pixmap.rect().adjusted(0, 54, 0, 0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                'Home Theater Digital Twin',
            )
            painter.setPen(QColor('#9aa0a8'))
            version_font = QFont()
            version_font.setPointSize(9)
            painter.setFont(version_font)
            painter.drawText(
                pixmap.rect().adjusted(0, 96, 0, 0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                version_string(),
            )
        finally:
            painter.end()
        return QSplashScreen(pixmap)
    except Exception:
        return None


def _splash_status(splash, app, message: str | None = None) -> None:
    """Update the splash's progress line; a processEvents keeps it live.

    ``message=None`` pumps the event loop without touching the text — used
    to re-show the splash after a modal dialog owned the screen. Fully
    exception-safe: launch must never depend on a cosmetic surface.
    """
    if splash is None:
        return
    try:
        if message:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QColor

            splash.showMessage(
                message,
                alignment=Qt.AlignmentFlag.AlignBottom
                | Qt.AlignmentFlag.AlignHCenter,
                color=QColor('#c8ccd2'),
            )
        app.processEvents()
    except Exception:
        pass


def _close_splash(splash) -> None:
    if splash is not None:
        try:
            splash.close()
        except Exception:
            pass


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
    if choice in ('verify_data', 'choose_another_project', 'restore_backup'):
        diagnostics.logger.info("recovery launch action: %s", choice)
        return None, choice
    diagnostics.logger.info("recovery launch: opening normally")
    return None, None


def _offer_post_update_revalidation(
    app,
    splash,
    data_dir: Path,
    diagnostics: NativeDiagnostics,
) -> None:
    """First-run-after-update stale-evidence summary + revalidation offer.

    Any build change (a real schema upgrade, or a semantic re-key where
    the schema version stayed the same) can strand persisted authority:
    reads that replay sealed records start refusing with no explanation.
    This check runs one bounded authority audit on the first launch of a
    changed build and offers the 再検証 lane — records that honestly
    re-derive under the current build get re-sealed; the rest stay stale
    with reasons. Every failure here is non-fatal: launch proceeds.
    """

    from .authority_revalidation import (
        launch_build_changed,
        post_update_copy_ja,
        revalidate_native_authority_graph,
        write_launch_marker,
    )
    from .native_authority_audit import audit_native_authority_graph

    database_path = Path(data_dir) / "cad-scenes.sqlite3"
    try:
        if not launch_build_changed(data_dir):
            return
        # The marker records "this build launched here", not "the data is
        # clean" — stamp before the audit so a later audit failure does
        # not re-run the (potentially expensive) check every launch.
        write_launch_marker(data_dir)
        if not database_path.is_file() or database_path.stat().st_size == 0:
            return
        audit = audit_native_authority_graph(database_path)
    except Exception:  # noqa: BLE001 - post-update notice must never block launch
        diagnostics.logger.warning(
            "post-update stale-evidence check failed", exc_info=True
        )
        return
    if audit.ok:
        return

    stale_count = len(
        {(d.authority, d.record_ref) for d in audit.diagnostics}
    )
    diagnostics.logger.warning(
        "post-update audit: %d records need revalidation (%s)",
        stale_count,
        audit.summary(),
    )
    from PySide6.QtWidgets import QMessageBox

    if splash is not None:
        splash.hide()
    try:
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "HTDT アップデート後の再検証",
            post_update_copy_ja(stale_count),
        )
        revalidate_button = box.addButton(
            "再検証を実行", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton("あとで", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is revalidate_button:
            try:
                report = revalidate_native_authority_graph(database_path)
                QMessageBox.information(
                    None, "HTDT 再検証", report.summary_ja()
                )
                diagnostics.logger.info(
                    "post-update revalidation: %d revalidated, "
                    "%d kept stale",
                    len(report.revalidated),
                    len(report.kept_stale),
                )
            except Exception as exc:  # noqa: BLE001
                diagnostics.logger.warning(
                    "post-update revalidation failed", exc_info=True
                )
                QMessageBox.warning(
                    None,
                    "HTDT 再検証",
                    "再検証を完了できませんでした。データは変更されていません。\n"
                    f"詳細: {exc}",
                )
    finally:
        if splash is not None:
            splash.show()
        _splash_status(splash, app)


def _notify_instance_active(diagnostics: NativeDiagnostics) -> None:
    """Second-launch notice: the running instance was asked to surface.

    Informational, never an error — the forwarded activation intent raises
    the existing window while this process exits. Falls back to stderr
    when Qt cannot present a dialog.
    """

    message = (
        "HTDTはすでに起動しています。\n\n"
        "実行中のウィンドウを前面に表示しました。"
        "プロジェクト・キャプチャ・バックアップファイルはそのウィンドウで"
        "開けます。"
    )
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is None:
            app = QApplication([sys.argv[0]])
        QMessageBox.information(None, "HTDTはすでに起動しています", message)
        return
    except Exception:
        diagnostics.logger.debug('already-running notice dialog unavailable')
    write_stderr(f'HTDTはすでに起動しています\n{message}')


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


#: Launch-failure reason per classified subsystem — the primary text of the
#: generic launch dialog stays a localized, culprit-naming line instead of a
#: raw exception dump (which lives in the Details expander / log).
_LAUNCH_CLASS_REASON_JA = {
    'renderer_initialization':
        '描画エンジン（GPU/ドライバ）の初期化に失敗しました',
    'schema_incompatibility': 'データ形式がこのビルドと互換性がありません',
    'migration_failure': 'データ移行を完了できませんでした',
    'preference_state': 'アプリケーション設定の読み込みに失敗しました',
    'integration_initialization': '外部連携サービスの初期化に失敗しました',
    'project_data': 'プロジェクトデータを開けませんでした',
}


def _launch_reason_ja(exc: BaseException) -> str:
    """Localized primary reason for a launch-failure dialog.

    Typed mappings win (file locked, permission denied, upgrade/schema
    failures); otherwise the classified subsystem names what failed, and
    only a truly unclassifiable error falls back to the generic line.
    """

    from .user_facing_error import operation_error_message

    message = operation_error_message(exc)
    if message != '操作を完了できませんでした':
        return message
    try:
        from .startup_recovery import classify_startup_failure
    except Exception:
        return message
    return _LAUNCH_CLASS_REASON_JA.get(
        classify_startup_failure(exc),
        message,
    )


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
    from .user_facing_error import operation_error_message

    application = getattr(window, 'workflow_application', None)
    if application is not None:
        # The pump stays bound to the first window it was given; after a
        # close+respawn project switch that shell is closed and hidden.
        # Route through the live composition instead of silently
        # rebinding the dead one.
        application = application.live_composition()
        window = application.shell
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

    if outcome == 'activated':
        # The raise/activate above is the whole effect; a second-instance
        # activation must not pop a dialog over the user's work.
        return result

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
                switch_reason = application._switch_project(result.document_id)
            except Exception as exc:
                diagnostics.logger.exception(
                    'project switch failed for %s', intent.path
                )
                outcome = 'failed'
                result = result.model_copy(
                    update={
                        'outcome': outcome,
                        'detail': (
                            'プロジェクトの切り替えに失敗: '
                            f'{operation_error_message(exc)}'
                        ),
                    }
                )
            else:
                if switch_reason is not None:
                    # The guarded switch names the real refusal — a frozen
                    # data gate, an archived entry, a running operation —
                    # so the dialog repeats it instead of guessing a cause.
                    outcome = 'blocked_dirty_state'
                    result = result.model_copy(
                        update={
                            'outcome': outcome,
                            'detail': switch_reason,
                        }
                    )
                elif application.document_id != result.document_id:
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
                            'バックアップは有効ですがプレビュー画面が'
                            'ビジーまたは利用不可です: '
                            f'{operation_error_message(exc)}'
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


def _drain_queued_launch_intents(data_dir, dispatch) -> int:
    """Drain queued forwarded intents and dispatch each through the router.

    #736: a queued file is retired only after its semantic dispatch produced
    an outcome — a crash before completion leaves it queued for the next
    drain tick. While a modal dialog owns the event loop the whole drain
    defers instead: routing an intent mid-dialog could switch the project,
    dispose mounted workspaces underneath the open dialog, or stack a
    second modal over the one the user is answering.
    """

    _self = sys.modules[__name__]
    QApplication = _self.QApplication
    drain_launch_intents = _self.drain_launch_intents
    complete_queued_intent = _self.complete_queued_intent

    if QApplication.activeModalWidget() is not None:
        return 0
    dispatched = 0
    for queued in drain_launch_intents(data_dir):
        result = dispatch(queued.intent)
        complete_queued_intent(
            queued, succeeded=result.outcome != 'failed'
        )
        dispatched += 1
    return dispatched


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
    log_default_document_classification = (
        _self.log_default_document_classification
    )
    build_launch_intent = _self.build_launch_intent
    inspect_legacy_store = _self.inspect_legacy_store
    IncompatibleNewerSchemaError = _self.IncompatibleNewerSchemaError
    NativeUpgradeError = _self.NativeUpgradeError
    execute_native_upgrade = _self.execute_native_upgrade
    plan_native_upgrade = _self.plan_native_upgrade
    ProjectLibraryRepository = _self.ProjectLibraryRepository

    # #739: set before the try so failure paths can complete the record
    # only when this attempt got far enough to create one.
    launch_record = None
    splash = None
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
        # Round9 #11: the 'restore from backup' recovery choice exists only
        # when a restorable generation actually exists on disk.
        restorable_backups = _restorable_backups(args.data_dir)
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
            backup_restore_available=bool(restorable_backups),
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
        try:
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
        except Exception:
            # Launch bookkeeping is diagnostic evidence, never a gate: an
            # unwritable recovery-metadata file must not fail the launch
            # itself (the launch would then be reported as a crash it
            # never was).
            diagnostics.logger.exception('launch record write failed')
            launch_record = None
        # Round8-lifecycle deferred item: an honest splash for the
        # pre-window phase — shown only after the recovery decision is
        # resolved (dialogs are user input, not loading), and hidden while
        # the upgrade notices own the screen since a splash is
        # always-on-top. Cosmetic only: _create_startup_splash returns
        # None rather than breaking launch.
        splash = _create_startup_splash(app)
        if splash is not None:
            try:
                splash.show()
            except Exception:
                splash = None
        _splash_status(splash, app, 'データ形式を確認しています…')
        # #606: run the explicit upgrade lifecycle before any repository
        # opens the store — preflight, mandatory recovery copy, migration,
        # verification and an operational journal entry.
        upgrade_plan = plan_native_upgrade(args.data_dir)
        if upgrade_plan.requires_data_update:
            from PySide6.QtWidgets import QMessageBox

            if splash is not None:
                splash.hide()
            QMessageBox.information(
                None,
                "HTDT データ更新",
                upgrade_plan.upgrade_copy_ja,
            )
            if splash is not None:
                splash.show()
            _splash_status(splash, app)
        _splash_status(splash, app, 'データ形式を更新しています…')
        upgrade_event = execute_native_upgrade(args.data_dir)
        if upgrade_event.outcome == 'completed':
            diagnostics.logger.info(
                "data upgrade applied: schema v%s -> v%s (recovery copy: %s)",
                upgrade_event.from_schema,
                upgrade_event.to_schema,
                upgrade_event.recovery_snapshot_ref,
            )
            from PySide6.QtWidgets import QMessageBox

            if splash is not None:
                splash.hide()
            QMessageBox.information(
                None,
                "HTDT データ更新",
                "HTDTがプロジェクトデータを形式 "
                f"{upgrade_event.from_schema} から {upgrade_event.to_schema} "
                "へ更新しました。先に復旧用コピーを作成しています。",
            )
            if splash is not None:
                splash.show()
            _splash_status(splash, app)
        # Round 14: after any build change — schema upgrade or a semantic
        # re-key with no schema bump — audit persisted evidence once and
        # offer the revalidation lane before the workspace opens. Safe
        # Mode skips it: the guarded launch keeps project authority
        # read-only.
        if safe_mode_policy is None:
            _offer_post_update_revalidation(
                app, splash, args.data_dir, diagnostics
            )
        _splash_status(splash, app, 'プロジェクトデータを開いています…')
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
        _splash_status(splash, app, '起動するプロジェクトを確認しています…')
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
        _splash_status(splash, app, '連携サービスを初期化しています…')
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
        _splash_status(splash, app, 'ウィンドウを構築しています…')
        window = (
            _self.OptimizationWorkspaceWindow(repository, project_entry.document_id)
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
        if splash is not None:
            try:
                splash.finish(window)
            except Exception:
                _close_splash(splash)
        # Recovery-dialog follow-throughs the dialog could not perform
        # itself (#739): Verify data opens the data-management surface and
        # Choose another project lands on the Projects destination instead
        # of auto-entering the project the failed session was bound to.
        if post_launch_action in ('verify_data', 'restore_backup'):
            application = getattr(window, 'workflow_application', None)
            settings_dialog = getattr(application, 'settings_dialog', None)
            if settings_dialog is not None:
                # Show the settings surface FIRST so its restore-preview
                # signal subscription exists before preview_restore runs.
                settings_dialog.open_settings()
            if post_launch_action == 'restore_backup':
                # Same surface as the forwarded .htdt-backup open path:
                # validate + preview the newest generation — the restore
                # itself remains an explicit user confirmation there.
                controller = getattr(
                    application, 'data_management_controller', None
                )
                newest = (
                    restorable_backups[0] if restorable_backups else None
                )
                if controller is None or newest is None:
                    diagnostics.logger.warning(
                        'restore_backup chosen but no controller/generation'
                    )
                else:
                    try:
                        controller.preview_restore(newest)
                    except Exception as exc:
                        diagnostics.logger.warning(
                            'restore preview failed for %s: %s', newest, exc
                        )
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
        from PySide6.QtCore import QFileSystemWatcher, QTimer

        QTimer.singleShot(
            0, app, lambda: [_dispatch(i) for i in initial_intents]
        )

        # Round 14: deliver forwarded intents through a filesystem watch on
        # the incoming queue directory instead of stat()-polling it every
        # 800ms for the whole session. The slow timer remains only as a
        # safety net for platforms where the watch silently drops its path
        # (the queue dir being replaced, some network filesystems), keeping
        # the guaranteed drain latency bounded without a constant poll.
        incoming_dir = _self.ensure_intent_incoming_dir(args.data_dir)
        intent_watcher = QFileSystemWatcher([str(incoming_dir)])

        def _rearm_intent_watch() -> None:
            # QFileSystemWatcher stops reporting once the watched directory
            # is removed; re-add it after each signal/tick so a recreated
            # queue directory keeps delivering.
            if (
                incoming_dir.is_dir()
                and str(incoming_dir) not in intent_watcher.directories()
            ):
                intent_watcher.addPath(str(incoming_dir))

        def _drain() -> None:
            _drain_queued_launch_intents(args.data_dir, _dispatch)
            _rearm_intent_watch()

        intent_watcher.directoryChanged.connect(lambda _path: _drain())

        # Unparented on purpose: the router's window abstraction is not
        # necessarily a QObject, and the local reference keeps the pump
        # alive through app.exec() either way.
        intent_pump = QTimer()
        intent_pump.setInterval(5000)
        intent_pump.timeout.connect(_drain)
        intent_pump.start()
        exit_code = int(app.exec())
        # #926: stop the LAN listener on exit; the requested policy in
        # preferences is untouched so next launch restores the same choice.
        if capture_receiver is not None:
            capture_receiver.shutdown()
        # #739: the session reached a clean close — the launch record is
        # completed so it no longer counts as failed-startup evidence. A
        # bookkeeping failure here must not fall through to the generic
        # startup-failure handler: the session WAS clean, and recording it
        # as crashed would offer a false recovery next launch.
        if launch_record is not None:
            try:
                complete_launch(
                    args.data_dir, launch_record.launch_id, clean=True
                )
            except Exception:
                diagnostics.logger.exception(
                    'clean-close launch record write failed'
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
        _close_splash(splash)
        if launch_record is not None:
            try:
                complete_launch(
                    args.data_dir,
                    launch_record.launch_id,
                    clean=False,
                    failure_class='schema_incompatibility',
                )
            except Exception:
                diagnostics.logger.exception(
                    'failed-launch record write failed'
                )
        diagnostics.log_startup_failure(exc)
        newer_reason, newer_recovery = _self.newer_schema_dialog_copy_ja(exc)
        report_launch_failure(
            title="HTDTデータがこのビルドより新しいです",
            reason=newer_reason,
            recovery=newer_recovery,
            log_path=diagnostics.log_path,
            technical_detail=concise_reason(exc),
        )
        return 1
    except NativeUpgradeError as exc:
        _close_splash(splash)
        if launch_record is not None:
            try:
                complete_launch(
                    args.data_dir,
                    launch_record.launch_id,
                    clean=False,
                    failure_class='migration_failure',
                )
            except Exception:
                diagnostics.logger.exception(
                    'failed-launch record write failed'
                )
        diagnostics.log_startup_failure(exc)
        report_launch_failure(
            title="HTDTがデータを更新できませんでした",
            reason=_launch_reason_ja(exc),
            recovery=_self.upgrade_failure_recovery_ja(exc),
            log_path=diagnostics.log_path,
            technical_detail=concise_reason(exc),
        )
        return 1
    except Exception as exc:
        _close_splash(splash)
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
            reason=_launch_reason_ja(exc),
            recovery=(
                "この失敗でデータは変更されていません。HTDTをもう一度起動"
                "してください。再発する場合は最新のバックアップを復元し、"
                "診断ログをサポートへ共有してください。"
            ),
            log_path=diagnostics.log_path,
            technical_detail=concise_reason(exc),
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    # Consoles in a non-UTF-8 code page (e.g. cp1252) would otherwise crash the
    # Japanese help/maintenance output — after the requested work already
    # completed — so stdout gets the same tolerant policy write_stderr uses.
    try:
        sys.stdout.reconfigure(errors='backslashreplace')
    except (AttributeError, OSError, ValueError):
        pass
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
    maintenance.add_argument(
        "--revalidate",
        action="store_true",
        help=(
            "アップデート後に要検証となった記録を現在のビルドで"
            "再検証（再導出できる記録を再署名）して終了"
        ),
    )
    # Round 14: the pre-update trap — a build that re-keyed persisted
    # evidence makes --backup refuse, so the user cannot even export. This
    # modifier pairs with --backup to write a degraded archive instead:
    # every failing row is declared in the manifest and re-checked at
    # restore staging.
    parser.add_argument(
        "--backup-allow-stale",
        action="store_true",
        help=(
            "要再検証の記録をマニフェストに明記したうえで"
            "バックアップを許可（--backup と併用）"
        ),
    )
    # Reports the display version ("<version>+g<sha>[.dirty]") so a packaged
    # binary identifies the exact source build it was produced from. This is
    # the same version recorded in installer AppVersion and backup manifests.
    parser.add_argument("--version", action="version", version=f"%(prog)s {version_string()}")
    args = parser.parse_args(argv)
    if args.workflow_shell and args.legacy_ui:
        parser.error("--workflow-shell と --legacy-ui は併用できません")
    if args.backup_allow_stale and args.backup is None:
        parser.error("--backup-allow-stale は --backup と併用してください")

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
    elif args.revalidate:
        launch_mode = "revalidate"
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
            reason=_launch_reason_ja(exc),
            recovery=(
                "保存先のドライブやフォルダを確認してからHTDTを起動し直して"
                "ください。データを移動した場合は --data-dir で新しい場所を"
                "指定してください。"
            ),
            log_path=None,
            technical_detail=concise_reason(exc),
        )
        return 1
    diagnostics = configure_diagnostics(args.data_dir)
    install_exception_hooks(diagnostics)
    diagnostics.log_session_start(launch_mode)

    guard = SingleInstanceGuard(args.data_dir)
    if not guard.acquire():
        diagnostics.log_lock_contention(read_lock_metadata(args.data_dir))
        # #612 + round9: a second GUI launch is not a failure — its
        # file-open intents are handed to the running instance through the
        # drop queue, an activation intent raises its window, and the
        # second process exits cleanly after telling the user. A lock held
        # only by a dead process is impossible (the OS owns the lock), so
        # reaching this point means a live instance will drain the queue.
        if not maintenance_request:
            from .launch_intents import (
                build_activation_intent,
                build_launch_intent,
                forward_launch_intent,
            )

            forwarded = True
            for path in args.open_paths:
                try:
                    forward_launch_intent(
                        args.data_dir,
                        build_launch_intent(path, source='forwarded'),
                    )
                except OSError:
                    forwarded = False
            try:
                forward_launch_intent(
                    args.data_dir,
                    build_activation_intent(args.data_dir),
                )
            except OSError:
                forwarded = False
            if forwarded:
                write_stderr(
                    "実行中のHTDTインスタンスへドキュメントオープン要求を"
                    "転送しました"
                    if args.open_paths
                    else "実行中のHTDTインスタンスへ起動要求を転送しました"
                )
                if not args.open_paths:
                    # A bare relaunch: the raise lands inside the running
                    # instance — report it on this side so the user sees a
                    # deliberate outcome, not a silently vanishing launch.
                    _notify_instance_active(diagnostics)
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

            manifest = create_backup(
                args.data_dir,
                args.backup,
                allow_stale=args.backup_allow_stale,
            )
            print(
                f"バックアップを作成しました: {args.backup} "
                f"(schema={manifest.schema_version}, files={len(manifest.files)})"
            )
            if manifest.stale_authorities:
                print(
                    "注意: "
                    f"{len(manifest.stale_authorities)} 件の記録は検証を"
                    "通過せず、マニフェストに明記されました。"
                    "「データ管理」の「記録を再検証」または "
                    "--revalidate で再検証できます。"
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
        if args.revalidate:
            from .authority_revalidation import (
                revalidate_native_authority_graph,
            )

            report = revalidate_native_authority_graph(
                args.data_dir / "cad-scenes.sqlite3"
            )
            print(report.summary_ja())
            return 0 if report.resolved else 1
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
