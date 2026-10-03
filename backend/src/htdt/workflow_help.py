"""Local help surfaces — real destination for Help-topic palette results."""

from __future__ import annotations

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

from .command_registry import CommandRegistry
from .help_registry import HelpRegistry, HelpTopic, shortcut_reference
from .localization import PresentationLocale, term_text
from .ui_theme import TypographyRole, set_typography_role


class HelpDialog(QDialog):
    """Small in-app help dialog; content is derived from real registry data."""

    def __init__(
        self,
        title: str,
        lines: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        heading = QLabel(title, self)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)
        for line in lines:
            label = QLabel(line, self)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.resize(480, 360)

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
        parent: QWidget | None = None,
    ) -> "HelpDialog":
        """Render one :class:`HelpTopic` — localized title, summary, sections."""

        content = topic.localized(locale)
        lines = [content.summary]
        for section in content.sections:
            lines.append(f"■ {section.heading}")
            lines.append(section.body)
        if topic.related_commands:
            labels = []
            for command_id in topic.related_commands:
                try:
                    labels.append(
                        command_registry.definition(command_id).display_name
                        if command_registry is not None
                        else command_id
                    )
                except KeyError:
                    labels.append(command_id)
            lines.append("関連操作: " + ", ".join(labels))
        return cls(content.title, tuple(lines), parent)


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
        HelpDialog.topic(topic, locale=self._locale, parent=self).exec()

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
