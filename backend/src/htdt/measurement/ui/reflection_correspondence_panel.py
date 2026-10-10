"""#1002 — '反射対応を確認' measured↔predicted correspondence compare surface.

Left: the measurement's ETC (display-derived envelope of the sealed IR)
with a time cursor and declared observed-event gates, plus the
registration / time-reference state. Right: the document's 3D viewport
with predicted reflection paths replayed from the sealed deterministic
path artifacts, and the pairing/candidate tables.

Authority discipline (issue contract):

- Verdicts and correspondence states come exclusively from the sealed
  pairing/set/verdict rows via ``build_correspondence_review`` — this
  widget never re-judges and never does nearest-peak matching.
- The human "save as hypothesis" action seals a ``manual_expert_label``
  pairing with time-only evidence; the canonical evaluator counts it
  ambiguous. Stored measurements are never modified.
- Missing measurement / IR / registration / predicted paths surface as
  explicit unsupported reasons with navigation targets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...cad_reflection_correspondence_repository import (
    CadReflectionCorrespondenceRepository,
)
from ...reflection_correspondence_review import (
    CorrespondenceReviewView,
    build_correspondence_review,
    build_manual_hypothesis_pairing,
    observed_event_from_manual_gate,
)
from ...error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)

if TYPE_CHECKING:
    from ...room_viewport import RoomViewport3D
    from ..services.measurement_workflow import (
        MeasurementWorkflowController,
    )

_STATE_LABELS = {
    'one_to_one': '1対1',
    'many_to_one': '多対1',
    'one_to_many': '1対多',
    'unresolved_cluster': '未解決クラスタ',
    'unmatched_predicted': '予測のみ(未対応)',
    'unmatched_observed': '観測のみ(未対応)',
    'ambiguous': '曖昧',
    'outside_observation_capability': '観測能力外',
}
_ALGORITHM_LABELS = {
    'manual_expert_label': '人手(仮説)',
    'time_gate_peak_match': '時間ゲート',
    'time_doa_geometric_match': '時間+DOA+幾何',
    'probabilistic_assignment': '確率的割当',
    'sparse_decomposition_match': '疎分解',
    'multi_position_joint_assignment': '多位置同時',
    'custom_validated': '検証済カスタム',
}
_EVIDENCE_LABELS = {
    'time_alignment': '時間',
    'direction_of_arrival': '到来方向',
    'geometric_path_consistency': '幾何パス',
    'reflection_order': '反射次数',
    'level_compatibility': 'レベル',
    'spectral_signature': 'スペクトル',
    'cross_position_consistency': '多位置',
}
_VERDICT_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '条件付き適格',
    'insufficient_evidence': '根拠不足',
    'registration_prerequisite_missing': '登録前提なし',
    'ambiguous_unresolved': '曖昧・未解決',
    'calibration_only_no_independent_validation': '校正専用(検証不可)',
}
_TIMING_METHOD_LABELS = {
    'shared_clock': '共有クロック',
    'trigger_alignment': 'トリガー整列',
    'loopback_reference': 'ループバック基準',
    'speaker_wire_reference': 'スピーカー線基準',
    'direct_path_alignment': '直接波整列',
    'unknown': '不明',
}

#: Below this width the measured/predicted panes stack vertically (#969-style
#: narrow handling — the review must stay readable without sideways scroll).
_NARROW_WIDTH_PX = 1160


def _card(title: str, parent: QWidget) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    frame.setObjectName('correspondenceCard')
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    layout = QVBoxLayout(frame)
    heading = QLabel(title, frame)
    heading.setStyleSheet('font-weight: 600;')
    layout.addWidget(heading)
    return frame, layout


class ReflectionCorrespondencePanel(QWidget):
    """The compare surface for one measurement (deep-link target)."""

    hypothesisSaved = Signal(str)  # pairing_id

    def __init__(
        self,
        controller: 'MeasurementWorkflowController',
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._view: CorrespondenceReviewView | None = None
        self._viewport: 'RoomViewport3D | None' = None
        self._viewport_failed = False
        self._selected_predicted_row_id: str | None = None
        self._selected_observed_row_id: str | None = None
        self._selected_pairing_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel('測定:', self))
        self.measurement_combo = QComboBox(self)
        self.measurement_combo.setObjectName(
            'correspondence_measurement_combo'
        )
        self.measurement_combo.setMinimumWidth(260)
        self.measurement_combo.currentIndexChanged.connect(
            self._on_measurement_changed
        )
        top_row.addWidget(self.measurement_combo, 1)
        self.refresh_button = QPushButton('再読み込み', self)
        self.refresh_button.setObjectName('correspondence_refresh_button')
        self.refresh_button.clicked.connect(self.refresh)
        top_row.addWidget(self.refresh_button)
        root.addLayout(top_row)

        self.state_label = QLabel('', self)
        self.state_label.setObjectName('correspondence_state_label')
        self.state_label.setWordWrap(True)
        root.addWidget(self.state_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        # -- left: measured ETC + registration state -------------------------
        measured_card, measured_layout = _card(
            '実測 ETC（表示用包絡 — 観測値ではありません）', self
        )
        self.etc_plot = pg.PlotWidget(measured_card)
        self.etc_plot.setObjectName('correspondence_etc_plot')
        self.etc_plot.setMinimumHeight(240)
        self.etc_plot.setLabel('bottom', '時間', units='s')
        self.etc_plot.setLabel('left', '包絡', units='dB')
        self.etc_plot.showGrid(x=True, y=True, alpha=0.25)
        measured_layout.addWidget(self.etc_plot, 1)

        self.cursor_line = pg.InfiniteLine(
            pos=0.0, movable=True, angle=90,
            pen=pg.mkPen('#ffd166', width=2, style=Qt.PenStyle.DashLine),
        )
        self.cursor_line.sigPositionChanged.connect(self._on_cursor_moved)
        self.cursor_line.setVisible(False)
        self.etc_plot.addItem(self.cursor_line)

        gate_row = QHBoxLayout()
        gate_row.addWidget(QLabel('ゲート (ms):', measured_card))
        self.gate_start = QDoubleSpinBox(measured_card)
        self.gate_start.setObjectName('correspondence_gate_start')
        self.gate_start.setRange(0.0, 60_000.0)
        self.gate_start.setDecimals(2)
        gate_row.addWidget(self.gate_start)
        gate_row.addWidget(QLabel('〜', measured_card))
        self.gate_end = QDoubleSpinBox(measured_card)
        self.gate_end.setObjectName('correspondence_gate_end')
        self.gate_end.setAccessibleName('ゲート終了時刻')
        self.gate_end.setRange(0.0, 60_000.0)
        self.gate_end.setDecimals(2)
        gate_row.addWidget(self.gate_end)
        self.gate_start.valueChanged.connect(self._update_cursor_gate)
        self.gate_end.valueChanged.connect(self._update_cursor_gate)
        gate_row.addStretch(1)
        self.cursor_label = QLabel('カーソル: —', measured_card)
        self.cursor_label.setObjectName('correspondence_cursor_label')
        gate_row.addWidget(self.cursor_label)
        measured_layout.addLayout(gate_row)

        self.gate_region = pg.LinearRegionItem(
            values=(0.0, 0.0),
            orientation=pg.LinearRegionItem.Vertical,
            movable=False,
            brush=pg.mkBrush(255, 209, 102, 40),
        )
        self.gate_region.setVisible(False)
        self.etc_plot.addItem(self.gate_region)

        self.registration_label = QLabel('', measured_card)
        self.registration_label.setObjectName(
            'correspondence_registration_label'
        )
        self.registration_label.setWordWrap(True)
        measured_layout.addWidget(self.registration_label)
        self.splitter.addWidget(measured_card)

        # -- right: 3D prediction + tables ----------------------------------
        predicted_card, predicted_layout = _card(
            '予測反射パス（3D）と対応候補', self
        )
        self.viewport_holder = QVBoxLayout()
        self.viewport_holder.setContentsMargins(0, 0, 0, 0)
        holder_widget = QWidget(predicted_card)
        holder_widget.setLayout(self.viewport_holder)
        holder_widget.setMinimumHeight(220)
        predicted_layout.addWidget(holder_widget, 2)
        self.viewport_fallback = QLabel('', holder_widget)
        self.viewport_fallback.setObjectName(
            'correspondence_viewport_fallback'
        )
        self.viewport_fallback.setWordWrap(True)
        self.viewport_fallback.setVisible(False)
        self.viewport_holder.addWidget(self.viewport_fallback)

        tables_row = QHBoxLayout()
        self.predicted_table = self._make_table(
            ('予測パス', '到達 ms', '面', '版', '状態'),
            'correspondence_predicted_table',
            '予測反射パス一覧',
            predicted_card,
        )
        tables_row.addWidget(self.predicted_table, 1)
        self.observed_table = self._make_table(
            ('観測イベント', '時刻 ms', '幅 ms', '抽出', 'DOA'),
            'correspondence_observed_table',
            '観測イベント一覧',
            predicted_card,
        )
        tables_row.addWidget(self.observed_table, 1)
        predicted_layout.addLayout(tables_row)

        self.pairing_table = self._make_table(
            (
                '対応状態', '観測 ms', '予測 ms', 'Δt ms', 'DOA',
                'アルゴリズム', '根拠次元', '役割', '参照',
            ),
            'correspondence_pairing_table',
            '予測・観測対応一覧',
            predicted_card,
        )
        predicted_layout.addWidget(self.pairing_table, 2)

        action_row = QHBoxLayout()
        self.detail_label = QLabel('', predicted_card)
        self.detail_label.setObjectName('correspondence_detail')
        self.detail_label.setWordWrap(True)
        action_row.addWidget(self.detail_label, 1)
        self.hypothesis_button = QPushButton(
            '候補を仮説として保存', predicted_card
        )
        self.hypothesis_button.setObjectName('correspondence_hypothesis_button')
        self.hypothesis_button.setEnabled(False)
        self.hypothesis_button.setToolTip(
            '選択した予測パスと観測イベント（またはカーソル位置の'
            '手動ゲート）の対応を「仮説」注釈として保存します。'
            '判定は正規評価器のみが行います。'
        )
        self.hypothesis_button.clicked.connect(self._save_hypothesis)
        action_row.addWidget(self.hypothesis_button)
        predicted_layout.addLayout(action_row)
        self.splitter.addWidget(predicted_card)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        self._populate_measurement_options()
        self.refresh()

    # -- helpers -------------------------------------------------------------

    def _make_table(
        self,
        headers: tuple[str, ...],
        name: str,
        accessible_name: str,
        parent: QWidget,
    ) -> QTableWidget:
        table = QTableWidget(parent)
        table.setObjectName(name)
        table.setAccessibleName(accessible_name)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setMinimumHeight(120)
        table.itemSelectionChanged.connect(
            lambda t=table: self._on_table_selection(t)
        )
        return table

    def _populate_measurement_options(self) -> None:
        current = self.measurement_combo.currentData()
        self.measurement_combo.blockSignals(True)
        self.measurement_combo.clear()
        try:
            views = self._controller.measurement_views()
        except EXPECTED_OPERATION_ERRORS:
            views = ()
        for view in views:
            label = (
                f'{view.effective_target_name or view.target_name}'
                f' · {view.measurement_id}'
            )
            self.measurement_combo.addItem(label, view.measurement_id)
        if current is not None:
            index = self.measurement_combo.findData(current)
            if index >= 0:
                self.measurement_combo.setCurrentIndex(index)
        self.measurement_combo.blockSignals(False)

    def reload_measurement_options(self) -> None:
        """Re-list the measurement combo (workspace refresh hook)."""
        self._populate_measurement_options()

    def select_measurement(self, measurement_id: str) -> bool:
        """Deep-link focus port: pick the measurement and refresh."""
        index = self.measurement_combo.findData(measurement_id)
        if index < 0:
            return False
        self.measurement_combo.setCurrentIndex(index)
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        self._apply_splitter_orientation(event.size().width())

    def _apply_splitter_orientation(self, width: int) -> None:
        orientation = (
            Qt.Orientation.Vertical
            if width < _NARROW_WIDTH_PX
            else Qt.Orientation.Horizontal
        )
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)

    # -- data ----------------------------------------------------------------

    def refresh(self) -> None:
        measurement_id = self.measurement_combo.currentData()
        try:
            self._view = build_correspondence_review(
                self._controller.scene_repository,
                self._controller.document_id,
                measurement_id,
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='反射対応の読み込み')
            self._view = None
            self.state_label.setText(
                f'対応ビューの構築に失敗しました: {exc}'
            )
            return
        self._render_state_banner()
        self._render_registration()
        self._render_etc()
        self._render_tables()
        self._render_viewport()

    def _render_state_banner(self) -> None:
        view = self._view
        if view is None:
            return
        lines: list[str] = []
        if view.availability == 'ready':
            lines.append(
                f'測定 {view.measurement_id} — '
                f'登録 {len(view.registrations)} 件 / '
                f'予測パス {len(view.predicted_rows)} / '
                f'宣言済み対応 {len(view.pairing_rows)} 件'
            )
        else:
            lines.extend(view.availability_reasons)
            if view.measurement_options:
                lines.append(
                    'データのある測定: '
                    + ', '.join(view.measurement_options[:6])
                )
        lines.extend(view.guidance_issues)
        self.state_label.setText('\n'.join(lines))

    def _render_registration(self) -> None:
        view = self._view
        if view is None:
            return
        if not view.registrations:
            self.registration_label.setText(
                '時刻基準/登録状態: 登録なし — 絶対時刻対応は定義されません'
            )
            return
        lines = []
        for registration in view.registrations:
            method = _TIMING_METHOD_LABELS.get(
                registration.timing_method, registration.timing_method
            )
            parts = [
                f'登録 {registration.registration_id}',
                f'比較可能性: {registration.comparability_state}',
                f'時刻方式: {method}',
            ]
            if registration.timing_offset_s is not None:
                parts.append(
                    f'オフセット {registration.timing_offset_s * 1000:.2f} ms'
                )
            if registration.timing_uncertainty_s is not None:
                parts.append(
                    f'不確かさ ±{registration.timing_uncertainty_s * 1000:.2f} ms'
                )
            parts.append(
                '到達時刻: 対応可'
                if registration.arrival_time_supported
                else '到達時刻: 根拠不足'
            )
            if registration.stale:
                parts.append('【旧シーン版 — stale】')
            if registration.comparability_reasons:
                parts.append(' / '.join(registration.comparability_reasons))
            lines.append(' · '.join(parts))
        self.registration_label.setText(
            '時刻基準/登録状態:\n' + '\n'.join(lines)
        )

    def _render_etc(self) -> None:
        plot = self.etc_plot
        for item in list(plot.listDataItems()):
            plot.removeItem(item)
        for item in list(plot.items()):
            if isinstance(item, pg.LinearRegionItem) and (
                item is not self.gate_region
            ):
                plot.removeItem(item)
            elif isinstance(item, pg.InfiniteLine) and (
                item is not self.cursor_line
            ):
                plot.removeItem(item)
        view = self._view
        if view is None or view.etc_curve is None:
            self.cursor_line.setVisible(False)
            self.gate_region.setVisible(False)
            return
        curve = view.etc_curve
        plot.plot(
            curve.times_s,
            curve.level_db,
            pen=pg.mkPen('#9ecfff', width=1),
            name=f'ETC包絡 ({curve.dataset_id} · 表示専用)',
        )
        # Declared observed events → gate regions (never fabricated peaks).
        for row in view.observed_rows:
            if row.observed_extent_s is not None:
                region = pg.LinearRegionItem(
                    values=row.observed_extent_s,
                    orientation=pg.LinearRegionItem.Vertical,
                    movable=False,
                    brush=pg.mkBrush(120, 220, 140, 60),
                )
                region.correspondence_row_id = row.row_id
                plot.addItem(region)
            line = pg.InfiniteLine(
                pos=row.observed_time_s,
                angle=90,
                movable=False,
                pen=pg.mkPen('#7ddc8c', width=1, style=Qt.PenStyle.DashLine),
            )
            plot.addItem(line)
        # Predicted arrivals → thin markers (registration-adjusted not yet
        # applied — the plot is in measurement time; adjusted times are a
        # separate column in the pairing table).
        for row in view.predicted_rows:
            line = pg.InfiniteLine(
                pos=row.predicted_arrival_s,
                angle=90,
                movable=False,
                pen=pg.mkPen('#b08cff', width=1, style=Qt.PenStyle.DotLine),
            )
            plot.addItem(line)
        self.cursor_line.setVisible(True)
        self._update_cursor_gate()

    def _on_cursor_moved(self) -> None:
        if self._view is None or self._view.etc_curve is None:
            return
        t_s = float(self.cursor_line.value())
        self.cursor_label.setText(f'カーソル: {t_s * 1000:.2f} ms')
        self.gate_start.blockSignals(True)
        self.gate_end.blockSignals(True)
        span_ms = max(
            0.25,
            1000.0 / float(self._view.etc_curve.sample_rate_hz),
        )
        self.gate_start.setValue(t_s * 1000.0)
        self.gate_end.setValue(t_s * 1000.0 + span_ms)
        self.gate_start.blockSignals(False)
        self.gate_end.blockSignals(False)
        self._update_cursor_gate()

    def _update_cursor_gate(self) -> None:
        if self._view is None or self._view.etc_curve is None:
            return
        start_s = self.gate_start.value() / 1000.0
        end_s = self.gate_end.value() / 1000.0
        if end_s < start_s:
            start_s, end_s = end_s, start_s
        self.gate_region.setRegion((start_s, end_s))
        self.gate_region.setVisible(True)
        self._update_hypothesis_enabled()

    def _render_tables(self) -> None:
        view = self._view
        if view is None:
            return
        self.predicted_table.setRowCount(len(view.predicted_rows))
        for i, row in enumerate(view.predicted_rows):
            cells = (
                f'{row.source_entity_id}→{row.receiver_entity_id}',
                f'{row.predicted_arrival_s * 1000:.2f}',
                ' / '.join(row.surface_ids) or '—',
                row.scene_revision_id[:12],
                'stale' if row.stale else 'current',
            )
            self._fill_row(self.predicted_table, i, row.row_id, cells)
        self.observed_table.setRowCount(len(view.observed_rows))
        for i, row in enumerate(view.observed_rows):
            extent_ms = (
                (row.observed_extent_s[1] - row.observed_extent_s[0])
                * 1000.0
                if row.observed_extent_s is not None
                else None
            )
            cells = (
                row.measurement_ref_id,
                f'{row.observed_time_s * 1000:.2f}',
                f'{extent_ms:.2f}' if extent_ms is not None else '—',
                f'{row.extraction_algorithm} v{row.extraction_version}',
                (
                    f'{row.observed_doa[0]:.2f},{row.observed_doa[1]:.2f},'
                    f'{row.observed_doa[2]:.2f}'
                    if row.observed_doa is not None
                    else 'なし'
                ),
            )
            self._fill_row(self.observed_table, i, row.row_id, cells)
        self.pairing_table.setRowCount(len(view.pairing_rows))
        predicted_by_id = {
            row.row_id: row for row in view.predicted_rows
        }
        observed_by_id = {
            row.row_id: row for row in view.observed_rows
        }
        for i, row in enumerate(view.pairing_rows):
            predicted = predicted_by_id.get(row.predicted_row_id or '')
            observed = observed_by_id.get(row.observed_row_id or '')
            delta_ms = None
            if predicted is not None and observed is not None:
                delta_ms = (
                    observed.observed_time_s - predicted.predicted_arrival_s
                ) * 1000.0
            doa_text = '—'
            if observed is not None and observed.observed_doa is not None:
                doa_text = (
                    f'{observed.observed_doa[0]:.2f},'
                    f'{observed.observed_doa[1]:.2f},'
                    f'{observed.observed_doa[2]:.2f}'
                )
                if observed.doa_uncertainty is not None:
                    doa_text += f' ±{observed.doa_uncertainty:.2f}'
            refs = row.pairing_id
            if row.is_hypothesis:
                refs += ' (仮説)'
            cells = (
                _STATE_LABELS.get(
                    row.correspondence_state, row.correspondence_state
                ),
                (
                    f'{observed.observed_time_s * 1000:.2f}'
                    if observed is not None
                    else '—'
                ),
                (
                    f'{predicted.predicted_arrival_s * 1000:.2f}'
                    if predicted is not None
                    else '—'
                ),
                f'{delta_ms:+.2f}' if delta_ms is not None else '—',
                doa_text,
                _ALGORITHM_LABELS.get(
                    row.matching_algorithm, row.matching_algorithm
                ),
                ' + '.join(
                    _EVIDENCE_LABELS.get(d, d)
                    for d in row.evidence_dimensions
                ) or '—',
                row.validation_role,
                refs,
            )
            self._fill_row(self.pairing_table, i, row.pairing_id, cells)

    def _fill_row(
        self,
        table: QTableWidget,
        row_index: int,
        row_id: str,
        cells: tuple[str, ...],
    ) -> None:
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row_id)
            if column == 0:
                item.setToolTip(row_id)
            table.setItem(row_index, column, item)

    def _on_table_selection(self, table: QTableWidget) -> None:
        items = table.selectedItems()
        row_id = (
            items[0].data(Qt.ItemDataRole.UserRole) if items else None
        )
        if table is self.predicted_table:
            self._selected_predicted_row_id = row_id
            self._sync_overlay_highlight()
        elif table is self.observed_table:
            self._selected_observed_row_id = row_id
        elif table is self.pairing_table:
            self._selected_pairing_id = row_id
            self._cross_select_pairing_sides(row_id)
            self._show_pairing_detail(row_id)
            self._sync_overlay_highlight()
        self._update_hypothesis_enabled()

    def _select_table_row(
        self, table: QTableWidget, row_id: str | None
    ) -> None:
        if row_id is None:
            return
        for index in range(table.rowCount()):
            item = table.item(index, 0)
            if (
                item is not None
                and item.data(Qt.ItemDataRole.UserRole) == row_id
            ):
                with QSignalBlocker(table):
                    table.selectRow(index)
                return

    def _cross_select_pairing_sides(self, pairing_id: str | None) -> None:
        """Mutual highlight (requirement 3): a pairing click selects the
        exact observed/predicted rows it binds — never a nearest guess."""

        view = self._view
        if view is None or pairing_id is None:
            return
        row = next(
            (r for r in view.pairing_rows if r.pairing_id == pairing_id),
            None,
        )
        if row is None:
            return
        self._select_table_row(
            self.predicted_table, row.predicted_row_id
        )
        self._select_table_row(
            self.observed_table, row.observed_row_id
        )
        if row.predicted_row_id is not None:
            self._selected_predicted_row_id = row.predicted_row_id
        if row.observed_row_id is not None:
            self._selected_observed_row_id = row.observed_row_id

    def _show_pairing_detail(self, pairing_id: str | None) -> None:
        view = self._view
        if view is None or pairing_id is None:
            return
        row = next(
            (r for r in view.pairing_rows if r.pairing_id == pairing_id),
            None,
        )
        if row is None:
            return
        parts = [
            f'対応 {row.pairing_id}',
            f'状態: {_STATE_LABELS.get(row.correspondence_state, row.correspondence_state)}',
        ]
        if row.cluster_member_ids:
            parts.append(
                'クラスタ: ' + ', '.join(row.cluster_member_ids[:6])
            )
        if row.ambiguity_note:
            parts.append(f'注記: {row.ambiguity_note}')
        for verdict in view.verdict_rows:
            if row.set_ids and verdict.set_id in row.set_ids:
                parts.append(
                    '判定: '
                    + _VERDICT_LABELS.get(verdict.state, verdict.state)
                )
                parts.extend(f'  - {reason}' for reason in verdict.reasons)
                parts.extend(
                    f'  - {limit}' for limit in verdict.limitations
                )
        self.detail_label.setText('\n'.join(parts))

    def _update_hypothesis_enabled(self) -> None:
        has_observed = (
            self._selected_observed_row_id is not None
            or self.gate_region.isVisible()
        )
        self.hypothesis_button.setEnabled(
            self._selected_predicted_row_id is not None and has_observed
        )

    def _render_viewport(self) -> None:
        view = self._view
        if view is None:
            return
        if self._viewport is None and not self._viewport_failed:
            try:
                from ...room_viewport import RoomOverlayState, RoomViewport3D

                self._viewport = RoomViewport3D(self)
                self._viewport.setMinimumHeight(220)
                self.viewport_holder.addWidget(self._viewport)
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='3Dビューの初期化')
                self._viewport_failed = True
                self.viewport_fallback.setVisible(True)
                self.viewport_fallback.setText(
                    'この環境では3D表示を利用できません。'
                    '下の表で対応を確認してください。'
                )
                return
        if self._viewport is None:
            return
        from ...room_viewport import RoomOverlayState

        try:
            latest = self._controller.latest_revision()
            with self._viewport.deferred_render():
                self._viewport.render_document(
                    latest.document,
                    selected_id=None,
                    overlays=RoomOverlayState(grid=True, labels=True),
                    reset_camera=True,
                )
                self._sync_overlay_highlight()
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='3Dビューの描画')
            self._viewport_failed = True
            self.viewport_fallback.setVisible(True)
            self.viewport_fallback.setText(
                '3D表示の描画に失敗しました。下の表で対応を確認してください。'
            )

    def _sync_overlay_highlight(self) -> None:
        view = self._view
        if view is None or self._viewport is None:
            return
        highlight_ids: set[str] = set()
        classes: dict[str, str] = {}
        if self._selected_pairing_id is not None:
            pairing = next(
                (
                    r
                    for r in view.pairing_rows
                    if r.pairing_id == self._selected_pairing_id
                ),
                None,
            )
            if pairing is not None:
                member_ids = set(pairing.cluster_member_ids) | {
                    pairing.pairing_id
                }
                cluster_class = (
                    'hypothesis'
                    if pairing.is_hypothesis
                    else (
                        'ambiguous'
                        if pairing.correspondence_state
                        in (
                            'ambiguous',
                            'unresolved_cluster',
                            'one_to_many',
                            'many_to_one',
                        )
                        else 'matched'
                    )
                )
                for member in view.pairing_rows:
                    if member.pairing_id in member_ids and (
                        member.predicted_row_id is not None
                    ):
                        highlight_ids.add(member.predicted_row_id)
                        classes[member.predicted_row_id] = cluster_class
        elif self._selected_predicted_row_id is not None:
            highlight_ids.add(self._selected_predicted_row_id)
            classes[self._selected_predicted_row_id] = 'matched'
        self._viewport.render_reflection_correspondence_overlay(
            [
                (row.row_id, row.geometry_points)
                for row in view.predicted_rows
            ],
            highlighted_row_ids=tuple(sorted(highlight_ids)),
            path_classes=classes,
        )

    def _on_measurement_changed(self) -> None:
        self._selected_predicted_row_id = None
        self._selected_observed_row_id = None
        self._selected_pairing_id = None
        self.detail_label.setText('')
        self.refresh()

    # -- hypothetical annotation -------------------------------------------

    def _save_hypothesis(self) -> None:
        view = self._view
        if view is None or self._selected_predicted_row_id is None:
            return
        predicted = next(
            (
                r
                for r in view.predicted_rows
                if r.row_id == self._selected_predicted_row_id
            ),
            None,
        )
        if predicted is None:
            return
        observed_event = None
        if self._selected_observed_row_id is not None:
            row = next(
                (
                    r
                    for r in view.observed_rows
                    if r.row_id == self._selected_observed_row_id
                ),
                None,
            )
            if row is not None:
                observed_event = row.event
        if observed_event is None:
            if view.etc_curve is None or view.measurement_id is None:
                return
            dataset = self._controller.ir_datasets_for_measurement(
                view.measurement_id
            )[0]
            start_s = self.gate_start.value() / 1000.0
            end_s = self.gate_end.value() / 1000.0
            observed_event = observed_event_from_manual_gate(
                measurement_id=view.measurement_id,
                dataset=dataset,
                observed_time_s=(start_s + end_s) / 2.0,
                observed_extent_s=(min(start_s, end_s), max(start_s, end_s)),
            )
        try:
            pairing = build_manual_hypothesis_pairing(
                document_id=view.document_id,
                predicted_path=predicted.predicted_path,
                observed_event=observed_event,
            )
            repository = CadReflectionCorrespondenceRepository(
                self._controller.scene_repository
            )
            repository.save_pairing(pairing)
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='仮説の保存')
            self.detail_label.setText(f'仮説の保存に失敗しました: {exc}')
            return
        self.hypothesisSaved.emit(pairing.pairing_id)
        self.detail_label.setText(
            f'仮説 {pairing.pairing_id} を保存しました — '
            '判定は正規評価器のみが行います'
        )
        self.refresh()


__all__ = ['ReflectionCorrespondencePanel']
