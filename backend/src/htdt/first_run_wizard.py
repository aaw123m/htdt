"""First-run setup wizard dialog (#886).

One guided path over the canonical authorities: stage rail on the left,
per-stage status + reason + primary action on the right. Progress is
re-derived on every refresh — expert edits outside the wizard are
reflected immediately, and reopening resumes at the derived stage
(never at a stored position that could disagree with authority).
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .first_run_wizard_state import (
    FirstRunWizardFacts,
    WizardStage,
    WizardStageStatus,
    WizardStageView,
    derive_wizard_progress,
    first_incomplete_stage,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_typography_role,
)
from .workflow_navigation import WorkspaceDeepLink, deeplink_hint

_STATUS_MARK = {
    WizardStageStatus.COMPLETE: '✓',
    WizardStageStatus.CURRENT: '●',
    WizardStageStatus.PENDING: '○',
    WizardStageStatus.BLOCKED: '⛔',
}

_STATUS_TEXT_JA = {
    WizardStageStatus.COMPLETE: '完了',
    WizardStageStatus.CURRENT: '現在のステップ',
    WizardStageStatus.PENDING: '未着手',
    WizardStageStatus.BLOCKED: 'ブロック中',
}


class FirstRunWizardDialog(QDialog):
    """Guided golden-path dialog. ``facts_provider`` is re-invoked on
    every refresh; ``navigate`` drives a WorkspaceDeepLink."""

    def __init__(
        self,
        facts_provider: Callable[[], FirstRunWizardFacts],
        *,
        navigate: Callable[[WorkspaceDeepLink], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('初回セットアップウィザード')
        self.setModal(True)
        self.resize(860, 560)
        self._facts_provider = facts_provider
        self._navigate = navigate

        layout = QHBoxLayout(self)
        layout.setSpacing(12)

        self._stage_list = QListWidget(self)
        self._stage_list.setFixedWidth(230)
        self._stage_list.setAccessibleName('ウィザードステップ一覧')
        self._stage_list.currentRowChanged.connect(self._select_stage)
        layout.addWidget(self._stage_list)

        right = QFrame(self)
        right_layout = QVBoxLayout(right)
        right_layout.setSpacing(10)
        layout.addWidget(right, stretch=1)

        self._title = QLabel()
        set_typography_role(self._title, TypographyRole.SECTION_TITLE)
        right_layout.addWidget(self._title)

        self._status = QLabel()
        right_layout.addWidget(self._status)

        self._detail = QLabel()
        self._detail.setWordWrap(True)
        right_layout.addWidget(self._detail)

        self._reason = QLabel()
        self._reason.setWordWrap(True)
        set_semantic_state(self._reason, SemanticState.WARNING)
        right_layout.addWidget(self._reason)

        right_layout.addStretch(1)

        self._action = QPushButton()
        self._action.setAccessibleName('ステップの操作を開く')
        right_layout.addWidget(self._action)

        buttons_row = QHBoxLayout()
        self._defer_button = QPushButton('あとでやる')
        self._defer_button.setAccessibleName(
            'ウィザードの自動表示を止める'
        )
        self._defer_button.setToolTip(
            '以後は起動時に自動表示しません。'
            'プロジェクトメニューからいつでも再開できます。'
        )
        self._defer_button.clicked.connect(self._defer)
        buttons_row.addWidget(self._defer_button)
        buttons_row.addStretch(1)
        self._close_button = QPushButton('とじる')
        self._close_button.setAccessibleName('ウィザードを閉じる')
        self._close_button.clicked.connect(self.accept)
        buttons_row.addWidget(self._close_button)
        right_layout.addLayout(buttons_row)

        self._deferred = False
        self._action_conn = None

        self._views: tuple[WizardStageView, ...] = ()
        self._selected_stage: WizardStage | None = None
        self.refresh()

    def refresh(self) -> None:
        """Re-derive every stage from canonical facts."""
        views = derive_wizard_progress(self._facts_provider())
        self._views = views
        resume_at = first_incomplete_stage(views)
        self._stage_list.clear()
        for view in views:
            item = QListWidgetItem(
                f'{_STATUS_MARK[view.status]} {view.title_ja}'
            )
            item.setData(Qt.ItemDataRole.UserRole, view.stage)
            if view.status is WizardStageStatus.BLOCKED:
                item.setToolTip(view.reason_ja or '')
            self._stage_list.addItem(item)
        target_stage = (
            self._selected_stage
            if self._selected_stage in [v.stage for v in views]
            else resume_at
        )
        if target_stage is None:
            target_stage = views[-1].stage if views else None
        if target_stage is not None:
            index = [v.stage for v in views].index(target_stage)
            self._stage_list.setCurrentRow(index)
        self._render_stage(target_stage)

    def _select_stage(self, row: int) -> None:
        if row < 0 or row >= len(self._views):
            return
        self._selected_stage = self._views[row].stage
        self._render_stage(self._selected_stage)

    def _render_stage(self, stage: WizardStage | None) -> None:
        view = next((v for v in self._views if v.stage == stage), None)
        if view is None:
            return
        self._title.setText(view.title_ja)
        self._status.setText(
            f'{_STATUS_MARK[view.status]} {_STATUS_TEXT_JA[view.status]}'
        )
        self._detail.setText(view.detail_ja)
        if view.reason_ja:
            self._reason.setText(f'理由: {view.reason_ja}')
            self._reason.show()
        else:
            self._reason.hide()
        if view.target is not None and view.action_label_ja:
            self._action.setText(view.action_label_ja)
            hint = deeplink_hint(view.target)
            self._action.setToolTip(hint)
            self._action.setEnabled(True)
            if view.status is WizardStageStatus.CURRENT:
                set_primary_action(self._action)
            else:
                set_primary_action(self._action, enabled=False)
            if self._action_conn is not None:
                self._action.clicked.disconnect(self._action_conn)
                self._action_conn = None
            target = view.target
            self._action_conn = self._action.clicked.connect(
                lambda checked=False: self._open_target(target)
            )
            self._action.show()
        else:
            self._action.hide()

    def _open_target(self, target: WorkspaceDeepLink) -> None:
        self._navigate(target)
        # The user may complete work in the destination workspace and come
        # back — re-derive so the rail reflects the new canonical state.
        self.refresh()

    def _defer(self) -> None:
        self._deferred = True
        self.accept()

    @property
    def deferred(self) -> bool:
        """Operator asked to stop the auto-show (menu reopen stays open)."""
        return self._deferred

    @property
    def current_stage(self) -> WizardStage | None:
        return self._selected_stage


__all__ = ['FirstRunWizardDialog']
