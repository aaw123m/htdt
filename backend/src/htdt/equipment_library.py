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
    QFileDialog,
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
from .ingress import read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES
from .user_facing_error import operation_error_message

_EVIDENCE_KINDS: tuple[tuple[str, EquipmentEvidenceKind], ...] = (
    ("ユーザー入力", "user_defined"),
    ("メーカー資料", "manufacturer"),
    ("実測", "measured"),
    ("推定", "inferred"),
    ("解析モデル", "analytic"),
)

_MOUNTING_MODES: tuple[tuple[str, str], ...] = (
    ("未設定（不明）", ""),
    ("自立 / フリースタンディング", "free_standing"),
    ("スタンド", "stand"),
    ("棚 / シェルフ", "shelf"),
    ("壁掛け", "wall"),
    ("天井", "ceiling"),
    ("壁埋込", "in_wall"),
    ("天井埋込", "in_ceiling"),
    ("その他", "custom"),
)

_PORT_TYPES: tuple[tuple[str, str], ...] = (
    ("不明", "unknown"),
    ("密閉", "sealed"),
    ("フロント", "front"),
    ("リア", "rear"),
    ("サイド", "side"),
    ("ダウン", "down"),
    ("パッシブラジエータ", "passive_radiator"),
    ("その他", "other"),
)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _definition_label(definition: EquipmentDefinition) -> str:
    if definition.manufacturer and definition.model:
        name = f"{definition.manufacturer} {definition.model}"
    else:
        name = definition.user_label or definition.definition_id
    return f"{name} (v{definition.version})"


def capability_preview(definition: EquipmentDefinition) -> tuple[str, ...]:
    """Human-readable capability availability for one definition."""
    lines: list[str] = []
    tier = definition.directivity.tier
    if tier == "unknown":
        lines.append("指向性: 不明 — 指向性依存の目的は UNKNOWN/UNAVAILABLE になります")
    else:
        lines.append(f"指向性: {tier} ({definition.directivity.data_format})")
    if definition.sensitivity is None:
        lines.append("感度/SPL参照: 未入力 — SPL系の目的は UNKNOWN になります")
    else:
        lines.append(
            f"感度: {definition.sensitivity.level_db_spl} dB SPL "
            f"({definition.sensitivity.input_quantity} "
            f"{definition.sensitivity.input_value} @ "
            f"{definition.sensitivity.distance_m} m)"
        )
    if definition.spl_capability is None:
        lines.append("最大SPL能力: 未入力 — ヘッドルーム評価は UNKNOWN になります")
    else:
        capability = definition.spl_capability
        lines.append(
            "最大SPL: "
            + (
                f"連続 {capability.continuous_db_spl} / "
                f"ピーク {capability.peak_db_spl} dB SPL"
            )
        )
    if not definition.mounting.mounting_modes:
        lines.append("設置モード: 未宣言 — 設置コンテキスト評価は UNKNOWN")
    else:
        lines.append(
            "設置モード: " + ", ".join(definition.mounting.mounting_modes)
        )
    lines.append(
        f"キャビネット: {definition.cabinet_envelope_m.x_m} x "
        f"{definition.cabinet_envelope_m.y_m} x "
        f"{definition.cabinet_envelope_m.z_m} m"
    )
    return tuple(lines)


def _domain_from_grids(
    frequencies: Sequence[float],
    horizontal: Sequence[float],
    vertical: Sequence[float],
) -> DirectivityDomain:
    return DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=min(frequencies),
            maximum_hz=max(frequencies),
        ),
        horizontal=AngleDomain(
            minimum_deg=min(horizontal),
            maximum_deg=max(horizontal),
        ),
        vertical=AngleDomain(
            minimum_deg=min(vertical),
            maximum_deg=max(vertical),
        ),
    )


def _capability_from_source(
    raw: bytes,
    *,
    adapter_id: str,
) -> DirectivityCapability:
    """Derive the declared directivity capability from the source file itself.

    The source asset's own metadata (kind, domain grids, interpolation,
    phase reference, provenance) is the authority — the UI never asks for
    IDs or hashes manually.
    """
    source_sha = sha256(raw).hexdigest()
    if adapter_id == NORMALIZED_JSON_ADAPTER_ID:
        try:
            source = NormalizedDirectivityJsonV1.model_validate(
                json.loads(raw.decode("utf-8", errors="strict"))
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"指向性JSONの解析に失敗しました: {exc}") from exc
        provenance = EquipmentDataProvenance(
            evidence_kind=source.evidence_kind,
            source_name=source.source_name,
            source_version=source.source_version,
            source_reference=source.source_reference,
            source_sha256=source_sha,
        )
        return DirectivityCapability(
            tier=source.kind,
            data_format="custom",
            provenance=provenance,
            data_asset_sha256=source_sha,
            valid_domain=_domain_from_grids(
                source.frequencies_hz,
                source.horizontal_angles_deg,
                source.vertical_angles_deg,
            ),
            interpolation=InterpolationProvenance(
                method=source.interpolation_method,
                implementation=source.interpolation_implementation,
                implementation_version=source.interpolation_version,
                provenance=provenance,
            ),
            coherent_phase=source.kind == "complex",
            phase_reference=source.phase_reference,
        )
    if adapter_id == POLAR_TABLE_ADAPTER_ID:
        try:
            metadata, table_lines = _parse_metadata_and_rows(raw)
        except ValueError as exc:
            raise ValueError(
                f"指向性テーブルの解析に失敗しました: {exc}"
            ) from exc
        capability = metadata["capability"]
        if capability not in {"magnitude_only", "complex"}:
            raise ValueError("polar_table capability must be magnitude_only or complex")
        provenance = EquipmentDataProvenance(
            evidence_kind=metadata["evidence_kind"],  # type: ignore[arg-type]
            source_name=metadata["source_name"],
            source_version=metadata["source_version"],
            source_reference=metadata["source_reference"],
            source_sha256=source_sha,
        )
        delimiter = "," if metadata["delimiter"] == "csv" else "\t"
        horizontal_column, vertical_column = (
            ("azimuth_deg", "elevation_deg")
            if metadata["angle_semantics"] == "spherical_azimuth_elevation"
            else ("horizontal_angle_deg", "vertical_angle_deg")
        )
        reader = csv.reader(
            io.StringIO("\n".join(table_lines)), delimiter=delimiter
        )
        header = next(reader, None) or ()
        columns = {name: index for index, name in enumerate(header)}
        frequencies: list[float] = []
        horizontal: list[float] = []
        vertical: list[float] = []
        for row in reader:
            if not row:
                continue
            frequencies.append(
                float(row[columns["frequency_hz"]])
            )
            horizontal.append(float(row[columns[horizontal_column]]))
            vertical.append(float(row[columns[vertical_column]]))
        if not frequencies:
            raise ValueError("指向性テーブルにサンプルがありません")
        return DirectivityCapability(
            tier=capability,  # type: ignore[arg-type]
            data_format="polar_table",
            provenance=provenance,
            data_asset_sha256=source_sha,
            valid_domain=_domain_from_grids(frequencies, horizontal, vertical),
            interpolation=InterpolationProvenance(
                method=metadata["interpolation_method"],  # type: ignore[arg-type]
                implementation=metadata["interpolation_implementation"],
                implementation_version=metadata["interpolation_version"],
                provenance=provenance,
            ),
            coherent_phase=capability == "complex",
            phase_reference=metadata.get("phase_reference"),
        )
    raise ValueError(f"未対応のインポートアダプタです: {adapter_id}")


class EquipmentLibraryService:
    """Workflow adapter over the equipment definition authorities."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.equipment_repository = CadEquipmentRepository(
            scene_repository,
            self.variant_repository,
        )
        self.directivity_repository = CadDirectivityRepository(
            scene_repository,
            self.equipment_repository,
        )
        self.source_response_repository = CadSourceResponseRepository(
            scene_repository.path, self.equipment_repository
        )

    def responses_for_definition(
        self,
        definition: EquipmentDefinition,
    ) -> tuple[SourceFrequencyResponseAuthority, ...]:
        """All persisted source-response authorities for one definition (#542)."""
        return tuple(
            response
            for response in self.source_response_repository.list_responses_for_equipment(
                definition.definition_id
            )
            if response.equipment_definition_sha256 == definition.semantic_sha256
        )

    def create_response_authority(
        self,
        definition: EquipmentDefinition,
        **kwargs: object,
    ) -> SourceFrequencyResponseAuthority:
        """Register a sealed frequency-response authority bound to the exact
        definition identity (#542) — capability is declared, never inferred."""
        response = build_source_response(
            equipment_definition=definition,
            **kwargs,
        )
        self.source_response_repository.save_response(response)
        return response

    def definitions(self) -> tuple[EquipmentDefinition, ...]:
        return self.equipment_repository.list_definitions()

    def definition_versions(self, definition_id: str) -> tuple[str, ...]:
        return tuple(
            item.version
            for item in self.definitions()
            if item.definition_id == definition_id
        )

    def next_version_label(self, base: EquipmentDefinition) -> str:
        """Suggested version label for a derived user version."""
        versions = self.definition_versions(base.definition_id)
        return f"{base.version}-u{len(versions) + 1}"

    @staticmethod
    def _provenance(
        *,
        evidence_kind: EquipmentEvidenceKind,
        source_name: str,
        source_version: str,
        source_reference: str,
        source_bytes: bytes | None = None,
    ) -> EquipmentDataProvenance:
        if source_bytes is not None:
            source_sha = sha256(source_bytes).hexdigest()
        else:
            # A manually typed citation has no retained source bytes; the
            # content hash of the typed citation is the honest identifier.
            source_sha = sha256(
                f"{source_name}|{source_version}|{source_reference}".encode(
                    "utf-8"
                )
            ).hexdigest()
        return EquipmentDataProvenance(
            evidence_kind=evidence_kind,
            source_name=source_name,
            source_version=source_version,
            source_reference=source_reference,
            source_sha256=source_sha,
        )

    def create_user_definition(
        self,
        *,
        user_label: str,
        manufacturer: str | None,
        model: str | None,
        width_m: float,
        height_m: float,
        depth_m: float,
        evidence_kind: EquipmentEvidenceKind,
        source_name: str,
        source_version: str,
        source_reference: str,
        actor: str,
        mounting_mode: str | None = None,
        port_type: str = "unknown",
        port_minimum_clearance_m: float | None = None,
        clearance: ClearanceMetadata | None = None,
        acoustic_reference_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
        source_bytes: bytes | None = None,
    ) -> EquipmentDefinition:
        """Author a minimal user-defined EquipmentDefinition.

        SPL/directivity may legitimately stay unknown; the affected
        objectives then report UNKNOWN instead of inventing numbers.
        Manual evidence authorities are derived and retained for every
        cited provenance claim before the definition is saved.
        """
        provenance = self._provenance(
            evidence_kind=evidence_kind,
            source_name=source_name,
            source_version=source_version,
            source_reference=source_reference,
            source_bytes=source_bytes,
        )
        definition = build_equipment_definition(
            definition_id=f"def-{uuid4().hex[:16]}",
            version="1",
            identity_kind="user_defined",
            provenance=(provenance,),
            cabinet_envelope_m=Size3(
                x_m=width_m, y_m=depth_m, z_m=height_m
            ),
            acoustic_reference_point_m=Offset3(
                x_m=acoustic_reference_m[0],
                y_m=acoustic_reference_m[1],
                z_m=acoustic_reference_m[2],
            ),
            directivity=DirectivityCapability(
                tier="unknown",
                data_format="unknown",
                provenance=provenance,
            ),
            manufacturer=manufacturer,
            model=model,
            user_label=user_label,
            mounting=(
                MountingMetadata(mounting_modes=(mounting_mode,))
                if mounting_mode
                else MountingMetadata()
            ),
            port=PortMetadata(
                port_type=port_type,
                minimum_clearance_m=port_minimum_clearance_m,
            ),
            clearance=clearance or ClearanceMetadata(),
        )
        self._save_with_evidence(definition, actor=actor)
        return definition

    def create_next_version(
        self,
        base: EquipmentDefinition,
        *,
        version: str,
        user_label: str,
        manufacturer: str | None,
        model: str | None,
        width_m: float,
        height_m: float,
        depth_m: float,
        evidence_kind: EquipmentEvidenceKind,
        source_name: str,
        source_version: str,
        source_reference: str,
        actor: str,
        mounting_mode: str | None = None,
        port_type: str = "unknown",
        port_minimum_clearance_m: float | None = None,
        clearance: ClearanceMetadata | None = None,
        acoustic_reference_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
        source_bytes: bytes | None = None,
    ) -> EquipmentDefinition:
        """Publish a new immutable version of an existing definition.

        The base authority is never mutated; the new row shares its
        ``definition_id`` with a different ``version``.
        """
        provenance = self._provenance(
            evidence_kind=evidence_kind,
            source_name=source_name,
            source_version=source_version,
            source_reference=source_reference,
            source_bytes=source_bytes,
        )
        definition = build_equipment_definition(
            definition_id=base.definition_id,
            version=version,
            identity_kind="user_defined",
            provenance=(provenance,),
            cabinet_envelope_m=Size3(
                x_m=width_m, y_m=depth_m, z_m=height_m
            ),
            acoustic_reference_point_m=Offset3(
                x_m=acoustic_reference_m[0],
                y_m=acoustic_reference_m[1],
                z_m=acoustic_reference_m[2],
            ),
            directivity=base.directivity,
            sensitivity=base.sensitivity,
            spl_capability=base.spl_capability,
            manufacturer=manufacturer,
            model=model,
            user_label=user_label,
            mounting=(
                MountingMetadata(mounting_modes=(mounting_mode,))
                if mounting_mode
                else MountingMetadata()
            ),
            port=PortMetadata(
                port_type=port_type,
                minimum_clearance_m=port_minimum_clearance_m,
            ),
            clearance=clearance or ClearanceMetadata(),
        )
        self._save_with_evidence(definition, actor=actor)
        return definition

    def _save_with_evidence(
        self,
        definition: EquipmentDefinition,
        *,
        actor: str,
    ) -> None:
        evidence_items = build_equipment_manual_evidence(
            definition,
            actor=actor,
            recorded_at_utc=_utc_now(),
        )
        for evidence in evidence_items:
            self.equipment_repository.save_evidence(evidence)
        self.equipment_repository.save_definition(definition)

    def supported_directivity_adapters(self):
        return tuple(
            adapter
            for adapter in DIRECTIVITY_ADAPTER_REGISTRY.adapters
            if adapter.support_state == "SUPPORTED"
        )

    def import_directivity(
        self,
        *,
        definition_sha256: str,
        file_path: Path,
        adapter_id: str,
        actor: str = "equipment-library-ui",
    ) -> str:
        """Import a directivity source asset for a persisted definition.

        The import derives the declared capability from the source file's
        own metadata, publishes a new immutable definition version carrying
        it, then binds and persists the normalized dataset. Returns a
        human-readable result line; raises ValueError when the target
        definition is not persisted.
        """
        base = self.equipment_repository.get_definition_by_hash(
            definition_sha256
        )
        if base is None:
            raise ValueError("equipment definition is not persisted")
        adapter = next(
            item
            for item in self.supported_directivity_adapters()
            if item.adapter_id == adapter_id
        )
        raw = read_file_bounded(
            Path(file_path),
            MAX_ATTACHMENT_BYTES,
            label='directivity source file',
        )
        directivity = _capability_from_source(
            raw,
            adapter_id=adapter.adapter_id,
        )
        definition = build_equipment_definition(
            definition_id=base.definition_id,
            version=self.next_version_label(base),
            identity_kind="user_defined",
            provenance=(*base.provenance, directivity.provenance),
            cabinet_envelope_m=base.cabinet_envelope_m,
            acoustic_reference_point_m=base.acoustic_reference_point_m,
            directivity=directivity,
            sensitivity=base.sensitivity,
            spl_capability=base.spl_capability,
            manufacturer=base.manufacturer,
            model=base.model,
            user_label=base.user_label,
            mounting=base.mounting,
            port=base.port,
            clearance=base.clearance,
            uncertainty=base.uncertainty,
        )
        result = import_directivity_asset(
            raw_source_bytes=raw,
            explicit_source_format=adapter.accepted_source_format,
            declared_schema=adapter.accepted_schema,
            equipment_definition=definition,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
        )
        diagnostic = result.diagnostic
        if diagnostic.import_state != "IMPORTED" or result.dataset is None:
            reason = diagnostic.rejection_reason or "import failed"
            return f"{diagnostic.import_state}: {reason}"
        self._save_with_evidence(definition, actor=actor)
        self.directivity_repository.save_dataset(
            result.dataset,
            source_bytes=raw,
            source_filename=Path(file_path).name,
            declared_schema=adapter.accepted_schema,
        )
        return (
            f"IMPORTED: {result.dataset.dataset_id} "
            f"(sha256 {diagnostic.source_sha256[:12]}…) "
            f"→ 新バージョン v{definition.version}"
        )


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
        self.definition_list.setAccessibleName("EquipmentDefinitions")
        self.definition_list.currentRowChanged.connect(self._selection_changed)
        body.addWidget(self.definition_list, 1)

        form_host = QWidget()
        form = QFormLayout(form_host)
        body.addWidget(form_host, 2)

        self.label_edit = QLineEdit()
        self.label_edit.setAccessibleName("EquipmentUserLabel")
        form.addRow("ラベル（必須）", self.label_edit)
        self.manufacturer_edit = QLineEdit()
        form.addRow("メーカー", self.manufacturer_edit)
        self.model_edit = QLineEdit()
        form.addRow("モデル", self.model_edit)

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
        source_file_row = QHBoxLayout()
        self.source_file_label = QLabel("（なし — 入力参照のハッシュを使用）")
        self.source_file_button = QPushButton("出典ファイルを添付…")
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
        self.save_new_button.clicked.connect(self._save_new)
        self.save_version_button = QPushButton("選択中の新バージョンとして保存")
        self.save_version_button.clicked.connect(self._save_new_version)
        self.import_button = QPushButton("指向性データをインポート…")
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

    def refresh_definitions(self) -> None:
        self.definition_list.clear()
        for definition in self.service.definitions():
            item = QListWidgetItem(_definition_label(definition))
            item.setData(Qt.ItemDataRole.UserRole, definition.semantic_sha256)
            self.definition_list.addItem(item)

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

    def _attach_source_file(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self, "出典ファイルを選択"
        )
        if not selected:
            return
        path = Path(selected)
        self._source_bytes = read_file_bounded(
            path,
            MAX_ATTACHMENT_BYTES,
            label='source file',
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
                self, "指向性インポート", "対応するインポートアダプタがありません"
            )
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self, "指向性ソースを選択"
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
        QMessageBox.information(self, "指向性インポート", message)


__all__ = [
    "EquipmentLibraryDialog",
    "EquipmentLibraryService",
    "capability_preview",
]
