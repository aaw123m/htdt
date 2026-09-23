"""Video-geometry panel for the workflow-first Room (#455).

Connects the canonical ``cad_video_geometry`` authority to Room editing:
projector/screen/seat bindings, an exact ``ProjectorSpecification`` selector
plus a manual-spec registration dialog (typed user_defined evidence), policy
fields, evaluation on the baseline revision AND SystemVariants, spatial
overlay, and a "view from seat" camera tool bound to the same seat-eye
authority the sightline evaluator consumes — it never invents eye positions.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_video_workspace import VideoGeometryWorkspace
from .ui_theme import TypographyRole, set_typography_role

_ENTITY_ROLE = Qt.ItemDataRole.UserRole
_SPEC_ROLE = Qt.ItemDataRole.UserRole
_VARIANT_ROLE = Qt.ItemDataRole.UserRole


class ProjectorSpecDialog(QDialog):
    """Manual projector-spec registration — user_defined evidence only.

    Only values the user actually enters are attested; missing lens-shift
    limits stay UNKNOWN (the evaluator reports UNKNOWN, never guesses).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("プロジェクター仕様を登録（ユーザー定義）")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.spec_id = QLineEdit()
        self.spec_id.setPlaceholderText("例: my-projector-model")
        self.version = QLineEdit("1")
        self.publisher = QLineEdit()
        self.publisher.setPlaceholderText("測定者/出典")
        self.doc_title = QLineEdit()
        self.doc_title.setPlaceholderText("出典タイトル（測定メモ等）")
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("参照位置（ページ/セクション）")
        self.citation = QLineEdit()
        self.citation.setPlaceholderText("出典引用（例: 2026-09-23 現場実測）")
        self.actor = QLineEdit()
        self.actor.setPlaceholderText("記録者")
        self.throw_min = QDoubleSpinBox()
        self.throw_min.setRange(0.1, 20.0)
        self.throw_min.setValue(1.0)
        self.throw_max = QDoubleSpinBox()
        self.throw_max.setRange(0.1, 20.0)
        self.throw_max.setValue(2.0)
        self.shift_h_enabled = QComboBox()
        self.shift_h_enabled.addItems(("不明", "範囲あり"))
        self.shift_h_min = QDoubleSpinBox()
        self.shift_h_min.setRange(-1.0, 1.0)
        self.shift_h_min.setSingleStep(0.05)
        self.shift_h_max = QDoubleSpinBox()
        self.shift_h_max.setRange(-1.0, 1.0)
        self.shift_h_max.setSingleStep(0.05)
        self.shift_h_max.setValue(0.5)
        self.shift_v_enabled = QComboBox()
        self.shift_v_enabled.addItems(("不明", "範囲あり"))
        self.shift_v_min = QDoubleSpinBox()
        self.shift_v_min.setRange(-1.0, 1.0)
        self.shift_v_min.setSingleStep(0.05)
        self.shift_v_max = QDoubleSpinBox()
        self.shift_v_max.setRange(-1.0, 1.0)
        self.shift_v_max.setSingleStep(0.05)
        self.shift_v_max.setValue(0.5)
        form.addRow("仕様ID", self.spec_id)
        form.addRow("バージョン", self.version)
        form.addRow("記録者", self.actor)
        form.addRow("出典者", self.publisher)
        form.addRow("出典タイトル", self.doc_title)
        form.addRow("参照", self.reference)
        form.addRow("出典引用", self.citation)
        form.addRow("スロー比 最小", self.throw_min)
        form.addRow("スロー比 最大", self.throw_max)
        form.addRow("水平レンズシフト", self.shift_h_enabled)
        form.addRow("水平シフト 最小", self.shift_h_min)
        form.addRow("水平シフト 最大", self.shift_h_max)
        form.addRow("垂直レンズシフト", self.shift_v_enabled)
        form.addRow("垂直シフト 最小", self.shift_v_min)
        form.addRow("垂直シフト 最大", self.shift_v_max)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        def _range(enabled: QComboBox, lo: QDoubleSpinBox, hi: QDoubleSpinBox):
            if enabled.currentIndex() == 0:
                return None
            return (float(lo.value()), float(hi.value()))

        return {
            'specification_id': self.spec_id.text().strip(),
            'version': self.version.text().strip() or '1',
            'publisher': self.publisher.text().strip() or 'HTDT user',
            'document_title': self.doc_title.text().strip() or 'Manual entry',
            'reference': self.reference.text().strip() or 'manual entry',
            'source_citation': self.citation.text().strip() or 'user entry',
            'actor': self.actor.text().strip() or 'user',
            'throw_ratio_min': float(self.throw_min.value()),
            'throw_ratio_max': float(self.throw_max.value()),
            'horizontal_lens_shift': _range(self.shift_h_enabled, self.shift_h_min, self.shift_h_max),
            'vertical_lens_shift': _range(self.shift_v_enabled, self.shift_v_min, self.shift_v_max),
        }


class _SpinRow(QWidget):
    """Tiny (label, spinbox) row used per numeric binding field."""

    def __init__(self, label: str, *, minimum: float, maximum: float, step: float, value: float, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        text = QLabel(label)
        set_typography_role(text, TypographyRole.SECONDARY)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(3)
        self.spin.setSuffix(' m')
        self.spin.setValue(value)
        row.addWidget(text, stretch=1)
        row.addWidget(self.spin)


class RoomVideoPanel(QWidget):
    """Projector/screen/sightline authoring + evaluation card."""

    bindingsChanged = Signal()
    evaluateRequested = Signal(object)  # variant_id or None
    viewFromSeatRequested = Signal(object)  # seat entity id or None
    createSpecRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("映像ジオメトリ（プロジェクター/スクリーン/視線）")
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        # --- projector binding -------------------------------------------------
        proj_row = QHBoxLayout()
        proj_row.setSpacing(4)
        proj_row.addWidget(QLabel("プロジェクター"))
        self.projector_combo = QComboBox()
        proj_row.addWidget(self.projector_combo, stretch=1)
        layout.addLayout(proj_row)

        spec_row = QHBoxLayout()
        spec_row.setSpacing(4)
        spec_row.addWidget(QLabel("仕様"))
        self.spec_combo = QComboBox()
        spec_row.addWidget(self.spec_combo, stretch=1)
        self.new_spec_button = QPushButton("登録…")
        spec_row.addWidget(self.new_spec_button)
        layout.addLayout(spec_row)

        # --- screen binding ----------------------------------------------------
        self.screen_heading = QLabel("スクリーン画素（バインド未設定）")
        set_typography_role(self.screen_heading, TypographyRole.SECONDARY)
        layout.addWidget(self.screen_heading)
        screen_form = QFormLayout()
        screen_form.setContentsMargins(0, 0, 0, 0)
        self.screen_width = QDoubleSpinBox()
        self.screen_width.setRange(0.5, 20.0)
        self.screen_width.setSingleStep(0.05)
        self.screen_width.setSuffix(' m')
        self.screen_height = QDoubleSpinBox()
        self.screen_height.setRange(0.3, 10.0)
        self.screen_height.setSingleStep(0.05)
        self.screen_height.setSuffix(' m')
        self.screen_offset_x = QDoubleSpinBox()
        self.screen_offset_x.setRange(-5.0, 5.0)
        self.screen_offset_x.setSingleStep(0.05)
        self.screen_offset_x.setSuffix(' m')
        self.screen_offset_z = QDoubleSpinBox()
        self.screen_offset_z.setRange(-5.0, 5.0)
        self.screen_offset_z.setSingleStep(0.05)
        self.screen_offset_z.setSuffix(' m')
        self.frame_clearance = QDoubleSpinBox()
        self.frame_clearance.setRange(0.0, 2.0)
        self.frame_clearance.setSingleStep(0.01)
        self.frame_clearance.setSuffix(' m')
        self.acoustic_combo = QComboBox()
        self.acoustic_combo.addItem("不明", None)
        self.acoustic_combo.addItem("透過あり", True)
        self.acoustic_combo.addItem("透過なし", False)
        screen_form.addRow("表示幅", self.screen_width)
        screen_form.addRow("表示高さ", self.screen_height)
        screen_form.addRow("中心オフセット X", self.screen_offset_x)
        screen_form.addRow("中心オフセット Z", self.screen_offset_z)
        screen_form.addRow("フレーム余白", self.frame_clearance)
        screen_form.addRow("音響透過", self.acoustic_combo)
        layout.addLayout(screen_form)

        # --- seat bindings -----------------------------------------------------
        self.seats_heading = QLabel("座席バインド")
        set_typography_role(self.seats_heading, TypographyRole.SECONDARY)
        layout.addWidget(self.seats_heading)
        self.seats_box = QVBoxLayout()
        self.seats_box.setContentsMargins(0, 0, 0, 0)
        self.seats_box.setSpacing(2)
        layout.addLayout(self.seats_box)
        self._seat_widgets: dict[str, dict[str, QWidget]] = {}

        # --- policy ------------------------------------------------------------
        policy_form = QFormLayout()
        policy_form.setContentsMargins(0, 0, 0, 0)
        self.sightline_clearance = QDoubleSpinBox()
        self.sightline_clearance.setRange(0.0, 1.0)
        self.sightline_clearance.setSingleStep(0.01)
        self.sightline_clearance.setSuffix(' m')
        self.max_axis_deviation = QDoubleSpinBox()
        self.max_axis_deviation.setRange(0.0, 90.0)
        self.max_axis_deviation.setSingleStep(1.0)
        self.max_axis_deviation.setSuffix('°')
        self.max_axis_deviation.setValue(30.0)
        policy_form.addRow("視線クリアランス", self.sightline_clearance)
        policy_form.addRow("光軸ズレ上限", self.max_axis_deviation)
        layout.addLayout(policy_form)

        # --- evaluation ----------------------------------------------------------
        eval_row = QHBoxLayout()
        eval_row.setSpacing(4)
        self.evaluate_button = QPushButton("評価")
        self.variant_combo = QComboBox()
        self.variant_combo.addItem("ベースライン（現在の保存版）", None)
        eval_row.addWidget(self.evaluate_button)
        eval_row.addWidget(self.variant_combo, stretch=1)
        layout.addLayout(eval_row)

        self.status_label = QLabel("プロジェクター・スクリーン・座席をバインドして評価できます")
        self.status_label.setWordWrap(True)
        set_typography_role(self.status_label, TypographyRole.SECONDARY)
        layout.addWidget(self.status_label)

        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(("項目", "状態"))
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)
        layout.addWidget(self.results_tree, stretch=1)

        seat_view_row = QHBoxLayout()
        seat_view_row.setSpacing(4)
        seat_view_row.addWidget(QLabel("座席視点:"))
        self.seat_view_combo = QComboBox()
        self.view_seat_button = QPushButton("座席から見る")
        self.restore_camera_button = QPushButton("カメラを戻す")
        seat_view_row.addWidget(self.seat_view_combo, stretch=1)
        seat_view_row.addWidget(self.view_seat_button)
        seat_view_row.addWidget(self.restore_camera_button)
        layout.addLayout(seat_view_row)

        self.new_spec_button.clicked.connect(self.createSpecRequested)
        self.evaluate_button.clicked.connect(
            lambda: self.evaluateRequested.emit(self.variant_combo.currentData())
        )
        self.view_seat_button.clicked.connect(
            lambda: self.viewFromSeatRequested.emit(self.seat_view_combo.currentData())
        )
        self.restore_camera_button.clicked.connect(
            lambda: self.viewFromSeatRequested.emit(None)
        )
        for spin in (
            self.screen_width,
            self.screen_height,
            self.screen_offset_x,
            self.screen_offset_z,
            self.frame_clearance,
            self.sightline_clearance,
            self.max_axis_deviation,
        ):
            spin.valueChanged.connect(lambda _v: self.bindingsChanged.emit())
        self.acoustic_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
        self.projector_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
        self.spec_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())

        self._syncing = False

    # -- data-in ----------------------------------------------------------------

    def sync_document(
        self,
        document,
        workspace: VideoGeometryWorkspace,
        specifications: tuple,
        variants: tuple,
        seat_names: dict[str, str],
    ) -> None:
        """Refresh all widgets from authoritative state."""

        self._syncing = True
        try:
            current_projector = self.projector_combo.currentData()
            self.projector_combo.blockSignals(True)
            self.projector_combo.clear()
            self.projector_combo.addItem("（未選択）", None)
            projectors = [entity for entity in document.entities if entity.kind == 'projector']
            for entity in projectors:
                self.projector_combo.addItem(entity.name, entity.entity_id)
            wanted = workspace.projector_entity_id or current_projector
            index = self.projector_combo.findData(wanted)
            self.projector_combo.setCurrentIndex(index if index >= 0 else 0)
            self.projector_combo.blockSignals(False)

            current_spec = self.spec_combo.currentData()
            self.spec_combo.blockSignals(True)
            self.spec_combo.clear()
            self.spec_combo.addItem("（未バインド）", None)
            for spec in specifications:
                label = f"{spec.specification_id} v{spec.version}"
                if spec.manufacturer or spec.model:
                    label += f" · {' '.join(v for v in (spec.manufacturer, spec.model) if v)}"
                self.spec_combo.addItem(label, spec.specification_sha256)
            wanted_spec = workspace.projector_specification_sha256 or current_spec
            index = self.spec_combo.findData(wanted_spec)
            self.spec_combo.setCurrentIndex(index if index >= 0 else 0)
            self.spec_combo.blockSignals(False)

            current_variant = self.variant_combo.currentData()
            self.variant_combo.blockSignals(True)
            self.variant_combo.clear()
            self.variant_combo.addItem("ベースライン（現在の保存版）", None)
            for variant in variants:
                self.variant_combo.addItem(
                    f"バリアント: {getattr(variant, 'name', None) or variant.variant_id[:12]}",
                    variant.variant_id,
                )
            index = self.variant_combo.findData(current_variant)
            self.variant_combo.setCurrentIndex(index if index >= 0 else 0)
            self.variant_combo.blockSignals(False)

            screens = [entity for entity in document.entities if entity.kind == 'screen']
            seats = [entity for entity in document.entities if entity.kind == 'seat']
            if screens:
                screen = screens[0]
                binding = workspace.screen_bindings.get(screen.entity_id)
                self.screen_heading.setText(f"スクリーン画素 — {screen.name}")
                if binding is not None:
                    self.screen_width.setValue(binding.visible_width_m)
                    self.screen_height.setValue(binding.visible_height_m)
                    self.screen_offset_x.setValue(binding.image_center_offset_local_m.x_m)
                    self.screen_offset_z.setValue(binding.image_center_offset_local_m.z_m)
                    self.frame_clearance.setValue(binding.frame_clearance_m)
                    index = self.acoustic_combo.findData(binding.acoustically_transparent)
                    self.acoustic_combo.setCurrentIndex(index if index >= 0 else 0)

            # Rebuild per-seat rows only when seat set changed.
            seat_ids = [entity.entity_id for entity in seats]
            if list(self._seat_widgets) != seat_ids:
                while self.seats_box.count():
                    item = self.seats_box.takeAt(0)
                    widget = item.widget()
                    if widget is not None:
                        widget.deleteLater()
                self._seat_widgets = {}
                self.seat_view_combo.clear()
                risers = [entity for entity in document.entities if entity.kind == 'riser']
                for seat in seats:
                    card = QWidget()
                    form = QFormLayout(card)
                    form.setContentsMargins(0, 0, 0, 0)
                    eye_z = _SpinRow("眼高さオフセット Z", minimum=0.0, maximum=3.0, step=0.05, value=1.10)
                    head_z = _SpinRow("頭部オフセット Z", minimum=0.0, maximum=3.0, step=0.05, value=1.15)
                    head_r = _SpinRow("頭半径", minimum=0.02, maximum=0.5, step=0.01, value=0.10)
                    row_id = QLineEdit('row-1')
                    riser_combo = QComboBox()
                    riser_combo.addItem("（ライザーなし）", None)
                    for riser in risers:
                        riser_combo.addItem(riser.name, riser.entity_id)
                    form.addRow(f"{seat.name}", QLabel(""))
                    form.addRow("行", row_id)
                    form.addRow(eye_z)
                    form.addRow(head_z)
                    form.addRow(head_r)
                    form.addRow("ライザー", riser_combo)
                    self.seats_box.addWidget(card)
                    self._seat_widgets[seat.entity_id] = {
                        'row_id': row_id,
                        'eye_z': eye_z,
                        'head_z': head_z,
                        'head_r': head_r,
                        'riser': riser_combo,
                    }
                    self.seat_view_combo.addItem(seat.name, seat.entity_id)
                    row_id.textChanged.connect(lambda _t: self.bindingsChanged.emit())
                    riser_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
                    eye_z.spin.valueChanged.connect(lambda _v: self.bindingsChanged.emit())
                    head_z.spin.valueChanged.connect(lambda _v: self.bindingsChanged.emit())
                    head_r.spin.valueChanged.connect(lambda _v: self.bindingsChanged.emit())
            for seat in seats:
                binding = workspace.seat_bindings.get(seat.entity_id)
                if binding is None:
                    continue
                widgets = self._seat_widgets[seat.entity_id]
                widgets['row_id'].setText(binding.row_id)
                widgets['eye_z'].spin.setValue(binding.eye_reference_offset_local_m.z_m)
                widgets['head_z'].spin.setValue(binding.head_center_offset_local_m.z_m)
                widgets['head_r'].spin.setValue(binding.head_radius_m)
                index = widgets['riser'].findData(binding.riser_entity_id)
                widgets['riser'].setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._syncing = False

    # -- data-out -----------------------------------------------------------------

    def current_projector_entity_id(self) -> str | None:
        return self.projector_combo.currentData()

    def current_specification_sha256(self) -> str | None:
        return self.spec_combo.currentData()

    def current_screen_values(self) -> dict[str, object]:
        return {
            'visible_width_m': float(self.screen_width.value()),
            'visible_height_m': float(self.screen_height.value()),
            'image_center_offset_x_m': float(self.screen_offset_x.value()),
            'image_center_offset_z_m': float(self.screen_offset_z.value()),
            'frame_clearance_m': float(self.frame_clearance.value()),
            'acoustically_transparent': self.acoustic_combo.currentData(),
        }

    def current_seat_bindings(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for entity_id, widgets in self._seat_widgets.items():
            result[entity_id] = {
                'row_id': widgets['row_id'].text().strip() or 'row-1',
                'eye_z_m': float(widgets['eye_z'].spin.value()),
                'head_z_m': float(widgets['head_z'].spin.value()),
                'head_radius_m': float(widgets['head_r'].spin.value()),
                'riser_entity_id': widgets['riser'].currentData(),
            }
        return result

    def current_policy_values(self) -> dict[str, float]:
        return {
            'sightline_clearance_m': float(self.sightline_clearance.value()),
            'max_optical_axis_deviation_deg': float(self.max_axis_deviation.value()),
        }

    # -- results -------------------------------------------------------------------

    def show_evaluation(self, evaluation) -> None:
        """Render category statuses of one VideoGeometryEvaluation."""

        status_labels = {
            'PASS': 'PASS',
            'FAIL': 'FAIL',
            'UNKNOWN': 'UNKNOWN',
            'NOT_APPLICABLE': '—',
        }
        self.results_tree.clear()
        if evaluation is None:
            return
        rows: list[tuple[str, str]] = []
        projection = evaluation.projection
        if projection is not None:
            rows.extend(
                [
                    ('投影: 総合', status_labels.get(projection.status, projection.status)),
                    ('投影: スロー比', status_labels.get(projection.throw_ratio_status, '?')),
                    ('投影: 水平レンズシフト', status_labels.get(projection.horizontal_lens_shift_status, '?')),
                    ('投影: 垂直レンズシフト', status_labels.get(projection.vertical_lens_shift_status, '?')),
                    ('投影: 光軸', status_labels.get(projection.optical_axis_status, '?')),
                    ('投影: スクリーン開口', status_labels.get(projection.screen_aperture_status, '?')),
                ]
            )
        for seat in evaluation.viewing:
            name = seat.seat_entity_id
            rows.append(
                (
                    f'視野角: {name}',
                    f"H {status_labels.get(seat.horizontal_status, '?')} / "
                    f"V {status_labels.get(seat.vertical_status, '?')} / "
                    f"仰角 {status_labels.get(seat.center_elevation_status, '?')}",
                )
            )
        for seat in evaluation.sightlines:
            rows.append(
                (
                    f'視線: {seat.seat_entity_id}',
                    status_labels.get(seat.status, seat.status)
                    + (f' · 遮蔽 {len(seat.blocking_seat_ids)}' if seat.blocking_seat_ids else ''),
                )
            )
        for riser in evaluation.risers:
            rows.append(
                (
                    f'ライザー: {riser.seat_entity_id}',
                    status_labels.get(riser.status, riser.status),
                )
            )
        collisions_fail = sum(1 for item in evaluation.collisions if item.intersects_or_violates_clearance)
        rows.append(
            (
                '衝突',
                'PASS' if collisions_fail == 0 else f'FAIL · {collisions_fail} 件',
            )
        )
        for name, status in rows:
            item = QTreeWidgetItem([name, status])
            if status.startswith('FAIL'):
                item.setForeground(1, Qt.GlobalColor.red)
            self.results_tree.addTopLevelItem(item)

    def show_message(self, text: str) -> None:
        self.status_label.setText(text)
