"""Workflow > コミッショニング panel (#868, operator commands #946).

Live surface over the commissioning orchestrator: current stage,
blocked reason, next permitted action, rollback availability, evidence
strength, stale stages, the sealed transition log — and the operator
command row (advance / authorize / deploy / read-back / rollback /
restore / cancel) wired through
:class:`commissioning_operations.CommissioningOperatorController`.
Every button's enablement is derived from the machine's own
``next_permitted_actions``; device mutations always pass through the
one-shot, candidate/deployment-pinned authorization dialog before
they reach an adapter.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
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
from .commissioning_operations import (
    CommissioningMutationPreview,
    CommissioningOperationResult,
    CommissioningOperatorController,
    CommissioningOperatorServices,
)
from .ui_theme import SemanticState, set_semantic_state
from .error_boundary import EXPECTED_OPERATION_ERRORS
from .user_facing_error import operation_error_message


class CommissioningApprovalDialog(QDialog):
    """One-shot operator authorization before a device mutation (#946).

    Shows the exact pins the authorization covers — target, scene /
    candidate / config hashes, the change the mutation applies — then
    seals a scoped, single-use authorization. Cancelling seals nothing.
    """

    def __init__(
        self,
        preview: CommissioningMutationPreview,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._preview = preview
        self.setModal(True)
        self.setWindowTitle(
            'ロールバックの承認' if preview.command == 'rollback'
            else 'デプロイの承認')

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for label, value in preview.lines:
            value_label = QLabel(value, self)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, value_label)
        layout.addLayout(form)

        oneshot = QLabel(
            'この承認は一回限り有効です。実行後は自動的に消費され、'
            '再実行には新しい承認が必要です。', self)
        oneshot.setWordWrap(True)
        layout.addWidget(oneshot)

        if preview.reusable_authorization_id is not None:
            reuse = QLabel(
                '既存の有効な承認 '
                f'{preview.reusable_authorization_id} '
                'を再利用します（新しい承認は発行しません）。', self)
            reuse.setWordWrap(True)
            layout.addWidget(reuse)

        fields = QFormLayout()
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.operator_edit = QLineEdit(self)
        self.operator_edit.setAccessibleName('オペレーターID')
        self.operator_edit.setPlaceholderText('オペレーターID（必須）')
        fields.addRow('オペレーターID', self.operator_edit)
        self.note_edit = QLineEdit(self)
        note_label = ('理由（必須）' if preview.requires_reason
                      else 'メモ（任意）')
        self.note_edit.setAccessibleName(note_label)
        self.note_edit.setPlaceholderText(note_label)
        fields.addRow(note_label, self.note_edit)
        layout.addLayout(fields)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, parent=self)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText('承認して実行')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self.operator_edit.textChanged.connect(self._sync_ok)
        self.note_edit.textChanged.connect(self._sync_ok)
        self._sync_ok()
        self.operator_edit.setFocus()

    def _sync_ok(self) -> None:
        ok = bool(self.operator_edit.text().strip())
        if self._preview.requires_reason:
            ok = ok and bool(self.note_edit.text().strip())
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    @property
    def operator_id(self) -> str:
        return self.operator_edit.text().strip()

    @property
    def note(self) -> str:
        return self.note_edit.text().strip()


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
        services: CommissioningOperatorServices | None = None,
        controller: CommissioningOperatorController | None = None,
        on_status=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._document_id = document_id
        self._services = (
            services or CommissioningOperatorServices())
        self._controller = controller or CommissioningOperatorController(
            orchestrator, document_id, services=self._services)
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
        self.refresh_button.setAccessibleName('状態を再読み込み')
        self.refresh_button.setToolTip(
            'コミッショニングランの最新の状態を読み込みます。'
        )
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        commands = QHBoxLayout()
        commands.setSpacing(4)
        self.advance_button = QPushButton('進める', self)
        self.advance_button.setAccessibleName('進める')
        self.advance_button.clicked.connect(self._on_advance)
        commands.addWidget(self.advance_button)

        self.authorize_button = QPushButton('承認して進める', self)
        self.authorize_button.setAccessibleName('承認して進める')
        self.authorize_button.clicked.connect(self._on_authorize)
        commands.addWidget(self.authorize_button)

        self.deploy_button = QPushButton('デプロイを実行', self)
        self.deploy_button.setAccessibleName('デプロイを実行')
        self.deploy_button.clicked.connect(self._on_deploy)
        commands.addWidget(self.deploy_button)

        self.readback_button = QPushButton('読み戻しを検証', self)
        self.readback_button.setAccessibleName('読み戻しを検証')
        self.readback_button.clicked.connect(self._on_readback)
        commands.addWidget(self.readback_button)

        self.rollback_button = QPushButton('ロールバック', self)
        self.rollback_button.setAccessibleName('ロールバック')
        self.rollback_button.clicked.connect(self._on_rollback)
        commands.addWidget(self.rollback_button)

        self.restore_button = QPushButton('接続復帰を記録', self)
        self.restore_button.setAccessibleName('接続復帰を記録')
        self.restore_button.clicked.connect(self._on_restore)
        commands.addWidget(self.restore_button)

        self.cancel_button = QPushButton('ランを中止', self)
        self.cancel_button.setAccessibleName('ランを中止')
        self.cancel_button.clicked.connect(self._on_cancel)
        commands.addWidget(self.cancel_button)
        commands.addStretch(1)
        layout.addLayout(commands)

        self.result_label = QLabel(self)
        self.result_label.setWordWrap(True)
        self.result_label.setAccessibleName('操作結果')
        layout.addWidget(self.result_label)

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
        for button in self._command_buttons():
            button.setEnabled(False)
            button.setToolTip('状態を読み込めないため操作できません')

    def _command_buttons(self) -> tuple[QPushButton, ...]:
        return (
            self.advance_button,
            self.authorize_button,
            self.deploy_button,
            self.readback_button,
            self.rollback_button,
            self.restore_button,
            self.cancel_button,
        )

    def _render_operations(self) -> None:
        """Enable/disable the command row from the machine's answer."""
        buttons = {
            'advance': self.advance_button,
            'authorize_deploy': self.authorize_button,
            'deploy': self.deploy_button,
            'readback': self.readback_button,
            'rollback': self.rollback_button,
            'restore_connectivity': self.restore_button,
            'cancel': self.cancel_button,
        }
        try:
            operations = self._controller.operations()
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: op derivation — expected failures fail closed; unexpected errors propagate to diagnostics
            self._fail_closed(
                '操作可否を評価できません: '
                + operation_error_message(exc))
            return
        for operation in operations:
            button = buttons[operation.command]
            button.setText(operation.label)
            button.setEnabled(operation.enabled)
            button.setToolTip(
                operation.disabled_reason
                or f'{operation.label}を実行します。')

    def _report_result(
        self, result: CommissioningOperationResult,
    ) -> None:
        self.result_label.setText(result.summary)
        set_semantic_state(
            self.result_label,
            SemanticState.SUCCESS if result.ok
            else SemanticState.ERROR)
        if self._on_status is not None:
            self._on_status(result.summary)

    def _run_command(self, fn) -> None:
        """Run one controller command, then re-render from the log."""
        for button in self._command_buttons():
            button.setEnabled(False)
        try:
            result = fn()
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: controller call — expected failures become an error result row; unexpected errors propagate to diagnostics
            result = CommissioningOperationResult(
                command='panel', ok=False, outcome='error',
                summary=operation_error_message(exc))
        self._report_result(result)
        self.refresh()

    # -- operator command handlers -------------------------------------

    def _on_advance(self) -> None:
        self._run_command(self._controller.advance)

    def _on_authorize(self) -> None:
        preview = self._controller.authorize_preview()
        if preview is None:
            self._report_result(CommissioningOperationResult(
                command='authorize_deploy', ok=False,
                outcome='unavailable',
                summary='承認対象の校正候補がありません'))
            return
        dialog = CommissioningApprovalDialog(preview, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        operator_id = dialog.operator_id
        note = dialog.note or None
        self._run_command(lambda: self._controller.authorize_deploy(
            operator_id=operator_id, note=note))

    def _on_deploy(self) -> None:
        preview = self._controller.deploy_preview()
        if preview is None:
            self._report_result(CommissioningOperationResult(
                command='deploy', ok=False, outcome='unavailable',
                summary='デプロイに必要な対象・候補・コンパイル結果'
                        'が揃っていません'))
            return
        dialog = CommissioningApprovalDialog(preview, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        operator_id = dialog.operator_id
        note = dialog.note or None
        self._run_command(lambda: self._controller.deploy(
            operator_id=operator_id, note=note))

    def _on_readback(self) -> None:
        self._run_command(self._controller.readback)

    def _on_rollback(self) -> None:
        preview = self._controller.rollback_preview()
        if preview is None:
            self._report_result(CommissioningOperationResult(
                command='rollback', ok=False, outcome='unavailable',
                summary='ロールバック対象のデプロイ記録がありません'))
            return
        dialog = CommissioningApprovalDialog(preview, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        operator_id = dialog.operator_id
        reason = dialog.note
        self._run_command(lambda: self._controller.rollback(
            operator_id=operator_id, reason=reason))

    def _on_restore(self) -> None:
        reason, ok = QInputDialog.getText(
            self, '接続復帰を記録', '復帰理由:')
        if not ok or not str(reason).strip():
            return
        self._run_command(lambda: self._controller.restore_connectivity(
            reason=str(reason).strip()))

    def _on_cancel(self) -> None:
        reason, ok = QInputDialog.getText(
            self, 'ランを中止', '中止理由:')
        if not ok or not str(reason).strip():
            return
        self._run_command(lambda: self._controller.cancel(
            reason=str(reason).strip()))

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

        self._render_operations()

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
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: authority read — expected failures fail closed; unexpected errors propagate to diagnostics
            self._fail_closed(
                'コミッショニング状態を読み込めません: '
                + operation_error_message(exc))
            return
        try:
            self._render(run, state)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: panel render — expected failures fail closed; unexpected errors propagate to diagnostics
            self._fail_closed(
                'コミッショニング状態を表示できません: '
                + operation_error_message(exc))


__all__ = [
    'CommissioningApprovalDialog',
    'CommissioningPanel',
]
