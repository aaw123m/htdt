"""Offscreen Qt coverage for workflow_help.HelpDialog — round 2.

The help dialog's shortcut listing is generated from the live
``CommandRegistry``: it must list exactly the commands that carry shortcuts
and fall back to an explicit empty-state line when none do.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QLabel

from htdt.command_registry import CommandDefinition, CommandRegistry
from htdt.workflow_help import HelpDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _shortcuts_dialog(*definitions: CommandDefinition) -> HelpDialog:
    registry = CommandRegistry()
    for definition in definitions:
        registry.register(definition, execute=lambda: None)
    return HelpDialog.shortcuts(registry)


def test_shortcuts_lists_only_commands_with_shortcuts() -> None:
    _app()
    dialog = _shortcuts_dialog(
        CommandDefinition(
            command_id='a', display_name='保存', shortcut='Ctrl+S'
        ),
        CommandDefinition(command_id='b', display_name='シーンに戻る'),
        CommandDefinition(
            command_id='c', display_name='Undo', shortcut='Ctrl+Z'
        ),
    )
    texts = [
        label.text()
        for label in dialog.findChildren(QLabel)
    ]
    body = [text for text in texts if '  ' in text]
    assert 'Ctrl+S  保存' in body
    assert 'Ctrl+Z  Undo' in body
    # The shortcut-less command never appears.
    assert all('シーンに戻る' not in text for text in body)
    assert dialog.windowTitle() == 'キーボードショートカット一覧'


def test_shortcuts_lists_shell_level_shortcuts() -> None:
    """Shell QShortcuts (Ctrl+K / Alt+Left/Right / F1) live outside the
    registry — the reference must still list them (round-13)."""
    _app()
    dialog = _shortcuts_dialog(
        CommandDefinition(command_id='a', display_name='保存')
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert 'Ctrl+K  コマンドパレットを開く' in texts
    assert 'Alt+←  前の画面に戻る' in texts
    assert 'Alt+→  次の画面に進む' in texts
    assert 'F1  ショートカット一覧（ヘルプ）' in texts
    # And a registry command without a shortcut still contributes nothing.
    assert all('保存' not in text for text in texts[1:])


def test_shortcuts_lists_alias_bindings_and_nudge() -> None:
    """Alias shortcuts (Backspace, Ctrl+Shift+Z) are real bindings via
    CommandShortcutBinder — the reference must list them, plus the
    viewport arrow-key nudge that lives outside the registry (round-14)."""
    _app()
    dialog = _shortcuts_dialog(
        CommandDefinition(
            command_id='a',
            display_name='削除',
            shortcut='Delete',
            shortcut_aliases=('Backspace',),
        ),
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert 'Delete  削除' in texts
    assert 'Backspace  削除' in texts
    assert (
        '矢印キー（部屋ビュー）  選択項目をグリッド1ステップ移動（Shiftで10倍）'
        in texts
    )


def test_palette_usage_dialog_has_fixed_content() -> None:
    _app()
    dialog = HelpDialog.palette_usage()
    assert dialog.windowTitle() == 'コマンドパレットの使い方'
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any('Ctrl+K' in text for text in texts)
