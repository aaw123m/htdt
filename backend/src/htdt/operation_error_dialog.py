"""Operator-facing error dialog (#807 boundary refactor).

``warn_user`` is the Qt ``QMessageBox`` presenter for
``user_facing_error``'s domain-side error mapping. It lives here so that
``user_facing_error`` stays a Qt-free domain module; every caller is a UI
layer widget/dialog already.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .user_facing_error import (
    RETRYABLE_ERROR_CODES,
    log_operation_error,
    to_user_facing_error,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from .user_facing_error import UserFacingError


def warn_user(
    parent: "QWidget | None",
    title: str,
    exc: BaseException,
    *,
    effect: str | None = None,
    on_retry: Callable[[], None] | None = None,
    retry_label: str | None = None,
    on_help: Callable[[str], None] | None = None,
) -> UserFacingError:
    """Present one operation failure as a warning dialog.

    Visible text is the mapped, localized message plus its recovery hint and
    optional effect line — never raw exception text. The exception's class
    and message are preserved under the dialog's Details expander and in the
    diagnostics log.

    ``on_retry`` adds a retry button only when the failure class is actually
    retryable (``RETRYABLE_ERROR_CODES``) — the dialog never offers a second
    attempt it knows cannot succeed differently. The callback runs after the
    dialog closes; its own failure surfaces through the same error channel.

    ``on_help`` (REV32-TERMS) adds a ヘルプ button that opens the topic
    bound to the error's code — it receives the code and the warning box
    re-shows afterwards, so the operator can read the explanation and then
    still choose OK/retry.
    """
    from PySide6.QtWidgets import QMessageBox

    error = to_user_facing_error(exc, title=title, effect=effect)
    log_operation_error(error, exc)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    text = error.message
    if error.recovery:
        text += f'\n{error.recovery}'
    if error.effect:
        text += f'\n{error.effect}'
    box.setText(text)
    if error.technical_detail:
        box.setDetailedText(error.technical_detail)
    retry_button = None
    if on_retry is not None and error.code in RETRYABLE_ERROR_CODES:
        box.addButton(QMessageBox.StandardButton.Ok)
        retry_button = box.addButton(
            retry_label or '再試行', QMessageBox.ButtonRole.ApplyRole
        )
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
    help_button = None
    if on_help is not None:
        if not box.buttons():
            # A HelpRole button alone leaves the box with no way out.
            box.addButton(QMessageBox.StandardButton.Ok)
            box.setDefaultButton(QMessageBox.StandardButton.Ok)
        help_button = box.addButton(
            'ヘルプ', QMessageBox.ButtonRole.HelpRole
        )
    while True:
        box.exec()
        if help_button is not None and box.clickedButton() is help_button:
            on_help(error.code)
            continue
        break
    # Transient box re-shown in a loop: delete once it exits (a per-exec
    # deleteLater could be delivered inside the next nested exec pass).
    box.deleteLater()
    if retry_button is not None and box.clickedButton() is retry_button:
        on_retry()
    return error

__all__ = ['warn_user']
