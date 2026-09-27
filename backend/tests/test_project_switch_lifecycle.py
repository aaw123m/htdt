"""Round 8: menu-driven project switch lifecycle safety."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from htdt.cad_repository import SceneRepository
from htdt.project_library import ProjectLibraryError
from htdt.workflow_application import WorkflowApplicationComposition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(tmp_path: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


class _FakeReceiver(QObject):
    """Minimal stand-in satisfying CaptureReceiverPanel's read surface."""

    delivery_staged = Signal(object)
    changed = Signal()
    requested_enabled = False
    last_error = None
    service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(port=5000)
    )

    def status_lines(self) -> list[str]:
        return []


def test_switch_to_project_validates_before_closing(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed open must keep the current window — never leave zero windows."""
    app = _app()
    composition = _composition(tmp_path)
    closed: list[bool] = []
    original_close = composition.shell.close
    monkeypatch.setattr(
        composition.shell,
        'close',
        lambda: closed.append(True) or original_close(),
    )
    monkeypatch.setattr(
        QMessageBox, 'warning', staticmethod(lambda *a, **k: 0)
    )

    def _failing_open(_project_id: str):
        raise ProjectLibraryError('entry vanished')

    monkeypatch.setattr(
        composition.project_library, 'open_project', _failing_open
    )
    entry = SimpleNamespace(document_id='doc-2', project_id='proj-2')

    composition._switch_to_project(entry)
    app.processEvents()

    assert closed == []  # window never closed
    assert composition.document_id == 'document-1'


def test_switch_to_project_marks_opened_then_closes(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    opened: list[str] = []
    close_observed_open: list[bool] = []
    original_close = composition.shell.close
    monkeypatch.setattr(
        composition.shell,
        'close',
        lambda: close_observed_open.append(bool(opened)) or original_close(),
    )
    monkeypatch.setattr(
        composition.project_library,
        'open_project',
        lambda project_id: opened.append(project_id)
        or SimpleNamespace(document_id='doc-2', project_id=project_id),
    )
    spawned: list[str] = []
    composition._open_project_callback = spawned.append

    entry = SimpleNamespace(document_id='doc-2', project_id='proj-2')
    composition._switch_to_project(entry)
    app.processEvents()

    assert opened == ['proj-2']
    assert close_observed_open == [True]  # open succeeded before close ran
    assert spawned == ['doc-2']


def test_open_document_carries_capture_receiver_and_preferences(
    tmp_path: Path,
) -> None:
    app = _app()
    receiver = _FakeReceiver()
    composition = _composition(tmp_path, capture_receiver=receiver)
    composition._open_document('doc-2')
    app.processEvents()

    assert len(composition._spawned_compositions) == 1
    spawned = composition._spawned_compositions[0]
    assert spawned.capture_receiver is receiver
    assert spawned.preferences is composition.preferences
    spawned.shell.close()
    app.processEvents()
