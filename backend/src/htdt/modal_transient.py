"""exec-and-dispose for one-shot modal dialogs and message boxes.

A parented ``QDialog``/``QMessageBox`` survives ``exec()`` as a hidden
top-level window: nothing ever deletes it, so every locally constructed
modal leaks a window for the rest of the session (verified: each first-
run wizard invocation left one more ``FirstRunWizardDialog`` in
``QApplication.topLevelWidgets()`` until the shell died).
``exec_transient`` execs the dialog and then queues its deferred
deletion — the object stays valid through the caller's post-``exec``
reads (``clickedButton()``, form state, chosen values) and is destroyed
on the next event-loop pass instead of never.

Dialogs re-``exec()``ed in a loop must NOT go through this helper — a
deferred delete queued after the first pass can be delivered inside the
next nested ``exec`` loop. Queue ``deleteLater`` once the loop exits.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog


def exec_transient(dialog: QDialog) -> int:
    """exec ``dialog`` once, then delete it; returns the exec result."""
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()
