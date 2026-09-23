from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from .overview_readiness import OverviewAction, OverviewReadinessService
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .workflow_navigation import WorkspaceDeepLink


class OverviewWorkspace(QWidget):
    """Read-only Issue #118 Overview surface backed by existing authorities."""

    def __init__(
        self,
        service: OverviewReadinessService,
        document_id: str,
        *,
        navigate: Callable[[WorkspaceDeepLink], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.document_id = document_id
        self.navigate = navigate
        self.setObjectName("overviewWorkspace")
        set_surface_role(self, SurfaceRole.BASE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(16)

        self.title = QLabel("概要")
        set_typography_role(self.title, TypographyRole.WORKSPACE_TITLE)
        layout.addWidget(self.title)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        set_typography_role(self.summary, TypographyRole.BODY)
        layout.addWidget(self.summary)

        self.notice_host = QWidget(self)
        self.notice_layout = QVBoxLayout(self.notice_host)
        self.notice_layout.setContentsMargins(0, 0, 0, 0)
        self.notice_layout.setSpacing(8)
        layout.addWidget(self.notice_host)

        self.next_button = QPushButton()
        set_primary_action(self.next_button)
        self.next_button.clicked.connect(self._run_next_action)
        layout.addWidget(self.next_button)
        layout.addStretch(1)

        self._next_action: OverviewAction | None = None
        self.refresh()

    def refresh(self) -> None:
        view = self.service.read(self.document_id)
        self.summary.setText(view.summary)
        self._clear_notices()

        for notice in view.blockers:
            self._add_notice(notice.message, SemanticState.ERROR)
        for notice in view.warnings:
            self._add_notice(notice.message, SemanticState.WARNING)

        self._next_action = view.next_action
        self.next_button.setVisible(view.next_action is not None)
        if view.next_action is not None:
            self.next_button.setText(view.next_action.label)

    def _add_notice(self, message: str, state: SemanticState) -> None:
        card = QFrame(self.notice_host)
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        label = QLabel(message, card)
        label.setWordWrap(True)
        set_semantic_state(label, state)
        layout.addWidget(label)
        self.notice_layout.addWidget(card)

    def _clear_notices(self) -> None:
        while self.notice_layout.count():
            item = self.notice_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _run_next_action(self) -> None:
        action = self._next_action
        if action is None:
            return
        self.navigate(action.target)


__all__ = ["OverviewWorkspace"]
