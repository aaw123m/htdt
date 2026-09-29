"""Round-10 convergence regressions.

Covers the deferred items implemented this round:
  * uncaught exceptions posted to the Activity Center as failed
    pseudo-operations (persistent surface beside the status-bar notice),
  * the 再試行 affordance on warn_user for retryable error codes,
  * the LocalizationService wiring (policy-resolved locale),
  * the automatic-backup policy card in Data Management,
  * the startup splash helpers,
  * @media print rules on the HTML report renderers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from htdt.activity_center import ActivityCenter, OperationState
from htdt.automatic_backup import AutomaticBackupScheduler, policy_path
from htdt.native_diagnostics import (
    configure_diagnostics,
    install_exception_hooks,
    push_uncaught_sink,
    uninstall_exception_hooks,
)
from htdt.rew_api import RewApiUnavailable
from htdt.user_facing_error import RETRYABLE_ERROR_CODES, warn_user


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------
# Uncaught exceptions land on the persistent Activity Center surface.


def test_uncaught_exception_posts_failed_pseudo_operation(
    tmp_path: Path,
) -> None:
    app = _app()
    diagnostics = configure_diagnostics(tmp_path / "data")
    window = QMainWindow()
    window.show()
    app.processEvents()

    from htdt.workflow_application import WorkflowApplicationComposition

    # The handler only needs the two attributes it touches — build the
    # record without paying for a full composition.
    comp = WorkflowApplicationComposition.__new__(
        WorkflowApplicationComposition
    )
    comp._uncaught_op_count = 0
    comp.activity_center = ActivityCenter()

    install_exception_hooks(diagnostics)
    pop = push_uncaught_sink(comp._record_uncaught_operation)
    try:
        sys.excepthook(RuntimeError, RuntimeError('boom'), None)
        recent = comp.activity_center.recent()
        (entry,) = [op for op in recent if op.operation_kind == 'uncaught_exception']
        assert entry.state == OperationState.FAILED
        assert entry.title == '予期しないエラー'
        # The summary leads with the mapped message; the exception
        # identity stays on the record after it in parentheses.
        assert '操作を完了できませんでした' in (entry.error_summary or '')
        assert 'RuntimeError' in (entry.error_summary or '')
        assert 'boom' in (entry.error_summary or '')
        # The persistent record carries the log path — the transient
        # status-bar notice keeps its "look here" role.
        assert str(diagnostics.log_path) in (entry.error_summary or '')
        assert '予期しないエラー' in window.statusBar().currentMessage()
    finally:
        pop()
        uninstall_exception_hooks()
        window.close()
        window.deleteLater()


def test_uncaught_sink_pop_removes_registration(tmp_path: Path) -> None:
    app = _app()
    diagnostics = configure_diagnostics(tmp_path / "data")
    window = QMainWindow()
    window.show()
    app.processEvents()
    install_exception_hooks(diagnostics)
    seen: list[BaseException] = []
    pop = push_uncaught_sink(lambda _d, exc, _w: seen.append(exc))
    try:
        sys.excepthook(RuntimeError, RuntimeError('first'), None)
        pop()
        sys.excepthook(RuntimeError, RuntimeError('second'), None)
        assert [str(e) for e in seen] == ['first']
        pop()  # idempotent — the close hook must never raise
    finally:
        uninstall_exception_hooks()
        window.close()
        window.deleteLater()


def test_uncaught_pseudo_operation_is_bounded(tmp_path: Path) -> None:
    _app()
    from htdt.workflow_application import WorkflowApplicationComposition

    comp = WorkflowApplicationComposition.__new__(
        WorkflowApplicationComposition
    )
    comp._uncaught_op_count = 0
    comp.activity_center = ActivityCenter()
    diagnostics = configure_diagnostics(tmp_path / "data")

    for index in range(25):
        comp._record_uncaught_operation(
            diagnostics, RuntimeError(f'loop {index}'), None
        )
    kinds = [
        op.operation_kind for op in comp.activity_center.recent(limit=50)
    ]
    assert kinds.count('uncaught_exception') == 20


def test_uncaught_sink_skips_worker_thread(tmp_path: Path) -> None:
    _app()
    diagnostics = configure_diagnostics(tmp_path / "data")
    install_exception_hooks(diagnostics)
    seen: list[BaseException] = []
    pop = push_uncaught_sink(lambda _d, exc, _w: seen.append(exc))
    try:
        done = Event()

        import threading

        def worker() -> None:
            sys.excepthook(RuntimeError, RuntimeError('thread'), None)
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        assert done.wait(5.0)
        thread.join()
        assert seen == []
    finally:
        pop()
        uninstall_exception_hooks()


# ---------------------------------------------------------------------
# warn_user retry affordance: only retryable codes offer the button.


def _exec_clicking(button_role, *, times: int = 99) -> object:
    """Pretend the user clicked the given-role button, at most ``times``.

    Bounded because a retry re-enters warn_user — an always-click fake would
    recurse on a persistent failure, which is the dialog's real contract
    (the operator picks OK to stop retrying).
    """
    clicked = 0

    def fake_exec(self: QMessageBox) -> int:
        nonlocal clicked
        if clicked < times:
            for button in self.buttons():
                if self.buttonRole(button) == button_role:
                    button.click()
                    clicked += 1
                    break
        return 0

    return fake_exec


def test_warn_user_retry_button_re_invokes_for_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app()
    calls: list[str] = []
    monkeypatch.setattr(
        QMessageBox, 'exec', _exec_clicking(QMessageBox.ButtonRole.ApplyRole)
    )
    warn_user(
        None,
        'REW一覧の読み込みに失敗しました',
        RewApiUnavailable('connection refused'),
        on_retry=lambda: calls.append('retry'),
    )
    assert calls == ['retry']


def test_warn_user_hides_retry_for_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app()
    calls: list[str] = []
    buttons_seen: list[int] = []

    def fake_exec(self: QMessageBox) -> int:
        buttons_seen.append(len(self.buttons()))
        return 0

    monkeypatch.setattr(QMessageBox, 'exec', fake_exec)
    warn_user(
        None,
        '測定を保存できませんでした',
        RuntimeError('sqlite3.OperationalError'),
        on_retry=lambda: calls.append('retry'),
    )
    assert calls == []  # operation.failed is not retryable
    assert all(count <= 1 for count in buttons_seen)


def test_retryable_code_set_covers_rew_and_transient_io() -> None:
    assert {'rew.unavailable', 'rew.api'} <= RETRYABLE_ERROR_CODES
    assert 'operation.failed' not in RETRYABLE_ERROR_CODES


def test_registry_error_handler_retry_re_executes_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app()
    from htdt.command_registry import CommandDefinition, CommandRegistry

    attempts: list[str] = []
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='measurement.refresh', display_name='更新'),
        execute=lambda: attempts.append('run')
        or (_ for _ in ()).throw(RewApiUnavailable('refused')),
    )
    monkeypatch.setattr(
        QMessageBox,
        'exec',
        _exec_clicking(QMessageBox.ButtonRole.ApplyRole, times=1),
    )
    registry.set_error_handler(
        lambda definition, exc: warn_user(
            None,
            f'「{definition.display_name}」',
            exc,
            on_retry=lambda: registry.execute(definition.command_id),
        )
    )
    registry.execute('measurement.refresh')
    assert len(attempts) == 2  # first failure + one 再試行


# ---------------------------------------------------------------------
# LocalizationService wiring: policy resolves the presentation locale.


def test_localization_service_honors_language_policy() -> None:
    from htdt.localization import (
        LanguagePolicy,
        LocalizationService,
        PresentationLocale,
        build_workflow_catalog,
        resolve_locale,
    )

    assert (
        resolve_locale(
            LanguagePolicy('en'),
            system_locale=PresentationLocale.JAPANESE,
        )
        == PresentationLocale.ENGLISH
    )
    assert (
        LocalizationService(
            build_workflow_catalog(), policy=LanguagePolicy('en')
        ).tr('action.retry')
        == 'Retry'
    )
    assert (
        LocalizationService(
            build_workflow_catalog(), policy=LanguagePolicy('ja')
        ).tr('action.retry')
        == '再試行'
    )


# ---------------------------------------------------------------------
# Automatic backup policy card in Data Management.


def _data_management_widget(data_dir: Path):
    from htdt.data_management import (
        ApplicationDataLifecycle,
        DataManagementBackend,
        DataManagementController,
    )
    from htdt.data_management_ui import DataManagementWidget

    backend = DataManagementBackend(data_dir)
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )
    controller = DataManagementController(backend, lifecycle)
    return DataManagementWidget(controller)


def test_backup_policy_card_commits_policy(tmp_path: Path) -> None:
    _app()
    widget = _data_management_widget(tmp_path / "data")
    try:
        widget.backup_policy_enabled.setChecked(False)
        widget.backup_interval_spin.setValue(12.0)
        widget.backup_keep_spin.setValue(7)

        scheduler = AutomaticBackupScheduler(tmp_path / "data")
        policy = scheduler.load_policy()
        assert policy.enabled is False
        assert policy.interval_hours == 12.0
        assert policy.keep_generations == 7
        assert policy_path(tmp_path / "data").exists()
    finally:
        widget.deleteLater()


def test_backup_policy_card_loads_persisted_policy(tmp_path: Path) -> None:
    _app()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    scheduler = AutomaticBackupScheduler(data_dir)
    scheduler.save_policy(
        scheduler.load_policy().model_copy(
            update={'enabled': False, 'interval_hours': 48.0}
        )
    )
    widget = _data_management_widget(data_dir)
    try:
        assert widget.backup_policy_enabled.isChecked() is False
        assert widget.backup_interval_spin.value() == 48.0
        assert "（既定）" in widget.backup_dir_label.text()
    finally:
        widget.deleteLater()


# ---------------------------------------------------------------------
# Startup splash helpers (round8-lifecycle deferred item).


def test_startup_splash_helpers_survive_offscreen() -> None:
    app = _app()
    from htdt.native_cad import (
        _close_splash,
        _create_startup_splash,
        _splash_status,
    )

    splash = _create_startup_splash(app)
    assert splash is not None
    splash.show()
    _splash_status(splash, app, 'データ形式を確認しています…')
    window = QMainWindow()
    window.show()
    splash.finish(window)
    _close_splash(splash)
    window.close()
    window.deleteLater()
    # A missing splash must be a silent no-op at every call site.
    _splash_status(None, app, 'x')
    _close_splash(None)


# ---------------------------------------------------------------------
# @media print rules on the HTML report renderers.


def test_report_html_has_print_rules() -> None:
    from htdt.report import build_report_payload, render_report_html

    comparison = {
        'id': 'cmp-1',
        'project_id': 'project-1',
        'dataset_a_id': 'dataset-a',
        'dataset_b_id': 'dataset-b',
        'created_at': '2026-09-16T00:00:00+00:00',
        'spec': {'low_hz': 60, 'high_hz': 200, 'expected_change_paths': []},
        'result': {
            'algorithm_version': 'fr-compare-1',
            'comparison_role': 'configuration_ab',
            'grid_hz': [60.0, 80.0, 100.0],
            'a_db': [70.0, 72.0, 71.0],
            'b_db': [69.0, 70.0, 70.0],
            'difference_db': [1.0, 2.0, 1.0],
            'mean_difference_db': 0.7,
            'rms_difference_db': 1.2,
            'level_offset_db': 0.5,
            'shape_rms_db': 0.9,
            'valid_points': 3,
            'total_grid_points': 3,
            'measurement_a': {'measurement_id': 'ma'},
            'measurement_b': {'measurement_id': 'mb'},
            'intended_changes': [],
            'confounders': [],
            'interpretation_warnings': [],
        },
    }
    payload = build_report_payload(
        {'id': 'project-1', 'name': 'Living room'}, comparison
    )
    html = render_report_html(payload)
    assert '@media print' in html
    assert 'background:#fff' in html
    assert 'break-inside:avoid' in html


def test_analysis_html_has_print_rules() -> None:
    from htdt.analysis_export import (
        AnalysisSeries,
        AnalysisSeriesPoint,
        build_analysis_export,
        render_analysis_html,
    )

    bundle = build_analysis_export(
        document_id='doc-1',
        title='Seat comparison export',
        generated_at_utc='2026-09-28T00:00:00+00:00',
        series=(
            AnalysisSeries(
                series_id='predicted-seat-1',
                label='Predicted seat 1',
                value_class='predicted',
                points=(AnalysisSeriesPoint(x=40.0, y=91.2),),
                source_kind='prediction',
                source_id='pred-1',
                source_sha256='b' * 64,
            ),
        ),
    )
    html = render_analysis_html(bundle)
    assert '@media print' in html


def test_installation_report_html_has_print_rules(tmp_path: Path) -> None:
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.cad_system_variant import build_system_variant
    from htdt.report import (
        build_installation_output,
        render_installation_report_html,
    )

    repository = SceneRepository(tmp_path / 'scene.sqlite3')
    saved = repository.save(make_f1_scene(), parent_revision_id=None)
    variant = build_system_variant(
        baseline=saved.revision,
        name='installation candidate',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc='2026-09-19T07:00:00+00:00',
    )
    output = build_installation_output(saved.revision, variant=variant)
    html = render_installation_report_html(
        output, exported_at_utc='2026-09-19T07:01:00+00:00'
    )
    assert '@media print' in html
    assert 'background:#fff' in html
