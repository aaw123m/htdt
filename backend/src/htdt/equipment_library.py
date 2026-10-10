"""First-class Equipment / Source Library surface.

Gives the native UI a path to author user-defined EquipmentDefinition
authorities, publish new immutable versions of an existing definition, and
import capability-gated directivity source assets — without internal
schema IDs or database edits.

All persistence and provenance/evidence semantics stay in the existing
``cad_equipment*`` / ``cad_directivity*`` authorities; this module is a
workflow adapter plus a Qt dialog hosted from the Room workspace.
"""

from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from pathlib import Path
from typing import Sequence
from uuid import uuid4

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
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .accessible_labels import wire_label_buddies
from .clock import utc_now_iso as _utc_now
from .cad_directivity import (
    NORMALIZED_JSON_ADAPTER_ID,
    NormalizedDirectivityJsonV1,
)
from .cad_directivity_import import (
    DIRECTIVITY_ADAPTER_REGISTRY,
    POLAR_TABLE_ADAPTER_ID,
    _parse_metadata_and_rows,
    import_directivity_asset,
)
from .cad_directivity_repository import CadDirectivityRepository
from .cad_equipment import (
    AngleDomain,
    ClearanceMetadata,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentDefinition,
    EquipmentEvidenceKind,
    FrequencyDomain,
    InterpolationProvenance,
    MountingMetadata,
    PortMetadata,
    build_equipment_definition,
)
from .cad_equipment_evidence import build_equipment_manual_evidence
from .cad_equipment_repository import CadEquipmentRepository
from .cad_repository import SceneRepository
from .cad_scene import Offset3, Size3
from .cad_source_response import (
    CadSourceResponseRepository,
    SourceFrequencyResponseAuthority,
    build_source_response,
)
from .cad_system_variant_repository import CadSystemVariantRepository
from .field_tooltips import apply_field_tooltip
from .ingress import read_file_bounded, strict_ascii_number
from .limits import MAX_ATTACHMENT_BYTES
from .equipment_library_service import (
    _EVIDENCE_KINDS,
    _MOUNTING_MODES,
    _PORT_TYPES,
    _definition_label,
    capability_preview,
    EquipmentLibraryService,
)
from .user_facing_error import operation_error_message


class EquipmentLibraryDialog(QDialog):
    """Room-workspace dialog to create/version equipment definitions."""

    definitionsChanged = Signal()

    def __init__(
        self,
        service: EquipmentLibraryService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("機器 / 音源ライブラリ")
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        layout.addLayout(body, 1)

        self.definition_list = QListWidget()
        self.definition_list.setAccessibleName("機器定義一覧")
        self.definition_list.setToolTip(
            "登録済みの機器定義の一覧 — 選択すると能力プレビューが表示され、"
            "新バージョン保存・指向性インポートが有効になります"
        )
        self.definition_list.currentRowChanged.connect(self._selection_changed)
        body.addWidget(self.definition_list, 1)

        form_host = QWidget()
        form = QFormLayout(form_host)
        body.addWidget(form_host, 2)

        self.label_edit = QLineEdit()
        self.label_edit.setAccessibleName("機器ユーザーラベル")
        form.addRow("ラベル（必須）", self.label_edit)
        self.manufacturer_edit = QLineEdit()
        form.addRow("メーカー", self.manufacturer_edit)
        self.model_edit = QLineEdit()
        form.addRow("モデル", self.model_edit)
        for field, tip in (
            (self.label_edit, "この機器定義の表示名（必須）— 一覧での表示名になります"),
            (self.manufacturer_edit, "機器のメーカー名（任意）"),
            (self.model_edit, "機器のモデル名（任意）"),
        ):
            apply_field_tooltip(field, tip, form)

        dims = QHBoxLayout()
        self.width_spin = QDoubleSpinBox()
        self.height_spin = QDoubleSpinBox()
        self.depth_spin = QDoubleSpinBox()
        for spin in (self.width_spin, self.height_spin, self.depth_spin):
            spin.setRange(0.001, 5.0)
            spin.setSingleStep(0.01)
            spin.setDecimals(3)
        self.width_spin.setValue(0.25)
        self.height_spin.setValue(0.4)
        self.depth_spin.setValue(0.3)
        dims.addWidget(QLabel("幅"))
        dims.addWidget(self.width_spin)
        dims.addWidget(QLabel("高さ"))
        dims.addWidget(self.height_spin)
        dims.addWidget(QLabel("奥行"))
        dims.addWidget(self.depth_spin)
        for spin, tip in (
            (self.width_spin, "キャビネットの幅（0.001–5 m）"),
            (self.height_spin, "キャビネットの高さ（0.001–5 m）"),
            (self.depth_spin, "キャビネットの奥行き（0.001–5 m）"),
        ):
            apply_field_tooltip(spin, tip)
        form.addRow("キャビネット寸法 (m)", dims)

        self.mounting_combo = QComboBox()
        for label, value in _MOUNTING_MODES:
            self.mounting_combo.addItem(label, value)
        form.addRow("設置モード", self.mounting_combo)
        self.port_combo = QComboBox()
        for label, value in _PORT_TYPES:
            self.port_combo.addItem(label, value)
        form.addRow("ポート", self.port_combo)
        self.port_clearance_spin = QDoubleSpinBox()
        self.port_clearance_spin.setRange(0.0, 2.0)
        self.port_clearance_spin.setSingleStep(0.05)
        self.port_clearance_spin.setDecimals(3)
        self.port_clearance_spin.setSpecialValueText("未設定")
        form.addRow("ポート最小クリアランス (m)", self.port_clearance_spin)
        for field, tip in (
            (self.mounting_combo, "機器の設置方法 · 不明なら「未設定（不明）」のまま"),
            (self.port_combo, "低音ポートの位置・形式 · 不明なら「不明」のまま"),
            (self.port_clearance_spin, "ポートに必要な最小クリアランス（0–2 m）· 「未設定」のままなら制約なし"),
        ):
            apply_field_tooltip(field, tip, form)

        self.evidence_combo = QComboBox()
        for label, value in _EVIDENCE_KINDS:
            self.evidence_combo.addItem(label, value)
        form.addRow("出典種別", self.evidence_combo)
        self.source_name_edit = QLineEdit()
        form.addRow("出典名（必須）", self.source_name_edit)
        self.source_version_edit = QLineEdit()
        form.addRow("出典バージョン（必須）", self.source_version_edit)
        self.source_reference_edit = QLineEdit()
        form.addRow("出典参照（必須）", self.source_reference_edit)
        for field, tip in (
            (self.evidence_combo, "この定義値の出典種別（ユーザー入力 / メーカー資料 / 実測 など）"),
            (self.source_name_edit, "出典の名前（必須 — メーカー仕様書・実測メモなど）"),
            (self.source_version_edit, "出典の版・日付（必須）"),
            (self.source_reference_edit, "出典内の参照位置（必須 — ページ・項目名）"),
        ):
            apply_field_tooltip(field, tip, form)
        source_file_row = QHBoxLayout()
        self.source_file_label = QLabel("（なし — 入力参照のハッシュを使用）")
        self.source_file_button = QPushButton("出典ファイルを添付…")
        self.source_file_button.setToolTip(
            "仕様書などのファイルを添付して出典のハッシュとして記録します"
        )
        self.source_file_button.clicked.connect(self._attach_source_file)
        source_file_row.addWidget(self.source_file_label, 1)
        source_file_row.addWidget(self.source_file_button)
        form.addRow("出典ファイル", source_file_row)
        self._source_bytes: bytes | None = None

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        form.addRow("能力プレビュー", self.preview_label)

        buttons = QHBoxLayout()
        self.save_new_button = QPushButton("新規保存")
        self.save_new_button.setToolTip(
            "フォームの内容を新しい機器定義として保存します"
        )
        self.save_new_button.clicked.connect(self._save_new)
        self.save_version_button = QPushButton("選択中の新バージョンとして保存")
        self.save_version_button.setToolTip(
            "一覧で選択中の定義の新バージョンとして保存します（定義を選ぶと有効）"
        )
        self.save_version_button.clicked.connect(self._save_new_version)
        self.import_button = QPushButton("指向性データをインポート…")
        self.import_button.setToolTip(
            "選択中の定義に指向性データ（CFx等）を読み込み、新バージョンとして保存します"
        )
        self.import_button.clicked.connect(self._import_directivity)
        buttons.addWidget(self.save_new_button)
        buttons.addWidget(self.save_version_button)
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.box.rejected.connect(self.reject)
        layout.addWidget(self.box)

        self.refresh_definitions()
        wire_label_buddies(self)

    def refresh_definitions(self) -> None:
        self.definition_list.clear()
        for definition in self.service.definitions():
            item = QListWidgetItem(_definition_label(definition))
            item.setData(Qt.ItemDataRole.UserRole, definition.semantic_sha256)
            self.definition_list.addItem(item)
        self._selection_changed(self.definition_list.currentRow())

    def _selected_definition(self) -> EquipmentDefinition | None:
        item = self.definition_list.currentItem()
        if item is None:
            return None
        sha = str(item.data(Qt.ItemDataRole.UserRole))
        return self.service.equipment_repository.get_definition_by_hash(sha)

    def _selection_changed(self, _row: int) -> None:
        definition = self._selected_definition()
        self.save_version_button.setEnabled(definition is not None)
        self.import_button.setEnabled(definition is not None)
        if definition is not None:
            self.preview_label.setText(
                "\n".join(capability_preview(definition))
            )
        else:
            self.preview_label.clear()

    def _attach_source_file(self) -> None:
        selected, _filter = file_dialog_memory.get_open_file_name(
            self, "出典ファイルを選択", 'equipment.attach_source'
        )
        if not selected:
            return
        path = Path(selected)
        self._source_bytes = read_file_bounded(
            path,
            MAX_ATTACHMENT_BYTES,
            label='ソースファイル',
        )
        self.source_file_label.setText(
            f"{path.name} (sha256 {sha256(self._source_bytes).hexdigest()[:12]}…)"
        )

    def _form_common(self) -> dict:
        return {
            "user_label": self.label_edit.text().strip(),
            "manufacturer": self.manufacturer_edit.text().strip() or None,
            "model": self.model_edit.text().strip() or None,
            "width_m": self.width_spin.value(),
            "height_m": self.height_spin.value(),
            "depth_m": self.depth_spin.value(),
            "evidence_kind": self.evidence_combo.currentData(),
            "source_name": self.source_name_edit.text().strip(),
            "source_version": self.source_version_edit.text().strip(),
            "source_reference": self.source_reference_edit.text().strip(),
            "actor": "ローカルユーザー",
            "mounting_mode": self.mounting_combo.currentData() or None,
            "port_type": self.port_combo.currentData(),
            "port_minimum_clearance_m": (
                self.port_clearance_spin.value()
                if self.port_clearance_spin.value() > 0.0
                else None
            ),
            "source_bytes": self._source_bytes,
        }

    def _run_save(self, fn) -> None:
        try:
            definition = fn()
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "機器定義",
                f"保存できませんでした · {operation_error_message(exc)}",
            )
            return
        self.refresh_definitions()
        self.definitionsChanged.emit()
        QMessageBox.information(
            self,
            "機器定義",
            f"保存しました: {_definition_label(definition)}",
        )

    def _save_new(self) -> None:
        common = self._form_common()
        self._run_save(
            lambda: self.service.create_user_definition(**common)
        )

    def _save_new_version(self) -> None:
        base = self._selected_definition()
        if base is None:
            return
        common = self._form_common()
        self._run_save(
            lambda: self.service.create_next_version(
                base,
                version=self.service.next_version_label(base),
                **common,
            )
        )

    def _import_directivity(self) -> None:
        definition = self._selected_definition()
        if definition is None:
            return
        adapters = self.service.supported_directivity_adapters()
        if not adapters:
            QMessageBox.information(
                self, "指向性インポート", "対応するインポートアダプターがありません"
            )
            return
        selected, _filter = file_dialog_memory.get_open_file_name(
            self, "指向性ソースを選択", 'equipment.import_directivity'
        )
        if not selected:
            return
        try:
            message = self.service.import_directivity(
                definition_sha256=definition.semantic_sha256,
                file_path=Path(selected),
                adapter_id=adapters[0].adapter_id,
            )
        except (ValueError, OSError) as exc:
            QMessageBox.warning(
                self,
                "指向性インポート",
                f"インポートできませんでした · {operation_error_message(exc)}",
            )
            return
        # The import published a new immutable version — republish the list
        # so pickers/preview reflect it immediately instead of a stale row.
        self.refresh_definitions()
        self.definitionsChanged.emit()
        QMessageBox.information(self, "指向性インポート", message)


__all__ = [
    "EquipmentLibraryDialog",
    "EquipmentLibraryService",
    "capability_preview",
]
