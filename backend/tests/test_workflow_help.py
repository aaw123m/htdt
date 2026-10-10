"""Offscreen Qt coverage for workflow_help.HelpDialog — round 2.

The help dialog's shortcut listing is generated from the live
``CommandRegistry``: it must list exactly the commands that carry shortcuts
and fall back to an explicit empty-state line when none do.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from htdt.command_registry import CommandDefinition, CommandRegistry
from htdt.help_registry import (
    HelpTopic,
    LocalizedTopicContent,
    build_help_registry,
)
from htdt.localization import PresentationLocale
from htdt.workflow_help import GlossaryDialog, HelpDialog


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


def test_topic_related_commands_render_display_names() -> None:
    """REV27: 関連操作 rendered raw command ids (``navigation.room``) —
    with a registry the labels resolve to display names. #1024 turned them
    into real launch buttons; unknown ids degrade to plain text + reason."""
    _app()
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='navigation.room', display_name='部屋'),
        execute=lambda: None,
    )
    registry.register(
        CommandDefinition(command_id='project.save', display_name='保存'),
        execute=lambda: None,
    )
    topic = HelpTopic(
        topic_id='room.overview',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(
                title='部屋の操作', summary='要約'
            )
        },
        related_commands=(
            'navigation.room', 'project.save', 'unknown.command'
        ),
    )
    dialog = HelpDialog.topic(topic, command_registry=registry)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert '関連操作' in texts
    buttons = {b.text() for b in dialog.findChildren(QPushButton)}
    assert {'部屋', '保存'} <= buttons
    assert any(
        'unknown.command' in text and '利用できません' in text
        for text in texts
    )


def test_glossary_related_topics_are_clickable_links() -> None:
    """REV34-HELPDESK: 関連ヘルプ must be reachable, not just named.

    A text-only related-topics line forced the user to re-type the title
    into the palette — effectively a dead-end affordance. Each related
    topic now renders an ``htdt-topic:`` anchor wired to the dialog's
    topic opener.
    """
    _app()
    registry = build_help_registry()
    dialog = GlossaryDialog(registry)
    anchors = [
        label.text()
        for label in dialog.findChildren(QLabel)
        if 'htdt-topic:' in label.text()
    ]
    expected = {
        topic_id
        for term in registry.glossary()
        for topic_id in term.related_topics
        if registry.get(topic_id) is not None
    }
    rendered = {
        part.split('htdt-topic:', 1)[1].split('"', 1)[0]
        for text in anchors
        for part in text.split('<a href=')[1:]
    }
    assert rendered == expected, (
        f'related topics without a rendered link: {expected - rendered}'
    )


def test_glossary_link_opens_the_bound_help_topic(monkeypatch) -> None:
    """A 関連ヘルプ click resolves the topic id and opens its HelpDialog."""
    _app()
    registry = build_help_registry()
    dialog = GlossaryDialog(registry)
    opened: list[str] = []

    def _fake_topic(topic, **kwargs):
        opened.append(topic.topic_id)

        class _FakeDialog:
            def exec(self) -> int:
                return 0

            def deleteLater(self) -> None:
                pass

        return _FakeDialog()

    monkeypatch.setattr(HelpDialog, 'topic', staticmethod(_fake_topic))
    dialog._open_related_topic('htdt-topic:workflow.measurements')
    assert opened == ['workflow.measurements']
    # Unknown/foreign links are ignored, never crash.
    dialog._open_related_topic('htdt-topic:no.such')
    dialog._open_related_topic('https://example.com')
    assert opened == ['workflow.measurements']
