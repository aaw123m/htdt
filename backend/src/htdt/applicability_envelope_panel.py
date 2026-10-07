"""Applicability-envelope panel (issue #814, REV63).

Read-only rendering of :class:`ApplicabilityEnvelope`: seven dimension
rows (state + JA label + band + evidence count), the phenomenon matrix
(capability vs external validation vs input-bound claims), the claim-axis
rows, and the context-of-use decisions.

The panel never computes anything — it renders the composed envelope.
Absent evidence renders as 未取得 with UNSUPPORTED styling, never as a
passed state; FAILED / 適用域外 / 未サポート / 証拠不足 stay visually
distinct per issue §5.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_applicability_envelope import (
    ApplicabilityEnvelope,
    EnvelopeDimensionState,
    capability_state_label,
    envelope_claim_label,
    envelope_class_label,
    envelope_decision_label,
    envelope_decision_verdict_label,
    envelope_dimension_label,
    phenomenon_label,
    qualification_verdict_label,
    envelope_verdict_label,
)
from .measurement_evidence_display import confidence_bound_label
from .ui_theme import SemanticState, set_semantic_state


_CLASS_STATE: dict[str, SemanticState] = {
    'externally_validated': SemanticState.SUCCESS,
    'numerically_verified': SemanticState.SUCCESS,
    'holdout_validated': SemanticState.SUCCESS,
    'qualified': SemanticState.SUCCESS,
    'supported': SemanticState.WARNING,
    'bounded': SemanticState.WARNING,
    'insufficient_evidence': SemanticState.WARNING,
    'stale': SemanticState.STALE,
    'failed': SemanticState.ERROR,
    'outside_applicability': SemanticState.WARNING,
    'unsupported': SemanticState.UNSUPPORTED,
    'absent': SemanticState.UNSUPPORTED,
}


def _class_state(evidence_class: str | None) -> SemanticState | None:
    if evidence_class is None:
        return None
    return _CLASS_STATE.get(evidence_class, SemanticState.UNSUPPORTED)


def _state_item(text: str, evidence_class: str | None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setData(
        _SEMANTIC_STATE_ROLE, _class_state(evidence_class)
    )
    return item


#: QTableWidgetItem user-data role carrying the SemanticState string —
#: tests and delegates can read the class without reparsing text.
from PySide6.QtCore import Qt  # noqa: E402

_SEMANTIC_STATE_ROLE = Qt.ItemDataRole.UserRole + 41


class ApplicabilityEnvelopePanel(QWidget):
    """Renders one composed :class:`ApplicabilityEnvelope`."""

    def __init__(
        self,
        envelope: ApplicabilityEnvelope | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('applicabilityEnvelopePanel')

        layout = QVBoxLayout(self)
        self.context_label = QLabel('-')
        self.context_label.setObjectName('envelopeContextLabel')
        layout.addWidget(self.context_label)

        self.summary_label = QLabel(
            '能力の宣言は検証ではありません。各次元は独立して読んでください。'
        )
        self.summary_label.setObjectName('envelopeSummaryLabel')
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        layout.addWidget(QLabel('適用範囲エンベロープ（7次元）:'))
        self.dimension_table = QTableWidget(0, 5)
        self.dimension_table.setObjectName('envelopeDimensionTable')
        self.dimension_table.setHorizontalHeaderLabels(
            ['次元', '判定', '状態', '帯域', '証拠']
        )
        self.dimension_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.dimension_table.verticalHeader().setVisible(False)
        self.dimension_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.dimension_table)

        layout.addWidget(QLabel('現象マトリクス（能力 × 外部検証 × 入力）:'))
        self.phenomenon_table = QTableWidget(0, 6)
        self.phenomenon_table.setObjectName('envelopePhenomenonTable')
        self.phenomenon_table.setHorizontalHeaderLabels(
            ['現象', '能力', '能力帯域', '外部検証', '外部帯域', '入力判定']
        )
        self.phenomenon_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.phenomenon_table.verticalHeader().setVisible(False)
        self.phenomenon_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.phenomenon_table)

        layout.addWidget(QLabel('クレーム別入力適格性:'))
        self.claim_table = QTableWidget(0, 4)
        self.claim_table.setObjectName('envelopeClaimTable')
        self.claim_table.setHorizontalHeaderLabels(
            ['クレーム', '判定', '上限', '最弱次元']
        )
        self.claim_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.claim_table.verticalHeader().setVisible(False)
        self.claim_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.claim_table)

        layout.addWidget(QLabel('用途コンテキスト:'))
        self.decision_label = QLabel('-')
        self.decision_label.setObjectName('envelopeDecisionLabel')
        self.decision_label.setWordWrap(True)
        layout.addWidget(self.decision_label)

        self.staleness_label = QLabel('')
        self.staleness_label.setObjectName('envelopeStalenessLabel')
        self.staleness_label.setWordWrap(True)
        set_semantic_state(self.staleness_label, SemanticState.STALE)
        layout.addWidget(self.staleness_label)

        self._envelope: ApplicabilityEnvelope | None = None
        self.set_envelope(envelope)

    def set_envelope(self, envelope: ApplicabilityEnvelope | None) -> None:
        """Render ``envelope`` (or clear the panel on ``None``)."""
        self._envelope = envelope
        if envelope is None:
            self.context_label.setText('エンベロープなし')
            self.dimension_table.setRowCount(0)
            self.phenomenon_table.setRowCount(0)
            self.claim_table.setRowCount(0)
            self.decision_label.setText('-')
            self.staleness_label.setText('')
            return

        context = envelope.context
        context_bits = [f'ドキュメント: {context.document_id}']
        if context.adapter_id is not None:
            context_bits.append(
                f'ソルバー: {context.adapter_id} {context.adapter_version}'
            )
        if context.solver_path is not None:
            context_bits.append(f'パス: {context.solver_path}')
        context_bits.append(f'評価: {envelope.evaluated_at_utc}')
        self.context_label.setText(' / '.join(context_bits))

        self._fill_dimensions(envelope.dimensions)
        self._fill_phenomena(envelope)
        self._fill_claims(envelope)
        self.decision_label.setText(
            '\n'.join(
                f'{envelope_decision_label(row.decision)}: '
                f'{envelope_decision_verdict_label(row.verdict)}'
                + (
                    f'（{"、".join(row.basis_states)}）'
                    if row.basis_states
                    else ''
                )
                for row in envelope.decisions
            )
        )
        notes = [
            note
            for dim in envelope.dimensions
            for note in dim.staleness_notes
        ]
        if envelope.model_form_discrepancy:
            notes.append('モデル形式乖離が疑われます')
        self.staleness_label.setText('\n'.join(notes))

    def _fill_dimensions(
        self, dimensions: tuple[EnvelopeDimensionState, ...]
    ) -> None:
        table = self.dimension_table
        table.setRowCount(len(dimensions))
        for row_index, state in enumerate(dimensions):
            table.setItem(
                row_index,
                0,
                QTableWidgetItem(envelope_dimension_label(state.dimension)),
            )
            verdict_text = (
                envelope_verdict_label(state.verdict)
                if state.verdict is not None
                else '—'
            )
            table.setItem(row_index, 1, QTableWidgetItem(verdict_text))
            table.setItem(
                row_index,
                2,
                _state_item(
                    envelope_class_label(state.evidence_class),
                    state.evidence_class,
                ),
            )
            band_text = (
                f'{state.band.minimum_hz:g}–{state.band.maximum_hz:g} Hz'
                if state.band is not None
                else '—'
            )
            table.setItem(row_index, 3, QTableWidgetItem(band_text))
            detail = f'{len(state.evidence_refs)} 件'
            if state.conflicts:
                detail += (
                    ' / 他判定: '
                    + '、'.join(
                        envelope_verdict_label(v)
                        for v in state.conflicts
                    )
                )
            if state.staleness_notes:
                detail += ' / 陳腐化あり'
            table.setItem(row_index, 4, QTableWidgetItem(detail))

    def _fill_phenomena(self, envelope: ApplicabilityEnvelope) -> None:
        table = self.phenomenon_table
        cells = envelope.phenomenon_cells
        table.setRowCount(len(cells))
        for row_index, cell in enumerate(cells):
            table.setItem(
                row_index,
                0,
                QTableWidgetItem(phenomenon_label(cell.phenomenon)),
            )
            capability_text = (
                capability_state_label(cell.capability_state)
                if cell.capability_state is not None
                else '未取得'
            )
            capability_item = QTableWidgetItem(capability_text)
            if cell.capability_state == 'UNSUPPORTED':
                capability_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.UNSUPPORTED
                )
            elif cell.capability_state == 'BOUNDED':
                capability_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.WARNING
                )
            elif cell.capability_state == 'SUPPORTED':
                capability_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.WARNING
                )
            else:
                capability_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.UNSUPPORTED
                )
            table.setItem(row_index, 1, capability_item)
            band_text = (
                f'{cell.band.minimum_hz:g}–{cell.band.maximum_hz:g} Hz'
                if cell.band is not None
                else '—'
            )
            if cell.bound_description:
                band_text += f'（{cell.bound_description}）'
            table.setItem(row_index, 2, QTableWidgetItem(band_text))
            external_text = (
                '、'.join(
                    envelope_verdict_label(v)
                    for v in cell.external_verdicts
                )
                if cell.external_verdicts
                else '未取得'
            )
            table.setItem(
                row_index,
                3,
                _state_item(external_text, cell.external_evidence_class),
            )
            external_band_text = (
                f'{cell.external_band.minimum_hz:g}–'
                f'{cell.external_band.maximum_hz:g} Hz'
                if cell.external_band is not None
                else '—'
            )
            table.setItem(
                row_index, 4, QTableWidgetItem(external_band_text)
            )
            claim_text = (
                '、'.join(
                    confidence_bound_label(token.split(':', 1)[1])
                    + f'（{envelope_claim_label(token.split(":", 1)[0])}）'
                    for token in cell.claim_verdicts
                )
                if cell.claim_verdicts
                else '未取得'
            )
            table.setItem(row_index, 5, QTableWidgetItem(claim_text))

    def _fill_claims(self, envelope: ApplicabilityEnvelope) -> None:
        table = self.claim_table
        rows = envelope.claim_rows
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            table.setItem(
                row_index,
                0,
                QTableWidgetItem(envelope_claim_label(row.claim)),
            )
            verdict_item = QTableWidgetItem(
                confidence_bound_label(row.verdict)
            )
            if row.verdict == 'claim_denied':
                verdict_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.ERROR
                )
            elif row.verdict in ('solver_bounded', 'bounded_by_input'):
                verdict_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.WARNING
                )
            elif row.verdict == 'envelope_inherited':
                verdict_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.SUCCESS
                )
            else:
                verdict_item.setData(
                    _SEMANTIC_STATE_ROLE, SemanticState.UNSUPPORTED
                )
            table.setItem(row_index, 1, verdict_item)
            table.setItem(
                row_index, 2, QTableWidgetItem(row.ceiling)
            )
            table.setItem(
                row_index,
                3,
                QTableWidgetItem('、'.join(row.weakest_dimensions) or '—'),
            )


class ApplicabilityEnvelopeDialog(QDialog):
    """Dialog wrapper for the envelope panel — the read-only surface the
    support page opens for the current document."""

    def __init__(
        self,
        envelope: ApplicabilityEnvelope | None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('applicabilityEnvelopeDialog')
        self.setWindowTitle('適用範囲エンベロープ')
        self.resize(860, 720)
        layout = QVBoxLayout(self)
        self.panel = ApplicabilityEnvelopePanel(envelope, parent=self)
        layout.addWidget(self.panel)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
