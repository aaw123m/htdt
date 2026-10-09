from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import htdt.native_cad as native_cad
from PySide6.QtCore import QObject


class _FakeApplication(QObject):
    """QObject-backed stand-in: native_cad passes the app as the receiver
    of QTimer.singleShot, which requires a real QObject instance."""

    def __init__(self, _argv: list[str]) -> None:
        super().__init__()
        self.theme_applied = False

    def setApplicationVersion(self, _version: str) -> None:
        return None

    def setWindowIcon(self, _icon) -> None:
        return None

    def exec(self) -> int:
        return 0


class _FakeProjectLibraryRepository:
    """Schema-bypassing stand-in: the real library verifies the migrated
    schema, which these tests never create."""

    calls: list[dict] = []

    def __init__(self, _repository) -> None:
        type(self).calls = []

    def resolve_startup_document(
        self, document_id: str | None, *, skip_last_opened: bool = False
    ):
        type(self).calls.append(
            {'document_id': document_id, 'skip_last_opened': skip_last_opened}
        )
        return SimpleNamespace(
            document_id=document_id or 'default-project-document'
        )


class _FakeWindow:
    def __init__(self, *_args, **_kwargs) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True


def _stub_gui(monkeypatch, *, workflow_cls=_FakeWindow):
    app = _FakeApplication(['htdt'])
    created: dict[str, object] = {}
    monkeypatch.setattr(native_cad, 'QApplication', lambda _argv: app)
    monkeypatch.setattr(
        native_cad, 'apply_dark_theme', lambda _app: setattr(app, 'theme_applied', True)
    )
    monkeypatch.setattr(
        native_cad,
        'build_workflow_shell',
        lambda repository, document_id, project_library=None, **_kwargs: created.setdefault('workflow', workflow_cls(repository, document_id)),
    )
    monkeypatch.setattr(
        native_cad,
        'ProjectLibraryRepository',
        _FakeProjectLibraryRepository,
    )
    return app, created


def test_default_launch_builds_workflow_shell(tmp_path: Path, monkeypatch) -> None:
    app, created = _stub_gui(monkeypatch)

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0

    assert 'workflow' in created
    assert created['workflow'].shown
    assert app.theme_applied
    # Normal launches keep the most-recent-project behavior.
    assert _FakeProjectLibraryRepository.calls[-1]['skip_last_opened'] is False


def test_legacy_ui_flag_is_retired_with_clear_message(tmp_path: Path, capsys) -> None:
    # REV36-UX140C: the legacy QMainWindow composition is gone; the flag is
    # still accepted so stale shortcuts fail with a JA explanation (exit 2)
    # rather than an opaque argparse "unrecognized arguments" error.
    with pytest.raises(SystemExit) as excinfo:
        native_cad.main(['--data-dir', str(tmp_path), '--legacy-ui'])

    assert excinfo.value.code == 2
    assert 'ワークフローシェル構成のみで起動します' in capsys.readouterr().err


def test_deprecated_workflow_shell_flag_still_accepted(tmp_path: Path, monkeypatch) -> None:
    _app, created = _stub_gui(monkeypatch)

    assert native_cad.main([
        '--data-dir', str(tmp_path), '--workflow-shell',
    ]) == 0

    assert 'workflow' in created


# ---------------------------------------------------------------------------
# Round 8: recovery dialog renders every offered choice + safe-mode wiring.


def _recovery_decision(**overrides):
    from htdt.startup_recovery import LaunchDecision, SafeModePolicy

    base = dict(
        mode='recovery_offered',
        reasons=('previous session ended unexpectedly',),
        choices=(
            'open_normal',
            'open_safe_mode',
            'open_diagnostics',
            'verify_data',
            'choose_another_project',
        ),
        restore_recommended=True,
        safe_mode_policy=SafeModePolicy(),
    )
    base.update(overrides)
    return LaunchDecision(**base)


def _auto_click(label: str):
    """QMessageBox.exec double: clicks the button with ``label``."""

    def _exec(box) -> int:
        for button in box.buttons():
            if button.text() == label:
                button.click()
                return 0
        return 0

    return _exec


def _stub_recovery(monkeypatch, label: str, **overrides):
    """Force the recovery offer and auto-click ``label`` in the dialog."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    import htdt.startup_recovery as startup_recovery

    # The dialog is a real widget: a real QApplication must exist even
    # though _run_gui's own construction is faked.
    _real_app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        startup_recovery,
        'decide_launch',
        lambda **_: _recovery_decision(**overrides),
    )
    monkeypatch.setattr(QMessageBox, 'exec', _auto_click(label))
    monkeypatch.setattr(
        QMessageBox, 'information', staticmethod(lambda *a, **k: 0)
    )


class _FakeCaptureReceiver:
    constructed = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).constructed += 1

    def start_if_requested(self):
        return None

    def shutdown(self) -> None:
        return None


def _spy_capture(monkeypatch):
    import htdt.capture_receiver_controller as receiver_module

    _FakeCaptureReceiver.constructed = 0
    monkeypatch.setattr(
        receiver_module, 'CaptureReceiverController', _FakeCaptureReceiver
    )
    return _FakeCaptureReceiver


def _fake_window_with_app(**app_stubs):
    # #886's autoshow hook is unconditional in _run_gui — the double must
    # carry it like the real WorkflowApplication does.
    stubs = {'enable_first_run_wizard_autoshow': lambda: None}
    stubs.update(app_stubs)

    class _Window:
        def __init__(self, *_args, **_kwargs) -> None:
            self.shown = False
            self.navigations: list = []
            self.workflow_application = SimpleNamespace(**stubs)

        def show(self) -> None:
            self.shown = True

        def navigate(self, workspace_id) -> bool:
            self.navigations.append(workspace_id)
            return True

    return _Window


def test_safe_mode_choice_skips_capture_and_marks_safe_mode(
    tmp_path: Path, monkeypatch
) -> None:
    captured_kwargs: dict = {}
    backups: list = []
    window_cls = _fake_window_with_app(
        start_automatic_backup=lambda: backups.append(1)
    )

    def _build(repository, document_id, project_library=None, **kwargs):
        captured_kwargs.update(kwargs)
        return window_cls()

    _stub_gui(monkeypatch)
    monkeypatch.setattr(native_cad, 'build_workflow_shell', _build)
    _stub_recovery(monkeypatch, 'セーフモードで開く')
    receiver = _spy_capture(monkeypatch)

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0
    assert captured_kwargs.get('safe_mode') is True
    assert receiver.constructed == 0
    # Background jobs stay off under the Safe Mode policy.
    assert backups == []
    # auto_open_last_project=False reaches the startup resolver (#739 r9).
    assert _FakeProjectLibraryRepository.calls[-1]['skip_last_opened'] is True

    from htdt.startup_recovery import load_recovery_metadata

    record = load_recovery_metadata(tmp_path).records[-1]
    assert record.launch_mode == 'safe_mode'
    assert record.clean_exit is True


def test_explicit_safe_mode_flag_skips_dialog_and_capture(
    tmp_path: Path, monkeypatch
) -> None:
    captured_kwargs: dict = {}

    class _Window:
        shown = False

        def show(self) -> None:
            self.shown = True

    def _build(repository, document_id, project_library=None, **kwargs):
        captured_kwargs.update(kwargs)
        return _Window()

    _stub_gui(monkeypatch)
    monkeypatch.setattr(native_cad, 'build_workflow_shell', _build)
    receiver = _spy_capture(monkeypatch)
    # No QMessageBox patching: an explicit safe-mode launch must never show
    # the recovery dialog at all (exec() would block the test run).

    assert (
        native_cad.main(
            ['--data-dir', str(tmp_path), '--safe-mode']
        )
        == 0
    )
    assert captured_kwargs.get('safe_mode') is True
    assert receiver.constructed == 0
    assert _FakeProjectLibraryRepository.calls[-1]['skip_last_opened'] is True


def test_verify_data_choice_opens_data_management(
    tmp_path: Path, monkeypatch
) -> None:
    opened: list = []
    backups: list = []
    window_cls = _fake_window_with_app(
        settings_dialog=SimpleNamespace(
            open_settings=lambda: opened.append('settings')
        ),
        start_automatic_backup=lambda: backups.append(1),
    )
    _stub_gui(monkeypatch, workflow_cls=window_cls)
    _stub_recovery(monkeypatch, '今すぐデータを検証')

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0
    assert opened == ['settings']
    assert backups == [1]  # normal mode still kicks the backup tick


def test_choose_another_project_lands_on_projects_destination(
    tmp_path: Path, monkeypatch
) -> None:
    window_cls = _fake_window_with_app(
        start_automatic_backup=lambda: None
    )
    _app, created = _stub_gui(monkeypatch, workflow_cls=window_cls)
    _stub_recovery(monkeypatch, '別のプロジェクトを選択')
    _spy_capture(monkeypatch)

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0
    # Post-launch: the shell navigates to Projects instead of entering the
    # project the failed session was bound to.
    assert created['workflow'].navigations == ['projects']


def test_launch_record_is_annotated_with_project_ref(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_gui(monkeypatch)
    monkeypatch.setattr(
        native_cad,
        'ProjectLibraryRepository',
        lambda _repo: SimpleNamespace(
            resolve_startup_document=lambda document_id, **_: SimpleNamespace(
                document_id=document_id or 'doc-1', project_id='proj-xyz'
            )
        ),
    )

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0

    from htdt.startup_recovery import load_recovery_metadata

    record = load_recovery_metadata(tmp_path).records[-1]
    assert record.last_project_ref == 'proj-xyz'


# ---------------------------------------------------------------------------
# Round 9 #10: second-instance launch forwards an activation intent and
# exits cleanly instead of dead-ending on an error dialog.


def test_second_gui_launch_forwards_activation_and_exits_clean(
    tmp_path: Path, monkeypatch
) -> None:
    from htdt.launch_intents import drain_launch_intents
    from htdt.runtime_instance import SingleInstanceGuard

    notices: list = []
    monkeypatch.setattr(
        native_cad, '_notify_instance_active', lambda _d: notices.append(1)
    )

    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire()
    try:
        assert native_cad.main(['--data-dir', str(tmp_path)]) == 0
    finally:
        guard.release()

    assert notices == [1]
    (queued,) = drain_launch_intents(tmp_path)
    assert queued.intent.kind == 'activate'


def test_second_launch_forwards_open_paths_then_activation(
    tmp_path: Path, monkeypatch
) -> None:
    from htdt.launch_intents import drain_launch_intents
    from htdt.runtime_instance import SingleInstanceGuard

    monkeypatch.setattr(
        native_cad,
        '_notify_instance_active',
        lambda _d: (_ for _ in ()).throw(
            AssertionError('file-open forwarding stays silent')
        ),
    )

    doc = tmp_path / 'room.htdtproject'
    doc.write_text('{"kind": "htdt-project-ref", "schema_version": 1}')

    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire()
    try:
        assert native_cad.main(
            ['--data-dir', str(tmp_path), str(doc)]
        ) == 0
    finally:
        guard.release()

    kinds = [q.intent.kind for q in drain_launch_intents(tmp_path)]
    # The file intent lands first so the document is open when the window
    # comes forward.
    assert kinds == ['open_project', 'activate']


def test_second_launch_falls_back_to_error_when_forward_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import htdt.launch_intents as launch_intents
    from htdt.runtime_instance import SingleInstanceGuard

    def _raise(*_a, **_k):
        raise OSError('queue write failed')

    monkeypatch.setattr(
        launch_intents, 'forward_launch_intent', _raise
    )
    failures: list = []
    monkeypatch.setattr(
        native_cad,
        'report_launch_failure',
        lambda **kwargs: failures.append(kwargs),
    )

    guard = SingleInstanceGuard(tmp_path)
    assert guard.acquire()
    try:
        assert native_cad.main(['--data-dir', str(tmp_path)]) == 2
    finally:
        guard.release()

    assert '使用中' in capsys.readouterr().err
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Round 9 #11: 'restore from backup' on the recovery dialog opens the
# settings surface and previews the newest existing generation.


def _seed_generation(data_dir: Path, name: str) -> Path:
    generations = data_dir.parent / f'{data_dir.name}-backups'
    generations.mkdir(parents=True, exist_ok=True)
    archive = generations / name
    archive.write_bytes(b'generation-bytes')
    return archive


def test_restore_backup_choice_previews_newest_generation(
    tmp_path: Path, monkeypatch
) -> None:
    generation = _seed_generation(
        tmp_path,
        'htdt-backup-manual-20260901T000000Z-abcd1234.htdt-backup',
    )
    older = _seed_generation(
        tmp_path,
        'htdt-backup-automatic_periodic-20260801T000000Z-ef567890.htdt-backup',
    )
    del older

    opened: list = []
    previews: list = []
    window_cls = _fake_window_with_app(
        settings_dialog=SimpleNamespace(
            open_settings=lambda: opened.append('settings')
        ),
        data_management_controller=SimpleNamespace(
            preview_restore=lambda path: previews.append(Path(path))
        ),
        start_automatic_backup=lambda: None,
    )
    _stub_gui(monkeypatch, workflow_cls=window_cls)

    _stub_recovery(
        monkeypatch,
        'バックアップから復元',
        choices=(
            'open_normal',
            'open_safe_mode',
            'open_diagnostics',
            'restore_backup',
            'verify_data',
            'choose_another_project',
        ),
        backup_restore_available=True,
    )

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0
    # Settings surface opens first, then the newest generation previews.
    assert opened == ['settings']
    assert previews == [generation]
