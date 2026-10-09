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
from .dynamic_a11y import (
    STABLE_ID_ROLE,
    DynamicAnnouncer,
    capture_focus,
    disabled_hint,
    reason_label,
    restore_focus,
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
    ifcImportRequested = Signal()
    sceneSubjectRequested = Signal()
    diagnoseRequested = Signal()
    deriveRequested = Signal()
    solverSelectionChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report: GeometryIntakeReport | None = None
        self._proposal: GeometryRepairProposal | None = None
        self._verdict: GeometrySolverReadinessVerdict | None = None
        self._action_rows: dict = {}
        self._decided_actions: set[str] = set()
        self._solver_payloads: list[object] = []
        self._announcer = DynamicAnnouncer(self)
        self._build_ui()

    # -- layout ---------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # --- intake actions --------------------------------------------
        action_row = QHBoxLayout()
        self.import_ifc_button = QPushButton(
            geometry_intake_label('ui.import_ifc')
        )
        self.import_ifc_button.clicked.connect(self.ifcImportRequested)
        action_row.addWidget(self.import_ifc_button)
        self.adopt_scene_button = QPushButton(
            geometry_intake_label('ui.adopt_scene')
        )
        self.adopt_scene_button.clicked.connect(self.sceneSubjectRequested)
        action_row.addWidget(self.adopt_scene_button)
        self.diagnose_button = QPushButton(
            geometry_intake_label('ui.diagnose')
        )
        self.diagnose_button.clicked.connect(self._emit_diagnose)
        self.diagnose_button.setEnabled(False)
        action_row.addWidget(self.diagnose_button)
        self.derive_button = QPushButton(
            geometry_intake_label('ui.derive')
        )
        self.derive_button.clicked.connect(self._emit_derive)
        self.derive_button.setEnabled(False)
        action_row.addWidget(self.derive_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        # #975: a disabled primary action must explain itself on screen —
        # the button leaves the Tab order, so the reason + resolution
        # live in this focusable label, updated by ``set_stage``.
        self.stage_hint = reason_label('', self)
        self.stage_hint.setVisible(False)
        layout.addWidget(self.stage_hint)

        solver_row = QHBoxLayout()
        solver_row.addWidget(
            QLabel(geometry_intake_label('ui.solver'))
        )
        self.solver_combo = QComboBox()
        self.solver_combo.currentIndexChanged.connect(
            self._emit_solver_selection
        )
        solver_row.addWidget(self.solver_combo, 1)
        layout.addLayout(solver_row)
        self.decision_progress = QLabel('')
        self.decision_progress.setWordWrap(True)
        layout.addWidget(self.decision_progress)

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

        # #975: stable objectNames anchor focus-retention tokens across
        # rebuilds; tables name themselves for assistive technology.
        self.import_ifc_button.setObjectName('intake-import-ifc')
        self.adopt_scene_button.setObjectName('intake-adopt-scene')
        self.diagnose_button.setObjectName('intake-diagnose')
        self.derive_button.setObjectName('intake-derive')
        self.locate_button.setObjectName('intake-locate')
        self.source_table.setObjectName('intake-source-defects')
        self.source_table.setAccessibleName('ソースモデルの欠陥一覧')
        self.solver_table.setObjectName('intake-solver-limitations')
        self.solver_table.setAccessibleName('ソルバー能力の制約一覧')
        self.proposal_table.setObjectName('intake-proposal-actions')
        self.proposal_table.setAccessibleName('修復提案の操作一覧')
        self.import_ifc_button.setToolTip(
            'IFCファイルを取り込んで診断対象にします。'
        )
        self.adopt_scene_button.setToolTip(
            '現在のシーンを診断対象として採用します。'
        )
        self.locate_button.setToolTip(
            '一覧で欠陥を選択すると、その位置を3Dビューで確認できます。'
        )

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
        # #975: the tables rebuild from scratch on every sync — capture
        # the keyboard anchor first, then re-point it at the same
        # defect_id (or keep it on the view when the defect vanished).
        token = capture_focus(self)
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
        restore_focus(self, token, fallback=self.diagnose_button)
        if report is None:
            self._announcer.announce_state(
                'report', None, 'operation_completed', ''
            )
        else:
            self._announcer.announce_state(
                'report',
                report.report_id,
                'operation_completed',
                f'診断が完了しました — 欠陥 {len(report.defects)} 件',
            )

    def set_solver_options(
        self,
        options: list[tuple[str, object]],
    ) -> None:
        """Populate the target-solver combo: (label, payload) pairs."""
        self._solver_payloads = [payload for _, payload in options]
        current = self.solver_combo.currentData()
        self.solver_combo.blockSignals(True)
        self.solver_combo.clear()
        self.solver_combo.addItem('（未選択）', None)
        selected = 0
        for index, (label, payload) in enumerate(options, start=1):
            descriptor = payload[0] if isinstance(payload, tuple) else payload
            self.solver_combo.addItem(label, index - 1)
            if current == index - 1 or (
                self._verdict is not None
                and getattr(descriptor, 'descriptor_id', None)
                == self._verdict.adapter_descriptor_ref.ref_id
            ):
                selected = index
        self.solver_combo.setCurrentIndex(selected)
        self.solver_combo.blockSignals(False)
        self._emit_solver_selection()

    def current_solver_payload(self) -> object:
        index = self.solver_combo.currentData()
        if index is None:
            return None
        return self._solver_payloads[index]

    def _emit_solver_selection(self) -> None:
        self.solverSelectionChanged.emit(self.current_solver_payload())

    def set_decision_progress(self, text: str) -> None:
        self.decision_progress.setText(text)

    def set_stage(
        self,
        *,
        has_subject: bool,
        derive_enabled: bool = False,
        derive_blocked: str | None = None,
        derive_resolution: str | None = None,
    ) -> None:
        """Advance the workflow affordances to the chain's position.

        #975: a disabled primary button is unreachable by keyboard, so
        the 'why' and the 'how to clear it' are rendered on screen in
        ``stage_hint`` (and mirrored into the button tooltip for pointer
        users) — the control is never enabled just to look complete.
        """

        self.diagnose_button.setEnabled(has_subject)
        self.derive_button.setEnabled(derive_enabled)
        hints: list[str] = []
        if has_subject:
            self.diagnose_button.setToolTip(
                '取り込んだモデルの欠陥を診断します。'
            )
        else:
            hint = disabled_hint(
                'geometry-intake.diagnose',
                '診断を実行できません: 診断対象がまだありません',
                '「IFCをインポート」または「シーンを対象に採用」で'
                '対象を設定してください',
            )
            hints.append(hint)
            self.diagnose_button.setToolTip(hint)
        if derive_enabled:
            self.derive_button.setToolTip(
                '受理された修復を適用した派生リビジョンを生成します。'
            )
        else:
            hint = disabled_hint(
                'geometry-intake.derive',
                '派生リビジョンを生成できません: '
                + (
                    derive_blocked
                    or '修復提案への決定がまだ完了していません'
                ),
                derive_resolution
                or '各修復提案の承認/却下をすべて記録してください',
            )
            hints.append(hint)
            self.derive_button.setToolTip(hint)
        self.stage_hint.setText('\n'.join(hints))
        self.stage_hint.setVisible(bool(hints))

    def mark_decided(self, action_id: str) -> None:
        """Disable the accept/reject controls for a decided action."""
        if action_id in self._decided_actions:
            return
        self._decided_actions.add(action_id)
        if self._proposal is None:
            return
        for row, action in enumerate(self._proposal.actions):
            if action.action_id != action_id:
                continue
            for column in (4, 5):
                widget = self.proposal_table.cellWidget(row, column)
                if widget is not None:
                    widget.setEnabled(False)
        # #975: a persisted decision is a meaningful state change —
        # announced once per action, not on every re-render.
        self._announcer.announce(
            'save_state', '修復提案への決定を記録しました'
        )

    def set_proposal(
        self, proposal: GeometryRepairProposal | None
    ) -> None:
        # Re-rendering the same proposal keeps decided rows disabled;
        # only a genuinely new proposal resets the decided set.
        if (
            proposal is None
            or self._proposal is None
            or proposal.proposal_id != self._proposal.proposal_id
        ):
            self._decided_actions.clear()
        token = capture_focus(self)
        self._proposal = proposal
        self.proposal_table.setRowCount(0)
        self._action_rows = {}
        if proposal is None:
            restore_focus(self, token, fallback=self.derive_button)
            self._announcer.announce_state(
                'proposal', None, 'operation_completed', ''
            )
            return
        for action in proposal.actions:
            row = self.proposal_table.rowCount()
            self.proposal_table.insertRow(row)
            self._action_rows[action.action_id] = row
            repair_item = QTableWidgetItem(
                geometry_intake_label(f'repair.{action.kind}')
            )
            # #975: the row's stable identity for focus retention.
            repair_item.setData(STABLE_ID_ROLE, action.action_id)
            self.proposal_table.setItem(row, 0, repair_item)
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
            params_widget = self._parameter_editor(action)
            params_widget.setObjectName(
                f'intake-params:{action.action_id}'
            )
            self.proposal_table.setCellWidget(row, 3, params_widget)
            accept = QPushButton(geometry_intake_label('ui.accept'))
            reject = QPushButton(geometry_intake_label('ui.reject'))
            # #975: per-action objectNames let a focus token re-resolve
            # to the rebuilt button for the same action id.
            accept.setObjectName(f'intake-accept:{action.action_id}')
            reject.setObjectName(f'intake-reject:{action.action_id}')
            accept.setAccessibleName(
                f'{geometry_intake_label(f"repair.{action.kind}")} を承認'
            )
            reject.setAccessibleName(
                f'{geometry_intake_label(f"repair.{action.kind}")} を却下'
            )
            if action.action_id in self._decided_actions:
                accept.setEnabled(False)
                reject.setEnabled(False)
                decided_tip = (
                    'この提案への決定は記録済みです — '
                    'やり直す場合は受理レコードを見直してください'
                )
                accept.setToolTip(decided_tip)
                reject.setToolTip(decided_tip)
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
        restore_focus(self, token, fallback=self.derive_button)
        self._announcer.announce_state(
            'proposal',
            proposal.proposal_id,
            'operation_completed',
            f'修復提案を受け取りました — {len(proposal.actions)} 件の操作',
        )

    def focus_proposal_actions(self, action_ids) -> None:
        """Select + scroll to the proposal rows for the given actions —
        the repair-target leg of a defect locate (#977)."""
        rows = [
            self._action_rows[a]
            for a in action_ids
            if a in self._action_rows
        ]
        if not rows:
            return
        self.proposal_table.clearSelection()
        for row in rows:
            self.proposal_table.selectRow(row)
        first = self.proposal_table.item(rows[0], 0)
        if first is not None:
            self.proposal_table.scrollToItem(first)

    def set_verdict(
        self,
        verdict: GeometrySolverReadinessVerdict | None,
        *,
        evidence_state: str | None = None,
    ) -> None:
        self._verdict = verdict
        if verdict is None:
            self.verdict_value.setText('—')
            self.readiness_reasons.setText('')
            self._announcer.announce_state(
                'verdict', None, 'operation_completed', ''
            )
            return
        # #975: a verdict (or its evidence going stale) is announced
        # once per verdict/evidence-state pair — re-syncs of unchanged
        # state stay silent.
        stale = evidence_state is not None and evidence_state != 'current'
        if stale:
            self._announcer.announce_state(
                'verdict',
                f'{verdict.verdict_id}|{evidence_state}',
                'result_stale',
                'ソルバー適合性の証拠が最新ではありません — '
                f'{_verdict_label(verdict.verdict)}。'
                '再診断で確認してください',
            )
        else:
            self._announcer.announce_state(
                'verdict',
                f'{verdict.verdict_id}|{evidence_state or "current"}',
                'operation_completed',
                'ソルバー適合性が判定されました: '
                f'{_verdict_label(verdict.verdict)}',
            )
        verdict_text = _verdict_label(verdict.verdict)
        if evidence_state is not None and evidence_state != 'current':
            verdict_text += (
                f'（{geometry_intake_label(f"evidence.{evidence_state}")}）'
            )
        self.verdict_value.setText(verdict_text)
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
            # #975: UserRole carries the whole defect; the focus token
            # reads the stable defect_id from STABLE_ID_ROLE.
            severity_item.setData(STABLE_ID_ROLE, defect.defect_id)
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
        enabled = defect is not None and bool(
            defect_locate_targets(defect)
        )
        self.locate_button.setEnabled(enabled)
        if not enabled:
            self.locate_button.setToolTip(
                '一覧で位置情報を持つ欠陥を選択すると、'
                'その位置を3Dビューで確認できます。'
            )

    def _emit_locate(self) -> None:
        defect = self._selected_defect()
        if defect is None:
            return
        targets = defect_locate_targets(defect)
        if targets:
            self.locateRequested.emit(targets)

    def _emit_diagnose(self) -> None:
        # #975: the busy leg of the lifecycle — the request queues to
        # the controller; completion arrives via set_report.
        self._announcer.announce(
            'operation_queued', '診断を開始しました'
        )
        self.diagnoseRequested.emit()

    def _emit_derive(self) -> None:
        self._announcer.announce(
            'operation_queued', '派生リビジョンの生成を開始しました'
        )
        self.deriveRequested.emit()

    # -- per-action operator parameters -----------------------------------

    def _parameter_editor(self, action: GeometryRepairAction) -> QWidget:
        """Parameter widget for actions the operator must supply values for."""
        container = QWidget(self.proposal_table)
        form = QHBoxLayout(container)
        form.setContentsMargins(2, 0, 2, 0)
        if action.kind == 'assign_material':
            editor = QLineEdit()
            editor.setPlaceholderText('材質名')
            editor.setAccessibleName(
                f'{geometry_intake_label("repair.assign_material")} の材質名'
            )
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
                float(action.proposed_parameters.get('scale_to_meters') or 1.0)
            )
            scale.setObjectName('scale_to_meters')
            form.addWidget(editor)
            form.addWidget(scale)
        elif action.kind == 'remove_disconnected_fragment':
            editor = QSpinBox()
            editor.setRange(0, 1 << 30)
            editor.setValue(
                int(action.proposed_parameters.get('component_index') or 0)
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
