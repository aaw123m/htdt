"""Local help surfaces — real destination for Help-topic palette results."""

from __future__ import annotations

import html

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .modal_transient import exec_transient
from .command_registry import CommandRegistry
from .help_registry import HelpRegistry, HelpTopic, shortcut_reference
from .localization import PresentationLocale, term_text
from .ui_theme import TypographyRole, set_typography_role


def _l(locale: PresentationLocale, ja: str, en: str) -> str:
    """Pick a dialog-chrome string for the presentation locale."""

    return ja if locale == PresentationLocale.JAPANESE else en


class _BodyScrollArea(QScrollArea):
    """Scrollable help body with explicit keyboard paging (#1024).

    A plain QScrollArea never consumes PageUp/PageDown/Home/End — arrow keys
    only moved focus between controls, so a keyboard-only or screen-reader
    user could not reach the bottom of a long topic. Unhandled key presses
    from focused child widgets propagate up to this handler as well.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        bar = self.verticalScrollBar()
        key = event.key()
        if key == Qt.Key.Key_PageDown:
            bar.setValue(bar.value() + bar.pageStep())
        elif key == Qt.Key.Key_PageUp:
            bar.setValue(bar.value() - bar.pageStep())
        elif key == Qt.Key.Key_Home:
            bar.setValue(bar.minimum())
        elif key == Qt.Key.Key_End:
            bar.setValue(bar.maximum())
        elif key == Qt.Key.Key_Down:
            bar.setValue(bar.value() + bar.singleStep())
        elif key == Qt.Key.Key_Up:
            bar.setValue(bar.value() - bar.singleStep())
        else:
            super().keyPressEvent(event)


class HelpDialog(QDialog):
    """Scrollable in-app help dialog; content is derived from real registry data.

    The heading stays fixed while the body lives in a QScrollArea so long
    JA/EN topics remain fully reachable on low-resolution and high-DPI
    screens (#1024). ``related_commands`` are rendered as buttons that run
    through the normal CommandRegistry gates — help text itself never
    executes an operation.
    """

    #: Preferred size; capped against the hosting screen in _fit_to_screen.
    _BASE_WIDTH = 560
    _BASE_HEIGHT = 420
    #: Never let the dialog cover more than this share of the screen.
    _SCREEN_SHARE = 0.9
    #: Floor so the cap never squeezes the dialog shut on tiny displays.
    _MIN_WIDTH = 240
    _MIN_HEIGHT = 200

    def __init__(
        self,
        title: str,
        lines: tuple[str, ...],
        parent: QWidget | None = None,
        *,
        locale: PresentationLocale = PresentationLocale.JAPANESE,
    ) -> None:
        super().__init__(parent)
        self._locale = locale
        self._command_registry: CommandRegistry | None = None
        self._help_registry: HelpRegistry | None = None
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        heading = QLabel(title, self)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self._scroll_area = _BodyScrollArea(self)
        self._scroll_area.setAccessibleName(
            _l(locale, "ヘルプ本文", "Help body")
        )
        self._body_host = QWidget(self._scroll_area)
        self._body_layout = QVBoxLayout(self._body_host)
        for line in lines:
            self._body_layout.addWidget(self._body_label(line))
        self._body_layout.addStretch(1)
        self._scroll_area.setWidget(self._body_host)
        layout.addWidget(self._scroll_area)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton(_l(locale, "閉じる", "Close"), self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._fit_to_screen()
        # Keyboard focus starts on the scrollable body so PageDown/End reach
        # long content immediately, not on the Close button.
        self._scroll_area.setFocus(Qt.FocusReason.OtherFocusReason)

    def _body_label(self, text: str) -> QLabel:
        label = QLabel(text, self._body_host)
        label.setWordWrap(True)
        return label

    def _insert_body(self, widget: QWidget) -> None:
        """Append a widget to the scroll body, above the trailing stretch."""

        self._body_layout.insertWidget(self._body_layout.count() - 1, widget)

    def _fit_to_screen(self, available: QRect | None = None) -> None:
        """Cap the dialog to the hosting screen — DPI-aware (#1024).

        Qt reports ``availableGeometry`` in device-independent pixels, so a
        200%-text or low-resolution display shrinks the cap automatically;
        the scroll area keeps every line reachable instead of the window
        overflowing the screen.
        """
        if available is None:
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is None:
                self.resize(self._BASE_WIDTH, self._BASE_HEIGHT)
                return
            available = screen.availableGeometry()
        max_w = max(
            self._MIN_WIDTH, int(available.width() * self._SCREEN_SHARE)
        )
        max_h = max(
            self._MIN_HEIGHT, int(available.height() * self._SCREEN_SHARE)
        )
        self.setMaximumSize(max_w, max_h)
        self.resize(min(self._BASE_WIDTH, max_w), min(self._BASE_HEIGHT, max_h))

    @classmethod
    def shortcuts(
        cls, registry: CommandRegistry, parent: QWidget | None = None
    ) -> "HelpDialog":
        # Shell-level QShortcuts are registered outside the command registry
        # (palette open, navigation history, help, viewport arrow-key nudge)
        # — listing only registry shortcuts would render an incomplete
        # reference. Registry rows come from shortcut_reference() so alias
        # bindings (Backspace, Ctrl+Shift+Z) cannot silently drop out.
        lines = (
            "Ctrl+K  コマンドパレットを開く",
            "Alt+←  前の画面に戻る",
            "Alt+→  次の画面に進む",
            "F1  ショートカット一覧（ヘルプ）",
            "矢印キー（部屋ビュー）  選択項目をグリッド1ステップ移動（Shiftで10倍）",
        ) + tuple(
            f"{doc.shortcut}  {doc.display_name}"
            for doc in shortcut_reference(registry)
        )
        return cls("キーボードショートカット一覧", lines, parent)

    @classmethod
    def palette_usage(cls, parent: QWidget | None = None) -> "HelpDialog":
        return cls(
            "コマンドパレットの使い方",
            (
                "Ctrl+K でコマンドパレットを開きます。",
                "操作・画面・シーン項目・設定・ヘルプをまとめて検索できます。",
                "Enter で実行、Esc で検索語を消去、もう一度 Esc で閉じます。",
            ),
            parent,
        )

    @classmethod
    def topic(
        cls,
        topic: HelpTopic,
        *,
        locale: PresentationLocale = PresentationLocale.JAPANESE,
        command_registry: CommandRegistry | None = None,
        help_registry: HelpRegistry | None = None,
        parent: QWidget | None = None,
    ) -> "HelpDialog":
        """Render one :class:`HelpTopic` — localized title, summary, sections.

        ``related_commands`` become per-command buttons resolved against the
        live CommandRegistry (current availability + disabled reasons) and
        dispatched through ``CommandRegistry.execute`` so the freeze gate and
        deep-link handler still apply (#1024). ``related_topics`` become
        ``htdt-topic:`` links like the glossary surface. Commands/topics that
        cannot be resolved degrade to plain text with an unavailable reason.
        """

        content = topic.localized(locale)
        lines = [content.summary]
        for section in content.sections:
            lines.append(f"■ {section.heading}")
            lines.append(section.body)
        dialog = cls(content.title, tuple(lines), parent, locale=locale)
        dialog._command_registry = command_registry
        dialog._help_registry = help_registry
        if topic.related_commands:
            dialog._add_related_commands(topic.related_commands)
        if topic.related_topics:
            dialog._add_related_topics(topic.related_topics)
        return dialog

    def _add_related_commands(self, command_ids: tuple[str, ...]) -> None:
        heading = self._body_label(
            _l(self._locale, "関連操作", "Related actions")
        )
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        self._insert_body(heading)
        for command_id in command_ids:
            self._insert_body(self._command_row(command_id))

    def _command_row(self, command_id: str) -> QWidget:
        """One related-command row: launch button or degraded plain text."""

        locale = self._locale
        registry = self._command_registry
        if registry is None:
            return self._body_label(
                f"・{command_id} — "
                + _l(
                    locale,
                    "この画面からは操作を実行できません",
                    "this action cannot be launched from here",
                )
            )
        try:
            definition = registry.definition(command_id)
        except KeyError:
            return self._body_label(
                f"・{command_id} — "
                + _l(
                    locale,
                    "この操作は利用できません",
                    "this action is unavailable",
                )
            )
        availability = registry.availability(command_id)
        row = QWidget(self._body_host)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(definition.display_name, row)
        button.setEnabled(availability.enabled)
        button.clicked.connect(
            lambda _checked=False, cid=command_id: self._launch_command(cid)
        )
        row_layout.addWidget(button)
        if availability.enabled:
            tooltip = (
                f"{definition.display_name}（{definition.shortcut}）"
                if definition.shortcut
                else definition.display_name
            )
            button.setToolTip(tooltip)
            if definition.shortcut:
                hint = QLabel(definition.shortcut, row)
                set_typography_role(hint, TypographyRole.SECONDARY)
                row_layout.addWidget(hint)
        else:
            reason = availability.localized_message(locale) or _l(
                locale, "利用できません", "unavailable"
            )
            reason_label = QLabel(reason, row)
            reason_label.setWordWrap(True)
            set_typography_role(reason_label, TypographyRole.SECONDARY)
            row_layout.addWidget(reason_label, 1)
            button.setToolTip(reason)
        row_layout.addStretch(1)
        return row

    def _launch_command(self, command_id: str) -> None:
        """Dispatch a related command through the normal registry gates.

        ``CommandRegistry.execute`` enforces the data-mutation freeze, the
        bound availability provider and the workspace deep-link handler —
        help never bypasses those gates, so authoritative operations are
        never run straight from help text (#1024). On a successful dispatch
        the dialog yields so the target workspace/result is visible; the
        app's navigation history keeps the return context.
        """

        registry = self._command_registry
        if registry is None:
            return
        if registry.execute(command_id):
            self.accept()

    def _add_related_topics(self, topic_ids: tuple[str, ...]) -> None:
        registry = self._help_registry
        unavailable = _l(self._locale, "利用できません", "unavailable")
        parts: list[str] = []
        for topic_id in topic_ids:
            topic = registry.get(topic_id) if registry is not None else None
            if topic is None:
                parts.append(
                    f"{html.escape(topic_id)}（{html.escape(unavailable)}）"
                )
            else:
                parts.append(
                    f'<a href="htdt-topic:{html.escape(topic_id)}">'
                    f"{html.escape(topic.localized(self._locale).title)}</a>"
                )
        label = QLabel(
            _l(self._locale, "関連ヘルプ", "Related help")
            + ": "
            + "、".join(parts),
            self._body_host,
        )
        label.setWordWrap(True)
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(self._open_related_topic)
        self._insert_body(label)

    def _open_related_topic(self, link: str) -> None:
        """Open the registry topic a 関連ヘルプ link points at (nested modal)."""

        registry = self._help_registry
        if registry is None or not link.startswith("htdt-topic:"):
            return
        topic = self._registry_topic(registry, link)
        if topic is None:
            return
        exec_transient(
            HelpDialog.topic(
                topic,
                locale=self._locale,
                command_registry=self._command_registry,
                help_registry=registry,
                parent=self,
            )
        )

    @staticmethod
    def _registry_topic(registry: HelpRegistry, link: str) -> HelpTopic | None:
        return registry.get(link.removeprefix("htdt-topic:"))


class GlossaryDialog(QDialog):
    """TermId-registry-driven glossary surface (REV32-TERMS).

    Every term the app coins lives in the terminology registry; this
    dialog is its UI: preferred label, meaning, what it is NOT, and the
    provenance implication — plus the related help topics when the
    registry links them. A filter box narrows the list as you type.
    """

    def __init__(
        self,
        registry: HelpRegistry,
        *,
        locale: PresentationLocale = PresentationLocale.JAPANESE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._locale = locale
        self.setWindowTitle("用語集")
        layout = QVBoxLayout(self)
        heading = QLabel("用語集", self)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("用語を検索…")
        self._filter.setToolTip("入力した文字で用語を絞り込みます — 語句の一部でも一致します")
        self._filter.setClearButtonEnabled(True)
        layout.addWidget(self._filter)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        host = QWidget(scroll)
        entries = QVBoxLayout(host)
        self._entry_widgets: list[tuple[str, QWidget]] = []
        for term in registry.glossary():
            body = term.meaning.get(locale) or next(
                iter(term.meaning.values()), ''
            )
            not_this = term.not_this.get(locale) or next(
                iter(term.not_this.values()), ''
            )
            provenance = term.provenance_implication.get(locale) or next(
                iter(term.provenance_implication.values()), ''
            )
            lines = [body]
            if not_this:
                lines.append(f"ではないもの: {not_this}")
            if provenance:
                lines.append(f"由来の意味: {provenance}")
            related = [
                (topic_id, topic.localized(locale).title)
                for topic_id in term.related_topics
                if (topic := registry.get(topic_id)) is not None
            ]
            text = "\n".join(line for line in lines if line)
            card = QWidget(host)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 8)
            name = term_text(term.term_id, locale)
            title_label = QLabel(name, card)
            set_typography_role(title_label, TypographyRole.SECTION_TITLE)
            card_layout.addWidget(title_label)
            body_label = QLabel(text, card)
            body_label.setWordWrap(True)
            card_layout.addWidget(body_label)
            if related:
                # Related topics are reachable in one click — a text-only
                # mention would force the user to re-search the title.
                links = "、".join(
                    f'<a href="htdt-topic:{topic_id}">{title}</a>'
                    for topic_id, title in related
                )
                related_label = QLabel(f"関連ヘルプ: {links}", card)
                related_label.setWordWrap(True)
                related_label.setOpenExternalLinks(False)
                related_label.linkActivated.connect(self._open_related_topic)
                card_layout.addWidget(related_label)
            entries.addWidget(card)
            haystack = (
                f"{name}\n{text}\n"
                + "、".join(title for _, title in related)
            ).lower()
            self._entry_widgets.append((haystack, card))
        entries.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll)
        self._filter.textChanged.connect(self._apply_filter)
        self.resize(520, 480)

    def _open_related_topic(self, link: str) -> None:
        """Open the registry topic a 関連ヘルプ link points at."""
        if not link.startswith('htdt-topic:'):
            return
        topic = self._registry.get(link.removeprefix('htdt-topic:'))
        if topic is None:
            return
        exec_transient(
            HelpDialog.topic(
                topic,
                locale=self._locale,
                help_registry=self._registry,
                parent=self,
            )
        )

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for haystack, card in self._entry_widgets:
            card.setVisible(not needle or needle in haystack)


class ReasonTextDialog(QDialog):
    """Text prompt that can carry a help button (REV32-TERMS).

    ``QInputDialog.getText`` has no affordance slot, so audit-reason
    prompts (訂正理由・状態記録理由) use this instead: a line edit with
    OK / キャンセル and an optional ヘルプ button that opens the bound
    help topic without closing the dialog.
    """

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        label: str,
        *,
        on_help=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        prompt = QLabel(label, self)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        self.line_edit = QLineEdit(self)
        layout.addWidget(self.line_edit)
        self.line_edit.setToolTip(label)
        buttons = QHBoxLayout()
        if on_help is not None:
            help_button = QPushButton("ヘルプ", self)
            help_button.setToolTip("この入力が必要な理由をヘルプで確認します")
            help_button.clicked.connect(on_help)
            buttons.addWidget(help_button)
        buttons.addStretch(1)
        ok_button = QPushButton("OK", self)
        ok_button.setDefault(True)
        ok_button.clicked.connect(self.accept)
        buttons.addWidget(ok_button)
        cancel_button = QPushButton("キャンセル", self)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def text(self) -> str:
        return self.line_edit.text()


__all__ = ["GlossaryDialog", "HelpDialog", "ReasonTextDialog"]
