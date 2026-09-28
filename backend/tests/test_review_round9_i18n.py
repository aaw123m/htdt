"""Round 9 regression tests: user-facing Qt strings stay Japanese (#i18n).

The product's string convention is hardcoded Japanese literals. These tests
pin the surfaces that regressed in the past — recovery dialogs, launch
reasons, and the dock-title lookup tables that drifted out of sync with the
editors' Japanese titles.
"""

from __future__ import annotations

import os
import re

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.native_cad import _RECOVERY_CHOICE_PRESENTATION
from htdt.startup_recovery import RecoveryMetadata, decide_launch
from htdt.workflow_legacy_bridge import _CONTEXT_DOCK_TITLES

_ASCII_WORD = re.compile(r'[A-Za-z]{2,}')


def _english_free(value: str) -> bool:
    return _ASCII_WORD.search(value) is None


def test_recovery_choice_labels_are_japanese() -> None:
    for choice, (label, _style) in _RECOVERY_CHOICE_PRESENTATION.items():
        assert _english_free(label), f'{choice} label is not Japanese: {label!r}'


def test_launch_decision_reasons_are_japanese() -> None:
    decision = decide_launch(
        unclean_previous_session=True,
        metadata=RecoveryMetadata(),
        build_id='b1',
        renderer_failure_detected=True,
    )
    assert decision.reasons
    for reason in decision.reasons:
        assert _english_free(reason), f'launch reason is not Japanese: {reason!r}'

    safe = decide_launch(
        unclean_previous_session=False,
        metadata=RecoveryMetadata(),
        build_id='b1',
        explicit_safe_mode=True,
    )
    for reason in safe.reasons:
        assert _english_free(reason), f'safe-mode reason is not Japanese: {reason!r}'


def test_legacy_bridge_dock_titles_match_editor_japanese() -> None:
    # The bridge once looked up English 'Room'/'Inspector' while the editors
    # created '部屋'/'インスペクター' — the highlights silently no-opped.
    room_titles = {
        title
        for titles in _CONTEXT_DOCK_TITLES.values()
        for group in titles.values()
        for title in group
    }
    assert '部屋' in room_titles
    assert 'インスペクター' in room_titles
    assert 'Room' not in room_titles
    assert 'Inspector' not in room_titles
    for title in room_titles:
        assert _english_free(title), f'dock title is not Japanese: {title!r}'


def test_stock_dialog_buttons_translate_to_japanese() -> None:
    # Qt's own chrome (OK/Cancel on QMessageBox/QInputDialog, file-dialog
    # buttons) comes from Qt's bundled catalogs, not authored literals —
    # apply_dark_theme must install the Japanese translator so stock
    # buttons don't render English inside a Japanese product.
    from PySide6.QtWidgets import QApplication, QMessageBox

    from htdt.ui_theme import apply_dark_theme

    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)

    box = QMessageBox()
    box.addButton(QMessageBox.StandardButton.Cancel)
    assert box.button(QMessageBox.StandardButton.Cancel).text() == (
        'キャンセル'
    )
