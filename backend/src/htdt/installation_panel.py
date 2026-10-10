"""Installation authority registration surfaces (REV44-INSTALL).

Hosts the two record forms on the room workspace's placement page — the
same context that already owns equipment assignment:

- スピーカー設置コンテキスト: per-speaker ``SpeakerInstallationContext``
  persisted append-only via ``CadInstallationContextRepository.save_context``
  (equipment pick restricted to persisted EquipmentDefinitions, mounting
  combo restricted to the definition's declared modes, host pick of walls
  or parts, measured clearances + unique-axis overrides, directivity
  applicability with a provenance-carried rationale when non-default).
  Persisted contexts re-evaluate via ``evaluate_for_entity`` so the saved
  authority is shown judged against the current head.
- 設置基準（データム）: scene-level ``InstallationDatum`` persisted via
  ``CadInstallationDatumRepository.save_datum``, pinned to an explicit
  SceneRevision so the handoff export's 設置基準 section resolves.
"""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import revision_display_label
from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
)
from .cad_equipment import EquipmentDataProvenance, EquipmentDefinition
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_context import (
    BaffleState,
    ClearanceAxis,
    ClearanceOverride,
    DirectivityApplicability,
    InstallationEvaluation,
    MeasuredClearances,
    MountingMode,
    SpeakerInstallationContext,
    build_installation_context,
    evaluate_installation_context,
)
from .cad_installation_context_repository import CadInstallationContextRepository
from .cad_installation_datum import (
    DatumFrameSemantics,
    DatumReferencePoint,
    build_installation_datum,
    evaluate_datum_freshness,
)
from .cad_installation_datum_repository import CadInstallationDatumRepository
from .cad_repository import SceneRepository
from .cad_scene import room_vertices
from .clock import utc_now_iso
from .length_spinbox import MetricSpinBox
from .user_facing_error import operation_error_message
from .ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


_MOUNTING_LABELS: dict[MountingMode, str] = {
    'free_standing': '自立（床置き）',
    'stand': 'スタンド',
    'shelf': '棚置き',
    'wall': '壁掛け',
    'ceiling': '天井吊り',
    'in_wall': '壁埋込み',
    'in_ceiling': '天井埋込み',
    'custom': '独自',
    'unknown': '未記録',
}

_BAFFLE_LABELS: dict[BaffleState, str] = {
    'unknown': '未記録',
    'free_space': '自由空間（バッフルなし）',
    'flush_baffle': '平面バッフル（壁面と同一面）',
    'finite_baffle': '有限バッフル',
    'boundary_adjacent': '境界面近接',
}

_DIRECTIVITY_LABELS: dict[DirectivityApplicability, str] = {
    'unknown': '未記録',
    'anechoic': '無響室条件',
    'iec_baffle': 'IEC規格バッフル',
    'in_wall': '壁埋込み条件',
    'manufacturer_fixture': 'メーカー治具条件',
}

_AXIS_LABELS: dict[ClearanceAxis, str] = {
    'front': '前',
    'rear': '後',
    'side': '側面',
    'top': '上',
    'bottom': '下',
    'port': 'ポート',
}

_MEASURED_FIELDS: tuple[tuple[str, str], ...] = (
    ('front_m', 'front'),
    ('rear_m', 'rear'),
    ('side_m', 'side'),
    ('top_m', 'top'),
    ('bottom_m', 'bottom'),
    ('port_to_boundary_m', 'port'),
)

_CHECK_STATE_LABELS: dict[str, str] = {
    'PASS': '合格',
    'FAIL': '不合格',
    'UNKNOWN': '不明',
}

_CONDITION_LABELS: dict[str, str] = {
    'free_standing': '自立',
    'boundary_adjacent': '境界面近接',
    'flush_in_wall': '壁面埋込み',
    'half_space_baffle': '半空間バッフル',
    'manufacturer_fixture': 'メーカー治具',
    'unknown': '不明',
}

_COMPATIBILITY_LABELS: dict[str, str] = {
    'exact_match': '一致',
    'compatible_by_declared_model': '宣言モデルで互換',
    'bounded_approximation': '限界近似',
    'incompatible': '非互換',
    'unknown': '不明',
}

_FRESHNESS_LABELS: dict[str, str] = {
    'current': '最新',
    'stale': '古い（場面が編集済み）',
    'missing': '参照先が欠落',
}


def _fill_combo(
    combo: QComboBox,
    items: tuple[tuple[str, str | None], ...],
    *,
    keep_data: object = None,
) -> None:
    combo.clear()
    for label, data in items:
        combo.addItem(label, data)
    if keep_data is not None:
        index = combo.findData(keep_data)
        if index >= 0:
            combo.setCurrentIndex(index)


def _styled_combo() -> QComboBox:
    combo = QComboBox()
    combo.setMinimumContentsLength(12)
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    return combo


class _OverrideRow(QWidget):
    """One clearance-override editor row (axis + required + rationale)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.axis_combo = _styled_combo()
        for axis, label in _AXIS_LABELS.items():
            self.axis_combo.addItem(label, axis)
        # Runtime-built row — keep every field shrinkable like the ctor-time
        # widgets in this narrow column (min 72px floor).
        self.axis_combo.setMinimumWidth(72)
        self.axis_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            self.axis_combo.sizePolicy().verticalPolicy(),
        )
        self.required_spin = MetricSpinBox(minimum_m=0.0, maximum_m=20.0)
        self.required_spin.setAccessibleName('必要クリアランス')
        self.required_spin.setMinimumWidth(72)
        self.required_spin.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            self.required_spin.sizePolicy().verticalPolicy(),
        )
        self.rationale_edit = QLineEdit()
        self.rationale_edit.setPlaceholderText('上書きの根拠（必須）')
        self.rationale_edit.setMinimumWidth(72)
        self.rationale_edit.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            self.rationale_edit.sizePolicy().verticalPolicy(),
        )
        layout.addWidget(self.axis_combo)
        layout.addWidget(self.required_spin)
        layout.addWidget(self.rationale_edit, 1)


class InstallationPanel(QFrame):
    """設置権威の登録パネル — 配置コンテキストの末尾にマウントされる。"""

    contextSaved = Signal()
    datumSaved = Signal()
    #: #990: scene→Library deep link — emits the selected definition_id.
    libraryRequested = Signal(str)

    def __init__(
        self,
        scene_repository: SceneRepository,
        equipment_repository: CadEquipmentRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.equipment_repository = equipment_repository
        self.document_id = document_id
        self.context_repository = CadInstallationContextRepository(
            scene_repository, equipment_repository
        )
        self.datum_repository = CadInstallationDatumRepository(scene_repository)
        self._definitions: dict[str, EquipmentDefinition] = {}
        self._length_policy = display_length_policy('m')
        self._syncing = False
        self._build_ui()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        set_surface_role(self, SurfaceRole.RAISED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel('設置の記録')
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        note = QLabel(
            'スピーカーの設置条件と部屋の設置基準を権威として記録します。'
            '記録は追記専用で、保存後に書き換えることはできません。'
        )
        set_typography_role(note, TypographyRole.SECONDARY)
        note.setWordWrap(True)
        layout.addWidget(note)

        self._build_context_section(layout)
        self._build_datum_section(layout)

    def _build_context_section(self, layout: QVBoxLayout) -> None:
        section_title = QLabel('スピーカー設置コンテキスト')
        set_typography_role(section_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(section_title)

        form = QFormLayout()
        form.setSpacing(6)

        self.speaker_combo = _styled_combo()
        self.speaker_combo.currentIndexChanged.connect(
            self._speaker_changed
        )
        form.addRow('対象スピーカー', self.speaker_combo)

        self.equipment_combo = _styled_combo()
        self.equipment_combo.currentIndexChanged.connect(
            self._equipment_changed
        )
        form.addRow('機材定義', self.equipment_combo)

        library_row = QWidget()
        library_row_layout = QHBoxLayout(library_row)
        library_row_layout.setContentsMargins(0, 0, 0, 0)
        library_row_layout.setSpacing(4)
        self.open_library_button = QPushButton('ライブラリで確認')
        set_control_size(self.open_library_button, ControlSize.COMPACT)
        self.open_library_button.setAccessibleName(
            '選択した機材定義をライブラリで開く'
        )
        self.open_library_button.setToolTip(
            '選択中の機材定義を参照ライブラリで開きます'
            '（参照のみ・紐付けは変更しません）。'
        )
        self.open_library_button.setEnabled(False)
        self.open_library_button.clicked.connect(self._open_in_library)
        library_row_layout.addWidget(self.open_library_button)
        library_row_layout.addStretch(1)
        form.addRow('', library_row)

        self.mounting_combo = _styled_combo()
        form.addRow('設置方式', self.mounting_combo)
        self.mounting_hint = QLabel('')
        set_typography_role(self.mounting_hint, TypographyRole.SECONDARY)
        self.mounting_hint.setWordWrap(True)
        form.addRow('', self.mounting_hint)

        self.host_combo = _styled_combo()
        form.addRow('取付先（壁・ほかの機材）', self.host_combo)

        self.host_surface_edit = QLineEdit()
        self.host_surface_edit.setPlaceholderText('取付面の目印（任意）')
        form.addRow('取付面ラベル', self.host_surface_edit)

        self.baffle_combo = _styled_combo()
        for state, label in _BAFFLE_LABELS.items():
            self.baffle_combo.addItem(label, state)
        form.addRow('バッフル状態', self.baffle_combo)

        clearances_widget = QWidget()
        clearances_grid = QGridLayout(clearances_widget)
        clearances_grid.setContentsMargins(0, 0, 0, 0)
        clearances_grid.setSpacing(4)
        self.clearance_checks: dict[str, QCheckBox] = {}
        self.clearance_spins: dict[str, MetricSpinBox] = {}
        for index, (field, axis) in enumerate(_MEASURED_FIELDS):
            check = QCheckBox(_AXIS_LABELS[axis])
            spin = MetricSpinBox(minimum_m=0.0, maximum_m=20.0)
            spin.setAccessibleName(f'実測クリアランス {_AXIS_LABELS[axis]}')
            spin.setEnabled(False)
            check.toggled.connect(spin.setEnabled)
            self.clearance_checks[field] = check
            self.clearance_spins[field] = spin
            # One column: a 2×N grid of (check + mm spin) pairs needs ~450px,
            # overflowing the narrow placement column.
            pair = QWidget()
            pair_layout = QHBoxLayout(pair)
            pair_layout.setContentsMargins(0, 0, 0, 0)
            pair_layout.setSpacing(4)
            pair_layout.addWidget(check)
            pair_layout.addWidget(spin, 1)
            clearances_grid.addWidget(pair, index, 0)
        form.addRow('実測クリアランス', clearances_widget)

        overrides_widget = QWidget()
        overrides_layout = QVBoxLayout(overrides_widget)
        overrides_layout.setContentsMargins(0, 0, 0, 0)
        overrides_layout.setSpacing(4)
        self.override_rows = QVBoxLayout()
        overrides_layout.addLayout(self.override_rows)
        override_buttons = QHBoxLayout()
        self.add_override_button = QPushButton('上書きを追加')
        set_control_size(self.add_override_button, ControlSize.COMPACT)
        self.add_override_button.clicked.connect(self._add_override_row)
        override_buttons.addWidget(self.add_override_button)
        override_buttons.addStretch(1)
        overrides_layout.addLayout(override_buttons)
        form.addRow('クリアランス上書き', overrides_widget)

        self.directivity_combo = _styled_combo()
        for state, label in _DIRECTIVITY_LABELS.items():
            self.directivity_combo.addItem(label, state)
        self.directivity_combo.currentIndexChanged.connect(
            self._directivity_changed
        )
        form.addRow('指向性の適用区分', self.directivity_combo)

        self.directivity_rationale_edit = QLineEdit()
        self.directivity_rationale_edit.setPlaceholderText(
            '標準区分以外を選ぶ場合は根拠が必須です'
        )
        self.directivity_rationale_edit.setEnabled(False)
        form.addRow('適用区分の根拠', self.directivity_rationale_edit)

        self.actor_edit = QLineEdit()
        self.actor_edit.setPlaceholderText('記録者名（必須）')
        form.addRow('記録者', self.actor_edit)

        self.evidence_edit = QLineEdit()
        self.evidence_edit.setPlaceholderText('証跡・計測メモ等の参照（任意）')
        form.addRow('証拠参照', self.evidence_edit)

        layout.addLayout(form)

        save_row = QHBoxLayout()
        self.save_context_button = QPushButton('設置コンテキストを記録')
        set_control_size(self.save_context_button, ControlSize.STANDARD)
        self.save_context_button.clicked.connect(self._save_context)
        save_row.addWidget(self.save_context_button)
        save_row.addStretch(1)
        layout.addLayout(save_row)

        self.context_error_label = QLabel('')
        self.context_error_label.setWordWrap(True)
        set_semantic_state(self.context_error_label, SemanticState.ERROR)
        self.context_error_label.setVisible(False)
        layout.addWidget(self.context_error_label)

        list_title = QLabel('このスピーカーの記録済みコンテキスト')
        set_typography_role(list_title, TypographyRole.SECONDARY)
        layout.addWidget(list_title)
        self.context_list = QListWidget()
        self.context_list.setMaximumHeight(110)
        self.context_list.currentRowChanged.connect(
            self._context_selected
        )
        layout.addWidget(self.context_list)

        self.evaluation_label = QLabel('')
        self.evaluation_label.setWordWrap(True)
        set_typography_role(self.evaluation_label, TypographyRole.SECONDARY)
        layout.addWidget(self.evaluation_label)

    def _build_datum_section(self, layout: QVBoxLayout) -> None:
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        section_title = QLabel('設置基準（データム）')
        set_typography_role(section_title, TypographyRole.SECTION_TITLE)
        layout.addWidget(section_title)
        datum_note = QLabel(
            '設置基準は選んだシーンリビジョンの部屋形状に固定されます。'
            '設置ハンドオフ出力は固定先リビジョンの書き出しでのみ'
            'この基準を掲載します。'
        )
        set_typography_role(datum_note, TypographyRole.SECONDARY)
        datum_note.setWordWrap(True)
        layout.addWidget(datum_note)

        form = QFormLayout()
        form.setSpacing(6)

        self.revision_combo = _styled_combo()
        self.revision_combo.currentIndexChanged.connect(
            self._datum_revision_changed
        )
        form.addRow('固定先リビジョン', self.revision_combo)

        self.origin_label_edit = QLineEdit('床面・前左隅')
        form.addRow('原点ラベル', self.origin_label_edit)
        self.x_label_edit = QLineEdit('右方向（X軸）')
        form.addRow('X軸ラベル', self.x_label_edit)
        self.y_label_edit = QLineEdit('奥方向（Y軸）')
        form.addRow('Y軸ラベル', self.y_label_edit)
        self.z_label_edit = QLineEdit('上方向（Z軸）')
        form.addRow('Z軸ラベル', self.z_label_edit)
        handedness_label = QLabel('右手系（固定）')
        set_typography_role(handedness_label, TypographyRole.SECONDARY)
        form.addRow('座標系', handedness_label)

        self.anchor_kind_combo = _styled_combo()
        self.anchor_kind_combo.addItem('部屋の頂点', 'room_vertex')
        self.anchor_kind_combo.addItem('壁面', 'wall_face')
        self.anchor_kind_combo.currentIndexChanged.connect(
            self._anchor_kind_changed
        )
        form.addRow('主アンカー種別', self.anchor_kind_combo)

        self.anchor_vertex_combo = _styled_combo()
        self.anchor_vertex_label = QLabel('アンカー頂点')
        form.addRow(self.anchor_vertex_label, self.anchor_vertex_combo)

        self.anchor_wall_combo = _styled_combo()
        self.anchor_wall_label = QLabel('アンカー壁')
        form.addRow(self.anchor_wall_label, self.anchor_wall_combo)
        self.anchor_wall_label.setVisible(False)
        self.anchor_wall_combo.setVisible(False)

        self.anchor_label_edit = QLineEdit('前左隅')
        form.addRow('アンカーラベル', self.anchor_label_edit)

        self.x_wall_combo = _styled_combo()
        form.addRow('X方向の壁', self.x_wall_combo)
        self.y_wall_combo = _styled_combo()
        form.addRow('Y方向の壁', self.y_wall_combo)

        self.datum_evidence_edit = QLineEdit()
        self.datum_evidence_edit.setPlaceholderText(
            '証拠参照（カンマ区切り、任意）'
        )
        form.addRow('証拠参照', self.datum_evidence_edit)

        self.datum_notes_edit = QLineEdit()
        self.datum_notes_edit.setPlaceholderText('備考（任意）')
        form.addRow('備考', self.datum_notes_edit)

        layout.addLayout(form)

        self.datum_hint_label = QLabel('')
        self.datum_hint_label.setWordWrap(True)
        set_semantic_state(self.datum_hint_label, SemanticState.WARNING)
        self.datum_hint_label.setVisible(False)
        layout.addWidget(self.datum_hint_label)

        save_row = QHBoxLayout()
        self.save_datum_button = QPushButton('設置基準を記録')
        set_control_size(self.save_datum_button, ControlSize.STANDARD)
        self.save_datum_button.clicked.connect(self._save_datum)
        save_row.addWidget(self.save_datum_button)
        save_row.addStretch(1)
        layout.addLayout(save_row)

        self.datum_error_label = QLabel('')
        self.datum_error_label.setWordWrap(True)
        set_semantic_state(self.datum_error_label, SemanticState.ERROR)
        self.datum_error_label.setVisible(False)
        layout.addWidget(self.datum_error_label)

        list_title = QLabel('記録済みの設置基準')
        set_typography_role(list_title, TypographyRole.SECONDARY)
        layout.addWidget(list_title)
        self.datum_list = QListWidget()
        self.datum_list.setMaximumHeight(110)
        layout.addWidget(self.datum_list)

    # -- shared helpers -----------------------------------------------------

    def _head_document(self):
        head = self.scene_repository.current_head(self.document_id)
        return None if head is None else head.document

    def _speaker_ids(self) -> tuple[str, ...]:
        document = self._head_document()
        if document is None:
            return ()
        return tuple(
            entity.entity_id
            for entity in document.entities
            if entity.kind == 'speaker'
        )

    def _selected_speaker_id(self) -> str | None:
        data = self.speaker_combo.currentData()
        return None if data is None else str(data)

    def _selected_definition(self) -> EquipmentDefinition | None:
        data = self.equipment_combo.currentData()
        if data is None:
            return None
        return self._definitions.get(str(data))

    def _set_context_error(self, message: str | None) -> None:
        self.context_error_label.setText(message or '')
        self.context_error_label.setVisible(bool(message))

    def _set_datum_error(self, message: str | None) -> None:
        self.datum_error_label.setText(message or '')
        self.datum_error_label.setVisible(bool(message))

    def _operator_provenance(
        self,
        *,
        actor: str,
        role: str,
    ) -> EquipmentDataProvenance:
        reference = self.evidence_edit.text().strip() or f'operator:{role}'
        # Typed-citation convention (#614): the content hash of the typed
        # citation is the honest identifier when no source bytes exist.
        digest = sha256(
            f'{actor}|{role}|{reference}'.encode('utf-8')
        ).hexdigest()
        return EquipmentDataProvenance(
            evidence_kind='user_defined',
            source_name=actor,
            source_version=utc_now_iso()[:10],
            source_reference=reference,
            source_sha256=digest,
        )

    # -- context form ---------------------------------------------------------

    def _speaker_changed(self) -> None:
        if self._syncing:
            return
        self._refresh_host_combo()
        self._refresh_context_list()

    def _equipment_changed(self) -> None:
        if self._syncing:
            return
        self.open_library_button.setEnabled(
            self._selected_definition() is not None
        )
        self._refresh_mounting_combo()

    def _open_in_library(self) -> None:
        definition = self._selected_definition()
        if definition is None:
            return
        self.libraryRequested.emit(definition.definition_id)

    def _directivity_changed(self) -> None:
        non_default = (
            self.directivity_combo.currentData() not in (None, 'unknown')
        )
        self.directivity_rationale_edit.setEnabled(non_default)
        if not non_default:
            self.directivity_rationale_edit.clear()

    def _refresh_mounting_combo(self) -> None:
        definition = self._selected_definition()
        declared = (
            tuple(definition.mounting.mounting_modes)
            if definition is not None and definition.mounting is not None
            else ()
        )
        items: list[tuple[str, str]] = [
            (_MOUNTING_LABELS[mode], mode) for mode in declared
        ]
        if not items:
            items.append((_MOUNTING_LABELS['unknown'], 'unknown'))
        _fill_combo(self.mounting_combo, tuple(items))
        if definition is None:
            self.mounting_hint.setText('')
        elif declared:
            self.mounting_hint.setText(
                'この機材定義が宣言する設置方式のみ選択できます。'
            )
        else:
            self.mounting_hint.setText(
                'この機材定義は設置方式を宣言していないため、'
                '記録は「未記録」のみ選択できます。'
            )

    def _refresh_host_combo(self) -> None:
        speaker_id = self._selected_speaker_id()
        document = self._head_document()
        items: list[tuple[str, str | None]] = [('（なし）', None)]
        if document is not None:
            topology = getattr(document, 'wall_topology', None)
            if topology is not None:
                for wall in topology.walls:
                    items.append((f'壁: {wall.wall_id}', wall.wall_id))
            for entity in document.entities:
                if entity.entity_id == speaker_id:
                    continue
                items.append(
                    (f'{entity.name}（{entity.entity_id}）', entity.entity_id)
                )
        _fill_combo(self.host_combo, tuple(items))

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        """Apply the #496 display-unit policy to the clearance fields.

        SI metres stay authoritative — only suffix/decimals change. Rows
        added later inherit the stored policy so they never re-open in
        metres while the rest of the app shows another unit.
        """

        self._length_policy = policy
        for spin in self.clearance_spins.values():
            spin.set_display_unit(policy.unit, decimals=policy.decimals)
        for index in range(self.override_rows.count()):
            row = self.override_rows.itemAt(index).widget()
            if isinstance(row, _OverrideRow):
                row.required_spin.set_display_unit(
                    policy.unit, decimals=policy.decimals
                )

    def _add_override_row(self) -> None:
        row = _OverrideRow(self)
        row.required_spin.set_display_unit(
            self._length_policy.unit, decimals=self._length_policy.decimals
        )
        remove = QPushButton('削除')
        set_control_size(remove, ControlSize.COMPACT)
        remove.clicked.connect(lambda: self._remove_override_row(row))
        row_layout = row.layout()
        row_layout.addWidget(remove)
        self.override_rows.addWidget(row)

    def _remove_override_row(self, row: _OverrideRow) -> None:
        self.override_rows.removeWidget(row)
        row.deleteLater()

    def _collect_overrides(self) -> list[ClearanceOverride]:
        overrides: list[ClearanceOverride] = []
        for index in range(self.override_rows.count()):
            row = self.override_rows.itemAt(index).widget()
            if not isinstance(row, _OverrideRow):
                continue
            rationale = row.rationale_edit.text().strip()
            if not rationale:
                raise ValueError(
                    'クリアランス上書きの根拠を入力してください。'
                )
            overrides.append(
                ClearanceOverride(
                    axis=row.axis_combo.currentData(),
                    required_m=float(row.required_spin.value_m()),
                    rationale=rationale,
                    provenance=self._operator_provenance(
                        actor=self.actor_edit.text().strip() or 'operator',
                        role='clearance-override',
                    ),
                )
            )
        axes = [item.axis for item in overrides]
        if len(axes) != len(set(axes)):
            raise ValueError('クリアランス上書きの軸は一意である必要があります。')
        return overrides

    def _save_context(self) -> None:
        self._set_context_error(None)
        try:
            speaker_id = self._selected_speaker_id()
            if speaker_id is None:
                raise ValueError('対象スピーカーを選択してください。')
            definition = self._selected_definition()
            if definition is None:
                raise ValueError('機材定義を選択してください。')
            actor = self.actor_edit.text().strip()
            if not actor:
                raise ValueError('記録者名を入力してください。')
            directivity = str(self.directivity_combo.currentData())
            directivity_rationale = (
                self.directivity_rationale_edit.text().strip()
            )
            if directivity != 'unknown' and not directivity_rationale:
                raise ValueError(
                    '標準以外の指向性適用区分には根拠の記録が必要です。'
                )
            measured = self._collect_measured_clearances()
            overrides = self._collect_overrides()
            provenance: list[EquipmentDataProvenance] = [
                self._operator_provenance(actor=actor, role='context')
            ]
            if directivity != 'unknown':
                # The context model carries no dedicated rationale field;
                # the reason is recorded as a second provenance citation so
                # the authority chain keeps the basis auditable.
                provenance.append(
                    self._operator_provenance(
                        actor=actor,
                        role=f'directivity:{directivity}:{directivity_rationale}',
                    )
                )
            host_data = self.host_combo.currentData()
            host_surface = self.host_surface_edit.text().strip() or None
            context = build_installation_context(
                context_id=f'ictx-{uuid4().hex[:16]}',
                document_id=self.document_id,
                entity_id=speaker_id,
                equipment_definition=definition,
                selected_mounting_mode=str(
                    self.mounting_combo.currentData()
                ),
                provenance=tuple(provenance),
                created_at_utc=utc_now_iso(),
                host_entity_id=None if host_data is None else str(host_data),
                host_surface_label=host_surface,
                baffle_state=str(self.baffle_combo.currentData()),
                measured_clearances=measured,
                clearance_overrides=tuple(overrides),
                directivity_applicability=directivity,
            )
            self.context_repository.save_context(context)
        except ValueError as exc:
            self._set_context_error(operation_error_message(exc))
            return
        self._refresh_context_list()
        self.contextSaved.emit()

    def _collect_measured_clearances(self) -> MeasuredClearances | None:
        values: dict[str, float] = {}
        for field, _axis in _MEASURED_FIELDS:
            if self.clearance_checks[field].isChecked():
                values[field] = float(self.clearance_spins[field].value_m())
        if not values:
            return None
        return MeasuredClearances(**values)

    def _refresh_context_list(self) -> None:
        self.context_list.clear()
        self.evaluation_label.setText('')
        speaker_id = self._selected_speaker_id()
        if speaker_id is None:
            return
        contexts = self.context_repository.list_contexts_for_entity(
            self.document_id, speaker_id
        )
        for context in contexts:
            actors = '、'.join(
                item.source_name for item in context.provenance
            )
            item = QListWidgetItem(
                f'{context.created_at_utc} — {context.context_id}'
                f'（記録者: {actors or "不明"}）'
            )
            item.setData(Qt.ItemDataRole.UserRole, context.context_id)
            self.context_list.addItem(item)
        if contexts:
            self.context_list.setCurrentRow(len(contexts) - 1)

    def _context_selected(self, row: int) -> None:
        if row < 0:
            self.evaluation_label.setText('')
            return
        speaker_id = self._selected_speaker_id()
        if speaker_id is None:
            return
        item = self.context_list.item(row)
        if item is None:
            return
        contexts = self.context_repository.list_contexts_for_entity(
            self.document_id, speaker_id
        )
        context = next(
            (
                entry
                for entry in contexts
                if entry.context_id == item.data(Qt.ItemDataRole.UserRole)
            ),
            None,
        )
        if context is None:
            self.evaluation_label.setText('')
            return
        try:
            evaluation = self._evaluate_context(context)
        except ValueError as exc:
            self.evaluation_label.setText(f'評価できません: {operation_error_message(exc)}')
            set_semantic_state(
                self.evaluation_label, SemanticState.ERROR
            )
            return
        if evaluation is None:
            self.evaluation_label.setText('')
            return
        self._show_evaluation(context, evaluation)

    def _evaluate_context(
        self,
        context: SpeakerInstallationContext,
    ) -> InstallationEvaluation | None:
        latest = self.context_repository.get_context_for_entity(
            self.document_id, context.entity_id
        )
        if latest is not None and latest.context_id == context.context_id:
            return self.context_repository.evaluate_for_entity(
                self.document_id, context.entity_id
            )
        # Older records in the append-only chain evaluate against the same
        # persisted definition + current head, using their own fields.
        definition = self.equipment_repository.get_definition_by_hash(
            context.equipment.equipment_definition_sha256
        )
        if definition is None:
            raise ValueError('機材定義が見つかりません')
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            return None
        entity = next(
            (
                item
                for item in head.document.entities
                if item.entity_id == context.entity_id
            ),
            None,
        )
        if entity is None:
            raise ValueError('対象スピーカーが現在の場面に存在しません')
        return evaluate_installation_context(
            document=head.document,
            entity=entity,
            equipment_definition=definition,
            context=context,
        )

    def _show_evaluation(
        self,
        context: SpeakerInstallationContext,
        evaluation: InstallationEvaluation,
    ) -> None:
        lines = [
            f'設置条件: {_CONDITION_LABELS.get(evaluation.installed_condition, evaluation.installed_condition)}'
            f' / 証拠条件: {_CONDITION_LABELS.get(evaluation.evidence_condition, evaluation.evidence_condition)}'
            f' / 互換性: {_COMPATIBILITY_LABELS.get(evaluation.condition_compatibility, evaluation.condition_compatibility)}'
        ]
        for check in evaluation.checks:
            state = _CHECK_STATE_LABELS.get(check.state, check.state)
            lines.append(f'{state}: {check.check} — {check.reason}')
        self.evaluation_label.setText('\n'.join(lines))
        set_semantic_state(
            self.evaluation_label,
            SemanticState.ERROR
            if any(check.state == 'FAIL' for check in evaluation.checks)
            else None,
        )

    # -- datum form -----------------------------------------------------------

    def _datum_revision_changed(self) -> None:
        if self._syncing:
            return
        self._refresh_datum_geometry_combos()

    def _anchor_kind_changed(self) -> None:
        vertex_mode = (
            self.anchor_kind_combo.currentData() == 'room_vertex'
        )
        self.anchor_vertex_combo.setVisible(vertex_mode)
        self.anchor_vertex_label.setVisible(vertex_mode)
        self.anchor_wall_combo.setVisible(not vertex_mode)
        self.anchor_wall_label.setVisible(not vertex_mode)

    def _datum_revision_document(self):
        revision_id = self.revision_combo.currentData()
        if revision_id is None:
            return None
        revision = self.scene_repository.get(str(revision_id))
        return None if revision is None else revision.document

    def _refresh_datum_geometry_combos(self) -> None:
        document = self._datum_revision_document()
        vertices = (
            ()
            if document is None or document.room is None
            else room_vertices(document.room)
        )
        topology = (
            None if document is None else getattr(document, 'wall_topology', None)
        )
        walls = () if topology is None else topology.walls
        _fill_combo(
            self.anchor_vertex_combo,
            tuple(
                (f'{v.vertex_id}（{v.x_m:.2f}, {v.y_m:.2f}）', v.vertex_id)
                for v in vertices
            ),
        )
        wall_items = tuple((wall.wall_id, wall.wall_id) for wall in walls)
        _fill_combo(self.anchor_wall_combo, wall_items)
        _fill_combo(self.x_wall_combo, wall_items)
        _fill_combo(self.y_wall_combo, wall_items)
        has_walls = bool(walls)
        self.datum_hint_label.setVisible(not has_walls)
        if not has_walls:
            self.datum_hint_label.setText(
                'このリビジョンには壁トポロジがありません。'
                '壁が定義された場面を保存してから記録してください。'
            )
        self.save_datum_button.setEnabled(has_walls)

    def _save_datum(self) -> None:
        self._set_datum_error(None)
        try:
            revision_id = self.revision_combo.currentData()
            if revision_id is None:
                raise ValueError('固定先リビジョンを選択してください。')
            revision = self.scene_repository.get(str(revision_id))
            if revision is None:
                raise ValueError('固定先リビジョンを読み込めません。')
            for label, edit in (
                ('原点ラベル', self.origin_label_edit),
                ('X軸ラベル', self.x_label_edit),
                ('Y軸ラベル', self.y_label_edit),
                ('Z軸ラベル', self.z_label_edit),
            ):
                if not edit.text().strip():
                    raise ValueError(f'{label}を入力してください。')
            anchor_label = self.anchor_label_edit.text().strip()
            if not anchor_label:
                raise ValueError('アンカーラベルを入力してください。')
            if self.anchor_kind_combo.currentData() == 'room_vertex':
                vertex_id = self.anchor_vertex_combo.currentData()
                if vertex_id is None:
                    raise ValueError('アンカー頂点を選択してください。')
                anchor = DatumReferencePoint(
                    kind='room_vertex',
                    vertex_id=str(vertex_id),
                    label=anchor_label,
                )
            else:
                wall_id = self.anchor_wall_combo.currentData()
                if wall_id is None:
                    raise ValueError('アンカー壁を選択してください。')
                anchor = DatumReferencePoint(
                    kind='wall_face',
                    wall_id=str(wall_id),
                    label=anchor_label,
                )
            x_wall = self.x_wall_combo.currentData()
            y_wall = self.y_wall_combo.currentData()
            if x_wall is None or y_wall is None:
                raise ValueError('X方向とY方向の壁を選択してください。')
            if x_wall == y_wall:
                raise ValueError('X方向とY方向には別の壁を選んでください。')
            evidence_refs = tuple(
                dict.fromkeys(
                    part.strip()
                    for part in self.datum_evidence_edit.text().split(',')
                    if part.strip()
                )
            )
            datum_id = f'datum-{self.document_id}'
            existing = self.datum_repository.list_datum_versions(datum_id)
            datum = build_installation_datum(
                document_id=self.document_id,
                scene_revision_id=str(revision_id),
                scene_content_hash=revision.content_hash,
                frame_semantics=DatumFrameSemantics(
                    origin_label=self.origin_label_edit.text().strip(),
                    x_label=self.x_label_edit.text().strip(),
                    y_label=self.y_label_edit.text().strip(),
                    z_label=self.z_label_edit.text().strip(),
                ),
                primary_anchor=anchor,
                x_direction_wall_id=str(x_wall),
                y_direction_wall_id=str(y_wall),
                created_at_utc=utc_now_iso(),
                datum_id=datum_id,
                version=str(len(existing) + 1),
                evidence_refs=evidence_refs,
                notes=self.datum_notes_edit.text().strip() or None,
            )
            self.datum_repository.save_datum(datum)
        except ValueError as exc:
            self._set_datum_error(operation_error_message(exc))
            return
        self._refresh_datum_list()
        self.datumSaved.emit()

    def _refresh_datum_list(self) -> None:
        self.datum_list.clear()
        datums = self.datum_repository.list_datums(self.document_id)
        head = self.scene_repository.current_head(self.document_id)
        head_hash = None if head is None else head.content_hash
        head_document = None if head is None else head.document
        vertex_ids: tuple[str, ...] = ()
        wall_ids: tuple[str, ...] = ()
        if head_document is not None and head_document.room is not None:
            vertex_ids = tuple(
                vertex.vertex_id
                for vertex in room_vertices(head_document.room)
            )
            topology = getattr(head_document, 'wall_topology', None)
            if topology is not None:
                wall_ids = tuple(wall.wall_id for wall in topology.walls)
        labels = self.scene_repository.revision_labels(self.document_id)
        for datum in datums:
            freshness = evaluate_datum_freshness(
                datum,
                scene_content_hash=head_hash or '',
                present_vertex_ids=vertex_ids,
                present_wall_ids=wall_ids,
            )
            revision = self.scene_repository.get(datum.scene_revision_id)
            revision_label = (
                revision_display_label(revision, labels)
                if revision is not None
                else datum.scene_revision_id
            )
            item = QListWidgetItem(
                f'{datum.datum_id} v{datum.version} — {revision_label} '
                f'（{_FRESHNESS_LABELS.get(freshness.status, freshness.status)}）'
            )
            item.setData(Qt.ItemDataRole.UserRole, datum.semantic_sha256)
            self.datum_list.addItem(item)

    # -- public surface ---------------------------------------------------------

    def set_selected_entity(self, entity_id: str | None) -> None:
        """Reflect a workspace selection in the speaker combo."""
        if entity_id is None:
            return
        index = self.speaker_combo.findData(entity_id)
        if index >= 0:
            self.speaker_combo.setCurrentIndex(index)

    def refresh(self) -> None:
        """Repopulate every pick from persisted authority."""
        self._syncing = True
        try:
            keep_speaker = self._selected_speaker_id()
            keep_equipment = self.equipment_combo.currentData()
            keep_revision = self.revision_combo.currentData()
            speakers = self._speaker_ids()
            document = self._head_document()
            names = (
                {}
                if document is None
                else {
                    entity.entity_id: entity.name
                    for entity in document.entities
                }
            )
            _fill_combo(
                self.speaker_combo,
                tuple(
                    (f'{names.get(eid, eid)}（{eid}）', eid)
                    for eid in speakers
                ),
                keep_data=keep_speaker,
            )
            self._definitions = {
                definition.semantic_sha256: definition
                for definition in self.equipment_repository.list_definitions()
            }
            _fill_combo(
                self.equipment_combo,
                tuple(
                    (
                        (
                            f'{definition.manufacturer} {definition.model}'
                            if definition.manufacturer and definition.model
                            else definition.user_label
                            or definition.definition_id
                        )
                        + f'（v{definition.version}）',
                        definition.semantic_sha256,
                    )
                    for definition in self._definitions.values()
                ),
                keep_data=keep_equipment,
            )
            labels = self.scene_repository.revision_labels(self.document_id)
            summaries = self.scene_repository.list_revision_summaries(
                self.document_id
            )
            head = self.scene_repository.current_head(self.document_id)
            head_id = None if head is None else head.revision_id
            _fill_combo(
                self.revision_combo,
                tuple(
                    (
                        revision_display_label(summary, labels)
                        + ('（分岐）' if summary.detached else ''),
                        summary.revision_id,
                    )
                    for summary in summaries
                ),
                keep_data=keep_revision if keep_revision else head_id,
            )
        finally:
            self._syncing = False
        self._refresh_mounting_combo()
        self._refresh_host_combo()
        self._refresh_context_list()
        self._refresh_datum_geometry_combos()
        self._refresh_datum_list()


__all__ = ['InstallationPanel']
