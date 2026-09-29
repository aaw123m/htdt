"""Local help surfaces — real destination for Help-topic palette results."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from .command_registry import CommandRegistry
from .help_registry import HelpTopic, shortcut_reference
from .localization import PresentationLocale
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
        parent: QWidget | None = None,
    ) -> "HelpDialog":
        """Render one :class:`HelpTopic` — localized title, summary, sections."""

        content = topic.localized(locale)
        lines = [content.summary]
        for section in content.sections:
            lines.append(f"■ {section.heading}")
            lines.append(section.body)
        if topic.related_commands:
            lines.append(
                "関連操作: " + ", ".join(topic.related_commands)
            )
        return cls(content.title, tuple(lines), parent)


__all__ = ["HelpDialog"]
