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
    QMessageBox,
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

_DISPLAY_CLASS_ITEMS: tuple[tuple[str, str], ...] = (
    ('lcd', 'LCD'),
    ('oled', 'OLED'),
    ('miniled', 'Mini LED'),
    ('microled', 'Micro LED'),
    ('other', 'その他'),
    ('unknown', '不明'),
)

_DISPLAY_MOUNTING_ITEMS: tuple[tuple[str, str], ...] = (
    ('unknown', '不明'),
    ('wall', '壁掛け'),
    ('stand', 'スタンド'),
    ('furniture', '家具設置'),
    ('recessed', '埋め込み'),
    ('custom', 'カスタム'),
)

_ENTITY_ROLE = Qt.ItemDataRole.UserRole
_SPEC_ROLE = Qt.ItemDataRole.UserRole
_VARIANT_ROLE = Qt.ItemDataRole.UserRole

# Tiers whose sealed authority requires typed transfer samples; mirrors the
# AcousticScreenTransferAuthority validator — the dialog blocks honestly
# before the form is lost instead of failing post-close.
_SAMPLED_TIERS = frozenset(
    {
        'MAGNITUDE_NORMAL_INCIDENCE',
        'FREQUENCY_AND_ANGLE',
        'COMPLEX',
        'TRANSMISSION_AND_REFLECTION',
    }
)


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

    def accept(self) -> None:
        if not self.spec_id.text().strip():
            QMessageBox.warning(
                self, "プロジェクター仕様", "仕様IDを入力してください"
            )
            self.spec_id.setFocus()
            return
        if self.throw_min.value() > self.throw_max.value():
            QMessageBox.warning(
                self,
                "プロジェクター仕様",
                "スロー比の最小値が最大値を超えています",
            )
            return
        for enabled, lo, hi, name in (
            (self.shift_h_enabled, self.shift_h_min, self.shift_h_max, "水平レンズシフト"),
            (self.shift_v_enabled, self.shift_v_min, self.shift_v_max, "垂直レンズシフト"),
        ):
            if enabled.currentIndex() != 0 and lo.value() > hi.value():
                QMessageBox.warning(
                    self,
                    "プロジェクター仕様",
                    f"{name}の最小値が最大値を超えています",
                )
                return
        super().accept()

    def values(self) -> dict[str, object]:
        def _range(enabled: QComboBox, lo: QDoubleSpinBox, hi: QDoubleSpinBox):
            if enabled.currentIndex() == 0:
                return None
            return (float(lo.value()), float(hi.value()))

        return {
            'specification_id': self.spec_id.text().strip(),
            'version': self.version.text().strip() or '1',
            'publisher': self.publisher.text().strip() or 'HTDTユーザー',
            'document_title': self.doc_title.text().strip() or '手動入力',
            'reference': self.reference.text().strip() or '手動入力',
            'source_citation': self.citation.text().strip() or 'ユーザー入力',
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
        form.addRow("性能ティア", self.tier)
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
        if not self.label.text().strip():
            QMessageBox.warning(
                self, "スクリーン伝達権威", "名称を入力してください"
            )
            self.label.setFocus()
            return
        if not self.provenance.text().strip():
            QMessageBox.warning(
                self, "スクリーン伝達権威", "出典を入力してください"
            )
            self.provenance.setFocus()
            return
        tier = self.tier.currentData()
        samples_text = self.samples.toPlainText().strip()
        if tier == 'MEASURED_DATASET':
            QMessageBox.warning(
                self,
                "スクリーン伝達権威",
                "MEASURED_DATASET には実測データセット権威参照が必要なため、"
                "このダイアログでは登録できません。下位のティアを選択してください",
            )
            return
        if tier in _SAMPLED_TIERS and not samples_text:
            QMessageBox.warning(
                self,
                "スクリーン伝達権威",
                "このティアにはサンプル行が必要です "
                "— 未測定なら UNKNOWN / AT_CLAIM を選択してください",
            )
            self.samples.setFocus()
            return
        if tier in ('UNKNOWN', 'AT_CLAIM') and samples_text:
            QMessageBox.warning(
                self,
                "スクリーン伝達権威",
                "UNKNOWN / AT_CLAIM ティアにサンプルは保存できません "
                "— 測定データに見合うティアを選択してください",
            )
            return
        if self.freq_min.value() > self.freq_max.value():
            QMessageBox.warning(
                self,
                "スクリーン伝達権威",
                "有効周波数の最小値が最大値を超えています",
            )
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


class DisplaySpecDialog(QDialog):
    """Manual direct-view display-spec registration — user_defined evidence.

    Same honesty contract as :class:`ProjectorSpecDialog`: only typed values
    are attested; video/photometric capabilities stay at their unmeasured
    defaults so spec-conformance never invents luminance/refresh claims.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ディスプレイ仕様を登録（ユーザー定義）")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.spec_id = QLineEdit()
        self.spec_id.setPlaceholderText("例: my-display-model")
        self.version = QLineEdit("1")
        self.user_label = QLineEdit()
        self.user_label.setPlaceholderText("例: リビングのテレビ")
        self.display_class = QComboBox()
        for value, label in _DISPLAY_CLASS_ITEMS:
            self.display_class.addItem(label, value)
        self.chassis_width = QDoubleSpinBox()
        self.chassis_width.setRange(0.1, 10.0)
        self.chassis_width.setDecimals(3)
        self.chassis_width.setSuffix(' m')
        self.chassis_depth = QDoubleSpinBox()
        self.chassis_depth.setRange(0.005, 2.0)
        self.chassis_depth.setDecimals(3)
        self.chassis_depth.setValue(0.06)
        self.chassis_depth.setSuffix(' m')
        self.chassis_height = QDoubleSpinBox()
        self.chassis_height.setRange(0.05, 5.0)
        self.chassis_height.setDecimals(3)
        self.chassis_height.setSuffix(' m')
        self.active_width = QDoubleSpinBox()
        self.active_width.setRange(0.1, 10.0)
        self.active_width.setDecimals(3)
        self.active_width.setSuffix(' m')
        self.active_height = QDoubleSpinBox()
        self.active_height.setRange(0.05, 5.0)
        self.active_height.setDecimals(3)
        self.active_height.setSuffix(' m')
        self.source_name = QLineEdit()
        self.source_name.setPlaceholderText("測定者/出典")
        self.source_reference = QLineEdit()
        self.source_reference.setPlaceholderText("参照位置（ページ/メモ）")
        form.addRow("仕様ID", self.spec_id)
        form.addRow("バージョン", self.version)
        form.addRow("表示名", self.user_label)
        form.addRow("ディスプレイ種別", self.display_class)
        form.addRow("筐体幅", self.chassis_width)
        form.addRow("筐体奥行", self.chassis_depth)
        form.addRow("筐体高さ", self.chassis_height)
        form.addRow("有効画域 幅", self.active_width)
        form.addRow("有効画域 高さ", self.active_height)
        form.addRow("出典", self.source_name)
        form.addRow("参照", self.source_reference)
        layout.addLayout(form)
        hint = QLabel(
            "ユーザー定義の仕様は user_defined 証跡として記録されます — "
            "メーカー/モデル名は入力できず、輝度・リフレッシュ等の能力は"
            "測定値がない限り未評価（UNKNOWN）のままです。"
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
        if not self.spec_id.text().strip():
            QMessageBox.warning(
                self, "ディスプレイ仕様", "仕様IDを入力してください"
            )
            self.spec_id.setFocus()
            return
        if not self.user_label.text().strip():
            QMessageBox.warning(
                self, "ディスプレイ仕様", "表示名を入力してください"
            )
            self.user_label.setFocus()
            return
        super().accept()

    def values(self) -> dict[str, object]:
        return {
            'specification_id': self.spec_id.text().strip(),
            'version': self.version.text().strip() or '1',
            'user_label': self.user_label.text().strip(),
            'display_class': self.display_class.currentData(),
            'chassis_width_m': float(self.chassis_width.value()),
            'chassis_depth_m': float(self.chassis_depth.value()),
            'chassis_height_m': float(self.chassis_height.value()),
            'active_image_width_m': float(self.active_width.value()),
            'active_image_height_m': float(self.active_height.value()),
            'source_name': self.source_name.text().strip() or '手動入力',
            'source_reference': self.source_reference.text().strip() or 'ユーザー入力',
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
    createDisplaySpecRequested = Signal()
    poseChanged = Signal(str, object)  # (seat entity id, pose_id or None)
    poseSaveRequested = Signal(str)  # seat entity id
    transferChanged = Signal(str, object)  # (screen entity id, transfer_id or None)
    transferSaveRequested = Signal(str)  # screen entity id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("映像ジオメトリ（プロジェクター/ディスプレイ/視線）")
        heading.setWordWrap(True)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        # --- target type (#1054) ----------------------------------------------
        # A direct-view display is never a fake projector + passive screen:
        # the workspace target type switches which bindings apply.
        target_row = QHBoxLayout()
        target_row.setSpacing(4)
        target_row.addWidget(QLabel("ターゲット"))
        self.target_combo = QComboBox()
        self.target_combo.addItem("プロジェクター＋スクリーン", 'projection')
        self.target_combo.addItem("ディスプレイ", 'direct_view')
        target_row.addWidget(self.target_combo, stretch=1)
        layout.addLayout(target_row)

        self.projection_section = QWidget()
        projection_layout = QVBoxLayout(self.projection_section)
        projection_layout.setContentsMargins(0, 0, 0, 0)
        projection_layout.setSpacing(4)
        layout.addWidget(self.projection_section)

        # --- projector binding -------------------------------------------------
        proj_row = QHBoxLayout()
        proj_row.setSpacing(4)
        proj_row.addWidget(QLabel("プロジェクター"))
        self.projector_combo = QComboBox()
        proj_row.addWidget(self.projector_combo, stretch=1)
        projection_layout.addLayout(proj_row)

        spec_row = QHBoxLayout()
        spec_row.setSpacing(4)
        spec_row.addWidget(QLabel("仕様"))
        self.spec_combo = QComboBox()
        spec_row.addWidget(self.spec_combo, stretch=1)
        self.new_spec_button = QPushButton("登録…")
        spec_row.addWidget(self.new_spec_button)
        projection_layout.addLayout(spec_row)

        # --- screen binding ----------------------------------------------------
        self.screen_heading = QLabel("スクリーン画素（バインド未設定）")
        set_typography_role(self.screen_heading, TypographyRole.SECONDARY)
        projection_layout.addWidget(self.screen_heading)
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
        projection_layout.addLayout(screen_form)

        # --- direct-view display binding (#1054) -------------------------------
        # A 'display' scene entity plus its active-aperture binding — never a
        # projector, never a passive screen, never acoustically transparent.
        self.display_section = QWidget()
        display_layout = QVBoxLayout(self.display_section)
        display_layout.setContentsMargins(0, 0, 0, 0)
        display_layout.setSpacing(4)
        layout.addWidget(self.display_section)

        display_row = QHBoxLayout()
        display_row.setSpacing(4)
        display_row.addWidget(QLabel("ディスプレイ"))
        self.display_combo = QComboBox()
        display_row.addWidget(self.display_combo, stretch=1)
        display_layout.addLayout(display_row)

        display_spec_row = QHBoxLayout()
        display_spec_row.setSpacing(4)
        display_spec_row.addWidget(QLabel("仕様"))
        self.display_spec_combo = QComboBox()
        display_spec_row.addWidget(self.display_spec_combo, stretch=1)
        self.new_display_spec_button = QPushButton("登録…")
        self.new_display_spec_button.setToolTip(
            "ディスプレイ仕様権威を新規登録します（ユーザー定義証跡）"
        )
        display_spec_row.addWidget(self.new_display_spec_button)
        display_layout.addLayout(display_spec_row)

        self.display_heading = QLabel("ディスプレイ有効画域（バインド未設定）")
        set_typography_role(self.display_heading, TypographyRole.SECONDARY)
        display_layout.addWidget(self.display_heading)
        display_form = QFormLayout()
        display_form.setContentsMargins(0, 0, 0, 0)
        self.display_width = QDoubleSpinBox()
        self.display_width.setRange(0.1, 10.0)
        self.display_width.setSingleStep(0.01)
        self.display_width.setDecimals(3)
        self.display_width.setSuffix(' m')
        self.display_height = QDoubleSpinBox()
        self.display_height.setRange(0.05, 5.0)
        self.display_height.setSingleStep(0.01)
        self.display_height.setDecimals(3)
        self.display_height.setSuffix(' m')
        self.display_offset_x = QDoubleSpinBox()
        self.display_offset_x.setRange(-5.0, 5.0)
        self.display_offset_x.setSingleStep(0.01)
        self.display_offset_x.setDecimals(3)
        self.display_offset_x.setSuffix(' m')
        self.display_offset_z = QDoubleSpinBox()
        self.display_offset_z.setRange(-5.0, 5.0)
        self.display_offset_z.setSingleStep(0.01)
        self.display_offset_z.setDecimals(3)
        self.display_offset_z.setSuffix(' m')
        self.display_frame_clearance = QDoubleSpinBox()
        self.display_frame_clearance.setRange(0.0, 2.0)
        self.display_frame_clearance.setSingleStep(0.01)
        self.display_frame_clearance.setSuffix(' m')
        self.display_mounting = QComboBox()
        for value, label in _DISPLAY_MOUNTING_ITEMS:
            self.display_mounting.addItem(label, value)
        display_form.addRow("有効画域 幅", self.display_width)
        display_form.addRow("有効画域 高さ", self.display_height)
        display_form.addRow("中心オフセット X", self.display_offset_x)
        display_form.addRow("中心オフセット Z", self.display_offset_z)
        display_form.addRow("フレーム余白", self.display_frame_clearance)
        display_form.addRow("設置方式", self.display_mounting)
        display_layout.addLayout(display_form)

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
        self.new_display_spec_button.clicked.connect(self.createDisplaySpecRequested)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
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
            self.display_width,
            self.display_height,
            self.display_offset_x,
            self.display_offset_z,
            self.display_frame_clearance,
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
        self.display_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
        self.display_spec_combo.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())
        self.display_mounting.currentIndexChanged.connect(lambda _i: self.bindingsChanged.emit())

        self._syncing = False
        self._screen_entity_id: str | None = None
        self._apply_target_visibility()

    def _target_changed(self, _index: int) -> None:
        self._apply_target_visibility()
        if not self._syncing:
            self.bindingsChanged.emit()

    def _apply_target_visibility(self) -> None:
        """Show only the binding section that matches the target type."""
        direct_view = self.target_combo.currentData() == 'direct_view'
        self.projection_section.setVisible(not direct_view)
        self.display_section.setVisible(direct_view)

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
        display_specifications: tuple = (),
    ) -> None:
        """Refresh all widgets from authoritative state."""

        self._syncing = True
        try:
            index = self.target_combo.findData(workspace.target_type)
            self.target_combo.blockSignals(True)
            self.target_combo.setCurrentIndex(index if index >= 0 else 0)
            self.target_combo.blockSignals(False)
            self._apply_target_visibility()

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

            current_display = self.display_combo.currentData()
            self.display_combo.blockSignals(True)
            self.display_combo.clear()
            self.display_combo.addItem("（未選択）", None)
            displays = [entity for entity in document.entities if entity.kind == 'display']
            for entity in displays:
                self.display_combo.addItem(entity.name, entity.entity_id)
            wanted_display = workspace.display_entity_id or current_display
            index = self.display_combo.findData(wanted_display)
            self.display_combo.setCurrentIndex(index if index >= 0 else 0)
            self.display_combo.blockSignals(False)

            current_display_spec = self.display_spec_combo.currentData()
            self.display_spec_combo.blockSignals(True)
            self.display_spec_combo.clear()
            self.display_spec_combo.addItem("（未バインド）", None)
            for spec in display_specifications:
                label = f"{spec.specification_id} v{spec.version}"
                if spec.manufacturer or spec.model:
                    label += f" · {' '.join(v for v in (spec.manufacturer, spec.model) if v)}"
                elif spec.user_label:
                    label += f" · {spec.user_label}"
                self.display_spec_combo.addItem(label, spec.specification_sha256)
            wanted_display_spec = (
                workspace.display_specification_sha256 or current_display_spec
            )
            index = self.display_spec_combo.findData(wanted_display_spec)
            self.display_spec_combo.setCurrentIndex(index if index >= 0 else 0)
            self.display_spec_combo.blockSignals(False)

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

            if displays:
                display = next(
                    (
                        entity
                        for entity in displays
                        if entity.entity_id == self.display_combo.currentData()
                    ),
                    displays[0],
                )
                binding = workspace.display_binding
                if binding is not None and binding.entity_id == display.entity_id:
                    self.display_width.setValue(binding.visible_width_m)
                    self.display_height.setValue(binding.visible_height_m)
                    self.display_offset_x.setValue(
                        binding.image_center_offset_local_m.x_m
                    )
                    self.display_offset_z.setValue(
                        binding.image_center_offset_local_m.z_m
                    )
                    self.display_frame_clearance.setValue(binding.frame_clearance_m)
                    index = self.display_mounting.findData(binding.mounting)
                    self.display_mounting.setCurrentIndex(index if index >= 0 else 0)
                else:
                    # Default the aperture to the chassis front face — the user
                    # edits it down to the real active area; never silently
                    # bound to a different display's leftover values.
                    self.display_width.setValue(display.size_m.x_m)
                    self.display_height.setValue(display.size_m.z_m)
                    self.display_offset_x.setValue(0.0)
                    self.display_offset_z.setValue(0.0)
                    self.display_frame_clearance.setValue(0.0)
                    self.display_mounting.setCurrentIndex(0)
                self.display_heading.setText(
                    f"ディスプレイ有効画域 — {display.name}"
                )
            else:
                self.display_heading.setText("ディスプレイ有効画域（バインド未設定）")

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

    def current_target_type(self) -> str:
        return self.target_combo.currentData() or 'projection'

    def current_projector_entity_id(self) -> str | None:
        return self.projector_combo.currentData()

    def current_display_entity_id(self) -> str | None:
        return self.display_combo.currentData()

    def current_display_specification_sha256(self) -> str | None:
        return self.display_spec_combo.currentData()

    def current_display_values(self) -> dict[str, object]:
        return {
            'visible_width_m': float(self.display_width.value()),
            'visible_height_m': float(self.display_height.value()),
            'image_center_offset_x_m': float(self.display_offset_x.value()),
            'image_center_offset_z_m': float(self.display_offset_z.value()),
            'frame_clearance_m': float(self.display_frame_clearance.value()),
            'mounting': self.display_mounting.currentData(),
        }

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
            'PASS': '合格',
            'FAIL': '不合格',
            'UNKNOWN': '判定不能',
            'NOT_APPLICABLE': '—',
        }
        self.results_tree.clear()
        if evaluation is None:
            return
        rows: list[tuple[str, str]] = []
        projection = getattr(evaluation, 'projection', None)
        surface = getattr(evaluation, 'surface', None)
        if surface is not None:
            # Direct-view evaluation (#1054): no projection fields — the
            # display surface carries containment/conformance instead.
            rows.extend(
                [
                    ('映像面: 総合', status_labels.get(surface.status, surface.status)),
                    (
                        '映像面: シャーシ収容',
                        status_labels.get(
                            surface.chassis_containment_status,
                            surface.chassis_containment_status,
                        ),
                    ),
                    (
                        '映像面: 仕様適合',
                        status_labels.get(
                            surface.spec_conformance_status,
                            surface.spec_conformance_status,
                        ),
                    ),
                ]
            )
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
                '合格' if collisions_fail == 0 else f'不合格 · {collisions_fail} 件',
            )
        )
        for name, status in rows:
            item = QTreeWidgetItem([name, status])
            if status.startswith('不合格'):
                item.setForeground(1, Qt.GlobalColor.red)
            self.results_tree.addTopLevelItem(item)

    def show_message(self, text: str) -> None:
        self.status_label.setText(text)
