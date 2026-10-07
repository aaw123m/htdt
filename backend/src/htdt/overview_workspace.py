from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .overview_readiness import (
    OVERVIEW_AREA_LABELS,
    OVERVIEW_AREA_ORDER,
    OverviewAction,
    OverviewNotice,
    OverviewReadinessService,
    OverviewSecondaryDomain,
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
from .workflow_navigation import WorkspaceDeepLink, deeplink_hint


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

        # The lifecycle/readiness cards live inside a scroll area so the
        # primary action stays reachable at constrained heights / high DPI
        # (#1086): title, summary and the next-step button are pinned outside
        # the scroll region instead of being pushed off-screen by tall cards.
        self.cards_scroll = QScrollArea(self)
        self.cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_scroll.setWidgetResizable(True)
        set_surface_role(self.cards_scroll, SurfaceRole.BASE)
        cards_host = QWidget()
        set_surface_role(cards_host, SurfaceRole.BASE)
        cards_layout = QVBoxLayout(cards_host)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(16)

        # Golden-path journey strip (#804 Gate B): the whole-project flow
        # 部屋 → 機材 → 測定 → 予測 → 比較 → 適用 → 再測定 → 出力, so the
        # operator always knows where they are and what comes next.
        self.journey_host = QWidget(cards_host)
        self.journey_layout = QVBoxLayout(self.journey_host)
        self.journey_layout.setContentsMargins(0, 0, 0, 0)
        self.journey_layout.setSpacing(8)
        cards_layout.addWidget(self.journey_host)

        self.variant_host = QWidget(cards_host)
        self.variant_layout = QVBoxLayout(self.variant_host)
        self.variant_layout.setContentsMargins(0, 0, 0, 0)
        self.variant_layout.setSpacing(8)
        cards_layout.addWidget(self.variant_host)

        # Result Trust lines (#740): compact_text projections of the latest
        # prediction/measurement/validation evidence, inside the same scroll
        # region so they never push the primary action off-screen.
        self.trust_host = QWidget(cards_host)
        self.trust_layout = QVBoxLayout(self.trust_host)
        self.trust_layout.setContentsMargins(0, 0, 0, 0)
        self.trust_layout.setSpacing(8)
        cards_layout.addWidget(self.trust_host)

        # Tier-C secondary domains live here — a status strip on the
        # project hub, not new primary workspaces (#887). Kept inside the
        # same scroll region so the primary action stays reachable (#1086).
        self.domain_header = QLabel("プロジェクト領域", cards_host)
        set_typography_role(self.domain_header, TypographyRole.SECTION_TITLE)
        cards_layout.addWidget(self.domain_header)

        self.domain_host = QWidget(cards_host)
        self.domain_layout = QVBoxLayout(self.domain_host)
        self.domain_layout.setContentsMargins(0, 0, 0, 0)
        self.domain_layout.setSpacing(8)
        cards_layout.addWidget(self.domain_host)

        self.notice_host = QWidget(cards_host)
        self.notice_layout = QVBoxLayout(self.notice_host)
        self.notice_layout.setContentsMargins(0, 0, 0, 0)
        self.notice_layout.setSpacing(8)
        cards_layout.addWidget(self.notice_host)
        cards_layout.addStretch(1)
        self.cards_scroll.setWidget(cards_host)
        layout.addWidget(self.cards_scroll, 1)

        self.next_button = QPushButton()
        set_primary_action(self.next_button)
        self.next_button.clicked.connect(self._run_next_action)
        layout.addWidget(self.next_button)

        self._next_action: OverviewAction | None = None
        self.refresh()

    def refresh(self) -> None:
        view = self.service.read(self.document_id)
        self.summary.setText(view.summary)
        self._clear_notices()

        self._render_golden_path(view.golden_path_steps)

        for state in view.variant_states:
            self._add_variant_state(state)

        if getattr(view, 'trust_lines', ()):
            header = QLabel("\u4fe1\u983c\u6027", self.trust_host)
            set_typography_role(header, TypographyRole.SECTION_TITLE)
            self.trust_layout.addWidget(header)
            for line in view.trust_lines:
                label = QLabel(line, self.trust_host)
                label.setWordWrap(True)
                set_typography_role(label, TypographyRole.SECONDARY)
                self.trust_layout.addWidget(label)
        secondary_domains = getattr(view, 'secondary_domains', ())
        for domain in secondary_domains:
            self._add_secondary_domain(domain)
        self.domain_header.setVisible(bool(secondary_domains))

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
            _hint = deeplink_hint(view.next_action.target)
            self.next_button.setToolTip(_hint)
            self.next_button.setWhatsThis(_hint)

    def _render_golden_path(self, steps) -> None:
        """Numbered whole-project journey strip (#804 Gate B).

        Done steps get a success tick, the single 'current' step is the
        primary action, pending steps stay plain, and every step button
        navigates — the strip is guidance, never a gate.
        """
        self.journey_host.setVisible(bool(steps))
        if not steps:
            return
        done_count = sum(1 for step in steps if step.status == 'done')

        card = QFrame(self.journey_host)
        card.setObjectName("overviewJourneyCard")
        set_surface_role(card, SurfaceRole.RAISED)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(8)

        header = QLabel(
            f"使い方の流れ {done_count}/{len(steps)}", card
        )
        header.setObjectName("overviewJourneyProgress")
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        card_layout.addWidget(header)

        row = QHBoxLayout()
        row.setSpacing(6)
        for step in steps:
            button = QPushButton(f"{step.number}. {step.title}", card)
            button.setObjectName(f"overviewJourneyStep_{step.key}")
            _hint = (
                step.detail
                if step.target is None
                else f"{step.detail} — {deeplink_hint(step.target)}"
            )
            button.setToolTip(_hint)
            button.setWhatsThis(_hint)
            button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            if step.status == 'current':
                set_primary_action(button)
            elif step.status == 'done':
                set_semantic_state(button, SemanticState.SUCCESS)
                button.setText(f"{step.number}. {step.title} ✓")
            if step.target is not None:
                button.setProperty("deeplink", step.target.as_uri())
                button.clicked.connect(
                    lambda checked=False, target=step.target: self.navigate(target)
                )
            else:
                button.setEnabled(False)
            row.addWidget(button)
        row.addStretch(1)
        card_layout.addLayout(row)
        self.journey_layout.addWidget(card)

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
            _hint = deeplink_hint(notice.action.target)
            button.setToolTip(_hint)
            button.setWhatsThis(_hint)
            # Qt hides tooltips on disabled buttons — a disabled action
            # still needs to explain what it would do.
            button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
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
            _hint = deeplink_hint(state.action.target)
            button.setToolTip(_hint)
            button.setWhatsThis(_hint)
            button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            button.clicked.connect(
                lambda checked=False, target=state.action.target: self.navigate(target)
            )
            layout.addWidget(button)

        self.variant_layout.addWidget(card)

    def _add_secondary_domain(self, domain: OverviewSecondaryDomain) -> None:
        """One card per tier-C secondary domain (#887, text-labeled)."""
        card = QFrame(self.domain_host)
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        body = QVBoxLayout()
        title = QLabel(f"{domain.title} · {domain.state_label}", card)
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        body.addWidget(title)
        detail = QLabel(domain.detail, card)
        set_typography_role(detail, TypographyRole.SECONDARY)
        detail.setWordWrap(True)
        body.addWidget(detail)
        layout.addLayout(body, 1)

        if domain.action is not None:
            button = QPushButton(domain.action.label, card)
            button.setObjectName(f"overviewDomainAction:{domain.action.action_id}")
            button.setProperty("deeplink", domain.action.target.as_uri())
            _hint = deeplink_hint(domain.action.target)
            button.setToolTip(_hint)
            button.setWhatsThis(_hint)
            button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            button.clicked.connect(
                lambda checked=False, target=domain.action.target: self.navigate(target)
            )
            layout.addWidget(button)

        self.domain_layout.addWidget(card)

    def _clear_notices(self) -> None:
        for host in (
            self.notice_layout,
            self.variant_layout,
            self.trust_layout,
            self.domain_layout,
            self.journey_layout,
        ):
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
