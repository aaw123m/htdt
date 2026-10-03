"""REV34-DIALOGUX: dialog keyboard/focus sanity.

Most-used dialogs must open with focus on a field the user can immediately
type into — a QTabWidget claiming initial focus leaves arrow keys switching
tabs instead of editing.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, SceneDocument
from htdt.playback_chain_widgets import PlaybackChainDialog, PlaybackChainService


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_playback_chain_dialog_opens_on_first_field(tmp_path) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(
        SceneDocument(document_id=F1_DOCUMENT_ID, room=None, entities=()),
        parent_revision_id=None,
    )
    dialog = PlaybackChainDialog(
        PlaybackChainService(repository, F1_DOCUMENT_ID)
    )
    dialog.show()
    app.processEvents()
    assert dialog.focusWidget() is dialog.amp_label

    dialog.close()
    dialog.deleteLater()
    app.processEvents()
