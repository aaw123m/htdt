from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

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

    def __init__(self, _repository) -> None:
        pass

    def resolve_startup_document(self, document_id: str | None):
        return SimpleNamespace(
            document_id=document_id or 'default-project-document'
        )


class _FakeWindow:
    def __init__(self, *_args, **_kwargs) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True


def _stub_gui(monkeypatch, *, workflow_cls=_FakeWindow, legacy_cls=_FakeWindow):
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
        'OptimizationWorkspaceWindow',
        lambda repository, document_id: created.setdefault('legacy', legacy_cls(repository, document_id)),
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

    assert 'workflow' in created and 'legacy' not in created
    assert created['workflow'].shown
    assert app.theme_applied


def test_legacy_ui_flag_selects_legacy_composition(tmp_path: Path, monkeypatch) -> None:
    app, created = _stub_gui(monkeypatch)

    assert native_cad.main(['--data-dir', str(tmp_path), '--legacy-ui']) == 0

    assert 'legacy' in created and 'workflow' not in created
    assert created['legacy'].shown
    # The bundled dark theme applies to every composition, not just the shell.
    assert app.theme_applied


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


def _stub_recovery(monkeypatch, label: str):
    """Force the recovery offer and auto-click ``label`` in the dialog."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    import htdt.startup_recovery as startup_recovery

    # The dialog is a real widget: a real QApplication must exist even
    # though _run_gui's own construction is faked.
    _real_app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        startup_recovery, 'decide_launch', lambda **_: _recovery_decision()
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
    class _Window:
        def __init__(self, *_args, **_kwargs) -> None:
            self.shown = False
            self.navigations: list = []
            self.workflow_application = SimpleNamespace(**app_stubs)

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
            resolve_startup_document=lambda document_id: SimpleNamespace(
                document_id=document_id or 'doc-1', project_id='proj-xyz'
            )
        ),
    )

    assert native_cad.main(['--data-dir', str(tmp_path)]) == 0

    from htdt.startup_recovery import load_recovery_metadata

    record = load_recovery_metadata(tmp_path).records[-1]
    assert record.last_project_ref == 'proj-xyz'
