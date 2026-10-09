"""Per-seat coverage display panel (Issue #1001).

Qt surface over :mod:`room_coverage_overlay`: the toggle arms the read-only
3D overlay, the selectors pick the sealed evaluation / exact-grid frequency /
comparison quantity / A/B baseline, the seat table binds every row to the
stable ``seat_entity_id``, and the detail block exposes the seat's
per-frequency results, provenance identity and failure remediation — all
verbatim from the authority. The panel never recomputes coverage, never
interpolates between seats, and never auto-fixes or approves the model.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .room_coverage_overlay import (
    COVERAGE_BAND_ASCII,
    COVERAGE_BAND_COLORS,
    COVERAGE_DISCLAIMER_JA,
    COVERAGE_FAIL_COLOR,
    COVERAGE_PASS_COLOR,
    COVERAGE_QUANTITY_LABELS,
    COVERAGE_QUANTITY_ORDER,
    COVERAGE_UNKNOWN_COLOR,
    COVERAGE_UNDECIDED_COLOR,
    CoverageEvaluationOption,
    CoverageOverlayRequest,
    CoverageOverlayScene,
    CoverageSeatRow,
)
from .ui_theme import TypographyRole, set_typography_role


class RoomSeatCoveragePanel(QWidget):
    """Read-only 座席カバレッジ表示 controls for the room placement page."""

    changed = Signal()
    seatSelected = Signal(object)  # str seat_entity_id | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('roomSeatCoveragePanel')
        self._scene: CoverageOverlayScene | None = None
        self._options: tuple[CoverageEvaluationOption, ...] = ()
        self._syncing = False
        self._rows_by_id: dict[str, int] = {}
        self._ids_by_row: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QLabel('座席カバレッジ（読み取り専用・評価の証跡点）')
        heading.setWordWrap(True)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.coverage_toggle = QCheckBox('カバレッジ表示')
        self.coverage_toggle.setObjectName('seatCoverageToggle')
        self.coverage_toggle.setToolTip(
            '封じられたカバレッジ評価の座席マーカーを部屋3D上に重ねます。'
            'マーカーは各座席の耳位置証跡点のみを示します（席間補間なし）'
        )
        self.coverage_toggle.toggled.connect(self._emit_change)
        layout.addWidget(self.coverage_toggle)

        self.evaluation_combo = QComboBox()
        self.evaluation_combo.setObjectName('seatCoverageEvaluation')
        self.evaluation_combo.setToolTip(
            '表示するカバレッジ評価（バリアント＋音源の組み合わせ）。'
            '現行シーン以外の評価は履歴として淡色表示されます'
        )
        self.evaluation_combo.currentIndexChanged.connect(
            self._on_evaluation_changed
        )
        layout.addWidget(self.evaluation_combo)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        self.frequency_combo = QComboBox()
        self.frequency_combo.setObjectName('seatCoverageFrequency')
        self.frequency_combo.setToolTip(
            '表示周波数 — 評価の要求グリッド上の値のみ選択可能です'
            '（グリッド外はブロックされ、補間は行いません）'
        )
        self.frequency_combo.currentIndexChanged.connect(
            lambda _i: self._emit_change()
        )
        row_layout.addWidget(self.frequency_combo, 1)

        self.quantity_combo = QComboBox()
        self.quantity_combo.setObjectName('seatCoverageQuantity')
        self.quantity_combo.setToolTip(
            '比較量 — 相対指向性レベル / オフアキシス損失 / 適合判定'
        )
        for quantity in COVERAGE_QUANTITY_ORDER:
            self.quantity_combo.addItem(
                COVERAGE_QUANTITY_LABELS[quantity], quantity
            )
        self.quantity_combo.currentIndexChanged.connect(
            lambda _i: self._emit_change()
        )
        row_layout.addWidget(self.quantity_combo, 1)
        layout.addWidget(row)

        self.baseline_combo = QComboBox()
        self.baseline_combo.setObjectName('seatCoverageBaseline')
        self.baseline_combo.setToolTip(
            'A/B 比較の基準評価 — 同一ベースラインリビジョン・同一耳位置・'
            '同一周波数グリッドの組み合わせでのみ座席ごとのΔを表示します'
        )
        self.baseline_combo.currentIndexChanged.connect(
            lambda _i: self._emit_change()
        )
        layout.addWidget(self.baseline_combo)

        self.status_label = QLabel('—')
        self.status_label.setObjectName('seatCoverageStatus')
        self.status_label.setWordWrap(True)
        set_typography_role(self.status_label, TypographyRole.SECONDARY)
        layout.addWidget(self.status_label)

        self.seat_table = QTableWidget()
        self.seat_table.setObjectName('seatCoverageTable')
        self.seat_table.setColumnCount(5)
        self.seat_table.setHorizontalHeaderLabels(
            ['座席', '値', '判定', 'Δ', '優先度']
        )
        self.seat_table.setMinimumHeight(90)
        self.seat_table.setMaximumHeight(150)
        self.seat_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.seat_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.seat_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.seat_table.setToolTip(
            '評価された座席の一覧 — 選択すると3D上で強調され、'
            '下の詳細に周波数ごとの結果と証跡を表示します'
        )
        self.seat_table.itemSelectionChanged.connect(
            self._on_table_selection
        )
        layout.addWidget(self.seat_table)

        self.detail_label = QLabel('—')
        self.detail_label.setObjectName('seatCoverageDetail')
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.detail_label)

        legend_host = QWidget()
        legend_layout = QHBoxLayout(legend_host)
        legend_layout.setContentsMargins(0, 0, 0, 0)
        legend_layout.setSpacing(8)
        self._legend_layout = legend_layout
        self._rebuild_legend(None)
        layout.addWidget(legend_host)

        self.disclaimer = QLabel(COVERAGE_DISCLAIMER_JA)
        self.disclaimer.setObjectName('seatCoverageDisclaimer')
        self.disclaimer.setWordWrap(True)
        set_typography_role(self.disclaimer, TypographyRole.SECONDARY)
        layout.addWidget(self.disclaimer)

    # ------------------------------------------------------------------
    # State surface
    # ------------------------------------------------------------------

    @property
    def coverage_enabled(self) -> bool:
        return self.coverage_toggle.isChecked()

    def set_enabled(self, enabled: bool) -> None:
        if self.coverage_toggle.isChecked() != enabled:
            self.coverage_toggle.setChecked(enabled)

    def request(self) -> CoverageOverlayRequest:
        """Current selector state as a display request."""
        evaluation_id = self.evaluation_combo.currentData()
        frequency = self.frequency_combo.currentData()
        quantity = self.quantity_combo.currentData() or 'relative_level'
        baseline = self.baseline_combo.currentData()
        return CoverageOverlayRequest(
            evaluation_id=evaluation_id,
            frequency_hz=(
                float(frequency) if frequency is not None else None
            ),
            quantity=quantity,
            baseline_evaluation_id=baseline,
        )

    def selected_seat_id(self) -> str | None:
        rows = self.seat_table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._ids_by_row.get(rows[0].row())

    # ------------------------------------------------------------------
    # Option + scene binding
    # ------------------------------------------------------------------

    def set_options(
        self,
        options: tuple[CoverageEvaluationOption, ...],
        notices: tuple[str, ...] = (),
    ) -> None:
        """Rebuild the evaluation/baseline combos — preserves selection."""
        previous_id = self.evaluation_combo.currentData()
        previous_baseline = self.baseline_combo.currentData()
        self._options = options
        self._syncing = True
        try:
            self.evaluation_combo.clear()
            for option in options:
                self.evaluation_combo.addItem(
                    option.label, option.evaluation_id
                )
            if not options:
                self.evaluation_combo.addItem('評価なし', None)
            self._restore_combo(self.evaluation_combo, previous_id)
            self._rebuild_frequency_combo()
            self._rebuild_baseline_combo(previous_baseline)
        finally:
            self._syncing = False

    def _restore_combo(self, combo: QComboBox, wanted) -> None:
        if wanted is not None:
            for index in range(combo.count()):
                if combo.itemData(index) == wanted:
                    combo.setCurrentIndex(index)
                    return
        combo.setCurrentIndex(0)

    def _rebuild_frequency_combo(self) -> None:
        previous = self.frequency_combo.currentData()
        self.frequency_combo.blockSignals(True)
        try:
            self.frequency_combo.clear()
            evaluation_id = self.evaluation_combo.currentData()
            option = next(
                (
                    item
                    for item in self._options
                    if item.evaluation_id == evaluation_id
                ),
                None,
            )
            self.frequency_combo.addItem('集約 (全要求周波数)', None)
            if option is not None:
                for hz in option.frequencies_hz:
                    self.frequency_combo.addItem(f'{hz:g} Hz', float(hz))
            self._restore_combo(self.frequency_combo, previous)
        finally:
            self.frequency_combo.blockSignals(False)

    def _rebuild_baseline_combo(self, previous) -> None:
        current_id = self.evaluation_combo.currentData()
        self.baseline_combo.blockSignals(True)
        try:
            self.baseline_combo.clear()
            self.baseline_combo.addItem('なし（A/B比較なし）', None)
            for option in self._options:
                if option.evaluation_id != current_id:
                    self.baseline_combo.addItem(
                        f'基準: {option.label}', option.evaluation_id
                    )
            self._restore_combo(self.baseline_combo, previous)
        finally:
            self.baseline_combo.blockSignals(False)

    def _on_evaluation_changed(self, _index: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._rebuild_frequency_combo()
            self._rebuild_baseline_combo(None)
        finally:
            self._syncing = False
        self._emit_change()

    def show_scene(self, scene: CoverageOverlayScene | None) -> None:
        """Bind the freshly resolved scene — every render pass."""
        self._scene = scene
        if scene is not None and scene.options:
            self.set_options(scene.options)
        previous_id = self.selected_seat_id()

        self._syncing = True
        try:
            self.seat_table.clearContents()
            self.seat_table.setRowCount(0)
            self._rows_by_id.clear()
            self._ids_by_row.clear()
            rows = scene.seat_rows if scene is not None else ()
            self.seat_table.setRowCount(len(rows))
            restore_row = -1
            for index, row in enumerate(rows):
                self._fill_row(index, row)
                self._rows_by_id[row.seat_entity_id] = index
                self._ids_by_row[index] = row.seat_entity_id
                if row.seat_entity_id == previous_id:
                    restore_row = index
            if restore_row >= 0:
                self.seat_table.selectRow(restore_row)
        finally:
            self._syncing = False

        self._sync_status()
        self._rebuild_legend(scene)
        if self._ids_by_row and not self.selected_seat_id():
            self.seat_table.selectRow(0)
        self._update_detail(self._selected_row())

    def _fill_row(self, row_index: int, row: CoverageSeatRow) -> None:
        cells = [
            row.seat_name,
            row.value_text or '—',
            row.gate_text or '—',
            row.delta_text or '—',
            f'{row.priority_text} {row.state_text}',
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, row.seat_entity_id)
            item.setToolTip(
                f'{row.seat_entity_id} · {row.state_text} · '
                f'{row.reason or "理由なし"}'
            )
            if column == 0 and not row.has_marker:
                item.setText(f'{text}（位置なし）')
            self.seat_table.setItem(row_index, column, item)

    def _selected_row(self) -> CoverageSeatRow | None:
        seat_id = self.selected_seat_id()
        if seat_id is None or self._scene is None:
            return None
        for row in self._scene.seat_rows:
            if row.seat_entity_id == seat_id:
                return row
        return None

    def _on_table_selection(self) -> None:
        if self._syncing:
            return
        self._update_detail(self._selected_row())
        self.seatSelected.emit(self.selected_seat_id())

    def select_seat(self, seat_entity_id: str | None) -> None:
        """Scene → table selection sync (stable seat id only)."""
        if seat_entity_id is None:
            return
        row = self._rows_by_id.get(seat_entity_id)
        if row is None:
            return
        if self.selected_seat_id() == seat_entity_id:
            return
        self._syncing = True
        try:
            self.seat_table.selectRow(row)
        finally:
            self._syncing = False
        self._update_detail(self._selected_row())

    # ------------------------------------------------------------------
    # Text surfaces
    # ------------------------------------------------------------------

    def _sync_status(self) -> None:
        scene = self._scene
        if scene is None:
            self.status_label.setText('—')
            return
        if scene.state == 'empty':
            self.status_label.setText(scene.summary_ja)
            return
        if scene.state == 'blocked':
            self.status_label.setText(f'表示ブロック: {scene.blocked_reason}')
            return
        lines = [scene.summary_ja]
        lines.extend(f'· {notice}' for notice in scene.notices)
        self.status_label.setText('\n'.join(lines))

    def _rebuild_legend(self, scene: CoverageOverlayScene | None) -> None:
        while self._legend_layout.count():
            item = self._legend_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        entries: list[tuple[str, str]] = []
        quantity = self.quantity_combo.currentData() or 'relative_level'
        if scene is None or quantity != 'coverage_gate':
            entries = [
                (label, color)
                for label, color in zip(
                    COVERAGE_BAND_ASCII, COVERAGE_BAND_COLORS, strict=True
                )
            ]
        else:
            entries = [
                ('PASS', COVERAGE_PASS_COLOR),
                ('FAIL', COVERAGE_FAIL_COLOR),
                ('未決定', COVERAGE_UNDECIDED_COLOR),
            ]
        entries.append(('不明/非対応', COVERAGE_UNKNOWN_COLOR))
        for text, color in entries:
            chip = QLabel(f'■{text}')
            chip.setStyleSheet(f'color: {color};')
            self._legend_layout.addWidget(chip)
        self._legend_layout.addStretch(1)

    def _update_detail(self, row: CoverageSeatRow | None) -> None:
        if row is None:
            scene = self._scene
            if scene is not None and scene.provenance is not None:
                prov = scene.provenance
                self.detail_label.setText(
                    '証跡: eval {eval_id} / scene {rev} / variant {var} / '
                    'dataset {ds} v{dsv}'.format(
                        eval_id=prov.evaluation_id.rsplit('-', 1)[-1][:10],
                        rev=prov.scene_revision_id[:8],
                        var=prov.variant_sha256[:8],
                        ds=prov.directivity_dataset_id,
                        dsv=prov.directivity_dataset_version,
                    )
                )
            else:
                self.detail_label.setText('—')
            return

        scene = self._scene
        lines: list[str] = [
            f'{row.seat_name} ({row.seat_entity_id}) · {row.state_text}',
        ]
        if row.position is not None:
            pos = row.position
            lines.append(
                f'耳位置: ({pos.x_m:.3f}, {pos.y_m:.3f}, {pos.z_m:.3f}) m'
            )
        if row.reason:
            lines.append(f'理由: {row.reason}')
        if row.remediation:
            lines.append(f'対応: {row.remediation}')
        if row.frequencies:
            lines.append('周波数ごとの結果（要求グリッド）:')
            for freq in row.frequencies:
                if freq.support_state == 'SUPPORTED':
                    angle_bits = []
                    if freq.horizontal_angle_deg is not None:
                        angle_bits.append(
                            f'水平 {freq.horizontal_angle_deg:.1f}°'
                        )
                    if freq.vertical_angle_deg is not None:
                        angle_bits.append(
                            f'垂直 {freq.vertical_angle_deg:.1f}°'
                        )
                    lines.append(
                        f'· {freq.frequency_hz:g} Hz: '
                        f'相対 {freq.relative_level_db:+.1f} dB / '
                        f'損失 {freq.off_axis_loss_db:.1f} dB'
                        + (
                            f'（{" · ".join(angle_bits)}）'
                            if angle_bits
                            else ''
                        )
                    )
                else:
                    lines.append(
                        f'· {freq.frequency_hz:g} Hz: 非対応 — {freq.reason}'
                    )
        if scene is not None and scene.provenance is not None:
            prov = scene.provenance
            lines.append(
                '証跡: eval {e} · シナリオ {s} · {alg} v{av}'.format(
                    e=prov.evaluation_id.rsplit('-', 1)[-1][:10],
                    s=prov.scenario_id.rsplit('-', 1)[-1][:10],
                    alg=prov.algorithm_id,
                    av=prov.algorithm_version,
                )
            )
            lines.append(
                'scene {rev}/{hash8} · variant {var} · {eq} v{eqv} · '
                '{ds} v{dsv}'.format(
                    rev=prov.scene_revision_id[:8],
                    hash8=prov.scene_content_hash[:8],
                    var=prov.variant_sha256[:8],
                    eq=prov.equipment_definition_id,
                    eqv=prov.equipment_definition_version,
                    ds=prov.directivity_dataset_id,
                    dsv=prov.directivity_dataset_version,
                )
            )
            lines.append(
                f'判定基準: {prov.coverage_criterion} '
                f'(閾値 {prov.coverage_threshold_db:g} dB, '
                f'集約 {prov.frequency_aggregation_semantics})'
            )
        self.detail_label.setText('\n'.join(lines))

    def _emit_change(self, *_args) -> None:
        if not self._syncing:
            self.changed.emit()
