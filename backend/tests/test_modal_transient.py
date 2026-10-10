"""REV-UI regression: zombie modal dialogs.

A parented ``QDialog``/``QMessageBox`` that is ``exec()``'d and dropped
stays a hidden top-level window forever — nothing deletes it. Verified
empirically on this build: each ``FirstRunWizardDialog`` invocation left
one more stray in ``QApplication.topLevelWidgets()`` until the shell
died; a plain ``QMessageBox(parent).exec()`` leaked two.

The fix is ``htdt.modal_transient.exec_transient`` at every single-shot
``exec`` site, and one ``deleteLater`` after each loop that re-``exec``'s
the same box (a per-exec deferred delete could be delivered inside the
next nested ``exec`` pass — that crash class is why the loop sites are
NOT routed through the helper).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMessageBox,
    QWidget,
)
from shiboken6 import isValid

from htdt.modal_transient import exec_transient
from htdt.operation_error_dialog import warn_user


@pytest.fixture
def qapp():
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture
def host(qapp):
    widget = QWidget()
    yield widget
    if isValid(widget):
        widget.deleteLater()
        qapp.sendPostedEvents(widget, QEvent.Type.DeferredDelete)


def _drain_deferred(app) -> None:
    """Deliver queued deferred deletes (typed sweep — the unfiltered
    variant does not dispatch DeferredDelete on this Qt build)."""
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _accept(self) -> int:
    """Offscreen stand-in for a modal exec: hide and report Accepted."""
    self.hide()
    return QDialog.DialogCode.Accepted


def test_exec_transient_destroys_dialog(qapp, host, monkeypatch):
    """One-shot exec must not leave the dialog in topLevelWidgets."""
    monkeypatch.setattr(QDialog, "exec", _accept)
    before = len(qapp.topLevelWidgets())

    dialog = QDialog(host)
    dialog.setModal(True)
    result = exec_transient(dialog)

    assert result == QDialog.DialogCode.Accepted
    # The object stays valid through the caller's post-exec reads; the
    # delete is queued, not synchronous.
    assert isValid(dialog)
    _drain_deferred(qapp)
    assert not isValid(dialog)
    assert len(qapp.topLevelWidgets()) == before


def test_exec_transient_returns_reject_result(qapp, host, monkeypatch):
    monkeypatch.setattr(
        QDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    dialog = QDialog(host)
    assert exec_transient(dialog) == QDialog.DialogCode.Rejected
    _drain_deferred(qapp)
    assert not isValid(dialog)


def test_warn_user_destroys_box_after_help_loop(
    qapp, host, monkeypatch
):
    """The help-button re-exec loop must delete the box exactly once,
    after the loop — not inside a nested exec pass."""
    created = []
    clicks = iter(["help", "ok"])

    def _fake_exec(self):
        if self not in created:
            created.append(self)
        which = next(clicks)
        if which == "help":
            button = next(
                b
                for b in self.buttons()
                if self.buttonRole(b) == QMessageBox.ButtonRole.HelpRole
            )
        else:
            button = self.button(QMessageBox.StandardButton.Ok)
        self.clickedButton = lambda: button
        self.hide()
        return 0

    monkeypatch.setattr(QDialog, "exec", _fake_exec)

    helped = []
    retried = []
    before = len(qapp.topLevelWidgets())

    error = warn_user(
        host,
        "テスト失敗",
        ValueError("boom"),
        on_help=helped.append,
        on_retry=lambda: retried.append(1),
    )

    # Help pass re-execs the SAME box: one exec for the help click, one
    # for the OK click — and only one box was ever created.
    assert len(created) == 1
    assert len(helped) == 1 and helped[0] == error.code
    box = created[0]
    _drain_deferred(qapp)
    assert not isValid(box)
    assert len(qapp.topLevelWidgets()) == before
