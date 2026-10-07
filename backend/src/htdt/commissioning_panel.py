"""Workflow > コミッショニング panel (#868).

Read-only surface over the commissioning orchestrator: current stage,
blocked reason, next permitted action, rollback availability, evidence
strength, stale stages, and the sealed transition log. The panel never
touches an adapter — there is deliberately no real-device mutation path
here; device steps are driven by the commissioning service layer and
this panel only reports what the machine sealed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_commissioning_orchestrator import (
    COMMISSIONING_DEVICE_MUTATING_STAGES,
    COMMISSIONING_EVENT_LABELS,
    COMMISSIONING_EVIDENCE_STRENGTH_LABELS,
    COMMISSIONING_OUTCOME_LABELS,
    COMMISSIONING_STAGE_LABELS,
    CommissioningOrchestrator,
    CommissioningRunState,
    TERMINAL_STAGES,
    derive_evidence_strength,
    next_permitted_actions,
)
from .ui_theme import SemanticState, set_semantic_state
from .user_facing_error import operation_error_message


class CommissioningPanel(QWidget):
    """コミッショニングワークフローの状態表示パネル。

    Displays the orchestrator's sealed state for one document. Every
    read failure renders as a fail-closed message — a panel that cannot
    read the authority must never imply progress.
    """

    def __init__(
        self,
        orchestrator: CommissioningOrchestrator,
        document_id: str,
        *,
        on_status=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._document_id = document_id
        self._on_status = on_status

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.stage_label = QLabel(self)
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)

        self.blocked_label = QLabel(self)
        self.blocked_label.setWordWrap(True)
        self.blocked_label.setAccessibleName('ブロック理由')
        layout.addWidget(self.blocked_label)

        self.next_label = QLabel(self)
        self.next_label.setWordWrap(True)
        self.next_label.setAccessibleName('次の許可アクション')
        layout.addWidget(self.next_label)

        self.rollback_label = QLabel(self)
        self.rollback_label.setWordWrap(True)
        self.rollback_label.setAccessibleName('ロールバック可否')
        layout.addWidget(self.rollback_label)

        self.strength_label = QLabel(self)
        self.strength_label.setWordWrap(True)
        self.strength_label.setAccessibleName('証拠の強さ')
        layout.addWidget(self.strength_label)

        self.stale_label = QLabel(self)
        self.stale_label.setWordWrap(True)
        self.stale_label.setAccessibleName('無効化済み段階')
        layout.addWidget(self.stale_label)

        actions = QHBoxLayout()
        self.refresh_button = QPushButton('状態を再読み込み', self)
        self.refresh_button.setToolTip(
            'コミッショニングランの最新の状態を読み込みます。'
        )
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.transition_tree = QTreeWidget(self)
        self.transition_tree.setAccessibleName('コミッショニング遷移ログ')
        self.transition_tree.setColumnCount(5)
        self.transition_tree.setHeaderLabels(
            ('No.', 'イベント', '結果', '遷移先', '理由 / 時刻'))
        self.transition_tree.setToolTip(
            'シール済みの遷移ログです。拒否・ブロックもすべて記録されます。')
        header = self.transition_tree.headerItem()
        for col, tip in {
            0: '遷移の連番',
            1: '発生したイベント',
            2: 'イベントの処理結果',
            3: '遷移後のステージ',
            4: '遷移理由と記録時刻',
        }.items():
            header.setToolTip(col, tip)
        self.transition_tree.setRootIsDecorated(False)
        layout.addWidget(self.transition_tree)

        self.refresh()

    # ------------------------------------------------------------------

    def _fail_closed(self, message: str) -> None:
        self.stage_label.setText(message)
        set_semantic_state(self.stage_label, SemanticState.ERROR)
        self.blocked_label.setText('')
        self.next_label.setText('次の許可アクション: 不明')
        self.rollback_label.setText('ロールバック可否: 不明')
        self.strength_label.setText('証拠の強さ: 不明')
        self.stale_label.setText('')
        self.transition_tree.clear()

    def _latest_verdict(self, run) -> object | None:
        verdicts = self._orchestrator.repository.list_verdicts(
            self._document_id)
        for verdict in reversed(verdicts):
            if verdict.run_ref.ref_id == run.run_id:
                return verdict
        return None

    def _render(self, run, state: CommissioningRunState) -> None:
        stage_text = COMMISSIONING_STAGE_LABELS.get(
            state.current_stage, state.current_stage)
        self.stage_label.setText(
            f'現在のステージ: {stage_text}（ラン {run.run_id}）')
        set_semantic_state(
            self.stage_label,
            SemanticState.ERROR
            if state.current_stage in ('rolled_back', 'aborted')
            else SemanticState.WARNING
            if state.blocked_reason or state.stale_stages
            else SemanticState.SUCCESS
            if state.current_stage == 'completed'
            else SemanticState.UNSUPPORTED)

        if state.blocked_reason:
            self.blocked_label.setText(
                f'ブロック理由: {state.blocked_reason}')
            set_semantic_state(self.blocked_label, SemanticState.ERROR)
        elif state.provider_disconnected or state.device_disconnected:
            self.blocked_label.setText('ブロック理由: 接続断')
            set_semantic_state(self.blocked_label, SemanticState.ERROR)
        else:
            self.blocked_label.setText('ブロック理由: なし')
            set_semantic_state(
                self.blocked_label, SemanticState.UNSUPPORTED)

        permitted = next_permitted_actions(state)
        if permitted:
            labels = '、'.join(
                COMMISSIONING_EVENT_LABELS.get(kind, kind)
                for kind in permitted)
            self.next_label.setText(f'次の許可アクション: {labels}')
        else:
            self.next_label.setText(
                '次の許可アクション: なし（終端ステージ）')

        rollbackable = (
            state.current_stage in COMMISSIONING_DEVICE_MUTATING_STAGES
            and state.current_stage not in TERMINAL_STAGES)
        self.rollback_label.setText(
            'ロールバック可否: '
            + ('可能（オペレーター承認が必要）' if rollbackable
               else '不可'))
        set_semantic_state(
            self.rollback_label,
            SemanticState.WARNING if rollbackable
            else SemanticState.UNSUPPORTED)

        verdict = self._latest_verdict(run)
        strength = derive_evidence_strength(state, verdict)
        strength_text = COMMISSIONING_EVIDENCE_STRENGTH_LABELS.get(
            strength, strength)
        self.strength_label.setText(f'証拠の強さ: {strength_text}')
        set_semantic_state(
            self.strength_label,
            SemanticState.SUCCESS if strength == 'accepted'
            else SemanticState.ERROR
            if strength in ('rejected', 'rolled_back', 'aborted')
            else SemanticState.UNSUPPORTED)

        if state.stale_stages:
            stale = '、'.join(
                COMMISSIONING_STAGE_LABELS.get(s, s)
                for s in state.stale_stages)
            self.stale_label.setText(f'無効化済み段階: {stale}')
            set_semantic_state(self.stale_label, SemanticState.STALE)
        else:
            self.stale_label.setText('無効化済み段階: なし')
            set_semantic_state(
                self.stale_label, SemanticState.UNSUPPORTED)

        self.transition_tree.clear()
        for transition in self._orchestrator.transitions(run):
            item = QTreeWidgetItem((
                str(transition.seq),
                COMMISSIONING_EVENT_LABELS.get(
                    transition.event_kind, transition.event_kind),
                COMMISSIONING_OUTCOME_LABELS.get(
                    transition.outcome, transition.outcome),
                COMMISSIONING_STAGE_LABELS.get(
                    transition.to_stage, transition.to_stage or ''),
                f'{transition.reason} — {transition.recorded_at_utc}',
            ))
            self.transition_tree.addTopLevelItem(item)
        for col in range(self.transition_tree.columnCount()):
            self.transition_tree.resizeColumnToContents(col)

    def refresh(self) -> None:
        try:
            opened = self._orchestrator.get_open_run(self._document_id)
            if opened is None:
                runs = self._orchestrator.list_runs(self._document_id)
                if not runs:
                    self._fail_closed(
                        'コミッショニングランはまだありません。')
                    set_semantic_state(
                        self.stage_label, SemanticState.UNSUPPORTED)
                    return
                run = runs[-1]
                state = self._orchestrator.derive_state(run)
            else:
                run, state = opened
        except Exception as exc:  # error-boundary: authority read
            self._fail_closed(
                'コミッショニング状態を読み込めません: '
                + operation_error_message(exc))
            return
        try:
            self._render(run, state)
        except Exception as exc:  # error-boundary: panel render
            self._fail_closed(
                'コミッショニング状態を表示できません: '
                + operation_error_message(exc))


__all__ = [
    'CommissioningPanel',
]
