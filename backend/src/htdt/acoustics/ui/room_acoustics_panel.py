"""Native acoustics-context panels: materials/boundary and treatments.

- ``SurfaceMaterialPanel`` (#465): the first-class acoustic material/
  boundary library plus per-semantic-surface assignment. Wave and
  geometric capabilities are declared separately — impedance is never
  inferred from absorption bands; unassigned surfaces are shown honestly
  and replay as UNSUPPORTED/missing in R120 rather than guessed.
- ``RoomTreatmentPanel`` (#451): native authoring of
  AcousticTreatmentDefinitions, placement/lifecycle against an exact
  SceneRevision + semantic surface host, surface-binding evaluation states,
  and named baseline-vs-treatment comparisons over the exact placement set.

Both panels read and write only the persisted authorities — no scripts or
direct database edits are needed to reach the states they display.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt
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
    QScrollArea,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...modal_transient import exec_transient
from ... import file_dialog_memory
from ..domain.acoustic_benchmark import GeometricAcousticBand, SpecificImpedancePoint
from ..domain.cad_acoustic_material import (
    GEOMETRIC_MODEL_LABELS,
    SURFACE_CLASS_LABELS,
    WAVE_MODEL_LABELS,
    AcousticMaterialAuthority,
    build_acoustic_material,
    material_capability_label,
)
from ..domain.cad_acoustic_treatment import (
    EXTERNAL_TREATMENT_SOURCE_KINDS,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentLayer,
    TreatmentPhysicalParameters,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    revise_treatment_placement,
)
from ..domain.cad_acoustic_treatment_comparison import (
    build_treatment_design_candidate,
    build_treatment_design_comparison,
)
from ...cad_scene import Position3
from ...cad_document import CommandPresentation, CompositeEditCommand
from ...room_treatment_overlay import LIFECYCLE_LABELS
from ...error_boundary import EXPECTED_OPERATION_ERRORS
from ...ui_theme import TypographyRole, set_typography_role
from ...user_facing_error import operation_error_message

_SURFACE_ROLE = Qt.ItemDataRole.UserRole
_MATERIAL_ROLE = Qt.ItemDataRole.UserRole
_DEFINITION_ROLE = Qt.ItemDataRole.UserRole
_PLACEMENT_ROLE = Qt.ItemDataRole.UserRole

#: Sentinel combo data shown for a stored assignment whose material
#: reference no longer resolves (#996) — displayed, never written back.
_UNRESOLVED_REF = '\x00unresolved-material-reference'

#: BoundaryMaterialState.summary → operator-facing JA (#996).
_BOUNDARY_STATE_LABELS: dict[str, str] = {
    'no_boundaries': '部屋境界面がありません',
    'unassigned': '全境界面が未割当',
    'partial': '一部の境界面のみ割当済み',
    'uniform': '全境界面に同一材質を適用済み',
    'mixed': '面ごとに異なる材質が割当（混在）',
    'unresolved': '解決不能な割当参照があります',
}

_TREATMENT_TYPE_LABELS: dict[str, str] = {
    'porous_absorber': '多孔質吸音材',
    'absorber_with_air_gap': '空気層付き吸音材',
    'membrane_panel_absorber': '膜パネル吸音材',
    'perforated_slotted_absorber': '穿孔/スリット吸音材',
    'bass_trap': 'バストラップ',
    'diffuser_scattering_element': '拡散要素',
    'hybrid': 'ハイブリッド',
}

_SOURCE_KIND_LABELS: dict[str, str] = {
    'user_defined': 'ユーザー定義（画面入力）',
    'manufacturer': 'メーカー資料',
    'measurement': '測定データ',
    'literature': '文献',
}

_BINDING_STATE_LABELS: dict[str, str] = {
    'exact': 'EXACT (バインド済み)',
    'unbound': '未バインド',
    'legacy_unverified': '旧式・未検証',
    'scene_revision_missing': 'シーンリビジョンなし',
    'wrong_scene_revision': '別リビジョン',
    'scene_authority_mismatch': 'シーン権威不一致',
    'semantic_geometry_missing': 'セマンティックジオメトリなし',
    'surface_removed': '面が削除済み',
    'surface_authority_mismatch': '面権威不一致',
    'stale_scene_revision': '古い (リビジョン更新)',
    'stale_semantic_geometry': '古い (ジオメトリ更新)',
}


class MaterialDialog(QDialog):
    """Register an AcousticMaterialAuthority with explicit tiered capability."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('音響マテリアルを登録')
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label = QLineEdit()
        self.label.setPlaceholderText('例: 25mmグラスウール')
        self.label.setToolTip('このマテリアルの表示名')
        form.addRow('名称', self.label)
        self.wave_model = QComboBox()
        for value, label_text in WAVE_MODEL_LABELS.items():
            self.wave_model.addItem(f'{value} — {label_text}', value)
        self.wave_model.setCurrentIndex(
            list(WAVE_MODEL_LABELS).index('unsupported')
        )
        self.wave_model.setToolTip(
            '低域（波動）計算でこのマテリアルを扱える精度 · 不明なら unsupported'
        )
        form.addRow('波動対応', self.wave_model)
        self.impedance = QPlainTextEdit()
        self.impedance.setPlaceholderText(
            '比インピーダンス点 — 1行1点: freq_hz,resistance,reactance '
            '(specific_impedance_tableのみ)'
        )
        self.impedance.setMaximumHeight(60)
        self.impedance.setToolTip(
            '周波数ごとの表面インピーダンス（波動計算の入力）· '
            '「波動対応」が specific_impedance_table のときだけ使われます'
        )
        form.addRow('インピーダンス', self.impedance)
        self.geometric_model = QComboBox()
        for value, label_text in GEOMETRIC_MODEL_LABELS.items():
            self.geometric_model.addItem(f'{value} — {label_text}', value)
        self.geometric_model.setCurrentIndex(
            list(GEOMETRIC_MODEL_LABELS).index('unsupported')
        )
        self.geometric_model.setToolTip(
            '中高域（幾何音響）計算でこのマテリアルを扱える精度'
        )
        form.addRow('幾何対応', self.geometric_model)
        self.bands = QPlainTextEdit()
        self.bands.setPlaceholderText(
            'バンド — 1行1点: center_hz,absorption,scattering (bandedのみ)'
        )
        self.bands.setMaximumHeight(60)
        self.bands.setToolTip(
            '帯域ごとの吸音率・散乱率（幾何音響の入力）· '
            '「幾何対応」が banded のときだけ使われます'
        )
        form.addRow('バンド', self.bands)
        self.provenance = QLineEdit()
        self.provenance.setPlaceholderText('出典（例: メーカー公表値 / 現場実測）')
        self.provenance.setToolTip('このマテリアル値の出典（公表値・実測など）')
        form.addRow('出典', self.provenance)
        for field in (
            self.label, self.wave_model, self.impedance,
            self.geometric_model, self.bands, self.provenance,
        ):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip() or 'このマテリアルの表示名')
        layout.addLayout(form)
        hint = QLabel(
            '波動と幾何の能力は別々の権威です — '
            '吸音バンドからインピーダンスは推定されません (その逆も同じ)。'
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
                self, '音響マテリアル', '名称を入力してください'
            )
            self.label.setFocus()
            return
        if not self.provenance.text().strip():
            QMessageBox.warning(
                self, '音響マテリアル', '出典を入力してください'
            )
            self.provenance.setFocus()
            return
        wave = self.wave_model.currentData()
        geometric = self.geometric_model.currentData()
        impedance_text = self.impedance.toPlainText().strip()
        bands_text = self.bands.toPlainText().strip()
        if wave == 'unsupported' and geometric == 'unsupported':
            QMessageBox.warning(
                self,
                '音響マテリアル',
                '波動対応または幾何対応のいずれかを選択してください',
            )
            return
        if wave == 'specific_impedance_table' and not impedance_text:
            QMessageBox.warning(
                self,
                '音響マテリアル',
                '比インピーダンス表にはインピーダンス点を1行以上入力してください',
            )
            self.impedance.setFocus()
            return
        if wave != 'specific_impedance_table' and impedance_text:
            QMessageBox.warning(
                self,
                '音響マテリアル',
                'インピーダンス点は比インピーダンス表の場合のみ有効です',
            )
            self.impedance.setFocus()
            return
        if geometric == 'banded' and not bands_text:
            QMessageBox.warning(
                self,
                '音響マテリアル',
                'バンドモデルにはバンドを1行以上入力してください',
            )
            self.bands.setFocus()
            return
        if geometric != 'banded' and bands_text:
            QMessageBox.warning(
                self,
                '音響マテリアル',
                'バンドはbandedモデルの場合のみ有効です',
            )
            self.bands.setFocus()
            return
        super().accept()

    def values(self) -> dict[str, Any]:
        return {
            'label': self.label.text().strip(),
            'wave_model': self.wave_model.currentData(),
            'impedance_text': self.impedance.toPlainText().strip(),
            'geometric_model': self.geometric_model.currentData(),
            'bands_text': self.bands.toPlainText().strip(),
            'provenance': self.provenance.text().strip(),
        }


class TreatmentDefinitionDialog(QDialog):
    """Author one AcousticTreatmentDefinition with user_defined provenance.

    The typed values become the immutable definition + a user_defined
    evidence authority. No acoustic model is fabricated — a definition
    without a measured/declared acoustic model reports UNKNOWN prediction
    capability downstream.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('音響処理の定義を新規作成')
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText('例: 50mm多孔質パネル')
        self.name.setToolTip('この吸音処理の表示名')
        form.addRow('名称', self.name)
        self.version = QLineEdit('1')
        self.version.setToolTip('同じ名前の定義を更新する際の版番号')
        form.addRow('バージョン', self.version)
        self.treatment_type = QComboBox()
        self.treatment_type.setToolTip(
            '吸音処理の構造種別（多孔質・空気層つき・バストラップ・拡散など）'
        )
        for value in (
            'porous_absorber',
            'absorber_with_air_gap',
            'membrane_panel_absorber',
            'perforated_slotted_absorber',
            'bass_trap',
            'diffuser_scattering_element',
            'hybrid',
        ):
            self.treatment_type.addItem(
                f'{value} — {_TREATMENT_TYPE_LABELS[value]}', value
            )
        form.addRow('種類', self.treatment_type)
        # ``width``/``height`` would shadow QWidget.width()/height().
        self.width_m = QDoubleSpinBox()
        self.width_m.setRange(0.01, 20.0)
        self.width_m.setValue(0.6)
        self.height_m = QDoubleSpinBox()
        self.height_m.setRange(0.01, 20.0)
        self.height_m.setValue(1.2)
        self.thickness = QDoubleSpinBox()
        self.thickness.setRange(0.001, 2.0)
        self.thickness.setValue(0.05)
        self.thickness.setDecimals(3)
        self.air_gap = QDoubleSpinBox()
        self.air_gap.setRange(0.0, 2.0)
        self.air_gap.setDecimals(3)
        self.width_m.setToolTip('処理パネルの幅（m）')
        self.height_m.setToolTip('処理パネルの高さ（m）')
        self.thickness.setToolTip('吸音材の厚さ（m）· 低音ほど厚さが効きます')
        self.air_gap.setToolTip(
            'パネル背面と壁の間の空気層の厚さ（m）· 低音域の吸音に効きます'
        )
        form.addRow('幅 (m)', self.width_m)
        form.addRow('高さ (m)', self.height_m)
        form.addRow('厚さ (m)', self.thickness)
        form.addRow('空気層 (m)', self.air_gap)
        self.layer_material = QLineEdit()
        self.layer_material.setPlaceholderText('層1 材質名 (例: グラスウール)')
        self.layer_material.setToolTip(
            '表面側から数えた第1層の材質名（例: グラスウール）'
        )
        form.addRow('層1 材質', self.layer_material)
        self.layer_density = QDoubleSpinBox()
        self.layer_density.setRange(0.0, 2000.0)
        self.layer_density.setSpecialValueText('不明')
        self.layer_density.setToolTip(
            '第1層材質の密度（kg/m³）· 0または「不明」のままなら不明として記録されます'
        )
        form.addRow('層1 密度 kg/m³', self.layer_density)
        # REV44-STAGED: external-source declarations retain the picked source
        # file as a managed content-addressed asset (cad_measurement_assets)
        # so the evidence's source_sha256 resolves to real retained bytes.
        self.source_kind = QComboBox()
        for value, label in _SOURCE_KIND_LABELS.items():
            self.source_kind.addItem(label, value)
        self.source_kind.setToolTip(
            '定義の出典 — 画面入力ならユーザー定義、メーカー資料・測定・文献'
            'なら出典ファイルを添付してください'
        )
        form.addRow('出典種別', self.source_kind)
        self.source_name = QLineEdit()
        self.source_name.setPlaceholderText('例: メーカー資料 XYZ-50')
        self.source_name.setToolTip('出典を識別する名前（資料名・測定名など）')
        form.addRow('出典名', self.source_name)
        self.source_version_edit = QLineEdit('1')
        self.source_version_edit.setToolTip('出典資料の版番号')
        form.addRow('出典版', self.source_version_edit)
        self.source_reference = QLineEdit()
        self.source_reference.setPlaceholderText('例: p.12、URL、測定メモ')
        self.source_reference.setToolTip('出典内の参照箇所（任意）')
        form.addRow('出典参照', self.source_reference)
        file_row = QHBoxLayout()
        self.source_file_button = QPushButton('出典ファイルを選択…')
        self.source_file_button.setToolTip(
            '出典ファイルを選ぶと、内容のハッシュでプロジェクト内に保管され、'
            'バックアップ対象になります'
        )
        self.source_file_button.clicked.connect(self._pick_source_file)
        file_row.addWidget(self.source_file_button)
        self.source_file_label = QLabel('（未選択）')
        self.source_file_label.setWordWrap(True)
        file_row.addWidget(self.source_file_label, 1)
        file_host = QWidget()
        file_host.setLayout(file_row)
        form.addRow('出典ファイル', file_host)
        self._source_bytes: bytes | None = None
        self._source_file_name: str | None = None
        self.source_kind.currentIndexChanged.connect(
            self._refresh_source_fields
        )
        self._refresh_source_fields()
        for field in (
            self.name, self.version, self.treatment_type,
            self.width_m, self.height_m, self.thickness, self.air_gap,
            self.layer_material, self.layer_density,
        ):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip())
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_source_fields(self) -> None:
        external = self.source_kind.currentData() in (
            EXTERNAL_TREATMENT_SOURCE_KINDS
        )
        for field in (
            self.source_name, self.source_version_edit,
            self.source_reference, self.source_file_button,
        ):
            field.setEnabled(external)
        if not external:
            self._source_bytes = None
            self._source_file_name = None
            self.source_file_label.setText('（未選択）')

    def _pick_source_file(self) -> None:
        selected, _filter = file_dialog_memory.get_open_file_name(
            self, '出典ファイルを選択', 'treatment-source'
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = path.read_bytes()
        except OSError as exc:
            QMessageBox.warning(
                self, '音響処理の定義',
                '出典ファイルを読み込めませんでした: '
                f'{operation_error_message(exc)}',
            )
            return
        self._source_bytes = data
        self._source_file_name = path.name
        self.source_file_label.setText(
            f'{path.name}（{len(data)} bytes）'
        )

    def accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(
                self, '音響処理の定義', '名称を入力してください'
            )
            self.name.setFocus()
            return
        if not self.layer_material.text().strip():
            QMessageBox.warning(
                self, '音響処理の定義', '層1 材質名を入力してください'
            )
            self.layer_material.setFocus()
            return
        if self.source_kind.currentData() in EXTERNAL_TREATMENT_SOURCE_KINDS:
            if not self.source_name.text().strip():
                QMessageBox.warning(
                    self, '音響処理の定義', '出典名を入力してください'
                )
                self.source_name.setFocus()
                return
            if self._source_bytes is None:
                QMessageBox.warning(
                    self, '音響処理の定義',
                    '外部出典の定義には出典ファイルの添付が必要です。',
                )
                return
        super().accept()

    def values(self) -> dict[str, Any]:
        return {
            'name': self.name.text().strip(),
            'version': self.version.text().strip() or '1',
            'treatment_type': self.treatment_type.currentData(),
            'width_m': float(self.width_m.value()),
            'height_m': float(self.height_m.value()),
            'thickness_m': float(self.thickness.value()),
            'air_gap_m': float(self.air_gap.value()),
            'layer_material': self.layer_material.text().strip(),
            'layer_density': (
                None
                if self.layer_density.value() <= 0.0
                else float(self.layer_density.value())
            ),
            'source_kind': self.source_kind.currentData(),
            'source_name': self.source_name.text().strip(),
            'source_version': (
                self.source_version_edit.text().strip() or '1'
            ),
            'source_reference': self.source_reference.text().strip(),
            'source_bytes': self._source_bytes,
            'source_file_name': self._source_file_name,
        }


class SurfaceMaterialPanel(QWidget):
    """Material library + per-semantic-surface assignment (#465)."""

    def __init__(self, controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._surfaces = ()
        self._syncing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel('マテリアル / 境界ライブラリ')
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        header.setToolTip('壁・天井・床の表面材質（吸音・反射特性）の定義と割り当て')
        layout.addWidget(header)
        material_row = QHBoxLayout()
        self.materials = QComboBox()
        self.materials.setToolTip(
            '登録済みの音響マテリアル · 選ぶと能力と下の面一覧で割り当てできます'
        )
        self.materials.currentIndexChanged.connect(self._material_selected)
        material_row.addWidget(self.materials, stretch=1)
        self.material_new = QPushButton('新規…')
        self.material_new.setToolTip('新しい音響マテリアルを登録します')
        self.material_new.clicked.connect(self._new_material)
        material_row.addWidget(self.material_new)
        self.material_apply_all = QPushButton('全部屋境界面へ適用')
        self.material_apply_all.setToolTip(
            '選択中のマテリアルを全ての部屋境界面（壁・天井・床）に割り当てます'
        )
        self.material_apply_all.clicked.connect(self._apply_to_all_boundaries)
        material_row.addWidget(self.material_apply_all)
        layout.addLayout(material_row)
        self.material_detail = QLabel('')
        self.material_detail.setWordWrap(True)
        set_typography_role(self.material_detail, TypographyRole.SECONDARY)
        layout.addWidget(self.material_detail)

        surfaces_header = QLabel('面ごとの割り当て')
        set_typography_role(surfaces_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(surfaces_header)
        self.surface_tree = QTreeWidget()
        self.surface_tree.setHeaderLabels(('面', 'マテリアル'))
        tree_header = self.surface_tree.headerItem()
        if tree_header is not None:
            tree_header.setToolTip(0, '部屋を構成する面（壁・天井・床・開口など）')
            tree_header.setToolTip(1, 'その面に割り当てる音響マテリアル')
        self.surface_tree.itemChanged.connect(self._surface_item_changed)
        layout.addWidget(self.surface_tree, stretch=1)
        self.readiness = QLabel('')
        self.readiness.setWordWrap(True)
        set_typography_role(self.readiness, TypographyRole.SECONDARY)
        layout.addWidget(self.readiness)
        self.refresh()

    def _revision(self):
        return self.controller.repository.current_head(self.controller.document_id)

    def refresh(self) -> None:
        self._syncing = True
        try:
            self._refresh_materials()
            self._refresh_surfaces()
        finally:
            self._syncing = False

    def _refresh_materials(self) -> None:
        materials = self.controller.material_repository.list_materials()
        current = self.materials.currentData()
        self.materials.blockSignals(True)
        self.materials.clear()
        for material in materials:
            self.materials.addItem(
                f'{material.label} v{material.authority_version}',
                material.material_id,
            )
        if current is not None:
            index = self.materials.findData(current)
            if index >= 0:
                self.materials.setCurrentIndex(index)
        self.materials.blockSignals(False)
        self._material_selected(self.materials.currentIndex())

    def _material_selected(self, _index: int) -> None:
        material_id = self.materials.currentData()
        material = (
            None
            if material_id is None
            else self.controller.material_repository.get_material(material_id)
        )
        self.material_detail.setText(
            '' if material is None else material_capability_label(material)
        )

    def _refresh_surfaces(self) -> None:
        revision = self._revision()
        geometry = (
            None
            if revision is None or revision.document.r120_semantic_geometry is None
            else revision.document.r120_semantic_geometry
        )
        self._surfaces = () if geometry is None else geometry.surfaces
        repository = self.controller.material_repository
        try:
            # #996: honest aggregate — rows that fail to resolve are
            # reported, never coerced to 未割当.
            state = repository.boundary_material_state(
                self.controller.document_id, self._surfaces
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self.surface_tree.clear()
            self._surfaces = ()
            self.readiness.setText(
                'マテリアル割当の読取に失敗 — '
                + operation_error_message(exc)
            )
            return
        entries = {entry.surface_id: entry for entry in state.entries}
        self.surface_tree.blockSignals(True)
        self.surface_tree.clear()
        for surface in self._surfaces:
            class_label = SURFACE_CLASS_LABELS.get(
                surface.semantic_class, surface.semantic_class
            )
            item = QTreeWidgetItem(
                (f'{surface.surface_key} · {class_label}', '')
            )
            item.setData(0, _SURFACE_ROLE, surface.surface_id)
            combo = QComboBox()
            combo.addItem('未割当 (不明)', None)
            for index in range(self.materials.count()):
                combo.addItem(
                    self.materials.itemText(index),
                    self.materials.itemData(index),
                )
            entry = entries.get(surface.surface_id)
            if entry is not None and entry.status == 'unresolved_reference':
                combo.addItem(
                    f'（解決不能な割当: {entry.material_id}）',
                    _UNRESOLVED_REF,
                )
                combo.setCurrentIndex(combo.count() - 1)
            else:
                combo.setCurrentIndex(
                    0
                    if entry is None or entry.material_id is None
                    else max(0, combo.findData(entry.material_id))
                )
            combo.activated.connect(
                lambda _i, sid=surface.surface_id, c=combo:
                self._surface_combo_changed(sid, c)
            )
            self.surface_tree.addTopLevelItem(item)
            self.surface_tree.setItemWidget(item, 1, combo)
        self.surface_tree.blockSignals(False)
        if self._surfaces:
            assigned = sum(
                1 for entry in state.entries if entry.status == 'assigned'
            )
            unassigned = sum(
                1 for entry in state.entries if entry.status == 'unassigned'
            )
            unresolved = sum(
                1
                for entry in state.entries
                if entry.status == 'unresolved_reference'
            )
            summary = _BOUNDARY_STATE_LABELS.get(
                state.summary, state.summary
            )
            if state.summary == 'uniform' and state.assigned_material_ids:
                resolved = repository.get_material(
                    state.assigned_material_ids[0]
                )
                if resolved is not None:
                    summary = (
                        f'全境界面に同一材質を適用済み「{resolved.label}」'
                    )
            parts = [
                f'{len(self._surfaces)}面 — 割当{assigned}・'
                f'未割当{unassigned}・参照解決不能{unresolved}',
                summary,
            ]
            if state.stale_surface_ids:
                parts.append(
                    f'幾何外の残存割当 {len(state.stale_surface_ids)}件'
                )
            if unassigned:
                parts.append(
                    '未割当面はR120で material_missing として報告されます。'
                )
            self.readiness.setText(' — '.join(parts))
        else:
            self.readiness.setText(
                'セマンティックジオメトリがありません — '
                '面ごとのマテリアル割当にはR120セマンティック面が必要です。'
            )

    def _surface_item_changed(self, _item, _column) -> None:
        pass

    def _surface_combo_changed(self, surface_id: str, combo: QComboBox) -> None:
        if self._syncing:
            return
        material_id = combo.currentData()
        if material_id == _UNRESOLVED_REF:
            # Displayed-only sentinel for a dangling stored row (#996).
            self.refresh()
            return
        repository = self.controller.material_repository
        try:
            if material_id is None:
                repository.clear_assignment(
                    self.controller.document_id, surface_id
                )
            else:
                material = repository.get_material(material_id)
                if material is not None:
                    repository.assign_material(
                        self.controller.document_id,
                        surface_id,
                        material,
                        surfaces=self._surfaces,
                    )
        except EXPECTED_OPERATION_ERRORS as exc:
            self.refresh()
            self.readiness.setText(
                '割当の変更に失敗: ' + operation_error_message(exc)
            )
            return
        self.refresh()

    def _new_material(self) -> None:
        dialog = MaterialDialog(self)
        if exec_transient(dialog) != MaterialDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            impedance_rows = [
                [part.strip() for part in line.split(',')]
                for line in str(values['impedance_text']).splitlines()
                if line.strip()
            ]
            if any(len(parts) != 3 for parts in impedance_rows):
                raise ValueError(
                    'インピーダンス行は周波数,抵抗,リアクタンスの3列です'
                )
            impedance = tuple(
                SpecificImpedancePoint(
                    frequency_hz=float(parts[0]),
                    resistance_pa_s_m=float(parts[1]),
                    reactance_pa_s_m=float(parts[2]),
                )
                for parts in impedance_rows
            )
            band_rows = [
                [part.strip() for part in line.split(',')]
                for line in str(values['bands_text']).splitlines()
                if line.strip()
            ]
            if any(not 2 <= len(parts) <= 3 for parts in band_rows):
                raise ValueError(
                    'バンド行は中心周波数,吸収率,散乱率 （散乱率は省略可）です'
                )
            bands = tuple(
                GeometricAcousticBand(
                    center_hz=float(parts[0]),
                    absorption=float(parts[1]),
                    scattering=float(parts[2]) if len(parts) > 2 else 0.0,
                )
                for parts in band_rows
            )
            material = build_acoustic_material(
                label=str(values['label']),
                provenance=str(values['provenance']),
                wave_model=values['wave_model'],
                specific_impedance=impedance,
                geometric_model=values['geometric_model'],
                geometric_bands=bands,
            )
        except (ValueError, IndexError) as exc:
            self.readiness.setText(f'マテリアル登録失敗: {operation_error_message(exc)}')
            return
        self.controller.material_repository.save_material(material)
        self.refresh()
        index = self.materials.findData(material.material_id)
        if index >= 0:
            self.materials.setCurrentIndex(index)

    def _confirm_bulk_apply(
        self,
        preview,
        material: AcousticMaterialAuthority,
    ) -> str:
        """Overwrite confirmation for the bulk apply (#996).

        Returns 'all' (overwrite every boundary), 'unassigned' (keep
        existing rows, apply only where none exists), or 'cancel'.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('全部屋境界面へのマテリアル適用')
        box.setText(
            f'「{material.label}」を全 {preview.target_count} 面に適用します。\n'
            f'・既存の割当を上書き: {preview.overwrite_count}面\n'
            f'・未割当に新規適用: {preview.unassigned_count}面\n'
            f'・同一材質のため維持: {preview.already_assigned_count}面'
            + (
                f'\n・解決不能な参照を上書き: {preview.dangling_count}面'
                if preview.dangling_count
                else ''
            )
        )
        box.setInformativeText(
            'この適用は1回の書き込みで全対象に確定します（失敗時は0面）。'
        )
        apply_all = box.addButton(
            'すべてに適用（上書き）', QMessageBox.ButtonRole.AcceptRole
        )
        unassigned_only = (
            box.addButton(
                '未割当のみに適用', QMessageBox.ButtonRole.ActionRole
            )
            if preview.unassigned_count
            else None
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        exec_transient(box)
        clicked = box.clickedButton()
        if clicked is apply_all:
            return 'all'
        if unassigned_only is not None and clicked is unassigned_only:
            return 'unassigned'
        return 'cancel'

    def _apply_to_all_boundaries(self) -> None:
        material_id = self.materials.currentData()
        repository = self.controller.material_repository
        material = (
            None
            if material_id is None
            else repository.get_material(material_id)
        )
        if material is None:
            self.readiness.setText('先にマテリアルを選択してください')
            return
        document_id = self.controller.document_id
        boundary_ids = sorted(
            surface.surface_id
            for surface in self._surfaces
            if surface.semantic_class == 'room_boundary'
        )
        if not boundary_ids:
            self.readiness.setText(
                '部屋境界面がありません — 変更された面: 0'
            )
            return
        try:
            # #996: read-only preview — head pin + per-surface rows.
            preview = repository.preview_assign_material_bulk(
                document_id,
                boundary_ids,
                material,
                surfaces=self._surfaces,
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self.readiness.setText(
                '一括適用の事前確認に失敗 — 変更された面: 0 · '
                + operation_error_message(exc)
            )
            return
        only_unassigned = False
        if preview.overwrite_count or preview.dangling_count:
            choice = self._confirm_bulk_apply(preview, material)
            if choice == 'cancel':
                self.readiness.setText(
                    '一括適用をキャンセルしました — 変更された面: 0'
                )
                return
            only_unassigned = choice == 'unassigned'
        # Snapshot rows before the apply — the revert leg of the single
        # Undo step restores this exact prior state in one transaction.
        try:
            prior_rows = repository.assignment_rows_for(
                document_id, boundary_ids
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self.readiness.setText(
                '一括適用の事前確認に失敗 — 変更された面: 0 · '
                + operation_error_message(exc)
            )
            return
        committed: dict[str, object] = {}
        head_pin = preview.scene_revision_id

        def _apply_side() -> None:
            committed['result'] = repository.assign_material_bulk(
                document_id,
                boundary_ids,
                material,
                surfaces=self._surfaces,
                only_unassigned=only_unassigned,
                expected_scene_revision_id=head_pin,
            )

        def _revert_side() -> None:
            repository.restore_assignment_rows(document_id, prior_rows)

        command = CompositeEditCommand(
            inner=None,
            apply_side=_apply_side,
            revert_side=_revert_side,
            presentation=CommandPresentation(
                action='assign_material_bulk',
                label=f'全境界面へ「{material.label}」を適用',
            ),
        )
        try:
            # One push_command == one Undo step for the whole bulk apply.
            pushed = self.controller.working.push_command(command)
        except EXPECTED_OPERATION_ERRORS as exc:
            self.refresh()
            self.readiness.setText(
                '一括適用に失敗 — 変更された面: 0 · '
                + operation_error_message(exc)
            )
            return
        result = committed.get('result')
        self.refresh()
        if not pushed or result is None:
            self.readiness.setText(
                '適用対象がありませんでした — 変更された面: 0'
            )
            return
        self.readiness.setText(
            f'「{material.label}」を全境界面に適用しました — '
            f'適用 {len(result.applied_surface_ids)}面・'
            f'維持 {len(result.kept_surface_ids)}面・'
            f'既存割当を保持 {len(result.skipped_surface_ids)}面 '
            '（1操作で取り消し可能）'
        )


class RoomTreatmentPanel(QWidget):
    """AcousticTreatment authoring, placement and comparison UX (#451)."""

    def __init__(self, controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        # #1009: set by the workspace — returns the current
        # TreatmentOverlayScene. The panel never derives patches itself;
        # it renders the resolved vocabulary (提案/設置/失効・実効面積比・
        # 警告) verbatim.
        self.coverage_provider = None
        self._surfaces = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel('音響処理 (AcousticTreatment)')
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        header.setToolTip(
            '吸音パネル・バストラップ・拡散板などの定義と壁面への配置'
        )
        layout.addWidget(header)
        self.definitions = QTreeWidget()
        self.definitions.setHeaderLabels(('定義', '種類'))
        definitions_header = self.definitions.headerItem()
        if definitions_header is not None:
            definitions_header.setToolTip(0, '登録済みの吸音処理の定義')
            definitions_header.setToolTip(1, '構造種別')
        layout.addWidget(self.definitions)
        definition_row = QHBoxLayout()
        self.new_definition = QPushButton('定義を新規…')
        self.new_definition.setToolTip(
            '新しい吸音処理の定義（寸法・材質・種別）を作成します'
        )
        self.new_definition.clicked.connect(self._new_definition)
        definition_row.addWidget(self.new_definition)
        layout.addLayout(definition_row)

        place_header = QLabel('配置')
        set_typography_role(place_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(place_header)
        form = QFormLayout()
        self.place_definition = QComboBox()
        self.place_definition.setToolTip('配置する吸音処理の定義を選択')
        form.addRow('定義', self.place_definition)
        self.place_surface = QComboBox()
        self.place_surface.setToolTip('処理を取り付ける壁・天井・床の面')
        form.addRow('ホスト面', self.place_surface)
        self.place_width = QDoubleSpinBox()
        self.place_width.setRange(0.01, 20.0)
        self.place_width.setValue(0.6)
        self.place_width.setToolTip('その面で実際に覆う幅（m）')
        self.place_height = QDoubleSpinBox()
        self.place_height.setRange(0.01, 20.0)
        self.place_height.setValue(1.2)
        self.place_height.setToolTip('その面で実際に覆う高さ（m）')
        form.addRow('カバー幅 (m)', self.place_width)
        form.addRow('カバー高 (m)', self.place_height)
        for field in (
            self.place_definition, self.place_surface,
            self.place_width, self.place_height,
        ):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip())
        layout.addLayout(form)
        self.place_button = QPushButton('提案として配置')
        self.place_button.setToolTip(
            '選択した定義を面に「提案」状態で配置します（まだ設置済みではありません）'
        )
        self.place_button.clicked.connect(self._place)
        layout.addWidget(self.place_button)

        self.placements = QTreeWidget()
        self.placements.setAccessibleName('配置一覧')
        self.placements.setHeaderLabels(('配置', '状態'))
        placements_header = self.placements.headerItem()
        if placements_header is not None:
            placements_header.setToolTip(0, '配置した吸音処理')
            placements_header.setToolTip(1, '提案=まだ未確定 / 設置済み=確定して反映')
        layout.addWidget(self.placements, stretch=1)
        self.install_button = QPushButton('選択配置を設置済みにする')
        self.install_button.setToolTip(
            '選択した「提案」配置を「設置済み」に確定します'
        )
        self.install_button.clicked.connect(self._install_selected)
        layout.addWidget(self.install_button)

        coverage_header = QLabel('壁面被覆 (ビューポート表示)')
        set_typography_role(coverage_header, TypographyRole.SECTION_TITLE)
        coverage_header.setToolTip(
            '3Dビューに描かれる被覆パッチ — '
            'クリップ済みの実効範囲のみが着色されます'
        )
        layout.addWidget(coverage_header)
        self.coverage = QTreeWidget()
        self.coverage.setAccessibleName('被覆一覧')
        self.coverage.setHeaderLabels(('配置', '被覆'))
        coverage_tree_header = self.coverage.headerItem()
        if coverage_tree_header is not None:
            coverage_tree_header.setToolTip(
                0, '被覆パッチを持つ配置（または描けない配置）'
            )
            coverage_tree_header.setToolTip(
                1,
                '実効面積/矩形面積の比率・ライフサイクル・警告 — '
                '範囲外や失効した配置は描画されず理由が示されます',
            )
        self.coverage.setToolTip(
            '実効パッチ（ホスト面でクリップ済み）だけが壁面に描画されます。'
            '矩形のはみ出し部分は被覆として着色されません。'
        )
        layout.addWidget(self.coverage)

        compare_header = QLabel('比較')
        set_typography_role(compare_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(compare_header)
        self.compare_name = QLineEdit()
        self.compare_name.setPlaceholderText('比較名（例: バストラップA/B）')
        self.compare_name.setToolTip('A/B比較セットにつける名前')
        layout.addWidget(self.compare_name)
        self.compare_button = QPushButton('現在の配置で比較を記録')
        self.compare_button.setToolTip(
            '今の配置状態を名前つき比較の1候補として記録します（複数記録してA/B比較）'
        )
        self.compare_button.clicked.connect(self._record_comparison)
        layout.addWidget(self.compare_button)
        self.comparisons = QTreeWidget()
        self.comparisons.setAccessibleName('比較一覧')
        self.comparisons.setHeaderLabels(('比較', '候補'))
        comparisons_header = self.comparisons.headerItem()
        if comparisons_header is not None:
            comparisons_header.setToolTip(0, '記録した比較セット')
            comparisons_header.setToolTip(1, 'セット内の候補数')
        layout.addWidget(self.comparisons)
        # #1008: read-only 3D fabrication preview — issues a sealed
        # TreatmentFabricationPackage and draws its parts in the
        # viewport. fabrication_host is set by the workspace.
        self.fabrication_host = None
        fabrication_header = QLabel('製作プレビュー')
        set_typography_role(
            fabrication_header, TypographyRole.SECTION_TITLE
        )
        fabrication_header.setToolTip(
            '発行済み製作パッケージの3D断面/分解プレビュー（読み取り専用）'
        )
        layout.addWidget(fabrication_header)
        self.fabrication_button = QPushButton('製作プレビュー…')
        self.fabrication_button.setAccessibleName('製作プレビューを開く')
        self.fabrication_button.setToolTip(
            '吸音パネル/QRD拡散体の製作パッケージを発行し、'
            '断面・分解図・部材表を3Dで確認します'
        )
        self.fabrication_button.clicked.connect(
            self._open_fabrication_preview
        )
        layout.addWidget(self.fabrication_button)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        layout.addWidget(self.status)
        self.refresh()

    def _revision(self):
        return self.controller.repository.current_head(self.controller.document_id)

    def _open_fabrication_preview(self) -> None:
        """Open the #1008 fabrication preview dialog (non-modal)."""

        from ...room_fabrication_panel import FabricationPreviewDialog

        dialog = getattr(self, '_fabrication_dialog', None)
        if dialog is not None and not dialog.isHidden():
            dialog.raise_()
            dialog.activateWindow()
            dialog.refresh_sources()
            return
        dialog = FabricationPreviewDialog(
            self.controller,
            host=self.fabrication_host,
            parent=self.window(),
        )
        self._fabrication_dialog = dialog
        dialog.refresh_sources()
        dialog.show()

    def refresh(self) -> None:
        repository = self.controller.treatment_repository
        revision = self._revision()
        geometry = (
            None
            if revision is None or revision.document.r120_semantic_geometry is None
            else revision.document.r120_semantic_geometry
        )
        self._surfaces = () if geometry is None else geometry.surfaces

        definitions = repository.list_definitions()
        self.definitions.clear()
        self.place_definition.clear()
        for definition in definitions:
            type_label = _TREATMENT_TYPE_LABELS.get(
                definition.treatment_type, definition.treatment_type
            )
            item = QTreeWidgetItem(
                (
                    f'{definition.name} v{definition.version}',
                    type_label,
                )
            )
            item.setData(
                0,
                _DEFINITION_ROLE,
                (definition.definition_id, definition.version),
            )
            self.definitions.addTopLevelItem(item)
            self.place_definition.addItem(
                f'{definition.name} v{definition.version}',
                (definition.definition_id, definition.version),
            )

        self.place_surface.clear()
        for surface in self._surfaces:
            class_label = SURFACE_CLASS_LABELS.get(
                surface.semantic_class, surface.semantic_class
            )
            self.place_surface.addItem(
                f'{surface.surface_key} · {class_label}',
                surface.surface_id,
            )

        self.placements.clear()
        placements = (
            ()
            if revision is None
            else repository.list_placements_for_scene(revision.revision_id)
        )
        for placement in placements:
            evaluation = repository.evaluate_placement_surface_binding(placement)
            state = _BINDING_STATE_LABELS.get(
                evaluation.binding_state, evaluation.binding_state
            )
            definition = repository.get_definition(
                placement.definition_id, placement.definition_version
            )
            name = (
                placement.instance_id
                if definition is None
                else f'{definition.name}'
            )
            item = QTreeWidgetItem(
                (
                    f'{name} v{placement.placement_version}',
                    f'{placement.lifecycle} · {state}',
                )
            )
            item.setData(
                0, _PLACEMENT_ROLE,
                (placement.instance_id, placement.placement_version),
            )
            self.placements.addTopLevelItem(item)

        self.comparisons.clear()
        # #917: comparisons are project-owned — never list a foreign
        # project's specs inside this Room surface.
        for spec in self.controller.treatment_comparison_repository.list_for_document(
            self.controller.document_id
        ):
            roles = ' / '.join(
                candidate.role for candidate in spec.candidates
            )
            self.comparisons.addTopLevelItem(
                QTreeWidgetItem((spec.name, roles))
            )
        self._refresh_coverage()
        if not self._surfaces:
            self.status.setText(
                'セマンティックジオメトリがありません — 面バインドは未対応です。'
            )

    def _refresh_coverage(self) -> None:
        """Populate the 被覆 list from the resolved overlay scene (#1009)."""

        self.coverage.clear()
        provider = self.coverage_provider
        if not callable(provider):
            return
        scene = provider()
        if scene is None:
            self.coverage.addTopLevelItem(
                QTreeWidgetItem(('', 'シーンリビジョンなし — 描画なし'))
            )
            return
        for patch in scene.patches:
            lifecycle = LIFECYCLE_LABELS.get(patch.lifecycle, patch.lifecycle)
            notes = []
            if patch.clipped:
                notes.append('クリップ済み')
            if patch.overlap_with:
                notes.append('重複: ' + ' / '.join(patch.overlap_with))
            item = QTreeWidgetItem(
                (
                    patch.placement.instance_id,
                    (
                        f'{lifecycle} · 実効 {patch.patch_area_m2:.2f} m² '
                        f'/ 矩形 {patch.rectangle_area_m2:.2f} m² '
                        f'({patch.effective_ratio:.0%})'
                        + (f' · {"; ".join(notes)}' if notes else '')
                    ),
                )
            )
            item.setToolTip(
                1,
                '実効面積はホスト面でクリップされた範囲のみ — '
                'はみ出した矩形部分は被覆として描画されません。',
            )
            self.coverage.addTopLevelItem(item)
        for notice in scene.notices:
            item = QTreeWidgetItem((notice.instance_id, notice.message))
            item.setToolTip(1, 'この配置は壁面に描画されません — 理由を確認してください。')
            self.coverage.addTopLevelItem(item)
        if not scene.patches and not scene.notices:
            self.coverage.addTopLevelItem(
                QTreeWidgetItem(('', '描画対象の配置がありません'))
            )

    def _new_definition(self) -> None:
        dialog = TreatmentDefinitionDialog(self)
        if exec_transient(dialog) != TreatmentDefinitionDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        repository = self.controller.treatment_repository
        try:
            dimensions = TreatmentDimensions(
                width_m=values['width_m'],
                height_m=values['height_m'],
                thickness_m=values['thickness_m'],
            )
            layers = (
                TreatmentLayer(
                    layer_id='layer-1',
                    material_name=values['layer_material'],
                    thickness_m=values['thickness_m'],
                    density_kg_m3=values['layer_density'],
                ),
            )
            parameters = TreatmentPhysicalParameters(
                bulk_density_kg_m3=values['layer_density'],
            )
            definition_id = f'treatment-{uuid4().hex[:12]}'
            source_kind = values['source_kind']
            if source_kind == 'user_defined':
                source_sha256 = None
                source_id = f'{definition_id}@ux'
                source_version = str(values['version'])
                reference = 'Room UX からユーザー入力'
            else:
                # Retain the picked source bytes as a managed asset first;
                # the evidence then claims the exact content hash.
                source_sha256 = repository.save_source_asset(
                    filename=values['source_file_name'],
                    data=values['source_bytes'],
                )
                source_id = values['source_name']
                source_version = str(values['source_version'])
                reference = (
                    values['source_reference']
                    or values['source_file_name']
                )
            evidence = build_treatment_evidence_authority(
                source_kind=source_kind,
                source_id=source_id,
                source_version=source_version,
                source_sha256=source_sha256,
                reference=reference,
                extraction_id='room-ux-declaration',
                extraction_version='1',
                subject=TreatmentEvidenceSubject(
                    definition_id=definition_id,
                    definition_version=str(values['version']),
                    treatment_type=values['treatment_type'],
                    dimensions=dimensions,
                    air_gap_m=values['air_gap_m'],
                    layers=layers,
                    parameters=parameters,
                ),
            )
            definition = build_acoustic_treatment_definition(
                definition_id=definition_id,
                version=str(values['version']),
                name=str(values['name']),
                treatment_type=values['treatment_type'],
                provenance=evidence.as_provenance(),
                dimensions=dimensions,
                air_gap_m=values['air_gap_m'],
                layers=layers,
                parameters=parameters,
            )
            repository.save_evidence(evidence)
            repository.save_definition(definition)
        except ValueError as exc:
            self.status.setText(f'定義作成失敗: {operation_error_message(exc)}')
            return
        self.refresh()
        self.status.setText(f"定義 '{values['name']}' を作成しました")

    def _place(self) -> None:
        revision = self._revision()
        if revision is None:
            return
        key = self.place_definition.currentData()
        surface_id = self.place_surface.currentData()
        if key is None:
            self.status.setText('先に定義を作成してください')
            return
        repository = self.controller.treatment_repository
        definition = repository.get_definition(key[0], key[1])
        if definition is None:
            self.status.setText('定義が解決できません')
            return
        try:
            placement = build_treatment_placement(
                definition=definition,
                revision=revision,
                instance_id=f'treatment-{uuid4().hex[:12]}',
                position=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
                coverage=TreatmentCoverage(
                    width_m=float(self.place_width.value()),
                    height_m=float(self.place_height.value()),
                ),
                host_surface_id=surface_id,
            )
            repository.save_placement(placement)
        except ValueError as exc:
            self.status.setText(f'配置失敗: {operation_error_message(exc)}')
            return
        self.refresh()
        self.status.setText('提案として配置しました')

    def _install_selected(self) -> None:
        item = self.placements.currentItem()
        revision = self._revision()
        if item is None or revision is None:
            return
        instance_id, _version = item.data(0, _PLACEMENT_ROLE)
        repository = self.controller.treatment_repository
        previous = repository.latest_placement(instance_id)
        if previous is None:
            return
        try:
            updated = revise_treatment_placement(
                previous,
                revision=revision,
                lifecycle='installed',
            )
            repository.save_placement(updated)
        except ValueError as exc:
            self.status.setText(f'install失敗: {operation_error_message(exc)}')
            return
        self.refresh()
        self.status.setText('配置を設置済みにしました')

    def _record_comparison(self) -> None:
        revision = self._revision()
        if revision is None:
            return
        name = self.compare_name.text().strip()
        if not name:
            self.status.setText('比較名を入力してください')
            return
        repository = self.controller.treatment_repository
        placements = repository.list_placements_for_scene(revision.revision_id)
        try:
            baseline = build_treatment_design_candidate(
                baseline=revision,
                label='無処理 (ベースライン)',
                role='no_treatment',
            )
            candidates = [baseline]
            if placements:
                candidates.append(
                    build_treatment_design_candidate(
                        baseline=revision,
                        label='現在の配置',
                        role='treatment',
                        placements=placements,
                    )
                )
            spec = build_treatment_design_comparison(
                name=name,
                baseline=revision,
                candidates=candidates,
            )
            self.controller.treatment_comparison_repository.save(spec)
        except ValueError as exc:
            self.status.setText(f'比較記録失敗: {operation_error_message(exc)}')
            return
        self.refresh()
        self.status.setText(f"比較 '{name}' を記録しました")


class RoomAcousticsTabs(QTabWidget):
    """Acoustics-context tab container: prediction + materials + treatments
    + reflection guidance."""

    def __init__(
        self,
        prediction_panel: QWidget,
        material_panel: QWidget,
        treatment_panel: QWidget,
        guidance_panel: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAccessibleName('音響コンテキスト')
        panels = [
            (prediction_panel, '予測'),
            (material_panel, 'マテリアル'),
            (treatment_panel, '音響処理'),
        ]
        if guidance_panel is not None:
            panels.append((guidance_panel, 'ガイダンス'))
        self._panels = tuple(panel for panel, _ in panels)
        for panel, label in panels:
            self.addTab(self._scroll_page(panel), label)

    @staticmethod
    def _scroll_page(panel: QWidget) -> QScrollArea:
        """Each tab page scrolls inside the narrow dock; the tab bar stays pinned."""

        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        page.setWidget(panel)
        return page

    def refresh(self) -> None:
        for panel in self._panels:
            refresh = getattr(panel, 'refresh', None)
            if callable(refresh):
                refresh()


__all__ = [
    'MaterialDialog',
    'RoomAcousticsTabs',
    'RoomTreatmentPanel',
    'SurfaceMaterialPanel',
    'TreatmentDefinitionDialog',
]
