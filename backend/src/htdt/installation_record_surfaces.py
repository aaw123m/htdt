"""REV44-STAGED — record-entry surface for installation authorities.

``CadEquipmentBindingRepository.save_binding`` and
``CadInstallationContextRepository.save_context`` had live readers — the
概要 installation card evaluates per-speaker contexts, the equipment
readiness notice + evidence-gap register read bindings per entity, and
R110 source resolution fail-closes on missing binding/context pins — but
no production writer existed, so those readers could only ever report
未記録.

This dialog records only operator-declared evidence: which equipment
definition each speaker entity is bound to (with the operator's declared
authority split), and how each speaker is actually installed (mounting,
baffle, host, measured clearances). Provenance follows the equipment
library convention — picked evidence bytes hash to the asset, otherwise
the typed citation text hashes to itself. Nothing is simulated; invalid
input fails closed with a JA status message.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
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
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .cad_equipment import EquipmentDataProvenance
from .cad_equipment_binding import build_equipment_binding_semantics
from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_equipment_repository import CadEquipmentRepository
from .cad_installation_context import (
    MeasuredClearances,
    build_installation_context,
)
from .cad_installation_context_repository import (
    CadInstallationContextRepository,
)
from .cad_installation_datum import (
    DatumFrameSemantics,
    DatumReferencePoint,
    build_installation_datum,
)
from .cad_installation_datum_repository import (
    CadInstallationDatumRepository,
)
from .cad_repository import SceneRepository
from .cad_scene import room_vertices
from .cad_walls import make_wall_topology
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _section_title(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    set_typography_role(label, TypographyRole.SECONDARY)
    return label


_EVIDENCE_KIND_JA = {
    'user_defined': 'ユーザー定義',
    'measured': '実測',
    'manufacturer': 'メーカー資料',
    'inferred': '推定',
    'analytic': '解析',
}

_BODY_GEOMETRY_JA = {
    'generic_placeholder': '汎用プレースホルダ',
    'equipment_nominal': '機器公称値',
    'user_authored': 'ユーザー作成',
    'observed_asbuilt': '実測・竣工',
}

_ACOUSTIC_REFERENCE_JA = {
    'equipment_derived': '機器由来',
    'scene_explicit': 'シーン明示',
    'approximate_placeholder': '近似プレースホルダ',
}

_MOUNTING_MODE_JA = {
    'free_standing': '自立・床置き',
    'stand': 'スタンド',
    'shelf': '棚置き',
    'wall': '壁掛け',
    'ceiling': '天吊り',
    'in_wall': '壁埋込',
    'in_ceiling': '天井埋込',
    'custom': 'カスタム',
    'unknown': '不明',
}

_BAFFLE_STATE_JA = {
    'free_space': 'フリー空間',
    'flush_baffle': '面内バッフル（壁面埋込相当）',
    'finite_baffle': '有限バッフル',
    'boundary_adjacent': '境界面近接',
    'unknown': '不明',
}

_DIRECTIVITY_JA = {
    'anechoic': '無響室',
    'iec_baffle': 'IECバッフル',
    'in_wall': '壁埋込',
    'manufacturer_fixture': 'メーカー指定治具',
    'unknown': '不明',
}

_ANCHOR_KIND_JA = {
    'room_vertex': '部屋の角',
    'wall_face': '壁面',
}


class _ProvenanceEditor(QWidget):
    """One EquipmentDataProvenance row: citation text or picked bytes.

    Mirrors ``EquipmentLibraryService._provenance``: retained source bytes
    hash to the asset; a typed citation hashes to itself so the claim is
    still content-addressed and honestly identifies what was declared.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_bytes: bytes | None = None
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.kind_combo = QComboBox()
        for value, label in _EVIDENCE_KIND_JA.items():
            self.kind_combo.addItem(label, value)
        form.addRow('証拠種別', self.kind_combo)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('例: 現地確認メモ、メーカー資料名')
        form.addRow('証拠名（必須）', self.name_edit)
        self.version_edit = QLineEdit('1')
        form.addRow('証拠版', self.version_edit)
        self.reference_edit = QLineEdit()
        self.reference_edit.setPlaceholderText('例: p.3、URL、確認日')
        form.addRow('参照', self.reference_edit)
        file_row = QHBoxLayout()
        self.file_button = QPushButton('証跡ファイル（任意）…')
        self.file_button.clicked.connect(self._pick_file)
        file_row.addWidget(self.file_button)
        self.file_label = QLabel('（未選択 — 入力テキストをハッシュ化）')
        self.file_label.setWordWrap(True)
        file_row.addWidget(self.file_label, 1)
        file_host = QWidget()
        file_host.setLayout(file_row)
        form.addRow('証跡ファイル', file_host)

    def _pick_file(self) -> None:
        selected, _filter = file_dialog_memory.get_open_file_name(
            self, '証跡ファイルを選択', 'installation-evidence'
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = path.read_bytes()
        except OSError:
            self.file_label.setText('読み込めませんでした')
            return
        self._source_bytes = data
        self.file_label.setText(f'{path.name}（{len(data)} bytes）')

    def provenance(self) -> EquipmentDataProvenance | None:
        """Build the declared provenance, or None when name is blank."""
        name = self.name_edit.text().strip()
        if not name:
            return None
        version = self.version_edit.text().strip() or '1'
        reference = self.reference_edit.text().strip()
        if self._source_bytes is not None:
            source_sha = sha256(self._source_bytes).hexdigest()
        else:
            source_sha = sha256(
                f'{name}|{version}|{reference}'.encode('utf-8')
            ).hexdigest()
        return EquipmentDataProvenance(
            evidence_kind=self.kind_combo.currentData(),
            source_name=name,
            source_version=version,
            source_reference=reference,
            source_sha256=source_sha,
        )


class InstallationRecordDialog(QDialog):
    """Record equipment bindings + speaker installation contexts."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        document_id: str,
        equipment_repository: CadEquipmentRepository | None = None,
        binding_repository: CadEquipmentBindingRepository | None = None,
        context_repository: CadInstallationContextRepository | None = None,
        datum_repository: CadInstallationDatumRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.equipment_repository = (
            equipment_repository or CadEquipmentRepository(scene_repository)
        )
        self.binding_repository = (
            binding_repository
            or CadEquipmentBindingRepository(
                scene_repository, self.equipment_repository
            )
        )
        self.context_repository = (
            context_repository
            or CadInstallationContextRepository(
                scene_repository, self.equipment_repository
            )
        )
        self.datum_repository = (
            datum_repository or CadInstallationDatumRepository(
                scene_repository
            )
        )
        self._head = None
        self._speakers = []
        self._definitions = []
        self._wall_items = []
        self._vertex_items = []

        self.setWindowTitle('機材・設置の記録')
        self.setMinimumWidth(700)
        layout = QVBoxLayout(self)

        layout.addWidget(_section_title('スピーカー別の記録状況', self))
        self.entity_status = QTreeWidget(self)
        self.entity_status.setHeaderLabels(
            ['スピーカー', '機材バインド', '設置コンテキスト']
        )
        self.entity_status.setRootIsDecorated(False)
        self.entity_status.setMinimumHeight(110)
        layout.addWidget(self.entity_status)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.tabs.addTab(self._build_binding_tab(), '機材バインド')
        self.tabs.addTab(self._build_context_tab(), '設置コンテキスト')
        self.tabs.addTab(self._build_datum_tab(), '設置基準')

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.reload()

    # -- shared pick helpers -------------------------------------------------

    def _speaker_items(self, combo: QComboBox) -> None:
        combo.clear()
        for entity in self._speakers:
            combo.addItem(
                f'{entity.name}（{entity.entity_id}）', entity.entity_id
            )
        if not self._speakers:
            combo.addItem('（スピーカーがありません）', None)

    def _definition_items(self, combo: QComboBox) -> None:
        combo.clear()
        for definition in self._definitions:
            label = definition.user_label or definition.definition_id
            if definition.manufacturer or definition.model:
                label = (
                    f'{label} — {definition.manufacturer or ""}'
                    f' {definition.model or ""}'
                ).strip()
            combo.addItem(label, definition)
        if not self._definitions:
            combo.addItem('（機器定義がありません）', None)

    # -- tab: equipment binding ----------------------------------------------

    def _build_binding_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.bind_entity_combo = QComboBox()
        form.addRow('スピーカー', self.bind_entity_combo)
        self.bind_equipment_combo = QComboBox()
        form.addRow('機器定義', self.bind_equipment_combo)
        self.body_authority_combo = QComboBox()
        for value, label in _BODY_GEOMETRY_JA.items():
            self.body_authority_combo.addItem(label, value)
        form.addRow('形状の権威', self.body_authority_combo)
        self.ref_authority_combo = QComboBox()
        for value, label in _ACOUSTIC_REFERENCE_JA.items():
            self.ref_authority_combo.addItem(label, value)
        form.addRow('音響基準の権威', self.ref_authority_combo)
        layout.addLayout(form)
        layout.addWidget(_section_title('証拠（どちらか必須）', page))
        self.bind_provenance = _ProvenanceEditor(page)
        layout.addWidget(self.bind_provenance)
        self.bind_button = QPushButton('バインドを記録', page)
        self.bind_button.clicked.connect(self._save_binding)
        layout.addWidget(self.bind_button)
        layout.addStretch(1)
        return page

    # -- tab: installation context -------------------------------------------

    def _build_context_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.context_entity_combo = QComboBox()
        self.context_entity_combo.currentIndexChanged.connect(
            self._refresh_host_items
        )
        form.addRow('スピーカー', self.context_entity_combo)
        self.context_equipment_combo = QComboBox()
        form.addRow('機器定義', self.context_equipment_combo)
        self.mounting_combo = QComboBox()
        for value, label in _MOUNTING_MODE_JA.items():
            self.mounting_combo.addItem(label, value)
        form.addRow('設置形態', self.mounting_combo)
        self.baffle_combo = QComboBox()
        for value, label in _BAFFLE_STATE_JA.items():
            self.baffle_combo.addItem(label, value)
        self.baffle_combo.setCurrentIndex(len(_BAFFLE_STATE_JA) - 1)
        form.addRow('バッフル状態', self.baffle_combo)
        self.directivity_combo = QComboBox()
        for value, label in _DIRECTIVITY_JA.items():
            self.directivity_combo.addItem(label, value)
        self.directivity_combo.setCurrentIndex(len(_DIRECTIVITY_JA) - 1)
        form.addRow('指向性データの適用条件', self.directivity_combo)
        self.host_combo = QComboBox()
        form.addRow('設置ホスト', self.host_combo)
        self.host_surface_edit = QLineEdit()
        self.host_surface_edit.setPlaceholderText('例: 天面、棚板上面')
        form.addRow('ホスト面', self.host_surface_edit)
        clearance_row = QHBoxLayout()
        self.clearance_spins: dict[str, QDoubleSpinBox] = {}
        for axis, label in (
            ('front_m', '前面'),
            ('rear_m', '背面'),
            ('side_m', '側面'),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 5.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
            spin.setSuffix(' m')
            spin.setSpecialValueText('不明')
            spin.setToolTip(
                f'{label}方向の実測クリアランス — 0/「不明」のままなら未記録'
            )
            clearance_row.addWidget(QLabel(label))
            clearance_row.addWidget(spin)
            self.clearance_spins[axis] = spin
        clearance_row.addStretch(1)
        clearance_host = QWidget()
        clearance_host.setLayout(clearance_row)
        form.addRow('実測クリアランス', clearance_host)
        layout.addLayout(form)
        layout.addWidget(_section_title('証拠（どちらか必須）', page))
        self.context_provenance = _ProvenanceEditor(page)
        layout.addWidget(self.context_provenance)
        self.context_button = QPushButton('設置コンテキストを記録', page)
        self.context_button.clicked.connect(self._save_context)
        layout.addWidget(self.context_button)
        layout.addStretch(1)
        return page

    def _refresh_host_items(self) -> None:
        selected = self.context_entity_combo.currentData()
        self.host_combo.clear()
        self.host_combo.addItem('（なし・床置きなど）', None)
        if self._head is None:
            return
        for entity in self._head.document.entities:
            if entity.entity_id == selected:
                continue
            self.host_combo.addItem(
                f'{entity.name}（{entity.entity_id}）', entity.entity_id
            )

    # -- tab: installation datum ----------------------------------------------

    def _build_datum_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self.datum_summary = QLabel('', page)
        self.datum_summary.setWordWrap(True)
        layout.addWidget(self.datum_summary)

        form = QFormLayout()
        self.anchor_kind_combo = QComboBox()
        for value, label in _ANCHOR_KIND_JA.items():
            self.anchor_kind_combo.addItem(label, value)
        self.anchor_kind_combo.currentIndexChanged.connect(
            self._refresh_anchor_items
        )
        form.addRow('基準点の種別', self.anchor_kind_combo)
        self.anchor_pick_combo = QComboBox()
        form.addRow('基準点', self.anchor_pick_combo)
        self.anchor_label_edit = QLineEdit()
        self.anchor_label_edit.setPlaceholderText('例: 前左角')
        form.addRow('基準点ラベル', self.anchor_label_edit)
        self.x_wall_combo = QComboBox()
        form.addRow('X方向の壁', self.x_wall_combo)
        self.y_wall_combo = QComboBox()
        form.addRow('Y方向の壁', self.y_wall_combo)
        self.origin_label_edit = QLineEdit('部屋の前左角')
        form.addRow('原点ラベル', self.origin_label_edit)
        self.x_label_edit = QLineEdit('前壁方向（部屋幅）')
        form.addRow('X軸ラベル', self.x_label_edit)
        self.y_label_edit = QLineEdit('左壁方向（部屋奥行）')
        form.addRow('Y軸ラベル', self.y_label_edit)
        self.z_label_edit = QLineEdit('上方向')
        form.addRow('Z軸ラベル', self.z_label_edit)
        self.evidence_refs_edit = QLineEdit()
        self.evidence_refs_edit.setPlaceholderText(
            '例: site-survey-2026-09, floorplan-v2（カンマ区切り）'
        )
        form.addRow('根拠資料', self.evidence_refs_edit)
        self.datum_notes_edit = QLineEdit()
        form.addRow('メモ', self.datum_notes_edit)
        layout.addLayout(form)

        self.datum_button = QPushButton('設置基準を記録', page)
        self.datum_button.clicked.connect(self._save_datum)
        layout.addWidget(self.datum_button)
        layout.addStretch(1)
        return page

    def _refresh_anchor_items(self) -> None:
        self.anchor_pick_combo.clear()
        if self.anchor_kind_combo.currentData() == 'room_vertex':
            for vertex_id in self._vertex_items:
                self.anchor_pick_combo.addItem(vertex_id, vertex_id)
        else:
            for wall_id in self._wall_items:
                self.anchor_pick_combo.addItem(wall_id, wall_id)
        if self.anchor_pick_combo.count() == 0:
            self.anchor_pick_combo.addItem('（選択肢がありません）', None)

    def _save_datum(self) -> None:
        if self._head is None:
            self.status_label.setText(
                '保存済みの部屋がないため、設置基準を記録できません。'
            )
            return
        anchor_id = self.anchor_pick_combo.currentData()
        x_wall = self.x_wall_combo.currentData()
        y_wall = self.y_wall_combo.currentData()
        anchor_label = self.anchor_label_edit.text().strip()
        if anchor_id is None or x_wall is None or y_wall is None:
            self.status_label.setText('基準点と方向の壁を選んでください。')
            return
        if not anchor_label:
            self.status_label.setText('基準点ラベルを入力してください。')
            return
        if x_wall == y_wall:
            self.status_label.setText(
                'X方向とY方向には別の壁を選んでください。'
            )
            return
        kind = self.anchor_kind_combo.currentData()
        anchor = DatumReferencePoint(
            kind=kind,
            vertex_id=anchor_id if kind == 'room_vertex' else None,
            wall_id=anchor_id if kind == 'wall_face' else None,
            label=anchor_label,
        )
        evidence_refs = tuple(
            item.strip()
            for item in self.evidence_refs_edit.text().split(',')
            if item.strip()
        )
        try:
            datum = build_installation_datum(
                document_id=self.document_id,
                scene_revision_id=self._head.revision_id,
                scene_content_hash=self._head.content_hash,
                frame_semantics=DatumFrameSemantics(
                    origin_label=self.origin_label_edit.text().strip()
                    or '部屋の角',
                    x_label=self.x_label_edit.text().strip() or 'X方向',
                    y_label=self.y_label_edit.text().strip() or 'Y方向',
                    z_label=self.z_label_edit.text().strip() or '上方向',
                ),
                primary_anchor=anchor,
                x_direction_wall_id=x_wall,
                y_direction_wall_id=y_wall,
                evidence_refs=evidence_refs,
                notes=self.datum_notes_edit.text().strip() or None,
                created_at_utc=_utc_now(),
            )
            self.datum_repository.save_datum(datum)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(
                f'設置基準を記録できませんでした: {operation_error_message(exc)}'
            )
            return
        self.status_label.setText('設置基準を記録しました。')
        self.reload()

    # -- refresh -------------------------------------------------------------

    def reload(self) -> None:
        """Re-resolve the current head, speakers, definitions + statuses."""
        self._head = self.scene_repository.current_head(self.document_id)
        entities = (
            () if self._head is None else self._head.document.entities
        )
        self._speakers = [
            entity for entity in entities if entity.kind == 'speaker'
        ]
        try:
            self._definitions = list(
                self.equipment_repository.list_definitions()
            )
        except Exception:  # noqa: BLE001 — unreadable store fails closed below
            self._definitions = []

        bindings = self.binding_repository.latest_bindings_for_document(
            self.document_id
        )
        contexts = self.context_repository.latest_contexts_for_document(
            self.document_id
        )
        self.entity_status.clear()
        for entity in self._speakers:
            binding = bindings.get(entity.entity_id)
            context = contexts.get(entity.entity_id)
            self.entity_status.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        entity.name,
                        '記録済み' if binding is not None else '未記録',
                        '記録済み' if context is not None else '未記録',
                    ]
                )
            )
        if not self._speakers:
            empty = QTreeWidgetItem(['（スピーカーがありません）', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.entity_status.addTopLevelItem(empty)

        self._speaker_items(self.bind_entity_combo)
        self._speaker_items(self.context_entity_combo)
        self._definition_items(self.bind_equipment_combo)
        self._definition_items(self.context_equipment_combo)
        self._refresh_host_items()

        document = None if self._head is None else self._head.document
        if document is None:
            self._vertex_items = []
            self._wall_items = []
        else:
            self._vertex_items = [
                vertex.vertex_id
                for vertex in room_vertices(document.room)
            ]
            topology = document.wall_topology
            if topology is None:
                # Legacy documents carry no wall topology; wall ids derive
                # deterministically from the room footprint, so the same ids
                # resolve identically downstream.
                topology = make_wall_topology(document.room)
            self._wall_items = [wall.wall_id for wall in topology.walls]
        for combo in (self.x_wall_combo, self.y_wall_combo):
            combo.clear()
            for wall_id in self._wall_items:
                combo.addItem(wall_id, wall_id)
            if not self._wall_items:
                combo.addItem('（壁がありません）', None)
        if self.y_wall_combo.count() > 1:
            self.y_wall_combo.setCurrentIndex(1)
        self._refresh_anchor_items()
        datums = self.datum_repository.list_datums(self.document_id)
        self.datum_summary.setText(
            f'設置基準の記録: {len(datums)}件'
            if datums
            else '設置基準は未記録です — 設置ハンドオフに基準が載りません。'
        )
        self.datum_button.setEnabled(document is not None)

        ready = bool(self._speakers) and bool(self._definitions)
        self.bind_button.setEnabled(ready)
        self.context_button.setEnabled(ready)
        if not ready:
            if not self._speakers:
                self.status_label.setText(
                    'スピーカーがありません — 部屋にスピーカーを配置して'
                    'ください。'
                )
            else:
                self.status_label.setText(
                    '機器定義がありません — ライブラリで機器を登録して'
                    'ください。'
                )

    # -- writes ---------------------------------------------------------------

    def _save_binding(self) -> None:
        entity_id = self.bind_entity_combo.currentData()
        definition = self.bind_equipment_combo.currentData()
        provenance = self.bind_provenance.provenance()
        if entity_id is None or definition is None:
            self.status_label.setText(
                'スピーカーと機器定義を選んでください。'
            )
            return
        if provenance is None:
            self.status_label.setText('証拠名を入力してください。')
            return
        try:
            binding = build_equipment_binding_semantics(
                binding_id=f'binding-{uuid4().hex[:12]}',
                document_id=self.document_id,
                entity_id=entity_id,
                equipment_definition=definition,
                body_geometry_authority=(
                    self.body_authority_combo.currentData()
                ),
                acoustic_reference_authority=(
                    self.ref_authority_combo.currentData()
                ),
                provenance=(provenance,),
                created_at_utc=_utc_now(),
            )
            self.binding_repository.save_binding(binding)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(
                f'バインドを記録できませんでした: {operation_error_message(exc)}'
            )
            return
        self.status_label.setText('機材バインドを記録しました。')
        self.reload()

    def _save_context(self) -> None:
        entity_id = self.context_entity_combo.currentData()
        definition = self.context_equipment_combo.currentData()
        provenance = self.context_provenance.provenance()
        if entity_id is None or definition is None:
            self.status_label.setText(
                'スピーカーと機器定義を選んでください。'
            )
            return
        if provenance is None:
            self.status_label.setText('証拠名を入力してください。')
            return
        clearances = MeasuredClearances(
            **{
                axis: (spin.value() or None)
                for axis, spin in self.clearance_spins.items()
            }
        )
        if all(
            getattr(clearances, axis) is None
            for axis in self.clearance_spins
        ):
            clearances = None
        try:
            context = build_installation_context(
                context_id=f'install-{uuid4().hex[:12]}',
                document_id=self.document_id,
                entity_id=entity_id,
                equipment_definition=definition,
                selected_mounting_mode=self.mounting_combo.currentData(),
                provenance=(provenance,),
                created_at_utc=_utc_now(),
                host_entity_id=self.host_combo.currentData(),
                host_surface_label=(
                    self.host_surface_edit.text().strip() or None
                ),
                baffle_state=self.baffle_combo.currentData(),
                measured_clearances=clearances,
                directivity_applicability=(
                    self.directivity_combo.currentData()
                ),
            )
            self.context_repository.save_context(context)
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(
                f'設置コンテキストを記録できませんでした: {operation_error_message(exc)}'
            )
            return
        self.status_label.setText('設置コンテキストを記録しました。')
        self.reload()


__all__ = [
    'InstallationRecordDialog',
]
