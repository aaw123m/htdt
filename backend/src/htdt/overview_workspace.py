from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .overview_readiness import (
    OVERVIEW_AREA_LABELS,
    OVERVIEW_AREA_ORDER,
    OverviewAction,
    OverviewNotice,
    OverviewReadinessService,
    OverviewVariantState,
)
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


_SEVERITY_ICON: dict[str, str] = {
    'blocker': '■',
    'warning': '▲',
}


class OverviewWorkspace(QWidget):
    """Read-only Issue #118/#443 Overview surface backed by existing authorities."""

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

        self.variant_host = QWidget(self)
        self.variant_layout = QVBoxLayout(self.variant_host)
        self.variant_layout.setContentsMargins(0, 0, 0, 0)
        self.variant_layout.setSpacing(8)
        layout.addWidget(self.variant_host)

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

        for state in view.variant_states:
            self._add_variant_state(state)

        # Notices are grouped by lifecycle area so a room blocker and a
        # measurement warning each sit under their own domain header (#443).
        grouped: dict[str, list[OverviewNotice]] = {}
        for notice in (*view.blockers, *view.warnings):
            grouped.setdefault(notice.area, []).append(notice)
        for area in OVERVIEW_AREA_ORDER:
            notices = grouped.get(area)
            if not notices:
                continue
            header = QLabel(OVERVIEW_AREA_LABELS.get(area, area), self.notice_host)
            set_typography_role(header, TypographyRole.SECTION_TITLE)
            self.notice_layout.addWidget(header)
            for notice in notices:
                self._add_notice(notice)

        self._next_action = view.next_action
        self.next_button.setVisible(view.next_action is not None)
        if view.next_action is not None:
            self.next_button.setText(view.next_action.label)

    def _add_notice(self, notice: OverviewNotice) -> None:
        """One card per notice: icon + text state label + message + action (#443)."""
        card = QFrame(self.notice_host)
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        icon = QLabel(_SEVERITY_ICON[notice.severity], card)
        set_semantic_state(
            icon,
            SemanticState.ERROR if notice.severity == 'blocker' else SemanticState.WARNING,
        )
        layout.addWidget(icon)

        body = QVBoxLayout()
        state_label = QLabel(notice.state_label, card)
        set_typography_role(state_label, TypographyRole.SECONDARY)
        set_semantic_state(
            state_label,
            SemanticState.ERROR if notice.severity == 'blocker' else SemanticState.WARNING,
        )
        body.addWidget(state_label)
        label = QLabel(notice.message, card)
        label.setWordWrap(True)
        body.addWidget(label)
        layout.addLayout(body, 1)

        if notice.action is not None:
            button = QPushButton(notice.action.label, card)
            button.setObjectName(f"overviewAction:{notice.action.action_id}")
            button.setProperty("deeplink", notice.action.target.as_uri())
            button.clicked.connect(
                lambda checked=False, target=notice.action.target: self.navigate(target)
            )
            layout.addWidget(button)

        self.notice_layout.addWidget(card)

    def _add_variant_state(self, state: OverviewVariantState) -> None:
        """One card per SystemVariant lifecycle stage (#443, text-labeled)."""
        card = QFrame(self.variant_host)
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        body = QVBoxLayout()
        title = QLabel(f"{state.name} · {state.stage_label}", card)
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        body.addWidget(title)
        detail = QLabel(
            f"{state.lifecycle_label} · {state.detail}" if state.detail else state.lifecycle_label,
            card,
        )
        set_typography_role(detail, TypographyRole.SECONDARY)
        detail.setWordWrap(True)
        body.addWidget(detail)
        layout.addLayout(body, 1)

        if state.action is not None:
            button = QPushButton(state.action.label, card)
            button.setObjectName(f"overviewAction:{state.action.action_id}")
            button.clicked.connect(
                lambda checked=False, target=state.action.target: self.navigate(target)
            )
            layout.addWidget(button)

        self.variant_layout.addWidget(card)

    def _clear_notices(self) -> None:
        for host in (self.notice_layout, self.variant_layout):
            while host.count():
                item = host.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

    def _run_next_action(self) -> None:
        action = self._next_action
        if action is None:
            return
        self.navigate(action.target)


__all__ = ["OverviewWorkspace"]
