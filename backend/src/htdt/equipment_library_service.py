"""Equipment library domain service — Qt-free (#807 boundary refactor).

The authoring/publish/import workflows of the equipment library are domain
operations on the ``cad_equipment*`` / ``cad_directivity*`` authorities; the
Qt dialog hosting them stays in ``equipment_library``, which imports this
module.
"""

from __future__ import annotations

import csv
import io
import json

from hashlib import (
    sha256,
)
from pathlib import (
    Path,
)
from typing import (
    Sequence,
)
from uuid import (
    uuid4,
)
from .clock import (
    utc_now_iso as _utc_now,
)
from .cad_directivity import (
    NORMALIZED_JSON_ADAPTER_ID,
    NormalizedDirectivityJsonV1,
)
from .cad_directivity_import import (
    _parse_metadata_and_rows,
    DIRECTIVITY_ADAPTER_REGISTRY,
    import_directivity_asset,
    POLAR_TABLE_ADAPTER_ID,
)
from .cad_directivity_repository import (
    CadDirectivityRepository,
)
from .cad_equipment import (
    AngleDomain,
    build_equipment_definition,
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
)
from .cad_equipment_evidence import (
    build_equipment_manual_evidence,
)
from .cad_equipment_repository import (
    CadEquipmentRepository,
)
from .cad_repository import (
    SceneRepository,
)
from .cad_scene import (
    Offset3,
    Size3,
)
from .cad_source_response import (
    build_source_response,
    CadSourceResponseRepository,
    SourceFrequencyResponseAuthority,
)
from .cad_system_variant_repository import (
    CadSystemVariantRepository,
)
from .ingress import (
    read_file_bounded,
    strict_ascii_number,
)
from .limits import (
    MAX_ATTACHMENT_BYTES,
)
from .user_facing_error import (
    operation_error_message,
)


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
    ("パッシブラジエーター", "passive_radiator"),
    ("その他", "other"),
)


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
        lines.append("指向性: 不明 — 指向性依存の目的は不明/利用不可になります")
    else:
        lines.append(f"指向性: {tier} ({definition.directivity.data_format})")
    if definition.sensitivity is None:
        lines.append("感度/SPL参照: 未入力 — SPL系の目的は不明になります")
    else:
        lines.append(
            f"感度: {definition.sensitivity.level_db_spl} dB SPL "
            f"({definition.sensitivity.input_quantity} "
            f"{definition.sensitivity.input_value} @ "
            f"{definition.sensitivity.distance_m} m)"
        )
    if definition.spl_capability is None:
        lines.append("最大SPL能力: 未入力 — ヘッドルーム評価は不明になります")
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
        lines.append("設置モード: 未宣言 — 設置コンテキスト評価は不明")
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
                json.loads(raw.decode("utf-8-sig", errors="strict"))
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
                strict_ascii_number(
                    row[columns["frequency_hz"]], field_name='frequency_hz'
                )
            )
            horizontal.append(
                strict_ascii_number(
                    row[columns[horizontal_column]],
                    field_name=horizontal_column,
                )
            )
            vertical.append(
                strict_ascii_number(
                    row[columns[vertical_column]], field_name=vertical_column
                )
            )
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
    raise ValueError(f"未対応のインポートアダプターです: {adapter_id}")


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
        user_label = user_label.strip()
        source_name = source_name.strip()
        source_version = source_version.strip()
        source_reference = source_reference.strip()
        manufacturer = manufacturer.strip() or None if manufacturer else None
        model = model.strip() or None if model else None
        if not user_label:
            raise ValueError("機材ラベルは必須です")
        if not (source_name and source_version and source_reference):
            raise ValueError("出典情報（名称・バージョン・参照）は必須です")
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
        user_label = user_label.strip()
        source_name = source_name.strip()
        source_version = source_version.strip()
        source_reference = source_reference.strip()
        manufacturer = manufacturer.strip() or None if manufacturer else None
        model = model.strip() or None if model else None
        if not user_label:
            raise ValueError("機材ラベルは必須です")
        if not (source_name and source_version and source_reference):
            raise ValueError("出典情報（名称・バージョン・参照）は必須です")
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
            label='指向性ソースファイル',
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
            reason = diagnostic.rejection_reason or "インポート失敗"
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
