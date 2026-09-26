from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import htdt.native_cad as native_cad


class _FakeApplication:
    def __init__(self, _argv: list[str]) -> None:
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
