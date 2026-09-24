"""Local help surfaces — real destination for Help-topic palette results."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from .command_registry import CommandRegistry
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
        lines = tuple(
            f"{definition.shortcut}  {definition.display_name}"
            for definition in registry.definitions()
            if definition.shortcut
        ) or ("ショートカットはまだ登録されていません。",)
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


__all__ = ["HelpDialog"]
