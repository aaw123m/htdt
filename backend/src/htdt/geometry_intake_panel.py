"""Geometry intake readiness panel (Issue #866).

Operator-facing surface for the intake chain. The panel is a pure
presentation widget: it shows the defect list split by origin
(source-model defects vs solver-limitation conditions), lets the
operator locate affected entities in the 3D workspace, accept/reject
each proposed repair action, and read the solver-readiness verdict.
All decisions flow outward through signals — the owning controller
persists acceptance and derives the geometry revision; the panel
itself never mutates geometry or writes records.

The locator logic is headless-testable: ``defect_locate_targets`` in
``cad_geometry_intake`` produces the entity highlight set and the
panel forwards it verbatim via ``locateRequested``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_geometry_intake import (
    GeometryDefect,
    GeometryIntakeReport,
    GeometryRepairAction,
    GeometryRepairProposal,
    GeometrySolverReadinessVerdict,
    defect_locate_targets,
    geometry_intake_label,
)


def _severity_label(severity: str) -> str:
    return geometry_intake_label(f'severity.{severity}')


def _defect_kind_label(kind: str) -> str:
    return geometry_intake_label(f'defect.{kind}')


def _verdict_label(verdict: str) -> str:
    return geometry_intake_label(f'verdict.{verdict}')


class GeometryIntakePanel(QWidget):
    """Defect/repair/readiness surface for geometry intake (#866).

    Signals:
      * ``locateRequested(tuple[str, ...])`` — scene entity ids to
        highlight in the 3D workspace (room-workspace selection idiom).
      * ``decisionRequested(str, str, dict)`` — action_id, 'accepted' or
        'rejected', and any operator parameters for the decision record.
    """

    locateRequested = Signal(tuple)
    decisionRequested = Signal(str, str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: GeometryIntakeReport | None = None
        self._proposal: GeometryRepairProposal | None = None
        self._verdict: GeometrySolverReadinessVerdict | None = None
        self._build_ui()

    # -- layout ---------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.readiness_label = QLabel(
            geometry_intake_label('section.readiness')
        )
        layout.addWidget(self.readiness_label)
        self.verdict_value = QLabel('—')
        layout.addWidget(self.verdict_value)
        self.readiness_reasons = QLabel('')
        self.readiness_reasons.setWordWrap(True)
        layout.addWidget(self.readiness_reasons)

        # --- source-model defects --------------------------------------
        self.source_group = QGroupBox(
            geometry_intake_label('section.source_defects')
        )
        source_layout = QVBoxLayout(self.source_group)
        self.source_table = QTableWidget(0, 4)
        self.source_table.setHorizontalHeaderLabels(
            ('重要度', '種別', '対象', '影響')
        )
        self.source_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.source_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.source_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        source_layout.addWidget(self.source_table)
        source_buttons = QHBoxLayout()
        self.locate_button = QPushButton(geometry_intake_label('ui.locate'))
        self.locate_button.setEnabled(False)
        self.locate_button.clicked.connect(self._emit_locate)
        source_buttons.addWidget(self.locate_button)
        source_buttons.addStretch(1)
        source_layout.addLayout(source_buttons)
        layout.addWidget(self.source_group)

        # --- solver-limitation conditions (visually distinct) -----------
        self.solver_group = QGroupBox(
            geometry_intake_label('section.solver_limitations')
        )
        solver_layout = QVBoxLayout(self.solver_group)
        solver_hint = QLabel(
            'これらはソルバー側の能力制約です。ソースモデルを変更するのではなく、'
            '対応ソルバーの選択またはポータル解決が必要です。'
        )
        solver_hint.setWordWrap(True)
        solver_layout.addWidget(solver_hint)
        self.solver_table = QTableWidget(0, 4)
        self.solver_table.setHorizontalHeaderLabels(
            ('重要度', '種別', '対象', '影響')
        )
        self.solver_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.solver_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.solver_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        solver_layout.addWidget(self.solver_table)
        layout.addWidget(self.solver_group)

        # --- repair proposal ---------------------------------------------
        self.proposal_group = QGroupBox(
            geometry_intake_label('section.repair_proposal')
        )
        proposal_layout = QVBoxLayout(self.proposal_group)
        self.proposal_table = QTableWidget(0, 6)
        self.proposal_table.setHorizontalHeaderLabels(
            ('修復アクション', '対象', '要操作', 'パラメータ', '承認', '却下')
        )
        self.proposal_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        proposal_layout.addWidget(self.proposal_table)
        layout.addWidget(self.proposal_group)

        self._wire_selection()

    def _wire_selection(self) -> None:
        # one defect selection at a time across the two tables —
        # selecting a row in one clears the other so the locate
        # affordance always targets the defect the operator picked last.
        self._clearing = False
        self.source_table.itemSelectionChanged.connect(
            lambda: self._exclusive_selection(self.source_table)
        )
        self.solver_table.itemSelectionChanged.connect(
            lambda: self._exclusive_selection(self.solver_table)
        )
        self.source_table.itemSelectionChanged.connect(
            self._sync_locate_enabled
        )
        self.solver_table.itemSelectionChanged.connect(
            self._sync_locate_enabled
        )

    def _exclusive_selection(self, table: QTableWidget) -> None:
        if self._clearing or table.currentRow() < 0:
            return
        self._clearing = True
        try:
            other = (
                self.solver_table
                if table is self.source_table
                else self.source_table
            )
            other.setCurrentCell(-1, -1)
            other.clearSelection()
        finally:
            self._clearing = False

    # -- content ---------------------------------------------------------

    def set_report(self, report: GeometryIntakeReport | None) -> None:
        self._report = report
        self._fill_defect_table(
            self.source_table,
            [d for d in (report.defects if report else ())
             if d.origin == 'source_model'],
        )
        self._fill_defect_table(
            self.solver_table,
            [d for d in (report.defects if report else ())
             if d.origin == 'solver_limitation'],
        )
        self._sync_locate_enabled()

    def set_proposal(
        self, proposal: GeometryRepairProposal | None
    ) -> None:
        self._proposal = proposal
        self.proposal_table.setRowCount(0)
        if proposal is None:
            return
        for action in proposal.actions:
            row = self.proposal_table.rowCount()
            self.proposal_table.insertRow(row)
            self.proposal_table.setItem(
                row, 0,
                QTableWidgetItem(
                    geometry_intake_label(f'repair.{action.kind}')
                ),
            )
            target = action.target_part_id or action.target_opening_id or '—'
            self.proposal_table.setItem(
                row, 1, QTableWidgetItem(target)
            )
            self.proposal_table.setItem(
                row, 2,
                QTableWidgetItem(
                    '操作員' if action.automation == 'operator_required'
                    else '自動'
                ),
            )
            self.proposal_table.setCellWidget(
                row, 3, self._parameter_editor(action)
            )
            accept = QPushButton(geometry_intake_label('ui.accept'))
            reject = QPushButton(geometry_intake_label('ui.reject'))
            accept.clicked.connect(
                lambda _=False, aid=action.action_id, r=row:
                self._request_decision(aid, 'accepted', r)
            )
            reject.clicked.connect(
                lambda _=False, aid=action.action_id, r=row:
                self._request_decision(aid, 'rejected', r)
            )
            self.proposal_table.setCellWidget(row, 4, accept)
            self.proposal_table.setCellWidget(row, 5, reject)

    def set_verdict(
        self, verdict: GeometrySolverReadinessVerdict | None
    ) -> None:
        self._verdict = verdict
        if verdict is None:
            self.verdict_value.setText('—')
            self.readiness_reasons.setText('')
            return
        self.verdict_value.setText(_verdict_label(verdict.verdict))
        lines: list[str] = []
        for reason in verdict.blocking_reasons:
            lines.append(f'[阻止] {reason.text}')
        for reason in verdict.evidence_gaps:
            lines.append(f'[証跡不足] {reason.text}')
        for reason in verdict.degraded_reasons:
            lines.append(f'[制限あり] {reason.text}')
        self.readiness_reasons.setText('\n'.join(lines))

    # -- helpers -----------------------------------------------------------

    def _fill_defect_table(
        self, table: QTableWidget, defects: list[GeometryDefect]
    ) -> None:
        table.setRowCount(0)
        for defect in defects:
            row = table.rowCount()
            table.insertRow(row)
            severity_item = QTableWidgetItem(
                _severity_label(defect.severity)
            )
            severity_item.setData(Qt.ItemDataRole.UserRole, defect)
            table.setItem(row, 0, severity_item)
            table.setItem(
                row, 1, QTableWidgetItem(_defect_kind_label(defect.kind))
            )
            targets = ','.join(
                defect.part_refs + defect.opening_refs
            ) or '—'
            table.setItem(row, 2, QTableWidgetItem(targets))
            table.setItem(
                row, 3, QTableWidgetItem(defect.consequence)
            )

    def _selected_defect(self) -> GeometryDefect | None:
        for table in (self.source_table, self.solver_table):
            row = table.currentRow()
            if row < 0:
                continue
            item = table.item(row, 0)
            if item is None:
                continue
            defect = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(defect, GeometryDefect):
                return defect
        return None

    def _sync_locate_enabled(self) -> None:
        defect = self._selected_defect()
        self.locate_button.setEnabled(
            defect is not None and bool(defect_locate_targets(defect))
        )

    def _emit_locate(self) -> None:
        defect = self._selected_defect()
        if defect is None:
            return
        targets = defect_locate_targets(defect)
        if targets:
            self.locateRequested.emit(targets)

    # -- per-action operator parameters -----------------------------------

    def _parameter_editor(self, action: GeometryRepairAction) -> QWidget:
        """Parameter widget for actions the operator must supply values for."""
        container = QWidget(self.proposal_table)
        form = QHBoxLayout(container)
        form.setContentsMargins(2, 0, 2, 0)
        if action.kind == 'assign_material':
            editor = QLineEdit()
            editor.setPlaceholderText('材質名')
            form.addWidget(editor)
        elif action.kind == 'resolve_portal':
            editor = QComboBox()
            editor.addItem(
                geometry_intake_label('resolution.open_portal'),
                'open_portal',
            )
            editor.addItem(
                geometry_intake_label('resolution.closed_boundary'),
                'closed_boundary',
            )
            form.addWidget(editor)
        elif action.kind == 'declare_units':
            editor = QComboBox()
            editor.addItem('m', 'meters')
            editor.addItem('mm', 'millimeters')
            editor.addItem('cm', 'centimeters')
            editor.addItem('ft', 'feet')
            editor.addItem('in', 'inches')
            scale = QDoubleSpinBox()
            scale.setRange(1e-9, 1e9)
            scale.setDecimals(9)
            scale.setValue(
                float(action.proposed_parameters.get('scale_to_meters', 1.0))
            )
            scale.setObjectName('scale_to_meters')
            form.addWidget(editor)
            form.addWidget(scale)
        elif action.kind == 'remove_disconnected_fragment':
            editor = QSpinBox()
            editor.setRange(0, 1 << 30)
            editor.setValue(
                int(action.proposed_parameters.get('component_index', 0))
            )
            form.addWidget(editor)
        else:
            form.addWidget(QLabel('—'))
        return container

    def _collect_parameters(self, row: int) -> dict[str, object]:
        widget = self.proposal_table.cellWidget(row, 3)
        if widget is None:
            return {}
        params: dict[str, object] = {}
        proposal = self._proposal
        action_kind = (
            proposal.actions[row].kind
            if proposal is not None and row < len(proposal.actions)
            else None
        )
        if action_kind == 'assign_material':
            editor = widget.findChild(QLineEdit)
            text = editor.text().strip() if editor is not None else ''
            if text:
                params['material_label'] = text
        elif action_kind == 'resolve_portal':
            editor = widget.findChild(QComboBox)
            if editor is not None:
                params['resolution'] = editor.currentData()
        elif action_kind == 'declare_units':
            editor = widget.findChild(QComboBox)
            scale = widget.findChild(QDoubleSpinBox)
            if editor is not None:
                params['source_unit'] = editor.currentData()
            if scale is not None:
                params['scale_to_meters'] = float(scale.value())
        elif action_kind == 'remove_disconnected_fragment':
            editor = widget.findChild(QSpinBox)
            if editor is not None:
                params['component_index'] = int(editor.value())
        return params

    def _request_decision(
        self, action_id: str, decision: str, row: int
    ) -> None:
        params = (
            self._collect_parameters(row)
            if decision == 'accepted'
            else {}
        )
        self.decisionRequested.emit(action_id, decision, params)


__all__ = [
    'GeometryIntakePanel',
]
