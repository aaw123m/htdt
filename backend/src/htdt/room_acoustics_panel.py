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
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .acoustic_benchmark import GeometricAcousticBand, SpecificImpedancePoint
from .cad_acoustic_material import (
    GEOMETRIC_MODEL_LABELS,
    SURFACE_CLASS_LABELS,
    WAVE_MODEL_LABELS,
    AcousticMaterialAuthority,
    build_acoustic_material,
    material_capability_label,
)
from .cad_acoustic_treatment import (
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
from .cad_acoustic_treatment_comparison import (
    build_treatment_design_candidate,
    build_treatment_design_comparison,
)
from .cad_scene import Position3
from .ui_theme import TypographyRole, set_typography_role

_SURFACE_ROLE = Qt.ItemDataRole.UserRole
_MATERIAL_ROLE = Qt.ItemDataRole.UserRole
_DEFINITION_ROLE = Qt.ItemDataRole.UserRole
_PLACEMENT_ROLE = Qt.ItemDataRole.UserRole

_TREATMENT_TYPE_LABELS: dict[str, str] = {
    'porous_absorber': '多孔質吸音材',
    'absorber_with_air_gap': '空気層付き吸音材',
    'membrane_panel_absorber': '膜パネル吸音材',
    'perforated_slotted_absorber': '穿孔/スリット吸音材',
    'bass_trap': 'バストラップ',
    'diffuser_scattering_element': '拡散要素',
    'hybrid': 'ハイブリッド',
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
    'stale_scene_revision': 'STALE (リビジョン更新)',
    'stale_semantic_geometry': 'STALE (ジオメトリ更新)',
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
        form.addRow('名称', self.label)
        self.wave_model = QComboBox()
        for value, label_text in WAVE_MODEL_LABELS.items():
            self.wave_model.addItem(f'{value} — {label_text}', value)
        self.wave_model.setCurrentIndex(
            list(WAVE_MODEL_LABELS).index('unsupported')
        )
        form.addRow('wave capability', self.wave_model)
        self.impedance = QPlainTextEdit()
        self.impedance.setPlaceholderText(
            '比インピーダンス点 — 1行1点: freq_hz,resistance,reactance '
            '(specific_impedance_tableのみ)'
        )
        self.impedance.setMaximumHeight(60)
        form.addRow('インピーダンス', self.impedance)
        self.geometric_model = QComboBox()
        for value, label_text in GEOMETRIC_MODEL_LABELS.items():
            self.geometric_model.addItem(f'{value} — {label_text}', value)
        self.geometric_model.setCurrentIndex(
            list(GEOMETRIC_MODEL_LABELS).index('unsupported')
        )
        form.addRow('geometric capability', self.geometric_model)
        self.bands = QPlainTextEdit()
        self.bands.setPlaceholderText(
            'バンド — 1行1点: center_hz,absorption,scattering (bandedのみ)'
        )
        self.bands.setMaximumHeight(60)
        form.addRow('バンド', self.bands)
        self.provenance = QLineEdit()
        self.provenance.setPlaceholderText('出典（例: メーカー公表値 / 現場実測）')
        form.addRow('出典', self.provenance)
        layout.addLayout(form)
        hint = QLabel(
            'waveとgeometricのcapabilityは別々の権威です — '
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
        if not self.label.text().strip() or not self.provenance.text().strip():
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
        form.addRow('名称', self.name)
        self.version = QLineEdit('1')
        form.addRow('バージョン', self.version)
        self.treatment_type = QComboBox()
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
        self.width = QDoubleSpinBox()
        self.width.setRange(0.01, 20.0)
        self.width.setValue(0.6)
        self.height = QDoubleSpinBox()
        self.height.setRange(0.01, 20.0)
        self.height.setValue(1.2)
        self.thickness = QDoubleSpinBox()
        self.thickness.setRange(0.001, 2.0)
        self.thickness.setValue(0.05)
        self.thickness.setDecimals(3)
        self.air_gap = QDoubleSpinBox()
        self.air_gap.setRange(0.0, 2.0)
        self.air_gap.setDecimals(3)
        form.addRow('幅 (m)', self.width)
        form.addRow('高さ (m)', self.height)
        form.addRow('厚さ (m)', self.thickness)
        form.addRow('空気層 (m)', self.air_gap)
        self.layer_material = QLineEdit()
        self.layer_material.setPlaceholderText('層1 材質名 (例: グラスウール)')
        form.addRow('層1 材質', self.layer_material)
        self.layer_density = QDoubleSpinBox()
        self.layer_density.setRange(0.0, 2000.0)
        self.layer_density.setSpecialValueText('不明')
        form.addRow('層1 密度 kg/m³', self.layer_density)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if (
            not self.name.text().strip()
            or not self.layer_material.text().strip()
        ):
            return
        super().accept()

    def values(self) -> dict[str, Any]:
        return {
            'name': self.name.text().strip(),
            'version': self.version.text().strip() or '1',
            'treatment_type': self.treatment_type.currentData(),
            'width_m': float(self.width.value()),
            'height_m': float(self.height.value()),
            'thickness_m': float(self.thickness.value()),
            'air_gap_m': float(self.air_gap.value()),
            'layer_material': self.layer_material.text().strip(),
            'layer_density': (
                None
                if self.layer_density.value() <= 0.0
                else float(self.layer_density.value())
            ),
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
        layout.addWidget(header)
        material_row = QHBoxLayout()
        self.materials = QComboBox()
        self.materials.currentIndexChanged.connect(self._material_selected)
        material_row.addWidget(self.materials, stretch=1)
        self.material_new = QPushButton('新規…')
        self.material_new.clicked.connect(self._new_material)
        material_row.addWidget(self.material_new)
        self.material_apply_all = QPushButton('全部屋境界面へ適用')
        self.material_apply_all.clicked.connect(self._apply_to_all_boundaries)
        material_row.addWidget(self.material_apply_all)
        layout.addLayout(material_row)
        self.material_detail = QLabel('')
        self.material_detail.setWordWrap(True)
        set_typography_role(self.material_detail, TypographyRole.SECONDARY)
        layout.addWidget(self.material_detail)

        surfaces_header = QLabel('面ごとの割当')
        set_typography_role(surfaces_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(surfaces_header)
        self.surface_tree = QTreeWidget()
        self.surface_tree.setHeaderLabels(('面', 'マテリアル'))
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
        assignments = self.controller.material_repository.assignments_for_document(
            self.controller.document_id
        )
        self.surface_tree.blockSignals(True)
        self.surface_tree.clear()
        self._surfaces = () if geometry is None else geometry.surfaces
        for surface in self._surfaces:
            class_label = SURFACE_CLASS_LABELS.get(
                surface.semantic_class, surface.semantic_class
            )
            item = QTreeWidgetItem(
                (f'{surface.surface_key} · {class_label}', '')
            )
            item.setData(0, _SURFACE_ROLE, surface.surface_id)
            combo = QComboBox()
            combo.addItem('未割当 (UNKNOWN)', None)
            for index in range(self.materials.count()):
                combo.addItem(
                    self.materials.itemText(index),
                    self.materials.itemData(index),
                )
            material = assignments.get(surface.surface_id)
            combo.setCurrentIndex(
                0
                if material is None
                else max(0, combo.findData(material.material_id))
            )
            combo.activated.connect(
                lambda _i, sid=surface.surface_id, c=combo:
                self._surface_combo_changed(sid, c)
            )
            self.surface_tree.addTopLevelItem(item)
            self.surface_tree.setItemWidget(item, 1, combo)
        self.surface_tree.blockSignals(False)
        unassigned = sum(
            1
            for surface in self._surfaces
            if surface.surface_id not in assignments
        )
        if self._surfaces:
            self.readiness.setText(
                f'{len(self._surfaces)}面中 {unassigned}面が未割当 — '
                '未割当面はR120で material_missing として報告されます。'
            )
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
        repository = self.controller.material_repository
        if material_id is None:
            repository.clear_assignment(
                self.controller.document_id, surface_id
            )
        else:
            material = repository.get_material(material_id)
            if material is not None:
                repository.assign_material(
                    self.controller.document_id, surface_id, material
                )
        self.refresh()

    def _new_material(self) -> None:
        dialog = MaterialDialog(self)
        if dialog.exec() != MaterialDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            impedance = tuple(
                SpecificImpedancePoint(
                    frequency_hz=float(parts[0]),
                    resistance_pa_s_m=float(parts[1]),
                    reactance_pa_s_m=float(parts[2]),
                )
                for parts in (
                    [part.strip() for part in line.split(',')]
                    for line in str(values['impedance_text']).splitlines()
                    if line.strip()
                )
            )
            bands = tuple(
                GeometricAcousticBand(
                    center_hz=float(parts[0]),
                    absorption=float(parts[1]),
                    scattering=float(parts[2]) if len(parts) > 2 else 0.0,
                )
                for parts in (
                    [part.strip() for part in line.split(',')]
                    for line in str(values['bands_text']).splitlines()
                    if line.strip()
                )
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
            self.readiness.setText(f'マテリアル登録失敗: {exc}')
            return
        self.controller.material_repository.save_material(material)
        self.refresh()
        index = self.materials.findData(material.material_id)
        if index >= 0:
            self.materials.setCurrentIndex(index)

    def _apply_to_all_boundaries(self) -> None:
        material_id = self.materials.currentData()
        material = (
            None
            if material_id is None
            else self.controller.material_repository.get_material(material_id)
        )
        if material is None:
            self.readiness.setText('先にマテリアルを選択してください')
            return
        boundaries = [
            surface
            for surface in self._surfaces
            if surface.semantic_class == 'room_boundary'
        ]
        for surface in boundaries:
            self.controller.material_repository.assign_material(
                self.controller.document_id,
                surface.surface_id,
                material,
            )
        self.refresh()


class RoomTreatmentPanel(QWidget):
    """AcousticTreatment authoring, placement and comparison UX (#451)."""

    def __init__(self, controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._surfaces = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel('音響処理 (AcousticTreatment)')
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        layout.addWidget(header)
        self.definitions = QTreeWidget()
        self.definitions.setHeaderLabels(('定義', '種類'))
        layout.addWidget(self.definitions)
        definition_row = QHBoxLayout()
        self.new_definition = QPushButton('定義を新規…')
        self.new_definition.clicked.connect(self._new_definition)
        definition_row.addWidget(self.new_definition)
        layout.addLayout(definition_row)

        place_header = QLabel('配置')
        set_typography_role(place_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(place_header)
        form = QFormLayout()
        self.place_definition = QComboBox()
        form.addRow('定義', self.place_definition)
        self.place_surface = QComboBox()
        form.addRow('ホスト面', self.place_surface)
        self.place_width = QDoubleSpinBox()
        self.place_width.setRange(0.01, 20.0)
        self.place_width.setValue(0.6)
        self.place_height = QDoubleSpinBox()
        self.place_height.setRange(0.01, 20.0)
        self.place_height.setValue(1.2)
        form.addRow('カバー幅 (m)', self.place_width)
        form.addRow('カバー高 (m)', self.place_height)
        layout.addLayout(form)
        self.place_button = QPushButton('proposedとして配置')
        self.place_button.clicked.connect(self._place)
        layout.addWidget(self.place_button)

        self.placements = QTreeWidget()
        self.placements.setHeaderLabels(('配置', '状態'))
        layout.addWidget(self.placements, stretch=1)
        self.install_button = QPushButton('選択配置を installed にする')
        self.install_button.clicked.connect(self._install_selected)
        layout.addWidget(self.install_button)

        compare_header = QLabel('比較')
        set_typography_role(compare_header, TypographyRole.SECTION_TITLE)
        layout.addWidget(compare_header)
        self.compare_name = QLineEdit()
        self.compare_name.setPlaceholderText('比較名（例: バストラップA/B）')
        layout.addWidget(self.compare_name)
        self.compare_button = QPushButton('現在の配置で比較を記録')
        self.compare_button.clicked.connect(self._record_comparison)
        layout.addWidget(self.compare_button)
        self.comparisons = QTreeWidget()
        self.comparisons.setHeaderLabels(('比較', '候補'))
        layout.addWidget(self.comparisons)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        layout.addWidget(self.status)
        self.refresh()

    def _revision(self):
        return self.controller.repository.current_head(self.controller.document_id)

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
        for spec in self.controller.treatment_comparison_repository.list_all():
            roles = ' / '.join(
                candidate.role for candidate in spec.candidates
            )
            self.comparisons.addTopLevelItem(
                QTreeWidgetItem((spec.name, roles))
            )
        if not self._surfaces:
            self.status.setText(
                'セマンティックジオメトリがありません — 面バインドは未対応です。'
            )

    def _new_definition(self) -> None:
        dialog = TreatmentDefinitionDialog(self)
        if dialog.exec() != TreatmentDefinitionDialog.DialogCode.Accepted:
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
            evidence = build_treatment_evidence_authority(
                source_kind='user_defined',
                source_id=f'{definition_id}@ux',
                source_version=str(values['version']),
                reference='Room UX からユーザー入力',
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
            self.status.setText(f'定義作成失敗: {exc}')
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
            self.status.setText(f'配置失敗: {exc}')
            return
        self.refresh()
        self.status.setText('proposed として配置しました')

    def _install_selected(self) -> None:
        item = self.placements.currentItem()
        revision = self._revision()
        if item is None or revision is None:
            return
        instance_id, _version = item.data(0, _PLACEMENT_ROLE)
        repository = self.controller.treatment_repository
        previous = repository.latest_placement(revision.document_id, instance_id)
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
            self.status.setText(f'install失敗: {exc}')
            return
        self.refresh()
        self.status.setText('配置を installed にしました')

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
                label='無処理 (baseline)',
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
            self.status.setText(f'比較記録失敗: {exc}')
            return
        self.refresh()
        self.status.setText(f"比較 '{name}' を記録しました")


class RoomAcousticsTabs(QTabWidget):
    """Acoustics-context tab container: prediction + materials + treatments."""

    def __init__(
        self,
        prediction_panel: QWidget,
        material_panel: QWidget,
        treatment_panel: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._panels = (prediction_panel, material_panel, treatment_panel)
        self.addTab(prediction_panel, '予測')
        self.addTab(material_panel, 'マテリアル')
        self.addTab(treatment_panel, '音響処理')

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
