from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QTabWidget, QVBoxLayout, QWidget

from .data_management_ui import DataManagementComponent


class DataManagementDialog(QDialog):
    """Settings host that keeps destructive data operations fail-closed."""

    def __init__(
        self,
        component: DataManagementComponent,
        parent: QWidget | None = None,
        capture_panel: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.component = component
        self.setWindowTitle("設定")
        self.setModal(False)
        self.resize(860, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        component.widget.setParent(self)
        self._capture_panel = capture_panel
        if capture_panel is None:
            layout.addWidget(component.widget)
            self._tabs = None
        else:
            self._tabs = QTabWidget(self)
            self._tabs.addTab(component.widget, "データ管理")
            self._tabs.addTab(capture_panel, "キャプチャ")
            layout.addWidget(self._tabs)

    def open_settings(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def open_capture_settings(self) -> None:
        if self._tabs is not None:
            self._tabs.setCurrentWidget(self._capture_panel)
        self.open_settings()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        allowed, _reason = self.component.before_deactivate()
        if not allowed:
            event.ignore()
            return
        event.accept()


__all__ = ["DataManagementDialog"]
