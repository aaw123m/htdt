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
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_screen_transfer import TIER_LABELS
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


class ScreenTransferDialog(QDialog):
    """Register one AcousticScreenTransferAuthority for a screen (#541).

    The declared tier is explicit evidence: UNKNOWN is the honest default,
    AT_CLAIM marks an unmeasured acoustically-transparent claim, and sampled
    tiers require typed rows ``freq_hz[,angle_deg[,magnitude[,phase_deg[,reflection]]]]``
    — coefficients are never synthesized.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("スクリーン伝達権威を登録")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label = QLineEdit()
        self.label.setPlaceholderText("例: メインスクリーン AT-2000")
        form.addRow("名称", self.label)
        self.tier = QComboBox()
        for value, label_text in TIER_LABELS.items():
            self.tier.addItem(f"{value} — {label_text}", value)
        form.addRow("capability tier", self.tier)
        self.freq_min = QDoubleSpinBox()
        self.freq_min.setRange(1.0, 20000.0)
        self.freq_min.setValue(20.0)
        self.freq_min.setSuffix(' Hz')
        self.freq_max = QDoubleSpinBox()
        self.freq_max.setRange(1.0, 24000.0)
        self.freq_max.setValue(8000.0)
        self.freq_max.setSuffix(' Hz')
        form.addRow("有効周波数 最小", self.freq_min)
        form.addRow("有効周波数 最大", self.freq_max)
        self.condition = QLineEdit()
        self.condition.setPlaceholderText("測定条件（例: 法線入射, free-field）")
        form.addRow("測定条件", self.condition)
        self.provenance = QLineEdit()
        self.provenance.setPlaceholderText("出典（例: メーカー測定 / 現場実測）")
        form.addRow("出典", self.provenance)
        self.samples = QPlainTextEdit()
        self.samples.setPlaceholderText(
            "周波数依存特性 — 1行1点: freq_hz[,angle_deg[,magnitude[,phase_deg[,reflection]]]]"
        )
        self.samples.setMaximumHeight(90)
        form.addRow("サンプル", self.samples)
        self.notes = QLineEdit()
        form.addRow("備考", self.notes)
        layout.addLayout(form)
        hint = QLabel(
            "tierに見合わない係数は保存されません — 測定されていない透過特性は"
            "UNKNOWN/AT_CLAIMとして正直に記録されます。"
        )
        hint.setWordWrap(True)
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if not self.label.text().strip() or not self.provenance.text().strip():
            return
        super().accept()

    def values(self) -> dict[str, object]:
        return {
            'label': self.label.text().strip(),
            'capability_tier': self.tier.currentData(),
            'frequency_minimum_hz': float(self.freq_min.value()),
            'frequency_maximum_hz': float(self.freq_max.value()),
            'measurement_condition': self.condition.text().strip(),
            'provenance': self.provenance.text().strip(),
            'samples_text': self.samples.toPlainText().strip(),
            'notes': self.notes.text().strip(),
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
    poseChanged = Signal(str, object)  # (seat entity id, pose_id or None)
    poseSaveRequested = Signal(str)  # seat entity id
    transferChanged = Signal(str, object)  # (screen entity id, transfer_id or None)
    transferSaveRequested = Signal(str)  # screen entity id

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
        self.transfer_combo = QComboBox()
        self.transfer_combo.addItem("不明（transfer権威なし）", None)
        self.transfer_combo.setToolTip(
            "スクリーンの音響透過/反射権威 (#541) — ATフラグではなく versioned authority"
        )
        self.transfer_save_button = QPushButton("登録…")
        self.transfer_save_button.setToolTip(
            "このスクリーンの伝達特性権威を新規登録します"
        )
        transfer_row = QHBoxLayout()
        transfer_row.addWidget(self.transfer_combo, stretch=1)
        transfer_row.addWidget(self.transfer_save_button)
        screen_form.addRow("表示幅", self.screen_width)
        screen_form.addRow("表示高さ", self.screen_height)
        screen_form.addRow("中心オフセット X", self.screen_offset_x)
        screen_form.addRow("中心オフセット Z", self.screen_offset_z)
        screen_form.addRow("フレーム余白", self.frame_clearance)
        screen_form.addRow("音響伝達", transfer_row)
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
        # #1056: seats whose eye/head numbers were explicitly materialized
        # (stored non-legacy binding, bound pose, or user-edited spins).
        # Untouched widget defaults never become seat bindings.
        self._configured_seats: set[str] = set()

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
        self.transfer_combo.currentIndexChanged.connect(
            lambda _i: self._transfer_selected(self.transfer_combo.currentData())
        )
        self.transfer_save_button.clicked.connect(
            lambda _c: self.transferSaveRequested.emit(self._screen_entity_id)
        )
        self.projector_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
        self.spec_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())

        self._syncing = False
        self._screen_entity_id: str | None = None

    # -- data-in ----------------------------------------------------------------

    def sync_document(
        self,
        document,
        workspace: VideoGeometryWorkspace,
        specifications: tuple,
        variants: tuple,
        seat_names: dict[str, str],
        seat_poses: dict[str, tuple[tuple[tuple[str, str], ...], str | None]] | None = None,
        screen_transfers: tuple[tuple[tuple[str, str], ...], str | None] | None = None,
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
                self._screen_entity_id = screen.entity_id

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
                    pose_combo = QComboBox()
                    pose_combo.addItem("カスタム（手動値）", None)
                    pose_combo.setToolTip(
                        "座席のリスナーポーズ権威 (#632) — 選択時は眼/頭オフセットがポーズから導出されます"
                    )
                    pose_save = QPushButton("ポーズ保存…")
                    pose_save.setToolTip(
                        "現在の眼/頭オフセットをこの座席のリスナーポーズ権威として保存します"
                    )
                    pose_row = QHBoxLayout()
                    pose_row.addWidget(pose_combo, stretch=1)
                    pose_row.addWidget(pose_save)
                    form.addRow("ポーズ", pose_row)
                    self.seats_box.addWidget(card)
                    self._seat_widgets[seat.entity_id] = {
                        'row_id': row_id,
                        'eye_z': eye_z,
                        'head_z': head_z,
                        'head_r': head_r,
                        'riser': riser_combo,
                        'pose': pose_combo,
                    }
                    self.seat_view_combo.addItem(seat.name, seat.entity_id)
                    row_id.textChanged.connect(lambda _t: self.bindingsChanged.emit())
                    riser_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
                    eye_z.spin.valueChanged.connect(
                        lambda _v, sid=seat.entity_id: self._seat_customized(sid)
                    )
                    head_z.spin.valueChanged.connect(
                        lambda _v, sid=seat.entity_id: self._seat_customized(sid)
                    )
                    head_r.spin.valueChanged.connect(
                        lambda _v, sid=seat.entity_id: self._seat_customized(sid)
                    )
                    pose_combo.currentIndexChanged.connect(
                        lambda _i, sid=seat.entity_id, c=pose_combo: self._pose_selected(sid, c.currentData())
                    )
                    pose_save.clicked.connect(
                        lambda _checked, sid=seat.entity_id: self.poseSaveRequested.emit(sid)
                    )
            for seat in seats:
                binding = workspace.seat_bindings.get(seat.entity_id)
                if binding is not None:
                    widgets = self._seat_widgets[seat.entity_id]
                    widgets['row_id'].setText(binding.row_id)
                    widgets['eye_z'].spin.setValue(binding.eye_reference_offset_local_m.z_m)
                    widgets['head_z'].spin.setValue(binding.head_center_offset_local_m.z_m)
                    widgets['head_r'].spin.setValue(binding.head_radius_m)
                    index = widgets['riser'].findData(binding.riser_entity_id)
                    widgets['riser'].setCurrentIndex(index if index >= 0 else 0)
                pose_info = (seat_poses or {}).get(seat.entity_id)
                if pose_info is not None:
                    items, selected_id = pose_info
                    self.set_seat_pose_options(seat.entity_id, items, selected_id)
            # #1056: only seats with real authority feed bindings — a seat
            # the user never configured must stay unbound so readiness can
            # report the missing eye/head geometry.
            self._configured_seats = {
                seat_id
                for seat_id, binding in workspace.seat_bindings.items()
                if binding.geometry_source != 'legacy'
            } | {
                seat_id
                for seat_id, pose_info in (seat_poses or {}).items()
                if pose_info is not None and pose_info[1] is not None
            }
            self._apply_screen_transfer_options(screen_transfers)
        finally:
            self._syncing = False

    def _apply_screen_transfer_options(
        self,
        screen_transfers: tuple[tuple[tuple[str, str], ...], str | None] | None,
    ) -> None:
        if screen_transfers is None:
            return
        items, selected_id = screen_transfers
        self.transfer_combo.blockSignals(True)
        self.transfer_combo.clear()
        self.transfer_combo.addItem("不明（transfer権威なし）", None)
        for label, transfer_id in items:
            self.transfer_combo.addItem(label, transfer_id)
        index = self.transfer_combo.findData(selected_id)
        self.transfer_combo.setCurrentIndex(index if index >= 0 else 0)
        self.transfer_combo.blockSignals(False)

    def _transfer_selected(self, transfer_id: object) -> None:
        if not self._syncing and self._screen_entity_id is not None:
            self.transferChanged.emit(self._screen_entity_id, transfer_id)

    def set_seat_pose_options(
        self,
        seat_id: str,
        items: tuple[tuple[str, str], ...],
        selected_id: str | None,
    ) -> None:
        """Populate the pose combo of one seat card (#632)."""

        widgets = self._seat_widgets.get(seat_id)
        if widgets is None:
            return
        combo = widgets['pose']
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("カスタム（手動値）", None)
        for label, pose_id in items:
            combo.addItem(label, pose_id)
        index = combo.findData(selected_id)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _pose_selected(self, seat_id: str, pose_id: object) -> None:
        if not self._syncing:
            self.poseChanged.emit(seat_id, pose_id)

    def _seat_customized(self, seat_id: str) -> None:
        # Editing the offsets manually means the seat no longer follows a
        # bound pose — the combo resets to カスタム and the selection clears.
        widgets = self._seat_widgets.get(seat_id)
        if widgets is not None and not self._syncing:
            self._configured_seats.add(seat_id)
            combo = widgets['pose']
            if combo.currentIndex() != 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
                self.poseChanged.emit(seat_id, None)
        self.bindingsChanged.emit()

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
        }

    def current_seat_bindings(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for entity_id, widgets in self._seat_widgets.items():
            if entity_id not in self._configured_seats:
                # #1056: unconfigured seat -> no binding -> evaluator never
                # consumes invented eye/head geometry.
                continue
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
