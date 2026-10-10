"""Issue #1024 — scrollable HelpDialog body + related-operation affordances.

Before this fix ``HelpDialog`` stacked wrapped QLabels under a plain
QVBoxLayout with ``resize(480, 360)``: long topics (10+ sections) and the
40+ row shortcut reference overflowed the window on low-resolution or
high-DPI displays with no way to reach the bottom, and ``related_commands``
rendered as dead-end plain text. These tests pin:

* a fixed heading with every body line inside a QScrollArea that keyboard
  PageUp/PageDown/Home/End (and the arrows) can walk end to end;
* a DPI-aware size cap so the dialog can never exceed the hosting screen;
* related commands as buttons resolved through the live CommandRegistry —
  current availability, localized disabled reasons, dispatch through
  ``CommandRegistry.execute`` (the freeze gate / deep-link handler / error
  handler path, never straight from help text);
* related topics as ``htdt-topic:`` links (the GlossaryDialog convention),
  with unknown commands/topics degrading to plain text + reason.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QScrollArea,
)

from htdt.command_registry import (
    CommandAvailability,
    CommandDefinition,
    CommandRegistry,
    register_default_commands,
)
from htdt.availability_reasons import availability_reason
from htdt.help_registry import (
    HelpRegistry,
    HelpSection,
    HelpTopic,
    LocalizedTopicContent,
    build_help_registry,
)
from htdt.localization import PresentationLocale
from htdt.workflow_help import HelpDialog
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _long_topic(
    sections: int = 12,
    *,
    related_commands: tuple[str, ...] = (),
    related_topics: tuple[str, ...] = (),
) -> HelpTopic:
    body = '長い本文。' * 60
    return HelpTopic(
        topic_id='test.long',
        content={
            PresentationLocale.JAPANESE: LocalizedTopicContent(
                title='長いトピック',
                summary='概要',
                sections=tuple(
                    HelpSection(heading=f'節{i}', body=body)
                    for i in range(sections)
                ),
            ),
            PresentationLocale.ENGLISH: LocalizedTopicContent(
                title='Long topic',
                summary='Summary',
                sections=tuple(
                    HelpSection(
                        heading=f'Section {i}',
                        body='Long body text. ' * 60,
                    )
                    for i in range(sections)
                ),
            ),
        },
        related_commands=related_commands,
        related_topics=related_topics,
    )


def _many_shortcuts_registry(count: int = 40) -> CommandRegistry:
    registry = CommandRegistry()
    for index in range(count):
        registry.register(
            CommandDefinition(
                command_id=f'test.command_{index:02d}',
                display_name=f'操作{index:02d}',
                shortcut=f'Ctrl+Alt+{index}',
            ),
            execute=lambda: None,
        )
    return registry


def _scroll_area(dialog: HelpDialog) -> QScrollArea:
    areas = dialog.findChildren(QScrollArea)
    assert len(areas) == 1
    return areas[0]


def test_long_topic_body_lives_in_a_scroll_area() -> None:
    """The fixed heading stays outside the scroll area; every body line is
    inside it, so low-res/high-DPI windows keep all content reachable."""
    _app()
    dialog = HelpDialog.topic(_long_topic())
    scroll = _scroll_area(dialog)
    heading = dialog.findChildren(QLabel)[0]
    assert heading.text() == '長いトピック'
    # Heading is a direct child of the dialog (fixed), not scrolled content.
    assert heading.parentWidget() is dialog
    host = scroll.widget()
    body_labels = host.findChildren(QLabel)
    # 1 summary + 12 headings + 12 bodies
    assert len(body_labels) == 25
    last = body_labels[-1]
    assert '長い本文' in last.text()


def test_keyboard_keys_walk_the_body_end_to_end() -> None:
    """PageDown/End reach the bottom and Home returns to the top — keyboard
    and screen-reader users are no longer stranded at the first screenful."""
    _app()
    dialog = HelpDialog.topic(_long_topic())
    dialog.show()
    scroll = _scroll_area(dialog)
    bar = scroll.verticalScrollBar()
    QTest.qWaitForWindowExposed(dialog, 2000)
    bar.setValue(bar.minimum())

    QTest.keyClick(scroll, Qt.Key.Key_End)
    assert bar.value() == bar.maximum()
    assert bar.maximum() > 0  # content really overflows

    QTest.keyClick(scroll, Qt.Key.Key_Home)
    assert bar.value() == bar.minimum()

    QTest.keyClick(scroll, Qt.Key.Key_PageDown)
    assert bar.value() > bar.minimum()
    value_after_page = bar.value()
    QTest.keyClick(scroll, Qt.Key.Key_PageUp)
    assert bar.value() < value_after_page

    QTest.keyClick(scroll, Qt.Key.Key_Down)
    assert bar.value() > 0
    dialog.close()


def test_scroll_area_is_a_strong_focus_keyboard_target() -> None:
    _app()
    dialog = HelpDialog.topic(_long_topic())
    scroll = _scroll_area(dialog)
    assert scroll.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert scroll.accessibleName() == 'ヘルプ本文'


def test_forty_shortcuts_fully_reachable() -> None:
    """DoD: a 40-row shortcut reference still scrolls end to end."""
    _app()
    dialog = HelpDialog.shortcuts(_many_shortcuts_registry(40))
    dialog.show()
    scroll = _scroll_area(dialog)
    QTest.qWaitForWindowExposed(dialog, 2000)
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0
    host = scroll.widget()
    last = host.findChildren(QLabel)[-1]
    assert 'Ctrl+Alt+39' in last.text()
    # At the bottom the final row is inside the visible viewport region.
    QTest.keyClick(scroll, Qt.Key.Key_End)
    viewport = scroll.viewport()
    top_left = last.mapTo(viewport, last.rect().topLeft())
    assert 0 <= top_left.y() < viewport.height()
    dialog.close()


def test_size_cap_follows_available_geometry_matrix() -> None:
    """DPI matrix DoD: 480×360 / 640×480 / larger and 200%-text-equivalent
    geometries — the dialog never exceeds the screen."""
    _app()
    dialog = HelpDialog.topic(_long_topic())
    for width, height in (
        (480, 360),   # low-res floor
        (640, 480),
        (960, 540),   # ~200% text on a small display
        (1280, 720),
        (1920, 1080),
        (2560, 1440),
    ):
        dialog._fit_to_screen(QRect(0, 0, width, height))
        cap_w = int(width * 0.9)
        cap_h = int(height * 0.9)
        assert dialog.width() <= cap_w
        assert dialog.height() <= cap_h
        assert dialog.maximumWidth() == max(240, cap_w)
        assert dialog.maximumHeight() == max(200, cap_h)
    # Tiny screens fall back to the minimum, not a collapsed window.
    dialog._fit_to_screen(QRect(0, 0, 200, 150))
    assert dialog.maximumWidth() == 240
    assert dialog.maximumHeight() == 200


def test_related_commands_dispatch_through_registry_gates() -> None:
    """A related-command button runs CommandRegistry.execute — the same
    freeze/availability/deep-link gates every other entry point uses."""
    _app()
    ran: list[str] = []
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='project.save', display_name='保存'),
        execute=lambda: ran.append('project.save'),
    )
    registry.register(
        CommandDefinition(
            command_id='navigation.measurements',
            display_name='測定',
            deep_link=WorkspaceDeepLink(WorkspaceId.MEASUREMENT),
            mutates_managed_data=False,
        ),
    )
    dialog = HelpDialog.topic(
        _long_topic(
            sections=1,
            related_commands=('project.save', 'navigation.measurements'),
        ),
        command_registry=registry,
    )
    buttons = {
        button.text(): button for button in dialog.findChildren(QPushButton)
    }
    buttons['保存'].click()
    assert ran == ['project.save']
    # Successful dispatch closes help so the result/workspace is visible.
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_related_command_deep_link_navigates() -> None:
    """DoD: a link inside the help text reaches the real workspace — the
    navigation goes through the registry's deep-link handler, so the shell's
    navigation history still records it."""
    _app()
    navigated: list[WorkspaceDeepLink] = []
    registry = CommandRegistry(deep_link_handler=navigated.append)
    registry.register(
        CommandDefinition(
            command_id='navigation.measurements',
            display_name='測定',
            deep_link=WorkspaceDeepLink(
                WorkspaceId.MEASUREMENT, section='import'
            ),
            mutates_managed_data=False,
        ),
    )
    dialog = HelpDialog.topic(
        _long_topic(
            sections=1, related_commands=('navigation.measurements',)
        ),
        command_registry=registry,
    )
    (button,) = [
        b for b in dialog.findChildren(QPushButton) if b.text() == '測定'
    ]
    assert button.isEnabled()
    button.click()
    assert navigated == [
        WorkspaceDeepLink(WorkspaceId.MEASUREMENT, section='import')
    ]
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_blocked_commands_stay_disabled_with_reason() -> None:
    """DoD: a blocked operation renders disabled with its localized reason —
    never a clickable dead end."""
    _app()
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='project.save', display_name='保存'),
        execute=lambda: None,
        availability=lambda: CommandAvailability.blocked(
            availability_reason('project.save.unavailable_or_busy')
        ),
    )
    dialog = HelpDialog.topic(
        _long_topic(sections=1, related_commands=('project.save',)),
        command_registry=registry,
    )
    button = next(
        b for b in dialog.findChildren(QPushButton) if b.text() == '保存'
    )
    assert not button.isEnabled()
    reason = '保存する変更がないか、候補生成・REW読み込み・編集操作が実行中です'
    assert button.toolTip() == reason
    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert reason in labels


def test_frozen_mutations_disable_related_commands() -> None:
    """The data-mutation freeze gate applies inside help exactly like in
    the palette — a mutating command is disabled with the freeze reason."""
    _app()
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='project.save', display_name='保存'),
        execute=lambda: None,
    )
    registry.freeze_data_mutations()
    dialog = HelpDialog.topic(
        _long_topic(sections=1, related_commands=('project.save',)),
        command_registry=registry,
    )
    button = next(
        b for b in dialog.findChildren(QPushButton) if b.text() == '保存'
    )
    assert not button.isEnabled()
    assert 'データ処理中はデータを変更できません' in button.toolTip()


def test_unknown_command_degrades_to_plain_text_with_reason() -> None:
    """DoD: a command id the registry does not know is plain text plus an
    unavailable reason — not a button that does nothing."""
    _app()
    dialog = HelpDialog.topic(
        _long_topic(sections=1, related_commands=('ghost.command',)),
        command_registry=CommandRegistry(),
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any(
        'ghost.command' in text and '利用できません' in text
        for text in texts
    )
    # No enabled button was created for it.
    assert all(
        'ghost' not in button.text()
        for button in dialog.findChildren(QPushButton)
    )


def test_no_registry_means_plain_text_rows() -> None:
    """Without a dispatcher (e.g. an embedded help fallback) related
    commands stay plain text with an explicit no-launch reason."""
    _app()
    dialog = HelpDialog.topic(
        _long_topic(sections=1, related_commands=('project.save',)),
        command_registry=None,
    )
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any(
        'project.save' in text and '実行できません' in text
        for text in texts
    )


def test_related_topics_render_htdt_topic_links() -> None:
    """related_topics use the same htdt-topic: anchors as GlossaryDialog;
    a missing topic degrades to plain text + unavailable reason."""
    _app()
    registry = HelpRegistry(
        [
            _long_topic(sections=1, related_topics=('other.topic', 'gone.topic')),
            HelpTopic(
                topic_id='other.topic',
                content={
                    PresentationLocale.JAPANESE: LocalizedTopicContent(
                        title='別のトピック', summary='要約'
                    )
                },
            ),
        ]
    )
    dialog = HelpDialog.topic(
        registry.require('test.long'), help_registry=registry
    )
    related = [
        label.text()
        for label in dialog.findChildren(QLabel)
        if label.text().startswith('関連ヘルプ')
    ]
    (related,) = related
    assert '<a href="htdt-topic:other.topic">別のトピック</a>' in related
    assert 'gone.topic（利用できません）' in related


def test_related_topic_link_opens_nested_topic(monkeypatch) -> None:
    """Clicking an htdt-topic: link opens the bound topic nested, carrying
    locale + registries through — closing it returns to this dialog."""
    _app()
    registry = HelpRegistry(
        [
            _long_topic(sections=1, related_topics=('other.topic',)),
            HelpTopic(
                topic_id='other.topic',
                content={
                    PresentationLocale.JAPANESE: LocalizedTopicContent(
                        title='別のトピック', summary='要約'
                    )
                },
            ),
        ]
    )
    dialog = HelpDialog.topic(
        registry.require('test.long'),
        command_registry=CommandRegistry(),
        help_registry=registry,
    )
    opened: list[tuple[str, dict]] = []

    def _fake_topic(topic, **kwargs):
        opened.append((topic.topic_id, kwargs))

        class _FakeDialog:
            def exec(self) -> int:
                return 0

            def deleteLater(self) -> None:
                pass

        return _FakeDialog()

    monkeypatch.setattr(HelpDialog, 'topic', staticmethod(_fake_topic))
    dialog._open_related_topic('htdt-topic:other.topic')
    assert [topic_id for topic_id, _ in opened] == ['other.topic']
    (_, kwargs), = opened
    assert kwargs['help_registry'] is registry
    assert kwargs['locale'] is PresentationLocale.JAPANESE
    # Unknown / foreign links are ignored, never crash.
    dialog._open_related_topic('htdt-topic:no.such')
    dialog._open_related_topic('https://example.com')
    assert len(opened) == 1


def test_english_locale_wraps_and_localizes_chrome() -> None:
    """JA/EN wrap DoD: body labels wrap, chrome strings follow locale."""
    _app()
    dialog = HelpDialog.topic(
        _long_topic(sections=2, related_commands=('ghost.cmd',)),
        locale=PresentationLocale.ENGLISH,
        command_registry=CommandRegistry(),
    )
    labels = dialog.findChildren(QLabel)
    assert all(label.wordWrap() for label in labels)
    assert dialog._scroll_area.accessibleName() == 'Help body'
    close = next(
        b for b in dialog.findChildren(QPushButton) if b.text() == 'Close'
    )
    assert close.isEnabled()
    texts = [label.text() for label in labels]
    assert any('unavailable' in text for text in texts)


def test_real_registry_topic_every_related_command_renders() -> None:
    """The shipped help registry + shipped commands: every related_commands
    entry renders either an enabled button or a reasoned disabled/plain
    row — nothing silently drops."""
    _app()
    help_registry = build_help_registry()
    commands = CommandRegistry()
    register_default_commands(commands)
    for topic_id in help_registry.topic_ids():
        topic = help_registry.require(topic_id)
        if not topic.related_commands:
            continue
        dialog = HelpDialog.topic(
            topic,
            command_registry=commands,
            help_registry=help_registry,
        )
        rendered = '\n'.join(
            [b.text() for b in dialog.findChildren(QPushButton)]
            + [l.text() for l in dialog.findChildren(QLabel)]
        )
        for command_id in topic.related_commands:
            definition = commands.definition(command_id)
            assert (
                definition.display_name in rendered
                or command_id in rendered
            ), f'{command_id} of {topic_id} not rendered'
