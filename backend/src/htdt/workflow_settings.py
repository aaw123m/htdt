from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from .data_management_ui import DataManagementComponent


class DataManagementDialog(QDialog):
    """Settings host that keeps destructive data operations fail-closed."""

    def __init__(
        self,
        component: DataManagementComponent,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.component = component
        self.setWindowTitle("設定 — データ管理")
        self.setModal(False)
        self.resize(860, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        component.widget.setParent(self)
        layout.addWidget(component.widget)

    def open_settings(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        allowed, _reason = self.component.before_deactivate()
        if not allowed:
            event.ignore()
            return
        event.accept()


__all__ = ["DataManagementDialog"]
